import asyncio
import importlib
import os

import pytest
from conftest import PKG, Node, Nodes, Plain, book_node, hint_node, make_config, make_context, run, FakeEvent, FakeRerankProvider

u = importlib.import_module(f"{PKG}.utils")


# ---------------------------------------------------------------- text matching
def test_normalize_match_text():
    assert u.normalize_match_text("《生态植保：理论、技术与实践》") == "生态植保理论技术与实践"
    assert u.normalize_match_text("Ｐｙｔｈｏｎ  Crash-Course") == "pythoncrashcourse"
    assert u.normalize_match_text(None) == ""


def test_match_score_prefers_prefix_and_ignores_single_chars():
    q = u.normalize_match_text("植物保护案例分析")
    prefix = u.match_score(q, u.normalize_match_text("植物保护专业英语教程"))
    tail = u.match_score(q, u.normalize_match_text("物业管理案例分析"))
    assert prefix > tail > 0
    assert u.match_score(q, "学") == 0
    assert u.match_score("", "abc") == 0


# ---------------------------------------------------------------- filenames
def test_sanitize_filename():
    assert u.sanitize_filename('a:b*c?"d"<e>|f') == "a b c d e f"
    assert u.sanitize_filename("  ..x.. ") == "x"
    assert u.sanitize_filename("///") == "unnamed"


def test_truncate_filename_byte_budget():
    short = "Python Crash Course.pdf"
    assert u.truncate_filename(short) == short
    long_cjk = "植物保护" * 30 + ".epub"
    out = u.truncate_filename(long_cjk)
    assert len(out.encode("utf-8")) <= 100
    assert out.endswith(" <省略>.epub")
    assert u.truncate_filename("核酸酶学 : 基础与应用.pdf") == "核酸酶学 基础与应用.pdf"


def test_format_bytes_and_cleanup_delay():
    assert u.format_bytes(512) == "512B"
    assert u.format_bytes(1536) == "1.5KB"
    assert u.format_bytes(None) == ""
    assert u.upload_cleanup_delay(0) == 5
    assert u.upload_cleanup_delay(50_000_000) == 50
    assert u.upload_cleanup_delay(10**12) == 1800


def test_split_url_credentials():
    clean, creds = u.split_url_credentials("http://user:secret@host:8083/x")
    assert clean == "http://host:8083/x" and creds == ("user", "secret")
    assert u.split_url_credentials("http://host/x") == ("http://host/x", None)


@pytest.mark.fixed
def test_split_url_credentials_percent_decodes():
    # 密码含 @ : / 时 URL 里必须写成 %40 等，BasicAuth 要的是解码后的原文。
    _, creds = u.split_url_credentials("http://us%40er:p%40ss%3Aw@host:8083/")
    assert creds == ("us@er", "p@ss:w")


# ---------------------------------------------------------------- query / limit parsing
def test_split_query_and_limit_unchanged_cases():
    assert u.split_query_and_limit("生理学 姚泰") == ("生理学 姚泰", "")
    assert u.split_query_and_limit("Python 20") == ("Python", "20")
    assert u.split_query_and_limit("  三体 3 ") == ("三体", "3")
    assert u.split_query_and_limit("1984") == ("1984", "")
    assert u.split_query_and_limit("") == ("", "")


@pytest.mark.fixed
def test_split_query_and_limit_keeps_years_and_long_numbers():
    assert u.split_query_and_limit("生理学 2018") == ("生理学 2018", "")
    assert u.split_query_and_limit("植物学 9787040396638") == ("植物学 9787040396638", "")
    assert u.split_query_and_limit("Python 100") == ("Python", "100")


def test_normalize_limit():
    assert u.normalize_limit("", 10, 1, 50) == (10, None)
    assert u.normalize_limit("5", 10, 1, 50) == (5, None)
    assert u.normalize_limit("0", 10, 1, 50)[0] is None
    assert u.normalize_limit("80", 10, 1, 50)[0] is None
    assert u.normalize_limit("80", 10, 1, 60, clamp_max=True) == (60, None)


# ---------------------------------------------------------------- id / url validators
def test_validators_on_strings():
    assert u.is_valid_calibre_book_url("http://h:8083/opds/download/12/epub/")
    assert not u.is_valid_calibre_book_url("http://h:8083/book/12")
    assert u.is_valid_liber3_book_id("L" + "a" * 32)
    assert not u.is_valid_liber3_book_id("A" + "a" * 32)
    assert u.is_valid_annas_book_id("A" + "0f" * 16)
    assert not u.is_valid_annas_book_id("A" + "0f" * 15)
    assert u.is_valid_zlib_book_id("11033158")
    assert not u.is_valid_zlib_book_id("11a")
    assert u.is_valid_zlib_book_hash("e5897f")
    assert u.is_valid_zlib_book_hash("012345")
    assert not u.is_valid_zlib_book_hash("e5897")


@pytest.mark.fixed
def test_archive_url_validator_accepts_real_urls():
    ok = [
        "https://archive.org/download/pythontricks/PythonTricksBookbyDanBader-1.epub",
        "https://archive.org/download/python-data-science-handbook.p/Python%20Data%20Science%20Handbook.epub",
        "https://archive.org/download/some-item/sub/dir/book.pdf",
    ]
    bad = [
        "https://archive.org/details/pythontricks",
        "https://evil.example/download/a/b.pdf",
        "http://archive.org/download/a/b.pdf",
        "https://archive.org/download/a/b c.pdf",
        "https://archive.org/download/onlyidentifier",
    ]
    assert all(u.is_valid_archive_book_url(x) for x in ok)
    assert not any(u.is_valid_archive_book_url(x) for x in bad)


@pytest.mark.fixed
def test_validators_tolerate_int_input():
    # AstrBot turns all-digit args into int when the handler default is None.
    assert u.is_valid_calibre_book_url(12345) is False
    assert u.is_valid_archive_book_url(12345) is False
    assert u.is_valid_liber3_book_id(12345) is False
    assert u.is_valid_annas_book_id(12345) is False


# ---------------------------------------------------------------- content filter
def test_safety_checker_builtin_and_boundaries():
    checker = u.make_safety_checker(None, make_config(extra_filter_keywords=["测试"]))
    assert checker is not None
    assert checker("中国即将崩溃") is False
    assert checker("中國即將崩潰") is False  # zhconv 简繁扩充
    assert checker("人民卫生出版社 生理学 第9版") is True
    assert checker("测试") is False
    assert checker("软件测试方法") is True  # 2 字词只做整词匹配
    assert u.make_safety_checker(None, make_config(enable_content_filter=False)) is None


def test_filter_unsafe_dicts_and_objects():
    checker = u.make_safety_checker(None, make_config())
    items = [{"title": "生理学"}, {"title": "中国即将崩溃"}, {"title": "x", "author": "中国即将崩溃"}]
    kept = u.filter_unsafe(items, checker, fields=["title", "author"], source="t")
    assert kept == [{"title": "生理学"}]
    assert u.filter_unsafe(items, None, fields=["title"]) == items


@pytest.mark.fixed
def test_safety_checker_ascii_keywords_are_whole_words():
    # 'anal' 以前按子串匹配，简介带 analysis 的书全被滤掉（「data analysis」100 条丢 70 条）。
    checker = u.make_safety_checker(None, make_config(extra_filter_keywords=["anal", "porn", "fuck", "gay片"]))
    assert checker("reviews of complex numbers and phasor analysis") is True
    assert checker("Suez Canal") is True
    assert checker("banal stories") is True
    assert checker("anal sex") is False
    assert checker("porn videos") is False
    assert checker("看porn视频") is False  # 紧挨中文也算词边界
    assert checker("fucking") is False  # 词形变化照样拦
    assert checker("gay片合集") is False  # 中英混写的词仍按子串


# ---------------------------------------------------------------- edition
@pytest.mark.fixed
@pytest.mark.parametrize("query, expected", [
    ("fundamentals of biostatistics 8th edition", "fundamentals of biostatistics 8th"),
    ("fundamentals of biostatistics  Edition 8", "fundamentals of biostatistics 8"),
    ("8th-edition calculus", "8th calculus"),
    ("norton anthology editions", "norton anthology"),
    ("生理学 第九版", "生理学 第九版"),  # only the English word goes; ordinals and 第N版 stay
    ("fundamentals of biostatistics 8e", "fundamentals of biostatistics 8e"),
    ("editorial design", "editorial design"),  # whole word only
    ("edition", "edition"),  # nothing would be left, so the query is kept
])
def test_strip_edition_word(query, expected):
    assert u.strip_edition_word(query) == expected


# ---------------------------------------------------------------- html → text
@pytest.mark.fixed
def test_html_to_text_keeps_lines_and_joins_inline_tags():
    raw = ("<p>生物<b>化学</b>&nbsp;第9版</p><p>目录：</p><ul><li>糖酵解</li><li>三羧酸循环</li></ul>"
           "1 (p1): 第一章 <br>21 (p2): 第二章<br/>正文里写的 &lt;br&gt; 保留<!-- 注释 --> A&amp;B")
    assert u.html_to_text(raw) == (
        "生物化学 第9版\n目录：\n· 糖酵解\n· 三羧酸循环\n1 (p1): 第一章\n21 (p2): 第二章\n正文里写的 <br> 保留 A&B"
    )
    assert u.html_to_text("纯文本  多个   空格\n\n\n第二段") == "纯文本 多个 空格\n第二段"
    assert u.html_to_text(None) == "" and u.html_to_text("") == ""


@pytest.mark.fixed
def test_excerpt_keeps_line_breaks_from_html():
    toc = "".join(f"{i} (p{i}): 第{i}章 其他内容<br>" for i in range(1, 30)) + "90 (p30): 第三十章 糖酵解<br>"
    e = u.excerpt_for_query(toc, "糖酵解", 150)
    assert "<br>" not in e and "\n" in e and "糖酵解" in e
    assert e.startswith("1 (p1): 第1章 其他内容\n2 (p2)")


# ---------------------------------------------------------------- excerpt / isbn
_TOC = "本书为国家规划教材，供基础、临床、预防、口腔医学类专业用。全书共分二十二章。" * 4 + \
    "第四章 糖代谢 第一节 糖的消化吸收 第二节 糖的无氧氧化（糖酵解） 第三节 糖的有氧氧化" + "第五章 生物氧化。" * 10


@pytest.mark.fixed
@pytest.mark.parametrize("query", ["糖酵解", "生物化学 糖酵解", "生物化学糖酵解"])
def test_excerpt_includes_far_match(query):
    e = u.excerpt_for_query(_TOC, query, 150)
    assert "糖酵解" in e and e.startswith("本书为国家规划教材") and len(e) <= 152


@pytest.mark.fixed
def test_excerpt_falls_back_to_head():
    assert u.excerpt_for_query(_TOC, "python", 150) == _TOC[:150] + "…"
    assert u.excerpt_for_query(_TOC, "", 150) == _TOC[:150] + "…"
    assert u.excerpt_for_query(_TOC, "临床", 150) == _TOC[:150] + "…"  # 命中本来就在开头
    assert u.excerpt_for_query("短简介", "糖酵解") == "短简介"
    en = "A comprehensive introduction. " * 8 + "Chapter 5 covers glycolysis in detail. " + "More. " * 30
    e = u.excerpt_for_query(en, "biochemistry of glycolysis", 150)
    assert "glycolysis" in e and "…ntroduction" not in e  # of 不算命中；不从单词中间切


@pytest.mark.fixed
def test_isbn_set():
    assert u.isbn_set("9781593276034,1593276036, B09ABC1234") == {"9781593276034", "1593276036"}
    assert u.isbn_set("978-7-117-26393-2") == {"9787117263932"}
    assert u.isbn_set("") == frozenset() and u.isbn_set(None) == frozenset()


@pytest.mark.fixed
def test_node_doc_text_keeps_intro_within_budget():
    from conftest import Node, Plain
    n = Node(uin="1", name="Z", content=[
        Plain("生物化学\n"), Plain("作者: 周春燕\n"), Plain("年份: 2018\n"), Plain("出版社: 人民卫生出版社\n"),
        Plain("语言: chinese\n"), Plain("文件: PDF · 50 MB · 600页\n"), Plain("ISBN: 9787117263932\n"),
        Plain("MD5: " + "a" * 32 + "\n"), Plain("简介: " + "填充" * 60 + "…糖酵解…\n"), Plain("下载命令:\n/zlib download 1 abcdef"),
    ])
    doc = u.node_doc_text(n, 300)
    assert "糖酵解" in doc and "MD5" not in doc and "下载命令" not in doc
    assert doc.index("简介") < doc.index("ISBN") if "ISBN" in doc else True


# ---------------------------------------------------------------- nodes / results
def test_is_book_node_and_doc_text():
    n = book_node("生理学", "/zlib download 1 abcdef")
    assert u.is_book_node(n)
    assert not u.is_book_node(hint_node("[Z-Library] 未找到"))
    doc = u.node_doc_text(n, 300)
    assert "生理学" in doc and "下载命令" not in doc
    assert len(u.node_doc_text(n, 3)) == 3


def test_to_event_results_chunking():
    ev = FakeEvent()
    nodes = [book_node(f"b{i}", f"/x {i}") for i in range(65)]
    merged = u.to_event_results(ev, "p", nodes)
    assert [len(r.chain[0].nodes) for r in merged] == [30, 30, 5]
    single = u.to_event_results(ev, "p", nodes[:3], merge_forward=False)
    assert len(single) == 3 and not isinstance(single[0].chain[0], Nodes)
    assert u.to_event_results(ev, "p", "oops")[0].chain[0].text == "oops"


def test_get_rerank_provider():
    prov = FakeRerankProvider(lambda d: 1)
    assert u.get_rerank_provider(make_context(prov), make_config()) is prov
    assert u.get_rerank_provider(make_context(None), make_config()) is None


# ---------------------------------------------------------------- temp files
@pytest.mark.fixed
def test_temp_download_paths_are_unique_and_cleaned(tmp_path):
    p1 = u.make_temp_download_path(str(tmp_path), "同一本书.pdf")
    p2 = u.make_temp_download_path(str(tmp_path), "同一本书.pdf")
    assert p1 != p2
    assert os.path.basename(p1) == os.path.basename(p2) == "同一本书.pdf"
    for p in (p1, p2):
        with open(p, "wb") as f:
            f.write(b"x")

    async def go():
        u.schedule_temp_cleanup(p1, 0.01)
        await asyncio.sleep(0.2)

    run(go())
    assert not os.path.exists(p1) and not os.path.exists(os.path.dirname(p1))
    assert os.path.exists(p2)
    u.discard_temp_file(p2)
    assert not os.path.exists(os.path.dirname(p2))
    assert os.path.isdir(tmp_path)  # 根目录本身不能删


# ---------------------------------------------------------------- rerank instruction / temp sweep
@pytest.mark.fixed
def test_rerank_instruction_defaults_to_raw_pair():
    import json
    schema_path = os.path.join(os.path.dirname(u.__file__), "_conf_schema.json")
    schema = json.load(open(schema_path, encoding="utf-8-sig"))
    assert schema["rerank_instruction"]["default"] == ""  # non-Qwen3 rerankers must get the raw pair
    assert u.rerank_inputs({}, "q", ["d"]) == ("q", ["d"])
    assert u.rerank_inputs({"rerank_instruction": "  "}, "q", ["d"]) == ("q", ["d"])
    q, docs = u.rerank_inputs({"rerank_instruction": "找对书"}, "q", ["d"])
    assert "<Instruct>: 找对书\n<Query>: q\n" in q and docs[0].startswith("<Document>: d")


@pytest.mark.fixed
def test_plugin_modules_use_relative_imports():
    # Absolute data.plugins.astrbot_plugin_ebooks imports break when the plugin dir has another name.
    import glob
    root = os.path.dirname(u.__file__)
    offenders = [f for f in glob.glob(os.path.join(root, "**", "*.py"), recursive=True)
                 if "data.plugins.astrbot_plugin_ebooks" in open(f, encoding="utf-8").read()]
    assert offenders == []


@pytest.mark.fixed
def test_sweep_stale_temp_dirs(tmp_path):
    import time
    old = tmp_path / "ebooks-old"
    old.mkdir()
    (old / "a.pdf").write_bytes(b"x")
    fresh = tmp_path / "ebooks-new"
    fresh.mkdir()
    other = tmp_path / "other-old"
    other.mkdir()
    past = time.time() - 7200
    os.utime(old, (past, past))
    os.utime(other, (past, past))
    assert u.sweep_stale_temp_dirs(str(tmp_path)) == 1
    assert not old.exists() and fresh.exists() and other.exists()
