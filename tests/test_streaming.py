"""End-to-end behaviour of the streaming and metadata endpoints."""


def _register(app_env, video_id="vid", name="clip.mp4", payload=b"0123456789"):
    import database

    (app_env["videos"] / name).write_bytes(payload)
    database.add_video_to_db(
        {
            "id": video_id,
            "directory": str(app_env["videos"].resolve()),
            "path": name,
            "title": "Clip",
            "creation_date": "2024-01-01T00:00:00",
            "has_audio": True,
        }
    )
    return payload


def test_full_request_returns_200_not_206(client, app_env):
    """An un-ranged request used to come back as 206 with a Content-Range."""
    test_client, _ = client
    payload = _register(app_env)

    response = test_client.get("/videos/vid")

    assert response.status_code == 200
    assert response.content == payload
    assert response.headers["content-length"] == str(len(payload))
    assert "content-range" not in response.headers
    assert response.headers["accept-ranges"] == "bytes"


def test_range_request_is_partial_and_exact(client, app_env):
    test_client, _ = client
    _register(app_env)

    response = test_client.get("/videos/vid", headers={"Range": "bytes=2-5"})

    assert response.status_code == 206
    assert response.content == b"2345"
    assert response.headers["content-range"] == "bytes 2-5/10"
    assert response.headers["content-length"] == "4"


def test_oversized_range_is_clamped_to_the_file(client, app_env):
    """Content-Length promised more than the body delivered, hanging clients."""
    test_client, _ = client
    _register(app_env)

    response = test_client.get("/videos/vid", headers={"Range": "bytes=5-999999"})

    assert response.status_code == 206
    assert response.headers["content-range"] == "bytes 5-9/10"
    assert response.headers["content-length"] == "5"
    assert len(response.content) == 5


def test_unsatisfiable_range_returns_416(client, app_env):
    test_client, _ = client
    _register(app_env)

    response = test_client.get("/videos/vid", headers={"Range": "bytes=100-200"})

    assert response.status_code == 416
    assert response.headers["content-range"] == "bytes */10"


def test_missing_video_is_404(client, app_env):
    test_client, _ = client
    assert test_client.get("/videos/nope").status_code == 404


def test_player_page_shows_the_title(client, app_env):
    """The header rendered {{ video_name }} while the route passed video_title."""
    test_client, _ = client
    _register(app_env)

    response = test_client.get("/play/vid")

    assert response.status_code == 200
    assert "Clip" in response.text


def test_folder_names_cannot_break_out_of_the_page(client, app_env):
    """A folder named with a quote used to escape an inline onclick handler.

    HTML escaping turns `\'` into `&#39;`, but the HTML parser decodes entities
    in an attribute value before the JavaScript parser sees it, so escaping
    alone never made `onclick="selectFolder(\'...\')"` safe.
    """
    test_client, _ = client
    hostile = "'); alert(1);"
    (app_env["parent"] / hostile).mkdir()

    response = test_client.get("/")

    assert response.status_code == 200
    # The name reaches the page as escaped data, not as code...
    assert 'data-folder="&#39;); alert(1);"' in response.text
    # ...and no inline handler is generated at all.
    assert "onclick" not in response.text
    assert f"selectFolder('{hostile}" not in response.text


def test_delete_removes_file_thumbnail_and_archived_original(client, app_env):
    test_client, _ = client
    _register(app_env)

    thumbnail = app_env["thumbnails"] / "vid.jpg"
    thumbnail.write_bytes(b"jpeg")

    archive = app_env["videos"] / "original_webm"
    archive.mkdir()
    original = archive / "vid_original.webm"
    original.write_bytes(b"webm")

    import database

    database.update_video_in_db("vid", {"original_webm": "vid_original.webm"})

    response = test_client.delete("/api/videos/vid")

    assert response.status_code == 200
    assert not (app_env["videos"] / "clip.mp4").exists()
    assert not thumbnail.exists()
    # The archived original is the biggest of the three and used to be orphaned.
    assert not original.exists()
    assert database.get_video_by_id("vid") is None


def test_thumbnail_endpoint_rejects_a_malformed_timecode(client, app_env):
    test_client, _ = client
    _register(app_env)

    response = test_client.post("/api/videos/vid/thumbnail?time=not-a-time")

    assert response.status_code == 400


def test_sort_is_a_query_parameter(client, app_env):
    test_client, _ = client
    import database

    for index, title in enumerate(["Zebra", "Apple"]):
        (app_env["videos"] / f"{index}.mp4").write_bytes(b"x")
        database.add_video_to_db(
            {
                "id": f"v{index}",
                "directory": str(app_env["videos"].resolve()),
                "path": f"{index}.mp4",
                "title": title,
                "creation_date": f"2024-01-0{index + 1}T00:00:00",
                "has_audio": True,
            }
        )

    # Apple is the newer of the two; one view of Zebra flips the other ordering.
    database.record_view("v0")

    by_views = test_client.get("/?sort=most_viewed").text
    assert by_views.index("Zebra") < by_views.index("Apple")

    by_newest = test_client.get("/?sort=newest").text
    assert by_newest.index("Apple") < by_newest.index("Zebra")

    # An unknown value falls back rather than erroring.
    assert test_client.get("/?sort=bogus").status_code == 200


def test_download_rejects_internal_and_non_media_urls(client, app_env):
    test_client, _ = client

    for url in (
        "http://127.0.0.1:8080/clip.mp4",
        "file:///etc/passwd",
        "https://example.com/index.html",
    ):
        response = test_client.post("/api/download", json={"url": url})
        assert response.status_code == 400, url


def test_unknown_task_is_404(client, app_env):
    test_client, _ = client
    assert test_client.get("/api/task-status/does-not-exist").status_code == 404


def test_download_accepts_a_booru_post_page(client, app_env, monkeypatch):
    """A pasted post page is resolved to its original file before queueing."""
    import boorus
    import downloads

    test_client, app_module = client
    monkeypatch.setattr(downloads, "assert_safe_url", lambda url: None)
    monkeypatch.setattr(
        boorus,
        "resolve_post",
        lambda url: boorus.Post(
            site="gelbooru",
            post_id="42",
            page_url=url,
            file_url="https://img3.gelbooru.com/images/ab/cd/abcd.mp4",
        ),
    )

    queued = []
    monkeypatch.setattr(
        app_module, "process_download_task", lambda *args, **kwargs: queued.append((args, kwargs))
    )

    response = test_client.post(
        "/api/download",
        json={"url": "https://gelbooru.com/index.php?page=post&s=view&id=42"},
    )

    assert response.status_code == 200
    assert "task_id" in response.json()
    (args, kwargs) = queued[0]
    assert args[1] == "https://img3.gelbooru.com/images/ab/cd/abcd.mp4"
    assert args[2] == ".mp4"
    assert kwargs["title"] == "gelbooru 42"
    assert kwargs["page_url"].endswith("id=42")


def test_download_explains_that_a_booru_post_is_not_a_video(client, app_env, monkeypatch):
    """The catalogue only stores .mp4 and .webm, so an image post is refused
    up front rather than after the progress bar has run."""
    import boorus

    test_client, _ = client
    monkeypatch.setattr(
        boorus,
        "resolve_post",
        lambda url: boorus.Post(
            site="rule34.us",
            post_id="42",
            page_url=url,
            file_url="https://img.rule34.us/images/ab/cd/abcd.png",
        ),
    )

    response = test_client.post(
        "/api/download", json={"url": "https://rule34.us/index.php?r=posts/view&id=42"}
    )

    assert response.status_code == 400
    assert ".png" in response.json()["detail"]


def test_a_failed_booru_resolution_is_a_400_with_the_reason(client, app_env):
    test_client, _ = client
    response = test_client.post(
        "/api/download", json={"url": "https://gelbooru.com/index.php?page=post&s=list"}
    )
    assert response.status_code == 400
    assert "not a single post" in response.json()["detail"]
