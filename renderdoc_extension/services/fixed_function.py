"""JSON-safe fixed-function state adapters for RenderDoc's native pipelines.

Missing or API-inapplicable members remain None, never guessed defaults.
"""


def _fields(obj, mapping):
    result = {}
    for key, member in mapping.items():
        value = getattr(obj, member, None)
        result[key] = value
    return result


def _enum_fields(obj, mapping):
    return {key: str(getattr(obj, member)) if getattr(obj, member, None) is not None
            else None for key, member in mapping.items()}


def _face(face):
    result = _fields(face, {
        "reference": "reference", "read_mask": "compareMask", "write_mask": "writeMask"})
    result.update(_enum_fields(face, {
        "compare_function": "function", "fail_operation": "failOperation",
        "depth_fail_operation": "depthFailOperation", "pass_operation": "passOperation"}))
    return result


def _blend(blend):
    attachments = getattr(blend, "blends", None)
    result = {"attachments": None, "blend_constant": None}
    factor = getattr(blend, "blendFactor", None)
    if factor is not None:
        result["blend_constant"] = list(factor)
    if attachments is not None:
        result["attachments"] = []
        for index, attachment in enumerate(attachments):
            item = {"index": index}
            item.update(_fields(attachment, {"enabled": "enabled", "write_mask": "writeMask"}))
            for key, member in (("rgb", "colorBlend"), ("alpha", "alphaBlend")):
                item[key] = _enum_fields(getattr(attachment, member, None), {
                    "source_factor": "source", "destination_factor": "destination",
                    "operation": "operation"})
            result["attachments"].append(item)
    return result


def get_fixed_function_state(controller, pipe, api):
    """Read only the active API; a failed section cannot hide other state."""
    result = {key: None for key in (
        "blend_state", "depth_state", "stencil_state", "rasterizer_state",
        "scissors", "multisample_state")}
    api_name = str(api).split(".")[-1]
    getter = {"D3D11": "GetD3D11PipelineState", "D3D12": "GetD3D12PipelineState",
              "OpenGL": "GetGLPipelineState", "Vulkan": "GetVulkanPipelineState"}.get(api_name)
    if getter is None:
        result["fixed_function_error"] = "Unsupported pipeline API: %s" % api
        return result
    try:
        native = getattr(controller, getter)()
        if native is None:
            raise ValueError("Native pipeline state is unavailable")
    except Exception as exc:
        result["fixed_function_error"] = str(exc)
        return result

    def section(key, reader):
        try:
            result[key] = reader()
        except Exception as exc:
            result[key + "_error"] = str(exc)

    # Resolve each group independently, including on partially exposed bindings.
    def blend_state():
        if api_name in ("D3D11", "D3D12"):
            return native.outputMerger.blendState
        return native.framebuffer.blendState if api_name == "OpenGL" else native.colorBlend

    def depth_stencil(stencil=False):
        if api_name in ("D3D11", "D3D12"):
            return native.outputMerger.depthStencilState
        if api_name == "OpenGL":
            return native.stencilState if stencil else native.depthState
        return native.depthStencil

    def raster():
        return native.rasterizer if api_name == "Vulkan" else native.rasterizer.state

    def depth():
        obj = depth_stencil()
        value = _fields(obj, {
            "test_enabled": "depthTestEnable" if api_name == "Vulkan" else "depthEnable",
            "write_enabled": "depthWriteEnable" if api_name == "Vulkan" else "depthWrites"})
        value.update(_enum_fields(obj, {"compare_function": "depthFunction"}))
        return value

    def stencil():
        obj = depth_stencil(True)
        value = _fields(obj, {
            "test_enabled": "stencilTestEnable" if api_name == "Vulkan" else "stencilEnable"})
        value.update({"front": _face(getattr(obj, "frontFace", None)),
                      "back": _face(getattr(obj, "backFace", None))})
        return value

    def rasterizer():
        obj = raster()
        value = _enum_fields(obj, {"cull_mode": "cullMode", "fill_mode": "fillMode"})
        value.update(_fields(obj, {
            "front_ccw": "frontCCW", "depth_bias": "depthBias",
            "slope_scaled_depth_bias": "slopeScaledDepthBias",
            "depth_bias_clamp": "offsetClamp" if api_name == "OpenGL" else "depthBiasClamp",
            "depth_clip_enabled": "depthClipEnable" if api_name == "Vulkan" else "depthClip",
            "depth_clamp_enabled": "depthClampEnable" if api_name == "Vulkan" else "depthClamp",
            "depth_bias_enabled": "depthBiasEnable"}))
        return value

    def scissors():
        regions = ([v.scissor for v in native.viewportScissor.viewportScissors]
                   if api_name == "Vulkan" else native.rasterizer.scissors)
        values = []
        for index, region in enumerate(regions):
            value = _fields(region, {"x": "x", "y": "y", "width": "width", "height": "height"})
            value["index"] = index
            # D3D11's enable is global; generic Scissor.enabled is not authoritative.
            value["enabled"] = (getattr(raster(), "scissorEnable", None) if api_name == "D3D11"
                                else getattr(region, "enabled", None) if api_name == "OpenGL" else None)
            values.append(value)
        return values

    def multisample():
        value = {key: None for key in (
            "sample_count", "sample_mask", "alpha_to_coverage", "multisample_enabled",
            "alpha_to_one", "sample_shading", "min_sample_shading", "sample_mask_enabled")}
        if api_name == "Vulkan":
            value.update(_fields(native.multisample, {
                "sample_count": "rasterSamples", "sample_mask": "sampleMask",
                "sample_shading": "sampleShadingEnable", "min_sample_shading": "minSampleShading"}))
            value.update(_fields(native.colorBlend, {
                "alpha_to_coverage": "alphaToCoverageEnable", "alpha_to_one": "alphaToOneEnable"}))
        elif api_name == "OpenGL":
            value.update(_fields(raster(), {
                "sample_mask": "sampleMaskValue", "sample_mask_enabled": "sampleMask",
                "alpha_to_coverage": "alphaToCoverage", "alpha_to_one": "alphaToOne",
                "multisample_enabled": "multisampleEnable", "sample_shading": "sampleShading",
                "min_sample_shading": "minSampleShadingRate"}))
        else:
            value.update(_fields(blend_state(), {"alpha_to_coverage": "alphaToCoverage"}))
            mask_source = blend_state() if api_name == "D3D11" else native.rasterizer
            value["sample_mask"] = getattr(mask_source, "sampleMask", None)
            if api_name == "D3D11":
                value["multisample_enabled"] = getattr(raster(), "multisampleEnable", None)
        if api_name != "Vulkan":
            # Describe the bound attachments, not a guessed rasterization sample count.
            value["sample_count_source"] = "bound_attachments"
            try:
                outputs = list(pipe.GetOutputTargets()) + [pipe.GetDepthTarget()]
                ids = {o.resource for o in outputs}
                counts = {t.msSamp for t in controller.GetTextures() if t.resourceId in ids}
                if len(counts) == 1:
                    value["sample_count"] = counts.pop()
            except Exception as exc:
                value["sample_count_error"] = str(exc)
        else:
            value["sample_count_source"] = "rasterization_state"
        return value

    section("blend_state", lambda: _blend(blend_state()))
    section("depth_state", depth)
    section("stencil_state", stencil)
    section("rasterizer_state", rasterizer)
    section("scissors", scissors)
    section("multisample_state", multisample)
    return result