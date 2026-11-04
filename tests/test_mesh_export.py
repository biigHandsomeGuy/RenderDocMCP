"""Post-VS export tests with fake bindings and real CSV files."""

import csv
import importlib
from pathlib import Path
import struct
import sys
import tempfile
from types import ModuleType, SimpleNamespace as NS
import unittest
from unittest.mock import Mock, call, patch


class MeshExportTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1] / "renderdoc_extension"
        package = ModuleType("_mesh_tests")
        package.__path__ = [str(root)]
        services = ModuleType("_mesh_tests.services")
        services.__path__ = [str(root / "services")]
        rd = ModuleType("renderdoc")
        rd.ResourceId = NS(Null=lambda: 0)
        rd.ActionFlags = NS(Drawcall=1)
        rd.ShaderStage = NS(Vertex="vertex")
        rd.MeshDataStage = NS(VSOut="VSOut")
        rd.ShaderBuiltin = NS(Position="position")
        rd.CompType = NS(Float="float", SInt="sint", UInt="uint")
        rd.VarTypeByteSize = lambda kind: {"half": 2, "double": 8}.get(kind, 4)
        rd.VarTypeCompType = lambda kind: {"int": "sint", "uint": "uint"}.get(kind, "float")
        modules = patch.dict(sys.modules, {"renderdoc": rd, "_mesh_tests": package,
                                          "_mesh_tests.services": services})
        modules.start()
        self.addCleanup(modules.stop)
        module = importlib.import_module("_mesh_tests.services.mesh_export_service")
        self.ctx = Mock()
        self.ctx.IsCaptureLoaded.return_value = True
        self.ctx.CurEvent.return_value = 10
        self.controller = Mock()
        self.action = NS(eventId=20, flags=1, numInstances=3, events=[], children=[])
        self.controller.GetRootActions.return_value = [self.action]
        self.pipe = self.controller.GetPipelineState.return_value
        self.pipe.MultiviewBroadcastCount.return_value = 2
        self.pipe.HasAlignedPostVSData.return_value = False
        self.pipe.IsRestartEnabled.return_value = False
        self.signatures = [self.sig("uv", "float", 2), self.sig("position", "float", 4, "position")]
        self.pipe.GetShaderReflection.return_value = NS(outputSignature=self.signatures)
        self.mesh = NS(vertexResourceId=100, vertexByteOffset=16, vertexByteStride=24,
                       vertexByteSize=48, indexResourceId=200, indexByteOffset=8,
                       indexByteStride=2, indexByteSize=6, baseVertex=0, numIndices=3,
                       topology="TriangleList", status="")
        self.controller.GetPostVSData.return_value = self.mesh
        self.vertices = struct.pack("<12f", 1, 2, 3, 4, .25, .5, 5, 6, 7, 8, .75, 1)
        self.indices = struct.pack("<3H", 1, 0, 1)
        self.controller.GetBufferData.side_effect = self.buffer
        self.service = module.MeshExportService(self.ctx, lambda fn: fn(self.controller))
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.path = str(self.directory / "vs.csv")

    @staticmethod
    def sig(name, kind, count, builtin=None):
        return NS(varName=name, semanticIdxName="", varType=kind, compCount=count,
                  systemValue=builtin, stream=0)

    def buffer(self, resource, offset, size):
        return (self.vertices if resource == 100 else self.indices)[:size]

    def export(self, **params):
        args = {"event_id": 20, "output_path": self.path}
        args.update(params)
        return self.service.export_vs_output(**args)

    def rows(self):
        with open(self.path, encoding="utf-8", newline="") as source:
            return list(csv.reader(source))

    def test_indexed_draw_order_all_attributes_and_restore(self):
        result = self.export(instance=2, view=1)
        rows = self.rows()
        self.assertEqual(rows[0], ["VTX", "IDX", "RESTART", "position.x", "position.y",
                                   "position.z", "position.w", "uv.x", "uv.y"])
        self.assertEqual([row[1] for row in rows[1:]], ["1", "0", "1"])
        self.assertEqual(rows[1][3:], ["5.0", "6.0", "7.0", "8.0", "0.75", "1.0"])
        self.assertEqual(result["row_count"], 3)
        self.assertEqual(result["size_bytes"], Path(self.path).stat().st_size)
        self.controller.GetPostVSData.assert_called_once_with(2, 1, "VSOut")
        self.controller.SetFrameEvent.assert_has_calls([call(20, True), call(10, True)])
        self.controller.GetBufferData.assert_any_call(100, 16, 48)

    def test_nonindexed_and_optional_size_fields(self):
        self.mesh.indexResourceId = 0
        self.mesh.numIndices = 2
        self.mesh.vertexByteSize = 0
        self.export()
        self.assertEqual([row[1] for row in self.rows()[1:]], ["0", "1"])

    def test_restart_and_base_vertex(self):
        self.mesh.baseVertex = -1
        self.indices = struct.pack("<3H", 2, 65535, 1)
        self.pipe.IsRestartEnabled.return_value = True
        self.pipe.GetRestartIndex.return_value = 0xffffffff
        self.export()
        rows = self.rows()
        self.assertEqual(rows[2], ["1", "65535", "True"] + [""] * 6)
        self.assertEqual(rows[3][1], "0")

    def test_alignment_and_promoted_half_integer_double(self):
        self.signatures[:] = [self.sig("half", "half", 1), self.sig("pair", "int", 2),
                              self.sig("double", "double", 1)]
        self.pipe.HasAlignedPostVSData.return_value = True
        self.mesh.indexResourceId = 0
        self.mesh.numIndices = 1
        self.vertices = struct.pack("<f4xiid", 1.5, -2, 3, 4.25)
        self.export()
        self.assertEqual(self.rows()[1][3:], ["1.5", "-2", "3", "4.25"])

    def test_argument_validation(self):
        for args in ({"instance": -1}, {"view": True}, {"event_id": 0},
                     {"output_path": "relative.csv"}, {"output_path": self.path + ".txt"},
                     {"overwrite": 1}, {"output_path": str(self.directory / "missing" / "vs.csv")}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.export(**args)
        self.controller.GetPostVSData.assert_not_called()

    def test_invalid_selection_and_missing_data(self):
        for args in ({"instance": 3}, {"view": 2}, {"event_id": 99}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.export(**args)
        self.mesh.vertexResourceId = 0
        with self.assertRaisesRegex(ValueError, "unavailable"):
            self.export()
        self.assertEqual(self.controller.SetFrameEvent.call_args, call(10, True))

    def test_failure_preserves_file_cleans_temp_and_restores(self):
        Path(self.path).write_bytes(b"original")
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.export()
        self.indices = struct.pack("<3H", 1, 65535, 0)
        self.mesh.baseVertex = -1
        self.pipe.IsRestartEnabled.return_value = True
        self.pipe.GetRestartIndex.return_value = 65535
        with self.assertRaisesRegex(ValueError, "Negative"):
            self.export(overwrite=True)
        self.assertEqual(Path(self.path).read_bytes(), b"original")
        self.assertEqual(list(self.directory.iterdir()), [Path(self.path)])
        self.assertEqual(self.controller.SetFrameEvent.call_args, call(10, True))

    def test_truncated_buffer_and_no_capture(self):
        self.vertices = b"bad"
        with self.assertRaisesRegex(ValueError, "truncated"):
            self.export()
        self.ctx.IsCaptureLoaded.return_value = False
        with self.assertRaisesRegex(ValueError, "No capture"):
            self.export()

    def test_route_validation(self):
        handler_module = importlib.import_module("_mesh_tests.request_handler")
        facade = Mock()
        handler = handler_module.RequestHandler(facade)
        params = {"event_id": 20, "output_path": self.path}
        response = handler.handle({"id": "1", "method": "export_vs_output", "params": params})
        self.assertIn("result", response)
        facade.export_vs_output.assert_called_once_with(**params)
        for bad in ({}, dict(params, unknown=True)):
            result = handler.handle({"id": "2", "method": "export_vs_output", "params": bad})
            self.assertEqual(result["error"]["code"], -32602)


if __name__ == "__main__":
    unittest.main()