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


RETRY_SECONDS = 15
KEEPALIVE_SECONDS = 30
LOCAL_TUNNEL_PORT = 18443
START_DURATION_SECONDS = 8 * 60 * 60
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

    def ping(self) -> None:
        self._send_packet(0xC0)

    def _recv_exact(self, length: int) -> bytes:
        if self.sock is None:
            raise ConnectionError("MQTT socket is not connected")
        data = bytearray()
        while len(data) < length:
            chunk = self.sock.recv(length - len(data))
            if not chunk:
                raise ConnectionError("MQTT connection closed")
            data.extend(chunk)
        return bytes(data)

    def receive(self) -> tuple[int, int, bytes]:
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
        if qos:
            offset += 2
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
        while b"\r\n\r\n" not in response and len(response) < 16384:
            chunk = self.sock.recv(4096)
            if not chunk:
                break
            response.extend(chunk)
        status = response.split(b"\r\n", 1)[0]
        if b" 101 " not in status:
            self.sock.close()
            raise ConnectionError(f"WebSocket handshake failed ({status.decode(errors='replace')})")

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
            chunk = self.sock.recv(length - len(data))
            if not chunk:
                raise ConnectionError("WebSocket connection closed")
            data.extend(chunk)
        return bytes(data)

    def receive_text(self) -> str | None:
        first, second = self._recv_exact(2)
        opcode = first & 0x0F
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._recv_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._recv_exact(8))[0]
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

    @property
    def key(self) -> str:
        return hashlib.sha256(self.device_id.encode()).hexdigest()[:8]

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
        entity: dict[str, Any]
        op: str
        payload: dict[str, Any] | None
        if self.generation == "gen2":
            entity = {"device": self.device_id, "service": "lwm2mserver"}
            op = "execute"
            if action == "start_mowing":
                entity["path"] = "smart_system_mower_api/0/manual_start"
                payload = {"as": [f"0='{START_DURATION_SECONDS}'"]}
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
            if action == "start_mowing" and self.generation == "gen1_lona":
                entity["path"] = "lemonbeat/0/mower_timer_with_distance"
                raw = struct.pack("!HI", 0, START_DURATION_SECONDS)
                payload = {"vo": base64.b64encode(raw).decode()}
            elif action == "start_mowing":
                entity["path"] = "lemonbeat/0/mower_timer"
                payload = {"vi": START_DURATION_SECONDS}
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
        for message in json.loads(raw):
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
            "identifiers": [f"gardena_control_{mower.key}"],
            "name": mower.name,
            "manufacturer": "GARDENA",
            "model": mower.name,
        },
    }
    if mower.supports_pause:
        payload["pause_command_topic"] = f"{base}/command"
    return payload


class Controller:
    def __init__(self) -> None:
        self.gateway = os.environ["GARDENA_GATEWAY_HOST"].strip()
        device_id = os.environ["GARDENA_DEVICE_ID"].strip()
        if len(device_id) < 8:
            raise ValueError("Gateway device ID is missing or too short")
        self.gateway_password = device_id[:8]
        self.private_key = os.environ.get("GARDENA_PRIV_KEY", "/data/ssh/addon_ed25519")
        self.broker_host = os.environ["GARDENA_MQTT_BROKER_HOST"].strip()
        self.broker_port = int(os.environ.get("GARDENA_MQTT_BROKER_PORT", "1883"))
        self.broker_user = os.environ.get("GARDENA_MQTT_BROKER_USER", "")
        self.broker_password = os.environ.get("GARDENA_MQTT_BROKER_PASSWORD", "")
        self.topic_prefix = os.environ.get("GARDENA_MQTT_TOPIC_PREFIX", "gardena")
        self.ha_prefix = os.environ.get("GARDENA_MQTT_HA_PREFIX", "homeassistant")
        self.availability_topic = f"{self.topic_prefix}/control/availability"
        self.tunnel: subprocess.Popen | None = None

    def enable_websocket_api(self) -> None:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        login = urllib.request.Request(
            f"https://{self.gateway}/login",
            data=json.dumps({"password": self.gateway_password}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(login, timeout=15, context=context) as response:
            session = json.loads(response.read()).get("session")
        if not session:
            raise ConnectionError("Gateway login returned no session")
        enable = urllib.request.Request(
            f"https://{self.gateway}/websocket_api",
            data=b'{"enable":true}',
            headers={"Content-Type": "application/json", "X-Session": session},
            method="PUT",
        )
        with urllib.request.urlopen(enable, timeout=15, context=context) as response:
            if response.status not in {200, 204}:
                raise ConnectionError(f"WebSocket enable failed ({response.status})")

    def start_tunnel(self) -> None:
        self.tunnel = subprocess.Popen(
            [
                "ssh", "-N", "-T",
                "-o", "StrictHostKeyChecking=no",
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

    def ensure_gateway_toggle_api(self) -> None:
        """Keep the gateway web UI API reachable after `/websocket_api`."""
        command = (
            "systemctl enable gardena-matter-toggle.socket >/dev/null 2>&1 && "
            "systemctl start gardena-matter-toggle.socket && "
            "(iptables -C INPUT -p udp --dport 5540 -j ACCEPT 2>/dev/null || "
            "iptables -I INPUT -p udp --dport 5540 -j ACCEPT) && "
            "(ip6tables -C INPUT -p udp --dport 5540 -j ACCEPT 2>/dev/null || "
            "ip6tables -I INPUT -p udp --dport 5540 -j ACCEPT) && "
            "(iptables -C INPUT -p tcp --dport 8099 -j ACCEPT 2>/dev/null || "
            "iptables -I INPUT -p tcp --dport 8099 -j ACCEPT) && "
            "(ip6tables -C INPUT -p tcp --dport 8099 -j ACCEPT 2>/dev/null || "
            "ip6tables -I INPUT -p tcp --dport 8099 -j ACCEPT) && "
            "systemctl is-active --quiet gardena-matter-toggle.socket"
        )
        result = subprocess.run(
            [
                "ssh",
                "-T",
                "-o", "StrictHostKeyChecking=no",
                "-o", "BatchMode=yes",
                "-o", "ConnectTimeout=5",
                "-i", self.private_key,
                f"root@{self.gateway}",
                command,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=20,
            check=False,
        )
        if result.returncode != 0:
            raise ConnectionError("Gateway control API repair failed")

    def publish_discovery(self, mqtt: MqttClient, mower: Mower) -> None:
        topic = f"{self.ha_prefix}/lawn_mower/gardena_{mower.key}/config"
        payload = discovery_payload(mower, self.topic_prefix, self.availability_topic)
        mqtt.publish(topic, json.dumps(payload, separators=(",", ":")), retain=True)
        self.publish_activity(mqtt, mower)

    def publish_activity(self, mqtt: MqttClient, mower: Mower, value: str | None = None) -> None:
        activity = value or mower.activity()
        if activity is None:
            return
        topic = f"{self.topic_prefix}/{mower.key}/mower/activity/state"
        mqtt.publish(topic, activity, retain=True)

    def run_once(self) -> None:
        mqtt = MqttClient(
            self.broker_host,
            self.broker_port,
            self.broker_user,
            self.broker_password,
            "gardena-control-" + secrets.token_hex(4),
        )
        ws: WebSocketClient | None = None
        try:
            mqtt.connect(self.availability_topic)
            mqtt.subscribe(f"{self.topic_prefix}/+/mower/command")
            mqtt.publish(self.availability_topic, "online", retain=True)
            self.enable_websocket_api()
            self.start_tunnel()
            self.ensure_gateway_toggle_api()
            ws = WebSocketClient("127.0.0.1", LOCAL_TUNNEL_PORT, self.gateway_password)
            mowers = discover_mowers(ws)
            if not mowers:
                raise RuntimeError("No supported mower found on gateway")
            by_topic = {
                f"{self.topic_prefix}/{mower.key}/mower/command": mower for mower in mowers
            }
            for mower in mowers:
                self.publish_discovery(mqtt, mower)
            log(f"Bereit: {len(mowers)} Mäher per MQTT steuerbar")

            mqtt.sock.settimeout(15)
            ws.sock.settimeout(15)
            pending_commands: dict[str, tuple[Mower, str, float]] = {}
            while True:
                ws_buffered = ws.sock.pending() > 0
                readable, _, _ = select.select(
                    [mqtt.sock, ws.sock], [], [], 0 if ws_buffered else 5
                )
                if mqtt.sock in readable:
                    packet_type, flags, body = mqtt.receive()
                    if packet_type == 3:
                        topic, action, retained = mqtt.parse_publish(flags, body)
                        mower = by_topic.get(topic)
                        if mower is None or retained:
                            continue
                        command = mower.command(action)
                        ws.send_text(json.dumps(command, separators=(",", ":")))
                        pending_commands[command[0]["request_id"]] = (
                            mower,
                            action,
                            time.monotonic() + 30,
                        )
                        optimistic = {
                            "start_mowing": "mowing",
                            "dock": "returning",
                            "pause": "paused",
                        }.get(action)
                        if optimistic:
                            self.publish_activity(mqtt, mower, optimistic)
                            log(f"Befehl '{action}' an Mäher {mower.key} gesendet")
                if ws_buffered or ws.sock in readable:
                    raw = ws.receive_text()
                    if raw:
                        for message in json.loads(raw):
                            pending = pending_commands.pop(message.get("request_id"), None)
                            if pending is not None:
                                mower, action, _deadline = pending
                                if message.get("success", False):
                                    log(f"Befehl '{action}' von Mäher {mower.key} bestätigt")
                                else:
                                    self.publish_activity(mqtt, mower)
                                    log(f"Befehl '{action}' von Mäher {mower.key} abgelehnt")
                            for mower in mowers:
                                if apply_event(mower, message):
                                    self.publish_activity(mqtt, mower)
                now = time.monotonic()
                for request_id, (mower, action, deadline) in list(pending_commands.items()):
                    if now >= deadline:
                        pending_commands.pop(request_id)
                        self.publish_activity(mqtt, mower)
                        log(f"Keine Bestätigung für Befehl '{action}' an Mäher {mower.key}")
                if time.monotonic() - mqtt.last_io > KEEPALIVE_SECONDS / 2:
                    mqtt.ping()
        finally:
            try:
                mqtt.publish(self.availability_topic, "offline", retain=True)
            except OSError:
                pass
            mqtt.close()
            if ws is not None:
                ws.close()
            if self.tunnel is not None:
                self.tunnel.terminate()
                try:
                    self.tunnel.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.tunnel.kill()
                self.tunnel = None

    def run_forever(self) -> None:
        while True:
            try:
                self.run_once()
            except (OSError, ValueError, RuntimeError, TimeoutError, urllib.error.URLError) as exc:
                log(f"Noch nicht bereit ({type(exc).__name__}); neuer Versuch in {RETRY_SECONDS}s")
            time.sleep(RETRY_SECONDS)


if __name__ == "__main__":
    try:
        Controller().run_forever()
    except (KeyError, ValueError) as exc:
        log(f"Konfiguration unvollständig: {exc}")
        raise SystemExit(2)
