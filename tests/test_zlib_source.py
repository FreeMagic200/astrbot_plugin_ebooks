"""Standalone Z-Library search test: runs without AstrBot (the astrbot API is stubbed).

    python -m unittest discover -s tests      # or: python -m pytest tests

Needs the plugin's own requirements (aiohttp, beautifulsoup4, Pillow). The Z-Library
client is stubbed, so no network access and no curl_cffi are needed. The fuller
regression suite under tests/astrbot_env/ runs inside an AstrBot container.
"""
import importlib
import sys
import tempfile
import types
import unittest
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
# Any package name works: the plugin's modules import each other relatively.
PKG = "ebooks_under_test"
STUBBED_MODULES = (
    "astrbot",
    "astrbot.api",
    "astrbot.api.all",
    PKG,
    f"{PKG}.Zlibrary",
    f"{PKG}.utils",
    f"{PKG}.zlib_source",
)
_MISSING = object()
ORIGINAL_MODULES = {name: sys.modules.get(name, _MISSING) for name in STUBBED_MODULES}


class Plain:
    def __init__(self, text):
        self.text = text


class Image:
    @classmethod
    def fromBase64(cls, data):
        return cls()


class Node:
    def __init__(self, uin=None, name=None, content=None):
        self.uin = uin
        self.name = name
        self.content = content or []


class Nodes:
    def __init__(self, nodes=None):
        self.nodes = nodes or []


class File:
    def __init__(self, name=None, file=None):
        self.name = name
        self.file = file


class MessageChain:
    def message(self, text):
        self.text = text
        return self


class Logger:
    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass

    def debug(self, *args, **kwargs):
        pass


astrbot_all = types.ModuleType("astrbot.api.all")
astrbot_all.Plain = Plain
astrbot_all.Image = Image
astrbot_all.Node = Node
astrbot_all.Nodes = Nodes
astrbot_all.File = File
astrbot_all.MessageChain = MessageChain
astrbot_all.logger = Logger()
sys.modules.setdefault("astrbot", types.ModuleType("astrbot"))
sys.modules.setdefault("astrbot.api", types.ModuleType("astrbot.api"))
sys.modules["astrbot.api.all"] = astrbot_all
plugin_package = types.ModuleType(PKG)
plugin_package.__path__ = [str(PLUGIN_ROOT)]
sys.modules[PKG] = plugin_package


class FakeZlibrary:
    def __init__(self, domain=None, **kwargs):
        self.logged_in = False
        self.search_called = False

    def isLoggedIn(self):
        return self.logged_in

    def login(self, email, password):
        self.logged_in = bool(email and password)
        return {"success": int(self.logged_in)}

    def search(self, message=None, limit=None, **kwargs):
        self.search_called = True
        return {
            "success": 1,
            "books": [
                {
                    "title": "Million Pound Note",
                    "author": "Mark Twain",
                    "year": "1893",
                    "publisher": None,
                    "language": "English",
                    "description": "A short story.",
                    "id": "12345",
                    "hash": "abcdef",
                }
            ],
        }


zlibrary_module = types.ModuleType(f"{PKG}.Zlibrary")
zlibrary_module.Zlibrary = FakeZlibrary
zlibrary_module.ZlibraryError = type("ZlibraryError", (Exception,), {})
sys.modules[f"{PKG}.Zlibrary"] = zlibrary_module

# The real helpers, with the homepage probe replaced by a tripwire.
utils_module = importlib.import_module(f"{PKG}.utils")


async def fail_url_accessible(*args, **kwargs):
    utils_module.url_accessible_called = True
    raise AssertionError("is_url_accessible() should not be called during Z-Library search")


utils_module.url_accessible_called = False
utils_module.is_url_accessible = fail_url_accessible

zlib_source = importlib.import_module(f"{PKG}.zlib_source")
ZlibSource = zlib_source.ZlibSource


class Config(dict):
    def save_config(self):
        pass


class Event:
    def get_self_id(self):
        return "10000"


class ZlibSourceTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def tearDownClass(cls):
        for name, module in ORIGINAL_MODULES.items():
            if module is _MISSING:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module

    async def test_search_uses_zlibrary_api_without_homepage_probe(self):
        utils_module.url_accessible_called = False
        source = ZlibSource(
            Config(
                {
                    "enable_zlib": True,
                    "zlib_email": "user@example.com",
                    "zlib_password": "password",
                }
            ),
            proxy=None,
            max_results=20,
            temp_path=tempfile.gettempdir(),
        )

        result = await source.search_nodes(Event(), "百万英镑", 20)

        self.assertIsInstance(result, list)
        self.assertEqual(1, len(result))
        self.assertTrue(source.zlibrary.search_called)
        self.assertFalse(utils_module.url_accessible_called)


if __name__ == "__main__":
    unittest.main()
