"""Shared harness for the ebooks plugin regression suite.

Runs inside the astrbot container. EBOOKS_ROOT points at a directory that holds
data/__init__.py + data/plugins/__init__.py + data/plugins/astrbot_plugin_ebooks/,
so `data` resolves to that copy (a regular package) instead of the live
/AstrBot/data namespace package. That lets the same suite run against the
pre-review baseline and the patched tree side by side.

Markers:
  fixed — pins a fix/behaviour added since the 2026-09-23 review; expected to FAIL on the baseline.
Everything unmarked is a regression guard and must pass on both trees.
"""
import asyncio
import os
import sys
from types import SimpleNamespace

import pytest

ROOT = os.environ.get("EBOOKS_ROOT")
if not ROOT:
    # These tests import the plugin inside a running AstrBot container.
    pytest.skip("needs the AstrBot runtime: run tests/astrbot_env/run.sh", allow_module_level=True)
sys.path.insert(0, "/AstrBot")
sys.path.insert(0, ROOT)

from astrbot.api.all import File, Node, Nodes, Plain  # noqa: E402
from astrbot.core.message.message_event_result import MessageEventResult  # noqa: E402

PKG = "data.plugins.astrbot_plugin_ebooks"


def pytest_configure(config):
    config.addinivalue_line("markers", "fixed: pins a fix/behaviour added since the 2026-09-23 review")


def run(coro):
    return asyncio.run(coro)


class FakeConfig(dict):
    saved = 0

    def save_config(self):
        self.saved += 1


def make_config(**over):
    cfg = FakeConfig(
        enable_merge_forward=True,
        enable_cover_image=False,
        cover_max_size=0,
        enable_content_filter=True,
        extra_filter_keywords=[],
        enable_calibre=False,
        enable_liber3=False,
        enable_archive=False,
        enable_zlib=False,
        enable_annas=False,
        calibre_web_url="http://127.0.0.1:9",
        annas_base_url="https://annas.invalid",
        annas_secret_key="",
        annas_language="",
        zlib_base_url="https://zlib.invalid",
        zlib_email="",
        zlib_password="",
        max_results=10,
        min_year=0,
        enable_rerank=False,
        rerank_provider_id="",
        rerank_candidates=60,
        rerank_doc_max_chars=300,
        # Raw query/docs so scorers can inspect document text; the wrapper has its own tests.
        rerank_instruction="",
        per_platform_results=20,
    )
    cfg.update(over)
    return cfg


class FakeEvent:
    def __init__(self):
        self.sent = []

    def get_self_id(self):
        return "10000"

    def plain_result(self, text):
        return MessageEventResult().message(text)

    def chain_result(self, chain):
        r = MessageEventResult()
        r.chain = chain
        return r

    async def send(self, chain):
        self.sent.append(chain)


class FakeRerankProvider:
    """Scores each document with `scorer(doc)`; returns results best-first like real providers."""

    def __init__(self, scorer):
        self.scorer = scorer
        self.calls = []

    async def rerank(self, query, documents, top_n=None):
        self.calls.append((query, list(documents), top_n))
        res = [SimpleNamespace(index=i, relevance_score=float(self.scorer(d))) for i, d in enumerate(documents)]
        res.sort(key=lambda r: r.relevance_score, reverse=True)
        return res[:top_n] if top_n else res


def make_context(provider=None):
    insts = [provider] if provider else []
    return SimpleNamespace(provider_manager=SimpleNamespace(rerank_provider_insts=insts, inst_map={}))


def book_node(title, cmd, name="X"):
    return Node(uin="10000", name=name, content=[Plain(f"{title}\n"), Plain(f"作者: 某人\n"), Plain(f"下载命令:\n{cmd}")])


def hint_node(text, name="X"):
    return Node(uin="10000", name=name, content=[Plain(text)])


def comp_text(comp):
    return getattr(comp, "text", "") or ""


def result_texts(result):
    """Flatten a MessageEventResult into a list of plain strings (walking forward nodes)."""
    out = []
    for comp in result.chain or []:
        if isinstance(comp, Nodes):
            for node in comp.nodes:
                out.append("".join(comp_text(c) for c in node.content))
        elif isinstance(comp, Node):
            out.append("".join(comp_text(c) for c in comp.content))
        else:
            t = comp_text(comp)
            if t:
                out.append(t)
    return out


def result_files(results):
    files = []
    for r in results:
        for comp in r.chain or []:
            if isinstance(comp, File):
                files.append(comp)
    return files


def file_path(f):
    return getattr(f, "file_", None) or getattr(f, "file", None)


async def collect(agen):
    return [r async for r in agen]


@pytest.fixture
def event():
    return FakeEvent()


class FakeZlibrary:
    """Stand-in for the vendored Zlibrary client; class-level state survives re-instantiation."""

    login_calls = 0
    pages = {}          # (order, page) -> list[book dict]
    search_calls = []
    # items: (name, bytes) = success; Exception = fails before any file is created;
    # (name, Exception) = writes a partial file, then fails.
    download_script = []
    download_calls = 0

    @classmethod
    def reset(cls):
        cls.login_calls = 0
        cls.pages = {}
        cls.search_calls = []
        cls.download_script = []
        cls.download_calls = 0

    def __init__(self, domain=None, **kw):
        self.domain = domain
        self._logged = False

    def login(self, email, password):
        FakeZlibrary.login_calls += 1
        self._logged = True
        return {"success": 1}

    def isLoggedIn(self):
        return self._logged

    def search(self, message=None, limit=None, page=None, order=None, yearFrom=None, **kw):
        FakeZlibrary.search_calls.append(dict(message=message, limit=limit, page=page, order=order))
        books = FakeZlibrary.pages.get((order, page or 1), [])
        return {"success": 1, "books": [dict(b) for b in books]}

    def getBookInfo(self, bookid, hashid=None):
        return {"success": 1, "book": {"title": "Fake Book"}}

    def downloadBook(self, book, fallback_name=None, *, target):
        FakeZlibrary.download_calls += 1
        item = FakeZlibrary.download_script.pop(0)
        if isinstance(item, Exception):
            raise item
        name, content = item
        path = target(name)
        with open(path, "wb") as f:
            f.write(b"partial" if isinstance(content, Exception) else content)
        if isinstance(content, Exception):
            raise content
        return name, path, len(content)


@pytest.fixture
def fake_zlib(monkeypatch):
    import importlib
    zs = importlib.import_module(f"{PKG}.zlib_source")
    FakeZlibrary.reset()
    monkeypatch.setattr(zs, "Zlibrary", FakeZlibrary)
    return FakeZlibrary
