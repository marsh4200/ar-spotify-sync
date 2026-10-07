"""The add-on's web page: pick the speakers from what is on the network.

Served through Home Assistant ingress (the add-on's "Open Web UI" button and its
sidebar entry). The page lists every AirPlay speaker discovered, lets the user
add them to the group from a dropdown, and trims each speaker's delay and volume
share while music is playing. The selection is stored in /data/speakers.json.
"""

from __future__ import annotations

import logging
import os

from aiohttp import web

from .config import Config, save_speakers, speaker_from_dict, speaker_to_dict
from .discovery import Discovery
from .group import Group

LOGGER = logging.getLogger("arsync.web")

# Under Home Assistant every ingress request comes from the Supervisor.
INGRESS_SOURCES = {"172.30.32.2", "127.0.0.1", "::1"}
EDITABLE_KEYS = ("name", "delay_ms", "volume_percent", "protocol", "buffer_ms")


class WebUI:
    """Small HTTP server with the page and its two API calls."""

    def __init__(self, cfg: Config, group: Group, discovery: Discovery) -> None:
        self.cfg = cfg
        self.group = group
        self.discovery = discovery
        self._runner: web.AppRunner | None = None
        self._restricted = bool(os.environ.get("SUPERVISOR_TOKEN"))
        self._refused: set[str | None] = set()
        with open(os.path.join(os.path.dirname(__file__), "ui.html"), encoding="utf-8") as handle:
            self._page = handle.read()

    async def start(self) -> None:
        """Start listening."""
        app = web.Application(middlewares=[self._guard])
        app.add_routes(
            [
                web.get("/", self._index),
                web.get("/api/state", self._state),
                web.post("/api/speakers", self._set_speakers),
            ]
        )
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        try:
            await web.TCPSite(self._runner, "0.0.0.0", self.cfg.ui_port).start()
        except OSError as err:
            LOGGER.error(
                "The web page could not start on port %d (%s). "
                "Speakers can still be set in the add-on configuration.",
                self.cfg.ui_port,
                err,
            )
            return
        LOGGER.info(
            "Web page ready: %s",
            "use 'Open Web UI' on the add-on page"
            if self._restricted
            else f"http://<this host>:{self.cfg.ui_port}/",
        )

    async def stop(self) -> None:
        """Stop listening."""
        if self._runner:
            await self._runner.cleanup()

    @web.middleware
    async def _guard(self, request: web.Request, handler):  # type: ignore[no-untyped-def]
        # The add-on runs on the host network, so the port is reachable from the
        # LAN. Under Home Assistant only the Supervisor's ingress proxy is let in.
        if self._restricted and request.remote not in INGRESS_SOURCES:
            if request.remote not in self._refused:
                self._refused.add(request.remote)
                LOGGER.warning("Refused a web page request from %s", request.remote)
            raise web.HTTPForbidden(text="Open this page from Home Assistant.")
        return await handler(request)

    async def _index(self, request: web.Request) -> web.Response:
        return web.Response(
            text=self._page, content_type="text/html", headers={"Cache-Control": "no-store"}
        )

    def _snapshot(self) -> dict:
        data = self.group.status()
        data["device_name"] = self.cfg.device_name
        data["discovered"] = [
            {
                "name": device.name,
                "address": device.address,
                "kind": "AirPlay 2" if device.airplay2_capable else "AirPlay 1",
            }
            for device in sorted(self.discovery.devices.values(), key=lambda d: d.name.casefold())
        ]
        return data

    async def _state(self, request: web.Request) -> web.Response:
        return web.json_response(self._snapshot(), headers={"Cache-Control": "no-store"})

    async def _set_speakers(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
            entries = body["speakers"]
            if not isinstance(entries, list) or len(entries) > 16:
                raise ValueError("speakers must be a list of at most 16 entries")
        except (ValueError, KeyError, TypeError) as err:
            raise web.HTTPBadRequest(text=f"Invalid request: {err}") from err

        existing = {s.name.casefold(): s for s in self.cfg.speakers}
        wanted = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise web.HTTPBadRequest(text="Each speaker must be an object.")
            name = str(entry.get("name") or "").strip()
            # Keep what the page does not edit (address, password) from the stored entry.
            merged = speaker_to_dict(existing[name.casefold()]) if name.casefold() in existing else {}
            merged.update({k: entry[k] for k in EDITABLE_KEYS if k in entry})
            speaker = speaker_from_dict(merged)
            if speaker is None:
                raise web.HTTPBadRequest(text=f"Invalid speaker entry: {name or '(no name)'}")
            wanted.append(speaker)

        await self.group.apply_speakers(wanted)
        try:
            save_speakers(self.cfg)
        except OSError as err:
            LOGGER.error("Could not save the speaker selection: %s", err)
            raise web.HTTPInternalServerError(text="The selection could not be saved.") from err
        return web.json_response(self._snapshot())
