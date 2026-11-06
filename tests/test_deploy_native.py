"""Native deployment preserves configuration and keeps access credentials private."""

import io
import json
import stat
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from nanobot.config.schema import Config
from scripts import deploy_native


def deployment_payload():
    return {
        "preset_name": "primary",
        "preset": {"provider": "openai", "model": "test-model"},
        "provider": "openai",
        "provider_config": {"apiKey": "test-provider-key"},
        "timezone": "America/Mexico_City",
        "web": {"enable": True, "search": {"provider": "brave", "apiKey": "test-search-key"}},
        "address": "debian.example.ts.net:8443",
    }


class NativeDeploymentTests(unittest.TestCase):
    def test_configuration_transfers_model_search_and_timezone_and_preserves_auth(self):
        existing = {"channels": {"websocket": {"tokenIssueSecret": "existing-webui-secret"}}}
        config = deploy_native.deployment_config(
            existing, deployment_payload(), Path("/srv/axis/workspace"), "debian.example.ts.net:8443",
        )
        parsed = Config.model_validate(config)

        self.assertEqual(parsed.agents.defaults.model_preset, "primary")
        self.assertEqual(parsed.agents.defaults.timezone, "America/Mexico_City")
        self.assertEqual(parsed.agents.defaults.timezone_mode, "manual")
        self.assertEqual(parsed.tools.web.search.provider, "brave")
        self.assertEqual(parsed.tools.web.search.api_key, "test-search-key")
        self.assertFalse(parsed.tools.restrict_to_workspace)
        self.assertEqual(parsed.tools.exec.sandbox, "")
        websocket = config["channels"]["websocket"]
        self.assertEqual(websocket["host"], "127.0.0.1")
        self.assertTrue(websocket["websocketRequiresToken"])
        self.assertEqual(websocket["tokenIssueSecret"], "existing-webui-secret")
        self.assertEqual(websocket["publicWsUrl"], "wss://debian.example.ts.net:8443/")

    def test_activation_writes_private_config_and_access_link_without_overwriting_soul(self):
        with TemporaryDirectory() as temporary:
            home = Path(temporary)
            state = home / ".local" / "share" / "axis"
            workspace = state / "workspace"
            workspace.mkdir(parents=True)
            soul = workspace / "SOUL.md"
            soul.write_text("Existing personality\n")
            output = io.StringIO()

            with (
                patch.object(deploy_native.Path, "home", return_value=home),
                patch.object(deploy_native, "run") as run,
                patch.object(deploy_native, "verify") as verify,
                redirect_stdout(output),
            ):
                deploy_native.activate_server(deployment_payload())

            config_file = state / "config.json"
            access_file = state / "webui-access-url.txt"
            config = json.loads(config_file.read_text())
            secret = config["channels"]["websocket"]["tokenIssueSecret"]
            self.assertEqual(soul.read_text(), "Existing personality\n")
            self.assertEqual(stat.S_IMODE(config_file.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(access_file.stat().st_mode), 0o600)
            self.assertIn(secret, access_file.read_text())
            self.assertNotIn(secret, output.getvalue())
            self.assertNotIn("test-provider-key", output.getvalue())
            unit = home / ".config" / "systemd" / "user" / "axis.service"
            self.assertIn("Environment=TZ=America/Mexico_City", unit.read_text())
            self.assertIn("gateway --foreground --config", unit.read_text())
            run.assert_any_call(["systemctl", "--user", "enable", "--now", "axis.service"])
            self.assertEqual(verify.call_count, 2)


if __name__ == "__main__":
    unittest.main()
