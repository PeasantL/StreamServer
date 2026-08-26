"""Partial metadata updates.

The endpoint used to require every field, so the rename button -- which only
has a title -- sent an empty description and tag list with it and destroyed
both. These tests pin the field-by-field semantics that replaced that.
"""

import pytest


def _register(app_env, video_id="vid", **extra):
    import database

    (app_env["videos"] / "clip.mp4").write_bytes(b"0123456789")
    row = {
        "id": video_id,
        "directory": str(app_env["videos"].resolve()),
        "path": "clip.mp4",
        "title": "Original title",
        "description": "https://gelbooru.com/index.php?page=post&s=view&id=42",
        "tags": ["blue", "animated"],
        "creation_date": "2024-01-01T00:00:00",
        "has_audio": True,
    }
    row.update(extra)
    database.add_video_to_db(row)
    return row


def test_a_title_only_update_keeps_description_and_tags(client, app_env):
    """The regression the rename button used to cause on every use."""
    test_client, _ = client
    original = _register(app_env)

    response = test_client.post("/api/videos/vid/update", json={"title": "Renamed"})

    assert response.status_code == 200
    video = response.json()["video"]
    assert video["title"] == "Renamed"
    assert video["description"] == original["description"]
    assert video["tags"] == original["tags"]


def test_tags_can_be_replaced_without_touching_the_title(client, app_env):
    test_client, _ = client
    _register(app_env)

    response = test_client.post("/api/videos/vid/update", json={"tags": ["red"]})

    assert response.status_code == 200
    video = response.json()["video"]
    assert video["tags"] == ["red"]
    assert video["title"] == "Original title"


def test_an_explicit_empty_list_still_clears_tags(client, app_env):
    """Omitting a field means "leave it"; sending an empty one means "clear it"."""
    test_client, _ = client
    _register(app_env)

    response = test_client.post("/api/videos/vid/update", json={"tags": []})

    assert response.status_code == 200
    assert response.json()["video"]["tags"] == []


def test_tags_are_trimmed_lowercased_and_deduplicated(client, app_env):
    test_client, _ = client
    _register(app_env)

    response = test_client.post(
        "/api/videos/vid/update",
        json={"tags": ["  Blue  ", "BLUE", "green", "", "   "]},
    )

    assert response.status_code == 200
    assert response.json()["video"]["tags"] == ["blue", "green"]


def test_an_empty_payload_is_rejected(client, app_env):
    test_client, _ = client
    _register(app_env)

    response = test_client.post("/api/videos/vid/update", json={})

    assert response.status_code == 400


def test_an_empty_title_is_still_rejected(client, app_env):
    test_client, _ = client
    _register(app_env)

    response = test_client.post("/api/videos/vid/update", json={"title": ""})

    assert response.status_code == 422


def test_an_overlong_tag_is_rejected(client, app_env):
    test_client, _ = client
    _register(app_env)

    response = test_client.post("/api/videos/vid/update", json={"tags": ["x" * 200]})

    assert response.status_code == 422


def test_updating_a_missing_video_is_404(client, app_env):
    test_client, _ = client

    response = test_client.post("/api/videos/nope/update", json={"title": "x"})

    assert response.status_code == 404


@pytest.mark.parametrize("field", ["id", "directory"])
def test_identity_fields_cannot_be_rewritten(client, app_env, field):
    """They identify the row; the database layer drops them from any update."""
    test_client, _ = client
    original = _register(app_env)

    test_client.post("/api/videos/vid/update", json={"title": "Renamed"})

    import database

    video = database.get_video_by_id("vid")
    assert video[field] == original[field]
