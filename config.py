"""Application configuration.

Values are resolved once, at import time, with this precedence (highest first):

  1. Environment variables
  2. ``config.json`` (or whatever ``CONFIG_FILE`` points at)
  3. Built-in defaults

Settings are immutable. The directory the UI is *currently browsing* is runtime
state rather than configuration, so it is persisted in the database file (see
``database.current_dir``) and never written back into ``config.json``.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from ipaddress import ip_network
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_CONFIG_FILE = "config.json"

DEFAULTS: dict[str, Any] = {
    "video_dir": "videos",
    "thumbnail_dir": "thumbnails",
    "db_file": "video_db.json",
    # Networks permitted to reach the app. Accepts bare addresses or CIDR.
    "allowed_ips": ["127.0.0.1", "::1"],
    # Peers whose X-Forwarded-For header may be believed. Empty means "none",
    # which is the safe default: see middleware.client_ip.
    "trusted_proxies": [],
    "ffmpeg_timeout": 30,
    "convert_timeout": 3600,
    "thumbnail_width": 320,
    "max_download_bytes": 8 * 1024 * 1024 * 1024,
    "download_timeout": 30,
    "host": "0.0.0.0",
    "port": 6969,
}

# Environment variable -> config key. Values are coerced to the type of the
# matching default.
ENV_KEYS = {
    "VIDEO_DIR": "video_dir",
    "THUMBNAIL_DIR": "thumbnail_dir",
    "DB_FILE": "db_file",
    "ALLOWED_IPS": "allowed_ips",
    "TRUSTED_PROXIES": "trusted_proxies",
    "FFMPEG_TIMEOUT": "ffmpeg_timeout",
    "CONVERT_TIMEOUT": "convert_timeout",
    "THUMBNAIL_WIDTH": "thumbnail_width",
    "MAX_DOWNLOAD_BYTES": "max_download_bytes",
    "DOWNLOAD_TIMEOUT": "download_timeout",
    "HOST": "host",
    "PORT": "port",
}


@dataclass(frozen=True)
class Settings:
    """Immutable resolved configuration."""

    video_dir: Path
    thumbnail_dir: Path
    db_file: Path
    allowed_ips: tuple[str, ...]
    trusted_proxies: tuple[str, ...]
    ffmpeg_timeout: int
    convert_timeout: int
    thumbnail_width: int
    max_download_bytes: int
    download_timeout: int
    host: str
    port: int

    @property
    def parent_dir(self) -> Path:
        """Directory whose children are offered as switchable libraries."""
        override = os.environ.get("PARENT_DIR")
        return Path(override) if override else self.video_dir.parent


def _coerce(value: str, default: Any) -> Any:
    if isinstance(default, list):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(default, bool):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(default, int):
        return int(value)
    return value


def _read_file(path: Path) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        # Not fatal: defaults plus environment variables are enough to boot,
        # which is what the container image relies on.
        log.warning("Config file %s not found; using defaults and environment", path)
        return {}
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in config file '{path}': {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError(f"Config file '{path}' must contain a JSON object")
    return data


def _validate_networks(values: tuple[str, ...], field: str) -> tuple[str, ...]:
    """Drop entries that are not a valid address or CIDR block, loudly."""
    valid = []
    for value in values:
        try:
            ip_network(value, strict=False)
        except ValueError:
            log.warning("Ignoring invalid entry %r in %s", value, field)
            continue
        valid.append(value)
    return tuple(valid)


def load_settings(config_file: str | os.PathLike[str] | None = None) -> Settings:
    """Build a Settings object. Explicit factory so tests can point elsewhere."""
    path = Path(config_file or os.environ.get("CONFIG_FILE") or DEFAULT_CONFIG_FILE)

    values: dict[str, Any] = dict(DEFAULTS)
    values.update({k: v for k, v in _read_file(path).items() if k in DEFAULTS})

    for env_name, key in ENV_KEYS.items():
        raw = os.environ.get(env_name)
        if raw is not None and raw != "":
            try:
                values[key] = _coerce(raw, DEFAULTS[key])
            except ValueError:
                log.warning("Ignoring malformed %s=%r", env_name, raw)

    return Settings(
        video_dir=Path(values["video_dir"]).expanduser(),
        thumbnail_dir=Path(values["thumbnail_dir"]).expanduser(),
        db_file=Path(values["db_file"]).expanduser(),
        allowed_ips=_validate_networks(tuple(values["allowed_ips"]), "allowed_ips"),
        trusted_proxies=_validate_networks(
            tuple(values["trusted_proxies"]), "trusted_proxies"
        ),
        ffmpeg_timeout=int(values["ffmpeg_timeout"]),
        convert_timeout=int(values["convert_timeout"]),
        thumbnail_width=int(values["thumbnail_width"]),
        max_download_bytes=int(values["max_download_bytes"]),
        download_timeout=int(values["download_timeout"]),
        host=str(values["host"]),
        port=int(values["port"]),
    )


settings = load_settings()
