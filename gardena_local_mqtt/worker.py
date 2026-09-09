"""Standalone sensor publisher and mower controller. Preview is read-only on MQTT."""
import hashlib
import json
import os
import select
import secrets
import signal
import subprocess
import time

import storage
import telemetry
from gateway import Gateway
from protocol import (ControlPublisher, MqttClient, WebSocketClient, discover_mowers,
    gateway_messages, apply_event, LOCAL_TUNNEL_PORT, MAX_PENDING_COMMANDS,
    START_ACTION_DURATIONS)
from security import validate_host


def options():
    config = storage.read('options.json', {})
    for name in ('gateway_host', 'mqtt_broker_host'):
        config[name] = validate_host(config.get(name, '').strip())
    for name in ('device_id', 'mqtt_broker_user', 'mqtt_broker_password'):
        value = config.get(name, '')
        if not isinstance(value, str) or len(value) > 4096 or any(ord(c) < 32 for c in value):
            raise ValueError('Invalid credentials')
    if len(config['device_id'].strip()) < 8:
        raise ValueError('Missing gateway device ID')
    for name, default in (('mqtt_topic_prefix', 'gardena'), ('mqtt_ha_prefix', 'homeassistant')):
        value = config.get(name, default)
        if not isinstance(value, str) or not value or len(value) > 200 or any(c in value for c in '+#\x00') or value.startswith('/') or value.endswith('/'):
            raise ValueError('Invalid topic prefix')
        config[name] = value
    config['mqtt_broker_port'] = int(config.get('mqtt_broker_port', 1883))
    if not 1 <= config['mqtt_broker_port'] <= 65535:
        raise ValueError('Invalid MQTT port')
    return config


def binding(config):
    return hashlib.sha256(json.dumps({k: config[k] for k in (
        'gateway_host', 'device_id', 'mqtt_broker_host', 'mqtt_broker_port',
        'mqtt_topic_prefix', 'mqtt_ha_prefix')}, sort_keys=True).encode()).hexdigest()


def report(mode, **values):
    storage.write('status.json', {'mode': mode, 'updated': time.time(), **values})


def collect_existing(mqtt, prefix, ha_prefix):
    pending = set()
    for component in ('sensor', 'binary_sensor'):
        pending.add(mqtt.subscribe(f'{ha_prefix}/{component}/+/+/config'))
    records = {}
    deadline = time.monotonic() + 10
    quiet_since = time.monotonic()
    while time.monotonic() < deadline:
        readable, _, _ = select.select([mqtt.sock], [], [], min(0.2, max(0, deadline-time.monotonic())))
        if not readable:
            if not pending and time.monotonic() - quiet_since >= 1:
                return list(records.values())
            continue
        quiet_since = time.monotonic()
        kind, flags, body = mqtt.receive()
        if kind == 9:
            if len(body) != 3 or body[2] != 0:
                raise ConnectionError('Discovery subscription rejected')
            pending.discard(int.from_bytes(body[:2], 'big'))
            continue
        if kind != 3:
            continue
        topic, payload, retained = mqtt.parse_publish(flags, body)
        if not payload:
            continue
        record = telemetry.validate_config(topic, payload, prefix, ha_prefix)
        if record:
            records[topic] = record
            if len(records) > 256:
                raise ValueError('Too many migration entities')
    raise TimeoutError('Discovery snapshot did not settle')


class Worker(ControlPublisher):
    def __init__(self, config):
        self.config = config
        self.gateway = config['gateway_host']
        self.gateway_password = config['device_id'].strip()[:8]
        self.private_key = str(storage.ROOT / 'ssh/addon_ed25519')
        self.topic_prefix = config['mqtt_topic_prefix']
        self.ha_prefix = config['mqtt_ha_prefix']
        self.availability_topic = self.topic_prefix + '/local/availability'
        self.tunnel = None
        self.gw = Gateway(self.gateway, config['device_id'].strip(), self.private_key)

    def cleanup(self, mqtt, ws):
        for client in (mqtt, ws):
            try:
                if client is not None:
                    client.close()
            except Exception:
                pass
        if self.tunnel is not None:
            self.tunnel.terminate()
            try:
                self.tunnel.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.tunnel.kill()
                self.tunnel.wait()
            self.tunnel = None

    def activate(self, plan, mowers, devices):
        telemetry.readings(plan, devices)
        previous = self.gw.publisher_state()
        # Write recovery journal BEFORE touching the existing service.
        state = {'phase': 'taking_over', 'binding': binding(self.config), 'previous': previous,
                 'plan': plan, 'mower_ids': [m.device_id for m in mowers]}
        storage.write('migration.json', state)
        self.gw.stop_publisher()
        state['phase'] = 'active'
        storage.write('migration.json', state)
        return state

    def rollback(self, mqtt, state):
        mqtt.publish(self.availability_topic, 'offline', retain=True)
        for record in state['plan']:
            original = record.get('original')
            mqtt.publish(record['topic'], json.dumps(original) if original else '', retain=True)
        # Legacy HA controller republishes its control discovery on restart.
        for key in {r['key'] for r in state['plan']}:
            mqtt.publish(f'{self.ha_prefix}/lawn_mower/gardena_{key}/config', '', retain=True)
            for hours in (1, 3, 6):
                mqtt.publish(f'{self.ha_prefix}/button/gardena_{key}/start_{hours}h/config', '', retain=True)
        self.gw.restore_publisher(state['previous'])
        storage.write('migration.json', None)

    def run(self):
        cfg = self.config
        mqtt = MqttClient(cfg['mqtt_broker_host'], cfg['mqtt_broker_port'],
            cfg.get('mqtt_broker_user', ''), cfg.get('mqtt_broker_password', ''),
            'gardena-local-' + secrets.token_hex(4))
        ws = None
        active = False
        try:
            state = storage.read('migration.json')
            if state and state['binding'] != binding(cfg):
                raise ValueError('Configuration changed during migration; restore original settings')
            mqtt.connect(self.availability_topic)
            self.gw.prepare()
            # Rollback must work even if sensor parsing or WebSocket discovery fails.
            if state and storage.read('action.json') == 'rollback':
                self.rollback(mqtt, state)
                storage.write('action.json', None)
                report('rolled_back', message='Alte App jetzt wieder starten. Diese App stoppen.')
                return
            self.start_tunnel()
            ws = WebSocketClient('127.0.0.1', LOCAL_TUNNEL_PORT, self.gateway_password)
            mowers = discover_mowers(ws)
            devices = telemetry.parse_snapshot(self.gw.snapshot())
            if state:
                plan = state['plan']
                telemetry.readings(plan, devices)
                if [m.device_id for m in mowers] != state['mower_ids']:
                    raise ValueError('Gateway device set changed')
                if mowers:
                    mowers[0].publisher_key = plan[0]['key']
                    mowers[0].publisher_identifier = 'gardena_' + plan[0]['key']
                if state['phase'] == 'taking_over':
                    self.gw.restore_publisher(state['previous'])
                    storage.write('migration.json', None)
                    state = None
                else:
                    self.gw.stop_publisher()
                    active = True
            else:
                existing = collect_existing(mqtt, self.topic_prefix, self.ha_prefix)
                plan = telemetry.make_plan(devices, existing, self.topic_prefix, self.ha_prefix, mowers)
            mqtt.subscribe(self.topic_prefix + '/+/mower/command')
            by_topic = {f'{self.topic_prefix}/{m.key}/mower/command': m for m in mowers}
            pending = {}
            next_poll = 0
            while True:
                action = storage.read('action.json')
                if action:
                    storage.write('action.json', None)
                    if action == 'activate' and not active:
                        devices = telemetry.parse_snapshot(self.gw.snapshot())
                        # Re-check all retained entities at the exact handover point.
                        existing = collect_existing(mqtt, self.topic_prefix, self.ha_prefix)
                        plan = telemetry.make_plan(devices, existing, self.topic_prefix, self.ha_prefix, mowers)
                        state = self.activate(plan, mowers, devices)
                        active = True
                        by_topic = {f'{self.topic_prefix}/{m.key}/mower/command': m for m in mowers}
                        next_poll = 0
                    elif action == 'rollback' and state:
                        self.rollback(mqtt, state)
                        report('rolled_back', message='Alte App jetzt wieder starten. Diese App stoppen.')
                        return
                if time.monotonic() >= next_poll:
                    devices = telemetry.parse_snapshot(self.gw.snapshot())
                    telemetry.readings(plan, devices)
                    if active:
                        telemetry.publish(mqtt, plan, devices, self.availability_topic)
                        for mower in mowers:
                            self.publish_discovery(mqtt, mower)
                        mqtt.publish(self.availability_topic, 'online', retain=True)
                    report('active' if active else 'preview', sensors=len(plan), mowers=len(mowers),
                           message='Sensoren und Steuerung aktiv.' if active else 'Vorschau bereit. Alte HA-App vor Übernahme stoppen.')
                    next_poll = time.monotonic() + 30
                buffered = ws.sock.pending() > 0
                readable, _, _ = select.select([mqtt.sock, ws.sock], [], [], 0 if buffered else 1)
                if mqtt.sock in readable:
                    kind, flags, body = mqtt.receive()
                    if kind == 3:
                        topic, action, retained = mqtt.parse_publish(flags, body)
                        mower = by_topic.get(topic)
                        if active and mower and not retained and len(pending) < MAX_PENDING_COMMANDS:
                            try:
                                command = mower.command(action.strip())
                            except ValueError:
                                continue
                            ws.send_text(json.dumps(command))
                            pending[command[0]['request_id']] = (mower, time.monotonic() + 30)
                if buffered or ws.sock in readable:
                    raw = ws.receive_text()
                    if raw:
                        for message in gateway_messages(raw):
                            result = pending.pop(message.get('request_id'), None)
                            if result:
                                print('[gardena-local] Befehl ' + ('bestätigt' if message.get('success') else 'abgelehnt'), flush=True)
                            for mower in mowers:
                                if apply_event(mower, message) and active:
                                    self.publish_activity(mqtt, mower)
                for request_id, (mower, deadline) in list(pending.items()):
                    if time.monotonic() >= deadline:
                        pending.pop(request_id)
                        print('[gardena-local] Keine Befehlsbestätigung', flush=True)
                if time.monotonic() - mqtt.last_io > 15:
                    mqtt.ping()
        finally:
            try:
                if active:
                    mqtt.publish(self.availability_topic, 'offline', retain=True)
            except Exception:
                pass
            self.cleanup(mqtt, ws)


def main():
    def stop(*_):
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, stop)
    while True:
        try:
            Worker(options()).run()
            return
        except Exception as exc:
            # No raw exception, gateway data or credentials in UI/log output.
            explanations = {
                'No supported LsDL sensor values': 'Keine unterstützten Sensordaten gefunden. Das Gateway-Schema muss geprüft werden.',
                'Migration requires exactly one device and one mower': 'Die Übernahme unterstützt derzeit genau ein Sensorgerät und einen Mäher.',
                'Mower sensor association is ambiguous': 'Mäher und Sensorgerät können nicht eindeutig zugeordnet werden.',
                'Existing sensor has no current gateway value': 'Mindestens ein bisheriger Sensor hat keinen passenden Gateway-Wert. Keine Übernahme.',
                'Unsupported existing sensor resource': 'Ein vorhandener Sensortyp benötigt noch einen Adapter. Keine Übernahme.',
                'Configuration changed during migration; restore original settings': 'Bitte die Einstellungen vor der Übernahme wiederherstellen; danach ist die Rückkehr möglich.',
                'Sensor disappeared from gateway snapshot': 'Ein übernommener Sensor fehlt im aktuellen Gateway-Abbild.',
            }
            message = explanations.get(str(exc), 'Verbindung oder Zuordnung fehlgeschlagen: ' + type(exc).__name__)
            report('error', message=message)
            print('[gardena-local] Nicht bereit (' + type(exc).__name__ + '); neuer Versuch in 15s', flush=True)
            time.sleep(15)


if __name__ == '__main__':
    main()
