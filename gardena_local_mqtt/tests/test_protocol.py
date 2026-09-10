import base64
import json
import struct
import sys
import unittest
from pathlib import Path


ADDON_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADDON_DIR))

import protocol as control  # noqa: E402


class MqttProtocolTests(unittest.TestCase):
    def test_remaining_length_encoding(self):
        self.assertEqual(control._encode_remaining_length(0), b"\x00")
        self.assertEqual(control._encode_remaining_length(127), b"\x7f")
        self.assertEqual(control._encode_remaining_length(128), b"\x80\x01")
        self.assertEqual(control._encode_remaining_length(16384), b"\x80\x80\x01")

    def test_publish_parser_rejects_retained_commands(self):
        body = control._mqtt_string("gardena/abcd/mower/command") + b"dock"
        topic, payload, retained = control.MqttClient.parse_publish(1, body)
        self.assertEqual(topic, "gardena/abcd/mower/command")
        self.assertEqual(payload, "dock")
        self.assertTrue(retained)


class MowerCommandTests(unittest.TestCase):
    def mower(self, model, generation, data=None):
        return control.Mower(
            "raw-device-id",
            model,
            "Test mower",
            generation,
            data or {},
        )

    def test_gen1_start_command(self):
        command = self.mower("29694", "gen1").command("start_mowing")[0]
        self.assertEqual(command["op"], "write")
        self.assertEqual(command["entity"]["service"], "lemonbeatd")
        self.assertEqual(command["entity"]["path"], "lemonbeat/0/mower_timer")
        self.assertEqual(command["payload"], {"vi": 28800})

    def test_gen1_dock_command_uses_opaque_pause_date(self):
        command = self.mower("29694", "gen1").command("dock")[0]
        raw = base64.b64decode(command["payload"]["vo"])
        self.assertEqual(command["entity"]["path"], "lemonbeat/0/action_paused_until_1")
        self.assertEqual(raw, (2042).to_bytes(2, "little") + bytes([12, 31, 22, 0]))

    def test_lona_start_command_contains_distance_and_duration(self):
        command = self.mower("53988", "gen1_lona").command("start_mowing")[0]
        raw = base64.b64decode(command["payload"]["vo"])
        self.assertEqual(command["entity"]["path"], "lemonbeat/0/mower_timer_with_distance")
        self.assertEqual(struct.unpack("!HI", raw), (0, 28800))

    def test_gen2_commands(self):
        mower = self.mower("488", "gen2")
        start = mower.command("start_mowing")[0]
        dock = mower.command("dock")[0]
        pause = mower.command("pause")[0]
        self.assertEqual(start["op"], "execute")
        self.assertEqual(start["entity"]["service"], "lwm2mserver")
        self.assertEqual(start["payload"], {"as": ["0='28800'"]})
        self.assertEqual(
            dock["entity"]["path"],
            "smart_system_mower_api/0/park_until_further_notice",
        )
        self.assertEqual(pause["entity"]["path"], "mower_app/0/pause")

    def test_gen1_does_not_advertise_pause(self):
        mower = self.mower("29694", "gen1")
        payload = control.discovery_payload(mower, "gardena", "gardena/control/availability")
        self.assertNotIn("pause_command_topic", payload)
        with self.assertRaisesRegex(ValueError, "Pause"):
            mower.command("pause")

    def test_discovery_is_native_lawn_mower_and_contains_no_raw_id(self):
        mower = self.mower("488", "gen2")
        payload = control.discovery_payload(mower, "gardena", "gardena/control/availability")
        serialized = json.dumps(payload)
        self.assertEqual(payload["start_mowing_command_topic"], payload["dock_command_topic"])
        self.assertEqual(payload["pause_command_topic"], payload["dock_command_topic"])
        self.assertNotIn("raw-device-id", serialized)
        self.assertIn(mower.key, serialized)

    def test_activity_mapping(self):
        gen1 = self.mower(
            "29694",
            "gen1",
            {"lemonbeat": {"0": {"status": {"vi": 15}}}},
        )
        gen2 = self.mower(
            "488",
            "gen2",
            {"mower_app": {"0": {"activity": {"vi": 4}, "state": {"vi": 6}}}},
        )
        self.assertEqual(gen1.activity(), "mowing")
        self.assertEqual(gen2.activity(), "returning")

    def test_gen1_leaving_station_is_active_and_unknown_is_not_published(self):
        leaving = self.mower(
            "29694",
            "gen1",
            {"lemonbeat": {"0": {"status": {"vi": 4}}}},
        )
        unknown = self.mower(
            "29694",
            "gen1",
            {"lemonbeat": {"0": {"status": {"vi": 11}}}},
        )
        self.assertEqual(leaving.activity(), "mowing")
        self.assertIsNone(unknown.activity())

    def test_event_updates_activity(self):
        mower = self.mower(
            "29694",
            "gen1",
            {"lemonbeat": {"0": {"status": {"vi": 3}}}},
        )
        changed = control.apply_event(
            mower,
            {
                "op": "update",
                "entity": {"device": "raw-device-id", "path": "lemonbeat/0/status"},
                "payload": {"vi": 2},
            },
        )
        self.assertTrue(changed)
        self.assertEqual(mower.activity(), "returning")


if __name__ == "__main__":
    unittest.main()
