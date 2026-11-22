# -*- coding: utf-8 -*-
"""在线歌词与封面。

只依赖标准库。默认走 QQ 音乐自己的公开接口（不需要登录、不需要 key）：
    搜索   https://c.y.qq.com/soso/fcgi-bin/client_search_cp
    歌词   https://c.y.qq.com/lyric/fcgi-bin/fcg_query_lyric_new.fcg
    封面   https://y.gtimg.cn/music/photo_new/T002R300x300M000{albummid}.jpg
QQ 接口失败时退到 LRCLIB（开源歌词库，无需 key）。

对外：
    split_name(stem)              "保留 - 郭顶" -> ("保留", "郭顶")
    search(keyword, limit)        -> [Candidate]
    match(stem, duration)         -> Candidate | None
    fetch_lyric(songmid)          -> (lrc, trans)
    fetch_cover(albummid, size)   -> bytes | None
    parse_lrc(text)               -> [(毫秒, 文本)]
    LyricStore(dir)               歌词/封面的本地缓存
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import urllib.error
import urllib.parse
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
_HEADERS = {"User-Agent": UA, "Referer": "https://y.qq.com/"}
_TIMEOUT = 6.0

# 搜索结果里出现这些词，说明是另一个版本，降权
_BAD_WORDS = ("live", "演唱会", "伴奏", "片段", "dj", "翻唱", "remix", "纯音乐", "试听")
# 拆开后的单侧清理：扩展名残留、重复下载的 (1) / _1
_TAIL_EXT = re.compile(r"[_\-\s]*(mp3|flac|mflac|mgg)$", re.I)
_TAIL_DUP = re.compile(r"(?:[_\s]\(\d+\)|[（]\d+[）]|[_\s]\d+)$")


class Candidate:
    """一首搜索命中的歌。"""

    __slots__ = ("songmid", "title", "artist", "album", "albummid", "duration", "score")

    def __init__(self, songmid, title, artist, album, albummid, duration):
        self.songmid = songmid
        self.title = title
        self.artist = artist
        self.album = album
        self.albummid = albummid
        self.duration = duration          # 秒，QQ 返回的就是整数秒
        self.score = 0

    def __repr__(self):
        return "<%s - %s %ss %d分>" % (self.title, self.artist, self.duration, self.score)


def _get(url, timeout=_TIMEOUT):
    req = urllib.request.Request(url, headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _get_json(url, timeout=_TIMEOUT):
    return json.loads(_get(url, timeout).decode("utf-8", "ignore"))


_TAIL_QUALITY = re.compile(r"_(SQ|HQ|LQ|ZQ|DOLBY|ATMOS|DTS|JY|L|H|M)$")


def strip_quality_tail(text):
    """去掉歌名尾部自带的音质后缀（_H、_SQ 这类）。

    只认结尾处的下划线 + 已知代号，所以「G_E_M邓紫棋」这种名字不会被切坏；
    也可能叠了两层（歌名_H_SQ），循环剥到剥不动为止。
    """
    out = (text or "").strip()
    while True:
        cut = _TAIL_QUALITY.sub("", out)
        if cut == out:
            return out
        out = cut.strip()


def _clean_piece(text):
    """拆开后的单侧清理：音质后缀、扩展名残留、重复下载的 (1)/_1。

    不剥裸数字 ——「21」「Blink-182」这类歌名/歌手要原样保留。
    """
    out = strip_quality_tail(text)
    out = _TAIL_EXT.sub("", out)
    out = _TAIL_DUP.sub("", out)
    return out.strip() or (text or "").strip()


def split_name(stem, mode="title_artist"):
    """把文件名拆成 (歌名, 歌手)。

    mode: title_artist（歌名 - 歌手，默认）/ artist_title（歌手 - 歌名）/ title_only（整段都是歌名）
    """
    name = os.path.splitext(os.path.basename(stem))[0].strip()
    if mode == "title_only":
        return _clean_piece(name), ""
    for sep in (" - ", " – ", " — ", "-"):
        if sep not in name:
            continue
        left, right = (s.strip() for s in name.split(sep, 1))
        # 裸连字符只在两边都不像纯数字时才算分隔符，「Blink-182」不该被拆开
        if sep == "-" and (left.isdigit() or right.isdigit()):
            continue
        # 两侧都要清理：音质尾巴经常挂在歌手名后面（「歌名 - 某某_L」）
        left, right = _clean_piece(left), _clean_piece(right)
        if left and right:
            if mode == "artist_title":
                return right, left
            return left, right
    return _clean_piece(name), ""


def _norm(text):
    return re.sub(r"[\s\-_·・,，.。()（）\[\]【】'\"/&、+＋]", "", (text or "")).lower()


def _artist_tokens(artist):
    """「张信哲 _ 薛之谦」-> ['张信哲', '薛之谦']。"""
    parts = re.split(r"[_/&、,，+＋]", artist or "")
    return [p.strip() for p in parts if p and p.strip()]


def _score(cand, title, artist, duration):
    """给候选打分。时长是最硬的证据，其次歌手，再次歌名。"""
    t = _norm(title)
    ct = _norm(cand.title)
    mine_artists = [_norm(a) for a in _artist_tokens(artist)] or ([_norm(artist)] if artist else [])
    cand_artists = [_norm(a) for a in _artist_tokens(cand.artist)] or ([_norm(cand.artist)] if cand.artist else [])

    def _hit(mine, theirs):
        return any(m and c and (m == c or m in c or c in m) for m in mine for c in theirs)

    title_hit = bool(t and ct and (t == ct or t in ct or ct in t))
    artist_hit = _hit(mine_artists, cand_artists)

    score = 0
    if t and ct:
        if t == ct:
            score += 30
        elif title_hit:
            score += 15
    if mine_artists and cand_artists and artist_hit:
        score += 30

    # 时长
    if duration and cand.duration:
        diff = abs(cand.duration - float(duration))
        if diff <= 2:
            score += 45
        elif diff <= 5:
            score += 30
        elif diff <= 10:
            score += 12
        elif diff > 20:
            score -= 25

    # 版本噪音
    low = (cand.title or "").lower()
    mine = (title or "").lower()
    for word in _BAD_WORDS:
        if word in low and word not in mine:
            score -= 18
    return score


def _keywords(title, artist):
    """按可靠性排出的搜索关键词列表。"""
    tokens = _artist_tokens(artist)
    out = []
    if tokens:
        out.append("%s %s" % (title, tokens[0]))
        out.append("%s %s" % (tokens[0], title))
        if len(tokens) > 1:
            out.append("%s %s" % (title, " ".join(tokens)))
    if title:
        out.append(title)
    seen, uniq = set(), []
    for kw in out:
        kw = kw.strip()
        if kw and kw not in seen:
            seen.add(kw)
            uniq.append(kw)
    return uniq


def search(keyword, limit=6):
    """搜 QQ 音乐，返回 [Candidate]。网络异常时返回空列表。"""
    if not keyword:
        return []
    url = ("https://c.y.qq.com/soso/fcgi-bin/client_search_cp"
           "?p=1&n=%d&w=%s&format=json" % (limit, urllib.parse.quote(keyword)))
    try:
        data = _get_json(url)
    except (urllib.error.URLError, socket.timeout, ValueError, OSError):
        return []
    songs = ((data.get("data") or {}).get("song") or {}).get("list") or []
    out = []
    for item in songs:
        singers = "/".join(a.get("name", "") for a in (item.get("singer") or [])) or item.get("singer", "")
        out.append(Candidate(
            item.get("songmid") or "",
            item.get("songname") or item.get("title") or "",
            singers,
            item.get("albumname") or "",
            item.get("albummid") or "",
            item.get("interval") or 0,
        ))
    return [c for c in out if c.songmid]


def match(stem, duration=None, min_score=50):
    """按文件名 + 时长挑最像的一首。挑不到返回 None。"""
    title, artist = split_name(stem)
    best = None
    for keyword in _keywords(title, artist):
        cands = search(keyword)
        if not cands:
            continue
        for cand in cands:
            cand.score = _score(cand, title, artist, duration)
            if best is None or cand.score > best.score:
                best = cand
        if best is not None and best.score >= min_score:
            break
    if best is None or best.score < min_score:
        return None
    return best


def fetch_lyric(songmid):
    """拿歌词，返回 (lrc 文本, 翻译文本)，都没有返回 ("", "")。"""
    if not songmid:
        return "", ""
    url = ("https://c.y.qq.com/lyric/fcgi-bin/fcg_query_lyric_new.fcg"
           "?songmid=%s&format=json&nobase64=1" % urllib.parse.quote(songmid))
    try:
        data = _get_json(url)
    except (urllib.error.URLError, socket.timeout, ValueError, OSError):
        return "", ""
    if data.get("retcode") not in (0, "0"):
        return "", ""
    lrc = (data.get("lyric") or "").replace("\\n", "\n").strip()
    trans = (data.get("trans") or "").replace("\\n", "\n").strip()
    return lrc, trans


def fetch_cover(albummid, size=300):
    """按专辑 mid 拿封面 JPEG 字节，失败返回 None。"""
    if not albummid:
        return None
    url = ("https://y.gtimg.cn/music/photo_new/T002R%dx%dM000%s.jpg"
           % (size, size, albummid))
    try:
        data = _get(url, timeout=8.0)
    except (urllib.error.URLError, socket.timeout, OSError):
        return None
    return data if data[:2] == b"\xff\xd8" else None


def fetch_lyric_lrclib(title, artist, album="", duration=None):
    """LRCLIB 兜底：返回 (lrc, "")。"""
    params = {"artist_name": artist or "", "track_name": title or "", "album_name": album or ""}
    if duration:
        params["duration"] = int(round(float(duration)))
    url = "https://lrclib.net/api/get?" + urllib.parse.urlencode(params)
    try:
        data = _get_json(url, timeout=8.0)
    except (urllib.error.URLError, socket.timeout, ValueError, OSError):
        return "", ""
    lrc = (data.get("syncedLyrics") or "").strip()
    return lrc, ""


_LRC_TIME = re.compile(r"\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]")


def parse_lrc(text):
    """解析 LRC，返回按时间排序的 [(毫秒, 文本)]。支持一行多时间戳。"""
    lines = []
    for raw in (text or "").splitlines():
        stamps = list(_LRC_TIME.finditer(raw))
        if not stamps:
            continue
        content = _LRC_TIME.sub("", raw).strip()
        for m in stamps:
            minute = int(m.group(1))
            second = int(m.group(2))
            frac = m.group(3) or "0"
            milli = int(frac.ljust(3, "0")[:3])
            lines.append((minute * 60000 + second * 1000 + milli, content))
    lines.sort(key=lambda x: x[0])
    return lines


def _digest(*parts):
    key = "|".join(str(p) for p in parts if p is not None)
    return hashlib.md5(key.encode("utf-8", "ignore")).hexdigest()[:16]


class LyricStore:
    """歌词与封面的本地缓存，断网后仍可显示。"""

    def __init__(self, root):
        self.root = root
        self.lyric_dir = os.path.join(root, "lyric")
        self.cover_dir = os.path.join(root, "cover")

    def _ensure(self, path):
        try:
            os.makedirs(path, exist_ok=True)
            return True
        except OSError:
            return False

    # ---- 歌词 ----
    def lyric_key(self, title, artist, duration=None):
        return _digest(title, artist, int(duration) if duration else None)

    def read_lyric(self, key):
        """返回 (lrc, trans)；缓存没有返回 None。"""
        path = os.path.join(self.lyric_dir, key + ".lrc")
        trans_path = os.path.join(self.lyric_dir, key + ".trans")
        try:
            with open(path, "r", encoding="utf-8") as f:
                lrc = f.read()
        except OSError:
            return None
        trans = ""
        try:
            with open(trans_path, "r", encoding="utf-8") as f:
                trans = f.read()
        except OSError:
            pass
        return lrc, trans

    def write_lyric(self, key, lrc, trans=""):
        if not self._ensure(self.lyric_dir):
            return False
        try:
            with open(os.path.join(self.lyric_dir, key + ".lrc"), "w", encoding="utf-8") as f:
                f.write(lrc or "")
            if trans:
                with open(os.path.join(self.lyric_dir, key + ".trans"), "w", encoding="utf-8") as f:
                    f.write(trans)
            return True
        except OSError:
            return False

    # ---- 封面 ----
    def cover_path(self, title, artist):
        key = _digest(title, artist)
        path = os.path.join(self.cover_dir, key + ".jpg")
        return path if os.path.exists(path) else None

    def write_cover(self, title, artist, data):
        if not data or not self._ensure(self.cover_dir):
            return None
        path = os.path.join(self.cover_dir, _digest(title, artist) + ".jpg")
        try:
            with open(path, "wb") as f:
                f.write(data)
            return path
        except OSError:
            return None


def default_cache_dir():
    """%LOCALAPPDATA%\\TuneFree\\cache，取不到就退到用户目录。"""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "TuneFree", "cache")


def load_for(stem, duration, store=None, allow_network=True):
    """一次把封面和歌词都准备好。

    返回 dict：{"cover": 路径或 None, "lrc": 文本, "trans": 文本, "hit": 是否用了缓存}
    """
    store = store or LyricStore(default_cache_dir())
    title, artist = split_name(stem)
    result = {"cover": None, "lrc": "", "trans": "", "hit": False, "matched": None}

    cached_cover = store.cover_path(title, artist)
    if cached_cover:
        result["cover"] = cached_cover

    key = store.lyric_key(title, artist, duration)
    cached = store.read_lyric(key)
    if cached is not None:
        result["lrc"], result["trans"] = cached
        result["hit"] = True

    if not allow_network and result["lrc"] and result["cover"]:
        return result

    # 缓存不全时才动网
    need_lyric = not result["lrc"]
    need_cover = result["cover"] is None
    if not (need_lyric or need_cover):
        return result

    cand = match(stem, duration)
    if cand is None:
        return result
    result["matched"] = cand

    if need_lyric:
        lrc, trans = fetch_lyric(cand.songmid)
        if not lrc:
            lrc, trans = fetch_lyric_lrclib(cand.title, cand.artist, cand.album, cand.duration)
        if lrc:
            result["lrc"], result["trans"] = lrc, trans
            store.write_lyric(key, lrc, trans)

    if need_cover and cand.albummid:
        data = fetch_cover(cand.albummid)
        path = store.write_cover(title, artist, data)
        if path:
            result["cover"] = path

    return result