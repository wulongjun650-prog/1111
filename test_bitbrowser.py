from __future__ import annotations

import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

from . import bitbrowser


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:  # noqa: N802
        size = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(size)
        server = self.server
        assert isinstance(server, _Server)
        server.seen.append(
            {
                "path": self.path,
                "key": self.headers.get("x-api-key"),
                "host": self.headers.get("Host"),
            }
        )
        required = server.required_key
        if required and self.headers.get("x-api-key") != required:
            body = json.dumps({"success": False, "msg": "x-api-key invalid"}).encode()
            status = 401
        else:
            body = json.dumps({"success": True, "data": {}}).encode()
            status = 200
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


class _Server(ThreadingHTTPServer):
    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.seen: list[dict[str, str | None]] = []
        self.required_key: str | None = None


class BitBrowserApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = TemporaryDirectory()
        self._root = Path(self._temp.name)
        self._saved_env = {
            key: os.environ.get(key)
            for key in ("APPDATA", "LOCALAPPDATA", "BITBROWSER_API_BASE", "BITBROWSER_API_KEY", "BITBROWSER_CONFIG")
        }
        os.environ["APPDATA"] = str(self._root / "appdata")
        os.environ["LOCALAPPDATA"] = str(self._root / "local")
        os.environ.pop("BITBROWSER_API_BASE", None)
        os.environ.pop("BITBROWSER_API_KEY", None)
        os.environ.pop("BITBROWSER_CONFIG", None)
        bitbrowser._clear_working_base()
        self.server = _Server()
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self._thread.join(timeout=3)
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        bitbrowser._clear_working_base()
        self._temp.cleanup()

    def _write_config(self, payload: dict[str, object]) -> None:
        path = self._root / "config.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        os.environ["BITBROWSER_CONFIG"] = str(path)

    def test_config_port_is_used_when_default_port_is_closed(self) -> None:
        self._write_config({"localServerAddress": f"127.0.0.1:{self.port}"})
        bitbrowser.health(timeout=2.0)
        self.assertTrue(self.server.seen)
        self.assertEqual(self.server.seen[0]["path"], "/health")
        self.assertEqual(self.server.seen[0]["host"], f"127.0.0.1:{self.port}")

    def test_saved_custom_address_wins_over_a_stale_config_port(self) -> None:
        self._write_config({"localServerAddress": "127.0.0.1:1"})
        bitbrowser.save_local_api(f"http://127.0.0.1:{self.port}", "")
        bitbrowser.health(timeout=2.0)
        self.assertEqual(self.server.seen[0]["host"], f"127.0.0.1:{self.port}")

    def test_auth_header_is_sent_and_missing_token_is_explained(self) -> None:
        self.server.required_key = "token-715"
        self._write_config({"localServerAddress": f"http://127.0.0.1:{self.port}"})
        with self.assertRaises(RuntimeError) as missing:
            bitbrowser.health(timeout=2.0)
        self.assertIn("鉴权", str(missing.exception))
        self.assertNotIn("token-715", str(missing.exception))
        bitbrowser._clear_working_base()
        os.environ["BITBROWSER_API_KEY"] = "token-715"
        bitbrowser.health(timeout=2.0)
        self.assertEqual(self.server.seen[-1]["key"], "token-715")

    def test_config_token_is_used_when_the_user_has_not_saved_one(self) -> None:
        self.server.required_key = "from-config"
        self._write_config(
            {
                "localApi": {"port": self.port, "token": "from-config"},
            }
        )
        bitbrowser.health(timeout=2.0)
        self.assertEqual(self.server.seen[-1]["key"], "from-config")

    def test_closed_port_mentions_the_7_1_5_setting(self) -> None:
        self._write_config({"localServerAddress": "127.0.0.1:1"})
        with self.assertRaises(RuntimeError) as caught:
            bitbrowser.health(timeout=1.0)
        message = str(caught.exception)
        self.assertIn("Local API", message)
        self.assertIn("7.1.5", message)
        self.assertIn("127.0.0.1:1", message)

    def test_candidates_try_ipv4_then_ipv6_loopback(self) -> None:
        os.environ["BITBROWSER_API_BASE"] = "http://127.0.0.1:54345"
        self.assertEqual(
            bitbrowser.api_base_candidates(),
            [
                "http://127.0.0.1:54345",
                "http://[::1]:54345",
                "http://localhost:54345",
            ],
        )

    def test_public_address_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            bitbrowser.save_local_api("http://203.0.113.8:54345", "secret")

    def test_saved_token_roundtrip_does_not_echo_into_errors(self) -> None:
        bitbrowser.save_local_api(f"http://127.0.0.1:{self.port}", "saved-token")
        self.assertEqual(bitbrowser.load_api_token(), "saved-token")
        self.server.required_key = "other"
        with self.assertRaises(RuntimeError) as caught:
            bitbrowser.health(timeout=2.0)
        self.assertNotIn("saved-token", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
