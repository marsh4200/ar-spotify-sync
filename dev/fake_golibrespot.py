#!/usr/bin/env python3
"""Stand-in for go-librespot, for testing without Spotify.

Mimics the parts of go-librespot v0.9.0 that AR Spotify Sync relies on, following
its source: PCM written to /dev/stdout as fast as it is read, the /events WebSocket
with the same event names and ordering, the 'loaded' log line on stderr, and
POST /player/volume. Playback is driven by text commands on a local TCP port.

Tracks are silence with a 1 kHz burst every 0.5 s. The burst length encodes the
track and the burst number so a receiver log shows exactly what was heard.
  track A (12 s): burst k lasts 10 + 4*(k % 10) ms
  track B (8 s):  burst k lasts 60 + 4*(k % 10) ms
"""
import asyncio, json, math, os, struct, sys, threading, time
from aiohttp import web

cfg_dir = sys.argv[sys.argv.index("--config_dir") + 1]
cfg = json.load(open(os.path.join(cfg_dir, "config.yml")))
PORT = cfg["server"]["port"]
CTRL_PORT = int(os.environ.get("FAKE_CTRL_PORT", "3999"))
LOAD_MS = int(os.environ.get("FAKE_LOAD_MS", "200"))     # time to fetch a track that was not prefetched
STATE_MS = int(os.environ.get("FAKE_STATE_MS", "150"))   # round trip to Spotify before events are emitted
SR = 44100

def make_track(seconds, base):
    out = bytearray(seconds * SR * 4)
    k = 0
    t = 0.25
    while t < seconds - 0.1:
        n = int((base + 4 * (k % 10)) / 1000 * SR)
        s0 = int(t * SR)
        for i in range(n):
            v = int(20000 * math.sin(2 * math.pi * 1000 * i / SR))
            struct.pack_into("<hh", out, (s0 + i) * 4, v, v)
        k += 1
        t += 0.5
    return bytes(out)

def make_tone(seconds):
    """Continuous 1 kHz tone with a 10 ms gap every 250 ms (so the receiver logs a burst per 250 ms)."""
    out = bytearray(seconds * SR * 4)
    for i in range(seconds * SR):
        if (i % (SR // 4)) < SR // 100:
            continue
        v = int(20000 * math.sin(2 * math.pi * 1000 * i / SR))
        struct.pack_into("<hh", out, i * 4, v, v)
    return bytes(out)

TRACKS = {"A": make_track(12, 10), "B": make_track(8, 60), "T": make_tone(20), "U": make_tone(20)}
ORDER = ["A", "B"]

def log(msg):
    sys.stderr.write('time="%s" level=info msg="%s"\n' % (time.strftime("%H:%M:%S"), msg))
    sys.stderr.flush()

class Player:
    def __init__(self):
        self.lock = threading.Lock()
        self.cond = threading.Condition(self.lock)
        self.track = None
        self.pos = 0
        self.paused = True
        self.ended = []          # tracks that finished naturally, for the event loop
        self.volume = 100
        threading.Thread(target=self.output_loop, daemon=True).start()

    def output_loop(self):
        fd = os.open("/dev/stdout", os.O_WRONLY)
        while True:
            with self.cond:
                while self.paused or self.track is None:
                    self.cond.wait()
                data = TRACKS[self.track][self.pos:self.pos + 8192]
                self.pos += len(data)
                if self.pos >= len(TRACKS[self.track]):
                    # gapless: switch to the next track inside the output path, then report
                    done = self.track
                    idx = ORDER.index(done) + 1 if done in ORDER else len(ORDER)
                    if idx < len(ORDER):
                        self.track, self.pos = ORDER[idx], 0
                    else:
                        self.track, self.paused = None, True
                    self.ended.append(done)
            # The write happens outside the lock here; real go-librespot holds its lock while
            # writing, which only makes its controls wait for one 8 KiB write to finish.
            if data:
                os.write(fd, data)

player = Player()
sockets = set()

async def emit(type_, data=None):
    msg = json.dumps({"type": type_, "data": data or {}})
    for ws in list(sockets):
        try:
            await ws.send_str(msg)
        except Exception:
            sockets.discard(ws)

async def watch_natural_end():
    while True:
        await asyncio.sleep(0.01)
        while player.ended:
            done = player.ended.pop(0)
            await emit("not_playing", {"uri": "spotify:track:" + done})
            nxt = player.track
            if nxt is None:
                await emit("stopped")
                continue
            await emit("will_play", {"uri": "spotify:track:" + nxt})
            log('loaded track \\"%s\\" (paused: false, position: 0ms, prefetched: true)' % nxt)
            await asyncio.sleep(STATE_MS / 1000)
            await emit("metadata", {"uri": "spotify:track:" + nxt, "name": "Track " + nxt, "artist_names": ["Test"]})
            await emit("playing", {"uri": "spotify:track:" + nxt, "resume": False})

async def load(track, paused=False):
    await emit("will_play", {"uri": "spotify:track:" + track})
    await asyncio.sleep(LOAD_MS / 1000)            # old track keeps being written meanwhile
    with player.cond:
        player.track, player.pos, player.paused = track, 0, paused
        player.cond.notify_all()
    log('loaded track \\"%s\\" (paused: %s, position: 0ms, prefetched: false)' % (track, str(paused).lower()))
    await asyncio.sleep(STATE_MS / 1000)
    await emit("metadata", {"uri": "spotify:track:" + track, "name": "Track " + track, "artist_names": ["Test"]})
    if not paused:
        await emit("playing", {"uri": "spotify:track:" + track, "resume": False})

async def command(line):
    parts = line.split()
    if not parts:
        return
    cmd = parts[0]
    if cmd == "activate":
        await emit("active")
        await emit("volume", {"value": player.volume, "max": 100})
    elif cmd == "play":
        await load(parts[1])
    elif cmd == "pause":
        with player.cond:
            player.paused = True
        await asyncio.sleep(STATE_MS / 1000)
        await emit("paused", {})
    elif cmd == "resume":
        with player.cond:
            player.paused = False
            player.cond.notify_all()
        await asyncio.sleep(STATE_MS / 1000)
        await emit("playing", {"resume": True})
    elif cmd == "seek":
        with player.cond:
            player.pos = int(float(parts[1]) * SR) * 4
        await asyncio.sleep(STATE_MS / 1000)
        await emit("seek", {"position": int(float(parts[1]) * 1000)})
    elif cmd == "volume":
        player.volume = int(parts[1])
        await emit("volume", {"value": player.volume, "max": 100})
    elif cmd == "inactive":
        with player.cond:
            player.paused = True
        await emit("inactive")

async def ctrl(reader, writer):
    while line := await reader.readline():
        await command(line.decode().strip())
        writer.write(b"ok\n"); await writer.drain()

async def events(request):
    ws = web.WebSocketResponse(); await ws.prepare(request); sockets.add(ws)
    async for _ in ws:
        pass
    sockets.discard(ws); return ws

async def set_volume(request):
    body = await request.json()
    player.volume = int(body["volume"])
    await emit("volume", {"value": player.volume, "max": 100})
    return web.json_response({})

async def main():
    app = web.Application()
    app.add_routes([web.get("/", lambda r: web.json_response({})), web.get("/events", events),
                    web.post("/player/volume", set_volume)])
    runner = web.AppRunner(app); await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", PORT).start()
    await asyncio.start_server(ctrl, "127.0.0.1", CTRL_PORT)
    log("running fake go-librespot")
    await watch_natural_end()

asyncio.run(main())
