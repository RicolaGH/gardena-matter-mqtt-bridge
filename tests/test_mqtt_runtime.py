import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ADDON_DIR = Path(__file__).resolve().parents[1] / "gardena_matter_bridge"
sys.path.insert(0, str(ADDON_DIR))

import orchestrate as orch  # noqa: E402
import web_ui  # noqa: E402


LOCK_SHA256 = "a" * 64


def lock_reader(_path):
    return json.dumps({
        "repo": "owner/repo",
        "tag": "v1.0.0",
        "sha256": LOCK_SHA256,
    })


class MqttConfigurationForwardingTests(unittest.TestCase):
    def test_environment_values_reach_deploy_plan(self):
        env = {
            "GARDENA_GATEWAY_HOST": "192.0.2.10",
            "GARDENA_DEVICE_ID": "secret-device-id",
            "GARDENA_GITHUB_REPO": "owner/repo",
            "GARDENA_RELEASE_TAG": "v1.0.0",
            "GARDENA_ENABLE_MQTT": "true",
            "GARDENA_MQTT_BROKER_HOST": "mqtt.internal",
            "GARDENA_MQTT_BROKER_PORT": "2883",
            "GARDENA_MQTT_BROKER_USER": "gardena",
            "GARDENA_MQTT_BROKER_PASSWORD": "secret-password",
            "GARDENA_MQTT_TOPIC_PREFIX": "garden",
            "GARDENA_MQTT_HA_PREFIX": "ha",
        }
        with patch.dict(os.environ, env, clear=True):
            cfg = web_ui._read_env_config()
            plan = web_ui.build_deploy_plan(cfg, read_text=lock_reader)

        self.assertIsNotNone(plan.mqtt_config)
        mqtt = plan.mqtt_config
        self.assertTrue(mqtt.enable)
        self.assertEqual(mqtt.broker_host, "mqtt.internal")
        self.assertEqual(mqtt.broker_port, 2883)
        self.assertEqual(mqtt.broker_user, "gardena")
        self.assertEqual(mqtt.broker_password, "secret-password")
        self.assertEqual(mqtt.topic_prefix, "garden")
        self.assertEqual(mqtt.ha_prefix, "ha")

    def test_port_and_prefix_defaults(self):
        with patch.dict(os.environ, {}, clear=True):
            mqtt = orch.load_mqtt_config_from_env()

        self.assertEqual(mqtt.broker_port, 1883)
        self.assertEqual(mqtt.topic_prefix, "gardena")
        self.assertEqual(mqtt.ha_prefix, "homeassistant")

    def test_enabled_mqtt_requires_explicit_broker_host(self):
        mqtt = orch.MqttConfig(enable=True, broker_host="")

        with self.assertRaisesRegex(orch.OrchestrationError, "mqtt_broker_host"):
            orch.validate_mqtt_config(mqtt)


class MqttDeploymentTests(unittest.TestCase):
    def _publisher_dir(self, root):
        publisher_dir = Path(root) / "mqtt-publisher"
        publisher_dir.mkdir()
        for name in orch.BUNDLE_MQTT_PUBLISHER_REQUIRED:
            (publisher_dir / name).write_text("test", encoding="utf-8")
        return str(publisher_dir)

    def test_install_receives_all_mqtt_values_and_checks_service(self):
        calls = []
        scp_wrapper_contents = []

        def runner(cmd):
            calls.append(list(cmd))
            if cmd[0] == "env":
                path_entry = next(value for value in cmd if value.startswith("PATH="))
                wrapper_dir = path_entry[len("PATH="):].split(":", 1)[0]
                scp_wrapper_contents.append(
                    (Path(wrapper_dir) / "scp").read_text(encoding="utf-8")
                )
            return 0

        with tempfile.TemporaryDirectory() as temp_dir:
            deployed = orch.deploy_mqtt_publisher_if_enabled(
                runner,
                mqtt_config=orch.MqttConfig(
                    enable=True,
                    broker_host="mqtt.internal",
                    broker_port=2883,
                    broker_user="gardena",
                    broker_password="secret-password",
                    topic_prefix="garden",
                    ha_prefix="ha",
                ),
                gateway_host="192.0.2.10",
                private_key_path="/tmp/key",
                mqtt_publisher_dir=self._publisher_dir(temp_dir),
                scripts_dir=str(Path(temp_dir) / "missing-scripts"),
            )

        self.assertTrue(deployed)
        self.assertEqual(len(calls), 2)
        install_cmd = calls[0]
        self.assertIn("MQTT_BROKER_HOST=mqtt.internal", install_cmd)
        self.assertIn("MQTT_BROKER_PORT=2883", install_cmd)
        self.assertIn("MQTT_TOPIC_PREFIX=garden", install_cmd)
        self.assertIn("MQTT_HA_PREFIX=ha", install_cmd)
        self.assertEqual(len(scp_wrapper_contents), 1)
        self.assertIn(' -O "$@"', scp_wrapper_contents[0])
        self.assertEqual(calls[1][0], "ssh")
        self.assertIn("systemctl is-active --quiet gardena-mqtt-publisher.service", calls[1][-1])

    def test_service_that_dies_returns_actionable_error(self):
        results = iter((0, 3))

        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaisesRegex(
                orch.OrchestrationError,
                r"journalctl -u gardena-mqtt-publisher\.service",
            ):
                orch.deploy_mqtt_publisher_if_enabled(
                    lambda _cmd: next(results),
                    mqtt_config=orch.MqttConfig(
                        enable=True,
                        broker_host="mqtt.internal",
                    ),
                    gateway_host="192.0.2.10",
                    private_key_path="/tmp/key",
                    mqtt_publisher_dir=self._publisher_dir(temp_dir),
                    scripts_dir=str(Path(temp_dir) / "missing-scripts"),
                )

    def test_missing_broker_never_falls_back_to_gateway(self):
        calls = []

        with self.assertRaisesRegex(orch.OrchestrationError, "mqtt_broker_host"):
            orch.deploy_mqtt_publisher_if_enabled(
                lambda cmd: calls.append(list(cmd)) or 0,
                mqtt_config=orch.MqttConfig(enable=True, broker_host=""),
                gateway_host="192.0.2.10",
                private_key_path="/tmp/key",
                mqtt_publisher_dir="/does/not/matter",
            )

        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
