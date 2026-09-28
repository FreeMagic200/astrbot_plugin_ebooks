import asyncio
import base64
import difflib
import html
import io
import os
import re
import shutil
import time
import unicodedata
import uuid
from pathlib import Path
from urllib.parse import unquote, urlsplit, urlunsplit

import aiohttp
from astrbot.api.all import Nodes
from PIL import Image as Img
from bs4 import BeautifulSoup


_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def get_proxy() -> str:
    """Pick up proxy from common env var names (lower/upper/ALL_PROXY)."""
    for key in ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY"):
        v = os.environ.get(key)
        if v:
            return v
    return None


_BUILTIN_FILTER_KEYWORDS_FILE = os.path.join(os.path.dirname(__file__), "_filter_keywords.txt")
_CJK_RE = r"[㐀-鿿豈-﫿＀-￯\w]"  # CJK 扩展 A + 基本 + 兼容汉字 + 全角 + ASCII word
_BUILTIN_KW_CACHE: list[str] | None = None


def _load_builtin_keywords() -> list[str]:
    global _BUILTIN_KW_CACHE
    if _BUILTIN_KW_CACHE is None:
        try:
            with open(_BUILTIN_FILTER_KEYWORDS_FILE, encoding="utf-8") as f:
                _BUILTIN_KW_CACHE = [
                    line.strip() for line in f if line.strip() and len(line.strip()) >= 2
                ]
        except FileNotFoundError:
            _BUILTIN_KW_CACHE = []
            try:
                from astrbot.api.all import logger
                logger.warning(f"[ebooks] 内置禁词文件未找到: {_BUILTIN_FILTER_KEYWORDS_FILE}")
            except Exception:
                pass
    return _BUILTIN_KW_CACHE


# 纯英文/数字关键词允许带的词形变化后缀（porns / fucking / narcotics）。
_ASCII_KW_RE = re.compile(r"[A-Za-z0-9]+")
_ASCII_INFLECTIONS = r"(?:s|es|ed|ing|er|ers)?"


def _compile_or_none(pattern: str):
    try:
        return re.compile(pattern)
    except re.error:
        return None


def _build_matchers(keywords: list[str]):
    """Three-tier matcher. Single chars dropped.

    - 2-char keywords: whole-word match (avoid '出版' in '人民出版社').
    - 3+ char pure ASCII keywords: whole English word, plus plural/tense suffixes.
      Substring matching made 'anal' hit every 'analysis' — 70 of 100 zlib
      results for "data analysis" were dropped.
    - Other 3+ char keywords: substring match (let '法轮功' hit '法轮功资料').

    Returns (long_substr_set, short_pattern_or_None, word_pattern_or_None).
    """
    cleaned = {(kw or "").strip() for kw in keywords if (kw or "").strip()}
    short_set = {k for k in cleaned if len(k) == 2}
    word_set = {k for k in cleaned if len(k) >= 3 and _ASCII_KW_RE.fullmatch(k)}
    long_set = {k for k in cleaned if len(k) >= 3} - word_set
    short_pattern = word_pattern = None
    if short_set:
        inner = "|".join(re.escape(k) for k in sorted(short_set))
        short_pattern = _compile_or_none(rf"(?<!{_CJK_RE})(?:{inner})(?!{_CJK_RE})")
    if word_set:
        inner = "|".join(re.escape(k) for k in sorted(word_set, key=len, reverse=True))
        word_pattern = _compile_or_none(rf"(?<![A-Za-z0-9])(?:{inner}){_ASCII_INFLECTIONS}(?![A-Za-z0-9])")
    return long_set, short_pattern, word_pattern


def make_safety_checker(context, plugin_config) -> "callable | None":
    """Return a (text: str) -> bool checker for filtering search results.

    Strategy:
      - Combine BUILTIN default keywords (from _filter_keywords.txt) with
        user's `extra_filter_keywords` from plugin config. User entries
        AUGMENT defaults, they never override them.
      - Use whole-word matching (CJK-aware boundary) to avoid the catastrophic
        false-positives that astrbot's bare `re.search` produces on search-result
        text (e.g. '出' matching '出版社', '楼' matching '红楼梦').
      - Single-character keywords are SKIPPED (always wrong for search-result use).
      - We deliberately DO NOT reuse astrbot's global content_safety strategies
        here, because the global keyword list is designed for user-input / LLM-output
        moderation and routinely contains 1-2 char regex traps that destroy
        search result quality. Global moderation still protects LLM flows.

    Returns None when the merged keyword set is empty or filtering is off.
    """
    if not plugin_config.get("enable_content_filter", True):
        return None

    builtin = (
        _load_builtin_keywords()
        if plugin_config.get("enable_builtin_filter_keywords", True)
        else []
    )
    user_extra = plugin_config.get("extra_filter_keywords", []) or []
    raw = [k.strip() for k in (list(builtin) + list(user_extra)) if k and k.strip() and len(k.strip()) >= 2]
    # 简繁互转扩充：让简体 keyword 也能命中繁体文本，反之亦然
    try:
        import zhconv  # type: ignore
        expanded = set(raw)
        for k in raw:
            expanded.add(zhconv.convert(k, "zh-hant"))
            expanded.add(zhconv.convert(k, "zh-hans"))
        all_kws = list(expanded)
    except ImportError:
        try:
            from astrbot.api.all import logger
            logger.warning("[ebooks] zhconv 未安装，禁词过滤无法做简繁等价匹配。建议 pip install zhconv。")
        except Exception:
            pass
        all_kws = list(set(raw))

    if not all_kws:
        return None

    long_set, short_pattern, word_pattern = _build_matchers(all_kws)
    patterns = [p for p in (short_pattern, word_pattern) if p is not None]
    if not long_set and not patterns:
        return None

    def _check(text: str) -> bool:
        if not text:
            return True
        for kw in long_set:
            if kw in text:
                return False
        return not any(p.search(text) for p in patterns)

    return _check


def filter_unsafe(items, checker, *, fields, source: str = "") -> list:
    """Filter out items whose joined text fields fail safety check.

    Args:
        items: iterable of objects/dicts.
        checker: (text)->bool, e.g. from make_safety_checker. If None, return items unchanged.
        fields: list of attribute names (objects) or keys (dicts) to concat for the check.
        source: short name for log line.
    """
    if checker is None:
        return list(items)
    kept = []
    dropped = 0
    for item in items:
        parts = []
        for f in fields:
            v = item.get(f) if isinstance(item, dict) else getattr(item, f, None)
            if v:
                parts.append(str(v))
        if checker(" ".join(parts)):
            kept.append(item)
        else:
            dropped += 1
    if dropped:
        try:
            from astrbot.api.all import logger
        except Exception:
            import logging
            logger = logging.getLogger("ebooks")
        logger.info(f"[{source}] 内容安全过滤丢弃 {dropped} 条 (剩 {len(kept)})")
    return kept


_MATCH_STRIP_RE = re.compile(r"[^\w]+", re.UNICODE)


def normalize_match_text(s) -> str:
    """归一化用于相关性匹配的文本：NFKC 折叠全角/兼容字符、casefold、剥掉所有非词字符。

    Why: 书源返回的标题常带《》、：、—、空格等标点，用户查询不带；
    「生态植保理论技术与实践」对不上「生态植保：理论、技术与实践」。
    统一剥掉标点后比较，子串/前缀匹配才不会被一个书名号打散。
    """
    if not s:
        return ""
    return _MATCH_STRIP_RE.sub("", unicodedata.normalize("NFKC", str(s)).casefold())


def match_score(query_norm: str, field_norm: str) -> int:
    """query 在字段里的加权覆盖度，越大越相关；0 表示没有任何 ≥2 字符的片段命中。

    按匹配块在 query 中的起始位置加权（size * (len(query) - start)）：
    中文书名的学科词都在前面（「植物保护…」「生理学…」），命中 query 前缀的
    书比只命中尾巴的更接近用户要找的那本。单字符块不计分——对中文来说一个
    孤立的「学/的/版」没有任何区分度。
    """
    if not query_norm or not field_norm:
        return 0
    n = len(query_norm)
    matcher = difflib.SequenceMatcher(a=query_norm, b=field_norm, autojunk=False)
    return sum(
        size * (n - start)
        for start, _, size in matcher.get_matching_blocks()
        if size >= 2
    )


# ---------------------------------------------------------------- 版次
_EN_ORDINAL_WORDS = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7,
    "eighth": 8, "ninth": 9, "tenth": 10, "eleventh": 11, "twelfth": 12, "thirteenth": 13,
    "fourteenth": 14, "fifteenth": 15, "sixteenth": 16, "seventeenth": 17, "eighteenth": 18,
    "nineteenth": 19, "twentieth": 20,
}
_CN_DIGITS = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_WORD_ORDINALS = "|".join(_EN_ORDINAL_WORDS)
_ORDINAL = rf"\d{{1,2}}(?:st|nd|rd|th)?|{_WORD_ORDINALS}"
_A = r"(?<![A-Za-z0-9])"   # 英文词左边界（CJK 相邻也算边界）
_Z = r"(?![A-Za-z0-9])"
# 按「标题/查询里明确写了是版次」的写法逐个试；group n 是版次号。
_EDITION_PHRASE_RES = [
    re.compile(rf"{_A}(?P<n>{_ORDINAL})[\s-]*(?:edition|edn|ed){_Z}\.?", re.I),   # 8th edition / 8 ed. / eighth edition
    re.compile(rf"{_A}editions?[\s:]*(?:no\.?\s*)?(?P<n>\d{{1,2}}){_Z}", re.I),   # Edition 8
    re.compile(rf"{_A}(?P<n>\d{{1,2}})e{_Z}", re.I),                               # 8e
    re.compile(r"第\s*(?P<n>\d{1,2}|[一二三四五六七八九十两]{1,3})\s*版"),         # 第8版 / 第八版
    re.compile(r"(?<![0-9])(?P<n>\d{1,2})\s*版"),                                  # 8版
]
# 查询末尾的孤立序数词（「fundamentals of biostatistics 8th」）也按版次理解；
# 标题里不这么认——「17th Century Europe」不是第 17 版。
_QUERY_TAIL_ORDINAL_RE = re.compile(rf"{_A}(?P<n>\d{{1,2}}(?:st|nd|rd|th)|{_WORD_ORDINALS})\s*$", re.I)
# 版次字段本身（zlib 的 edition：「7」「5th ed.」「First Edition」）：开头的序数就是版次。
_LOOSE_EDITION_RE = re.compile(rf"^\s*(?P<n>{_ORDINAL}){_Z}", re.I)
_EDITION_WORD_RE = re.compile(rf"-?{_A}editions?{_Z}", re.I)


def _edition_number(token: str):
    token = (token or "").strip().lower()
    m = re.fullmatch(r"(\d{1,2})(?:st|nd|rd|th)?", token)
    if m:
        n = int(m.group(1))
    elif token in _EN_ORDINAL_WORDS:
        n = _EN_ORDINAL_WORDS[token]
    elif token and all(c in _CN_DIGITS or c == "十" for c in token):
        tens, sep, ones = token.partition("十")
        if sep:
            n = (_CN_DIGITS.get(tens, 0) if tens else 1) * 10 + (_CN_DIGITS.get(ones, 0) if ones else 0)
        else:
            n = _CN_DIGITS.get(token, 0) if len(token) == 1 else 0
    else:
        return None
    return n if 0 < n < 100 else None


def parse_edition(text, *, loose: bool = False):
    """从文本里认出版次号（1-99），认不出返回 None。

    loose=True 用于版次字段本身：「7」「5th ed.」「First Edition」开头的序数即版次；
    4 位数（有些书把年份填进了版次字段）不算。标题/查询只认明确的版次写法。
    """
    s = _as_str(text)
    if not s:
        return None
    if loose:
        m = _LOOSE_EDITION_RE.match(s)
        if m:
            return _edition_number(m.group("n"))
    for pat in _EDITION_PHRASE_RES:
        m = pat.search(s)
        if m:
            n = _edition_number(m.group("n"))
            if n:
                return n
    return None


def split_edition_query(query) -> tuple:
    """拆出查询里的版次，返回 (engine_query, core_query, edition)。

    - engine_query：发给 Z-Library 的查询，只去掉英文单词 edition。Z-Library 把它
      当标题必含词，而版次存在单独字段里、标题常常不带：「fundamentals of
      biostatistics 7th edition」反而搜不到第 7 版，去掉 edition 后排第 2。
      序数（8th/eighth/8e）和中文「第9版」留着——实测它们只会帮上游把对的版次排前面。
    - core_query：去掉整段版次写法后的书名部分，用来判断「是不是这本书」。
    - edition：版次号，查询里没提版次则为 None。
    """
    q = " ".join(_as_str(query).split())
    edition, core = None, q
    for pat in (*_EDITION_PHRASE_RES, _QUERY_TAIL_ORDINAL_RE):
        m = pat.search(core)
        if not m:
            continue
        n = _edition_number(m.group("n"))
        if n:
            edition = edition or n
            core = f"{core[:m.start()]} {core[m.end():]}"
    core = " ".join(core.split()) or q
    engine = " ".join(_EDITION_WORD_RE.sub(" ", q).split()) or q
    return engine, core, edition


def book_edition(book):
    """书的版次号：先看版次字段，再看标题里的版次写法。"""
    return parse_edition(book.get("edition"), loose=True) or parse_edition(book.get("title"))


def edition_label(book) -> str:
    """展示/喂给 rerank 用的版次文本；没有有效版次返回空串。

    统一写成「第 N 版」：实测 Qwen3-Reranker 对「8th edition」和「第8版」两种
    提问都能据此把第 8 版排到第 7 版前面，不带版次时两版同分。
    """
    n = book_edition(book)
    if n:
        return f"第 {n} 版"
    raw = _as_str(book.get("edition"))
    if not raw or raw.lower() == "none" or re.fullmatch(r"\d{4}", raw):
        return ""  # 空值或误填的年份
    return raw[:30]


# ---------------------------------------------------------------- ISBN
_ISBN_RE = re.compile(r"\d{9}[\dX]|\d{13}")


def isbn_set(identifier) -> frozenset:
    """zlib 的 identifier（「9781593276034,1593276036」，偶尔混着 ASIN）里的 ISBN 集合。"""
    out = set()
    for token in re.split(r"[,;\s]+", _as_str(identifier)):
        token = token.replace("-", "").upper()
        if _ISBN_RE.fullmatch(token):
            out.add(token)
    return frozenset(out)


# ---------------------------------------------------------------- 简介 HTML → 文本
# Z-Library 一半以上的简介带 HTML：实测 576 条里 314 条，<br> 4137 处、<p> 1563 处，
# 其次是 li/span/i/em/b/ul，外加 &nbsp; &amp; &lt; 等转义。
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
_HTML_LIST_ITEM_RE = re.compile(r"<\s*li\b[^>]*>", re.I)
_HTML_BREAK_RE = re.compile(r"<\s*(?:br|/?p|/?div|/?li|/?ul|/?ol|/?h[1-6]|/?tr|/?blockquote|/?section)\b[^>]*>", re.I)
_HTML_CELL_RE = re.compile(r"<\s*/?t[dh]\b[^>]*>", re.I)
_HTML_TAG_RE = re.compile(r"<\s*/?\s*[A-Za-z][^>]*>")


def html_to_text(text) -> str:
    """把简介里的 HTML 转成纯文本，保留分行。

    - <br>、<p>、<div>、标题、列表等换行，列表项前加「· 」；
    - <b>、<span>、<i> 等行内标签直接去掉、不插空格——插了空格，「生物<b>化学</b>」
      就成了「生物 化学」，关键词匹配和内容过滤都会漏；
    - &nbsp; &amp; &lt; 等转义解码（先去标签再解码，正文里的「&lt;b&gt;」不会被当成标签删掉）；
    - 每行内的空白压成一个空格，去掉空行。纯文本原样进来、原样出去（只做空白整理）。
    """
    s = _as_str(text)
    if not s:
        return ""
    if "<" in s:
        s = _HTML_COMMENT_RE.sub("", s)
        s = _HTML_LIST_ITEM_RE.sub("\n· ", s)
        s = _HTML_BREAK_RE.sub("\n", s)
        s = _HTML_CELL_RE.sub(" ", s)
        s = _HTML_TAG_RE.sub("", s)
    s = html.unescape(s)
    lines = (" ".join(line.split()) for line in s.splitlines())
    return "\n".join(line for line in lines if line)


# ---------------------------------------------------------------- 简介摘录
_CJK_CHAR_RE = re.compile(r"[㐀-鿿豈-﫿]")


def _query_hits(text_lower: str, query) -> list:
    """查询词在文本里的命中区间 [(start, end)]；text_lower 须已 lower()。

    按空格分词逐个找整词。一个整词都没命中时，才对没空格的中文长查询
    （「生物化学糖酵解」）退回找公共片段，且只留最长的片段——否则「生物」这种
    碎片会分走摘录名额。英文词短于 3 个字母的（of/in）不算，到处都是。
    """
    hits, fragments = [], []
    for token in _as_str(query).lower().split():
        has_cjk = bool(_CJK_CHAR_RE.search(token))
        if len(token) < (2 if has_cjk else 3):
            continue
        pos = text_lower.find(token)
        if pos >= 0:
            hits.append((pos, pos + len(token)))
        elif has_cjk and len(token) >= 3:
            matcher = difflib.SequenceMatcher(a=token, b=text_lower, autojunk=False)
            fragments.extend(
                (b, b + size) for a, b, size in matcher.get_matching_blocks()
                if size >= 2 and _CJK_CHAR_RE.search(token[a:a + size])
            )
    if hits or not fragments:
        return hits
    longest = max(end - start for start, end in fragments)
    return [(start, end) for start, end in fragments if end - start == longest]


def excerpt_for_query(text, query, max_chars: int = 150, head_chars: int = 60, context: int = 30) -> str:
    """简介摘录，约 max_chars 字：开头一段 + 查询在开头之外的命中片段。

    Why: 目录/简介里的「糖酵解」常在几百上千字之后（实测 223–1589 字），只截开头
    的话 rerank 模型和用户都看不到。把 rerank 文本整体加长也不划算：Qwen3-Reranker
    80 条 × 300 字 1.1 秒、× 1500 字 3.8 秒，还是够不着更靠后的命中。
    命中都在开头 max_chars 字以内（或根本没命中）时，退化成原来的截开头。
    """
    s = html_to_text(text)
    if len(s) <= max_chars:
        return s
    head_end = min(head_chars, max_chars)
    hits = [h for h in _query_hits(s.lower(), query) if h[1] > head_end]
    if not hits or all(end <= max_chars for _, end in hits):
        return s[:max_chars] + "…"

    hits.sort(key=lambda h: (-(h[1] - h[0]), h[0]))  # 长的命中优先占预算
    budget = max_chars - head_end
    if len({s[a:b].lower() for a, b in hits}) == 1:
        context = max(context, (budget - 1 - (hits[0][1] - hits[0][0])) // 2)  # 只有一种命中：名额全给它
    windows = []
    for start, end in hits:
        if any(ws <= start and end <= we for ws, we in windows):
            continue
        width = min(budget - 1, (end - start) + 2 * context)  # -1 留给片段前的省略号
        if width < end - start:
            break
        ws = max(head_end, start - (width - (end - start)) // 2)
        while ws < start and s[ws - 1].isascii() and s[ws - 1].isalnum() and s[ws].isalnum():
            ws += 1  # 别从英文单词中间切开
        we = min(len(s), ws + width)
        windows.append((ws, we))
        budget -= (we - ws) + 1
        if budget <= 10:
            break

    merged = []
    for ws, we in sorted(windows):
        if merged and ws <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], we))
        else:
            merged.append((ws, we))
    out, prev = s[:head_end], head_end
    for ws, we in merged:
        out += ("…" if ws > prev else "") + s[ws:we]
        prev = we
    return out + ("…" if prev < len(s) else "")


async def is_url_accessible(url: str, proxy: str = None) -> bool:
    """Check whether a URL's host is reachable.

    Any HTTP response (incl. 403/503 from Cloudflare challenges) counts as
    reachable — downstream code uses curl_cffi to bypass CF. Only
    connection-level failures (DNS/TLS/timeout) return False.
    """
    headers = {"User-Agent": _BROWSER_UA}
    try:
        async with aiohttp.ClientSession() as session:
            try:
                async with session.head(
                    url,
                    timeout=10,
                    proxy=proxy,
                    allow_redirects=True,
                    headers=headers,
                ) as response:
                    if response.status < 600:
                        return True
            except Exception:
                pass
            async with session.get(
                url,
                timeout=10,
                proxy=proxy,
                allow_redirects=True,
                headers=headers,
            ) as response:
                return response.status < 600
    except Exception:
        return False


def compress_cover_bytes(data: bytes, max_edge: int = 400, quality: int = 75) -> bytes:
    """Resize + re-encode as JPEG to shrink cover payload.

    Returns the input unchanged on any error or when max_edge<=0. Used to
    keep QQ forward msg payload small enough to avoid the 180s
    send_private_forward_msg upload timeout (see merge_forward docs).
    """
    if not data or max_edge is None or max_edge <= 0:
        return data
    try:
        img = Img.open(io.BytesIO(data))
        img.load()
        if img.mode in ("RGBA", "LA"):
            bg = Img.new("RGB", img.size, (255, 255, 255))
            bg.paste(img, mask=img.split()[-1])
            img = bg
        elif img.mode == "P":
            img = img.convert("RGBA")
            bg = Img.new("RGB", img.size, (255, 255, 255))
            bg.paste(img, mask=img.split()[-1])
            img = bg
        elif img.mode != "RGB":
            img = img.convert("RGB")
        w, h = img.size
        if max(w, h) > max_edge:
            ratio = max_edge / max(w, h)
            img = img.resize((max(1, int(w * ratio)), max(1, int(h * ratio))), Img.LANCZOS)
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=int(quality), optimize=True)
        return out.getvalue()
    except Exception:
        return data


_COVER_TIMEOUT = aiohttp.ClientTimeout(total=20)


async def download_and_convert_to_base64(cover_url: str, proxy: str = None, *, max_edge: int = 0, jpeg_quality: int = 75):
    """Fetch an image and convert it to base64 (handles HTML indirection).

    Pass max_edge>0 to resize+recompress before encoding (shrinks payload for
    QQ forward msg). max_edge=0 (default) preserves backwards-compatible
    behavior — original bytes are encoded as-is.
    封面拉不下来（含超时、半截响应）一律返回 None：封面可有可无，不能拖住整次搜索。
    """
    try:
        async with aiohttp.ClientSession(timeout=_COVER_TIMEOUT) as session:
            async with session.get(cover_url, proxy=proxy) as response:
                if response.status != 200:
                    return None

                content_type = response.headers.get("Content-Type", "").lower()
                if "html" in content_type:
                    html_content = await response.text()
                    soup = BeautifulSoup(html_content, "html.parser")
                    img_tag = soup.find("meta", attrs={"property": "og:image"})
                    if img_tag:
                        return await download_and_convert_to_base64(
                            img_tag.get("content"),
                            proxy=proxy,
                            max_edge=max_edge,
                            jpeg_quality=jpeg_quality,
                        )
                    return None

                content = await response.read()
                if max_edge and max_edge > 0:
                    content = compress_cover_bytes(content, max_edge, jpeg_quality)
                base64_data = base64.b64encode(content).decode("utf-8")
                return base64_data
    except Exception:
        return None


def is_base64_image(base64_data: str) -> bool:
    """Validate that the base64 data represents an image."""
    try:
        image_data = base64.b64decode(base64_data)
        image = Img.open(io.BytesIO(image_data))
        image.verify()
        return True
    except Exception:
        return False


def format_bytes(n) -> str:
    """Human-readable byte size. Accepts int / str / None."""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return ""
    if n <= 0:
        return ""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{n}B"
        n /= 1024
    return ""


_WINDOWS_FORBIDDEN_FN_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def sanitize_filename(filename: str) -> str:
    """Strip chars that NTQQ / Windows FS forbids in filenames.

    Why: NTQQ's fileService rejects uploads whose name contains `\\ / : * ? " < > |`
    or control chars (retcode 1200 'rich media transfer failed'). Anna's Archive
    book titles routinely contain `:` (e.g. '核酸酶学 : 基础与应用'), and the
    download_url path inherits that. Linux saves the temp file fine, then
    napcat→NTQQ handoff dies. Replace all forbidden chars with a single space,
    collapse runs, and strip leading/trailing dots+spaces.
    """
    if not filename:
        return filename
    s = _WINDOWS_FORBIDDEN_FN_CHARS.sub(" ", filename)
    s = re.sub(r"\s+", " ", s).strip(" .")
    return s or "unnamed"


_TRUNCATION_MARKER = " <省略>"


def truncate_filename(filename: str, max_length: int = 100):
    """Sanitize NTQQ-forbidden chars, then truncate while preserving extension.

    max_length is a UTF-8 *byte* budget. Slicing the base by characters (as this
    used to) never bit ASCII names but is a no-op on CJK titles: a 62-char title
    is 130 bytes yet fits in the 88-char slice, so the ' <省略>' marker got
    appended without a single byte being dropped. Cut the base on the byte
    budget instead and let decode(errors='ignore') drop the half codepoint.
    """
    filename = sanitize_filename(filename)
    if len(filename.encode("utf-8")) <= max_length:
        return filename
    base, ext = os.path.splitext(filename)
    budget = max_length - len((_TRUNCATION_MARKER + ext).encode("utf-8"))
    if budget <= 0:
        # 扩展名本身就撑满了预算，保名不保标记。
        return filename[:max_length] if not ext else ext.lstrip(".")[:max_length]
    truncated = base.encode("utf-8")[:budget].decode("utf-8", "ignore").rstrip(" .")
    return f"{truncated}{_TRUNCATION_MARKER}{ext}"


def upload_cleanup_delay(size: int) -> float:
    """按体积估上传耗时：小文件仍是 5 秒，大文件按 ~1MB/s 保守放宽，最多 30 分钟。

    协议端（llbot/napcat）是收到 send 动作后才去拉文件的，删早了它只能拿到残片。
    """
    return min(max(5, (size or 0) / 1_000_000), 1800)


_TEMP_DIR_PREFIX = "ebooks-"
# 事件循环只弱引用 task，不自己留一份引用的话延时清理任务可能被 GC 掉。
_BACKGROUND_TASKS: set = set()


def make_temp_download_path(temp_root: str, filename: str) -> str:
    """给一次下载分配独立的临时路径：<temp_root>/ebooks-<随机>/<filename>。

    Why: 以前所有书源都直接写 <temp_root>/<书名>。同一本书被两个人（或同一个人
    在清理延时内）连着下两次时，第二次的 "wb" 会截断协议端还在拉的第一份文件，
    第一次的延时清理又会删掉第二份。每次下载一个子目录，文件名保持原样。
    """
    d = os.path.join(temp_root, f"{_TEMP_DIR_PREFIX}{uuid.uuid4().hex[:12]}")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, filename)


def discard_temp_file(path: str) -> None:
    """立即删掉临时文件；是 make_temp_download_path 分配的就连同它的子目录一起删。"""
    if not path:
        return
    parent = os.path.dirname(path)
    if os.path.basename(parent).startswith(_TEMP_DIR_PREFIX):
        shutil.rmtree(parent, ignore_errors=True)
        return
    try:
        os.remove(path)
    except OSError:
        pass


def schedule_temp_cleanup(path: str, delay: float) -> None:
    """delay 秒后删除临时文件（协议端收到 send 动作后才来拉文件，不能立刻删）。"""

    async def _later():
        try:
            await asyncio.sleep(delay)
        finally:
            discard_temp_file(path)

    task = asyncio.get_running_loop().create_task(_later())
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)


def sweep_stale_temp_dirs(temp_root: str, max_age: float = 3600) -> int:
    """Remove ebooks-* temp dirs older than max_age seconds; returns how many were removed.

    Delayed cleanups live only in memory, so a restart mid-delay leaks the book file.
    max_age stays above upload_cleanup_delay's 30-minute cap: a plugin reload keeps the
    old instance's pending cleanups running, and their files may still be uploading.
    """
    cutoff = time.time() - max_age
    removed = 0
    for d in Path(temp_root).glob(f"{_TEMP_DIR_PREFIX}*"):
        try:
            if d.is_dir() and d.stat().st_mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True)
                removed += 1
        except OSError:
            continue
    return removed


def split_url_credentials(url: str) -> tuple:
    """把 URL 里的 basic-auth 用户名密码拆出来，返回 (干净 URL, (user, password) 或 None)。

    Why: Calibre-Web 常配成 http://user:pass@host:8083，这个串会一路带到两个
    不该出现的地方 —— 一是搜索结果里的“下载命令”，等于把密码发进聊天窗口；
    二是 File(url=...)，协议端（llbot/napcat）用 fetch 拉文件，而 fetch 直接
    拒收带 credentials 的 URL（retcode 1200 'Request cannot be constructed
    from a URL that includes credentials'）。
    返回的用户名密码已做百分号解码：密码里的 @ : / 在 URL 里只能写成 %40 等。
    """
    if not url:
        return url, None
    parts = urlsplit(url)
    if not parts.username and not parts.password:
        return url, None
    host = parts.hostname or ""
    if ":" in host:  # IPv6
        host = f"[{host}]"
    if parts.port:
        host = f"{host}:{parts.port}"
    clean = urlunsplit((parts.scheme, host, parts.path, parts.query, parts.fragment))
    return clean, (unquote(parts.username or ""), unquote(parts.password or ""))


def _as_str(value) -> str:
    """AstrBot 会把默认值为 None 的纯数字参数转成 int，校验前统一转回字符串。"""
    return "" if value is None else str(value).strip()


def is_valid_calibre_book_url(book_url) -> bool:
    """检测电子书下载链接格式是否合法"""
    book_url = _as_str(book_url)
    if not re.match(r"^https?://.+/.+$", book_url):
        return False
    return "/opds/download/" in book_url


def is_valid_zlib_book_id(book_id) -> bool:
    """检测 zlib ID 是否为纯数字"""
    return _as_str(book_id).isdigit()


def is_valid_zlib_book_hash(book_hash) -> bool:
    """检测 zlib Hash 是否为 6 位十六进制"""
    return bool(re.fullmatch(r"[a-f0-9]{6}", _as_str(book_hash), re.IGNORECASE))


def is_valid_liber3_book_id(book_id) -> bool:
    """检测 Liber3 的 book_id 是否有效"""
    return bool(re.fullmatch(r"L[a-fA-F0-9]{32}", _as_str(book_id)))


def is_valid_annas_book_id(book_id) -> bool:
    """检测 Anna's Archive 的 book_id 是否有效"""
    return bool(re.fullmatch(r"A[a-fA-F0-9]{32}", _as_str(book_id)))


# identifier 之后允许多级路径和百分号编码，但不能有空白——指令按空格切参数，
# 带空格的链接到这里早就被截断了，文件名里的空格必须编码成 %20。
_ARCHIVE_DOWNLOAD_RE = re.compile(r"https://archive\.org/download/[^/\s]+/\S+")


def is_valid_archive_book_url(book_url) -> bool:
    """检测 archive.org 下载链接格式是否合法"""
    return bool(_ARCHIVE_DOWNLOAD_RE.fullmatch(_as_str(book_url)))


_API_SESSION_TIMEOUT = aiohttp.ClientTimeout(total=60)
# Streaming downloads: no total cap (large books take many minutes), only stall detection.
DOWNLOAD_TIMEOUT = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=120)


class SharedSession:
    """Provide a reusable aiohttp session per source."""

    def __init__(self, proxy: str = None):
        self.proxy = proxy
        self._session: aiohttp.ClientSession = None

    async def get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            # aiohttp's default is a 300 s total timeout; a hung API call would stall the search
            # that long. Downloads pass their own streaming timeout per request.
            self._session = aiohttp.ClientSession(timeout=_API_SESSION_TIMEOUT)
        return self._session

    async def close_session(self):
        if self._session and not self._session.closed:
            await self._session.close()


def to_event_results(event, platform_name: str, results, chunk_size: int = 30, merge_forward: bool = True):
    """Convert search results to event results.

    Args:
        merge_forward: when True (default), pack all Nodes into a single forward
            message; when False, emit one independent chain per book so a single
            slow QQ upload can't kill the entire batch (avoids the 180s
            send_private_forward_msg timeout when many large base64 covers are
            included).
    """
    if isinstance(results, str):
        return [event.plain_result(results)]
    if isinstance(results, list):
        if not merge_forward:
            return [event.chain_result(list(node.content)) for node in results]
        if len(results) <= chunk_size:
            return [event.chain_result([Nodes(results)])]
        chains = []
        for i in range(0, len(results), chunk_size):
            chunk_results = results[i : i + chunk_size]
            chains.append(event.chain_result([Nodes(chunk_results)]))
        return chains
    raise ValueError("Unknown result type.")


def split_query_and_limit(text: str) -> tuple[str, str]:
    """Pull a trailing pure-digit token off the query string.

    Why: astrbot's command parser binds whitespace-separated positional args,
    so `query: str, limit: str = ""` causes `/ebooks search 生理学 姚泰` to
    parse as query='生理学', limit='姚泰' — silently dropping the author
    token because normalize_limit() rejects non-digit limits but doesn't
    re-append them. With GreedyStr+this helper, the entire user input lands
    in `text` and only an actual trailing digit is peeled off as limit.

    只认 1-3 位数字：「生理学 2018」里的 2018 是年份、ISBN 更长，
    都该留在查询里，而任何命令的数量上限都不超过 100。
    """
    if not text:
        return "", ""
    text = text.strip()
    parts = text.rsplit(" ", 1)
    if len(parts) == 2 and parts[1].isdigit() and len(parts[1]) <= 3:
        return parts[0].strip(), parts[1]
    return text, ""


def normalize_limit(limit, default: int, min_value: int, max_value: int, clamp_max: bool = False):
    """Normalize limit input from string/int and enforce bounds."""
    value = default
    if isinstance(limit, int):
        value = limit
    elif isinstance(limit, str) and limit.strip().isdigit():
        value = int(limit)

    if value < min_value:
        return None, f"请确认搜索返回结果数量在 {min_value}-{max_value} 之间。"
    if value > max_value:
        if clamp_max:
            value = max_value
        else:
            return None, f"请确认搜索返回结果数量在 {min_value}-{max_value} 之间。"
    return value, None


def get_rerank_provider(context, config):
    """Pick an enabled rerank provider from AstrBot's provider manager.

    Returns None when none is enabled. `rerank_provider_id` may pin a specific
    provider id; an unknown/disabled id falls back to the first enabled one.
    """
    from astrbot.api.all import logger

    pm = getattr(context, "provider_manager", None)
    insts = getattr(pm, "rerank_provider_insts", None) or []
    if not insts:
        return None
    pid = str(config.get("rerank_provider_id") or "").strip()
    if pid:
        inst = (getattr(pm, "inst_map", None) or {}).get(pid)
        if inst in insts:
            return inst
        logger.warning(f"[ebooks] rerank provider '{pid}' 未启用或不是 rerank 类型，改用第一个可用实例。")
    return insts[0]


# Qwen3-Reranker's official prompt layout; the instruction is what steers it.
_QWEN3_RERANK_PREFIX = (
    "<|im_start|>system\nJudge whether the Document meets the requirements based on the Query "
    'and the Instruct provided. Note that the answer can only be "yes" or "no".<|im_end|>\n'
    "<|im_start|>user\n"
)
_QWEN3_RERANK_SUFFIX = "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"


def rerank_inputs(config, query: str, docs: list) -> tuple:
    """(query, docs) to send to the rerank provider.

    With a non-empty rerank_instruction both are wrapped in Qwen3-Reranker's
    instruction template. Measured with Qwen3-Reranker-4B behind vLLM, the
    bare pair scored the requested 8th edition 0.38 vs 0.33 for the 7th; with
    the instruction recommended in the README it is 0.54 vs 0.31. Empty (the
    default) sends the raw pair, which is what non-Qwen3 rerankers expect.
    """
    instruction = str(config.get("rerank_instruction", "") or "").strip()
    if not instruction:
        return query, docs
    return (
        f"{_QWEN3_RERANK_PREFIX}<Instruct>: {instruction}\n<Query>: {query}\n",
        [f"<Document>: {d}{_QWEN3_RERANK_SUFFIX}" for d in docs],
    )


def is_book_node(node) -> bool:
    """True when a merged-forward node is a book card (carries a 下载命令 line).

    All sources append a `下载命令:` Plain to book nodes; hint/error nodes lack
    it, so this separates rankable cards from status messages without needing
    per-source structured data (Node is a pydantic model — no attributes).
    """
    return any(
        "下载命令" in (getattr(c, "text", "") or "")
        for c in getattr(node, "content", []) or []
    )


_DOC_DROP_PREFIXES = ("MD5:",)
_DOC_TAIL_PREFIXES = ("语言:", "文件:", "ISBN:", "标识:", "DOI:", "IPFS CID:")


def node_doc_text(node, max_chars: int = 300) -> str:
    """Rerank doc built from a book node's rendered Plain text.

    Uses what the user sees, drops the download command and MD5 (hex noise to
    the model), and moves 语言/文件/ISBN-style lines after 简介 before truncating
    to max_chars: a zlib card's metadata alone is ~190 chars, so in display order
    the 简介 — which carries the query-matched excerpt — was the part cut off.
    """
    parts = [
        getattr(c, "text", "") or ""
        for c in getattr(node, "content", []) or []
        if "下载命令" not in (getattr(c, "text", "") or "")
    ]
    head, tail = [], []
    for line in "".join(parts).split("\n"):
        if not line.strip() or line.startswith(_DOC_DROP_PREFIXES):
            continue
        (tail if line.startswith(_DOC_TAIL_PREFIXES) else head).append(line)
    return "\n".join(head + tail).strip()[:max_chars]

