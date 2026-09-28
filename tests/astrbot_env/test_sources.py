import asyncio
import base64
import importlib
import os
from types import SimpleNamespace

import pytest
from aiohttp import web
from conftest import (
    PKG, File, FakeEvent, FakeZlibrary, collect, file_path, make_config, result_files, result_texts, run,
)

u = importlib.import_module(f"{PKG}.utils")
zs = importlib.import_module(f"{PKG}.zlib_source")
ZlibraryError = importlib.import_module(f"{PKG}.Zlibrary").ZlibraryError
an = importlib.import_module(f"{PKG}.annas_source")
ar = importlib.import_module(f"{PKG}.archive_source")
cs = importlib.import_module(f"{PKG}.calibre_source")
lb = importlib.import_module(f"{PKG}.liber3_source")


def all_files_under(path):
    return [os.path.join(d, f) for d, _, fs in os.walk(path) for f in fs]


def read_files(res):
    out = []
    for f in result_files(res):
        with open(file_path(f), "rb") as fh:
            out.append(fh.read())
    return out


def run_download(coro):
    """跑下载并在事件循环还活着时读出文件内容——循环一关，延时清理任务会被取消并删文件。"""
    async def go():
        res = await coro
        return res, read_files(res)
    return run(go())


# ====================================================================== Z-Library
def zbook(i, title, author="某人", ext="pdf", **kw):
    b = {"id": i, "hash": f"{i:06x}", "title": title, "author": author, "extension": ext,
         "year": 2020, "publisher": "出版社", "language": "chinese", "md5": f"{i:032x}"}
    b.update(kw)
    return b


def zcfg(tmp_path=None, **over):
    return make_config(enable_zlib=True, zlib_email="a@b.c", zlib_password="pw", **over)


def test_zlib_relevance_key_tiers():
    q = u.normalize_match_text("生理学")
    toks = [q]
    key = lambda t, a="": zs.ZlibSource._relevance_key({"title": t, "author": a}, q, toks)
    assert key("生理学")[0] == 0
    assert key("生理学 第9版")[0] == 1
    assert key("人体生理学")[0] == 2
    assert key("心理学与生活")[0] == 5
    assert key("完全无关")[0] == 6


def test_zlib_collect_books_merges_orders(fake_zlib, tmp_path):
    fake_zlib.pages = {
        (None, 1): [zbook(1, "心理学与生活"), zbook(2, "生理学"), zbook(3, "理解人性")],
        ("bestmatch", 1): [zbook(2, "生理学"), zbook(4, "人体生理学")],
    }
    src = zs.ZlibSource(zcfg(), None, 10, str(tmp_path))
    q = u.normalize_match_text("生理学")
    books, err = run(src._collect_books({"message": "生理学", "limit": 100}, q, [q]))
    assert [b["id"] for b in books] == [1, 2, 4, 3]
    assert not err
    assert {c["page"] for c in fake_zlib.search_calls} == {1}


def test_zlib_collect_books_deepens_when_weak(fake_zlib, tmp_path):
    fake_zlib.pages = {
        (None, 1): [zbook(i, f"无关书{i}") for i in range(1, 101)],
        ("bestmatch", 1): [zbook(500, "另一本")],
        (None, 2): [zbook(900, "生理学")],
    }
    src = zs.ZlibSource(zcfg(), None, 10, str(tmp_path))
    q = u.normalize_match_text("生理学")
    books, _ = run(src._collect_books({"message": "生理学", "limit": 100}, q, [q]))
    pages = [(c["order"], c["page"]) for c in fake_zlib.search_calls]
    assert (None, 2) in pages and ("bestmatch", 2) not in pages
    assert any(b["id"] == 900 for b in books)


def test_zlib_search_nodes(fake_zlib, tmp_path, event):
    fake_zlib.pages = {
        (None, 1): [zbook(1, "生理学"), zbook(2, "中国即将崩溃"), zbook(3, "生理学 第9版")],
        ("bestmatch", 1): [],
    }
    checker = u.make_safety_checker(None, make_config())
    src = zs.ZlibSource(zcfg(), None, 10, str(tmp_path), checker)
    nodes = run(src.search_nodes(event, "生理学", 2))
    assert isinstance(nodes, list) and len(nodes) == 2
    texts = ["".join(c.text for c in n.content if hasattr(c, "text")) for n in nodes]
    assert "/zlib download 1 000001" in texts[0]
    assert all("中国即将崩溃" not in t for t in texts)


def test_zlib_rerank_books(fake_zlib, tmp_path):
    from conftest import FakeRerankProvider, make_context
    prov = FakeRerankProvider(lambda d: 2 if "目标" in d else 1)
    src = zs.ZlibSource(zcfg(enable_rerank=True), None, 10, str(tmp_path), context=make_context(prov))
    books = [zbook(1, "甲"), zbook(2, "目标书"), zbook(3, "乙")]
    ranked, top = run(src._rerank_books("q", books, 2))
    assert ranked[0]["id"] == 2 and top == 2.0 and len(ranked) == 3


def biostat(i, edition, author="Bernard Rosner"):
    return zbook(i, "Fundamentals of Biostatistics", author=author, edition=edition, language="english")


@pytest.mark.fixed
def test_zlib_keeps_same_title_records(fake_zlib, tmp_path, event):
    # 按书名/作者/格式去重会把同名不同版次吞掉（Rosner 第 7/8 版标题相同），不去重。
    fake_zlib.pages = {
        (None, 1): [biostat(1, "7"), biostat(2, "8"), biostat(3, "7")],
        ("bestmatch", 1): [biostat(2, "8")],  # 同一条记录在两种 order 里各出现一次，只算一条
    }
    src = zs.ZlibSource(zcfg(), None, 10, str(tmp_path))
    texts = node_texts(run(src.search_nodes(event, "fundamentals of biostatistics", 10)))
    assert [t.split("/zlib download ")[1].split()[0] for t in texts] == ["1", "2", "3"]


def test_zlib_query_without_edition_word_sent_verbatim(fake_zlib, tmp_path, event):
    fake_zlib.pages = {(None, 1): [zbook(1, "生理学")], ("bestmatch", 1): []}
    src = zs.ZlibSource(zcfg(), None, 10, str(tmp_path))
    run(src.search_nodes(event, "生理学 第9版", 5))
    assert {c["message"] for c in fake_zlib.search_calls} == {"生理学 第9版"}


@pytest.mark.fixed
def test_zlib_edition_query_keeps_upstream_order(fake_zlib, tmp_path, event):
    # 上游把 edition 当标题必含词，而版次在单独字段里：发出去的查询要去掉 edition。
    # The plugin does not interpret editions: without rerank the upstream order stands,
    # and the card shows Z-Library's edition field verbatim.
    fake_zlib.pages = {
        (None, 1): [biostat(1, "7"), zbook(2, "Fundamentals of Physics", edition="None"),
                    biostat(3, "5th ed."), biostat(4, "8")],
        ("bestmatch", 1): [],
    }
    src = zs.ZlibSource(zcfg(), None, 10, str(tmp_path))
    texts = node_texts(run(src.search_nodes(event, "fundamentals of biostatistics 8th edition", 4)))
    assert {c["message"] for c in fake_zlib.search_calls} == {"fundamentals of biostatistics 8th"}
    assert [t.split("/zlib download ")[1].split()[0] for t in texts] == ["1", "2", "3", "4"]
    assert "版次: 7\n" in texts[0] and "版次" not in texts[1]
    assert "版次: 5th ed.\n" in texts[2] and "版次: 8\n" in texts[3]


@pytest.mark.fixed
def test_zlib_rerank_sees_edition_and_decides_order(fake_zlib, tmp_path, event):
    from conftest import FakeRerankProvider, make_context
    prov = FakeRerankProvider(lambda d: 5 if "版次: 7\n" in d else 1)  # the model prefers the 7th edition
    fake_zlib.pages = {(None, 1): [biostat(4, "8"), biostat(1, "7")], ("bestmatch", 1): []}
    src = zs.ZlibSource(zcfg(enable_rerank=True), None, 10, str(tmp_path), context=make_context(prov))
    texts = node_texts(run(src.search_nodes(event, "fundamentals of biostatistics 8th edition", 2)))
    query, docs, _ = prov.calls[0]
    assert query == "fundamentals of biostatistics 8th edition"  # rerank 用用户原话
    assert docs[0].startswith("Fundamentals of Biostatistics\n版次: 8\n作者: Bernard Rosner\n")
    assert "/zlib download 1 000001" in texts[0]  # the model's order is final


@pytest.mark.fixed
def test_zlib_title_matches_enter_rerank_window(fake_zlib, tmp_path, event):
    # 上游序里书名就叫《Data Analysis》的书排在第 116 位，窗口只有 rerank_candidates 条。
    from conftest import FakeRerankProvider, make_context
    prov = FakeRerankProvider(lambda d: 1)
    noise = [zbook(i, f"Excel tricks {i}", description="data analysis made easy") for i in range(1, 6)]
    contains = [zbook(i, f"Practical Data Analysis vol {i}") for i in range(10, 16)]  # 书名包含，档次低于完全相同
    fake_zlib.pages = {(None, 1): noise + contains + [zbook(99, "Data Analysis")], ("bestmatch", 1): []}
    src = zs.ZlibSource(zcfg(enable_rerank=True, rerank_candidates=3), None, 10, str(tmp_path),
                        context=make_context(prov))
    run(src.search_nodes(event, "data analysis", 3))
    _, docs, _ = prov.calls[0]
    assert len(docs) == 3 and docs[0].startswith("Data Analysis")


TOC = ("本书为国家规划教材，供基础、临床、预防、口腔医学类专业用。全书共分二十二章。" * 4 + "第四章 糖代谢 第一节 糖的消化吸收 第二节 糖的无氧氧化（糖酵解） 第三节 糖的有氧氧化")  # 「糖酵解」在第 200 字之后


@pytest.mark.fixed
def test_zlib_dedupe_only_same_file():
    # 只合并格式、ISBN 都相同且大小或哈希相同的记录；书名/作者不参与判断。
    isbn = "9781593276034,1593276036"
    books = [
        zbook(1, "Python Crash Course", identifier=isbn, filesize=100, md5="a" * 32),
        zbook(2, "Python Crash Course (retail)", identifier=isbn, filesize=100, md5="b" * 32),  # 同大小 → 合并
        zbook(3, "PCC", identifier="1593276036,9781593276034", filesize=7, md5="a" * 32),  # 同哈希 → 合并
        zbook(4, "Python Crash Course", identifier=isbn, filesize=200, md5="c" * 32),  # 大小哈希都不同 → 保留
        zbook(5, "Python Crash Course", identifier=isbn, filesize=100, md5="d" * 32, ext="epub"),  # 格式不同 → 保留
        zbook(6, "Python Crash Course", filesize=100, md5="e" * 32),  # 没有 ISBN → 保留
        zbook(7, "Python Crash Course", filesize=100, md5="f" * 32),
        biostat(8, "7"), biostat(9, "8"),  # 同名同作者同格式，不同版次 → 都保留
    ]
    assert [b["id"] for b in zs.ZlibSource._dedupe(books)] == [1, 4, 5, 6, 7, 8, 9]


@pytest.mark.fixed
def test_zlib_description_excerpt_reaches_card_and_rerank(fake_zlib, tmp_path, event):
    from conftest import FakeRerankProvider, make_context
    prov = FakeRerankProvider(lambda d: 1)
    fake_zlib.pages = {(None, 1): [zbook(1, "生物化学", description=TOC)], ("bestmatch", 1): []}
    src = zs.ZlibSource(zcfg(enable_rerank=True), None, 10, str(tmp_path), context=make_context(prov))
    texts = node_texts(run(src.search_nodes(event, "生物化学 糖酵解", 5)))
    card_intro = [line for line in texts[0].split("\n") if line.startswith("简介:")][0]
    assert "糖酵解" in card_intro and len(card_intro) < 170
    assert "糖酵解" in prov.calls[0][1][0]


@pytest.mark.fixed
def test_zlib_description_matches_enter_rerank_window(fake_zlib, tmp_path, event):
    from conftest import FakeRerankProvider, make_context
    prov = FakeRerankProvider(lambda d: 1)
    noise = [zbook(i, f"生物化学习题 {i}", description="习题与解答") for i in range(1, 6)]
    target = zbook(99, "生物化学习题选解", description=TOC + "<br>")
    fake_zlib.pages = {(None, 1): noise + [target], ("bestmatch", 1): []}
    src = zs.ZlibSource(zcfg(enable_rerank=True, rerank_candidates=3), None, 10, str(tmp_path),
                        context=make_context(prov))
    run(src.search_nodes(event, "生物化学 糖酵解", 3))
    _, docs, _ = prov.calls[0]
    assert docs[0].startswith("生物化学习题选解") and "糖酵解" in docs[0] and "<br>" not in docs[0]


@pytest.mark.fixed
def test_zlib_description_html_is_cleaned_at_ingestion(fake_zlib, tmp_path, event):
    # 行内标签去掉后「生物<b>化学</b>」要连成「生物化学」；<br>/<p> 变成换行，&nbsp; 解码。
    desc = "<p>本书讲生物<b>化学</b>。</p>1 (p1): 第一章&nbsp;糖代谢 <br>2 (p2): 第二章 糖酵解"
    fake_zlib.pages = {(None, 1): [zbook(1, "教材", description=desc)], ("bestmatch", 1): []}
    src = zs.ZlibSource(zcfg(), None, 10, str(tmp_path))
    card = node_texts(run(src.search_nodes(event, "生物化学", 5)))[0]
    assert "简介: 本书讲生物化学。\n1 (p1): 第一章 糖代谢\n2 (p2): 第二章 糖酵解\n" in card
    assert "<" not in card and "&nbsp;" not in card


def test_zlib_find_by_md5(fake_zlib, tmp_path):
    fake_zlib.pages = {(None, 1): [zbook(7, "x", md5="AB" * 16)]}
    src = zs.ZlibSource(zcfg(), None, 10, str(tmp_path))
    assert run(src.find_by_md5("ab" * 16))["id"] == 7
    assert run(src.find_by_md5("cd" * 16)) is None


def test_zlib_download_writes_file(fake_zlib, tmp_path, event):
    fake_zlib.download_script = [("生理学 (姚泰).pdf", b"%PDF-1.4 ok")]
    src = zs.ZlibSource(zcfg(), None, 10, str(tmp_path))
    res, contents = run_download(src.download(event, "123", "0a0b0c"))
    files = result_files(res)
    assert len(files) == 1 and files[0].name == "生理学 (姚泰).pdf"
    assert contents == [b"%PDF-1.4 ok"]


def test_zlib_download_rejects_bad_args(fake_zlib, tmp_path, event):
    src = zs.ZlibSource(zcfg(), None, 10, str(tmp_path))
    res = run(src.download(event, "123", "zzz"))
    assert "/zlib download" in result_texts(res[0])[0]


def test_zlib_download_quota_error_is_shown(fake_zlib, tmp_path, event):
    fake_zlib.download_script = [ZlibraryError("今日下载额度已用尽。")]
    src = zs.ZlibSource(zcfg(), None, 10, str(tmp_path))
    res = run(src.download(event, "123", "0a0b0c"))
    assert "额度已用尽" in result_texts(res[0])[0]
    assert fake_zlib.download_calls == 1  # 非登录类错误不重试


@pytest.mark.fixed
def test_zlib_init_does_not_block_on_login(fake_zlib, tmp_path):
    async def go():
        src = zs.ZlibSource(zcfg(), None, 10, str(tmp_path))
        during_init = fake_zlib.login_calls
        await asyncio.sleep(0.3)
        return src, during_init

    src, during_init = run(go())
    assert during_init == 0
    assert fake_zlib.login_calls == 1 and src.zlibrary.isLoggedIn()


@pytest.mark.fixed
def test_zlib_download_relogs_on_invalid_credentials(fake_zlib, tmp_path, event):
    fake_zlib.download_script = [ZlibraryError("Invalid credentials"), ("Book.pdf", b"%PDF-ok")]
    src = zs.ZlibSource(zcfg(), None, 10, str(tmp_path))
    res = run(src.download(event, "123", "0a0b0c"))
    files = result_files(res)
    assert files, result_texts(res[0])
    assert fake_zlib.download_calls == 2 and fake_zlib.login_calls >= 2


@pytest.mark.fixed
def test_zlib_same_book_twice_gets_distinct_temp_files(fake_zlib, tmp_path, event):
    fake_zlib.download_script = [("Same.pdf", b"one"), ("Same.pdf", b"two")]
    src = zs.ZlibSource(zcfg(), None, 10, str(tmp_path))

    async def go():
        a = await src.download(event, "1", "0a0b0c")
        b = await src.download(event, "1", "0a0b0c")
        return a, b, read_files(a)

    a, b, content_a = run(go())
    pa, pb = file_path(result_files(a)[0]), file_path(result_files(b)[0])
    assert pa != pb
    assert content_a == [b"one"]


# ====================================================================== Anna's Archive
def acfg(**over):
    return make_config(enable_annas=True, annas_secret_key="k", **over)


@pytest.mark.fixed
def test_annas_md5_parsing_keeps_leading_a(tmp_path, event):
    src = an.AnnasSource(acfg(), None, 10, str(tmp_path))
    seen = []

    async def fake(ev, md5):
        seen.append(md5)
        return []

    src._download_via_fast_api = fake
    run(src.download(event, "A" + "AB" * 16))
    assert seen == ["ab" * 16]


def test_annas_fast_download_success(tmp_path, event):
    src = an.AnnasSource(acfg(), None, 10, str(tmp_path))
    md5 = "0f" * 16
    urls = []

    async def resolve(m):
        return (f"https://cdn.example/d/{md5}/Long%20Title%20--%20Author.pdf",
                {"account_fast_download_info": {"downloads_left": 7}}, None)

    async def stream(url, path):
        urls.append(url)
        with open(path, "wb") as f:
            f.write(b"%PDF-annas")
        return 10

    src._resolve_fast_url = resolve
    src._stream_to_file = stream
    res, contents = run_download(src.download(event, "A" + md5))
    assert urls == [f"https://cdn.example/d/{md5}/{md5}.pdf"]
    files = result_files(res)
    assert files[0].name == "Long Title -- Author.pdf"
    assert contents == [b"%PDF-annas"]
    assert any("今日剩余" in t for t in result_texts(res[0]))


def test_annas_failed_stream_leaves_no_temp(tmp_path, event):
    src = an.AnnasSource(acfg(), None, 10, str(tmp_path))

    async def resolve(m):
        return ("https://cdn.example/d/x/Book.pdf", {}, None)

    async def stream(url, path):
        with open(path, "wb") as f:
            f.write(b"partial")
        raise IOError("boom")

    src._resolve_fast_url = resolve
    src._stream_to_file = stream
    res = run(src.download(event, "A" + "0f" * 16))
    assert "下载电子书时发生错误" in result_texts(res[0])[0]
    assert all_files_under(tmp_path) == []
    assert os.listdir(tmp_path) == []


def test_annas_fallback_to_zlib(tmp_path, event):
    from astrbot.api.all import File
    zlib = SimpleNamespace(last_login_error="")

    async def find(md5):
        return {"id": 5, "hash": "0a0b0c"}

    async def dl(ev, bid, h):
        return [ev.chain_result([File(name="z.pdf", file="/tmp/z.pdf")])]

    zlib.find_by_md5, zlib.download = find, dl
    src = an.AnnasSource(acfg(), None, 10, str(tmp_path), zlib_source=zlib)
    res = run(src._fallback_download(event, "0f" * 16, an.INVALID_INDEX_ERROR))
    assert result_files(res)[0].name == "z.pdf"


def test_annas_static_helpers():
    assert an.AnnasSource._filename_from_url("https://x/a/b%20c.pdf") == "b c.pdf"
    assert an.AnnasSource._rewrite_to_short_url("https://x/a/long.pdf?s=1", "m.pdf") == "https://x/a/m.pdf?s=1"
    resp = SimpleNamespace(headers={"Content-Range": "bytes 10-19/100"})
    assert an.AnnasSource._total_size(resp, 10) == 100
    resp = SimpleNamespace(headers={"Content-Length": "90"})
    assert an.AnnasSource._total_size(resp, 10) == 100
    assert an._resolve_annas_language("") == an.Language.ANY
    assert an._resolve_annas_language("zh") == an.Language.ZH
    assert an._resolve_annas_language("xx") == an.Language.ANY


# ====================================================================== archive.org
class FakeResp:
    def __init__(self, data, status=200):
        self.data, self.status = data, status

    async def json(self, **kw):
        return self.data

    def release(self):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeReq:
    """Works both as `await session.get()` (old code) and `async with session.get()` (new code)."""

    def __init__(self, resp):
        self.resp = resp

    def __await__(self):
        async def _():
            return self.resp
        return _().__await__()

    async def __aenter__(self):
        return self.resp

    async def __aexit__(self, *a):
        return False


class FakeSession:
    closed = False

    def __init__(self, route):
        self.route = route
        self.calls = []

    def get(self, url, **kw):
        self.calls.append((url, kw))
        return FakeReq(self.route(url, kw))


def fetch_meta(meta):
    src = ar.ArchiveSource(make_config(enable_archive=True), None, 10, "/tmp")
    sess = FakeSession(lambda url, kw: FakeResp(meta))
    return run(src._fetch_metadata(sess, "https://archive.org/metadata/x", ("pdf", "epub")))


def test_archive_metadata_simple():
    md = fetch_meta({
        "metadata": {"identifier": "pythontricks", "creator": "Dan Bader", "date": "2017",
                     "publicdate": "2017-05-01 00:00:00", "language": "eng", "publisher": "self",
                     "description": "<p>Tricks</p>"},
        "files": [{"name": "cover.jpg"}, {"name": "PythonTricks.epub", "size": "2048", "md5": "ff"}],
    })
    assert md["download_url"] == "https://archive.org/download/pythontricks/PythonTricks.epub"
    assert md["year"] == "2017" and md["authors"] == "Dan Bader" and md["description"] == "Tricks"
    assert md["file_ext"] == "EPUB" and md["file_size_str"] == "2.0KB"


@pytest.mark.fixed
def test_archive_description_keeps_paragraphs():
    md = fetch_meta({
        "metadata": {"identifier": "x", "description": "<p>First</p><p>Second&nbsp;para<br>line</p>"},
        "files": [{"name": "x.epub", "size": "2048"}],
    })
    assert md["description"] == "First\nSecond para\nline"


@pytest.mark.fixed
def test_archive_metadata_encodes_and_skips_encrypted():
    md = fetch_meta({
        "metadata": {"identifier": "pdsh", "creator": ["Jake VanderPlas", "O'Reilly"], "date": "2016-11-21",
                     "publicdate": "2022-07-02 12:00:00", "language": ["eng"], "publisher": "O'Reilly"},
        "files": [
            {"name": "Python Data Science Handbook_encrypted.pdf"},
            {"name": "Python Data Science Handbook.lcpdf"},
            {"name": "private.pdf", "private": "true"},
            {"name": "Python Data Science Handbook.epub", "size": "1048576"},
        ],
    })
    assert md["download_url"] == "https://archive.org/download/pdsh/Python%20Data%20Science%20Handbook.epub"
    assert u.is_valid_archive_book_url(md["download_url"])
    assert md["year"] == "2016"
    assert md["authors"] == "Jake VanderPlas, O'Reilly" and md["language"] == "eng"


@pytest.mark.fixed
def test_archive_metadata_skips_lending_only_items():
    md = fetch_meta({
        "metadata": {"identifier": "borrowme", "access-restricted-item": "true"},
        "files": [{"name": "borrowme.pdf"}],
    })
    assert md == {"lending_only": True}  # not downloadable, but counted for the "all borrow-only" hint


@pytest.mark.fixed
def test_archive_search_escapes_quotes():
    src = ar.ArchiveSource(make_config(enable_archive=True), None, 10, "/tmp")
    sess = FakeSession(lambda url, kw: FakeResp({"response": {"docs": []}}))

    async def get_session():
        return sess

    src.get_session = get_session
    run(src._search_archive_books('他说"你好"', 5))
    q = sess.calls[0][1]["params"]["q"]
    assert q.count('"') == 2, q


# ====================================================================== Calibre-Web (local fake CWA)
FEED = ('<?xml version="1.0" encoding="UTF-8"?>\n<feed xmlns="http://www.w3.org/2005/Atom" '
        'xmlns:dc="http://purl.org/dc/terms/" xmlns:dcterms="http://purl.org/dc/terms/">{}</feed>')


def entry(bid, title, author="某人", summary="简介", lang="zho", pub="出版社"):
    return (f"<entry><title>{title}</title><id>urn:uuid:{bid}</id><author><name>{author}</name></author>"
            f"<publisher><name>{pub}</name></publisher><published>2019-01-01T00:00:00+00:00</published>"
            f"<dcterms:language>{lang}</dcterms:language><summary>{summary}</summary>"
            f'<link rel="http://opds-spec.org/image" href="/opds/cover/{bid}" type="image/jpeg"/>'
            f'<link rel="http://opds-spec.org/acquisition" href="/opds/download/{bid}/epub/" '
            f'type="application/epub+zip" length="2048"/></entry>')


AUTH = "Basic " + base64.b64encode(b"user:pw").decode()


async def start_cwa(search_map, discover=None):
    async def search(req):
        if req.headers.get("Authorization") != AUTH:
            return web.Response(status=401)
        q = req.match_info["q"]
        return web.Response(text=FEED.format("".join(search_map.get(q, []))), content_type="application/atom+xml")

    async def disc(req):
        if discover is None:
            return web.Response(status=404)
        return web.Response(text=FEED.format("".join(discover)), content_type="application/atom+xml")

    async def download(req):
        if req.headers.get("Authorization") != AUTH:
            return web.Response(status=401)
        return web.Response(body=b"PK-epub-" + req.match_info["bid"].encode(), headers={
            "Content-Disposition": "attachment; filename*=UTF-8''%E7%94%9F%E7%90%86%E5%AD%A6.epub"})

    app = web.Application()
    app.router.add_get("/opds/search/{q:.*}", search)
    app.router.add_get("/opds/discover", disc)
    app.router.add_get("/opds/download/{bid}/{fmt}/", download)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, f"http://user:pw@127.0.0.1:{port}"


def with_cwa(search_map, body, discover=None, **cfg):
    async def go():
        runner, url = await start_cwa(search_map, discover)
        checker = u.make_safety_checker(None, make_config())
        src = cs.CalibreSource(make_config(enable_calibre=True, calibre_web_url=url, **cfg), None, 10, cfg.get("_tmp", "/tmp"), checker)
        try:
            return await body(src, url)
        finally:
            await src.close()
            await runner.cleanup()
    return run(go())


def node_texts(nodes):
    return ["".join(getattr(c, "text", "") or "" for c in n.content) for n in nodes]


def test_calibre_search_ranks_and_hides_credentials(event):
    smap = {"植物学": [entry(1, "中国植物志 第一卷"), entry(2, "植物学"), entry(3, "植物学实验")]}

    async def body(src, url):
        return await src.search_nodes(event, "植物学", 10)

    texts = node_texts(with_cwa(smap, body))
    assert texts[0].startswith("植物学\n") and texts[1].startswith("植物学实验")
    assert all("user:pw" not in t for t in texts)
    assert "/calibre download http://127.0.0.1:" in texts[0]


def test_calibre_fallback_multi_token(event):
    smap = {
        "王庭槐": [entry(9, "生理学 第9版", author="王庭槐")],
        "生理学": [entry(8, "生理学", author="朱大年"), entry(9, "生理学 第9版", author="王庭槐")],
    }

    async def body(src, url):
        return await src.search_nodes(event, "王庭槐 - 2024 - 生理学", 10)

    texts = node_texts(with_cwa(smap, body))
    assert len(texts) == 2 and "王庭槐" in texts[0]


@pytest.mark.fixed
def test_calibre_filters_before_truncating(event):
    smap = {"植物学": [entry(1, "植物学"), entry(2, "植物学中国即将崩溃"), entry(3, "植物学实验指导与习题解答")]}

    async def body(src, url):
        return await src.search_nodes(event, "植物学", 2)

    texts = node_texts(with_cwa(smap, body))
    assert len(texts) == 2 and all("崩溃" not in t for t in texts)


@pytest.mark.fixed
def test_calibre_filters_summary_and_shows_language(event):
    smap = {"植物学": [entry(1, "植物学", summary="中国即将崩溃 相关"), entry(2, "植物学实验", lang="eng")]}

    async def body(src, url):
        return await src.search_nodes(event, "植物学", 10)

    texts = node_texts(with_cwa(smap, body))
    assert len(texts) == 1 and "语言: eng" in texts[0]


@pytest.mark.fixed
def test_calibre_card_shows_query_excerpt(event):
    book = [entry(1, "生物化学", summary=TOC)]

    def card(query):
        async def body(src, url):
            return await src.search_nodes(event, query, 10)
        return node_texts(with_cwa({"生物化学": book}, body))[0]  # 整句 0 命中时插件会按词拆开重搜

    assert "糖酵解" in card("生物化学 糖酵解")
    assert "糖酵解" not in card("生物化学")  # 查询词都在开头，照旧截开头


@pytest.mark.fixed
def test_calibre_recommend_uses_discover(event):
    smap = {"*": [entry(1, "带*号的书"), entry(2, "另一本*")]}
    discover = [entry(i, f"随机书{i}") for i in range(10, 30)]

    async def body(src, url):
        return await src.recommend(event, 5)

    res = with_cwa(smap, body, discover=discover)
    texts = result_texts(res[0])
    books = [t for t in texts if "下载命令" in t]
    assert len(books) == 5 and all(t.startswith("随机书") for t in books)


@pytest.mark.fixed
def test_calibre_recommend_bounds(event):
    async def body(src, url):
        return await src.recommend(event, 0), await src.recommend(event, 51)

    low, high = with_cwa({}, body, discover=[entry(1, "x")])
    assert "1-50" in result_texts(low[0])[0] and "1-50" in result_texts(high[0])[0]


def test_calibre_download_uses_config_credentials(event, tmp_path):
    async def body(src, url):
        link = url.replace("user:pw@", "") + "/opds/download/42/epub/"
        res = await src.download(event, link)
        return res, read_files(res)

    res, contents = with_cwa({}, body, _tmp=str(tmp_path))
    files = result_files(res)
    assert files and files[0].name == "生理学.epub"
    assert contents == [b"PK-epub-42"]


# ====================================================================== Liber3
@pytest.mark.fixed
def test_liber3_download_url_encodes_filename(event):
    src = lb.Liber3Source(make_config(enable_liber3=True), None, 10)
    bid = "a" * 32

    async def details(ids):
        return {bid: {"book": {"title": "C# 编程 & more?", "extension": "pdf", "ipfs_cid": "bafy123"}}}

    src._get_liber3_book_details = details
    res = run(src.download(event, "L" + bid))
    f = result_files(res)[0]
    assert "#" not in f.url and "%23" in f.url and "&more" not in f.url
    assert f.name.endswith(".pdf") and "?" not in f.name


@pytest.mark.fixed
def test_liber3_failure_is_not_reported_as_no_results(event):
    src = lb.Liber3Source(make_config(enable_liber3=True), None, 10)

    async def failed(*a):
        return None

    async def empty(*a):
        return {"search_results": [], "detailed_books": {}}

    src._search_liber3_books_with_details = failed
    assert "无法访问" in run(src.search_nodes(event, "x", 5))
    src._search_liber3_books_with_details = empty
    assert "未找到" in run(src.search_nodes(event, "x", 5))


# ====================================================================== streaming downloads / failure reporting
@pytest.mark.fixed
def test_zlib_failed_download_leaves_no_temp_files(fake_zlib, tmp_path, event):
    fake_zlib.download_script = [("Part.pdf", ZlibraryError("文件下载失败（尝试了 5 次）：short read"))]
    src = zs.ZlibSource(zcfg(), None, 10, str(tmp_path))
    res = run(src.download(event, "123", "0a0b0c"))
    assert "文件下载失败" in result_texts(res[0])[0]
    assert os.listdir(tmp_path) == []


Zl = importlib.import_module(f"{PKG}.Zlibrary")


class FakeDl:
    def __init__(self, status, headers, chunks, fail=False):
        self.status_code, self.headers, self.chunks, self.fail = status, headers, chunks, fail

    def iter_content(self, chunk_size=None):
        yield from self.chunks
        if self.fail:
            raise IOError("connection reset")

    def close(self):
        pass


def stream_book(tmp_path, monkeypatch, responses):
    z = Zl.Zlibrary(domain="z.invalid")
    z._Zlibrary__loggedin = True
    z._Zlibrary__makeGetRequest = lambda url, params={}, cookies=None: {
        "success": 1, "file": {"downloadLink": "https://cdn.invalid/f", "description": "Book", "extension": "pdf"}}
    ranges = []

    def fake_get(url, **kw):
        ranges.append(kw["headers"].get("Range"))
        return responses.pop(0)

    monkeypatch.setattr(Zl.requests, "get", fake_get)
    monkeypatch.setattr(Zl.time, "sleep", lambda s: None)
    name, path, size = z.downloadBook({"id": 1, "hash": "abcdef"}, target=lambda n: str(tmp_path / n))
    with open(path, "rb") as f:
        return name, f.read(), size, ranges


@pytest.mark.fixed
def test_zlibrary_streams_to_disk_and_resumes(tmp_path, monkeypatch):
    name, content, size, ranges = stream_book(tmp_path, monkeypatch, [
        FakeDl(200, {"Content-Length": "10", "Accept-Ranges": "bytes"}, [b"abc", b"de"], fail=True),
        FakeDl(206, {"Content-Length": "5", "Accept-Ranges": "bytes"}, [b"fghij"]),
    ])
    assert name == "Book.pdf" and content == b"abcdefghij" and size == 10
    assert ranges == [None, "bytes=5-"]


@pytest.mark.fixed
def test_zlibrary_restart_without_range_support_truncates(tmp_path, monkeypatch):
    _, content, size, ranges = stream_book(tmp_path, monkeypatch, [
        FakeDl(200, {"Content-Length": "10"}, [b"abcde"], fail=True),
        FakeDl(200, {"Content-Length": "10"}, [b"abcdefghij"]),
    ])
    assert content == b"abcdefghij" and size == 10 and ranges == [None, None]


def archive_src(monkeypatch, route_q, checker=None, restricted=()):
    meta = {"metadata": {"identifier": "x", "creator": "Rosner"}, "files": [{"name": "x.pdf", "size": "2048"}]}

    def route(url, kw):
        if url.endswith("advancedsearch.php"):
            return route_q(kw["params"]["q"])
        ident = url.rsplit("/", 1)[-1]
        extra = {"access-restricted-item": "true"} if ident in restricted else {}
        return FakeResp({**meta, "metadata": {**meta["metadata"], "identifier": ident, **extra}})

    src = ar.ArchiveSource(make_config(enable_archive=True), None, 10, "/tmp", checker)
    sess = FakeSession(route)

    async def get_session():
        return sess

    async def ok(*a, **k):
        return True

    src.get_session = get_session
    monkeypatch.setattr(ar, "is_url_accessible", ok)
    return src


def docs(*titles):
    return FakeResp({"response": {"docs": [{"identifier": f"id{i}", "title": t} for i, t in enumerate(titles)]}})


@pytest.mark.fixed
def test_archive_relaxes_query_when_phrase_misses(monkeypatch):
    qs = []

    def route_q(q):
        qs.append(q)
        hit = not q.startswith('title:"') and "8th" not in q and "edition" not in q
        return docs("Fundamentals of biostatistics") if hit else docs()

    src = archive_src(monkeypatch, route_q)
    books = run(src._search_archive_books("Fundamentals of Biostatistics 8th edition", 5))
    assert [b["title"] for b in books] == ["Fundamentals of biostatistics"]
    assert len(qs) == 4
    assert "(title:(biostatistics) OR creator:(biostatistics))" in qs[-1] and "8th" not in qs[-1]


@pytest.mark.fixed
def test_archive_relax_is_bounded_and_keeps_two_words(monkeypatch):
    qs = []
    src = archive_src(monkeypatch, lambda q: (qs.append(q), docs())[1])
    assert run(src._search_archive_books("a1 b2 c3 d4 e5 f6", 5)) == []
    assert len(qs) == 1 + ar.RELAX_MAX_STEPS
    qs.clear()
    run(src._search_archive_books("微积分 刘建亚", 5))
    assert len(qs) == 2  # phrase + one relaxed try; a single CJK word is never searched alone


@pytest.mark.fixed
def test_archive_api_failure_is_not_reported_as_no_results(monkeypatch, event):
    src = archive_src(monkeypatch, lambda q: FakeResp({}, status=503))
    out = run(src.search_nodes(event, "python", 5))
    assert isinstance(out, str) and "暂时不可用" in out


@pytest.mark.fixed
def test_archive_filters_before_truncating(monkeypatch, event):
    checker = u.make_safety_checker(None, make_config(extra_filter_keywords=["禁书测试词"]))
    src = archive_src(monkeypatch, lambda q: docs("禁书测试词 之书", "Python 入门", "Python 进阶"), checker)
    out = run(src.search_nodes(event, "python", 2))
    titles = ["".join(getattr(c, "text", "") or "" for c in n.content).split("\n")[0] for n in out]
    assert titles == ["Python 入门", "Python 进阶"]


@pytest.mark.fixed
def test_calibre_auth_failure_is_not_reported_as_no_results(event):
    async def body(src, url):
        src.config["calibre_web_url"] = url.replace("user:pw@", "")
        return await src.search_nodes(event, "植物学", 10)

    out = with_cwa({"植物学": [entry(1, "植物学")]}, body)
    assert isinstance(out, str) and "无法从书库获取" in out


@pytest.mark.fixed
def test_archive_all_lending_only_is_reported(monkeypatch, event):
    src = archive_src(monkeypatch, lambda q: docs("Fundamentals A", "Fundamentals B"), restricted={"id0", "id1"})
    out = run(src.search_nodes(event, "fundamentals", 5))
    assert isinstance(out, str) and "找到 2 本，但都只能在线借阅" in out


@pytest.mark.fixed
def test_archive_lending_only_mixed_shows_downloadable(monkeypatch, event):
    src = archive_src(monkeypatch, lambda q: docs("Borrow Only", "Free Copy"), restricted={"id0"})
    out = run(src.search_nodes(event, "copy", 5))
    titles = ["".join(getattr(c, "text", "") or "" for c in n.content).split("\n")[0] for n in out]
    assert titles == ["Free Copy"]
