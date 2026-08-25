"""Fetching remote video files, with the network side locked down.

The previous version passed a user-supplied URL straight to ``requests.get``
with no scheme check, no host check and no byte cap, which made the endpoint an
open SSRF proxy into whatever the server could reach, and a way to fill the
disk with a single request.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import socket
from collections.abc import Callable, Iterator
from pathlib import Path
from urllib.parse import urlsplit

import requests

from config import settings

log = logging.getLogger(__name__)

ALLOWED_SCHEMES = ("http", "https")
ALLOWED_EXTENSIONS = (".mp4", ".webm")
MAX_REDIRECTS = 5
CHUNK_SIZE = 1024 * 1024

# Booru APIs rate-limit or reject the default ``python-requests/x.y`` agent,
# and danbooru's terms ask that clients identify themselves.
USER_AGENT = "StreamServer/1.0 (+https://github.com/PeasantL/StreamServer)"

# Ceiling for a page or API response read into memory. Distinct from
# max_download_bytes, which sizes a video going to disk.
MAX_TEXT_BYTES = 4 * 1024 * 1024


class UnsafeURLError(ValueError):
    """The URL points somewhere we refuse to fetch from."""


class DownloadTooLargeError(ValueError):
    """The response exceeded the configured size ceiling."""


def url_extension(url: str) -> str:
    """Extension of the URL *path*, ignoring query string and fragment."""
    return Path(urlsplit(url).path).suffix.lower()


def url_filename(url: str) -> str:
    """Last path segment of the URL, for display only.

    Never use this to build a path on disk: it is remote-controlled. Callers
    name files from their own UUID and keep this purely as metadata.
    """
    return Path(urlsplit(url).path).name


def _is_forbidden(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def assert_safe_url(url: str) -> None:
    """Reject non-HTTP schemes and any host resolving to an internal address."""
    parsed = urlsplit(url)

    if parsed.scheme not in ALLOWED_SCHEMES:
        raise UnsafeURLError(f"Unsupported URL scheme {parsed.scheme!r}")

    host = parsed.hostname
    if not host:
        raise UnsafeURLError("URL has no host")

    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise UnsafeURLError(f"Cannot resolve host {host!r}") from exc

    if not infos:
        raise UnsafeURLError(f"Cannot resolve host {host!r}")

    # Every resolved address must be external: a name with one public and one
    # private record must not be usable to reach the private one.
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if _is_forbidden(address):
            raise UnsafeURLError(
                f"Refusing to fetch {host!r}: resolves to internal address {address}"
            )


def open_stream(
    url: str, *, headers: dict[str, str] | None = None
) -> requests.Response:
    """GET *url*, re-validating the target across every redirect hop.

    ``headers`` carries per-request extras such as the ``Referer`` a booru CDN
    wants before it will serve a file. They are addressed to the host they were
    written for, so a redirect that leaves that host drops them rather than
    handing them to somewhere else.
    """
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    extra = dict(headers or {})
    origin_host = urlsplit(url).hostname
    current = url

    for _ in range(MAX_REDIRECTS + 1):
        assert_safe_url(current)
        response = session.get(
            current,
            headers=extra if urlsplit(current).hostname == origin_host else {},
            stream=True,
            allow_redirects=False,
            timeout=settings.download_timeout,
        )
        if response.is_redirect or response.is_permanent_redirect:
            location = response.headers.get("location")
            response.close()
            if not location:
                raise UnsafeURLError("Redirect with no Location header")
            current = requests.compat.urljoin(current, location)
            continue

        response.raise_for_status()
        return response

    raise UnsafeURLError("Too many redirects")


def fetch_text(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    max_bytes: int = MAX_TEXT_BYTES,
) -> str:
    """Read a page or API response into memory, with a hard size ceiling.

    ``requests.Response.text`` would buffer whatever the far end sent; a booru
    is not going to hand back a gigabyte of HTML, but nothing about the
    endpoint guarantees that, so the read stops at *max_bytes*.
    """
    response = open_stream(url, headers=headers)
    chunks: list[bytes] = []
    size = 0

    with response:
        for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
            if not chunk:
                continue
            size += len(chunk)
            if size > max_bytes:
                raise DownloadTooLargeError(
                    f"Response from {url} exceeded {max_bytes} bytes"
                )
            chunks.append(chunk)
        # apparent_encoding is deliberately not consulted: it re-reads
        # response.content, which a streamed body no longer has.
        encoding = response.encoding or "utf-8"

    return b"".join(chunks).decode(encoding, errors="replace")


def fetch_json(url: str, *, headers: dict[str, str] | None = None) -> object:
    """``fetch_text`` plus a JSON parse, with the URL kept in the error."""
    body = fetch_text(url, headers=headers)
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Response from {url} was not JSON: {exc}") from None


def stream_to_file(
    response: requests.Response,
    destination: Path,
    on_progress: Callable[[float | None], None] | None = None,
) -> int:
    """Write the response body to disk, stopping at the configured ceiling.

    ``on_progress`` receives a fraction between 0 and 1, or ``None`` when the
    server did not send a Content-Length. The previous version divided by that
    length unconditionally and raised ZeroDivisionError on any chunked response.
    """
    try:
        total = int(response.headers.get("content-length") or 0)
    except ValueError:
        total = 0

    if total > settings.max_download_bytes:
        raise DownloadTooLargeError(
            f"Declared size {total} exceeds limit {settings.max_download_bytes}"
        )

    written = 0
    with open(destination, "wb") as handle:
        for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
            if not chunk:
                continue
            written += len(chunk)
            if written > settings.max_download_bytes:
                raise DownloadTooLargeError(
                    f"Download exceeded limit of {settings.max_download_bytes} bytes"
                )
            handle.write(chunk)
            if on_progress is not None:
                on_progress(written / total if total > 0 else None)

    return written


def iter_file_range(
    path: Path, start: int, end: int, chunk_size: int = CHUNK_SIZE
) -> Iterator[bytes]:
    """Yield bytes [start, end] inclusive from *path*."""
    with open(path, "rb") as handle:
        handle.seek(start)
        remaining = end - start + 1
        while remaining > 0:
            data = handle.read(min(chunk_size, remaining))
            if not data:
                break
            yield data
            remaining -= len(data)
