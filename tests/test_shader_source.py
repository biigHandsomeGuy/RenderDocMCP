"""Shader source tests using only the standard library and a fake RenderDoc API."""

import importlib
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch


class ShaderSourceTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1] / "renderdoc_extension"
        package = ModuleType("_shader_source_tests")
        package.__path__ = [str(root)]
        services = ModuleType("_shader_source_tests.services")
        services.__path__ = [str(root / "services")]
        rd = ModuleType("renderdoc")
        rd.ResourceId = SimpleNamespace(Null=lambda: 0)
        rd.ShaderStage = SimpleNamespace(
            Vertex=1, Hull=2, Domain=3, Geometry=4, Pixel=5, Compute=6
        )
        modules = patch.dict(sys.modules, {
            "renderdoc": rd,
            "_shader_source_tests": package,
            "_shader_source_tests.services": services,
        })
        modules.start()
        self.addCleanup(modules.stop)
        module = importlib.import_module(
            "_shader_source_tests.services.pipeline_service"
        )
        self.ctx = Mock()
        self.ctx.IsCaptureLoaded.return_value = True
        self.controller = Mock()
        self.pipe = self.controller.GetPipelineState.return_value
        self.pipe.GetShader.return_value = 123
        self.pipe.GetShaderEntryPoint.return_value = "main"
        self.controller.GetDisassemblyTargets.return_value = ["default"]
        self.controller.DisassembleShader.return_value = "shader assembly"
        self.service = module.PipelineService(
            self.ctx, lambda callback: callback(self.controller)
        )

    def reflection(self, files, **metadata):
        debug = SimpleNamespace(files=files, encoding="HLSL", **metadata)
        return SimpleNamespace(
            debugInfo=debug, constantBlocks=[],
            readOnlyResources=[], readWriteResources=[]
        )

    def test_multiple_source_files_preserve_contents_and_order(self):
        files = [
            SimpleNamespace(filename="main.hlsl", contents='#include "common.hlsl"\n'),
            SimpleNamespace(filename="common.hlsl", contents="// 中文\nfloat value;\n"),
        ]
        source = self.service._get_shader_source(self.reflection(files))
        self.assertTrue(source["available"])
        self.assertEqual(source["encoding"], "HLSL")
        self.assertEqual(source["files"], [vars(f) for f in files])
        self.assertNotIn("reason", source)
        self.assertEqual(json.loads(json.dumps(source)), source)

    def test_source_does_not_require_source_debugging_support(self):
        reflection = self.reflection(
            [SimpleNamespace(filename="main.hlsl", contents="source")],
            sourceDebugInformation=False,
        )
        self.assertTrue(self.service._get_shader_source(reflection)["available"])

    def test_missing_reflection(self):
        source = self.service._get_shader_source(None)
        self.assertFalse(source["available"])
        self.assertEqual(source["files"], [])
        self.assertIn("reflection", source["reason"])

    def test_missing_debug_info(self):
        for reflection in (SimpleNamespace(), SimpleNamespace(debugInfo=None)):
            with self.subTest(reflection=reflection):
                source = self.service._get_shader_source(reflection)
                self.assertFalse(source["available"])
                self.assertIn("debug information", source["reason"])

    def test_missing_optional_debug_fields(self):
        source = self.service._get_shader_source(
            SimpleNamespace(debugInfo=SimpleNamespace())
        )
        self.assertIsNone(source["encoding"])
        self.assertEqual(source["files"], [])
        self.assertFalse(source["available"])

    def test_empty_files_do_not_claim_source_is_available(self):
        for files in ([], None, [SimpleNamespace(filename="main.hlsl", contents="")]):
            with self.subTest(files=files):
                source = self.service._get_shader_source(self.reflection(files))
                self.assertFalse(source["available"])
                self.assertIn("No embedded", source["reason"])

    def test_get_shader_info_preserves_existing_fields(self):
        self.pipe.GetShaderReflection.return_value = self.reflection([
            SimpleNamespace(filename="main.hlsl", contents="source")
        ])
        info = self.service.get_shader_info(100, "pixel")
        self.assertEqual(info["resource_id"], "123")
        self.assertEqual(info["entry_point"], "main")
        self.assertEqual(info["stage"], "pixel")
        self.assertEqual(info["disassembly"], "shader assembly")
        self.assertEqual(info["constant_buffers"], [])
        self.assertEqual(info["resources"], [])
        self.assertTrue(info["source"]["available"])
        self.controller.SetFrameEvent.assert_called_once_with(100, True)

    def test_disassembly_survives_missing_source(self):
        self.pipe.GetShaderReflection.return_value = None
        info = self.service.get_shader_info(100, "pixel")
        self.assertFalse(info["source"]["available"])
        self.assertEqual(info["disassembly"], "shader assembly")

    def test_source_survives_disassembly_error(self):
        self.pipe.GetShaderReflection.return_value = self.reflection([
            SimpleNamespace(filename="main.hlsl", contents="source")
        ])
        self.controller.DisassembleShader.side_effect = RuntimeError("unsupported")
        info = self.service.get_shader_info(100, "pixel")
        self.assertTrue(info["source"]["available"])
        self.assertEqual(info["disassembly_error"], "unsupported")

    def test_no_capture(self):
        self.ctx.IsCaptureLoaded.return_value = False
        with self.assertRaisesRegex(ValueError, "No capture loaded"):
            self.service.get_shader_info(100, "pixel")

    def test_unbound_shader(self):
        self.pipe.GetShader.return_value = 0
        with self.assertRaisesRegex(ValueError, "No pixel shader bound"):
            self.service.get_shader_info(100, "pixel")


if __name__ == "__main__":
    unittest.main()