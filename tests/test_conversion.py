"""Choosing between remuxing and re-encoding, and which containers convert.

ffmpeg is never actually run: the tests assert on the argument list that would
be handed to it, which is where the remux decision lives.
"""

import pathlib
import subprocess

import pytest

import utils


@pytest.fixture
def recorded(monkeypatch):
    """Capture the ffmpeg command instead of running it, and fake its output."""
    commands = []

    def fake_run(command, **kwargs):
        commands.append(command)
        # convert_to_mp4 renames the temp file into place afterwards.
        if command and command[0] == "ffmpeg":
            pathlib.Path(command[-1]).write_bytes(b"converted")
        return subprocess.CompletedProcess(command, 0, stdout=b"{}", stderr=b"")

    monkeypatch.setattr(utils.subprocess, "run", fake_run)
    return commands


def _convert(recorded, tmp_path, info):
    source = tmp_path / "in.mkv"
    source.write_bytes(b"data")
    utils.convert_to_mp4(source, tmp_path / "out.mp4", info=info)
    return recorded[-1]


# --- the plan ------------------------------------------------------------------


def test_h264_and_aac_are_copied_not_re_encoded():
    info = utils.MediaInfo(video_codec="h264", audio_codec="aac", has_audio=True)

    args, encoding = utils.plan_conversion(info)

    assert args == ["-c:v", "copy", "-c:a", "copy"]
    assert encoding is False


def test_h264_with_opus_audio_copies_the_video_and_re_encodes_the_audio():
    """The case that turns a long transcode into a short one."""
    info = utils.MediaInfo(video_codec="h264", audio_codec="opus", has_audio=True)

    args, encoding = utils.plan_conversion(info)

    assert args == ["-c:v", "copy", "-c:a", "aac"]
    assert encoding is True


def test_vp9_video_is_re_encoded():
    info = utils.MediaInfo(video_codec="vp9", audio_codec="aac", has_audio=True)

    args, _ = utils.plan_conversion(info)

    assert args[:2] == ["-c:v", "libx264"]
    assert args[2:] == ["-c:a", "copy"]


def test_a_silent_file_gets_no_audio_arguments():
    info = utils.MediaInfo(video_codec="h264", has_audio=False)

    args, encoding = utils.plan_conversion(info)

    assert args == ["-c:v", "copy"]
    assert encoding is False


def test_an_unreadable_probe_falls_back_to_re_encoding():
    """A copy of a stream MP4 cannot hold fails silently at playback time."""
    args, encoding = utils.plan_conversion(utils.MediaInfo())

    assert args == ["-c:v", "libx264"]
    assert encoding is True


def test_the_codec_name_is_matched_case_insensitively():
    info = utils.MediaInfo(video_codec="H264", audio_codec="AAC", has_audio=True)

    args, _ = utils.plan_conversion(info)

    assert args == ["-c:v", "copy", "-c:a", "copy"]


# --- the command ---------------------------------------------------------------


def test_a_remux_command_copies_both_streams(recorded, tmp_path):
    command = _convert(
        recorded, tmp_path,
        utils.MediaInfo(video_codec="h264", audio_codec="aac", has_audio=True),
    )

    assert "-c:v" in command
    assert command[command.index("-c:v") + 1] == "copy"
    assert "libx264" not in command


def test_faststart_is_always_applied(recorded, tmp_path):
    """Playback must be able to start before the whole file has arrived."""
    command = _convert(recorded, tmp_path, utils.MediaInfo(video_codec="h264"))

    assert "-movflags" in command
    assert command[command.index("-movflags") + 1] == "+faststart"


def test_the_output_is_moved_into_place_only_on_success(recorded, tmp_path):
    source = tmp_path / "in.mkv"
    source.write_bytes(b"data")
    destination = tmp_path / "out.mp4"

    utils.convert_to_mp4(source, destination, info=utils.MediaInfo(video_codec="h264"))

    assert destination.read_bytes() == b"converted"
    assert not list(tmp_path.glob(".*partial*"))


def test_a_failed_conversion_leaves_no_partial_file(monkeypatch, tmp_path):
    def fake_run(command, **kwargs):
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(utils.subprocess, "run", fake_run)
    source = tmp_path / "in.mkv"
    source.write_bytes(b"data")

    with pytest.raises(subprocess.CalledProcessError):
        utils.convert_to_mp4(source, tmp_path / "out.mp4", info=utils.MediaInfo())

    assert not (tmp_path / "out.mp4").exists()
    assert not list(tmp_path.glob(".*partial*"))


def test_conversion_probes_the_source_when_not_told_about_it(monkeypatch, recorded, tmp_path):
    calls = []
    monkeypatch.setattr(
        utils, "probe_media",
        lambda path: calls.append(path) or utils.MediaInfo(video_codec="h264"),
    )
    source = tmp_path / "in.mkv"
    source.write_bytes(b"data")

    utils.convert_to_mp4(source, tmp_path / "out.mp4")

    assert calls == [source]


# --- hardware acceleration ------------------------------------------------------


def test_hwaccel_is_skipped_on_a_stream_copy(recorded, tmp_path, monkeypatch):
    """Hardware decode buys nothing when no frames are being re-encoded."""
    monkeypatch.setattr(utils, "settings", utils.settings.__class__(
        **{**utils.settings.__dict__, "hwaccel": "cuda"}
    ))

    command = _convert(
        recorded, tmp_path,
        utils.MediaInfo(video_codec="h264", audio_codec="aac", has_audio=True),
    )

    assert "-hwaccel" not in command


def test_hwaccel_and_the_encoder_are_applied_when_re_encoding(recorded, tmp_path, monkeypatch):
    monkeypatch.setattr(utils, "settings", utils.settings.__class__(
        **{**utils.settings.__dict__, "hwaccel": "cuda", "video_encoder": "h264_nvenc"}
    ))

    command = _convert(recorded, tmp_path, utils.MediaInfo(video_codec="vp9"))

    assert command[1:3] == ["-hwaccel", "cuda"]
    assert "h264_nvenc" in command


# --- which containers are picked up ---------------------------------------------


@pytest.mark.parametrize("suffix", [".mkv", ".mov", ".avi", ".m4v", ".webm", ".flv"])
def test_extra_containers_are_converted_on_scan(monkeypatch, app_env, recorded, suffix):
    """Only .webm used to be picked up; everything else sat there unusable."""
    monkeypatch.setattr(utils, "probe_media", lambda path: utils.MediaInfo(video_codec="h264"))
    monkeypatch.setattr(utils, "generate_thumbnail", lambda *args, **kwargs: None)
    (app_env["videos"] / f"clip{suffix}").write_bytes(b"data")

    assert utils.convert_source_files(app_env["videos"]) == 1
    assert list(app_env["videos"].glob("*.mp4"))


def test_the_original_is_archived_not_deleted(monkeypatch, app_env, recorded):
    monkeypatch.setattr(utils, "probe_media", lambda path: utils.MediaInfo(video_codec="h264"))
    monkeypatch.setattr(utils, "generate_thumbnail", lambda *args, **kwargs: None)
    (app_env["videos"] / "clip.mkv").write_bytes(b"data")

    utils.convert_source_files(app_env["videos"])

    assert (utils.get_originals_dir(app_env["videos"]) / "clip.mkv").exists()
    assert not (app_env["videos"] / "clip.mkv").exists()


def test_an_mp4_is_left_alone(monkeypatch, app_env, recorded):
    """It is already playable; converting it would be pure loss."""
    monkeypatch.setattr(utils, "probe_media", lambda path: utils.MediaInfo(video_codec="h264"))
    (app_env["videos"] / "clip.mp4").write_bytes(b"data")

    assert utils.convert_source_files(app_env["videos"]) == 0
    assert (app_env["videos"] / "clip.mp4").exists()
    assert recorded == []


def test_a_non_video_file_is_ignored(monkeypatch, app_env, recorded):
    (app_env["videos"] / "notes.txt").write_bytes(b"data")

    assert utils.convert_source_files(app_env["videos"]) == 0
    assert recorded == []


def test_the_archive_directory_name_is_unchanged(app_env):
    """Renaming it would orphan an existing library's archived originals."""
    assert utils.get_originals_dir(app_env["videos"]).name == "original_webm"
