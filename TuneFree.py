import sys
import os
import re
import html
import time
import json
import math
import locale
import shutil
import socket
import subprocess
import zipfile
import threading
import urllib.request
import webbrowser
from pathlib import Path
from PySide6.QtCore import *
from PySide6.QtWidgets import *
from PySide6.QtGui import *


AFDIAN_URL = "https://ifdian.net/a/TuneFree"
SUPPORT_IMG_NAMES = ("赞赏码.png", "docs/support.png", "support.png")


# -------------------- 中文排序 --------------------
def _init_collate():
    """让「歌曲」「歌手」按拼音排，而不是按 Unicode 码位乱序。"""
    for name in ("Chinese_China.54936", "Chinese (Simplified)_China.936", ""):
        try:
            got = locale.setlocale(locale.LC_COLLATE, name)
        except Exception:
            continue
        if got and got.upper() not in ("C", "POSIX"):
            return True
    return False


# 只在导入时设一次 locale，中文排序靠它；结果本身后面用不到
_init_collate()

_COLLATE_CACHE = {}


def collate(text):
    """把字符串转成可比较的排序键（中文按拼音）。"""
    text = text or ""
    hit = _COLLATE_CACHE.get(text)
    if hit is not None:
        return hit
    try:
        key = locale.strxfrm(text)
    except Exception:
        key = text.lower()
    if len(_COLLATE_CACHE) > 20000:
        _COLLATE_CACHE.clear()
    _COLLATE_CACHE[text] = key
    return key


# -------------------- 后端与播放 --------------------
import locale
import logging
import logging.handlers
import tempfile

import lyrics
import main
from player import Player
import scanner
import session as qsession
from hotkeys import MediaKeyHook
from miniplayer import MiniPlayer



# -------------------- 项目信息 --------------------
try:  # 版本号统一放在 version.py，老版本打包里没有这个文件就退回内置值
    from version import (
        VERSION, GITHUB_URL, GITHUB_RELEASES_URL, GITHUB_LATEST_API,
        GITHUB_LATEST_PAGE, VERSION_JSON_URLS, QQ_GROUP, DOWNLOAD_URL,
        SITE_URL, SITE_PRIMARY, SITE_PAGE,
    )
except Exception:  # pragma: no cover
    VERSION = "3.1.2"
    DOWNLOAD_URL = ""
    SITE_URL = "https://zksbx.github.io/TuneFree/"
    SITE_PRIMARY = SITE_URL
    SITE_PAGE = SITE_PRIMARY.rstrip("/") + "/tunefree.html"
    GITHUB_REPO = "zksbx/TuneFree"
    GITHUB_URL = "https://github.com/" + GITHUB_REPO
    GITHUB_RELEASES_URL = GITHUB_URL + "/releases"
    GITHUB_LATEST_API = "https://api.github.com/repos/" + GITHUB_REPO + "/releases/latest"
    GITHUB_LATEST_PAGE = GITHUB_RELEASES_URL + "/latest"
    VERSION_JSON_URLS = [
        "https://cdn.jsdelivr.net/gh/" + GITHUB_REPO + "@main/version.json",
        "https://raw.githubusercontent.com/" + GITHUB_REPO + "/main/version.json",
    ]
    QQ_GROUP = "1067975918"

# 设置页「使用帮助」打开官网的帮助章节（docs/tunefree.html 里的 #help）
HELP_URL = SITE_PAGE + "#help"

# 赞赏 / 赞助相关（不是下载渠道，别和 DOWNLOAD_URL 混）
AFDIAN_URL = "https://ifdian.net/a/TuneFree"
# 赞赏码图片可能放的几个位置，按顺序找
SUPPORT_IMG_NAMES = ("赞赏码.png", "docs/support.png", "support.png")


# -------------------- 工具函数 --------------------
def app_dir():
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).parent
    return Path(__file__).parent


def log_path():
    """日志文件：%LOCALAPPDATA%\\TuneFree\\logs\\tunefree.log（打包前后同一个位置）。"""
    base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    return os.path.join(base, "TuneFree", "logs", "tunefree.log")


def setup_logging():
    """把 logging 同时写到文件和控制台；重复调用无副作用。"""
    root = logging.getLogger()
    if any(isinstance(h, logging.FileHandler) for h in root.handlers):
        return log_path()
    path = log_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=2 * 1024 * 1024, backupCount=1, encoding="utf-8")
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S"))
        handler.setLevel(logging.INFO)
        root.addHandler(handler)
        root.setLevel(logging.INFO)
    except Exception:
        pass
    if not any(isinstance(h, logging.StreamHandler) for h in root.handlers):
        root.addHandler(logging.StreamHandler())
    logging.info("===== TuneFree %s 启动 =====", VERSION)
    return path


def resource_dirs():
    """找随程序附带的文件（图标、图片、说明）：先看 exe 同级，再看打包出来的 _internal 目录。"""
    dirs = [app_dir()]
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        d = Path(meipass)
        if d not in dirs:
            dirs.append(d)
    return dirs


def app_icon():
    """程序图标：ICO 优先（多尺寸，标题栏/任务栏都清晰），没有就退回 PNG。"""
    for base in resource_dirs():
        for name in ("tunefree.ico", "tunefree.png"):
            path = base / name
            if not path.is_file():
                continue
            icon = QIcon(str(path))
            if not icon.isNull():
                return icon
    return None


def make_settings():
    """优先写注册表；注册表写不了时退回 ini 文件，保证主题/路径真的能被记住。"""
    native = QSettings("TuneFree", "TuneFree")
    try:
        native.setValue("_probe", 1)
        native.remove("_probe")
        native.sync()
        if native.status() == QSettings.NoError:
            return native, ""
    except Exception:
        pass

    candidates = [app_dir() / "TuneFree.ini"]
    cfg = QStandardPaths.writableLocation(QStandardPaths.AppConfigLocation)
    if cfg:
        candidates.append(Path(cfg) / "TuneFree.ini")
    for path in candidates:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            alt = QSettings(str(path), QSettings.IniFormat)
            alt.setValue("_probe", 1)
            alt.remove("_probe")
            alt.sync()
            if alt.status() == QSettings.NoError:
                return alt, str(path)
        except Exception:
            continue
    return native, ""


def explain_exit_code(code):
    """把后端异常返回码翻成能照着做的话；已经是人话的消息就原样返回。"""
    try:
        code = int(code)
    except (TypeError, ValueError):
        return str(code or "").strip()
    if code in (4294967283, -13):
        return ("拒绝访问(EACCES)。常见原因：杀毒软件拦下了程序，或输出文件正被"
                "播放器/网盘同步占用。把 TuneFree 整个文件夹加入杀软白名单再试一次。")
    if code in (4294967295, -1):
        return "后端异常退出，通常是 QQ音乐版本不匹配或注入被拦截，请看日志里的报错行。"
    if code in (4294967294, -2):
        return "找不到文件，可能解密产物被移走，或输入目录路径失效。"
    if code > 2147483647:
        return f"系统层错误(0x{code:08X})，多半是杀毒软件/安全软件拦截了程序运行。"
    return ""


# -------------------- 更新检查 --------------------
def parse_version(text):
    """把 v2.1.2 / 2.1.2-beta 这类字符串换成能比较大小的元组。"""
    nums = re.findall(r"\d+", str(text or ""))
    if not nums:
        return (0,)
    return tuple(int(x) for x in nums[:3])


def _http_json(url, timeout):
    req = urllib.request.Request(url, headers={
        "User-Agent": "TuneFree/" + VERSION,
        "Accept": "application/vnd.github+json, application/json",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def fetch_latest_release(timeout=4.0):
    """查最新版本，返回 (版本号, 链接, 更新说明)；全都拿不到就返回 None。

    先问 GitHub Releases，连不上再退到 jsDelivr 上的 version.json——
    国内不挂梯子访问 api.github.com 经常超时，备用源是给这种用户兜底的。
    """
    try:
        data = _http_json(GITHUB_LATEST_API, timeout)
        tag = data.get("tag_name") or ""
        if tag:
            return (str(tag).lstrip("vV"),
                    data.get("html_url") or GITHUB_LATEST_PAGE,
                    (data.get("body") or "").strip())
    except Exception:
        pass

    for url in VERSION_JSON_URLS:
        try:
            data = _http_json(url, timeout)
            ver = data.get("version") or ""
            if ver:
                return (str(ver).lstrip("vV"),
                        data.get("url") or GITHUB_LATEST_PAGE,
                        (data.get("notes") or "").strip())
        except Exception:
            continue
    return None


class UpdateChecker(QObject):
    """后台线程里查版本，结果用信号发回界面线程，绝不让界面卡住。"""

    found = Signal(str, str, str)   # 版本号, 链接, 更新说明
    failed = Signal(str)            # 失败原因

    def check(self, timeout=4.0):
        threading.Thread(target=self._run, args=(timeout,), daemon=True).start()

    def _run(self, timeout):
        try:
            info = fetch_latest_release(timeout)
        except Exception as e:  # 兜底，线程里绝不能抛出去
            self.failed.emit(str(e) or "未知错误")
            return
        if info:
            self.found.emit(info[0], info[1], info[2])
        else:
            self.failed.emit("连不上更新服务器")


# -------------------- 自动更新 --------------------
def can_self_update():
    """只有打包后的 exe 才能原地覆盖自己；源码运行就老老实实跳浏览器。"""
    return bool(getattr(sys, "frozen", False)) and os.name == "nt"


def fetch_release_zip(version, timeout=6.0):
    """找指定版本的 zip 包地址：GitHub Releases 的 assets 里挑第一个 .zip。"""
    try:
        data = _http_json(GITHUB_LATEST_API, timeout)
    except Exception:
        return ""
    tag = str(data.get("tag_name") or "").lstrip("vV")
    if tag and parse_version(tag) != parse_version(version):
        return ""
    for asset in data.get("assets") or []:
        name = str(asset.get("name") or "")
        if name.lower().endswith(".zip"):
            return str(asset.get("browser_download_url") or "")
    return ""


def download_file(url, target, on_progress=None, cancel=None, timeout=30):
    """流式下载，边下边报进度；返回 True 表示拿到了完整文件。"""
    req = urllib.request.Request(url, headers={"User-Agent": "TuneFree/" + VERSION})
    done = 0
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            total = int(resp.headers.get("Content-Length") or 0)
            with open(target, "wb") as fh:
                while True:
                    if cancel is not None and cancel():
                        return False
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    fh.write(chunk)
                    done += len(chunk)
                    if on_progress is not None:
                        on_progress(done, total)
    except Exception as e:
        logging.warning("下载更新包失败: %s", e)
        return False
    return done > 0


def stage_update(zip_path, version):
    """把更新包解到临时目录，返回 TuneFree.exe 所在的那层目录。"""
    root = os.path.join(cache_root(), "update", version)
    if os.path.isdir(root):
        shutil.rmtree(root, ignore_errors=True)
    os.makedirs(root, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        bad = zf.testzip()
        if bad:
            raise ValueError("压缩包损坏：%s" % bad)
        zf.extractall(root)
    if os.path.isfile(os.path.join(root, "TuneFree.exe")):
        return root
    for name in os.listdir(root):
        sub = os.path.join(root, name)
        if os.path.isdir(sub) and os.path.isfile(os.path.join(sub, "TuneFree.exe")):
            return sub
    raise ValueError("更新包里没找到 TuneFree.exe")


def write_update_script(source_dir, target_dir, exe_name="TuneFree.exe"):
    """写一个「等本进程退出后覆盖安装」的 bat，返回脚本路径。

    Windows 不让运行中的 exe 覆盖自己，只能把活交给外部脚本：
    死等本进程 PID 消失 → robocopy 覆盖 → 启动新版 → 清理临时文件。
    bat 里有中文路径，必须 GBK + CRLF，否则 cmd 会乱码。
    """
    import tempfile

    pid = os.getpid()
    script = os.path.join(tempfile.gettempdir(), "TuneFree_update_%d.bat" % pid)
    lines = [
        "@echo off",
        "chcp 936 >nul",
        ":wait",
        'tasklist /FI "PID eq %d" 2>nul | find "%d" >nul' % (pid, pid),
        "if not errorlevel 1 (",
        "  timeout /t 1 /nobreak >nul",
        "  goto wait",
        ")",
        'robocopy "%s" "%s" /E /IS /IT /R:2 /W:1 /NFL /NDL /NJH /NJS /NC /NS >nul'
        % (source_dir, target_dir),
        'start "" "%s"' % os.path.join(target_dir, exe_name),
        'rd /s /q "%s"' % source_dir,
        'del "%~f0"',
    ]
    with open(script, "w", encoding="gbk", newline="\r\n") as fh:
        fh.write("\n".join(lines) + "\n")
    return script


class UpdateDownloader(QObject):
    """后台把更新包下下来并解压，进度用信号回界面线程。"""

    progress = Signal(int, int)     # 已下载字节, 总字节（0 表示未知）
    stage = Signal(str)             # 当前在干什么
    finished = Signal(bool, str)    # 成功?, 解压后的目录 / 失败原因

    def __init__(self, version, zip_path, parent=None):
        super().__init__(parent)
        self.version = version
        self.zip_path = zip_path
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        self.stage.emit("正在查找更新包…")
        zip_url = fetch_release_zip(self.version)
        if not zip_url:
            self.finished.emit(False, "找不到安装包地址，可能是网络被挡住了")
            return
        self.stage.emit("正在下载…")
        ok = download_file(
            zip_url, self.zip_path,
            on_progress=lambda done, total: self.progress.emit(done, total),
            cancel=lambda: self._cancel,
            timeout=45,
        )
        if self._cancel:
            self.finished.emit(False, "已取消")
            return
        if not ok:
            self.finished.emit(False, "下载失败，检查网络后再试一次")
            return
        self.stage.emit("正在解压…")
        try:
            source = stage_update(self.zip_path, self.version)
        except Exception as e:
            self.finished.emit(False, str(e) or "解压失败")
            return
        self.finished.emit(True, source)


# -------------------- 样式表 --------------------
LIGHT_STYLESHEET = """
QMainWindow, QWidget#central, QWidget#body { background: #f7f7f4; }

QDialog { background: #ffffff; color: #26251e; }
QDialog QLabel { color: #26251e; }

QFrame#topBar {
    background: #ffffff;
    border-bottom: 1px solid #e6e5e0;
}
QLabel#metaText { color: #807d72; font-size: 9.5pt; }

QPushButton#folderBtn {
    background: #fafaf7;
    border: 1px solid #e6e5e0;
    border-radius: 10px;
    color: #3f3d35;
    padding: 0 12px;
    font-size: 9.5pt;
    text-align: left;
}
QPushButton#folderBtn:hover { background: #ffffff; border-color: #cfcdc4; }

QPushButton#startBtn {
    background: #2f6bff;
    border: none;
    border-radius: 10px;
    color: #ffffff;
    font-size: 10pt;
    font-weight: 600;
    padding: 0 18px;
}
QPushButton#startBtn:hover { background: #2459e0; }
QPushButton#startBtn:pressed { background: #1d49c2; }
QPushButton#startBtn:disabled { background: #c9d5ee; color: #ffffff; }

QPushButton#ghostBtn {
    background: transparent;
    border: 1px solid transparent;
    border-radius: 10px;
    color: #5a5852;
    font-size: 9.5pt;
    padding: 0 10px;
}
QPushButton#ghostBtn:hover { background: #f1f0ea; color: #26251e; }
QPushButton#ghostBtn:pressed { background: #e9e8e1; }
QPushButton#ghostBtn:checked { background: #fdece5; color: #cf4a12; }

QLabel#connPill {
    background: #f4f3ee;
    border: 1px solid #e6e5e0;
    border-radius: 14px;
    color: #807d72;
    font-size: 9pt;
    padding: 3px 10px;
}
QLabel#connPill[ok="true"] { background: #edf5f1; border-color: #cfe6dc; color: #1f8a65; }
QLabel#connPill[ok="false"] { background: #f4f3ee; border-color: #ddd8cb; color: #807d72; }

QComboBox#bitrate, QComboBox#nameFormat {
    background: #ffffff;
    border: 1px solid #e6e5e0;
    border-radius: 10px;
    color: #3f3d35;
    font-size: 9pt;
    padding: 0 6px 0 11px;
    min-width: 74px;
}
QComboBox#bitrate:hover, QComboBox#nameFormat:hover { border-color: #cfcdc4; }
QComboBox#bitrate::drop-down { border: none; width: 16px; }
QComboBox#nameFormat::drop-down { border: none; width: 22px; }
QComboBox#nameFormat::down-arrow { image: url({CARET}); width: 11px; height: 11px; }
QComboBox#bitrate QAbstractItemView, QComboBox#nameFormat QAbstractItemView {
    background: #ffffff;
    border: 1px solid #e6e5e0;
    border-radius: 10px;
    color: #3f3d35;
    outline: 0;
    padding: 4px;
    selection-background-color: #fdece5;
    selection-color: #cf4a12;
}

QTreeWidget#sideTree {
    background: #ffffff;
    border: none;
    border-right: 1px solid #e6e5e0;
    color: #3f3d35;
    font-size: 9.5pt;
    outline: 0;
    padding: 8px 6px;
}
QWidget#listWrap { background: #f7f7f4; }

QListView#trackView {
    background: #ffffff;
    border: none;
    outline: 0;
}
QListView#trackView::item { border: none; }

QFrame#drawer {
    background: #ffffff;
    border-left: 1px solid #e6e5e0;
}
QLabel#drawerTitle { color: #26251e; font-size: 10pt; font-weight: 600; }
QLabel#drawerHint { color: #8b877c; font-size: 8.5pt; }
QLabel#fieldLabel { color: #5a5852; font-size: 9pt; }

QLineEdit#search, QLineEdit#numInput, QSpinBox#numInput {
    background: #fafaf7;
    border: 1px solid #e6e5e0;
    border-radius: 10px;
    color: #26251e;
    font-size: 9.5pt;
    padding: 6px 9px;
    selection-background-color: #26251e;
    selection-color: #ffffff;
}
QLineEdit#search:focus, QSpinBox#numInput:focus { border-color: #b9b5a8; background: #ffffff; }

QCheckBox { color: #3f3d35; font-size: 9.5pt; spacing: 7px; }
QCheckBox::indicator {
    width: 15px; height: 15px;
    border: 1px solid #cfcdc4;
    border-radius: 5px;
    background: #ffffff;
}
QCheckBox::indicator:hover { border-color: #cf4a12; }
QCheckBox::indicator:checked { background: #cf4a12; border-color: #cf4a12; image: url({CHECK}); }

QPushButton#primaryBtn {
    background: #26251e;
    border: none;
    border-radius: 10px;
    color: #ffffff;
    font-size: 9.5pt;
    font-weight: 600;
    padding: 7px 14px;
}
QPushButton#primaryBtn:hover { background: #3b3930; }
QPushButton#secondaryBtn {
    background: #fafaf7;
    border: 1px solid #e6e5e0;
    border-radius: 10px;
    color: #4a4841;
    font-size: 9.5pt;
    padding: 7px 14px;
}
QPushButton#secondaryBtn:hover { background: #f1f0ea; }

QFrame#playerBar {
    background: #ffffff;
    border-top: 1px solid #e6e5e0;
}
QLabel#nowCover {
    background: #eeece4;
    border-radius: 12px;
}
QFrame#divider { background: #e6e5e0; border: none; }
QLabel#nowTitle { color: #26251e; font-size: 10pt; font-weight: 600; }
QLabel#nowArtist { color: #8b877c; font-size: 8.5pt; }

QPushButton#playBtn {
    background: #26251e;
    border: none;
    border-radius: 20px;
    color: #ffffff;
}
QPushButton#playBtn:hover { background: #3b3930; }
QPushButton#roundBtn {
    background: transparent;
    border: none;
    border-radius: 16px;
    color: #5a5852;
    font-family: "Segoe UI Symbol";
    font-size: 11pt;
}
QPushButton#roundBtn:hover { background: #f1f0ea; color: #26251e; }

QLabel#timeText { color: #8b877c; font-size: 8.5pt; }

QSlider#seek::groove:horizontal, QSlider#volume::groove:horizontal {
    height: 4px;
    background: #e6e5e0;
    border-radius: 2px;
}
QSlider#seek::sub-page:horizontal, QSlider#volume::sub-page:horizontal {
    background: #26251e;
    border-radius: 2px;
}
QSlider#volume::sub-page:horizontal { background: #c9c4b6; }
QSlider#seek::handle:horizontal, QSlider#volume::handle:horizontal {
    width: 12px; height: 12px;
    margin: -4px 0;
    border-radius: 6px;
    background: #ffffff;
    border: 1px solid #b9b5a8;
}
QSlider#seek::handle:horizontal:hover, QSlider#volume::handle:horizontal:hover {
    border-color: #26251e;
}

QScrollBar:vertical {
    background: transparent;
    width: 10px;
    margin: 2px 2px 2px 0;
}
QScrollBar::handle:vertical {
    background: #dbd8ce;
    border-radius: 5px;
    min-height: 28px;
}
QScrollBar::handle:vertical:hover { background: #c6c2b6; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
QScrollBar:horizontal {
    background: transparent;
    height: 10px;
    margin: 0 2px 2px 2px;
}
QScrollBar::handle:horizontal {
    background: #dbd8ce;
    border-radius: 5px;
    min-width: 28px;
}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal { background: transparent; }

/* 侧边栏的滚动条：细一点、颜色压下去，别跟导航抢视线 */
QTreeWidget#sideTree QScrollBar:vertical {
    background: transparent;
    width: 7px;
    margin: 4px 2px 4px 0;
    border: none;
}
QTreeWidget#sideTree QScrollBar::handle:vertical {
    background: #d5cfc0;
    border-radius: 3px;
    min-height: 32px;
}
QTreeWidget#sideTree QScrollBar::handle:vertical:hover { background: #b7b0a0; }
QTreeWidget#sideTree QScrollBar::handle:vertical:pressed { background: #a29b8a; }
QTreeWidget#sideTree QScrollBar::add-line:vertical,
QTreeWidget#sideTree QScrollBar::sub-line:vertical { height: 0; background: none; border: none; }
QTreeWidget#sideTree QScrollBar::add-page:vertical,
QTreeWidget#sideTree QScrollBar::sub-page:vertical { background: none; }

QToolTip {
    background: #26251e;
    color: #ffffff;
    border: none;
    border-radius: 7px;
    padding: 5px 9px;
    font-size: 9pt;
}
"""

UI_EXTRA_LIGHT = """
QLabel#selCount { color: #cf4a12; font-size: 8.4pt; font-weight: 600; }
QTreeView::indicator, QTreeWidget::indicator {
    width: 13px; height: 13px; border: 1px solid #cfcdc4; border-radius: 4px; background: #ffffff;
}
QTreeView::indicator:checked, QTreeWidget::indicator:checked {
    background: #26251e; border-color: #26251e; image: url({CHECK});
}
QTreeView::indicator:indeterminate, QTreeWidget::indicator:indeterminate {
    background: #a8a396; border-color: #a8a396; image: url({DASH});
}
QFrame#optPanel { background: #ffffff; border: 1px solid #e6e5e0; border-radius: 14px; }
QLabel#optTitle { color: #26251e; font-size: 10pt; font-weight: 600; }
QLabel#optHint { color: #8b877c; font-size: 8.4pt; }
QRadioButton { color: #3f3d35; font-size: 9.2pt; spacing: 7px; padding: 2px 0; }
QRadioButton::indicator {
    width: 14px; height: 14px; border: 1px solid #cfcdc4; border-radius: 7px; background: #ffffff;
}
QRadioButton::indicator:checked {
    border: 1px solid #cf4a12;
    background: qradialgradient(cx:0.5, cy:0.5, radius:0.5, fx:0.5, fy:0.5,
        stop:0 #ffffff, stop:0.34 #ffffff, stop:0.44 #cf4a12, stop:1 #cf4a12);
}

QMessageBox { background: #f7f7f4; }
QMessageBox QLabel { color: #26251e; }
QMessageBox QPushButton {
    background: #f2f1ec;
    color: #26251e;
    border: 1px solid #e0ded6;
    border-radius: 8px;
    padding: 6px 18px;
    min-width: 68px;
    min-height: 20px;
}
QMessageBox QPushButton:hover { background: #e9e8e1; }
QMessageBox QPushButton:pressed { background: #dedcd4; }

QMenu {
    background: #ffffff;
    color: #26251e;
    border: 1px solid #e6e5e0;
    border-radius: 10px;
    padding: 6px;
}
QMenu::item {
    padding: 7px 18px;
    border-radius: 6px;
    font-size: 9pt;
}
QMenu::item:selected { background: #f2f1ec; }
QMenu::item:disabled { color: #b7b3a7; }
QMenu::separator { height: 1px; background: #efece5; margin: 5px 10px; }

/* 设置页 */
QDialog#settingsDlg { background: #f7f7f4; }
QScrollArea#setScroll { background: transparent; border: none; }
QWidget#setBody { background: transparent; }
QFrame#setCard { background: #ffffff; border: 1px solid #eceae2; border-radius: 12px; }
QLabel#setCap { color: #26251e; font-size: 10pt; font-weight: 600; }
QLabel#setHint { color: #8b877c; font-size: 8.2pt; }
QLabel#setLabel { color: #3f3d35; font-size: 8.8pt; }
QLabel#setValue { color: #8b877c; font-size: 8.8pt; }
QPushButton#schemeChip {
    background: #fafaf7;
    border: 1px solid #e6e5e0;
    border-radius: 9px;
    color: #5a5852;
    font-size: 9pt;
    font-weight: 600;
}
QPushButton#schemeChip:hover { background: #f2f1ec; }
QPushButton#schemeChip:checked {
    background: #fdece5;
    border-color: #f0c9b5;
    color: #cf4a12;
}
QSlider#setSlider::groove:horizontal { height: 4px; background: #eceae2; border-radius: 2px; }
QSlider#setSlider::sub-page:horizontal { background: #cf4a12; border-radius: 2px; }
QSlider#setSlider::handle:horizontal {
    width: 14px;
    height: 14px;
    margin: -5px 0;
    border-radius: 7px;
    background: #ffffff;
    border: 1px solid #cfcdc4;
}
QSlider#setSlider::handle:horizontal:hover { border-color: #cf4a12; }
/* 卡片里的实心小按钮：「检查更新」「使用帮助」得看得出能点 */
QPushButton#setBtn {
    background: #ffffff;
    border: 1px solid #e2e0d8;
    border-radius: 8px;
    color: #3f3d35;
    font-size: 9pt;
    padding: 0 13px;
}
QPushButton#setBtn:hover { background: #faf9f5; border-color: #cfcdc4; color: #26251e; }
QPushButton#setBtn:pressed { background: #f1f0ea; }
QPushButton#setBtn:disabled { color: #b3afa4; border-color: #ecebe4; background: #fbfaf7; }

/* 更新弹窗：说明用小字号，进度条跟设置页滑块一个调子 */
QDialog#updateDlg { background: #ffffff; }
QLabel#updateNotes { color: #26251e; font-size: 9.4pt; }
QProgressBar#updateBar { background: #f2f1ec; border: none; border-radius: 3px; }
QProgressBar#updateBar::chunk { background: #cf4a12; border-radius: 3px; }

/* ---- 晨雾：淡蓝渐变页面，侧栏与列表面板是半透明白卡片 ---- */
QWidget#central { background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #f9fbff, stop:0.55 #eef2f9, stop:1 #e9eefa); }
QFrame#topBar { background: rgba(255, 255, 255, 0.86); border-bottom: 1px solid #e3e7ef; }
QFrame#listWrap { background: rgba(255, 255, 255, 0.90); }
QFrame#playerBar { background: rgba(255, 255, 255, 0.90); border-top: 1px solid #e3e7ef; }
"""

UI_EXTRA_DARK = """
QLabel#sideCap { color: #75797d; font-size: 8.4pt; font-weight: 600; letter-spacing: 1px; }
QFrame#selBar { background: #131d26; border-radius: 8px; }
QLabel#selCount { color: #57c1ff; font-size: 8.4pt; font-weight: 600; }
QPushButton#iconBtn { background: transparent; border: none; border-radius: 9px; color: #9c9c9d; font-size: 12pt; }
QPushButton#iconBtn:hover { background: #17181b; color: #f4f4f6; }
QPushButton#iconBtn:pressed { background: #202224; }
QPushButton#starBtn, QPushButton#supportBtn {
    background: #121212;
    border: 1px solid #242728;
    border-radius: 10px;
    color: #a9aaac;
    font-size: 9.5pt;
    padding: 0 11px;
}
QPushButton#starBtn:hover { background: #1e1a12; border-color: #3b3320; color: #ffc533; }
QPushButton#supportBtn:hover { background: #1e1419; border-color: #3c2229; color: #ff8fa8; }
QLabel#supportTitle { color: #f4f4f6; font-size: 13pt; font-weight: 600; }
QLabel#supportSub { color: #9c9c9d; font-size: 9pt; }
QLabel#supportDesc { color: #cdcdcd; font-size: 9pt; }
QFrame#supportCard { background: #17181b; border: 1px solid #242728; border-radius: 14px; }
QLabel#fieldCap { color: #75797d; font-size: 8pt; }
QLabel#changeHint { color: #5f6367; font-size: 8pt; }
QTreeWidget#sideTree { background: transparent; border: none; outline: none; font-size: 9.2pt; color: #cdcdcd; padding: 2px 0px; }
QTreeWidget#sideTree::branch { background: transparent; border: none; }
QTreeView::indicator, QTreeWidget::indicator {
    width: 13px; height: 13px; border: 1px solid #34383b; border-radius: 4px; background: #121212;
}
QTreeView::indicator:checked, QTreeWidget::indicator:checked {
    background: #ffffff; border-color: #ffffff; image: url({CHECK});
}
QTreeView::indicator:indeterminate, QTreeWidget::indicator:indeterminate {
    background: #5f6367; border-color: #5f6367; image: url({DASH});
}
QFrame#optPanel { background: #121212; border: 1px solid #242728; border-radius: 14px; }
QLabel#optTitle { color: #f4f4f6; font-size: 10pt; font-weight: 600; }
QLabel#optHint { color: #75797d; font-size: 8.4pt; }
QRadioButton { color: #cdcdcd; font-size: 9.2pt; spacing: 7px; padding: 2px 0; }
QRadioButton::indicator {
    width: 14px; height: 14px; border: 1px solid #34383b; border-radius: 7px; background: #121212;
}
QRadioButton::indicator:checked {
    border: 1px solid #57c1ff;
    background: qradialgradient(cx:0.5, cy:0.5, radius:0.5, fx:0.5, fy:0.5,
        stop:0 #121212, stop:0.34 #121212, stop:0.44 #57c1ff, stop:1 #57c1ff);
}

QMessageBox { background: #0d0d0d; }
QMessageBox QLabel { color: #f4f4f6; }
QMessageBox QPushButton {
    background: #17181b;
    color: #f4f4f6;
    border: 1px solid #262a2e;
    border-radius: 8px;
    padding: 6px 18px;
    min-width: 68px;
    min-height: 20px;
}
QMessageBox QPushButton:hover { background: #202227; }
QMessageBox QPushButton:pressed { background: #2a2d33; }

QMenu {
    background: #141518;
    color: #f4f4f6;
    border: 1px solid #262a2e;
    border-radius: 10px;
    padding: 6px;
}
QMenu::item {
    padding: 7px 18px;
    border-radius: 6px;
    font-size: 9pt;
}
QMenu::item:selected { background: #202227; }
QMenu::item:disabled { color: #5f6469; }
QMenu::separator { height: 1px; background: #23262a; margin: 5px 10px; }

/* 设置页 */
QDialog#settingsDlg { background: #07080a; }
QFrame#setHead { background: #0d0d0d; border-bottom: 1px solid #1c1e21; }
QLabel#setTitle { color: #f4f4f6; font-size: 15pt; font-weight: 600; }
QLabel#setSub { color: #9c9c9d; font-size: 8.6pt; }
QScrollArea#setScroll { background: transparent; border: none; }
QWidget#setBody { background: transparent; }
QFrame#setCard { background: #121316; border: 1px solid #1f2226; border-radius: 12px; }
QLabel#setCap { color: #f4f4f6; font-size: 10pt; font-weight: 600; }
QLabel#setHint { color: #9c9c9d; font-size: 8.2pt; }
QLabel#setLabel { color: #d3d4d6; font-size: 8.8pt; }
QLabel#setValue { color: #9c9c9d; font-size: 8.8pt; }
QPushButton#schemeChip {
    background: #17181b;
    border: 1px solid #262a2e;
    border-radius: 9px;
    color: #c7c8ca;
    font-size: 9pt;
    font-weight: 600;
}
QPushButton#schemeChip:hover { background: #1e2024; }
QPushButton#schemeChip:checked {
    background: #14242f;
    border-color: #2b4b60;
    color: #57c1ff;
}
QSlider#setSlider::groove:horizontal { height: 4px; background: #24272b; border-radius: 2px; }
QSlider#setSlider::sub-page:horizontal { background: #57c1ff; border-radius: 2px; }
QSlider#setSlider::handle:horizontal {
    width: 14px;
    height: 14px;
    margin: -5px 0;
    border-radius: 7px;
    background: #d8d9db;
    border: 1px solid #3a3f44;
}
QSlider#setSlider::handle:horizontal:hover { border-color: #57c1ff; }
QFrame#setFoot { background: #0d0d0d; border-top: 1px solid #1c1e21; }
/* 卡片里的实心小按钮：「检查更新」「使用帮助」得看得出能点 */
QPushButton#setBtn {
    background: #16181c;
    border: 1px solid #2a2d31;
    border-radius: 8px;
    color: #e8e8ea;
    font-size: 9pt;
    padding: 0 13px;
}
QPushButton#setBtn:hover { background: #1c1f23; border-color: #3a3f44; color: #ffffff; }
QPushButton#setBtn:pressed { background: #202428; }
QPushButton#setBtn:disabled { color: #5c6065; border-color: #1e2023; background: #101113; }

/* 更新弹窗：说明用小字号，进度条跟设置页滑块一个调子 */
QDialog#updateDlg { background: #0d0d0d; }
QLabel#updateNotes { color: #f4f4f6; font-size: 9.4pt; }
QProgressBar#updateBar { background: #17181b; border: none; border-radius: 3px; }
QProgressBar#updateBar::chunk { background: #57c1ff; border-radius: 3px; }

/* ---- 石墨：整窗纯黑，面板是极浅的一层，只用 1px 线分界 ---- */
QWidget#central { background: #000000; }
QFrame#topBar { background: #000000; border-bottom: 1px solid #1b1b1f; }
QFrame#listWrap { background: #0b0b0d; }
QFrame#playerBar { background: #0b0b0d; border-top: 1px solid #1b1b1f; }
"""

DARK_STYLESHEET = """
QMainWindow, QWidget#central, QWidget#body { background: #07080a; }

QDialog { background: #121212; color: #f4f4f6; }
QDialog QLabel { color: #f4f4f6; }

QFrame#topBar {
    background: #0d0d0d;
    border-bottom: 1px solid #1c1e21;
}
QLabel#metaText { color: #9c9c9d; font-size: 9.5pt; }

QPushButton#folderBtn {
    background: #121212;
    border: 1px solid #242728;
    border-radius: 10px;
    color: #cdcdcd;
    padding: 0 12px;
    font-size: 9.5pt;
    text-align: left;
}
QPushButton#folderBtn:hover { background: #17181b; border-color: #34383b; }

QPushButton#startBtn {
    background: #a78bfa;
    border: none;
    border-radius: 10px;
    color: #0b0b0d;
    font-size: 10pt;
    font-weight: 600;
    padding: 0 18px;
}
QPushButton#startBtn:hover { background: #b9a4ff; }
QPushButton#startBtn:pressed { background: #9172f0; }
QPushButton#startBtn:disabled { background: #241d3d; color: #6f6a86; }

QPushButton#ghostBtn {
    background: transparent;
    border: 1px solid transparent;
    border-radius: 10px;
    color: #a9aaac;
    font-size: 9.5pt;
    padding: 0 10px;
}
QPushButton#ghostBtn:hover { background: #17181b; color: #f4f4f6; }
QPushButton#ghostBtn:pressed { background: #202224; }
QPushButton#ghostBtn:checked { background: #14242f; color: #7fd0ff; }

QLabel#connPill {
    background: #121212;
    border: 1px solid #242728;
    border-radius: 14px;
    color: #9c9c9d;
    font-size: 9pt;
    padding: 3px 10px;
}
QLabel#connPill[ok="true"] { background: #10251c; border-color: #1d4635; color: #59d499; }
QLabel#connPill[ok="false"] { background: #121212; border-color: #2e3134; color: #9c9c9d; }


QWidget#listWrap { background: #07080a; }

QListView#trackView {
    background: #0b0c0e;
    border: none;
    outline: 0;
}
QListView#trackView::item { border: none; }

QLabel#drawerHint { color: #75797d; font-size: 8.5pt; }
QLabel#fieldLabel { color: #a9aaac; font-size: 9pt; }

QLineEdit#search, QLineEdit#numInput, QSpinBox#numInput {
    background: #121212;
    border: 1px solid #242728;
    border-radius: 10px;
    color: #f4f4f6;
    font-size: 9.5pt;
    padding: 6px 9px;
    selection-background-color: #ffffff;
    selection-color: #000000;
}
QLineEdit#search:focus, QSpinBox#numInput:focus { border-color: #3d4145; background: #17181b; }

QCheckBox { color: #cdcdcd; font-size: 9.5pt; spacing: 7px; }
QCheckBox::indicator {
    width: 15px; height: 15px;
    border: 1px solid #34383b;
    border-radius: 5px;
    background: #121212;
}
QCheckBox::indicator:hover { border-color: #57c1ff; }
QCheckBox::indicator:checked { background: #57c1ff; border-color: #57c1ff; image: url({CHECK}); }

QPushButton#primaryBtn {
    background: #ffffff;
    border: none;
    border-radius: 10px;
    color: #000000;
    font-size: 9.5pt;
    font-weight: 600;
    padding: 7px 14px;
}
QPushButton#primaryBtn:hover { background: #e8e8e8; }
QPushButton#secondaryBtn {
    background: #121212;
    border: 1px solid #242728;
    border-radius: 10px;
    color: #cdcdcd;
    font-size: 9.5pt;
    padding: 7px 14px;
}
QPushButton#secondaryBtn:hover { background: #17181b; }

QFrame#playerBar {
    background: #0d0d0d;
    border-top: 1px solid #1c1e21;
}
QLabel#nowCover {
    background: #17181b;
    border-radius: 12px;
}
QFrame#divider { background: #1c1e21; border: none; }
QLabel#nowTitle { color: #f4f4f6; font-size: 10pt; font-weight: 600; }
QLabel#nowArtist { color: #75797d; font-size: 8.5pt; }


QLabel#timeText { color: #75797d; font-size: 8.5pt; }

QSlider#seek::groove:horizontal, QSlider#volume::groove:horizontal {
    height: 4px;
    background: #242728;
    border-radius: 2px;
}
QSlider#seek::sub-page:horizontal, QSlider#volume::sub-page:horizontal {
    background: #57c1ff;
    border-radius: 2px;
}
QSlider#volume::sub-page:horizontal { background: #4a4e52; }
QSlider#seek::handle:horizontal, QSlider#volume::handle:horizontal {
    width: 12px; height: 12px;
    margin: -4px 0;
    border-radius: 6px;
    background: #f4f4f6;
    border: 1px solid #0d0d0d;
}
QSlider#seek::handle:horizontal:hover, QSlider#volume::handle:horizontal:hover {
    background: #ffffff;
}

QScrollBar:vertical {
    background: transparent;
    width: 10px;
    margin: 2px 2px 2px 0;
}
QScrollBar::handle:vertical {
    background: #242728;
    border-radius: 5px;
    min-height: 28px;
}
QScrollBar::handle:vertical:hover { background: #34383b; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
QScrollBar:horizontal {
    background: transparent;
    height: 10px;
    margin: 0 2px 2px 2px;
}
QScrollBar::handle:horizontal {
    background: #242728;
    border-radius: 5px;
    min-width: 28px;
}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal { background: transparent; }

/* 侧边栏的滚动条：细一点、颜色压下去，别跟导航抢视线 */
QTreeWidget#sideTree QScrollBar::add-line:vertical,
QTreeWidget#sideTree QScrollBar::add-page:vertical,

QToolTip {
    background: #f4f4f6;
    color: #0d0d0d;
    border: none;
    border-radius: 7px;
    padding: 5px 9px;
    font-size: 9pt;
}
"""
_CHECK_ICON = ""
_DASH_ICON = ""


def cover_pixmap(source, size, dpr=1.0, radius=0):
    """把封面缩到 size 逻辑像素，并按屏幕缩放比给足物理像素。

    以前直接 scaled(size, size) 就完事，125% / 150% 缩放的屏幕上等于拿 36px 的图
    去铺 54 个物理像素，看着发虚；这里按 dpr 渲染再标回逻辑尺寸，画的时候 1:1。
    """
    raw = QPixmap(source) if isinstance(source, str) else source
    if raw is None or raw.isNull():
        return None
    dpr = float(dpr or 1.0)
    target = max(1, int(round(size * dpr)))
    pix = raw.scaled(target, target, Qt.KeepAspectRatioByExpanding,
                     Qt.SmoothTransformation)
    if pix.width() != target or pix.height() != target:
        pix = pix.copy(max(0, (pix.width() - target) // 2),
                       max(0, (pix.height() - target) // 2), target, target)
    if radius:
        pix = round_pixmap(pix, radius * dpr)
    pix.setDevicePixelRatio(dpr)
    return pix


def round_pixmap(pix, radius):
    """把图片裁成圆角，避免在卡片里出现直角白块。"""
    if pix.isNull():
        return pix
    out = QPixmap(pix.size())
    out.fill(Qt.transparent)
    painter = QPainter(out)
    painter.setRenderHint(QPainter.Antialiasing, True)
    path = QPainterPath()
    path.addRoundedRect(QRectF(0, 0, pix.width(), pix.height()), radius, radius)
    painter.setClipPath(path)
    painter.drawPixmap(0, 0, pix)
    painter.end()
    return out


def check_icon_path():
    """把勾选用的对勾画成 PNG 存到缓存目录，QSS 里引用它。"""
    global _CHECK_ICON
    if _CHECK_ICON:
        return _CHECK_ICON
    _CHECK_ICON = _glyph_icon("check")
    return _CHECK_ICON


def dash_icon_path():
    """半选态用的一条横线。"""
    global _DASH_ICON
    if _DASH_ICON:
        return _DASH_ICON
    _DASH_ICON = _glyph_icon("dash")
    return _DASH_ICON


_CARET_ICONS = {}


def caret_icon_path(dark=False):
    """下拉箭头：QSS 没法画三角形，就画成 PNG 让 QSS 引用。"""
    key = "dark" if dark else "light"
    hit = _CARET_ICONS.get(key)
    if hit is not None:
        return hit
    base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    path = os.path.join(base, "TuneFree", "cache", "caret_%s.png" % key)
    try:
        if not os.path.exists(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            pm = QPixmap(22, 22)
            pm.fill(QColor(0, 0, 0, 0))
            p = QPainter(pm)
            p.setRenderHint(QPainter.Antialiasing, True)
            pen = QPen(QColor("#9c9c9d" if dark else "#6f6b62"), 2.2)
            pen.setCapStyle(Qt.RoundCap)
            pen.setJoinStyle(Qt.RoundJoin)
            p.setPen(pen)
            p.drawPolyline([QPointF(6.5, 9.0), QPointF(11.0, 13.4), QPointF(15.5, 9.0)])
            p.end()
            pm.save(path, "PNG")
    except Exception:
        _CARET_ICONS[key] = ""
        return ""
    url = path.replace(os.sep, "/")
    _CARET_ICONS[key] = url
    return url


def _glyph_icon(kind):
    base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    path = os.path.join(base, "TuneFree", "cache", "%s.png" % kind)
    try:
        if not os.path.exists(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            pm = QPixmap(28, 28)
            pm.fill(QColor(0, 0, 0, 0))
            p = QPainter(pm)
            p.setRenderHint(QPainter.Antialiasing, True)
            pen = QPen(QColor("#ffffff"), 3.4)
            pen.setCapStyle(Qt.RoundCap)
            pen.setJoinStyle(Qt.RoundJoin)
            p.setPen(pen)
            if kind == "check":
                p.drawPolyline([QPointF(7.0, 14.4), QPointF(12.0, 19.4), QPointF(21.0, 9.2)])
            else:
                p.drawLine(QPointF(7.5, 14.0), QPointF(20.5, 14.0))
            p.end()
            pm.save(path, "PNG")
    except Exception:
        return ""
    return path.replace(os.sep, "/")


# -------------------- 配色 --------------------
PALETTE_BASE = {
    "light": {
        "bg": "#f3f4f8", "page": "#ffffff", "panel": "#ffffff",
        "card": "#ffffff", "card2": "#f7f8fb",
        "line": "#ecedf1", "line2": "#dfe1e6", "row_line": "#f0f1f5",
        "fg": "#14161d", "fg2": "#3d4150", "sub": "#6b7280", "sub2": "#9aa1ae",
        "accent": "#2563eb", "accent2": "#38bdf8", "accent_soft": "#dfeaff",
        "accent_line": "#bad3ff",
        "hover": "#f5f6f7", "sel": "#dfeaff", "chip": "#f3f4f7",
        "ok": "#0f9d76", "ok_soft": "#dcf2ea", "fail": "#e5484d", "fail_soft": "#fdecee",
        "warn": "#b47800", "warn_soft": "#f9edd8", "cover_bg": "#e9edf5",
        "pill_fg": "#5b6270", "glass": "#fcfcfd", "shadow": "#dfe4ee",
    },
    "dark": {
        "bg": "#0a0a0c", "page": "#101013", "panel": "#16161a",
        "card": "#16161a", "card2": "#1c1c21",
        "line": "#222226", "line2": "#313136", "row_line": "#1e1e22",
        "fg": "#f4f4f7", "fg2": "#c8c8d0", "sub": "#8f8f9a", "sub2": "#6d6d78",
        "accent": "#4c8dff", "accent2": "#7dd3fc", "accent_soft": "#1a2439",
        "accent_line": "#27406d",
        "hover": "#1a1a1c", "sel": "#1a2439", "chip": "#1e1e22",
        "ok": "#34d399", "ok_soft": "#14302a", "fail": "#f87171", "fail_soft": "#2e1618",
        "warn": "#fbbf24", "warn_soft": "#322715", "cover_bg": "#1c1c21",
        "pill_fg": "#9a9aa2", "glass": "#17171b", "shadow": "#000000",
    },
}

PALETTE = {name: dict(colors) for name, colors in PALETTE_BASE.items()}

STATUS_TEXT = {
    "pending": "待解密", "ready": "待解密", "working": "处理中",
    "done": "已完成", "failed": "失败", "skipped": "已跳过",
}

# -------------------- 配色方案 --------------------
SCHEME_SLOTS = 3          # 最多允许用户存几套自定义配色
SCHEME_EDIT_KEYS = ("bg", "panel", "fg", "sub", "line", "accent")
SCHEME_EDIT_LABELS = {
    "bg": "背景", "panel": "面板 / 卡片", "fg": "正文",
    "sub": "次要文字", "line": "分割线", "accent": "强调色",
}
_SCHEME_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def _clean_scheme(entry):
    """把一条自定义配色洗干净；结构不对就退回空槽。"""
    if not isinstance(entry, dict):
        return None
    base = entry.get("base")
    if base not in ("light", "dark"):
        return None
    colors = {}
    raw = entry.get("colors")
    if isinstance(raw, dict):
        for name in SCHEME_EDIT_KEYS:
            value = raw.get(name)
            if isinstance(value, str) and _SCHEME_COLOR_RE.match(value):
                colors[name] = value.lower()
    return {"base": base, "colors": colors, "name": str(entry.get("name") or "")}


def load_scheme_slots(settings):
    """读最多 3 套自定义配色；坏掉的槽位当作空位。"""
    slots = []
    raw = settings.value("ui/schemes", "") if settings is not None else ""
    if isinstance(raw, str) and raw.strip():
        try:
            data = json.loads(raw)
        except Exception:
            data = []
        if isinstance(data, list):
            for entry in data[:SCHEME_SLOTS]:
                slots.append(_clean_scheme(entry))
    while len(slots) < SCHEME_SLOTS:
        slots.append(None)
    return slots


def save_scheme_slots(settings, slots):
    data = list(slots[:SCHEME_SLOTS])
    while len(data) < SCHEME_SLOTS:
        data.append(None)
    try:
        settings.setValue("ui/schemes", json.dumps(data, ensure_ascii=False))
        settings.sync()
    except Exception:
        logging.debug("保存自定义配色失败", exc_info=True)


def resolve_scheme(scheme_id, slots):
    """方案 id → (基底 light|dark, 覆盖色)。空槽/坏 id 退回浅色。"""
    if scheme_id in ("light", "dark"):
        return scheme_id, {}
    text = str(scheme_id or "")
    if text.startswith("custom"):
        try:
            index = int(text[6:])
        except (TypeError, ValueError):
            return "light", {}
        if 0 <= index < len(slots) and slots[index]:
            slot = slots[index]
            return slot["base"], dict(slot.get("colors") or {})
    return "light", {}


def _mix_color(front, back, ratio):
    """按比例把 front 混进 back，用来从强调色推浅底。"""
    f, b = QColor(front), QColor(back)
    if not f.isValid() or not b.isValid():
        return ""
    return QColor(
        int(round(f.red() * ratio + b.red() * (1 - ratio))),
        int(round(f.green() * ratio + b.green() * (1 - ratio))),
        int(round(f.blue() * ratio + b.blue() * (1 - ratio))),
    ).name()


def scheme_colors(base, colors):
    """展开自定义颜色：只改强调色时，浅底和选中底色自动跟着调，
    不然会出现「字变绿了、胶囊还是橙的」这种半吊子效果。"""
    out = dict(colors or {})
    accent = out.get("accent")
    if accent:
        panel = PALETTE[base]["panel"]
        out.setdefault("accent_soft", _mix_color(accent, panel, 0.12))
        out.setdefault("sel", _mix_color(accent, panel, 0.07))
    return out


_FONT_SIZE_RE = re.compile(r"font-size:\s*([0-9]*\.?[0-9]+)pt")


def scale_qss_fonts(qss, factor):
    """主界面字号整体缩放：QSS 里所有 font-size 一起乘系数。"""
    try:
        factor = float(factor)
    except (TypeError, ValueError):
        return qss
    if abs(factor - 1.0) < 0.005:
        return qss

    def repl(match):
        try:
            size = float(match.group(1)) * factor
        except ValueError:
            return match.group(0)
        return "font-size: %.2fpt" % max(6.0, min(40.0, size))

    return _FONT_SIZE_RE.sub(repl, qss)


def cache_root(settings=None):
    """封面、歌词缓存放在哪。用户换过位置就用他的。"""
    custom = ""
    if settings is not None:
        try:
            custom = str(settings.value("cache/root", "") or "").strip()
        except Exception:
            custom = ""
    if custom:
        return Path(custom)
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "TuneFree" / "cache"


def dir_size(path):
    """目录占多大（清缓存时给用户看个数）。"""
    total = 0
    try:
        for name in os.listdir(path):
            full = os.path.join(path, name)
            try:
                total += os.path.getsize(full) if os.path.isfile(full) else dir_size(full)
            except OSError:
                continue
    except OSError:
        return 0
    return total

STATUS_STYLE = {
    "pending": ("chip", "pill_fg"),
    "ready": ("chip", "pill_fg"),
    "working": ("accent_soft", "accent"),
    "done": ("ok_soft", "ok"),
    "failed": ("fail_soft", "fail"),
    "skipped": ("chip", "sub"),
}

# 文件名识别格式：文件名里哪一段是歌名、哪一段是歌手
NAME_FORMATS = (
    ("title_artist", "歌名 - 歌手"),
    ("artist_title", "歌手 - 歌名"),
    ("title_only", "只有歌名"),
)

def split_name_by_format(stem, mode):
    """按用户选的文件名格式拆成 (歌名, 歌手)；「只有歌名」时歌手留空。

    具体规则（含尾部 _H 之类的音质后缀处理）统一放在 lyrics.split_name，
    这里只做转发，免得两处逻辑各改一半。
    """
    return lyrics.split_name(stem, mode)

# 列表列宽（表头与 delegate 共用，保证对齐）
COL_COVER = 38      # 封面（其它列宽在 TrackDelegate.columns() 里按行现算）
COL_STATUS = 96     # 转换时的状态文字，画在格式胶囊右侧


# 用户自定义配色：apply_scheme 把选中的方案写进这里，
# apply_theme_overrides 再按内置色板的原值替换 QSS；自绘控件直接读 PALETTE。
_THEME_OVERRIDES = {}


# 旧配色 -> 新配色的字面量映射：QSS 里写死的是上一版色板，
# 这里一次性换成「亮色=晨雾 / 暗色=石墨」的新色，避免手改上千行 QSS。
_LEGACY_LIGHT = {
    "#f7f7f4": "#f3f4f8", "#fafaf7": "#ffffff", "#f4f3ee": "#f5f6f7",
    "#e9e8e1": "#ecedf1", "#eceae2": "#dfe1e6", "#f1f0ea": "#f0f1f5",
    "#f2f1ec": "#f3f4f7", "#f4f4f6": "#f5f6f7", "#faf9f5": "#ffffff",
    "#fdf3ee": "#e9effd", "#e2e0d8": "#dfe1e6", "#ecebe4": "#ecedf1",
    "#fbfaf7": "#ffffff", "#e0dcd1": "#dfe1e6", "#d5cfc0": "#c9d2e0",
    "#b7b0a0": "#aab4c6", "#a29b8a": "#97a2b6", "#26251e": "#14161d",
    "#3f3d35": "#3d4150", "#5a5852": "#5b6270", "#807d72": "#7b8290",
    "#8b877c": "#6b7280", "#6b6860": "#5b6270", "#e6e5e0": "#ecedf1",
    "#cfcdc4": "#dfe1e6", "#dbd8ce": "#ecedf1", "#b9b5a8": "#c3cbd9",
    "#a8a396": "#a3adbd", "#cf4a12": "#2563eb", "#fdece5": "#e9effd",
    "#1f8a65": "#0f9d76", "#edf5f1": "#e4f4f0", "#cfe6dc": "#cfe6dc",
    "#cf2d56": "#e5484d", "#fbe9ee": "#fdecee", "#a86b16": "#b47800",
    "#fbf0dc": "#f6efe0", "#eeece4": "#e9edf5",
}
_LEGACY_DARK = {
    "#07080a": "#0a0a0c", "#0d0d0d": "#16161a", "#121212": "#1c1c21",
    "#1c1e21": "#222226", "#17181b": "#1e1e22", "#131316": "#1c1c20",
    "#0b0c0e": "#101013", "#f4f4f6": "#f4f4f7", "#9c9c9d": "#8f8f9a",
    "#cdcdcd": "#b8b8c0", "#e8e8e8": "#dedee4", "#a9aaac": "#9a9aa2",
    "#d3d3d4": "#c3c3c9", "#57c1ff": "#4c8dff", "#7fd0ff": "#7dd3fc",
    "#14242f": "#1a2439", "#59d499": "#34d399", "#10251c": "#14302a",
    "#1d4635": "#17503a", "#242728": "#1e1e23", "#34383b": "#2a2a30",
    "#3a3f44": "#303036", "#5f6367": "#55555c", "#75797d": "#6b6b73",
    "#5a5852": "#6f6f77", "#3b3930": "#26262c", "#2e3134": "#26262b",
    "#3d4145": "#34343a", "#4a4e52": "#3e3e45", "#2a2c2e": "#242429",
    "#6a6b6c": "#62626a", "#202224": "#1a1a1e", "#303439": "#24242a",
    "#454a50": "#38383f", "#555b62": "#4a4a52", "#23262a": "#1c1c21",
}


def remap_legacy_colors(qss, key):
    """把 QSS 里的旧色值换成新色板（先换基底，自定义配色再覆盖）。"""
    table = _LEGACY_LIGHT if key == "light" else _LEGACY_DARK
    for old, new in table.items():
        qss = qss.replace(old, new)
    return qss


def apply_theme_overrides(qss, key):
    """把 QSS 里内置色板的值换成用户自定义的。"""
    if not _THEME_OVERRIDES:
        return qss
    for name, old in PALETTE_BASE[key].items():
        new = _THEME_OVERRIDES.get((key, name))
        if new and old and new.lower() != old.lower():
            qss = qss.replace(old, new)
    return qss


def apply_titlebar_theme(widget, dark):
    """让 Windows 原生标题栏跟着亮/暗主题走（Win10 1809+）。"""
    try:
        import ctypes

        hwnd = int(widget.winId())
        value = ctypes.c_int(1 if dark else 0)
        for attr in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE，新旧两版编号
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, attr, ctypes.byref(value), ctypes.sizeof(value)) == 0:
                break
        # 只触发重绘，不动位置/大小/Z 序
        ctypes.windll.user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x0002 | 0x0001 | 0x0004)
    except Exception:
        logging.debug("设置标题栏主题失败", exc_info=True)


def format_time(seconds):
    """秒数 → 4:30 / 1:02:03。"""
    try:
        total = int(round(float(seconds)))
    except (TypeError, ValueError):
        return "—"
    if total <= 0:
        return "—"
    minutes, sec = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return "%d:%02d:%02d" % (hours, minutes, sec)
    return "%d:%02d" % (minutes, sec)


def volume_pixmap(level, color, size=18):
    """喇叭图标：0 静音、1-50 单声波、>50 双声波。"""
    dpr = QApplication.instance().devicePixelRatio() if QApplication.instance() else 1.0
    pm = QPixmap(int(size * dpr), int(size * dpr))
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    pen = QPen(QColor(color))
    pen.setWidthF(1.35)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)

    body = QPainterPath()
    body.moveTo(3.2, 7.2)
    body.lineTo(5.9, 7.2)
    body.lineTo(8.9, 4.1)
    body.lineTo(8.9, 13.9)
    body.lineTo(5.9, 10.8)
    body.lineTo(3.2, 10.8)
    body.closeSubpath()
    p.drawPath(body)

    if level <= 0:
        p.drawLine(QPointF(11.6, 6.8), QPointF(16.0, 11.2))
        p.drawLine(QPointF(16.0, 6.8), QPointF(11.6, 11.2))
    else:
        p.drawArc(QRectF(8.4, 5.6, 4.4, 6.8), -55 * 16, 110 * 16)
        if level > 50:
            p.drawArc(QRectF(8.4, 3.1, 8.2, 11.8), -55 * 16, 110 * 16)
    p.end()
    return pm


def format_size(num):
    """字节数转成人看的单位。"""
    try:
        num = float(num)
    except (TypeError, ValueError):
        return "—"
    if num <= 0:
        return "-"
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024 or unit == "GB":
            return ("%.0f %s" % (num, unit)) if unit == "B" else ("%.1f %s" % (num, unit))
        num /= 1024.0
    return "-"


def _round_rect(painter, rect, radius, color):
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor(color))
    painter.drawRoundedRect(rect, radius, radius)


def _round_outline(painter, rect, radius, color, width=1.0):
    pen = QPen(QColor(color))
    pen.setWidthF(width)
    painter.setPen(pen)
    painter.setBrush(Qt.NoBrush)
    painter.drawRoundedRect(rect, radius, radius)


def format_pills(track, view="all", select_mode=False, has_lossless=False, palette=None):
    """这一行「格式」列该显示哪些胶囊。

    两个来源分开说：源文件是什么壳（mflac / mgg）、解出来是什么音频（flac / ogg）。
    解密不会删源文件，所以解完的歌依然该看得见 MFLAC，而不是被 FLAC 顶掉。
    """
    c = palette or PALETTE["light"]
    src_ext = (track.path.rsplit(".", 1)[-1] or "").lower()
    if track.is_out:
        # 选择模式下一行就是一个文件，输出行只说自己是什么
        kind = getattr(track, "out_kind", "") or src_ext
        kind = kind if kind in ("flac", "ogg") else "flac"
        return [(kind.upper(), c["ok_soft"], c["ok"])]

    pills = []
    if src_ext in ("mflac", "mgg"):
        pills.append((src_ext.upper(), c["warn_soft"], c["warn"]))
    if has_lossless and not select_mode:
        # 选择模式里解出来的文件自己占一行，源文件行不再重复挂这个胶囊
        kind = track.kind if track.kind in ("flac", "ogg") else \
            ("flac" if src_ext == "mflac" else "ogg")
        pills.append((kind.upper(), c["ok_soft"], c["ok"]))
    if not pills:
        pills.append((src_ext.upper() or "?", c["chip"], c["sub"]))

    # 切到某个具体格式后，这一列只留该格式的胶囊
    key = (view or "all").lower()
    if key in ("mflac", "mgg", "flac", "ogg"):
        only = [pill for pill in pills if pill[0].lower() == key]
        if not only:
            soft, ink = ((c["warn_soft"], c["warn"]) if key in ("mflac", "mgg")
                         else (c["ok_soft"], c["ok"]))
            only = [(key.upper(), soft, ink)]
        return only
    if key == "enc":
        only = [pill for pill in pills if pill[0].lower() in ("mflac", "mgg")]
        if only:
            return only
    return pills


# -------------------- 数据模型 --------------------
class TrackModel(QAbstractListModel):
    """把可见的 Track 列表交给 QListView。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows = []

    def set_rows(self, rows):
        self.beginResetModel()
        self.rows = list(rows)
        self.endResetModel()

    def row_of(self, track):
        for i, item in enumerate(self.rows):
            if item is track:
                return i
        return -1

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid() or not (0 <= index.row() < len(self.rows)):
            return None
        track = self.rows[index.row()]
        if role == Qt.UserRole:
            return track
        if role == Qt.DisplayRole:
            return track.title or track.name
        return None

    def ping(self, track):
        """某一行的状态变了，通知视图重画那一行。"""
        row = self.row_of(track)
        if row >= 0:
            idx = self.index(row, 0)
            self.dataChanged.emit(idx, idx)


# -------------------- 列表绘制 --------------------
class TrackDelegate(QStyledItemDelegate):
    """一行一首歌：序号/播放键、封面、歌名歌手、格式胶囊、大小、时长、更多。"""

    def __init__(self, window):
        super().__init__(window)
        self.win = window
        self._covers = {}

    def palette_of(self):
        return PALETTE["dark" if self.win.is_dark else "light"]

    def _cover(self, track, size):
        path = track.cover_path
        if not path or not os.path.exists(path):
            return None
        dpr = float(self.win.devicePixelRatioF() or 1.0)
        key = (path, size, round(dpr, 2))
        pix = self._covers.get(key)
        if pix is not None:
            return pix
        pix = cover_pixmap(path, size, dpr)
        if pix is not None:
            self._covers[key] = pix
        return pix

    def sizeHint(self, option, index):
        return QSize(0, 52)

    def columns(self, rect, show_check, show_status):
        """把一行切成几块，paint 和鼠标热区共用，免得两处算错。"""
        x = rect.left() + 14
        cols = {}
        if show_check:
            cols["check"] = QRect(x, rect.center().y() - 9, 18, 18)
            x += 18 + 12
        cols["no"] = QRect(x, rect.top(), 44, rect.height())
        x += 44 + 12
        cols["cover"] = QRect(x, rect.center().y() - 19, 38, 38)
        x += 38 + 12
        right = rect.right() - 14
        cols["more"] = QRect(right - 22, rect.top(), 22, rect.height())
        right -= 22 + 12
        cols["dur"] = QRect(right - 52, rect.top(), 52, rect.height())
        right -= 52 + 12
        cols["size"] = QRect(right - 78, rect.top(), 78, rect.height())
        right -= 78 + 12
        cols["pills"] = QRect(right - 150, rect.top(), 150, rect.height())
        right -= 150 + 12
        if show_status:
            cols["status"] = QRect(right - COL_STATUS, rect.top(), COL_STATUS, rect.height())
            right -= COL_STATUS + 10
        cols["meta"] = QRect(x, rect.top(), max(90, right - x), rect.height())
        return cols

    def paint(self, painter, option, index):
        track = index.data(Qt.UserRole)
        if track is None:
            return
        c = self.palette_of()
        rect = option.rect
        scale = getattr(self.win, "font_scale", 1.0)
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)

        hovered = bool(option.state & QStyle.State_MouseOver)
        selected = bool(option.state & QStyle.State_Selected)
        inner = QRectF(rect).adjusted(2, 1, -2, -1)
        if selected:
            _round_rect(painter, inner, 10, c["accent_soft"])
        elif hovered:
            _round_rect(painter, inner, 10, c["hover"])

        show_check = bool(getattr(self.win, "select_mode", False))
        show_status = bool(getattr(self.win, "show_status", False))
        cols = self.columns(rect, show_check, show_status)
        mid = rect.center().y()

        if show_check:
            checked = track.path in self.win.checked
            box = QRectF(cols["check"]).adjusted(0.75, 0.75, -0.75, -0.75)
            if checked:
                _round_rect(painter, box, 5, c["accent"])
                pen = QPen(QColor("#ffffff"), 1.8)
                pen.setCapStyle(Qt.RoundCap)
                pen.setJoinStyle(Qt.RoundJoin)
                painter.setPen(pen)
                painter.setBrush(Qt.NoBrush)
                painter.drawPolyline([QPointF(box.left() + 3.8, box.center().y() + 0.4),
                                      QPointF(box.center().x() - 0.4, box.bottom() - 4.0),
                                      QPointF(box.right() - 3.4, box.top() + 4.6)])
            else:
                _round_outline(painter, box, 5, c["line2"], 1.5)

        # 序号 / 播放键：鼠标在哪一行，哪一行就露出播放键
        font = painter.font()
        if hovered or selected:
            play = QRectF(cols["no"].right() - 26, mid - 13, 26, 26)
            _round_rect(painter, play, 13, c["accent"])
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor("#ffffff"))
            tri = QPolygonF([QPointF(play.center().x() - 3.2, play.center().y() - 5.4),
                             QPointF(play.center().x() - 3.2, play.center().y() + 5.4),
                             QPointF(play.center().x() + 5.6, play.center().y())])
            painter.drawPolygon(tri)
        else:
            font.setPixelSize(max(9, int(round(11.5 * scale))))
            painter.setFont(font)
            painter.setPen(QColor(c["sub2"]))
            painter.drawText(cols["no"], Qt.AlignRight | Qt.AlignVCenter, str(index.row() + 1))

        # 封面
        cover_rect = cols["cover"]
        cv = self._cover(track, 38)
        painter.save()
        clip = QPainterPath()
        clip.addRoundedRect(QRectF(cover_rect), 8, 8)
        painter.setClipPath(clip)
        if cv is not None:
            painter.drawPixmap(cover_rect, cv)
        else:
            grad = QLinearGradient(cover_rect.topLeft(), cover_rect.bottomRight())
            grad.setColorAt(0.0, QColor(c["cover_bg"]))
            grad.setColorAt(1.0, QColor(c["accent_soft"]))
            painter.fillRect(cover_rect, grad)
            font.setPixelSize(max(11, int(round(14 * scale))))
            painter.setFont(font)
            painter.setPen(QColor(c["accent"] if track.kind == "flac" else c["warn"]))
            painter.drawText(cover_rect, Qt.AlignCenter, "♪")
        painter.restore()

        # 歌名 / 歌手
        meta = cols["meta"]
        name = track.title or track.name
        font.setPixelSize(max(10, int(round(13 * scale))))
        font.setWeight(QFont.DemiBold)
        painter.setFont(font)
        painter.setPen(QColor(c["fg"]))
        title_rect = QRect(meta.left(), mid - 17, meta.width(), 18)
        elided = painter.fontMetrics().elidedText(name, Qt.ElideRight, meta.width())
        painter.drawText(title_rect, Qt.AlignLeft | Qt.AlignVCenter, elided)
        artist = track.artist or ("" if track.is_out else "未知歌手")
        if artist:
            font.setPixelSize(max(9, int(round(11.5 * scale))))
            font.setWeight(QFont.Normal)
            painter.setFont(font)
            painter.setPen(QColor(c["sub"]))
            artist_rect = QRect(meta.left(), mid + 2, meta.width(), 16)
            elided = painter.fontMetrics().elidedText(artist, Qt.ElideRight, meta.width())
            painter.drawText(artist_rect, Qt.AlignLeft | Qt.AlignVCenter, elided)

        # 格式胶囊
        view = getattr(self.win, "filter_kind", "all")
        select_mode = getattr(self.win, "select_mode", False)
        pills = format_pills(track, view, select_mode, self.win.has_lossless(track), c)
        font.setPixelSize(max(8, int(round(10 * scale))))
        font.setWeight(QFont.DemiBold)
        painter.setFont(font)
        pills_rect = cols["pills"]
        used = 0
        for label, soft, ink in pills:
            w = 16 + painter.fontMetrics().horizontalAdvance(label)
            if used and used + 5 + w > pills_rect.width():
                break
            px = pills_rect.left() + used + (5 if used else 0)
            pill = QRectF(px, mid - 9.5, w, 19)
            _round_rect(painter, pill, 5, soft)
            painter.setPen(QColor(ink))
            painter.drawText(pill, Qt.AlignCenter, label)
            used = px - pills_rect.left() + w

        # 大小 / 时长
        font.setPixelSize(max(9, int(round(11.5 * scale))))
        font.setWeight(QFont.Normal)
        painter.setFont(font)
        painter.setPen(QColor(c["sub"]))
        fkey = getattr(self.win, "filter_kind", "all")
        files = self.win.format_files(track)
        if track.is_out:
            # 选择模式里的 FLAC / OGG 行本身就是单个文件，不能加「共」
            size_text = format_size(track.output_size or track.size or 0)
        elif fkey in ("mflac", "mgg", "flac", "ogg"):
            matched = [item for item in files if item[0] == fkey]
            size_text = format_size(matched[0][2] if matched else 0)
        else:
            # 全部 / 未解密：同名歌曲所有格式加起来，加「共」免得被当成单个文件
            size_text = "共 " + format_size(sum(item[2] for item in files)
                                            or (track.size or 0))
        painter.drawText(cols["size"], Qt.AlignRight | Qt.AlignVCenter, size_text)
        painter.drawText(cols["dur"], Qt.AlignRight | Qt.AlignVCenter,
                         format_time(track.duration))

        # 转换时临时露出的状态文字，就在胶囊右边
        if show_status:
            key = track.status if track.status in STATUS_STYLE else "pending"
            text = STATUS_TEXT.get(key, "待解密")
            color = {"done": c["ok"], "failed": c["fail"], "working": c["accent"],
                     "skipped": c["sub"]}.get(key, c["sub2"])
            font.setPixelSize(max(9, int(round(11.5 * scale))))
            painter.setFont(font)
            painter.setPen(QColor(color))
            painter.drawText(cols["status"], Qt.AlignRight | Qt.AlignVCenter, text)

        # 行尾更多
        painter.setPen(QPen(QColor(c["accent"] if hovered else c["sub2"]), 1.8))
        painter.setBrush(Qt.NoBrush)
        cx = cols["more"].center().x()
        for iy in (-4, 0, 4):
            painter.drawPoint(QPointF(cx, mid + iy))

        painter.restore()


class TrackView(QListView):
    """列表：双击播放，行首勾选，行尾「⋯」和右键都是同一份菜单。"""

    def __init__(self, window):
        super().__init__(window)
        self.win = window
        self.setObjectName("trackView")
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setMouseTracking(True)
        self.setUniformItemSizes(True)
        self.setItemDelegate(TrackDelegate(window))
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._on_menu)
        self.doubleClicked.connect(self._on_double)
        self._hover_row = -1

    def _cols(self, index):
        rect = self.visualRect(index)
        return self.itemDelegate().columns(rect, bool(self.win.select_mode),
                                          bool(self.win.show_status))

    def _on_double(self, index):
        track = index.data(Qt.UserRole)
        if track is not None:
            self.win.play_track(track)

    def mouseMoveEvent(self, event):
        pos = event.position().toPoint()
        row = self.indexAt(pos).row()
        if row != self._hover_row:
            self._hover_row = row
            self.viewport().update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        if self._hover_row != -1:
            self._hover_row = -1
            self.viewport().update()
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        index = self.indexAt(event.position().toPoint())
        if index.isValid() and event.button() == Qt.LeftButton:
            track = index.data(Qt.UserRole)
            cols = self._cols(index)
            pt = event.position().toPoint()
            if track is not None:
                if "check" in cols and cols["check"].adjusted(-6, -8, 8, 8).contains(pt):
                    self.win.toggle_checked(track)
                    return
                if cols["more"].adjusted(-4, -9, 6, 9).contains(pt):
                    self.win.track_menu(track, self.viewport().mapToGlobal(pt))
                    return
                if event.position().x() >= cols["no"].right() - 28 and                         cols["no"].contains(QPoint(cols["no"].right() - 14, pt.y())):
                    self.win.play_track(track)
                    return
        super().mousePressEvent(event)

    def _on_menu(self, pos):
        index = self.indexAt(pos)
        if not index.isValid():
            return
        track = index.data(Qt.UserRole)
        if track is not None:
            self.win.track_menu(track, self.viewport().mapToGlobal(pos))


# -------------------- 顶部表头 --------------------
class ShellWidget(QWidget):
    """主界面底层：纯底色，不再叠光晕（顶栏要保持干净）。"""

    def paintEvent(self, _ev):
        win = self.window()
        dark = bool(getattr(win, "is_dark", False))
        c = PALETTE["dark" if dark else "light"]
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(c["bg"]))


class ChipButton(QAbstractButton):
    """胶囊筛选块：文字 + 右侧计数，选中时整体染成强调色。

    设计稿里数字是另一个字号和颜色，QSS 做不出来，所以这块自绘。
    """

    def __init__(self, window, text, key, kind="chip", count=None):
        super().__init__(window)
        self.win = window
        self.key = key
        self.kind = kind            # chip / sep / filter
        self.count = count
        self.check_state = 0
        self.select_mode = False
        self._hover = False
        self.setAttribute(Qt.WA_Hover, True)
        self.setFixedHeight(30)
        if kind == "sep":
            self.setFixedWidth(18)
            self.setCursor(Qt.ArrowCursor)
        else:
            self.setText(text)
            self.setCheckable(True)
            self.setCursor(Qt.PointingHandCursor)
            self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.setToolTip(text if kind != "sep" else "")

    # ---- 尺寸 ----
    def _font(self, px, weight=QFont.Normal):
        f = QFont(self.font())
        f.setPixelSize(max(9, int(round(px * getattr(self.win, "font_scale", 1.0)))))
        f.setWeight(weight)
        return f

    def sizeHint(self):
        if self.kind == "sep":
            return QSize(18, 30)
        fm = QFontMetrics(self._font(12))
        w = 26 + fm.horizontalAdvance(self.text())
        if self.count is not None:
            fm2 = QFontMetrics(self._font(11.5, QFont.DemiBold))
            w += 7 + fm2.horizontalAdvance(str(self.count))
        if self.kind == "filter":
            w += 7 + 14
        if self.kind == "chip" and self.select_mode:
            w += 24
        return QSize(w, 30)

    def minimumSizeHint(self):
        return self.sizeHint()

    checkToggled = Signal(str)

    def set_check_state(self, state):
        state = int(state)
        if state != getattr(self, "check_state", 0):
            self.check_state = state
            self.update()

    def set_select_mode(self, on):
        on = bool(on)
        if on == self.select_mode:
            return
        self.select_mode = on
        self.updateGeometry()
        self.update()

    def _check_rect(self):
        mid = self.height() / 2.0
        return QRectF(13, mid - 9, 18, 18)

    def mousePressEvent(self, ev):
        if self.kind == "chip" and getattr(self, "select_mode", False):
            if self._check_rect().adjusted(-3, -6, 8, 6).contains(ev.position()):
                self.checkToggled.emit(self.key)
                return
        super().mousePressEvent(ev)

    def set_count(self, n):
        if n != self.count:
            self.count = n
            self.updateGeometry()
            self.update()

    def enterEvent(self, _ev):
        self._hover = True
        self.update()

    def leaveEvent(self, _ev):
        self._hover = False
        self.update()

    def _paint_check(self, p, c, chip_on):
        r = self._check_rect()
        state = int(getattr(self, "check_state", 0))
        if state:
            _round_rect(p, r, 4, c["accent"])
            pen = QPen(QColor("#ffffff"), 1.8)
            pen.setCapStyle(Qt.RoundCap)
            pen.setJoinStyle(Qt.RoundJoin)
            p.setPen(pen)
            if state == 2:
                p.drawLine(QPointF(r.left() + 4, r.center().y()),
                           QPointF(r.right() - 4, r.center().y()))
            else:
                p.drawPolyline([QPointF(r.left() + 3.6, r.center().y() + 0.2),
                                QPointF(r.left() + 6.2, r.center().y() + 3.0),
                                QPointF(r.right() - 3.4, r.center().y() - 3.0)])
        else:
            edge = QColor(c["line2"])
            _round_outline(p, r.adjusted(0.75, 0.75, -0.75, -0.75), 4, edge, 1.5)

    def paintEvent(self, _ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        c = PALETTE["dark" if self.win.is_dark else "light"]
        if self.kind == "sep":
            p.setFont(self._font(12))
            p.setPen(QColor(c["sub2"]))
            p.drawText(self.rect(), Qt.AlignCenter, "·")
            return
        on = self.isChecked()
        if on:
            bg = c["accent_soft"]
        elif self.kind == "filter":
            bg = c["card"]
        elif self._hover:
            bg = _mix_color(c["fg"], c["chip"], 0.05) or c["chip"]
        else:
            bg = c["chip"]
        if bg:
            _round_rect(p, QRectF(self.rect()), 15, bg)
        if on:
            edge = QColor(c["accent"])
            edge.setAlphaF(0.38)
            _round_outline(p, QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 15, edge, 1)
        elif self.kind == "filter":
            _round_outline(p, QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 15, c["line"], 1)
        if self.kind == "chip" and self.select_mode:
            self._paint_check(p, c, on)
        x = 37 if (self.kind == "chip" and self.select_mode) else 13
        mid = self.rect().center().y()
        p.setFont(self._font(12, QFont.DemiBold if on else QFont.Normal))
        p.setPen(QColor(c["accent"] if on else c["sub"]))
        fm = p.fontMetrics()
        p.drawText(QRect(x, mid - 10, self.width() - x - 13, 20),
                   Qt.AlignLeft | Qt.AlignVCenter, self.text())
        x += fm.horizontalAdvance(self.text()) + 7
        if self.count is not None:
            p.setFont(self._font(11.5, QFont.DemiBold))
            p.setPen(QColor(c["accent"] if on else c["fg2"]))
            p.drawText(QRect(x, mid - 10, 40, 20),
                       Qt.AlignLeft | Qt.AlignVCenter, str(self.count))
        if self.kind == "chip" and self.select_mode:
            pass
        if self.kind == "filter":
            # 三横线漏斗图标
            ix = self.width() - 13 - 14
            pen = QPen(QColor(c["accent"] if on else c["fg2"]), 2)
            pen.setCapStyle(Qt.RoundCap)
            p.setPen(pen)
            p.drawLine(ix, mid - 4, ix + 14, mid - 4)
            p.drawLine(ix + 3, mid, ix + 11, mid)
            p.drawLine(ix + 5, mid + 4, ix + 9, mid + 4)


class FilterItemDelegate(QStyledItemDelegate):
    """筛选下拉里的一行：勾选框 + 歌手 + 歌数。"""

    def __init__(self, popup):
        super().__init__(popup)
        self.pop = popup

    def sizeHint(self, option, index):
        return QSize(0, 30)

    def paint(self, painter, option, index):
        win = self.pop.win
        c = PALETTE["dark" if win.is_dark else "light"]
        rect = option.rect
        name = index.data(Qt.UserRole + 1) or ""
        count = index.data(Qt.UserRole + 2) or 0
        on = name in self.pop.checked
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        if option.state & QStyle.State_MouseOver:
            _round_rect(painter, QRectF(rect.adjusted(0, 1, 0, -1)), 7, c["hover"])
        mid = rect.center().y()
        tick = QRect(rect.left() + 9, mid - 7, 15, 15)
        if on:
            _round_rect(painter, QRectF(tick), 4, c["accent"])
        else:
            _round_outline(painter, QRectF(tick).adjusted(0.5, 0.5, -0.5, -0.5), 4, c["line2"], 1.2)
        if on:
            pen = QPen(QColor("#ffffff"), 1.8)
            pen.setCapStyle(Qt.RoundCap)
            pen.setJoinStyle(Qt.RoundJoin)
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)
            painter.drawPolyline(QPolygon([QPoint(tick.left() + 3, mid),
                                           QPoint(tick.center().x(), tick.bottom() - 4),
                                           QPoint(tick.right() - 3, tick.top() + 4)]))
        painter.setPen(QColor(c["fg"] if on else c["fg2"]))
        f = painter.font()
        f.setPixelSize(int(round(12 * getattr(win, "font_scale", 1.0))))
        painter.setFont(f)
        fm = painter.fontMetrics()
        text_rect = QRect(tick.right() + 9, rect.top(), rect.width() - tick.right() - 9 - 34, rect.height())
        painter.drawText(text_rect, Qt.AlignLeft | Qt.AlignVCenter,
                         fm.elidedText(name, Qt.ElideRight, text_rect.width()))
        painter.setPen(QColor(c["sub2"]))
        f.setPixelSize(int(round(10.5 * getattr(win, "font_scale", 1.0))))
        painter.setFont(f)
        painter.drawText(QRect(rect.right() - 40, rect.top(), 30, rect.height()),
                         Qt.AlignRight | Qt.AlignVCenter, str(count))
        painter.restore()


class FilterPopup(QFrame):
    """chips 行最右的「筛选」下拉：只做歌手筛选，勾完点应用才生效。"""

    applied = Signal(list)

    def __init__(self, window):
        super().__init__(window)
        self.win = window
        self.setObjectName("filterPop")
        self.setWindowFlags(Qt.Popup | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setFixedWidth(268)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(0)
        self.search = QLineEdit()
        self.search.setObjectName("fpSearch")
        self.search.setPlaceholderText("搜索歌手")
        self.search.setFixedHeight(30)
        self.search.textChanged.connect(self._filter_rows)
        lay.addWidget(self.search)
        lay.addSpacing(8)
        self.list = QListWidget()
        self.list.setObjectName("fpList")
        self.list.setFrameShape(QFrame.NoFrame)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.list.setItemDelegate(FilterItemDelegate(self))
        self.list.setMouseTracking(True)
        self.list.setUniformItemSizes(True)
        self.list.setSelectionMode(QAbstractItemView.NoSelection)
        self.list.setFocusPolicy(Qt.NoFocus)
        lay.addWidget(self.list)
        lay.addSpacing(10)
        foot = QWidget()
        foot.setObjectName("fpFoot")
        foot_lay = QHBoxLayout(foot)
        foot_lay.setContentsMargins(0, 10, 0, 0)
        foot_lay.setSpacing(8)
        foot_lay.addStretch(1)
        self.clear_btn = QPushButton("清空")
        self.clear_btn.setObjectName("fpBtn")
        self.clear_btn.setFixedHeight(28)
        self.clear_btn.setCursor(Qt.PointingHandCursor)
        self.clear_btn.clicked.connect(self._clear)
        foot_lay.addWidget(self.clear_btn)
        self.apply_btn = QPushButton("应用")
        self.apply_btn.setObjectName("fpBtnPrimary")
        self.apply_btn.setFixedHeight(28)
        self.apply_btn.setCursor(Qt.PointingHandCursor)
        self.apply_btn.clicked.connect(self._apply)
        foot_lay.addWidget(self.apply_btn)
        lay.addWidget(foot)
        self.checked = set()
        self.counts = {}
        self.artists = []
        self.list.viewport().installEventFilter(self)

    def eventFilter(self, obj, event):
        if obj is self.list.viewport() and event.type() == QEvent.MouseButtonRelease:
            item = self.list.itemAt(event.position().toPoint())
            if item is not None:
                name = item.data(Qt.UserRole + 1)
                if name in self.checked:
                    self.checked.discard(name)
                else:
                    self.checked.add(name)
                self.list.viewport().update()
                return True
        return super().eventFilter(obj, event)

    def load(self, artists, counts, checked):
        """artists: 全部歌手（按歌数降序），counts: 名字 -> 歌数。"""
        self.artists = list(artists)
        self.counts = dict(counts)
        self.checked = set(checked)
        self.search.blockSignals(True)
        self.search.clear()
        self.search.blockSignals(False)
        self._fill(self.artists)

    def _fill(self, names):
        self.list.clear()
        for name in names:
            item = QListWidgetItem()
            item.setData(Qt.UserRole + 1, name)
            item.setData(Qt.UserRole + 2, self.counts.get(name, 0))
            item.setToolTip(name)
            self.list.addItem(item)
        rows = max(1, min(len(names), 7))
        self.list.setFixedHeight(rows * 30)
        self.adjustSize()

    def _filter_rows(self, text):
        q = (text or "").strip().lower()
        names = [n for n in self.artists if q in n.lower()] if q else list(self.artists)
        self._fill(names)

    def _clear(self):
        self.checked.clear()
        self.list.viewport().update()

    def _apply(self):
        self.hide()
        self.applied.emit(sorted(self.checked))

    def popup_at(self, anchor):
        self.search.setFocus()
        pos = anchor.mapToGlobal(QPoint(anchor.width() - self.width(), anchor.height() + 8))
        screen = self.win.screen().availableGeometry() if self.win.screen() else None
        if screen is not None:
            pos.setX(max(screen.left() + 8, min(pos.x(), screen.right() - self.width() - 8)))
            pos.setY(max(screen.top() + 8, min(pos.y(), screen.bottom() - self.height() - 8)))
        self.move(pos)
        self.show()
        self.search.setFocus()


class ToolbarBar(QWidget):
    """列表上方那一行：左边列名，右边排序与「选择」。"""

    sortRequested = Signal(str)

    def __init__(self, window):
        super().__init__(window)
        self.setObjectName("toolbar")
        self.win = window
        self.setFixedHeight(37)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(26, 12, 26, 10)
        lay.setSpacing(0)

        self.cap = QLabel("标题")
        self.cap.setObjectName("toolCap")
        lay.addWidget(self.cap)
        self.caps = []
        for text in ("格式", "大小", "时长"):
            lay.addSpacing(7)
            sep = QLabel("·")
            sep.setObjectName("toolSep")
            lay.addWidget(sep)
            lay.addSpacing(7)
            lk = QLabel(text)
            lk.setObjectName("toolLink")
            lay.addWidget(lk)
            self.caps.append(lk)
        lay.addStretch(1)

        self.sort_box = QWidget()
        self.sort_box.setObjectName("sortBox")
        sort_lay = QHBoxLayout(self.sort_box)
        sort_lay.setContentsMargins(0, 0, 0, 0)
        sort_lay.setSpacing(0)
        self.buttons = {}
        self.seps = []
        for i, (key, text) in enumerate((("title", "按标题"), ("artist", "按歌手"),
                                         ("size", "按大小"))):
            if i:
                sep = QLabel("·")
                sep.setObjectName("toolSep")
                sort_lay.addSpacing(6)
                sort_lay.addWidget(sep)
                sort_lay.addSpacing(6)
                self.seps.append(sep)
            btn = QPushButton(text)
            btn.setObjectName("toolSort")
            btn.setProperty("active", "false")
            btn.setCursor(Qt.PointingHandCursor)
            btn.setFlat(True)
            btn.clicked.connect(lambda _=False, k=key: self.sortRequested.emit(k))
            sort_lay.addWidget(btn)
            self.buttons[key] = btn
        lay.addWidget(self.sort_box)
        lay.addSpacing(14)

        self.select_btn = QPushButton("选择")
        self.select_btn.setObjectName("toolSort")
        self.select_btn.setProperty("active", "false")
        self.select_btn.setCursor(Qt.PointingHandCursor)
        self.select_btn.setFlat(True)
        self.select_btn.setCheckable(True)
        self.select_btn.setMinimumWidth(44)
        lay.addWidget(self.select_btn)
        self._lay = lay
        self.all_btn = None
        self.select_bar = None

    def bind_select(self, all_btn, select_bar):
        """选择模式的「全选 / 已选中 N 项 / 删除」挂到这一行右端。"""
        self.all_btn = all_btn
        self.select_bar = select_bar
        self._lay.addSpacing(8)
        self._lay.addWidget(all_btn)
        self._lay.addSpacing(6)
        self._lay.addWidget(select_bar)
        all_btn.hide()
        select_bar.hide()

    def set_select_mode(self, on):
        self.sort_box.setVisible(not on)
        self.select_btn.setText("完成" if on else "选择")
        if self.all_btn is not None:
            self.all_btn.setVisible(bool(on))
        if self.select_bar is not None:
            self.select_bar.setVisible(bool(on))
        # 进入选择模式后「完成」挪到最右，让 全选 / 已选中 / 删除 排成一串
        if on and self._lay.indexOf(self.select_btn) < self._lay.indexOf(self.all_btn):
            self._lay.removeWidget(self.select_btn)
            self._lay.addWidget(self.select_btn)

    def set_status_visible(self, on):
        """状态列现在挂在胶囊行，工具行不用变。"""
        return

    def set_active(self, key):
        for k, btn in self.buttons.items():
            btn.setProperty("active", "true" if k == key else "false")
            btn.style().unpolish(btn)
            btn.style().polish(btn)


# -------------------- 播放器按钮 --------------------
class GlyphButton(QAbstractButton):
    """顶栏小图标按钮：星标 / 支持作者，圆角底 + 自绘图形。"""

    COLORS = {
        "star": {"light": ("#8a8578", "#a8761a", "#fdf6e8"),
                 "dark": ("#a9aaac", "#ffc533", "#241d0e")},
        "heart": {"light": ("#8a8578", "#c0405f", "#fdeef1"),
                  "dark": ("#a9aaac", "#ff8fa8", "#241014")},
        "trash": {"light": ("#8a8578", "#cf2d56", "#fbe9ee"),
                  "dark": ("#a9aaac", "#ff6161", "#2a1618")},
    }

    def __init__(self, kind, parent=None, size=34):
        super().__init__(parent)
        self.kind = kind
        self._hover = False
        self.setFixedSize(size, size)
        self.setCursor(Qt.PointingHandCursor)
        self.setAttribute(Qt.WA_Hover, True)
        self.setFocusPolicy(Qt.NoFocus)
        self.setToolTip("")

    def enterEvent(self, event):
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def _shape(self, cx, cy, r):
        path = QPainterPath()
        if self.kind == "star":
            pts = []
            for i in range(10):
                ang = -math.pi / 2 + i * math.pi / 5
                rad = r if i % 2 == 0 else r * 0.44
                pts.append(QPointF(cx + rad * math.cos(ang), cy + rad * math.sin(ang)))
            path.moveTo(pts[0])
            for pt in pts[1:]:
                path.lineTo(pt)
            path.closeSubpath()
        elif self.kind == "trash":
            lid = QRectF(cx - r * 0.56, cy - r * 0.52, r * 1.12, r * 0.20)
            path.addRoundedRect(lid, r * 0.08, r * 0.08)
            handle = QRectF(cx - r * 0.20, cy - r * 0.74, r * 0.40, r * 0.18)
            path.addRoundedRect(handle, r * 0.06, r * 0.06)
            body = QPainterPath()
            body.moveTo(cx - r * 0.42, cy - r * 0.28)
            body.lineTo(cx + r * 0.42, cy - r * 0.28)
            body.lineTo(cx + r * 0.32, cy + r * 0.78)
            body.lineTo(cx - r * 0.32, cy + r * 0.78)
            body.closeSubpath()
            path.addPath(body)
        else:
            path.moveTo(cx, cy + r * 0.95)
            path.cubicTo(cx - r * 1.60, cy - r * 0.10, cx - r * 0.66, cy - r * 1.24, cx, cy - r * 0.38)
            path.cubicTo(cx + r * 0.66, cy - r * 1.24, cx + r * 1.60, cy - r * 0.10, cx, cy + r * 0.95)
            path.closeSubpath()
        return path

    def paintEvent(self, event):
        dark = bool(getattr(self.window(), "is_dark", False))
        base, active, soft = self.COLORS[self.kind]["dark" if dark else "light"]
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        bg_path = QPainterPath()
        bg_path.addRoundedRect(rect, 10, 10)
        p.fillPath(bg_path, QColor(soft if self._hover else ("#121212" if dark else "#fafaf7")))
        border = QColor(active if self._hover else ("#242728" if dark else "#e6e5e0"))
        pen = QPen(border)
        pen.setWidthF(1.0)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawPath(bg_path)

        color = QColor(active if self._hover else base)
        cx, cy = self.width() / 2.0, self.height() / 2.0
        r = min(self.width(), self.height()) * 0.27
        shape = self._shape(cx, cy, r)
        if self._hover and self.kind != "trash":
            p.fillPath(shape, color)
        else:
            sp = QPen(color)
            sp.setWidthF(1.35)
            sp.setJoinStyle(Qt.RoundJoin)
            sp.setCapStyle(Qt.RoundCap)
            p.setPen(sp)
            p.setBrush(Qt.NoBrush)
            p.drawPath(shape)


class PlayerButton(QAbstractButton):
    """自绘播放器按钮：中间是圆的播放/暂停，两侧是上一首/下一首。"""

    def __init__(self, kind, window, size=32):
        super().__init__(window)
        self.kind = kind
        self.win = window
        self._hover = False
        self.setFixedSize(size, size)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.NoFocus)

    def set_kind(self, kind):
        if kind != self.kind:
            self.kind = kind
            self.update()

    def enterEvent(self, event):
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def _colors(self):
        return PALETTE["dark" if self.win.is_dark else "light"]

    def paintEvent(self, _event):
        c = self._colors()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        w = self.width()
        h = self.height()
        cx, cy = w / 2.0, h / 2.0
        big = self.kind in ("play", "pause")

        if big:
            r = min(w, h) / 2.0 - 1
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(c["accent"]))
            p.setOpacity(0.86 if self._hover else 1.0)
            p.drawEllipse(QPointF(cx, cy), r, r)
            p.setOpacity(1.0)
            icon = QColor(c["panel"])
            s = r * 0.54
            if self.kind == "play":
                path = QPainterPath()
                path.moveTo(cx - s * 0.62, cy - s)
                path.lineTo(cx + s * 0.92, cy)
                path.lineTo(cx - s * 0.62, cy + s)
                path.closeSubpath()
                pen = QPen(icon, s * 0.42)
                pen.setJoinStyle(Qt.RoundJoin)
                pen.setCapStyle(Qt.RoundCap)
                p.setPen(pen)
                p.setBrush(icon)
                p.drawPath(path)
            else:
                bw = s * 0.44
                p.setPen(Qt.NoPen)
                p.setBrush(icon)
                p.drawRoundedRect(QRectF(cx - s * 0.72, cy - s, bw, s * 2), bw * 0.5, bw * 0.5)
                p.drawRoundedRect(QRectF(cx + s * 0.28, cy - s, bw, s * 2), bw * 0.5, bw * 0.5)
        else:
            r = min(w, h) / 2.0 - 1
            if self._hover:
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(c["hover"]))
                p.drawEllipse(QPointF(cx, cy), r, r)
            col = QColor(c["accent"] if self._hover else c["fg"])
            col.setAlpha(255 if self._hover else 205)
            s = min(w, h) * 0.20
            bar_w = max(2.0, s * 0.36)
            if self.kind == "prev":
                bar_x = cx - s * 1.00
                base_x = cx + s * 0.66
                tip_x = cx - s * 0.44
            else:
                bar_x = cx + s * 1.00 - bar_w
                base_x = cx - s * 0.66
                tip_x = cx + s * 0.44
            p.setPen(Qt.NoPen)
            p.setBrush(col)
            p.drawRoundedRect(QRectF(bar_x, cy - s * 0.92, bar_w, s * 1.84), bar_w / 2, bar_w / 2)
            path = QPainterPath()
            path.moveTo(base_x, cy - s * 0.92)
            path.lineTo(tip_x, cy)
            path.lineTo(base_x, cy + s * 0.92)
            path.closeSubpath()
            pen = QPen(col, s * 0.34)
            pen.setJoinStyle(Qt.RoundJoin)
            pen.setCapStyle(Qt.RoundCap)
            p.setPen(pen)
            p.setBrush(col)
            p.drawPath(path)
        p.end()


# -------------------- 播放进度条 --------------------
class SeekSlider(QSlider):
    """点一下轨道就跳到那个位置 —— 只靠拖太费劲，尤其是长歌。"""

    def __init__(self, orientation=Qt.Horizontal, parent=None):
        super().__init__(orientation, parent)
        self.setCursor(Qt.PointingHandCursor)

    def _value_at(self, x):
        span = max(1.0, float(self.width() - 8))
        ratio = (float(x) - 4.0) / span
        ratio = max(0.0, min(1.0, ratio))
        return int(round(self.minimum() + ratio * (self.maximum() - self.minimum())))

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self.maximum() > self.minimum():
            self.setValue(self._value_at(event.position().x()))
            self.setSliderDown(True)
            self.sliderMoved.emit(self.value())
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.isSliderDown():
            self.setValue(self._value_at(event.position().x()))
            self.sliderMoved.emit(self.value())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self.isSliderDown():
            self.setSliderDown(False)
            self.sliderReleased.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)


# -------------------- 歌词层 --------------------
class LyricPanel(QWidget):
    """覆盖在列表上的沉浸歌词层：大封面 + 逐行滚动歌词。

    滚动与换歌都走 QPropertyAnimation：位置是浮点的（_pos），字号、颜色、
    透明度按「离当前行多远」连续插值，所以切行是滑过去的，不是硬跳。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_StyledBackground, False)
        self.hide()
        self.lines = []        # [(ms, text)]
        self.trans = []
        self.active = -1
        self.cover = None
        self.title = ""
        self.artist = ""
        self._pos = 0.0        # 当前滚到第几行（带小数：动画中间态）
        self._fade = 1.0       # 换歌词时的整体淡入
        self._anim = None
        self._fade_anim = None
        self._areas = {}       # 当前帧的可点区域（播放/上一首/下一首/格式/进度/返回）
        self._hot = None

    # ---- 动画属性 ----
    def _get_pos(self):
        return self._pos

    def _set_pos(self, value):
        self._pos = float(value)
        self.update()

    def _get_fade(self):
        return self._fade

    def _set_fade(self, value):
        self._fade = float(value)
        self.update()

    scrollPos = Property(float, _get_pos, _set_pos)
    fadeLevel = Property(float, _get_fade, _set_fade)

    def _stop_anim(self, anim):
        """停掉旧动画。DeleteWhenStopped 之后 Python 包装可能已经失效，兜一下。"""
        if anim is None:
            return
        try:
            anim.stop()
        except Exception:
            pass

    def _animate_pos(self, target, duration=320):
        self._stop_anim(self._anim)
        anim = QPropertyAnimation(self, b"scrollPos", self)
        anim.setDuration(int(duration))
        anim.setStartValue(float(self._pos))
        anim.setEndValue(float(target))
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.start(QAbstractAnimation.DeleteWhenStopped)
        self._anim = anim

    def _fade_in(self, duration=240):
        self._stop_anim(self._fade_anim)
        self._fade = 0.0
        anim = QPropertyAnimation(self, b"fadeLevel", self)
        anim.setDuration(int(duration))
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.start(QAbstractAnimation.DeleteWhenStopped)
        self._fade_anim = anim

    def set_track(self, track, cover_path=""):
        self.title = (track.title or track.name) if track else ""
        self.artist = track.artist if track else ""
        self.lines = []
        self.trans = []
        self._tr_map = {}
        self.active = -1
        self._pos = 0.0
        self.cover = None
        if cover_path and os.path.exists(cover_path):
            pix = QPixmap(cover_path)
            if not pix.isNull():
                self.cover = pix
        self._fade_in(220)
        self.update()
        win = self.window()
        if self.isVisible() and hasattr(win, "_apply_caption"):
            win._apply_caption()

    def set_lyrics(self, lines, trans=None):
        tr = trans
        if isinstance(lines, str) or isinstance(tr, str):
            try:
                import lyrics as lyric_mod
                if isinstance(lines, str):
                    lines = lyric_mod.parse_lrc(lines)
                if isinstance(tr, str):
                    tr = lyric_mod.parse_lrc(tr)
            except Exception:
                lines = []
                tr = []
        self.lines = list(lines or [])
        self.trans = list(tr or [])
        self._tr_map = {}
        for item in self.trans:
            try:
                self._tr_map[int(item[0])] = item[1]
            except Exception:
                pass
        self.active = -1
        self._stop_anim(self._anim)
        self._anim = None
        self._pos = 0.0
        self._fade_in(200)
        self.update()

    def current_line(self):
        """当前这句的 (原文, 译文)；没有歌词或还没唱到返回两个空串。"""
        idx = self.active
        if not self.lines or idx < 0 or idx >= len(self.lines):
            return "", ""
        try:
            at, text = self.lines[idx]
            tr = self._tr_map.get(int(at), "")
        except Exception:
            return "", ""
        return (text or "").strip(), (tr or "").strip()

    def set_position(self, ms):
        if not self.lines:
            return
        idx = -1
        for i, (at, _text) in enumerate(self.lines):
            if at <= ms:
                idx = i
            else:
                break
        if idx == self.active:
            return
        first = self.active < 0
        self.active = idx
        target = float(max(0, idx))
        if first:
            self._stop_anim(self._anim)
            self._anim = None
            self._pos = target
            self.update()
            return
        # 手动拖很远时别一帧帧滑过去，直接落位；正常播放则平滑滚动
        delta = abs(target - self._pos)
        if delta > 3.2:
            self._stop_anim(self._anim)
            self._anim = None
            self._pos = target
            self.update()
        else:
            self._animate_pos(target, 200 + int(min(delta, 3.0) * 70))

    # ================= 播放页 =================

    def _play_metrics(self):
        """播放页的几何：左栏封面 + 右侧歌词，整块垂直居中。"""
        w = max(640, self.width())
        h = max(460, self.height())
        pad = int(max(44, min(84, w * 0.059)))
        cover = int(min(316, h * 0.42))
        gap = 60
        left_w = cover + 44
        right_x = pad + left_w + gap
        block_h = (cover + 26 + 40 + 8 + 22 + 12 + 24 + 24 + 4 + 10 + 16
                   + 16 + 38)
        top = int(max(20, (h - block_h) / 2))
        return dict(w=w, h=h, pad=pad, cover=cover, left_w=left_w, gap=gap,
                    right_x=right_x, right_w=max(240, w - right_x - pad),
                    top=top, block_h=block_h)

    def _paint_backdrop(self, p, rect):
        """封面放大糊成底，再压一层径向暗角 —— 越靠边越黑，字才压得住。"""
        p.save()
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        p.fillRect(rect, QColor("#07070a"))
        src = self.cover
        if src is not None and not src.isNull():
            small = src.scaled(24, 24, Qt.KeepAspectRatioByExpanding,
                               Qt.SmoothTransformation)
            big = small.scaled(int(rect.width() * 1.16), int(rect.height() * 1.16),
                               Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
            dx = rect.left() - (big.width() - rect.width()) // 2
            dy = rect.top() - (big.height() - rect.height()) // 2
            p.setOpacity(0.42)
            p.drawPixmap(dx, dy, big)
            p.setOpacity(1.0)
        g = QRadialGradient(QPointF(rect.width() * 0.46, rect.height() * 0.42),
                            max(rect.width(), rect.height()) * 0.82)
        g.setColorAt(0.0, QColor(0, 0, 0, 88))
        g.setColorAt(0.62, QColor(0, 0, 0, 172))
        g.setColorAt(1.0, QColor(0, 0, 0, 232))
        p.fillRect(rect, QBrush(g))
        p.fillRect(rect, QColor(4, 4, 7, 132))
        p.restore()

    def _pill_size(self, p, label, height=24):
        f = p.font()
        f.setPointSizeF(9.0)
        f.setWeight(QFont.DemiBold)
        p.setFont(f)
        return 18 + p.fontMetrics().horizontalAdvance(label)

    def _draw_pills(self, p, track, x, y, height=24):
        """歌名下面那排格式胶囊：能播的格式都给，当前在播的那个亮一点。"""
        win = self.window()
        items = []
        try:
            files = win.format_files(track) if track is not None else []
        except Exception:
            files = []
        cur_ext = ""
        try:
            cur_ext = os.path.splitext(win.player.current_path() or "")[1].lower()
        except Exception:
            cur_ext = ""
        for key, path, _size in files:
            label = key.upper()
            on = bool(cur_ext) and cur_ext == os.path.splitext(path)[1].lower()
            items.append((label, on))
        if not items:
            items = [("MFLAC", False)]
        p.save()
        p.setRenderHint(QPainter.Antialiasing, True)
        gap = 6
        rects = []
        for label, on in items:
            w = self._pill_size(p, label, height)
            rects.append((QRectF(x, y, w, height), label, on))
            x += w + gap
        for r, label, on in rects:
            path = QPainterPath()
            path.addRoundedRect(r, height / 2.0, height / 2.0)
            p.fillPath(path, QColor(255, 255, 255, 40 if on else 16))
            f = p.font()
            f.setPointSizeF(9.0)
            f.setWeight(QFont.DemiBold)
            p.setFont(f)
            p.setPen(QColor(255, 255, 255, 235 if on else 160))
            p.drawText(r, Qt.AlignCenter, label)
        p.restore()
        return x

    def _draw_cover(self, p, rect, radius=18):
        p.save()
        p.setRenderHint(QPainter.Antialiasing, True)
        # 自绘阴影：不挂 QGraphicsDropShadowEffect，免得 grab / 缩放出岔子
        for i, alpha in ((22, 26), (12, 40), (5, 62)):
            path = QPainterPath()
            path.addRoundedRect(rect.adjusted(-i, -i + 10, i, i + 10),
                                radius + i, radius + i)
            p.fillPath(path, QColor(0, 0, 0, alpha))
        path = QPainterPath()
        path.addRoundedRect(rect, radius, radius)
        p.setClipPath(path)
        pix = cover_pixmap(self.cover, int(rect.width()), self.devicePixelRatioF()) \
            if self.cover is not None else None
        if pix is not None:
            p.drawPixmap(rect.toRect(), pix)
        else:
            grad = QLinearGradient(rect.topLeft(), rect.bottomRight())
            grad.setColorAt(0.0, QColor(28, 28, 34))
            grad.setColorAt(1.0, QColor(46, 46, 56))
            p.fillRect(rect, grad)
            f = p.font()
            f.setPointSizeF(26)
            p.setFont(f)
            p.setPen(QColor(255, 255, 255, 90))
            p.drawText(rect, Qt.AlignCenter, "\u266a")
        p.restore()

    def _draw_controls(self, p, areas):
        win = self.window()
        playing = False
        try:
            playing = win.player.is_playing()
        except Exception:
            playing = False
        p.save()
        p.setRenderHint(QPainter.Antialiasing, True)
        # 上一首 / 下一首：白色描边三角
        for kind in ("prev", "next"):
            r = areas[kind]
            cx, cy = r.center().x(), r.center().y()
            s = r.height() * 0.26
            col = QColor(255, 255, 255, 220)
            p.setPen(Qt.NoPen)
            p.setBrush(col)
            if kind == "prev":
                bar_x, base_x, tip_x = cx - s * 1.05, cx + s * 0.72, cx - s * 0.42
            else:
                bar_x, base_x, tip_x = cx + s * 1.05 - 2.4, cx - s * 0.72, cx + s * 0.42
            p.drawRoundedRect(QRectF(bar_x, cy - s, 2.4, s * 2), 1.2, 1.2)
            path = QPainterPath()
            path.moveTo(base_x, cy - s)
            path.lineTo(tip_x, cy)
            path.lineTo(base_x, cy + s)
            path.closeSubpath()
            p.drawPath(path)
        # 播放 / 暂停：跟主界面播放条同一套 —— 实心强调色圆 + 反色图标
        r = areas["play"]
        win_pal = PALETTE["dark" if getattr(win, "is_dark", False) else "light"]
        hot = getattr(self, "_hot", None) == "play"
        p.setPen(Qt.NoPen)
        p.setOpacity(0.86 if hot else 1.0)
        p.setBrush(QColor(win_pal["accent"]))
        p.drawEllipse(r.center(), r.width() / 2.0, r.height() / 2.0)
        p.setOpacity(1.0)
        cx, cy = r.center().x(), r.center().y()
        p.setBrush(QColor(win_pal["panel"]))
        if playing:
            bw = r.width() * 0.11
            p.drawRoundedRect(QRectF(cx - bw * 2.4, cy - r.height() * 0.15, bw, r.height() * 0.3), bw / 2, bw / 2)
            p.drawRoundedRect(QRectF(cx + bw * 1.4, cy - r.height() * 0.15, bw, r.height() * 0.3), bw / 2, bw / 2)
        else:
            path = QPainterPath()
            s = r.height() * 0.17
            path.moveTo(cx - s * 0.7, cy - s)
            path.lineTo(cx + s, cy)
            path.lineTo(cx - s * 0.7, cy + s)
            path.closeSubpath()
            p.drawPath(path)
        # 格式按钮
        fr = areas["fmt"]
        path = QPainterPath()
        path.addRoundedRect(fr, 8, 8)
        p.setPen(Qt.NoPen)
        p.fillPath(path, QColor(255, 255, 255, 22))
        label = "格式"
        try:
            cur = (win.fmt_btn.text() or "").replace("\u25be", "").strip()
            if cur:
                label = cur
        except Exception:
            pass
        f = p.font()
        f.setPointSizeF(10.0)
        p.setFont(f)
        p.setPen(QColor(255, 255, 255, 205))
        p.drawText(fr.adjusted(0, 0, -14, 0), Qt.AlignCenter, label)
        p.setPen(QPen(QColor(255, 255, 255, 170), 1.4))
        cx2, cy2 = fr.right() - 11, fr.center().y()
        p.drawLine(QPointF(cx2 - 3, cy2 - 1.6), QPointF(cx2, cy2 + 1.4))
        p.drawLine(QPointF(cx2 + 3, cy2 - 1.6), QPointF(cx2, cy2 + 1.4))
        p.restore()

    def _draw_left(self, p, m, areas):
        win = self.window()
        pad, top, cover = m["pad"], m["top"], m["cover"]
        p.save()
        p.setRenderHint(QPainter.Antialiasing, True)
        self._draw_cover(p, QRectF(pad, top, cover, cover))
        y = top + cover + 26
        f = p.font()
        f.setPointSizeF(22.0)
        f.setWeight(QFont.Bold)
        p.setFont(f)
        p.setPen(QColor(255, 255, 255, 246))
        p.drawText(QRectF(pad, y, m["left_w"], 40), Qt.AlignLeft | Qt.AlignVCenter,
                   self.title or "\u672a\u5728\u64ad\u653e")
        y += 40 + 8
        f.setPointSizeF(10.5)
        f.setWeight(QFont.Normal)
        p.setFont(f)
        p.setPen(QColor(255, 255, 255, 158))
        p.drawText(QRectF(pad, y, m["left_w"], 22), Qt.AlignLeft | Qt.AlignVCenter,
                   self.artist or "")
        y += 22 + 12
        self._draw_pills(p, getattr(win, "_current_track", None), pad, y)
        y += 24 + 24
        # 细进度条
        dur = 1
        pos = 0
        try:
            dur = max(1, win.player.duration())
            pos = max(0, min(dur, win.player.position()))
        except Exception:
            pass
        ratio = pos / float(dur)
        track = QRectF(pad, y, cover, 4)
        path = QPainterPath()
        path.addRoundedRect(track, 2, 2)
        p.fillPath(path, QColor(255, 255, 255, 58))
        if ratio > 0:
            fill = QRectF(pad, y, max(4.0, cover * ratio), 4)
            path = QPainterPath()
            path.addRoundedRect(fill, 2, 2)
            p.fillPath(path, QColor(255, 255, 255, 240))
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 255, 255, 250))
        p.drawEllipse(QPointF(pad + cover * ratio, y + 2), 4.0, 4.0)
        areas["seek"] = QRectF(pad, y - 8, cover, 20)
        y += 4 + 10
        f.setPointSizeF(8.5)
        p.setFont(f)
        p.setPen(QColor(255, 255, 255, 150))
        pos_text = format_time(pos / 1000.0) if pos > 0 else "0:00"
        dur_text = format_time(dur / 1000.0) if dur > 0 else "0:00"
        p.drawText(QRectF(pad, y, 60, 16), Qt.AlignLeft | Qt.AlignVCenter, pos_text)
        p.drawText(QRectF(pad + cover - 60, y, 60, 16),
                   Qt.AlignRight | Qt.AlignVCenter, dur_text)
        y += 16 + 16
        areas["prev"] = QRectF(pad, y + 3, 32, 32)
        areas["play"] = QRectF(pad + 62, y, 38, 38)
        areas["next"] = QRectF(pad + 124, y + 3, 32, 32)
        areas["fmt"] = QRectF(pad + 188, y + 5, 84, 28)
        self._draw_controls(p, areas)
        p.restore()

    def paintEvent(self, event):
        m = self._play_metrics()
        ls = float(getattr(self.window(), "lyric_font_scale", 1.0) or 1.0)
        ls = max(0.8, min(1.6, ls))
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.TextAntialiasing, True)
        rect = QRectF(self.rect())
        self._paint_backdrop(p, rect)
        self._areas = {}

        # ---- 右栏歌词 ----
        rx = m["right_x"]
        rw = m["right_w"]
        line_h = 68.0
        center_y = m["h"] * 0.5
        p.setOpacity(max(0.0, min(1.0, self._fade)))
        if self.lines:
            base = max(0.0, float(self._pos))
            for i, (at, text) in enumerate(self.lines):
                off = i - base
                if off < -3.9 or off > 3.9:
                    continue
                d = abs(off)
                alpha = 1.0
                if d > 3.2:
                    alpha = max(0.0, 1.0 - (d - 3.2) / 0.8)
                y = center_y + off * line_h
                cur = d < 0.5
                f = p.font()
                if cur:
                    f.setPointSizeF(17.2 * ls)
                    f.setWeight(QFont.Bold)
                    col = QColor(255, 255, 255, int(252 * alpha))
                else:
                    f.setPointSizeF(14.2 * ls)
                    f.setWeight(QFont.DemiBold)
                    col = QColor(255, 255, 255, int(112 * alpha))
                p.setFont(f)
                p.setPen(col)
                p.drawText(QRectF(rx, y - 34, rw, 40),
                           Qt.AlignLeft | Qt.AlignVCenter, text)
                tr = self._tr_map.get(int(at), "")
                if tr and d < 3.4:
                    f.setPointSizeF(9.75 * ls)
                    f.setWeight(QFont.Normal)
                    p.setFont(f)
                    p.setPen(QColor(255, 255, 255, int(118 * min(1.0, alpha + 0.2))))
                    p.drawText(QRectF(rx, y + 6, rw, 22),
                               Qt.AlignLeft | Qt.AlignTop, tr)
        else:
            f = p.font()
            f.setPointSizeF(10.5 * ls)
            p.setFont(f)
            p.setPen(QColor(255, 255, 255, 130))
            p.drawText(QRectF(rx, 0, rw, m["h"]), Qt.AlignLeft | Qt.AlignVCenter,
                       "\u6682\u65f6\u6ca1\u6709\u627e\u5230\u8fd9\u9996\u6b4c\u7684\u6b4c\u8bcd")
        p.setOpacity(1.0)

        # ---- 左栏 ----
        self._draw_left(p, m, self._areas)

        # ---- 右上角返回主页 ----
        back = QRectF(rect.width() - 62, 18, 36, 36)
        self._areas["back"] = back
        hot = getattr(self, "_hot", None) == "back"
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 255, 255, 58 if hot else 32))
        p.drawEllipse(back)
        pen = QPen(QColor(255, 255, 255, 255 if hot else 206), 1.8)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        cx = back.center().x()
        cy = back.center().y()
        rr = 6.0
        p.drawLine(QPointF(cx - rr, cy - rr), QPointF(cx + rr, cy + rr))
        p.drawLine(QPointF(cx - rr, cy + rr), QPointF(cx + rr, cy - rr))
        p.end()

    # ---- 播放页点击 ----
    def _emit_seek(self, x):
        m = self._play_metrics()
        ratio = (float(x) - m["pad"]) / max(1.0, float(m["cover"]))
        ratio = max(0.0, min(1.0, ratio))
        win = self.window()
        try:
            dur = win.player.duration()
            if dur:
                win.player.seek(int(dur * ratio))
                self.update()
        except Exception:
            pass

    def mousePressEvent(self, event):
        if event.button() != Qt.LeftButton or not self._areas:
            super().mousePressEvent(event)
            return
        pos = event.position()
        win = self.window()
        areas = self._areas
        if areas.get("back") is not None and areas["back"].contains(pos):
            win.lyric_btn.setChecked(False)
            return
        if areas.get("play") is not None and areas["play"].contains(pos):
            win.player_toggle()
            self.update()
            return
        if areas.get("prev") is not None and areas["prev"].contains(pos):
            win.player_prev()
            self.update()
            return
        if areas.get("next") is not None and areas["next"].contains(pos):
            win.player_next()
            self.update()
            return
        if areas.get("fmt") is not None and areas["fmt"].contains(pos):
            try:
                win.show_format_menu(event.globalPosition().toPoint())
            except Exception:
                win.show_format_menu()
            return
        if areas.get("seek") is not None and areas["seek"].contains(pos):
            self._emit_seek(pos.x())
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if not self._areas:
            super().mouseMoveEvent(event)
            return
        pos = event.position()
        hot = None
        for key in ("play", "prev", "next", "fmt", "seek", "back"):
            r = self._areas.get(key)
            if r is not None and r.contains(pos):
                hot = key
                break
        if hot != getattr(self, "_hot", None):
            self._hot = hot
            self.setCursor(Qt.PointingHandCursor if hot else Qt.ArrowCursor)
            self.update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        self._hot = None
        self.setCursor(Qt.ArrowCursor)
        self.update()
        super().leaveEvent(event)

class LyricStrip(QWidget):
    """播放条上方那两行歌词：当前一句压着下一句，扫一眼就知道唱到哪。

    没有歌词时整条收起来，别白占一行高度。
    """

    def __init__(self, window):
        super().__init__(window)
        self.win = window
        self.setObjectName("lyricStrip")
        self.setFixedHeight(48)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.lines = []
        self.trans = []
        self.index = -1
        self.hide()

    def sync_visibility(self):
        """歌词页开着的时候这条就收起来 —— 两处同时显示同一句歌词没有意义。"""
        btn = getattr(self.win, "lyric_btn", None)
        panel_open = bool(btn is not None and btn.isChecked())
        self.setVisible(bool(self.lines) and not panel_open)

    def clear(self):
        self.lines = []
        self.trans = []
        self.index = -1
        self.sync_visibility()

    def set_lyrics(self, lines, trans=None):
        self.lines = list(lines or [])
        self.trans = list(trans or [])
        self.index = -1
        self.sync_visibility()
        self.update()

    def set_position(self, ms):
        if not self.lines:
            return
        idx = -1
        for i, (at, _text) in enumerate(self.lines):
            if at <= ms:
                idx = i
            else:
                break
        idx = max(0, idx)
        if idx != self.index:
            self.index = idx
            self.update()

    def paintEvent(self, event):
        if not self.lines:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.TextAntialiasing, True)
        c = PALETTE["dark" if getattr(self.win, "is_dark", False) else "light"]
        # 这一条夹在页面卡片和播放条中间，铺满底色就成了一条灰带；只画字
        idx = max(0, min(self.index, len(self.lines) - 1))
        pair = []
        for offset in (0, 1):
            i = idx + offset
            if i < len(self.lines):
                pair.append(self.lines[i][1])
        if len(pair) == 1 and idx > 0:
            pair.insert(0, self.lines[idx - 1][1])
        if not pair:
            return

        width = max(160, min(int(self.width() * 0.72), 820))
        x = (self.width() - width) // 2

        font = QFont(self.font())
        font.setPointSizeF(11.0)
        font.setWeight(QFont.DemiBold)
        p.setFont(font)
        cur = p.fontMetrics().elidedText(pair[0], Qt.ElideRight, width)
        if len(pair) >= 2:
            font.setPointSizeF(9.0)
            font.setWeight(QFont.Normal)
            p.setFont(font)
            nxt = p.fontMetrics().elidedText(pair[1], Qt.ElideRight, width)
            p.setPen(QColor(c["accent"]))
            p.drawText(QRect(x, 3, width, 24), Qt.AlignCenter, cur)
            col = QColor(c["sub"])
            col.setAlpha(200)
            p.setPen(col)
            p.drawText(QRect(x, 27, width, 18), Qt.AlignCenter, nxt)
        else:
            p.setPen(QColor(c["accent"]))
            p.drawText(QRect(x, 12, width, 24), Qt.AlignCenter, cur)


class ProgressToast(QFrame):
    """转换进度浮窗。

    置顶、不激活、鼠标穿透 —— 它只是让人瞄一眼进度，不接管任何操作：
    用户在转换期间照样能点列表、拖滚动条、切分类。
    """

    def __init__(self, host):
        super().__init__(None)
        self.host = host
        self.setWindowFlags(Qt.Tool | Qt.FramelessWindowHint
                            | Qt.WindowStaysOnTopHint | Qt.WindowTransparentForInput)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setFixedSize(340, 108)
        self.title = "正在准备…"
        self.note = ""
        self.phase = ""
        self.done = 0
        self.total = 0
        self.ratio = 0.0
        self.ok_count = -1
        self._fail = 0
        self._skip = 0
        self._elapsed = 0.0
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self.hide)

    # ---- 数据 ----
    def begin(self, total=0):
        self.title = "正在准备…"
        self.phase = ""
        self.done = 0
        self.total = int(total or 0)
        self.ratio = 0.0
        self.ok_count = -1
        self._fail = 0
        self._skip = 0
        self._elapsed = 0.0
        self.note = ("共 %d 首，正在解密…" % self.total) if self.total else "正在读取文件…"
        self._hide_timer.stop()
        self.reposition()
        self.show()
        self.raise_()
        self.update()

    def set_progress(self, phase, done, total, note=""):
        self.phase = phase or ""
        self.done = int(done or 0)
        self.total = int(total or 0)
        self.ratio = (float(self.done) / self.total) if self.total else 0.0
        self.title = ("正在解密 · %s" % self.phase) if self.phase else "正在解密"
        if note:
            self.note = note
        self._hide_timer.stop()
        if not self.isVisible():
            self.reposition()
            self.show()
        self.update()

    def set_current(self, text):
        if text:
            self.note = text
            self.update()

    def say(self, text):
        if text:
            self.note = text
            self.update()

    def finish(self, ok, fail, skip, elapsed):
        self.ok_count = int(ok or 0)
        self._fail = int(fail or 0)
        self._skip = int(skip or 0)
        self._elapsed = float(elapsed or 0)
        self.ratio = 1.0
        self.title = "解密完成" if self._fail == 0 else "解密结束"
        self.note = "成功 %d · 失败 %d · 跳过 %d · 用时 %s" % (
            self.ok_count, self._fail, self._skip, format_time(self._elapsed))
        if not self.isVisible():
            self.reposition()
            self.show()
        self.update()
        self._hide_timer.start(4500)

    def reposition(self):
        host = self.host
        try:
            geo = host.frameGeometry()
        except Exception:
            return
        x = geo.right() - self.width() - 16
        # 往上让开播放条，别挡住音量/歌词那排控件
        y = geo.bottom() - self.height() - 92
        screen = host.screen() if host is not None else None
        if screen is not None:
            avail = screen.availableGeometry()
            x = max(avail.left() + 8, min(x, avail.right() - self.width() - 8))
            y = max(avail.top() + 8, min(y, avail.bottom() - self.height() - 8))
        self.move(x, y)

    def paintEvent(self, _event):
        c = PALETTE["dark" if getattr(self.host, "is_dark", False) else "light"]
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        pad = 9
        card = QRectF(pad, pad, self.width() - pad * 2, self.height() - pad * 2)
        for i in range(5, 0, -1):                 # 很轻的外阴影，让卡片浮起来
            shadow = QPainterPath()
            shadow.addRoundedRect(card.adjusted(-i, -i + 3, i, i + 3), 15 + i, 15 + i)
            p.fillPath(shadow, QColor(0, 0, 0, 12))
        body = QPainterPath()
        body.addRoundedRect(card, 15, 15)
        p.fillPath(body, QColor(c["panel"]))
        p.setPen(QPen(QColor(c["line"]), 1))
        p.setBrush(Qt.NoBrush)
        p.drawPath(body)

        left = int(card.left()) + 16
        width = int(card.width()) - 32

        title_font = QFont(self.font())
        title_font.setPointSizeF(10)
        title_font.setWeight(QFont.DemiBold)
        p.setFont(title_font)
        p.setPen(QColor(c["fg"]))
        p.drawText(QRect(left, int(card.top()) + 12, width - 58, 20),
                   Qt.AlignLeft | Qt.AlignVCenter, self.title)

        pct_font = QFont(self.font())
        pct_font.setPointSizeF(9)
        p.setFont(pct_font)
        p.setPen(QColor(c["sub"]))
        pct = "100%" if self.ok_count >= 0 else "%d%%" % int(max(0.0, min(1.0, self.ratio)) * 100)
        p.drawText(QRect(left + width - 58, int(card.top()) + 12, 58, 20),
                   Qt.AlignRight | Qt.AlignVCenter, pct)

        bar_y = int(card.top()) + 42
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(c["chip"]))
        p.drawRoundedRect(QRectF(left, bar_y, width, 6), 3, 3)
        fill_w = width * max(0.0, min(1.0, self.ratio))
        if fill_w > 0.5:
            p.setBrush(QColor(c["ok"] if self.ok_count >= 0 else c["accent"]))
            p.drawRoundedRect(QRectF(left, bar_y, fill_w, 6), 3, 3)

        note_font = QFont(self.font())
        note_font.setPointSizeF(8.6)
        p.setFont(note_font)
        p.setPen(QColor(c["sub"]))
        note_rect = QRect(left, bar_y + 12, width, 18)
        if self.ok_count < 0 and self.total:
            counter = "%d / %d" % (self.done, self.total)
            cw = p.fontMetrics().horizontalAdvance(counter) + 10
            note_rect.setWidth(max(40, width - cw))
            p.drawText(QRect(left + width - cw, bar_y + 12, cw, 18),
                       Qt.AlignRight | Qt.AlignVCenter, counter)
        note = p.fontMetrics().elidedText(self.note or "", Qt.ElideRight, note_rect.width())
        p.drawText(note_rect, Qt.AlignLeft | Qt.AlignVCenter, note)


class NotifyToast(QFrame):
    """屏幕级的轻提示：不抢焦点、不挡鼠标，几秒后自己消失。

    和 ProgressToast 分开，是因为这个只在出错的瞬间露一下脸 ——
    用模态对话框会把用户从手上的操作里拽出来，太重了。
    """

    def __init__(self, host=None):
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint
                         | Qt.WindowStaysOnTopHint | Qt.WindowTransparentForInput)
        self.host = host
        self._text = ""
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setFixedHeight(48)
        font = QFont(self.font())
        font.setPointSizeF(10.5)
        self.setFont(font)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide)
        self._set_width(240)

    def _set_width(self, width):
        self.setFixedWidth(max(200, min(860, int(width))))

    def show_message(self, text, duration=3000):
        self._text = text or ""
        fm = QFontMetrics(self.font())
        self._set_width(fm.horizontalAdvance(self._text) + 64)
        self.reposition()
        self.update()
        self.show()
        self.raise_()
        self._timer.start(duration)

    def reposition(self):
        host = self.host
        if host is not None and host.isVisible():
            geo = host.frameGeometry()
            x = geo.center().x() - self.width() // 2
            y = geo.top() + int(geo.height() * 0.66)
        else:
            screen = (host.screen() if host is not None else None) or QApplication.primaryScreen()
            geo = screen.availableGeometry()
            x = geo.center().x() - self.width() // 2
            y = geo.center().y()
        scr = (host.screen() if host is not None else None) or QApplication.primaryScreen()
        area = scr.availableGeometry()
        x = max(area.left() + 12, min(x, area.right() - self.width() - 12))
        y = max(area.top() + 12, min(y, area.bottom() - self.height() - 12))
        self.move(x, y)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.TextAntialiasing, True)
        c = PALETTE["dark" if getattr(self.host, "is_dark", False) else "light"]
        body = QRectF(6, 6, self.width() - 12, self.height() - 12)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 26))
        p.drawRoundedRect(body.adjusted(0, 2, 0, 2), 12, 12)
        p.setBrush(QColor(c["fg"]))
        p.drawRoundedRect(body, 12, 12)
        p.setPen(QColor(c["panel"]))
        text = p.fontMetrics().elidedText(self._text, Qt.ElideRight, int(body.width()) - 36)
        p.drawText(body, Qt.AlignCenter, text)


# -------------------- 设置页 --------------------
class SettingsDialog(QDialog):
    """配色方案、字号、文件名格式、缓存，都收在这一页。

    改一下立刻生效并落盘，没有「确定 / 取消」——用户不用猜自己到底存没存。
    """

    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.setObjectName("settingsDlg")
        self.setWindowTitle("设置")
        self.setModal(False)
        self.setMinimumSize(660, 560)
        self.resize(780, 700)
        self._building = True
        self.scheme_chips = {}
        self.color_swatches = {}
        self._build()
        self._building = False

    # ---------- 搭界面 ----------
    def _card(self, title, hint=""):
        card = QFrame()
        card.setObjectName("setCard")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(17, 14, 17, 14)
        lay.setSpacing(9)
        cap = QLabel(title)
        cap.setObjectName("setCap")
        lay.addWidget(cap)
        if hint:
            note = QLabel(hint)
            note.setObjectName("setHint")
            note.setWordWrap(True)
            lay.addWidget(note)
        return card, lay

    NAV_ITEMS = (("look", "\u5916\u89c2"), ("format", "\u8bc6\u522b\u683c\u5f0f"),
                 ("behavior", "\u884c\u4e3a"), ("cache", "\u7f13\u5b58"),
                 ("about", "\u5173\u4e8e"))

    def _make_page(self, cards):
        """一页一个滚动区：卡片竖着排，窗口再矮也不会互相挤。"""
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setObjectName("setScroll")
        body = QWidget()
        body.setObjectName("setBody")
        col = QVBoxLayout(body)
        col.setContentsMargins(24, 22, 26, 22)
        col.setSpacing(12)
        for card in cards:
            col.addWidget(card)
        col.addStretch(1)
        scroll.setWidget(body)
        return scroll

    def _switch_page(self, key):
        keys = [k for k, _label in self.NAV_ITEMS]
        if key not in keys:
            key = keys[0]
        self.stack.setCurrentIndex(keys.index(key))
        for k, btn in self.nav_buttons.items():
            btn.setChecked(k == key)

    def _build(self):
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        nav = QFrame()
        nav.setObjectName("setNav")
        nav.setFixedWidth(190)
        nav_lay = QVBoxLayout(nav)
        nav_lay.setContentsMargins(16, 22, 14, 18)
        nav_lay.setSpacing(3)
        title = QLabel("\u8bbe\u7f6e")
        title.setObjectName("setNavTitle")
        nav_lay.addWidget(title)
        nav_lay.addSpacing(14)

        self.nav_buttons = {}
        group = QButtonGroup(self)
        group.setExclusive(True)
        for key, label in self.NAV_ITEMS:
            btn = QPushButton(label)
            btn.setObjectName("setNavBtn")
            btn.setCheckable(True)
            btn.setFixedHeight(34)
            btn.setCursor(Qt.PointingHandCursor)
            btn.clicked.connect(lambda _=False, k=key: self._switch_page(k))
            group.addButton(btn)
            nav_lay.addWidget(btn)
            self.nav_buttons[key] = btn
        nav_lay.addStretch(1)

        done = QPushButton("\u5b8c\u6210")
        done.setObjectName("setDoneBtn")
        done.setFixedHeight(32)
        done.setCursor(Qt.PointingHandCursor)
        done.clicked.connect(self.close)
        nav_lay.addWidget(done)
        root.addWidget(nav)

        self.stack = QStackedWidget()
        self.stack.setObjectName("setStack")
        pages = (
            ("look", (self._build_scheme_card(), self._build_color_card(),
                      self._build_font_card())),
            ("format", (self._build_name_card(),)),
            ("behavior", (self._build_behavior_card(),)),
            ("cache", (self._build_cache_card(),)),
            ("about", (self._build_about_card(),)),
        )
        for _key, cards in pages:
            self.stack.addWidget(self._make_page(cards))
        root.addWidget(self.stack, 1)
        self._switch_page("look")

    def _build_scheme_card(self):
        card, lay = self._card(
            "配色方案",
            "浅色、深色是内置的；自定义最多存三套，每套记住自己的底色和颜色。")
        row = QWidget()
        row_lay = QHBoxLayout(row)
        row_lay.setContentsMargins(0, 0, 0, 0)
        row_lay.setSpacing(8)
        for scheme_id in ("light", "dark", "custom0", "custom1", "custom2"):
            chip = QPushButton("")
            chip.setObjectName("schemeChip")
            chip.setCheckable(True)
            chip.setFixedHeight(34)
            chip.setCursor(Qt.PointingHandCursor)
            chip.clicked.connect(lambda _=False, sid=scheme_id: self._on_scheme(sid))
            self.scheme_chips[scheme_id] = chip
            row_lay.addWidget(chip, 1)
        lay.addWidget(row)
        return card

    def _build_color_card(self):
        card, lay = self._card("自定义颜色", "选一套自定义配色后，点色块就能改颜色。")
        # 内置方案下整块收起来，别让首屏被六个色块占满
        self.color_body = QWidget()
        body_lay = QVBoxLayout(self.color_body)
        body_lay.setContentsMargins(0, 0, 0, 0)
        body_lay.setSpacing(9)
        grid = QWidget()
        grid_lay = QGridLayout(grid)
        grid_lay.setContentsMargins(0, 0, 0, 0)
        grid_lay.setHorizontalSpacing(10)
        grid_lay.setVerticalSpacing(8)
        for row, key in enumerate(SCHEME_EDIT_KEYS):
            name = QLabel(SCHEME_EDIT_LABELS[key])
            name.setObjectName("setLabel")
            grid_lay.addWidget(name, row, 0)
            swatch = QPushButton("")
            swatch.setObjectName("swatchBtn")
            swatch.setFixedSize(58, 24)
            swatch.setCursor(Qt.PointingHandCursor)
            swatch.setToolTip("点一下换个颜色")
            swatch.clicked.connect(lambda _=False, k=key: self._edit_color(k))
            grid_lay.addWidget(swatch, row, 1)
            value = QLabel("")
            value.setObjectName("setValue")
            grid_lay.addWidget(value, row, 2, 1, 3)
            self.color_swatches[key] = (swatch, value)
        grid_lay.setColumnStretch(2, 1)
        body_lay.addWidget(grid)
        self.color_tip = QLabel("")
        self.color_tip.setObjectName("setHint")
        self.color_tip.setWordWrap(True)
        body_lay.addWidget(self.color_tip)
        self.color_body.setVisible(False)
        lay.addWidget(self.color_body)
        self.color_card = card
        return card

    def _slider_row(self, lay, text, low, high, slot):
        row = QWidget()
        row_lay = QHBoxLayout(row)
        row_lay.setContentsMargins(0, 0, 0, 0)
        row_lay.setSpacing(10)
        name = QLabel(text)
        name.setObjectName("setLabel")
        name.setFixedWidth(88)
        row_lay.addWidget(name)
        slider = QSlider(Qt.Horizontal)
        slider.setObjectName("setSlider")
        slider.setRange(low, high)
        slider.setSingleStep(5)
        slider.setPageStep(10)
        slider.setTracking(False)       # 拖动时不反复重刷，松手才应用
        slider.valueChanged.connect(slot)
        row_lay.addWidget(slider, 1)
        value = QLabel("")
        value.setObjectName("setValue")
        value.setFixedWidth(48)
        value.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        row_lay.addWidget(value)
        lay.addWidget(row)
        return slider, value

    def _build_font_card(self):
        card, lay = self._card("字号", "只影响界面显示，不影响导出的文件。")
        self.font_slider, self.font_value = self._slider_row(
            lay, "主界面字号", 85, 135, self._on_font)
        self.lyric_slider, self.lyric_value = self._slider_row(
            lay, "歌词字号", 80, 160, self._on_lyric_font)
        return card

    def _build_name_card(self):
        card, lay = self._card("文件名格式", "决定怎么从文件名里拆出歌名和歌手。")
        self.name_radios = {}
        row = QWidget()
        row_lay = QHBoxLayout(row)
        row_lay.setContentsMargins(0, 0, 0, 0)
        row_lay.setSpacing(18)
        for key, label in NAME_FORMATS:
            rb = QRadioButton(label)
            rb.setObjectName("setRadio")
            rb.setCursor(Qt.PointingHandCursor)
            rb.toggled.connect(lambda on, k=key: self._on_name(on, k))
            self.name_radios[key] = rb
            row_lay.addWidget(rb)
        row_lay.addStretch(1)
        lay.addWidget(row)
        return card

    def _build_behavior_card(self):
        card, lay = self._card("行为", "窗口关闭方式、后台运行、系统通知与媒体键的统一设置。")
        self.behavior_checks = {}
        rows = (
            ("tray", "关闭主窗口时收进系统托盘，解密任务继续在后台执行"),
            ("notice", "解密完成后发送系统通知（窗口处于前台时不发送）"),
            ("hotkeys", "启用全局媒体键：播放 / 暂停、上一首、下一首"),
            ("mini", "启用迷你播放器窗口（显示歌词与播放控制）"),
        )
        for key, text in rows:
            cb = QCheckBox(text)
            cb.setObjectName("setCheck")
            cb.toggled.connect(lambda on, k=key: self._on_behavior(k, on))
            lay.addWidget(cb)
            self.behavior_checks[key] = cb
        self.hotkey_tip = QLabel("")
        self.hotkey_tip.setObjectName("setHint")
        self.hotkey_tip.setWordWrap(True)
        self.hotkey_tip.hide()
        lay.addWidget(self.hotkey_tip)
        return card

    def _on_behavior(self, key, on):
        if self._building:
            return
        win = self.win
        if key == "tray":
            win.ui_tray = bool(on)
            win.settings.setValue("ui/tray", "1" if on else "0")
            if win._tray is not None:
                if on:
                    win._tray.show()
                else:
                    win._tray.hide()
        elif key == "notice":
            win.ui_notice = bool(on)
            win.settings.setValue("ui/notice", "1" if on else "0")
        elif key == "hotkeys":
            win.play_hotkeys = bool(on)
            win.settings.setValue("play/hotkeys", "1" if on else "0")
            if on:
                win._install_media_hook()
            else:
                win._uninstall_media_hook()
            self._refresh_hotkey_tip()
        elif key == "mini":
            if hasattr(win, "mini_btn"):
                win.mini_btn.setChecked(bool(on))
        win.settings.sync()

    def _refresh_hotkey_tip(self):
        tip = getattr(self, "hotkey_tip", None)
        if tip is None:
            return
        error = getattr(self.win, "_media_key_error", "")
        if self.win.play_hotkeys and error:
            tip.setText("媒体键没启用：%s" % error)
            tip.show()
        else:
            tip.hide()

    def _build_cache_card(self):
        card, lay = self._card("缓存", "封面、歌词都缓存在这里；换位置或清掉都不会动歌。")
        self.cache_label = QLabel("")
        self.cache_label.setObjectName("setValue")
        self.cache_label.setWordWrap(True)
        self.cache_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(self.cache_label)
        row = QWidget()
        row_lay = QHBoxLayout(row)
        row_lay.setContentsMargins(0, 0, 0, 0)
        row_lay.setSpacing(8)
        for text, slot in (("更改位置", self._change_cache),
                           ("打开目录", self._open_cache),
                           ("清理缓存", self._clear_cache)):
            btn = QPushButton(text)
            btn.setObjectName("setBtn")
            btn.setFixedHeight(30)
            btn.setCursor(Qt.PointingHandCursor)
            btn.clicked.connect(slot)
            row_lay.addWidget(btn)
        row_lay.addStretch(1)
        lay.addWidget(row)
        self.cache_size = QLabel("")
        self.cache_size.setObjectName("setHint")
        lay.addWidget(self.cache_size)
        return card

    def _build_about_card(self):
        card, lay = self._card("关于")
        row = QWidget()
        row_lay = QHBoxLayout(row)
        row_lay.setContentsMargins(0, 0, 0, 0)
        row_lay.setSpacing(8)
        self.version_label = QLabel("TuneFree v%s" % VERSION)
        self.version_label.setObjectName("setValue")
        self.version_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        row_lay.addWidget(self.version_label)
        row_lay.addStretch(1)
        self.check_btn = QPushButton("检查更新")
        self.check_btn.setObjectName("setBtn")
        self.check_btn.setFixedHeight(30)
        self.check_btn.setMinimumWidth(92)
        self.check_btn.setCursor(Qt.PointingHandCursor)
        self.check_btn.clicked.connect(self._check_update)
        row_lay.addWidget(self.check_btn)
        lay.addWidget(row)

        # 自动检查更新和使用帮助同一行：左边是个开关，右边是个入口
        opt_row = QWidget()
        opt_lay = QHBoxLayout(opt_row)
        opt_lay.setContentsMargins(0, 0, 0, 0)
        opt_lay.setSpacing(10)
        self.auto_update = QCheckBox("启动时自动检查更新")
        self.auto_update.toggled.connect(self._on_auto_update)
        opt_lay.addWidget(self.auto_update)
        opt_lay.addStretch(1)
        self.help_btn = QPushButton("使用帮助")
        self.help_btn.setObjectName("setBtn")
        self.help_btn.setFixedHeight(30)
        self.help_btn.setCursor(Qt.PointingHandCursor)
        self.help_btn.clicked.connect(self._open_help)
        opt_lay.addWidget(self.help_btn)
        lay.addWidget(opt_row)

        # 手动点「检查更新」后，结果写在这行；平时收起来，别白占一行
        self.check_tip = QLabel("")
        self.check_tip.setObjectName("setHint")
        self.check_tip.setWordWrap(True)
        self.check_tip.setVisible(False)
        lay.addWidget(self.check_tip)
        return card

    # ---------- 回填 ----------
    def _current_custom_index(self):
        scheme = str(getattr(self.win, "scheme", "") or "")
        if scheme.startswith("custom"):
            text = scheme[6:]
            if text.isdigit():
                index = int(text)
                if 0 <= index < len(self.win.scheme_slots) and self.win.scheme_slots[index]:
                    return index
        return -1

    def reload(self):
        self._building = True
        win = self.win
        for scheme_id, chip in self.scheme_chips.items():
            if scheme_id == "light":
                chip.setText("浅色")
            elif scheme_id == "dark":
                chip.setText("深色")
            else:
                index = int(scheme_id[6:])
                slot = win.scheme_slots[index] if index < len(win.scheme_slots) else None
                if slot:
                    chip.setText(slot.get("name") or ("自定义 %d" % (index + 1)))
                else:
                    chip.setText("＋ 自定义 %d" % (index + 1))
            chip.setChecked(win.scheme == scheme_id)

        custom = self._current_custom_index()
        slot = win.scheme_slots[custom] if custom >= 0 else None
        base = slot["base"] if slot else ("dark" if win.is_dark else "light")
        for key, (swatch, value) in self.color_swatches.items():
            picked = (slot.get("colors") or {}).get(key, "") if slot else ""
            color = picked or PALETTE[base][key]
            swatch.setStyleSheet(
                "QPushButton#swatchBtn { background: %s; border: 1px solid rgba(0,0,0,0.16);"
                " border-radius: 6px; }" % color)
            value.setText(color.upper() + ("" if picked else "   · 默认"))
        self.color_tip.setText("")
        self.color_body.setVisible(bool(slot))
        self.color_card.setVisible(bool(slot))

        self.font_slider.setValue(int(round(win.font_scale * 100)))
        self.font_value.setText("%d%%" % int(round(win.font_scale * 100)))
        self.lyric_slider.setValue(int(round(win.lyric_font_scale * 100)))
        self.lyric_value.setText("%d%%" % int(round(win.lyric_font_scale * 100)))

        for key, rb in self.name_radios.items():
            rb.setChecked(key == win.name_format)

        path = win.cache_path()
        self.cache_label.setText(path)
        self.cache_label.setToolTip(path)
        self.cache_size.setText("当前占用 %s" % format_size(dir_size(path)))
        self.auto_update.setChecked(
            str(win.settings.value("ui/check_update", "1")) in ("1", "true", "True"))
        values = {
            "tray": win.ui_tray,
            "notice": win.ui_notice,
            "hotkeys": win.play_hotkeys,
            "mini": win.ui_mini,
        }
        for key, cb in getattr(self, "behavior_checks", {}).items():
            cb.setChecked(bool(values.get(key, False)))
        self._refresh_hotkey_tip()
        self._building = False

    # ---------- 动作 ----------
    def _on_scheme(self, scheme_id):
        if self._building:
            return
        win = self.win
        if scheme_id.startswith("custom"):
            index = int(scheme_id[6:])
            slots = win.scheme_slots
            if not slots[index]:
                slots[index] = {"base": "dark" if win.is_dark else "light",
                                "colors": {}, "name": "自定义 %d" % (index + 1)}
                save_scheme_slots(win.settings, slots)
        win.apply_scheme(scheme_id, animate=False)
        self.reload()
        win.polish_dialog(self)

    def _edit_color(self, key):
        index = self._current_custom_index()
        if index < 0:
            self.win.notify("先选一套自定义配色，再改颜色")
            return
        slot = self.win.scheme_slots[index]
        current = (slot.get("colors") or {}).get(key) or PALETTE[slot["base"]][key]
        color = QColorDialog.getColor(
            QColor(current), self, "选择「%s」颜色" % SCHEME_EDIT_LABELS[key])
        if not color.isValid():
            return
        slot.setdefault("colors", {})[key] = color.name()
        save_scheme_slots(self.win.settings, self.win.scheme_slots)
        self.win.apply_scheme(self.win.scheme, animate=False)
        self.reload()
        self.win.polish_dialog(self)

    def _on_font(self, value):
        if self._building:
            return
        self.font_value.setText("%d%%" % value)
        self.win.set_font_scale(value / 100.0)

    def _on_lyric_font(self, value):
        if self._building:
            return
        self.lyric_value.setText("%d%%" % value)
        self.win.set_lyric_font_scale(value / 100.0)

    def _on_name(self, on, key):
        if self._building or not on:
            return
        self.win.set_name_format(key)

    def _change_cache(self):
        self.win.change_cache_dir()
        self.reload()

    def _open_cache(self):
        path = self.win.cache_path()
        try:
            os.makedirs(path, exist_ok=True)
            os.startfile(path)
        except Exception as exc:
            self.win.info("打不开这个目录", str(exc))

    def _clear_cache(self):
        self.win.clear_cache()
        self.reload()

    # ---------- 关于 ----------
    def _check_update(self):
        if self._building:
            return
        self.check_btn.setEnabled(False)
        self.check_btn.setText("检查中…")
        self.check_tip.setText("正在连更新服务器…")
        self.win._settings_check_hook = self._after_check
        self.win.start_update_check(manual=True)

    def _after_check(self, text):
        """检查结束后由主窗口回调：按钮复原，把结果写在下边一行。"""
        self.check_btn.setEnabled(True)
        self.check_btn.setText("检查更新")
        self.check_tip.setText(text or "")
        self.check_tip.setVisible(bool(text))
        self.win.polish_dialog(self)

    def _on_auto_update(self, checked):
        if self._building:
            return
        settings = self.win.settings
        settings.setValue("ui/check_update", "1" if checked else "0")
        settings.sync()

    def _open_help(self):
        if self._building:
            return
        self.win.open_help()

    def closeEvent(self, event):
        # 检查更新还没回来就关了设置页，别把回调留给已销毁的对话框
        win = self.win
        if getattr(win, "_settings_check_hook", None) == self._after_check:
            win._settings_check_hook = None
        super().closeEvent(event)


# -------------------- 主窗口 --------------------
class TuneFreeWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("TuneFree")
        icon = app_icon()
        if icon is not None:
            self.setWindowIcon(icon)
        self.resize(1180, 780)
        self.setMinimumSize(980, 620)

        self.settings, self._settings_path = make_settings()
        self.is_dark = False
        self.scheme = "light"                 # 当前配色方案：light / dark / custom0-2
        self.scheme_slots = load_scheme_slots(self.settings)
        self.font_scale = 1.0
        self.lyric_font_scale = 1.0           # 歌词字号缩放
        self.filter_popup = None

        self.tracks = []
        self.rows = []
        self.checked = set()
        self._out_rows = {}

        self.sort_key = "title"
        self.sort_desc = False
        self.name_format = NAME_FORMATS[0][0]      # 文件名拆分格式，restore_settings 会覆盖
        self.filter_kind = "all"
        self.filter_status = "all"
        self.filter_artist = set()
        self.search_text = ""

        self.select_mode = False
        self.show_status = False
        self.session = None
        self._scan_session = None
        self._attach_thread = None
        self._attach_next_try = 0.0
        self._playlist_tracks = []
        self._tree_syncing = False

        self.input_dir = ""
        self._runner = None
        self._runner_thread = None
        self._lyric_req = None
        self._lyric_cache = {}
        self._current_track = None

        self.bridge = RunnerBridge()
        self.bridge.event.connect(self.on_runner_event)

        self.player = Player(self)
        self.player.trackChanged.connect(self.on_player_track)
        self.player.positionChanged.connect(self.on_position)
        self.player.stateChanged.connect(self.on_player_state)
        self.player.durationChanged.connect(self.on_duration)
        self.player.failed.connect(self.on_player_failed)
        self.player.finished.connect(self.on_player_finished)
        self.player.volumeChanged.connect(self.on_volume)

        self.update_checker = UpdateChecker()
        self.update_checker.found.connect(self.on_update_found)
        self.update_checker.failed.connect(self.on_update_failed)

        # 第 11 节那几项：托盘、通知、媒体键、迷你播放器
        self.ui_tray = True
        self.ui_notice = True
        self.play_hotkeys = True
        self.ui_mini = False
        self._tray = None
        self._tray_hint_shown = False
        self._really_quit = False
        self._mini = None
        self._mini_placed = False
        self._media_hook = None
        self._media_key_error = ""
        self._drag_active = False
        self._shortcuts = []
        self._settings_dlg = None

        self.setup_ui()
        self._setup_shell()
        self.restore_settings()
        self._apply_behavior_settings()
        self.apply_stylesheet()
        self.refresh_view()

        # 每 2 秒确认一次 QQ 音乐还活着：进程被杀了要立刻变回「未连接」
        self.conn_timer = QTimer(self)
        self.conn_timer.setInterval(2000)
        self.conn_timer.timeout.connect(self.check_connection)

        # 转换过程中分类计数一直在变，但每首歌都重建整棵树太重：
        # 攒起来，600ms 内没有新事件才真刷一次
        self._side_refresh_timer = QTimer(self)
        self._side_refresh_timer.setSingleShot(True)
        self._side_refresh_timer.setInterval(600)
        self._side_refresh_timer.timeout.connect(self.flush_side_refresh)
        self.conn_timer.start()

    # ================= 托盘 / 快捷键 / 拖拽 / 媒体键 =================
    def _setup_shell(self):
        """窗口级小功能：托盘、应用内快捷键、拖拽导入。"""
        self._setup_tray()
        self._setup_shortcuts()
        self.setAcceptDrops(True)
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)

    # ---------- 托盘常驻 ----------
    def _setup_tray(self):
        if not QSystemTrayIcon.isSystemTrayAvailable():
            self._tray = None
            return
        icon = app_icon() or self.windowIcon()
        tray = QSystemTrayIcon(icon, self)
        tray.setToolTip("TuneFree")
        self._tray_menu = QMenu(self)
        self._tray_menu.aboutToShow.connect(self._refresh_tray_menu)
        self._tray_show_act = self._tray_menu.addAction("显示主界面", self._show_from_tray)
        self._tray_play_act = self._tray_menu.addAction("播放 / 暂停", self.player_toggle)
        self._tray_menu.addAction("上一首", self.player_prev)
        self._tray_menu.addAction("下一首", self.player_next)
        self._tray_menu.addSeparator()
        self._tray_menu.addAction("退出 TuneFree", self.quit_app)
        tray.setContextMenu(self._tray_menu)
        tray.activated.connect(self._on_tray_activated)
        tray.hide()
        self._tray = tray

    def _refresh_tray_menu(self):
        if getattr(self, "_tray_show_act", None) is not None:
            self._tray_show_act.setText("隐藏主界面" if self.isVisible() else "显示主界面")
        if getattr(self, "_tray_play_act", None) is not None:
            self._tray_play_act.setText("暂停" if self.player.is_playing() else "播放")

    def _on_tray_activated(self, reason):
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self._show_from_tray()
        elif reason == QSystemTrayIcon.MiddleClick:
            self.player_toggle()

    def _show_from_tray(self):
        if self.isVisible() and not self.isMinimized():
            self.hide()
            return
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def quit_app(self):
        self._really_quit = True
        self.close()

    # ---------- 应用内快捷键 ----------
    def _setup_shortcuts(self):
        self._shortcuts = []
        specs = (
            ("Ctrl+O", self.choose_folder),
            ("Ctrl+F", self.focus_search),
            ("Ctrl+R", self.restart_scan),
            ("Ctrl+,", self.show_settings),
            ("Ctrl+Q", self.quit_app),
            ("F1", self.open_help),
            ("Delete", self._delete_shortcut),
            ("Escape", self._escape_shortcut),
        )
        for seq, slot in specs:
            shortcut = QShortcut(QKeySequence(seq), self)
            shortcut.setContext(Qt.WindowShortcut)
            shortcut.activated.connect(slot)
            self._shortcuts.append(shortcut)

    def focus_search(self):
        box = getattr(self, "search_box", None)
        if box is not None:
            box.setFocus(Qt.ShortcutFocusReason)
            box.selectAll()

    def restart_scan(self):
        if self._runner is not None:
            self.notify("正在解密，等这一轮跑完再重新扫描")
            return
        if self.input_dir and os.path.isdir(self.input_dir):
            self.start_scan()

    def _delete_shortcut(self):
        if self.select_mode and self.checked:
            self.delete_selected()

    def _escape_shortcut(self):
        if self.lyric_panel.isVisible():
            if self.lyric_btn.isChecked():
                self.lyric_btn.setChecked(False)
            else:
                self.toggle_lyrics(False)
        elif self.select_mode:
            self.select_btn.setChecked(False)
        elif self._mini is not None and self._mini.isVisible():
            self._close_mini()

    @staticmethod
    def _text_editing():
        widget = QApplication.focusWidget()
        if widget is None:
            return False
        return isinstance(widget, (QLineEdit, QTextEdit, QPlainTextEdit, QAbstractSpinBox))

    # ---------- 全局媒体键 ----------
    def on_media_key(self, name):
        if name == "play_pause":
            self.player_toggle()
        elif name == "next":
            self.player_next()
        elif name == "prev":
            self.player_prev()
        elif name == "stop":
            self.player.stop()
            self.on_player_state("stopped")

    def _install_media_hook(self):
        if os.name != "nt":
            self._media_key_error = "只有 Windows 支持全局媒体键"
            return
        if self._media_hook is None:
            hook = MediaKeyHook(self)
            hook.mediaKey.connect(self.on_media_key)
            self._media_hook = hook
        if self._media_hook.install():
            self._media_key_error = ""
        else:
            self._media_key_error = self._media_hook.error() or "媒体键注册失败"

    def _uninstall_media_hook(self):
        if self._media_hook is not None:
            self._media_hook.uninstall()

    def _apply_behavior_settings(self):
        tray = self._tray
        if tray is not None:
            if self.ui_tray:
                tray.show()
            else:
                tray.hide()
        if self.play_hotkeys:
            self._install_media_hook()
        if self.ui_mini and hasattr(self, "mini_btn"):
            self.mini_btn.setChecked(True)

    # ---------- 系统通知 ----------
    def notify_system(self, title, text):
        """托盘气泡通知；托盘不可用时返回 False，调用方可以退回应用内提示。"""
        tray = self._tray
        if tray is not None and tray.isVisible():
            try:
                tray.showMessage(title, text, QSystemTrayIcon.Information, 6000)
                return True
            except Exception:
                pass
        return False

    def _notify_run_done(self, ok, fail, skip, elapsed):
        if not self.ui_notice:
            return
        if self.isVisible() and not self.isMinimized() and self.isActiveWindow():
            return          # 人就在窗口前面，完成对话框已经说过了
        text = "成功 %d · 失败 %d · 跳过 %d · 用时 %s" % (ok, fail, skip, format_time(elapsed))
        self.notify_system("解密完成", text)

    # ---------- 拖拽导入文件夹 ----------
    @staticmethod
    def _dropped_folder(mime):
        for url in mime.urls():
            if not url.isLocalFile():
                continue
            path = url.toLocalFile()
            if os.path.isdir(path):
                return path
            if os.path.isfile(path):
                return os.path.dirname(path)
        return ""

    def dragEnterEvent(self, event):
        if self._dropped_folder(event.mimeData()):
            event.acceptProposedAction()
            self._set_drop_hint(True)

    def dragMoveEvent(self, event):
        if self._dropped_folder(event.mimeData()):
            event.acceptProposedAction()

    def dragLeaveEvent(self, event):
        self._set_drop_hint(False)

    def dropEvent(self, event):
        self._set_drop_hint(False)
        folder = self._dropped_folder(event.mimeData())
        if not folder:
            return
        event.acceptProposedAction()
        if self._runner is not None:
            self.notify("正在解密，等这一轮跑完再换文件夹")
            return
        self.set_input_dir(folder)

    def _set_drop_hint(self, on):
        if on == self._drag_active:
            return
        self._drag_active = on
        btn = getattr(self, "folder_btn", None)
        if btn is None:
            return
        btn.setProperty("drop", "true" if on else "false")
        btn.style().unpolish(btn)
        btn.style().polish(btn)
        if on:
            btn.setText("松开导入")
        else:
            name = os.path.basename(self.input_dir.rstrip("\\/")) if self.input_dir else ""
            btn.setText(name or "选择存放 .mflac / .mgg 的文件夹…")

    # ---------- 迷你播放器 ----------
    def _ensure_mini(self):
        if self._mini is not None:
            return
        mini = MiniPlayer()
        mini.playPauseClicked.connect(self.player_toggle)
        mini.prevClicked.connect(self.player_prev)
        mini.nextClicked.connect(self.player_next)
        mini.restoreRequested.connect(self._restore_from_mini)
        mini.closeRequested.connect(self._close_mini)
        mini.seekRequested.connect(self._mini_seek)
        mini.movedByUser.connect(self._mini_moved)
        self._mini = mini
        self._sync_mini_theme()
        self._sync_mini_all()

    def _place_mini(self):
        """优先回到上次拖动的位置；记的位置已经不在屏幕里就贴右下角。"""
        mini = self._mini
        if mini is None:
            return
        screen = self.screen() or QApplication.primaryScreen()
        if screen is None:
            return
        area = screen.availableGeometry()
        spot = None
        raw = str(self.settings.value("ui/mini_pos", "") or "")
        if raw:
            try:
                parts = [int(v) for v in raw.split(",")[:2]]
                if len(parts) == 2:
                    spot = QPoint(parts[0], parts[1])
            except Exception:
                spot = None
        if spot is None:
            spot = QPoint(area.right() - mini.width() - 24,
                          area.bottom() - mini.height() - 24)
        else:
            # 屏幕换过或分辨率变了：夹回可见范围
            spot = QPoint(min(max(spot.x(), area.left()), area.right() - mini.width()),
                          min(max(spot.y(), area.top()), area.bottom() - mini.height()))
        mini.move(spot)
        self._mini_placed = True

    def _mini_moved(self, pos):
        try:
            self.settings.setValue("ui/mini_pos", "%d,%d" % (pos.x(), pos.y()))
            self.settings.sync()
        except Exception:
            pass

    def toggle_mini(self, checked):
        self.ui_mini = bool(checked)
        self.settings.setValue("ui/mini", "1" if checked else "0")
        self.settings.sync()
        if checked:
            self._ensure_mini()
            self._sync_mini_all()
            self._mini.show()
            self._place_mini()
            self._mini.raise_()
        elif self._mini is not None:
            self._mini.hide()

    def _restore_from_mini(self):
        self._close_mini()
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _close_mini(self):
        if self._mini is not None:
            self._mini.hide()
        if hasattr(self, "mini_btn"):
            self.mini_btn.blockSignals(True)
            self.mini_btn.setChecked(False)
            self.mini_btn.blockSignals(False)
        self.ui_mini = False
        self.settings.setValue("ui/mini", "0")
        self.settings.sync()

    def _mini_seek(self, fraction):
        duration = self.player.duration()
        if duration > 0:
            self.player.seek(int(duration * max(0.0, min(1.0, float(fraction)))))

    def _sync_mini_theme(self):
        mini = self._mini
        if mini is None:
            return
        base = "dark" if self.is_dark else "light"
        colors = dict(PALETTE[base])
        colors["accent2"] = "#7dd3fc" if self.is_dark else "#38bdf8"
        mini.set_theme(colors)

    def _sync_mini_track(self, track):
        if self._mini is not None:
            self._mini.set_lyrics("")
        mini = self._mini
        if mini is None or track is None:
            return
        cover = track.cover_path if track.cover_path and os.path.exists(track.cover_path) else ""
        mini.set_track(track.title or track.name, track.artist or "未知歌手", cover)

    def _sync_mini_playback(self):
        mini = self._mini
        if mini is None:
            return
        mini.set_playback(self.player.is_playing(), self.player.position(), self.player.duration())

    def _sync_mini_all(self):
        if self._mini is None:
            return
        self._sync_mini_track(self._current_track)
        self._sync_mini_playback()

    def _teardown_features(self):
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)
        if self._media_hook is not None:
            self._media_hook.uninstall()
        if self._mini is not None:
            self._mini.hide()
            self._mini = None
        if self._tray is not None:
            self._tray.hide()

    # ================= 界面搭建 =================
    def setup_ui(self):
        central = ShellWidget()
        central.setObjectName("central")
        central.setAttribute(Qt.WA_StyledBackground, True)
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self.build_top_bar())

        # 内容全部收进一张圆角页面卡片
        self.page_card = QFrame()
        self.page_card.setObjectName("pageCard")
        self.page_card.setAttribute(Qt.WA_StyledBackground, True)
        card_lay = QVBoxLayout(self.page_card)
        card_lay.setContentsMargins(0, 0, 0, 0)
        card_lay.setSpacing(0)
        card_lay.addWidget(self.build_hero())
        card_lay.addWidget(self.build_chips())
        self.toolbar = self.build_toolbar()
        card_lay.addWidget(self.toolbar)
        self.list_wrap = self.build_list_area()
        self.list_wrap.installEventFilter(self)
        card_lay.addWidget(self.list_wrap, 1)

        card_wrap = QWidget()
        card_wrap.setObjectName("cardWrap")
        card_wrap_lay = QHBoxLayout(card_wrap)
        card_wrap_lay.setContentsMargins(18, 4, 18, 8)
        card_wrap_lay.setSpacing(0)
        card_wrap_lay.addWidget(self.page_card)
        root.addWidget(card_wrap, 1)

        self.lyric_strip = LyricStrip(self)
        root.addWidget(self.lyric_strip)
        root.addWidget(self.build_player_bar())

        self.lyric_panel = LyricPanel(central)
        self.lyric_panel.hide()

        self.build_options_panel()

        self.toast = ProgressToast(self)
        self._run_total = 0
        self._run_done = 0
        self.notify_toast = NotifyToast(self)

    def notify(self, text, duration=3000):
        """屏幕中间飘一句提示，不打断手头的操作。"""
        self.notify_toast.show_message(text, duration)

    def build_top_bar(self):
        bar = QFrame()
        bar.setObjectName("topBar")
        bar.setAttribute(Qt.WA_StyledBackground, True)
        bar.setFixedHeight(64)
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(22, 0, 22, 0)
        lay.setSpacing(14)

        # 品牌：只留名字，不要小方块和版本号
        self.brand_label = QLabel("TuneFree")
        self.brand_label.setObjectName("brandName")
        lay.addWidget(self.brand_label)

        # 文件夹胶囊：直接显示当前目录名，点一下换目录
        self.folder_btn = QPushButton("选择存放 .mflac / .mgg 的文件夹…")
        self.folder_btn.setObjectName("folderBtn")
        self.folder_btn.setFixedHeight(36)
        self.folder_btn.setMaximumWidth(330)
        self.folder_btn.setMinimumWidth(180)
        self.folder_btn.setCursor(Qt.PointingHandCursor)
        self.folder_btn.setToolTip("选一个文件夹，程序会列出里面所有 .mflac / .mgg，解密成 FLAC / OGG")
        self.folder_btn.clicked.connect(self.choose_folder)
        lay.addWidget(self.folder_btn)

        lay.addStretch(1)

        self.search_box = QLineEdit()
        self.search_box.setObjectName("search")
        self.search_box.setPlaceholderText("搜索歌名、歌手")
        self.search_box.setFixedSize(260, 36)
        self.search_box.setClearButtonEnabled(True)
        self.search_box.textChanged.connect(self.on_search)
        lay.addWidget(self.search_box)

        self.conn_pill = QLabel("● 未连接")
        self.conn_pill.setObjectName("connPill")
        self.conn_pill.setFixedHeight(32)
        self.conn_pill.setAlignment(Qt.AlignCenter)
        self.conn_pill.setToolTip("QQ 音乐注入状态，转码前需要它是绿的")
        lay.addWidget(self.conn_pill, 0, Qt.AlignVCenter)

        self.settings_btn = QPushButton("设置")
        self.settings_btn.setObjectName("ghostBtn")
        self.settings_btn.setFixedHeight(32)
        self.settings_btn.setCursor(Qt.PointingHandCursor)
        self.settings_btn.setToolTip("设置")
        self.settings_btn.clicked.connect(self.show_settings)
        lay.addWidget(self.settings_btn)

        self.star_btn = GlyphButton("star", self, 34)
        self.star_btn.setToolTip("如果 TuneFree 帮到了你，去 GitHub 点个 Star")
        self.star_btn.clicked.connect(self.open_github)
        lay.addWidget(self.star_btn)

        self.support_btn = GlyphButton("heart", self, 34)
        self.support_btn.setToolTip("支持作者")
        self.support_btn.clicked.connect(self.open_support)
        lay.addWidget(self.support_btn)

        self.opt_btn = QPushButton("解密选项")
        self.opt_btn.setObjectName("pillBtn")
        self.opt_btn.setFixedHeight(36)
        self.opt_btn.setCursor(Qt.PointingHandCursor)
        self.opt_btn.setToolTip("是否自动补封面、是否重新解密")
        self.opt_btn.clicked.connect(self.show_options)
        lay.addWidget(self.opt_btn)

        self.start_btn = QPushButton("开始解密")
        self.start_btn.setObjectName("startBtn")
        self.start_btn.setFixedHeight(36)
        self.start_btn.setMinimumWidth(126)
        self.start_btn.setCursor(Qt.PointingHandCursor)
        self.start_btn.clicked.connect(self.on_start_clicked)
        lay.addWidget(self.start_btn)
        return bar

    def build_hero(self):
        hero = QWidget()
        hero.setObjectName("pageHero")
        lay = QVBoxLayout(hero)
        lay.setContentsMargins(26, 20, 26, 0)
        lay.setSpacing(0)
        self.hero_eyebrow = QLabel("LOCAL LIBRARY")
        self.hero_eyebrow.setObjectName("heroEyebrow")
        lay.addWidget(self.hero_eyebrow)
        self.hero_title = QLabel("我的曲库")
        self.hero_title.setObjectName("heroTitle")
        lay.addWidget(self.hero_title)
        lay.addSpacing(7)
        self.count_label = QLabel("")
        self.count_label.setObjectName("heroMeta")
        lay.addWidget(self.count_label)
        return hero

    def build_chips(self):
        box = QWidget()
        box.setObjectName("chipRow")
        lay = QHBoxLayout(box)
        lay.setContentsMargins(26, 16, 26, 4)
        lay.setSpacing(8)
        self.chips = {}
        self.chip_order = []

        def add(key, text, kind="chip"):
            chip = ChipButton(self, text, key, kind)
            if kind == "sep":
                chip.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            else:
                chip.clicked.connect(lambda _=False, c=chip: self.on_chip_clicked(c))
                chip.checkToggled.connect(self.on_chip_check)
            lay.addWidget(chip)
            self.chips[key] = chip
            self.chip_order.append(chip)
            return chip

        add("all", "全部")
        add("enc", "未解密")
        add("sep1", "", "sep")
        add("mflac", "MFLAC")
        add("mgg", "MGG")
        add("sep2", "", "sep")
        add("flac", "FLAC")
        add("ogg", "OGG")
        lay.addStretch(1)
        self.filter_chip = add("filter", "筛选", "filter")
        return box

    def build_toolbar(self):
        bar = ToolbarBar(self)
        bar.sortRequested.connect(self.set_sort)
        bar.select_btn.toggled.connect(self.set_select_mode)
        return bar

    def build_list_area(self):
        wrap = QWidget()
        wrap.setObjectName("listWrap")
        lay = QVBoxLayout(wrap)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        # 选择模式的「全选 / 已选中 N 项 / 删除」挂到工具行右端
        self.all_btn = QPushButton("全选")
        self.all_btn.setObjectName("toolSort")
        self.all_btn.setCursor(Qt.PointingHandCursor)
        self.all_btn.clicked.connect(self.toggle_select_all)
        self.all_btn.hide()

        self.select_bar = QWidget()
        sel_lay = QHBoxLayout(self.select_bar)
        sel_lay.setContentsMargins(0, 0, 0, 0)
        sel_lay.setSpacing(6)
        self.sel_count = QLabel("已选中 0 项")
        self.sel_count.setObjectName("selCount")
        sel_lay.addWidget(self.sel_count)
        self.del_btn = QPushButton("删除")
        self.del_btn.setObjectName("toolDanger")
        self.del_btn.setCursor(Qt.PointingHandCursor)
        self.del_btn.setFocusPolicy(Qt.NoFocus)
        self.del_btn.setToolTip("删除选中的歌曲")
        self.del_btn.clicked.connect(self.delete_selected)
        sel_lay.addWidget(self.del_btn)
        self.select_bar.setVisible(False)
        self.toolbar.bind_select(self.all_btn, self.select_bar)

        self.view = TrackView(self)
        self.model = TrackModel(self)
        self.view.setModel(self.model)
        lay.addWidget(self.view, 1)

        self.empty_hint = QLabel("选一个文件夹，这里会列出所有 .mflac / .mgg")
        self.empty_hint.setObjectName("metaText")
        self.empty_hint.setAlignment(Qt.AlignCenter)
        self.empty_hint.setParent(self.view.viewport())
        self.empty_hint.hide()
        return wrap

    def build_options_panel(self):
        panel = QFrame(self)
        panel.setObjectName("optPanel")
        panel.setWindowFlags(Qt.Popup | Qt.FramelessWindowHint)
        panel.setAttribute(Qt.WA_StyledBackground, True)
        panel.setFixedWidth(272)
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(8)

        title = QLabel("解密选项")
        title.setObjectName("optTitle")
        lay.addWidget(title)
        hint = QLabel("这些设置对下一次点「开始解密」生效")
        hint.setObjectName("optHint")
        hint.setWordWrap(True)
        lay.addWidget(hint)

        line = QFrame()
        line.setObjectName("divider")
        line.setFixedHeight(1)
        lay.addWidget(line)

        self.cb_cover = QCheckBox("没有封面时自动补上")
        self.cb_cover.setToolTip("从 QQ 音乐的专辑图缓存里挑最大的一张写进文件")
        self.cb_force = QCheckBox("重新解密已存在的文件")
        self.cb_force.setToolTip("默认跳过已经解出来的，勾上则覆盖重解")
        for cb in (self.cb_cover, self.cb_force):
            cb.stateChanged.connect(self.on_options_changed)
            lay.addWidget(cb)

        self.opt_panel = panel
        return panel

    def show_options(self):
        panel = self.opt_panel
        panel.adjustSize()
        pos = self.opt_btn.mapToGlobal(QPoint(0, self.opt_btn.height() + 6))
        screen = self.screen().availableGeometry() if self.screen() else QRect(pos, panel.size())
        if pos.x() + panel.width() > screen.right():
            pos.setX(screen.right() - panel.width() - 8)
        panel.move(pos)
        panel.show()
        panel.raise_()

    def on_options_changed(self, *_args):
        try:
            self.settings.setValue("ui/cover", "1" if self.cb_cover.isChecked() else "0")
            self.settings.setValue("ui/force", "1" if self.cb_force.isChecked() else "0")
        except Exception:
            pass

    def update_volume_icon(self, value=None):
        if not hasattr(self, "vol_icon"):
            return
        level = self.volume.value() if value is None else int(value)
        color = PALETTE["dark" if self.is_dark else "light"]["sub"]
        self.vol_icon.setPixmap(volume_pixmap(level, color))

    def build_player_bar(self):
        wrap = QWidget()
        wrap.setObjectName("playerWrap")
        wrap_lay = QHBoxLayout(wrap)
        wrap_lay.setContentsMargins(18, 12, 18, 14)
        wrap_lay.setSpacing(0)

        bar = QFrame()
        bar.setObjectName("playerBar")
        bar.setFixedHeight(78)
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(18, 0, 18, 0)
        lay.setSpacing(16)

        # 左：封面 + 歌名/歌手，固定 230 免得歌名长短把中间挤歪
        now = QWidget()
        now.setFixedWidth(230)
        now_lay = QHBoxLayout(now)
        now_lay.setContentsMargins(0, 0, 0, 0)
        now_lay.setSpacing(11)

        self.now_cover = QLabel()
        self.now_cover.setObjectName("nowCover")
        self.now_cover.setFixedSize(42, 42)
        self.now_cover.setAlignment(Qt.AlignCenter)
        self.now_cover.setText("\u266a")
        self.now_cover.setCursor(Qt.PointingHandCursor)
        self.now_cover.setToolTip("\u70b9\u5f00\u6c89\u6d78\u9875")
        self.now_cover.installEventFilter(self)
        now_lay.addWidget(self.now_cover)

        info = QVBoxLayout()
        info.setSpacing(3)
        info.setContentsMargins(0, 0, 0, 0)
        self.now_title = QLabel("\u672a\u5728\u64ad\u653e")
        self.now_title.setObjectName("nowTitle")
        self.now_artist = QLabel("\u53cc\u51fb\u5217\u8868\u91cc\u7684\u6b4c\u540d\u5f00\u59cb\u64ad\u653e")
        self.now_artist.setObjectName("nowArtist")
        for lbl in (self.now_title, self.now_artist):
            lbl.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        info.addWidget(self.now_title)
        info.addWidget(self.now_artist)
        now_lay.addLayout(info, 1)
        lay.addWidget(now)

        # 中：上一首 / 播放 / 下一首
        self.prev_btn = PlayerButton("prev", self, 30)
        self.prev_btn.setToolTip("\u4e0a\u4e00\u9996")
        self.prev_btn.clicked.connect(self.player_prev)
        self.play_btn = PlayerButton("play", self, 38)
        self.play_btn.setToolTip("\u64ad\u653e / \u6682\u505c")
        self.play_btn.clicked.connect(self.player_toggle)
        self.next_btn = PlayerButton("next", self, 30)
        self.next_btn.setToolTip("\u4e0b\u4e00\u9996")
        self.next_btn.clicked.connect(self.player_next)

        ctrl = QWidget()
        ctrl_lay = QHBoxLayout(ctrl)
        ctrl_lay.setContentsMargins(0, 0, 0, 0)
        ctrl_lay.setSpacing(8)
        ctrl_lay.addWidget(self.prev_btn)
        ctrl_lay.addWidget(self.play_btn)
        ctrl_lay.addWidget(self.next_btn)
        lay.addWidget(ctrl)

        # 右：时间 + 进度
        timeline = QWidget()
        tl = QHBoxLayout(timeline)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.setSpacing(10)
        self.time_cur = QLabel("0:00")
        self.time_cur.setObjectName("timeText")
        self.time_cur.setFixedWidth(34)
        self.time_cur.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.time_total = QLabel("0:00")
        self.time_total.setObjectName("timeText")
        self.time_total.setFixedWidth(34)
        self.time_total.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.seek = SeekSlider(Qt.Horizontal)
        self.seek.setObjectName("seek")
        self.seek.setRange(0, 1000)
        self.seek.sliderMoved.connect(self.on_seek)
        tl.addWidget(self.time_cur)
        tl.addWidget(self.seek, 1)
        tl.addWidget(self.time_total)
        lay.addWidget(timeline, 1)

        # 最右：格式 / 音量 / 迷你 / 歌词
        extra = QWidget()
        ex = QHBoxLayout(extra)
        ex.setContentsMargins(0, 0, 0, 0)
        ex.setSpacing(8)

        self.fmt_btn = QPushButton("格式 \u25be")
        self.fmt_btn.setObjectName("fmtBtn")
        self.fmt_btn.setFixedHeight(28)
        self.fmt_btn.setCursor(Qt.PointingHandCursor)
        self.fmt_btn.setToolTip("\u5207\u6362\u8fd9\u9996\u6b4c\u7684\u64ad\u653e\u683c\u5f0f")
        self.fmt_btn.clicked.connect(lambda: self.show_format_menu())
        ex.addWidget(self.fmt_btn)

        self.vol_icon = QLabel()
        self.vol_icon.setObjectName("volIcon")
        self.vol_icon.setFixedSize(18, 18)
        self.vol_icon.setToolTip("\u97f3\u91cf")
        ex.addWidget(self.vol_icon)

        self.volume = QSlider(Qt.Horizontal)
        self.volume.setObjectName("volume")
        self.volume.setRange(0, 100)
        self.volume.setValue(80)
        self.volume.setFixedWidth(76)
        self.volume.setCursor(Qt.PointingHandCursor)
        self.volume.valueChanged.connect(self.player.set_volume)
        self.volume.valueChanged.connect(self.update_volume_icon)
        self.update_volume_icon()          # 先按当前音量画一次喇叭，不然空标签
        ex.addWidget(self.volume)

        self.mini_btn = QPushButton("\u8ff7\u4f60")
        self.mini_btn.setObjectName("ghostBtn")
        self.mini_btn.setFixedHeight(28)
        self.mini_btn.setCursor(Qt.PointingHandCursor)
        self.mini_btn.setCheckable(True)
        self.mini_btn.setToolTip("\u8ff7\u4f60\u64ad\u653e\u5668\uff1a\u7f29\u6210\u5c0f\u7a97\u6d6e\u5728\u684c\u9762\u89d2\u843d")
        self.mini_btn.toggled.connect(self.toggle_mini)
        ex.addWidget(self.mini_btn)

        self.lyric_btn = QPushButton("\u6c89\u6d78")
        self.lyric_btn.setObjectName("ghostBtn")
        self.lyric_btn.setFixedHeight(28)
        self.lyric_btn.setCursor(Qt.PointingHandCursor)
        self.lyric_btn.setCheckable(True)
        self.lyric_btn.setToolTip("\u6c89\u6d78\u9875\uff1a\u6574\u9875\u5c01\u9762 + \u4e2d\u82f1\u6b4c\u8bcd\uff08Esc \u9000\u51fa\uff09")
        self.lyric_btn.toggled.connect(self.toggle_lyrics)
        ex.addWidget(self.lyric_btn)
        lay.addWidget(extra)

        wrap_lay.addWidget(bar)
        self.player_bar = bar
        return wrap

    # ================= 主题 =================
    def apply_scheme(self, scheme_id, animate=True):
        """切配色方案：浅色 / 深色 / 三套自定义槽。自定义色直接覆盖内置调色板。"""
        base, colors = resolve_scheme(scheme_id, self.scheme_slots)
        colors = scheme_colors(base, colors)
        # 自绘控件（列表、侧栏、歌词、浮窗）读的是 PALETTE，得同步改，
        # 否则会出现「QSS 变绿了、胶囊还是橙的」这种半吊子效果。
        for name, factory in PALETTE_BASE.items():
            PALETTE[name] = dict(factory)
        PALETTE[base].update(colors)
        _THEME_OVERRIDES.clear()
        for name, value in colors.items():
            _THEME_OVERRIDES[(base, name)] = value
        self.scheme = scheme_id
        self.set_theme(base == "dark", animate=animate)
        try:
            self.settings.setValue("ui/scheme", scheme_id)
            self.settings.sync()
        except Exception:
            pass

    def repaint_surfaces(self):
        """自绘控件不走 QSS，换配色后要挨个叫醒它们重画。"""
        for widget in (self.view, self.toolbar, self.lyric_strip, self.lyric_panel):
            if widget is None:
                continue
            target = widget.viewport() if hasattr(widget, "viewport") else widget
            target.update()
        for widget in (getattr(self, "toast", None), getattr(self, "notify_toast", None)):
            if widget is not None:
                widget.update()
        shell = self.centralWidget()
        if shell is not None:
            shell.update()

    def set_theme(self, dark, animate=True):
        dark = bool(dark)
        if dark == self.is_dark:
            self.apply_stylesheet()
            self.repaint_surfaces()
            return
        ghost = None
        if animate and self.isVisible():
            try:
                ghost = QLabel(self)
                ghost.setPixmap(self.grab())
                ghost.setGeometry(0, 0, self.width(), self.height())
                ghost.setAttribute(Qt.WA_TransparentForMouseEvents, True)
                ghost.show()
                ghost.raise_()
            except Exception:
                ghost = None
        self.is_dark = dark
        self.apply_stylesheet()
        self.repaint_surfaces()
        self.update_volume_icon()
        if ghost is not None:
            effect = QGraphicsOpacityEffect(ghost)
            ghost.setGraphicsEffect(effect)
            anim = QPropertyAnimation(effect, b"opacity", ghost)
            anim.setDuration(260)
            anim.setStartValue(1.0)
            anim.setEndValue(0.0)
            anim.setEasingCurve(QEasingCurve.OutCubic)
            anim.finished.connect(ghost.deleteLater)
            anim.start(QAbstractAnimation.DeleteWhenStopped)
        try:
            self.settings.setValue("ui/dark", "1" if self.is_dark else "0")
        except Exception:
            pass

    def apply_stylesheet(self):
        qss = (DARK_STYLESHEET + UI_EXTRA_DARK) if self.is_dark else (LIGHT_STYLESHEET + UI_EXTRA_LIGHT)
        icon = check_icon_path()
        if icon:
            qss = qss.replace("{CHECK}", icon)
        dash = dash_icon_path()
        if dash:
            qss = qss.replace("{DASH}", dash)
        caret = caret_icon_path(self.is_dark)
        if caret:
            qss = qss.replace("{CARET}", caret)
        theme_key = "dark" if self.is_dark else "light"
        qss = remap_legacy_colors(qss, theme_key)
        qss = apply_theme_overrides(qss, theme_key)
        qss = scale_qss_fonts(qss, getattr(self, "font_scale", 1.0))
        # 拖文件夹进来时高亮「歌曲文件夹」按钮
        accent = PALETTE[theme_key]["accent"]
        qss = qss + ('\nQPushButton#folderBtn[drop="true"] { border: 1px solid %s;'
                     ' color: %s; }') % (accent, accent)
        # 新布局的样式：顶栏、页面卡片、工具行、列表底
        fs = getattr(self, "font_scale", 1.0)

        def px(v):
            return max(8, int(round(v * fs)))

        base = PALETTE[theme_key]
        vals = dict(base)
        vals.update({
            "fs_brand": px(15), "fs_ver": px(11), "fs_folder": px(12.5),
            "fs_search": px(12.5), "fs_conn": px(12), "fs_pill": px(12.5),
            "fs_hero": px(27), "fs_eyebrow": px(11), "fs_meta": px(12.5),
            "fs_tool": px(11.5), "fs_sel": px(11.5),
            "fs_fp": px(11.5), "fs_fpbtn": px(12),
            "fs_now": px(12.5), "fs_now2": px(11), "fs_time": px(10.5),
            "fs_fmt": px(11.5), "fs_ghost": px(11.5), "fs_cover": px(16),
            "fs_nav": px(12.5), "fs_navtitle": px(19),
            "fs_cap": px(13.5), "fs_hint": px(11.5), "fs_hint2": px(12),
        })
        extra = ("""
QFrame#topBar { background: %(bg)s; border: none; }
QLabel#brandName { color: %(fg)s; font-size: %(fs_brand)spx; font-weight: 700; }
QPushButton#folderBtn { background: %(chip)s; border: 1px solid %(line)s;
    border-radius: 10px; padding: 0 14px; color: %(fg2)s; font-size: %(fs_folder)spx;
    text-align: left; }
QPushButton#folderBtn:hover { border-color: %(accent_line)s; color: %(fg)s; }
QLineEdit#search { background: %(chip)s; border: 1px solid %(line)s; border-radius: 10px;
    padding: 0 12px; color: %(fg)s; font-size: %(fs_search)spx; }
QLineEdit#search:focus { border-color: %(accent)s; background: %(panel)s; }
QLabel#connPill { background: %(chip)s; border: 1px solid %(line)s; border-radius: 16px;
    padding: 0 13px; color: %(sub)s; font-size: %(fs_conn)spx; }
QLabel#connPill[ok="true"] { background: %(ok_soft)s; border-color: transparent; color: %(ok)s; }
QLabel#connPill[ok="false"] { background: %(chip)s; border-color: %(line)s; color: %(sub)s; }
QPushButton#pillBtn { background: %(chip)s; border: 1px solid %(line)s; border-radius: 10px;
    padding: 0 16px; color: %(fg2)s; font-size: %(fs_pill)spx; }
QPushButton#pillBtn:hover { border-color: %(accent_line)s; color: %(fg)s; }
QPushButton#startBtn { background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
    stop:0 %(accent)s, stop:1 %(accent2)s); border: none; border-radius: 10px;
    color: #ffffff; font-size: %(fs_pill)spx; font-weight: 600; padding: 0 18px; }
QPushButton#startBtn:hover { background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
    stop:0 %(accent2)s, stop:1 %(accent)s); }
QPushButton#startBtn:disabled { background: %(chip)s; color: %(sub)s; }
QFrame#pageCard { background: %(page)s; border: 1px solid %(line)s; border-radius: 18px; }
QWidget#pageHero { background: transparent; }
QLabel#heroEyebrow { color: %(accent)s; font-size: %(fs_eyebrow)spx; font-weight: 700; }
QLabel#heroTitle { color: %(fg)s; font-size: %(fs_hero)spx; font-weight: 700; }
QLabel#heroMeta { color: %(sub)s; font-size: %(fs_meta)spx; }
QWidget#toolbar { background: transparent; border-bottom: 1px solid %(line)s; }
QLabel#toolCap { color: %(sub2)s; font-size: %(fs_tool)spx; }
QLabel#toolLink { color: %(fg2)s; font-size: %(fs_tool)spx; }
QLabel#toolSep { color: %(sub2)s; font-size: %(fs_tool)spx; }
QPushButton#toolSort { background: transparent; border: none; color: %(fg2)s;
    font-size: %(fs_tool)spx; padding: 0 2px; }
QPushButton#toolSort:hover { color: %(fg)s; }
QPushButton#toolSort[active="true"] { color: %(accent)s; }
QLabel#selCount { color: %(sub)s; font-size: %(fs_sel)spx; }
QPushButton#toolDanger { background: transparent; border: none; color: %(fail)s;
    font-size: %(fs_sel)spx; padding: 0 4px; }
QPushButton#toolDanger:hover { color: %(fail)s; }
QPushButton#toolDanger:disabled { color: %(sub2)s; }
QWidget#listWrap { background: transparent; }
QListView#trackView { background: transparent; border: none; outline: none; }
QListView#trackView::item { background: transparent; border: none; }
QListView#trackView::item:selected { background: transparent; }
QFrame#filterPop { background: %(card)s; border: 1px solid %(line2)s; border-radius: 14px; }
QLineEdit#fpSearch { background: %(chip)s; border: 1px solid transparent; border-radius: 8px;
    padding: 0 10px; color: %(fg)s; font-size: %(fs_fp)spx;
    selection-background-color: %(accent)s; }
QLineEdit#fpSearch:focus { border: 1px solid %(accent)s; }
QListWidget#fpList { background: transparent; border: none; outline: none; }
QWidget#fpFoot { background: transparent; }
QPushButton#fpBtn { background: %(chip)s; border: none; border-radius: 8px;
    color: %(fg2)s; font-size: %(fs_fpbtn)spx; padding: 0 16px; }
QPushButton#fpBtn:hover { background: %(hover)s; color: %(fg)s; }
QPushButton#fpBtnPrimary { background: %(accent)s; border: none; border-radius: 8px;
    color: #ffffff; font-size: %(fs_fpbtn)spx; padding: 0 16px; }
QPushButton#fpBtnPrimary:hover { background: %(accent_line)s; }
QWidget#playerWrap { background: transparent; }
QFrame#playerBar { background: %(glass)s; border: 1px solid %(line)s; border-radius: 14px; }
QLabel#nowCover { background: %(chip)s; border-radius: 9px; color: %(sub2)s;
    font-size: %(fs_cover)spx; }
QLabel#nowTitle { color: %(fg)s; font-size: %(fs_now)spx; font-weight: 600; }
QLabel#nowArtist { color: %(sub)s; font-size: %(fs_now2)spx; }
QLabel#timeText { color: %(sub)s; font-size: %(fs_time)spx; }
QPushButton#fmtBtn { background: transparent; border: 1px solid %(line)s;
    border-radius: 8px; color: %(fg2)s; font-size: %(fs_fmt)spx; padding: 0 10px; }
QPushButton#fmtBtn:hover { border: 1px solid %(accent_line)s; color: %(accent)s; }
QPushButton#fmtBtn:pressed { background: %(chip)s; }
QPushButton#ghostBtn { background: transparent; border: 1px solid transparent;
    border-radius: 8px; color: %(fg2)s; font-size: %(fs_ghost)spx; padding: 0 10px; }
QPushButton#ghostBtn:hover { background: %(chip)s; color: %(fg)s; }
QPushButton#ghostBtn:checked { background: %(accent_soft)s; color: %(accent)s; }
QSlider#seek::groove:horizontal { height: 4px; background: %(chip)s; border-radius: 2px; }
QSlider#seek::sub-page:horizontal,
QSlider#seek::sub-page:horizontal:hover { border-radius: 2px;
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 %(accent)s, stop:1 %(accent2)s); }
QSlider#seek::add-page:horizontal { background: transparent; }
QSlider#seek::handle:horizontal { width: 10px; height: 10px; margin: -3px 0;
    border-radius: 5px; background: #ffffff; border: 1px solid rgba(0, 0, 0, 0.14); }
QSlider#seek::handle:horizontal:hover { width: 12px; height: 12px; margin: -4px 0;
    border-radius: 6px; }
QSlider#volume::groove:horizontal { height: 4px; background: %(chip)s; border-radius: 2px; }
QSlider#volume::sub-page:horizontal { background: %(fg2)s; border-radius: 2px; }
QSlider#volume::add-page:horizontal { background: %(chip)s; border-radius: 2px; }
QSlider#volume::handle:horizontal { width: 10px; height: 10px; margin: -3px 0;
    border-radius: 5px; background: #ffffff; border: 1px solid rgba(0, 0, 0, 0.14); }
QFrame#setNav { background: %(card)s; border-right: 1px solid %(line)s; }
QLabel#setNavTitle { color: %(fg)s; font-size: %(fs_navtitle)spx; font-weight: 700; }
QPushButton#setNavBtn { background: transparent; border: none; border-radius: 8px;
    color: %(sub)s; font-size: %(fs_nav)spx; text-align: left; padding: 0 12px; }
QPushButton#setNavBtn:hover { background: %(hover)s; color: %(fg)s; }
QPushButton#setNavBtn:checked { background: %(accent_soft)s; color: %(accent)s; font-weight: 600; }
QPushButton#setDoneBtn { background: %(chip)s; border: 1px solid %(line)s; border-radius: 8px;
    color: %(fg2)s; font-size: %(fs_nav)spx; }
QPushButton#setDoneBtn:hover { border: 1px solid %(accent_line)s; color: %(accent)s; }
QStackedWidget#setStack { background: %(page)s; }
QFrame#setCard { background: %(card)s; border: 1px solid %(line)s; border-radius: 14px; }
QLabel#setCap { color: %(fg)s; font-size: %(fs_cap)spx; font-weight: 700; }
QLabel#setHint { color: %(sub)s; font-size: %(fs_hint)spx; }
QLabel#setLabel { color: %(sub)s; font-size: %(fs_hint2)spx; }
QLabel#setValue { color: %(fg2)s; font-size: %(fs_hint2)spx; }
QPushButton#schemeChip { background: %(chip)s; border: 1px solid transparent;
    border-radius: 10px; color: %(sub)s; font-size: %(fs_hint2)spx; }
QPushButton#schemeChip:hover { background: %(hover)s; color: %(fg)s; }
QPushButton#schemeChip:checked { background: %(accent_soft)s; border: 1px solid %(accent_line)s;
    color: %(accent)s; font-weight: 600; }
QSlider#setSlider::groove:horizontal { height: 4px; background: %(chip)s; border-radius: 2px; }
QSlider#setSlider::sub-page:horizontal { background: %(accent)s; border-radius: 2px; }
QSlider#setSlider::handle:horizontal { width: 13px; height: 13px; margin: -5px 0;
    border-radius: 6px; background: #ffffff; border: 2px solid %(accent)s; }
QPushButton#swatchBtn { border-radius: 8px; border: 1px solid %(line)s; }
""" % vals)
        qss = qss + "\n" + extra
        self.setStyleSheet(qss)
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(qss)      # 弹窗是独立窗口，得靠全局表才吃到主题
        apply_titlebar_theme(self, self.is_dark)
        self._sync_mini_theme()

    def polish_dialog(self, dlg):
        """弹窗也要跟主题：标题栏 + 强制重刷样式。"""
        apply_titlebar_theme(dlg, self.is_dark)
        dlg.setStyleSheet(QApplication.instance().styleSheet() if QApplication.instance() else "")
        return dlg

    def info(self, title, text):
        box = self.polish_dialog(QMessageBox(QMessageBox.Information, title, text, QMessageBox.Ok, self))
        box.exec()

    # ================= 设置 =================
    def restore_settings(self):
        s = self.settings
        self.scheme_slots = load_scheme_slots(s)
        scheme = str(s.value("ui/scheme", "") or "").strip()
        if not scheme:
            # 老版本只存了 ui/dark，这里顺着升级过来
            dark = str(s.value("ui/dark", "0")) in ("1", "true", "True")
            scheme = "dark" if dark else "light"
        base, colors = resolve_scheme(scheme, self.scheme_slots)
        if scheme.startswith("custom"):
            index = scheme[6:]
            if not (index.isdigit() and 0 <= int(index) < len(self.scheme_slots)
                    and self.scheme_slots[int(index)]):
                scheme = base        # 槽位已空，退回它的基底
        self.scheme = scheme
        colors = scheme_colors(base, colors)
        for name, factory in PALETTE_BASE.items():
            PALETTE[name] = dict(factory)
        PALETTE[base].update(colors)
        _THEME_OVERRIDES.clear()
        for name, value in colors.items():
            _THEME_OVERRIDES[(base, name)] = value
        self.is_dark = base == "dark"
        self.font_scale = self._clamp_scale(s.value("ui/font_scale", 1.0), 0.85, 1.35)
        self.lyric_font_scale = self._clamp_scale(s.value("ui/lyric_font_scale", 1.0), 0.8, 1.6)

        mode = str(s.value("ui/name_format", NAME_FORMATS[0][0]))
        valid = [k for k, _ in NAME_FORMATS]
        self.name_format = mode if mode in valid else valid[0]

        self.cb_cover.setChecked(str(s.value("ui/cover", "1")) in ("1", "true", "True"))
        self.cb_force.setChecked(str(s.value("ui/force", "0")) in ("1", "true", "True"))

        # 第 11 节的行为开关：托盘常驻、完成通知、全局媒体键、迷你播放器
        self.ui_tray = str(s.value("ui/tray", "0")) in ("1", "true", "True")
        self.ui_notice = str(s.value("ui/notice", "1")) in ("1", "true", "True")
        self.play_hotkeys = str(s.value("play/hotkeys", "1")) in ("1", "true", "True")
        self.ui_mini = str(s.value("ui/mini", "0")) in ("1", "true", "True")

        last = str(s.value("ui/last_input", ""))
        if last and os.path.isdir(last):
            self.set_input_dir(last, remember=False)

        auto = str(s.value("ui/check_update", "1"))
        if auto in ("1", "true", "True"):
            QTimer.singleShot(1200, lambda: self.start_update_check(manual=False))

    def save_settings(self):
        s = self.settings
        s.setValue("ui/scheme", self.scheme)
        s.setValue("ui/dark", "1" if self.is_dark else "0")
        s.setValue("ui/font_scale", self.font_scale)
        s.setValue("ui/lyric_font_scale", self.lyric_font_scale)
        s.setValue("ui/cover", "1" if self.cb_cover.isChecked() else "0")
        s.setValue("ui/force", "1" if self.cb_force.isChecked() else "0")
        s.setValue("ui/last_input", self.input_dir)
        s.setValue("ui/name_format", self.name_format)
        s.setValue("ui/tray", "1" if self.ui_tray else "0")
        s.setValue("ui/notice", "1" if self.ui_notice else "0")
        s.setValue("play/hotkeys", "1" if self.play_hotkeys else "0")
        s.setValue("ui/mini", "1" if self.ui_mini else "0")
        s.sync()

    @staticmethod
    def _clamp_scale(value, low, high):
        try:
            value = float(value)
        except (TypeError, ValueError):
            return 1.0
        return max(low, min(high, value))

    # ================= 设置页的入口 =================
    def show_settings(self):
        dlg = getattr(self, "_settings_dlg", None)
        if dlg is None:
            dlg = SettingsDialog(self)
            self._settings_dlg = dlg
        dlg.reload()
        dlg.adjustSize()
        # adjustSize 会把窗口压到内容刚好大小，左导航 + 右卡片的版式得给足宽度
        if dlg.width() < 780 or dlg.height() < 640:
            dlg.resize(max(780, dlg.width()), max(640, dlg.height()))
        geo = self.frameGeometry()
        dlg.move(max(0, geo.center().x() - dlg.width() // 2),
                 max(0, geo.top() + (geo.height() - dlg.height()) // 2))
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()
        self.polish_dialog(dlg)      # 独立窗口吃不到主窗的新样式，得补一次

    def set_name_format(self, mode):
        valid = [k for k, _ in NAME_FORMATS]
        if mode not in valid or mode == self.name_format:
            return
        self.name_format = mode
        try:
            self.settings.setValue("ui/name_format", mode)
            self.settings.sync()
        except Exception:
            pass
        self.retitle_tracks()

    def set_font_scale(self, value, save=True):
        value = self._clamp_scale(value, 0.85, 1.35)
        if abs(value - self.font_scale) < 0.005:
            return
        self.font_scale = value
        self.apply_stylesheet()
        self.view.viewport().update()
        self.refresh_layout_metrics()
        if save:
            try:
                self.settings.setValue("ui/font_scale", value)
                self.settings.sync()
            except Exception:
                pass

    def refresh_layout_metrics(self):
        """字号变了，表头和列表都要重算尺寸。"""
        try:
            self.toolbar.update()
            for chip in getattr(self, "chips", {}).values():
                chip.update()
            self.view.doItemsLayout()
            self.view.viewport().update()
        except Exception:
            pass

    def set_lyric_font_scale(self, value, save=True):
        value = self._clamp_scale(value, 0.8, 1.6)
        if abs(value - self.lyric_font_scale) < 0.005:
            return
        self.lyric_font_scale = value
        self.lyric_panel.update()
        if save:
            try:
                self.settings.setValue("ui/lyric_font_scale", value)
                self.settings.sync()
            except Exception:
                pass

    def cache_path(self, *parts):
        root = cache_root(self.settings)
        return str(root.joinpath(*parts)) if parts else str(root)

    def change_cache_dir(self):
        start = self.cache_path()
        path = QFileDialog.getExistingDirectory(self, "选择缓存目录", start)
        if not path:
            return
        try:
            os.makedirs(path, exist_ok=True)
            settings = self.settings
            settings.setValue("cache/root", path)
            settings.sync()
        except Exception as exc:
            self.info("换个位置失败", str(exc))
            return
        self.notify("缓存目录已改到 " + path)

    def clear_cache(self):
        root = cache_root(self.settings)
        if not os.path.isdir(str(root)):
            self.notify("缓存本来就是空的")
            return
        removed = 0
        for name in os.listdir(str(root)):
            full = root / name
            try:
                if full.is_dir():
                    shutil.rmtree(str(full), ignore_errors=True)
                else:
                    os.remove(str(full))
                removed += 1
            except OSError:
                continue
        self.notify("已清理 %d 项缓存" % removed)

    # ================= 文件名格式 =================
    def _apply_name_format(self, tracks):
        for t in tracks or []:
            if getattr(t, "is_out", False):
                continue
            title, artist = split_name_by_format(t.name, self.name_format)
            t.title = title
            if artist or self.name_format == "title_only":
                t.artist = artist

    def retitle_tracks(self):
        """按当前格式重算歌名/歌手，然后刷新列表（不动文件、不重新扫描）。"""
        if not self.tracks:
            return
        self._apply_name_format(self.tracks)
        self.refresh_view()

    # ================= 文件夹 / 扫描 =================
    def choose_folder(self):
        start = self.input_dir or str(Path.home())
        path = QFileDialog.getExistingDirectory(self, "选择含 .mflac / .mgg 的文件夹", start)
        if path:
            self.set_input_dir(path)

    def set_input_dir(self, path, remember=True):
        self.input_dir = os.path.abspath(path)
        name = os.path.basename(self.input_dir.rstrip("\\/")) or self.input_dir
        fm = self.folder_btn.fontMetrics()
        self.folder_btn.setText(fm.elidedText(name, Qt.ElideMiddle, 280))
        self.folder_btn.setToolTip(self.input_dir)
        if remember:
            try:
                self.settings.setValue("ui/last_input", self.input_dir)
            except Exception:
                pass
        self.start_scan()

    def start_scan(self):
        if not self.input_dir or not os.path.isdir(self.input_dir):
            return
        self.count_label.setText("正在扫描…")
        QApplication.processEvents()

        cover_dir = self.cache_path("covers")
        session = getattr(self, "session", None)
        try:
            if session is not None and not session.is_alive():
                session = None
        except Exception:
            session = None
        if session is None:
            try:
                import session as qsession
                session = qsession.QQMusicSession(qsession.default_hook_path())
                if not session.attach():
                    session = None
            except Exception:
                session = None

        try:
            tracks = scanner.scan(self.input_dir, session=session, cover_dir=cover_dir,
                                 name_format=self.name_format)
        except Exception as e:
            self.count_label.setText("扫描失败：%s" % e)
            tracks = []
        finally:
            if session is not None and session.connected:
                # 留着这个会话：转码和播放解密都直接复用，省一次注入
                self._scan_session = session
                self.set_conn_state(True, "已连接 QQ 音乐")
                self.session = session

        self.tracks = tracks
        self.invalidate_lossless()      # 换了目录，缓存里的「有没有无损」全部作废
        self.mark_existing_outputs()
        for t in tracks:
            if not t.cover_path:
                cached = self.lookup_cover(t)
                if cached:
                    t.cover_path = cached
        self.checked = {t.path for t in tracks if t.status != "failed"}
        self.set_status_visible(False)
        self.refresh_view()
        QTimer.singleShot(200, self.load_covers_online)

    def lookup_cover(self, track):
        """看缓存里有没有这张封面（之前下过就不重复请求）。"""
        try:
            import lyrics as lyric_mod
            store = lyric_mod.LyricStore(lyric_mod.default_cache_dir())
            hit = self._lyric_cache.get(track.name)
            if hit and hit.get("cover"):
                return hit["cover"]
            return ""
        except Exception:
            return ""

    def mark_existing_outputs(self):
        """之前已经解出来的文件直接认出来：状态显示「已完成」，也能直接播放。"""
        if not self.input_dir:
            return
        for track in self.tracks:
            path = self.lossless_path(track)
            if path and os.path.isfile(path):
                track.output = path
                try:
                    track.output_size = os.path.getsize(path)
                except OSError:
                    track.output_size = 0
                if track.status in ("pending", "ready"):
                    track.status = "done"
                    if not track.message:
                        track.message = "已完成"

    def set_conn_state(self, ok, text=""):
        """顶栏连接胶囊。"""
        if hasattr(self, "conn_pill"):
            body = text or ("已连接 QQ 音乐" if ok else "未连接")
            self.conn_pill.setText(("● " if ok else "○ ") + body)
            self.conn_pill.setProperty("ok", "true" if ok else "false")
            style = self.conn_pill.style()
            style.unpolish(self.conn_pill)
            style.polish(self.conn_pill)

    def load_covers_online(self):
        """后台把封面补齐（QQ 音乐的文件里没有封面，只能联网找）。"""
        pending = [t for t in self.tracks if not t.cover_path]
        if not pending:
            return

        def work():
            changed = False
            for track in pending:
                try:
                    import lyrics as lyric_mod
                    info = lyric_mod.load_for(track.name, track.duration)
                    self._lyric_cache[track.name] = info
                    if info.get("cover"):
                        track.cover_path = info["cover"]
                    # 文件名里的「谁 - 谁」谁是歌手分不清，联网匹配上了就以匹配结果为准
                    cand = info.get("matched")
                    if cand is not None and getattr(cand, "title", ""):
                        changed = True
                        track.title = cand.title
                        track.artist = cand.artist or track.artist
                    if info.get("cover"):
                        self.bridge.event.emit("cover", track)
                except Exception:
                    continue
            if changed:
                self.bridge.event.emit("meta", None)

        threading.Thread(target=work, daemon=True).start()

    # ================= 过滤 / 排序 =================
    def has_lossless(self, track):
        """这首歌解出来的 FLAC / OGG 还在不在。"""
        path = track.output or self.lossless_path(track)
        return bool(path) and path.lower().endswith((".flac", ".ogg")) \
            and os.path.isfile(path)

    def lossless_path(self, track):
        """解密产物该在哪 —— 算不出来就返回空串。

        .mflac 解到 output/flac，.mgg 解到 output/ogg，互不混放。
        """
        if track.is_out or not track.plain_ext:
            return ""
        try:
            stem = Path(track.path).stem
            folder = os.path.join(self.input_dir or "", "output",
                                  "ogg" if track.plain_ext == ".ogg" else "flac")
            return os.path.join(folder, stem + track.plain_ext)
        except (OSError, ValueError):
            return ""

    def has_flac(self, track):
        """解出来的文件还在不在 —— 直接看磁盘，不信 status。

        解密产物现在一律保留，所以只要 output 目录里那份文件在，这一格就亮。
        """
        path = self.lossless_path(track)
        if not path:
            return False
        cache = getattr(self, "_lossless_cache", None)
        if cache is None:
            cache = self._lossless_cache = {}
        key = track.path
        if key in cache:
            return cache[key]
        ok = False
        try:
            ok = os.path.isfile(path)
        except (OSError, ValueError):
            ok = False
        cache[key] = ok
        return ok

    def format_files(self, track):
        """这首歌在磁盘上实际存在哪些文件：[(格式, 路径, 字节数)]。"""
        ext = (track.ext or track.path.rsplit(".", 1)[-1] or "").lower().lstrip(".")
        if track.is_out:
            size = 0
            try:
                if os.path.isfile(track.path):
                    size = os.path.getsize(track.path)
            except OSError:
                size = 0
            return [(ext, track.path, size or (track.size or 0))]
        files = []
        size = 0
        try:
            if os.path.isfile(track.path):
                size = os.path.getsize(track.path)
        except OSError:
            size = 0
        files.append((ext, track.path, size or (track.size or 0)))
        lp = self.lossless_path(track)
        if lp and self.has_flac(track):
            inner = (os.path.splitext(lp)[1] or "").lstrip(".").lower()
            try:
                inner_size = os.path.getsize(lp)
            except OSError:
                inner_size = 0
            files.append((inner or "flac", lp, inner_size))
        return files

    def context_file(self, track):
        """右键「在文件夹中显示」该跳到哪个文件。"""
        key = self.filter_kind
        if key in ("mflac", "mgg", "flac", "ogg"):
            for ext, path, _size in self.format_files(track):
                if ext == key:
                    return path
        return track.path

    def invalidate_lossless(self, track=None):
        """删除 / 转换完成后让「有没有无损」的缓存跟着失效。"""
        cache = getattr(self, "_lossless_cache", None)
        if cache is None:
            return
        if track is None:
            cache.clear()
        else:
            cache.pop(track.path, None)

    def base_rows(self):
        """选择模式一行一个文件；非选择模式一行一首歌。"""
        return self.all_rows() if self.select_mode else list(self.tracks)

    def is_converted(self, track):
        """这首歌是不是已经解密/转好了（还没动的 mflac、mgg 不算）。"""
        return bool(track.output) or track.status in ("done", "skipped")

    def refresh_view(self, rebuild_side=True):
        rows = self.base_rows()

        kind = self.filter_kind
        if kind == "enc":
            # 这一项回答「目录里有几首加密源文件」。解密不删源文件，所以解完也
            # 不该从这儿消失 —— 否则父项数字对不上 mflac + mgg 的和
            rows = [t for t in rows if not t.is_out]
        elif kind in ("mflac", "mgg"):
            # 容器分类回答「这堆文件是什么壳」，解没解密都算 —— 否则一解完
            # mgg 就从侧边栏消失，看着像文件丢了
            rows = [t for t in rows if not t.is_out
                    and t.path.lower().endswith("." + kind)]
        elif kind in ("flac", "ogg"):
            if self.select_mode:
                # 一行一个文件，这一栏只列真的躺在 output\flac / output\ogg 里的
                rows = [t for t in rows if t.is_out and t.out_kind == kind]
            else:
                # 只收已经解出来的 —— 还躺在 mflac / mgg 里的歌归「未解密」
                rows = [t for t in rows if not t.is_out and t.kind == kind
                        and self.is_converted(t)]
        if self.filter_status != "all":
            rows = [t for t in rows if self.status_bucket(t) == self.filter_status]
        if self.filter_artist:
            rows = [t for t in rows if (t.artist or "未知歌手") in self.filter_artist]
        if self.search_text:
            q = self.search_text.lower()
            rows = [t for t in rows if q in (t.title or "").lower()
                    or q in (t.artist or "").lower() or q in t.name.lower()]

        rows = self.sorted_rows(rows)
        self.rows = rows
        self.model.set_rows(rows)
        self.toolbar.set_active(self.sort_key)
        self.rebuild_chips()
        self.update_count()
        self.update_empty_hint()

    def lossless_row(self, track):
        """解密出来的 FLAC / OGG，在选择模式里也单独占一行。"""
        path = self.lossless_path(track)
        if not path:
            return None
        row = self._out_rows.get(path)
        if row is None:
            row = scanner.Track(path)
            row.is_out = True
            self._out_rows[path] = row
        row.title = track.title
        row.artist = track.artist
        row.name = track.name
        row.kind = track.kind if track.kind in ("flac", "ogg") else "flac"
        row.out_kind = row.kind
        row.ext = track.plain_ext
        row.plain_ext = track.plain_ext
        row.duration = track.duration
        row.sample_rate = track.sample_rate
        row.channels = track.channels
        row.bits = track.bits
        row.cover_path = track.cover_path
        row.status = "done"
        row.message = "已完成"
        try:
            row.size = os.path.getsize(path)
        except OSError:
            row.size = 0
        return row

    def all_rows(self):
        """一个真实文件一行：源文件、解密出的 FLAC / OGG 各占一行。"""
        rows = list(self.tracks)
        for track in self.tracks:
            if self.has_flac(track):
                lossless = self.lossless_row(track)
                if lossless is not None:
                    rows.append(lossless)
        return rows

    def status_bucket(self, track):
        if track.status in ("done", "skipped"):
            return "done"
        if track.status == "failed":
            return "failed"
        return "todo"

    def sorted_rows(self, rows):
        key = self.sort_key

        def sort_key(track):
            if key == "duration":
                return track.duration or 0
            if key == "size":
                return track.size or 0
            if key == "kind":
                return track.kind
            if key == "status":
                return STATUS_TEXT.get(track.status, "")
            return collate(track.title or track.name)

        return sorted(rows, key=sort_key, reverse=self.sort_desc)

    def set_sort(self, key):
        if key == self.sort_key:
            self.sort_desc = not self.sort_desc
        else:
            self.sort_key = key
            self.sort_desc = False
        self.refresh_view()

    def on_search(self, text):
        self.search_text = (text or "").strip()
        self.refresh_view()

    def tracks_for_key(self, key):
        """侧边栏某个分类对应哪些行（随选择模式决定是否拆成单文件）。"""
        rows = self.base_rows()
        if key in ("all", "", "group"):
            return rows
        if key in ("enc", "mflac", "mgg", "flac", "ogg"):
            key = "kind:" + key          # 胶囊行直接用裸 key，这里统一收口
        if key.startswith("kind:"):
            value = key.split(":", 1)[1]
            if value == "enc":
                return [t for t in rows if not t.is_out]
            if value in ("mflac", "mgg"):
                # 和 refresh_view 保持一致：容器分类不看解密状态
                return [t for t in rows if not t.is_out
                        and t.path.lower().endswith("." + value)]
            if value in ("flac", "ogg"):
                if self.select_mode:
                    return [t for t in rows if t.is_out and t.out_kind == value]
                return [t for t in rows if not t.is_out and t.kind == value
                        and self.is_converted(t)]
            return [t for t in rows if not t.is_out and t.kind == value]
        if key.startswith("status:"):
            value = key.split(":", 1)[1]
            return [t for t in rows if self.status_bucket(t) == value]
        if key.startswith("artist:"):
            value = key.split(":", 1)[1]
            return [t for t in rows if (t.artist or "未知歌手") == value]
        return []

    def _category_state(self, key):
        """分类的勾选态：0 全不选、1 全选、2 部分选。"""
        tracks = self.tracks_for_key(key)
        if not tracks:
            return 0
        picked = sum(1 for t in tracks if t.path in self.checked)
        if picked == 0:
            return 0
        if picked == len(tracks):
            return 1
        return 2

    def _filter_label(self):
        names = sorted(self.filter_artist)
        if not names:
            return "筛选"
        if len(names) == 1:
            return "筛选 · %s" % names[0]
        return "筛选 · %d 位歌手" % len(names)

    def rebuild_chips(self):
        """刷新胶囊计数、当前选中态，以及选择模式下的整类勾选态。"""
        for key, chip in getattr(self, "chips", {}).items():
            if getattr(chip, "kind", "") == "sep":
                continue
            if key == "filter":
                chip.setText(self._filter_label())
                chip.setChecked(bool(self.filter_artist))
                chip.set_check_state(0)
                continue
            chip.set_count(len(self.tracks_for_key(key)))
            chip.setChecked(self.filter_status == "all" and self.filter_kind == key)
            chip.set_check_state(self._category_state(key) if self.select_mode else 0)

    def on_chip_clicked(self, chip):
        """点胶囊换筛选条件：单选，一次只留一个。"""
        key = chip.key
        if key == "filter":
            self.open_filter_popup()
            return
        if self.filter_status != "all" or self.filter_artist:
            self.filter_status = "all"
            self.filter_artist = set()
        if key == "all":
            self.filter_kind = "all"
        else:
            self.filter_kind = key
        self.refresh_view(rebuild_side=False)

    def on_chip_check(self, key):
        """选择模式下点胶囊左侧的勾选框：整类勾上或整类取消。"""
        tracks = self.tracks_for_key(key)
        if not tracks:
            return
        if self._category_state(key) == 1:
            self.checked.difference_update(t.path for t in tracks)
        else:
            self.checked.update(t.path for t in tracks)
        for track in tracks:
            self.model.ping(track)
        self.rebuild_chips()
        self.update_select_bar()
        self.update_count()

    def open_filter_popup(self):
        counts = {}
        for t in self.all_rows():
            name = t.artist or "未知歌手"
            counts[name] = counts.get(name, 0) + 1
        names = sorted(counts, key=lambda x: (-counts[x], collate(x)))
        pop = getattr(self, "filter_popup", None)
        if pop is None:
            pop = FilterPopup(self)
            pop.applied.connect(self.apply_artist_filter)
            self.filter_popup = pop
        pop.load(names, counts, self.filter_artist)
        pop.popup_at(self.filter_chip)

    def apply_artist_filter(self, names):
        names = {n for n in (names or []) if n}
        if names == self.filter_artist:
            return
        self.filter_artist = names
        self.filter_kind = "all"
        self.filter_status = "all"
        self.refresh_view(rebuild_side=False)

    def toggle_select_all(self):
        visible = list(self.rows)
        if not visible:
            return
        all_checked = all(t.path in self.checked for t in visible)
        if all_checked:
            self.checked.difference_update(t.path for t in visible)
        else:
            self.checked.update(t.path for t in visible)
        self.all_btn.setText("取消全选" if not all_checked else "全选")
        self.update_select_bar()
        self.rebuild_chips()
        self.model.set_rows(self.rows)
        self.update_count()

    def _sync_side_checks(self):
        """列表里勾选后，胶囊上的整类勾选态要跟着变。"""
        if not self.select_mode:
            return
        self.rebuild_chips()

    def update_select_bar(self):
        """刷新「已选中 N 项」，并决定这条栏要不要露出来。"""
        n = sum(1 for t in self.all_rows() if t.path in self.checked)
        if hasattr(self, "sel_count"):
            self.sel_count.setText("已选中 %d 项" % n)
        if hasattr(self, "select_bar"):
            self.select_bar.setVisible(bool(self.select_mode))
        if hasattr(self, "del_btn"):
            self.del_btn.setEnabled(n > 0)

    def track_menu(self, track, global_pos):
        """行尾「⋯」和右键共用这一份菜单。"""
        menu = QMenu(self)
        menu.setAttribute(Qt.WA_StyledBackground, True)
        act_play = menu.addAction("播放")
        act_reveal = menu.addAction("在文件夹中显示")
        menu.addSeparator()
        act_remove = menu.addAction("从列表中移除")
        act_delete = menu.addAction("删除文件…")
        chosen = menu.exec(global_pos)
        if chosen is act_play:
            self.play_track(track)
        elif chosen is act_reveal:
            self.reveal_in_explorer(self.context_file(track))
        elif chosen is act_remove:
            self.remove_track_row(track)
        elif chosen is act_delete:
            self.delete_track_file(track)

    def reveal_in_explorer(self, path):
        path = path or ""
        if not path or not os.path.exists(path):
            self.notify("文件已经不在了")
            return
        try:
            if os.name == "nt":
                subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
            else:
                subprocess.Popen(["xdg-open", os.path.dirname(path)])
        except OSError:
            self.notify("打不开文件夹")

    def remove_track_row(self, track):
        """只从列表里拿掉，不动磁盘上的文件。"""
        if getattr(track, "is_out", False):
            self._out_rows.pop(track.path, None)
            for src in self.tracks:
                if os.path.normcase(self.lossless_path(src) or "") == \
                        os.path.normcase(track.path):
                    src.output = ""
                    src.output_size = 0
                    src.message = ""
                    self.invalidate_lossless(src)
                    src.status = "ready"
        else:
            self.tracks = [t for t in self.tracks if t is not track]
            self._playlist_tracks = [t for t in self._playlist_tracks if t is not track]
        self.checked.discard(track.path)
        self.refresh_view()
        self.update_select_bar()

    def delete_track_file(self, track):
        """单行删除：复用多选删除的确认与善后逻辑。"""
        saved = set(self.checked)
        self.checked = {track.path}
        try:
            self.delete_selected()
        finally:
            self.checked = saved

    def delete_selected(self):
        """删除勾选的项：一项对应一个文件，删的就是这个文件。"""
        targets = [t for t in self.all_rows() if t.path in self.checked]
        if not targets:
            self.info("没有选中", "先在列表里勾选要删除的歌。")
            return

        box = self.polish_dialog(QMessageBox(self))
        box.setWindowTitle("删除文件")
        box.setIcon(QMessageBox.Warning)
        box.setText("请确定删除选中的 %d 项，此操作会删除对应的文件。" % len(targets))
        box.setInformativeText("删除后无法恢复。")
        yes = box.addButton("删除", QMessageBox.DestructiveRole)
        cancel = box.addButton("取消", QMessageBox.RejectRole)
        box.setDefaultButton(cancel)
        box.exec()
        if box.clickedButton() is not yes:
            return

        removed = set()
        gone = 0
        for track in targets:
            stem = Path(track.path).stem
            candidates = [track.path]
            for path in candidates:
                try:
                    if path and os.path.isfile(path):
                        os.remove(path)
                        gone += 1
                except OSError:
                    logging.warning("删除失败：%s", path, exc_info=True)
            if track.is_out:
                # 删的是解密产物：按磁盘现状重新判定每首歌的状态
                for src in self.tracks:
                    lp = self.lossless_path(src)
                    if lp and os.path.isfile(lp):
                        src.output = lp
                        try:
                            src.output_size = os.path.getsize(lp)
                        except OSError:
                            src.output_size = 0
                    else:
                        src.output = ""
                        src.output_size = 0
                        src.message = ""
                        self.invalidate_lossless(src)
                        if src.status in ("done", "skipped"):
                            src.status = "ready"
            else:
                removed.add(track.path)
                for src in self.tracks:
                    if src is track:
                        src.output = ""
                        src.output_size = 0
            logging.info("已删除：%s", stem)

        self.tracks = [t for t in self.tracks if t.path not in removed]
        self.checked.difference_update(removed)
        for track in targets:
            self.checked.discard(track.path)
        self._playlist_tracks = [t for t in self._playlist_tracks if t.path not in removed]
        self._out_rows = {k: v for k, v in self._out_rows.items()
                          if os.path.isfile(k)}
        self.refresh_view()
        self.update_select_bar()
        logging.info("删除完成：%d 首，共移除 %d 个文件", len(targets), gone)

    def set_select_mode(self, on):
        was_on = self.select_mode
        self.select_mode = bool(on)
        if self.select_mode and not was_on:
            self.checked.clear()           # 每次进来都从零开始勾，不继承上一轮的残留
        if hasattr(self, "select_btn") and self.select_btn.isChecked() != self.select_mode:
            self.select_btn.blockSignals(True)
            self.select_btn.setChecked(self.select_mode)
            self.select_btn.blockSignals(False)
        if hasattr(self, "all_btn"):
            self.all_btn.setVisible(self.select_mode)
            self.all_btn.setText("全选")
        if not self.select_mode:
            self._tree_syncing = True
        self.toolbar.set_select_mode(self.select_mode)
        for chip in getattr(self, "chips", {}).values():
            chip.set_select_mode(self.select_mode)
        self.refresh_view()
        self.update_select_bar()

    def set_status_visible(self, on):
        self.show_status = bool(on)
        if hasattr(self, "header"):
            self.toolbar.set_status_visible(self.show_status)
        self.view.viewport().update()


    def check_connection(self):
        """定时确认连接：既发现 QQ 音乐被杀，也发现它刚被打开。"""
        sess = getattr(self, "session", None)
        if sess is None:
            self._try_attach()
            return
        try:
            alive = sess.is_alive()
        except Exception:
            alive = False
        if alive and sess.connected:
            self.set_conn_state(True, "已连接 QQ 音乐")
            return
        self.set_conn_state(False, "未连接")
        # 会话不可用（进程没了，或通道断了）：直接丢掉，下一轮自己重连
        try:
            sess.detach()
        except Exception:
            pass
        if getattr(self, "_scan_session", None) is sess:
            self._scan_session = None
        self.session = None

    def _try_attach(self):
        """没有会话时，发现 QQ 音乐就自己连上去（先开本程序、后开 QQ 音乐也能连上）。"""
        if getattr(self, "_attach_thread", None) is not None:
            return
        if getattr(self, "_runner_thread", None) is not None:
            return  # 正在转换，连接由 Runner 自己管，别抢
        if time.time() < getattr(self, "_attach_next_try", 0):
            return  # 刚失败过，退避几秒再试
        try:
            import session as qsession
            running = qsession.qqmusic_running()
        except Exception:
            running = False
        if not running:
            self.set_conn_state(False, "未连接")
            return
        self.set_conn_state(False, "正在连接…")
        thread = AttachThread(self)
        thread.result.connect(self.on_attach_result)
        thread.finished.connect(self._on_attach_finished)
        self._attach_thread = thread
        thread.start()

    def _on_attach_finished(self):
        if self.sender() is self._attach_thread:
            self._attach_thread = None

    def on_attach_result(self, session, message):
        """AttachThread 的结果回到主线程。"""
        if session is None:
            self._attach_next_try = time.time() + 8
            logging.info("自动连接 QQ音乐失败：%s", message or "未知原因")
            self.set_conn_state(False, "未连接")
            return
        self._attach_next_try = 0
        self.session = session
        self._scan_session = session
        self.set_conn_state(True, "已连接 QQ 音乐")
        logging.info("已自动连接到 QQ音乐")
        # 扫描时还没连上时，时长/格式是空的 —— 连上后补一次
        if self.input_dir and self.tracks and self._runner_thread is None:
            if any(t.status in ("pending", "ready") for t in self.tracks):
                QTimer.singleShot(0, self.start_scan)

    def open_github(self):
        QDesktopServices.openUrl(QUrl(GITHUB_URL))

    def _local_help_path(self):
        """找程序自带的使用说明.html：打包后在 _internal 或 exe 同级，源码运行在项目目录。"""
        roots = []
        meipass = getattr(sys, "_MEIPASS", "")
        if meipass:
            roots.append(meipass)
        if getattr(sys, "frozen", False):
            exe_dir = os.path.dirname(os.path.abspath(sys.executable))
            roots.extend([os.path.join(exe_dir, "_internal"), exe_dir])
        roots.extend([os.path.dirname(os.path.abspath(__file__)), os.getcwd()])
        seen = set()
        for root in roots:
            if not root or root in seen:
                continue
            seen.add(root)
            path = os.path.join(root, "使用说明.html")
            if os.path.isfile(path):
                return path
        return ""

    def _has_network(self):
        """只判断有没有网：能建立 TCP 连接就算有（断网时两个都立刻失败）。"""
        for host in ("www.baidu.com", "zksbx.github.io"):
            try:
                socket.create_connection((host, 443), timeout=1.2).close()
                return True
            except OSError:
                continue
        return False

    def _open_local_help(self, anchor=""):
        path = self._local_help_path()
        if not path:
            self.notify("没找到本地的使用说明")
            return False
        url = QUrl.fromLocalFile(path)
        if anchor:
            url.setFragment(anchor)
        return QDesktopServices.openUrl(url)

    def open_support(self):
        """支持作者：有网打开官网赞赏区块，断网回落到自带说明的赞赏章节。"""
        if self._has_network():
            QDesktopServices.openUrl(QUrl(SITE_URL.rstrip("/") + "/#support"))
        else:
            self._open_local_help("support")

    def open_help(self):
        """使用帮助：有网打开官网帮助章节，断网打开程序自带的使用说明.html。"""
        if self._has_network():
            QDesktopServices.openUrl(QUrl(HELP_URL))
        else:
            self._open_local_help()

    def schedule_side_refresh(self):
        """转换进度推着分类计数走，先攒着；事件密的时候不重建整棵树。"""
        timer = getattr(self, "_side_refresh_timer", None)
        if timer is not None:
            timer.start()

    def flush_side_refresh(self):
        """只重建侧栏和计数，不动列表 —— 转换中重排列表会让人点到别的歌。"""
        timer = getattr(self, "_side_refresh_timer", None)
        if timer is not None:
            timer.stop()
        self.rebuild_chips()
        self.update_count()



    def update_count(self):
        shown = len(self.rows)
        self.count_label.setText("共 %d 首" % shown if self.tracks else "")

    def show_progress(self, text):
        """进度显示在开始按钮上，右侧计数只留给「共 N 首」。"""
        if getattr(self, "start_btn", None) is None:
            return
        base = "停止" if (self._runner_thread is not None) else "开始解密"
        self.start_btn.setText("%s · %s" % (base, text) if text else base)

    def update_empty_hint(self):
        if not self.tracks:
            self.empty_hint.setText("选一个文件夹，这里会列出所有 .mflac / .mgg")
            self.empty_hint.show()
        elif not self.rows:
            self.empty_hint.setText("没有符合筛选条件的歌曲")
            self.empty_hint.show()
        else:
            self.empty_hint.hide()
        self.empty_hint.setGeometry(self.view.viewport().rect().adjusted(0, 120, 0, 0))

    # ================= 勾选 =================
    def toggle_checked(self, track):
        if track.path in self.checked:
            self.checked.discard(track.path)
        else:
            self.checked.add(track.path)
        self.model.ping(track)
        self.update_count()
        self.update_select_bar()
        self._sync_side_checks()

    def show_track_info(self, track, path=None):
        self.now_title.setText(track.title or track.name)
        if hasattr(self, "fmt_btn"):
            files = self.playable_files(track)
            if files:
                label = files[0][0]
                target = os.path.normcase(path or self.player.current_path() or "")
                if target:
                    for item_label, item_path in files:
                        if os.path.normcase(item_path) == target:
                            label = item_label
                            break
                self.fmt_btn.setText(label + " ▾")
                self.fmt_btn.setEnabled(len(files) > 1)
                self.fmt_btn.setToolTip("切换这首歌的播放格式" if len(files) > 1
                                        else "这首歌只有一种可播放格式")
            else:
                self.fmt_btn.setText("未解密")
                self.fmt_btn.setEnabled(False)
                self.fmt_btn.setToolTip("这首歌还没解密，解密后才能播放")
        self.now_artist.setText(track.artist or "未知歌手")
        if track.cover_path and os.path.exists(track.cover_path):
            pix = cover_pixmap(track.cover_path, 46, self.devicePixelRatioF())
            if pix is not None:
                self.now_cover.setPixmap(pix)
        self._sync_mini_track(track)

    def eventFilter(self, obj, event):
        # 播放条上的小封面也能点开沉浸页
        if getattr(self, "now_cover", None) is obj \
                and event.type() == QEvent.MouseButtonRelease \
                and event.button() == Qt.LeftButton:
            self.lyric_btn.setChecked(True)
            return True
        if obj is self.list_wrap and event.type() == QEvent.Resize:
            if self.lyric_panel.isVisible():
                self.lyric_panel.setGeometry(self.lyric_rect())
        # 空格播放 / 暂停：装在 QApplication 上，焦点在列表里也能用；
        # 正在输入文字时不抢空格
        if event.type() == QEvent.KeyPress and event.key() == Qt.Key_Space \
                and event.modifiers() == Qt.NoModifier and not event.isAutoRepeat():
            if QApplication.activeModalWidget() is None \
                    and QApplication.activeWindow() is self \
                    and not self._text_editing():
                self.player_toggle()
                return True
        return super().eventFilter(obj, event)

    # ================= 开始解密 =================
    def on_start_clicked(self):
        if self._runner_thread is not None and self._runner_thread.isRunning():
            self._runner.cancel()
            self.start_btn.setText("正在停止…")
            self.start_btn.setEnabled(False)
            self.toast.say("正在停止…")
            return
        if not self.input_dir or not os.path.isdir(self.input_dir):
            self.choose_folder()
            if not self.input_dir:
                return
        pool = [t for t in self.rows if not t.is_out]
        targets = [t for t in pool if t.path in self.checked]
        if not targets:
            targets = pool
        if not targets:
            self.info("没有可解密的歌", "先选一个含有 .mflac / .mgg 的文件夹。")
            return
        if self.select_mode:               # 开始解密就退回普通视图，别停在挑选状态
            self.set_select_mode(False)

        root = os.path.join(self.input_dir, "output")
        runner = main.Runner(
            self.input_dir,
            flac_dir=os.path.join(root, "flac"),
            ogg_dir=os.path.join(root, "ogg"),
            force=self.cb_force.isChecked(),
            embed_covers=self.cb_cover.isChecked(),
            session=getattr(self, "_scan_session", None),
            on_event=self.emit_event,
        )
        self._runner = runner
        self._runner_thread = RunnerThread(runner, targets, self)
        self._runner_thread.done.connect(self.on_run_finished)
        self._runner_thread.start()

        self.start_btn.setText("停止")
        self.start_btn.setEnabled(True)
        self.opt_btn.setEnabled(False)
        self.set_status_visible(True)

        self._run_total = len(targets)
        self._run_done = 0
        self.toast.begin(self._run_total)

    def emit_event(self, kind, payload):
        self.bridge.event.emit(kind, payload)

    def on_runner_event(self, kind, payload):
        if kind == "log":
            text = payload.get("message") if isinstance(payload, dict) else str(payload)
            if text:
                logging.info(str(text))
            return
        if kind == "conn":
            ok = bool(payload.get("ok"))
            self.conn_pill.setText("已连接 QQ 音乐" if ok else "未连接")
            self.conn_pill.setProperty("ok", "true" if ok else "false")
            self.conn_pill.style().unpolish(self.conn_pill)
            self.conn_pill.style().polish(self.conn_pill)
        elif kind == "items":
            self.tracks = payload
            self.invalidate_lossless()
            self.mark_existing_outputs()
            self.refresh_view()
        elif kind == "item":
            track = payload.get("track")
            state = payload.get("state")
            if track is not None:
                if state == "done":
                    track.status = "done"
                elif state == "fail":
                    track.status = "failed"
                    logging.warning("解密失败：%s —— %s", track.name, payload.get("message") or "")
                elif state == "skip":
                    track.status = "skipped"
                else:
                    track.status = "working"
                if state == "working":
                    self.toast.set_current(track.name or Path(track.path).name)
                elif state in ("done", "fail", "skip"):
                    self._run_done += 1
                    self.toast.set_progress("解密", self._run_done, self._run_total)
                if payload.get("message"):
                    track.message = payload["message"]
                if state in ("done", "skip"):
                    out = payload.get("output") or ""
                    if out and os.path.isfile(out):
                        track.output = out
                        try:
                            track.output_size = os.path.getsize(out)
                        except OSError:
                            track.output_size = 0
                self.model.ping(track)
                self.update_count()
                self.invalidate_lossless(track)   # 刚解出来的无损可能刚落到磁盘上
                if state in ("done", "fail", "skip"):
                    self.schedule_side_refresh()
        elif kind == "progress":
            self.update_progress(payload)
        elif kind == "log":
            msg = payload.get("message", "")
            if msg:
                self.show_progress(msg)
        elif kind == "summary":
            self.on_summary(payload)
        elif kind == "cover":
            self.model.ping(payload)
        elif kind == "meta":
            self.refresh_view()

    def update_progress(self, payload):
        phase = payload.get("phase", "")
        done = payload.get("done", 0)
        total = payload.get("total", 0)
        if total:
            self.show_progress("%s %d/%d" % (phase, done, total))
            self.toast.set_progress(phase, done, total)

    def on_summary(self, summary):
        self.set_status_visible(False)
        ok = summary.get("ok", 0)
        fail = summary.get("fail", 0)
        skip = summary.get("skip", 0)
        elapsed = summary.get("elapsed", 0)
        self.show_progress("完成：成功 %d，失败 %d，跳过 %d，用时 %.1f 秒" % (ok, fail, skip, elapsed))
        self.toast.finish(ok, fail, skip, elapsed)
        self.flush_side_refresh()      # 结算时把侧栏计数一次刷到位
        self.show_completion_dialog(ok, fail, skip, elapsed)
        self._notify_run_done(ok, fail, skip, elapsed)

    def on_run_finished(self):
        self.start_btn.setText("开始解密")
        self.start_btn.setEnabled(True)
        self.opt_btn.setEnabled(True)
        self._runner_thread = None
        self.flush_side_refresh()      # 中途停止（没 summary）也要把计数补上
        if self.toast.ok_count < 0:        # 中途停止、没等到 summary：别把浮窗留在屏幕上
            self.toast.hide()

    def show_completion_dialog(self, ok, fail, skip, elapsed):
        if ok == 0 and fail == 0:
            return
        dlg = self.polish_dialog(QDialog(self))
        dlg.setWindowTitle("处理完成")
        dlg.setMinimumWidth(420)
        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(22, 20, 22, 18)
        lay.setSpacing(10)

        title = QLabel("全部搞定" if fail == 0 else "处理完成，有 %d 首没成功" % fail)
        font = title.font()
        font.setPointSizeF(12)
        font.setWeight(QFont.DemiBold)
        title.setFont(font)
        lay.addWidget(title)

        info = QLabel("成功 %d · 失败 %d · 跳过 %d · 用时 %.1f 秒" % (ok, fail, skip, elapsed))
        info.setObjectName("metaText")
        lay.addWidget(info)

        if fail:
            reasons = []
            for track in self.tracks:
                if track.status != "failed":
                    continue
                why = explain_exit_code(getattr(track, "message", "") or "")
                if why and why not in reasons:
                    reasons.append(why)
            if reasons:
                detail = QLabel("具体原因：\n" + "\n".join("· " + r for r in reasons[:3]))
                detail.setWordWrap(True)
                detail.setObjectName("drawerHint")
                lay.addWidget(detail)
            tips = QLabel("常见原因，按顺序排查：\n"
                          "1. 音质不支持 —— 只支持标准 / HQ / SQ，臻品音质、臻品母带、杜比全景声解不开\n"
                          "2. 没注入进去 —— 完全退出 QQ 音乐，重新打开并登录后再试\n"
                          "3. 被杀软拦了 —— 把程序所在文件夹加进信任区，压缩包先解压再用")
            tips.setWordWrap(True)
            tips.setObjectName("drawerHint")
            lay.addWidget(tips)

        row = QHBoxLayout()
        row.addStretch(1)
        if fail:
            log_btn = QPushButton("打开日志文件")
            log_btn.setObjectName("secondaryBtn")
            log_btn.clicked.connect(lambda: self.open_path(log_path()))
            row.addWidget(log_btn)
        open_btn = QPushButton("打开输出目录")
        open_btn.setObjectName("secondaryBtn")
        open_btn.clicked.connect(lambda: self.open_path(os.path.join(self.input_dir, "output")))
        row.addWidget(open_btn)
        close_btn = QPushButton("关闭")
        close_btn.setObjectName("primaryBtn")
        close_btn.clicked.connect(dlg.accept)
        row.addWidget(close_btn)
        lay.addLayout(row)
        dlg.exec()

    def open_path(self, path):
        if path and os.path.exists(path):
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    # ================= 播放 =================
    def play_track(self, track):
        """双击一首歌：能播就播它，不能播就只提示，绝不顺手播别的歌。"""
        files = self.playable_files(track)
        if not files:
            self.notify("这个使不得 请先解密再试")
            return
        # 列表停在某个具体格式上时，优先播这个格式（双击 FLAC 行就放 FLAC）
        prefer = self.filter_kind.upper() if self.filter_kind in ("flac", "ogg") else ""
        pick = files[0][1]
        if prefer:
            for label, path in files:
                if label == prefer:
                    pick = path
                    break
        self._current_track = track
        self.show_track_info(track, pick)
        pairs = []
        for item in self.rows:
            item_files = self.playable_files(item)
            if not item_files:
                continue
            choice = item_files[0][1]
            if item is track and prefer:
                for label, path in item_files:
                    if label == prefer:
                        choice = path
                        break
            pairs.append((item, choice))
        if not pairs:
            pairs = [(track, pick)]
        self._playlist_tracks = [p[0] for p in pairs]
        paths = [p[1] for p in pairs]
        self.player.set_playlist(paths)
        try:
            idx = paths.index(pick)
        except ValueError:
            idx = 0
        self.player.play_index(idx)
        self.request_lyrics(track, paths[idx])

    def on_player_track(self, index):
        """上一首 / 下一首 / 自动连播都要把封面、歌名、歌词一起换掉。"""
        tracks = getattr(self, "_playlist_tracks", [])
        track = tracks[index] if 0 <= index < len(tracks) else None
        if track is None:
            return
        self._current_track = track
        self.show_track_info(track, self.player.current_path())
        self.request_lyrics(track, self.player.current_path())
        row = self.model.row_of(track)
        if row >= 0:
            self.view.setCurrentIndex(self.model.index(row, 0))

    def playable_files(self, track):
        """这首歌真正能播的文件：解密出来的 FLAC / OGG。加密源文件不算。"""
        files = []
        if track is None:
            return files
        if track.is_out:
            # 选择模式下每个输出文件自己就是一行，它的 path 就是那个文件
            lower = track.path.lower()
            if lower.endswith(".flac") and os.path.isfile(track.path):
                files.append(("FLAC", track.path))
            elif lower.endswith(".ogg") and os.path.isfile(track.path):
                files.append(("OGG", track.path))
            return files
        lp = self.lossless_path(track)
        if lp and os.path.isfile(lp):
            ext = (os.path.splitext(lp)[1] or "").lstrip(".").lower()
            files.append((ext.upper() or "FLAC", lp))
        return files

    def show_format_menu(self, global_pos=None):
        """只列出能播的格式，选一个换成播放源。"""
        track = self._current_track
        if track is None:
            self.info("没有在播放", "先在列表里双击一首歌。")
            return

        files = self.playable_files(track)
        if not files:
            self.info("没有可播放的文件", "这首歌还没有解密出来。")
            return
        if len(files) == 1:
            self.info("只有一种格式", "这首歌目前只有 %s 可以播放。" % files[0][0])
            return

        current = os.path.normcase(self.player.current_path() or "")
        menu = QMenu(self)
        for label, path in files:
            try:
                size = format_size(os.path.getsize(path))
            except OSError:
                size = ""
            act = menu.addAction(label + ("\u3000" + size if size else ""))
            if path and os.path.normcase(path) == current:
                act.setCheckable(True)
                act.setChecked(True)
            act.triggered.connect(lambda _=False, p=path: self.switch_play_format(p))
        if global_pos is None:
            global_pos = self.fmt_btn.mapToGlobal(self.fmt_btn.rect().bottomLeft())
        menu.exec(global_pos)

    def switch_play_format(self, path):
        """把当前曲目的播放源换成另一个真实文件。"""
        if not path:
            return
        if self.player.play_file(path):
            logging.info("切换播放源：%s", path)
            if self._current_track is not None:
                self.show_track_info(self._current_track, path)
            self.request_lyrics(self._current_track, path)

    def player_toggle(self):
        if self.player.is_playing():
            self.player.pause()
        else:
            if not self.player.playlist():
                if self.rows:
                    self.play_track(self.rows[0])
                return
            self.player.play()

    def player_prev(self):
        self.player.prev()

    def player_next(self):
        self.player.next()

    def on_player_state(self, state):
        if hasattr(self, "play_btn"):
            self.play_btn.set_kind("pause" if state == "playing" else "play")
        if self._mini is not None and self._mini.isVisible():
            self._sync_mini_playback()

    def on_position(self, ms):
        if not self.seek.isSliderDown():
            duration = max(1, self.player.duration())
            self.seek.setValue(int(ms * 1000 / duration))
        self.time_cur.setText(format_time(ms / 1000.0))
        self.time_total.setText(format_time(self.player.duration() / 1000.0))
        self.lyric_panel.set_position(ms)
        self.lyric_strip.set_position(ms)
        if self._mini is not None and self._mini.isVisible():
            self._mini.set_playback(self.player.is_playing(), ms, self.player.duration())
            main, trans = self.lyric_panel.current_line()
            self._mini.set_lyrics(main or trans)

    def on_duration(self, _duration):
        self.on_position(self.player.position())

    def on_seek(self, value):
        duration = self.player.duration()
        if duration:
            self.player.seek(int(duration * value / 1000))

    def on_volume(self, value):
        if self.volume.value() != value:
            self.volume.blockSignals(True)
            self.volume.setValue(value)
            self.volume.blockSignals(False)
            self.update_volume_icon(value)

    def on_player_failed(self, message):
        self.now_artist.setText("播放失败：%s" % message)

    def on_player_finished(self):
        self.player_next()

    # ================= 歌词 =================
    def lyric_rect(self):
        central = self.centralWidget()
        return QRect(0, 0, central.width(), central.height())

    # ---------- 沉浸页：窗口标题栏跟着封面走 ----------
    @staticmethod
    def _caption_rgb(pix):
        if pix is None or pix.isNull():
            return None
        img = pix.scaled(1, 1, Qt.IgnoreAspectRatio, Qt.SmoothTransformation).toImage()
        col = QColor(img.pixel(0, 0))
        # 压到三成再掺一点蓝，白字才立得住，也不会脏成纯黑
        return (min(255, int(col.red() * 0.32) + 10),
                min(255, int(col.green() * 0.32) + 10),
                min(255, int(col.blue() * 0.32) + 12))

    def _apply_caption(self):
        if os.name != "nt":
            return
        rgb = self._caption_rgb(getattr(getattr(self, "lyric_panel", None), "cover", None))
        if rgb is None:
            return
        try:
            import ctypes
            hwnd = int(self.winId())
            dwm = ctypes.windll.dwmapi
            color = ctypes.c_uint32(rgb[0] | (rgb[1] << 8) | (rgb[2] << 16))
            dwm.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(color), ctypes.sizeof(color))
            dark = ctypes.c_int(1)          # 标题文字转白
            dwm.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(dark), ctypes.sizeof(dark))
        except Exception:
            pass

    @staticmethod
    def _system_dark_title():
        """系统本身是不是深色标题栏（决定退出沉浸页时还原成哪一档）"""
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                 r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
            value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
            return int(value) == 0
        except Exception:
            return False

    def _reset_caption(self):
        if os.name != "nt":
            return
        try:
            import ctypes
            hwnd = int(self.winId())
            dwm = ctypes.windll.dwmapi
            default = ctypes.c_uint32(0xFFFFFFFF)   # 交回系统默认配色
            dwm.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(default), ctypes.sizeof(default))
            dark = ctypes.c_int(1 if self._system_dark_title() else 0)
            dwm.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(dark), ctypes.sizeof(dark))
        except Exception:
            pass

    def toggle_lyrics(self, checked):
        if checked:
            self.lyric_panel.setGeometry(self.lyric_rect())
            self.lyric_panel.show()
            self.lyric_panel.raise_()
            QTimer.singleShot(0, lambda: self.lyric_panel.setGeometry(self.lyric_rect()))
            if self._current_track is not None:
                self.request_lyrics(self._current_track, self.player.current_path())
            self.lyric_strip.sync_visibility()
            QTimer.singleShot(0, self._apply_caption)
        else:
            self.lyric_panel.hide()
            self.lyric_strip.sync_visibility()
            self._reset_caption()

    def request_lyrics(self, track, audio_path=""):
        cached = self._lyric_cache.get(track.name)
        if cached:
            self.apply_lyrics(track, cached)
            return
        self.lyric_panel.set_track(track, track.cover_path)
        self.lyric_panel.update()
        self.lyric_strip.clear()

        def work():
            try:
                import lyrics as lyric_mod
                info = lyric_mod.load_for(track.name, track.duration)
            except Exception:
                info = {}
            self._lyric_cache[track.name] = info
            self.bridge.event.emit("lyric", (track, info))

        threading.Thread(target=work, daemon=True).start()

    def apply_lyrics(self, track, info):
        lines = []
        raw = info.get("lrc") or ""
        if raw:
            try:
                import lyrics as lyric_mod
                lines = lyric_mod.parse_lrc(raw)
            except Exception:
                lines = []
        cover = info.get("cover") or track.cover_path
        self.lyric_panel.set_track(track, cover)
        self.lyric_panel.set_lyrics(lines, info.get("trans"))
        self.lyric_strip.set_lyrics(lines, info.get("trans"))
        if cover:
            pix = cover_pixmap(cover, 46, self.devicePixelRatioF())
            if pix is not None:
                self.now_cover.setPixmap(pix)

    # ================= 更新检查 =================
    def start_update_check(self, manual=False):
        self._manual_update = manual
        self.update_checker.check()

    def _settings_check_done(self, text=""):
        """设置页点过「检查更新」的话，把结果回给它（按钮复原 + 一行说明）。"""
        hook = getattr(self, "_settings_check_hook", None)
        self._settings_check_hook = None
        if hook is not None:
            try:
                hook(text)
            except Exception as exc:  # 回调出问题不能影响更新判断
                logging.warning("设置页更新回调失败: %s", exc)

    def on_update_found(self, version, url, notes):
        if parse_version(version) <= parse_version(VERSION):
            self._settings_check_done("已经是最新的 v%s" % VERSION)
            return
        self._settings_check_done("发现新版本 v%s，按弹窗提示就能更新" % version)
        self._update_version = version
        self._update_url = url or DOWNLOAD_URL
        self._update_notes = notes or ""

        dlg = QDialog(self)
        dlg.setWindowTitle("发现新版本")
        dlg.setObjectName("updateDlg")
        dlg.setMinimumWidth(470)
        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(22, 20, 22, 18)
        lay.setSpacing(10)

        title = QLabel("发现新版本 v%s" % version)
        font = title.font()
        font.setPointSizeF(12.5)
        font.setWeight(QFont.DemiBold)
        title.setFont(font)
        lay.addWidget(title)

        sub = QLabel("当前 %s · 更新后配色、字号、解密选项都会保留" % VERSION)
        sub.setObjectName("metaText")
        lay.addWidget(sub)

        summary = self._update_summary(notes)
        if summary:
            body = QLabel()
            body.setObjectName("updateNotes")
            body.setWordWrap(True)
            body.setMinimumWidth(420)
            body.setTextFormat(Qt.RichText)
            lines = [html.escape(x) for x in summary.splitlines()]
            body.setText("<div style='line-height:140%%;'>%s</div>" % "<br>".join(lines))
            lay.addWidget(body)

        self._update_bar = QProgressBar()
        self._update_bar.setObjectName("updateBar")
        self._update_bar.setRange(0, 100)
        self._update_bar.setValue(0)
        self._update_bar.setTextVisible(False)
        self._update_bar.setFixedHeight(6)
        self._update_bar.setVisible(False)
        lay.addWidget(self._update_bar)

        self._update_status = QLabel("")
        self._update_status.setObjectName("metaText")
        self._update_status.setVisible(False)
        lay.addWidget(self._update_status)

        row = QHBoxLayout()
        row.addStretch(1)
        later = QPushButton("以后再说")
        later.setObjectName("secondaryBtn")
        later.clicked.connect(dlg.reject)
        row.addWidget(later)
        go = QPushButton("立即更新")
        go.setObjectName("primaryBtn")
        go.clicked.connect(self.start_self_update)
        row.addWidget(go)
        lay.addLayout(row)

        self._update_dlg = dlg
        self._update_later = later
        self._update_go = go
        if not can_self_update():
            # 源码跑的时候没法自己覆盖自己，把按钮退化成打开下载页
            go.setText("去下载")
            go.clicked.disconnect()
            go.clicked.connect(self._open_update_page)
        self.polish_dialog(dlg)
        dlg.exec()

    @staticmethod
    def _update_summary(notes, limit=6):
        """更新说明只留前几行正文，markdown 符号去掉。"""
        lines = []
        for line in (notes or "").splitlines():
            text = line.strip().lstrip("#").strip()
            text = text.lstrip("-*·").strip()
            if text:
                lines.append(text)
            if len(lines) >= limit:
                break
        return "\n".join(lines)

    def _open_update_page(self):
        webbrowser.open(self._update_url or DOWNLOAD_URL)
        if getattr(self, "_update_dlg", None) is not None:
            self._update_dlg.accept()

    def start_self_update(self):
        """点「立即更新」：转换先停干净，再后台下包、解压、写覆盖脚本。"""
        if not can_self_update():
            self._open_update_page()
            return
        thread = getattr(self, "_runner_thread", None)
        if thread is not None and thread.is_alive():
            self.notify("等这次解密结束再更新")
            return
        version = self._update_version
        zip_path = os.path.join(self.cache_path("update"), "TuneFree-%s.zip" % version)
        os.makedirs(os.path.dirname(zip_path), exist_ok=True)
        self._update_bar.setVisible(True)
        self._update_bar.setValue(0)
        self._update_status.setVisible(True)
        self._update_status.setText("正在准备…")
        self._update_go.setEnabled(False)
        self._update_later.setEnabled(False)

        worker = UpdateDownloader(version, zip_path, self)
        worker.stage.connect(self._update_status.setText)
        worker.progress.connect(self._on_update_progress)
        worker.finished.connect(self._on_update_ready)
        self._update_worker = worker
        worker.start()

    def _on_update_progress(self, done, total):
        if total > 0:
            percent = int(done * 100 / total)
            self._update_bar.setValue(percent)
            self._update_status.setText("正在下载… %d%%（%.1f / %.1f MB）"
                                        % (percent, done / 1048576.0, total / 1048576.0))
        else:
            self._update_status.setText("正在下载… %.1f MB" % (done / 1048576.0))

    def _on_update_ready(self, ok, payload):
        if not ok:
            self._update_status.setText("更新失败：%s" % payload)
            self._update_go.setEnabled(True)
            self._update_go.setText("重试")
            self._update_later.setEnabled(True)
            return
        self._update_bar.setValue(100)
        self._update_status.setText("正在重启完成更新…")
        try:
            script = write_update_script(payload, str(app_dir()))
        except Exception as e:
            self._update_status.setText("写更新脚本失败：%s" % e)
            self._update_go.setEnabled(True)
            return
        self.save_settings()
        if getattr(self, "toast", None) is not None:
            self.toast.hide()
        QTimer.singleShot(500, lambda: self._run_update_script(script))

    def _run_update_script(self, script):
        """把覆盖安装交给 bat，本进程退出让位。"""
        try:
            QProcess.startDetached("cmd.exe", ["/c", script])
        except Exception:
            try:
                os.startfile(script)
            except Exception:
                logging.warning("拉起更新脚本失败", exc_info=True)
                return
        try:
            self.player.stop()
        except Exception:
            pass
        QApplication.quit()

    def on_update_failed(self, reason):
        # 手动检查失败：设置页那行给原因就够了，再弹模态框会跟设置页抢焦点
        self._settings_check_done("检查失败：%s" % reason)
        if getattr(self, "_manual_update", False):
            self.notify("检查更新失败：%s" % reason, 4000)

    # ================= 收尾 =================
    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.lyric_panel.isVisible():
            self.lyric_panel.setGeometry(self.lyric_rect())
        if getattr(self, "toast", None) is not None and self.toast.isVisible():
            self.toast.reposition()

    def moveEvent(self, event):
        super().moveEvent(event)
        if getattr(self, "toast", None) is not None and self.toast.isVisible():
            self.toast.reposition()

    def showEvent(self, event):
        super().showEvent(event)
        apply_titlebar_theme(self, self.is_dark)

    def hideEvent(self, event):
        super().hideEvent(event)
        if getattr(self, "toast", None) is not None:
            self.toast.hide()

    def closeEvent(self, event):
        # 托盘常驻：关窗口只是收起来，扫描/转换还在后台跑
        if self.ui_tray and self._tray is not None and self._tray.isVisible() \
                and not self._really_quit:
            event.ignore()
            self.hide()
            if not self._tray_hint_shown:
                self._tray_hint_shown = True
                self.notify_system("TuneFree 还在后台",
                                   "点托盘图标回到主界面；右键托盘菜单可以真正退出。")
            return
        self._teardown_features()
        try:
            self.save_settings()
        except Exception:
            pass
        self.player.stop()
        if getattr(self, "toast", None) is not None:
            self.toast.hide()
        if self._runner is not None:
            self._runner.cancel()
        if self._runner_thread is not None and self._runner_thread.isRunning():
            self._runner_thread.wait(1500)
        if getattr(self, "_attach_thread", None) is not None and self._attach_thread.isRunning():
            self._attach_thread.wait(1500)
        super().closeEvent(event)


class RunnerBridge(QObject):
    """把工作线程里的事件转成 Qt 信号（跨线程自动排队）。"""

    event = Signal(str, object)


class AttachThread(QThread):
    """后台尝试注入 QQ 音乐（attach 可能卡住几百毫秒，不放到界面线程）。"""

    result = Signal(object, str)

    def run(self):
        session = None
        message = ""
        try:
            import session as qsession
            session = qsession.QQMusicSession(qsession.default_hook_path())
            if not session.attach():
                message = getattr(session, "last_error", "") or "注入失败"
                session = None
        except Exception as e:  # 兜底，别让线程带着异常退出
            message = str(e)
            session = None
        self.result.emit(session, message)


class RunnerThread(QThread):
    done = Signal()

    def __init__(self, runner, tracks, parent=None):
        super().__init__(parent)
        self.runner = runner
        self.tracks = tracks

    def run(self):
        try:
            self.runner.run(self.tracks)
        except Exception as e:
            logging.exception("任务异常: %s", e)
        finally:
            self.done.emit()


def run_gui(argv=None):
    setup_logging()
    app = QApplication(argv or sys.argv)
    # Qt 自带的对话框（挑颜色、选文件）默认说英文，装一下官方中文包，
    # 选颜色的窗口标题、OK / Cancel 这些才会跟着变中文
    translator = QTranslator(app)
    for name in ("qtbase_zh_CN", "qt_zh_CN"):
        if translator.load(name, QLibraryInfo.path(QLibraryInfo.TranslationsPath)):
            app.installTranslator(translator)
            break
    app.setApplicationName("TuneFree")
    app.setApplicationDisplayName("")
    icon = app_icon()
    if icon is not None:
        app.setWindowIcon(icon)
    window = TuneFreeWindow()
    window.show()
    return app.exec()
if __name__ == "__main__":
    raise SystemExit(run_gui())
