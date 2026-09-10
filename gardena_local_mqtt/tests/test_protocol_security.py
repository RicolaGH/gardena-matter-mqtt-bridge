import hashlib
import json
import struct
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import protocol as control

class ProtocolSecurityTests(unittest.TestCase):
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


    def test_malformed_gateway_messages_are_ignored(self):
        for value in ['null', '1', '[null]', '[{"entity":1}]', '[{"payload":[]}]']:
            self.assertEqual(control.gateway_messages(value), [])


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

