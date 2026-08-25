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


class _FakeSession:
    """Records every request and replays a scripted sequence of responses."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.headers = {}
        self.sent = []

    def get(self, url, headers=None, **kwargs):
        self.sent.append((url, dict(self.headers), dict(headers or {})))
        return self._responses.pop(0)


class _StubResponse:
    def __init__(self, chunks=(), headers=None, status=200, encoding="utf-8"):
        self._chunks = list(chunks)
        self.headers = headers or {}
        self.status_code = status
        self.encoding = encoding
        self.is_redirect = status in (301, 302, 303, 307, 308)
        self.is_permanent_redirect = status in (301, 308)
        self.closed = False

    def iter_content(self, chunk_size=None):
        return iter(self._chunks)

    def raise_for_status(self):
        return None

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _session_factory(monkeypatch, responses):
    session = _FakeSession(responses)
    monkeypatch.setattr(downloads.requests, "Session", lambda: session)
    monkeypatch.setattr(downloads, "assert_safe_url", lambda url: None)
    return session


def test_every_request_identifies_the_client(monkeypatch, app_env):
    """Booru APIs rate-limit or reject the default python-requests agent."""
    session = _session_factory(monkeypatch, [_StubResponse()])
    downloads.open_stream("https://example.com/clip.mp4")
    assert session.sent[0][1]["User-Agent"] == downloads.USER_AGENT


def test_extra_headers_reach_the_host_they_were_written_for(monkeypatch, app_env):
    session = _session_factory(monkeypatch, [_StubResponse()])
    downloads.open_stream(
        "https://img3.gelbooru.com/images/a/b/x.mp4",
        headers={"Referer": "https://gelbooru.com/index.php?id=42"},
    )
    assert session.sent[0][2] == {"Referer": "https://gelbooru.com/index.php?id=42"}


def test_extra_headers_are_dropped_when_a_redirect_leaves_the_host(monkeypatch, app_env):
    """A Referer is addressed to one host; a redirect elsewhere must not
    hand it to somewhere the user never named."""
    session = _session_factory(
        monkeypatch,
        [
            _StubResponse(status=302, headers={"location": "https://elsewhere.example/x.mp4"}),
            _StubResponse(),
        ],
    )

    downloads.open_stream(
        "https://img3.gelbooru.com/images/a/b/x.mp4",
        headers={"Referer": "https://gelbooru.com/index.php?id=42"},
    )

    assert session.sent[0][2] == {"Referer": "https://gelbooru.com/index.php?id=42"}
    assert session.sent[1][2] == {}
    # The User-Agent is not host-specific and stays on.
    assert session.sent[1][1]["User-Agent"] == downloads.USER_AGENT


def test_fetch_text_decodes_with_the_declared_encoding(monkeypatch, app_env):
    _session_factory(monkeypatch, [_StubResponse([b"caf\xe9"], encoding="latin-1")])
    assert downloads.fetch_text("https://example.com/page") == "café"


def test_fetch_text_stops_at_its_own_ceiling(monkeypatch, app_env):
    """A page read into memory is capped separately from a video going to disk."""
    _session_factory(monkeypatch, [_StubResponse([b"x" * 10] * 5)])
    with pytest.raises(downloads.DownloadTooLargeError):
        downloads.fetch_text("https://example.com/page", max_bytes=20)


def test_fetch_json_parses_and_keeps_the_url_in_the_error(monkeypatch, app_env):
    _session_factory(monkeypatch, [_StubResponse([b'{"file_url": "x"}'])])
    assert downloads.fetch_json("https://example.com/api") == {"file_url": "x"}

    _session_factory(monkeypatch, [_StubResponse([b"<html>not json</html>"])])
    with pytest.raises(ValueError, match="was not JSON"):
        downloads.fetch_json("https://example.com/api")
