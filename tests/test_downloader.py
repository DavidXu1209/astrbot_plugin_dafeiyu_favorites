import sys
import unittest
from pathlib import Path
from urllib.request import Request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from downloader import ALLOWED_HOSTS, DownloadError, _ValidatingRedirect, _validate_url


class UrlPolicyTests(unittest.TestCase):
    def test_allows_qq_cdn_hosts(self):
        for host in ALLOWED_HOSTS:
            self.assertEqual(
                _validate_url(f"https://{host}/club/item/a.gif"),
                f"https://{host}/club/item/a.gif",
            )
        _validate_url("https://gchat.qpic.cn:443/a")

    def test_rejects_other_destinations(self):
        rejected = [
            "http://gchat.qpic.cn/a",
            "https://evil.example/a",
            "https://gchat.qpic.cn.evil.com/a",
            "https://user:pass@gchat.qpic.cn/a",
            "https://gchat.qpic.cn/a#frag",
            "https://gchat.qpic.cn:8443/a",
            "https://gchat.qpic.cn/a b",
            "https://gchat.qpic.cn/a\\b",
            "",
            "not a url",
        ]
        for url in rejected:
            with self.assertRaises(DownloadError):
                _validate_url(url)

    def test_redirect_must_stay_on_allowlist(self):
        handler = _ValidatingRedirect()
        request = Request("https://gchat.qpic.cn/a")
        with self.assertRaises(DownloadError) as caught:
            handler.redirect_request(request, None, 302, "Found", {}, "https://evil.example/steal")
        self.assertIn("redirect", str(caught.exception))
        with self.assertRaises(DownloadError):
            handler.redirect_request(request, None, 302, "Found", {}, "/relative.gif")
        allowed = handler.redirect_request(
            request, None, 302, "Found", {}, "https://multimedia.nt.qq.com.cn/a.jpg")
        self.assertIn("multimedia.nt.qq.com.cn", allowed.full_url)


if __name__ == "__main__":
    unittest.main()
