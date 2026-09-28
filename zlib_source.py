import asyncio
import os
from urllib.parse import urlparse

from astrbot.api.all import Plain, Image, Node, File, MessageChain, logger

from .Zlibrary import Zlibrary, ZlibraryError
from .utils import (
    discard_temp_file,
    download_and_convert_to_base64,
    excerpt_for_query,
    filter_unsafe,
    get_rerank_provider,
    html_to_text,
    is_base64_image,
    is_valid_zlib_book_hash,
    is_valid_zlib_book_id,
    isbn_set,
    make_temp_download_path,
    match_score,
    normalize_match_text,
    rerank_inputs,
    schedule_temp_cleanup,
    strip_edition_word,
    truncate_filename,
    upload_cleanup_delay,
)

MAX_ZLIB_RETRY_COUNT = 3
MAX_ZLIB_SEARCH_RETRY_COUNT = 3
ZLIB_LOGIN_BACKOFF = 1.5
DEFAULT_ZLIB_BASE_URL = "https://z-library.ec/"
# 一页多拉一些候选，给过滤和可选 rerank 留余地。搜索不消耗下载额度，只多花一点延迟。
# 上游对中文是字符碎片匹配，真正相关的书常被热门榜垃圾压在 100-200 名
# （实测「植物保护案例分析」的植保类书集中在 100-200 位）。但命中良好的
# 查询首页就够用，所以只拉一页；池里全是近似命中时再往深拉一页。
ZLIB_CANDIDATE_POOL = 100
# 弱命中时最多加深的页数。2 页 × 100 = 每 order 覆盖上游前 ~200 位。
ZLIB_DEEP_PAGES = 2
# 会话 key 失效时 /eapi/book/{id}/{hash}/file 回 {"success":0,"error":"Invalid credentials"}
# （搜索接口不校验登录，照常返回），只能在下载时识别出来再重登。
AUTH_ERROR_MARKERS = ("invalid credentials", "please login", "not logged in", "unauthorized")
# 两种排序都拉一遍取并集提高召回；合并后按各自在上游的最好名次排——直接信上游排序，
# 本地不再做启发式重排（开启 rerank 时由模型重排）。None = 不传 order（等价于 popular）。
ZLIB_SEARCH_ORDERS = (None, "bestmatch")


class ZlibSource:
    def __init__(self, config, proxy: str, max_results: int, temp_path: str, safety_checker=None, context=None):
        self.config = config
        self.proxy = proxy
        self.max_results = max_results
        self.temp_path = temp_path
        self.safety_checker = safety_checker
        self.context = context
        self.base_url = (self.config.get("zlib_base_url", DEFAULT_ZLIB_BASE_URL) or DEFAULT_ZLIB_BASE_URL).rstrip("/")
        self.domain = urlparse(self.base_url).netloc or urlparse(DEFAULT_ZLIB_BASE_URL).netloc
        self.zlibrary = Zlibrary(domain=self.domain)
        self.last_login_error = ""
        # zlib CDN 会掐断同账号/IP 的并发下载流（h2 INTERNAL_ERROR），下载串行排队。
        self._download_lock = asyncio.Lock()
        self._login_lock = asyncio.Lock()
        self._login_task = None
        self._init_login()

    def _init_login(self):
        if not self.config.get("enable_zlib", False):
            return
        email = self.config.get("zlib_email", "").strip()
        password = self.config.get("zlib_password", "").strip()
        if not (email and password):
            self.disable("未设置 Z-Library 账户，禁用该平台。")
            return
        # 以前在这里同步登录：curl 超时 5+30 秒，插件每次加载/重载都把整个事件循环
        # 卡住。改成后台预热，失败了也不要紧——搜索/下载前 _ensure_login 会按需重登。
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._login_task = loop.create_task(self._warm_up_login())

    async def _warm_up_login(self):
        if await self._ensure_login():
            logger.info("[ebooks] 已登录 Z-Library。")
        else:
            logger.error(f"[ebooks] 登录 Z-Library 失败：{self.last_login_error or '原因未知'}（使用时会再重试）")

    def _reset_login(self):
        """丢掉失效的会话，下次 _ensure_login 会重新登录。"""
        self.zlibrary = Zlibrary(domain=self.domain)

    @staticmethod
    def _is_auth_error(message) -> bool:
        text = str(message or "").lower()
        return any(marker in text for marker in AUTH_ERROR_MARKERS)

    def disable(self, reason: str):
        self.zlibrary = Zlibrary(domain=self.domain)
        self.config["enable_zlib"] = False
        self.config.save_config()
        logger.info(f"[ebooks] {reason}")

    async def terminate(self):
        if self._login_task and not self._login_task.done():
            self._login_task.cancel()
        if self.zlibrary and self.zlibrary.isLoggedIn():
            self.zlibrary = Zlibrary(domain=self.domain)

    async def _ensure_login(self):
        # 并发的搜索/下载同时发现没登录时只登一次，其余等结果。
        async with self._login_lock:
            return await self._login_locked()

    async def _login_locked(self):
        # 每轮都清掉，免得调用方读到上一次失败留下的陈旧原因。
        self.last_login_error = ""
        if self.zlibrary.isLoggedIn():
            return True

        email = self.config.get("zlib_email", "").strip()
        password = self.config.get("zlib_password", "").strip()
        for attempt in range(MAX_ZLIB_RETRY_COUNT):
            try:
                await asyncio.to_thread(self.zlibrary.login, email, password)
                if self.zlibrary.isLoggedIn():
                    return True
                self.last_login_error = "账号或密码被拒绝"
            except Exception as e:
                # 以前这里是 except: pass，上游抽风时日志里只剩一句「登录失败」，
                # 查不出原因；真实报错必须留下来。
                self.last_login_error = f"{type(e).__name__}: {e}"
                logger.warning(f"[Z-Library] 登录第 {attempt + 1} 次失败：{self.last_login_error}")
            # 失败多半是上游短时抽风，三次连着打完只花几秒，等于没重试。
            if attempt < MAX_ZLIB_RETRY_COUNT - 1:
                await asyncio.sleep(ZLIB_LOGIN_BACKOFF * (2 ** attempt))
        return False

    async def _search_one(self, order, search_kwargs, page: int = 1):
        """按单一 order 拉一页，带重试。返回 (books, had_exception)。"""
        kwargs = dict(search_kwargs)
        kwargs["page"] = page
        if order:
            kwargs["order"] = order
        had_exception = False
        for attempt in range(MAX_ZLIB_SEARCH_RETRY_COUNT):
            try:
                results = await asyncio.to_thread(self.zlibrary.search, **kwargs)
                if results and results.get("books"):
                    for idx, b in enumerate(results["books"]):
                        # 标注上游名次，跨 order/跨页合并时按最好名次保留上游排序。
                        b["_urank"] = (page - 1) * ZLIB_CANDIDATE_POOL + idx
                        # 简介里的 <br>/<p>/&nbsp; 在入口处转成纯文本，过滤、匹配、摘录、rerank 都用它。
                        b["description"] = html_to_text(b.get("description"))
                    return results["books"], had_exception
                # 上游好好地应答了、只是真没命中：再问两遍还是没命中，白等 4 秒。
                if isinstance(results, dict) and results.get("success"):
                    return [], had_exception
            except Exception as e:
                had_exception = True
                logger.warning(
                    f"[Z-Library] Search attempt {attempt + 1} (order={order or 'default'}) failed: {e}"
                )
            if attempt < MAX_ZLIB_SEARCH_RETRY_COUNT - 1:
                await asyncio.sleep(0.5)
        return [], had_exception

    @staticmethod
    def _absorb(batches, pool, seen):
        """把一批 (books, had_exception) 按 id 去重并入 pool，返回是否有异常。"""
        had_exception = False
        for books, err in batches:
            had_exception = had_exception or err
            for book in books:
                book_id = str(book.get("id") or "")
                if not book_id:
                    continue
                if book_id in seen:
                    # 同一本书在另一个 order/页里名次更好，记到已入池的那条上。
                    prev = seen[book_id]
                    if book.get("_urank", 1 << 30) < prev.get("_urank", 1 << 30):
                        prev["_urank"] = book["_urank"]
                    continue
                seen[book_id] = book
                pool.append(book)
        return had_exception

    @staticmethod
    def _dedupe(books):
        """只合并确定是同一份文件的记录：格式相同、ISBN 相同，且文件大小或哈希（md5/sha256）相同。

        不看书名/作者：同名的第 7 版和第 8 版、不同年份的同名期刊都是不同的书。
        没有 ISBN 的记录一律不合并。实测上游已经去掉了 md5 相同的文件，这里只兜底。
        """
        seen, kept = set(), []
        for book in books:
            isbns = isbn_set(book.get("identifier"))
            ext = str(book.get("extension") or "").strip().lower()
            keys = []
            if isbns and ext:
                size = str(book.get("filesize") or "").strip()
                if size and size != "0":
                    keys.append(("size", ext, isbns, size))
                for field in ("md5", "sha256"):
                    digest = str(book.get(field) or "").strip().lower()
                    if digest:
                        keys.append((field, ext, isbns, digest))
            if any(k in seen for k in keys):
                continue
            seen.update(keys)
            kept.append(book)
        return kept

    @classmethod
    def _pool_is_weak(cls, pool, query_norm, tokens) -> bool:
        """值得往深挖：池里最好的书也只够得上部分命中（tier>=5）。"""
        for b in pool:
            if cls._relevance_key(b, query_norm, tokens)[0] <= 4:
                return False
        return bool(pool)

    async def _collect_books(self, search_kwargs, query_norm: str, tokens: list):
        """两种 order 各拉一页起步；池里全是近似命中时才再往深拉，返回 (books, had_exception)。

        池子按书在任一 order 里的最好名次排——直接信上游排序，本地不再做启发式重排
        （开启 rerank 时由模型对头部候选重排）。
        一种 order 挂掉不影响另一种，两种都挂了才算失败。

        上游对中文是字符碎片匹配，真正相关的书常被热门榜压在 100-200 位；
        但整串/作者能命中的查询首页池就够用了，不值得每次都拉满几百条。
        """
        first = await asyncio.gather(
            *[self._search_one(order, search_kwargs, 1) for order in ZLIB_SEARCH_ORDERS]
        )
        pool, seen = [], {}
        had_exception = self._absorb(first, pool, seen)

        if self._pool_is_weak(pool, query_norm, tokens):
            # 只有近似命中或完全没命中：上一页拉满过的 order 才往深翻，
            # 一页不满说明这个排序下结果已经见底，再翻也是空页。
            last_full = [order for order, (books, _) in zip(ZLIB_SEARCH_ORDERS, first)
                         if len(books) >= ZLIB_CANDIDATE_POOL]
            for page in range(2, ZLIB_DEEP_PAGES + 1):
                if not last_full:
                    break
                batches = await asyncio.gather(
                    *[self._search_one(order, search_kwargs, page) for order in last_full]
                )
                had_exception = self._absorb(batches, pool, seen) or had_exception
                last_full = [order for order, (books, _) in zip(last_full, batches)
                             if len(books) >= ZLIB_CANDIDATE_POOL]
                if not self._pool_is_weak(pool, query_norm, tokens):
                    break
        pool.sort(key=lambda b: b.get("_urank", 1 << 30))
        return pool, had_exception

    @staticmethod
    def _relevance_key(book, query_norm: str, tokens: list) -> tuple:
        """命中强度分级键，越小命中越强；只用于弱命中判定（要不要往深翻页）和 rerank 候选窗口的优先级，不再用于排序。query_norm/tokens 均为 normalize_match_text 后的形态。

        Why: /eapi/book/search 不传 order 时等价于 order=popular —— 按下载热度排，
        不是按相关度。中文又是按字碎片命中（搜「生理学」，标题里有「生活」+
        「心理学」的照样算命中），两件事叠起来的结果就是前十条全是《心理学与生活》
        《理解人性》这类中文电子书畅销书，真正叫《生理学》的教材被挤到几百条之后。
        单换 order=bestmatch 只是换个姿势翻车：「生理学」确实变好，「发育生物学」
        反而从「植物发育生物学 / Gilbert 第 11 版」退化成初中生物教科书。
        所以两种 order 都拉，命中位置在本地判。

        分档比连续打分好维护，也更好解释。第 5 档不能再是「命中就算一档」：
        无空格中文查询是单 token，hits 退化成「整串是否为标题子串」的二值判断，
        整串没命中时所有候选拿到完全相同的键，等于没重排（实测「植物保护案例
        分析」44 条全部落进 (6,0,0)，返回的是热度榜原序）。改为 match_score
        按位置加权覆盖度分级：「植物保护专业英语教程」命中「植物保护」前缀，
        天然排在只命中「案例分析」的《物业管理案例分析》前面。
        关键词一个字都没命中的统一落到最后一档，键完全相同，稳定排序会原样
        保留上游顺序，不会把结果搅乱。
        """
        title = normalize_match_text(book.get("title"))
        author = normalize_match_text(book.get("author"))
        publisher = normalize_match_text(book.get("publisher"))
        description = normalize_match_text(book.get("description"))
        hits = sum(1 for t in tokens if t in title)

        if not query_norm:
            return (6, 0, 0)
        if title == query_norm:
            tier = 0
        elif title.startswith(query_norm):
            tier = 1
        elif query_norm in title:
            tier = 2
        elif tokens and hits == len(tokens):
            tier = 3  # 词都在，只是不连续（「生理学 第8版」这种多词查询）
        elif query_norm in author:
            tier = 4
        else:
            score = max(
                match_score(query_norm, title),
                match_score(query_norm, author),
                match_score(query_norm, publisher),
                match_score(query_norm, description),
            )
            if score:
                # 部分命中：覆盖度越高的越靠前；再同就挑标题短的。
                return (5, -score, len(title))
            return (6, 0, 0)
        # 同档内命中词多的优先；再同就挑标题短的 ——《生理学》要排在
        # 《医学生理学和生物物理学 下》前面。
        return (tier, -hits, len(title))

    def _get_rerank_provider(self):
        """从 AstrBot 服务提供商里挑一个已启用的 rerank provider；没有返回 None。"""
        return get_rerank_provider(self.context, self.config)

    @staticmethod
    def _field(book, key: str) -> str:
        """A book field as display text; Z-Library sends missing values as "" or the string "None"."""
        value = str(book.get(key) or "").strip()
        return "" if value.lower() == "none" else value

    @classmethod
    def _edition_text(cls, book) -> str:
        """Z-Library's edition field verbatim (「7」「5th ed.」「First Edition」), not interpreted."""
        return cls._field(book, "edition")[:30]

    @classmethod
    def _book_doc(cls, book, max_chars: int = 300, query: str = "") -> str:
        """拼给 reranker 的短文档：书名 + 版次/作者/年份/出版社/格式 + 简介摘录（开头 + 查询命中处）。

        One labeled line per field, like the result card, so the reranker can tell which value
        is the edition. Measured with Qwen3-Reranker-4B and the README instruction on
        "Fundamentals of Biostatistics 8th edition": edition "8"/"8th"/"Eighth Edition" scored
        0.53-0.56, "7" 0.29, no edition 0.42. A single "title / 版次: 8 / author" row (measured
        without descriptions) scored the 8th edition 0.38 against 0.37 for no edition.
        """
        lines = [cls._field(book, "title")]
        for label, value in (
            ("版次", cls._edition_text(book)),
            ("作者", cls._field(book, "author")),
            ("年份", cls._field(book, "year")),
            ("出版社", cls._field(book, "publisher")),
            ("格式", cls._field(book, "extension").upper()),
        ):
            if value:
                lines.append(f"{label}: {value}")
        doc = "\n".join(line for line in lines if line)
        desc = str(book.get("description") or "").strip()
        budget = max_chars - len(doc) - 5
        if desc and budget > 20:
            doc += "\n简介: " + excerpt_for_query(desc, query, budget, head_chars=budget // 3)
        return doc[:max_chars]

    async def _rerank_books(self, query: str, books: list, limit: int, front=None):
        """可选 rerank：对头部候选调 rerank 模型重排。

        front(book) 返回非 None 的书按其返回值先挪进候选窗口（同值保持原序），再按上游序补满窗口：
        上游序只看热度/粗匹配，「data analysis」里书名就叫《Data Analysis》的
        书排在第 116 位，窗口只有 rerank_candidates 条，模型根本看不到它。
        返回 (books, top_score|None)；未启用、无 provider 或调用失败一律回退传入的顺序。
        """
        if not self.config.get("enable_rerank", False) or not books:
            return books, None
        provider = self._get_rerank_provider()
        if provider is None:
            logger.warning("[Z-Library] enable_rerank 已开但 AstrBot 没有启用的 rerank provider，回退上游排序。")
            return books, None

        ordered = books
        if front is not None:
            head, tail = [], []
            for b in books:
                rank = front(b)
                (tail if rank is None else head).append((rank, b))
            head.sort(key=lambda rb: rb[0])
            ordered = [b for _, b in head] + [b for _, b in tail]
        top_k = int(self.config.get("rerank_candidates", 60) or 60)
        max_chars = int(self.config.get("rerank_doc_max_chars", 300) or 300)
        pool = ordered[:max(top_k, 1)]
        try:
            rq, docs = rerank_inputs(self.config, query, [self._book_doc(b, max_chars, query) for b in pool])
            results = await provider.rerank(rq, docs, top_n=limit)
        except Exception as e:
            logger.warning(f"[Z-Library] rerank 调用失败，回退上游排序：{type(e).__name__}: {e}")
            return books, None

        results = sorted(results, key=lambda r: getattr(r, "relevance_score", 0.0) or 0.0, reverse=True)
        valid = [r for r in results if 0 <= getattr(r, "index", -1) < len(pool)]
        if not valid:
            return books, None
        top_score = getattr(valid[0], "relevance_score", None)
        ranked_idx = [r.index for r in valid]
        seen = set(ranked_idx)
        ranked = [pool[i] for i in ranked_idx]
        ranked.extend(b for i, b in enumerate(pool) if i not in seen)
        ranked.extend(ordered[len(pool):])
        return ranked, top_score

    async def search_nodes(self, event, query: str, limit: int = 0):
        if not self.config.get("enable_zlib", False):
            return "[Z-Library] 功能未启用。"

        if not query:
            return "[Z-Library] 请提供电子书关键词以进行搜索。"

        if limit < 1:
            return "[Z-Library] 请确认搜索返回结果数量在 1-60 之间。"
        if limit > 60:
            limit = 60

        min_year = int(self.config.get("min_year", 0) or 0)
        # Z-Library gets the query without the word "edition" (see strip_edition_word); the
        # local hit tiers use the same words, and rerank still sees what the user typed.
        engine_query = strip_edition_word(query)
        search_kwargs = {"message": engine_query, "limit": max(limit, ZLIB_CANDIDATE_POOL)}
        if min_year > 0:
            search_kwargs["yearFrom"] = min_year
        query_norm = normalize_match_text(engine_query)
        tokens = [t for t in (normalize_match_text(w) for w in engine_query.split()) if t]

        try:
            sent = f"（上游查询: {engine_query}）" if engine_query != query else ""
            logger.info(
                f"[Z-Library] Received books search query: {query}{sent}, limit: {limit}, yearFrom={min_year or '-'}"
            )

            if not await self._ensure_login():
                return "[Z-Library] 登录失败。"

            books, had_exception = await self._collect_books(search_kwargs, query_norm, tokens)
            if not books:
                if had_exception:
                    return "[Z-Library] 暂时无法连接到 Z-Library，请稍后再试。"
                return "[Z-Library] 未找到匹配的电子书。"

            # 同一条记录（同 id）在两种 order 里重复出现，_absorb 已经合并过了；
            # 这里只合并确定是同一份文件的不同 id（见 _dedupe）。
            books = self._dedupe(books)
            candidates = len(books)
            # 过滤放在截断前：先截到 limit 再过滤，掉几条就少几条，凑不满用户要的数量。
            books = filter_unsafe(
                books, self.safety_checker,
                fields=["title", "author", "publisher", "description"],
                source="Z-Library",
            )
            if not books:
                return "[Z-Library] 全部结果被内容过滤丢弃。"

            def front(book):
                # 书名等于 → 书名以查询开头 → 书名包含查询 → 查询词全在
                # 书名+简介里（「生物化学 糖酵解」的「糖酵解」常只出现在简介的目录里）；
                # 其余不抢窗口。「data analysis」光是书名含这个词的就上百本，不分档照样挤不进前 80。
                tier = self._relevance_key(book, query_norm, tokens)[0]
                if tier <= 2:
                    return tier
                text = normalize_match_text(f"{book.get('title') or ''} {book.get('description') or ''}")
                return 3 if tokens and all(t in text for t in tokens) else None

            books, top_score = await self._rerank_books(query, books, limit, front=front)
            books = books[:limit]
            extra = f"，rerank 最高分 {top_score:.3f}" if top_score is not None else ""
            logger.info(f"[Z-Library] 候选 {candidates} 条，返回 {len(books)} 条{extra}")

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

                edition = self._edition_text(book)
                if edition:
                    chain.append(Plain(f"版次: {edition}\n"))

                chain.append(Plain(f"作者: {book.get('author', '未知')}\n"))
                chain.append(Plain(f"年份: {book.get('year', '未知')}\n"))

                publisher = book.get("publisher", None)
                if not publisher or publisher == "None":
                    publisher = "未知"
                chain.append(Plain(f"出版社: {publisher}\n"))

                chain.append(Plain(f"语言: {book.get('language', '未知')}\n"))

                extension = (book.get("extension") or "").strip()
                size_str = (book.get("filesizeString") or "").strip()
                pages = book.get("pages")
                fmt_parts = []
                if extension:
                    fmt_parts.append(extension.upper())
                if size_str:
                    fmt_parts.append(size_str)
                if pages:
                    fmt_parts.append(f"{pages}页")
                if fmt_parts:
                    chain.append(Plain(f"文件: {' · '.join(fmt_parts)}\n"))

                identifier = (book.get("identifier") or "").strip()
                if identifier:
                    chain.append(Plain(f"ISBN: {identifier}\n"))

                md5 = (book.get("md5") or "").strip()
                if md5:
                    chain.append(Plain(f"MD5: {md5}\n"))

                description = book.get("description", "无简介")
                if isinstance(description, str) and description.strip() != "":
                    description = excerpt_for_query(description, query, 150)
                else:
                    description = "无简介"
                chain.append(Plain(f"简介: {description}\n"))

                chain.append(Plain(f"下载命令:\n/zlib download {book.get('id')} {book.get('hash')}"))

                return Node(
                    uin=event.get_self_id(),
                    name="Z-Library",
                    content=chain,
                )

            tasks = [construct_node(book) for book in books]
            return await asyncio.gather(*tasks)
        except Exception as e:
            logger.error(f"[Z-Library] Error during book search: {e}")
            return "[Z-Library] 搜索电子书时发生错误，请稍后再试。"

    async def find_by_md5(self, md5: str):
        """用 md5 在 Z-Library 里定位同一本书，供其他书源兜底下载；找不到返回 None。"""
        if not self.config.get("enable_zlib", False) or not md5:
            return None
        if not await self._ensure_login():
            logger.warning(f"[Z-Library] 登录失败，无法按 md5 查书：{self.last_login_error or '原因未知'}")
            return None

        try:
            results = await asyncio.to_thread(self.zlibrary.search, message=md5, limit=5)
        except Exception as e:
            logger.warning(f"[Z-Library] 按 md5 搜索失败：{e}")
            return None

        for book in (results or {}).get("books") or []:
            if (book.get("md5") or "").lower() != md5.lower():
                continue
            if not is_valid_zlib_book_id(str(book.get("id"))) or not is_valid_zlib_book_hash(book.get("hash")):
                continue
            return book
        return None

    async def download(self, event, book_id: str = "", book_hash: str = ""):
        if not self.config.get("enable_zlib", False):
            return [event.plain_result("[Z-Library] 功能未启用。")]

        book_id = "" if book_id is None else str(book_id).strip()
        book_hash = "" if book_hash is None else str(book_hash).strip()
        if not is_valid_zlib_book_id(book_id) or not is_valid_zlib_book_hash(book_hash):
            return [event.plain_result("[Z-Library] 请使用 /zlib download <id> <hash> 下载。")]

        try:
            if not await self._ensure_login():
                return [event.plain_result("[Z-Library] 登录失败。")]

            book_details = await asyncio.to_thread(self.zlibrary.getBookInfo, book_id, hashid=book_hash)
            # 出错时 API 返回的是 {"success": 0, "error": "..."}，本身是 truthy，必须查 success。
            if not isinstance(book_details, dict) or not book_details.get("success", 1):
                reason = book_details.get("error") if isinstance(book_details, dict) else None
                return [event.plain_result(
                    f"[Z-Library] 无法获取电子书详情（{reason or '请检查 ID 与 Hash 是否正确'}）。"
                )]

            # /file 返回的书名字段可能缺失或为空串，用详情里的标题兜底。
            fallback_name = (book_details.get("book") or {}).get("title")
            # 排队时先吱一声，不然锁一挂几分钟，用户会以为命令没生效。
            if self._download_lock.locked():
                try:
                    await event.send(
                        MessageChain().message("[Z-Library] 已有下载任务进行中，本次下载已排队。")
                    )
                except Exception:
                    pass
            downloaded_book = await self._download_with_relogin(book_id, book_hash, fallback_name)
            if isinstance(downloaded_book, str):
                return [event.plain_result(downloaded_book)]
            if downloaded_book:
                _, temp_file_path, size = downloaded_book
                logger.debug(f"[Z-Library] 文件已下载并保存到临时目录：{temp_file_path}（{size} 字节）")

                # 固定等 5 秒对几十 MB 的书不够：协议端还在拉文件就被删掉了。
                # Scheduled before building the reply so the file can't be orphaned.
                schedule_temp_cleanup(temp_file_path, upload_cleanup_delay(size))
                file = File(name=os.path.basename(temp_file_path), file=str(temp_file_path))
                return [event.chain_result([file])]
            return [event.plain_result("[Z-Library] 下载电子书时发生错误，请稍后再试。")]
        except ZlibraryError as e:
            logger.error(f"[Z-Library] Download rejected: {e}")
            return [event.plain_result(f"[Z-Library] {e}")]
        except Exception as e:
            logger.error(f"[Z-Library] Error during book download: {e}", exc_info=True)
            return [event.plain_result("[Z-Library] 下载电子书时发生错误，请稍后再试。")]

    async def _download_with_relogin(self, book_id: str, book_hash: str, fallback_name):
        """串行下载；会话 key 失效（Invalid credentials）时重登一次再下。

        返回 (文件名, 临时文件路径, 字节数)；重登失败时返回给用户看的提示字符串。
        以前 isLoggedIn() 一旦为 True 就永远不会再登录，key 失效后每次下载都只会
        把「Invalid credentials」原样甩给用户。
        The book streams straight into a fresh temp dir (not into memory); a failed or
        cancelled download removes that dir.
        """
        for attempt in range(2):
            created = []

            def target(name, created=created):
                created.append(make_temp_download_path(self.temp_path, truncate_filename(name)))
                return created[-1]

            try:
                async with self._download_lock:
                    return await asyncio.to_thread(
                        self.zlibrary.downloadBook, {"id": book_id, "hash": book_hash}, fallback_name,
                        target=target,
                    )
            except BaseException as e:
                for path in created:
                    discard_temp_file(path)
                if not isinstance(e, ZlibraryError) or attempt or not self._is_auth_error(e):
                    raise
                logger.warning(f"[Z-Library] 会话已失效（{e}），重新登录后重试。")
                self._reset_login()
                if not await self._ensure_login():
                    return f"[Z-Library] 会话失效且重新登录失败：{self.last_login_error or '原因未知'}"
        return None
