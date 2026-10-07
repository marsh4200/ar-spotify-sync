"""AR Spotify Sync entry point."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import secrets
import signal
import sys

from arsync import __version__
from arsync.config import Config, load_config
from arsync.discovery import Discovery
from arsync.group import Group
from arsync.spotify import SpotifySource

LOGGER = logging.getLogger("arsync")

PTP_READY_MARKER = "[PTP] daemon up"
PTP_READY_TIMEOUT_S = 3.0
DISCOVERY_SETTLE_S = 4.0


def load_dacp_id(cfg: Config) -> str:
    """Return a stable 16-hex-digit sender identity, created on first run."""
    path = os.path.join(cfg.data_dir, "arsync_state.json")
    state: dict[str, str] = {}
    with contextlib.suppress(OSError, ValueError):
        with open(path, encoding="utf-8") as handle:
            state = json.load(handle)
    dacp_id = str(state.get("dacp_id") or "")
    if len(dacp_id) != 16:
        dacp_id = secrets.token_hex(8).upper()
        state["dacp_id"] = dacp_id
        with contextlib.suppress(OSError):
            os.makedirs(cfg.data_dir, exist_ok=True)
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(state, handle)
    return dacp_id


async def start_ptp_daemon(cfg: Config, dacp_id: str) -> asyncio.subprocess.Process | None:
    """Start the shared PTP clock that AirPlay 2 speakers (Sonos among them) lock to.

    Returns the process once it reports ready, or None when it could not start.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            cfg.cliairplay_bin,
            "--ptp-daemon",
            "--dacp",
            dacp_id,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except OSError as err:
        LOGGER.error("Could not run %s: %s", cfg.cliairplay_bin, err)
        return None
    assert proc.stdout is not None
    ready = asyncio.Event()
    log = logging.getLogger("arsync.ptp")

    async def reader() -> None:
        assert proc.stdout is not None
        while raw := await proc.stdout.readline():
            line = raw.decode("utf-8", errors="replace").rstrip()
            if PTP_READY_MARKER in line:
                ready.set()
            log.debug("%s", line)

    task = asyncio.create_task(reader())
    proc._arsync_reader = task  # type: ignore[attr-defined]  # keep a reference
    try:
        await asyncio.wait_for(ready.wait(), PTP_READY_TIMEOUT_S)
    except asyncio.TimeoutError:
        LOGGER.warning(
            "The shared PTP clock did not start (UDP ports 319/320 busy or not permitted). "
            "AirPlay 2 speakers such as Sonos may stay silent. Run with host networking as "
            "root, and make sure no other AirPlay sender (Music Assistant) uses this host."
        )
        if proc.returncode is None:
            proc.terminate()
        return None
    LOGGER.info("Shared PTP clock running")
    return proc


async def run() -> int:
    """Run until terminated."""
    try:
        cfg = load_config()
    except (OSError, ValueError) as err:
        print(f"Configuration error: {err}", file=sys.stderr)
        return 2

    level = {"debug": logging.DEBUG, "warning": logging.WARNING}.get(cfg.log_level, logging.INFO)
    logging.basicConfig(
        level=level, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", stream=sys.stdout
    )
    logging.getLogger("zeroconf").setLevel(logging.WARNING)
    LOGGER.info("AR Spotify Sync %s", __version__)

    for path in (cfg.cliairplay_bin, cfg.golibrespot_bin):
        if not os.access(path, os.X_OK):
            LOGGER.error("Required program missing: %s", path)
            return 2

    dacp_id = load_dacp_id(cfg)
    discovery = Discovery()
    await discovery.start()
    ptp = await start_ptp_daemon(cfg, dacp_id)

    group = Group(cfg, discovery, dacp_id)
    group.shared_ptp = ptp is not None
    group.start()

    await asyncio.sleep(DISCOVERY_SETTLE_S)
    for speaker in cfg.speakers:
        device = discovery.resolve(speaker)
        if device:
            LOGGER.info(
                "Speaker '%s' -> %s at %s (%s, delay %+d ms, volume %d%%)",
                speaker.name,
                device.name,
                device.address,
                "AirPlay 2" if device.airplay2_capable and speaker.protocol != "raop" else "AirPlay 1",
                speaker.delay_ms,
                speaker.volume_percent,
            )
        else:
            LOGGER.warning(
                "Speaker '%s' not found yet. AirPlay devices seen: %s",
                speaker.name,
                ", ".join(sorted(d.name for d in discovery.devices.values())) or "none",
            )

    source = SpotifySource(cfg, group)
    source_task = asyncio.create_task(source.run())

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()

    LOGGER.info("Shutting down")
    source_task.cancel()
    await asyncio.gather(source_task, return_exceptions=True)
    await group.shutdown()
    if ptp and ptp.returncode is None:
        ptp.terminate()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(ptp.wait(), 3.0)
    await discovery.stop()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
