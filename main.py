import asyncio
import os

from astrbot.api.all import *
from astrbot.api.event.filter import *
from astrbot.core.star.filter.command import GreedyStr

from .annas_source import AnnasSource
from .archive_source import ArchiveSource
from .calibre_source import CalibreSource
from .liber3_source import Liber3Source
from .utils import (
    get_proxy,
    get_rerank_provider,
    is_book_node,
    is_valid_annas_book_id,
    is_valid_archive_book_url,
    is_valid_calibre_book_url,
    is_valid_liber3_book_id,
    make_safety_checker,
    node_doc_text,
    normalize_limit,
    rerank_inputs,
    split_query_and_limit,
    sweep_stale_temp_dirs,
    to_event_results,
)
from .zlib_source import ZlibSource

# /ebooks search 的数量是「每个平台」的条数，合并后可能是它的好几倍，所以单独封顶。
MERGED_SEARCH_MAX = 50
# Per-platform budget in the merged search (Anna's first challenge alone takes 20-45 s).
PLATFORM_SEARCH_TIMEOUT = 90

# 指令参数一律用 "" 作默认值而不是 None：AstrBot 按默认值推断类型，默认值为 None
# 的参数遇到纯数字会被转成 int，Z-Library 的 hash「012345」就成了 12345。


@register("ebooks", "buding", "一个功能强大的电子书搜索和下载插件", "2.0.1", "https://github.com/zouyonghe/astrbot_plugin_ebooks")
class ebooks(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.proxy = get_proxy()
        self.TEMP_PATH = os.path.abspath("data/temp")
        os.makedirs(self.TEMP_PATH, exist_ok=True)
        swept = sweep_stale_temp_dirs(self.TEMP_PATH)
        if swept:
            logger.info(f"[ebooks] 清理了 {swept} 个过期的下载临时目录")
        raw_max = self.config.get("max_results", 20)
        try:
            self.max_results = int(raw_max)
        except (TypeError, ValueError):
            self.max_results = 0
        if not (1 <= self.max_results <= 100):
            logger.warning(f"[ebooks] max_results 配置无效 ({raw_max!r})，已重置为 20")
            self.max_results = 20

        if self.config.get("enable_calibre", False) and not self.config.get("calibre_web_url", "").strip():
            self.config["enable_calibre"] = False
            self.config.save_config()
            logger.info("[ebooks] 未设置 Calibre-Web URL，禁用该平台。")

        self.safety_checker = make_safety_checker(context, self.config)
        if self.safety_checker:
            logger.info("[ebooks] 内容过滤已启用（插件内置禁书词表 + extra_filter_keywords）")

        self.calibre_source = CalibreSource(self.config, self.proxy, self.max_results, self.TEMP_PATH, self.safety_checker)
        self.liber3_source = Liber3Source(self.config, self.proxy, self.max_results, self.safety_checker)
        self.archive_source = ArchiveSource(self.config, self.proxy, self.max_results, self.TEMP_PATH, self.safety_checker)
        self.zlib_source = ZlibSource(self.config, self.proxy, self.max_results, self.TEMP_PATH, self.safety_checker, context=self.context)
        self.annas_source = AnnasSource(
            self.config, self.proxy, self.max_results, self.TEMP_PATH, self.safety_checker,
            zlib_source=self.zlib_source,
        )

    async def terminate(self):
        await asyncio.gather(
            self.calibre_source.close(),
            self.liber3_source.close(),
            self.archive_source.close(),
            self.zlib_source.terminate(),
        )

    async def _yield_download_results(self, results):
        for item in results:
            yield item

    @command_group("calibre")
    def calibre(self):
        pass

    @calibre.command("search")
    async def search_calibre(self, event: AstrMessageEvent, query: GreedyStr):
        q, limit_str = split_query_and_limit(query)
        if not q:
            yield event.plain_result("[Calibre-Web] 请提供电子书关键词以进行搜索。")
            return
        limit_value, err = normalize_limit(limit_str, self.max_results, 1, 100)
        if err:
            yield event.plain_result(f"[Calibre-Web] {err}")
            return
        result = await self.calibre_source.search_nodes(event, q, limit_value)
        for response in to_event_results(event, "Calibre-Web", result, merge_forward=self.config.get("enable_merge_forward", True)):
            yield response

    @calibre.command("download")
    async def download_calibre(self, event: AstrMessageEvent, book_url: str = ""):
        results = await self.calibre_source.download(event, book_url)
        async for response in self._yield_download_results(results):
            yield response

    @calibre.command("recommend")
    async def recommend_calibre(self, event: AstrMessageEvent, n: int):
        results = await self.calibre_source.recommend(event, n)
        async for response in self._yield_download_results(results):
            yield response

    @command_group("liber3")
    def liber3(self):
        pass

    @liber3.command("search")
    async def search_liber3(self, event: AstrMessageEvent, query: GreedyStr):
        q, limit_str = split_query_and_limit(query)
        if not q:
            yield event.plain_result("[Liber3] 请提供电子书关键词以进行搜索。")
            return
        limit_value, err = normalize_limit(limit_str, self.max_results, 1, 100)
        if err:
            yield event.plain_result(f"[Liber3] {err}")
            return
        result = await self.liber3_source.search_nodes(event, q, limit_value)
        for response in to_event_results(event, "Liber3", result, merge_forward=self.config.get("enable_merge_forward", True)):
            yield response

    @liber3.command("download")
    async def download_liber3(self, event: AstrMessageEvent, book_id: str = ""):
        results = await self.liber3_source.download(event, book_id)
        async for response in self._yield_download_results(results):
            yield response

    @command_group("archive")
    def archive(self):
        pass

    @archive.command("search")
    async def search_archive(self, event: AstrMessageEvent, query: GreedyStr):
        q, limit_str = split_query_and_limit(query)
        if not q:
            yield event.plain_result("[archive.org] 请提供电子书关键词以进行搜索。")
            return
        limit_value, err = normalize_limit(limit_str, self.max_results, 1, 60, clamp_max=True)
        if err:
            yield event.plain_result(f"[archive.org] {err}")
            return
        result = await self.archive_source.search_nodes(event, q, limit_value)
        for response in to_event_results(event, "archive.org", result, merge_forward=self.config.get("enable_merge_forward", True)):
            yield response

    @archive.command("download")
    async def download_archive(self, event: AstrMessageEvent, book_url: str = ""):
        results = await self.archive_source.download(event, book_url)
        async for response in self._yield_download_results(results):
            yield response

    @command_group("zlib")
    def zlib(self):
        pass

    @zlib.command("search")
    async def search_zlib(self, event: AstrMessageEvent, query: GreedyStr):
        q, limit_str = split_query_and_limit(query)
        if not q:
            yield event.plain_result("[Z-Library] 请提供电子书关键词以进行搜索。")
            return
        limit_value, err = normalize_limit(limit_str, self.max_results, 1, 60, clamp_max=True)
        if err:
            yield event.plain_result(f"[Z-Library] {err}")
            return
        result = await self.zlib_source.search_nodes(event, q, limit_value)
        for response in to_event_results(event, "Z-Library", result, merge_forward=self.config.get("enable_merge_forward", True)):
            yield response

    @zlib.command("download")
    async def download_zlib(self, event: AstrMessageEvent, book_id: str = "", book_hash: str = ""):
        results = await self.zlib_source.download(event, book_id, book_hash)
        async for response in self._yield_download_results(results):
            yield response

    @command_group("annas")
    def annas(self):
        pass

    @annas.command("search")
    async def search_annas(self, event: AstrMessageEvent, query: GreedyStr):
        q, limit_str = split_query_and_limit(query)
        if not q:
            yield event.plain_result("[Anna's Archive] 请提供电子书关键词以进行搜索。")
            return
        limit_value, err = normalize_limit(limit_str, self.max_results, 1, 60, clamp_max=True)
        if err:
            yield event.plain_result(f"[Anna's Archive] {err}")
            return
        result = await self.annas_source.search_nodes(event, q, limit_value)
        for response in to_event_results(event, "anna's archive", result, merge_forward=self.config.get("enable_merge_forward", True)):
            yield response

    @annas.command("download")
    async def download_annas(self, event: AstrMessageEvent, book_id: str = ""):
        results = await self.annas_source.download(event, book_id)
        async for response in self._yield_download_results(results):
            yield response

    @command_group("ebooks")
    def ebooks(self):
        pass

    @ebooks.command("help")
    async def show_help(self, event: AstrMessageEvent):
        platforms = [
            ("Calibre-Web", "enable_calibre"), ("Z-Library", "enable_zlib"),
            ("Anna's Archive", "enable_annas"), ("archive.org", "enable_archive"), ("Liber3", "enable_liber3"),
        ]
        enabled = [name for name, key in platforms if self.config.get(key, False)]
        # QQ 按纯文本显示，不用 Markdown（** 和反引号会原样露出来）。
        help_msg = [
            "📚 ebooks 电子书搜索下载",
            f"当前已启用：{'、'.join(enabled) if enabled else '无（请在插件配置里开启平台）'}",
            "",
            "🔍 常用",
            f"/ebooks search 关键词 [数量] —— 所有已启用平台一起搜，数量指每个平台取几条（默认 {self.max_results}，最多 {MERGED_SEARCH_MAX}）",
            "/ebooks download 参数 —— 把搜索结果里「下载命令」的内容发出来即可下载",
            "",
            "📖 单独搜某个平台",
            "/calibre search 关键词 [数量]、/calibre download 下载链接",
            "/calibre recommend 数量 —— 从书库随机推荐 1-50 本",
            "/zlib search 关键词 [数量]、/zlib download ID Hash",
            "/annas search 关键词 [数量]、/annas download ID",
            "/archive search 关键词 [数量]、/archive download 下载链接",
            "",
            "💡 提示",
            "· 关键词末尾 1-3 位的数字会被当成数量；书名以数字结尾时（如 Python 3）再补一个数量：/ebooks search Python 3 10",
            "· 4 位以上的数字（年份、ISBN）会留在关键词里，如：/ebooks search 生理学 2018",
            "· 想要指定版次就写进关键词，如「… 8th edition」「… 第9版」。结果里会显示每本书的版次；开启重排模型并填写重排模型指令后，模型会优先排这一版",
            "· 也可以直接对 AI 说「帮我找一本 …」",
            "· 大文件下载要十几到二十几分钟，中途断线会自动续传，请耐心等待",
        ]
        yield event.plain_result("\n".join(help_msg))

    _PLATFORM_WEIGHT_KEYS = {
        "Calibre-Web": "platform_weight_calibre",
        "Liber3": "platform_weight_liber3",
        "archive.org": "platform_weight_archive",
        "Z-Library": "platform_weight_zlib",
        "Anna's Archive": "platform_weight_annas",
    }

    def _platform_weight(self, platform_name: str) -> float:
        """该平台在合并视图里的排序权重（相关分乘子），默认 1.0，负数按 0 处理。

        0 是合法值（垫底），不能用 `x or 1.0` 取默认——那会把 0 变回 1.0。
        """
        key = self._PLATFORM_WEIGHT_KEYS.get(platform_name)
        if not key:
            return 1.0
        raw = self.config.get(key, 1.0)
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            return 1.0
        try:
            w = float(raw)
        except (TypeError, ValueError):
            return 1.0
        return max(w, 0.0)

    @staticmethod
    def _interleave(entries: list) -> list:
        """按平台轮流取：[(A,a1),(A,a2),(B,b1)] → [(A,a1),(B,b1),(A,a2)]，平台先后保持原序。

        Why: rerank 只看前 rerank_candidates 条。直接截分段拼接的列表时，排在后面的
        平台一条都送不进模型，只能整体垫底。
        """
        buckets = {}
        for entry in entries:
            buckets.setdefault(entry[0], []).append(entry)
        out = []
        depth = max((len(b) for b in buckets.values()), default=0)
        for i in range(depth):
            for bucket in buckets.values():
                if i < len(bucket):
                    out.append(bucket[i])
        return out

    async def _rerank_nodes(self, query: str, entries: list):
        """跨平台全局重排：把各平台返回的书籍节点一并喂给 reranker。

        entries 为 (platform_name, node) 对。返回 (nodes, top_score|None)；
        未启用、无 provider、调用失败一律回退原序（此时顺序是各平台分段拼接）。
        送入模型的是各平台轮流交错后的前 rerank_candidates 条，单条文档长度受
        rerank_doc_max_chars 限制，未送入的节点按交错顺序垫底。
        排序键 = 相关分 × 平台权重（platform_weight_*，默认 1.0）。
        """
        nodes = [n for _, n in entries]
        if not self.config.get("enable_rerank", False) or not nodes:
            return nodes, None
        provider = get_rerank_provider(self.context, self.config)
        if provider is None:
            logger.warning("[ebooks] enable_rerank 已开但没有启用的 rerank provider，保持各平台原序。")
            return nodes, None

        top_k = int(self.config.get("rerank_candidates", 60) or 60)
        max_chars = int(self.config.get("rerank_doc_max_chars", 300) or 300)
        ordered = self._interleave(entries)
        fed = ordered[:max(top_k, 1)]
        try:
            rq, docs = rerank_inputs(self.config, query, [node_doc_text(n, max_chars) for _, n in fed])
            results = await provider.rerank(rq, docs, top_n=len(fed))
        except Exception as e:
            logger.warning(f"[ebooks] 全局 rerank 失败，保持各平台原序：{type(e).__name__}: {e}")
            return nodes, None

        valid = [r for r in results if 0 <= getattr(r, "index", -1) < len(fed)]
        if not valid:
            return nodes, None
        top_raw = max(getattr(r, "relevance_score", 0.0) or 0.0 for r in valid)
        ranked = sorted(
            valid,
            key=lambda r: (getattr(r, "relevance_score", 0.0) or 0.0)
            * self._platform_weight(fed[r.index][0]),
            reverse=True,
        )
        ranked_idx = [r.index for r in ranked]
        seen = set(ranked_idx)
        out = [fed[i][1] for i in ranked_idx]
        out.extend(n for i, (_, n) in enumerate(fed) if i not in seen)
        out.extend(n for _, n in ordered[len(fed):])
        return out, top_raw

    async def _search_all(self, event: AstrMessageEvent, query: str, limit: int):
        """多平台并发搜索 → 状态节点在前、书目节点（可选全局重排）在后。"""
        # 合并视图里每平台封顶 per_platform_results 条，控制全局 rerank 的输入规模。
        per_platform = int(self.config.get("per_platform_results", 20) or 0)
        plimit = min(limit, per_platform) if per_platform > 0 else limit

        tasks = []
        if self.config.get("enable_calibre", False):
            tasks.append(("Calibre-Web", self.calibre_source.search_nodes(event, query, plimit)))
        if self.config.get("enable_liber3", False):
            tasks.append(("Liber3", self.liber3_source.search_nodes(event, query, plimit)))
        if self.config.get("enable_archive", False):
            tasks.append(("archive.org", self.archive_source.search_nodes(event, query, plimit)))
        if self.config.get("enable_zlib", False):
            tasks.append(("Z-Library", self.zlib_source.search_nodes(event, query, plimit)))
        if self.config.get("enable_annas", False):
            tasks.append(("Anna's Archive", self.annas_source.search_nodes(event, query, plimit)))

        if not tasks:
            yield event.plain_result("[ebooks] 未启用任何电子书平台，请在插件配置里至少开启一个。")
            return

        async def bounded(name, coro):
            # The merged reply waits for the slowest platform; one hung source must not stall it.
            try:
                return await asyncio.wait_for(coro, PLATFORM_SEARCH_TIMEOUT)
            except TimeoutError:
                logger.warning(f"[ebooks] {name} 搜索超过 {PLATFORM_SEARCH_TIMEOUT} 秒，已跳过")
                return f"[{name}] 搜索超时（超过 {PLATFORM_SEARCH_TIMEOUT} 秒），本次已跳过该平台。"

        try:
            # 单个平台抛异常不能拖垮整次搜索：转成该平台的报错提示。
            search_results = await asyncio.gather(
                *[bounded(name, task) for name, task in tasks], return_exceptions=True
            )
            named_results = []
            for (name, _), res in zip(tasks, search_results):
                if isinstance(res, BaseException):
                    logger.error(f"[ebooks] {name} 搜索异常：{type(res).__name__}: {res}")
                    res = f"[{name}] 搜索电子书时发生错误，请稍后再试。"
                named_results.append((name, res))
            # 平台权重同时决定：分段拼接时的平台先后，以及全局重排的加权。
            named_results.sort(key=lambda t: -self._platform_weight(t[0]))

            # 书节点（带「下载命令」行）进全局重排；报错/提示节点不参与，排在前面。
            book_entries, other_nodes = [], []
            for platform_name, platform_results in named_results:
                if isinstance(platform_results, str):
                    other_nodes.append(Node(
                        uin=event.get_self_id(),
                        name="ebooks",
                        content=[Plain(platform_results)],
                    ))
                    continue
                for node in platform_results:
                    if is_book_node(node):
                        book_entries.append((platform_name, node))
                    else:
                        other_nodes.append(node)

            book_nodes, top_score = await self._rerank_nodes(query, book_entries)
            if top_score is not None:
                logger.info(f"[ebooks] 全局 rerank：{len(book_nodes)} 条书节点，最高分 {top_score:.3f}")
            # 候选池可以很大（喂 reranker），发出去的另有独立上限。
            display_limit = int(self.config.get("merged_display_limit", 30) or 0)
            if display_limit > 0 and len(book_nodes) > display_limit:
                hidden = len(book_nodes) - display_limit
                if top_score is None:
                    # Unranked: the list is whole platform segments, so cutting its head would
                    # drop the trailing platforms entirely. Give each platform a fair share.
                    keep = {id(n) for _, n in self._interleave(book_entries)[:display_limit]}
                    book_nodes = [n for n in book_nodes if id(n) in keep]
                else:
                    book_nodes = book_nodes[:display_limit]
                book_nodes.append(Node(
                    uin=event.get_self_id(),
                    name="ebooks",
                    content=[Plain(f"其余 {hidden} 条候选未显示；可换更精确的关键词，或用单平台命令（/zlib search 等）单独翻该平台。")],
                ))
            all_nodes = other_nodes + book_nodes
            if not all_nodes:
                yield event.plain_result("[ebooks] 未找到匹配的电子书。")
                return
            # 合并转发超过 30 条分片发送；关闭合并转发时逐条发送（顺序同样是重排后的）。
            merge_forward = self.config.get("enable_merge_forward", True)
            for response in to_event_results(event, "ebooks", all_nodes, merge_forward=merge_forward):
                yield response
        except Exception as e:
            logger.error(f"[ebooks] Error during multi-platform search: {type(e).__name__}: {e}", exc_info=True)
            yield event.plain_result("[ebooks] 搜索电子书时发生错误，请稍后再试。")

    @ebooks.command("search")
    async def search_all_platforms(self, event: AstrMessageEvent, query: GreedyStr):
        q, limit_str = split_query_and_limit(query)
        if not q:
            yield event.plain_result("[ebooks] 请提供电子书关键词以进行搜索。")
            return
        # 夹紧而不是报错：max_results 允许配到 100，超过 50 时不带数量的搜索也不能直接报错。
        limit, err = normalize_limit(limit_str, self.max_results, 1, MERGED_SEARCH_MAX, clamp_max=True)
        if err:
            yield event.plain_result(f"[ebooks] {err}")
            return
        async for result in self._search_all(event, q, limit):
            yield result

    @ebooks.command("download")
    async def download_all_platforms(self, event: AstrMessageEvent, arg1: str = "", arg2: str = ""):
        arg1 = "" if arg1 is None else str(arg1).strip()
        arg2 = "" if arg2 is None else str(arg2).strip()
        if not arg1:
            yield event.plain_result("[ebooks] 请提供有效的下载链接、ID 或参数！")
            return

        try:
            if arg1 and arg2:
                logger.info("[ebooks] 检测到 Z-Library ID 和 Hash，开始下载...")
                async for result in self.download_zlib(event, arg1, arg2):
                    yield result
                return

            if is_valid_calibre_book_url(arg1):
                logger.info("[ebooks] 检测到 Calibre-Web 链接，开始下载...")
                async for result in self.download_calibre(event, arg1):
                    yield result
                return

            if is_valid_archive_book_url(arg1):
                logger.info("[ebooks] 检测到 archive.org 链接，开始下载...")
                async for result in self.download_archive(event, arg1):
                    yield result
                return

            if is_valid_liber3_book_id(arg1):
                logger.info("[ebooks] ⏳ 检测到 Liber3 ID，开始下载...")
                async for result in self.download_liber3(event, arg1):
                    yield result
                return

            if is_valid_annas_book_id(arg1):
                logger.info("[ebooks] ⏳ 检测到 Annas Archive ID，开始下载...")
                async for result in self.download_annas(event, arg1):
                    yield result
                return

            if arg1.isdigit():
                yield event.plain_result(
                    "[ebooks] 这看起来是 Z-Library 的书籍 ID，还需要同时提供 Hash："
                    "/ebooks download <ID> <Hash>（搜索结果的下载命令里有）。"
                )
                return

            yield event.plain_result(
                "[ebooks] 未识别的输入格式，请提供以下格式之一：\n"
                "- Calibre-Web 下载链接\n"
                "- archive.org 下载链接\n"
                "- Liber3/Annas Archive 32位 ID\n"
                "- Z-Library 的 ID 和 Hash"
            )
        except Exception as e:
            logger.error(f"[ebooks] 下载分发失败：{type(e).__name__}: {e}", exc_info=True)
            yield event.plain_result("[ebooks] 下载电子书时发生错误，请稍后再试。")

    @llm_tool("search_ebooks")
    async def search_ebooks(self, event: AstrMessageEvent, query: str):
        """Search for eBooks across all supported platforms.

        When to use:
            This method performs a unified search across multiple platforms supported by this plugin,
            allowing users to find ebooks by title or keyword.
            Unless a specific platform is explicitly mentioned, this function should be used as the default means for searching books.


        Args:
            query (string): The keyword or book title for searching.
        """
        q = (query or "").strip()
        if not q:
            yield event.plain_result("[ebooks] 请提供电子书关键词以进行搜索。")
            return
        # 直接传数量，不再拼进查询串：旧写法写死 20 条，还会让以数字结尾的书名被误拆。
        async for result in self._search_all(event, q, min(self.max_results, MERGED_SEARCH_MAX)):
            yield result

    @llm_tool("download_ebook")
    async def download_ebook(self, event: AstrMessageEvent, arg1: str, arg2: str = None):
        """Download eBooks by dispatching to the appropriate platform's download method.

        When to use:
            This method facilitates downloading of ebooks by automatically identifying the platform
            from the provided identifier (ID, URL, or Hash) and then calling the corresponding platform's download function.
            Unless the platform is specifically mentioned, this function serves as the default for downloading ebooks.

        Args:
            arg1 (string): Primary identifier, such as a URL or book ID.
            arg2 (string): Secondary input, such as a hash, required for Z-Library downloads.
        """
        async for result in self.download_all_platforms(event, arg1, arg2):
            yield result
