"""View counting: what the player records and how the grid orders by it."""

import pytest

import utils


@pytest.fixture
def library(app_env):
    """Three videos, none of them viewed yet."""
    import database

    rows = [
        {"id": "a", "path": "a.mp4", "title": "A", "creation_date": "2024-03-01T00:00:00"},
        {"id": "b", "path": "b.mp4", "title": "B", "creation_date": "2024-02-01T00:00:00"},
        {"id": "c", "path": "c.mp4", "title": "C", "creation_date": "2024-01-01T00:00:00"},
    ]
    for row in rows:
        (app_env["videos"] / row["path"]).write_bytes(b"data")
        database.add_video_to_db({**row, "directory": str(app_env["videos"].resolve())})
    return app_env


# --- the counter --------------------------------------------------------------


def test_a_first_view_starts_a_row_that_predates_the_field_at_one(library):
    """No migration: rows without the field begin counting from their first view."""
    import database

    assert "view_count" not in database.get_video_by_id("a")
    assert database.record_view("a") == 1


def test_views_accumulate(library):
    import database

    for _ in range(3):
        database.record_view("a")

    assert database.get_video_by_id("a")["view_count"] == 3


def test_a_view_of_one_video_leaves_the_others_alone(library):
    import database

    database.record_view("a")

    assert database.get_video_by_id("b").get("view_count") is None


def test_recording_a_view_of_an_unknown_id_reports_it_rather_than_raising(library):
    import database

    assert database.record_view("no-such-video") is None


def test_a_view_survives_a_reload_from_disk(library):
    """The count is persisted, not just held in the cached document."""
    import database

    database.record_view("a")
    database.reset_cache()

    assert database.get_video_by_id("a")["view_count"] == 1


# --- the label ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("count", "expected"),
    [(None, ""), (0, ""), (1, "1 view"), (2, "2 views"), (57, "57 views")],
)
def test_view_formatting(count, expected):
    assert utils.format_views(count) == expected


def test_an_unviewed_video_carries_no_badge(library):
    video = utils.browse_videos().videos[0]

    assert video["view_count"] == 0
    assert video["views_label"] == ""


# --- the ordering -------------------------------------------------------------


def _ids(sort_by):
    return [video["id"] for video in utils.browse_videos(sort_by=sort_by).videos]


def test_most_viewed_puts_the_most_watched_first(library):
    import database

    database.record_view("c")
    database.record_view("c")
    database.record_view("b")

    assert _ids("most_viewed") == ["c", "b", "a"]


def test_unviewed_videos_sort_to_the_end_not_the_front(library):
    """A row with no `view_count` at all must not outrank a watched one."""
    import database

    database.record_view("c")

    assert _ids("most_viewed")[0] == "c"


# --- through the HTTP layer ---------------------------------------------------


def test_opening_the_player_counts_a_view(client, library):
    import database

    test_client, _ = client

    assert test_client.get("/play/a").status_code == 200
    assert database.get_video_by_id("a")["view_count"] == 1


def test_the_grid_shows_the_view_badge(client, library):
    import database

    database.record_view("a")
    test_client, _ = client

    assert "1 view" in test_client.get("/").text


def test_the_most_viewed_sort_is_accepted_by_the_index(client, library):
    """An unknown sort silently falls back to newest, so this proves it is known."""
    test_client, _ = client

    response = test_client.get("/?sort=most_viewed")

    assert response.status_code == 200
    assert '<option value="most_viewed" selected>' in response.text


def test_a_missing_video_is_not_counted(client, library):
    test_client, _ = client

    assert test_client.get("/play/no-such-video").status_code == 404
