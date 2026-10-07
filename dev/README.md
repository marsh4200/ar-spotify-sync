# Lab tools

These test the add-on without Spotify and without real speakers. They need
`shairport-sync` (classic AirPlay 1 build, with the `stdout` backend) and a running
Avahi daemon on the test machine.

- `rx.py NAME PORT LOGFILE` runs one software AirPlay receiver and logs every tone burst
  that comes out of it, with its wall-clock time, length and peak level.
- `fake_golibrespot.py` stands in for go-librespot: same config file, event names and
  order, log line and volume endpoint. It plays test tracks whose bursts encode the
  track and position. Point `ARSYNC_GOLIBRESPOT_BIN` at it.
- `ctl.py activate | play A | pause | resume | seek 9 | volume 40 | inactive` drives it.
- `analyze.py rx1.log rx2.log [since_unix_time]` compares what the two receivers played.

Run the add-on code directly with:

```
ARSYNC_OPTIONS=./options.json ARSYNC_DATA_DIR=./data ARSYNC_RUN_DIR=./run \
ARSYNC_CLIAIRPLAY_BIN=/path/to/cliairplay ARSYNC_GOLIBRESPOT_BIN=./dev/fake_golibrespot.py \
python3 ar_spotify_sync/app/main.py
```

The software receiver must output at its true play time for the timings to mean
anything, which is why `rx.py` sets `audio_backend_buffer_desired_length_in_seconds`.
