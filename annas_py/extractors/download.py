from html import unescape as html_unescape
from urllib.parse import urljoin

from bs4 import NavigableString

from ..models.data import URL, Download
from ..utils import html_parser
from . import BASE_URL
from .search import _extract_file_info_and_year


def get_information(id: str, base_url: str = BASE_URL) -> Download:
    soup = html_parser(urljoin(base_url, f"md5/{id}"))

    title_div = soup.find(
        "div",
        class_=lambda c: c is not None and "text-2xl" in c and "font-semibold" in c,
    )
    title = ""
    if title_div:
        title = title_div.get_text(strip=True).rstrip("🔍").strip()

    authors = ""
    publisher = None
    publish_date = None
    if title_div:
        for sib in title_div.find_next_siblings("a"):
            icon_classes = _icon_classes_of(sib)
            if "mdi--user-edit" in icon_classes and not authors:
                authors = sib.get_text(strip=True)
            elif "mdi--company" in icon_classes and publisher is None:
                pub_text = sib.get_text(strip=True)
                publisher, publish_date = _split_publisher_year(pub_text)

    file_info = None
    if title_div:
        row = title_div.parent or soup
        file_info, year_from_fi = _extract_file_info_and_year(row)
        if publish_date is None:
            publish_date = year_from_fi

    description = ""
    desc_div = soup.find("div", class_=lambda c: c is not None and "js-md5-top-box-description" in c)
    if desc_div:
        raw = desc_div.get_text(strip=True)
        if raw.startswith("description"):
            raw = raw[len("description"):].lstrip()
        if "Alternative title" in raw:
            raw = raw.split("Alternative title")[0].strip()
        description = raw

    thumbnail = None
    img = soup.find("img")
    if img:
        thumbnail = img.get("src") or None

    raw_links = [parse_link(container, base_url) for container in soup.find_all("a", class_="js-download-link")]
    download_links = list({(link.title, link.url): link for link in raw_links if link}.values())

    return Download(
        title=html_unescape(title) if title else "",
        description=html_unescape(description) if description else "",
        authors=html_unescape(authors) if authors else "",
        file_info=file_info,
        urls=download_links,
        thumbnail=thumbnail,
        publisher=html_unescape(publisher) if publisher else None,
        publish_date=publish_date,
    )


def _icon_classes_of(a) -> str:
    span = a.find("span")
    if not span:
        return ""
    return " ".join(span.get("class") or [])


def _split_publisher_year(text: str) -> tuple[str | None, str | None]:
    """'北京：中国医药科技出版社, 3, 2019' → ('北京：中国医药科技出版社', '2019')"""
    if not text:
        return None, None
    parts = [p.strip() for p in text.split(",") if p.strip()]
    if not parts:
        return None, None
    year = None
    if parts[-1].isdigit() and len(parts[-1]) == 4:
        year = parts[-1]
        parts = parts[:-1]
    publisher = parts[0] if parts else None
    return publisher, year


def parse_link(link: NavigableString, base_url: str = BASE_URL) -> URL | None:
    url = link.get("href")
    if not url or url == "/datasets":
        return None
    if url[0] == "/":
        url = urljoin(base_url, url[1:])
    return URL(html_unescape(link.text), url)
