import asyncio
import tempfile
import unittest
from pathlib import Path

from sentinel.image_fetch import (ImageFetchError, ImageFetcher, sniff_image_mime,
                                  validate_image_url)

PNG = b"\x89PNG\r\n\x1a\n" + b"payload"
JPEG = b"\xff\xd8\xff" + b"payload"
GIF = b"GIF89a" + b"payload"
WEBP = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBPpayload"


async def public_resolver(host, port):
    return ["93.184.216.34"]


def fetcher(request=None, resolver=public_resolver, *, max_images=4, per_image=1024,
            total=2048, timeout=1, local_roots=None):
    return ImageFetcher(None, max_images=max_images, max_bytes_per_image=per_image,
                        max_total_bytes=total, timeout_seconds=timeout,
                        request=request, resolver=resolver, local_roots=local_roots)


class ImageFetchTests(unittest.TestCase):
    def test_url_validation_rejects_ambiguous_or_unsafe_references(self):
        self.assertEqual(validate_image_url("https://example.com/a.png").hostname, "example.com")
        validate_image_url("http://example.com:80/a.png")
        for url in ("ftp://example.com/a.png", "https://user:pass@example.com/a.png",
                    "https://example.com:8080/a.png", "https://example.com/a b.png",
                    "https://example.com/" + "a" * 8300, "example.com/a.png", "", None):
            with self.subTest(url=url), self.assertRaises(ImageFetchError) as raised:
                validate_image_url(url)
            self.assertEqual(raised.exception.code, "image_url_rejected")

    def test_magic_sniffing_ignores_url_and_filename(self):
        self.assertEqual(sniff_image_mime(PNG), "image/png")
        self.assertEqual(sniff_image_mime(JPEG), "image/jpeg")
        self.assertEqual(sniff_image_mime(GIF), "image/gif")
        self.assertEqual(sniff_image_mime(WEBP), "image/webp")
        self.assertIsNone(sniff_image_mime(b"<html>"))

    def test_fetch_returns_data_urls(self):
        async def request(url):
            return PNG

        images = asyncio.run(fetcher(request).fetch(["https://example.com/a.png"]))
        self.assertEqual(len(images), 1)
        self.assertTrue(images[0].data_url.startswith("data:image/png;base64,"))
        self.assertEqual(images[0].byte_count, len(PNG))

    def test_local_temp_files_are_read_without_network(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "a.png"
            path.write_bytes(PNG)
            images = asyncio.run(fetcher(local_roots=[temp]).fetch([str(path)]))
            self.assertTrue(images[0].data_url.startswith("data:image/png;base64,"))
            with self.assertRaises(ImageFetchError) as rejected:
                asyncio.run(fetcher(local_roots=[Path(temp) / "other"]).fetch([str(path)]))
            self.assertEqual(rejected.exception.code, "image_url_rejected")
            with self.assertRaises(ImageFetchError) as missing:
                asyncio.run(fetcher(local_roots=[temp]).fetch([str(Path(temp) / "missing.png")]))
            self.assertEqual(missing.exception.code, "image_fetch_failed")

    def test_size_and_count_limits_are_fixed_codes(self):
        async def request(url):
            return PNG

        with self.assertRaises(ImageFetchError) as too_many:
            asyncio.run(fetcher(request, max_images=1).fetch(["https://example.com/1", "https://example.com/2"]))
        self.assertEqual(too_many.exception.code, "image_too_many")
        with self.assertRaises(ImageFetchError) as per_image:
            asyncio.run(fetcher(request, per_image=8).fetch(["https://example.com/1"]))
        self.assertEqual(per_image.exception.code, "image_too_large")

        async def small(url):
            return b"\x89PNG\r\n\x1a\nabc"

        with self.assertRaises(ImageFetchError) as total:
            asyncio.run(fetcher(small, per_image=100, total=20).fetch(
                ["https://example.com/1", "https://example.com/2"]))
        self.assertEqual(total.exception.code, "image_too_large")

    def test_unsupported_or_failed_downloads_are_not_judged(self):
        async def markup(url):
            return b"<html>not an image</html>"

        with self.assertRaises(ImageFetchError) as unsupported:
            asyncio.run(fetcher(markup).fetch(["https://example.com/a"]))
        self.assertEqual(unsupported.exception.code, "image_unsupported_format")

        async def broken(url):
            raise RuntimeError("private failure text")

        with self.assertRaises(ImageFetchError) as failed:
            asyncio.run(fetcher(broken).fetch(["https://example.com/a"]))
        self.assertEqual(failed.exception.code, "image_fetch_failed")
        self.assertNotIn("private", str(failed.exception))

    def test_private_addresses_are_rejected_before_any_request(self):
        called = []
        async def request(url):
            called.append(url)
            return PNG

        async def private(host, port):
            return ["127.0.0.1"]

        for url in ("https://localhost/a.png", "https://10.0.0.5/a.png"):
            with self.subTest(url=url), self.assertRaises(ImageFetchError) as rejected:
                asyncio.run(fetcher(request, resolver=private).fetch([url]))
            self.assertEqual(rejected.exception.code, "image_url_rejected")
        self.assertEqual(called, [])

    def test_timeout_is_reported_as_fetch_failure(self):
        async def slow(url):
            await asyncio.sleep(1)
            return PNG

        with self.assertRaises(ImageFetchError) as failed:
            asyncio.run(fetcher(slow, timeout=0.01).fetch(["https://example.com/a"]))
        self.assertEqual(failed.exception.code, "image_fetch_failed")


if __name__ == "__main__":
    unittest.main()
