"""从解密后的音频数据（文件头 / 文件尾）解析元信息与内嵌封面。

设计要点：
- 只依赖标准库，不启动 ffmpeg、不联网；
- 输入是「已经解密的一段字节」，由 hook_qq_music.js 的 readHead / readTail 提供；
- 解析失败一律返回 None，不抛异常，调用方退回占位图。
"""

from __future__ import annotations

import base64
import os
import struct

FLAC_MAGIC = b"fLaC"
OGG_MAGIC = b"OggS"


class AudioInfo:
    """一首歌的元信息。cover 为原始图片字节（可能为 None）。"""

    __slots__ = ("kind", "duration", "sample_rate", "channels", "bits", "cover",
                 "cover_mime", "title", "artist", "album")

    def __init__(self):
        self.kind = ""
        self.duration = 0.0
        self.sample_rate = 0
        self.channels = 0
        self.bits = 0
        self.cover = b""
        self.cover_mime = ""
        self.title = ""
        self.artist = ""
        self.album = ""


def _comment_pairs(body):
    """解析 Vorbis comment 载荷（FLAC 块 / Ogg 注释包共用），返回 [(key, value)]。"""
    pairs = []
    try:
        (vendor_len,) = struct.unpack_from("<I", body, 0)
        pos = 4 + vendor_len
        (count,) = struct.unpack_from("<I", body, pos)
        pos += 4
        for _ in range(min(count, 512)):
            (clen,) = struct.unpack_from("<I", body, pos)
            pos += 4
            entry = body[pos:pos + clen]
            pos += clen
            key, _, value = entry.partition(b"=")
            pairs.append((key.decode("utf-8", "replace"),
                          value.decode("utf-8", "replace")))
    except Exception:
        pass
    return pairs


def _apply_tag(info, key, value):
    """把 TITLE / ARTIST / ALBUM 这类常用标签收进 info。"""
    name = (key or "").strip().upper()
    if name in ("TITLE", "TIT2"):
        info.title = info.title or value.strip()
    elif name in ("ARTIST", "TPE1"):
        info.artist = info.artist or value.strip()
    elif name in ("ALBUM", "TALB"):
        info.album = info.album or value.strip()


def _parse_flac_picture(blk: bytes):
    """解析 FLAC PICTURE 块载荷（类型 6）。返回 (mime, data) 或 None。"""
    try:
        pos = 0
        (pic_type,) = struct.unpack_from(">I", blk, pos)
        pos += 4
        (mime_len,) = struct.unpack_from(">I", blk, pos)
        pos += 4
        mime = blk[pos:pos + mime_len].decode("ascii", "replace")
        pos += mime_len
        (desc_len,) = struct.unpack_from(">I", blk, pos)
        pos += 4 + desc_len
        pos += 16  # width / height / depth / colors
        (data_len,) = struct.unpack_from(">I", blk, pos)
        pos += 4
        data = blk[pos:pos + data_len]
        if pic_type == 3 and data:
            # 类型 3 = 封面正面，优先；其他类型也收下，交给调用方决定
            return mime, data
        return mime, data
    except Exception:
        return None


def _parse_flac(data: bytes) -> AudioInfo:
    if not data.startswith(FLAC_MAGIC):
        return None
    info = AudioInfo()
    info.kind = "flac"
    pos = 4
    best = None
    total = len(data)
    while pos + 4 <= total:
        flag = data[pos]
        block_type = flag & 0x7F
        is_last = bool(flag & 0x80)
        length = int.from_bytes(data[pos + 1:pos + 4], "big")
        body = pos + 4
        end = body + length
        if block_type == 0 and length >= 34 and body + 34 <= total:
            blk = data[body:end]
            packed = int.from_bytes(blk[10:18], "big")
            info.sample_rate = (packed >> 44) & 0xFFFFF
            info.channels = ((packed >> 41) & 0x7) + 1
            info.bits = ((packed >> 36) & 0x1F) + 1
            samples = packed & 0xFFFFFFFFF
            if info.sample_rate:
                info.duration = samples / float(info.sample_rate)
        elif block_type == 4 and end <= total:
            for key, value in _comment_pairs(data[body:end]):
                _apply_tag(info, key, value)
        elif block_type == 6 and end <= total:
            pic = _parse_flac_picture(data[body:end])
            if pic and pic[1]:
                if best is None or len(pic[1]) > len(best[1]):
                    best = pic
        if is_last:
            break
        if end > total:
            # 数据没读全，但已经拿到的信息依然可用
            break
        pos = end
    if best:
        info.cover_mime, info.cover = best
    return info


def _ogg_packets(data: bytes, limit=8):
    """按页重组前几个完整的 Ogg 数据包（忽略续页拼接的边界情况）。"""
    packets = []
    pos = 0
    buf = bytearray()
    total = len(data)
    while pos + 27 <= total and len(packets) < limit:
        if data[pos:pos + 4] != OGG_MAGIC:
            nxt = data.find(OGG_MAGIC, pos + 1)
            if nxt < 0:
                break
            pos = nxt
            continue
        nsegs = data[pos + 26]
        seg_table = data[pos + 27:pos + 27 + nsegs]
        body = pos + 27 + nsegs
        if len(seg_table) < nsegs or body > total:
            break
        cur = body
        for lace in seg_table:
            if cur + lace > total:
                return packets
            buf += data[cur:cur + lace]
            cur += lace
            if lace < 255:
                packets.append(bytes(buf))
                buf = bytearray()
                if len(packets) >= limit:
                    return packets
        pos = cur
    return packets


def _parse_ogg(data: bytes) -> AudioInfo:
    if not data.startswith(OGG_MAGIC):
        return None
    packets = _ogg_packets(data, limit=4)
    if len(packets) < 2:
        return None
    info = AudioInfo()
    ident = packets[0]
    try:
        if ident.startswith(b"\x01vorbis"):
            info.kind = "ogg"
            info.channels = ident[11]
            info.sample_rate = int.from_bytes(ident[12:16], "little")
        elif ident.startswith(b"OpusHead"):
            info.kind = "opus"
            info.channels = ident[9]
            info.sample_rate = int.from_bytes(ident[12:16], "little")
        elif ident.startswith(b"\x7fFLAC"):
            info.kind = "flac"
            # Ogg FLAC：包内紧跟 fLaC 头
            inner = ident.find(FLAC_MAGIC)
            if inner >= 0:
                sub = _parse_flac(ident[inner:])
                if sub:
                    return sub
        else:
            info.kind = "ogg"
    except Exception:
        return None

    comments = packets[1]
    try:
        if comments.startswith(b"\x03vorbis"):
            body = comments[7:]
        elif comments.startswith(b"OpusTags"):
            body = comments[8:]
        else:
            return info
        best = None
        for key, value in _comment_pairs(body):
            _apply_tag(info, key, value)
            if key.upper() == "METADATA_BLOCK_PICTURE":
                try:
                    raw = base64.b64decode(value)
                except Exception:
                    continue
                pic = _parse_flac_picture(raw)
                if pic and pic[1]:
                    if best is None or len(pic[1]) > len(best[1]):
                        best = pic
        if best:
            info.cover_mime, info.cover = best
    except Exception:
        pass
    return info


def parse_head(data: bytes):
    """解析文件头。返回 AudioInfo 或 None。"""
    if not data:
        return None
    if data.startswith(FLAC_MAGIC):
        return _parse_flac(data)
    if data.startswith(OGG_MAGIC):
        return _parse_ogg(data)
    return None


def ogg_duration_from_tail(tail: bytes, sample_rate: int) -> float:
    """从文件尾部的 Ogg 页 granule 位置推算时长（秒）。"""
    if not tail or not sample_rate:
        return 0.0
    idx = tail.rfind(OGG_MAGIC)
    if idx < 0 or idx + 14 > len(tail):
        return 0.0
    try:
        (granule,) = struct.unpack_from("<Q", tail, idx + 6)
    except Exception:
        return 0.0
    if granule <= 0:
        return 0.0
    return granule / float(sample_rate)


_MP3_BITRATES_V1 = [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, None]
_MP3_BITRATES_V2 = [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, None]
_MP3_RATES = {
    3: [44100, 48000, 32000, 0],
    2: [22050, 24000, 16000, 0],
    0: [11025, 12000, 8000, 0],
}


def mp3_duration(path):
    """不解码，靠帧头（或 Xing/Info 头）算时长；失败返回 0.0。"""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            head = f.read(256 * 1024)
    except OSError:
        return 0.0
    if len(head) < 16:
        return 0.0

    offset = 0
    if head[:3] == b"ID3":
        packed = 0
        for byte in head[6:10]:
            packed = (packed << 7) | (byte & 0x7F)
        offset = 10 + packed + (10 if head[5] & 0x10 else 0)

    pos = offset
    limit = len(head) - 4
    while pos < limit:
        if head[pos] == 0xFF and (head[pos + 1] & 0xE0) == 0xE0:
            version = (head[pos + 1] >> 3) & 0x03      # 3=MPEG1 2=MPEG2 0=MPEG2.5
            layer = (head[pos + 1] >> 1) & 0x03        # 1=Layer III
            br_idx = (head[pos + 2] >> 4) & 0x0F
            sr_idx = (head[pos + 2] >> 2) & 0x03
            if layer == 1 and sr_idx != 3 and 0 < br_idx < 15:
                sample_rate = _MP3_RATES.get(version, [0, 0, 0, 0])[sr_idx]
                table = _MP3_BITRATES_V1 if version == 3 else _MP3_BITRATES_V2
                bitrate = table[br_idx]
                if sample_rate and bitrate:
                    spf = 1152 if version == 3 else 576
                    for tag in (b"Xing", b"Info"):
                        at = head.find(tag, pos, pos + 200)
                        if at >= 0:
                            flags = int.from_bytes(head[at + 4:at + 8], "big")
                            if flags & 1:
                                frames = int.from_bytes(head[at + 8:at + 12], "big")
                                if frames:
                                    return frames * spf / float(sample_rate)
                    frame_len = (144 if version == 3 else 72) * bitrate * 1000 // sample_rate
                    if frame_len > 0:
                        audio = max(0, size - offset)
                        return audio / float(frame_len) * spf / float(sample_rate)
        pos += 1
    return 0.0
