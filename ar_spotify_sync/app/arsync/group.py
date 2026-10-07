"""The synced group: fans one PCM stream out to every speaker on one shared timeline.

How sync works
--------------
Every speaker gets the same bytes. Once each sender has audio buffered, all of them
are told to make the first pending sample audible at the same wall-clock instant
(plus that speaker's configured delay). The mapping "stream offset X is audible at
time T" is the anchor. Knowing it lets a pause resume from the exact position that
was last heard, even though the senders buffer several seconds ahead.

States
------
idle    no sender processes running
live    senders running; audio is being fed (anchored once playback has a start instant)
paused  senders connected but flushed; waiting for the source to continue
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Awaitable, Callable

from . import BYTES_PER_SECOND, FRAME_BYTES
from .config import Config, SpeakerConfig
from .discovery import Discovery
from .member import Member

LOGGER = logging.getLogger("arsync.group")

SLICE_BYTES = 4096
# Unsent audio at which the source is held back while live. Keeps the Spotify
# client only a little ahead of what has been handed to the senders.
LIVE_BACKLOG_LIMIT = 32 * 1024
# Hard cap on audio accepted while nothing is consuming it.
PARKED_BACKLOG_LIMIT = 8 * 1024 * 1024
# How far past the position being heard the senders are fed. They need at least
# one receiver latency in hand (2.25 s on AirPlay 1, up to 3 s on AirPlay 2);
# feeding no further than this keeps the position shown in Spotify close to the sound.
FEED_AHEAD_BYTES = 4 * BYTES_PER_SECOND
# Audio kept behind the write position so a pause can resume from what was heard.
HISTORY_KEEP_BYTES = 15 * BYTES_PER_SECOND
# While idle, this much new audio must arrive before the speakers are woken, so
# the few milliseconds still in transit after a stop cannot start a session.
IDLE_START_BYTES = 48 * 1024
# Audio arriving this soon after a pause is what the source had already written.
RESIDUAL_WINDOW_S = 0.5
# Start leads, as used by Music Assistant for the same sender.
SOLO_LEAD_MS = 400
WARM_GROUP_LEAD_MS = 500
COLD_START_LEAD_MS = 2500
CLOCK_READY_LEAD_MS = 500
SPLICE_MARGIN_MS = 150
CONNECT_TIMEOUT_S = 8.0
REJOIN_INTERVAL_S = 20.0
JUMP_DISCARD_TIMEOUT_S = 8.0
# Audio of the track being left that is still in transit from the source at the
# moment the new track is switched in: its pipe (4 KiB), one write in progress
# (up to 8 KiB) and our read buffer. Dropped after the switch so none of it is
# heard; costs at most ~70 ms off the very start of a track that was skipped to.
JUMP_BOUNDARY_DISCARD_BYTES = 16 * 1024


def now_ms() -> int:
    """Wall-clock time in unix milliseconds."""
    return int(time.time() * 1000)


class Group:
    """Owns the sender processes and the shared playback timeline."""

    def __init__(self, cfg: Config, discovery: Discovery, dacp_id: str) -> None:
        self.cfg = cfg
        self.discovery = discovery
        self.dacp_id = dacp_id
        self.shared_ptp = False

        self.lock = asyncio.Lock()
        self.state = "idle"
        self.members: list[Member] = []
        self.volume = cfg.start_volume

        # Stream bookkeeping. Offsets count bytes since start and never go backwards.
        self.history = bytearray()
        self.history_base = 0  # stream offset of history[0]
        self.fed = 0  # stream offset just past the newest byte accepted
        self.unsent_from = 0  # stream offset of the next byte to hand to the senders
        self.anchor_ms: int | None = None  # instant at which anchor_offset is audible
        self.anchor_offset = 0

        self.discarding = False
        self._discard_deadline = 0.0
        self._discard_min_left = 0
        self.epoch = 0
        self._feed_enabled = False
        self._feeder_busy = False
        self._writes = 0
        self._feed_wake = asyncio.Event()
        self._space = asyncio.Event()
        self._paused_at = 0.0
        self._idle_mark = 0
        self._joining: set[int] = set()  # speakers a rejoin attempt is connecting to
        self._no_speakers_until = 0.0
        self._anchor_task: asyncio.Task[None] | None = None
        self._timer: asyncio.Task[None] | None = None
        self._tasks: list[asyncio.Task[None]] = []

    # ------------------------------------------------------------------ setup

    def start(self) -> None:
        """Start the background tasks."""
        self._tasks = [
            asyncio.create_task(self._feeder()),
            asyncio.create_task(self._rejoin_loop()),
        ]

    async def shutdown(self) -> None:
        """Stop everything."""
        await self.hard_stop("shutting down")
        for task in self._tasks:
            task.cancel()

    # ------------------------------------------------------------------ helpers

    def _alive(self) -> list[Member]:
        return [m for m in self.members if m.alive]

    def _member_volume(self, member: Member) -> int:
        return round(self.volume * member.speaker.volume_percent / 100)

    def _heard_offset(self, at_ms: int) -> int:
        """Stream offset that is audible at an instant on the group timeline."""
        if self.anchor_ms is None:
            return self.anchor_offset
        raw = self.anchor_offset + (at_ms - self.anchor_ms) * BYTES_PER_SECOND // 1000
        raw -= raw % FRAME_BYTES
        low = max(self.anchor_offset, self.history_base)
        return max(low, min(raw, self.unsent_from))

    def _feed_limit(self) -> int:
        """Stream offset up to which the senders may be fed right now."""
        base = self.anchor_offset
        if self.anchor_ms is not None:
            base += max(0, now_ms() - self.anchor_ms) * BYTES_PER_SECOND // 1000
        return base + FEED_AHEAD_BYTES

    def _trim_history(self) -> None:
        keep_from = max(self.history_base, self.unsent_from - HISTORY_KEEP_BYTES)
        drop = keep_from - self.history_base
        if drop >= BYTES_PER_SECOND:  # trim in chunks, not on every slice
            del self.history[:drop]
            self.history_base = keep_from

    def _set_timer(self, delay: float, action: Callable[[], Awaitable[None]]) -> None:
        self._cancel_timer()

        async def runner() -> None:
            await asyncio.sleep(delay)
            self._timer = None
            await action()

        self._timer = asyncio.create_task(runner())

    def _cancel_timer(self) -> None:
        if self._timer and self._timer is not asyncio.current_task():
            self._timer.cancel()
        self._timer = None

    def _schedule_anchor(self) -> None:
        if self._anchor_task and self._anchor_task is not asyncio.current_task():
            self._anchor_task.cancel()
        self._anchor_task = asyncio.create_task(self._anchor(self.epoch))

    def _enter_idle(self) -> None:
        self.state = "idle"
        self._idle_mark = self.fed

    def _clear_stream(self) -> None:
        """Forget all buffered audio; the next byte accepted starts a new timeline."""
        self.history.clear()
        self.history_base = self.fed
        self.unsent_from = self.fed
        self.anchor_ms = None
        self.anchor_offset = self.fed
        self._space.set()

    # ------------------------------------------------------------------ audio in

    async def accept(self, data: bytes) -> None:
        """Take one frame-aligned chunk of PCM from the source."""
        if self.discarding and time.monotonic() > self._discard_deadline:
            self.discarding = False
            self._discard_min_left = 0
        if self.discarding:
            # Read at playback speed so the source cannot race ahead while
            # the audio of the track being left is thrown away.
            await asyncio.sleep(len(data) / BYTES_PER_SECOND)
            return
        if self._discard_min_left > 0:
            self._discard_min_left -= len(data)
            return
        if self.state == "idle" and time.monotonic() < self._no_speakers_until:
            # Nothing to play on. Drop the audio at playback speed, otherwise the
            # Spotify client would race through the queue.
            await asyncio.sleep(len(data) / BYTES_PER_SECOND)
            return
        async with self.lock:
            if self.discarding or self._discard_min_left > 0:
                self._discard_min_left -= len(data)
                return
            if self.state == "idle":
                self.history += data
                self.fed += len(data)
                if self.fed - self._idle_mark >= IDLE_START_BYTES:
                    await self._begin_locked()
            elif self.state == "paused":
                self.history += data
                self.fed += len(data)
                if time.monotonic() - self._paused_at >= RESIDUAL_WINDOW_S:
                    await self._resume_locked("audio is flowing again")
            else:
                if self._ran_dry():
                    LOGGER.info("Audio feed had a gap; restarting the group in sync")
                    await self._restart_stream_locked()
                self.history += data
                self.fed += len(data)
            self._feed_wake.set()

        # Hold the source back while the senders are not keeping up.
        while not self.discarding:
            limit = LIVE_BACKLOG_LIMIT if self.state == "live" else PARKED_BACKLOG_LIMIT
            if self.fed - self.unsent_from <= limit:
                break
            self._space.clear()
            await self._space.wait()

    def _ran_dry(self) -> bool:
        """Whether everything fed so far finished playing a while ago."""
        if self.anchor_ms is None or self.unsent_from < self.fed:
            return False
        played_out_ms = self.anchor_ms + (self.fed - self.anchor_offset) * 1000 // BYTES_PER_SECOND
        return now_ms() > played_out_ms + 750

    async def _feeder(self) -> None:
        """Pump unsent audio to every sender, slice by slice."""
        while True:
            await self._feed_wake.wait()
            self._feed_wake.clear()
            while self._feed_enabled and self.state == "live" and self.unsent_from < self.fed:
                members = self._alive()
                if not members:
                    break
                if self.unsent_from >= self._feed_limit():
                    await asyncio.sleep(0.02)
                    continue
                begin = self.unsent_from - self.history_base
                data = bytes(self.history[begin : begin + SLICE_BYTES])
                epoch = self.epoch
                self._feeder_busy = True
                try:
                    await asyncio.gather(*(m.write(data) for m in members))
                finally:
                    self._feeder_busy = False
                    self._writes += 1
                if epoch != self.epoch:
                    continue  # the stream was rebased while this slice was in flight
                self.unsent_from += len(data)
                self._trim_history()
                self._space.set()

    # ------------------------------------------------------------------ members

    async def _spawn(self, speaker: SpeakerConfig) -> Member | None:
        device = self.discovery.resolve(speaker)
        if device is None or not device.address:
            return None
        member = Member(self.cfg, speaker, device, self.dacp_id, self._on_member_exit)
        try:
            await member.spawn(round(self.volume * speaker.volume_percent / 100), self.shared_ptp)
        except OSError as err:
            LOGGER.error("Could not start the sender for %s: %s", device.name, err)
            return None
        return member

    def _missing_speakers(self) -> list[SpeakerConfig]:
        active = {id(m.speaker) for m in self._alive()} | self._joining
        return [s for s in self.cfg.speakers if id(s) not in active]

    async def _spawn_missing_locked(self) -> None:
        self.members = self._alive()
        for speaker in self._missing_speakers():
            member = await self._spawn(speaker)
            if member:
                self.members.append(member)
            else:
                LOGGER.warning("Speaker '%s' was not found on the network", speaker.name)

    async def _stop_members_locked(self) -> None:
        members, self.members = self.members, []
        self._feed_enabled = False
        self.epoch += 1
        if self._anchor_task:
            self._anchor_task.cancel()
            self._anchor_task = None
        await asyncio.gather(*(m.stop() for m in members))

    async def _on_member_exit(self, member: Member) -> None:
        """A sender process ended on its own (speaker went away, connect refused...)."""
        async with self.lock:
            if member not in self.members:
                return
            self.members.remove(member)
            if self.state != "idle" and not self._alive():
                LOGGER.warning("No speakers left in the group; stopping")
                await self._stop_members_locked()
                self._clear_stream()
                self._enter_idle()
                self._no_speakers_until = time.monotonic() + 5.0
            self._space.set()

    async def _rejoin_loop(self) -> None:
        """Bring a speaker that was missing back into a running group."""
        while True:
            await asyncio.sleep(REJOIN_INTERVAL_S)
            try:
                if self.state != "live" or self.anchor_ms is None:
                    continue
                for speaker in self._missing_speakers():
                    self._joining.add(id(speaker))
                    try:
                        await self._try_rejoin(speaker)
                    finally:
                        self._joining.discard(id(speaker))
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                LOGGER.exception("Rejoin attempt failed")

    async def _try_rejoin(self, speaker: SpeakerConfig) -> None:
        candidate = await self._spawn(speaker)
        if candidate is None:
            return
        if not await candidate.wait_connected(CONNECT_TIMEOUT_S):
            await candidate.stop()
            return
        async with self.lock:
            if self.state != "live":
                await candidate.stop()
                return
            LOGGER.info("%s is back; re-syncing the group", candidate.name)
            self.members.append(candidate)
            await self._resync_locked()

    # ------------------------------------------------------------------ transitions

    async def _flush_members_locked(self) -> None:
        """Silence every speaker and empty the senders, leaving them connected."""
        self._feed_enabled = False
        self.epoch += 1
        if self._anchor_task:
            self._anchor_task.cancel()
            self._anchor_task = None
        members = self._alive()
        if not members:
            return
        writes = self._writes
        await asyncio.gather(*(m.flush() for m in members))
        deadline = time.monotonic() + 1.0
        while self._feeder_busy and time.monotonic() < deadline:
            await asyncio.sleep(0.005)
        if self._writes != writes:
            # A slice landed after the first flush; clear it so nothing stale plays.
            await asyncio.gather(*(m.flush() for m in self._alive()))

    def _enable_feed(self) -> None:
        self._feed_enabled = True
        self._schedule_anchor()
        self._feed_wake.set()
        self._space.set()

    async def _begin_locked(self) -> None:
        """Cold start: connect to the speakers and start feeding."""
        self.members = []
        await self._spawn_missing_locked()
        if not self.members:
            LOGGER.error(
                "None of the configured speakers are on the network; audio is being dropped. "
                "Known AirPlay devices: %s",
                ", ".join(sorted(d.name for d in self.discovery.devices.values())) or "none",
            )
            self._clear_stream()
            self._no_speakers_until = time.monotonic() + 5.0
            return
        LOGGER.info("Starting group: %s", ", ".join(m.name for m in self.members))
        self.state = "live"
        self.anchor_ms = None
        self.anchor_offset = self.unsent_from
        self._enable_feed()

    async def _restart_stream_locked(self) -> None:
        """Drop buffered audio and start a fresh timeline with whatever comes next."""
        if self.state == "live":
            self._cancel_timer()  # a pending end-of-queue release no longer applies
            await self._flush_members_locked()
        self._clear_stream()
        if self.state == "live":
            await self._spawn_missing_locked()
            self._enable_feed()

    async def _resync_locked(self) -> None:
        """Re-start all speakers together from the position being heard right now."""
        asked = now_ms()
        await self._flush_members_locked()
        self._rebase_to_heard(asked)
        self._enable_feed()

    def _rebase_to_heard(self, asked_ms: int) -> None:
        """Rewind the write position to the last audio that was actually heard."""
        resume_ms = asked_ms
        for member in self._alive():
            # Speakers on the splice timeline keep playing what they had queued.
            if member.flushed_head_unix_ms:
                resume_ms = max(resume_ms, member.flushed_head_unix_ms - member.delay_ms)
        offset = self._heard_offset(resume_ms)
        self.unsent_from = offset
        self.anchor_offset = offset
        self.anchor_ms = None

    async def _resume_locked(self, reason: str) -> None:
        self._cancel_timer()
        await self._spawn_missing_locked()
        if not self.members:
            self._enter_idle()
            return
        LOGGER.info("Resuming (%s)", reason)
        self.state = "live"
        self._enable_feed()

    # ------------------------------------------------------------------ source events

    async def pause(self) -> None:
        """The listener paused: silence now, remember the position that was heard."""
        async with self.lock:
            if self.state != "live":
                return
            self._cancel_timer()
            asked = now_ms()
            await self._flush_members_locked()
            self._rebase_to_heard(asked)
            self.state = "paused"
            self._paused_at = time.monotonic()
            self._space.set()
            LOGGER.info("Paused")
            self._set_timer(self.cfg.idle_disconnect_seconds, self._idle_disconnect)

    async def _idle_disconnect(self) -> None:
        async with self.lock:
            if self.state != "paused":
                return
            LOGGER.info("Paused for a while; releasing the speakers")
            await self._stop_members_locked()
            self._enter_idle()

    async def resume(self) -> None:
        """The listener pressed play."""
        async with self.lock:
            if self.state == "paused":
                await self._resume_locked("play pressed")
            elif self.state == "idle" and self.fed > self.unsent_from:
                await self._begin_locked()

    async def jump(self) -> None:
        """A different track is about to load (skip, new playlist, new selection)."""
        async with self.lock:
            was_live = self.state == "live"
            await self._restart_stream_locked()
            if was_live:
                self.discarding = True
                self._discard_deadline = time.monotonic() + JUMP_DISCARD_TIMEOUT_S

    def jump_boundary(self) -> None:
        """The new track's audio starts now."""
        if self.discarding:
            self.discarding = False
            self._discard_min_left = JUMP_BOUNDARY_DISCARD_BYTES

    async def seek(self) -> None:
        """The listener moved within the track."""
        async with self.lock:
            await self._restart_stream_locked()

    async def drain_stop(self) -> None:
        """Nothing more to play: let what is buffered play out, then release the speakers."""
        async with self.lock:
            if self.state == "paused":
                await self._stop_members_locked()
                self._clear_stream()
                self._enter_idle()
                return
            if self.state != "live":
                return
            LOGGER.info("End of queue; %.1f s left to play out", self._playout_remaining())
            self._set_timer(self._playout_remaining() + 1.5, self._finish_drain)

    def _playout_remaining(self) -> float:
        """Seconds until everything accepted so far has been heard."""
        if self.anchor_ms is None:
            return 5.0 if self.fed > self.anchor_offset else 0.0
        end_ms = self.anchor_ms + (self.fed - self.anchor_offset) * 1000 // BYTES_PER_SECOND
        return max(0.0, (end_ms - now_ms()) / 1000)

    async def _finish_drain(self) -> None:
        async with self.lock:
            if self.state != "live":
                return
            remaining = self._playout_remaining()
            waiting_to_start = self.anchor_ms is None and self.fed > self.anchor_offset
            if waiting_to_start or remaining > 0.25:
                # The last audio was still on its way in when the end was announced.
                self._set_timer(remaining + 1.5, self._finish_drain)
                return
            LOGGER.info("Finished; releasing the speakers")
            await self._stop_members_locked()
            self._clear_stream()
            self._enter_idle()

    async def hard_stop(self, reason: str) -> None:
        """Stop right now and release the speakers."""
        self.discarding = False
        self._discard_min_left = 0
        async with self.lock:
            self._cancel_timer()
            if self.state != "idle":
                LOGGER.info("Stopping (%s)", reason)
            await self._stop_members_locked()
            self._clear_stream()
            self._enter_idle()

    def set_volume(self, volume: int) -> None:
        """Set the group volume (0-100); each speaker applies its own percentage."""
        volume = max(0, min(100, volume))
        if volume == self.volume:
            return
        self.volume = volume
        for member in self._alive():
            member.set_volume(self._member_volume(member))
        LOGGER.info("Volume %d", volume)

    # ------------------------------------------------------------------ the synced start

    async def _anchor(self, epoch: int) -> None:
        """Give every speaker the same audible start instant."""
        try:
            members = self._alive()
            results = await asyncio.gather(*(m.wait_connected(CONNECT_TIMEOUT_S) for m in members))
            for member, ok in zip(members, results):
                if not ok and member.alive:
                    LOGGER.warning("%s did not connect; continuing without it", member.name)
                    await member.stop()
                    await self._on_member_exit(member)
            members = self._alive()
            if not members or epoch != self.epoch:
                return

            # Every sender must have audio buffered before an instant is chosen.
            await asyncio.gather(*(m.audio_present.wait() for m in members))
            for member in members:
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(member.clock_ready.wait(), 2.5)
            members = [m for m in members if m.alive]
            if not members or epoch != self.epoch:
                return

            # A receiver on a brand-new session needs time to get its clock and
            # buffer in place; with a short lead the opening seconds go missing.
            cold = any(not m.ever_started for m in members)
            if cold:
                lead = COLD_START_LEAD_MS
            else:
                lead = SOLO_LEAD_MS if len(members) == 1 else WARM_GROUP_LEAD_MS
            current = now_ms()
            target = current + lead
            ready_at = max(m.clock_ready_at_unix_ms for m in members)
            if ready_at:
                target = max(target, ready_at + CLOCK_READY_LEAD_MS)
            for member in members:
                # A speaker with a negative delay is commanded earlier than the
                # group instant, so the shared instant has to leave room for it.
                early = -min(0, member.delay_ms)
                target = max(target, current + lead + early)
                if member.ever_started and member.warm_lead_ms > 0:
                    target = max(
                        target, current + member.warm_lead_ms + early + SPLICE_MARGIN_MS
                    )
                if member.flushed_head_unix_ms:
                    target = max(
                        target,
                        member.flushed_head_unix_ms - member.delay_ms + SPLICE_MARGIN_MS,
                    )
                target = max(target, member.earliest_restart_unix_ms - member.delay_ms)

            corrected = target
            for _ in range(4):
                acks = await asyncio.gather(*(m.start(target + m.delay_ms) for m in members))
                corrected = max(ack - m.delay_ms for ack, m in zip(acks, members))
                if corrected <= target + 2:
                    break
                if len(members) == 1:
                    target = corrected
                    break
                LOGGER.info(
                    "A speaker could not make the start instant; moving the group %d ms later",
                    corrected - target,
                )
                target = corrected + SPLICE_MARGIN_MS
            else:
                target = corrected
                LOGGER.warning("Group start did not converge; speakers may be out of sync")

            if epoch != self.epoch:
                return
            self.anchor_ms = target
            LOGGER.info(
                "Playing on %s (audible in %d ms)",
                ", ".join(f"{m.name} [{m.route or 'route?'}, {m.delay_ms:+d} ms]" for m in members),
                target - now_ms(),
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            LOGGER.exception("Synced start failed")
