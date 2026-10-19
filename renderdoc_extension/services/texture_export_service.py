"""Native texture export without third-party image dependencies."""

import base64
import math
import os
import shutil
import tempfile

import renderdoc as rd

from ..utils import Parsers


class TextureExportService:
    """Save textures on the replay thread; keep binary previews bounded."""

    FORMATS = {"png": "PNG", "jpg": "JPG", "hdr": "HDR", "dds": "DDS"}
    MAX_PREVIEW_BYTES = 4 * 1024 * 1024

    def __init__(self, ctx, invoke_fn):
        self.ctx = ctx
        self._invoke = invoke_fn

    @staticmethod
    def _integer(name, value, minimum=0):
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise ValueError("%s must be an integer >= %d" % (name, minimum))

    @staticmethod
    def _validate_event(controller, event_id):
        pending = list(controller.GetRootActions())
        while pending:
            action = pending.pop()
            if action.eventId == event_id:
                return
            if any(event.eventId == event_id for event in action.events):
                return
            pending.extend(action.children)
        raise ValueError("Event not found: %d" % event_id)

    def _run(self, event_id, operation):
        if not self.ctx.IsCaptureLoaded():
            raise ValueError("No capture loaded")
        if event_id is not None:
            self._integer("event_id", event_id, 1)
        previous_event = self.ctx.CurEvent()
        result = {}

        def callback(controller):
            try:
                if event_id is not None:
                    self._validate_event(controller, event_id)
                # Existing inspection tools may have moved the replay controller
                # independently of the UI. Explicitly select our baseline event.
                controller.SetFrameEvent(
                    event_id if event_id is not None else previous_event, True
                )
                try:
                    result["value"] = operation(controller)
                finally:
                    controller.SetFrameEvent(previous_event, True)
            except Exception as exc:
                result["error"] = str(exc)

        self._invoke(callback)
        if "error" in result:
            raise ValueError(result["error"])
        return result["value"]

    @staticmethod
    def _texture(controller, resource_id):
        try:
            target = Parsers.extract_numeric_id(resource_id)
        except (TypeError, ValueError, AttributeError):
            raise ValueError("Invalid resource ID: %s" % resource_id)
        for texture in controller.GetTextures():
            if Parsers.extract_numeric_id(str(texture.resourceId)) == target:
                return texture
        raise ValueError("Texture not found: %s" % resource_id)

    def _save(self, controller, texture, output_path, format, mip, slice,
              sample, overwrite, channel, black_point, white_point):
        if format not in self.FORMATS:
            raise ValueError("format must be png, jpg, hdr, or dds")
        if not isinstance(output_path, str) or not os.path.isabs(output_path):
            raise ValueError("output_path must be an absolute file path")
        path = os.path.abspath(output_path)
        extension = os.path.splitext(path)[1].lower()
        allowed = (".jpg", ".jpeg") if format == "jpg" else ("." + format,)
        if extension not in allowed:
            raise ValueError("File extension does not match format")
        if not isinstance(overwrite, bool):
            raise ValueError("overwrite must be a boolean")
        if os.path.lexists(path) and not overwrite:
            raise ValueError("Output already exists; set overwrite=True to replace it")
        parent = os.path.dirname(path)
        if not os.path.isdir(parent):
            raise ValueError("Output directory does not exist: %s" % parent)
        for name, value in (("mip", mip), ("slice", slice), ("sample", sample)):
            self._integer(name, value)
        if mip >= texture.mips:
            raise ValueError("Invalid mip level")
        # RenderDoc arraysize already includes all six faces of each cube.
        slices = max(1, texture.depth >> mip) if texture.depth > 1 else texture.arraysize
        if slice >= slices:
            raise ValueError("Invalid array/cube/depth slice")
        if sample >= max(1, texture.msSamp):
            raise ValueError("Invalid MSAA sample")
        if isinstance(channel, bool) or not isinstance(channel, int) or channel not in (-1, 0, 1, 2, 3):
            raise ValueError("channel must be -1 (RGBA) or 0..3 (grayscale)")
        try:
            valid_range = (math.isfinite(black_point) and math.isfinite(white_point)
                           and black_point < white_point)
        except TypeError:
            valid_range = False
        if not valid_range:
            raise ValueError("black_point and white_point must be finite and increasing")

        save = rd.TextureSave()
        save.resourceId = texture.resourceId
        save.destType = getattr(rd.FileType, self.FORMATS[format])
        save.mip = mip
        save.slice.sliceIndex = slice
        save.sample.sampleIndex = sample
        save.sample.mapToArray = False
        save.channelExtract = channel
        save.comp.blackPoint = black_point
        save.comp.whitePoint = white_point

        fd, temporary = tempfile.mkstemp(prefix=".renderdoc-export-", suffix=extension, dir=parent)
        os.close(fd)
        try:
            status = controller.SaveTexture(save, temporary)
            # Older bindings return bool; current bindings return ResultDetails.
            ok = status if isinstance(status, bool) else status.OK()
            if not ok:
                raise ValueError("RenderDoc failed to save texture: %s" % status)
            size = os.path.getsize(temporary)
            if size == 0:
                raise ValueError("RenderDoc produced an empty image")
            if overwrite:
                os.replace(temporary, path)
            else:
                # Exclusive creation also protects against a destination appearing
                # after validation. Remove only a file created by this operation.
                with open(temporary, "rb") as source:
                    with open(path, "xb") as destination:
                        try:
                            shutil.copyfileobj(source, destination)
                        except Exception:
                            destination.close()
                            os.remove(path)
                            raise
            return {
                "output_path": path, "size_bytes": size, "format": format,
                "resource_id": str(texture.resourceId),
                "width": max(1, texture.width >> mip),
                "height": max(1, texture.height >> mip),
                "mip": mip, "slice": slice, "sample": sample,
                "channel": channel, "black_point": black_point, "white_point": white_point,
            }
        finally:
            if os.path.exists(temporary):
                os.remove(temporary)

    def export_texture(self, resource_id, output_path, event_id=None, format="png",
                       mip=0, slice=0, sample=0, overwrite=False, channel=-1,
                       black_point=0.0, white_point=1.0):
        def operation(controller):
            texture = self._texture(controller, resource_id)
            return self._save(controller, texture, output_path, format, mip, slice,
                              sample, overwrite, channel, black_point, white_point)
        result = self._run(event_id, operation)
        result["event_id"] = event_id if event_id is not None else self.ctx.CurEvent()
        return result

    def export_event_output(self, event_id, output_path, target_index=0, format="png",
                            mip=0, slice=0, sample=0, overwrite=False, channel=-1,
                            black_point=0.0, white_point=1.0):
        self._integer("target_index", target_index)

        def operation(controller):
            targets = controller.GetPipelineState().GetOutputTargets()
            if target_index >= len(targets):
                raise ValueError("Color output target index is out of range")
            rid = targets[target_index].resource
            if rid == rd.ResourceId.Null():
                raise ValueError("No texture bound at the selected color output")
            texture = self._texture(controller, str(rid))
            return self._save(controller, texture, output_path, format, mip, slice,
                              sample, overwrite, channel, black_point, white_point)
        result = self._run(event_id, operation)
        result.update(event_id=event_id, target_index=target_index)
        return result

    def preview_texture(self, resource_id, event_id=None, mip=0, slice=0, sample=0,
                        max_dimension=1024, channel=-1, black_point=0.0, white_point=1.0):
        self._integer("max_dimension", max_dimension, 1)
        if max_dimension > 4096:
            raise ValueError("max_dimension cannot exceed 4096")
        self._integer("mip", mip)

        def operation(controller):
            texture = self._texture(controller, resource_id)
            if mip >= texture.mips:
                raise ValueError("Invalid mip level")
            selected = mip
            while (selected + 1 < texture.mips and
                   max(texture.width >> selected, texture.height >> selected) > max_dimension):
                selected += 1
            if max(texture.width >> selected, texture.height >> selected) > max_dimension:
                raise ValueError("No mip fits max_dimension; use export_texture or a larger limit")
            with tempfile.TemporaryDirectory(prefix="renderdoc-preview-") as directory:
                result = self._save(controller, texture, os.path.join(directory, "preview.png"),
                                    "png", selected, slice, sample, False,
                                    channel, black_point, white_point)
                if result["size_bytes"] > self.MAX_PREVIEW_BYTES:
                    raise ValueError("Preview exceeds 4 MiB; select a smaller mip")
                with open(result.pop("output_path"), "rb") as image:
                    result["content_base64"] = base64.b64encode(image.read()).decode("ascii")
                result["mime_type"] = "image/png"
                return result
        return self._run(event_id, operation)