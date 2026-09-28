import asyncio
import os
import re
from urllib.parse import quote, unquote, urlparse

import aiofiles
import aiohttp
from astrbot.api.all import Plain, Image, Node, File, logger

from .utils import (
    SharedSession,
    discard_temp_file,
    download_and_convert_to_base64,
    excerpt_for_query,
    html_to_text,
    filter_unsafe,
    format_bytes,
    is_base64_image,
    is_url_accessible,
    make_temp_download_path,
    is_valid_archive_book_url,
    schedule_temp_cleanup,
    truncate_filename,
    upload_cleanup_delay,
)

API_TIMEOUT = aiohttp.ClientTimeout(total=30)
# archive.org 的大 PDF 动辄上百 MB，卡总时长会把正常下载掐死；只卡「多久没新数据」。
DOWNLOAD_TIMEOUT = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=120)
# 借阅库（lending library）的书只给加密文件：*_encrypted.pdf / .lcpdf / .lcpepub，
# 或者把原件标成 private，直链一律 401/403。
_ENCRYPTED_NAME_RE = re.compile(r"_encrypted\.[^.]+$", re.IGNORECASE)
_YEAR_RE = re.compile(r"\d{4}")
_LUCENE_SPECIAL_RE = re.compile(r'[+\-!(){}\[\]^"~*?:\\/&|]')
_LUCENE_OPERATORS = {"and", "or", "not", "to"}
# Relaxed search: at most this many requests after the phrase search misses.
RELAX_MAX_STEPS = 3


def _as_text(value, default: str = "未知") -> str:
    """元数据字段既可能是字符串也可能是列表（多作者、多语言），统一拼成一行。"""
    if isinstance(value, (list, tuple)):
        value = ", ".join(str(v).strip() for v in value if str(v).strip())
    value = str(value).strip() if value is not None else ""
    return value or default


def _is_true(value) -> bool:
    return str(value).strip().lower() == "true"


class ArchiveSource(SharedSession):
    def __init__(self, config, proxy: str, max_results: int, temp_path: str, safety_checker=None):
        super().__init__(proxy)
        self.config = config
        self.max_results = max_results
        self.temp_path = temp_path
        self.safety_checker = safety_checker

    async def _advanced_search(self, session, q: str, rows: int):
        """One advancedsearch call; returns the docs list, or None when the API fails."""
        params = {
            "q": q,
            "fl[]": "identifier,title",
            "sort[]": "downloads desc",
            "rows": rows,
            "page": 1,
            "output": "json",
        }
        async with session.get(
            "https://archive.org/advancedsearch.php", params=params, proxy=self.proxy, timeout=API_TIMEOUT
        ) as response:
            if response.status != 200:
                logger.error(f"[archive.org] Error during search: archive.org API returned status code {response.status}")
                return None
            result_data = await response.json()
        return result_data.get("response", {}).get("docs", [])

    async def _search_archive_books(self, query: str, limit: int = 20):
        """Returns the (untruncated) book list, or None when the search API fails."""
        base_metadata_url = "https://archive.org/metadata/"
        formats = ("pdf", "epub")
        rows = limit + 10

        # 查询串要放进 title:"..." 短语里，里面再出现引号/反斜杠会把 Lucene 语法打断。
        phrase = re.sub(r'["\\]+', " ", query).strip() or query
        session = await self.get_session()
        docs = await self._advanced_search(session, f'title:"{phrase}" mediatype:texts', rows)
        if docs == []:
            docs = await self._relaxed_search(session, query, rows)
        if docs is None:
            return None
        if not docs:
            logger.info("[archive.org] 未找到匹配的电子书。")
            return []

        tasks = [self._fetch_metadata(session, base_metadata_url + doc["identifier"], formats) for doc in docs]
        metadata_results = await asyncio.gather(*tasks)

        books = [
            {
                "title": _as_text(doc.get("title")),
                "cover": metadata.get("cover"),
                "authors": metadata.get("authors"),
                "language": metadata.get("language"),
                "year": metadata.get("year"),
                "publisher": metadata.get("publisher"),
                "download_url": metadata.get("download_url"),
                "description": metadata.get("description"),
                "file_ext": metadata.get("file_ext"),
                "file_size_str": metadata.get("file_size_str"),
                "file_md5": metadata.get("file_md5"),
                "isbn": metadata.get("isbn"),
                "doi": metadata.get("doi"),
                "lending_only": metadata.get("lending_only", False),
            }
            for doc, metadata in zip(docs, metadata_results)
            if metadata
        ]
        return books

    async def _relaxed_search(self, session, query: str, rows: int):
        """Phrase got 0 hits: every word must be in the title or creator, dropping trailing words.

        The phrase needs the whole query inside the title, so "fundamentals of biostatistics
        8th edition" or "rosner biostatistics" found nothing. Dropping from the end keeps the
        leading title words (measured: the 8th-edition query recovers Rosner's book once
        "8th edition" is dropped). At least two words stay — archive.org matches CJK per
        character, so a single Chinese word is pure noise.
        """
        terms = [t for t in _LUCENE_SPECIAL_RE.sub(" ", query).lower().split() if t not in _LUCENE_OPERATORS]
        for _ in range(RELAX_MAX_STEPS):
            if len(terms) < 2:
                break
            clause = " AND ".join(f"(title:({t}) OR creator:({t}))" for t in terms)
            docs = await self._advanced_search(session, f"{clause} AND mediatype:texts", rows)
            if docs is None or docs:
                return docs
            terms = terms[:-1]
        return []

    @staticmethod
    def _pick_file(files: list, formats: tuple):
        """挑第一个能直接下载的 pdf/epub：跳过 private 与借阅加密版本。"""
        for file in files:
            name = file.get("name", "") or ""
            ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
            if ext not in formats:
                continue  # 顺带排除 .lcpdf / .lcpepub 这类 endswith("pdf") 也能匹配上的加密格式
            if _is_true(file.get("private")) or _ENCRYPTED_NAME_RE.search(name):
                continue
            return file
        return None

    async def _fetch_metadata(self, session: aiohttp.ClientSession, url: str, formats: tuple) -> dict:
        try:
            async with session.get(url, proxy=self.proxy, timeout=API_TIMEOUT) as response:
                if response.status != 200:
                    logger.error(f"[archive.org] Error retrieving Metadata: Status code {response.status}")
                    return {}
                book_detail = await response.json()

            md = book_detail.get("metadata", {}) or {}
            identifier = md.get("identifier", None)
            if not identifier:
                return {}
            # 借阅书整本都只能在线借阅，文件列表里就算有 pdf/epub 也下不下来。
            # Kept as a marker (not dropped) so search_nodes can say "found N, all borrow-only".
            if _is_true(md.get("access-restricted-item")):
                return {"lending_only": True}

            file = self._pick_file(book_detail.get("files", []) or [], formats)
            if not file:
                return {}

            description = md.get("description", "无简介")
            if isinstance(description, list):
                description = "\n".join(str(d) for d in description)
            if isinstance(description, str):
                # 存全文，展示时再按查询摘录（见 excerpt_for_query）。
                description = html_to_text(description)
            else:
                description = "无简介"

            # publicdate 是上传到 archive.org 的时间，不是出版年份。
            year_match = _YEAR_RE.search(_as_text(md.get("date") or md.get("year"), ""))
            year = year_match.group(0) if year_match else "未知"

            ext_ids = md.get("external-identifier", [])
            if isinstance(ext_ids, str):
                ext_ids = [ext_ids]
            isbn_vals, doi_vals = [], []
            for token in ext_ids:
                low = token.lower()
                if low.startswith("urn:isbn:") or low.startswith("isbn:"):
                    isbn_vals.append(token.split(":", 1 if low.startswith("isbn:") else 2)[-1])
                elif low.startswith("urn:doi:") or low.startswith("doi:"):
                    doi_vals.append(token.split(":", 1 if low.startswith("doi:") else 2)[-1])

            name = file["name"]
            return {
                "cover": f"https://archive.org/services/img/{quote(identifier)}",
                "authors": _as_text(md.get("creator")),
                "year": year,
                "publisher": _as_text(md.get("publisher")),
                "language": _as_text(md.get("language")),
                "description": description,
                # 文件名常带空格：不编码的话下载命令会被指令解析按空格切断。
                "download_url": f"https://archive.org/download/{quote(identifier)}/{quote(name)}",
                "file_ext": name.rsplit(".", 1)[-1].upper(),
                "file_size_str": format_bytes(file.get("size")),
                "file_md5": (file.get("md5") or "").strip(),
                "isbn": ", ".join(isbn_vals),
                "doi": ", ".join(doi_vals),
            }
        except Exception as e:
            logger.error(f"[archive.org] 获取 Metadata 数据时发生错误: {e}")
        return {}

    async def search_nodes(self, event, query: str = None, limit: int = 0):
        if not self.config.get("enable_archive", False):
            return "[archive.org] 功能未启用。"

        if not query:
            return "[archive.org] 请提供电子书关键词以进行搜索。"

        if not await is_url_accessible("https://archive.org", proxy=self.proxy):
            return "[archive.org] 无法连接到 archive.org。"

        if limit < 1:
            return "[archive.org] 请确认搜索返回结果数量在 1-60 之间。"

        try:
            logger.info(f"[archive.org] Received books search query: {query}, limit: {limit}")
            results = await self._search_archive_books(query, limit)

            if results is None:
                return "[archive.org] 搜索接口暂时不可用，请稍后再试。"
            lending = sum(1 for b in results if b["lending_only"])
            results = [b for b in results if not b["lending_only"]]
            if not results:
                if lending:
                    return f"[archive.org] 找到 {lending} 本，但都只能在线借阅（需登录 archive.org 借阅），无法直接下载。"
                return "[archive.org] 未找到匹配的电子书。"

            # 先过滤再截断：先截到 limit 再过滤，掉几条就少几条，凑不满用户要的数量。
            results = filter_unsafe(
                results, self.safety_checker,
                fields=["title", "authors", "publisher", "description"],
                source="archive.org",
            )
            if not results:
                return "[archive.org] 全部结果被内容过滤丢弃。"
            results = results[:limit]

            enable_cover = self.config.get("enable_cover_image", True)
            cover_max = int(self.config.get("cover_max_size", 400) or 0)

            async def construct_node(book):
                chain = [Plain(f"{book.get('title', '未知')}\n")]

                if enable_cover and book.get("cover"):
                    base64_image = await download_and_convert_to_base64(
                        book.get("cover"), proxy=self.proxy, max_edge=cover_max
                    )
                    if base64_image and is_base64_image(base64_image):
                        chain.append(Image.fromBase64(base64_image))

                chain.append(Plain(f"作者: {book.get('authors', '未知')}\n"))
                chain.append(Plain(f"年份: {book.get('year', '未知')}\n"))
                chain.append(Plain(f"出版社: {book.get('publisher', '未知')}\n"))
                chain.append(Plain(f"语言: {book.get('language', '未知')}\n"))

                fmt_parts = []
                if book.get("file_ext"):
                    fmt_parts.append(book["file_ext"])
                if book.get("file_size_str"):
                    fmt_parts.append(book["file_size_str"])
                if fmt_parts:
                    chain.append(Plain(f"文件: {' · '.join(fmt_parts)}\n"))
                if book.get("isbn"):
                    chain.append(Plain(f"ISBN: {book['isbn']}\n"))
                if book.get("doi"):
                    chain.append(Plain(f"DOI: {book['doi']}\n"))
                if book.get("file_md5"):
                    chain.append(Plain(f"MD5: {book['file_md5']}\n"))

                chain.append(Plain(f"简介: {excerpt_for_query(book.get('description') or '无简介', query, 150)}\n"))
                download_url = book.get("download_url", "")
                if download_url:
                    chain.append(Plain(f"下载命令:\n/archive download {download_url}"))
                else:
                    chain.append(Plain("下载命令: (无可用直链)"))

                return Node(
                    uin=event.get_self_id(),
                    name="archive.org",
                    content=chain,
                )

            tasks = [construct_node(book) for book in results]
            return await asyncio.gather(*tasks)
        except Exception as e:
            logger.error(f"[archive.org] Error processing archive.org search request: {e}")
            return "[archive.org] 搜索电子书时发生错误，请稍后再试。"

    async def download(self, event, book_url: str = ""):
        if not self.config.get("enable_archive", False):
            return [event.plain_result("[archive.org] 功能未启用。")]

        if not is_valid_archive_book_url(book_url):
            return [event.plain_result("[archive.org] 请提供有效的下载链接。")]

        if not await is_url_accessible("https://archive.org", proxy=self.proxy):
            return [event.plain_result("[archive.org] 无法连接到 archive.org。")]

        temp_file_path = None
        try:
            session = await self.get_session()
            async with session.get(
                str(book_url), allow_redirects=True, proxy=self.proxy, timeout=DOWNLOAD_TIMEOUT
            ) as response:
                if response.status in (401, 403):
                    return [event.plain_result(
                        f"[archive.org] 该文件需要借阅权限，无法直接下载（状态码 {response.status}）。"
                    )]
                if response.status != 200:
                    return [event.plain_result(f"[archive.org] 无法下载电子书，状态码: {response.status}")]

                ebook_url = str(response.url)
                logger.debug(f"[archive.org] 跳转后的下载地址: {ebook_url}")

                content_disposition = response.headers.get("Content-Disposition", "")
                book_name = None
                if content_disposition:
                    book_name_match = re.search(r'filename\*=(?:UTF-8\'\')?([^;]+)', content_disposition)
                    if book_name_match:
                        book_name = unquote(book_name_match.group(1))
                    else:
                        book_name_match = re.search(r'filename=["\']?([^;\'"]+)["\']?', content_disposition)
                        if book_name_match:
                            book_name = book_name_match.group(1)

                if not book_name or book_name.strip() == "":
                    book_name = unquote(os.path.basename(urlparse(ebook_url).path)) or "unknown_book"

                book_name = truncate_filename(book_name)
                temp_file_path = make_temp_download_path(self.temp_path, book_name)

                size = 0
                async with aiofiles.open(temp_file_path, "wb") as temp_file:
                    async for chunk in response.content.iter_chunked(64 * 1024):
                        size += len(chunk)
                        await temp_file.write(chunk)

            logger.info(f"[archive.org] 文件已下载并保存到临时目录：{temp_file_path}（{size} 字节）")
            file = File(name=book_name, file=temp_file_path)
            # 固定等 5 秒对几十 MB 的书不够：协议端还在拉文件就被删掉了。
            schedule_temp_cleanup(temp_file_path, upload_cleanup_delay(size))
            return [event.chain_result([file])]
        except Exception as e:
            logger.error(f"[archive.org] 下载失败: {type(e).__name__}: {e}")
            discard_temp_file(temp_file_path)
            return [event.plain_result("[archive.org] 下载电子书时发生错误，请稍后再试。")]

    async def close(self):
        await self.close_session()
