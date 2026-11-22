# -*- coding: utf-8 -*-
"""从 QQ 音乐图片缓存里找封面，并写进解密出来的音频文件。

背景：QQ 音乐下载加密歌曲（.mflac / .mgg）时，封面只落在自己的图片缓存
（QQMusicCache\\QQMusicPicture\\<歌手>_<专辑>_<尺寸>.jpg），既不写进加密文件，
也不会在下载目录里留一份。所以「同时下载专辑图片」勾了也看不到图。

这里在解密完成后做三件事：
1. 看解出来的文件里有没有封面（FLAC 的 PICTURE 块 / Ogg 的 METADATA_BLOCK_PICTURE）；
2. 没有就按「歌手 + 专辑」从缓存里挑分辨率最高的一张；
3. 写回文件：FLAC 写 PICTURE 块，OGG / Opus 写 base64 编码的 METADATA_BLOCK_PICTURE。

只改元数据，音频字节不动；FLAC 的 SEEKTABLE 偏移是相对第一帧的，
元数据变长不需要跟着改。
"""

from __future__ import annotations

import base64
import glob
import logging
import os

CACHE_PARTS = ("Tencent", "QQMusic", "QQMusicCache", "QQMusicPicture")
IMAGE_EXTS = (".jpg", ".jpeg", ".png")

_COVER_TAG = "metadata_block_picture"


# -------------------- 缓存位置 --------------------
def picture_cache_dir():
    """QQ 音乐图片缓存目录；拿不到 APPDATA 时返回空串。"""
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    path = os.path.join(base, *CACHE_PARTS)
    return path if os.path.isdir(path) else ""


# -------------------- 小工具 --------------------
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


# -------------------- 找封面 --------------------
def find_cover(artist, album):
    """按歌手 + 专辑在缓存里找封面，返回最合适的一张路径；找不到返回空串。

    打分：专辑名命中 +3，歌手名命中再加分；同分取分辨率高的。
    专辑名缺失时，只要歌手前缀唯一命中也可以兜底。
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


# -------------------- 读文件里的标签 --------------------
def read_tags(path):
    """从音频文件里读 artist / album / title，读不到就给空串。"""
    out = {"artist": "", "album": "", "title": ""}
    try:
        import mutagen
        audio = mutagen.File(path)
    except Exception:
        return out
    if audio is None or getattr(audio, "tags", None) is None:
        return out
    tags = audio.tags
    for key in ("artist", "album", "title"):
        try:
            value = tags.get(key)
        except Exception:
            value = None
        if isinstance(value, (list, tuple)):
            value = value[0] if value else ""
        if value:
            out[key] = str(value)
    return out


def _audio_kind(path):
    ext = os.path.splitext(path)[1].lower()
    return ext.lstrip(".")


def has_cover(path):
    """文件里是不是已经有封面了。"""
    kind = _audio_kind(path)
    try:
        if kind == "flac":
            from mutagen.flac import FLAC
            return bool(FLAC(path).pictures)
        import mutagen
        audio = mutagen.File(path)
        if audio is None or getattr(audio, "tags", None) is None:
            return False
        return any(str(key).lower() == _COVER_TAG for key in audio.tags.keys())
    except Exception:
        return False


# -------------------- 写封面 --------------------
def _picture_block(data, mime):
    """拼一个 FLAC PICTURE 块；Ogg 侧也复用同一份格式。"""
    from mutagen.flac import Picture
    pic = Picture()
    pic.type = 3            # 3 = front cover
    pic.mime = mime
    pic.desc = ""
    pic.data = data
    return pic


def embed_cover(path, image_path):
    """把图片写进音频文件；已经有封面就不动。返回 True 表示这次真的写了。"""
    if not os.path.isfile(image_path):
        return False
    with open(image_path, "rb") as fh:
        data = fh.read()
    if not data:
        return False
    mime = "image/png" if image_path.lower().endswith(".png") else "image/jpeg"
    pic = _picture_block(data, mime)
    kind = _audio_kind(path)

    if kind == "flac":
        from mutagen.flac import FLAC
        audio = FLAC(path)
        if audio.pictures:
            return False
        audio.add_picture(pic)
        audio.save()
        return True

    import mutagen
    audio = mutagen.File(path)
    if audio is None:
        return False
    tags = getattr(audio, "tags", None)
    if tags is None:
        return False
    if any(str(key).lower() == _COVER_TAG for key in tags.keys()):
        return False
    audio[_COVER_TAG] = [base64.b64encode(pic.write()).decode("ascii")]
    audio.save()
    return True


def ensure_cover(path, artist="", album="", title=""):
    """没有封面就从缓存补一张。

    返回状态字符串，方便调用方决定日志/提示：
        has-cover / embedded / no-cache / no-meta / missing-file / error
    """
    if not os.path.isfile(path):
        return "missing-file"
    if has_cover(path):
        return "has-cover"

    tags = read_tags(path)
    artist = tags.get("artist") or artist
    album = tags.get("album") or album
    title = tags.get("title") or title
    if not artist and not album:
        return "no-meta"

    cover = find_cover(artist, album)
    if not cover:
        logging.info("没有在 QQ 音乐缓存里找到封面：%s / %s", artist or "?", album or "?")
        return "no-cache"

    try:
        if embed_cover(path, cover):
            logging.info("已补封面：%s ← %s", os.path.basename(path), os.path.basename(cover))
            return "embedded"
        return "has-cover"
    except Exception as e:      # 写封面失败不该影响解密结果
        logging.warning("写封面失败：%s（%s）", os.path.basename(path), e)
        return "error"
