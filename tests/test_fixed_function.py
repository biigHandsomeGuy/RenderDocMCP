"""Fixed-function adapters tested without RenderDoc or third-party dependencies."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace as NS
import unittest


path = Path(__file__).resolve().parents[1] / "renderdoc_extension/services/fixed_function.py"
spec = importlib.util.spec_from_file_location("fixed_function_tests", path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class FixedFunctionTests(unittest.TestCase):
    def setUp(self):
        self.face = NS(function="Less", failOperation="Keep", depthFailOperation="Replace",
                       passOperation="Increment", reference=7, compareMask=0x7f, writeMask=0x3f)
        equation = NS(source="SrcAlpha", destination="InvSrcAlpha", operation="Add")
        self.blend = NS(blends=[NS(enabled=False, writeMask=5, colorBlend=equation,
                                  alphaBlend=equation), NS(enabled=True, writeMask=15,
                                                          colorBlend=equation, alphaBlend=equation)],
                        blendFactor=[0.1, 0.2, 0.3, 0.4], alphaToCoverage=True,
                        sampleMask=0xfffffffe)
        self.ds = NS(depthEnable=True, depthWrites=False, depthFunction="LessEqual",
                     stencilEnable=True, frontFace=self.face, backFace=self.face)
        self.rs = NS(cullMode="Back", fillMode="Solid", frontCCW=True, depthBias=2,
                     depthBiasClamp=1.5, slopeScaledDepthBias=3.5, depthClip=True,
                     scissorEnable=False, multisampleEnable=True)
        self.region = NS(x=1, y=2, width=320, height=240, enabled=True)
        self.native = NS(outputMerger=NS(blendState=self.blend, depthStencilState=self.ds),
                         rasterizer=NS(state=self.rs, scissors=[self.region, self.region], sampleMask=123))
        self.controller = NS(GetTextures=lambda: [NS(resourceId=10, msSamp=4)],
                             GetD3D11PipelineState=lambda: self.native,
                             GetD3D12PipelineState=lambda: self.native,
                             GetGLPipelineState=lambda: self.native,
                             GetVulkanPipelineState=lambda: self.native)
        self.pipe = NS(GetOutputTargets=lambda: [NS(resource=10)],
                       GetDepthTarget=lambda: NS(resource=0))

    def read(self, api):
        result = module.get_fixed_function_state(self.controller, self.pipe, api)
        self.assertEqual(json.loads(json.dumps(result)), result)
        return result

    def test_d3d11_all_groups(self):
        result = self.read("GraphicsAPI.D3D11")
        self.assertFalse(result["blend_state"]["attachments"][0]["enabled"])
        self.assertEqual(result["blend_state"]["attachments"][1]["index"], 1)
        self.assertEqual(result["blend_state"]["attachments"][0]["rgb"]["source_factor"], "SrcAlpha")
        self.assertEqual(result["blend_state"]["blend_constant"], [0.1, 0.2, 0.3, 0.4])
        self.assertFalse(result["depth_state"]["write_enabled"])
        self.assertEqual(result["stencil_state"]["back"]["reference"], 7)
        self.assertEqual(result["stencil_state"]["front"]["read_mask"], 0x7f)
        self.assertEqual(result["rasterizer_state"]["depth_bias"], 2)
        self.assertEqual(len(result["scissors"]), 2)
        self.assertFalse(result["scissors"][0]["enabled"])
        self.assertEqual(result["multisample_state"]["sample_count"], 4)
        self.assertEqual(result["multisample_state"]["sample_mask"], 0xfffffffe)
        self.assertIsNone(result["multisample_state"]["alpha_to_one"])

    def test_d3d12_mask_and_unknown_switches(self):
        result = self.read("D3D12")
        self.assertEqual(result["multisample_state"]["sample_mask"], 123)
        self.assertIsNone(result["multisample_state"]["multisample_enabled"])
        self.assertIsNone(result["scissors"][0]["enabled"])

    def test_opengl_independent_scissor_and_sample_controls(self):
        self.native.framebuffer = NS(blendState=self.blend)
        self.native.depthState = self.ds
        self.native.stencilState = self.ds
        self.rs.sampleMask = False
        self.rs.sampleMaskValue = 5
        self.rs.alphaToCoverage = False
        self.rs.alphaToOne = True
        self.rs.sampleShading = True
        self.rs.minSampleShadingRate = 0.25
        self.rs.offsetClamp = 7.0
        self.native.rasterizer.scissors.append(NS(x=0, y=0, width=1, height=1, enabled=False))
        result = self.read("OpenGL")
        self.assertTrue(result["scissors"][0]["enabled"])
        self.assertFalse(result["scissors"][2]["enabled"])
        self.assertEqual(result["multisample_state"]["sample_mask"], 5)
        self.assertFalse(result["multisample_state"]["sample_mask_enabled"])
        self.assertEqual(result["multisample_state"]["min_sample_shading"], 0.25)
        self.assertTrue(result["multisample_state"]["alpha_to_one"])
        self.assertEqual(result["rasterizer_state"]["depth_bias_clamp"], 7.0)

    def test_vulkan_native_sample_count(self):
        self.native.colorBlend = self.blend
        self.blend.alphaToCoverageEnable = True
        self.blend.alphaToOneEnable = False
        self.native.depthStencil = self.ds
        self.ds.depthTestEnable = True
        self.ds.depthWriteEnable = False
        self.ds.stencilTestEnable = True
        self.native.rasterizer = self.rs
        self.rs.depthClipEnable = False
        self.native.viewportScissor = NS(viewportScissors=[NS(scissor=self.region)])
        self.native.multisample = NS(rasterSamples=8, sampleMask=0x55,
                                     sampleShadingEnable=True, minSampleShading=0.5)
        result = self.read("Vulkan")
        self.assertEqual(result["multisample_state"]["sample_count"], 8)
        self.assertEqual(result["multisample_state"]["sample_count_source"], "rasterization_state")
        self.assertTrue(result["depth_state"]["test_enabled"])
        self.assertFalse(result["rasterizer_state"]["depth_clip_enabled"])

    def test_partial_state_does_not_hide_other_groups(self):
        del self.native.outputMerger.blendState
        result = self.read("D3D11")
        self.assertIn("blend_state_error", result)
        self.assertTrue(result["depth_state"]["test_enabled"])

    def test_missing_members_are_null_not_false(self):
        self.native.outputMerger.depthStencilState = NS()
        result = self.read("D3D11")
        self.assertIsNone(result["depth_state"]["test_enabled"])
        self.assertIsNone(result["stencil_state"]["front"]["reference"])

    def test_unknown_api_and_unavailable_native_state(self):
        self.assertIn("fixed_function_error", self.read("Unknown"))
        self.native = None
        self.assertIn("fixed_function_error", self.read("D3D11"))

    def test_no_attachments_and_mismatched_counts(self):
        self.controller.GetTextures = lambda: []
        self.assertIsNone(self.read("D3D11")["multisample_state"]["sample_count"])
        self.controller.GetTextures = lambda: [NS(resourceId=10, msSamp=4), NS(resourceId=0, msSamp=8)]
        self.assertIsNone(self.read("D3D11")["multisample_state"]["sample_count"])


if __name__ == "__main__":
    unittest.main()