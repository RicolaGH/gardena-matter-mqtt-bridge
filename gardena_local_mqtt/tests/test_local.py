import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app
import gateway
import protocol
import storage
import telemetry
import worker


def archive(files):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w') as tar:
        for name, value in files.items():
            raw = json.dumps(value).encode()
            info = tarfile.TarInfo(name)
            info.size = len(raw)
            tar.addfile(info, io.BytesIO(raw))
    return output.getvalue()


def fixture():
    return archive({
        'Device_descriptionID_1/Device_descriptionID_1.json': {'serialid': 'test-device'},
        'Device_descriptionID_1/Value_description/123.json': {'id': 123, 'name': 'battery_level'},
        'Device_descriptionID_1/Value/Value_123r.json': {'value': '75'},
        'Device_descriptionID_1/Value_description/Value_description_456.json': {'name': 'status'},
        'Device_descriptionID_1/Value/Value_456r.json': {'value': '8'},
    })


def mower():
    return protocol.Mower('test-device', '6146', 'GARDENA smart SILENO', 'gen1',
        {'lemonbeat': {'0': {'status': {'vi': 8}}}})


def existing():
    return [telemetry.validate_config('homeassistant/sensor/gardena_abcd/mower_status/config',
        json.dumps({'unique_id': 'gardena_abcd_mower_status', 'state_topic': 'gardena/abcd/status/state',
            'device': {'identifiers': ['gardena_abcd'], 'manufacturer': 'GARDENA'},
            'availability_topic': 'gardena/abcd/availability'}), 'gardena', 'homeassistant')]


def config():
    return {'gateway_host': '192.0.2.1', 'device_id': 'testonly-device',
        'mqtt_broker_host': '192.0.2.2', 'mqtt_broker_port': 1883,
        'mqtt_broker_user': '', 'mqtt_broker_password': '',
        'mqtt_topic_prefix': 'gardena', 'mqtt_ha_prefix': 'homeassistant'}


class SensorTests(unittest.TestCase):
    def test_reads_documented_lsdl_layout_and_filename_id(self):
        devices = telemetry.parse_snapshot(fixture())
        self.assertEqual(devices[0].serial, 'test-device')
        self.assertEqual(devices[0].values, {'battery_level': '75', 'status': '8'})

    def test_no_path_traversal_or_large_member(self):
        for data in (archive({'../escape.json': {}}), archive({'big.json': {'x': 'x'*65537}})):
            with self.assertRaises(ValueError):
                telemetry.parse_snapshot(data)

    def test_invalid_numeric_values_not_published(self):
        for value in ('NaN', 'inf', {}, True):
            with self.assertRaises(ValueError):
                telemetry.numeric(value)

    def test_existing_topics_ids_and_device_are_preserved(self):
        devices = telemetry.parse_snapshot(fixture())
        m = mower()
        plan = telemetry.make_plan(devices, existing(), 'gardena', 'homeassistant', [m])
        self.assertEqual(m.key, 'abcd')
        self.assertEqual(m.device_identifier, 'gardena_abcd')
        mqtt = Mock()
        telemetry.publish(mqtt, plan, devices, 'gardena/local/availability')
        topic, raw = mqtt.publish.call_args_list[0].args
        self.assertEqual(topic, 'homeassistant/sensor/gardena_abcd/mower_status/config')
        self.assertEqual(json.loads(raw)['unique_id'], 'gardena_abcd_mower_status')
        self.assertEqual(mqtt.publish.call_args_list[1].args, ('gardena/abcd/status/state', '8'))

    def test_multi_device_migration_is_blocked(self):
        devices = telemetry.parse_snapshot(fixture()) * 2
        with self.assertRaises(ValueError):
            telemetry.make_plan(devices, existing(), 'gardena', 'homeassistant', [mower()])

    def test_missing_sensor_blocks_all_publication(self):
        devices = telemetry.parse_snapshot(fixture())
        plan = telemetry.make_plan(devices, existing(), 'gardena', 'homeassistant', [mower()])
        devices[0].values.pop('status')
        mqtt = Mock()
        with self.assertRaises(ValueError):
            telemetry.publish(mqtt, plan, devices, 'availability')
        mqtt.publish.assert_not_called()

    def test_fresh_device_is_stable_without_broker_records(self):
        devices = telemetry.parse_snapshot(fixture())
        first = telemetry.make_plan(devices, [], 'gardena', 'homeassistant', [mower()])
        second = telemetry.make_plan(devices, [], 'gardena', 'homeassistant', [mower()])
        self.assertEqual(first, second)
        self.assertEqual(len(first), 2)

    def test_unsupported_existing_sensor_blocks_migration(self):
        payload = existing()[0]['original']
        payload['state_topic'] = 'gardena/abcd/unsupported/state'
        with self.assertRaises(ValueError):
            telemetry.validate_config(existing()[0]['topic'], json.dumps(payload), 'gardena', 'homeassistant')


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = patch.object(storage, 'ROOT', Path(self.temp.name))
        self.root.start()
        self.worker = worker.Worker(config())
        self.worker.gw = Mock()
        self.worker.gw.publisher_state.return_value = {'active': True, 'enabled': True}
        self.devices = telemetry.parse_snapshot(fixture())
        self.mowers = [mower()]
        self.plan = telemetry.make_plan(self.devices, existing(), 'gardena', 'homeassistant', self.mowers)

    def tearDown(self):
        self.root.stop()
        self.temp.cleanup()

    def test_journal_precedes_service_stop(self):
        def stop():
            self.assertEqual(storage.read('migration.json')['phase'], 'taking_over')
        self.worker.gw.stop_publisher.side_effect = stop
        self.worker.activate(self.plan, self.mowers, self.devices)
        self.assertEqual(storage.read('migration.json')['phase'], 'active')
        self.assertEqual((storage.ROOT/'migration.json').stat().st_mode & 0o777, 0o600)

    def test_no_service_mutation_when_validation_fails(self):
        self.devices[0].values.clear()
        with self.assertRaises(ValueError):
            self.worker.activate(self.plan, self.mowers, self.devices)
        self.worker.gw.stop_publisher.assert_not_called()
        self.assertIsNone(storage.read('migration.json'))

    def test_failed_takeover_retains_recovery_journal(self):
        self.worker.gw.stop_publisher.side_effect = ConnectionError()
        with self.assertRaises(ConnectionError):
            self.worker.activate(self.plan, self.mowers, self.devices)
        self.assertEqual(storage.read('migration.json')['phase'], 'taking_over')

    def test_rollback_restores_discovery_and_previous_service_state(self):
        state = self.worker.activate(self.plan, self.mowers, self.devices)
        mqtt = Mock()
        self.worker.rollback(mqtt, state)
        payload = json.loads(mqtt.publish.call_args_list[1].args[1])
        self.assertEqual(payload['availability_topic'], 'gardena/abcd/availability')
        self.worker.gw.restore_publisher.assert_called_once_with({'active': True, 'enabled': True})
        self.assertIsNone(storage.read('migration.json'))

    def test_configuration_cannot_retarget_active_migration(self):
        cfg = config()
        before = worker.binding(cfg)
        cfg['gateway_host'] = '192.0.2.3'
        self.assertNotEqual(before, worker.binding(cfg))


class GatewayTests(unittest.TestCase):
    def test_bootstrap_uses_local_api_and_only_public_key(self):
        gw = gateway.Gateway('192.0.2.1', 'testonly-device', '/unused/key')
        gw.request = Mock(side_effect=[{'session': 'test-session'}, {}, {}, {}])
        gw.ssh = Mock(side_effect=ConnectionError())
        with patch.object(Path, 'read_text', return_value='ssh-ed25519 AAAA test'):
            gw.prepare()
        paths = [(c.args[0], c.args[1]) for c in gw.request.call_args_list]
        self.assertEqual(paths, [('POST','/login'), ('POST','/ssh_access_credentials'),
            ('PUT','/ssh_access_enable'), ('PUT','/websocket_api')])
        self.assertNotIn('testonly', str(gw.request.call_args_list[1:]))

    def test_existing_key_is_not_replaced(self):
        gw = gateway.Gateway('192.0.2.1', 'testonly-device', '/unused/key')
        gw.request = Mock(side_effect=[{'session': 'test-session'}, {}])
        gw.ssh = Mock(return_value=b'')
        gw.prepare()
        self.assertEqual(gw.request.call_count, 2)

    def test_no_bridge_firewall_or_download_dependency(self):
        folder = Path(__file__).resolve().parents[1]
        for path in list(folder.glob('*.py')) + [folder/'Dockerfile', folder/'run.sh']:
            code = path.read_text()
            for forbidden in ('gardena-matter-toggle', 'iptables', 'bridge-release.lock', 'api.github.com', 'scp '):
                self.assertNotIn(forbidden, code, path.name)


class IngressTests(unittest.TestCase):
    def test_direct_clients_cannot_read_or_activate_even_with_forged_headers(self):
        server = app.Server(('127.0.0.1', 0), app.Handler)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            with patch.object(storage, 'write') as write:
                for method, path in [('GET','/api/status'), ('POST','/api/activate')]:
                    request = urllib.request.Request(f'http://127.0.0.1:{server.server_port}{path}',
                        method=method, headers={'X-Forwarded-For': '172.30.32.2'})
                    with self.assertRaises(urllib.error.HTTPError) as raised:
                        urllib.request.urlopen(request, timeout=2)
                    self.assertEqual(raised.exception.code, 403)
                write.assert_not_called()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


class LoopEnd(Exception):
    pass


class RuntimeTests(unittest.TestCase):
    setUp = MigrationTests.setUp
    tearDown = MigrationTests.tearDown
    def run_loop(self, active=False, retained=False):
        if active:
            self.worker.activate(self.plan, self.mowers, self.devices)
        mqtt = Mock()
        mqtt.last_io = 0
        mqtt.parse_publish.return_value = ('gardena/abcd/mower/command', 'dock', retained)
        mqtt.receive.return_value = (3, 0, b'')
        ws = Mock()
        ws.sock.pending.return_value = 0
        self.worker.gw.snapshot.return_value = fixture()
        with patch.object(worker, 'MqttClient', return_value=mqtt), \
             patch.object(worker, 'WebSocketClient', return_value=ws), \
             patch.object(worker, 'discover_mowers', return_value=self.mowers), \
             patch.object(worker, 'collect_existing', return_value=existing()) as collect, \
             patch.object(self.worker, 'start_tunnel'), \
             patch.object(worker.select, 'select', side_effect=[([mqtt.sock], [], []), LoopEnd()]):
            with self.assertRaises(LoopEnd):
                self.worker.run()
        return mqtt, ws, collect

    def test_preview_neither_publishes_nor_sends_commands_nor_stops_publisher(self):
        mqtt, ws, _ = self.run_loop()
        mqtt.publish.assert_not_called()
        ws.send_text.assert_not_called()
        self.worker.gw.stop_publisher.assert_not_called()
        self.assertEqual(storage.read('status.json')['mode'], 'preview')

    def test_restart_uses_persisted_plan_and_accepts_live_command(self):
        mqtt, ws, collect = self.run_loop(active=True)
        collect.assert_not_called()
        command = json.loads(ws.send_text.call_args.args[0])[0]
        self.assertEqual(command['entity']['path'], 'lemonbeat/0/action_paused_until_1')
        self.assertTrue(any(c.args == ('gardena/local/availability', 'online') for c in mqtt.publish.call_args_list))
        self.assertEqual(mqtt.publish.call_args_list[-1].args, ('gardena/local/availability', 'offline'))

    def test_retained_command_is_never_replayed(self):
        _, ws, _ = self.run_loop(active=True, retained=True)
        ws.send_text.assert_not_called()

    def test_rollback_works_without_a_sensor_snapshot(self):
        state = self.worker.activate(self.plan, self.mowers, self.devices)
        storage.write('action.json', 'rollback')
        with patch.object(worker, 'MqttClient', return_value=Mock()):
            self.worker.run()
        self.worker.gw.snapshot.assert_not_called()
        self.worker.gw.restore_publisher.assert_called_with(state['previous'])
        self.assertEqual(storage.read('status.json')['mode'], 'rolled_back')


if __name__ == '__main__':
    unittest.main()
