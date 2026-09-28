import asyncio
import random
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from urllib.parse import quote_plus, urljoin, unquote

import aiofiles
import aiohttp
from astrbot.api.all import Plain, Image, Node, Nodes, File, logger
from .utils import (
    DOWNLOAD_TIMEOUT,
    SharedSession,
    discard_temp_file,
    download_and_convert_to_base64,
    excerpt_for_query,
    filter_unsafe,
    format_bytes,
    is_base64_image,
    is_valid_calibre_book_url,
    make_temp_download_path,
    match_score,
    normalize_match_text,
    schedule_temp_cleanup,
    split_url_credentials,
    truncate_filename,
    upload_cleanup_delay,
)

# 0 命中退化时最多单独发多少个词的请求，防止长查询把请求数打爆。
MAX_FALLBACK_TOKENS = 6
MAX_RECOMMEND = 50
# /opds/discover 每次随机返回 books_per_page（默认 60）本，不够 n 本时最多再要几次。
MAX_DISCOVER_ROUNDS = 3
FILTER_FIELDS = ["title", "authors", "publisher", "summary"]


class CalibreSource(SharedSession):
    def __init__(
        self, config, proxy: str, max_results: int, temp_path: str, safety_checker=None
    ):
        super().__init__(proxy)
        self.config = config
        self.max_results = max_results
        self.temp_path = temp_path
        self.safety_checker = safety_checker

    def _base_url(self) -> str:
        return (self.config.get("calibre_web_url", "http://127.0.0.1:8083") or "").rstrip("/")

    def _auth(self, book_url: str = None):
        """URL 自带的凭据优先，否则回退到配置里 calibre_web_url 的凭据。"""
        for candidate in (book_url, self._base_url()):
            if not candidate:
                continue
            _, creds = split_url_credentials(candidate)
            if creds:
                return aiohttp.BasicAuth(creds[0], creds[1])
        return None

    async def _search_calibre_web(self, query: str, limit: int = None):
        # 整串查询优先（命中即收工），0 命中才退化为逐词搜索合并重排。
        results = await self._query_opds(query)
        if results == []:
            results = await self._fallback_search(query) or []
        if results is None:
            return None
        return self._rank_results(results, query, limit)

    async def _query_opds(self, query: str):
        # query 收原始关键词，编码在这里做：_parse_opds_response 要拿没编码的串来算相关性。
        return await self._fetch_opds(f"/opds/search/{quote_plus(query)}")

    async def _fetch_opds(self, path: str):
        """GET 一个 OPDS feed 并解析成条目列表；非 200 / 非 atom 返回 None。"""
        calibre_web_url, _ = split_url_credentials(self._base_url())
        session = await self.get_session()
        async with session.get(
            f"{calibre_web_url}{path}", proxy=self.proxy, auth=self._auth()
        ) as response:
            if response.status == 200:
                content_type = response.headers.get("Content-Type", "")
                if "application/atom+xml" in content_type:
                    data = await response.text()
                    return self._parse_opds_response(data)
                logger.error(f"[Calibre-Web] Unexpected content type: {content_type}")
            else:
                logger.error(
                    f"[Calibre-Web] {path.split('/')[2]} 请求失败：Calibre-Web returned status code {response.status}"
                )
            return None

    async def _fallback_search(self, query: str):
        """整串查询 0 命中时的逐词退化：把空格拆开的词单独发请求，合并去重。

        Why: CWA 的 search_query 把整个 query 串当作 title/tags/series/publisher
        的 ilike 短语，作者字段则要求单个作者名同时包含所有空格/逗号拆开的词——
        「王庭槐 - 2024 - 生理学」这类作者+年份+书名混合查询整串发出去必然 0 命中。
        逐词搜索后合并，再由 _relevance_rank 决定排序。纯标点/纯数字词
        （如 "-"、"2024"）不单独发请求：前者无信息量，年份进不了 CWA 的搜索字段，
        只会放大噪音池。
        """
        pieces = re.split(r"[\s\u2013\u2014]+", (query or "").strip())
        expanded = []
        for piece in pieces:
            expanded.extend(p for p in re.split(r"-", piece) if p)
        meaningful = [t for t in dict.fromkeys(expanded) if re.search(r"[^\W\d_]", t)]
        if len(expanded) < 2 or not meaningful:
            return None

        token_items = await asyncio.gather(
            *(self._query_opds(t) for t in meaningful[:MAX_FALLBACK_TOKENS])
        )
        merged = {}
        for items in token_items:
            if not items:
                continue
            for item in items:
                merged.setdefault(self._book_key(item), item)
        return list(merged.values()) or None

    @staticmethod
    def _book_key(item: dict) -> tuple:
        # OPDS 下载链接带 calibre 内部 book id（/opds/download/<id>/...），
        # 去重比书名+作者稳；没有下载链接时退回书名+作者。
        m = re.match(r"^.*?/opds/download/(\d+)/", item.get("download_link") or "")
        if m:
            return ("id", int(m.group(1)))
        return ("title", item.get("title"), item.get("authors"))

    def _rank_results(self, results: list, query: str, limit: int = None):
        q = normalize_match_text(query)
        tokens = [
            t for t in (normalize_match_text(w) for w in (query or "").split()) if t
        ]
        if q:
            results.sort(key=lambda item: self._relevance_rank(item, q, tokens))
        if limit is not None:
            return results[:limit]
        return results

    @staticmethod
    def _relevance_rank(item: dict, query_norm: str, tokens: list) -> tuple:
        """相关性排序键，越小越靠前。query_norm/tokens 均为 normalize_match_text 后的形态。

        Why: Calibre-Web 的搜索是 tags/series/authors/publishers/title 的 OR
        (cps/db.py search_query)，命中后按书名码点排序 (order_by(Books.sort))，
        中文没有 collation。结果是搜「植物学」时 29 卷《中国植物志》靠标签全部命中，
        而「中」(U+4E2D) < 「植」(U+690D)，书名就叫《植物学》的教材被挤到第 32 条，
        插件取前 10 条时一本都看不到。

        服务端没法下精确条件（CWA 的 OPDS 搜索不认 calibre 的 title: 语法），
        所以在这里按命中位置重排，把标题命中的提到标签命中的前面。
        第 5 档按 match_score 分级：只靠 tags/series 命中（或拼音模糊命中）的
        书彼此不全相同——标题/作者部分覆盖 query 的排前面，一个字符都没沾的
        落进第 6 档原样保留码点序。

        第 4 档额外覆盖跨字段命中：多词查询里至少一个词命中作者、且至少一个词
        命中标题时（如「王庭槐 生理学」→ 作者=王庭槐 + 书名=生理学），整串在
        CWA 上必然 0 命中，只能靠 _fallback_search 的逐词合并捞回来，这里按
        作者+标题双命中给到作者档，而不是按部分命中沉到第 5 档。
        """
        title = normalize_match_text(item.get("title"))
        authors = normalize_match_text(item.get("authors"))
        publisher = normalize_match_text(item.get("publisher"))
        hits = sum(1 for t in tokens if t in title)

        if not query_norm:
            return (6, 0, 0)
        if title == query_norm:
            return (0, -hits, len(title))
        if title.startswith(query_norm):
            return (1, -hits, len(title))
        if query_norm in title:
            return (2, -hits, len(title))
        if tokens and hits == len(tokens):
            return (3, -hits, len(title))
        if query_norm in authors or (
            tokens and any(t in authors for t in tokens) and hits >= 1
        ):
            return (4, -hits, len(title))
        score = max(
            match_score(query_norm, title),
            match_score(query_norm, authors),
            match_score(query_norm, publisher),
        )
        if score:
            return (5, -score, len(title))
        return (6, 0, 0)  # 只靠 tags/series 命中的，或拼音模糊命中的

    def _parse_opds_response(self, xml_data: str):
        # 封面由插件自己拉取再转 base64，URL 不外泄，保留凭据让 aiohttp 直接带上；
        # 下载链接要发给用户，必须用去掉凭据的干净地址。
        calibre_web_url = self._base_url()
        public_url, _ = split_url_credentials(calibre_web_url)
        xml_data = re.sub(r"[^\x09\x0A\x0D\x20-\uD7FF\uE000-\uFFFD]", "", xml_data)
        xml_data = re.sub(r"\s+", " ", xml_data)

        try:
            root = ET.fromstring(xml_data)
            namespace = {
                "default": "http://www.w3.org/2005/Atom",
                "dcterms": "http://purl.org/dc/terms/",
            }
            entries = root.findall("default:entry", namespace)

            results = []
            for entry in entries:
                title_element = entry.find("default:title", namespace)
                title = title_element.text if title_element is not None else "未知"

                authors = []
                author_elements = entry.findall(
                    "default:author/default:name", namespace
                )
                for author in author_elements:
                    authors.append(author.text if author is not None else "未知")
                authors = ", ".join(authors) if authors else "未知"

                summary_element = entry.find("default:summary", namespace)
                summary = (
                    summary_element.text if summary_element is not None else "无描述"
                )

                published_element = entry.find("default:published", namespace)
                if published_element is not None and published_element.text:
                    try:
                        year = datetime.fromisoformat(published_element.text).year
                    except ValueError:
                        year = "未知"
                else:
                    year = "未知"

                # dcterms 是独立命名空间，不能挂在 default: 下面（那样永远找不到）。
                lang_elements = entry.findall("dcterms:language", namespace)
                language = ", ".join(e.text.strip() for e in lang_elements if e.text and e.text.strip()) or "未知"

                publisher_element = entry.find(
                    "default:publisher/default:name", namespace
                )
                publisher = (
                    publisher_element.text if publisher_element is not None else "未知"
                )

                cover_element = entry.find(
                    "default:link[@rel='http://opds-spec.org/image']", namespace
                )
                cover_suffix = (
                    cover_element.attrib.get("href", "")
                    if cover_element is not None
                    else ""
                )
                if cover_suffix and re.match(r"^/opds/cover/\d+$", cover_suffix):
                    cover_link = urljoin(calibre_web_url, cover_suffix)
                else:
                    cover_link = ""

                thumbnail_element = entry.find(
                    "default:link[@rel='http://opds-spec.org/image/thumbnail']",
                    namespace,
                )
                thumbnail_suffix = (
                    thumbnail_element.attrib.get("href", "")
                    if thumbnail_element is not None
                    else ""
                )
                if thumbnail_suffix and re.match(
                    r"^/opds/cover/\d+$", thumbnail_suffix
                ):
                    thumbnail_link = urljoin(calibre_web_url, thumbnail_suffix)
                else:
                    thumbnail_link = ""

                acquisition_element = entry.find(
                    "default:link[@rel='http://opds-spec.org/acquisition']", namespace
                )
                if acquisition_element is not None:
                    download_suffix = (
                        acquisition_element.attrib.get("href", "")
                        if acquisition_element is not None
                        else ""
                    )
                    if download_suffix and re.match(
                        r"^/opds/download/\d+/[\w]+/$", download_suffix
                    ):
                        download_link = urljoin(public_url, download_suffix)
                    else:
                        download_link = ""
                    file_type = acquisition_element.attrib.get("type", "未知")
                    file_size = acquisition_element.attrib.get("length", "未知")
                else:
                    download_link = ""
                    file_type = "未知"
                    file_size = "未知"

                isbn_values = []
                for ident in entry.findall("dcterms:identifier", namespace):
                    text = (ident.text or "").strip()
                    if text:
                        isbn_values.append(text)
                identifier = ", ".join(isbn_values)

                results.append(
                    {
                        "title": title,
                        "authors": authors,
                        "summary": summary,
                        "year": year,
                        "publisher": publisher,
                        "language": language,
                        "cover_link": cover_link,
                        "thumbnail_link": thumbnail_link,
                        "download_link": download_link,
                        "file_type": file_type,
                        "file_size": file_size,
                        "identifier": identifier,
                    }
                )

            # 排序和截断由 _rank_results 统一做：OPDS 一次把全部命中都返回了，
            # 逐词合并出来的池子也要在同一处重排（list.sort 稳定，同档内保持
            # Calibre-Web 原本的书名顺序；关键词一个字都没命中时全部落在最后一档，
            # 等价于不排序，不会把结果搅乱）。
            return results
        except ET.ParseError as e:
            logger.error(f"[Calibre-Web] Error parsing OPDS response: {e}")
            return None

    async def _build_book_chain(self, item: dict, query: str = "") -> list:
        chain = [Plain(f"{item['title']}\n")]
        enable_cover = self.config.get("enable_cover_image", True)
        cover_max = int(self.config.get("cover_max_size", 400) or 0)
        if enable_cover and item.get("cover_link"):
            base64_image = await download_and_convert_to_base64(
                item["cover_link"], proxy=self.proxy, max_edge=cover_max
            )
            if is_base64_image(base64_image):
                chain.append(Image.fromBase64(base64_image))
        chain.append(Plain(f"作者: {item.get('authors', '未知')}\n"))
        chain.append(Plain(f"年份: {item.get('year', '未知')}\n"))
        chain.append(Plain(f"出版社: {item.get('publisher', '未知')}\n"))
        language = item.get("language")
        if language and language != "未知":
            chain.append(Plain(f"语言: {language}\n"))
        ftype = (item.get("file_type") or "").strip()
        fsize_raw = item.get("file_size")
        fsize = (
            format_bytes(fsize_raw)
            if fsize_raw and str(fsize_raw).isdigit()
            else (str(fsize_raw) if fsize_raw and fsize_raw != "未知" else "")
        )
        fmt_parts = []
        if ftype and ftype != "未知":
            fmt_parts.append(
                ftype.split("/")[-1].upper() if "/" in ftype else ftype.upper()
            )
        if fsize:
            fmt_parts.append(fsize)
        if fmt_parts:
            chain.append(Plain(f"文件: {' · '.join(fmt_parts)}\n"))
        identifier = (item.get("identifier") or "").strip()
        if identifier:
            chain.append(Plain(f"标识: {identifier}\n"))
        description = item.get("summary", "")
        if isinstance(description, str) and description != "":
            description = excerpt_for_query(description, query, 150)
        else:
            description = "无简介"
        chain.append(Plain(f"简介: {description}\n"))
        download_link = item.get("download_link", "")
        if download_link:
            chain.append(Plain(f"下载命令:\n/calibre download {download_link}"))
        else:
            chain.append(Plain("下载命令: (无可用直链)"))
        return chain

    def _filter(self, results: list) -> list:
        return filter_unsafe(results, self.safety_checker, fields=FILTER_FIELDS, source="Calibre-Web")

    async def _convert_calibre_results_to_nodes(self, event, results: list, query: str = ""):
        if not results:
            return "[Calibre-Web] 未找到匹配的电子书。"

        async def construct_node(book):
            chain = await self._build_book_chain(book, query)
            return Node(
                uin=event.get_self_id(),
                name="Calibre-Web",
                content=chain,
            )

        tasks = [construct_node(book) for book in results]
        return await asyncio.gather(*tasks)

    async def search_nodes(self, event, query: str, limit: str | int = ""):
        if not self.config.get("enable_calibre", False):
            return "[Calibre-Web] 功能未启用。"

        if not query:
            return "[Calibre-Web] 请提供电子书关键词以进行搜索。"

        limit = int(limit) if str(limit).isdigit() else int(self.max_results)
        if not (1 <= limit <= 100):
            return "[Calibre-Web] 请确认搜索返回结果数量在 1-100 之间。"

        try:
            logger.info(
                f"[Calibre-Web] Received books search query: {query}, limit: {limit}"
            )
            results = await self._search_calibre_web(query)
            if results is None:
                # _fetch_opds returns None on non-200 / non-atom / unparsable feeds.
                return "[Calibre-Web] 无法从书库获取搜索结果（服务不可用或账号密码错误，详见日志）。"
            if not results:
                return "[Calibre-Web] 未找到匹配的电子书。"
            # 先过滤再截断：先截到 limit 再过滤，掉几条就少几条，凑不满用户要的数量。
            results = self._filter(results)
            if not results:
                return "[Calibre-Web] 全部结果被内容过滤丢弃。"
            return await self._convert_calibre_results_to_nodes(event, results[:limit], query)
        except Exception as e:
            logger.error(f"[Calibre-Web] 搜索失败: {e}")
            return "[Calibre-Web] 搜索电子书时发生错误，请稍后再试。"

    async def download(self, event, book_url: str = ""):
        if not self.config.get("enable_calibre", False):
            return [event.plain_result("[Calibre-Web] 功能未启用。")]

        if not is_valid_calibre_book_url(book_url):
            return [event.plain_result("[Calibre-Web] 请提供有效的电子书链接。")]

        auth = self._auth(book_url)
        book_url, _ = split_url_credentials(str(book_url).strip())

        temp_file_path = None
        try:
            session = await self.get_session()
            async with session.get(
                book_url, proxy=self.proxy, auth=auth, timeout=DOWNLOAD_TIMEOUT
            ) as response:
                if response.status == 200:
                    content_disposition = response.headers.get("Content-Disposition")
                    book_name = None

                    if content_disposition:
                        book_name_match = re.search(
                            r"filename\*=(?:UTF-8\'\')?([^;]+)", content_disposition
                        )
                        if book_name_match:
                            book_name = unquote(book_name_match.group(1))
                        else:
                            book_name_match = re.search(
                                r'filename=["\']?([^;\']+)["\']?', content_disposition
                            )
                            if book_name_match:
                                book_name = book_name_match.group(1)

                    if not book_name or book_name.strip() == "":
                        logger.error(
                            f"[Calibre-Web] 无法提取书名，电子书地址: {book_url}"
                        )
                        return [
                            event.plain_result(
                                "[Calibre-Web] 无法提取书名，取消发送电子书。"
                            )
                        ]

                    # 不能把 URL 直接丢给协议端：Calibre-Web 多半要 basic auth，
                    # 而 llbot/napcat 的 fetch 拒收带凭据的 URL，也没法替它带
                    # Authorization 头。插件自己下到临时目录再发本地文件。
                    book_name = truncate_filename(book_name)
                    temp_file_path = make_temp_download_path(self.temp_path, book_name)

                    size = 0
                    async with aiofiles.open(temp_file_path, "wb") as temp_file:
                        async for chunk in response.content.iter_chunked(64 * 1024):
                            size += len(chunk)
                            await temp_file.write(chunk)

                    logger.info(
                        f"[Calibre-Web] 文件已下载并保存到临时目录：{temp_file_path}"
                    )
                    file = File(name=book_name, file=temp_file_path)
                    # 协议端是收到 send 动作后才来拉文件的，删早了它只能拿到残片。
                    schedule_temp_cleanup(temp_file_path, upload_cleanup_delay(size))
                    return [event.chain_result([file])]
                if response.status in (401, 403):
                    logger.error(
                        f"[Calibre-Web] 下载被拒绝，状态码: {response.status}，检查 calibre_web_url 里的账号密码。"
                    )
                    return [
                        event.plain_result(
                            f"[Calibre-Web] 下载被拒绝（{response.status}），请检查 Calibre-Web 账号密码配置。"
                        )
                    ]
                return [
                    event.plain_result(
                        f"[Calibre-Web] 无法下载电子书，状态码: {response.status}"
                    )
                ]
        except Exception as e:
            logger.error(f"[Calibre-Web] 下载失败: {type(e).__name__}: {e}")
            discard_temp_file(temp_file_path)
            return [
                event.plain_result("[Calibre-Web] 下载电子书时发生错误，请稍后再试。")
            ]

    async def _random_pool(self, n: int):
        """从 /opds/discover 取随机书目（按 book id 去重），不够 n 本时再要几轮。

        Why: 以前是搜索 "*"。CWA 的 LIKE 把 * 当普通字符，只命中名字/标签里恰好带
        星号的十几本书，「随机推荐」永远在这十几本里打转。/opds/discover 是 CWA
        自带的 ORDER BY random() 书目。关掉了「随机书籍」栏目的实例会 404，
        那就退回最新书目 /opds/new。
        """
        pool = {}
        for _ in range(MAX_DISCOVER_ROUNDS):
            items = await self._fetch_opds("/opds/discover")
            if items is None:
                break
            for item in items:
                pool.setdefault(self._book_key(item), item)
            if len(pool) >= n or not items:
                return list(pool.values())
        if pool:
            return list(pool.values())
        logger.warning("[Calibre-Web] /opds/discover 不可用，改从最新书目中随机挑选。")
        return await self._fetch_opds("/opds/new")

    async def recommend(self, event, n: int):
        if not self.config.get("enable_calibre", False):
            return [event.plain_result("[Calibre-Web] 功能未启用。")]

        if not isinstance(n, int) or not (1 <= n <= MAX_RECOMMEND):
            return [event.plain_result(f"[Calibre-Web] 推荐数量需在 1-{MAX_RECOMMEND} 之间。")]

        try:
            results = self._filter(await self._random_pool(n) or [])
            if not results:
                return [event.plain_result("[Calibre-Web] 未找到可推荐的电子书。")]

            n = min(n, len(results))
            recommended_books = random.sample(results, n)
            result = await self._convert_calibre_results_to_nodes(
                event, recommended_books
            )

            if isinstance(result, str):
                return [event.plain_result(result)]
            guidance = f"[Calibre-Web] 如下是随机推荐的 {n} 本电子书。"
            nodes = [
                Node(
                    uin=event.get_self_id(),
                    name="Calibre-Web",
                    content=[Plain(guidance)],
                )
            ]
            nodes.extend(result)
            return [event.chain_result([Nodes(nodes)])]
        except Exception as e:
            logger.error(f"[Calibre-Web] 推荐电子书时发生错误: {e}")
            return [
                event.plain_result("[Calibre-Web] 推荐电子书时发生错误，请稍后再试。")
            ]

    async def close(self):
        await self.close_session()
