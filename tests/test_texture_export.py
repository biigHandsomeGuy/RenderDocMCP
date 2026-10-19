"""Native export tests with fake RenderDoc bindings and real temporary files."""

import base64
import importlib
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch


class TextureExportTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1] / "renderdoc_extension"
        package = ModuleType("_export_tests")
        package.__path__ = [str(root)]
        services = ModuleType("_export_tests.services")
        services.__path__ = [str(root / "services")]
        rd = ModuleType("renderdoc")
        rd.ResourceId = SimpleNamespace(Null=lambda: 0)
        rd.FileType = SimpleNamespace(PNG="PNG", JPG="JPG", HDR="HDR", DDS="DDS")
        rd.TextureSave = lambda: SimpleNamespace(
            slice=SimpleNamespace(), sample=SimpleNamespace(), comp=SimpleNamespace())
        modules = patch.dict(sys.modules, {
            "renderdoc": rd, "_export_tests": package,
            "_export_tests.services": services,
        })
        modules.start()
        self.addCleanup(modules.stop)
        module = importlib.import_module("_export_tests.services.texture_export_service")
        self.module = module
        self.ctx = Mock()
        self.ctx.IsCaptureLoaded.return_value = True
        self.ctx.CurEvent.return_value = 10
        self.controller = Mock()
        self.texture = SimpleNamespace(resourceId=123, width=2048, height=1024,
                                       depth=1, arraysize=1, mips=4, msSamp=1)
        self.controller.GetTextures.return_value = [self.texture]
        self.controller.GetRootActions.return_value = [SimpleNamespace(
            eventId=20, events=[SimpleNamespace(eventId=19)], children=[])]
        self.controller.GetPipelineState.return_value.GetOutputTargets.return_value = [
            SimpleNamespace(resource=123)]
        self.payload = b"test image bytes"
        self.controller.SaveTexture.side_effect = self.save
        self.service = module.TextureExportService(
            self.ctx, lambda callback: callback(self.controller))
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.path = str(self.directory / "image.png")

    def save(self, config, path):
        Path(path).write_bytes(self.payload)
        return SimpleNamespace(OK=lambda: True)

    def export(self, **kwargs):
        params = {"resource_id": "ResourceId::123", "output_path": self.path}
        params.update(kwargs)
        return self.service.export_texture(**params)

    def test_export_metadata_native_settings_and_restore(self):
        info = self.export(event_id=20, mip=1, channel=2, black_point=-1, white_point=2)
        self.assertEqual(Path(self.path).read_bytes(), self.payload)
        self.assertEqual(info["width"], 1024)
        self.assertEqual(info["size_bytes"], len(self.payload))
        self.assertEqual(info["event_id"], 20)
        save = self.controller.SaveTexture.call_args.args[0]
        self.assertEqual(save.destType, "PNG")
        self.assertEqual(save.mip, 1)
        self.assertEqual(save.channelExtract, 2)
        self.assertEqual(save.comp.blackPoint, -1)
        self.assertEqual(save.sample.sampleIndex, 0)
        self.assertFalse(save.sample.mapToArray)
        self.assertEqual(self.controller.SetFrameEvent.call_args_list, [call(20, True), call(10, True)])
        self.assertEqual(list(self.directory.iterdir()), [Path(self.path)])

    def test_default_selects_ui_event(self):
        self.assertEqual(self.export()["event_id"], 10)
        self.controller.SetFrameEvent.assert_has_calls([call(10, True), call(10, True)])

    def test_formats_and_legacy_boolean_status(self):
        for format in ("png", "jpg", "hdr", "dds"):
            with self.subTest(format=format):
                path = str(self.directory / ("result." + format))
                self.controller.SaveTexture.side_effect = lambda config, path: (
                    Path(path).write_bytes(b"data") and True)
                self.assertEqual(self.export(format=format, output_path=path)["format"], format)

    def test_no_overwrite_and_explicit_overwrite(self):
        Path(self.path).write_bytes(b"original")
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.export()
        self.assertEqual(Path(self.path).read_bytes(), b"original")
        self.export(overwrite=True)
        self.assertEqual(Path(self.path).read_bytes(), self.payload)

    def test_invalid_arguments(self):
        cases = [
            {"mip": -1}, {"mip": 4}, {"mip": True}, {"mip": "1"},
            {"slice": 1}, {"sample": 1}, {"channel": 4}, {"channel": True},
            {"black_point": 1, "white_point": 1}, {"white_point": float("inf")},
            {"black_point": "bad"}, {"format": "bmp"}, {"overwrite": "yes"},
            {"output_path": "relative.png"}, {"output_path": str(self.directory / "bad.jpg")},
            {"output_path": str(self.directory / "missing" / "image.png")},
            {"resource_id": "invalid"}, {"resource_id": "999"},
            {"event_id": -1}, {"event_id": 999},
        ]
        for params in cases:
            with self.subTest(params=params), self.assertRaises(ValueError):
                self.export(**params)
        self.controller.SaveTexture.assert_not_called()

    def test_no_capture(self):
        self.ctx.IsCaptureLoaded.return_value = False
        with self.assertRaisesRegex(ValueError, "No capture"):
            self.export()
        self.controller.SetFrameEvent.assert_not_called()

    def test_failed_save_preserves_destination_and_cleans_temporary(self):
        Path(self.path).write_bytes(b"original")
        self.controller.SaveTexture.side_effect = lambda config, path: False
        with self.assertRaisesRegex(ValueError, "failed to save"):
            self.export(event_id=20, overwrite=True)
        self.assertEqual(Path(self.path).read_bytes(), b"original")
        self.assertEqual(list(self.directory.iterdir()), [Path(self.path)])
        self.controller.SetFrameEvent.assert_has_calls([call(20, True), call(10, True)])

    def test_empty_save_and_exception(self):
        for effect in (lambda config, path: True, RuntimeError("encoder error")):
            self.controller.SaveTexture.side_effect = effect
            with self.assertRaises(ValueError):
                self.export(event_id=20)
            self.assertFalse(Path(self.path).exists())
            self.assertEqual(list(self.directory.iterdir()), [])
            self.assertEqual(self.controller.SetFrameEvent.call_args, call(10, True))

    def test_concurrent_destination_creation_not_overwritten(self):
        def save(config, path):
            Path(self.path).write_bytes(b"concurrent")
            return self.save(config, path)
        self.controller.SaveTexture.side_effect = save
        with self.assertRaises(ValueError):
            self.export()
        self.assertEqual(Path(self.path).read_bytes(), b"concurrent")

    def test_cube_arraysize_already_includes_faces(self):
        self.texture.arraysize = 6
        self.export(slice=5)
        self.assertEqual(self.controller.SaveTexture.call_args.args[0].slice.sliceIndex, 5)
        with self.assertRaisesRegex(ValueError, "slice"):
            self.export(slice=6, overwrite=True)

    def test_3d_slice_and_msaa_validation(self):
        self.texture.depth = 8
        self.texture.msSamp = 4
        self.export(mip=1, slice=3, sample=3)
        with self.assertRaisesRegex(ValueError, "slice"):
            self.export(mip=1, slice=4, overwrite=True)

    def test_export_output_and_unbound_slot(self):
        result = self.service.export_event_output(20, self.path)
        self.assertEqual(result["target_index"], 0)
        self.assertEqual(result["resource_id"], "123")
        for index in (-1, 1):
            with self.assertRaises(ValueError):
                self.service.export_event_output(20, self.path, target_index=index)
        self.controller.GetPipelineState.return_value.GetOutputTargets.return_value = [
            SimpleNamespace(resource=0)]
        with self.assertRaisesRegex(ValueError, "No texture bound"):
            self.service.export_event_output(20, self.path)
        self.assertEqual(self.controller.SetFrameEvent.call_args, call(10, True))

    def test_api_event_inside_action_is_accepted(self):
        self.export(event_id=19)

    def test_preview_selects_mip_and_cleans_file(self):
        result = self.service.preview_texture("123", event_id=20, max_dimension=512)
        self.assertEqual(result["mip"], 2)
        self.assertEqual(result["width"], 512)
        self.assertEqual(result["mime_type"], "image/png")
        self.assertEqual(base64.b64decode(result["content_base64"]), self.payload)
        temporary = self.controller.SaveTexture.call_args.args[1]
        self.assertFalse(Path(temporary).parent.exists())
        self.assertNotIn("output_path", result)

    def test_preview_bounds_and_missing_small_mip(self):
        for limit in (0, 4097, True, 1):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                self.service.preview_texture("123", max_dimension=limit)
        with self.assertRaises(ValueError):
            self.service.preview_texture("123", mip=99)

    def test_preview_size_limit_cleans_files(self):
        self.service.MAX_PREVIEW_BYTES = 2
        with self.assertRaisesRegex(ValueError, "4 MiB"):
            self.service.preview_texture("123")
        self.assertFalse(Path(self.controller.SaveTexture.call_args.args[1]).parent.exists())

    def test_request_routes_and_validation(self):
        handler_module = importlib.import_module("_export_tests.request_handler")
        facade = Mock()
        handler = handler_module.RequestHandler(facade)
        for method, params in (
            ("export_texture", {"resource_id": "123", "output_path": self.path}),
            ("preview_texture", {"resource_id": "123"}),
            ("export_event_output", {"event_id": 20, "output_path": self.path}),
        ):
            with self.subTest(method=method):
                response = handler.handle({"id": "1", "method": method, "params": params})
                self.assertIn("result", response)
                getattr(facade, method).assert_called_once_with(**params)
                bad = handler.handle({"id": "2", "method": method, "params": {}})
                self.assertEqual(bad["error"]["code"], -32602)
                unknown = handler.handle({"id": "3", "method": method,
                                          "params": dict(params, unexpected=True)})
                self.assertEqual(unknown["error"]["code"], -32602)


if __name__ == "__main__":
    unittest.main()