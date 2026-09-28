import logging
import os
import threading
import time
from urllib.parse import urlencode

from bs4 import BeautifulSoup, NavigableString
from curl_cffi.requests import get

logger = logging.getLogger(__name__)


class HTTPFailed(Exception):
    pass

REQUEST_TIMEOUT = (5, 30)
_IMPERSONATE = "chrome120"
# 这个 UA 同时用于 curl_cffi 和无头浏览器：DDoS-Guard 的 cookie 与 UA 绑定，
# 两边不一致会导致解出来的 cookie 复用时照样 403。
_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8",
}

# Anna's Archive 的 /search 与 /md5 路径挂在 DDoS-Guard 后面，会 302 到 ?check=1
# 再返回 403 挑战页。挑战是 190KB 的浏览器指纹脚本（WebGL/canvas/navigator），
# 纯 HTTP 无法通过。这里用无头 chromium 解一次，把 __ddg* cookie 缓存下来交给
# curl_cffi 复用；cookie 失效（再次 403）时自动重解。
_CHALLENGE_TTL = 30 * 60
_CHALLENGE_TIMEOUT_MS = 60000
_challenge_lock = threading.Lock()
_challenge_cookies: dict = {}
_challenge_stamp: float = 0.0


def _pick_proxy():
    for key in ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY"):
        v = os.environ.get(key)
        if v:
            return v
    return None


def _solve_challenge(url: str, params: dict) -> dict:
    """用无头浏览器过 DDoS-Guard，返回可给 curl_cffi 复用的 cookie。"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.warning("[annas] 未安装 playwright，无法通过 DDoS-Guard 挑战。")
        return {}

    target = f"{url}?{urlencode(params)}" if params else url
    proxy = _pick_proxy()
    args = ["--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"]

    logger.info("[annas] 正在通过无头浏览器解 DDoS-Guard 挑战……")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True, args=args,
                **({"proxy": {"server": proxy}} if proxy else {}),
            )
            try:
                ctx = browser.new_context(
                    user_agent=_USER_AGENT, locale="en-US",
                    viewport={"width": 1280, "height": 800},
                )
                # 挑战脚本会检查 navigator.webdriver
                ctx.add_init_script(
                    "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
                )
                page = ctx.new_page()
                page.goto(target, wait_until="domcontentloaded", timeout=_CHALLENGE_TIMEOUT_MS)

                # __ddg2_ 出现得比挑战真正完成早，必须等挑战页跳走、真实内容
                # 渲染出来后取的 cookie 才是通行的。跳转期间读 content() 会抛
                # 异常，忽略重试即可。
                deadline = time.monotonic() + _CHALLENGE_TIMEOUT_MS / 1000
                cookies = {}
                solved = False
                while time.monotonic() < deadline:
                    try:
                        body = page.content()
                    except Exception:
                        body = ""
                    cookies = {c["name"]: c["value"] for c in ctx.cookies()}
                    if "__ddg2_" in cookies and body and "js-challenge" not in body:
                        solved = True
                        break
                    page.wait_for_timeout(1000)

                if not solved:
                    logger.warning("[annas] 挑战超时，未取得可用 cookie。")
                    return {}
                page.wait_for_timeout(500)
                cookies = {c["name"]: c["value"] for c in ctx.cookies()}
                logger.info("[annas] DDoS-Guard 挑战已通过。")
                return cookies
            finally:
                browser.close()
    except Exception as e:
        logger.warning(f"[annas] 解 DDoS-Guard 挑战失败：{type(e).__name__}: {e}")
        return {}


def _cached_cookies() -> dict:
    if _challenge_cookies and time.monotonic() - _challenge_stamp < _CHALLENGE_TTL:
        return _challenge_cookies
    return {}


def _refresh_cookies(url: str, params: dict, stale: dict) -> dict:
    """重解挑战。并发调用时只有第一个真的去解，其余复用结果。"""
    global _challenge_cookies, _challenge_stamp
    with _challenge_lock:
        # 等锁期间可能已被别的线程解好了
        current = _cached_cookies()
        if current and current is not stale and current != stale:
            return current
        cookies = _solve_challenge(url, params)
        if cookies:
            _challenge_cookies = cookies
            _challenge_stamp = time.monotonic()
        return cookies


def _fetch(url: str, params: dict, cookies: dict):
    kwargs = {
        "params": params,
        "timeout": REQUEST_TIMEOUT,
        "impersonate": _IMPERSONATE,
        "headers": _HEADERS,
    }
    if cookies:
        kwargs["cookies"] = cookies
    proxy = _pick_proxy()
    if proxy:
        kwargs["proxies"] = {"http": proxy, "https": proxy}
    return get(url, **kwargs)


def html_parser(url: str, params: dict = {}) -> NavigableString:
    params = dict(filter(lambda i: i[1], params.items()))
    cookies = _cached_cookies()
    response = _fetch(url, params, cookies)

    # 403 = 撞上 DDoS-Guard 挑战（或缓存的 cookie 已过期），解一次再重试。
    if response.status_code == 403:
        fresh = _refresh_cookies(url, params, cookies)
        if fresh:
            response = _fetch(url, params, fresh)

    if response.status_code >= 400:
        raise HTTPFailed(f"server returned http status {response.status_code}")
    # Uncomment code that would be dynamically rendered by JavaScript
    html = response.text.replace("<!--", "").replace("-->", "")
    soup = BeautifulSoup(html, "lxml")
    return soup
