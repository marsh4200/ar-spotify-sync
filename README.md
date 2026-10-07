# AR Spotify Sync

A Home Assistant add-on that presents one Spotify Connect speaker and plays it in sync
on several AirPlay speakers of different brands, for example a Sonos soundbar and an
Arylic amplifier driving a subwoofer. No Music Assistant needed.

## Install

[![Add this repository to Home Assistant](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fmarsh4200%2Far-spotify-sync)

Or by hand:

1. Settings -> Add-ons -> Add-on Store -> menu (top right) -> **Repositories**.
2. Add `https://github.com/marsh4200/ar-spotify-sync`.
3. Install **AR Spotify Sync**, set the speaker names on the Configuration tab and start it.

Needs Home Assistant OS or Supervised, on amd64 or aarch64. Full instructions,
options and troubleshooting are in [the add-on docs](ar_spotify_sync/DOCS.md).

## Status

Version 0.1.0 has been tested in a lab setup only: two software AirPlay 1 receivers and
a simulated Spotify source. Real Spotify, AirPlay 2 speakers and the container build on
Home Assistant are not yet proven. See "Test status" in the docs.

## Repository layout

- `ar_spotify_sync/` - the add-on
- `repository.yaml` - makes this repository installable from the Add-on Store
- `docker-compose.yml`, `options.example.json` - running it as a plain container
- `dev/` - lab tools used to test sync without Spotify or real speakers
- `.github/workflows/build.yml` - builds the image for both architectures on every push
