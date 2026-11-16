# -*- coding: utf-8 -*-
"""转码：把解密出来的 FLAC/OGG 交给 ffmpeg 转成 MP3。

- `convert_to_mp3()` 是单文件入口，带三层退路（直接写 → 管道输出 → 临时目录中转）；
- `Converter` 是线程池版本，多首并行转，事件里带 track 方便界面定位到行。

本模块不依赖 main.py（避免循环导入），只依赖标准库。
"""

from __future__ import annotations

import logging
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

# 并发上限：转 MP3 时 ffmpeg 基本只吃一个核，所以按核数并行最划算；
# 上限留 12，再多就是几个进程抢同一块磁盘，反而慢
MAX_WORKERS = 12

# 打包成窗口程序后，父进程没有控制台；不关掉子进程控制台，ffmpeg 启动时会闪黑框
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def worker_count(requested=None):
    """想要几个 worker：给了就夹在 1..12，没给按 CPU 一半再多给两个。

    ffmpeg 转 MP3 基本单线程，多开就是多吃核；实测 20 核开 12 个比开 4 个
    快三成，再往上提就被功耗墙吃掉了。4 核机器给 4 个、8 核给 6 个。
    """
    if requested and requested > 0:
        return min(int(requested), MAX_WORKERS)
    cpu = os.cpu_count() or 4
    return max(2, min(MAX_WORKERS, cpu // 2 + 2))


def force_remove(file_path, max_retries=5, delay=0.2):
    """删文件，遇到「正被占用」重试几次。"""
    for _ in range(max_retries):
        try:
            if os.path.exists(file_path):
                os.unlink(file_path)
            return True
        except PermissionError:
            time.sleep(delay)
            continue
        except OSError:
            return False
    return False


def explain_returncode(code):
    """把 ffmpeg/系统的返回码翻译成人话，方便用户自查。"""
    if code in (4294967283, -13):
        return ("权限不足/拒绝访问(EACCES)：常见原因是杀毒软件拦下了 ffmpeg.exe，"
                "或输出目录里的同名 .mp3 正被别的程序占用（播放器/网盘同步/资源管理器预览）")
    if code in (4294967295, -1):
        return "ffmpeg 内部错误，通常是输入文件损坏或未解密完整"
    if code in (4294967294, -2):
        return "找不到输入文件(ENOENT)，可能解密产物已被移走或删除"
    if code in (4294967264, -32):
        return "文件正被其他进程占用(EBUSY)"
    if code in (4294967263, -33):
        return "参数不合法(EINVAL)"
    if code == 1:
        return "ffmpeg 处理失败（输入不是有效音频，或输出路径不可写）"
    if code > 2147483647:
        return "系统/驱动层错误(0x%08X)" % code
    return "未知错误"


def _run_ffmpeg(audio_path, out_path, ffmpeg_path, bitrate):
    """真正调用 ffmpeg，失败时抛 CalledProcessError。"""
    cmd = [str(ffmpeg_path), "-hide_banner", "-loglevel", "error", "-nostdin",
           "-i", str(audio_path), "-b:a", bitrate, str(out_path), "-y"]
    subprocess.run(cmd, check=True, capture_output=True, text=True,
                   encoding="utf-8", errors="ignore", creationflags=_NO_WINDOW)


def _run_ffmpeg_to_bytes(audio_path, ffmpeg_path, bitrate):
    """让 ffmpeg 把 MP3 输出到标准输出，由 Python 负责落盘。

    有些安全软件会禁止 ffmpeg.exe 创建任何文件（连 %TEMP% 都不许），
    但读文件和写管道是放行的，这条路能绕过去。
    """
    cmd = [str(ffmpeg_path), "-hide_banner", "-loglevel", "error",
           "-i", str(audio_path), "-b:a", bitrate, "-f", "mp3", "pipe:1"]
    proc = subprocess.run(cmd, check=True, capture_output=True, creationflags=_NO_WINDOW)
    data = proc.stdout or b""
    if len(data) < 2048:
        raise RuntimeError("ffmpeg 只输出了 %d 字节，转码未成功" % len(data))
    return data


def convert_to_mp3(audio_path, mp3_path, ffmpeg_path, bitrate="320k"):
    """单文件转码。返回 True/False，失败原因写日志。"""
    try:
        _run_ffmpeg(audio_path, mp3_path, ffmpeg_path, bitrate)
        return True
    except FileNotFoundError:
        logging.error("未找到 ffmpeg.exe（请确认 ffmpeg.exe 与程序在同一目录）")
        return False
    except OSError as e:
        logging.error("无法运行 ffmpeg.exe: %s（多半是杀毒软件拦截，请把程序目录加入白名单）" % e)
        return False
    except subprocess.CalledProcessError as e:
        code = e.returncode
        stderr = e.stderr if isinstance(e.stderr, str) else (e.stderr or b"").decode("utf-8", "ignore")
        detail = [x.strip() for x in stderr.splitlines() if x.strip()]

        # 写入被拒(EACCES/EPERM)：退一步，让 ffmpeg 把音频输出到管道，由 Python 自己写文件
        if code in (4294967283, -13, 4294967291, -5):
            logging.warning("%s: ffmpeg 无法创建输出文件，改用「管道输出 + Python 落盘」重试…"
                            % Path(mp3_path).name)
            try:
                data = _run_ffmpeg_to_bytes(audio_path, ffmpeg_path, bitrate)
                Path(mp3_path).write_bytes(data)
                logging.info("转码成功（管道模式）: %s" % Path(mp3_path).name)
                return True
            except Exception as e2:
                logging.error("  管道模式也失败: %s" % e2)

            # 再退一步：先写临时目录，再搬过去
            tmp = Path(tempfile.gettempdir()) / ("tf_%d_%s.mp3" % (os.getpid(), Path(mp3_path).stem))
            try:
                _run_ffmpeg(audio_path, str(tmp), ffmpeg_path, bitrate)
                shutil.move(str(tmp), str(mp3_path))
                logging.info("转码成功（经临时目录中转）: %s" % Path(mp3_path).name)
                return True
            except Exception as e3:
                logging.error("转码失败 %s，返回码: %s（%s）"
                              % (Path(audio_path).name, code, explain_returncode(code)))
                for line in detail[-4:]:
                    logging.error("  ffmpeg: %s" % line)
                logging.error("  经临时目录重试仍失败: %s" % e3)
                return False
            finally:
                try:
                    if tmp.exists():
                        tmp.unlink()
                except OSError:
                    pass

        logging.error("转码失败 %s，返回码: %s（%s）"
                      % (Path(audio_path).name, code, explain_returncode(code)))
        for line in detail[-4:]:
            logging.error("  ffmpeg: %s" % line)
        return False


class Job:
    """一次转码任务。"""

    __slots__ = ("src", "dst", "keep_src", "force", "track", "ok", "message")

    def __init__(self, src, dst, keep_src=False, force=False, track=None):
        self.src = str(src)
        self.dst = str(dst)
        self.keep_src = keep_src
        self.force = force
        self.track = track
        self.ok = None
        self.message = ""

    def __repr__(self):
        return "<Job %s -> %s>" % (Path(self.src).name, Path(self.dst).name)


class Converter:
    """固定数量的转码线程 + 一个任务队列。

    on_event(kind, job, extra)：kind 为 start / done / fail / skip。
    """

    def __init__(self, ffmpeg_path, bitrate="320k", workers=None, on_event=None):
        self.ffmpeg_path = ffmpeg_path
        self.bitrate = bitrate
        self.workers = worker_count(workers)
        self.on_event = on_event
        self._queue = queue.Queue()
        self._threads = []
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.done = 0
        self.failed = 0
        self.skipped = 0
        self.total = 0

    # ---------------- 事件 ----------------
    def _emit(self, kind, job, extra=None):
        if self.on_event is None:
            return
        try:
            self.on_event(kind, job, extra or {})
        except Exception:
            logging.debug("转码事件回调出错", exc_info=True)

    # ---------------- 生命周期 ----------------
    def start(self):
        for i in range(self.workers):
            t = threading.Thread(target=self._run, name="tf-convert-%d" % i, daemon=True)
            t.start()
            self._threads.append(t)

    def submit(self, job):
        with self._lock:
            self.total += 1
        self._queue.put(job)

    def join(self):
        """等所有已提交的任务处理完。"""
        self._queue.join()

    def shutdown(self):
        self._stop.set()
        for _ in self._threads:
            self._queue.put(None)
        for t in self._threads:
            t.join(timeout=1.0)
        self._threads.clear()

    # ---------------- 工作线程 ----------------
    def _run(self):
        while not self._stop.is_set():
            job = self._queue.get()
            if job is None:
                self._queue.task_done()
                break
            try:
                self._handle(job)
            except Exception as e:  # 兜底，绝不让线程死掉
                job.ok = False
                job.message = str(e)
                logging.error("转码线程异常: %s" % e)
                with self._lock:
                    self.failed += 1
                self._emit("fail", job)
            finally:
                self._queue.task_done()

    def _handle(self, job):
        dst = Path(job.dst)
        src = Path(job.src)

        if dst.exists() and not job.force:
            with self._lock:
                self.skipped += 1
                self.done += 1
            job.ok = True
            job.message = "已存在，跳过"
            if not job.keep_src:
                force_remove(str(src))
            self._emit("skip", job)
            return

        self._emit("start", job)
        ok = convert_to_mp3(str(src), str(dst), self.ffmpeg_path, self.bitrate)
        job.ok = ok
        if ok:
            if not job.keep_src and not force_remove(str(src)):
                logging.warning("源文件删除失败（可能被播放器占用），已保留: %s" % src.name)
            with self._lock:
                self.done += 1
            self._emit("done", job)
        else:
            if not job.message:
                job.message = "ffmpeg 转换失败"
            with self._lock:
                self.failed += 1
            self._emit("fail", job)