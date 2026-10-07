"""Spotify Connect source, using the go-librespot daemon.

The daemon shows up in the Spotify app as a speaker. It writes decoded PCM to its
stdout (as fast as it is read) and reports playback changes on a local WebSocket.
It is configured with `external_volume`, so it never changes the samples: the
Spotify volume slider is applied on the speakers instead, which keeps volume
changes immediate even though audio is buffered a few seconds ahead.

Event order that matters here (from the daemon's source, v0.9.0):

    natural track change:  not_playing -> will_play -> metadata -> playing
    skip / new selection:  will_play -> (load) -> metadata -> playing
    seek:                  seek
    pause / resume:        paused / playing
    end of queue:          not_playing -> stopped
    device deselected:     inactive
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import logging
import os
import socket
import time

import aiohttp

from . import FRAME_BYTES
from .config import Config
from .group import Group

LOGGER = logging.getLogger("arsync.spotify")
DAEMON_LOG = logging.getLogger("arsync.go-librespot")

F_SETPIPE_SZ = 1031
VOLUME_STEPS = 100
# After this device becomes active the daemon reports its own default volume;
# ignore volume reports for a moment so the configured level wins.
VOLUME_GRACE_S = 2.0
RESTART_DELAYS = (2, 5, 10, 30, 60)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class SpotifySource:
    """Runs the daemon, forwards its audio to the group and reacts to its events."""

    def __init__(self, cfg: Config, group: Group) -> None:
        self.cfg = cfg
        self.group = group
        self.config_dir = os.path.join(cfg.data_dir, "go-librespot")
        self._port = 0
        self._proc: asyncio.subprocess.Process | None = None
        self._natural_next_until = 0.0
        self._jump_pending = False
        self._volume_grace_until = 0.0
        self._session: aiohttp.ClientSession | None = None

    # ------------------------------------------------------------------ daemon

    def _write_config(self) -> None:
        os.makedirs(self.config_dir, exist_ok=True)
        self._port = _free_port()
        config = {
            "device_name": self.cfg.device_name,
            "device_type": "speaker",
            "bitrate": self.cfg.bitrate,
            "audio_backend": "pipe",
            "audio_output_pipe": "/dev/stdout",
            "audio_output_pipe_format": "s16le",
            "external_volume": True,
            "volume_steps": VOLUME_STEPS,
            "normalisation_disabled": not self.cfg.normalisation,
            "zeroconf_enabled": True,
            "credentials": {"type": "zeroconf", "zeroconf": {"persist_credentials": True}},
            "server": {"enabled": True, "address": "127.0.0.1", "port": self._port},
        }
        # JSON is valid YAML, which avoids quoting problems with the device name.
        with open(os.path.join(self.config_dir, "config.yml"), "w", encoding="utf-8") as handle:
            json.dump(config, handle, indent=2)

    async def run(self) -> None:
        """Keep the daemon running until cancelled."""
        attempt = 0
        self._session = aiohttp.ClientSession()
        try:
            while True:
                started = time.monotonic()
                try:
                    await self._run_once()
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    LOGGER.exception("Spotify daemon handling failed")
                await self.group.hard_stop("Spotify daemon stopped")
                attempt = 1 if time.monotonic() - started > 120 else attempt + 1
                delay = RESTART_DELAYS[min(attempt, len(RESTART_DELAYS)) - 1]
                LOGGER.warning("Spotify daemon exited; restarting in %d s", delay)
                await asyncio.sleep(delay)
        finally:
            await self._kill()
            await self._session.close()

    async def _run_once(self) -> None:
        self._write_config()
        read_fd, write_fd = os.pipe()
        # A small pipe keeps the amount of audio in transit (and so the audio
        # of the previous position after a skip or pause) down to milliseconds.
        with contextlib.suppress(OSError):
            fcntl.fcntl(read_fd, F_SETPIPE_SZ, 4096)
        try:
            self._proc = await asyncio.create_subprocess_exec(
                self.cfg.golibrespot_bin,
                "--config_dir",
                self.config_dir,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=write_fd,
                stderr=asyncio.subprocess.PIPE,
            )
        finally:
            os.close(write_fd)
        LOGGER.info("Spotify Connect device '%s' starting", self.cfg.device_name)

        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader(limit=1024)
        transport, _ = await loop.connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader), os.fdopen(read_fd, "rb", 0)
        )
        tasks = [
            asyncio.create_task(self._pump_audio(reader)),
            asyncio.create_task(self._read_log()),
            asyncio.create_task(self._listen_events()),
        ]
        try:
            await self._proc.wait()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            transport.close()
            await self._kill()

    async def _kill(self) -> None:
        proc, self._proc = self._proc, None
        if proc and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 3.0)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()

    async def _pump_audio(self, reader: asyncio.StreamReader) -> None:
        """Read PCM from the daemon and hand whole frames to the group."""
        carry = b""
        while True:
            data = await reader.read(4096)
            if not data:
                return
            if carry:
                data = carry + data
            usable = len(data) - (len(data) % FRAME_BYTES)
            carry = data[usable:]
            if usable:
                await self.group.accept(data[:usable])

    async def _read_log(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        while True:
            try:
                raw = await self._proc.stderr.readline()
            except ValueError:
                continue
            if not raw:
                return
            line = raw.decode("utf-8", errors="replace").rstrip()
            # Logged the moment the new track's audio is switched in, ahead of
            # the WebSocket events, which wait on a round trip to Spotify.
            if 'msg="loaded ' in line:
                self._boundary()
            if "level=error" in line or "level=fatal" in line or "level=warn" in line:
                DAEMON_LOG.warning("%s", line)
            else:
                DAEMON_LOG.debug("%s", line)

    # ------------------------------------------------------------------ events

    async def _listen_events(self) -> None:
        assert self._session is not None
        base = f"http://127.0.0.1:{self._port}"
        while True:
            try:
                async with self._session.get(
                    f"{base}/", timeout=aiohttp.ClientTimeout(total=2)
                ) as resp:
                    if resp.status == 200:
                        break
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
                pass
            await asyncio.sleep(0.25)
        LOGGER.info(
            "Spotify Connect device '%s' is ready - pick it in the Spotify app",
            self.cfg.device_name,
        )
        while True:
            try:
                async with self._session.ws_connect(
                    f"ws://127.0.0.1:{self._port}/events", heartbeat=30
                ) as ws:
                    async for msg in ws:
                        if msg.type != aiohttp.WSMsgType.TEXT:
                            continue
                        try:
                            payload = json.loads(msg.data)
                        except ValueError:
                            continue
                        if event := payload.get("type"):
                            try:
                                await self._on_event(event, payload.get("data") or {})
                            except Exception:  # noqa: BLE001
                                LOGGER.exception("Handling of '%s' failed", event)
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as err:
                LOGGER.debug("Event stream dropped: %s", err)
            await asyncio.sleep(1.0)

    def _boundary(self) -> None:
        if self._jump_pending:
            self._jump_pending = False
            self.group.jump_boundary()

    async def _on_event(self, event: str, data: dict) -> None:
        LOGGER.debug("event %s %s", event, data)
        if event == "active":
            await self._push_volume()
        elif event == "inactive":
            self._jump_pending = False
            await self.group.hard_stop("another device was selected in Spotify")
        elif event == "not_playing":
            # The track played to its end; the next one follows gaplessly.
            self._natural_next_until = time.monotonic() + 5.0
        elif event == "will_play":
            natural = time.monotonic() < self._natural_next_until
            self._natural_next_until = 0.0
            if not natural:
                self._jump_pending = True
                await self.group.jump()
        elif event == "metadata":
            self._boundary()
            LOGGER.info(
                "Now playing: %s - %s",
                ", ".join(data.get("artist_names") or []) or "?",
                data.get("name") or "?",
            )
        elif event == "playing":
            self._boundary()
            await self.group.resume()
        elif event == "paused":
            await self.group.pause()
        elif event == "seek":
            await self.group.seek()
        elif event == "stopped":
            self._natural_next_until = 0.0
            await self.group.drain_stop()
        elif event == "volume":
            value, top = data.get("value"), data.get("max") or VOLUME_STEPS
            if value is None:
                return
            percent = round(int(value) * 100 / int(top))
            if time.monotonic() < self._volume_grace_until and percent != self.group.volume:
                return  # the daemon's own default, reported before our level landed
            self.group.set_volume(percent)

    async def _push_volume(self) -> None:
        """Tell the daemon (and so the Spotify app) the group's current volume."""
        assert self._session is not None
        for _ in range(5):
            self._volume_grace_until = time.monotonic() + VOLUME_GRACE_S
            try:
                async with self._session.post(
                    f"http://127.0.0.1:{self._port}/player/volume",
                    json={"volume": self.group.volume},
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    if resp.status == 200:
                        return
                    LOGGER.debug("Volume push answered %s", resp.status)
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as err:
                LOGGER.debug("Could not push the volume: %s", err)
            await asyncio.sleep(0.5)
        LOGGER.warning(
            "Could not set the starting volume in Spotify; the slider there may not match the speakers"
        )
