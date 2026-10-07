"""One AirPlay receiver, driven by one `cliairplay` process.

The sender binary takes raw PCM on stdin for its whole lifetime and is controlled
through a named pipe. It reports state as `[STATUS] ...` lines on stdout and stderr.
The contract used here:

    START_UNIX_MS=<ms> + ACTION=START   first pending sample is audible at that instant
    ACTION=FLUSH                        drop buffered audio, stay connected
    VOLUME=<0-100>                      set the receiver volume
    ACTION=STOP                         end the session, process exits
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import time
from typing import Awaitable, Callable

from .config import Config, SpeakerConfig
from .discovery import Device, source_ip_for

LOGGER = logging.getLogger("arsync.member")

RAOP_DEFAULT_PORT = 5000
AIRPLAY_DEFAULT_PORT = 7000
RAOP_TXT_KEYS = ("et", "md", "am", "pk", "pw", "cn")

# Standard AirPlay 1 receiver latency (2 s buffer plus 11025 frames), used
# when the sender has not reported the receiver's own figure.
RAOP_DEFAULT_LATENCY_MS = 2250

_FIELD = re.compile(r"(\w+)=(\S+)")
_RAOP_LATENCY = re.compile(r"player latency is (\d+) ms")


def _fields(line: str) -> dict[str, str]:
    return dict(_FIELD.findall(line))


def _int(fields: dict[str, str], key: str) -> int:
    try:
        return int(fields.get(key, "0"))
    except ValueError:
        return 0


class Member:
    """A running sender process for one speaker."""

    def __init__(
        self,
        cfg: Config,
        speaker: SpeakerConfig,
        device: Device,
        dacp_id: str,
        on_exit: Callable[["Member"], Awaitable[None]],
    ) -> None:
        self.cfg = cfg
        self.speaker = speaker
        self.device = device
        self.name = device.name
        self.delay_ms = speaker.delay_ms
        self._dacp_id = dacp_id
        self._on_exit = on_exit
        self.log = logging.getLogger(f"arsync.member.{self.name}")

        self.proc: asyncio.subprocess.Process | None = None
        self._cmd_fd: int | None = None
        self._cmd_path = ""
        self._tasks: list[asyncio.Task[None]] = []
        self._stopping = False

        self.connected = asyncio.Event()
        self.exited = asyncio.Event()
        self.audio_present = asyncio.Event()
        self.clock_ready = asyncio.Event()
        self.clock_ready_at_unix_ms = 0
        self.flushed = asyncio.Event()
        self.flushed_head_unix_ms = 0
        self.started = asyncio.Event()
        self.started_at_unix_ms = 0
        self.warm_lead_ms = 0
        self.ever_started = False
        self.volume = 0
        self.route = ""
        self.raop_flow = False
        self.raop_latency_ms = RAOP_DEFAULT_LATENCY_MS
        self.last_flush_unix_ms = 0
        self.error = ""

    # ------------------------------------------------------------------ lifecycle

    @property
    def alive(self) -> bool:
        """Return whether the sender process is running."""
        return self.proc is not None and self.proc.returncode is None and not self._stopping

    def _build_args(self, volume: int, shared_ptp: bool) -> list[str]:
        device = self.device
        raop, airplay = device.raop, device.airplay
        wanted = self.speaker.protocol
        use_ap2 = device.airplay2_capable and wanted != "raop"
        if wanted == "auto":
            # An AirPlay-2-only receiver cannot be recognised from feature bits alone.
            protocol = "airplay2" if (use_ap2 and raop is None) else "auto"
        else:
            protocol = wanted

        active_remote = "1"
        if device.device_id:
            with contextlib.suppress(ValueError):
                active_remote = str(int(device.device_id, 16) & 0xFFFFFFFF)

        args = [
            self.cfg.cliairplay_bin,
            "--protocol", protocol,
            "--dacp", self._dacp_id,
            "--activeremote", active_remote,
            "--cmdpipe", self._cmd_path,
            "--samplerate", "44100",
            "--bitdepth", "16",
            "--volume", str(volume),
        ]  # fmt: skip
        if use_ap2 and airplay:
            args += ["--port", str(airplay.port or AIRPLAY_DEFAULT_PORT)]
            args += ["--name", device.name]
            if airplay.server:
                args += ["--hostname", airplay.server]
        elif raop:
            args += ["--port", str(raop.port or RAOP_DEFAULT_PORT)]

        if raop:
            args += ["--udn", raop.instance]
            for key in RAOP_TXT_KEYS:
                if value := raop.props.get(key):
                    args += [f"--{key}", value]
        if not use_ap2:
            args += ["--encrypt"]

        if airplay:
            pairs = [
                f"{k}={v}"
                for k, v in airplay.props.items()
                if not any(c.isspace() for c in k + v)
            ]
            has_features = airplay.props.get("features") or airplay.props.get("ft")
            if not has_features and raop and (raop_ft := raop.props.get("ft")):
                pairs.append(f"ft={raop_ft}")
            if pairs:
                args += ["--txt", " ".join(pairs)]

        if self.speaker.password:
            args += ["--password", self.speaker.password]
        if use_ap2:
            if self.speaker.buffer_ms:
                args += ["--latency", str(self.speaker.buffer_ms)]
            if shared_ptp:
                args += ["--ptp-shared"]

        address = device.address or ""
        if source_ip := source_ip_for(address):
            args += ["--if", source_ip]
        if LOGGER.isEnabledFor(logging.DEBUG):
            args += ["--debug", "5"]
        args.append(address)
        return args

    async def spawn(self, volume: int, shared_ptp: bool) -> None:
        """Start the sender process. It connects to the speaker on its own."""
        os.makedirs(self.cfg.run_dir, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9]+", "_", self.name)
        self._cmd_path = os.path.join(self.cfg.run_dir, f"{safe}-{time.monotonic_ns()}.cmd")
        os.mkfifo(self._cmd_path)
        # Read-write so opening never blocks and commands queue until the sender reads them.
        self._cmd_fd = os.open(self._cmd_path, os.O_RDWR | os.O_NONBLOCK)

        self.volume = max(0, min(100, volume))
        args = self._build_args(self.volume, shared_ptp)
        self.log.debug("spawn: %s", " ".join(args))
        self.proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert self.proc.stdin and self.proc.stdout and self.proc.stderr
        # Small write buffer so backpressure from the sender reaches the feeder promptly.
        self.proc.stdin.transport.set_write_buffer_limits(high=8192, low=0)
        self._tasks = [
            asyncio.create_task(self._read_lines(self.proc.stdout)),
            asyncio.create_task(self._read_lines(self.proc.stderr)),
            asyncio.create_task(self._watch_exit()),
        ]

    async def _watch_exit(self) -> None:
        assert self.proc is not None
        code = await self.proc.wait()
        self.exited.set()
        # Release anything waiting on this member.
        for event in (self.connected, self.audio_present, self.clock_ready, self.flushed):
            event.set()
        self.started.set()
        self._close_cmd()
        if not self._stopping:
            self.log.warning(
                "sender exited (code %s)%s", code, f": {self.error}" if self.error else ""
            )
            await self._on_exit(self)

    def _close_cmd(self) -> None:
        if self._cmd_fd is not None:
            with contextlib.suppress(OSError):
                os.close(self._cmd_fd)
            self._cmd_fd = None
        if self._cmd_path:
            with contextlib.suppress(OSError):
                os.unlink(self._cmd_path)

    async def stop(self) -> None:
        """End the session and make sure the process is gone."""
        if self.proc is None or self._stopping:
            return
        self._stopping = True
        if self.proc.returncode is None:
            self._command("ACTION=STOP")
            with contextlib.suppress(Exception):
                if self.proc.stdin and not self.proc.stdin.is_closing():
                    self.proc.stdin.close()
            try:
                await asyncio.wait_for(self.exited.wait(), 3.0)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    self.proc.terminate()
                try:
                    await asyncio.wait_for(self.exited.wait(), 2.0)
                except asyncio.TimeoutError:
                    with contextlib.suppress(ProcessLookupError):
                        self.proc.kill()
        self._close_cmd()

    # ------------------------------------------------------------------ status parsing

    async def _read_lines(self, stream: asyncio.StreamReader) -> None:
        while True:
            try:
                raw = await stream.readline()
            except ValueError:  # over-long line
                continue
            if not raw:
                return
            line = raw.decode("utf-8", errors="replace").rstrip()
            if line:
                self._handle_line(line)

    def _handle_line(self, line: str) -> None:
        if "[STATUS]" not in line:
            if match := _RAOP_LATENCY.search(line):
                self.raop_latency_ms = int(match.group(1))
            if "[ERROR]" in line:
                self.log.warning("%s", line)
            else:
                self.log.debug("%s", line)
            return
        if "[STATUS] playing" not in line:
            self.log.debug("%s", line)
        fields = _fields(line)
        if "[STATUS] connected" in line:
            self.connected.set()
            # The level given at connect is not always taken, and some receivers
            # ignore the first volume command, so send it again now and once more
            # shortly after (the same approach Music Assistant uses).
            self._send_volume()
            asyncio.get_running_loop().call_later(2.0, self._send_volume)
        elif "[STATUS] route" in line:
            self.route = (
                f"{fields.get('protocol', '?')}/{fields.get('flow', '?')}/"
                f"{fields.get('timing', '?')}"
            )
            self.raop_flow = fields.get("flow") in ("legacy", "raop-compat")
        elif "[STATUS] latency" in line:
            self.warm_lead_ms = _int(fields, "warm_lead_ms")
        elif "[STATUS] clock_ready" in line:
            mode, state = fields.get("mode", ""), fields.get("state", "")
            if state == "cold" and mode != "ntp":
                return  # no projection yet, more lines follow
            if state == "stalled" and mode != "ntp":
                self.log.warning(
                    "speaker is not answering the PTP clock, so it will stay silent. "
                    "Check that UDP ports 319 and 320 can pass between this host and the speaker."
                )
                self.clock_ready_at_unix_ms = 0
            elif mode == "ntp":
                self.clock_ready_at_unix_ms = 0
            else:
                self.clock_ready_at_unix_ms = _int(fields, "ready_at_unix_ms")
            self.clock_ready.set()
        elif "[STATUS] audio " in line:
            self.audio_present.set()
        elif "[STATUS] started " in line:
            self.started_at_unix_ms = _int(fields, "at_unix_ms")
            self.started.set()
        elif "[STATUS] flushed" in line:
            self.flushed_head_unix_ms = _int(fields, "head_unix_ms")
            self.flushed.set()
        elif "[STATUS] error " in line:
            detail = line.split("detail=", 1)[-1].strip('"') if "detail=" in line else line
            self.error = f"{fields.get('code', 'error')}: {detail}"
            self.log.warning("sender reported an error: %s", self.error)

    # ------------------------------------------------------------------ commands

    def _command(self, text: str) -> bool:
        if self._cmd_fd is None:
            return False
        try:
            os.write(self._cmd_fd, (text + "\n").encode())
        except OSError as err:
            self.log.debug("command '%s' not delivered: %s", text, err)
            return False
        return True

    async def wait_connected(self, timeout: float) -> bool:
        """Wait for the RTSP session to come up. False if it failed or timed out."""
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self.connected.wait(), timeout)
        return self.alive and self.connected.is_set() and not self.exited.is_set()

    async def write(self, data: bytes) -> bool:
        """Write PCM to the sender, waiting while its buffer is full."""
        if not self.alive or self.proc is None or self.proc.stdin is None:
            return False
        try:
            self.proc.stdin.write(data)
            await self.proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, RuntimeError):
            return False
        return True

    async def flush(self, timeout: float = 2.5) -> bool:
        """Drop everything buffered in the sender and on the receiver."""
        if not self.alive:
            return False
        self.flushed.clear()
        self.audio_present.clear()
        self.flushed_head_unix_ms = 0
        if not self._command("ACTION=FLUSH"):
            return False
        self.last_flush_unix_ms = int(time.time() * 1000)
        try:
            await asyncio.wait_for(self.flushed.wait(), timeout)
        except asyncio.TimeoutError:
            self.log.warning("no answer to FLUSH within %.1f s", timeout)
            return False
        return self.alive

    async def start(self, unix_ms: int, timeout: float = 6.0) -> int:
        """Schedule the first pending sample for an instant; return the true instant."""
        if not self.alive:
            return unix_ms
        self.started.clear()
        self.started_at_unix_ms = 0
        if not self._command(f"START_UNIX_MS={unix_ms}\nACTION=START"):
            return unix_ms
        try:
            await asyncio.wait_for(self.started.wait(), timeout)
        except asyncio.TimeoutError:
            self.log.warning("no answer to START within %.1f s", timeout)
            return unix_ms
        self.ever_started = True
        return self.started_at_unix_ms or unix_ms

    @property
    def earliest_restart_unix_ms(self) -> int:
        """Earliest instant new audio may be scheduled for after the last flush.

        On the AirPlay 1 flow the sender has already delivered audio stamped up
        to one receiver latency past the flush, and the flush tells the receiver
        to drop everything stamped before that point. A restart scheduled inside
        that window would have its first part dropped by receivers that take
        the flush literally, so the start of the new audio would go missing.
        """
        if not self.raop_flow or not self.last_flush_unix_ms:
            return 0
        return self.last_flush_unix_ms + self.raop_latency_ms + 100

    def _send_volume(self) -> None:
        if self.alive:
            self._command(f"VOLUME={self.volume}")

    def set_volume(self, volume: int) -> None:
        """Set the receiver volume (0-100)."""
        self.volume = max(0, min(100, volume))
        self._send_volume()
