"""Searching and tag filtering on the catalogue view."""

import pytest

import utils


@pytest.fixture
def library(app_env):
    """Three videos with overlapping tags to filter between."""
    import database

    rows = [
        {
            "id": "a",
            "path": "a.mp4",
            "title": "Sunset over the bay",
            "description": "https://gelbooru.com/index.php?page=post&s=view&id=1",
            "tags": ["scenery", "blue_hair"],
            "creation_date": "2024-03-01T00:00:00",
        },
        {
            "id": "b",
            "path": "b.mp4",
            "title": "Morning run",
            "description": "shot on a phone",
            "tags": ["scenery", "outdoors"],
            "creation_date": "2024-02-01T00:00:00",
        },
        {
            "id": "c",
            "path": "c.mp4",
            "title": "Kitchen timelapse",
            "description": "",
            "tags": ["indoors"],
            "creation_date": "2024-01-01T00:00:00",
        },
    ]
    for row in rows:
        (app_env["videos"] / row["path"]).write_bytes(b"data")
        database.add_video_to_db({**row, "directory": str(app_env["videos"].resolve())})
    return app_env


def _ids(sort_by="newest", **kwargs):
    return [video["id"] for video in utils.get_video_files(sort_by=sort_by, **kwargs)]


def test_no_filter_returns_everything(library):
    assert set(_ids()) == {"a", "b", "c"}


def test_query_matches_the_title_case_insensitively(library):
    assert _ids(query="SUNSET") == ["a"]


def test_query_matches_the_description(library):
    assert _ids(query="on a phone") == ["b"]


def test_query_matches_a_tag_as_a_substring(library):
    """Searching "hair" should find blue_hair without the exact tag."""
    assert _ids(query="hair") == ["a"]


def test_query_that_matches_nothing_returns_nothing(library):
    assert _ids(query="zzzz") == []


def test_surrounding_whitespace_in_a_query_is_ignored(library):
    assert _ids(query="   sunset   ") == ["a"]


def test_a_single_tag_filters_the_grid(library):
    assert set(_ids(tags=["scenery"])) == {"a", "b"}


def test_multiple_tags_narrow_rather_than_widen(library):
    """Conjunctive: a second tag must reduce the result set, not grow it."""
    assert _ids(tags=["scenery", "outdoors"]) == ["b"]
    assert _ids(tags=["scenery", "indoors"]) == []


def test_tag_matching_is_case_insensitive(library):
    assert set(_ids(tags=["SCENERY"])) == {"a", "b"}


def test_tag_matching_is_exact_not_substring(library):
    """A tag filter comes from a chip the server offered, so it must be exact."""
    assert _ids(tags=["scene"]) == []


def test_a_query_and_a_tag_apply_together(library):
    assert _ids(query="morning", tags=["scenery"]) == ["b"]
    assert _ids(query="sunset", tags=["outdoors"]) == []


def test_filtering_still_respects_the_sort_order(library):
    assert _ids(sort_by="title", tags=["scenery"]) == ["b", "a"]
    assert _ids(sort_by="newest", tags=["scenery"]) == ["a", "b"]


def test_collect_tags_counts_and_orders_by_use(library):
    assert utils.collect_tags() == [
        ("scenery", 2),
        ("blue_hair", 1),
        ("indoors", 1),
        ("outdoors", 1),
    ]


def test_a_row_with_no_tags_is_excluded_by_any_tag_filter(app_env):
    import database

    (app_env["videos"] / "x.mp4").write_bytes(b"data")
    database.add_video_to_db({
        "id": "x",
        "directory": str(app_env["videos"].resolve()),
        "path": "x.mp4",
        "title": "Untagged",
        "creation_date": "2024-01-01T00:00:00",
    })

    assert _ids(tags=["anything"]) == []
    assert _ids(query="untagged") == ["x"]


# --- through the HTTP layer ---------------------------------------------------


def test_the_index_applies_the_query_parameter(client, library):
    test_client, _ = client

    response = test_client.get("/?q=sunset")

    assert response.status_code == 200
    assert "Sunset over the bay" in response.text
    assert "Morning run" not in response.text


def test_the_index_applies_repeated_tag_parameters(client, library):
    test_client, _ = client

    response = test_client.get("/?tag=scenery&tag=outdoors")

    assert "Morning run" in response.text
    assert "Sunset over the bay" not in response.text


def test_an_empty_result_says_so_distinctly(client, library):
    """"No matches" and "no videos at all" are different situations."""
    test_client, _ = client

    response = test_client.get("/?q=zzzz")

    assert "Nothing matches that search" in response.text


def test_an_unfiltered_empty_folder_keeps_the_original_message(client, app_env):
    test_client, _ = client

    response = test_client.get("/")

    assert "No videos in this folder yet" in response.text


def test_blank_and_duplicate_tags_are_discarded(client, library):
    test_client, _ = client

    response = test_client.get("/?tag=scenery&tag=+&tag=SCENERY")

    assert response.status_code == 200
    # Deduplication leaves one filter, so both scenery videos survive.
    assert "Sunset over the bay" in response.text
    assert "Morning run" in response.text


def test_an_overlong_query_is_rejected(client, library):
    test_client, _ = client

    assert test_client.get(f"/?q={'x' * 500}").status_code == 422
