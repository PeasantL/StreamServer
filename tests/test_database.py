"""Storage: atomicity, locking, and per-directory scoping."""

import json
import threading


def _row(video_id, directory, path, **extra):
    row = {
        "id": video_id,
        "directory": str(directory.resolve()),
        "path": path,
        "title": path,
        "creation_date": "2024-01-01T00:00:00",
        "has_audio": True,
    }
    row.update(extra)
    return row


def test_writes_are_atomic_and_leave_no_temp_files(app_env):
    import database

    database.add_video_to_db(_row("a", app_env["videos"], "a.mp4"))

    db_file = database.settings.db_file
    assert json.loads(db_file.read_text())["videos"][0]["id"] == "a"

    leftovers = [p for p in db_file.parent.iterdir() if p.name.startswith(f".{db_file.name}")]
    assert leftovers == []


def test_concurrent_writes_do_not_lose_updates(app_env):
    """Unlocked read-modify-write dropped one of two simultaneous updates."""
    import database

    def add(index):
        database.add_video_to_db(_row(f"id-{index}", app_env["videos"], f"{index}.mp4"))

    threads = [threading.Thread(target=add, args=(i,)) for i in range(25)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(database.list_videos(app_env["videos"])) == 25


def test_rows_are_scoped_to_their_directory(app_env):
    """A row from one library must not resolve against another's file."""
    import database

    database.add_video_to_db(_row("a", app_env["videos"], "same-name.mp4"))
    database.add_video_to_db(_row("b", app_env["other"], "same-name.mp4"))

    assert database.get_video_by_id("a", app_env["videos"])["id"] == "a"
    assert database.get_video_by_id("a", app_env["other"]) is None
    assert database.get_video_by_id("b", app_env["other"])["id"] == "b"


def test_delete_only_touches_the_named_directory(app_env):
    import database

    database.add_video_to_db(_row("a", app_env["videos"], "clip.mp4"))
    database.add_video_to_db(_row("b", app_env["other"], "clip.mp4"))

    assert database.delete_video_from_db("a", app_env["other"]) is False
    assert database.delete_video_from_db("a", app_env["videos"]) is True
    assert len(database.list_videos(app_env["other"])) == 1


def test_prune_removes_only_rows_whose_file_is_gone(app_env):
    import database

    database.add_video_to_db(_row("present", app_env["videos"], "here.mp4"))
    database.add_video_to_db(_row("absent", app_env["videos"], "gone.mp4"))
    database.add_video_to_db(_row("elsewhere", app_env["other"], "gone.mp4"))

    removed = database.prune_missing(app_env["videos"], {"here.mp4"})

    assert removed == 1
    assert {row["id"] for row in database.list_videos(app_env["videos"])} == {"present"}
    # A different library is untouched even though it has the same filename.
    assert len(database.list_videos(app_env["other"])) == 1


def test_v1_documents_are_migrated(app_env, monkeypatch):
    import database

    legacy = {"videos": [{"id": "old", "path": "clip.mp4", "title": "Clip"}]}
    database.settings.db_file.write_text(json.dumps(legacy))
    database.reset_cache()

    rows = database.list_videos(app_env["videos"])
    assert len(rows) == 1
    assert rows[0]["directory"] == str(app_env["videos"].resolve())
    assert database.load_db()["version"] == database.SCHEMA_VERSION


def test_corrupt_database_does_not_take_the_server_down(app_env):
    import database

    database.settings.db_file.write_text("{not json")
    database.reset_cache()
    assert database.load_db()["videos"] == []


def test_current_dir_is_not_written_to_config(app_env):
    """Switching folders used to rewrite config.json and fight the env var."""
    import database

    database.set_current_dir(app_env["other"])
    assert database.current_dir() == app_env["other"].resolve()
    assert json.loads(database.settings.db_file.read_text())["current_dir"] == str(
        app_env["other"].resolve()
    )
