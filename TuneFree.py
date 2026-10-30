import sys
import os
import re
import time
import json
import threading
import urllib.request
from pathlib import Path
from PySide6.QtCore import *
from PySide6.QtWidgets import *
from PySide6.QtGui import *


# -------------------- 项目信息 --------------------
try:  # 版本号统一放在 version.py，老版本打包里没有这个文件就退回内置值
    from version import (
        VERSION, GITHUB_URL, GITHUB_RELEASES_URL, GITHUB_LATEST_API,
        GITHUB_LATEST_PAGE, VERSION_JSON_URLS, QQ_GROUP, DOWNLOAD_URL,
        SITE_URL, SITE_PRIMARY,
    )
except Exception:  # pragma: no cover
    VERSION = "2.1.2"
    DOWNLOAD_URL = ""
    SITE_URL = "https://github.com/zksbx/TuneFree"
    SITE_PRIMARY = SITE_URL
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


# -------------------- 工具函数 --------------------
def app_dir():
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).parent
    return Path(__file__).parent


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
        background-color: #f5f7fa;
        color: #1e293b;
        font-family: "Segoe UI", "Microsoft YaHei", sans-serif;
        font-size: 10pt;
    }
    #titleLabel {
        font-size: 22pt;
        font-weight: bold;
        color: #0f172a;
        margin-bottom: 8px;
    }
    #label {
        color: #334155;
        padding-right: 8px;
        min-width: 60px;
    }
    #inputField {
        background-color: #ffffff;
        border: 1px solid #cbd5e1;
        border-radius: 8px;
        padding: 6px 10px;
        color: #1e293b;
    }
    #inputField:focus {
        border-color: #3b82f6;
        outline: none;
    }
    #browseBtn {
        background-color: #e2e8f0;
        border: none;
        border-radius: 8px;
        padding: 6px 16px;
        color: #1e293b;
    }
    #browseBtn:hover {
        background-color: #cbd5e1;
    }
    #toggleCheck {
        color: #1e293b;
        spacing: 8px;
    }
    #toggleCheck::indicator {
        width: 16px;
        height: 16px;
        border-radius: 4px;
        background-color: #ffffff;
        border: 1px solid #cbd5e1;
    }
    #toggleCheck::indicator:checked {
        background-color: #3b82f6;
        border-color: #3b82f6;
    }
    #combo {
        background-color: #ffffff;
        border: 1px solid #cbd5e1;
        border-radius: 8px;
        padding: 4px 8px;
        color: #1e293b;
        min-width: 70px;
    }
    #combo::drop-down {
        border: none;
    }
    #combo QAbstractItemView {
        background-color: #ffffff;
        color: #1e293b;
        selection-background-color: #3b82f6;
        selection-color: #ffffff;
    }
    #formatRadio {
        color: #1e293b;
        spacing: 6px;
        margin-right: 12px;
    }
    #formatRadio::indicator {
        width: 14px;
        height: 14px;
        border-radius: 7px;
        background-color: #ffffff;
        border: 1px solid #cbd5e1;
    }
    #formatRadio::indicator:checked {
        background-color: #3b82f6;
        border-color: #3b82f6;
    }
    #preview, #log {
        background-color: #ffffff;
        border: 1px solid #e2e8f0;
        border-radius: 12px;
        padding: 10px;
        color: #1e293b;
    }
    #preview {
        background-color: #f8fafc;
    }
    #log {
        background-color: #f1f5f9;
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
        background-color: #94a3b8;
        color: #e2e8f0;
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
        background-color: #94a3b8;
        color: #e2e8f0;
    }
    #clearBtn, #helpBtn, #themeBtn, #togglePreviewBtn, #supportBtn {
        background-color: #e2e8f0;
        border: none;
        border-radius: 8px;
        padding: 8px 16px;
        color: #1e293b;
    }
    #clearBtn:hover, #helpBtn:hover, #themeBtn:hover, #togglePreviewBtn:hover, #supportBtn:hover {
        background-color: #cbd5e1;
    }
    #status {
        color: #475569;
    }
    #progress {
        border: 1px solid #cbd5e1;
        border-radius: 8px;
        background-color: #e2e8f0;
        text-align: center;
        font-size: 8pt;
        color: #1e293b;
    }
    #progress::chunk {
        background-color: #3b82f6;
        border-radius: 7px;
    }
    #tipLabel {
        color: #475569;
        background-color: #f1f5f9;
        border-radius: 8px;
        padding: 8px 12px;
        font-size: 9pt;
    }
    QScrollBar:vertical {
        background: #f1f5f9;
        width: 10px;
        border-radius: 5px;
    }
    QScrollBar::handle:vertical {
        background: #cbd5e1;
        border-radius: 5px;
        min-height: 20px;
    }
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
        height: 0;
    }
    QMessageBox {
        background-color: #ffffff;
    }
    QMessageBox QPushButton {
        background-color: #e2e8f0;
        border: none;
        border-radius: 6px;
        padding: 6px 16px;
        color: #1e293b;
    }
    QMessageBox QPushButton:hover {
        background-color: #cbd5e1;
    }
    #starBtn {
        background-color: #fef3c7;
        border: 1px solid #f59e0b;
        border-radius: 8px;
        padding: 8px 16px;
        color: #92400e;
        font-weight: bold;
    }
    #starBtn:hover {
        background-color: #fde68a;
    }
    #updateBanner {
        background-color: #fffbeb;
        border: 1px solid #fcd34d;
        border-radius: 8px;
    }
    #updateText {
        color: #92400e;
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
        color: #92400e;
        border: none;
        padding: 5px 8px;
    }
    #updateIgnoreBtn:hover {
        color: #78350f;
        text-decoration: underline;
    }
"""

DARK_STYLESHEET = """
    QWidget {
        background-color: #1e1e2e;
        color: #cdd6f4;
        font-family: "Segoe UI", "Microsoft YaHei", sans-serif;
        font-size: 10pt;
    }
    #titleLabel {
        font-size: 22pt;
        font-weight: bold;
        color: #89b4fa;
        margin-bottom: 8px;
    }
    #label {
        color: #a6adc8;
        padding-right: 8px;
        min-width: 60px;
    }
    #inputField {
        background-color: #313244;
        border: 1px solid #45475a;
        border-radius: 8px;
        padding: 6px 10px;
        color: #cdd6f4;
    }
    #inputField:focus {
        border-color: #89b4fa;
    }
    #browseBtn {
        background-color: #45475a;
        border: none;
        border-radius: 8px;
        padding: 6px 16px;
        color: #cdd6f4;
    }
    #browseBtn:hover {
        background-color: #585b70;
    }
    #toggleCheck {
        color: #cdd6f4;
        spacing: 8px;
    }
    #toggleCheck::indicator {
        width: 16px;
        height: 16px;
        border-radius: 4px;
        background-color: #313244;
        border: 1px solid #45475a;
    }
    #toggleCheck::indicator:checked {
        background-color: #89b4fa;
        border-color: #89b4fa;
    }
    #combo {
        background-color: #313244;
        border: 1px solid #45475a;
        border-radius: 8px;
        padding: 4px 8px;
        color: #cdd6f4;
        min-width: 70px;
    }
    #combo::drop-down {
        border: none;
    }
    #combo QAbstractItemView {
        background-color: #313244;
        color: #cdd6f4;
        selection-background-color: #89b4fa;
    }
    #formatRadio {
        color: #cdd6f4;
        spacing: 6px;
        margin-right: 12px;
    }
    #formatRadio::indicator {
        width: 14px;
        height: 14px;
        border-radius: 7px;
        background-color: #313244;
        border: 1px solid #45475a;
    }
    #formatRadio::indicator:checked {
        background-color: #89b4fa;
        border-color: #89b4fa;
    }
    #preview, #log {
        background-color: #1e1e2e;
        border: 1px solid #313244;
        border-radius: 12px;
        padding: 10px;
        color: #cdd6f4;
    }
    #preview {
        background-color: #11111b;
        border-color: #45475a;
    }
    #log {
        background-color: #0f0f1a;
    }
    #startBtn {
        background-color: #a6e3a1;
        color: #1e1e2e;
        border: none;
        border-radius: 8px;
        padding: 8px 24px;
        font-weight: bold;
    }
    #startBtn:hover {
        background-color: #94d78f;
    }
    #startBtn:disabled {
        background-color: #45475a;
        color: #6c7086;
    }
    #stopBtn {
        background-color: #f28b82;
        color: #1e1e2e;
        border: none;
        border-radius: 8px;
        padding: 8px 24px;
        font-weight: bold;
    }
    #stopBtn:hover {
        background-color: #e06c6c;
    }
    #stopBtn:disabled {
        background-color: #45475a;
        color: #6c7086;
    }
    #clearBtn, #helpBtn, #themeBtn, #togglePreviewBtn, #supportBtn {
        background-color: #45475a;
        border: none;
        border-radius: 8px;
        padding: 8px 16px;
        color: #cdd6f4;
    }
    #clearBtn:hover, #helpBtn:hover, #themeBtn:hover, #togglePreviewBtn:hover, #supportBtn:hover {
        background-color: #585b70;
    }
    #status {
        color: #a6adc8;
    }
    #progress {
        border: 1px solid #45475a;
        border-radius: 8px;
        background-color: #313244;
        text-align: center;
        font-size: 8pt;
        color: #cdd6f4;
    }
    #progress::chunk {
        background-color: #89b4fa;
        border-radius: 7px;
    }
    #tipLabel {
        color: #a6adc8;
        background-color: #2a2a3e;
        border-radius: 8px;
        padding: 8px 12px;
        font-size: 9pt;
    }
    QScrollBar:vertical {
        background: #1e1e2e;
        width: 10px;
        border-radius: 5px;
    }
    QScrollBar::handle:vertical {
        background: #45475a;
        border-radius: 5px;
        min-height: 20px;
    }
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
        height: 0;
    }
    QMessageBox {
        background-color: #1e1e2e;
    }
    QMessageBox QPushButton {
        background-color: #45475a;
        border: none;
        border-radius: 6px;
        padding: 6px 16px;
        color: #cdd6f4;
    }
    QMessageBox QPushButton:hover {
        background-color: #585b70;
    }
    #starBtn {
        background-color: #3a2f1b;
        border: 1px solid #f9e2af;
        border-radius: 8px;
        padding: 8px 16px;
        color: #f9e2af;
        font-weight: bold;
    }
    #starBtn:hover {
        background-color: #4d3f24;
    }
    #updateBanner {
        background-color: #3b3320;
        border: 1px solid #7c6320;
        border-radius: 8px;
    }
    #updateText {
        color: #f9e2af;
        background: transparent;
    }
    #updateLinkBtn {
        background-color: #f9e2af;
        color: #1e1e2e;
        border: none;
        border-radius: 6px;
        padding: 5px 14px;
        font-weight: bold;
    }
    #updateLinkBtn:hover {
        background-color: #f5d98b;
    }
    #updateIgnoreBtn {
        background: transparent;
        color: #c9b98a;
        border: none;
        padding: 5px 8px;
    }
    #updateIgnoreBtn:hover {
        color: #f9e2af;
        text-decoration: underline;
    }
"""


# -------------------- 主窗口 --------------------
class TuneFreeWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"TuneFree {VERSION}")
        self.setMinimumSize(950, 780)

        # 后端命令行程序：打包后是 main.exe；源码直接运行时回退到 python main.py
        self.exe_path = app_dir() / "main.exe"
        self.dev_script = app_dir() / "main.py"
        self.process = None
        self.running = False
        self.settings, self._settings_path = make_settings()
        self.is_dark = self._saved_bool("ui/dark", False)  # 首次运行默认亮色
        self.decrypt_stats = None  # (成功数, 失败数)，用于判断这次是否真的转出了文件
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
        self.theme_btn.setText("☀️" if self.is_dark else "🌙")

        saved_geo = self.settings.value("ui/geometry")
        if saved_geo is not None:
            self.restoreGeometry(saved_geo)

        self.restore_settings()
        self.update_preview()

        icon_file = app_dir() / "tunefree.ico"
        if icon_file.exists():
            self.setWindowIcon(QIcon(str(icon_file)))

        # 启动 3 秒后再查更新：别和界面初始化、QQ音乐注入抢时间
        QTimer.singleShot(3000, self.auto_check_update)

        if not self.backend_ready():
            QMessageBox.warning(
                self, "提示",
                f"未找到 main.exe（或源码版的 main.py），请确保它与本程序在同一目录。\n当前目录：{app_dir()}")

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
        self.mp3_edit.setText(s.value("path/mp3", str(app_dir() / "output" / "mp3")))
        self.flac_edit.setText(s.value("path/flac", str(app_dir() / "output" / "flac")))

        bitrate = s.value("out/bitrate", "192k")
        if self.bitrate_combo.findText(bitrate) < 0:
            bitrate = "192k"
        self.bitrate_combo.setCurrentText(bitrate)
        fmt = s.value("out/format", "mp3")
        {"flac": self.format_flac, "both": self.format_both}.get(fmt, self.format_mp3).setChecked(True)
        self.independent_check.setChecked(self._saved_bool("out/independent", False))
        self.overwrite_check.setChecked(self._saved_bool("out/force", False))
        self.check_update_check.setChecked(self._saved_bool("ui/check_update", True))

    def save_settings(self):
        s = self.settings
        try:
            s.setValue("path/input", self.input_edit.text().strip())
            s.setValue("path/root", self.root_edit.text().strip())
            s.setValue("path/mp3", self.mp3_edit.text().strip())
            s.setValue("path/flac", self.flac_edit.text().strip())
            s.setValue("out/bitrate", self.bitrate_combo.currentText())
            s.setValue("out/format", self.get_output_format())
            s.setValue("out/independent", self.independent_check.isChecked())
            s.setValue("out/force", self.overwrite_check.isChecked())
            s.setValue("ui/check_update", self.check_update_check.isChecked())
            s.sync()
        except Exception:
            pass

    def setup_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(20, 20, 20, 20)
        main_layout.setSpacing(12)

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
        
        self.theme_btn = QPushButton("🌙")
        self.theme_btn.setObjectName("themeBtn")
        self.theme_btn.clicked.connect(self.toggle_theme)
        title_layout.addWidget(self.theme_btn)
        
        main_layout.addLayout(title_layout)

        # 输入目录
        row1 = QHBoxLayout()
        label_input = QLabel("输入目录")
        label_input.setObjectName("label")
        self.input_edit = QLineEdit()
        self.input_edit.setObjectName("inputField")
        self.input_edit.textChanged.connect(self.update_preview)
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

        # 第二行
        row2 = QHBoxLayout()
        self.independent_check = QCheckBox("使用独立输出目录")
        self.independent_check.setObjectName("toggleCheck")
        self.independent_check.stateChanged.connect(lambda: self.toggle_output_mode())
        self.independent_check.setToolTip("勾选后，MP3 和 FLAC 可分别输出到两个不同的文件夹。")
        row2.addWidget(self.independent_check)
        row2.addSpacing(20)

        label_bitrate = QLabel("比特率")
        label_bitrate.setObjectName("label")
        self.bitrate_combo = QComboBox()
        self.bitrate_combo.addItems(["128k", "160k", "192k", "224k", "256k", "320k"])
        self.bitrate_combo.setCurrentText("192k")
        self.bitrate_combo.setObjectName("combo")
        self.bitrate_combo.currentTextChanged.connect(self.update_preview)
        self.bitrate_combo.setToolTip("数值越高，MP3 音质越好，文件越大。\n推荐 192k（平衡）或 320k（高品质）。")
        row2.addWidget(label_bitrate)
        row2.addWidget(self.bitrate_combo)
        row2.addSpacing(20)

        label_format = QLabel("输出格式")
        label_format.setObjectName("label")
        row2.addWidget(label_format)
        self.format_group = QButtonGroup(self)
        self.format_mp3 = QRadioButton("仅 MP3")
        self.format_flac = QRadioButton("仅 FLAC")
        self.format_both = QRadioButton("两者均保留")
        for btn in [self.format_mp3, self.format_flac, self.format_both]:
            btn.setObjectName("formatRadio")
            btn.toggled.connect(self.update_preview)
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

        # 输出目录区域
        self.output_stack = QStackedWidget()
        self.output_stack.setObjectName("outputStack")

        # 根目录模式
        page_root = QWidget()
        root_layout = QHBoxLayout(page_root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        label_root = QLabel("根目录")
        label_root.setObjectName("label")
        self.root_edit = QLineEdit()
        self.root_edit.setObjectName("inputField")
        self.root_edit.textChanged.connect(self.update_preview)
        self.root_edit.editingFinished.connect(self.save_settings)
        self.root_edit.setToolTip("输出 MP3 和 FLAC 的总文件夹，程序会自动在其下创建 mp3 和 flac 子文件夹。")
        browse_root = QPushButton("浏览")
        browse_root.setObjectName("browseBtn")
        browse_root.clicked.connect(lambda: self.browse_folder(self.root_edit))
        root_layout.addWidget(label_root)
        root_layout.addWidget(self.root_edit, 1)
        root_layout.addWidget(browse_root)
        self.output_stack.addWidget(page_root)

        # 独立目录模式
        page_ind = QWidget()
        ind_layout = QVBoxLayout(page_ind)
        ind_layout.setContentsMargins(0, 0, 0, 0)
        ind_layout.setSpacing(6)
        mp3_row = QHBoxLayout()
        label_mp3 = QLabel("MP3 目录")
        label_mp3.setObjectName("label")
        self.mp3_edit = QLineEdit()
        self.mp3_edit.setObjectName("inputField")
        self.mp3_edit.textChanged.connect(self.update_preview)
        self.mp3_edit.editingFinished.connect(self.save_settings)
        self.mp3_edit.setToolTip("存放最终 MP3 文件的文件夹。")
        browse_mp3 = QPushButton("浏览")
        browse_mp3.setObjectName("browseBtn")
        browse_mp3.clicked.connect(lambda: self.browse_folder(self.mp3_edit))
        mp3_row.addWidget(label_mp3)
        mp3_row.addWidget(self.mp3_edit, 1)
        mp3_row.addWidget(browse_mp3)
        ind_layout.addLayout(mp3_row)
        flac_row = QHBoxLayout()
        label_flac = QLabel("FLAC 目录")
        label_flac.setObjectName("label")
        self.flac_edit = QLineEdit()
        self.flac_edit.setObjectName("inputField")
        self.flac_edit.textChanged.connect(self.update_preview)
        self.flac_edit.editingFinished.connect(self.save_settings)
        self.flac_edit.setToolTip("存放解密后 FLAC 文件的文件夹（仅当选择“两者均保留”或“仅 FLAC”时有效）。")
        browse_flac = QPushButton("浏览")
        browse_flac.setObjectName("browseBtn")
        browse_flac.clicked.connect(lambda: self.browse_folder(self.flac_edit))
        flac_row.addWidget(label_flac)
        flac_row.addWidget(self.flac_edit, 1)
        flac_row.addWidget(browse_flac)
        ind_layout.addLayout(flac_row)
        self.output_stack.addWidget(page_ind)

        self.output_stack.setCurrentIndex(0)
        main_layout.addWidget(self.output_stack)

        # ---------- 预览切换按钮 ----------
        preview_toggle_layout = QHBoxLayout()
        self.toggle_preview_btn = QPushButton("▶ 显示命令预览")
        self.toggle_preview_btn.setObjectName("togglePreviewBtn")
        self.toggle_preview_btn.clicked.connect(self.toggle_preview_visibility)
        preview_toggle_layout.addWidget(self.toggle_preview_btn)

        self.overwrite_check = QCheckBox("重新生成已存在的 MP3")
        self.overwrite_check.setObjectName("toggleCheck")
        self.overwrite_check.setToolTip(
            "默认跳过已经生成过的 MP3，同一批歌再跑一次会快很多。\n勾选后即使 MP3 已存在也会重新转码（改了比特率时用得上）。")
        self.overwrite_check.stateChanged.connect(self.update_preview)
        preview_toggle_layout.addSpacing(16)
        preview_toggle_layout.addWidget(self.overwrite_check)

        self.check_update_check = QCheckBox("启动时检查更新")
        self.check_update_check.setObjectName("toggleCheck")
        self.check_update_check.setToolTip(
            "启动后静默查一次有没有新版本（不会自动下载、不会自动更新）。\n网络不通时什么都不会发生。")
        self.check_update_check.stateChanged.connect(lambda: self.settings.setValue(
            "ui/check_update", self.check_update_check.isChecked()))
        preview_toggle_layout.addSpacing(16)
        preview_toggle_layout.addWidget(self.check_update_check)

        preview_toggle_layout.addStretch()
        main_layout.addLayout(preview_toggle_layout)

        # 预览（默认隐藏）
        self.preview = QTextEdit()
        self.preview.setObjectName("preview")
        self.preview.setReadOnly(True)
        self.preview.setFixedHeight(80)
        self.preview.setFont(QFont("Consolas", 9))
        self.preview.setToolTip("这里是即将执行的命令行预览，你可以核对参数是否正确。")
        self.preview.setVisible(False)
        main_layout.addWidget(self.preview)

        # 日志
        self.log = QTextEdit()
        self.log.setObjectName("log")
        self.log.setReadOnly(True)
        self.log.setFont(QFont("Consolas", 9))
        self.log.setToolTip("程序运行过程中的详细日志，包括解密进度、转码状态和错误信息。")
        main_layout.addWidget(self.log)

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
        self.progress.setFixedWidth(190)
        self.progress.setFixedHeight(16)
        self.progress.setTextVisible(True)
        self.progress.setVisible(False)
        bottom.addWidget(self.status_label)
        bottom.addWidget(self.progress)
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

        bottom.addWidget(self.status_label)
        bottom.addStretch()
        bottom.addWidget(self.start_btn)
        bottom.addWidget(self.stop_btn)
        bottom.addWidget(self.check_update_btn)
        bottom.addWidget(self.clear_btn)
        bottom.addWidget(self.help_btn)
        main_layout.addLayout(bottom)
    def toggle_output_mode(self):
        if self.independent_check.isChecked():
            self.output_stack.setCurrentIndex(1)
        else:
            self.output_stack.setCurrentIndex(0)

    def toggle_theme(self):
        self.is_dark = not self.is_dark
        self.apply_stylesheet()
        if self.is_dark:
            self.theme_btn.setText("☀️")
        else:
            self.theme_btn.setText("🌙")
        # 立刻落盘：就算程序被强制关掉，下次进来也是这个主题
        try:
            self.settings.setValue("ui/dark", self.is_dark)
            self.settings.sync()
        except Exception:
            pass

    def apply_stylesheet(self):
        if self.is_dark:
            self.setStyleSheet(DARK_STYLESHEET)
        else:
            self.setStyleSheet(LIGHT_STYLESHEET)

    # ---------- 预览显隐切换 ----------
    def toggle_preview_visibility(self):
        if self.preview.isVisible():
            self.preview.setVisible(False)
            self.toggle_preview_btn.setText("▶ 显示命令预览")
        else:
            self.preview.setVisible(True)
            self.toggle_preview_btn.setText("▼ 隐藏命令预览")

    # ---------- 支持作者 ----------
    def open_support(self):
        img_path = app_dir() / "赞赏码.png"
        if not img_path.exists():
            QMessageBox.information(self, "提示", "未找到赞赏码图片，请将 '赞赏码.png' 放在程序目录下。")
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("支持作者")
        dialog.setFixedSize(420, 520)
        layout = QVBoxLayout(dialog)

        label = QLabel("如果你觉得 TuneFree 对你有帮助，欢迎扫码赞赏！")
        label.setAlignment(Qt.AlignCenter)
        layout.addWidget(label)

        pixmap = QPixmap(str(img_path))
        pixmap = pixmap.scaled(350, 350, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        img_label = QLabel()
        img_label.setPixmap(pixmap)
        img_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(img_label)

        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(dialog.accept)
        layout.addWidget(close_btn, alignment=Qt.AlignCenter)
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
        """打包版用 main.exe，源码版回退到 python main.py，方便开发调试。"""
        return self.exe_path.exists() or self.dev_script.exists()

    def backend_prefix(self):
        if self.exe_path.exists():
            return [str(self.exe_path)]
        return [sys.executable, str(self.dev_script)]

    # ---------- Star 引导 ----------
    def open_github(self):
        QDesktopServices.openUrl(QUrl(GITHUB_URL))

    def maybe_show_star_dialog(self):
        """转换成功后引导一次 Star。整个生命周期只弹一次，避免打扰。"""
        if self._star_prompted:
            return
        self._star_prompted = True
        if self.settings.value("promo/star_prompted", False, type=bool):
            return
        self.settings.setValue("promo/star_prompted", True)

        box = QMessageBox(self)
        box.setWindowTitle("转换完成")
        box.setIcon(QMessageBox.Information)
        box.setText("✅ 全部转换完成")
        box.setInformativeText(
            "TuneFree 是免费开源的，没有广告也没有捆绑。\n"
            "如果它帮你省了时间，去 GitHub 点个 ⭐ Star，能让更多人找到这个项目。"
        )
        star_btn = box.addButton("⭐ 去点 Star", QMessageBox.AcceptRole)
        copy_btn = box.addButton("🔗 复制链接", QMessageBox.ActionRole)
        box.addButton("关闭", QMessageBox.RejectRole)
        box.exec()

        if box.clickedButton() is star_btn:
            self.open_github()
        elif box.clickedButton() is copy_btn:
            QGuiApplication.clipboard().setText(GITHUB_URL)
            self.log.append(f"已复制项目链接：{GITHUB_URL}")

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

        if self.independent_check.isChecked():
            mp3_dir = self.mp3_edit.text().strip()
            flac_dir = self.flac_edit.text().strip()
            if not mp3_dir:
                mp3_dir = str(app_dir() / "output" / "mp3")
            if not flac_dir:
                flac_dir = str(app_dir() / "output" / "flac")
            cmd += ["--mp3-dir", mp3_dir, "--flac-dir", flac_dir]
        else:
            root = self.root_edit.text().strip()
            if not root:
                root = str(app_dir() / "output")
            cmd += ["-o", root]

        if output_format == "flac":
            cmd.append("--no-mp3")
        elif output_format == "both":
            cmd.append("--keep-flac")

        if self.overwrite_check.isChecked():
            cmd.append("--force")

        return cmd

    def update_preview(self):
        # setup_ui 里 setChecked(True) 会提前触发 toggled，此时输出目录控件还没建好
        if not hasattr(self, "root_edit") or not hasattr(self, "mp3_edit"):
            return
        cmd = self.build_command()
        self.preview.setText(" ".join(cmd))

    def start_conversion(self):
        if not self.backend_ready():
            QMessageBox.warning(
                self, "错误",
                "未找到 main.exe（或源码版的 main.py），请确保它位于程序目录。")
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

    def update_progress(self, phase, done, total):
        if total <= 0:
            return
        self.progress.setMaximum(total)
        self.progress.setValue(done)
        self.progress.setFormat(f"{phase} %v/%m")
        self.status_label.setText(f"{phase}中… {done}/{total}")

    @staticmethod
    def parse_decrypt_stats(line):
        """从后端日志里抓出「解密阶段完成: 成功 N 个，失败 M 个」，用来判断这次是否真的有产出。"""
        m = re.search(r"解密阶段完成[:：]\s*成功\s*(\d+)\s*个[，,]\s*失败\s*(\d+)\s*个", line)
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
            self.status_label.setText(f"完成（用时 {elapsed:.1f} 秒）")
            self.log.append(f"✅ 执行完成，用时 {elapsed:.1f} 秒")
            if self.decrypt_stats and self.decrypt_stats[0] > 0:
                self.maybe_show_star_dialog()
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
        """优先打开官网的帮助页，打不开再退回随程序附带的离线说明。"""
        if QDesktopServices.openUrl(QUrl(SITE_PRIMARY.rstrip("/") + "/#help")):
            return
        help_file = app_dir() / "使用说明.html"
        if help_file.exists():
            import webbrowser
            webbrowser.open(str(help_file))
            return
        QMessageBox.information(self, "帮助", f"请在浏览器中打开：\n{SITE_PRIMARY}")


# -------------------- 启动 --------------------
if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = TuneFreeWindow()
    window.show()
    sys.exit(app.exec())