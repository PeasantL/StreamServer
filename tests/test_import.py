"""Booru tag-search batch import.

The network is stubbed throughout: what is under test is which URLs count as
searches, which results survive filtering, and how a partial batch is reported.
"""

import pytest

import boorus


@pytest.fixture(autouse=True)
def _isolated(app_env):
    """Every test runs against the reloaded, throwaway configuration."""


def _stub_json(monkeypatch, payload):
    def fake_json(url, *, headers=None):
        return payload(url) if callable(payload) else payload

    monkeypatch.setattr(boorus.downloads, "fetch_json", fake_json)


# --- recognising a search URL ------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "tags"),
    [
        ("https://danbooru.donmai.us/posts?tags=blue_hair+video", "blue_hair video"),
        ("https://gelbooru.com/index.php?page=post&s=list&tags=animated", "animated"),
        ("https://rule34.xxx/index.php?page=post&s=list&tags=webm", "webm"),
        ("https://safebooru.org/index.php?page=post&s=list&tags=cat", "cat"),
    ],
)
def test_search_urls_are_recognised(url, tags):
    request = boorus.find_search(url)

    assert request is not None
    assert request.tags == tags


@pytest.mark.parametrize(
    "url",
    [
        # A single post is not a search.
        "https://danbooru.donmai.us/posts/12345",
        "https://gelbooru.com/index.php?page=post&s=view&id=42",
        # A listing with no tags has nothing to search for.
        "https://danbooru.donmai.us/posts",
        "https://gelbooru.com/index.php?page=post&s=list",
        # rule34.us has no search API, so its listings stay unrecognised.
        "https://rule34.us/index.php?r=posts/index&q=cat",
        # Not a booru at all.
        "https://example.com/videos?tags=cat",
    ],
)
def test_non_search_urls_are_not_recognised(url):
    assert boorus.find_search(url) is None


def test_resolving_a_non_search_url_is_an_error():
    with pytest.raises(boorus.BooruError):
        boorus.resolve_search("https://example.com/clip.mp4", 10)


# --- danbooru ------------------------------------------------------------------------


DANBOORU_RESULTS = [
    {"id": 1, "file_url": "https://cdn.donmai.us/original/a/b/one.mp4", "tag_string": "solo"},
    {"id": 2, "file_url": "https://cdn.donmai.us/original/a/b/two.png", "tag_string": "solo"},
    {"id": 3, "file_url": "https://cdn.donmai.us/original/a/b/three.webm", "tag_string": "duo"},
]


def test_only_video_posts_are_returned(monkeypatch):
    """A tag search matches mostly images; fetching them would be pure waste."""
    _stub_json(monkeypatch, DANBOORU_RESULTS)

    posts = boorus.resolve_search("https://danbooru.donmai.us/posts?tags=solo", 10)

    assert [post.post_id for post in posts] == ["1", "3"]


def test_each_result_carries_its_own_post_page_and_tags(monkeypatch):
    _stub_json(monkeypatch, DANBOORU_RESULTS)

    first = boorus.resolve_search("https://danbooru.donmai.us/posts?tags=solo", 10)[0]

    assert first.page_url == "https://danbooru.donmai.us/posts/1"
    assert first.file_url == "https://cdn.donmai.us/original/a/b/one.mp4"
    assert first.tags == ("solo",)
    assert first.title == "danbooru 1"


def test_the_requested_limit_reaches_the_api(monkeypatch):
    seen = {}

    def fake_json(url, *, headers=None):
        seen["url"] = url
        return []

    monkeypatch.setattr(boorus.downloads, "fetch_json", fake_json)
    boorus.resolve_search("https://danbooru.donmai.us/posts?tags=solo", 7)

    assert "limit=7" in seen["url"]
    assert "tags=solo" in seen["url"]


def test_an_off_domain_result_is_skipped_not_fetched(monkeypatch):
    """A search response is no more trustworthy than a post page."""
    _stub_json(monkeypatch, [
        {"id": 1, "file_url": "https://attacker.example/original/a/b/x.mp4"},
        {"id": 2, "file_url": "https://cdn.donmai.us/original/a/b/ok.mp4"},
    ])

    posts = boorus.resolve_search("https://danbooru.donmai.us/posts?tags=solo", 10)

    assert [post.post_id for post in posts] == ["2"]


def test_a_result_missing_a_file_url_is_skipped(monkeypatch):
    """Danbooru withholds file_url on restricted posts."""
    _stub_json(monkeypatch, [
        {"id": 1, "tag_string": "solo"},
        {"id": 2, "file_url": "https://cdn.donmai.us/original/a/b/ok.mp4"},
    ])

    assert [p.post_id for p in boorus.resolve_search(
        "https://danbooru.donmai.us/posts?tags=solo", 10)] == ["2"]


def test_a_search_with_no_video_matches_returns_empty(monkeypatch):
    _stub_json(monkeypatch, [DANBOORU_RESULTS[1]])

    assert boorus.resolve_search("https://danbooru.donmai.us/posts?tags=solo", 10) == []


def test_an_unexpected_response_shape_is_an_error(monkeypatch):
    _stub_json(monkeypatch, {"unexpected": True})

    with pytest.raises(boorus.BooruError):
        boorus.resolve_search("https://danbooru.donmai.us/posts?tags=solo", 10)


def test_a_failing_api_is_reported_as_a_booru_error(monkeypatch):
    def fake_json(url, *, headers=None):
        raise OSError("connection reset")

    monkeypatch.setattr(boorus.downloads, "fetch_json", fake_json)

    with pytest.raises(boorus.BooruError, match="Could not search"):
        boorus.resolve_search("https://danbooru.donmai.us/posts?tags=solo", 10)


# --- gelbooru family --------------------------------------------------------------------


def test_gelbooru_search_results_are_parsed(monkeypatch):
    _stub_json(monkeypatch, {"post": [
        {"id": 5, "file_url": "https://img3.gelbooru.com/images/a/b/x.mp4", "tags": "animated"},
    ]})

    posts = boorus.resolve_search(
        "https://gelbooru.com/index.php?page=post&s=list&tags=animated", 10
    )

    assert len(posts) == 1
    assert posts[0].page_url == "https://gelbooru.com/index.php?page=post&s=view&id=5"
    assert posts[0].tags == ("animated",)


def test_an_empty_gelbooru_search_without_credentials_explains_itself(monkeypatch):
    """The API answers an unauthenticated search with an empty result, not an error."""
    _stub_json(monkeypatch, {})

    with pytest.raises(boorus.BooruError, match="credentials"):
        boorus.resolve_search(
            "https://gelbooru.com/index.php?page=post&s=list&tags=animated", 10
        )


def test_a_clone_without_credentials_reports_no_matches_plainly(monkeypatch):
    """rule34.xxx has an open API, so empty means empty."""
    _stub_json(monkeypatch, {})

    assert boorus.resolve_search(
        "https://rule34.xxx/index.php?page=post&s=list&tags=nothing", 10
    ) == []


# --- the import task ----------------------------------------------------------------------


def _posts(count):
    return [
        boorus.Post(
            site="danbooru",
            post_id=str(index),
            page_url=f"https://danbooru.donmai.us/posts/{index}",
            file_url=f"https://cdn.donmai.us/original/a/b/{index}.mp4",
            tags=("solo",),
        )
        for index in range(1, count + 1)
    ]


def test_the_endpoint_routes_a_search_url_to_an_import_task(client, app_env, monkeypatch):
    test_client, main = client
    monkeypatch.setattr(main.boorus, "resolve_search", lambda url, limit: [])

    response = test_client.post(
        "/api/download", json={"url": "https://danbooru.donmai.us/posts?tags=solo"}
    )

    assert response.status_code == 200
    assert "task_id" in response.json()


def test_every_matched_post_is_stored(client, app_env, monkeypatch):
    test_client, main = client
    stored = []
    monkeypatch.setattr(main.boorus, "resolve_search", lambda url, limit: _posts(3))
    monkeypatch.setattr(main.downloads, "assert_safe_url", lambda url: None)
    monkeypatch.setattr(
        main, "store_remote_video",
        lambda url, ext, directory, **kwargs: stored.append(url) or "id",
    )

    task_id = main.registry.create("import")
    main.process_import_task(task_id, "https://danbooru.donmai.us/posts?tags=solo",
                             app_env["videos"], 10)

    assert len(stored) == 3
    assert main.registry.get(task_id)["status"] == "completed"


def test_one_failing_post_does_not_abandon_the_batch(client, app_env, monkeypatch):
    """A single bad post must not cost the user the other nineteen."""
    test_client, main = client
    stored = []

    def flaky(url, ext, directory, **kwargs):
        if url.endswith("2.mp4"):
            raise OSError("connection reset")
        stored.append(url)
        return "id"

    monkeypatch.setattr(main.boorus, "resolve_search", lambda url, limit: _posts(3))
    monkeypatch.setattr(main.downloads, "assert_safe_url", lambda url: None)
    monkeypatch.setattr(main, "store_remote_video", flaky)

    task_id = main.registry.create("import")
    main.process_import_task(task_id, "https://danbooru.donmai.us/posts?tags=solo",
                             app_env["videos"], 10)

    assert len(stored) == 2
    task = main.registry.get(task_id)
    assert task["status"] == "completed"
    assert "1 failed" in task["detail"]


def test_already_present_posts_are_skipped_without_downloading(client, app_env, monkeypatch):
    import database

    test_client, main = client
    (app_env["videos"] / "a.mp4").write_bytes(b"x")
    database.add_video_to_db({
        "id": "existing",
        "directory": str(app_env["videos"].resolve()),
        "path": "a.mp4",
        "title": "Existing",
        "creation_date": "2024-01-01T00:00:00",
        "source_url": "https://cdn.donmai.us/original/a/b/1.mp4",
    })

    stored = []
    monkeypatch.setattr(main.boorus, "resolve_search", lambda url, limit: _posts(2))
    monkeypatch.setattr(main.downloads, "assert_safe_url", lambda url: None)
    monkeypatch.setattr(
        main, "store_remote_video",
        lambda url, ext, directory, **kwargs: stored.append(url) or "id",
    )

    task_id = main.registry.create("import")
    main.process_import_task(task_id, "https://danbooru.donmai.us/posts?tags=solo",
                             app_env["videos"], 10)

    assert stored == ["https://cdn.donmai.us/original/a/b/2.mp4"]
    assert "1 already here" in main.registry.get(task_id)["detail"]


def test_a_search_matching_nothing_fails_with_a_reason(client, app_env, monkeypatch):
    test_client, main = client
    monkeypatch.setattr(main.boorus, "resolve_search", lambda url, limit: [])

    task_id = main.registry.create("import")
    main.process_import_task(task_id, "https://danbooru.donmai.us/posts?tags=none",
                             app_env["videos"], 10)

    task = main.registry.get(task_id)
    assert task["status"] == "failed"
    assert "no video posts" in task["error"]


def test_a_batch_where_everything_failed_is_reported_as_failed(client, app_env, monkeypatch):
    test_client, main = client

    def always_fails(url, ext, directory, **kwargs):
        raise OSError("nope")

    monkeypatch.setattr(main.boorus, "resolve_search", lambda url, limit: _posts(2))
    monkeypatch.setattr(main.downloads, "assert_safe_url", lambda url: None)
    monkeypatch.setattr(main, "store_remote_video", always_fails)

    task_id = main.registry.create("import")
    main.process_import_task(task_id, "https://danbooru.donmai.us/posts?tags=solo",
                             app_env["videos"], 10)

    assert main.registry.get(task_id)["status"] == "failed"


def test_a_search_error_is_reported_on_the_task(client, app_env, monkeypatch):
    test_client, main = client

    def boom(url, limit):
        raise main.boorus.BooruError("gelbooru's search API needs credentials.")

    monkeypatch.setattr(main.boorus, "resolve_search", boom)

    task_id = main.registry.create("import")
    main.process_import_task(task_id, "https://gelbooru.com/index.php?page=post&s=list&tags=a",
                             app_env["videos"], 10)

    task = main.registry.get(task_id)
    assert task["status"] == "failed"
    assert "credentials" in task["error"]


def test_the_configured_limit_is_bounded(monkeypatch):
    import importlib

    import config

    monkeypatch.setenv("IMPORT_LIMIT", "9999")
    importlib.reload(config)
    assert config.settings.import_limit == 100

    monkeypatch.setenv("IMPORT_LIMIT", "0")
    importlib.reload(config)
    assert config.settings.import_limit == 1

    monkeypatch.delenv("IMPORT_LIMIT")
    importlib.reload(config)
