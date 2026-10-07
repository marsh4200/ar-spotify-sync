"""Configuration loading.

General options come from the Home Assistant add-on options file
(/data/options.json). Outside Home Assistant, mount a JSON file with the same
keys at that path or point ARSYNC_OPTIONS at it.

The speaker selection is normally made on the add-on's web page and stored in
/data/speakers.json. A `speakers` list in the options file is only used until
that file exists.
"""

from __future__ import annotations

import json
import os
import sys
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
    ui_port: int = 8377

    @property
    def speakers_file(self) -> str:
        """Where the selection made on the web page is kept."""
        return os.path.join(self.data_dir, "speakers.json")


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def speaker_from_dict(entry: dict) -> SpeakerConfig | None:
    """Build a validated speaker entry from loosely typed input."""
    name = str(entry.get("name") or "").strip()
    address = str(entry.get("address") or "").strip() or None
    if not name and not address:
        return None
    protocol = str(entry.get("protocol") or "auto").lower()
    try:
        return SpeakerConfig(
            name=name or address or "",
            delay_ms=_clamp(int(entry.get("delay_ms") or 0), -1000, 1000),
            volume_percent=_clamp(int(entry.get("volume_percent", 100)), 0, 100),
            protocol=protocol if protocol in PROTOCOLS else "auto",
            password=str(entry["password"]) if entry.get("password") else None,
            address=address,
            buffer_ms=_clamp(int(entry.get("buffer_ms") or 0), 0, 3000),
        )
    except (TypeError, ValueError):
        return None


def speaker_to_dict(speaker: SpeakerConfig) -> dict:
    """Serialise a speaker entry, leaving out unset optional fields."""
    out: dict = {
        "name": speaker.name,
        "delay_ms": speaker.delay_ms,
        "volume_percent": speaker.volume_percent,
    }
    if speaker.protocol != "auto":
        out["protocol"] = speaker.protocol
    if speaker.buffer_ms:
        out["buffer_ms"] = speaker.buffer_ms
    if speaker.address:
        out["address"] = speaker.address
    if speaker.password:
        out["password"] = speaker.password
    return out


def save_speakers(cfg: Config) -> None:
    """Persist the current speaker selection."""
    os.makedirs(cfg.data_dir, exist_ok=True)
    tmp = cfg.speakers_file + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump([speaker_to_dict(s) for s in cfg.speakers], handle, indent=2)
    os.replace(tmp, cfg.speakers_file)


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
        if speaker := speaker_from_dict(entry):
            cfg.speakers.append(speaker)

    cfg.data_dir = os.environ.get("ARSYNC_DATA_DIR", cfg.data_dir)
    cfg.cliairplay_bin = os.environ.get("ARSYNC_CLIAIRPLAY_BIN", cfg.cliairplay_bin)
    cfg.golibrespot_bin = os.environ.get("ARSYNC_GOLIBRESPOT_BIN", cfg.golibrespot_bin)
    cfg.run_dir = os.environ.get("ARSYNC_RUN_DIR", cfg.run_dir)
    cfg.ui_port = int(os.environ.get("ARSYNC_UI_PORT", raw.get("ui_port", cfg.ui_port)))

    # A selection saved from the web page wins over the options file.
    try:
        with open(cfg.speakers_file, encoding="utf-8") as handle:
            saved = json.load(handle)
        cfg.speakers = [s for s in map(speaker_from_dict, saved) if s]
    except FileNotFoundError:
        pass
    except (OSError, ValueError, TypeError, AttributeError) as err:
        # Keep running on the options file rather than refusing to start.
        print(f"Ignoring unreadable {cfg.speakers_file}: {err}", file=sys.stderr)
    return cfg
