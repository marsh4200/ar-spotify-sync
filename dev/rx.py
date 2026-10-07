#!/usr/bin/env python3
"""Test AirPlay receiver wrapper.

Runs one shairport-sync instance with the stdout backend and logs, for every tone
burst that comes out of it, the wall-clock time it started, how long it lasted and
its peak level.

usage: rx.py NAME PORT LOGFILE
"""
import os, struct, subprocess, sys, time

name, port, logfile = sys.argv[1], sys.argv[2], sys.argv[3]
THRESH = 150
GAP = 150   # quiet frames that end a burst
FRAME = 4

conf = f"/tmp/shairport-{port}.conf"
with open(conf, "w") as fh:
    fh.write('general = { name = "%s"; port = %s; udp_port_base = %d; interpolation = "basic"; audio_backend_buffer_desired_length_in_seconds = 0.05; };\n'
             % (name, port, 6000 + (int(port) % 100) * 20))

proc = subprocess.Popen(["shairport-sync", "-c", conf, "-o", "stdout", "-u", "-v"],
                        stdout=subprocess.PIPE, stderr=open(logfile + ".err", "w"), bufsize=0)
log = open(logfile, "w", buffering=1)
log.write(f"# receiver {name} port {port} started {time.time():.3f}\n")

in_burst = False
quiet = 10**9
start_t = 0.0; start_frame = 0; last_loud = 0; peak = 0
total = 0
fd = proc.stdout.fileno()
buf = b""
while True:
    chunk = os.read(fd, 4096)
    now = time.time()
    if not chunk:
        break
    buf += chunk
    n = len(buf) // FRAME
    if not n:
        continue
    data, buf = buf[: n * FRAME], buf[n * FRAME:]
    samples = struct.unpack("<%dh" % (n * 2), data)
    for i in range(n):
        amp = abs(samples[2 * i])
        frame = total + i
        if amp > THRESH:
            if not in_burst:
                in_burst = True
                start_t = now - (n - i) / 44100.0
                start_frame = frame
                peak = 0
            last_loud = frame
            quiet = 0
            if amp > peak:
                peak = amp
        else:
            quiet += 1
            if in_burst and quiet > GAP:
                in_burst = False
                dur = (last_loud - start_frame + 1) * 1000.0 / 44100
                log.write(f"BURST t={start_t:.4f} dur_ms={dur:.1f} peak={peak}\n")
    total += n
