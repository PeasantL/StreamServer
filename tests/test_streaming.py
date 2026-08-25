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

    by_title = test_client.get("/?sort=title").text
    assert by_title.index("Apple") < by_title.index("Zebra")

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
