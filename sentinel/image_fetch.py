"""Download untrusted chat images into bounded data URLs.

The image URL arrives in the platform payload, so it is treated as untrusted
input: only http(s) on standard ports, no user info, no redirects, and every
resolved address has to be publicly routable. The downloaded bytes are held in
memory only and are never written to the database or the audit trail.

The caller supplies the HTTP session and request function; tests can inject a
fake request and resolver so no network access is required.
"""

import asyncio
import base64
import ipaddress
import socket
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import url2pathname

MAX_URL_CHARS = 8192
MAX_PATH_CHARS = 4096
ALLOWED_PORTS = frozenset({80, 443})
PREVIEW_MAX_EDGE = 360
PREVIEW_MAX_BYTES = 131072
_CHUNK_BYTES = 65536


class ImageFetchError(Exception):
    """A bounded, non-sensitive reason an image could not be prepared."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class FetchedImage:
    data_url: str
    byte_count: int
    preview: tuple[str, bytes] | None = None


def _has_control_chars(value):
    return any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value)


def _local_path(reference):
    """Return an absolute local path for a raw path or file:// URI."""
    if not isinstance(reference, str) or not reference or len(reference) > MAX_PATH_CHARS:
        raise ImageFetchError("image_url_rejected")
    if _has_control_chars(reference):
        raise ImageFetchError("image_url_rejected")
    if reference.startswith("file://"):
        try:
            path = Path(url2pathname(urlsplit(reference).path))
        except ValueError:
            raise ImageFetchError("image_url_rejected") from None
    else:
        path = Path(reference)
    if not path.is_absolute():
        raise ImageFetchError("image_url_rejected")
    return path


def validate_image_url(url):
    """Validate an image URL and return the split URL."""
    if not isinstance(url, str) or not url or len(url) > MAX_URL_CHARS:
        raise ImageFetchError("image_url_rejected")
    if _has_control_chars(url):
        raise ImageFetchError("image_url_rejected")
    try:
        parts = urlsplit(url)
    except ValueError:
        raise ImageFetchError("image_url_rejected") from None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ImageFetchError("image_url_rejected")
    if parts.username is not None or parts.password is not None:
        raise ImageFetchError("image_url_rejected")
    try:
        port = parts.port
    except ValueError:
        raise ImageFetchError("image_url_rejected") from None
    if port is not None and port not in ALLOWED_PORTS:
        raise ImageFetchError("image_url_rejected")
    return parts


def sniff_image_mime(data):
    """Identify a supported image type by content, never by URL or filename."""
    if not isinstance(data, bytes) or not data:
        return None
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"BM"):
        return "image/bmp"
    return None


def make_preview(payload):
    """Return a small (mime, bytes) preview, or None when unavailable."""
    if not isinstance(payload, bytes) or not payload:
        return None
    try:
        import io

        from PIL import Image, ImageOps

        with Image.open(io.BytesIO(payload)) as opened:
            image = ImageOps.exif_transpose(opened)
            image.thumbnail((PREVIEW_MAX_EDGE, PREVIEW_MAX_EDGE), Image.Resampling.LANCZOS)
            has_alpha = "A" in image.getbands() or "transparency" in image.info
            output = io.BytesIO()
            if has_alpha:
                image.convert("RGBA").save(output, "PNG", optimize=True)
                mime = "image/png"
            else:
                image.convert("RGB").save(output, "JPEG", quality=80, optimize=True)
                mime = "image/jpeg"
        data = output.getvalue()
        if not data or len(data) > PREVIEW_MAX_BYTES:
            return None
        return mime, data
    except Exception:
        return None


async def _default_resolver(host, port):
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [info[4][0] for info in infos]


class ImageFetcher:
    """Fetch at most a few images within explicit size and time limits."""

    def __init__(self, session, *, max_images, max_bytes_per_image, max_total_bytes,
                 timeout_seconds, request=None, resolver=None, local_roots=None):
        if type(max_images) is not int or not 0 < max_images <= 32:
            raise ValueError("invalid image count limit")
        if any(type(value) is not int or value <= 0
               for value in (max_bytes_per_image, max_total_bytes)):
            raise ValueError("invalid image size limit")
        if type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds <= 120:
            raise ValueError("invalid image timeout")
        self.session = session
        self.max_images = max_images
        self.max_bytes_per_image = max_bytes_per_image
        self.max_total_bytes = max_total_bytes
        self.timeout_seconds = timeout_seconds
        self._request = request
        self._resolver = resolver or _default_resolver
        self._local_roots = tuple(Path(root).resolve() for root in (local_roots or ()))

    async def _ensure_public_host(self, host, port):
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            literal = None
        addresses = [str(literal)] if literal is not None else await self._resolver(host, port)
        if not addresses:
            raise ImageFetchError("image_url_rejected")
        for address in addresses:
            try:
                parsed = ipaddress.ip_address(address)
            except ValueError:
                raise ImageFetchError("image_url_rejected") from None
            if parsed.version == 6 and parsed.ipv4_mapped is not None:
                parsed = parsed.ipv4_mapped
            if not parsed.is_global:
                # Loopback, private, link-local, reserved and shared networks
                # must never be reachable from a chat payload.
                raise ImageFetchError("image_url_rejected")

    async def _load(self, url):
        try:
            if self._request is not None:
                payload = await asyncio.wait_for(self._request(url), self.timeout_seconds)
            else:
                payload = await asyncio.wait_for(self._http_request(url), self.timeout_seconds)
        except ImageFetchError:
            raise
        except (TimeoutError, asyncio.TimeoutError):
            raise ImageFetchError("image_fetch_failed") from None
        except Exception:
            raise ImageFetchError("image_fetch_failed") from None
        if not isinstance(payload, bytes) or not payload:
            raise ImageFetchError("image_fetch_failed")
        if len(payload) > self.max_bytes_per_image:
            raise ImageFetchError("image_too_large")
        return payload

    async def _http_request(self, url):
        import aiohttp

        timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
        async with self.session.get(url, timeout=timeout, allow_redirects=False,
                                    headers={"Accept": "image/*"}) as response:
            if response.status != 200:
                raise ImageFetchError("image_fetch_failed")
            chunks = []
            size = 0
            async for chunk in response.content.iter_chunked(_CHUNK_BYTES):
                size += len(chunk)
                if size > self.max_bytes_per_image:
                    raise ImageFetchError("image_too_large")
                chunks.append(chunk)
            return b"".join(chunks)

    async def _load_local(self, reference):
        path = _local_path(reference)
        try:
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError):
            raise ImageFetchError("image_fetch_failed") from None
        if self._local_roots and not any(
                resolved == root or resolved.is_relative_to(root) for root in self._local_roots):
            # Only framework-owned temporary media may be read from disk.
            raise ImageFetchError("image_url_rejected")
        if not resolved.is_file():
            raise ImageFetchError("image_fetch_failed")
        try:
            size = resolved.stat().st_size
        except OSError:
            raise ImageFetchError("image_fetch_failed") from None
        if size <= 0:
            raise ImageFetchError("image_fetch_failed")
        if size > self.max_bytes_per_image:
            raise ImageFetchError("image_too_large")
        try:
            payload = await asyncio.to_thread(resolved.read_bytes)
        except OSError:
            raise ImageFetchError("image_fetch_failed") from None
        if len(payload) > self.max_bytes_per_image:
            raise ImageFetchError("image_too_large")
        return payload

    async def fetch(self, urls):
        """Return the images as data URLs, or raise a fixed error code."""
        if not isinstance(urls, (list, tuple)) or not urls:
            raise ValueError("image references are required")
        if len(urls) > self.max_images:
            raise ImageFetchError("image_too_many")
        images = []
        total = 0
        for url in urls:
            if isinstance(url, str) and url.startswith(("http://", "https://")):
                parts = validate_image_url(url)
                port = parts.port or (443 if parts.scheme == "https" else 80)
                await self._ensure_public_host(parts.hostname, port)
                payload = await self._load(url)
            else:
                payload = await self._load_local(url)
            total += len(payload)
            if total > self.max_total_bytes:
                raise ImageFetchError("image_too_large")
            mime = sniff_image_mime(payload)
            if mime is None:
                raise ImageFetchError("image_unsupported_format")
            encoded = base64.b64encode(payload).decode("ascii")
            preview = await asyncio.to_thread(make_preview, payload)
            images.append(FetchedImage(f"data:{mime};base64,{encoded}", len(payload), preview))
        return tuple(images)
