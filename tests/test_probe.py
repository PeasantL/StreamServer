"""Media probing, formatting and the backfill of pre-existing rows.

ffprobe itself is stubbed: what is under test is how its output is parsed and
what happens when it is absent, malformed or partial.
"""

import json
import subprocess

import pytest

import utils


def _stub_ffprobe(monkeypatch, payload, *, fail=False):
    """Make subprocess.run return *payload* as ffprobe's JSON output."""
    def fake_run(command, **kwargs):
        if fail:
            raise subprocess.CalledProcessError(1, command)
        return subprocess.CompletedProcess(
            command, 0, stdout=json.dumps(payload).encode(), stderr=b""
        )

    monkeypatch.setattr(utils.subprocess, "run", fake_run)


FULL_PROBE = {
    "format": {"duration": "125.5", "size": "1048576"},
    "streams": [
        {"codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080},
        {"codec_type": "audio", "codec_name": "aac"},
    ],
}


def test_a_full_probe_populates_every_field(monkeypatch, tmp_path):
    _stub_ffprobe(monkeypatch, FULL_PROBE)

    info = utils.probe_media(tmp_path / "clip.mp4")

    assert info.has_audio is True
    assert info.duration == 125.5
    assert info.width == 1920
    assert info.height == 1080
    assert info.video_codec == "h264"
    assert info.audio_codec == "aac"
    assert info.size_bytes == 1048576


def test_a_file_with_no_audio_stream_reports_none(monkeypatch, tmp_path):
    _stub_ffprobe(monkeypatch, {
        "format": {"duration": "10"},
        "streams": [{"codec_type": "video", "codec_name": "vp9", "height": 720}],
    })

    info = utils.probe_media(tmp_path / "clip.webm")

    assert info.has_audio is False
    assert info.audio_codec is None
    assert info.video_codec == "vp9"


def test_duration_falls_back_to_the_video_stream(monkeypatch, tmp_path):
    """Some WebM files carry no container-level duration."""
    _stub_ffprobe(monkeypatch, {
        "format": {},
        "streams": [{"codec_type": "video", "codec_name": "vp9", "duration": "42.0"}],
    })

    assert utils.probe_media(tmp_path / "clip.webm").duration == 42.0


def test_a_failed_probe_yields_empty_info_rather_than_raising(monkeypatch, tmp_path):
    """An unreadable file must still be catalogued."""
    _stub_ffprobe(monkeypatch, {}, fail=True)

    info = utils.probe_media(tmp_path / "broken.mp4")

    assert info == utils.MediaInfo()
    assert info.has_audio is False


def test_unparseable_output_yields_empty_info(monkeypatch, tmp_path):
    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, stdout=b"not json", stderr=b"")

    monkeypatch.setattr(utils.subprocess, "run", fake_run)

    assert utils.probe_media(tmp_path / "clip.mp4") == utils.MediaInfo()


def test_non_numeric_values_are_dropped_not_crashed_on(monkeypatch, tmp_path):
    """ffprobe reports unknown numbers as "N/A" for some containers."""
    _stub_ffprobe(monkeypatch, {
        "format": {"duration": "N/A", "size": "N/A"},
        "streams": [{"codec_type": "video", "codec_name": "h264", "width": "N/A"}],
    })

    info = utils.probe_media(tmp_path / "clip.mp4")

    assert info.duration is None
    assert info.size_bytes is None
    assert info.width is None
    assert info.video_codec == "h264"


def test_has_audio_stream_still_works_for_existing_callers(monkeypatch, tmp_path):
    _stub_ffprobe(monkeypatch, FULL_PROBE)

    assert utils.has_audio_stream(tmp_path / "clip.mp4") is True


# --- formatting ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (None, ""),
        (0, "0:00"),
        (9, "0:09"),
        (65, "1:05"),
        (599, "9:59"),
        (3600, "1:00:00"),
        (3725, "1:02:05"),
        (-1, ""),
    ],
)
def test_duration_formatting(seconds, expected):
    assert utils.format_duration(seconds) == expected


@pytest.mark.parametrize(
    ("size", "expected"),
    [
        (None, ""),
        (512, "512 B"),
        (2048, "2.0 KB"),
        (1048576, "1.0 MB"),
        (3221225472, "3.0 GB"),
    ],
)
def test_size_formatting(size, expected):
    assert utils.format_size(size) == expected


@pytest.mark.parametrize(
    ("width", "height", "expected"),
    [
        (None, None, ""),
        (1920, 1080, "1080p"),
        (1280, 720, "720p"),
        # Portrait: the short side is what names the quality tier, so a
        # 1080-wide phone clip is "1080p" even though it stands 1920 tall.
        (1080, 1920, "1080p"),
        (None, 1080, "1080p"),
    ],
)
def test_resolution_formatting(width, height, expected):
    assert utils.format_resolution(width, height) == expected


# --- backfill -----------------------------------------------------------------


def _add_row(app_env, video_id, **extra):
    import database

    (app_env["videos"] / f"{video_id}.mp4").write_bytes(b"data")
    row = {
        "id": video_id,
        "directory": str(app_env["videos"].resolve()),
        "path": f"{video_id}.mp4",
        "title": video_id,
        "creation_date": "2024-01-01T00:00:00",
    }
    row.update(extra)
    database.add_video_to_db(row)
    return row


def test_backfill_probes_rows_that_predate_the_fields(monkeypatch, app_env):
    import database

    _add_row(app_env, "old")
    _stub_ffprobe(monkeypatch, FULL_PROBE)

    assert utils.backfill_media_info(app_env["videos"]) == 1

    row = database.get_video_by_id("old", app_env["videos"])
    assert row["duration"] == 125.5
    assert row["height"] == 1080
    assert row["size_bytes"] == 1048576


def test_backfill_skips_rows_that_already_carry_the_fields(monkeypatch, app_env):
    """The cost is paid once, not on every scan."""
    _add_row(app_env, "fresh", **utils.MediaInfo(duration=5.0).as_row_fields())

    calls = []
    monkeypatch.setattr(utils, "probe_media", lambda path: calls.append(path) or utils.MediaInfo())

    assert utils.backfill_media_info(app_env["videos"]) == 0
    assert calls == []


def test_a_row_whose_probe_found_nothing_is_not_retried(monkeypatch, app_env):
    """An empty MediaInfo still writes the marker field, so it counts as done."""
    _add_row(app_env, "silent")
    _stub_ffprobe(monkeypatch, {}, fail=True)

    assert utils.backfill_media_info(app_env["videos"]) == 1
    assert utils.backfill_media_info(app_env["videos"]) == 0


def test_backfill_ignores_rows_whose_file_is_missing(monkeypatch, app_env):
    import database

    database.add_video_to_db({
        "id": "gone",
        "directory": str(app_env["videos"].resolve()),
        "path": "gone.mp4",
        "title": "gone",
        "creation_date": "2024-01-01T00:00:00",
    })
    _stub_ffprobe(monkeypatch, FULL_PROBE)

    assert utils.backfill_media_info(app_env["videos"]) == 0


# --- sorting ------------------------------------------------------------------


def test_sorting_by_duration_and_size(app_env):
    _add_row(app_env, "short", duration=10.0, size_bytes=100, video_codec="h264")
    _add_row(app_env, "long", duration=900.0, size_bytes=50, video_codec="h264")

    longest = [v["id"] for v in utils.get_video_files(sort_by="longest")]
    largest = [v["id"] for v in utils.get_video_files(sort_by="largest")]

    assert longest == ["long", "short"]
    assert largest == ["short", "long"]


def test_rows_with_no_duration_sort_last(app_env):
    _add_row(app_env, "known", duration=10.0, video_codec="h264")
    _add_row(app_env, "unknown", video_codec="h264")

    assert [v["id"] for v in utils.get_video_files(sort_by="longest")] == ["known", "unknown"]


def test_the_index_accepts_the_new_sort_options(client, app_env):
    test_client, _ = client

    for sort in ("longest", "largest"):
        assert test_client.get(f"/?sort={sort}").status_code == 200
