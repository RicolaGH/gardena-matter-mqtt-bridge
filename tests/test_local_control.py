import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_DIR = Path(__file__).resolve().parents[1]
ADDON_DIR = REPO_DIR / "gardena_matter_bridge"
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


class LocalControlApiTests(unittest.TestCase):
    def test_official_websocket_api_is_enabled_with_session(self):
        calls = []

        def http(**kwargs):
            calls.append(kwargs)
            return orch.HttpResponse(status=204, body="")

        gateway = orch.GatewayClient(host="192.0.2.10", http=http)
        gateway.session = "session-secret"
        gateway.set_websocket_enabled(True)

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["method"], "PUT")
        self.assertEqual(
            calls[0]["url"],
            "https://192.0.2.10/websocket_api",
        )
        self.assertEqual(calls[0]["json_body"], {"enable": True})
        self.assertEqual(calls[0]["headers"]["X-Session"], "session-secret")

    def test_http_failure_is_actionable_and_does_not_expose_session(self):
        gateway = orch.GatewayClient(
            host="192.0.2.10",
            http=lambda **_kwargs: orch.HttpResponse(status=500, body="secret"),
            session="session-secret",
        )

        with self.assertRaisesRegex(
            orch.OrchestrationError,
            r"Lokale GARDENA-Steuerung.*HTTP 500",
        ) as raised:
            gateway.set_websocket_enabled(True)

        self.assertNotIn("session-secret", str(raised.exception))

    def test_addon_option_reaches_deploy_plan(self):
        env = {
            "GARDENA_GATEWAY_HOST": "192.0.2.10",
            "GARDENA_DEVICE_ID": "secret-device-id",
            "GARDENA_GITHUB_REPO": "owner/repo",
            "GARDENA_RELEASE_TAG": "v1.0.0",
            "GARDENA_ENABLE_LOCAL_CONTROL": "true",
        }
        with patch.dict(os.environ, env, clear=True):
            cfg = web_ui._read_env_config()
            plan = web_ui.build_deploy_plan(cfg, read_text=lock_reader)

        self.assertTrue(plan.enable_local_control)

    def test_native_control_rejects_disabling_ssh(self):
        env = {
            "GARDENA_GATEWAY_HOST": "192.0.2.10",
            "GARDENA_DEVICE_ID": "secret-device-id",
            "GARDENA_GITHUB_REPO": "owner/repo",
            "GARDENA_RELEASE_TAG": "v1.0.0",
            "GARDENA_ENABLE_LOCAL_CONTROL": "true",
            "GARDENA_ENABLE_MQTT": "true",
            "GARDENA_MQTT_BROKER_HOST": "192.0.2.20",
            "GARDENA_DISABLE_SSH_AFTER": "true",
        }
        with patch.dict(os.environ, env, clear=True):
            cfg = web_ui._read_env_config()
            with self.assertRaisesRegex(
                orch.OrchestrationError,
                "SSH-Verbindung dauerhaft",
            ):
                web_ui.build_deploy_plan(cfg, read_text=lock_reader)

    def test_existing_ssh_path_logs_in_before_enabling_local_control(self):
        calls = []

        class FakeGateway:
            session = None

            def login(self, password):
                self.session = "session-secret"
                calls.append(("login", password))

            def set_websocket_enabled(self, enable):
                calls.append(("websocket", enable, self.session))

            def install_public_key(self, _key):
                raise AssertionError("SSH setup must be skipped")

            def set_ssh_enabled(self, _enable):
                raise AssertionError("SSH setup must be skipped")

        plan = orch.DeployPlan(
            gateway_host="192.0.2.10",
            private_key_path="/tmp/private-key",
            public_key_path="/tmp/public-key",
            repo="owner/repo",
            tag="v1.0.0",
            expected_sha256=LOCK_SHA256,
            enable_local_control=True,
        )
        artifact = orch.ReleaseArtifact(path="/tmp/release.tar.gz", sha256=LOCK_SHA256)
        bundle = orch.UnpackedBundle(
            binary_path="/tmp/chip-bridge-app",
            libs_tgz_path="/tmp/libs.tar.gz",
            version="v1.0.0",
            web_ui_dir="/tmp/web-ui",
        )

        with (
            patch.object(orch, "fetch_and_verify_release", return_value=artifact),
            patch.object(orch, "unpack_bundle", return_value=bundle),
            patch.object(orch, "deploy_via_ssh", return_value=list(plan.scripts)),
        ):
            result = orch.run_full_deploy(
                plan,
                gateway=FakeGateway(),
                device_id="12345678-rest-of-id",
                read_public_key=lambda _path: "ssh-ed25519 test",
                downloader=lambda *_args: artifact,
                read_bytes=lambda _path: b"unused",
                ssh_runner=lambda _cmd: 0,
            )

        self.assertEqual(
            calls,
            [
                ("login", "12345678"),
                ("websocket", True, "session-secret"),
            ],
        )
        self.assertTrue(result.local_control_enabled)
        self.assertIn("login_local_control", result.steps)
        self.assertIn("enable_local_control", result.steps)


class GatewayCompatibilityRegressionTests(unittest.TestCase):
    def test_launcher_does_not_use_busybox_incompatible_head_minus_one(self):
        source = (ADDON_DIR / "install-scripts" / "install_bridge.sh").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("head -1", source)
        self.assertIn("sed -n '1p'", source)

    def test_missing_supervisor_token_is_safe_under_set_u(self):
        source = (ADDON_DIR / "run.sh").read_text(encoding="utf-8")
        self.assertIn("${SUPERVISOR_TOKEN:-}", source)


if __name__ == "__main__":
    unittest.main()
