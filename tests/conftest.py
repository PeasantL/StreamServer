"""Test fixtures.

Every test runs against a throwaway config so nothing touches a real library.
The environment is set before the application modules are reloaded, because
settings are resolved once at import time.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Modules that bind `config.settings` at import time and so must be reloaded
# once the test environment is in place. `ranges` and `tasks` are deliberately
# absent: they have no configuration dependency, and reloading them would
# rebind the exception classes that tests import by name.
CONFIG_DEPENDENT_MODULES = ("database", "downloads", "boorus", "utils", "middleware")


def with_peer(app, host: str, port: int = 5555):
    """Wrap an ASGI app so requests appear to come from *host*.

    Starlette's TestClient reports a peer of ("testclient", 50000), which is not
    an address the allowlist can evaluate, and its constructor has no hook for
    overriding it.
    """

    async def wrapper(scope, receive, send):
        if scope["type"] == "http":
            scope = dict(scope, client=(host, port))
        await app(scope, receive, send)

    return wrapper


@pytest.fixture
def app_env(tmp_path, monkeypatch):
    """Reload the application against an isolated temporary library."""
    parent = tmp_path / "media"
    videos = parent / "library"
    other = parent / "other"
    thumbs = tmp_path / "thumbnails"
    for directory in (videos, other, thumbs):
        directory.mkdir(parents=True)

    monkeypatch.setenv("CONFIG_FILE", str(tmp_path / "missing-config.json"))
    monkeypatch.setenv("VIDEO_DIR", str(videos))
    monkeypatch.setenv("PARENT_DIR", str(parent))
    monkeypatch.setenv("THUMBNAIL_DIR", str(thumbs))
    monkeypatch.setenv("DB_FILE", str(tmp_path / "db.json"))
    monkeypatch.setenv("ALLOWED_IPS", "127.0.0.1,::1")
    monkeypatch.setenv("TRUSTED_PROXIES", "")

    import config

    importlib.reload(config)
    for name in CONFIG_DEPENDENT_MODULES:
        if name in sys.modules:
            importlib.reload(sys.modules[name])
        else:
            importlib.import_module(name)

    import database

    database.reset_cache()
    database.init_db()

    yield {
        "parent": parent,
        "videos": videos,
        "other": other,
        "thumbnails": thumbs,
    }

    database.reset_cache()


@pytest.fixture
def app_module(app_env, monkeypatch):
    """The reloaded `main` module, with library scanning stubbed out.

    Scanning shells out to ffmpeg, which the test environment does not need.
    """
    import utils

    monkeypatch.setattr(
        utils,
        "scan_library",
        lambda directory=None: {
            "converted": 0,
            "added": 0,
            "pruned": 0,
            "backfilled": 0,
            "duplicates": 0,
        },
    )

    if "main" in sys.modules:
        return importlib.reload(sys.modules["main"])
    return importlib.import_module("main")


@pytest.fixture
def client(app_module):
    """A TestClient whose requests come from an allowed address."""
    from fastapi.testclient import TestClient

    with TestClient(with_peer(app_module.app, "127.0.0.1")) as test_client:
        yield test_client, app_module
