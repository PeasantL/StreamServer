"""Paging the catalogue grid."""

import pytest

import utils


@pytest.fixture
def small_pages(monkeypatch):
    """Shrink the page size before the config reload.

    `Settings` is frozen, so the value cannot be patched onto the resolved
    object; it has to be in the environment before `app_env` rebuilds it.
    Requesting this fixture ahead of `client` is what orders the two.
    """
    monkeypatch.setenv("PAGE_SIZE", "10")


@pytest.fixture
def paged_client(small_pages, client):
    return client


@pytest.fixture
def many_videos(app_env):
    """Twenty-five videos, newest first by construction."""
    import database

    for index in range(25):
        name = f"v{index:02d}.mp4"
        (app_env["videos"] / name).write_bytes(b"data")
        database.add_video_to_db({
            "id": f"v{index:02d}",
            "directory": str(app_env["videos"].resolve()),
            "path": name,
            "title": f"Video {index:02d}",
            "tags": ["even"] if index % 2 == 0 else ["odd"],
            "creation_date": f"2024-01-{index + 1:02d}T00:00:00",
            "video_codec": "h264",
        })
    return app_env


def test_a_page_holds_at_most_page_size_videos(many_videos):
    result = utils.browse_videos(page_size=10)

    assert len(result.videos) == 10
    assert result.total == 25
    assert result.pages == 3


def test_the_last_page_holds_the_remainder(many_videos):
    result = utils.browse_videos(page=3, page_size=10)

    assert len(result.videos) == 5
    assert result.has_next is False
    assert result.has_previous is True


def test_pages_do_not_overlap_or_drop_videos(many_videos):
    seen = []
    for number in (1, 2, 3):
        seen.extend(v["id"] for v in utils.browse_videos(page=number, page_size=10).videos)

    assert len(seen) == len(set(seen)) == 25


def test_paging_preserves_the_sort_order(many_videos):
    """Page 1 of a most-viewed sort must hold the ten most watched videos."""
    import database

    # v24 watched most, v15 least, so the top ten are v24 down to v15.
    for index in range(15, 25):
        for _ in range(index):
            database.record_view(f"v{index:02d}")

    page = utils.browse_videos(sort_by="most_viewed", page=1, page_size=10)
    first = [v["id"] for v in page.videos]

    assert first == [f"v{index:02d}" for index in range(24, 14, -1)]


def test_a_page_beyond_the_end_clamps_to_the_last(many_videos):
    """A bookmarked page survives the library shrinking under it."""
    result = utils.browse_videos(page=99, page_size=10)

    assert result.page == 3
    assert len(result.videos) == 5


def test_a_page_below_one_clamps_up(many_videos):
    assert utils.browse_videos(page=0, page_size=10).page == 1


def test_the_total_reflects_the_filter_not_the_library(many_videos):
    result = utils.browse_videos(tags=["even"], page_size=10)

    assert result.total == 13
    assert result.pages == 2


def test_an_empty_result_is_one_empty_page(app_env):
    result = utils.browse_videos(page_size=10)

    assert result.videos == []
    assert result.total == 0
    assert result.pages == 1
    assert result.first_index == 0
    assert result.last_index == 0
    assert result.has_next is False


def test_page_size_zero_disables_paging(many_videos):
    result = utils.browse_videos(page_size=0)

    assert len(result.videos) == 25
    assert result.pages == 1


def test_get_video_files_still_returns_everything_unpaged(many_videos):
    """The unpaged helper the rest of the code uses must ignore page_size."""
    assert len(utils.get_video_files()) == 25


def test_the_displayed_range_matches_the_page(many_videos):
    result = utils.browse_videos(page=2, page_size=10)

    assert (result.first_index, result.last_index) == (11, 20)


def test_a_row_whose_file_is_missing_is_not_counted(many_videos, app_env):
    """A total that counted missing files would page to empty screens."""
    (app_env["videos"] / "v00.mp4").unlink()

    assert utils.browse_videos(page_size=10).total == 24


# --- through the HTTP layer ---------------------------------------------------


def test_the_index_renders_pagination_controls(paged_client, many_videos):
    test_client, _ = paged_client

    response = test_client.get("/")

    assert response.status_code == 200
    assert "Next" in response.text
    assert "of 25" in response.text


def test_pagination_links_carry_the_active_filter(paged_client, many_videos):
    test_client, _ = paged_client

    response = test_client.get("/?tag=even&sort=most_viewed")

    assert "tag=even" in response.text
    assert "sort=most_viewed" in response.text


def test_thumbnails_are_lazily_loaded(client, app_env):
    """A large library must not fetch every thumbnail on first paint."""
    import database

    (app_env["videos"] / "clip.mp4").write_bytes(b"data")
    (app_env["thumbnails"] / "vid.jpg").write_bytes(b"jpeg")
    database.add_video_to_db({
        "id": "vid",
        "directory": str(app_env["videos"].resolve()),
        "path": "clip.mp4",
        "title": "Clip",
        "creation_date": "2024-01-01T00:00:00",
    })
    test_client, _ = client

    response = test_client.get("/")

    assert 'loading="lazy"' in response.text


def test_a_negative_page_is_rejected_by_validation(client, many_videos):
    test_client, _ = client

    assert test_client.get("/?page=-1").status_code == 422


def test_a_single_page_renders_no_controls(client, many_videos):
    """The default page size comfortably holds all 25."""
    test_client, _ = client

    response = test_client.get("/")

    assert "Catalogue pages" not in response.text
