import hashlib
import io
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tarfile
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
ADDON = ROOT / 'gardena_matter_bridge'
sys.path.insert(0, str(ADDON))
import mqtt_control as control
import orchestrate as orch
import security
import web_ui


class IngressTests(unittest.TestCase):
    def test_direct_client_cannot_deploy_or_read_status_even_with_forged_headers(self):
        server = web_ui.IngressServer(('127.0.0.1', 0), web_ui.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with patch.object(web_ui, 'start_deploy_async') as deploy:
                for path, method in [('/api/deploy', 'POST'), ('/api/status', 'GET')]:
                    request = urllib.request.Request(
                        f'http://127.0.0.1:{server.server_port}{path}', method=method,
                        headers={'X-Forwarded-For': '172.30.32.2', 'X-Ingress-Path': '/test'})
                    with self.assertRaises(urllib.error.HTTPError) as raised:
                        urllib.request.urlopen(request, timeout=2)
                    self.assertEqual(raised.exception.code, 403)
                deploy.assert_not_called()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_real_ingress_can_dispatch_deploy(self):
        handler = object.__new__(web_ui.Handler)
        handler.client_address = ('172.30.32.2', 2345)
        handler.path = '/api/deploy'
        handler._send_json = Mock()
        with patch.object(web_ui, 'start_deploy_async', return_value=True) as deploy:
            handler.do_POST()
        deploy.assert_called_once()
        self.assertEqual(handler._send_json.call_args.args[0], 202)


class ProtocolTests(unittest.TestCase):
    def test_invalid_discovery_shapes_are_ignored(self):
        for value in ['null', '[]', '1', '"string"', '{"device":1}', '{"device":{"identifiers":1}}']:
            self.assertIsNone(control.publisher_mower_identity('homeassistant/sensor/gardena_abcd/mower_status/config', value))
            self.assertIsNone(control.corrected_mower_status_discovery('homeassistant/sensor/gardena_abcd/mower_status/config', value))

    def test_repair_only_for_associated_publisher_topic(self):
        topic = 'homeassistant/sensor/gardena_abcd/mower_status/config'
        config = {'device': {'manufacturer': 'GARDENA', 'identifiers': ['gardena_abcd']},
                  'state_topic': 'gardena/abcd/mower_status/state', 'value_template': 'old'}
        self.assertIsNone(control.corrected_mower_status_discovery(topic, json.dumps(config)))
        fixed = control.corrected_mower_status_discovery(topic, json.dumps(config), allowed_keys={'abcd'})
        self.assertEqual(json.loads(fixed)['value_template'], control.MOWER_STATUS_VALUE_TEMPLATE)
        config['device']['manufacturer'] = 'Other'
        self.assertIsNone(control.corrected_mower_status_discovery(topic, json.dumps(config), allowed_keys={'abcd'}))

    def test_mqtt_limit_checked_before_body_read(self):
        client = control.MqttClient('', 0, '', '', '')
        client._recv_exact = Mock(side_effect=[b'\x30', b'\xff', b'\xff', b'\xff', b'\x7f'])
        with self.assertRaisesRegex(ValueError, 'size limit'):
            client.receive()
        self.assertTrue(all(call.args == (1,) for call in client._recv_exact.call_args_list))

    def test_websocket_limit_checked_before_body_read(self):
        client = object.__new__(control.WebSocketClient)
        client._recv_exact = Mock(side_effect=[b'\x81\x7f', struct.pack('!Q', 2**40)])
        with self.assertRaisesRegex(ValueError, 'size limit'):
            client.receive_text()
        self.assertEqual(client._recv_exact.call_count, 2)

    def test_packet_deadline_is_not_reset_by_drip_feed(self):
        client = control.MqttClient('', 0, '', '', '')
        client.sock = Mock()
        client.sock.gettimeout.return_value = 15
        client.sock.recv.return_value = b'a'
        client._packet_deadline = 10
        with patch.object(control.time, 'monotonic', side_effect=[0, 0, 11, 11]):
            with self.assertRaises(TimeoutError):
                client._recv_exact(10)
        self.assertEqual(client.sock.recv.call_count, 1)

    def test_unexpected_worker_exception_retries_without_secret_log(self):
        controller = object.__new__(control.Controller)
        controller.run_once = Mock(side_effect=[AttributeError('SECRET'), KeyboardInterrupt()])
        with patch.object(control.time, 'sleep'), patch.object(control, 'log') as log:
            with self.assertRaises(KeyboardInterrupt):
                controller.run_forever()
        self.assertEqual(controller.run_once.call_count, 2)
        self.assertNotIn('SECRET', str(log.call_args_list))

    def test_malformed_gateway_messages_are_ignored(self):
        for value in ['null', '1', '[null]', '[{"entity":1}]', '[{"payload":[]}]']:
            self.assertEqual(control.gateway_messages(value), [])


class DeployTests(unittest.TestCase):
    def test_no_gateway_mutation_when_release_verification_fails(self):
        gateway = Mock()
        runner = Mock()
        plan = orch.DeployPlan('192.0.2.1', '/key', '/key.pub', 'owner/repo', 'v1', 'a'*64)
        with patch.object(orch, 'fetch_and_verify_release', side_effect=orch.OrchestrationError('hash mismatch')):
            with self.assertRaisesRegex(orch.OrchestrationError, 'hash mismatch'):
                orch.run_full_deploy(plan, gateway=gateway, device_id='12345678-device',
                                    read_public_key=Mock(), downloader=Mock(), read_bytes=Mock(), ssh_runner=runner)
        self.assertEqual(gateway.mock_calls, [])
        runner.assert_not_called()

    def test_disable_policy_applies_on_failure_only_when_enabled_here(self):
        for reachable, expected in [(False, [True, False]), (True, [])]:
            gateway = Mock(session='session')
            plan = orch.DeployPlan('192.0.2.1', '/key', '/key.pub', 'owner/repo', 'v1', 'a'*64, disable_ssh_after=True)
            artifact = orch.ReleaseArtifact('/unused', 'a'*64)
            bundle = orch.UnpackedBundle('/unused/binary', '/unused/libs')
            with patch.object(orch, 'fetch_and_verify_release', return_value=artifact), \
                 patch.object(orch, 'unpack_bundle', return_value=bundle), \
                 patch.object(orch, 'ssh_reachable', return_value=reachable), \
                 patch.object(orch, 'deploy_via_ssh', side_effect=orch.OrchestrationError('failure')):
                with self.assertRaises(orch.OrchestrationError):
                    orch.run_full_deploy(plan, gateway=gateway, device_id='12345678-device',
                                        read_public_key=lambda p:'ssh-ed25519 dummy', downloader=Mock(), read_bytes=Mock(), ssh_runner=Mock())
            self.assertEqual([c.args[0] for c in gateway.set_ssh_enabled.call_args_list], expected)

    def test_safe_tar_rejects_traversal(self):
        with tempfile.TemporaryDirectory() as td:
            archive = Path(td)/'archive.tgz'
            with tarfile.open(archive, 'w:gz') as tar:
                member = tarfile.TarInfo('../escape'); member.size = 1
                tar.addfile(member, io.BytesIO(b'x'))
            with self.assertRaises(tarfile.FilterError):
                orch._default_tar_extract(str(archive), str(Path(td)/'out'))
            self.assertFalse((Path(td)/'escape').exists())

    def test_old_python_never_falls_back_to_unsafe_extraction(self):
        with tempfile.TemporaryDirectory() as td, patch.object(orch.tarfile, 'open') as open_tar:
            tar = open_tar.return_value.__enter__.return_value
            tar.extractall.side_effect = TypeError('filter unsupported')
            with self.assertRaises(orch.OrchestrationError):
                orch._default_tar_extract('archive', td)
            self.assertEqual(tar.extractall.call_count, 1)

    def test_stale_bundle_members_cannot_satisfy_new_bundle(self):
        with tempfile.TemporaryDirectory() as td:
            for name in [orch.BUNDLE_BINARY_NAME, orch.BUNDLE_LIBS_NAME]:
                (Path(td)/name).write_text('stale')
            with self.assertRaisesRegex(orch.OrchestrationError, 'fehlende Member'):
                orch.unpack_bundle('archive', td, extractor=lambda a,d:None)

    def test_config_control_characters_rejected_without_echoing_password(self):
        for secret in ['password\nENVEOF', 'password\r', 'password\x00']:
            with self.assertRaises(orch.OrchestrationError) as raised:
                orch.validate_mqtt_config(orch.MqttConfig(enable=True, broker_host='mqtt.local', broker_password=secret))
            self.assertNotIn(secret, str(raised.exception))

    def test_runner_passes_secrets_via_stdin_not_argv(self):
        command = security.Command(['bash', 'installer'], input=b'SECRET', env={'TEST':'1'})
        with patch.object(orch.subprocess, 'run', return_value=Mock(returncode=0)) as run:
            orch.real_subprocess_runner(command)
        self.assertNotIn('SECRET', str(run.call_args.args))
        self.assertEqual(run.call_args.kwargs['input'], b'SECRET')

    def test_actual_installer_never_interprets_config_as_shell(self):
        with tempfile.TemporaryDirectory() as td:
            temp = Path(td)
            ssh = temp/'ssh'
            # Execute only the actual config-writing command, mapped into a temporary directory.
            ssh.write_text('''#!/usr/bin/env python3
import os, sys, subprocess
from pathlib import Path
cmd=sys.argv[-1]
root=Path(os.environ['TEST_ROOT'])
with (root/'argv').open('a') as f: f.write(repr(sys.argv)+'\\n')
if 'cat > "$tmp"' in cmd:
    cmd=cmd.replace('/etc/gardena-matter', str(root/'remote'))
    sys.exit(subprocess.run(['sh','-c',cmd]).returncode)
''')
            ssh.chmod(0o700)
            scp = temp/'scp'; scp.write_text('#!/bin/sh\nexit 0\n'); scp.chmod(0o700)
            data = b'MQTT_BROKER_PASS="dummy;$(touch SHOULD_NOT_EXIST)"\nENVEOF\nprintf injected\n'
            env={**os.environ, 'PATH':td+':'+os.environ['PATH'], 'TEST_ROOT':td,
                 'GATEWAY_IP':'192.0.2.1','GARDENA_SSH_KEY':'/dummy key',
                 'MQTT_BINARY':'/dummy binary','MQTT_SERVICE':'/dummy service'}
            result=subprocess.run(['bash',str(ADDON/'install-scripts/install_mqtt_publisher.sh')],input=data,env=env,cwd=td,capture_output=True)
            self.assertEqual(result.returncode,0,result.stderr.decode())
            self.assertEqual((temp/'remote/mqtt.env').read_bytes(),data)
            self.assertEqual((temp/'remote/mqtt.env').stat().st_mode & 0o777,0o600)
            self.assertFalse((temp/'SHOULD_NOT_EXIST').exists())
            self.assertNotIn('dummy;', (temp/'argv').read_text())


class PolicyTests(unittest.TestCase):
    def test_ssh_rejects_changed_keys_and_uses_persistent_store(self):
        options=security.ssh_options()
        self.assertIn('StrictHostKeyChecking=accept-new',options)
        self.assertNotIn('StrictHostKeyChecking=no', options)
        self.assertIn('UserKnownHostsFile=/data/ssh/known_hosts_gardena',options)

    def test_compatibility_copies_match_addon(self):
        for name in ['orchestrate.py','mqtt_control.py','web_ui.py','security.py','run.sh','supervise_control.py']:
            self.assertEqual((ROOT/name).read_bytes(),(ADDON/name).read_bytes(),name)
        for p in (ADDON/'install-scripts').glob('*.sh'):
            self.assertEqual((ROOT/p.name).read_bytes(),p.read_bytes(),p.name)

class WorkerAndHandshakeTests(unittest.TestCase):
    def test_supervisor_restarts_worker_and_cleans_process_groups(self):
        import supervise_control
        import signal
        handlers = {}
        first = Mock(pid=101)
        first.poll.return_value = 1
        second = Mock(pid=102)
        second.poll.return_value = None
        def spawn(*args, **kwargs):
            if spawn.count == 0:
                spawn.count += 1
                return first
            handlers[signal.SIGTERM](signal.SIGTERM, None)
            return second
        spawn.count = 0
        with patch.object(supervise_control.signal, 'signal', side_effect=lambda s,h:handlers.update({s:h})), \
             patch.object(supervise_control.subprocess, 'Popen', side_effect=spawn) as popen, \
             patch.object(supervise_control.os, 'killpg') as killpg, \
             patch.object(supervise_control.time, 'sleep'):
            supervise_control.main()
        self.assertEqual(popen.call_count, 2)
        self.assertTrue(popen.call_args.kwargs['start_new_session'])
        self.assertIn(((101, signal.SIGKILL),), [(c.args,) for c in killpg.call_args_list])
        self.assertIn(((102, signal.SIGKILL),), [(c.args,) for c in killpg.call_args_list])

    def test_handshake_validates_accept_and_preserves_first_frame(self):
        import base64
        class Socket:
            def __init__(self, valid):
                self.valid = valid
                self.timeout = 15
                self.closed = False
            def settimeout(self, value): self.timeout = value
            def gettimeout(self): return self.timeout
            def sendall(self, request):
                key = request.split(b'Sec-WebSocket-Key: ')[1].split(b'\r\n')[0]
                accept = base64.b64encode(hashlib.sha1(key+b'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').digest())
                if not self.valid: accept = b'invalid'
                self.data = bytearray(b'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: '+accept+b'\r\n\r\n\x81\x02[]')
            def recv(self, count):
                value = bytes(self.data[:count]); del self.data[:count]; return value
            def close(self): self.closed = True
        for valid in (True, False):
            sock = Socket(valid)
            context = Mock()
            context.wrap_socket.return_value = sock
            with patch.object(control.socket, 'create_connection', return_value=sock), \
                 patch.object(control.ssl, 'SSLContext', return_value=context):
                if valid:
                    client = control.WebSocketClient('127.0.0.1', 1, 'dummy')
                    self.assertEqual(client.receive_text(), '[]')
                else:
                    with self.assertRaises(ConnectionError):
                        control.WebSocketClient('127.0.0.1', 1, 'dummy')
                    self.assertTrue(sock.closed)
