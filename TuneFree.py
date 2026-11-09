import sys
import os
import re
import time
import json
import math
import threading
import urllib.request
import webbrowser
from pathlib import Path
from PySide6.QtCore import *
from PySide6.QtWidgets import *
from PySide6.QtGui import *


# -------------------- 项目信息 --------------------
try:  # 版本号统一放在 version.py，老版本打包里没有这个文件就退回内置值
    from version import (
        VERSION, GITHUB_URL, GITHUB_RELEASES_URL, GITHUB_LATEST_API,
        GITHUB_LATEST_PAGE, VERSION_JSON_URLS, QQ_GROUP, DOWNLOAD_URL,
        SITE_URL, SITE_PRIMARY, SITE_PAGE,
    )
except Exception:  # pragma: no cover
    VERSION = "2.1.3"
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

# 赞赏 / 赞助相关（不是下载渠道，别和 DOWNLOAD_URL 混）
AFDIAN_URL = "https://ifdian.net/a/TuneFree"
# 赞赏码图片可能放的几个位置，按顺序找
SUPPORT_IMG_NAMES = ("赞赏码.png", "docs/support.png", "support.png")
# 「帮助」按钮直接落到官网的帮助区：写死 tunefree.html，避免经过 docs/index.html 那层跳转壳（跳转壳会丢 #锚点）
HELP_URL = SITE_PAGE + "#help"


# -------------------- 工具函数 --------------------
def app_dir():
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).parent
    return Path(__file__).parent


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
    """把后端进程的异常返回码翻译成能照着做的提示。"""
    if code in (4294967283, -13):
        return ("拒绝访问(EACCES)。常见原因：杀毒软件拦下了 ffmpeg.exe，或输出的 .mp3 正被"
                "播放器/网盘同步占用。把 TuneFree 整个文件夹加入杀软白名单再试一次。")
    if code in (4294967295, -1):
        return "后端异常退出，通常是 QQ音乐版本不匹配或注入被拦截，请看上方日志里的报错行。"
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


# -------------------- 主题样式表 --------------------
LIGHT_STYLESHEET = """
    QWidget {
        background-color: #fafbfc;
        color: #24272d;
        font-family: "Segoe UI", "Microsoft YaHei", sans-serif;
        font-size: 10pt;
    }
    #titleLabel {
        font-size: 22pt;
        font-weight: bold;
        color: #1c1a18;
        margin-bottom: 8px;
    }
    #label {
        color: #3c4048;
        padding-right: 8px;
        min-width: 60px;
    }
    #inputField {
        background-color: #ffffff;
        border: 1px solid #e3e8f0;
        border-radius: 8px;
        padding: 6px 10px;
        color: #24272d;
    }
    #inputField:focus {
        border-color: #2f6ee0;
        outline: none;
    }
    #browseBtn {
        background-color: #edf1f7;
        border: none;
        border-radius: 8px;
        padding: 6px 16px;
        color: #24272d;
    }
    #browseBtn:hover {
        background-color: #e3e8f0;
    }
    #switchLabel {
        color: #3c4048;
        font-size: 9.5pt;
    }
    #combo {
        background-color: #ffffff;
        border: 1px solid #e3e8f0;
        border-radius: 8px;
        padding: 4px 8px;
        color: #24272d;
        min-width: 70px;
    }
    #combo::drop-down {
        border: none;
    }
    #combo QAbstractItemView {
        background-color: #ffffff;
        color: #24272d;
        selection-background-color: #2f6ee0;
        selection-color: #ffffff;
    }
    #formatRadio {
        color: #24272d;
        spacing: 6px;
        margin-right: 12px;
    }
    #formatRadio::indicator {
        width: 14px;
        height: 14px;
        border-radius: 7px;
        background-color: #ffffff;
        border: 1px solid #e3e8f0;
    }
    #formatRadio::indicator:checked {
        background-color: #2f6ee0;
        border-color: #2f6ee0;
    }
    QScrollBar:vertical {
        background: transparent;
        width: 10px;
        margin: 6px 2px 6px 0;
    }
    QScrollBar::handle:vertical {
        background: #e3e8f0;
        border-radius: 4px;
        min-height: 32px;
    }
    QScrollBar::handle:vertical:hover {
        background: #a7adb8;
    }
    QScrollBar::handle:vertical:pressed {
        background: #7c828b;
    }
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
        height: 0;
        background: none;
        border: none;
    }
    QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
        background: transparent;
    }
    QScrollBar:horizontal {
        background: transparent;
        height: 10px;
        margin: 0 6px 2px 6px;
    }
    QScrollBar::handle:horizontal {
        background: #e3e8f0;
        border-radius: 4px;
        min-width: 32px;
    }
    QScrollBar::handle:horizontal:hover {
        background: #a7adb8;
    }
    QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {
        width: 0;
        background: none;
        border: none;
    }
    QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {
        background: transparent;
    }
    #log {
        background-color: #ffffff;
        border: 1px solid #e4e8ef;
        border-radius: 12px;
        padding: 10px;
        color: #24272d;
    }
    #startBtn {
        background-color: #22c55e;
        color: #ffffff;
        border: none;
        border-radius: 8px;
        padding: 8px 24px;
        font-weight: bold;
    }
    #startBtn:hover {
        background-color: #16a34a;
    }
    #startBtn:disabled {
        background-color: #a7adb8;
        color: #edf1f7;
    }
    #stopBtn {
        background-color: #ef4444;
        color: #ffffff;
        border: none;
        border-radius: 8px;
        padding: 8px 24px;
        font-weight: bold;
    }
    #stopBtn:hover {
        background-color: #dc2626;
    }
    #stopBtn:disabled {
        background-color: #a7adb8;
        color: #edf1f7;
    }
    #clearBtn, #helpBtn, #supportBtn {
        background-color: #edf1f7;
        border: none;
        border-radius: 8px;
        padding: 8px 16px;
        color: #24272d;
    }
    #clearBtn:hover, #helpBtn:hover, #supportBtn:hover {
        background-color: #e3e8f0;
    }
    #status {
        color: #474b52;
    }
    #progress {
        border: 1px solid #e3e8f0;
        border-radius: 8px;
        background-color: #edf1f7;
        text-align: center;
        font-size: 8pt;
        color: #24272d;
    }
    #progress::chunk {
        background-color: #2f6ee0;
        border-radius: 7px;
    }
    #tipLabel {
        color: #474b52;
        background-color: #eff3f9;
        border-radius: 8px;
        padding: 8px 12px;
        font-size: 9pt;
    }
    QMessageBox {
        background-color: #ffffff;
    }
    QMessageBox QPushButton {
        background-color: #edf1f7;
        border: none;
        border-radius: 6px;
        padding: 6px 16px;
        color: #24272d;
    }
    QMessageBox QPushButton:hover {
        background-color: #e3e8f0;
    }
    #starBtn {
        background-color: #fdf1d6;
        border: 1px solid #f59e0b;
        border-radius: 8px;
        padding: 8px 16px;
        color: #7a4a12;
        font-weight: bold;
    }
    #starBtn:hover {
        background-color: #fbe4b4;
    }
    #updateBanner {
        background-color: #fffaf0;
        border: 1px solid #f0cd8a;
        border-radius: 8px;
    }
    #updateText {
        color: #7a4a12;
        background: transparent;
    }
    #updateLinkBtn {
        background-color: #f59e0b;
        color: #ffffff;
        border: none;
        border-radius: 6px;
        padding: 5px 14px;
        font-weight: bold;
    }
    #updateLinkBtn:hover {
        background-color: #d97706;
    }
    #updateIgnoreBtn {
        background: transparent;
        color: #7a4a12;
        border: none;
        padding: 5px 8px;
    }
    #updateIgnoreBtn:hover {
        color: #5f3a0d;
        text-decoration: underline;
    }
"""

DARK_STYLESHEET = """
    QWidget {
        background-color: #000000;
        color: #e6e6e6;
        font-family: "Segoe UI", "Microsoft YaHei", sans-serif;
        font-size: 10pt;
    }
    #titleLabel {
        font-size: 22pt;
        font-weight: bold;
        color: #5b9bff;
        margin-bottom: 8px;
    }
    #label {
        color: #a3a3a3;
        padding-right: 8px;
        min-width: 60px;
    }
    #inputField {
        background-color: #141414;
        border: 1px solid #262626;
        border-radius: 8px;
        padding: 6px 10px;
        color: #e6e6e6;
    }
    #inputField:focus {
        border-color: #5b9bff;
    }
    #browseBtn {
        background-color: #262626;
        border: none;
        border-radius: 8px;
        padding: 6px 16px;
        color: #e6e6e6;
    }
    #browseBtn:hover {
        background-color: #333333;
    }
    #switchLabel {
        color: #a3a3a3;
        font-size: 9.5pt;
    }
    #combo {
        background-color: #141414;
        border: 1px solid #262626;
        border-radius: 8px;
        padding: 4px 8px;
        color: #e6e6e6;
        min-width: 70px;
    }
    #combo::drop-down {
        border: none;
    }
    #combo QAbstractItemView {
        background-color: #141414;
        color: #e6e6e6;
        selection-background-color: #5b9bff;
    }
    #formatRadio {
        color: #e6e6e6;
        spacing: 6px;
        margin-right: 12px;
    }
    #formatRadio::indicator {
        width: 14px;
        height: 14px;
        border-radius: 7px;
        background-color: #141414;
        border: 1px solid #262626;
    }
    #formatRadio::indicator:checked {
        background-color: #5b9bff;
        border-color: #5b9bff;
    }
    QScrollBar:vertical {
        background: transparent;
        width: 10px;
        margin: 6px 2px 6px 0;
    }
    QScrollBar::handle:vertical {
        background: #262626;
        border-radius: 4px;
        min-height: 32px;
    }
    QScrollBar::handle:vertical:hover {
        background: #333333;
    }
    QScrollBar::handle:vertical:pressed {
        background: #4a4a4a;
    }
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
        height: 0;
        background: none;
        border: none;
    }
    QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
        background: transparent;
    }
    QScrollBar:horizontal {
        background: transparent;
        height: 10px;
        margin: 0 6px 2px 6px;
    }
    QScrollBar::handle:horizontal {
        background: #262626;
        border-radius: 4px;
        min-width: 32px;
    }
    QScrollBar::handle:horizontal:hover {
        background: #333333;
    }
    QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {
        width: 0;
        background: none;
        border: none;
    }
    QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {
        background: transparent;
    }
    #log {
        background-color: #0b0b0b;
        border: 1px solid #262626;
        border-radius: 12px;
        padding: 10px;
        color: #e6e6e6;
    }
    #startBtn {
        background-color: #22c55e;
        color: #000000;
        border: none;
        border-radius: 8px;
        padding: 8px 24px;
        font-weight: bold;
    }
    #startBtn:hover {
        background-color: #16a34a;
    }
    #startBtn:disabled {
        background-color: #262626;
        color: #6b6b6b;
    }
    #stopBtn {
        background-color: #ef4444;
        color: #000000;
        border: none;
        border-radius: 8px;
        padding: 8px 24px;
        font-weight: bold;
    }
    #stopBtn:hover {
        background-color: #dc2626;
    }
    #stopBtn:disabled {
        background-color: #262626;
        color: #6b6b6b;
    }
    #clearBtn, #helpBtn, #supportBtn {
        background-color: #262626;
        border: none;
        border-radius: 8px;
        padding: 8px 16px;
        color: #e6e6e6;
    }
    #clearBtn:hover, #helpBtn:hover, #supportBtn:hover {
        background-color: #333333;
    }
    #status {
        color: #a3a3a3;
    }
    #progress {
        border: 1px solid #262626;
        border-radius: 8px;
        background-color: #141414;
        text-align: center;
        font-size: 8pt;
        color: #e6e6e6;
    }
    #progress::chunk {
        background-color: #5b9bff;
        border-radius: 7px;
    }
    #tipLabel {
        color: #a3a3a3;
        background-color: #141414;
        border-radius: 8px;
        padding: 8px 12px;
        font-size: 9pt;
    }
    QMessageBox {
        background-color: #000000;
    }
    QMessageBox QPushButton {
        background-color: #262626;
        border: none;
        border-radius: 6px;
        padding: 6px 16px;
        color: #e6e6e6;
    }
    QMessageBox QPushButton:hover {
        background-color: #333333;
    }
    #starBtn {
        background-color: #ffffff;
        border: 1px solid #fbbf24;
        border-radius: 8px;
        padding: 8px 16px;
        color: #fbbf24;
        font-weight: bold;
    }
    #starBtn:hover {
        background-color: #2a2210;
    }
    #updateBanner {
        background-color: #ffffff;
        border: 1px solid #7a5410;
        border-radius: 8px;
    }
    #updateText {
        color: #fbbf24;
        background: transparent;
    }
    #updateLinkBtn {
        background-color: #fbbf24;
        color: #000000;
        border: none;
        border-radius: 6px;
        padding: 5px 14px;
        font-weight: bold;
    }
    #updateLinkBtn:hover {
        background-color: #fcd34d;
    }
    #updateIgnoreBtn {
        background: transparent;
        color: #b9a97e;
        border: none;
        padding: 5px 8px;
    }
    #updateIgnoreBtn:hover {
        color: #fbbf24;
        text-decoration: underline;
    }
"""


# -------------------- 支持作者弹窗样式 --------------------
# 单独写一套，不继承主窗口的 QWidget 规则：弹窗里控件少，字号和间距才好控制
SUPPORT_QSS_LIGHT = """
    QDialog { background-color: #fbfaf9; }
    QLabel { color: #3c4048; background: transparent; font-family: "Segoe UI", "Microsoft YaHei", sans-serif; }
    QLabel#supportTitle { color: #1c1a18; font-size: 15pt; font-weight: bold; }
    QLabel#supportSub { color: #7c828b; font-size: 9pt; }
    QLabel#supportDesc { color: #474b52; font-size: 10pt; }
    QLabel#supportHint { color: #a49e95; font-size: 8.5pt; }
    QFrame#supportCard { background-color: #f6f3ef; border: 1px solid #e8e3dc; border-radius: 14px; }
    QPushButton {
        font-family: "Segoe UI", "Microsoft YaHei", sans-serif;
        font-size: 10pt; border-radius: 9px; padding: 9px 0; min-width: 108px;
    }
    QPushButton#afdianBtn {
        background-color: #2f6ee0; color: #fbfaf9; border: none; font-weight: 600;
        min-width: 96px; padding: 9px 16px;
    }
    QPushButton#afdianBtn:hover { background-color: #2560c9; }
    QPushButton#afdianBtn:pressed { background-color: #1d4fae; }
    QPushButton#closeBtn { background-color: #eff3f9; color: #3c4048; border: 1px solid #e4e8ef; }
    QPushButton#closeBtn:hover { background-color: #e6ecf4; }
    QLabel#doneBadge, QLabel#doneBadgeWarn {
        border-radius: 10px; font-size: 15pt; font-weight: bold;
    }
    QLabel#doneBadge { background-color: #e8f6ed; color: #16a34a; }
    QLabel#doneBadgeWarn { background-color: #fef3c7; color: #d97706; }
    QLabel#doneTitle { color: #1c1a18; font-size: 13pt; font-weight: bold; }
    QFrame#doneRow { background: transparent; border-bottom: 1px solid #ecf0f6; }
    QLabel#doneKey { color: #7c828b; font-size: 9.5pt; }
    QLabel#doneVal { color: #1c1a18; font-size: 10pt; font-weight: 600; }
    QLabel#doneValBad { color: #e11d48; font-size: 10pt; font-weight: 600; }
    QLabel#doneValMono { color: #474b52; font-size: 9pt; font-family: Consolas, "Cascadia Mono", monospace; }
    QPushButton#linkBtn {
        background: transparent; border: none; color: #2f6ee0;
        font-size: 9.5pt; font-weight: 600; padding: 6px 4px; min-width: 0;
    }
    QPushButton#linkBtn:hover { color: #1d4fae; }
    QPushButton#doneCloseBtn {
        background-color: #eff3f9; color: #3c4048; border: 1px solid #e4e8ef;
        min-width: 72px; padding: 9px 14px;
    }
    QPushButton#doneCloseBtn:hover { background-color: #e6ecf4; }
    QFrame#troubleBox {
        background-color: #f3f7fd; border: 1px solid #d8e4f6; border-radius: 12px;
    }
    QLabel#troubleHead { color: #4a6fa5; font-size: 9pt; font-weight: 600; }
    QLabel#troubleNum {
        background-color: #d7e4f7; color: #2a5596; border-radius: 9px;
        font-size: 8pt; font-weight: bold;
    }
    QLabel#troubleText { color: #474b52; font-size: 9pt; }
    QLabel#doneNote {
        color: #3d5f92; background-color: #eef4fd; border-radius: 9px;
        padding: 9px 12px; font-size: 9pt;
    }
"""

SUPPORT_QSS_DARK = """
    QDialog { background-color: #0a0a0a; }
    QLabel { color: #d4d4d4; background: transparent; font-family: "Segoe UI", "Microsoft YaHei", sans-serif; }
    QLabel#supportTitle { color: #f1f1f1; font-size: 15pt; font-weight: bold; }
    QLabel#supportSub { color: #8a8a8a; font-size: 9pt; }
    QLabel#supportDesc { color: #c4c4c4; font-size: 10pt; }
    QLabel#supportHint { color: #7a7a7a; font-size: 8.5pt; }
    QFrame#supportCard { background-color: #ffffff; border: 1px solid #1e1e1e; border-radius: 14px; }
    QPushButton {
        font-family: "Segoe UI", "Microsoft YaHei", sans-serif;
        font-size: 10pt; border-radius: 9px; padding: 9px 0; min-width: 108px;
    }
    QPushButton#afdianBtn { background-color: #2f6ee0; color: #ffffff; border: none; font-weight: 600; }
    QPushButton#afdianBtn:hover { background-color: #3f7dd8; }
    QPushButton#afdianBtn:pressed { background-color: #2560c9; }
    QPushButton#closeBtn { background-color: #141414; color: #d4d4d4; border: 1px solid #2a2a2a; }
    QPushButton#closeBtn:hover { background-color: #1f1f1f; }
    QLabel#doneBadge, QLabel#doneBadgeWarn {
        border-radius: 10px; font-size: 15pt; font-weight: bold;
    }
    QLabel#doneBadge { background-color: #0f2417; color: #4ade80; }
    QLabel#doneBadgeWarn { background-color: #2a2109; color: #fbbf24; }
    QLabel#doneTitle { color: #f1f1f1; font-size: 13pt; font-weight: bold; }
    QFrame#doneRow { background: transparent; border-bottom: 1px solid #1a1a1a; }
    QLabel#doneKey { color: #8a8a8a; font-size: 9.5pt; }
    QLabel#doneVal { color: #e6e6e6; font-size: 10pt; font-weight: 600; }
    QLabel#doneValBad { color: #fb7185; font-size: 10pt; font-weight: 600; }
    QLabel#doneValMono { color: #d4d4d4; font-size: 9pt; font-family: Consolas, "Cascadia Mono", monospace; }
    QPushButton#linkBtn {
        background: transparent; border: none; color: #5b9bff;
        font-size: 9.5pt; font-weight: 600; padding: 6px 4px; min-width: 0;
    }
    QPushButton#linkBtn:hover { color: #9cc4ff; }
    QPushButton#doneCloseBtn {
        background-color: #141414; color: #d4d4d4; border: 1px solid #2a2a2a;
        min-width: 72px; padding: 9px 14px;
    }
    QPushButton#doneCloseBtn:hover { background-color: #1f1f1f; }
    QFrame#troubleBox {
        background-color: #0d1420; border: 1px solid #1d2a40; border-radius: 12px;
    }
    QLabel#troubleHead { color: #8fb4ee; font-size: 9pt; font-weight: 600; }
    QLabel#troubleNum {
        background-color: #1d2b45; color: #b9d0f7; border-radius: 9px;
        font-size: 8pt; font-weight: bold;
    }
    QLabel#troubleText { color: #cfcac2; font-size: 9pt; }
    QLabel#doneNote {
        color: #b9cff2; background-color: #101a2b; border-radius: 9px;
        padding: 9px 12px; font-size: 9pt;
    }
"""


def make_badge_pixmap(success, dark=False, size=34):
    """自己画完成/失败徽标，不依赖系统字体里有没有 ✓ 这个字形。"""
    dpr = 2
    pm = QPixmap(size * dpr, size * dpr)
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setPen(Qt.NoPen)
    if success:
        p.setBrush(QColor("#16301f" if dark else "#e9f9ef"))
    else:
        p.setBrush(QColor("#332a13" if dark else "#fef3c7"))
    p.drawRoundedRect(QRectF(0, 0, size, size), 10, 10)

    if success:
        color = "#4ade80" if dark else "#16a34a"
    else:
        color = "#fbbf24" if dark else "#d97706"
    pen = QPen(QColor(color), 3.2)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    p.setPen(pen)
    if success:
        p.drawPolyline(QPolygonF([QPointF(9.8, 17.8), QPointF(14.7, 22.6), QPointF(24.3, 11.9)]))
    else:
        p.drawLine(QPointF(17.0, 9.6), QPointF(17.0, 19.6))
        p.drawLine(QPointF(17.0, 24.3), QPointF(17.0, 24.9))
    p.end()
    return pm


def format_duration(seconds):
    """把秒数变成人话：45.2 秒 / 3 分 12 秒 / 1 小时 4 分。"""
    seconds = max(0.0, float(seconds))
    if seconds < 60:
        return f"{seconds:.1f} 秒"
    total = int(round(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours} 小时 {minutes} 分"
    return f"{minutes} 分 {secs} 秒"


# -------------------- 自绘开关 --------------------
# 原生 QCheckBox 的方框太「工具」，这里自己画一个胶囊滑轨，三种开关共用一套逻辑
SWITCH_COLORS = {
    "light": {
        "track_off": "#e4e8ef", "track_off_hover": "#d6dce7",
        "track_on": "#2f6ee0", "track_on_hover": "#2560c9",
        "knob": "#ffffff", "knob_border": "#d3d9e3",
        "icon_off": "#7c828b", "icon_on": "#2f6ee0",
    },
    "dark": {
        "track_off": "#2a2a2a", "track_off_hover": "#363636",
        "track_on": "#5b9bff", "track_on_hover": "#7db1ff",
        "knob": "#f2f2f2", "knob_border": "#000000",
        "icon_off": "#8a8a8a", "icon_on": "#5b9bff",
    },
}


# 主题那颗开关单独一套色：关着＝白天（淡蓝天空 + 太阳），打开＝夜里（深蓝夜空 + 月亮）
THEME_SWITCH_COLORS = {
    "light": {
        "track_a": "#eef5ff", "track_b": "#d9e7fb", "border": "#cbdaef",
        "knob": "#ffffff", "knob_border": "#d5e0ef",
        "sun_core": "#ffdd7d", "sun_edge": "#f3962a", "ray": "#ef9f22",
        "deco": "#b8c7de",
    },
    "dark": {
        "track_a": "#3a4d78", "track_b": "#1b2740", "border": "#17223a",
        "knob": "#fdf9ee", "knob_border": "#cfd9ee",
        "moon": "#e9c87e", "sparkle": "#b9d2ff", "crater": "#d4b269", "deco": "#eaf2ff",
    },
}


def _paint_sparkle(painter, cx, cy, r, color):
    """四角星光，小尺寸下比圆点更像「星」。"""
    path = QPainterPath()
    path.moveTo(cx, cy - r)
    path.quadTo(cx, cy, cx + r, cy)
    path.quadTo(cx, cy, cx, cy + r)
    path.quadTo(cx, cy, cx - r, cy)
    path.quadTo(cx, cy, cx, cy - r)
    painter.setPen(Qt.NoPen)
    painter.setBrush(color)
    painter.drawPath(path)


def _paint_sun(painter, cx, cy, r, core, edge, ray):
    """暖黄太阳：一圈柔光 + 渐变圆盘 + 八根圆头光芒 + 左上高光。"""
    glow = QRadialGradient(QPointF(cx, cy), r * 1.55)
    glow.setColorAt(0.0, QColor(255, 206, 110, 55))
    glow.setColorAt(1.0, QColor(255, 206, 110, 0))
    painter.setPen(Qt.NoPen)
    painter.setBrush(glow)
    painter.drawEllipse(QPointF(cx, cy), r * 1.55, r * 1.55)

    grad = QRadialGradient(QPointF(cx - r * 0.28, cy - r * 0.32), r * 1.8)
    grad.setColorAt(0.0, QColor(core))
    grad.setColorAt(1.0, QColor(edge))
    painter.setBrush(grad)
    painter.drawEllipse(QPointF(cx, cy), r * 0.74, r * 0.74)

    pen = QPen(QColor(ray), max(1.15, r * 0.18))
    pen.setCapStyle(Qt.RoundCap)
    painter.setPen(pen)
    for i in range(8):
        a = math.pi / 4 * i
        dx, dy = math.cos(a), math.sin(a)
        painter.drawLine(QPointF(cx + dx * r * 1.06, cy + dy * r * 1.06),
                         QPointF(cx + dx * r * 1.38, cy + dy * r * 1.38))

    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor(255, 255, 255, 160))
    painter.drawEllipse(QPointF(cx - r * 0.24, cy - r * 0.3), r * 0.18, r * 0.14)


def _paint_moon(painter, cx, cy, r, color, sparkle=None, crater=None):
    """暖金月牙（一个圆减去偏移的另一个圆），带两个浅坑，右上角再点一颗小星。"""
    body = QPainterPath()
    body.addEllipse(QPointF(cx, cy), r, r)
    bite = QPainterPath()
    bite.addEllipse(QPointF(cx + r * 0.58, cy - r * 0.46), r * 0.86, r * 0.86)
    crescent = body.subtracted(bite)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor(color))
    painter.drawPath(crescent)

    if crater:
        painter.setClipPath(crescent)
        painter.setBrush(QColor(crater))
        painter.drawEllipse(QPointF(cx - r * 0.12, cy - r * 0.16), r * 0.2, r * 0.2)
        painter.drawEllipse(QPointF(cx - r * 0.02, cy + r * 0.42), r * 0.13, r * 0.13)
        painter.setClipping(False)

    if sparkle:
        _paint_sparkle(painter, cx + r * 1.12, cy - r * 0.95,
                       max(1.6, r * 0.4), QColor(sparkle))


class SwitchButton(QAbstractButton):
    """胶囊开关：可点击/空格切换，滑块带缓动动画，主题切换时自动换色。"""

    def __init__(self, parent=None, checked=False, dark=False,
                 width=44, height=24, glyph=""):
        super().__init__(parent)
        self._w, self._h = width, height
        self._glyph = glyph            # ""=纯色开关 / "theme"=滑块里带太阳月亮
        self._pos = 1.0 if checked else 0.0
        self._hover = False
        self._syncing = False
        self._colors = SWITCH_COLORS["dark" if dark else "light"]
        self.setCheckable(True)
        self.setChecked(checked)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setFixedSize(width, height)
        self._anim = QPropertyAnimation(self, b"knobPos", self)
        self._anim.setDuration(260)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self._icon_t = 1.0        # 图标进出场进度：0=刚切换 1=已到位
        self._exit_on = checked   # 这次切换之前显示的是哪个图标
        self._icon_anim = QPropertyAnimation(self, b"iconT", self)
        self._icon_anim.setDuration(640)
        self._icon_anim.setEasingCurve(QEasingCurve.OutCubic)
        self.toggled.connect(self._slide)

    # 滑块位置 0=关 1=开，交给动画去推
    def _get_knob_pos(self):
        return self._pos

    def _set_knob_pos(self, value):
        self._pos = float(value)
        self.update()

    knobPos = Property(float, _get_knob_pos, _set_knob_pos)

    def _get_icon_t(self):
        return self._icon_t

    def _set_icon_t(self, value):
        self._icon_t = float(value)
        self.update()

    iconT = Property(float, _get_icon_t, _set_icon_t)

    def _slide(self, checked):
        if self._syncing:      # 程序化设值（启动恢复主题）不走动画
            self._pos = 1.0 if checked else 0.0
            self._icon_t = 1.0
            self._exit_on = checked
            self.update()
            return
        self._exit_on = not checked
        self._anim.stop()
        self._anim.setStartValue(self._pos)
        self._anim.setEndValue(1.0 if checked else 0.0)
        self._anim.start()
        if self._glyph == "theme":
            self._icon_anim.stop()
            self._icon_anim.setStartValue(0.0)
            self._icon_anim.setEndValue(1.0)
            self._icon_anim.start()

    def set_dark(self, dark):
        self._colors = SWITCH_COLORS["dark" if dark else "light"]
        self.update()

    def setChecked(self, checked):  # noqa: N802 - Qt 命名
        self._pos = 1.0 if checked else 0.0
        self._icon_t = 1.0
        self._exit_on = checked
        self._syncing = True
        try:
            super().setChecked(checked)
        finally:
            self._syncing = False
        self.update()

    def enterEvent(self, event):
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def sizeHint(self):
        return QSize(self._w, self._h)

    def _paint_theme_track(self, p, r, radius, on):
        """主题开关的轨道：白天淡蓝天空、夜里深蓝夜空，切换时两层交叉淡入。"""
        pos = min(1.0, max(0.0, self._pos))
        if 0.0 < pos < 1.0 and self._exit_on != on:
            self._paint_theme_layer(p, r, radius, self._exit_on, 1.0)
            self._paint_theme_layer(p, r, radius, on, pos)
        else:
            self._paint_theme_layer(p, r, radius, on, 1.0)

    def _paint_theme_layer(self, p, r, radius, on, alpha):
        c = THEME_SWITCH_COLORS["dark" if on else "light"]
        p.setOpacity(alpha)
        grad = QLinearGradient(r.topLeft(), r.bottomLeft())
        grad.setColorAt(0.0, QColor(c["track_a"]))
        grad.setColorAt(1.0, QColor(c["track_b"]))
        p.setPen(QPen(QColor(c["border"]), 1))
        p.setBrush(grad)
        p.drawRoundedRect(r, radius, radius)

        p.setPen(Qt.NoPen)
        if on:      # 月亮在右边，左边空着 —— 撒两颗星
            _paint_sparkle(p, r.left() + 14.0, r.center().y() - 4.6, 3.1, QColor(c["deco"]))
            _paint_sparkle(p, r.left() + 23.5, r.center().y() + 5.2, 1.9, QColor(c["deco"]))
        else:       # 太阳在左边，右边空着 —— 落几点细沙
            p.setBrush(QColor(c["deco"]))
            p.drawEllipse(QPointF(r.right() - 13.0, r.center().y() - 4.2), 2.1, 2.1)
            p.drawEllipse(QPointF(r.right() - 20.5, r.center().y() + 5.0), 1.3, 1.3)
            p.drawEllipse(QPointF(r.right() - 8.6, r.center().y() + 6.0), 1.0, 1.0)
        p.setOpacity(1.0)

    def paintEvent(self, event):
        on = self.isChecked()
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        radius = r.height() / 2

        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)

        if self._glyph == "theme":
            self._paint_theme_track(p, r, radius, on)
            c = THEME_SWITCH_COLORS["dark" if on else "light"]
        else:
            c = self._colors
            if on:
                track = c["track_on_hover"] if self._hover else c["track_on"]
            else:
                track = c["track_off_hover"] if self._hover else c["track_off"]
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(track))
            p.drawRoundedRect(r, radius, radius)

        margin = 3.0
        d = r.height() - margin * 2
        x = margin + (r.width() - d - margin * 2) * self._pos
        knob = QRectF(r.left() + x, r.top() + margin, d, d)

        # 滑块底下垫一层淡影，浅色轨道上才有层次
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 30))
        p.drawEllipse(knob.translated(0, 1.2))

        p.setPen(QPen(QColor(c["knob_border"]), 1))
        p.setBrush(QColor(c["knob"]))
        p.drawEllipse(knob)

        cx, cy = knob.center().x(), knob.center().y()
        if self._glyph == "theme":
            # 图标＝当前主题：前段旧图标退场，后段新图标进场，留一点交叠免得中间空一拍
            t = min(1.0, max(0.0, self._icon_t))
            out_t = min(1.0, t / 0.5)
            in_t = max(0.0, min(1.0, (t - 0.2) / 0.8))
            if out_t < 1.0:
                self._paint_theme_icon(p, self._exit_on, cx, cy, d, out_t, True)
            self._paint_theme_icon(p, on, cx, cy, d, in_t, False)

        if self.hasFocus():
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(QColor(self._colors["track_on"]), 1.4))
            p.drawRoundedRect(QRectF(self.rect()).adjusted(1.2, 1.2, -1.2, -1.2), radius, radius)
        p.end()

    def _paint_theme_icon(self, p, on, cx, cy, d, progress, exiting):
        """太阳绕中心转进来/转出去；月亮从下面升上来/沉下去。"""
        c = THEME_SWITCH_COLORS["dark" if on else "light"]
        r = d * 0.30
        if exiting:
            phase, alpha = progress, (1.0 - progress) ** 1.6
        else:
            phase, alpha = 1.0 - progress, progress ** 1.6
        p.save()
        p.setOpacity(max(0.0, min(1.0, alpha)))
        if on:
            p.translate(cx, cy + 7.0 * phase)
            p.rotate((14.0 if exiting else -14.0) * phase)
            _paint_moon(p, 0.0, 0.0, r, c["moon"], c.get("sparkle"), c.get("crater"))
        else:
            p.translate(cx, cy)
            p.rotate((110.0 if exiting else -110.0) * phase)
            _paint_sun(p, 0.0, 0.0, r, c["sun_core"], c["sun_edge"], c["ray"])
        p.restore()


class ClickableLabel(QLabel):
    """点一下等于点到旁边的开关，免得手指非要精准点进滑块。"""

    clicked = Signal()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


# -------------------- 主窗口 --------------------
class TuneFreeWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"TuneFree-{VERSION}")
        self.setMinimumSize(950, 780)

        # 后端命令行程序：打包后是 main.exe；源码直接运行时回退到 python main.py
        self.exe_path = app_dir() / "main.exe"
        self.dev_script = app_dir() / "main.py"
        self.process = None
        self.running = False
        self.settings, _ = make_settings()
        self.is_dark = self._saved_bool("ui/dark", False)  # 首次运行默认亮色
        self._fade_overlay = None   # 换主题时盖在窗口上的旧主题截图
        self._fade_anim = None
        self._pre_theme_shot = None  # 按下主题开关时拍的旧窗口
        self.decrypt_stats = None  # (成功数, 失败数)，用于判断这次是否真的转出了文件
        self.transcode_stats = None  # (成功数, 失败数)，转码阶段的结果
        self._star_prompted = False  # 本次运行内已弹过，避免设置存不下时反复打扰
        self._buf = ""  # 管道输出的半行缓冲
        self._user_stopped = False  # 用户点了停止，收尾时别再报"执行失败"
        self._started_at = None
        self._checking_update = False
        self._manual_update = False
        self._latest_version = ""
        self._latest_url = GITHUB_LATEST_PAGE
        self.checker = UpdateChecker(self)
        self.checker.found.connect(self.on_update_found)
        self.checker.failed.connect(self.on_update_failed)

        self.setup_ui()
        self.apply_stylesheet()

        saved_geo = self.settings.value("ui/geometry")
        if saved_geo is not None:
            self.restoreGeometry(saved_geo)

        self.restore_settings()

        icon = app_icon()
        if icon is not None:
            self.setWindowIcon(icon)

        # 启动 3 秒后再查更新：别和界面初始化、QQ音乐注入抢时间
        QTimer.singleShot(3000, self.auto_check_update)

        if not self.backend_ready():
            QMessageBox.warning(
                self, "提示",
                f"未找到后端程序（main.exe 或源码版的 main.py），请确保它与本程序在同一目录。\n当前目录：{app_dir()}")

    def _saved_bool(self, key, default=False):
        """QSettings 在不同平台把布尔存成 true/false/1/0 字符串，这里统一解析。"""
        try:
            v = self.settings.value(key)
        except Exception:
            return default
        if v is None:
            return default
        if isinstance(v, bool):
            return v
        return str(v).strip().lower() in ("1", "true", "yes", "on")

    def restore_settings(self):
        """记住上次用的路径和选项，省得每次开程序都要重新选一遍。"""
        s = self.settings
        self.input_edit.setText(s.value("path/input", str(app_dir())))
        self.root_edit.setText(s.value("path/root", str(app_dir() / "output")))

        bitrate = s.value("out/bitrate", "192k")
        if self.bitrate_combo.findText(bitrate) < 0:
            bitrate = "192k"
        self.bitrate_combo.setCurrentText(bitrate)
        fmt = s.value("out/format", "mp3")
        {"flac": self.format_flac, "both": self.format_both}.get(fmt, self.format_mp3).setChecked(True)
        self.overwrite_switch.setChecked(self._saved_bool("out/force", False))
        self.check_update_switch.setChecked(self._saved_bool("ui/check_update", True))

    def save_settings(self):
        s = self.settings
        try:
            s.setValue("path/input", self.input_edit.text().strip())
            s.setValue("path/root", self.root_edit.text().strip())
            s.setValue("out/bitrate", self.bitrate_combo.currentText())
            s.setValue("out/format", self.get_output_format())
            s.setValue("out/force", self.overwrite_switch.isChecked())
            s.setValue("ui/check_update", self.check_update_switch.isChecked())
            s.sync()
        except Exception:
            pass

    def setup_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(22, 20, 22, 18)
        main_layout.setSpacing(14)

        # 新版本提示条（默认隐藏，发现新版本才出现）
        self.update_banner = QFrame()
        self.update_banner.setObjectName("updateBanner")
        banner_layout = QHBoxLayout(self.update_banner)
        banner_layout.setContentsMargins(12, 6, 10, 6)
        banner_layout.setSpacing(10)
        self.update_text = QLabel("")
        self.update_text.setObjectName("updateText")
        self.update_text.setWordWrap(True)
        banner_layout.addWidget(self.update_text, 1)
        self.update_link_btn = QPushButton("查看更新")
        self.update_link_btn.setObjectName("updateLinkBtn")
        self.update_link_btn.setCursor(Qt.PointingHandCursor)
        self.update_link_btn.clicked.connect(self.open_update_page)
        banner_layout.addWidget(self.update_link_btn)
        self.update_ignore_btn = QPushButton("忽略此版本")
        self.update_ignore_btn.setObjectName("updateIgnoreBtn")
        self.update_ignore_btn.setCursor(Qt.PointingHandCursor)
        self.update_ignore_btn.clicked.connect(self.ignore_this_version)
        banner_layout.addWidget(self.update_ignore_btn)
        self.update_banner.setVisible(False)
        main_layout.addWidget(self.update_banner)

        # 标题行
        title_layout = QHBoxLayout()
        title = QLabel("TuneFree 让音乐自由")
        title.setObjectName("titleLabel")
        title_layout.addWidget(title)
        title_layout.addStretch()
        
        self.star_btn = QPushButton("⭐ Star 支持")
        self.star_btn.setObjectName("starBtn")
        self.star_btn.setCursor(Qt.PointingHandCursor)
        self.star_btn.setToolTip("如果 TuneFree 帮到了你，去 GitHub 点个 Star，能让更多人找到这个项目。")
        self.star_btn.clicked.connect(self.open_github)
        title_layout.addWidget(self.star_btn)

        self.support_btn = QPushButton("❤️ 支持作者")
        self.support_btn.setObjectName("supportBtn")
        self.support_btn.clicked.connect(self.open_support)
        title_layout.addWidget(self.support_btn)
        
        self.theme_switch = SwitchButton(
            checked=self.is_dark, dark=self.is_dark, width=64, height=30, glyph="theme")
        self.theme_switch.setToolTip("在明亮 / 黑夜主题之间切换，选择会被记住。")
        self.theme_switch.setAccessibleName("切换主题")
        self.theme_switch.toggled.connect(self.set_theme)
        # 按下（还没切状态）就先拍一张，换主题时拿它做淡出，否则截图里开关已经是新颜色了
        self.theme_switch.pressed.connect(self._snapshot_before_theme)
        title_layout.addWidget(self.theme_switch)
        
        main_layout.addLayout(title_layout)

        # 输入目录
        row1 = QHBoxLayout()
        row1.setSpacing(10)
        label_input = QLabel("输入目录")
        label_input.setObjectName("label")
        self.input_edit = QLineEdit()
        self.input_edit.setObjectName("inputField")
        self.input_edit.editingFinished.connect(self.save_settings)
        self.input_edit.setToolTip(
            "包含 .mflac 或 .mgg 文件的文件夹。<br>建议使用英文路径，避免中文、日文等特殊字符，以防解密失败。")
        browse_input = QPushButton("浏览")
        browse_input.setObjectName("browseBtn")
        browse_input.clicked.connect(lambda: self.browse_folder(self.input_edit))
        row1.addWidget(label_input)
        row1.addWidget(self.input_edit, 1)
        row1.addWidget(browse_input)
        main_layout.addLayout(row1)

        # 第二行：重新生成开关 / 比特率 / 输出格式
        row2 = QHBoxLayout()
        row2.setSpacing(8)
        self.overwrite_label = ClickableLabel("重新生成已存在的 MP3")
        self.overwrite_label.setObjectName("switchLabel")
        self.overwrite_label.setCursor(Qt.PointingHandCursor)
        self.overwrite_switch = SwitchButton(dark=self.is_dark, width=42, height=22)
        self.overwrite_switch.setAccessibleName("重新生成已存在的 MP3")
        self.overwrite_switch.setToolTip(
            "默认跳过已经生成过的 MP3，同一批歌再跑一次会快很多。\n打开后即使 MP3 已存在也会重新转码（改了比特率时用得上）。")
        # 点文字也能切换，省得非要点到那个小滑块
        self.overwrite_label.clicked.connect(self.overwrite_switch.toggle)
        row2.addWidget(self.overwrite_label)
        row2.addWidget(self.overwrite_switch)
        row2.addSpacing(18)

        label_bitrate = QLabel("比特率")
        label_bitrate.setObjectName("label")
        self.bitrate_combo = QComboBox()
        self.bitrate_combo.addItems(["128k", "160k", "192k", "224k", "256k", "320k"])
        self.bitrate_combo.setCurrentText("192k")
        self.bitrate_combo.setObjectName("combo")
        self.bitrate_combo.setToolTip("数值越高，MP3 音质越好，文件越大。\n推荐 192k（平衡）或 320k（高品质）。")
        row2.addWidget(label_bitrate)
        row2.addWidget(self.bitrate_combo)
        row2.addSpacing(24)

        label_format = QLabel("输出格式")
        label_format.setObjectName("label")
        row2.addWidget(label_format)
        self.format_group = QButtonGroup(self)
        self.format_mp3 = QRadioButton("仅 MP3")
        self.format_flac = QRadioButton("仅 FLAC")
        self.format_both = QRadioButton("两者均保留")
        for btn in [self.format_mp3, self.format_flac, self.format_both]:
            btn.setObjectName("formatRadio")
            self.format_group.addButton(btn)
        self.format_mp3.setChecked(True)
        self.format_mp3.setToolTip("只生成 MP3 文件，解密后的 FLAC 会自动删除。")
        self.format_flac.setToolTip(
            "只保留 FLAC 无损文件，不生成 MP3。<br>注意：部分歌曲解密后是 OGG 格式，会强制转 MP3 并删除 OGG。")
        self.format_both.setToolTip("同时保留 FLAC 和 MP3 两种格式，适合收藏爱好者。")
        row2.addWidget(self.format_mp3)
        row2.addWidget(self.format_flac)
        row2.addWidget(self.format_both)
        row2.addStretch()
        main_layout.addLayout(row2)

        # 输出目录
        row3 = QHBoxLayout()
        row3.setSpacing(10)
        label_output = QLabel("输出目录")
        label_output.setObjectName("label")
        self.root_edit = QLineEdit()
        self.root_edit.setObjectName("inputField")
        self.root_edit.editingFinished.connect(self.save_settings)
        self.root_edit.setToolTip("转换后的 MP3 和 FLAC 都放在这个文件夹里，程序会自动在其下创建 mp3 和 flac 子文件夹。")
        browse_output = QPushButton("浏览")
        browse_output.setObjectName("browseBtn")
        browse_output.clicked.connect(lambda: self.browse_folder(self.root_edit))
        row3.addWidget(label_output)
        row3.addWidget(self.root_edit, 1)
        row3.addWidget(browse_output)
        main_layout.addLayout(row3)

        # 日志
        self.log = QTextEdit()
        self.log.setObjectName("log")
        self.log.setReadOnly(True)
        self.log.setFont(QFont("Consolas", 9))
        self.log.document().setMaximumBlockCount(5000)   # 长时间跑大目录时别让日志把内存吃满
        self.log.setToolTip("程序运行过程中的详细日志，包括解密进度、转码状态和错误信息。")
        main_layout.addWidget(self.log, 1)

        # ---------- 提示信息区域 ----------
        tip_label = QLabel(
            "使用提示：\n"
            "• 转换前请确保 QQ音乐客户端已在后台运行（否则会提示无法连接）。\n"
            "• 建议输入/输出路径使用纯英文，避免中文字符，以免解密失败。\n"
            "• 若遇到“No such file or directory”错误，请检查路径中是否包含空格或特殊字符。\n"
            "• 选择“仅 FLAC”时，部分 OGG 文件会强制转 MP3 并删除 OGG 源文件。"
        )
        tip_label.setObjectName("tipLabel")
        tip_label.setWordWrap(True)
        main_layout.addWidget(tip_label)

        # 底部按钮
        bottom = QHBoxLayout()
        self.status_label = QLabel("就绪")
        self.status_label.setObjectName("status")

        self.progress = QProgressBar()
        self.progress.setObjectName("progress")
        self.progress.setFixedWidth(150)
        self.progress.setFixedHeight(16)
        self.progress.setTextVisible(False)   # 文字已在左边状态栏，条内不再重复
        self.progress.setVisible(False)
        bottom.setSpacing(10)
        bottom.addWidget(self.status_label)
        bottom.addWidget(self.progress)
        bottom.addStretch()
        self.start_btn = QPushButton("开始执行")
        self.start_btn.setObjectName("startBtn")
        self.start_btn.clicked.connect(self.start_conversion)
        self.start_btn.setToolTip("点击开始转换，处理过程会显示在日志中。")
        self.stop_btn = QPushButton("停止")
        self.stop_btn.setObjectName("stopBtn")
        self.stop_btn.clicked.connect(self.stop_conversion)
        self.stop_btn.setEnabled(False)
        self.stop_btn.setToolTip("终止当前正在进行的转换任务。")
        self.clear_btn = QPushButton("清空日志")
        self.clear_btn.setObjectName("clearBtn")
        self.clear_btn.clicked.connect(self.clear_log)
        self.help_btn = QPushButton("帮助")
        self.help_btn.setObjectName("helpBtn")
        self.help_btn.clicked.connect(self.show_help)
        self.help_btn.setToolTip("在浏览器打开在线帮助：下载、使用说明、常见问题都在里面。")

        self.check_update_btn = QPushButton("检查更新")
        self.check_update_btn.setObjectName("helpBtn")
        self.check_update_btn.clicked.connect(self.manual_check_update)
        self.check_update_btn.setToolTip(f"当前版本 {VERSION}，点一下看看有没有新版。")

        self.update_pref_label = ClickableLabel("启动时自动检查更新")
        self.update_pref_label.setObjectName("switchLabel")
        self.update_pref_label.setCursor(Qt.PointingHandCursor)
        self.update_pref_label.setToolTip(
            "启动后静默查一次有没有新版本（不会自动下载、不会自动更新）。\n网络不通时什么都不会发生。")
        self.check_update_switch = SwitchButton(dark=self.is_dark, width=42, height=22)
        self.check_update_switch.setAccessibleName("启动时自动检查更新")
        self.check_update_switch.setToolTip(self.update_pref_label.toolTip())
        self.update_pref_label.clicked.connect(self.check_update_switch.toggle)
        self.check_update_switch.toggled.connect(
            lambda checked: self.settings.setValue("ui/check_update", checked))

        bottom.addWidget(self.update_pref_label)
        bottom.addWidget(self.check_update_switch)
        bottom.addSpacing(10)
        bottom.addWidget(self.start_btn)
        bottom.addWidget(self.stop_btn)
        bottom.addWidget(self.check_update_btn)
        bottom.addWidget(self.clear_btn)
        bottom.addWidget(self.help_btn)
        main_layout.addLayout(bottom)

    def set_theme(self, dark, animate=True):
        """主题开关的唯一入口：换样式表、同步三个开关的配色、落盘。

        点开关时先把旧主题「拍下来」盖在新界面上再淡出，整窗换色就不是硬切了；
        启动恢复主题走 animate=False，不闪。
        """
        dark = bool(dark)
        changed = dark != self.is_dark
        snapshot = None
        if animate and changed and self.isVisible():
            fresh = (self._pre_theme_shot is not None
                     and time.monotonic() - self._pre_theme_shot_at < 1.5)
            snapshot = self._pre_theme_shot if fresh else self.grab()
            self._pre_theme_shot = None
        self.is_dark = dark
        self.apply_stylesheet()
        for switch in (self.theme_switch, self.overwrite_switch, self.check_update_switch):
            switch.set_dark(self.is_dark)
        if snapshot is not None:
            self._fade_from_snapshot(snapshot)
        # 立刻落盘：就算程序被强制关掉，下次进来也是这个主题
        try:
            self.settings.setValue("ui/dark", self.is_dark)
            self.settings.sync()
        except Exception:
            pass

    def _snapshot_before_theme(self):
        """在开关状态还没翻转前拍一张当前窗口，供 set_theme 做淡出。"""
        try:
            self._pre_theme_shot = self.grab()
            self._pre_theme_shot_at = time.monotonic()
        except RuntimeError:
            self._pre_theme_shot = None

    def _fade_from_snapshot(self, snapshot, duration=360):
        """在窗口上盖一张旧主题截图，淡出后删掉，做出「整窗渐变」的观感。"""
        self._clear_fade_overlay()
        overlay = QLabel(self)
        overlay.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        overlay.setScaledContents(True)
        overlay.setPixmap(snapshot)
        overlay.setGeometry(self.rect())
        overlay.show()
        overlay.raise_()
        effect = QGraphicsOpacityEffect(overlay)
        overlay.setGraphicsEffect(effect)
        anim = QPropertyAnimation(effect, b"opacity", overlay)
        anim.setDuration(duration)
        anim.setStartValue(1.0)
        anim.setEndValue(0.0)
        anim.setEasingCurve(QEasingCurve.InOutCubic)
        anim.finished.connect(lambda: self._end_fade(overlay))
        anim.start(QAbstractAnimation.DeleteWhenStopped)
        self._fade_overlay = overlay
        self._fade_anim = anim

    def _end_fade(self, overlay):
        if self._fade_overlay is overlay:
            self._fade_overlay = None
            self._fade_anim = None
        overlay.deleteLater()

    def _clear_fade_overlay(self):
        """连点开关时把上一张还没淡完的截图直接撤掉，免得叠成两层。"""
        overlay = getattr(self, "_fade_overlay", None)
        if overlay is not None:
            try:
                overlay.hide()
                overlay.deleteLater()
            except RuntimeError:
                pass
        self._fade_overlay = None
        self._fade_anim = None

    def apply_stylesheet(self):
        if self.is_dark:
            self.setStyleSheet(DARK_STYLESHEET)
        else:
            self.setStyleSheet(LIGHT_STYLESHEET)

    # ---------- 支持作者 ----------
    def open_support(self):
        img_path = None
        for base in resource_dirs():
            for name in SUPPORT_IMG_NAMES:
                candidate = base / name
                if candidate.exists():
                    img_path = candidate
                    break
            if img_path:
                break

        dialog = QDialog(self)
        dialog.setWindowTitle("支持作者")
        dialog.setStyleSheet(SUPPORT_QSS_DARK if self.is_dark else SUPPORT_QSS_LIGHT)
        dialog.setWindowModality(Qt.ApplicationModal)

        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(26, 22, 26, 22)
        layout.setSpacing(6)

        title = QLabel("支持作者")
        title.setObjectName("supportTitle")
        title.setAlignment(Qt.AlignCenter)
        layout.addWidget(title)

        sub = QLabel("TuneFree 完全免费 · 没有广告 · 不锁功能")
        sub.setObjectName("supportSub")
        sub.setAlignment(Qt.AlignCenter)
        layout.addWidget(sub)

        layout.addSpacing(6)

        pixmap = QPixmap(str(img_path)) if img_path is not None else QPixmap()

        if img_path is not None and not pixmap.isNull():
            desc = QLabel("如果 TuneFree 确实帮到了您<br>您可以选择请作者喝杯奶茶 🧋")
            desc.setObjectName("supportDesc")
            desc.setAlignment(Qt.AlignCenter)
            desc.setWordWrap(True)
            layout.addWidget(desc)
            layout.addSpacing(8)

            card = QFrame()
            card.setObjectName("supportCard")
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(14, 14, 14, 14)

            # 按原图比例缩放到 300 以内：方形的赞赏码不会糊，双码长图也不会被拉成大方块
            pixmap = pixmap.scaled(300, 300, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            img_label = QLabel()
            img_label.setPixmap(pixmap)
            img_label.setAlignment(Qt.AlignCenter)
            img_label.setFixedSize(pixmap.size())
            card_layout.addWidget(img_label, alignment=Qt.AlignCenter)
            layout.addWidget(card)
            layout.addSpacing(10)

            hint_text = "扫码金额随意，都是一份心意 🙏"
        else:
            desc = QLabel("没有找到赞赏码图片（赞赏码.png），\n不过想支持作者还有别的办法。")
            desc.setObjectName("supportDesc")
            desc.setAlignment(Qt.AlignCenter)
            desc.setWordWrap(True)
            layout.addWidget(desc)
            layout.addSpacing(4)
            hint_text = "把 赞赏码.png 放到程序目录，下次就能直接扫码"

        hint = QLabel(hint_text)
        hint.setObjectName("supportHint")
        hint.setAlignment(Qt.AlignCenter)
        hint.setWordWrap(True)
        layout.addWidget(hint)

        layout.addSpacing(10)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(12)

        afdian_btn = QPushButton("☕ 爱发电")
        afdian_btn.setObjectName("afdianBtn")
        afdian_btn.setFixedHeight(38)
        afdian_btn.setCursor(Qt.PointingHandCursor)
        afdian_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(AFDIAN_URL)))

        close_btn = QPushButton("关闭")
        close_btn.setObjectName("doneCloseBtn")
        close_btn.setFixedHeight(38)
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.clicked.connect(dialog.accept)

        btn_row.addStretch()
        btn_row.addWidget(afdian_btn)
        btn_row.addWidget(close_btn)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        dialog.setFixedWidth(400)   # 宽度定住，高度跟着内容走
        dialog.adjustSize()
        dialog.exec()

    # ---------- 其他逻辑 ----------
    def browse_folder(self, line_edit):
        dir_path = QFileDialog.getExistingDirectory(self, "选择文件夹", line_edit.text())
        if dir_path:
            line_edit.setText(dir_path)
            if line_edit is self.input_edit:
                self.root_edit.setText(str(Path(dir_path) / "output"))
            self.save_settings()

    # ---------- 后端调用 ----------
    def backend_ready(self):
        """打包版不需要额外的 main.exe（用自己带 --cli 再起一个进程）；源码版回退到 python main.py。"""
        if getattr(sys, "frozen", False):
            return True
        return self.exe_path.exists() or self.dev_script.exists()

    def backend_prefix(self):
        is_frozen = getattr(sys, "frozen", False)
        if is_frozen:
            return [sys.executable, "--cli"]
        if self.exe_path.exists():
            return [str(self.exe_path)]
        return [sys.executable, str(self.dev_script)]

    # ---------- 完成提示 ----------
    def open_github(self):
        QDesktopServices.openUrl(QUrl(GITHUB_URL))

    def output_location(self):
        """这次运行实际写文件的位置，用于「打开输出目录」。"""
        raw = self.root_edit.text().strip() or str(app_dir() / "output")
        return Path(raw)

    def open_output_folder(self):
        folder = self.output_location()
        try:
            folder.mkdir(parents=True, exist_ok=True)
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))
        except Exception as e:
            self.log.append(f"(打开输出目录失败：{e})")

    def result_counts(self):
        """(成功数, 失败数)：转码过就以转码结果为准，只解密（仅 FLAC）时用解密结果。"""
        if self.transcode_stats:
            return self.transcode_stats
        if self.decrypt_stats:
            return self.decrypt_stats
        return (0, 0)

    def offer_star(self):
        """这次要不要给 Star 引导：整个安装只给一次，点过或忽略过就不再提。"""
        if self._star_prompted:
            return False
        self._star_prompted = True
        if self.settings.value("promo/star_prompted", False, type=bool):
            return False
        self.settings.setValue("promo/star_prompted", True)
        return True

    def log_file_path(self):
        return app_dir() / "TuneFree_日志.txt"

    def open_log_file(self):
        """打开这次运行的日志文件；还没生成就先落盘一份。"""
        path = self.log_file_path()
        if not path.exists():
            self.save_run_log()
        target = path if path.exists() else app_dir()
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))

    def show_completion_dialog(self, elapsed):
        """任务结束后的收尾提示：成功/失败/用时/输出四行明细 + 下一步动作。"""
        ok, bad = self.result_counts()
        folder = self.output_location()
        success = ok > 0

        dialog = QDialog(self)
        dialog.setWindowTitle("转换完成" if success else "没有转出文件")
        dialog.setStyleSheet(SUPPORT_QSS_DARK if self.is_dark else SUPPORT_QSS_LIGHT)
        dialog.setWindowModality(Qt.ApplicationModal)
        dialog.setFixedWidth(440)

        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(26, 24, 26, 20)
        layout.setSpacing(0)

        head = QHBoxLayout()
        head.setSpacing(11)
        badge = QLabel()
        badge.setPixmap(make_badge_pixmap(success, self.is_dark))
        badge.setFixedSize(34, 34)
        head.addWidget(badge)

        title = QLabel("转换完成" if success else "这次没有转出文件")
        title.setObjectName("doneTitle")
        head.addWidget(title)
        head.addStretch()
        layout.addLayout(head)
        layout.addSpacing(12)

        rows = (
            ("成功", f"{ok} 首", "doneVal"),
            ("失败", f"{bad} 首", "doneValBad" if bad else "doneVal"),
            ("用时", format_duration(elapsed), "doneVal"),
            ("输出", str(folder), "doneValMono"),
        )
        for key, value, obj in rows:
            row = QFrame()
            row.setObjectName("doneRow")
            row.setToolTip(value)
            h = QHBoxLayout(row)
            h.setContentsMargins(0, 10, 0, 10)
            h.setSpacing(12)

            k = QLabel(key)
            k.setObjectName("doneKey")
            k.setFixedWidth(52)
            h.addWidget(k)

            v = QLabel(value)
            v.setObjectName(obj)
            v.setTextInteractionFlags(Qt.TextSelectableByMouse)
            if obj == "doneValMono":
                v.setWordWrap(True)
            h.addWidget(v, 1)
            layout.addWidget(row)

        if not success:
            layout.addSpacing(14)
            layout.addWidget(self._build_trouble_tips())
        elif bad:
            layout.addSpacing(14)
            note = QLabel(f"有 {bad} 首没转出来 —— 多半是音质不支持（只认 标准 / HQ / SQ），"
                          f"也可能是文件没下载完整。剩下的都在上面的输出目录里。")
            note.setObjectName("doneNote")
            note.setWordWrap(True)
            layout.addWidget(note)

        layout.addSpacing(16)

        foot = QHBoxLayout()
        foot.setSpacing(12)

        if success:
            primary = QPushButton("打开输出目录")
            primary.clicked.connect(lambda: (dialog.accept(), self.open_output_folder()))
            primary.setToolTip(str(folder))
            link = QPushButton("查看日志")
            link.clicked.connect(lambda: (dialog.accept(), self.open_log_file()))
        else:
            primary = QPushButton("查看日志")
            primary.clicked.connect(lambda: (dialog.accept(), self.open_log_file()))
            link = QPushButton("打开帮助")
            link.clicked.connect(lambda: (dialog.accept(), self.show_help()))

        primary.setObjectName("afdianBtn")
        primary.setCursor(Qt.PointingHandCursor)
        primary.setDefault(True)
        foot.addWidget(primary)

        link.setObjectName("linkBtn")
        link.setCursor(Qt.PointingHandCursor)
        foot.addWidget(link)
        foot.addStretch()

        star_btn = None
        if success and self.offer_star():
            star_btn = QPushButton("去点 Star")
            star_btn.setObjectName("linkBtn")
            star_btn.setToolTip("在 GitHub 上给这个项目点个 Star，比什么都管用。")
            star_btn.setCursor(Qt.PointingHandCursor)
            foot.addWidget(star_btn)

        close_btn = QPushButton("关闭")
        close_btn.setObjectName("doneCloseBtn")
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.clicked.connect(dialog.reject)
        foot.addWidget(close_btn)
        layout.addLayout(foot)

        if star_btn is not None:
            star_btn.clicked.connect(lambda: (dialog.accept(), self.open_github()))

        dialog.adjustSize()
        dialog.exec()

    def _build_trouble_tips(self):
        """转不出文件时，别只写「失败」——给三条能照着做的排查步骤。"""
        tips = (
            ("音质不支持",
             "臻品音质 / 臻品母带 / 杜比全景声 解不出来，在 QQ 音乐里换成 标准 / HQ / SQ 再试。"),
            ("没注入进去",
             "完全退出 QQ 音乐再重新打开；还是不行就右键「以管理员身份运行」本程序"
             "（QQ 音乐是管理员身份时尤其要这样）。"),
            ("被杀软拦了",
             "把整个 TuneFree 文件夹加进杀软信任区；压缩包要先解压再运行，别在压缩包里直接点。"),
        )
        box = QFrame()
        box.setObjectName("troubleBox")
        v = QVBoxLayout(box)
        v.setContentsMargins(14, 12, 14, 12)
        v.setSpacing(10)

        head = QLabel("可能是这几个原因，按顺序试：")
        head.setObjectName("troubleHead")
        v.addWidget(head)

        for idx, (title, text) in enumerate(tips, 1):
            row = QHBoxLayout()
            row.setSpacing(9)
            num = QLabel(str(idx))
            num.setObjectName("troubleNum")
            num.setFixedSize(18, 18)
            num.setAlignment(Qt.AlignCenter)
            row.addWidget(num, 0, Qt.AlignTop)

            body = QLabel(f"<b>{title}</b>　{text}")
            body.setObjectName("troubleText")
            body.setWordWrap(True)
            body.setTextFormat(Qt.RichText)
            row.addWidget(body, 1)
            v.addLayout(row)
        return box

    def get_output_format(self):
        if self.format_mp3.isChecked():
            return "mp3"
        elif self.format_flac.isChecked():
            return "flac"
        else:
            return "both"

    def build_command(self):
        input_dir = self.input_edit.text().strip()
        if not input_dir:
            input_dir = str(app_dir())
        bitrate = self.bitrate_combo.currentText()
        output_format = self.get_output_format()

        cmd = self.backend_prefix() + ["-i", input_dir, "-b", bitrate]

        root = self.root_edit.text().strip()
        if not root:
            root = str(app_dir() / "output")
        cmd += ["-o", root]

        if output_format == "flac":
            cmd.append("--no-mp3")
        elif output_format == "both":
            cmd.append("--keep-flac")

        if self.overwrite_switch.isChecked():
            cmd.append("--force")

        return cmd

    def start_conversion(self):
        if not self.backend_ready():
            QMessageBox.warning(
                self, "错误",
                "未找到后端程序（main.exe 或源码版的 main.py），请确保它位于程序目录。")
            return
        if self.running:
            return

        input_dir = self.input_edit.text().strip() or str(app_dir())
        if not os.path.isdir(input_dir):
            QMessageBox.warning(self, "路径无效",
                                f"输入目录不存在：\n{input_dir}\n\n请重新选择包含 .mflac / .mgg 的文件夹。")
            return

        cmd = self.build_command()
        self.process = QProcess(self)
        self.process.setProgram(cmd[0])
        self.process.setArguments(cmd[1:])
        self.process.setWorkingDirectory(str(app_dir()))
        self.process.setProcessChannelMode(QProcess.MergedChannels)
        self.process.readyReadStandardOutput.connect(self.read_output)
        self.process.finished.connect(self.process_finished)

        self.decrypt_stats = None
        self.transcode_stats = None
        self._user_stopped = False
        self._buf = ""
        self._started_at = time.time()
        self.save_settings()  # 这次用的路径和选项，下次开程序直接还原

        self.running = True
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.progress.setMaximum(0)          # 0 = 不确定状态，先转起来
        self.progress.setFormat("准备中…")
        self.progress.setVisible(True)
        self.status_label.setText("运行中...")
        self.log.append("")
        self.log.append(">>> " + " ".join(cmd))
        self.process.start()

    def stop_conversion(self):
        if self.process and self.running:
            self._user_stopped = True
            self.process.kill()
            self.process.waitForFinished(2000)
            self.log.append("⚠️ 用户终止任务")
            self.status_label.setText("已停止")
            self.progress.setVisible(False)
            self.running = False
            self.start_btn.setEnabled(True)
            self.stop_btn.setEnabled(False)

    def read_output(self):
        data = self.process.readAllStandardOutput()
        self._buf += bytes(data.data()).decode('utf-8', errors='replace')
        # 管道读到的可能是半行，留到下次拼起来再处理
        *lines, self._buf = self._buf.split('\n')
        for line in lines:
            self.handle_line(line)

    def handle_line(self, line):
        line = line.strip()
        if not line:
            return
        if line.startswith("@@PROGRESS"):
            # 进度协议：只更新进度条，不污染日志
            parts = line.split()
            if len(parts) >= 3 and '/' in parts[2]:
                done, _, total = parts[2].partition('/')
                if done.isdigit() and total.isdigit():
                    self.update_progress(parts[1], int(done), int(total))
            return
        self.log.append(line)
        parsed = self.parse_decrypt_stats(line)
        if parsed:
            self.decrypt_stats = parsed
            return
        trans = self.parse_transcode_stats(line)
        if trans:
            self.transcode_stats = trans

    def update_progress(self, phase, done, total):
        if total <= 0:
            return
        self.progress.setMaximum(total)
        self.progress.setValue(done)
        self.status_label.setText(f"{phase}中… {done}/{total}")

    @staticmethod
    def parse_decrypt_stats(line):
        """从后端日志里抓出「解密阶段完成: 成功 N 个，失败 M 个」，用来判断这次是否真的有产出。"""
        m = re.search(r"解密阶段完成[:：]\s*成功\s*(\d+)\s*个[，,]\s*失败\s*(\d+)\s*个", line)
        if not m:
            return None
        return (int(m.group(1)), int(m.group(2)))

    @staticmethod
    def parse_transcode_stats(line):
        """抓「转码阶段完成: 成功 N 个，失败 M 个」，完成提示里要显示这个数。"""
        m = re.search(r"转码阶段完成[:：]\s*成功\s*(\d+)\s*个[，,]\s*失败\s*(\d+)\s*个", line)
        if not m:
            return None
        return (int(m.group(1)), int(m.group(2)))

    def process_finished(self, exit_code, exit_status):
        self.running = False
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.progress.setVisible(False)

        elapsed = (time.time() - self._started_at) if self._started_at else 0.0
        self._started_at = None

        if self.process is not None:
            self.process.deleteLater()

        if self._user_stopped:
            # 用户自己点的停止，别再来一句"执行失败"吓人
            self.status_label.setText("已停止")
        elif exit_code == 0:
            ok, bad = self.result_counts()
            if ok > 0:
                self.status_label.setText(f"完成：成功 {ok} 首 · {format_duration(elapsed)}")
                self.log.append(f"✅ 完成：成功 {ok} 首，失败 {bad} 首，用时 {format_duration(elapsed)}")
            else:
                self.status_label.setText(f"没有转出文件 · {format_duration(elapsed)}")
                self.log.append(f"⚠️ 没有转出文件，用时 {format_duration(elapsed)}")
            # 先把日志落盘，弹窗里的「查看日志」才能立刻打开这一轮的记录
            self.save_run_log()
            self.show_completion_dialog(elapsed)
        else:
            self.status_label.setText(f"异常退出 (代码 {exit_code})")
            self.log.append(f"❌ 执行失败，返回码：{exit_code}")
            hint = explain_exit_code(exit_code)
            if hint:
                self.log.append(f"💡 {hint}")
            self.save_error_log()
            self.save_run_log()

    def save_run_log(self):
        """每次运行结束都把完整日志落盘，出问题直接把文件发给作者。"""
        try:
            text = self.log.toPlainText()
            if text:
                (app_dir() / "TuneFree_日志.txt").write_text(text, encoding="utf-8")
        except Exception:
            pass

    def save_error_log(self):
        """失败时把完整日志落盘，方便排查/反馈。"""
        try:
            path = app_dir() / "TuneFree_错误日志.txt"
            path.write_text(self.log.toPlainText(), encoding="utf-8")
            self.log.append(f"📄 完整日志已保存：{path}")
        except Exception as e:
            self.log.append(f"(日志保存失败: {e})")

    # ---------- 更新检查 ----------
    def auto_check_update(self):
        """启动时静默查一次，没有新版就当没发生过。"""
        if not self._saved_bool("ui/check_update", True):
            return
        self._start_update_check(manual=False)

    def manual_check_update(self):
        self._start_update_check(manual=True)

    def _start_update_check(self, manual=False):
        if self._checking_update:
            return
        self._checking_update = True
        self._manual_update = manual
        self.check_update_btn.setEnabled(False)
        if manual:
            self.status_label.setText("正在检查更新…")
        self.checker.check(4.0)

    def on_update_found(self, version, url, notes):
        self._checking_update = False
        self.check_update_btn.setEnabled(True)
        self._latest_version = version
        self._latest_url = DOWNLOAD_URL or url or GITHUB_LATEST_PAGE

        is_newer = parse_version(version) > parse_version(VERSION)
        if self._manual_update and not is_newer:
            self.status_label.setText("已是最新版本")
            QMessageBox.information(self, "检查更新", f"当前已是最新版本 {VERSION}。")
            return

        skipped = str(self.settings.value("update/skipped", "") or "")
        if not self._manual_update and (not is_newer or skipped == version):
            return  # 不是新版，或者用户点过"忽略此版本"，别打扰

        first_line = ""
        for line in (notes or "").splitlines():
            line = line.strip().lstrip("#").strip()
            if line and not line.startswith("-"):
                first_line = line
                break
        text = f"🎉 发现新版本 v{version}（当前 {VERSION}）"
        if first_line:
            text += f" · {first_line}"
        self.update_text.setText(text)
        self.update_banner.setVisible(True)
        self.status_label.setText(f"发现新版本 v{version}")

    def on_update_failed(self, reason):
        self._checking_update = False
        self.check_update_btn.setEnabled(True)
        if self._manual_update:
            self.status_label.setText("检查更新失败")
            QMessageBox.information(
                self, "检查更新",
                "暂时连不上更新服务器。\n国内网络直连 GitHub 经常超时，这不影响正常使用。\n"
                f"当前版本：{VERSION}")

    def open_update_page(self):
        QDesktopServices.openUrl(QUrl(self._latest_url or GITHUB_LATEST_PAGE))

    def ignore_this_version(self):
        if self._latest_version:
            self.settings.setValue("update/skipped", self._latest_version)
            try:
                self.settings.sync()
            except Exception:
                pass
        self.update_banner.setVisible(False)
        self.status_label.setText("已忽略该版本")

    def closeEvent(self, event):
        self.settings.setValue("ui/geometry", self.saveGeometry())
        self.settings.setValue("ui/dark", self.is_dark)
        self.save_settings()
        super().closeEvent(event)

    def clear_log(self):
        self.log.clear()
        self._buf = ""

    def show_help(self):
        """优先打开官网的帮助区（要求网址带 #help），打不开再退回随程序附带的离线说明。"""
        if QDesktopServices.openUrl(QUrl(HELP_URL)):
            return
        for base in resource_dirs():
            help_file = base / "使用说明.html"
            if help_file.exists():
                webbrowser.open(str(help_file))
                return
        QMessageBox.information(self, "帮助", f"请在浏览器中打开：\n{SITE_PRIMARY}")


# -------------------- 启动 --------------------
def _set_taskbar_identity():
    """Windows 任务栏按 AppUserModelID 归组；设了它，任务栏图标才会用程序自己的图标。"""
    if os.name != "nt":
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("zksbx.TuneFree")
    except Exception:
        pass


def run_gui(argv=None):
    """图形界面入口；打包后由 app.py 调用（同一个 exe 既能开界面也能跑命令行）。"""
    _set_taskbar_identity()
    app = QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName("TuneFree")
    # 空 displayName：Windows 标题栏就不会再拼成 “TuneFree-2.1.2 - TuneFree”
    app.setApplicationDisplayName("")
    icon = app_icon()
    if icon is not None:
        app.setWindowIcon(icon)   # 应用级图标：主窗口、弹窗、任务栏都跟着用
    window = TuneFreeWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(run_gui())
