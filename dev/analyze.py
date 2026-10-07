#!/usr/bin/env python3
"""Compare what two test receivers played. usage: analyze.py rx1.log rx2.log [since_unix]"""
import sys

def load(path, since):
    out = []
    for line in open(path):
        if not line.startswith("BURST"):
            continue
        f = dict(p.split("=") for p in line.split()[1:])
        t, dur, peak = float(f["t"]), float(f["dur_ms"]), int(f["peak"])
        if t < since:
            continue
        track = "A" if dur < 55 else "B"
        base = 10 if track == "A" else 60
        idx = round((dur - base) / 4) % 10
        out.append((t, track, idx, peak, dur))
    return out

since = float(sys.argv[3]) if len(sys.argv) > 3 else 0
a, b = load(sys.argv[1], since), load(sys.argv[2], since)
print(f"{'time':>10}  what  peak1 peak2   rx2-rx1(ms)   gap since previous (s)")
t0 = a[0][0] if a else 0
j = 0
diffs = []
prev = None
for (t, track, idx, peak, dur) in a:
    # nearest burst in b with the same label
    best = None
    for (t2, track2, idx2, peak2, dur2) in b:
        if track2 == track and idx2 == idx and abs(t2 - t) < 0.24:
            if best is None or abs(t2 - t) < abs(best[0] - t):
                best = (t2, peak2)
    gap = f"{t - prev:6.3f}" if prev else "      "
    prev = t
    if best:
        d = (best[0] - t) * 1000
        diffs.append(d)
        print(f"{t - t0:10.3f}  {track}{idx}   {peak:5d} {best[1]:5d}   {d:+8.1f}      {gap}")
    else:
        print(f"{t - t0:10.3f}  {track}{idx}   {peak:5d}     -   (not on rx2)   {gap}")
if diffs:
    diffs.sort()
    print(f"\n{len(diffs)} matched bursts: offset rx2-rx1 min {diffs[0]:+.1f} ms, median {diffs[len(diffs)//2]:+.1f} ms, max {diffs[-1]:+.1f} ms")
print(f"rx1 heard {len(a)} bursts, rx2 heard {len(b)}")
