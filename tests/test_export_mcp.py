"""Exercise the public MCP tools using the installed FastMCP client."""

import asyncio
import base64
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastmcp import Client

from mcp_server.server import mcp


class ExportMCPTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.previous_policy = asyncio.get_event_loop_policy()
        if sys.platform == "win32":
            # In-process tests need no subprocess IOCP support.
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    @classmethod
    def tearDownClass(cls):
        asyncio.set_event_loop_policy(cls.previous_policy)

    async def test_registered_tools_and_schemas(self):
        async with Client(mcp) as client:
            tools = {tool.name: tool for tool in await client.list_tools()}
            self.assertEqual(len(tools), 18)
            self.assertIn("output_path", tools["export_texture"].inputSchema["required"])
            self.assertIn("resource_id", tools["preview_texture"].inputSchema["required"])
            self.assertIn("event_id", tools["export_event_output"].inputSchema["required"])

    async def test_exports_forward_parameters(self):
        async with Client(mcp) as client:
            for name, params in (
                ("export_texture", {"resource_id": "123", "output_path": "E:\\exports\\a.png"}),
                ("export_event_output", {"event_id": 20, "output_path": "E:\\exports\\a.png"}),
            ):
                with self.subTest(name=name), patch("mcp_server.server.bridge.call", return_value={"ok": True}) as bridge:
                    result = await client.call_tool(name, params)
                    self.assertFalse(result.is_error)
                    method, forwarded = bridge.call_args.args
                    self.assertEqual(method, name)
                    for key, value in params.items():
                        self.assertEqual(forwarded[key], value)
                    self.assertFalse(forwarded["overwrite"])
                    self.assertEqual(forwarded["format"], "png")

    async def test_preview_is_image_content(self):
        # Serialization is tested here; the native encoder is mocked separately.
        payload = b"\x89PNG\r\n\x1a\nexample"
        async with Client(mcp) as client:
            with patch("mcp_server.server.bridge.call", return_value={
                "content_base64": base64.b64encode(payload).decode("ascii"),
            }) as bridge:
                result = await client.call_tool("preview_texture", {"resource_id": "123"})
                self.assertFalse(result.is_error)
                self.assertEqual(result.content[0].type, "image")
                self.assertEqual(result.content[0].mimeType, "image/png")
                self.assertEqual(base64.b64decode(result.content[0].data), payload)
                self.assertEqual(bridge.call_args.args[1]["max_dimension"], 1024)

    async def test_preview_rejects_corrupt_and_oversized_payload(self):
        async with Client(mcp) as client:
            for payload in ("!bad!", base64.b64encode(b"x" * (4 * 1024 * 1024 + 1)).decode("ascii")):
                with patch("mcp_server.server.bridge.call", return_value={"content_base64": payload}):
                    result = await client.call_tool("preview_texture", {"resource_id": "123"}, raise_on_error=False)
                    self.assertTrue(result.is_error)


if __name__ == "__main__":
    unittest.main()