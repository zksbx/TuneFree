"""扫描输入目录：列出所有加密音频，并尽量补上时长与封面。

封面来自文件内嵌的图片（FLAC PICTURE 块 / Ogg METADATA_BLOCK_PICTURE），
所以既能显示得像 QQ 音乐，又不需要联网。

没有连上 QQ音乐时也能扫描：只列文件名和大小，状态标成「未连接」，
连上之后重新扫描一次就能补齐时长和封面。
"""

from __future__ import annotations

import hashlib
import logging
import os

import lyrics
import metainfo

ENCRYPTED_EXTS = (".mflac", ".mgg")

# 读多少字节去解析元信息：绝大多数封面都在 256KB 以内，不够再补读到 1.5MB
_FIRST_READ = 256 * 1024
_SECOND_READ = 1536 * 1024


class Track:
    __slots__ = ("index", "path", "name", "ext", "size", "kind", "duration",
                 "sample_rate", "channels", "bits", "cover_path", "status", "message",
                 "title", "artist", "plain_ext", "output", "output_size", "is_out",
                 "out_kind", "dupes")

    def __init__(self, path, index=0, name_format="title_artist"):
        self.index = index
        self.path = str(path)
        self.name = os.path.splitext(os.path.basename(self.path))[0]
        self.ext = os.path.splitext(self.path)[1].lower()
        self.size = 0
        self.kind = "flac" if self.ext == ".mflac" else "ogg"
        self.duration = 0.0
        self.sample_rate = 0
        self.channels = 0
        self.bits = 0
        self.cover_path = ""
        self.status = "pending"      # pending / ready / working / done / failed / skipped
        self.message = ""
        self.title, self.artist = lyrics.split_name(self.name, name_format)
        self.plain_ext = ".flac" if self.kind == "flac" else ".ogg"
        self.output = ""
        self.output_size = 0
        self.is_out = False
        self.out_kind = ""           # 输出行才有：mp3 / flac
        self.dupes = 0


def find_encrypted(input_dir):
    """递归找出所有 .mflac / .mgg，按路径排序（稳定，方便对照）。"""
    found = []
    for root, _dirs, files in os.walk(input_dir):
        for name in files:
            if os.path.splitext(name)[1].lower() in ENCRYPTED_EXTS:
                found.append(os.path.join(root, name))
    found.sort(key=lambda p: p.lower())
    return found


# QQ 音乐下不同音质会在文件名后加后缀：_SQ / _HQ / _LQ / _H / _M ...
_QUALITY_SUFFIX = ("_dolby", "_atmos", "_sq", "_hq", "_lq", "_zq", "_h", "_m")


def base_stem(name):
    """去掉音质后缀的花名：「保留 - 郭顶_H」→「保留 - 郭顶」。"""
    text = (name or "").lower()
    while True:
        for suffix in _QUALITY_SUFFIX:
            if text.endswith(suffix) and len(text) > len(suffix):
                text = text[:-len(suffix)]
                break
        else:
            return text


def dedupe(tracks):
    """同一首歌下过多个音质时只留一份：优先无损 .mflac，其次文件更大的。"""
    kept = {}
    order = []
    for track in tracks:
        key = base_stem(track.name)
        old = kept.get(key)
        if old is None:
            kept[key] = track
            order.append(key)
            continue
        if (track.kind == "flac", track.size) > (old.kind == "flac", old.size):
            track.dupes = old.dupes + 1
            kept[key] = track
        else:
            old.dupes += 1
    result = [kept[key] for key in order]
    for track in result:
        if track.dupes and not track.message:
            track.message = "另有 %d 个低音质副本，已忽略" % track.dupes
    return result


def _cover_cache_path(cover_dir, src_path, mtime):
    key = f"{os.path.abspath(src_path)}|{int(mtime)}".encode("utf-8", "replace")
    digest = hashlib.md5(key).hexdigest()[:16]
    return os.path.join(cover_dir, f"c{digest}.img")


def _save_cover(cover_dir, src_path, mtime, data, mime):
    if not data:
        return ""
    try:
        os.makedirs(cover_dir, exist_ok=True)
        target = _cover_cache_path(cover_dir, src_path, mtime)
        if not os.path.exists(target):
            with open(target, "wb") as f:
                f.write(data)
        return target
    except OSError as e:
        logging.debug("封面缓存写入失败: %s", e)
        return ""


def fill_meta(track, session, cover_dir):
    """读文件头/尾补上时长与封面。失败只记 message，不抛异常。"""
    try:
        mtime = os.path.getmtime(track.path)
    except OSError:
        mtime = 0

    head = session.read_head(track.path, _FIRST_READ)
    info = metainfo.parse_head(head)
    if info is not None and not info.cover and track.size > _FIRST_READ:
        bigger = session.read_head(track.path, min(track.size, _SECOND_READ))
        info2 = metainfo.parse_head(bigger)
        if info2 is not None and info2.cover:
            info = info2

    if info is None:
        track.status = "failed"
        track.message = "读不出音频信息，文件可能没下载完整"
        return track

    track.kind = info.kind or track.kind
    track.plain_ext = ".flac" if track.kind == "flac" else ".ogg"
    track.duration = info.duration
    track.sample_rate = info.sample_rate
    track.channels = info.channels
    track.bits = info.bits
    if info.cover:
        track.cover_path = _save_cover(cover_dir, track.path, mtime, info.cover, info.cover_mime)

    if not track.duration and info.kind in ("ogg", "opus") and info.sample_rate:
        try:
            tail = session.read_tail(track.path, 64 * 1024)
            track.duration = metainfo.ogg_duration_from_tail(tail, info.sample_rate)
        except Exception:
            pass

    track.status = "ready"
    return track


def scan(input_dir, session=None, cover_dir=None, on_item=None, on_progress=None,
         dedupe_mode=True, name_format="title_artist"):
    """扫描目录，返回 Track 列表。

    on_item(track)      —— 每扫完一首（含失败）回调一次，界面可以逐行刷新
    on_progress(done, total) —— 进度
    """
    paths = find_encrypted(input_dir)
    total = len(paths)
    tracks = []
    can_read = session is not None and getattr(session, "connected", False)

    for idx, path in enumerate(paths, 1):
        track = Track(path, index=idx, name_format=name_format)
        try:
            track.size = os.path.getsize(path)
        except OSError:
            track.size = 0

        if can_read:
            try:
                fill_meta(track, session, cover_dir or "")
            except Exception as e:
                track.status = "failed"
                track.message = str(e)
                logging.warning("读取 %s 的信息失败: %s", track.name, e)
        else:
            track.status = "pending"
            track.message = "未连接 QQ音乐，暂时只显示文件名"

        tracks.append(track)
        if on_item is not None:
            try:
                on_item(track)
            except Exception:
                logging.debug("扫描回调出错", exc_info=True)
        if on_progress is not None:
            try:
                on_progress(idx, total)
            except Exception:
                logging.debug("进度回调出错", exc_info=True)

    return dedupe(tracks) if dedupe_mode else tracks