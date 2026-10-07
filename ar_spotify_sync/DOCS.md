# AR Spotify Sync

One Spotify Connect speaker that plays in sync on several AirPlay speakers, without
Music Assistant.

The add-on shows up in the Spotify app as a single device (for example "TV Room Music").
Whatever is played to it is sent to every configured AirPlay speaker with one shared
start instant, so a soundbar and an amplifier driving a subwoofer play together. Each
speaker has its own delay trim for fine alignment.

## How it works

```
Spotify app --Connect--> go-librespot --PCM--> AR Spotify Sync --+--> cliairplay --> speaker 1
                                                                 +--> cliairplay --> speaker 2
```

- **go-librespot** is the Spotify Connect receiver. It hands over decoded audio.
- **cliairplay** is Music Assistant's AirPlay sender (AirPlay 1 and AirPlay 2). One runs
  per speaker; all of them are told to make the same sample audible at the same instant.
- **AR Spotify Sync** (the Python code in `app/`) discovers the speakers, fans the audio
  out, keeps the shared timeline, and handles pause, skip, seek and volume.

## Requirements

- **Spotify Premium.** go-librespot is a reverse-engineered client. Music Assistant's own
  notes for it say it works for Premium accounts created before December 2024 and can
  break when Spotify changes its service.
- **Host networking, running as root** (the add-on default). The speakers must be on the
  same network segment as this host: mDNS and the AirPlay 2 clock (PTP, UDP ports 319
  and 320) do not cross VLANs.
- **No other AirPlay sender on this host.** Music Assistant's AirPlay provider uses the
  same PTP ports and the same shared clock. Run one or the other.
- Speakers that appear in an AirPlay picker (iPhone, iPad or Mac).

## Install on Home Assistant OS

From GitHub:

1. Settings -> Add-ons -> Add-on Store -> menu (top right) -> **Repositories**.
2. Add `https://github.com/marsh4200/ar-spotify-sync` and close the dialog.
3. Open **AR Spotify Sync** in the store and install it. The image is built on the
   device, which takes a few minutes.
4. Start the add-on, then choose **Open Web UI** and pick your speakers from the list.

Without GitHub: copy the `ar_spotify_sync` folder into the `addons` share (Samba or
SSH), choose **Check for updates** in the store menu, and install it from *Local add-ons*.

Outside Home Assistant OS, use the `docker-compose.yml` in the repository root.

To release an update, raise `version` in `config.yaml` and push. Home Assistant then
offers the update and rebuilds the image.

## Choosing the speakers

Open the add-on's page with **Open Web UI** (turn on *Show in sidebar* to keep it one
click away). It lists every AirPlay speaker found on the network:

- **Add or remove speakers** opens the list of everything found. Tap a speaker to add
  it to the group or take it out, also while music is playing. **Back** (or Escape, or
  a tap outside the list) returns to the group without changing anything.
- Each speaker in the group has a **Delay** and a **Share of group volume**. Both apply
  while music plays, so you can tune by ear.
- **Remove** takes a speaker out of the group.
- **Advanced** holds the protocol and the AirPlay 2 buffer for that speaker.

Changes are saved straight away and survive restarts. Changing a delay or adding a
speaker re-syncs the group, which drops the music out for a second or two.

The list shows AirPlay speakers, under the name they have in an AirPlay picker, because
those are the devices the add-on can play to. It is not the list of Home Assistant
media player entities: a media player that does not speak AirPlay cannot join.

Under Home Assistant the page is only reachable through Home Assistant. Run as a plain
container it is served on port 8377 without a login, so keep that port off untrusted
networks.

## Configuration

| Option | Default | Meaning |
|---|---|---|
| `device_name` | `TV Room Music` | Name shown in the Spotify app. |
| `bitrate` | `320` | Spotify quality: 96, 160 or 320 kbps. |
| `start_volume` | `25` | Group volume (0-100) used when the add-on starts. |
| `idle_disconnect_seconds` | `45` | How long after a pause the speakers are released. |
| `normalisation` | `false` | Spotify loudness normalisation. |
| `log_level` | `info` | `debug`, `info` or `warning`. |
| `speakers` | empty | Optional. Only used until a selection is saved on the web page. |

The per-speaker settings, whether set on the page or under `speakers`:

| Setting | Default | Meaning |
|---|---|---|
| `name` | | AirPlay name of the speaker. |
| `delay_ms` | `0` | Play this speaker later (positive) or earlier (negative), -1000 to 1000. |
| `volume_percent` | `100` | This speaker's share of the group volume. |
| `protocol` | `auto` | `auto`, `raop` (AirPlay 1), `airplay2` or `airplay2-compat`. |
| `buffer_ms` | `0` | AirPlay 2 only: receiver queue depth. `0` is automatic. |
| `address` | | IP address, to pick a speaker when names are ambiguous (YAML only). |
| `password` | | AirPlay password, if the speaker has one (YAML only). |

The selection made on the page is stored in `/data/speakers.json` and wins over the
`speakers` option. Delete that file to go back to the option.

## Using it

Open Spotify on any phone or computer on the same network and pick the device.
Once the device has been used it should also be listed as a source in Home Assistant's
Spotify integration, so `media_player.select_source` can start it from dashboards and
automations. That part has not been tested yet.

The Spotify volume slider is the group volume. It is applied on the speakers, not in
the audio, so it reacts immediately. Each speaker gets `group volume x volume_percent`.
AirPlay always sets the receiver's volume, so the soundbar's own volume will be where
the music left it when you go back to TV sound.

## Aligning the subwoofer

1. Open the web page, leave both delays at 0 and play something with a sharp kick drum.
2. If the bass lands late, add delay to the soundbar. If it lands early, add delay to
   the amplifier. Steps of 20 ms are a good start, then 5 ms.
3. Each change re-syncs the group after a second or two, so wait for the music to come
   back before judging it. The value is saved once it is right.

Set a low-pass crossover (around 80 Hz) on the amplifier so the sub only plays bass.

## What to expect

- Music starts about 2.5 seconds after you press play on a cold start.
- Pause is immediate. Resume continues from the exact position that was heard, in about
  half a second (up to 2.5 seconds if you resume right after pausing).
- Skip and seek take about 2.5 seconds on AirPlay 1 speakers. That is the receiver's own
  buffer: starting sooner would cut off the beginning of the track.
- The position shown in Spotify runs about 4 seconds ahead of the sound, because that
  much audio is buffered in the senders.
- Skipping to a track can clip up to about 90 ms from its very start.
- A speaker that was off when playback started joins within about 20 seconds of coming
  back, with a short gap while the group re-syncs.
- Speakers are released when the queue ends, when another Spotify device is chosen, or
  45 seconds after a pause.

## Troubleshooting

- **Sonos (or another AirPlay 2 speaker) connects but stays silent.** It is not getting
  the PTP clock. Check the log for "Shared PTP clock running", make sure nothing else on
  the host uses UDP 319/320, and that the speaker is on the same network segment.
- **An AirPlay 2 LinkPlay/Arylic device drops out or starts late.** Set `buffer_ms: 2500`
  for it, or force `protocol: raop`.
- **A speaker is not in the list.** It must be switched on, on the same network
  segment as Home Assistant, and visible in an AirPlay picker on a phone.
- **The web page does not open.** Another program on the host may be using port 8377;
  the log says so at start. Speakers can then still be set under `speakers`.
- **The device does not appear in Spotify.** The phone must be on the same network. Check
  the log for the Spotify daemon restarting, which means it cannot reach Spotify or the
  account is not accepted.
- Set `log_level: debug` to see every status line from the senders.

## Test status of 0.2.1

Tested in a lab setup, with two software AirPlay 1 receivers (shairport-sync) and a
simulated Spotify source that follows go-librespot's event order:

- both receivers play within about 1 ms of each other
- `delay_ms` shifts a speaker by exactly the configured amount (+120 and -80 ms checked)
- pause and resume lose and repeat nothing; seek, skip, gapless track change, end of
  queue, volume, deselecting the device, a speaker joining late, daemon restart and
  shutdown all behave as described above
- the web page, driven in a browser: adding from the speaker list, changing delay and
  volume share and removing a speaker all take effect while music plays and are kept
  across a restart

Not yet tested: a real Spotify account, AirPlay 2 speakers (the Sonos path, including
PTP), real hardware, the container build on Home Assistant, and the web page opened
through Home Assistant's ingress.

## Credits and licences

The container downloads two third-party programs at build time and runs them as
separate processes. Both are licensed under the GNU GPL v3; their source is at the
links below.

- [cliairplay](https://github.com/music-assistant/airplay-cli) by the Music Assistant
  project, built on [libraop](https://github.com/philippe44/libraop)
- [go-librespot](https://github.com/devgianlu/go-librespot) by devgianlu

The way the senders are driven follows Music Assistant's AirPlay provider.
