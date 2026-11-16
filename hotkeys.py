# -*- coding: utf-8 -*-
"""全局媒体键：Windows 低级键盘钩子（WH_KEYBOARD_LL）。

只旁听媒体键、不吞按键，别的播放器照样能收到。钩子回调必须尽快返回，
所以回调里只把键名排进 Qt 事件队列，真正的处理交给主线程的事件循环。

用法：
    hook = MediaKeyHook(parent)
    hook.mediaKey.connect(on_media_key)
    hook.install()          # 返回 False 时看 hook.error()
    hook.uninstall()
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import logging
import os

from PySide6.QtCore import QObject, QTimer, Signal

WH_KEYBOARD_LL = 13
WM_KEYDOWN = 0x0100
WM_SYSKEYDOWN = 0x0104
LLKHF_INJECTED = 0x10

VK_MEDIA_NEXT_TRACK = 0xB0
VK_MEDIA_PREV_TRACK = 0xB1
VK_MEDIA_STOP = 0xB2
VK_MEDIA_PLAY_PAUSE = 0xB3

MEDIA_KEYS = {
    VK_MEDIA_PLAY_PAUSE: "play_pause",
    VK_MEDIA_NEXT_TRACK: "next",
    VK_MEDIA_PREV_TRACK: "prev",
    VK_MEDIA_STOP: "stop",
}


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wt.DWORD),
        ("scanCode", wt.DWORD),
        ("flags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class MediaKeyHook(QObject):
    """一个常驻的媒体键监听器；窗口销毁前记得 uninstall()。"""

    mediaKey = Signal(str)          # play_pause / next / prev / stop

    def __init__(self, parent=None):
        super().__init__(parent)
        self._hook = None
        self._proc = None
        self._user32 = None
        self._error = ""

    # ---------- 状态 ----------
    def error(self):
        return self._error

    # ---------- 安装 / 卸载 ----------
    def install(self):
        if self._hook is not None:
            return True
        self._error = ""
        if os.name != "nt":
            self._error = "只有 Windows 支持全局媒体键"
            return False
        # 跑自动化测试时把钩子关掉，免得影响本机其它程序
        if os.environ.get("TUNEFREE_DISABLE_MEDIA_HOOK"):
            self._error = "已被环境变量禁用"
            return False
        try:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            proc_type = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int,
                                           wt.WPARAM, wt.LPARAM)
            user32.SetWindowsHookExW.argtypes = [ctypes.c_int, proc_type,
                                                 wt.HINSTANCE, wt.DWORD]
            user32.SetWindowsHookExW.restype = ctypes.c_void_p
            user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                              wt.WPARAM, wt.LPARAM]
            user32.CallNextHookEx.restype = ctypes.c_ssize_t
            user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
            user32.UnhookWindowsHookEx.restype = wt.BOOL
            kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
            kernel32.GetModuleHandleW.restype = ctypes.c_void_p
        except Exception as e:      # 不是 Windows 或者 API 缺了
            self._error = "系统接口不可用：%s" % e
            return False

        def callback(n_code, w_param, l_param):
            # 这个函数在系统消息泵里跑，慢一点都会拖累整个桌面输入
            try:
                if n_code >= 0 and w_param in (WM_KEYDOWN, WM_SYSKEYDOWN):
                    info = ctypes.cast(l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                    if not (info.flags & LLKHF_INJECTED):
                        name = MEDIA_KEYS.get(int(info.vkCode))
                        if name:
                            QTimer.singleShot(0, lambda n=name: self._deliver(n))
            except Exception:
                logging.warning("媒体键回调异常", exc_info=True)
            return user32.CallNextHookEx(None, n_code, w_param, l_param)

        try:
            self._proc = proc_type(callback)
            hmod = kernel32.GetModuleHandleW(None)
            hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._proc, hmod, 0)
            if not hook:
                # 拿不到模块句柄时 hMod 传 NULL 也能装
                hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._proc, None, 0)
        except Exception as e:
            self._error = "安装钩子失败：%s" % e
            self._proc = None
            return False

        if not hook:
            self._error = "安装钩子失败（错误码 %s）" % ctypes.get_last_error()
            self._proc = None
            return False

        self._user32 = user32
        self._hook = hook
        logging.info("全局媒体键已启用")
        return True

    def uninstall(self):
        hook, user32 = self._hook, self._user32
        self._hook = None
        self._user32 = None
        if not hook or user32 is None:
            return
        try:
            user32.UnhookWindowsHookEx(hook)
            logging.info("全局媒体键已关闭")
        except Exception:
            logging.warning("卸载媒体键钩子失败", exc_info=True)

    # ---------- 内部 ----------
    def _deliver(self, name):
        try:
            self.mediaKey.emit(name)
        except RuntimeError:
            # 窗口已经销毁，钩子该在这个之前卸掉，这里兜个底
            pass