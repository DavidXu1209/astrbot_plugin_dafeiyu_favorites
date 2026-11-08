"""Bounded downloads for explicit NapCat stickers; no model or account API calls."""

from __future__ import annotations

import asyncio
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


# NapCatQQ v4.18.38 types/constant.ts and api/msg.ts; keep this list exact.
ALLOWED_HOSTS = frozenset({"gchat.qpic.cn", "multimedia.nt.qq.com.cn", "gxh.vip.qq.com"})
MAX_REDIRECTS = 3
# QQ CDNs answer plain bots with 403 or an HTML error page; a normal UA plus the
# referer the QQ client sends keeps these sticker URLs readable.
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AstrBotFavorites/0.1.7",
    "Accept": "image/*",
    "Referer": "https://im.qq.com/",
    "Accept-Encoding": "identity",
}


class DownloadError(ValueError):
    """Carries a short reason code only: no signed URL, token, or response body."""


def _validate_url(url: str) -> str:
    if not isinstance(url, str) or not url or len(url) > 8192:
        raise DownloadError("invalid url")
    if any(ord(char) <= 32 or ord(char) == 127 for char in url) or "\\" in url:
        raise DownloadError("invalid url")
    try:
        parsed = urlsplit(url)
        valid = (
            parsed.scheme == "https"
            and parsed.hostname in ALLOWED_HOSTS
            and parsed.port in (None, 443)
            and parsed.username is None
            and parsed.password is None
            and not parsed.fragment
            and parsed.path.startswith("/")
        )
    except ValueError:
        valid = False
    if not valid:
        raise DownloadError("url host not allowed")
    return url


class _ValidatingRedirect(HTTPRedirectHandler):
    """Follows a bounded redirect chain, but every hop must stay on a QQ CDN host."""

    max_redirections = MAX_REDIRECTS

    def redirect_request(self, request, fp, code, msg, headers, newurl):
        try:
            _validate_url(newurl)
        except DownloadError:
            raise DownloadError("redirect host not allowed") from None
        return super().redirect_request(request, fp, code, msg, headers, newurl)


def _download_image(url: str, max_bytes: int) -> bytes:
    _validate_url(url)
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise DownloadError("invalid size limit")
    # No environment proxy, credentials, cookies or application identity beyond a plain UA.
    opener = build_opener(ProxyHandler({}), _ValidatingRedirect())
    request = Request(url, headers=HEADERS)
    deadline = time.monotonic() + 10
    try:
        with opener.open(request, timeout=10) as response:
            if response.status != 200:
                raise DownloadError("http status")
            length = response.headers.get("Content-Length")
            if length is not None:
                try:
                    declared = int(length)
                except (TypeError, ValueError):
                    raise DownloadError("invalid length header") from None
                if declared < 0 or declared > max_bytes:
                    raise DownloadError("too large")
            result = bytearray()
            read = getattr(response, "read1", response.read)
            while True:
                if time.monotonic() >= deadline:
                    raise DownloadError("timeout")
                chunk = read(min(65536, max_bytes + 1 - len(result)))
                if not chunk:
                    break
                result.extend(chunk)
                if len(result) > max_bytes:
                    raise DownloadError("too large")
            if not result:
                raise DownloadError("empty response")
            return bytes(result)
    except DownloadError:
        raise
    except HTTPError as exc:
        raise DownloadError(f"http {exc.code}") from None
    except URLError as exc:
        reason = exc.reason
        raise DownloadError("tls error" if isinstance(reason, OSError) and "SSL" in str(reason) else "network error") from None
    except (OSError, ValueError):
        raise DownloadError("download error") from None


async def download_image(url: str, max_bytes: int) -> bytes:
    """Network work stays off AstrBot's event loop and is bounded to one image."""
    return await asyncio.to_thread(_download_image, url, max_bytes)
