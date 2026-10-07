"""Configuration loading.

Options come from the Home Assistant add-on options file (/data/options.json).
Outside Home Assistant, mount a JSON file with the same keys at that path or
point ARSYNC_OPTIONS at it.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

PROTOCOLS = ("auto", "raop", "airplay2", "airplay2-compat")


@dataclass
class SpeakerConfig:
    """One AirPlay device that is part of the group."""

    name: str
    delay_ms: int = 0
    volume_percent: int = 100
    protocol: str = "auto"
    password: str | None = None
    address: str | None = None
    buffer_ms: int = 0


@dataclass
class Config:
    """Everything the add-on needs to run."""

    device_name: str = "AR Spotify Sync"
    bitrate: int = 320
    start_volume: int = 25
    idle_disconnect_seconds: int = 45
    normalisation: bool = False
    log_level: str = "info"
    speakers: list[SpeakerConfig] = field(default_factory=list)
    # Paths, overridable through the environment for development and testing.
    data_dir: str = "/data"
    cliairplay_bin: str = "/usr/local/bin/cliairplay"
    golibrespot_bin: str = "/usr/local/bin/go-librespot"
    run_dir: str = "/tmp/arsync"


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def load_config() -> Config:
    """Load and validate the options file."""
    path = os.environ.get("ARSYNC_OPTIONS", "/data/options.json")
    with open(path, encoding="utf-8") as handle:
        raw = json.load(handle)

    cfg = Config()
    cfg.device_name = str(raw.get("device_name") or cfg.device_name).strip()
    bitrate = int(raw.get("bitrate", cfg.bitrate))
    cfg.bitrate = bitrate if bitrate in (96, 160, 320) else 320
    cfg.start_volume = _clamp(int(raw.get("start_volume", cfg.start_volume)), 0, 100)
    # The AirPlay sender ends a flushed session on its own after 120 s, so stay below that.
    cfg.idle_disconnect_seconds = _clamp(
        int(raw.get("idle_disconnect_seconds", cfg.idle_disconnect_seconds)), 5, 100
    )
    cfg.normalisation = bool(raw.get("normalisation", cfg.normalisation))
    cfg.log_level = str(raw.get("log_level", cfg.log_level)).lower()

    for entry in raw.get("speakers") or []:
        name = str(entry.get("name") or "").strip()
        address = str(entry.get("address") or "").strip() or None
        if not name and not address:
            continue
        protocol = str(entry.get("protocol") or "auto").lower()
        cfg.speakers.append(
            SpeakerConfig(
                name=name or address or "",
                delay_ms=_clamp(int(entry.get("delay_ms", 0)), -1000, 1000),
                volume_percent=_clamp(int(entry.get("volume_percent", 100)), 0, 100),
                protocol=protocol if protocol in PROTOCOLS else "auto",
                password=str(entry["password"]) if entry.get("password") else None,
                address=address,
                buffer_ms=_clamp(int(entry.get("buffer_ms", 0)), 0, 3000),
            )
        )
    if not cfg.speakers:
        raise ValueError("No speakers configured: add at least one entry under 'speakers'.")

    cfg.data_dir = os.environ.get("ARSYNC_DATA_DIR", cfg.data_dir)
    cfg.cliairplay_bin = os.environ.get("ARSYNC_CLIAIRPLAY_BIN", cfg.cliairplay_bin)
    cfg.golibrespot_bin = os.environ.get("ARSYNC_GOLIBRESPOT_BIN", cfg.golibrespot_bin)
    cfg.run_dir = os.environ.get("ARSYNC_RUN_DIR", cfg.run_dir)
    return cfg
