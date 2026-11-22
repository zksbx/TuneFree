# -*- coding: utf-8 -*-
"""TuneFree 后端：把 QQ 音乐的 .mflac / .mgg 解密成 FLAC/OGG，再转成 MP3。

两种用法：
1. 命令行：`app.py --cli -i 目录 -o 输出目录`
2. 被界面直接调用：`Runner(..., on_event=回调).run()`，进度通过 on_event 回传

on_event(kind, payload) 的 kind：
    "conn"     连接状态，payload = {"ok": bool, "message": str}
    "items"    扫描结果，payload = [Track]
    "item"     单曲状态，payload = {"track": Track, "state": str, "message": str}
    "progress" 阶段进度，payload = {"phase": str, "done": int, "total": int}
    "log"      一行日志，payload = {"level": "info"/"error", "message": str}
    "summary"  全部结束，payload = {"ok": int, "fail": int, "skip": int, "elapsed": float}
"""

from __future__ import annotations

import argparse
import itertools
import logging
import os
import re
import shutil
import sys
import tempfile
import time
import traceback
from pathlib import Path

import converter
import scanner
import session as qsession
# 这几个原来住在本文件，现在挪到 converter.py，这里原样导出，老调用方不用改
from converter import force_remove

try:  # 版本号统一放在 version.py，打包时如果没带上就退回内置值
    from version import VERSION
except Exception:
    VERSION = "3.1.2"

# 打包成窗口程序后，父进程自己没有控制台；这时每启动一次 ffmpeg（控制台程序），
# Windows 都会给它新建一个控制台窗口 —— 就是用户看到的一闪而过的黑框。
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# 注入阶段失败的原因：收尾时用它决定要不要报失败，而不是假装「全部处理完成」
_last_inject_error = ""

DEFAULT_BITRATE = "320k"


# -------------------- 强制 UTF-8 输出 --------------------
def _usable_stream(stream, mode="w"):
    """把已有流包成 UTF-8，并确认真的写得进去；不能写就退回空设备。

    打包成窗口程序（--noconsole）时 sys.stdout 常常是 None，或者指向一个
    已经失效的句柄。后者会在解释器退出时刷新失败，让进程以退出码 120 结束，
    所以这里先写一个空串试一下，失败就换成 os.devnull。
    """
    if stream is None:
        return open(os.devnull, mode, encoding="utf-8", errors="replace")
    try:
        fd = stream.fileno()
    except (OSError, ValueError, AttributeError):
        fd = None
    if fd is not None:
        try:
            wrapped = open(fd, mode, encoding="utf-8", errors="replace", closefd=False)
            wrapped.write("")
            wrapped.flush()
            return wrapped
        except (OSError, ValueError):
            pass
    try:
        stream.write("")
        stream.flush()
        return stream
    except (OSError, ValueError):
        return open(os.devnull, mode, encoding="utf-8", errors="replace")


def _force_utf8_stdio():
    """窗口模式打包（--noconsole）时 sys.stdout 常常是 None。

    print() 会静默丢弃输出，连 @@PROGRESS 进度协议也发不出去。这里保证
    stdout / stderr 一定指向一个写得进去的 UTF-8 流；没有控制台就写空设备。
    """
    sys.stdout = _usable_stream(getattr(sys, "stdout", None))
    sys.stderr = _usable_stream(getattr(sys, "stderr", None)) or sys.stdout


if sys.platform == "win32":
    _force_utf8_stdio()


# -------------------- 小工具 --------------------
def resource_path(relative_path):
    """打包后在 _MEIPASS，源码运行时在本文件目录。"""
    if hasattr(sys, "_MEIPASS"):
        return os.path.join(sys._MEIPASS, relative_path)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), relative_path)


def sanitize_bitrate(value, default=DEFAULT_BITRATE):
    """比特率只接受 128k / 320k 这种写法，别把奇怪的值传给 ffmpeg。"""
    bitrate = (value or default).strip().lower()
    if not re.fullmatch(r"\d{2,3}k", bitrate):
        return default
    kbps = int(bitrate[:-1])
    if kbps > 320:
        return "320k"
    if kbps < 32:
        return default
    return bitrate


def default_output_dirs(input_dir, output=None):
    """算出 (输出根目录, FLAC 目录, MP3 目录)。"""
    root = os.path.abspath(output) if output else os.path.join(input_dir, "output")
    return root, os.path.join(root, "flac"), os.path.join(root, "mp3")


def ffmpeg_path():
    return resource_path("ffmpeg.exe")


def _safe_print(line):
    """往控制台写一行；打包成窗口程序后可能没有可用的 stdout，写不出去就跳过。"""
    try:
        print(line, flush=True)
    except (OSError, ValueError):
        pass


def emit_progress(phase, done, total):
    """进度协议：界面认 `@@PROGRESS 解密 3/10` 这一行（不写进日志）。"""
    _safe_print("@@PROGRESS %s %d/%d" % (phase, done, total))


# -------------------- 任务执行器 --------------------
class Runner:
    """一次完整的「解密 + 转码」任务。"""

    def __init__(self, input_dir, flac_dir=None, mp3_dir=None, bitrate=DEFAULT_BITRATE,
                 keep_flac=False, no_mp3=False, force=False, workers=None,
                 on_event=None, hook_path=None, session=None):
        self.input_dir = os.path.abspath(input_dir)
        self.flac_dir = os.path.abspath(flac_dir) if flac_dir else os.path.join(self.input_dir, "output", "flac")
        self.mp3_dir = os.path.abspath(mp3_dir) if mp3_dir else os.path.join(self.input_dir, "output", "mp3")
        self.bitrate = sanitize_bitrate(bitrate)
        self.keep_flac = bool(keep_flac)
        self.no_mp3 = bool(no_mp3)
        self.force = bool(force)
        self.workers = workers
        self.on_event = on_event
        self.hook_path = hook_path or qsession.default_hook_path()
        self._tmp_seq = itertools.count()
        self._preset_session = session

        self.session = None
        self.tracks = []
        self.cancelled = False
        self.summary = {"ok": 0, "fail": 0, "skip": 0, "elapsed": 0.0}

    # ---------- 事件 ----------
    def _emit(self, kind, payload):
        if not self.on_event:
            return
        try:
            self.on_event(kind, payload)
        except Exception:  # 界面回调自己炸了不该带崩任务
            logging.debug("on_event 回调异常", exc_info=True)

    def _log(self, level, message):
        logging.log(logging.ERROR if level == "error" else logging.INFO, message)
        self._emit("log", {"level": level, "message": message})

    def _on_session_state(self, connected):
        if connected:
            self._emit("conn", {"ok": True, "message": "已连接 QQ音乐"})
        else:
            message = (self.session.last_error if self.session else "") or "与 QQ音乐 的连接已断开"
            self._emit("conn", {"ok": False, "message": message})

    # ---------- 连接 ----------
    def connect(self):
        """连上 QQ音乐。失败返回 False，原因在 last_error 里。"""
        global _last_inject_error
        preset = self._preset_session
        if preset is not None and getattr(preset, "connected", False):
            self.session = preset
            _last_inject_error = ""
            self._emit("conn", {"ok": True, "message": "已连接 QQ音乐"})
            return True
        self.session = qsession.QQMusicSession(self.hook_path, on_state=self._on_session_state)
        if not self.session.attach():
            _last_inject_error = self.session.last_error or "无法连接 QQ音乐进程"
            self._emit("conn", {"ok": False, "message": _last_inject_error})
            return False
        _last_inject_error = ""
        self._emit("conn", {"ok": True, "message": "已连接 QQ音乐"})
        return True

    def disconnect(self):
        if self.session:
            self.session.detach()
            self.session = None

    # ---------- 扫描 ----------
    def scan(self):
        """列出目录里的加密文件，能连上 QQ音乐时顺带读出时长/格式。"""
        cover_dir = os.path.join(self.flac_dir, ".covers") if self.flac_dir else None
        self.tracks = scanner.scan(self.input_dir, session=self.session, cover_dir=cover_dir)
        self._emit("items", self.tracks)
        return self.tracks

    # ---------- 执行 ----------
    def _target_paths(self, track):
        stem = Path(track.path).stem
        flac_path = os.path.join(self.flac_dir, stem + track.plain_ext)
        mp3_path = os.path.join(self.mp3_dir, stem + ".mp3")
        return flac_path, mp3_path

    def _decrypt_one(self, track, dst):
        """解一首，直接落到最终路径。

        第一选择是让 QQ音乐在它自己的进程里写盘（hook 里走 kernel32）：几十 MB
        不用跨进程搬一遍，中文文件名也不会炸。这一步失败才退回老路 —— 先用
        英文临时名写盘再改名（frida 的 File 写不了非 ASCII 文件名，实测中文
        文件名会抛 Illegal byte sequence）。
        """
        session = self.session
        if session is None:
            raise qsession.SessionError("还没有连接到 QQ音乐")

        try:
            wrote = session.decrypt(track.path, dst)
            if wrote and os.path.isfile(dst) and os.path.getsize(dst) > 0:
                return
        except Exception as e:
            logging.debug("直接写盘失败，改用兜底路径：%s", e)
        force_remove(dst)

        folder = os.path.dirname(dst) or "."
        token = "%d_%d" % (os.getpid(), next(self._tmp_seq))
        tmp = os.path.join(folder, ".tf_%s%s" % (token, track.plain_ext))
        try:
            session.decrypt_file(track.path, tmp)
            os.replace(tmp, dst)
            return
        except Exception:
            force_remove(tmp)
            # 目录本身带非英文字符时退到 %TEMP% 再来一次
            tmp = os.path.join(tempfile.gettempdir(), "tf_%s%s" % (token, track.plain_ext))
            try:
                session.decrypt_file(track.path, tmp)
                shutil.move(tmp, dst)
            finally:
                force_remove(tmp)

    def run(self, tracks=None):
        """跑完整个任务。返回 summary。"""
        global _last_inject_error
        started = time.time()
        self.cancelled = False
        result = {"ok": 0, "fail": 0, "skip": 0, "elapsed": 0.0}

        if not os.path.isdir(self.input_dir):
            self._log("error", "输入目录不存在：%s" % self.input_dir)
            self._emit("summary", result)
            return result

        connected = self.session is not None and self.session.connected
        if not connected:
            connected = self.connect()

        tracks = tracks if tracks is not None else self.tracks
        if not tracks:
            tracks = self.scan()

        targets = []
        for track in tracks:
            if track.status in ("done", "skipped") and not self.force:
                result["skip"] += 1
                continue
            targets.append(track)

        # 大文件先排：转码是固定几个线程排队干，先开工的都最久，
        # 把最长的放前面，最后收尾就不用干等一首 50MB 的
        def _size_of(track):
            try:
                return os.path.getsize(track.path)
            except OSError:
                return 0

        targets.sort(key=_size_of, reverse=True)

        total = len(targets)
        if total == 0:
            self._log("info", "没有需要处理的文件")
            result["elapsed"] = round(time.time() - started, 2)
            self.summary = result
            self._emit("summary", result)
            return result

        for folder in (self.flac_dir, self.mp3_dir):
            try:
                os.makedirs(folder, exist_ok=True)
            except OSError as e:
                self._log("error", "创建输出目录失败：%s（%s）" % (folder, e))
                self._emit("summary", result)
                return result

        conv = converter.Converter(ffmpeg_path(), self.bitrate, workers=self.workers,
                                   on_event=self._on_convert_event)
        conv.start()

        try:
            for done, track in enumerate(targets, 1):
                if self.cancelled:
                    break
                track.status = "working"
                self._emit("item", {"track": track, "state": "decrypt", "message": "正在解密"})

                flac_path, mp3_path = self._target_paths(track)
                try:
                    if self.no_mp3 and track.plain_ext == ".flac":
                        # 只要 FLAC：直接解到目标位置，不转码也不算跳过
                        if os.path.exists(flac_path) and not self.force:
                            result["skip"] += 1
                            track.status = "skipped"
                            track.message = "已存在，跳过"
                        else:
                            self._decrypt_one(track, flac_path)
                            result["ok"] += 1
                            track.status = "done"
                            track.message = "已解密"
                            track.output = flac_path
                        self._emit("item", {"track": track, "state": "done", "message": track.message})
                        self._emit("progress", {"phase": "解密", "done": done, "total": total})
                        emit_progress("解密", done, total)
                        continue

                    # 正常路径：解密到中间文件，再交给转码线程池
                    need_decrypt = True
                    if os.path.exists(flac_path) and not self.force:
                        need_decrypt = False
                        self._log("info", "%s 之前已解密过，直接复用" % os.path.basename(flac_path))
                    if need_decrypt:
                        self._decrypt_one(track, flac_path)

                    keep_src = self.keep_flac
                    job = converter.Job(flac_path, mp3_path, keep_src=keep_src,
                                        force=self.force, track=track)
                    if os.path.exists(mp3_path) and not self.force:
                        result["skip"] += 1
                        track.status = "skipped"
                        track.message = "MP3 已存在"
                        if not keep_src:
                            force_remove(flac_path)
                        self._emit("item", {"track": track, "state": "skip", "message": track.message})
                    else:
                        conv.submit(job)
                        self._emit("item", {"track": track, "state": "convert", "message": "等待转码"})
                except Exception as e:
                    result["fail"] += 1
                    track.status = "failed"
                    track.message = _human_error(e)
                    self._log("error", "%s 解密失败：%s" % (track.name, track.message))
                    self._emit("item", {"track": track, "state": "fail", "message": track.message})

                self._emit("progress", {"phase": "解密", "done": done, "total": total})
                emit_progress("解密", done, total)

            conv.join()
        finally:
            conv.shutdown()
            self.disconnect()

        result["ok"] += max(0, conv.done - conv.skipped)
        result["skip"] += conv.skipped
        result["fail"] += conv.failed
        result["elapsed"] = round(time.time() - started, 2)
        self.summary = result

        self._log("info", "处理完成：成功 %d 个，失败 %d 个，跳过 %d 个，用时 %.1f 秒"
                  % (result["ok"], result["fail"], result["skip"], result["elapsed"]))
        if result["fail"]:
            self._log("info", "失败的大多是音质不支持：TuneFree 只能处理标准/HQ/SQ，"
                              "臻品音质、臻品母带、杜比全景声都解不开")
        self._emit("summary", result)
        return result

    def _on_convert_event(self, kind, job, extra=None):
        """转码线程池的回调：把 Job 翻译成界面认识的行状态。"""
        track = getattr(job, "track", None)
        if kind == "start" and track is not None:
            track.status = "working"
            self._emit("item", {"track": track, "state": "convert", "message": "正在转码"})
        elif kind == "done" and track is not None:
            track.status = "done"
            track.message = "已完成"
            track.output = job.dst
            self._emit("item", {"track": track, "state": "done", "message": "已完成"})
        elif kind == "skip" and track is not None:
            track.status = "skipped"
            track.message = "MP3 已存在，跳过"
            self._emit("item", {"track": track, "state": "skip", "message": track.message})
        elif kind == "fail" and track is not None:
            track.status = "failed"
            track.message = job.message or "转码失败"
            self._log("error", "%s 转码失败：%s" % (track.name, track.message))
            self._emit("item", {"track": track, "state": "fail", "message": track.message})

    def cancel(self):
        self.cancelled = True


def _human_error(exc):
    """把底层异常翻译成人能看懂的一句话。"""
    text = str(exc).strip()
    low = text.lower()
    if "transport" in low or "connection is closed" in low:
        return "和 QQ音乐 的连接断了，请重新打开 QQ音乐再试"
    if "processnotfound" in low or "not found" in low:
        return "没有找到 QQ音乐进程"
    if "invalid size" in low or "invalid data" in low:
        return "这个文件解不开，多半是音质不支持（只支持标准/HQ/SQ）"
    if "timed out" in low or "timeout" in low:
        return "解密超时，请重试"
    return text or "未知错误"


# -------------------- 命令行 --------------------
def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="解密 QQ音乐 mflac/mgg 文件并转换为 MP3")
    parser.add_argument("-i", "--input", help="输入目录（包含 .mflac / .mgg 文件），默认为当前目录")
    parser.add_argument("-o", "--output", help="输出根目录（默认：输入目录下的 output），其下自动创建 mp3 与 flac 两个子文件夹")
    parser.add_argument("--flac-dir", help="FLAC 输出目录（仅在 --keep-flac 时有效）")
    parser.add_argument("--mp3-dir", help="MP3 输出目录（覆盖默认）")
    parser.add_argument("-b", "--bitrate", default=DEFAULT_BITRATE, help="MP3 比特率，例如 128k, 192k, 320k (默认: 320k)")
    parser.add_argument("--keep-flac", action="store_true", help="保留解密后的 FLAC 文件（默认不保留）")
    parser.add_argument("--no-mp3", action="store_true", help="不生成 MP3，仅保留 FLAC（忽略 --keep-flac）")
    parser.add_argument("--force", action="store_true", help="已经存在的 MP3 也重新生成（默认跳过）")
    parser.add_argument("--workers", type=int, default=None, help="同时转码的进程数（默认按 CPU 自动）")
    parser.add_argument("-v", "--version", action="version", version="TuneFree %s" % VERSION)
    return parser.parse_args(argv)


def _print_event(kind, payload):
    """命令行模式把事件打到控制台。"""
    if kind == "log":
        prefix = "错误" if payload.get("level") == "error" else "信息"
        _safe_print("[%s] %s" % (prefix, payload.get("message", "")))
    elif kind == "item":
        track = payload.get("track")
        state = payload.get("state")
        if state in ("done", "fail", "skip") and track is not None:
            _safe_print("@@ITEM %s %s %s" % (state, track.name, payload.get("message", "")))
    elif kind == "conn":
        if not payload.get("ok"):
            _safe_print("[错误] %s" % payload.get("message", ""))


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")
    args = parse_args(argv)
    logging.info("TuneFree %s" % VERSION)

    input_dir = os.path.abspath(args.input) if args.input else os.getcwd()
    if not os.path.isdir(input_dir):
        logging.error("输入目录不存在: %s" % input_dir)
        return 1

    root_output, flac_dir, mp3_dir = default_output_dirs(input_dir, args.output)
    flac_dir = os.path.abspath(args.flac_dir) if args.flac_dir else flac_dir
    mp3_dir = os.path.abspath(args.mp3_dir) if args.mp3_dir else mp3_dir

    logging.info("输入目录: %s" % input_dir)
    logging.info("FLAC 输出: %s" % flac_dir)
    logging.info("MP3 输出: %s" % mp3_dir)

    tool = ffmpeg_path()
    if not os.path.exists(tool):
        logging.error("未找到 ffmpeg.exe，请将其放在 %s 路径" % tool)
        return 1

    runner = Runner(input_dir, flac_dir, mp3_dir, args.bitrate, args.keep_flac,
                    args.no_mp3, args.force, args.workers, on_event=_print_event)
    if not runner.connect():
        logging.error("%s；这次不会有任何输出" % _last_inject_error)
        return 1

    summary = runner.run()
    if summary["ok"] == 0 and summary["skip"] == 0:
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        logging.warning("已被用户中断")
        sys.exit(130)
    except Exception:  # 兜底：别让界面只看到一个莫名其妙的返回码
        logging.error("程序异常终止，下面是详细信息：")
        traceback.print_exc()
        sys.exit(1)


# 发行者：曾困睡不醒
# 发行日期：26.3.8