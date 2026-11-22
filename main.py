# -*- coding: utf-8 -*-
"""TuneFree 后端：把 QQ 音乐的 .mflac / .mgg 解密成 FLAC / OGG。

只做解密，不再转 MP3 —— 无损文件就是最终产物；OGG 是源文件自带的有损格式，
再转一遍只会更差。解密完成后如果文件里没有封面，会尝试从 QQ 音乐图片缓存补一张。

两种用法：
1. 命令行：`app.py --cli -i 目录 -o 输出目录`
2. 被界面直接调用：`Runner(..., on_event=回调).run()`，进度通过 on_event 回传

on_event(kind, payload) 的 kind：
    "conn"     连接状态，payload = {"ok": bool, "message": str}
    "items"    扫描结果，payload = [Track]
    "item"     单曲状态，payload = {"track": Track, "state": str, "message": str, "output": str}
    "progress" 阶段进度，payload = {"phase": str, "done": int, "total": int}
    "log"      一行日志，payload = {"level": "info"/"error", "message": str}
    "summary"  全部结束，payload = {"ok": int, "fail": int, "skip": int, "elapsed": float}
"""

from __future__ import annotations

import argparse
import itertools
import logging
import os
import shutil
import sys
import tempfile
import time
import traceback
from pathlib import Path

import coverart
import scanner
import session as qsession

try:  # 版本号统一放在 version.py，打包时如果没带上就退回内置值
    from version import VERSION
except Exception:
    VERSION = "3.1.2"

# 注入阶段失败的原因：收尾时用它决定要不要报失败，而不是假装「全部处理完成」
_last_inject_error = ""


# -------------------- 强制 UTF-8 输出 --------------------
def _reopen_fd(fd, name):
    """把第 fd 号文件描述符包成 UTF-8 文本流；fd 不可用返回 None。"""
    try:
        return open(fd, mode="w", encoding="utf-8", errors="replace", closefd=False)
    except (OSError, ValueError):
        return None


def _force_utf8_stdio():
    """窗口模式打包（--noconsole）时 sys.stdout 常常是 None。

    这时 print() 会静默丢弃输出，连 @@PROGRESS 进度协议也发不出去，
    界面上的进度条就永远不动。所以这里先试着按 fd 重新打开，拿不到再退回另一个流。
    """
    if sys.stdout is None:
        sys.stdout = _reopen_fd(1, "stdout") or sys.stderr or open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = _reopen_fd(2, "stderr") or sys.stdout or open(os.devnull, "w")

    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:
            continue
        try:
            setattr(sys, name, open(stream.fileno(), mode="w", encoding="utf-8", errors="replace"))
        except (OSError, ValueError, AttributeError):
            pass


if sys.platform == "win32":
    _force_utf8_stdio()


# -------------------- 小工具 --------------------
def default_output_dirs(input_dir, output=None):
    """算出 (输出根目录, FLAC 目录, OGG 目录)。"""
    root = os.path.abspath(output) if output else os.path.join(input_dir, "output")
    return root, os.path.join(root, "flac"), os.path.join(root, "ogg")


def emit_progress(phase, done, total):
    """进度协议：界面认 `@@PROGRESS 解密 3/10` 这一行（不写进日志）。"""
    print("@@PROGRESS %s %d/%d" % (phase, done, total), flush=True)


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


# -------------------- 任务执行器 --------------------
class Runner:
    """一次完整的「解密 + 补封面」任务。"""

    def __init__(self, input_dir, flac_dir=None, ogg_dir=None, force=False,
                 embed_covers=True, on_event=None, hook_path=None, session=None):
        self.input_dir = os.path.abspath(input_dir)
        default_root = os.path.join(self.input_dir, "output")
        self.flac_dir = os.path.abspath(flac_dir) if flac_dir else os.path.join(default_root, "flac")
        self.ogg_dir = os.path.abspath(ogg_dir) if ogg_dir else os.path.join(default_root, "ogg")
        self.force = bool(force)
        self.embed_covers = bool(embed_covers)
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
        self.tracks = scanner.scan(self.input_dir, session=self.session)
        self._emit("items", self.tracks)
        return self.tracks

    # ---------- 执行 ----------
    def _target_path(self, track):
        stem = Path(track.path).stem
        folder = self.ogg_dir if track.plain_ext == ".ogg" else self.flac_dir
        return os.path.join(folder, stem + track.plain_ext)

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
        ext = os.path.splitext(dst)[1]
        token = "%d_%d" % (os.getpid(), next(self._tmp_seq))
        tmp = os.path.join(folder, ".tf_%s%s" % (token, ext))
        try:
            session.decrypt_file(track.path, tmp)
            os.replace(tmp, dst)
            return
        except Exception:
            force_remove(tmp)
            # 目录本身带非英文字符时退到 %TEMP% 再来一次
            tmp = os.path.join(tempfile.gettempdir(), "tf_%s%s" % (token, ext))
            try:
                session.decrypt_file(track.path, tmp)
                shutil.move(tmp, dst)
            finally:
                force_remove(tmp)

    def _add_cover(self, track, dst):
        """解密完补封面；返回是否真的写进去了。失败只记日志。"""
        if not self.embed_covers:
            return False
        try:
            status = coverart.ensure_cover(dst, artist=track.artist or "", title=track.title or "")
            return status == "embedded"
        except Exception as e:
            logging.warning("补封面出错：%s（%s）", os.path.basename(dst), e)
            return False

    def run(self, tracks=None):
        """跑完整个任务。返回 summary。"""
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

        # 大文件先排：解密是固定一条通道排队干，先开工的都最久
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

        for folder in (self.flac_dir, self.ogg_dir):
            try:
                os.makedirs(folder, exist_ok=True)
            except OSError as e:
                self._log("error", "创建输出目录失败：%s（%s）" % (folder, e))
                result["elapsed"] = round(time.time() - started, 2)
                self.summary = result
                self._emit("summary", result)
                return result

        try:
            for done, track in enumerate(targets, 1):
                if self.cancelled:
                    break
                dst = self._target_path(track)
                track.status = "working"
                self._emit("item", {"track": track, "state": "working",
                                    "message": "正在解密", "output": ""})

                try:
                    if os.path.isfile(dst) and os.path.getsize(dst) > 0 and not self.force:
                        result["skip"] += 1
                        track.status = "skipped"
                        track.message = "已存在，跳过"
                        covered = self._add_cover(track, dst)
                        if covered:
                            track.message = "已存在；补上了封面"
                        track.output = dst
                        track.output_size = os.path.getsize(dst)
                        self._emit("item", {"track": track, "state": "skip",
                                            "message": track.message, "output": dst})
                    else:
                        self._decrypt_one(track, dst)
                        covered = self._add_cover(track, dst)
                        track.status = "done"
                        track.message = "已解密" + ("，已补封面" if covered else "")
                        track.output = dst
                        try:
                            track.output_size = os.path.getsize(dst)
                        except OSError:
                            track.output_size = 0
                        result["ok"] += 1
                        self._emit("item", {"track": track, "state": "done",
                                            "message": track.message, "output": dst})
                except Exception as e:
                    result["fail"] += 1
                    track.status = "failed"
                    track.message = _human_error(e)
                    self._log("error", "%s 解密失败：%s" % (track.name, track.message))
                    self._emit("item", {"track": track, "state": "fail",
                                        "message": track.message, "output": ""})

                self._emit("progress", {"phase": "解密", "done": done, "total": total})
                emit_progress("解密", done, total)
        finally:
            self.disconnect()

        result["elapsed"] = round(time.time() - started, 2)
        self.summary = result

        self._log("info", "处理完成：成功 %d 个，失败 %d 个，跳过 %d 个，用时 %.1f 秒"
                  % (result["ok"], result["fail"], result["skip"], result["elapsed"]))
        if result["fail"]:
            self._log("info", "失败的大多是音质不支持：TuneFree 只能处理标准/HQ/SQ，"
                              "臻品音质、臻品母带、杜比全景声都解不开")
        self._emit("summary", result)
        return result

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
    parser = argparse.ArgumentParser(description="解密 QQ音乐 mflac/mgg 文件为 FLAC/OGG")
    parser.add_argument("-i", "--input", help="输入目录（包含 .mflac / .mgg 文件），默认为当前目录")
    parser.add_argument("-o", "--output", help="输出根目录（默认：输入目录下的 output）")
    parser.add_argument("--flac-dir", help="FLAC 输出目录（默认：<输出根目录>/flac）")
    parser.add_argument("--ogg-dir", help="OGG 输出目录（默认：<输出根目录>/ogg）")
    parser.add_argument("--no-cover", action="store_true", help="不从 QQ音乐缓存补封面")
    parser.add_argument("--force", action="store_true", help="已经解出来的文件也重新解密")
    parser.add_argument("-v", "--version", action="version", version="TuneFree %s" % VERSION)
    return parser.parse_args(argv)


def _print_event(kind, payload):
    """命令行模式把事件打到控制台。"""
    if kind == "log":
        prefix = "错误" if payload.get("level") == "error" else "信息"
        print("[%s] %s" % (prefix, payload.get("message", "")), flush=True)
    elif kind == "item":
        track = payload.get("track")
        state = payload.get("state")
        if state in ("done", "fail", "skip") and track is not None:
            print("@@ITEM %s %s %s" % (state, track.name, payload.get("message", "")), flush=True)
    elif kind == "conn":
        if not payload.get("ok"):
            print("[错误] %s" % payload.get("message", ""), flush=True)


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")
    args = parse_args(argv)
    logging.info("TuneFree %s" % VERSION)

    input_dir = os.path.abspath(args.input) if args.input else os.getcwd()
    if not os.path.isdir(input_dir):
        logging.error("输入目录不存在: %s" % input_dir)
        return 1

    _root, flac_dir, ogg_dir = default_output_dirs(input_dir, args.output)
    flac_dir = os.path.abspath(args.flac_dir) if args.flac_dir else flac_dir
    ogg_dir = os.path.abspath(args.ogg_dir) if args.ogg_dir else ogg_dir

    logging.info("输入目录: %s" % input_dir)
    logging.info("FLAC 输出: %s" % flac_dir)
    logging.info("OGG 输出: %s" % ogg_dir)

    runner = Runner(input_dir, flac_dir, ogg_dir,
                    force=args.force, embed_covers=not args.no_cover,
                    on_event=_print_event)
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
