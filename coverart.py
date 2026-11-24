# -*- coding: utf-8 -*-
"""从 QQ 音乐图片缓存里按「歌手 / 专辑」找封面（只读，不改文件）。

QQ 音乐下载加密歌曲时，封面只落在自己的图片缓存目录
（%APPDATA%\\Tencent\\QQMusic\\QQMusicCache\\QQMusicPicture\\<歌手>_<专辑>_<尺寸>.jpg），
既不写进加密文件，也不在下载目录留一份，所以列表里常常是空的。

这里只负责在缓存里挑一张最像的图给界面显示，不做任何写文件操作。
"""

from __future__ import annotations

import glob
import os

CACHE_PARTS = ("Tencent", "QQMusic", "QQMusicCache", "QQMusicPicture")
IMAGE_EXTS = (".jpg", ".jpeg", ".png")


def picture_cache_dir():
    """QQ 音乐图片缓存目录；不存在返回空串。"""
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    path = os.path.join(base, *CACHE_PARTS)
    return path if os.path.isdir(path) else ""


def _norm(text):
    """比较用：去掉所有空白并转小写。"""
    return "".join((text or "").split()).lower()


def _jpeg_size(data):
    """读 JPEG 的 SOF 段拿宽高；解析失败返回 (0, 0)。"""
    pos = 2
    total = len(data)
    while pos + 9 < total:
        if data[pos] != 0xFF:
            pos += 1
            continue
        marker = data[pos + 1]
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            pos += 2
            continue
        length = int.from_bytes(data[pos + 2:pos + 4], "big")
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                      0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            if pos + 9 <= total:
                height = int.from_bytes(data[pos + 5:pos + 7], "big")
                width = int.from_bytes(data[pos + 7:pos + 9], "big")
                return width, height
            return 0, 0
        pos += 2 + length
    return 0, 0


def _png_size(data):
    if len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n":
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    return 0, 0


def _image_size(path):
    try:
        with open(path, "rb") as fh:
            data = fh.read(64 * 1024)
    except OSError:
        return 0, 0
    if data[:2] == b"\xff\xd8":
        return _jpeg_size(data)
    return _png_size(data)


def find_cover(artist, album):
    """按歌手 + 专辑在缓存里找封面，返回最合适的一张路径；找不到返回空串。

    打分：专辑名命中 3 分，歌手名再命中 +2；只有歌手命中且没有专辑名时 1 分。
    同分取分辨率高的，再同分取文件大的。
    """
    folder = picture_cache_dir()
    if not folder:
        return ""
    want_artist, want_album = _norm(artist), _norm(album)
    if not want_artist and not want_album:
        return ""

    scored = []
    for path in glob.glob(os.path.join(folder, "*")):
        if os.path.splitext(path)[1].lower() not in IMAGE_EXTS:
            continue
        name = _norm(os.path.basename(path))
        artist_hit = bool(want_artist) and want_artist[:10] in name
        album_hit = bool(want_album) and want_album in name
        if album_hit:
            score = 3 + (2 if artist_hit else 0)
        elif artist_hit and not want_album:
            score = 1
        else:
            continue
        width, height = _image_size(path)
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
        scored.append((score, width * height, size, path))

    if not scored:
        return ""
    scored.sort(key=lambda item: (-item[0], -item[1], -item[2]))
    return scored[0][3]