import asyncio
import os
import re
import time
from urllib.parse import unquote, urlparse

import aiohttp
from astrbot.api.all import Plain, Image, Node, File, logger

from .annas_py import get_information as get_annas_information
from .annas_py import search as annas_search
from .annas_py.models.args import Language
from .utils import (
    discard_temp_file,
    download_and_convert_to_base64,
    filter_unsafe,
    is_base64_image,
    is_url_accessible,
    is_valid_annas_book_id,
    make_temp_download_path,
    schedule_temp_cleanup,
    truncate_filename,
    upload_cleanup_delay,
)

DEFAULT_ANNAS_BASE_URL = "https://annas-archive.gl"

# 书籍页上的快速下载入口：/fast_download/{md5}/{path_index}/{domain_index}
FAST_DOWNLOAD_RE = re.compile(r"/fast_download/[0-9a-fA-F]{32}/(\d+)/(\d+)")
# 书不在合作服务器的馆藏里（只有 IPFS / Z-Library 等外部源）时，fast_download.json
# 对任何索引都回这个错——换服务器没用，只能整体换下载源。
INVALID_INDEX_ERROR = "Invalid domain_index or path_index"
MAX_FAST_ATTEMPTS = 5
# 合作 CDN 每隔几十 MB 就掐一次连接，固定次数的预算下不完大文件：只有「毫无
# 进展」才算消耗次数，同时用总次数和总时长兜底防止无限重试。
DOWNLOAD_STALL_LIMIT = 5
DOWNLOAD_MAX_ATTEMPTS = 40
DOWNLOAD_MAX_SECONDS = 45 * 60
RANGE_REFUSAL_LIMIT = 3
DOWNLOAD_BACKOFF = 2
DOWNLOAD_BACKOFF_CAP = 15
# 合作 CDN 实测只有 60-190 KB/s，上百 MB 的书要下十几分钟，用「总时长」卡会
# 把正常下载直接掐死；只能卡「多久没有新数据进来」。
DOWNLOAD_TIMEOUT = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=120)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}


def _result_has_file(results) -> bool:
    for result in results or []:
        for comp in getattr(result, "chain", None) or []:
            if isinstance(comp, File):
                return True
    return False


def _result_text(results) -> str:
    for result in results or []:
        for comp in getattr(result, "chain", None) or []:
            if isinstance(comp, Plain) and comp.text.strip():
                return comp.text.strip()
    return ""


def _resolve_annas_language(value) -> Language:
    """Map config string ('', 'zh', 'en', ...) to Language enum; '' = ANY."""
    raw = (value or "").strip().lower()
    if not raw or raw == "any":
        return Language.ANY
    for lang in Language:
        if lang.value == raw:
            return lang
    logger.warning(f"[Anna's Archive] 未识别的语言代码 {value!r}，回退到 ANY。")
    return Language.ANY


class AnnasSource:
    def __init__(self, config, proxy: str, max_results: int, temp_path: str, safety_checker=None, zlib_source=None):
        self.config = config
        self.proxy = proxy
        self.max_results = max_results
        self.temp_path = temp_path
        self.safety_checker = safety_checker
        # AA 没有合作服务器的书通常来自 Z-Library 馆藏，用同一个 md5 兜底下载。
        self.zlib_source = zlib_source
        self.base_url = (self.config.get("annas_base_url", DEFAULT_ANNAS_BASE_URL) or DEFAULT_ANNAS_BASE_URL).rstrip("/")
        self.secret_key = (self.config.get("annas_secret_key", "") or "").strip()

    async def search_nodes(self, event, query: str, limit: int = 0):
        if not self.config.get("enable_annas", False):
            return "[Anna's Archive] 功能未启用。"

        if not await is_url_accessible(self.base_url, proxy=self.proxy):
            return f"[Anna's Archive] 无法连接到 {self.base_url}。"

        if not query:
            return "[Anna's Archive] 请提供电子书关键词以进行搜索。"

        if limit < 1:
            return "[Anna's Archive] 请确认搜索返回结果数量在 1-60 之间。"

        min_year = int(self.config.get("min_year", 0) or 0)
        language = _resolve_annas_language(self.config.get("annas_language", ""))

        try:
            logger.info(
                f"[Anna's Archive] Received books search query: {query}, limit: {limit}, "
                f"year_from={min_year or '-'}, lang={language.value or 'any'}"
            )
            results = await asyncio.to_thread(
                annas_search, query, language,
                base_url=self.base_url, year_from=min_year,
            )
            if not results or len(results) == 0:
                return "[Anna's Archive] 未找到匹配的电子书。"

            results = filter_unsafe(
                results, self.safety_checker,
                fields=["title", "authors", "publisher"],
                source="Anna's Archive",
            )
            if not results:
                return "[Anna's Archive] 全部结果被内容过滤丢弃。"

            books = results[:limit]

            enable_cover = self.config.get("enable_cover_image", True)
            cover_max = int(self.config.get("cover_max_size", 400) or 0)

            async def construct_node(book):
                chain = [Plain(f"{book.title}\n")]

                if enable_cover and book.thumbnail:
                    base64_image = await download_and_convert_to_base64(
                        book.thumbnail, proxy=self.proxy, max_edge=cover_max
                    )
                    if base64_image and is_base64_image(base64_image):
                        chain.append(Image.fromBase64(base64_image))

                chain.append(Plain(f"作者: {book.authors or '未知'}\n"))
                chain.append(Plain(f"出版社: {book.publisher or '未知'}\n"))
                chain.append(Plain(f"年份: {book.publish_date or '未知'}\n"))
                fi = book.file_info
                language = fi.language if fi else None
                if language:
                    chain.append(Plain(f"语言: {language}\n"))
                fmt_parts = []
                if fi and fi.extension and fi.extension != "未知":
                    fmt_parts.append(fi.extension.upper())
                if fi and fi.size and fi.size != "未知":
                    fmt_parts.append(fi.size)
                if fi and fi.library and fi.library != "未知":
                    fmt_parts.append(f"来源:{fi.library}")
                if fmt_parts:
                    chain.append(Plain(f"文件: {' · '.join(fmt_parts)}\n"))
                # Anna's Archive 用 MD5 作为 book id
                chain.append(Plain(f"MD5: {book.id}\n"))
                chain.append(Plain(f"下载命令:\n/annas download A{book.id}"))

                return Node(
                    uin=event.get_self_id(),
                    name="Anna's Archive",
                    content=chain,
                )

            tasks = [construct_node(book) for book in books]
            return await asyncio.gather(*tasks)
        except Exception as e:
            logger.error(f"[Anna's Archive] Error during book search: {e}", exc_info=True)
            if "403" in str(e):
                return ("[Anna's Archive] 被站点反爬拦截（DDoS-Guard），暂时无法搜索。"
                        "若持续失败请检查容器内 playwright 浏览器是否可用。")
            return "[Anna's Archive] 搜索电子书时发生错误，请稍后再试。"

    async def download(self, event, book_id: str = ""):
        if not self.config.get("enable_annas", False):
            return [event.plain_result("[Anna's Archive] 功能未启用。")]

        if not is_valid_annas_book_id(book_id):
            return [event.plain_result("[Anna's Archive] 请提供有效的书籍 ID。")]

        # 只剥掉前缀那一个 A：lstrip("A") 会把以大写 A 开头的 md5 多剥一位。
        md5 = str(book_id).strip()[1:].lower()

        if self.secret_key:
            return await self._download_via_fast_api(event, md5)
        return await self._download_link_list(event, md5)

    async def _download_via_fast_api(self, event, md5: str):
        try:
            download_url, data, err = await self._resolve_fast_url(md5)
        except Exception as e:
            logger.error(f"[Anna's Archive] 获取直链失败：{type(e).__name__}: {e}", exc_info=True)
            return [event.plain_result(f"[Anna's Archive] 下载电子书时发生错误，请稍后再试：{type(e).__name__}: {e}")]

        if not download_url:
            return await self._fallback_download(event, md5, err)

        temp_file_path = None
        try:
            original_name = self._filename_from_url(download_url)
            book_name = truncate_filename(original_name) if original_name else f"{md5}.bin"
            temp_file_path = make_temp_download_path(self.temp_path, book_name)

            # Anna's partner CDN rejects long-filename URLs whose path contains
            # NUL bytes or other oddities baked into the title metadata
            # ("400 Bad request. Try using the 'short filename' link instead.").
            # Always swap the last path segment for <md5>.<ext> — the signature
            # covers the prefix only, so the short form downloads the same file.
            _, ext = os.path.splitext(original_name or "")
            if not ext or len(ext) > 8:
                ext = ".bin"
            short_download_url = self._rewrite_to_short_url(download_url, f"{md5}{ext}")

            size = await self._stream_to_file(short_download_url, temp_file_path)

            logger.debug(f"[Anna's Archive] 文件已保存：{temp_file_path}（{size} 字节）")
            file = File(name=book_name, file=str(temp_file_path))
            # 固定等 5 秒对上百 MB 的书不够：文件还在往协议端上传就被删掉了。
            schedule_temp_cleanup(temp_file_path, upload_cleanup_delay(size))

            left = (data.get("account_fast_download_info") or {}).get("downloads_left")
            extra = []
            if left is not None:
                extra.append(Plain(f"今日剩余 Anna’s Archive fast 下载额度：{left}"))
            return [event.chain_result([file, *extra])] if extra else [event.chain_result([file])]
        except Exception as e:
            logger.error(f"[Anna's Archive] fast 下载失败：{type(e).__name__}: {e}", exc_info=True)
            # 别把下了一半的残片留在 temp 里占地方。
            discard_temp_file(temp_file_path)
            return [event.plain_result(f"[Anna's Archive] 下载电子书时发生错误，请稍后再试：{type(e).__name__}: {e}")]

    async def _stream_to_file(self, url: str, path: str) -> int:
        """下载到本地，断了就续传。

        合作 CDN 经常在传输中途掐断连接（ClientPayloadError: Not enough data to
        satisfy content length header），文件越大越必然踩到。它支持 Range
        （206 + Content-Range），所以断点接着要就行；服务端无视 Range 时退回重下。
        """
        done = 0
        total = None
        last_err = None
        stalled = 0
        refusals = 0
        deadline = time.monotonic() + DOWNLOAD_MAX_SECONDS

        async with aiohttp.ClientSession() as session:
            for attempt in range(1, DOWNLOAD_MAX_ATTEMPTS + 1):
                if time.monotonic() > deadline:
                    last_err = last_err or IOError("超过总时长上限")
                    break

                before = done
                headers = dict(_HEADERS)
                if done:
                    headers["Range"] = f"bytes={done}-"
                try:
                    async with session.get(
                        url, headers=headers, proxy=self.proxy, timeout=DOWNLOAD_TIMEOUT,
                    ) as resp:
                        # 416 = 请求的起点已越过文件末尾，说明本来就下完了。
                        if resp.status == 416 and done:
                            return done
                        if done and resp.status == 200:
                            # 服务端这次没认 Range。直接按 200 从头写会把已下的几十 MB
                            # 截掉（实测就是这样白丢了 44MB），先换连接再要几次；
                            # 连着都不给续传，才认栽重下。
                            refusals += 1
                            if refusals < RANGE_REFUSAL_LIMIT:
                                raise IOError("服务端忽略 Range，换连接重试")
                            logger.warning(
                                f"[Anna's Archive] 服务端始终不支持续传，只能从头重下"
                                f"（丢弃已下的 {done} 字节）"
                            )
                            done = 0
                            refusals = 0
                        elif resp.status not in (200, 206):
                            raise IOError(f"HTTP {resp.status}")
                        else:
                            refusals = 0

                        if total is None:
                            total = self._total_size(resp, done)

                        with open(path, "ab" if done else "wb") as f:
                            async for chunk in resp.content.iter_chunked(64 * 1024):
                                f.write(chunk)
                                done += len(chunk)

                    if total is None or done >= total:
                        return done
                    last_err = IOError(f"数据不完整 {done}/{total}")
                except Exception as e:
                    last_err = e
                    # 写了一半才断的，以磁盘上的实际字节数为准重新对齐。
                    done = os.path.getsize(path) if os.path.exists(path) else 0

                # 只要还在往前推进就接着续传，卡住不动才消耗重试次数——CDN 每隔
                # 几十 MB 就断一次，固定次数的预算根本下不完大文件。
                stalled = 0 if done > before else stalled + 1
                if stalled >= DOWNLOAD_STALL_LIMIT:
                    break
                logger.warning(
                    f"[Anna's Archive] 下载中断（{done}/{total or '?'} 字节，第 {attempt} 次），"
                    f"续传重试：{type(last_err).__name__}: {last_err}"
                )
                await asyncio.sleep(min(DOWNLOAD_BACKOFF * max(stalled, 1), DOWNLOAD_BACKOFF_CAP))

        raise IOError(f"下载未完成（{done}/{total or '?'} 字节）：{last_err}")

    @staticmethod
    def _total_size(resp, offset: int):
        """从 Content-Range / Content-Length 推出文件总大小，拿不到就返回 None。"""
        crange = resp.headers.get("Content-Range", "")
        if "/" in crange:
            tail = crange.rsplit("/", 1)[-1].strip()
            if tail.isdigit():
                return int(tail)
        length = resp.headers.get("Content-Length")
        if length and length.isdigit():
            return offset + int(length)
        return None

    async def _resolve_fast_url(self, md5: str):
        """逐个试 (path_index, domain_index)，返回 (直链, 接口返回, 错误)。

        (0, 0) 对大多数书有效，省掉一次抓页面；失败后再去书籍页解真实的
        /fast_download/{md5}/{path_index}/{domain_index} 组合重试。
        """
        tried = set()
        candidates = [(0, 0)]
        refreshed = False
        last_err = None

        while candidates and len(tried) < MAX_FAST_ATTEMPTS:
            indices = candidates.pop(0)
            if indices in tried:
                continue
            tried.add(indices)

            data = await self._fast_download_json(md5, *indices)
            download_url = data.get("download_url")
            if download_url:
                return download_url, data, None

            last_err = data.get("error") or "unknown error"
            logger.warning(
                f"[Anna's Archive] fast_download.json path_index={indices[0]} "
                f"domain_index={indices[1]}: {last_err}"
            )
            # 额度用尽、key 失效之类换服务器也没用，直接结束。
            if last_err != INVALID_INDEX_ERROR:
                break
            if not refreshed:
                refreshed = True
                candidates = [i for i in await self._page_fast_indices(md5) if i not in tried]

        return None, None, last_err

    async def _fast_download_json(self, md5: str, path_index: int, domain_index: int) -> dict:
        params = {
            "md5": md5, "key": self.secret_key,
            "path_index": path_index, "domain_index": domain_index,
        }
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{self.base_url}/dyn/api/fast_download.json",
                params=params, headers=_HEADERS, proxy=self.proxy,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                data = await resp.json(content_type=None)
        return data if isinstance(data, dict) else {}

    async def _page_fast_indices(self, md5: str) -> list:
        """从书籍页解析出这本书真实可用的 (path_index, domain_index) 组合。"""
        try:
            book_info = await asyncio.to_thread(get_annas_information, md5, base_url=self.base_url)
        except Exception as e:
            logger.warning(f"[Anna's Archive] 读取书籍页失败，无法确定下载服务器索引：{e}")
            return []

        indices = []
        for url in book_info.urls or []:
            match = FAST_DOWNLOAD_RE.search(url.url or "")
            if not match:
                continue
            pair = (int(match.group(1)), int(match.group(2)))
            if pair not in indices:
                indices.append(pair)
        return indices

    async def _fallback_download(self, event, md5: str, err):
        """没有合作服务器的书（只在 Z-Library / IPFS 等外部源）走 Z-Library 兜底。"""
        no_partner = err == INVALID_INDEX_ERROR
        zlib_note = ""

        if no_partner and self.zlib_source is not None:
            logger.info(f"[Anna's Archive] {md5} 无合作服务器，改用 Z-Library 下载。")
            try:
                book = await self.zlib_source.find_by_md5(md5)
            except Exception as e:
                logger.warning(f"[Anna's Archive] Z-Library 查询 md5 失败：{type(e).__name__}: {e}")
                book = None
            if book:
                results = await self.zlib_source.download(event, str(book.get("id")), book.get("hash"))
                if _result_has_file(results):
                    return results
                zlib_note = _result_text(results)
                logger.warning(f"[Anna's Archive] Z-Library 兜底下载失败：{zlib_note}")
            elif getattr(self.zlib_source, "last_login_error", ""):
                # 登录不上和「这本书 Z-Library 也没有」是两回事，别让用户以为书没了。
                # 完整原因（可能带上游 HTML 片段）留在日志里，发出去的只留一句话。
                detail = self.zlib_source.last_login_error
                logger.warning(f"[Anna's Archive] Z-Library 登录失败，无法兜底：{detail}")
                brief = detail if len(detail) <= 60 else detail[:60] + "…"
                zlib_note = f"[Z-Library] 暂时登录不上（{brief}），稍后重试即可。"

        if no_partner:
            reason = "该书不在 Anna's Archive 的合作服务器上，无法直链下载，请通过下列链接手动下载："
        else:
            reason = f"无法获取直链（{err}），请通过下列链接手动下载："
        if zlib_note:
            reason = f"{zlib_note}\n{reason}"
        return await self._download_link_list(event, md5, reason)

    async def _download_link_list(self, event, md5: str, reason: str = None):
        try:
            book_info = await asyncio.to_thread(get_annas_information, md5, base_url=self.base_url)
            urls = book_info.urls

            if not urls:
                return [event.plain_result("[Anna's Archive] 未找到任何下载链接！")]

            lead = reason or "未配置 secret_key 无法直接下载，请通过下列链接手动下载："
            chain = [Plain(f"Anna's Archive\n{lead}")]

            fast_links = [url for url in urls if "Fast Partner Server" in url.title]
            if fast_links:
                chain.append(Plain("\n快速链接（需要付费/会员）：\n"))
                for index, url in enumerate(fast_links, 1):
                    chain.append(Plain(f"{index}. {url.url}\n"))

            slow_links = [url for url in urls if "Slow Partner Server" in url.title]
            if slow_links:
                chain.append(Plain("\n慢速链接（需要等待）：\n"))
                for index, url in enumerate(slow_links, 1):
                    chain.append(Plain(f"{index}. {url.url}\n"))

            other_links = [
                url for url in urls if "Fast Partner Server" not in url.title and "Slow Partner Server" not in url.title
            ]
            if other_links:
                chain.append(Plain("\n第三方链接：\n"))
                for index, url in enumerate(other_links, 1):
                    chain.append(Plain(f"{index}. {url.url}\n"))

            node = Node(uin=event.get_self_id(), name="Anna's Archive", content=chain)
            return [event.chain_result([node])]
        except Exception as e:
            logger.error(f"[Anna's Archive] 下载失败：{e}")
            return [event.plain_result(f"[Anna's Archive] 下载电子书时发生错误，请稍后再试：{e}")]

    @staticmethod
    def _filename_from_url(url: str) -> str:
        path = urlparse(url).path
        last = path.rsplit("/", 1)[-1]
        return unquote(last) if last else ""

    @staticmethod
    def _rewrite_to_short_url(url: str, short_name: str) -> str:
        u = urlparse(url)
        head, _, _ = u.path.rpartition("/")
        new_path = f"{head}/{short_name}" if head else f"/{short_name}"
        return u._replace(path=new_path).geturl()
