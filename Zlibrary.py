"""
Copyright (c) 2023-2024 Bipinkrish
This file is part of the Zlibrary-API by Bipinkrish
Zlibrary-API / Zlibrary.py

For more information, see:
https://github.com/bipinkrish/Zlibrary-API/
"""
import html
import os
import re
import time

from curl_cffi import CurlOpt, requests
from curl_cffi.const import CurlHttpVersion

_IMPERSONATE = "chrome120"

# Streaming download tuning — handles network jitter on large book files.
# Per-attempt timeout is (connect, read); read is per-chunk so the overall
# transfer can take as long as needed as long as bytes keep arriving.
_DL_CONNECT_TIMEOUT = 30
_DL_READ_TIMEOUT = 120
_DL_CHUNK_SIZE = 64 * 1024
_DL_MAX_ATTEMPTS = 5
# CDN 不支持续传时，每次重试都要整本重下，所以单独卡一个更小的上限。
_DL_MAX_RESTARTS = 2
_DL_BACKOFF_BASE = 2
_DL_BACKOFF_CAP = 30


class ZlibraryError(Exception):
    """Z-Library 返回了错误或非预期的响应体，消息可直接展示给用户。"""


def _strip_html(text) -> str:
    """Z-Library 的提示文案带 HTML 标签和实体，转成纯文本再展示。"""
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", str(text or "")))).strip()


def _pick_proxy():
    for key in ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY"):
        v = os.environ.get(key)
        if v:
            return v
    return None


class Zlibrary:
    def __init__(
        self,
        email: str = None,
        password: str = None,
        remix_userid: [int, str] = None,
        remix_userkey: str = None,
        domain: str = "z-library.ec",
    ):
        self.__timeout = (5, 30)
        self.__email: str
        self.__name: str
        self.__kindle_email: str
        self.__remix_userid: [int, str]
        self.__remix_userkey: str
        self.__domain = domain

        self.__loggedin = False
        self.__headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
            "accept-language": "en-US,en;q=0.9",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36",
        }
        self.__cookies = {
            "siteLanguageV2": "en",
        }
        self.__proxies = {"http": proxy, "https": proxy} if (proxy := _pick_proxy()) else None

        if email is not None and password is not None:
            self.login(email, password)
        elif remix_userid is not None and remix_userkey is not None:
            self.loginWithToken(remix_userid, remix_userkey)

    def __setValues(self, response) -> dict[str, str]:
        if not response["success"]:
            return response
        self.__email = response["user"]["email"]
        self.__name = response["user"]["name"]
        self.__kindle_email = response["user"]["kindle_email"]
        self.__remix_userid = str(response["user"]["id"])
        self.__remix_userkey = response["user"]["remix_userkey"]
        self.__cookies["remix_userid"] = self.__remix_userid
        self.__cookies["remix_userkey"] = self.__remix_userkey
        self.__loggedin = True
        return response

    def __login(self, email, password) -> dict[str, str]:
        return self.__setValues(
            self.__makePostRequest(
                "/eapi/user/login",
                data={
                    "email": email,
                    "password": password,
                },
                override=True,
            )
        )

    def __checkIDandKey(self, remix_userid, remix_userkey) -> dict[str, str]:
        return self.__setValues(
            self.__makeGetRequest(
                "/eapi/user/profile",
                cookies={
                    "siteLanguageV2": "en",
                    "remix_userid": str(remix_userid),
                    "remix_userkey": remix_userkey,
                },
            )
        )

    def login(self, email: str, password: str) -> dict[str, str]:
        return self.__login(email, password)

    def loginWithToken(
        self, remix_userid: [int, str], remix_userkey: str
    ) -> dict[str, str]:
        return self.__checkIDandKey(remix_userid, remix_userkey)

    def __makePostRequest(
        self, url: str, data: dict = {}, override=False
    ) -> dict[str, str]:
        if not self.isLoggedIn() and override is False:
            print("Not logged in")
            return

        payload = {
            "data": data,
            "cookies": self.__cookies,
            "headers": self.__headers,
            "timeout": self.__timeout,
        }
        if self.__proxies:
            payload["proxies"] = self.__proxies
        payload["impersonate"] = _IMPERSONATE

        return self.__asJson(requests.post("https://" + self.__domain + url, **payload), url)

    def __makeGetRequest(
        self, url: str, params: dict = {}, cookies=None
    ) -> dict[str, str]:
        if not self.isLoggedIn() and cookies is None:
            print("Not logged in")
            return

        payload = {
            "params": params,
            "cookies": self.__cookies if cookies is None else cookies,
            "headers": self.__headers,
            "timeout": self.__timeout,
        }
        if self.__proxies:
            payload["proxies"] = self.__proxies
        payload["impersonate"] = _IMPERSONATE

        return self.__asJson(requests.get("https://" + self.__domain + url, **payload), url)

    def __asJson(self, response, url: str) -> dict[str, str]:
        """把非 JSON 响应翻译成看得懂的错误。

        Z-Library 抽风时会对 API 路径返回 HTML 错误页（HTTP 513 之类），
        直接 .json() 只会抛一句 'Expecting value: line 1 column 1 (char 0)'，
        日志里完全看不出是上游挂了还是账号有问题。
        """
        try:
            return response.json()
        except Exception as e:
            ctype = response.headers.get("Content-Type", "?")
            snippet = " ".join(response.text[:120].split())
            raise ZlibraryError(
                f"{url} 返回了非 JSON 响应（HTTP {response.status_code}, {ctype}）："
                f"{snippet or '空响应'}"
            ) from e

    def getProfile(self) -> dict[str, str]:
        return self.__makeGetRequest("/eapi/user/profile")

    def getMostPopular(self, switch_language: str = None) -> dict[str, str]:
        if switch_language is not None:
            return self.__makeGetRequest(
                "/eapi/book/most-popular", {"switch-language": switch_language}
            )
        return self.__makeGetRequest("/eapi/book/most-popular")

    def getRecently(self) -> dict[str, str]:
        return self.__makeGetRequest("/eapi/book/recently")

    def getUserRecommended(self) -> dict[str, str]:
        return self.__makeGetRequest("/eapi/user/book/recommended")

    def deleteUserBook(self, bookid: [int, str]) -> dict[str, str]:
        return self.__makeGetRequest(f"/eapi/user/book/{bookid}/delete")

    def unsaveUserBook(self, bookid: [int, str]) -> dict[str, str]:
        return self.__makeGetRequest(f"/eapi/user/book/{bookid}/unsave")

    def getBookForamt(self, bookid: [int, str], hashid: str) -> dict[str, str]:
        return self.__makeGetRequest(f"/eapi/book/{bookid}/{hashid}/formats")

    def getDonations(self) -> dict[str, str]:
        return self.__makeGetRequest("/eapi/user/donations")

    def getUserDownloaded(
        self, order: str = None, page: int = None, limit: int = None
    ) -> dict[str, str]:
        """
        order takes one of the values\n
        ["year",...]
        """
        params = {
            k: v
            for k, v in {"order": order, "page": page, "limit": limit}.items()
            if v is not None
        }
        return self.__makeGetRequest("/eapi/user/book/downloaded", params)

    def getExtensions(self) -> dict[str, str]:
        return self.__makeGetRequest("/eapi/info/extensions")

    def getDomains(self) -> dict[str, str]:
        return self.__makeGetRequest("/eapi/info/domains")

    def getLanguages(self) -> dict[str, str]:
        return self.__makeGetRequest("/eapi/info/languages")

    def getPlans(self, switch_language: str = None) -> dict[str, str]:
        if switch_language is not None:
            return self.__makeGetRequest(
                "/eapi/info/plans", {"switch-language": switch_language}
            )
        return self.__makeGetRequest("/eapi/info/plans")

    def getUserSaved(
        self, order: str = None, page: int = None, limit: int = None
    ) -> dict[str, str]:
        """
        order takes one of the values\n
        ["year",...]
        """
        params = {
            k: v
            for k, v in {"order": order, "page": page, "limit": limit}.items()
            if v is not None
        }
        return self.__makeGetRequest("/eapi/user/book/saved", params)

    def getInfo(self, switch_language: str = None) -> dict[str, str]:
        if switch_language is not None:
            return self.__makeGetRequest(
                "/eapi/info", {"switch-language": switch_language}
            )
        return self.__makeGetRequest("/eapi/info")

    def hideBanner(self) -> dict[str, str]:
        return self.__makeGetRequest("/eapi/user/hide-banner")

    def recoverPassword(self, email: str) -> dict[str, str]:
        return self.__makePostRequest(
            "/eapi/user/password-recovery", {"email": email}, override=True
        )

    def makeRegistration(self, email: str, password: str, name: str) -> dict[str, str]:
        return self.__makePostRequest(
            "/eapi/user/registration",
            {"email": email, "password": password, "name": name},
            override=True,
        )

    def resendConfirmation(self) -> dict[str, str]:
        return self.__makePostRequest("/eapi/user/email/confirmation/resend")

    def saveBook(self, bookid: [int, str]) -> dict[str, str]:
        return self.__makeGetRequest(f"/eapi/user/book/{bookid}/save")

    def sendTo(self, bookid: [int, str], hashid: str, totype: str) -> dict[str, str]:
        return self.__makeGetRequest(f"/eapi/book/{bookid}/{hashid}/send-to-{totype}")

    def getBookInfo(
        self, bookid: [int, str], hashid: str, switch_language: str = None
    ) -> dict[str, str]:
        if switch_language is not None:
            return self.__makeGetRequest(
                f"/eapi/book/{bookid}/{hashid}", {"switch-language": switch_language}
            )
        return self.__makeGetRequest(f"/eapi/book/{bookid}/{hashid}")

    def getSimilar(self, bookid: [int, str], hashid: str) -> dict[str, str]:
        return self.__makeGetRequest(f"/eapi/book/{bookid}/{hashid}/similar")

    def makeTokenSigin(self, name: str, id_token: str) -> dict[str, str]:
        return self.__makePostRequest(
            "/eapi/user/token-sign-in",
            {"name": name, "id_token": id_token},
            override=True,
        )

    def updateInfo(
        self,
        email: str = None,
        password: str = None,
        name: str = None,
        kindle_email: str = None,
    ) -> dict[str, str]:
        return self.__makePostRequest(
            "/eapi/user/update",
            {
                k: v
                for k, v in {
                    "email": email,
                    "password": password,
                    "name": name,
                    "kindle_email": kindle_email,
                }.items()
                if v is not None
            },
        )

    def search(
        self,
        message: str = None,
        yearFrom: int = None,
        yearTo: int = None,
        languages: str = None,
        extensions: [str] = None,
        order: str = None,
        page: int = None,
        limit: int = None,
    ) -> dict[str, str]:
        return self.__makePostRequest(
            "/eapi/book/search",
            {
                k: v
                for k, v in {
                    "message": message,
                    "yearFrom": yearFrom,
                    "yearTo": yearTo,
                    "languages": languages,
                    "extensions[]": extensions,
                    "order": order,
                    "page": page,
                    "limit": limit,
                }.items()
                if v is not None
            },
        )

    def __getImageData(self, url: str):
        kwargs = {"headers": self.__headers, "timeout": self.__timeout, "impersonate": _IMPERSONATE}
        if self.__proxies:
            kwargs["proxies"] = self.__proxies
        res = requests.get(url, **kwargs)
        if res.status_code == 200:
            return res.content

    def getImage(self, book: dict[str, str]):
        return self.__getImageData(book["cover"])

    @staticmethod
    def __explainDisallow(raw_message) -> str:
        """把 disallowDownloadMessage 里的推销文案剥掉，只留额度与重置倒计时。"""
        text = _strip_html(raw_message)
        if not text:
            return "该书当前不可下载（可能已达每日下载上限）。"

        limit = re.search(r"daily limit\s+(.+?)\s+is already reached", text)
        wait = re.search(r"You can wait\s+(.+?)\s+for the download counter", text)
        if limit or wait:
            msg = f"今日下载额度已用尽（{limit.group(1)}）。" if limit else "今日下载额度已用尽。"
            return msg + (f" 约 {wait.group(1)} 后重置。" if wait else "")

        # 文案变了就原样透出，至少不丢信息。
        return re.split(r"\s+or increase your limit", text)[0].rstrip(" :：") or text

    def __getBookFile(self, bookid: [int, str], hashid: str, fallback_name: str = None, *, target) -> (str, str, int):
        """Stream the book to the path returned by target(filename); returns (filename, path, size).

        Written chunk by chunk instead of buffered: a 180 MB book used to peak at ~360 MB
        of RAM (bytearray plus its bytes() copy).
        """
        response = self.__makeGetRequest(f"/eapi/book/{bookid}/{hashid}/file")
        if not isinstance(response, dict):
            raise ZlibraryError("未登录，或 Z-Library 返回了非 JSON 响应。")

        if not response.get("success", 1):
            raise ZlibraryError(
                str(response.get("error") or response.get("message") or "Z-Library 拒绝了本次下载请求。")
            )

        file_info = response.get("file")
        if not isinstance(file_info, dict):
            raise ZlibraryError(f"Z-Library 响应中缺少 file 字段（顶层字段: {sorted(response)}）。")

        # 额度用尽时 success 仍为 1，但 file 里只有 allowDownload/disallowDownloadMessage
        # 两个键，硬取 description 就会 KeyError。这里优先透出官方提示（含重置倒计时）。
        if file_info.get("allowDownload") is False:
            raise ZlibraryError(self.__explainDisallow(file_info.get("disallowDownloadMessage")))

        ddl = file_info.get("downloadLink")
        if not ddl:
            raise ZlibraryError(
                f"Z-Library 未返回下载链接，可能已达每日下载上限（file 字段: {sorted(file_info)}）。"
            )

        # /file 里这个字段既可能缺失也可能为空串（简介为空的书就是如此），
        # 逐级回退到调用方给的书名，最后才用 id 兜底。
        title = (
            file_info.get("description")
            or file_info.get("title")
            or file_info.get("name")
            or fallback_name
            or f"book_{bookid}"
        )
        filename = str(title).strip() or f"book_{bookid}"

        # description 里往往已经带了作者，别再拼一遍。
        author = str(file_info.get("author") or "").strip()
        if author and author not in filename:
            filename += f" ({author})"

        extension = str(file_info.get("extension") or "").strip().lstrip(".")
        if extension:
            filename += f".{extension}"

        base_headers = self.__headers.copy()
        base_headers["authority"] = ddl.split("/")[2]

        path = target(filename)
        done = 0
        total_size = None
        last_err = None
        supports_range = True
        restarts = 0
        tries = 0

        for attempt in range(1, _DL_MAX_ATTEMPTS + 1):
            request_headers = base_headers.copy()
            if done and supports_range:
                request_headers["Range"] = f"bytes={done}-"
            elif done:
                # 不支持续传，只能丢掉已下载的部分从头再来。整本书重下一次
                # 要几分钟，所以这种重来的次数要卡死，不能白等十几分钟。
                restarts += 1
                if restarts > _DL_MAX_RESTARTS:
                    break
                done = 0
                total_size = None
                open(path, "wb").close()

            dl_kwargs = {
                "headers": request_headers,
                "timeout": (_DL_CONNECT_TIMEOUT, _DL_READ_TIMEOUT),
                "impersonate": _IMPERSONATE,
                "stream": True,
                # CDN 的 h2 流在并发或链路干扰下会被 RST（INTERNAL_ERROR），
                # 强制 HTTP/1.1 绕开整类 h2 中途断流问题。
                "curl_options": {CurlOpt.HTTP_VERSION: CurlHttpVersion.V1_1},
            }
            if self.__proxies:
                dl_kwargs["proxies"] = self.__proxies

            res = None
            try:
                tries += 1
                res = requests.get(ddl, **dl_kwargs)

                # Z-Library 的下载 CDN（dln*.ncdn.ec）直接拒绝 Range，返回
                # 416 "Range requests are disabled."。一旦续传被拒就永久关掉，
                # 否则后续每次重试都撞同一堵墙，永远恢复不了。
                if res.status_code == 416:
                    supports_range = False
                    last_err = IOError("CDN 不支持断点续传 (HTTP 416)")
                    continue

                # 服务端不声明 accept-ranges 就别去试，省得白费一轮。
                if res.headers.get("Accept-Ranges", "").strip().lower() in ("", "none"):
                    supports_range = False

                # If we asked for a range but server replied 200, it ignored
                # Range and is sending the whole file — discard partial buffer.
                if done and res.status_code == 200:
                    done = 0
                    total_size = None

                if res.status_code not in (200, 206):
                    last_err = IOError(f"HTTP {res.status_code} from download link")
                    if attempt < _DL_MAX_ATTEMPTS:
                        time.sleep(min(_DL_BACKOFF_BASE ** attempt, _DL_BACKOFF_CAP))
                        continue
                    break

                if total_size is None:
                    cl = res.headers.get("Content-Length")
                    if cl:
                        try:
                            total_size = int(cl) + done
                        except ValueError:
                            pass

                # "wb" when starting over truncates whatever an earlier attempt left behind.
                with open(path, "ab" if done else "wb") as f:
                    for chunk in res.iter_content(chunk_size=_DL_CHUNK_SIZE):
                        if chunk:
                            f.write(chunk)
                            done += len(chunk)

                if total_size is not None and done < total_size:
                    raise IOError(f"short read: {done}/{total_size} bytes")

                return filename, path, done
            except Exception as e:
                last_err = e
                # Resume from what actually reached the disk, not from the loop's counter.
                done = os.path.getsize(path) if os.path.exists(path) else 0
                if attempt >= _DL_MAX_ATTEMPTS:
                    break
                time.sleep(min(_DL_BACKOFF_BASE ** attempt, _DL_BACKOFF_CAP))
            finally:
                if res is not None:
                    try:
                        res.close()
                    except Exception:
                        pass

        # 以前这里会静默 return None，调用方只能报一句「发生错误」，日志里
        # 连原因都没有。现在一律带着真实原因抛出去。
        if last_err is not None:
            raise ZlibraryError(f"文件下载失败（尝试了 {tries} 次）：{last_err}") from last_err
        raise ZlibraryError("文件下载失败：未收到任何数据。")

    def downloadBook(self, book: dict[str, str], fallback_name: str = None, *, target) -> (str, str, int):
        """target(filename) -> destination path; returns (filename, path, size)."""
        return self.__getBookFile(book["id"], book["hash"], fallback_name, target=target)

    def isLoggedIn(self) -> bool:
        return self.__loggedin

    def sendCode(self, email: str, password: str, name: str) -> dict[str, str]:
        usr_data = {
            "email": email,
            "password": password,
            "name": name,
            "rx": 215,
            "action": "registration",
            "site_mode": "books",
            "isSinglelogin": 1,
        }
        response = self.__makePostRequest(
            "/papi/user/verification/send-code", data=usr_data, override=True
        )
        if response["success"]:
            response["msg"] = (
                "Verification code is sent to mail, use verify_code to complete registration"
            )
        return response

    def verifyCode(
        self, email: str, password: str, name: str, code: str
    ) -> dict[str, str]:
        usr_data = {
            "email": email,
            "password": password,
            "name": name,
            "verifyCode": code,
            "rx": 215,
            "action": "registration",
            "redirectUrl": "",
            "isModa": True,
            "gg_json_mode": 1,
        }
        return self.__makePostRequest("/rpc.php", data=usr_data, override=True)

    def getDownloadsLeft(self) -> int:
        user_profile: dict = self.getProfile()["user"]
        return user_profile.get("downloads_limit", 10) - user_profile.get(
            "downloads_today", 0
        )
