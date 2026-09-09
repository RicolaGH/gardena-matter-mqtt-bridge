# Adapted for GARDENA Local MQTT, 2026-09-09. See LICENSE and NOTICE.
#!/usr/bin/env python3
"""Native MQTT mower control for the GARDENA gateway.

Runs inside the Home Assistant add-on. It publishes MQTT lawn_mower discovery,
subscribes to command topics and forwards commands to the gateway's official
local WebSocket API through the already provisioned SSH connection.

Only Python's standard library is used so the add-on remains portable across
all supported Home Assistant architectures.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import re

from security import ssh_options, validate_host
import select
import socket
import ssl
import struct
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Any


MAX_PACKET_BYTES = 256 * 1024
PACKET_TIMEOUT = 15
MAX_PENDING_COMMANDS = 32
RETRY_SECONDS = 15
KEEPALIVE_SECONDS = 60
LOCAL_TUNNEL_PORT = 18443
START_DURATION_SECONDS = 8 * 60 * 60
START_ACTION_DURATIONS = {
    "start_mowing": START_DURATION_SECONDS,
    "start_1h": 1 * 60 * 60,
    "start_3h": 3 * 60 * 60,
    "start_6h": 6 * 60 * 60,
}
MOWER_STATUS_VALUE_TEMPLATE = (
    "{% if value|int in [1,2] %}RUNNING"
    "{% elif value|int == 3 %}CHARGING"
    "{% elif value|int >= 4 and value|int <= 8 %}DOCKED"
    "{% elif value|int >= 9 and value|int <= 13 %}ERROR"
    "{% else %}STOPPED{% endif %}"
)
MOWER_MODELS = {
    "488": ("GARDENA smart SILENO pro/max/free", "gen2"),
    "6146": ("GARDENA smart SILENO", "gen1"),
    "29694": ("GARDENA smart SILENO city/life", "gen1"),
    "53988": ("GARDENA smart SILENO city/life LONA", "gen1_lona"),
}


def log(message: str) -> None:
    print(f"[gardena-control] {message}", flush=True)


def _encode_remaining_length(length: int) -> bytes:
    encoded = bytearray()
    while True:
        digit = length % 128
        length //= 128
        if length:
            digit |= 0x80
        encoded.append(digit)
        if not length:
            return bytes(encoded)


def _mqtt_string(value: str | bytes) -> bytes:
    raw = value.encode() if isinstance(value, str) else value
    return struct.pack("!H", len(raw)) + raw


class MqttClient:
    """Small MQTT 3.1.1 QoS-0 client with retained discovery and LWT."""

    def __init__(self, host: str, port: int, user: str, password: str, client_id: str):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.client_id = client_id
        self.sock: socket.socket | None = None
        self.last_io = 0.0

    def connect(self, availability_topic: str) -> None:
        self.sock = socket.create_connection((self.host, self.port), timeout=15)
        self.sock.settimeout(15)
        flags = 0x02 | 0x04 | 0x20  # clean session, will, retained will
        payload = _mqtt_string(self.client_id)
        payload += _mqtt_string(availability_topic) + _mqtt_string("offline")
        if self.user:
            flags |= 0x80
            payload += _mqtt_string(self.user)
        if self.password:
            flags |= 0x40
            payload += _mqtt_string(self.password)
        variable = _mqtt_string("MQTT") + bytes([4, flags]) + struct.pack(
            "!H", KEEPALIVE_SECONDS
        )
        self._send_packet(0x10, variable + payload)
        packet_type, _flags, body = self.receive()
        if packet_type != 2 or len(body) != 2 or body[1] != 0:
            code = body[1] if len(body) > 1 else "unknown"
            raise ConnectionError(f"MQTT CONNACK failed ({code})")

    def _send_packet(self, header: int, payload: bytes = b"") -> None:
        if self.sock is None:
            raise ConnectionError("MQTT socket is not connected")
        self.sock.sendall(bytes([header]) + _encode_remaining_length(len(payload)) + payload)
        self.last_io = time.monotonic()

    def publish(self, topic: str, payload: str, *, retain: bool = False) -> None:
        self._send_packet(0x30 | (1 if retain else 0), _mqtt_string(topic) + payload.encode())

    def subscribe(self, topic: str) -> None:
        packet_id = secrets.randbelow(65535) + 1
        self._send_packet(0x82, struct.pack("!H", packet_id) + _mqtt_string(topic) + b"\x00")
        return packet_id

    def ping(self) -> None:
        self._send_packet(0xC0)

    def _recv_exact(self, length: int) -> bytes:
        if self.sock is None:
            raise ConnectionError("MQTT socket is not connected")
        data = bytearray()
        while len(data) < length:
            remaining = getattr(self, "_packet_deadline", time.monotonic() + PACKET_TIMEOUT) - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Packet deadline exceeded")
            old_timeout = self.sock.gettimeout()
            self.sock.settimeout(min(remaining, old_timeout or PACKET_TIMEOUT))
            try:
                chunk = self.sock.recv(length - len(data))
            finally:
                self.sock.settimeout(old_timeout)
            if not chunk:
                raise ConnectionError("MQTT connection closed")
            data.extend(chunk)
        return bytes(data)

    def receive(self) -> tuple[int, int, bytes]:
        self._packet_deadline = time.monotonic() + PACKET_TIMEOUT
        first = self._recv_exact(1)[0]
        multiplier = 1
        remaining = 0
        while True:
            digit = self._recv_exact(1)[0]
            remaining += (digit & 0x7F) * multiplier
            if not digit & 0x80:
                break
            multiplier *= 128
            if multiplier > 128**3:
                raise ValueError("Invalid MQTT remaining length")
        if remaining > MAX_PACKET_BYTES:
            raise ValueError("MQTT packet exceeds size limit")
        body = self._recv_exact(remaining)
        self.last_io = time.monotonic()
        return first >> 4, first & 0x0F, body

    @staticmethod
    def parse_publish(flags: int, body: bytes) -> tuple[str, str, bool]:
        if len(body) < 2:
            raise ValueError("Short MQTT publish packet")
        topic_len = struct.unpack("!H", body[:2])[0]
        offset = 2 + topic_len
        if offset > len(body):
            raise ValueError("Invalid MQTT topic length")
        topic = body[2:offset].decode("utf-8")
        qos = (flags >> 1) & 0x03
        if qos > 0:
            # Subscription requests QoS 0; higher QoS is not supported here.
            raise ValueError("Unexpected MQTT publish QoS")
        if not topic or any(c in topic for c in "#+\x00"):
            raise ValueError("Invalid MQTT publish topic")
        return topic, body[offset:].decode("utf-8"), bool(flags & 0x01)

    def close(self) -> None:
        if self.sock is not None:
            try:
                self._send_packet(0xE0)
            except OSError:
                pass
            self.sock.close()
            self.sock = None


class WebSocketClient:
    """Minimal RFC 6455 text client over TLS."""

    def __init__(self, host: str, port: int, password: str):
        raw = socket.create_connection((host, port), timeout=15)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        self.sock = context.wrap_socket(raw, server_hostname=host)
        self.sock.settimeout(15)
        key = base64.b64encode(secrets.token_bytes(16)).decode()
        auth = base64.b64encode(f"_:{password}".encode()).decode()
        request = (
            "GET / HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            f"Authorization: Basic {auth}\r\n\r\n"
        )
        self.sock.sendall(request.encode())
        response = bytearray()
        deadline = time.monotonic() + PACKET_TIMEOUT
        try:
            while b"\r\n\r\n" not in response:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or len(response) >= 16384:
                    raise ConnectionError("WebSocket handshake limit exceeded")
                self.sock.settimeout(remaining)
                chunk = self.sock.recv(1)
                if not chunk:
                    raise ConnectionError("WebSocket handshake closed")
                response.extend(chunk)
            lines = bytes(response).split(b"\r\n")
            headers = {}
            for line in lines[1:]:
                if b":" in line:
                    name, value = line.split(b":", 1)
                    headers[name.strip().lower()] = value.strip()
            expected = base64.b64encode(hashlib.sha1(
                (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()
            ).digest())
            if (lines[0].split()[1:2] != [b"101"] or
                headers.get(b"sec-websocket-accept") != expected or
                headers.get(b"upgrade", b"").lower() != b"websocket" or
                b"upgrade" not in [v.strip().lower() for v in headers.get(b"connection", b"").split(b",")]):
                raise ConnectionError("Invalid WebSocket handshake")
            self.sock.settimeout(PACKET_TIMEOUT)
        except BaseException:
            self.sock.close()
            raise

    def send_text(self, text: str) -> None:
        data = text.encode()
        mask = secrets.token_bytes(4)
        if len(data) < 126:
            header = bytes([0x81, 0x80 | len(data)])
        elif len(data) <= 65535:
            header = bytes([0x81, 0xFE]) + struct.pack("!H", len(data))
        else:
            header = bytes([0x81, 0xFF]) + struct.pack("!Q", len(data))
        masked = bytes(value ^ mask[index % 4] for index, value in enumerate(data))
        self.sock.sendall(header + mask + masked)

    def _recv_exact(self, length: int) -> bytes:
        data = bytearray()
        while len(data) < length:
            remaining = getattr(self, "_packet_deadline", time.monotonic() + PACKET_TIMEOUT) - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Packet deadline exceeded")
            old_timeout = self.sock.gettimeout()
            self.sock.settimeout(min(remaining, old_timeout or PACKET_TIMEOUT))
            try:
                chunk = self.sock.recv(length - len(data))
            finally:
                self.sock.settimeout(old_timeout)
            if not chunk:
                raise ConnectionError("WebSocket connection closed")
            data.extend(chunk)
        return bytes(data)

    def receive_text(self) -> str | None:
        self._packet_deadline = time.monotonic() + PACKET_TIMEOUT
        first, second = self._recv_exact(2)
        if first & 0x70 or not first & 0x80 or second & 0x80:
            raise ValueError("Unsupported WebSocket frame flags")
        opcode = first & 0x0F
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._recv_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._recv_exact(8))[0]
        if length > MAX_PACKET_BYTES or (opcode >= 8 and length > 125):
            raise ValueError("WebSocket frame exceeds size limit")
        mask = self._recv_exact(4) if second & 0x80 else b""
        payload = self._recv_exact(length)
        if mask:
            payload = bytes(v ^ mask[i % 4] for i, v in enumerate(payload))
        if opcode == 0x08:
            raise ConnectionError("WebSocket closed by gateway")
        if opcode == 0x09:
            self._send_control(0x0A, payload)
            return None
        if opcode != 0x01:
            return None
        return payload.decode("utf-8")

    def _send_control(self, opcode: int, payload: bytes) -> None:
        mask = secrets.token_bytes(4)
        masked = bytes(v ^ mask[i % 4] for i, v in enumerate(payload))
        self.sock.sendall(bytes([0x80 | opcode, 0x80 | len(payload)]) + mask + masked)

    def close(self) -> None:
        try:
            self._send_control(0x08, b"")
        except OSError:
            pass
        self.sock.close()


@dataclass
class Mower:
    device_id: str
    model_number: str
    name: str
    generation: str
    data: dict[str, Any]
    publisher_key: str | None = None
    publisher_identifier: str | None = None

    @property
    def legacy_key(self) -> str:
        return hashlib.sha256(self.device_id.encode()).hexdigest()[:8]

    @property
    def key(self) -> str:
        return self.publisher_key or self.legacy_key

    @property
    def device_identifier(self) -> str:
        return self.publisher_identifier or f"gardena_control_{self.legacy_key}"

    @property
    def supports_pause(self) -> bool:
        return self.generation == "gen2"

    def activity(self) -> str | None:
        if self.generation.startswith("gen1"):
            status = _nested_value(self.data, "lemonbeat", "0", "status", "vi")
            if status in {7, 8, 16, 17, 18}:
                return "docked"
            if status in {1, 4, 15}:
                return "mowing"
            if status == 2:
                return "returning"
            if status == 3:
                return "docked"
            if status in {0, 5, 6, 9, 10, 14}:
                return "paused"
            if status in {12, 13}:
                return "error"
            return None
        activity = _nested_value(self.data, "mower_app", "0", "activity", "vi")
        state = _nested_value(self.data, "mower_app", "0", "state", "vi")
        if activity in {1, 5}:
            return "docked"
        if activity in {2, 3}:
            return "mowing"
        if activity == 4:
            return "returning"
        if state == 5:
            return "paused"
        if state in {3, 8}:
            return "error"
        return None

    def command(self, action: str) -> list[dict[str, Any]]:
        request_id = str(uuid.uuid4())
        duration = START_ACTION_DURATIONS.get(action)
        entity: dict[str, Any]
        op: str
        payload: dict[str, Any] | None
        if self.generation == "gen2":
            entity = {"device": self.device_id, "service": "lwm2mserver"}
            op = "execute"
            if duration is not None:
                entity["path"] = "smart_system_mower_api/0/manual_start"
                payload = {"as": [f"0='{duration}'"]}
            elif action == "dock":
                entity["path"] = "smart_system_mower_api/0/park_until_further_notice"
                payload = None
            elif action == "pause":
                entity["path"] = "mower_app/0/pause"
                payload = None
            else:
                raise ValueError("Unsupported mower command")
        else:
            entity = {
                "device": self.device_id,
                "service": "lemonbeatd",
            }
            op = "write"
            if duration is not None and self.generation == "gen1_lona":
                entity["path"] = "lemonbeat/0/mower_timer_with_distance"
                raw = struct.pack("!HI", 0, duration)
                payload = {"vo": base64.b64encode(raw).decode()}
            elif duration is not None:
                entity["path"] = "lemonbeat/0/mower_timer"
                payload = {"vi": duration}
            elif action == "dock":
                entity["path"] = "lemonbeat/0/action_paused_until_1"
                raw = (2042).to_bytes(2, "little") + bytes([12, 31, 22, 0])
                payload = {"vo": base64.b64encode(raw).decode()}
            else:
                raise ValueError("Pause is not supported by this mower")
        request = {"entity": entity, "op": op, "request_id": request_id}
        if payload is not None:
            request["payload"] = payload
        return [request]


def _nested_value(data: dict[str, Any], *path: str) -> Any:
    value: Any = data
    for part in path:
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def gateway_messages(raw):
    try:
        messages = json.loads(raw)
    except (ValueError, RecursionError):
        return []
    if not isinstance(messages, list):
        return []
    return [m for m in messages if isinstance(m, dict) and
            isinstance(m.get("entity", {}), dict) and
            isinstance(m.get("payload", {}), dict) and
            isinstance(m.get("request_id", ""), str)]


def discover_mowers(ws: WebSocketClient) -> list[Mower]:
    request_ids = [str(uuid.uuid4()), str(uuid.uuid4())]
    request = [
        {
            "entity": {"service": service, "path": "devices"},
            "op": "read",
            "request_id": request_id,
        }
        for service, request_id in zip(("lemonbeatd", "lwm2mserver"), request_ids)
    ]
    ws.send_text(json.dumps(request, separators=(",", ":")))
    devices: dict[str, Any] = {}
    pending = set(request_ids)
    deadline = time.monotonic() + 30
    while pending and time.monotonic() < deadline:
        raw = ws.receive_text()
        if raw is None:
            continue
        for message in gateway_messages(raw):
            request_id = message.get("request_id")
            if request_id in pending:
                pending.remove(request_id)
                if message.get("success", True):
                    devices.update(message.get("payload", {}))
    if pending:
        raise TimeoutError("Gateway device discovery timed out")
    mowers = []
    for device_id, data in devices.items():
        model_number = str(_nested_value(data, "device", "0", "model_number", "vs") or "")
        if model_number in MOWER_MODELS:
            name, generation = MOWER_MODELS[model_number]
            mowers.append(Mower(device_id, model_number, name, generation, data))
    return mowers


def apply_event(mower: Mower, message: dict[str, Any]) -> bool:
    entity = message.get("entity", {})
    if entity.get("device") != mower.device_id or message.get("op") not in {"update", "overwrite"}:
        return False
    path = str(entity.get("path", "")).split("/")
    payload = message.get("payload") or {}
    target: dict[str, Any] = mower.data
    for part in path:
        target = target.setdefault(part, {})
    if isinstance(target, dict) and isinstance(payload, dict):
        target.update(payload)
        return True
    return False


def discovery_payload(mower: Mower, topic_prefix: str, availability_topic: str) -> dict[str, Any]:
    base = f"{topic_prefix}/{mower.key}/mower"
    payload: dict[str, Any] = {
        "name": None,
        "unique_id": f"gardena_{mower.key}_lawn_mower",
        "activity_state_topic": f"{base}/activity/state",
        "start_mowing_command_topic": f"{base}/command",
        "dock_command_topic": f"{base}/command",
        "availability_topic": availability_topic,
        "payload_available": "online",
        "payload_not_available": "offline",
        "device": {
            "identifiers": [mower.device_identifier],
            "name": mower.name,
            "manufacturer": "GARDENA",
            "model": mower.name,
        },
    }
    if mower.supports_pause:
        payload["pause_command_topic"] = f"{base}/command"
    return payload


def button_discovery_payload(
    mower: Mower,
    topic_prefix: str,
    availability_topic: str,
    hours: int,
) -> dict[str, Any]:
    return {
        "name": f"Start {hours} h",
        "object_id": f"gardena_{mower.key}_start_{hours}h",
        "unique_id": f"gardena_{mower.key}_start_{hours}h",
        "command_topic": f"{topic_prefix}/{mower.key}/mower/command",
        "payload_press": f"start_{hours}h",
        "availability_topic": availability_topic,
        "payload_available": "online",
        "payload_not_available": "offline",
        "icon": "mdi:timer-play-outline",
        "device": {
            "identifiers": [mower.device_identifier],
            "name": mower.name,
            "manufacturer": "GARDENA",
            "model": mower.name,
        },
    }


def publisher_mower_identity(topic: str, payload: str, topic_prefix="gardena", ha_prefix="homeassistant") -> tuple[str, str] | None:
    """Only recognize the publisher's precise topic/schema, never arbitrary JSON."""
    try:
        config = json.loads(payload)
    except (TypeError, ValueError, RecursionError):
        return None
    if not isinstance(config, dict):
        return None
    device = config.get("device")
    if not isinstance(device, dict) or device.get("manufacturer") != "GARDENA":
        return None
    identifiers = device.get("identifiers")
    if isinstance(identifiers, str):
        identifiers = [identifiers]
    if not isinstance(identifiers, list) or len(identifiers) != 1:
        return None
    identifier = identifiers[0]
    if not isinstance(identifier, str) or not re.fullmatch(r"gardena_[0-9a-f]{4}", identifier):
        return None
    key = identifier.removeprefix("gardena_")
    if topic != f"{ha_prefix}/sensor/{identifier}/mower_status/config":
        return None
    if config.get("state_topic") not in {f"{topic_prefix}/{key}/status/state", f"{topic_prefix}/{key}/mower_status/state"}:
        return None
    return key, identifier


def corrected_mower_status_discovery(topic: str, payload: str, *, allowed_keys=(), topic_prefix="gardena", ha_prefix="homeassistant") -> str | None:
    identity = publisher_mower_identity(topic, payload, topic_prefix, ha_prefix)
    if identity is None or identity[0] not in allowed_keys:
        return None
    config = json.loads(payload)
    if config.get("value_template") == MOWER_STATUS_VALUE_TEMPLATE:
        return None
    config["value_template"] = MOWER_STATUS_VALUE_TEMPLATE
    return json.dumps(config, separators=(",", ":"))


def repair_mower_status_discovery(mqtt: MqttClient, topic: str, payload: str, **scope) -> bool:
    corrected = corrected_mower_status_discovery(topic, payload, **scope)
    if corrected is None:
        return False
    mqtt.publish(topic, corrected, retain=True)
    log("MQTT-Mäherstatus korrigiert: Status 8 wird als DOCKED angezeigt")
    return True


def collect_publisher_mower_identities(
    mqtt: MqttClient,
    *,
    timeout_seconds: float = 2.0,
    topic_prefix="gardena", ha_prefix="homeassistant",
) -> list[tuple[str, str]]:
    """Collect retained mower sensor discovery records already held by MQTT."""
    if mqtt.sock is None:
        return []
    identities: list[tuple[str, str]] = []
    deadline = time.monotonic() + timeout_seconds
    mqtt.sock.settimeout(0.25)
    while time.monotonic() < deadline:
        try:
            packet_type, flags, body = mqtt.receive()
        except socket.timeout:
            continue
        if packet_type != 3:
            continue
        topic, payload, _retained = mqtt.parse_publish(flags, body)
        identity = publisher_mower_identity(topic, payload, topic_prefix, ha_prefix)
        if identity is not None and identity not in identities:
            if len(identities) >= 64:
                raise ValueError("Too many discovery identities")
            identities.append(identity)
    return identities


def adopt_publisher_identities(
    mowers: list[Mower], identities: list[tuple[str, str]]
) -> bool:
    """Merge a single discovered mower with its existing sensor device."""
    if len(mowers) != 1 or len(identities) != 1:
        return False
    mowers[0].publisher_key, mowers[0].publisher_identifier = identities[0]
    return True


class ControlPublisher:
    def start_tunnel(self) -> None:
        self.tunnel = subprocess.Popen(
            [
                "ssh", "-N", "-T",
                *ssh_options(),
                "-o", "BatchMode=yes",
                "-o", "ExitOnForwardFailure=yes",
                "-o", "ServerAliveInterval=30",
                "-o", "ServerAliveCountMax=3",
                "-i", self.private_key,
                "-L", f"127.0.0.1:{LOCAL_TUNNEL_PORT}:127.0.0.1:8443",
                f"root@{self.gateway}",
            ],
            stdin=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.tunnel.poll() is not None:
                raise ConnectionError("SSH tunnel exited before becoming ready")
            try:
                with socket.create_connection(("127.0.0.1", LOCAL_TUNNEL_PORT), timeout=1):
                    return
            except OSError:
                time.sleep(0.5)
        raise TimeoutError("SSH tunnel did not become ready")

    def publish_discovery(self, mqtt: MqttClient, mower: Mower) -> None:
        if mower.key != mower.legacy_key:
            legacy_topic = (
                f"{self.ha_prefix}/lawn_mower/gardena_{mower.legacy_key}/config"
            )
            mqtt.publish(legacy_topic, "", retain=True)
        topic = f"{self.ha_prefix}/lawn_mower/gardena_{mower.key}/config"
        payload = discovery_payload(mower, self.topic_prefix, self.availability_topic)
        mqtt.publish(topic, json.dumps(payload, separators=(",", ":")), retain=True)
        for hours in (1, 3, 6):
            button_topic = (
                f"{self.ha_prefix}/button/gardena_{mower.key}/start_{hours}h/config"
            )
            button_payload = button_discovery_payload(
                mower,
                self.topic_prefix,
                self.availability_topic,
                hours,
            )
            mqtt.publish(
                button_topic,
                json.dumps(button_payload, separators=(",", ":")),
                retain=True,
            )
        self.publish_activity(mqtt, mower)

    def publish_activity(self, mqtt: MqttClient, mower: Mower, value: str | None = None) -> None:
        activity = value or mower.activity()
        if activity is None:
            return
        topic = f"{self.topic_prefix}/{mower.key}/mower/activity/state"
        mqtt.publish(topic, activity, retain=True)
