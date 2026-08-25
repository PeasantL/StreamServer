"""Directory containment - the guard behind the arbitrary-path findings."""

import pytest


def test_resolve_within_accepts_children(app_env):
    import utils

    base = app_env["videos"]
    (base / "clip.mp4").write_bytes(b"x")
    assert utils.resolve_within(base, "clip.mp4") == (base / "clip.mp4").resolve()


@pytest.mark.parametrize(
    "candidate",
    [
        "/etc/passwd",        # absolute: pathlib discards the left operand
        "../other",
        "../../escape",
        "sub/../../escape",
    ],
)
def test_resolve_within_rejects_escapes(app_env, candidate):
    import utils

    with pytest.raises(utils.UnsafePathError):
        utils.resolve_within(app_env["videos"], candidate)


def test_change_directory_rejects_absolute_paths(client, app_env):
    """POST {"folder": "/etc"} used to repoint the entire application."""
    test_client, _ = client

    for folder in ("/etc", "../..", "/", "does-not-exist"):
        response = test_client.post("/api/change-directory", json={"folder": folder})
        assert response.status_code == 404, folder

    import database

    assert database.current_dir() == app_env["videos"].resolve()


def test_change_directory_accepts_an_offered_sibling(client, app_env):
    test_client, _ = client

    response = test_client.post("/api/change-directory", json={"folder": "other"})
    assert response.status_code == 200

    import database

    assert database.current_dir() == app_env["other"].resolve()
