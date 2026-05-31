#!/usr/bin/env python3
from __future__ import annotations

import fcntl
import http.client
import ipaddress
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import signal
import socket
import socketserver
import struct
import tempfile
import threading
import time
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Iterable, NamedTuple, Optional, Tuple
import subprocess
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape as xml_escape


LOG = logging.getLogger("natd")

DEFAULT_SOCKET_PATH = "/run/casanode-natd/control.sock"
DEFAULT_STATE_PATH = "/var/lib/casanode-natd/state.json"
DEFAULT_UPSTREAM_INTERFACE = "eth0"
DEFAULT_DISCOVER_TIMEOUT = 2.0
DEFAULT_HTTP_TIMEOUT = 5.0
DEFAULT_LEASE_SECONDS = 3600
DEFAULT_REFRESH_MARGIN = 120
DEFAULT_HEALTH_INTERVAL = 300
DEFAULT_RETRY_INITIAL = 5
DEFAULT_RETRY_MAX = 300
BACKEND_ORDER = ("upnp-igd", "pcp", "nat-pmp")

UPNP_SERVICE_TYPES = (
    "urn:schemas-upnp-org:service:WANIPConnection:2",
    "urn:schemas-upnp-org:service:WANIPConnection:1",
    "urn:schemas-upnp-org:service:WANPPPConnection:2",
    "urn:schemas-upnp-org:service:WANPPPConnection:1",
)


def _now() -> float:
    return time.time()


def _parse_bool(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off", ""}
    return default


def _parse_int(value: Any, *, field: str, minimum: int = 1, maximum: int = 65535) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be an integer") from exc
    if parsed < minimum or parsed > maximum:
        raise ValueError(f"{field} must be between {minimum} and {maximum}")
    return parsed


def _normalize_protocol(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("protocol must be a string")
    protocol = value.strip().upper()
    if protocol not in {"TCP", "UDP"}:
        raise ValueError("protocol must be TCP or UDP")
    return protocol


def _normalize_description(value: Any, mapping_id: str) -> str:
    if value is None:
        value = f"casanode:{mapping_id}"
    if not isinstance(value, str):
        raise ValueError("description must be a string")
    desc = value.strip()
    if not desc:
        desc = f"casanode:{mapping_id}"
    return desc[:63]


def _normalize_ipv4(value: Any, *, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    try:
        return str(ipaddress.IPv4Address(value.strip()))
    except ipaddress.AddressValueError as exc:
        raise ValueError(f"{field} must be a valid IPv4 address") from exc


def _parse_xml_text(node: ET.Element, tag: str, namespaces: Dict[str, str]) -> Optional[str]:
    child = node.find(tag, namespaces)
    if child is None or child.text is None:
        return None
    return child.text.strip()


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=str(path.parent), delete=False) as handle:
        handle.write(content)
        temp_name = handle.name
    os.replace(temp_name, path)


def _get_interface_ipv4(interface: str) -> Optional[str]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        request = struct.pack("256s", interface.encode("utf-8")[:15])
        response = fcntl.ioctl(sock.fileno(), 0x8915, request)  # SIOCGIFADDR
        return socket.inet_ntoa(response[20:24])
    except OSError:
        return None
    finally:
        sock.close()


def _get_interface_gateway_ipv4(interface: str) -> Optional[str]:
    try:
        with open("/proc/net/route", "r", encoding="utf-8") as handle:
            for line in handle:
                parts = line.split()
                if len(parts) < 3 or parts[0] != interface:
                    continue
                destination = parts[1]
                gateway = parts[2]
                flags = int(parts[3], 16)
                if destination != "00000000" or not (flags & 0x2):
                    continue
                gateway_int = int(gateway, 16)
                return socket.inet_ntoa(struct.pack("<I", gateway_int))
    except OSError:
        return None
    return None


def configure_logging() -> None:
    log_dir = Path("/var/log/casanode")
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "natd.log"

    logger = logging.getLogger("natd")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.propagate = False

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    file_handler = RotatingFileHandler(log_file, maxBytes=2_000_000, backupCount=5)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)


def _iptables(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["iptables", "-w", "5", *args], text=True, capture_output=True, check=False)


class FirewallManager:
    chain_name = "CASANODE_PORTS"

    def __init__(self, interface: str = "eth0"):
        self.interface = interface

    def ensure_chain(self) -> None:
        _iptables(["-N", self.chain_name])
        _iptables(["-F", self.chain_name])

    def sync(self, mappings: Iterable[Dict[str, Any]]) -> None:
        self.ensure_chain()
        _iptables(["-F", self.chain_name])
        for mapping in mappings:
            if not mapping.get("enabled", True):
                continue
            protocol = mapping["protocol"].lower()
            internal_port = str(mapping["internal_port"])
            rule = [
                "-A",
                self.chain_name,
                "-p",
                protocol,
                "--dport",
                internal_port,
                "-m",
                "comment",
                "--comment",
                f"casanode:{mapping['id']}",
                "-j",
                "ACCEPT",
            ]
            result = _iptables(rule)
            if result.returncode != 0:
                raise RuntimeError(f"failed to add firewall rule for {mapping['id']}: {result.stderr.strip()}")

    def clear(self) -> None:
        self.ensure_chain()
        _iptables(["-F", self.chain_name])


def _ipv4_to_mapped_bytes(ip: str) -> bytes:
    return ipaddress.IPv4Address(ip).packed + b"\x00" * 12


def _mapped_bytes_to_ipv4(data: bytes) -> str:
    if len(data) != 16:
        raise ValueError("expected 16 bytes for mapped address")
    return str(ipaddress.IPv4Address(data[-4:]))


class BackendError(RuntimeError):
    pass


class RouterBackend:
    name = "base"

    def external_ip(self) -> Optional[str]:
        return None

    def apply(self, mapping: Dict[str, Any], previous: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        raise NotImplementedError

    def delete(self, record: Dict[str, Any]) -> None:
        raise NotImplementedError


class UpnpIgdBackend(RouterBackend):
    name = "upnp-igd"

    def __init__(self, upstream_interface: str, discover_timeout: float, http_timeout: float):
        self.client = UpnpClient(discover_timeout, http_timeout, upstream_interface)
        self._gateway: Optional[GatewayEndpoint] = None

    def _ensure_gateway(self) -> GatewayEndpoint:
        if self._gateway is None:
            self._gateway = self.client.discover()
        return self._gateway

    def external_ip(self) -> Optional[str]:
        try:
            return self.client.external_ip(self._ensure_gateway())
        except Exception:
            return None

    def apply(self, mapping: Dict[str, Any], previous: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        gateway = self._ensure_gateway()
        record = deepcopy(mapping)
        if previous and previous.get("backend") == self.name and self._same_port_key(previous, mapping):
            try:
                self.client.delete_port_mapping(gateway, previous)
            except Exception:
                LOG.debug("igd pre-delete failed for %s", mapping["id"], exc_info=True)
        self.client.add_port_mapping(gateway, mapping)
        record.update(
            {
                "backend": self.name,
                "external_port": mapping["external_port"],
                "external_ip": self.client.external_ip(gateway),
            }
        )
        return record

    def delete(self, record: Dict[str, Any]) -> None:
        gateway = self._ensure_gateway()
        try:
            self.client.delete_port_mapping(gateway, record)
        except Exception as exc:
            raise BackendError(str(exc)) from exc

    @staticmethod
    def _same_port_key(left: Dict[str, Any], right: Dict[str, Any]) -> bool:
        return left.get("protocol") == right.get("protocol") and left.get("external_port") == right.get("external_port")


class _UdpGatewayBackend(RouterBackend):
    port = 5351

    def __init__(self, upstream_interface: str, timeout: float):
        self.upstream_interface = upstream_interface
        self.timeout = timeout
        self.source_ip = _get_interface_ipv4(upstream_interface)
        self.gateway_ip = _get_interface_gateway_ipv4(upstream_interface)
        if not self.source_ip:
            raise BackendError(f"unable to determine IPv4 address for {upstream_interface}")
        if not self.gateway_ip:
            raise BackendError(f"unable to determine default gateway for {upstream_interface}")

    def _send(self, payload: bytes) -> bytes:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(self.timeout)
        try:
            sock.bind((self.source_ip, 0))
            sock.sendto(payload, (self.gateway_ip, self.port))
            data, _ = sock.recvfrom(4096)
            return data
        except OSError as exc:
            raise BackendError(str(exc)) from exc
        finally:
            sock.close()


class NatPmpBackend(_UdpGatewayBackend):
    name = "nat-pmp"

    def _request_external_ip(self) -> str:
        response = self._send(struct.pack("!BBH", 0, 0, 0))
        if len(response) < 12:
            raise BackendError("short NAT-PMP external address response")
        version, opcode = response[0], response[1]
        result = struct.unpack("!H", response[2:4])[0]
        if version != 0 or opcode != 128 or result != 0:
            raise BackendError(f"NAT-PMP external address request failed with result {result}")
        return socket.inet_ntoa(response[8:12])

    def external_ip(self) -> Optional[str]:
        try:
            return self._request_external_ip()
        except BackendError:
            return None

    def _map(self, protocol: str, internal_port: int, external_port: int, lifetime: int) -> Dict[str, Any]:
        opcode = 1 if protocol == "UDP" else 2
        payload = struct.pack("!BBHHHI", 0, opcode, 0, internal_port, external_port, lifetime)
        response = self._send(payload)
        if len(response) < 16:
            raise BackendError("short NAT-PMP mapping response")
        version, resp_opcode = response[0], response[1]
        result = struct.unpack("!H", response[2:4])[0]
        if version != 0 or resp_opcode != (opcode | 0x80):
            raise BackendError("invalid NAT-PMP response")
        if result != 0:
            raise BackendError(f"NAT-PMP mapping failed with result {result}")
        assigned_internal = struct.unpack("!H", response[8:10])[0]
        assigned_external = struct.unpack("!H", response[10:12])[0]
        assigned_lifetime = struct.unpack("!I", response[12:16])[0]
        return {
            "assigned_internal_port": assigned_internal,
            "assigned_external_port": assigned_external,
            "lifetime": assigned_lifetime,
            "external_ip": self._request_external_ip(),
        }

    def apply(self, mapping: Dict[str, Any], previous: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if previous and previous.get("backend") == self.name and self._same_port_key(previous, mapping):
            try:
                self.delete(previous)
            except BackendError:
                LOG.debug("nat-pmp pre-delete failed for %s", mapping["id"], exc_info=True)
        assigned = self._map(mapping["protocol"], mapping["internal_port"], mapping["external_port"], mapping["lease_seconds"])
        record = deepcopy(mapping)
        record.update({"backend": self.name, **assigned})
        return record

    def delete(self, record: Dict[str, Any]) -> None:
        protocol = record["protocol"]
        opcode = 1 if protocol == "UDP" else 2
        internal_port = int(record["internal_port"])
        external_port = int(record.get("assigned_external_port") or record.get("external_port") or 0)
        payload = struct.pack("!BBHHHI", 0, opcode, 0, internal_port, external_port, 0)
        response = self._send(payload)
        if len(response) < 16:
            raise BackendError("short NAT-PMP deletion response")
        result = struct.unpack("!H", response[2:4])[0]
        if result != 0:
            raise BackendError(f"NAT-PMP deletion failed with result {result}")

    @staticmethod
    def _same_port_key(left: Dict[str, Any], right: Dict[str, Any]) -> bool:
        return left.get("protocol") == right.get("protocol") and left.get("external_port") == right.get("external_port")


class PcpBackend(_UdpGatewayBackend):
    name = "pcp"

    def _map(self, protocol: str, internal_port: int, external_port: int, lifetime: int, nonce: bytes) -> Dict[str, Any]:
        opcode = 1
        protocol_num = 17 if protocol == "UDP" else 6
        client_ip = _ipv4_to_mapped_bytes(self.source_ip)
        common = struct.pack("!BBH I 16s", 2, opcode, 0, lifetime, client_ip)
        body = nonce + struct.pack("!B3sHH16s", protocol_num, b"\x00\x00\x00", internal_port, external_port, b"\x00" * 16)
        response = self._send(common + body)
        if len(response) < 60:
            raise BackendError("short PCP mapping response")
        version, resp_byte, reserved, result = struct.unpack("!BBBB", response[:4])
        resp_opcode = resp_byte & 0x7F
        is_response = bool(resp_byte & 0x80)
        if version != 2 or not is_response or resp_opcode != opcode:
            raise BackendError("invalid PCP response")
        if result != 0:
            raise BackendError(f"PCP mapping failed with result {result}")
        assigned_lifetime = struct.unpack("!I", response[4:8])[0]
        assigned_external_port = struct.unpack("!H", response[42:44])[0]
        assigned_external_ip = response[44:60]
        return {
            "assigned_internal_port": internal_port,
            "assigned_external_port": assigned_external_port,
            "lifetime": assigned_lifetime,
            "external_ip": _mapped_bytes_to_ipv4(assigned_external_ip),
            "nonce": nonce.hex(),
        }

    def external_ip(self) -> Optional[str]:
        return None

    def apply(self, mapping: Dict[str, Any], previous: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        nonce = os.urandom(12)
        if previous and previous.get("backend") == self.name and self._same_port_key(previous, mapping):
            try:
                self.delete(previous)
            except BackendError:
                LOG.debug("pcp pre-delete failed for %s", mapping["id"], exc_info=True)
        assigned = self._map(mapping["protocol"], mapping["internal_port"], mapping["external_port"], mapping["lease_seconds"], nonce)
        record = deepcopy(mapping)
        record.update({"backend": self.name, **assigned})
        return record

    def delete(self, record: Dict[str, Any]) -> None:
        protocol = record["protocol"]
        protocol_num = 17 if protocol == "UDP" else 6
        internal_port = int(record["internal_port"])
        external_port = int(record.get("assigned_external_port") or record.get("external_port") or 0)
        nonce_hex = record.get("nonce")
        nonce = bytes.fromhex(nonce_hex) if isinstance(nonce_hex, str) else os.urandom(12)
        client_ip = _ipv4_to_mapped_bytes(self.source_ip)
        common = struct.pack("!BBH I 16s", 2, 1, 0, 0, client_ip)
        body = nonce + struct.pack("!B3sHH16s", protocol_num, b"\x00\x00\x00", internal_port, external_port, b"\x00" * 16)
        response = self._send(common + body)
        if len(response) < 60:
            raise BackendError("short PCP deletion response")
        result = response[3]
        if result != 0:
            raise BackendError(f"PCP deletion failed with result {result}")

    @staticmethod
    def _same_port_key(left: Dict[str, Any], right: Dict[str, Any]) -> bool:
        return left.get("protocol") == right.get("protocol") and left.get("external_port") == right.get("external_port")


class GatewayEndpoint(NamedTuple):
    location: str
    service_type: str
    control_url: str


class UpnpClient:
    def __init__(self, discover_timeout: float, http_timeout: float, upstream_interface: str):
        self.discover_timeout = discover_timeout
        self.http_timeout = http_timeout
        self.upstream_interface = upstream_interface

    def _resolve_source_ip(self) -> str:
        address = _get_interface_ipv4(self.upstream_interface)
        if not address:
            raise RuntimeError(f"unable to determine IPv4 address for {self.upstream_interface}")
        return address

    def discover(self) -> GatewayEndpoint:
        location = self._discover_location()
        if not location:
            raise RuntimeError("no UPnP Internet Gateway Device discovered")
        return self._endpoint_from_location(location)

    def _discover_location(self) -> Optional[str]:
        for st in (
            "urn:schemas-upnp-org:device:InternetGatewayDevice:1",
            "upnp:rootdevice",
        ):
            location = self._ssdp_discover(st)
            if location:
                return location
        return None

    def _ssdp_discover(self, search_target: str) -> Optional[str]:
        request = "\r\n".join(
            [
                "M-SEARCH * HTTP/1.1",
                'HOST: 239.255.255.250:1900',
                'MAN: "ssdp:discover"',
                "MX: 2",
                f"ST: {search_target}",
                "",
                "",
            ]
        )
        source_ip = self._resolve_source_ip()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.settimeout(self.discover_timeout)
        try:
            sock.bind((source_ip, 0))
            sock.sendto(request.encode("ascii"), ("239.255.255.250", 1900))
            deadline = _now() + self.discover_timeout
            while _now() < deadline:
                try:
                    data, _ = sock.recvfrom(65507)
                except socket.timeout:
                    break
                headers = self._parse_headers(data.decode("utf-8", "ignore"))
                location = headers.get("location")
                if location:
                    return location.strip()
        finally:
            sock.close()
        return None

    @staticmethod
    def _parse_headers(raw: str) -> Dict[str, str]:
        headers: Dict[str, str] = {}
        for line in raw.splitlines():
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            headers[key.strip().lower()] = value.strip()
        return headers

    def _endpoint_from_location(self, location: str) -> GatewayEndpoint:
        xml_bytes = self._http_get_bytes(location)
        root = ET.fromstring(xml_bytes)
        namespaces = {
            "dev": "urn:schemas-upnp-org:device-1-0",
        }
        for service in root.findall(".//dev:service", namespaces):
            service_type = _parse_xml_text(service, "dev:serviceType", namespaces)
            control_url = _parse_xml_text(service, "dev:controlURL", namespaces)
            if not service_type or not control_url:
                continue
            if service_type in UPNP_SERVICE_TYPES:
                return GatewayEndpoint(
                    location=location,
                    service_type=service_type,
                    control_url=urljoin(location, control_url),
                )
        raise RuntimeError("no WANIPConnection/WANPPPConnection service found on IGD")

    def external_ip(self, gateway: GatewayEndpoint) -> Optional[str]:
        try:
            response = self.soap_call(gateway, "GetExternalIPAddress", {})
        except Exception:
            return None
        namespaces = {
            "s": "http://schemas.xmlsoap.org/soap/envelope/",
            "u": gateway.service_type,
        }
        root = ET.fromstring(response)
        body = root.find("s:Body", namespaces)
        if body is None:
            return None
        payload = body.find(".//u:GetExternalIPAddressResponse", namespaces)
        if payload is None:
            return None
        external_ip = _parse_xml_text(payload, "NewExternalIPAddress", namespaces)
        return external_ip or None

    def soap_call(self, gateway: GatewayEndpoint, action: str, args: Dict[str, Any]) -> bytes:
        payload_lines = []
        for key, value in args.items():
            payload_lines.append(f"<{key}>{xml_escape(str(value))}</{key}>")
        payload = "\n".join(payload_lines)
        body = (
            '<?xml version="1.0"?>\n'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">\n'
            "<s:Body>\n"
            f'<u:{action} xmlns:u="{gateway.service_type}">\n'
            f"{payload}\n"
            f"</u:{action}>\n"
            "</s:Body>\n"
            "</s:Envelope>\n"
        ).encode("utf-8")
        try:
            return self._http_request(
                gateway.control_url,
                "POST",
                body,
                {
                    "Content-Type": 'text/xml; charset="utf-8"',
                    "SOAPAction": f'"{gateway.service_type}#{action}"',
                },
            )
        except HTTPError as exc:
            error_body = exc.read().decode("utf-8", "ignore") if exc.fp else ""
            message = f"UPnP SOAP {action} failed with HTTP {exc.code}"
            if error_body:
                message = f"{message}: {error_body[:300]}"
            raise RuntimeError(message) from exc
        except URLError as exc:
            raise RuntimeError(f"UPnP SOAP {action} failed: {exc.reason}") from exc

    def _http_request(self, url: str, method: str, body: bytes, headers: Dict[str, str]) -> bytes:
        parsed = urlsplit(url)
        source_ip = self._resolve_source_ip()
        connection_cls = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
        connection = connection_cls(
            parsed.hostname,
            parsed.port or (443 if parsed.scheme == "https" else 80),
            timeout=self.http_timeout,
            source_address=(source_ip, 0),
        )
        try:
            path = parsed.path or "/"
            if parsed.query:
                path = f"{path}?{parsed.query}"
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            payload = response.read()
            if response.status >= 400:
                raise HTTPError(url, response.status, response.reason, response.headers, None)
            return payload
        except OSError as exc:
            raise URLError(exc) from exc
        finally:
            connection.close()

    def _http_get_bytes(self, url: str) -> bytes:
        return self._http_request(url, "GET", b"", {})

    def add_port_mapping(self, gateway: GatewayEndpoint, mapping: Dict[str, Any]) -> None:
        args = {
            "NewRemoteHost": "",
            "NewExternalPort": mapping["external_port"],
            "NewProtocol": mapping["protocol"],
            "NewInternalPort": mapping["internal_port"],
            "NewInternalClient": mapping["internal_client"],
            "NewEnabled": 1 if mapping.get("enabled", True) else 0,
            "NewPortMappingDescription": mapping["description"],
            "NewLeaseDuration": mapping["lease_seconds"],
        }
        self.soap_call(gateway, "AddPortMapping", args)

    def delete_port_mapping(self, gateway: GatewayEndpoint, mapping: Dict[str, Any]) -> None:
        args = {
            "NewRemoteHost": "",
            "NewExternalPort": mapping["external_port"],
            "NewProtocol": mapping["protocol"],
        }
        self.soap_call(gateway, "DeletePortMapping", args)


class PortMappingManager:
    def __init__(
        self,
        *,
        socket_path: str = DEFAULT_SOCKET_PATH,
        state_path: str = DEFAULT_STATE_PATH,
        upstream_interface: str = DEFAULT_UPSTREAM_INTERFACE,
        discover_timeout: float = DEFAULT_DISCOVER_TIMEOUT,
        http_timeout: float = DEFAULT_HTTP_TIMEOUT,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        refresh_margin: int = DEFAULT_REFRESH_MARGIN,
        health_interval: int = DEFAULT_HEALTH_INTERVAL,
        retry_initial: int = DEFAULT_RETRY_INITIAL,
        retry_max: int = DEFAULT_RETRY_MAX,
    ):
        self.socket_path = socket_path
        self.state_path = Path(state_path)
        self.upstream_interface = upstream_interface
        self.discover_timeout = discover_timeout
        self.http_timeout = http_timeout
        self.default_lease_seconds = lease_seconds
        self.refresh_margin = refresh_margin
        self.health_interval = health_interval
        self.retry_initial = retry_initial
        self.retry_max = retry_max

        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._wakeup_event = threading.Event()
        self._desired: Dict[str, Dict[str, Any]] = {}
        self._applied: Dict[str, Dict[str, Any]] = {}
        self._pending_sync = False
        self._next_retry_at = 0.0
        self._next_health_check_at = 0.0
        self._retry_delay = float(self.retry_initial)
        self._last_error: Optional[str] = None
        self._active_backend_name: Optional[str] = None
        self._external_ip: Optional[str] = None
        self._last_reconcile_at: Optional[float] = None
        self._firewall = FirewallManager(self.upstream_interface)
        self._load_state()

    def _load_state(self) -> None:
        if not self.state_path.exists():
            return
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception as exc:
            LOG.warning("failed to load state file %s: %s", self.state_path, exc)
            return
        if not isinstance(data, dict):
            return
        mappings = data.get("mappings", [])
        if not isinstance(mappings, list):
            return
        for raw in mappings:
            try:
                mapping = self._normalize_mapping(raw, allow_internal_client=True)
            except Exception as exc:
                LOG.warning("ignoring invalid mapping from state: %s", exc)
                continue
            self._desired[mapping["id"]] = mapping
        self._pending_sync = bool(self._desired)
        self._next_retry_at = 0.0
        self._next_health_check_at = 0.0

    def _save_state_locked(self) -> None:
        payload = {
            "version": 1,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "mappings": list(self._desired.values()),
        }
        _atomic_write(self.state_path, json.dumps(payload, indent=2, sort_keys=True) + "\n")

    def stop(self) -> None:
        self._stop_event.set()
        self._wakeup_event.set()

    def should_stop(self) -> bool:
        return self._stop_event.is_set()

    def _normalize_mapping(self, raw: Any, *, allow_internal_client: bool = False) -> Dict[str, Any]:
        if not isinstance(raw, dict):
            raise ValueError("mapping entries must be objects")

        mapping_id = raw.get("id") or raw.get("name")
        if not isinstance(mapping_id, str) or not mapping_id.strip():
            protocol = _normalize_protocol(raw.get("protocol", "TCP"))
            external_port = _parse_int(raw.get("external_port"), field="external_port")
            mapping_id = f"{protocol.lower()}-{external_port}"
        mapping_id = mapping_id.strip()

        protocol = _normalize_protocol(raw.get("protocol", "TCP"))
        external_port = _parse_int(raw.get("external_port"), field="external_port")
        internal_port = _parse_int(raw.get("internal_port", external_port), field="internal_port")
        enabled = _parse_bool(raw.get("enabled"), default=True)
        lease_seconds = _parse_int(
            raw.get("lease_seconds", self.default_lease_seconds),
            field="lease_seconds",
            minimum=0,
            maximum=86400 * 30,
        )
        description = _normalize_description(raw.get("description"), mapping_id)

        internal_client = raw.get("internal_client")
        if internal_client is None:
            if allow_internal_client:
                internal_client = ""
        else:
            internal_client = _normalize_ipv4(internal_client, field="internal_client")

        mapping = {
            "id": mapping_id,
            "protocol": protocol,
            "external_port": external_port,
            "internal_port": internal_port,
            "internal_client": internal_client or "",
            "lease_seconds": lease_seconds,
            "description": description,
            "enabled": enabled,
        }
        return mapping

    def _resolve_internal_client(self, mapping: Dict[str, Any]) -> str:
        if mapping.get("internal_client"):
            return mapping["internal_client"]
        address = _get_interface_ipv4(self.upstream_interface)
        if not address:
            raise RuntimeError(f"unable to determine IPv4 address for {self.upstream_interface}")
        return address

    def _snapshot_status(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "desired": deepcopy(list(self._desired.values())),
                "applied": deepcopy(list(self._applied.values())),
                "pending_sync": self._pending_sync,
                "next_retry_at": self._next_retry_at,
                "next_health_check_at": self._next_health_check_at,
                "retry_delay": self._retry_delay,
                "last_error": self._last_error,
                "last_reconcile_at": self._last_reconcile_at,
                "backend": self._active_backend_name,
                "external_ip": self._external_ip,
            }

    def _make_backend(self, name: str) -> RouterBackend:
        if name == "upnp-igd":
            return UpnpIgdBackend(self.upstream_interface, self.discover_timeout, self.http_timeout)
        if name == "pcp":
            return PcpBackend(self.upstream_interface, self.http_timeout)
        if name == "nat-pmp":
            return NatPmpBackend(self.upstream_interface, self.http_timeout)
        raise ValueError(f"unknown backend: {name}")

    def _backend_candidates(self) -> list[str]:
        ordered: list[str] = []
        if self._active_backend_name:
            ordered.append(self._active_backend_name)
        for name in BACKEND_ORDER:
            if name not in ordered:
                ordered.append(name)
        return ordered

    def _firewall_desired(self, mappings: Dict[str, Dict[str, Any]]) -> list[Dict[str, Any]]:
        desired = []
        for mapping in mappings.values():
            if mapping.get("enabled", True):
                desired.append(mapping)
        return desired

    def _update_desired(self, mappings: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
        normalized: Dict[str, Dict[str, Any]] = {}
        for raw in mappings:
            mapping = self._normalize_mapping(raw)
            normalized[mapping["id"]] = mapping
        with self._lock:
            self._desired = normalized
            self._pending_sync = True
            self._next_retry_at = 0.0
            self._next_health_check_at = 0.0
            self._save_state_locked()
        self._wakeup_event.set()
        return {
            "ok": True,
            "action": "sync",
            "count": len(normalized),
        }

    def _delete_desired(self, mapping_id: str) -> Dict[str, Any]:
        with self._lock:
            removed = self._desired.pop(mapping_id, None)
            if removed is not None:
                self._pending_sync = True
                self._next_retry_at = 0.0
                self._next_health_check_at = 0.0
                self._save_state_locked()
        if removed is not None:
            LOG.info("request delete id=%s", mapping_id)
            self._wakeup_event.set()
        return {"ok": True, "deleted": removed is not None, "id": mapping_id}

    def _reconcile(self) -> bool:
        with self._lock:
            desired_snapshot = deepcopy(self._desired)
            applied_snapshot = deepcopy(self._applied)
            active_backend_name = self._active_backend_name

        if not desired_snapshot and not applied_snapshot:
            try:
                self._firewall.clear()
            except Exception as exc:
                with self._lock:
                    self._pending_sync = True
                    self._last_error = str(exc)
                    self._next_retry_at = _now() + self._retry_delay
                    self._retry_delay = min(self._retry_delay * 2, float(self.retry_max))
                    self._next_health_check_at = _now() + self.health_interval
                LOG.warning("failed to clear firewall chain: %s", exc)
                LOG.info("retry scheduled in %.1fs", self._retry_delay)
                return False
            with self._lock:
                self._pending_sync = False
                self._next_retry_at = 0.0
                self._next_health_check_at = _now() + self.health_interval
                self._last_error = None
                self._last_reconcile_at = _now()
                self._external_ip = None
            LOG.info("all mappings cleared")
            return True

        desired_active = {mapping_id: mapping for mapping_id, mapping in desired_snapshot.items() if mapping.get("enabled", True)}

        if not desired_active:
            try:
                for record in list(applied_snapshot.values()):
                    backend = self._make_backend(record.get("backend") or active_backend_name or "upnp-igd")
                    backend.delete(record)
                self._firewall.clear()
                with self._lock:
                    self._applied = {}
                    self._pending_sync = False
                    self._next_retry_at = 0.0
                    self._next_health_check_at = _now() + self.health_interval
                    self._last_error = None
                    self._last_reconcile_at = _now()
                    self._external_ip = None
                LOG.info("all mappings cleared")
                return True
            except Exception as exc:
                with self._lock:
                    self._pending_sync = True
                    self._last_error = str(exc)
                    self._next_retry_at = _now() + self._retry_delay
                    self._retry_delay = min(self._retry_delay * 2, float(self.retry_max))
                    self._next_health_check_at = _now() + self.health_interval
                LOG.warning("failed to clear mappings: %s", exc)
                LOG.info("retry scheduled in %.1fs", self._retry_delay)
                return False

        try:
            now = _now()
            next_refresh_candidates: list[float] = []
            had_failure = False

            for backend_name in self._backend_candidates():
                backend = self._make_backend(backend_name)
                trial_records = deepcopy(applied_snapshot)
                created_records: Dict[str, Dict[str, Any]] = {}
                LOG.info("reconciling %d mappings via %s", len(desired_active), backend_name)
                try:
                    external_ip = backend.external_ip()
                    for mapping_id, desired in desired_active.items():
                        effective_desired = deepcopy(desired)
                        effective_desired["internal_client"] = self._resolve_internal_client(effective_desired)
                        current = trial_records.get(mapping_id)
                        previous = current if current and current.get("backend") == backend_name else None
                        record = backend.apply(effective_desired, previous)
                        record.update(
                            {
                                "id": mapping_id,
                                "expires_at": now + effective_desired["lease_seconds"] if effective_desired["lease_seconds"] > 0 else 0.0,
                                "health_check_at": now + self.health_interval,
                                "last_applied_at": now,
                                "backend": backend_name,
                            }
                        )
                        if external_ip:
                            record["external_ip"] = external_ip
                        trial_records[mapping_id] = record
                        created_records[mapping_id] = record
                        LOG.info(
                            "applied mapping %s via %s => %s:%s/%s",
                            mapping_id,
                            backend_name,
                            record["internal_client"],
                            record["internal_port"],
                            record["protocol"],
                        )
                        if record.get("lease_seconds", 0) > 0:
                            next_refresh_candidates.append(max(now, float(record["expires_at"]) - self.refresh_margin))
                        else:
                            next_refresh_candidates.append(now + self.health_interval)

                    self._firewall.sync(trial_records[mid] for mid in desired_active.keys())

                    for mapping_id, old_record in list(applied_snapshot.items()):
                        new_record = trial_records.get(mapping_id)
                        if new_record is None or old_record != new_record:
                            old_backend = self._make_backend(old_record.get("backend") or backend_name)
                            try:
                                old_backend.delete(old_record)
                                LOG.info("removed stale mapping %s from %s", mapping_id, old_record.get("backend"))
                            except Exception as exc:
                                had_failure = True
                                LOG.warning("failed to remove stale mapping %s: %s", mapping_id, exc)

                    with self._lock:
                        self._applied = {mapping_id: trial_records[mapping_id] for mapping_id in desired_active.keys()}
                        self._active_backend_name = backend_name
                        self._external_ip = external_ip or self._external_ip
                        self._last_reconcile_at = now
                        self._last_error = "stale mapping cleanup failed" if had_failure else None
                        self._pending_sync = had_failure
                        self._next_retry_at = now + self._retry_delay if had_failure else 0.0
                        self._retry_delay = min(self._retry_delay * 2, float(self.retry_max)) if had_failure else float(self.retry_initial)
                        self._next_health_check_at = min(next_refresh_candidates) if next_refresh_candidates else now + self.health_interval
                    if had_failure:
                        LOG.warning("reconcile succeeded but stale cleanup failed; retry scheduled")
                        LOG.info("retry scheduled in %.1fs", self._retry_delay)
                        return False
                    return True
                except Exception as exc:
                    LOG.warning("backend %s failed: %s", backend_name, exc)
                    had_failure = True
                    for record in reversed(list(created_records.values())):
                        try:
                            backend.delete(record)
                        except Exception:
                            LOG.debug("rollback delete failed for %s", record.get("id"), exc_info=True)
                    continue

            with self._lock:
                self._pending_sync = True
                self._last_error = "no supported port mapping backend available"
                self._next_retry_at = now + self._retry_delay
                self._retry_delay = min(self._retry_delay * 2, float(self.retry_max))
                self._next_health_check_at = now + self.health_interval
            if had_failure:
                LOG.warning("reconcile failed for all backends; retry scheduled")
                LOG.info("retry scheduled in %.1fs", self._retry_delay)
            return False
        except Exception as exc:
            with self._lock:
                self._pending_sync = True
                self._last_error = str(exc)
                self._next_retry_at = _now() + self._retry_delay
                self._retry_delay = min(self._retry_delay * 2, float(self.retry_max))
                self._next_health_check_at = _now() + self.health_interval
            LOG.warning("reconcile failed: %s", exc)
            LOG.info("retry scheduled in %.1fs", self._retry_delay)
            return False

    def scheduler_loop(self) -> None:
        while not self._stop_event.is_set():
            with self._lock:
                now = _now()
                next_health = self._next_health_check_at or (now + self.health_interval)
                next_retry = self._next_retry_at if self._pending_sync else 0.0
                should_reconcile = False
                if self._pending_sync and (next_retry == 0.0 or now >= next_retry):
                    should_reconcile = True
                elif self._applied and now >= next_health:
                    self._pending_sync = True
                    should_reconcile = True
                sleep_for = self.health_interval
                due_times = [next_health]
                if next_retry:
                    due_times.append(next_retry)
                due_times = [candidate for candidate in due_times if candidate > 0]
                if due_times:
                    sleep_for = max(1.0, min(due_times) - now)
                if should_reconcile:
                    sleep_for = 0.0
            if should_reconcile:
                self._reconcile()
                continue
            if self._wakeup_event.wait(timeout=min(sleep_for, self.health_interval)):
                self._wakeup_event.clear()

    def handle_request(self, request: Dict[str, Any]) -> Dict[str, Any]:
        action = request.get("action")
        LOG.info("request action=%s", action)
        if action == "sync":
            mappings = request.get("mappings")
            if not isinstance(mappings, list):
                return {"ok": False, "error": "mappings must be an array"}
            LOG.info("request sync count=%d", len(mappings))
            return self._update_desired(mappings)
        if action == "upsert":
            mapping = request.get("mapping")
            if not isinstance(mapping, dict):
                return {"ok": False, "error": "mapping must be an object"}
            with self._lock:
                normalized = self._normalize_mapping(mapping)
                self._desired[normalized["id"]] = normalized
                self._pending_sync = True
                self._next_retry_at = 0.0
                self._next_health_check_at = 0.0
                self._save_state_locked()
            self._wakeup_event.set()
            LOG.info("request upsert id=%s", normalized["id"])
            return {"ok": True, "action": "upsert", "id": normalized["id"]}
        if action == "delete":
            mapping_id = request.get("id") or request.get("name")
            if not isinstance(mapping_id, str) or not mapping_id.strip():
                return {"ok": False, "error": "id is required"}
            return self._delete_desired(mapping_id.strip())
        if action == "clear":
            with self._lock:
                self._desired = {}
                self._pending_sync = True
                self._next_retry_at = 0.0
                self._next_health_check_at = 0.0
                self._save_state_locked()
            self._wakeup_event.set()
            LOG.info("request clear")
            return {"ok": True, "action": "clear"}
        if action == "status":
            LOG.info("request status")
            return {"ok": True, **self._snapshot_status()}
        if action == "refresh":
            with self._lock:
                self._pending_sync = True
                self._next_retry_at = 0.0
            self._wakeup_event.set()
            LOG.info("request refresh")
            return {"ok": True, "action": "refresh"}
        if action == "ping":
            LOG.info("request ping")
            return {"ok": True, "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        return {"ok": False, "error": f"unknown action: {action}"}

    def cleanup(self) -> None:
        with self._lock:
            applied_snapshot = deepcopy(self._applied)
        if not applied_snapshot:
            try:
                self._firewall.clear()
            except Exception as exc:
                LOG.warning("failed to clear firewall on shutdown: %s", exc)
            return
        LOG.info("shutdown cleanup: removing %d applied mappings", len(applied_snapshot))
        try:
            self._firewall.clear()
        except Exception as exc:
            LOG.warning("failed to clear firewall on shutdown: %s", exc)
        for mapping in applied_snapshot.values():
            try:
                backend = self._make_backend(mapping.get("backend") or self._active_backend_name or "upnp-igd")
                backend.delete(mapping)
                LOG.info("cancelled mapping %s", mapping.get("id"))
            except Exception:
                LOG.debug("cleanup delete failed for %s", mapping.get("id"), exc_info=True)


class CasanodeRequestHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        try:
            raw = self.rfile.readline(65536)
            if not raw:
                return
            request = json.loads(raw.decode("utf-8"))
            if not isinstance(request, dict):
                raise ValueError("request must be a JSON object")
            response = self.server.manager.handle_request(request)  # type: ignore[attr-defined]
        except Exception as exc:
            response = {"ok": False, "error": str(exc)}
        payload = json.dumps(response, sort_keys=True) + "\n"
        self.wfile.write(payload.encode("utf-8"))
        self.wfile.flush()


class CasanodeUnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True

    def server_bind(self) -> None:
        if os.path.exists(self.server_address):
            os.unlink(self.server_address)
        super().server_bind()
        os.chmod(self.server_address, 0o666)


def main() -> int:
    configure_logging()
    manager = PortMappingManager()
    socket_path = manager.socket_path
    Path(socket_path).parent.mkdir(parents=True, exist_ok=True)

    server = CasanodeUnixServer(socket_path, CasanodeRequestHandler)
    server.manager = manager  # type: ignore[attr-defined]

    stop_event = threading.Event()

    def _signal_handler(_signum: int, _frame: Any) -> None:
        stop_event.set()

    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    LOG.info("starting daemon socket=%s upstream_interface=%s", socket_path, manager.upstream_interface)
    scheduler = threading.Thread(target=manager.scheduler_loop, name="casanode-natd-scheduler", daemon=True)
    scheduler.start()

    server_thread = threading.Thread(target=server.serve_forever, name="casanode-natd-server", daemon=True)
    server_thread.start()

    try:
        while not stop_event.wait(1.0):
            pass
    finally:
        LOG.info("shutting down daemon")
        manager.stop()
        server.shutdown()
        server.server_close()
        manager.cleanup()
        scheduler.join(timeout=2.0)
        server_thread.join(timeout=2.0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
