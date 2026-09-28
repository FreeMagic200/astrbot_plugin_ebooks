from html import unescape as html_unescape
from urllib.parse import urljoin

from bs4 import NavigableString

from ..models.args import FileType, Language, OrderBy
from ..models.data import FileInfo, SearchResult
from ..utils import html_parser
from . import BASE_URL


def search(
    query: str,
    language: Language = Language.ANY,
    file_type: FileType = FileType.ANY,
    order_by: OrderBy = OrderBy.MOST_RELEVANT,
    base_url: str = BASE_URL,
    year_from: int = 0,
) -> list[SearchResult]:
    if not query.strip():
        raise ValueError("query can not be empty")
    params = {
        "q": query,
        "lang": language.value,
        "ext": file_type.value,
        "sort": order_by.value,
    }
    if year_from and year_from > 0:
        params["year_from"] = str(year_from)
    soup = html_parser(urljoin(base_url, "search"), params)
    raw_results = soup.find_all("a", class_="js-vim-focus")
    return list(filter(lambda i: i is not None, map(parse_result, raw_results)))


def parse_result(title_a: NavigableString) -> SearchResult | None:
    try:
        title = title_a.get_text(strip=True)
        href = title_a.get("href", "") or ""
        if not title or "/md5/" not in href:
            return None
        id = href.split("/md5/")[-1]

        row = _find_row(title_a)
        if row is None:
            return None

        authors = None
        publisher = None
        for a in row.find_all("a"):
            a_href = a.get("href", "") or ""
            if not a_href.startswith("/search?q="):
                continue
            icon_classes = _icon_class_string(a)
            if not icon_classes:
                continue
            if "mdi--user-edit" in icon_classes and authors is None:
                authors = a.get_text(strip=True)
            elif "mdi--company" in icon_classes and publisher is None:
                publisher = a.get_text(strip=True)

        file_info, publish_date = _extract_file_info_and_year(row)

        thumbnail = None
        img = row.find("img")
        if img:
            thumbnail = img.get("src")

        return SearchResult(
            id=id,
            title=html_unescape(title),
            authors=html_unescape(authors) if authors else "",
            file_info=file_info,
            thumbnail=thumbnail,
            publisher=html_unescape(publisher) if publisher else None,
            publish_date=publish_date,
        )
    except Exception:
        return None


def _find_row(title_a: NavigableString):
    cur = title_a
    for _ in range(5):
        if cur.parent is None:
            return None
        cur = cur.parent
        classes = cur.get("class") or []
        if "flex" in classes and any(c.startswith("pt-") for c in classes):
            return cur
    return None


def _icon_class_string(a) -> str:
    span = a.find("span")
    if not span:
        return ""
    return " ".join(span.get("class") or [])


def _extract_file_info_and_year(row):
    """Parse the file info line, e.g.:
        'Chinese [zh] · EPUB · 0.8MB · 📕 Book (fiction) · 🚀/lgli/zlib · ...'
        'Chinese [zh] · PDF · 72.3MB · 2005 · 📗 Book (unknown) · 🚀/duxiu/upload · ...'
    Returns (FileInfo or None, year_string or None).
    """
    fi_div = None
    for d in row.find_all("div"):
        cls = d.get("class") or []
        if "text-gray-800" in cls and "font-semibold" in cls:
            fi_div = d
            break
    if fi_div is None:
        return None, None

    raw = fi_div.get_text(" ", strip=True).replace("\xa0", " ")
    parts = [p.strip() for p in raw.split("·") if p.strip()]
    language = None
    extension = None
    size = None
    library = None
    year = None
    for p in parts:
        if "[" in p and "]" in p and language is None:
            language = p
            continue
        if p.startswith("🚀"):
            stripped = p.replace("🚀", "").strip().lstrip("/")
            library = stripped.split("/")[-1] if stripped else None
            continue
        if any(emoji in p for emoji in ("📕", "📗", "📘", "📙", "📓", "📔", "📒")):
            continue
        upper = p.upper()
        if upper.endswith(("MB", "KB", "GB", "TB", "B")) and any(ch.isdigit() for ch in p):
            size = p
            continue
        if p.isdigit() and len(p) == 4 and year is None:
            year = p
            continue
        if extension is None and p.replace(".", "").isalnum() and len(p) <= 6 and any(c.isalpha() for c in p):
            extension = p.lstrip(".")
            continue

    if not any([extension, size, language, library]):
        return None, year
    return FileInfo(extension or "未知", size or "未知", language, library or "未知"), year
