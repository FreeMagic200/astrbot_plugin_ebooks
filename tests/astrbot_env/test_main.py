import asyncio
import importlib
import os
from types import SimpleNamespace

import pytest
from astrbot.core.star.filter.command import CommandFilter
from conftest import (
    PKG, FakeRerankProvider, Nodes, book_node, collect, file_path, make_config, make_context,
    result_files, result_texts, run,
)

main = importlib.import_module(f"{PKG}.main")
Plugin = main.ebooks


def make_plugin(provider=None, **over):
    return Plugin(make_context(provider), make_config(**over))


def stub_search(inst, attr, result, calls):
    async def fake(event, query, limit):
        calls.append((attr, query, limit))
        return result(limit) if callable(result) else result
    getattr(inst, attr).search_nodes = fake


def books(prefix, n, name):
    return lambda limit: [book_node(f"{prefix}{i}", f"/x download {prefix}{i}", name) for i in range(min(n, limit))]


def parse(fn, args):
    cf = CommandFilter("x", handler_md=SimpleNamespace(handler=fn))
    return cf.validate_and_convert_params(args, cf.handler_params)


def flat(results):
    out = []
    for r in results:
        out.extend(result_texts(r))
    return out


# ---------------------------------------------------------------- weights
def test_platform_weight_parsing(fake_zlib):
    inst = make_plugin(platform_weight_zlib=1.5, platform_weight_annas="abc", platform_weight_archive=-2)
    assert inst._platform_weight("Z-Library") == 1.5
    assert inst._platform_weight("Anna's Archive") == 1.0
    assert inst._platform_weight("archive.org") == 0.0
    assert inst._platform_weight("Calibre-Web") == 1.0
    assert inst._platform_weight("unknown") == 1.0


@pytest.mark.fixed
def test_platform_weight_zero_is_honoured(fake_zlib):
    inst = make_plugin(platform_weight_zlib=0)
    assert inst._platform_weight("Z-Library") == 0.0


# ---------------------------------------------------------------- merged search
def test_merge_search_status_first_then_books(fake_zlib, event):
    inst = make_plugin(enable_calibre=True, enable_zlib=True, zlib_email="a", zlib_password="b")
    calls = []
    stub_search(inst, "calibre_source", "[Calibre-Web] 未找到匹配的电子书。", calls)
    stub_search(inst, "zlib_source", books("Z", 3, "Z-Library"), calls)
    out = run(collect(inst.search_all_platforms(event, "植物学")))
    assert len(out) == 1 and isinstance(out[0].chain[0], Nodes)
    texts = flat(out)
    assert "未找到" in texts[0] and [t.split("\n")[0] for t in texts[1:]] == ["Z0", "Z1", "Z2"]
    assert sorted(calls) == [("calibre_source", "植物学", 10), ("zlib_source", "植物学", 10)]


def test_merge_search_global_rerank(fake_zlib, event):
    scores = {"Z1": 5, "C0": 3, "C1": 2, "Z0": 1}
    prov = FakeRerankProvider(lambda d: scores[d[:2]])
    inst = make_plugin(prov, enable_calibre=True, enable_zlib=True, zlib_email="a", zlib_password="b", enable_rerank=True)
    calls = []
    stub_search(inst, "calibre_source", books("C", 2, "Calibre-Web"), calls)
    stub_search(inst, "zlib_source", books("Z", 2, "Z-Library"), calls)
    texts = flat(run(collect(inst.search_all_platforms(event, "q 5"))))
    assert [t.split("\n")[0] for t in texts] == ["Z1", "C0", "C1", "Z0"]


def test_platform_weight_orders_segments_without_rerank(fake_zlib, event):
    inst = make_plugin(enable_calibre=True, enable_zlib=True, zlib_email="a", zlib_password="b", platform_weight_zlib=2)
    calls = []
    stub_search(inst, "calibre_source", books("C", 2, "Calibre-Web"), calls)
    stub_search(inst, "zlib_source", books("Z", 2, "Z-Library"), calls)
    texts = flat(run(collect(inst.search_all_platforms(event, "q"))))
    assert [t.split("\n")[0] for t in texts] == ["Z0", "Z1", "C0", "C1"]


@pytest.mark.fixed
def test_rerank_candidates_cover_every_platform(fake_zlib, event):
    prov = FakeRerankProvider(lambda d: 1)
    inst = make_plugin(prov, enable_calibre=True, enable_zlib=True, zlib_email="a", zlib_password="b",
                       enable_rerank=True, rerank_candidates=4)
    calls = []
    stub_search(inst, "calibre_source", books("C", 5, "Calibre-Web"), calls)
    stub_search(inst, "zlib_source", books("Z", 5, "Z-Library"), calls)
    texts = flat(run(collect(inst.search_all_platforms(event, "q"))))
    fed = prov.calls[0][1]
    assert len(fed) == 4 and any(d.startswith("Z") for d in fed) and any(d.startswith("C") for d in fed)
    assert len(texts) == 10


@pytest.mark.fixed
def test_zero_weight_sinks_platform_in_rerank(fake_zlib, event):
    prov = FakeRerankProvider(lambda d: 9 if d.startswith("Z") else 1)
    inst = make_plugin(prov, enable_calibre=True, enable_zlib=True, zlib_email="a", zlib_password="b",
                       enable_rerank=True, platform_weight_zlib=0)
    calls = []
    stub_search(inst, "calibre_source", books("C", 2, "Calibre-Web"), calls)
    stub_search(inst, "zlib_source", books("Z", 2, "Z-Library"), calls)
    texts = flat(run(collect(inst.search_all_platforms(event, "q"))))
    assert [t[0] for t in texts] == ["C", "C", "Z", "Z"]


@pytest.mark.fixed
def test_non_merge_mode_also_reranks(fake_zlib, event):
    prov = FakeRerankProvider(lambda d: 9 if d.startswith("Z") else 1)
    inst = make_plugin(prov, enable_calibre=True, enable_zlib=True, zlib_email="a", zlib_password="b",
                       enable_rerank=True, enable_merge_forward=False)
    calls = []
    stub_search(inst, "calibre_source", books("C", 2, "Calibre-Web"), calls)
    stub_search(inst, "zlib_source", books("Z", 2, "Z-Library"), calls)
    out = run(collect(inst.search_all_platforms(event, "q")))
    assert len(out) == 4 and not isinstance(out[0].chain[0], Nodes)
    assert [flat([r])[0][0] for r in out] == ["Z", "Z", "C", "C"]


@pytest.mark.fixed
def test_merged_forward_is_chunked(fake_zlib, event):
    inst = make_plugin(enable_calibre=True, enable_zlib=True, zlib_email="a", zlib_password="b", max_results=20,
                       merged_display_limit=0)
    calls = []
    stub_search(inst, "calibre_source", books("C", 20, "Calibre-Web"), calls)
    stub_search(inst, "zlib_source", books("Z", 20, "Z-Library"), calls)
    out = run(collect(inst.search_all_platforms(event, "q")))
    assert [len(r.chain[0].nodes) for r in out] == [30, 10]


@pytest.mark.fixed
def test_merged_display_limit_truncates_with_hint(fake_zlib, event):
    inst = make_plugin(enable_calibre=True, enable_zlib=True, zlib_email="a", zlib_password="b", max_results=20,
                       merged_display_limit=30)
    calls = []
    stub_search(inst, "calibre_source", books("C", 20, "Calibre-Web"), calls)
    stub_search(inst, "zlib_source", books("Z", 20, "Z-Library"), calls)
    texts = flat(run(collect(inst.search_all_platforms(event, "q"))))
    assert sum("下载命令" in t for t in texts) == 30
    assert "其余 10 条候选未显示" in texts[-1]


@pytest.mark.fixed
def test_display_limit_keeps_every_platform_without_rerank(fake_zlib, event):
    # Default weights order segments Calibre -> archive -> Z-Library; a head cut used to drop Z-Library.
    inst = make_plugin(enable_calibre=True, enable_archive=True, enable_zlib=True, zlib_email="a", zlib_password="b",
                       max_results=20, merged_display_limit=30)
    calls = []
    stub_search(inst, "calibre_source", books("C", 20, "Calibre-Web"), calls)
    stub_search(inst, "archive_source", books("A", 12, "archive.org"), calls)
    stub_search(inst, "zlib_source", books("Z", 20, "Z-Library"), calls)
    texts = flat(run(collect(inst.search_all_platforms(event, "q"))))
    assert [t[0] for t in texts if "下载命令" in t] == ["C"] * 10 + ["A"] * 10 + ["Z"] * 10
    assert "其余 22 条候选未显示" in texts[-1]


@pytest.mark.fixed
def test_slow_platform_times_out_without_blocking_others(fake_zlib, event, monkeypatch):
    monkeypatch.setattr(main, "PLATFORM_SEARCH_TIMEOUT", 0.05)
    inst = make_plugin(enable_calibre=True, enable_zlib=True, zlib_email="a", zlib_password="b")
    stub_search(inst, "calibre_source", books("C", 2, "Calibre-Web"), [])

    async def slow(event, query, limit):
        await asyncio.sleep(5)
        return []

    inst.zlib_source.search_nodes = slow
    texts = flat(run(collect(inst.search_all_platforms(event, "q"))))
    assert any("[Z-Library] 搜索超时" in t for t in texts)
    assert [t.split("\n")[0] for t in texts if "下载命令" in t] == ["C0", "C1"]


@pytest.mark.fixed
def test_rerank_instruction_wraps_query_and_docs(fake_zlib, event):
    prov = FakeRerankProvider(lambda d: 1)
    inst = make_plugin(prov, enable_calibre=True, enable_rerank=True, rerank_instruction="只要第 8 版")
    stub_search(inst, "calibre_source", books("C", 2, "Calibre-Web"), [])
    run(collect(inst.search_all_platforms(event, "biostatistics 8th edition")))
    query, docs, _ = prov.calls[0]
    assert "<Instruct>: 只要第 8 版\n<Query>: biostatistics 8th edition\n" in query
    assert docs and all(d.startswith("<Document>: C") for d in docs)


def test_empty_rerank_instruction_sends_raw_pair(fake_zlib, event):
    prov = FakeRerankProvider(lambda d: 1)
    inst = make_plugin(prov, enable_calibre=True, enable_rerank=True, rerank_instruction="")
    stub_search(inst, "calibre_source", books("C", 2, "Calibre-Web"), [])
    run(collect(inst.search_all_platforms(event, "q")))
    query, docs, _ = prov.calls[0]
    assert query == "q" and docs[0].startswith("C0")


@pytest.mark.fixed
def test_no_platform_enabled_message(fake_zlib, event):
    inst = make_plugin()
    out = run(collect(inst.search_all_platforms(event, "q")))
    assert any("未启用" in t for t in flat(out))


@pytest.mark.fixed
def test_large_max_results_is_clamped_not_rejected(fake_zlib, event):
    inst = make_plugin(enable_calibre=True, max_results=80)
    calls = []
    stub_search(inst, "calibre_source", books("C", 2, "Calibre-Web"), calls)
    texts = flat(run(collect(inst.search_all_platforms(event, "植物学"))))
    assert not any("之间" in t for t in texts)
    assert calls == [("calibre_source", "植物学", 20)]


def test_explicit_limit_is_passed_per_platform(fake_zlib, event):
    inst = make_plugin(enable_calibre=True, per_platform_results=0)
    calls = []
    stub_search(inst, "calibre_source", books("C", 2, "Calibre-Web"), calls)
    run(collect(inst.search_all_platforms(event, "植物学 学名 7")))
    assert calls == [("calibre_source", "植物学 学名", 7)]


@pytest.mark.fixed
def test_llm_search_uses_max_results(fake_zlib, event):
    inst = make_plugin(enable_calibre=True, max_results=7)
    calls = []
    stub_search(inst, "calibre_source", books("C", 2, "Calibre-Web"), calls)
    run(collect(inst.search_ebooks(event, "Python 3")))
    assert calls == [("calibre_source", "Python 3", 7)]


# ---------------------------------------------------------------- download routing
def stub_downloads(inst):
    seen = []

    def rec(name):
        async def fake(event, *args):
            seen.append((name, args))
            return [event.plain_result(f"ok {name}")]
        return fake

    inst.calibre_source.download = rec("calibre")
    inst.archive_source.download = rec("archive")
    inst.liber3_source.download = rec("liber3")
    inst.annas_source.download = rec("annas")
    inst.zlib_source.download = rec("zlib")
    return seen


@pytest.mark.parametrize("args,expect", [
    (("http://h:8083/opds/download/12/epub/",), "calibre"),
    (("L" + "a" * 32,), "liber3"),
    (("A" + "0f" * 16,), "annas"),
    (("11033158", "e5897f"), "zlib"),
])
def test_download_routing(fake_zlib, event, args, expect):
    inst = make_plugin()
    seen = stub_downloads(inst)
    run(collect(inst.download_all_platforms(event, *args)))
    assert seen and seen[0][0] == expect


@pytest.mark.fixed
def test_download_routing_archive(fake_zlib, event):
    inst = make_plugin()
    seen = stub_downloads(inst)
    run(collect(inst.download_all_platforms(event, "https://archive.org/download/x/Some%20Book.epub")))
    assert seen and seen[0][0] == "archive"


def test_download_unrecognised(fake_zlib, event):
    inst = make_plugin()
    stub_downloads(inst)
    texts = flat(run(collect(inst.download_all_platforms(event, "hello"))))
    assert "未识别" in texts[0]


# ---------------------------------------------------------------- AstrBot command parsing
def test_parse_greedy_search():
    assert parse(Plugin.search_zlib, ["生理学", "姚泰", "10"]) == {"query": "生理学 姚泰 10"}
    assert parse(Plugin.search_all_platforms, []) == {"query": ""}


def test_parse_recommend_int():
    assert parse(Plugin.recommend_calibre, ["5"]) == {"n": 5}


@pytest.mark.fixed
def test_parse_zlib_hash_keeps_leading_zero():
    p = parse(Plugin.download_zlib, ["11033158", "012345"])
    assert p == {"book_id": "11033158", "book_hash": "012345"}
    assert parse(Plugin.download_all_platforms, ["11033158", "000123"])["arg2"] == "000123"


@pytest.mark.fixed
def test_zlib_download_end_to_end_with_numeric_hash(fake_zlib, event):
    fake_zlib.download_script = [("Book.pdf", b"%PDF-ok")]
    inst = make_plugin(enable_zlib=True, zlib_email="a", zlib_password="b")
    params = parse(Plugin.download_zlib, ["11033158", "012345"])
    out = run(collect(inst.download_zlib(event, **params)))
    assert result_files(out), flat(out)


@pytest.mark.fixed
def test_ebooks_download_digits_only_is_friendly(fake_zlib, event):
    inst = make_plugin(enable_zlib=True, zlib_email="a", zlib_password="b")
    params = parse(Plugin.download_all_platforms, ["11033158"])
    texts = flat(run(collect(inst.download_all_platforms(event, **params))))
    assert "Hash" in texts[0] and "错误" not in texts[0]


# ---------------------------------------------------------------- docs stay in sync with code
def _plugin_dir():
    import importlib
    return os.path.dirname(importlib.import_module(f"{PKG}.main").__file__)


@pytest.mark.fixed
def test_schema_covers_every_config_key_and_readme_documents_them():
    import glob
    import json
    import re
    d = _plugin_dir()
    schema = json.load(open(os.path.join(d, "_conf_schema.json"), encoding="utf-8-sig"))
    used = set()
    for f in glob.glob(os.path.join(d, "*.py")):
        used |= set(re.findall(r'config(?:\.get)?[(\[]"([a-z_0-9]+)"', open(f, encoding="utf-8").read()))
    used |= set(Plugin._PLATFORM_WEIGHT_KEYS.values())
    assert used - set(schema) == set(), "代码里读了但配置说明里没有"
    assert set(schema) - used == set(), "配置说明里有但代码没用"
    readme = open(os.path.join(d, "README.md"), encoding="utf-8").read()
    for key, item in schema.items():
        assert item.get("description") and item.get("hint"), key
        assert "<" not in item["hint"], f"{key}: 面板按 HTML 渲染提示，尖括号会被吞掉"
        short = key.replace("platform_weight_", "_") if key.startswith("platform_weight_") else key
        assert short in readme, f"README 没写 {key}"


@pytest.mark.fixed
def test_help_lists_enabled_platforms_in_plain_text(event):
    inst = make_plugin(enable_calibre=True, calibre_web_url="http://x", enable_zlib=True, zlib_email="a", zlib_password="b")
    text = "".join(flat(run(collect(inst.show_help(event)))))
    assert "当前已启用：Calibre-Web、Z-Library" in text
    assert "**" not in text and "`" not in text

