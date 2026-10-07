"""mDNS discovery of AirPlay receivers.

AirPlay devices advertise up to two services: `_raop._tcp` (AirPlay 1) and
`_airplay._tcp` (AirPlay 2). The sender needs the port and TXT records of both to
pick the right route, so both are tracked and merged per device name.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import socket
from dataclasses import dataclass, field

import ifaddr
from zeroconf import IPVersion, ServiceStateChange
from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf

from .config import SpeakerConfig

LOGGER = logging.getLogger("arsync.discovery")

RAOP_TYPE = "_raop._tcp.local."
AIRPLAY_TYPE = "_airplay._tcp.local."


@dataclass
class ServiceRecord:
    """The parts of one mDNS service record the sender needs."""

    instance: str  # full service name as advertised
    server: str
    port: int
    addresses: list[str]
    props: dict[str, str]


@dataclass
class Device:
    """One AirPlay receiver, merged from its RAOP and AirPlay records."""

    name: str
    raop: ServiceRecord | None = None
    airplay: ServiceRecord | None = None
    seen: float = field(default=0.0)

    @property
    def address(self) -> str | None:
        """Return the IPv4 address to connect to.

        A device can advertise several addresses. One on a network this host is
        directly attached to is preferred, since AirPlay timing needs a direct path.
        """
        candidates: list[str] = []
        for record in (self.airplay, self.raop):
            if record:
                candidates += [a for a in record.addresses if a not in candidates]
        if not candidates:
            return None
        routable = [a for a in candidates if not a.startswith("127.")]
        for net in local_networks():  # physical interfaces first
            for addr in routable:
                if ipaddress.ip_address(addr) in net:
                    return addr
        return (routable or candidates)[0]

    @property
    def device_id(self) -> str:
        """Return the device MAC-style id as 12 hex characters, when advertised."""
        if self.raop and "@" in self.raop.instance:
            return self.raop.instance.split("@", 1)[0].upper()
        if self.airplay and (dev := self.airplay.props.get("deviceid")):
            return dev.replace(":", "").upper()
        return ""

    @property
    def features(self) -> int:
        """Return the advertised AirPlay features bitmask (0 when unknown)."""
        value = None
        if self.airplay:
            value = self.airplay.props.get("features") or self.airplay.props.get("ft")
        if not value and self.raop:
            value = self.raop.props.get("ft")
        return parse_features(value)

    @property
    def airplay2_capable(self) -> bool:
        """Mirror the sender's own test for whether AirPlay 2 can be used."""
        if not self.airplay:
            return False
        features = self.features
        advertises_ap2 = bool((features >> 38) & 1 or (features >> 48) & 1)
        return advertises_ap2 or self.raop is None


def local_networks() -> list[ipaddress.IPv4Network]:
    """IPv4 networks this host has an interface on, physical interfaces first."""
    found: list[tuple[int, ipaddress.IPv4Network]] = []
    for adapter in ifaddr.get_adapters():
        virtual = adapter.name.startswith(("lo", "docker", "br-", "veth", "hassio"))
        for ip in adapter.ips:
            if not isinstance(ip.ip, str):
                continue  # IPv6
            with contextlib.suppress(ValueError):
                net = ipaddress.ip_network(f"{ip.ip}/{ip.network_prefix}", strict=False)
                if not net.is_loopback:
                    found.append((1 if virtual else 0, net))
    return [net for _, net in sorted(found, key=lambda item: item[0])]


def parse_features(value: str | None) -> int:
    """Parse an AirPlay features TXT value ('0x...,0x...') into one integer."""
    if not value:
        return 0
    try:
        parts = value.split(",")
        features = int(parts[0], 16)
        if len(parts) > 1:
            features |= int(parts[1], 16) << 32
    except ValueError:
        return 0
    return features


def display_name(service_type: str, instance: str) -> str:
    """Turn an mDNS instance name into the name people see in an AirPlay picker."""
    name = instance[: -len(service_type) - 1] if instance.endswith(service_type) else instance
    if service_type == RAOP_TYPE and "@" in name:
        name = name.split("@", 1)[1]
    return name


class Discovery:
    """Keeps a live table of the AirPlay receivers on the network."""

    def __init__(self) -> None:
        self._zc: AsyncZeroconf | None = None
        self._browser: AsyncServiceBrowser | None = None
        self.devices: dict[str, Device] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    async def start(self) -> None:
        """Start browsing."""
        self._zc = AsyncZeroconf(ip_version=IPVersion.V4Only)
        self._browser = AsyncServiceBrowser(
            self._zc.zeroconf, [RAOP_TYPE, AIRPLAY_TYPE], handlers=[self._on_change]
        )

    async def stop(self) -> None:
        """Stop browsing."""
        if self._browser:
            await self._browser.async_cancel()
        if self._zc:
            await self._zc.async_close()

    def _on_change(
        self, zeroconf: object, service_type: str, name: str, state_change: ServiceStateChange
    ) -> None:
        if state_change is ServiceStateChange.Removed:
            return  # keep the last known record; a failed connect is handled by the caller
        task = asyncio.ensure_future(self._resolve(service_type, name))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _resolve(self, service_type: str, name: str) -> None:
        assert self._zc is not None
        info = AsyncServiceInfo(service_type, name)
        if not await info.async_request(self._zc.zeroconf, 3000):
            return
        props: dict[str, str] = {}
        for key, value in (info.properties or {}).items():
            if value is None:
                continue
            try:
                props[key.decode("utf-8")] = value.decode("utf-8")
            except UnicodeDecodeError:
                continue
        record = ServiceRecord(
            instance=name,
            server=info.server or "",
            port=info.port or 0,
            addresses=info.parsed_addresses(IPVersion.V4Only),
            props=props,
        )
        shown = display_name(service_type, name)
        device = self.devices.get(shown.casefold())
        if device is None:
            device = self.devices[shown.casefold()] = Device(name=shown)
            LOGGER.info("Found AirPlay device: %s (%s)", shown, record.addresses or "no address")
        if service_type == RAOP_TYPE:
            device.raop = record
        else:
            device.airplay = record
        device.seen = asyncio.get_running_loop().time()

    def resolve(self, speaker: SpeakerConfig) -> Device | None:
        """Find the discovered device for a configured speaker."""
        if speaker.address:
            for device in self.devices.values():
                records = [r for r in (device.raop, device.airplay) if r]
                if any(speaker.address in r.addresses for r in records):
                    return device
        wanted = speaker.name.casefold()
        if wanted in self.devices:
            return self.devices[wanted]
        matches = sorted(
            (d for key, d in self.devices.items() if wanted and wanted in key),
            key=lambda d: d.name,
        )
        if len(matches) > 1:
            LOGGER.warning(
                "Speaker name '%s' matches several devices (%s); using '%s'. "
                "Use the full name to pick one.",
                speaker.name,
                ", ".join(d.name for d in matches),
                matches[0].name,
            )
        return matches[0] if matches else None


def source_ip_for(target: str) -> str | None:
    """Return the local address the OS would use to reach a target address."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect((target, 9))
            return str(sock.getsockname()[0])
    except OSError:
        return None
