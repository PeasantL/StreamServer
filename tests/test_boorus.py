"""Turning booru post pages into original file URLs.

Nothing here touches the network: every test stubs the two functions that do
(`downloads.fetch_json` and `downloads.fetch_text`) with recorded response
shapes, so the parsing rules are what is under test.
"""

import pytest

import boorus


@pytest.fixture(autouse=True)
def _isolated(app_env):
    """Every test runs against the reloaded, throwaway configuration."""


def _stub(monkeypatch, *, json=None, text=None):
    """Point the resolvers at canned responses and record what they asked for."""
    calls = {"json": [], "text": []}

    def fake_json(url, *, headers=None):
        calls["json"].append((url, headers))
        if json is None:
            raise ValueError(f"Response from {url} was not JSON")
        return json(url) if callable(json) else json

    def fake_text(url, *, headers=None, max_bytes=None):
        calls["text"].append((url, headers))
        if text is None:
            raise OSError("no page")
        return text

    monkeypatch.setattr(boorus.downloads, "fetch_json", fake_json)
    monkeypatch.setattr(boorus.downloads, "fetch_text", fake_text)
    return calls


# --- site and post-id recognition --------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://danbooru.donmai.us/posts/123", "danbooru"),
        ("https://gelbooru.com/index.php?page=post&s=view&id=1", "gelbooru"),
        ("https://WWW.Gelbooru.COM/index.php?page=post&s=view&id=1", "gelbooru"),
        ("https://rule34.us/index.php?r=posts/view&id=1", "rule34.us"),
        ("https://rule34.xxx/index.php?page=post&s=view&id=1", "rule34.xxx"),
        # A direct CDN link is not a post page; it takes the ordinary path.
        ("https://img3.gelbooru.com/images/ab/cd/hash.mp4", None),
        ("https://example.com/clip.mp4", None),
        ("not a url at all", None),
    ],
)
def test_find_site_matches_on_host_only(url, expected):
    site = boorus.find_site(url)
    assert (site.name if site else None) == expected


@pytest.mark.parametrize(
    ("url", "post_id"),
    [
        ("https://danbooru.donmai.us/posts/9876", "9876"),
        ("https://danbooru.donmai.us/posts/9876?q=tag", "9876"),
        # The legacy route danbooru still redirects from.
        ("https://danbooru.donmai.us/post/show/9876", "9876"),
        ("https://gelbooru.com/index.php?page=post&s=view&id=9876&tags=all", "9876"),
        ("https://rule34.us/index.php?r=posts/view&id=9876", "9876"),
    ],
)
def test_post_ids_are_read_from_the_url(monkeypatch, url, post_id):
    # The canned original has to sit on whichever site is under test, so it is
    # built from that site's own media domain.
    site = boorus.find_site(url)
    media = f"https://cdn.{site.media_domain}/images/ab/cd/abcd.mp4"
    _stub(monkeypatch, json={"file_url": media}, text=f'<a href="{media}">Original</a>')

    post = boorus.resolve_post(url)

    assert post.post_id == post_id
    assert post.file_url == media


@pytest.mark.parametrize(
    "url",
    [
        # A tag listing, the front page, a search: all on booru hosts, none a post.
        "https://gelbooru.com/index.php?page=post&s=list&tags=cat",
        "https://gelbooru.com/",
        "https://rule34.us/index.php?r=posts/index",
        "https://danbooru.donmai.us/posts",
        # An id that is not a number must not be pasted into an API URL.
        "https://gelbooru.com/index.php?page=post&s=view&id=../../etc",
    ],
)
def test_non_post_pages_on_a_booru_host_say_so(url):
    with pytest.raises(boorus.BooruError, match="not a single post"):
        boorus.resolve_post(url)


def test_a_non_booru_url_is_refused_outright():
    with pytest.raises(boorus.BooruError, match="not a recognised booru"):
        boorus.resolve_post("https://example.com/clip.mp4")


# --- danbooru ----------------------------------------------------------------


def test_danbooru_uses_the_json_api(monkeypatch):
    calls = _stub(monkeypatch, json={
        "id": 9876,
        "file_url": "https://cdn.donmai.us/original/ab/cd/abcd.webm",
        "large_file_url": "https://cdn.donmai.us/sample/ab/cd/sample-abcd.webm",
    })

    post = boorus.resolve_post("https://danbooru.donmai.us/posts/9876")

    assert calls["json"][0][0] == "https://danbooru.donmai.us/posts/9876.json"
    # The original, never the "large"/sample rendition.
    assert post.file_url == "https://cdn.donmai.us/original/ab/cd/abcd.webm"
    assert post.title == "danbooru 9876"
    assert post.page_url == "https://danbooru.donmai.us/posts/9876"


def test_danbooru_post_without_a_file_url_is_reported(monkeypatch):
    """Restricted posts come back as JSON with the file_url withheld."""
    _stub(monkeypatch, json={"id": 9876, "is_banned": True})
    with pytest.raises(boorus.BooruError, match="does not expose an original file"):
        boorus.resolve_post("https://danbooru.donmai.us/posts/9876")


def test_danbooru_api_failure_is_reported_not_swallowed(monkeypatch):
    _stub(monkeypatch, json=None)
    with pytest.raises(boorus.BooruError, match="Could not read danbooru post"):
        boorus.resolve_post("https://danbooru.donmai.us/posts/9876")


# --- gelbooru family ---------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        # gelbooru's own json=1 envelope...
        {"@attributes": {"count": 1}, "post": [{"file_url": "https://img3.gelbooru.com/images/ab/cd/abcd.mp4"}]},
        # ...a bare list, as most clones return...
        [{"file_url": "https://img3.gelbooru.com/images/ab/cd/abcd.mp4"}],
        # ...and a single object under "post".
        {"post": {"file_url": "https://img3.gelbooru.com/images/ab/cd/abcd.mp4"}},
    ],
)
def test_gelbooru_accepts_every_api_shape(monkeypatch, payload):
    _stub(monkeypatch, json=payload)
    post = boorus.resolve_post("https://gelbooru.com/index.php?page=post&s=view&id=42")
    assert post.file_url == "https://img3.gelbooru.com/images/ab/cd/abcd.mp4"
    assert post.title == "gelbooru 42"


def test_gelbooru_credentials_are_sent_when_configured(monkeypatch):
    import dataclasses

    monkeypatch.setattr(
        boorus,
        "settings",
        dataclasses.replace(boorus.settings, gelbooru_api_key="KEY", gelbooru_user_id="7"),
    )
    calls = _stub(monkeypatch, json=[{"file_url": "https://img3.gelbooru.com/images/a/b/x.mp4"}])

    boorus.resolve_post("https://gelbooru.com/index.php?page=post&s=view&id=42")

    api = calls["json"][0][0]
    assert "api_key=KEY" in api and "user_id=7" in api


def test_gelbooru_without_credentials_sends_none(monkeypatch):
    calls = _stub(monkeypatch, json=[{"file_url": "https://img3.gelbooru.com/images/a/b/x.mp4"}])
    boorus.resolve_post("https://gelbooru.com/index.php?page=post&s=view&id=42")
    assert "api_key" not in calls["json"][0][0]


def test_gelbooru_falls_back_to_the_page_when_the_api_is_empty(monkeypatch):
    """An unauthenticated gelbooru API returns an empty envelope, not an error."""
    calls = _stub(
        monkeypatch,
        json={"@attributes": {"count": 0}},
        text=(
            '<img id="image" src="https://img3.gelbooru.com/samples/ab/cd/sample_abcd.jpg">'
            '<a href="https://img3.gelbooru.com/images/ab/cd/abcd.mp4">Original image</a>'
        ),
    )

    post = boorus.resolve_post("https://gelbooru.com/index.php?page=post&s=view&id=42")

    assert calls["text"], "the page should have been read"
    assert post.file_url == "https://img3.gelbooru.com/images/ab/cd/abcd.mp4"


def test_gelbooru_falls_back_when_the_api_is_not_json(monkeypatch):
    _stub(
        monkeypatch,
        json=None,
        text='<a href="https://img3.gelbooru.com/images/ab/cd/abcd.webm">Original</a>',
    )
    post = boorus.resolve_post("https://gelbooru.com/index.php?page=post&s=view&id=42")
    assert post.file_url == "https://img3.gelbooru.com/images/ab/cd/abcd.webm"


# --- rule34.us (scrape only) -------------------------------------------------


def test_rule34_us_reads_the_video_source_off_the_page(monkeypatch):
    _stub(
        monkeypatch,
        text=(
            '<video id="videoelement" poster="https://img.rule34.us/thumbnails/ab/cd/thumb.jpg">'
            '<source src="https://video.rule34.us/images/ab/cd/abcd1234.mp4" type="video/mp4">'
            "</video>"
        ),
    )
    post = boorus.resolve_post("https://rule34.us/index.php?r=posts/view&id=42")
    assert post.file_url == "https://video.rule34.us/images/ab/cd/abcd1234.mp4"
    assert post.title == "rule34.us 42"


def test_the_sample_and_thumbnail_are_never_chosen(monkeypatch):
    _stub(
        monkeypatch,
        text=(
            '<img src="https://img.rule34.us/thumbnails/ab/cd/thumbnail_abcd.jpg">'
            '<img src="https://img.rule34.us/samples/ab/cd/sample_abcd.jpg">'
            '<img id="image" src="https://img.rule34.us/images/ab/cd/abcd.png">'
        ),
    )
    assert boorus.resolve_post(
        "https://rule34.us/index.php?r=posts/view&id=42"
    ).file_url == "https://img.rule34.us/images/ab/cd/abcd.png"


def test_a_video_on_the_page_wins_over_an_image(monkeypatch):
    """This is a video catalogue; a post page can reference both."""
    _stub(
        monkeypatch,
        text=(
            '<img src="https://img.rule34.us/images/ab/cd/poster.jpg">'
            '<source src="https://video.rule34.us/images/ab/cd/clip.webm">'
        ),
    )
    assert boorus.resolve_post(
        "https://rule34.us/index.php?r=posts/view&id=42"
    ).file_url == "https://video.rule34.us/images/ab/cd/clip.webm"


def test_a_page_with_no_original_is_reported(monkeypatch):
    _stub(monkeypatch, text="<html><body>Post not found</body></html>")
    with pytest.raises(boorus.BooruError, match="Could not find an original file"):
        boorus.resolve_post("https://rule34.us/index.php?r=posts/view&id=42")


def test_html_entities_in_the_link_are_decoded(monkeypatch):
    _stub(
        monkeypatch,
        text='<a href="https://img.rule34.us/images/a&amp;b/cd/abcd.mp4">Original</a>',
    )
    assert boorus.resolve_post(
        "https://rule34.us/index.php?r=posts/view&id=42"
    ).file_url == "https://img.rule34.us/images/a&b/cd/abcd.mp4"


# --- the off-domain guard ----------------------------------------------------


def test_a_url_scraped_from_a_comment_cannot_steer_the_download(monkeypatch):
    """Post pages carry user-written comments; an off-domain link in one is
    not something the post hosts, and must not be fetched."""
    _stub(
        monkeypatch,
        text=(
            '<div class="comment">check out '
            'https://attacker.example/images/ab/cd/payload.mp4</div>'
        ),
    )
    with pytest.raises(boorus.BooruError, match="Could not find an original file"):
        boorus.resolve_post("https://rule34.us/index.php?r=posts/view&id=42")


def test_an_off_domain_api_file_url_is_refused(monkeypatch):
    _stub(monkeypatch, json={"file_url": "https://attacker.example/original/a/b/x.mp4"})
    with pytest.raises(boorus.BooruError, match="not hosted on donmai.us"):
        boorus.resolve_post("https://danbooru.donmai.us/posts/9876")


def test_a_lookalike_domain_is_refused(monkeypatch):
    """`donmai.us.attacker.example` and `notdonmai.us` must both fail."""
    for host in ("donmai.us.attacker.example", "notdonmai.us"):
        _stub(monkeypatch, json={"file_url": f"https://{host}/original/a/b/x.mp4"})
        with pytest.raises(boorus.BooruError, match="not hosted on"):
            boorus.resolve_post("https://danbooru.donmai.us/posts/9876")


def test_a_cdn_subdomain_is_accepted(monkeypatch):
    _stub(monkeypatch, json={"file_url": "https://cdn.donmai.us/original/a/b/x.mp4"})
    post = boorus.resolve_post("https://danbooru.donmai.us/posts/9876")
    assert post.file_url == "https://cdn.donmai.us/original/a/b/x.mp4"


def test_a_non_http_file_url_is_refused(monkeypatch):
    _stub(monkeypatch, json={"file_url": "file:///etc/passwd"})
    with pytest.raises(boorus.BooruError):
        boorus.resolve_post("https://danbooru.donmai.us/posts/9876")


def test_a_relative_file_url_is_resolved_against_the_site(monkeypatch):
    """Some gelbooru clones return a site-relative path."""
    _stub(monkeypatch, json=[{"file_url": "//safebooru.org/images/ab/cd/abcd.mp4"}])
    post = boorus.resolve_post("https://safebooru.org/index.php?page=post&s=view&id=42")
    assert post.file_url == "https://safebooru.org/images/ab/cd/abcd.mp4"


# --- URL rebuilding ----------------------------------------------------------


def test_api_calls_are_pinned_to_https_and_drop_userinfo(monkeypatch):
    """A pasted http:// link must not downgrade the lookup that follows, and
    a `user:pass@` prefix must not be carried into the API URL."""
    calls = _stub(monkeypatch, json={"file_url": "https://cdn.donmai.us/original/a/b/x.mp4"})
    boorus.resolve_post("http://someone:secret@danbooru.donmai.us/posts/9876")
    assert calls["json"][0][0] == "https://danbooru.donmai.us/posts/9876.json"


def test_the_post_page_is_sent_as_the_referer(monkeypatch):
    """Booru CDNs and APIs commonly refuse requests without one."""
    page = "https://danbooru.donmai.us/posts/9876"
    calls = _stub(monkeypatch, json={"file_url": "https://cdn.donmai.us/original/a/b/x.mp4"})
    boorus.resolve_post(page)
    assert calls["json"][0][1] == {"Referer": page}
