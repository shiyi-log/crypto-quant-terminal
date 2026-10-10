"""Direct API logging is safe to enable at the next normal bot start."""

from datetime import datetime
import json
import logging
from pathlib import Path
import subprocess
import tempfile
import unittest

from bot.api_access_logging import (
    AccessFieldsFilter,
    AccessJsonFormatter,
    UvicornRedactionFilter,
)
import manage


ROOT = Path(__file__).resolve().parents[1]
OVERLAY = ROOT / "bot/config_api_access_log.json"


class AccessLoggingTests(unittest.TestCase):
    def record(self, args):
        return logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1,
                                 '%s - "%s %s HTTP/%s" %d', args, None)

    def test_http_access_uses_only_allowlisted_fields(self):
        record = self.record(("127.0.0.1:1234", "POST",
                              "/api/v1/forceexit?token=do-not-store", "1.1", 200))
        record.headers = {"Authorization": "Bearer also-do-not-store"}
        self.assertTrue(AccessFieldsFilter().filter(record))
        rendered = AccessJsonFormatter().format(record)
        result = json.loads(rendered)
        self.assertEqual(set(result), {"time_utc", "client", "method", "path", "status"})
        self.assertEqual(result["path"], "/api/v1/forceexit")
        self.assertEqual(result["method"], "POST")
        self.assertEqual(result["status"], 200)
        self.assertEqual(result["client"], "127.0.0.1:1234")
        self.assertIsNotNone(datetime.fromisoformat(result["time_utc"]).tzinfo)
        self.assertNotIn("do-not-store", rendered)
        self.assertNotIn("Authorization", rendered)

    def test_unrecognized_access_records_are_dropped(self):
        for args in ((), ("unexpected-secret",), ({"Authorization": "Bearer secret"},),
                     ("127.0.0.1", "invalid\nmethod", "/", "1.1", 200),
                     ("127.0.0.1", "GET", "/", "1.1", "secret")):
            with self.subTest(args=args):
                self.assertFalse(AccessFieldsFilter().filter(self.record(args)))

    def test_ws_accept_and_reject_logs_cannot_leak_query_token(self):
        for message, args in (
            ('%s - "WebSocket %s" [accepted]',
             ("127.0.0.1:1234", "/api/v1/message/ws?token=ws-secret")),
            ('%s - "WebSocket %s" 403',
             ("127.0.0.1:1234", "/api/v1/message/ws?token=ws-secret")),
            ('%s - "WebSocket %s" %d',
             ("127.0.0.1:1234", "/api/v1/message/ws?token=ws-secret", 403)),
        ):
            with self.subTest(message=message):
                record = logging.LogRecord("uvicorn.error", logging.INFO, __file__, 1,
                                           message, args, None)
                self.assertTrue(UvicornRedactionFilter().filter(record))
                self.assertIn("/api/v1/message/ws", record.getMessage())
                self.assertNotIn("ws-secret", record.getMessage())
                self.assertNotIn("?", record.getMessage())

    def test_authorization_is_redacted_in_headers_and_tracebacks(self):
        for message in (
            "Authorization: Bearer header-secret",
            '{"Authorization": "Bearer header-secret"}',
            "[(b'authorization', b'Bearer header-secret')]",
        ):
            with self.subTest(message=message):
                try:
                    raise RuntimeError("URL /api/v1/message/ws?token=trace-secret\n" + message)
                except RuntimeError as exception:
                    record = logging.LogRecord("uvicorn.error", logging.ERROR, __file__, 1,
                                               message, (),
                                               (type(exception), exception, exception.__traceback__))
                self.assertTrue(UvicornRedactionFilter().filter(record))
                rendered = logging.Formatter("%(message)s").format(record)
                self.assertNotIn("header-secret", rendered)
                self.assertNotIn("trace-secret", rendered)
                self.assertIn("[REDACTED]", rendered)
                self.assertIn("RuntimeError", rendered)
                self.assertIsNone(record.exc_info)

    def test_manage_uses_overlay_only_for_trade_api(self):
        commands = manage.services(ROOT)
        self.assertEqual(commands["api"].command[4:8],
                         ("--config", "user_data/config_trend_live.json", "--config",
                          "config_api_access_log.json"))
        self.assertEqual(commands["api"].command[-2:], ("--strategy", "TrendFollowing"))
        for name, service in commands.items():
            if name != "api":
                self.assertNotIn("config_api_access_log.json", service.command)

    def test_overlay_contains_no_trade_settings_or_credentials(self):
        overlay = json.loads(OVERLAY.read_text())
        self.assertEqual(set(overlay), {"api_server", "log_config"})
        self.assertEqual(overlay["api_server"], {"verbosity": "info"})
        self.assertEqual(overlay["log_config"]["root"],
                         {"level": "INFO", "handlers": ["console"]})

    def test_installed_freqtrade_loader_logging_and_uvicorn_keep_overlay(self):
        python = ROOT / "bot/.venv/bin/python"
        if not python.exists():
            self.skipTest("Local Freqtrade environment unavailable")
        # A child interpreter isolates dictConfig from the test runner and imports the
        # very Freqtrade environment manage uses. No exchange, server or bot is started.
        script = '''
from copy import deepcopy
import json
import logging
from pathlib import Path
import sys
import uvicorn
from freqtrade.configuration.load_config import load_from_files
from freqtrade.config_schema.config_schema import CONF_SCHEMA
from freqtrade.loggers import _create_log_config, bufferHandler, setup_logging
from freqtrade.loggers.ft_rich_handler import FtRichHandler
from jsonschema import Draft4Validator

temporary = Path(sys.argv[1])
base = {
    "api_server": {
        "verbosity": "error", "listen_port": 8889, "password": "synthetic",
        "enabled": True, "listen_ip_address": "127.0.0.1", "username": "test",
        "jwt_secret_key": "synthetic-test-secret-longer-than-32-chars",
    },
    "dry_run": True, "stake_amount": 337, "max_open_trades": 10,
    "exchange": {"pair_whitelist": ["ADA/USDT:USDT"]},
    "verbosity": 0, "print_colorized": False,
    "logfile": str(temporary / "ordinary.log"),
}
base_path = temporary / "base.json"
base_path.write_text(json.dumps(base))
config = load_from_files([str(base_path), "config_api_access_log.json"])
expected = deepcopy(base)
expected["api_server"]["verbosity"] = "info"
comparable = {key: value for key, value in config.items()
              if key not in {"log_config", "config_files"}}
assert comparable == expected, "Overlay changed non-logging settings"
Draft4Validator(CONF_SCHEMA["definitions"]["logging"]).validate(config["log_config"])
Draft4Validator(CONF_SCHEMA["properties"]["api_server"]).validate(config["api_server"])
access_path = temporary / "new-bot-logs" / "api-access.log"
assert not access_path.parent.exists()
config["log_config"]["handlers"]["api_access"]["filename"] = str(access_path)
created = _create_log_config(config)
assert access_path.parent.is_dir(), "Freqtrade must create missing handler directories"
assert created["handlers"]["file"]["filename"] == base["logfile"]
assert created["root"]["handlers"] == ["console", "file"]
setup_logging(config)
assert any(isinstance(handler, FtRichHandler) for handler in logging.root.handlers)
assert bufferHandler in logging.root.handlers
logging.getLogger("freqtrade").info("ordinary-root-preserved")

async def app(scope, receive, send):
    pass

uvconfig = uvicorn.Config(app, log_config=None,
                         access_log=config["api_server"]["verbosity"] != "error")
assert uvconfig.access_log is True
logging.getLogger("uvicorn.access").info('%s - "%s %s HTTP/%s" %d',
    "127.0.0.1:1234", "POST", "/api/v1/forceexit?token=access-secret", "1.1", 200)
logging.getLogger("uvicorn.error").info('%s - "WebSocket %s" [accepted]',
    "127.0.0.1:1234", "/api/v1/message/ws?token=ws-secret")
logging.getLogger("uvicorn.error").warning("Authorization: Bearer authorization-secret")
for handler in logging.root.handlers + logging.getLogger("uvicorn.access").handlers:
    handler.flush()
access_text = access_path.read_text()
ordinary_text = (temporary / "ordinary.log").read_text()
entries = [json.loads(line) for line in access_text.splitlines()]
assert len(entries) == 1
assert entries[0]["path"] == "/api/v1/forceexit"
assert entries[0]["status"] == 200
assert "ordinary-root-preserved" in ordinary_text
assert "/api/v1/message/ws" in ordinary_text
for secret in ("access-secret", "ws-secret", "authorization-secret"):
    assert secret not in access_text + ordinary_text
assert not logging.getLogger("uvicorn.access").propagate
print("Installed Freqtrade/Uvicorn integration passed")
'''
        with tempfile.TemporaryDirectory() as temporary:
            result = subprocess.run([str(python), "-c", script, temporary],
                                    cwd=ROOT / "bot", capture_output=True, text=True,
                                    timeout=30, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("integration passed", result.stdout)


if __name__ == "__main__":
    unittest.main()
