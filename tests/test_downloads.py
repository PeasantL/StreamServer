"""The SSRF guard and the download size ceiling."""

import pytest

import downloads


@pytest.mark.parametrize(
    ("url", "extension"),
    [
        ("https://example.com/clip.mp4", ".mp4"),
        ("https://example.com/clip.webm", ".webm"),
        # A query string must not defeat the extension check.
        ("https://example.com/clip.mp4?token=abc", ".mp4"),
        ("https://example.com/page.html", ".html"),
        ("https://example.com/clip", ""),
    ],
)
def test_url_extension_ignores_query_strings(url, extension):
    assert downloads.url_extension(url) == extension


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/clip.mp4",
        "gopher://example.com/clip.mp4",
        "https:///clip.mp4",
    ],
)
def test_non_http_schemes_and_missing_hosts_are_refused(url):
    with pytest.raises(downloads.UnsafeURLError):
        downloads.assert_safe_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/clip.mp4",
        "http://localhost/clip.mp4",
        "http://169.254.169.254/latest/meta-data",   # cloud metadata
        "http://192.168.1.1/clip.mp4",
        "http://10.0.0.5/clip.mp4",
        "http://[::1]/clip.mp4",
    ],
)
def test_internal_addresses_are_refused(url):
    with pytest.raises(downloads.UnsafeURLError):
        downloads.assert_safe_url(url)


def test_a_name_resolving_to_any_internal_address_is_refused(monkeypatch):
    """One public and one private record must not be usable to reach the private one."""

    def fake_getaddrinfo(host, port, **kwargs):
        return [
            (2, 1, 6, "", ("93.184.216.34", port)),
            (2, 1, 6, "", ("127.0.0.1", port)),
        ]

    monkeypatch.setattr(downloads.socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(downloads.UnsafeURLError):
        downloads.assert_safe_url("https://split-horizon.example/clip.mp4")


def test_public_addresses_are_accepted(monkeypatch):
    monkeypatch.setattr(
        downloads.socket,
        "getaddrinfo",
        lambda host, port, **kwargs: [(2, 1, 6, "", ("93.184.216.34", port))],
    )
    downloads.assert_safe_url("https://example.com/clip.mp4")


class _FakeResponse:
    def __init__(self, chunks, headers=None):
        self._chunks = chunks
        self.headers = headers or {}

    def iter_content(self, chunk_size=None):
        return iter(self._chunks)


def test_missing_content_length_does_not_raise_zero_division(tmp_path, app_env):
    """A chunked response used to crash on the first progress update."""
    seen = []
    written = downloads.stream_to_file(
        _FakeResponse([b"abc", b"def"]),
        tmp_path / "out.bin",
        on_progress=seen.append,
    )
    assert written == 6
    assert seen == [None, None]


def test_progress_is_reported_when_length_is_known(tmp_path, app_env):
    seen = []
    downloads.stream_to_file(
        _FakeResponse([b"a" * 5, b"b" * 5], {"content-length": "10"}),
        tmp_path / "out.bin",
        on_progress=seen.append,
    )
    assert seen == [0.5, 1.0]


def test_downloads_stop_at_the_size_ceiling(tmp_path, app_env, monkeypatch):
    import dataclasses

    monkeypatch.setattr(
        downloads, "settings", dataclasses.replace(downloads.settings, max_download_bytes=8)
    )
    with pytest.raises(downloads.DownloadTooLargeError):
        downloads.stream_to_file(_FakeResponse([b"x" * 5] * 4), tmp_path / "out.bin")


def test_declared_oversize_is_refused_before_writing(tmp_path, app_env, monkeypatch):
    import dataclasses

    monkeypatch.setattr(
        downloads, "settings", dataclasses.replace(downloads.settings, max_download_bytes=8)
    )
    with pytest.raises(downloads.DownloadTooLargeError):
        downloads.stream_to_file(
            _FakeResponse([b"x"], {"content-length": "999"}), tmp_path / "out.bin"
        )
