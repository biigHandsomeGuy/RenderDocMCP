"""Regression coverage for partially written and unserializable IPC responses."""

import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from types import ModuleType
import unittest
from unittest.mock import patch

from mcp_server.bridge import client


class TransportTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.paths = {
            "IPC_DIR": str(self.root),
            "REQUEST_FILE": str(self.root / "request.json"),
            "RESPONSE_FILE": str(self.root / "response.json"),
            "LOCK_FILE": str(self.root / "lock"),
        }
        patches = patch.multiple(client, **self.paths)
        patches.start()
        self.addCleanup(patches.stop)
        qt = ModuleType("PySide2.QtCore")
        qt.QObject = object
        qt.QTimer = object
        with patch.dict(sys.modules, {"PySide2": ModuleType("PySide2"), "PySide2.QtCore": qt}):
            path = Path(__file__).resolve().parents[1] / "renderdoc_extension/socket_server.py"
            spec = importlib.util.spec_from_file_location("_ipc_server_test", path)
            self.server = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.server)
        for key, value in self.paths.items():
            setattr(self.server, key, value)

    def test_atomic_response_and_serialization_error(self):
        publish = self.server.MCPBridgeServer._publish_response
        publish({"id": "one", "result": {"text": "hello"}})
        self.assertEqual(json.loads(Path(self.paths["RESPONSE_FILE"]).read_text())["result"], {"text": "hello"})
        for invalid in (object(), float("nan")):
            publish({"id": "two", "result": invalid})
            response = json.loads(Path(self.paths["RESPONSE_FILE"]).read_text())
            self.assertEqual(response["id"], "two")
            self.assertEqual(response["error"]["code"], -32603)
        self.assertFalse(list(self.root.glob("*.tmp")))

    def test_partial_json_and_mismatched_id_are_retried(self):
        steps = iter(("partial", "stale", "correct"))

        def respond(delay):
            request = json.loads(Path(self.paths["REQUEST_FILE"]).read_text())
            step = next(steps)
            if step == "partial":
                Path(self.paths["RESPONSE_FILE"]).write_text("")
            else:
                self.server.MCPBridgeServer._publish_response({
                    "id": "old-request" if step == "stale" else request["id"],
                    "result": {"ok": True},
                })

        with patch.object(client.time, "sleep", side_effect=respond):
            self.assertEqual(client.RenderDocBridge().call("get_pipeline_state"), {"ok": True})

    def test_invalid_json_times_out_with_context(self):
        ticks = iter((0, 0, 0.1, 2))
        bridge = client.RenderDocBridge()
        bridge.timeout = 1
        with patch.object(client.time, "monotonic", side_effect=lambda: next(ticks)), patch.object(
            client.time, "sleep", side_effect=lambda _: Path(self.paths["RESPONSE_FILE"]).write_text("{")):
            with self.assertRaisesRegex(client.RenderDocBridgeError, "last response read error"):
                bridge.call("get_pipeline_state")

    def test_request_encoding_failure_leaves_no_lock(self):
        with self.assertRaises(client.RenderDocBridgeError):
            client.RenderDocBridge().call("bad", {"bad": object()})
        self.assertFalse(Path(self.paths["LOCK_FILE"]).exists())


if __name__ == "__main__":
    unittest.main()