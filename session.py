"""QQ音乐注入会话管理。

和 2.1.3 的差别：
- 会话和脚本只注入一次，整个运行期间复用（原来每次「开始执行」都要 attach/load/detach）；
- 提供连接状态回调，界面据此显示「已连接 / 未连接」；
- 连接断了会自动重连一次，转码中途 QQ 音乐重启也不会让整批任务全废。
"""

from __future__ import annotations

import logging
import os
import threading

import frida

_HOOK_FILE = "hook_qq_music.js"
_TARGET = "QQMusic.exe"

# frida 的本地设备是进程级全局对象，两个线程同时 attach 会直接访问冲突崩溃
# （启动时的自动扫描 + 用户马上点文件夹，就会撞上）。RPC 也用同一把锁，
# 免得转码线程和界面线程同时往一个 session 里塞调用。
_LOCK = threading.RLock()


def _pid_alive(pid):
    """Windows 上判断进程还在不在（几毫秒，不碰 frida）。"""
    try:
        import ctypes
        from ctypes import wintypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            k32.CloseHandle(handle)
    except Exception:
        return True


def find_qqmusic_pids():
    """枚举名为 QQMusic.exe 的进程（几毫秒，不碰 frida）。"""
    pids = []
    try:
        import ctypes
        from ctypes import wintypes

        TH32CS_SNAPPROCESS = 0x00000002
        INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_void_p),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", wintypes.WCHAR * 260),
            ]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if not snap or int(snap) == INVALID_HANDLE_VALUE:
            return pids
        try:
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            ok = k32.Process32FirstW(snap, ctypes.byref(entry))
            while ok:
                if (entry.szExeFile or "").lower() == "qqmusic.exe":
                    pids.append(int(entry.th32ProcessID))
                ok = k32.Process32NextW(snap, ctypes.byref(entry))
        finally:
            k32.CloseHandle(snap)
    except Exception:
        return pids
    return pids


def qqmusic_running():
    """QQ 音乐是否在运行（用来决定要不要去注入）。"""
    return bool(find_qqmusic_pids())


class SessionError(RuntimeError):
    pass


class QQMusicSession:
    """对 frida 的一层薄封装，所有 RPC 都走 call()。"""

    def __init__(self, hook_path, on_state=None):
        self._hook_path = hook_path
        self._on_state = on_state
        self._session = None
        self._script = None
        self._connected = False
        self.last_error = ""

    # ---------------- 状态 ----------------
    @property
    def connected(self):
        return self._connected and self._session is not None

    def _set_state(self, connected, message=""):
        self._connected = connected
        if not connected:
            self.last_error = message
        if self._on_state is not None:
            try:
                self._on_state(bool(connected), message)
            except Exception:
                logging.debug("连接状态回调出错", exc_info=True)

    # ---------------- 连接 ----------------
    def attach(self):
        """建立连接并加载 hook；成功返回 True，失败把原因留在 last_error。"""
        with _LOCK:
            return self._attach_inner()

    def _attach_inner(self):
        self.detach()
        try:
            self._session = frida.attach(_TARGET)
        except frida.ProcessNotFoundError:
            self._set_state(False, "没有找到 QQ音乐进程，请先打开并登录 QQ音乐")
            return False
        except frida.TransportError as e:
            self._set_state(False, f"无法连接 QQ音乐进程（注入通道建立失败）：{e}")
            logging.error("无法连接 QQ音乐进程（注入通道建立失败）: %s", e)
            logging.error("常见原因：杀毒软件拦下了 frida 的注入组件；QQ音乐以管理员身份运行而本程序不是；"
                          "临时目录 %TEMP% 不可写")
            return False
        except Exception as e:  # 权限等其它异常
            self._set_state(False, f"连接 QQ音乐失败：{e}")
            return False

        try:
            with open(self._hook_path, "r", encoding="utf-8") as f:
                jscode = f.read()
            self._script = self._session.create_script(jscode)
            self._script.load()
        except (OSError, frida.TransportError) as e:
            self._set_state(False, f"注入脚本失败：{e}")
            self._session = None
            self._script = None
            return False

        try:
            probe = self._script.exports_sync.probe()
            if not probe.get("ok", True):
                msg = str(probe.get("message") or "QQ音乐版本与 TuneFree 不匹配")
                logging.error(msg)
                self._set_state(False, msg)
                self.detach()
                return False
        except Exception as e:
            # 旧版 hook 没有 probe 导出，忽略即可
            logging.debug("跳过版本自检: %s", e)

        self._set_state(True, "")
        logging.info("已连接到 QQMusic 进程")
        return True

    @property
    def pid(self):
        session = self._session
        return int(getattr(session, "pid", 0) or 0) if session is not None else 0

    def is_alive(self):
        """QQ 音乐进程还在、frida 会话也还没断。界面每 2 秒问一次。"""
        if self._session is None:
            return False
        try:
            if getattr(self._session, "is_detached", False):
                return False
        except Exception:
            pass
        pid = self.pid
        if not pid:
            return self._connected
        return _pid_alive(pid)

    def ensure(self):
        """已连接就直接用；断了就重连一次。"""
        if self.connected:
            return True
        logging.info("连接已断开，正在重新连接 QQ音乐…")
        return self.attach()

    def detach(self):
        with _LOCK:
            self._detach_inner()

    def _detach_inner(self):
        script, session = self._script, self._session
        self._script = None
        self._session = None
        if script is not None:
            try:
                script.unload()
            except Exception:
                pass
        if session is not None:
            try:
                session.detach()
            except Exception:
                pass
        if self._connected:
            self._set_state(False, "")

    # ---------------- RPC ----------------
    def call(self, name, *args):
        """调用 hook 里的方法；传输层错误时重连并重试一次。"""
        with _LOCK:
            return self._call_inner(name, *args)

    def _call_inner(self, name, *args):
        if not self.ensure():
            raise SessionError(self.last_error or "未连接到 QQ音乐进程")

        fn = getattr(self._script.exports_sync, name, None)
        if fn is None:
            raise SessionError(f"当前 hook 版本不支持 {name}()，请更新 TuneFree")

        try:
            return fn(*args)
        except frida.TransportError as e:
            logging.warning("注入通道断开（%s），准备重连后重试", e)
            self._set_state(False, f"注入通道断开：{e}")
            self.detach()
            if not self.ensure():
                raise SessionError(self.last_error or "重连失败")
            fn = getattr(self._script.exports_sync, name, None)
            if fn is None:
                raise SessionError(f"当前 hook 版本不支持 {name}()")
            return fn(*args)
        except Exception:
            raise

    # 便捷包装
    def decrypt(self, src, dst):
        """让 QQ音乐在它自己的进程里把解密结果写到 dst（支持中文名）。"""
        return self.call("decrypt", src, dst)

    def decrypt_file(self, src, dst):
        """老写法：解密结果先回传到 Frida 再写盘，只作兜底。"""
        return self.call("decryptfile", src, dst)

    def decrypt_whole(self, src, dst):
        """整块解密后写盘：流式分块解出来不对时回退用。"""
        return self.call("decryptwhole", src, dst)

    def can_alloc(self, size=192 * 1024 * 1024):
        """问一下 QQ音乐进程还能不能申请这么大一块内存。

        旧版 hook 不支持这个方法时返回 True（当作有余量），
        免得老版本 QQ音乐配新程序时把任务卡住。
        """
        try:
            return bool(self.call("probealloc", int(size)))
        except Exception:
            return True

    def read_head(self, src, limit):
        data = self.call("readhead", src, int(limit))
        return bytes(data) if data else b""

    def read_tail(self, src, limit):
        data = self.call("readtail", src, int(limit))
        return bytes(data) if data else b""


def default_hook_path():
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(here, _HOOK_FILE)
