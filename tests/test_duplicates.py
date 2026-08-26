"""Recognising a video that is already catalogued.

The same file arrives twice in three ways: the same direct URL, the same booru
post, or two different URLs serving byte-identical content.
"""

import importlib

import pytest

import config
import utils


@pytest.fixture
def reload_config():
    """Rebuild `config.settings` from the environment, then put it back.

    `Settings` is frozen and resolved at import time, so a test that wants a
    different value has to reload the module -- and has to undo that, or the
    stale object leaks into every test that follows.
    """
    def reload():
        importlib.reload(config)
        return config.settings

    yield reload
    importlib.reload(config)


def _add(app_env, video_id, *, name=None, payload=b"video-bytes", **extra):
    import database

    name = name or f"{video_id}.mp4"
    (app_env["videos"] / name).write_bytes(payload)
    row = {
        "id": video_id,
        "directory": str(app_env["videos"].resolve()),
        "path": name,
        "title": f"Title {video_id}",
        "creation_date": "2024-01-01T00:00:00",
        "video_codec": "h264",
    }
    row.update(extra)
    database.add_video_to_db(row)
    return row


# --- hashing -------------------------------------------------------------------


def test_identical_files_hash_identically(tmp_path):
    (tmp_path / "a").write_bytes(b"x" * 5000)
    (tmp_path / "b").write_bytes(b"x" * 5000)

    assert utils.file_digest(tmp_path / "a") == utils.file_digest(tmp_path / "b")


def test_different_files_hash_differently(tmp_path):
    (tmp_path / "a").write_bytes(b"x" * 5000)
    (tmp_path / "b").write_bytes(b"y" * 5000)

    assert utils.file_digest(tmp_path / "a") != utils.file_digest(tmp_path / "b")


def test_a_file_larger_than_one_chunk_hashes_correctly(tmp_path):
    """The chunked read must not truncate at the buffer boundary."""
    import hashlib

    payload = b"z" * (utils.DIGEST_CHUNK_SIZE * 2 + 17)
    (tmp_path / "big").write_bytes(payload)

    assert utils.file_digest(tmp_path / "big") == hashlib.sha256(payload).hexdigest()


def test_an_unreadable_file_hashes_to_none(tmp_path):
    assert utils.file_digest(tmp_path / "missing") is None


# --- database lookup -------------------------------------------------------------


def test_a_matching_source_url_is_found(app_env):
    import database

    _add(app_env, "a", source_url="https://example.com/clip.mp4")

    found = database.find_duplicate(app_env["videos"], source_url="https://example.com/clip.mp4")

    assert found["id"] == "a"


def test_a_matching_booru_post_is_found(app_env):
    """A booru re-upload has a new file URL but the same post page."""
    import database

    post = "https://gelbooru.com/index.php?page=post&s=view&id=42"
    _add(app_env, "a", description=post)

    assert database.find_duplicate(app_env["videos"], page_url=post)["id"] == "a"


def test_a_matching_content_hash_is_found(app_env):
    import database

    _add(app_env, "a", source_hash="abc123")

    assert database.find_duplicate(app_env["videos"], source_hash="abc123")["id"] == "a"


def test_a_non_match_returns_none(app_env):
    import database

    _add(app_env, "a", source_url="https://example.com/one.mp4")

    other = "https://example.com/two.mp4"
    assert database.find_duplicate(app_env["videos"], source_url=other) is None


def test_no_identity_given_matches_nothing(app_env):
    """An absent field must not match a row that also lacks it."""
    import database

    _add(app_env, "a")

    assert database.find_duplicate(app_env["videos"]) is None
    assert database.find_duplicate(app_env["videos"], source_url=None) is None


def test_duplicates_do_not_cross_directories(app_env):
    """Two libraries may legitimately hold the same video."""
    import database

    _add(app_env, "a", source_url="https://example.com/clip.mp4")

    same = "https://example.com/clip.mp4"
    assert database.find_duplicate(app_env["other"], source_url=same) is None


# --- on-disk duplicate groups ------------------------------------------------------


def test_byte_identical_files_are_grouped(app_env):
    _add(app_env, "a", payload=b"same", size_bytes=4)
    _add(app_env, "b", payload=b"same", size_bytes=4)

    groups = utils.find_duplicate_groups(app_env["videos"])

    assert len(groups) == 1
    assert {row["id"] for row in groups[0]} == {"a", "b"}


def test_files_of_the_same_size_but_different_content_are_not_grouped(app_env):
    _add(app_env, "a", payload=b"aaaa", size_bytes=4)
    _add(app_env, "b", payload=b"bbbb", size_bytes=4)

    assert utils.find_duplicate_groups(app_env["videos"]) == []


def test_files_of_different_sizes_are_never_hashed(app_env, monkeypatch):
    """Size is compared first so a whole library is not read on every scan."""
    _add(app_env, "a", payload=b"aaaa", size_bytes=4)
    _add(app_env, "b", payload=b"bbbbbb", size_bytes=6)

    calls = []
    monkeypatch.setattr(utils, "file_digest", lambda path: calls.append(path) or "x")

    assert utils.find_duplicate_groups(app_env["videos"]) == []
    assert calls == []


def test_the_computed_hash_is_stored_on_the_row(app_env):
    """So the next scan compares hashes rather than re-reading the files."""
    import database

    _add(app_env, "a", payload=b"same", size_bytes=4)
    _add(app_env, "b", payload=b"same", size_bytes=4)

    utils.find_duplicate_groups(app_env["videos"])

    assert database.get_video_by_id("a", app_env["videos"])["content_hash"]


def test_a_row_whose_file_is_gone_is_ignored(app_env):
    import database

    _add(app_env, "a", payload=b"same", size_bytes=4)
    database.add_video_to_db({
        "id": "gone",
        "directory": str(app_env["videos"].resolve()),
        "path": "gone.mp4",
        "title": "gone",
        "size_bytes": 4,
        "creation_date": "2024-01-01T00:00:00",
    })

    assert utils.find_duplicate_groups(app_env["videos"]) == []


# --- through the download endpoint --------------------------------------------------


def test_a_repeated_direct_url_is_refused_before_downloading(client, app_env, monkeypatch):
    test_client, main = client
    _add(app_env, "a", source_url="https://example.com/clip.mp4")
    monkeypatch.setattr(main.downloads, "assert_safe_url", lambda url: None)

    response = test_client.post("/api/download", json={"url": "https://example.com/clip.mp4"})

    assert response.status_code == 409
    assert "Title a" in response.json()["detail"]


def test_a_repeated_booru_post_is_refused(client, app_env, monkeypatch):
    test_client, main = client
    post = "https://gelbooru.com/index.php?page=post&s=view&id=42"
    _add(app_env, "a", description=post)

    monkeypatch.setattr(main.downloads, "assert_safe_url", lambda url: None)
    monkeypatch.setattr(
        main.boorus, "resolve_post",
        lambda url: main.boorus.Post(
            site="gelbooru", post_id="42", page_url=post,
            file_url="https://img3.gelbooru.com/images/ab/cd/new.mp4",
        ),
    )

    response = test_client.post("/api/download", json={"url": post})

    assert response.status_code == 409


def test_a_new_url_is_accepted(client, app_env, monkeypatch):
    test_client, main = client
    _add(app_env, "a", source_url="https://example.com/one.mp4")
    monkeypatch.setattr(main.downloads, "assert_safe_url", lambda url: None)

    response = test_client.post("/api/download", json={"url": "https://example.com/two.mp4"})

    assert response.status_code == 200
    assert "task_id" in response.json()


def test_allow_duplicates_disables_the_check(app_env, monkeypatch, reload_config):
    """The opt-out for anyone who wants the old behaviour back."""
    import database

    monkeypatch.setenv("ALLOW_DUPLICATES", "true")

    assert reload_config().allow_duplicates is True
    # The lookup itself still works; it is the endpoint that consults the flag.
    _add(app_env, "a", source_url="https://example.com/clip.mp4")
    assert database.find_duplicate(
        app_env["videos"], source_url="https://example.com/clip.mp4"
    ) is not None


@pytest.mark.parametrize("value", ["1", "yes", "on", "true"])
def test_the_flag_accepts_the_usual_truthy_spellings(monkeypatch, value, reload_config):
    monkeypatch.setenv("ALLOW_DUPLICATES", value)

    assert reload_config().allow_duplicates is True
