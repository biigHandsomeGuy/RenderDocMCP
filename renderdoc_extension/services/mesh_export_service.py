"""Export decoded vertex shader outputs using only the standard library."""

import csv
import os
import shutil
import struct
import tempfile

import renderdoc as rd

from .texture_export_service import TextureExportService


class MeshExportService:
    """Fetch post-VS buffers on the replay thread and save draw-order CSV rows."""

    def __init__(self, ctx, invoke_fn):
        self._replay = TextureExportService(ctx, invoke_fn)

    @staticmethod
    def _action(controller, event_id):
        pending = list(controller.GetRootActions())
        while pending:
            action = pending.pop()
            if action.eventId == event_id:
                if not action.flags & rd.ActionFlags.Drawcall:
                    raise ValueError("event_id must identify a draw call")
                return action
            pending.extend(action.children)
        raise ValueError("Draw call not found: %d" % event_id)

    @staticmethod
    def _attributes(pipe, stride):
        shader = pipe.GetShaderReflection(rd.ShaderStage.Vertex)
        if shader is None:
            raise ValueError("No vertex shader reflection available")
        signatures = [sig for sig in shader.outputSignature if sig.stream == 0]
        # RenderDoc stores the position first, independent of signature order.
        signatures.sort(key=lambda sig: sig.systemValue != rd.ShaderBuiltin.Position)
        aligned = pipe.HasAlignedPostVSData(rd.MeshDataStage.VSOut)
        attributes = []
        offset = 0
        used_names = set()
        for sig in signatures:
            # Post-VS capture promotes sub-32-bit outputs to 32-bit components.
            width = max(4, rd.VarTypeByteSize(sig.varType))
            kind = rd.VarTypeCompType(sig.varType)
            chars = {
                rd.CompType.Float: {2: "e", 4: "f", 8: "d"},
                rd.CompType.SInt: {1: "b", 2: "h", 4: "i", 8: "q"},
                rd.CompType.UInt: {1: "B", 2: "H", 4: "I", 8: "Q"},
            }
            code = chars.get(kind, {}).get(width)
            if code is None or not 1 <= sig.compCount <= 4:
                raise ValueError("Unsupported VS output type: %s" % sig.varType)
            slot = max(4, width)
            alignment = slot * (4 if sig.compCount > 2 else sig.compCount)
            if aligned and sig.compCount > 1:
                offset = (offset + alignment - 1) // alignment * alignment
            if offset + slot * sig.compCount > stride:
                raise ValueError("VS output signature exceeds vertex stride")
            name = sig.varName or sig.semanticIdxName or "attribute"
            original = name
            suffix = 2
            while name in used_names:
                name = "%s_%d" % (original, suffix)
                suffix += 1
            used_names.add(name)
            attributes.append({"name": name, "offset": offset,
                               "components": sig.compCount, "type": str(sig.varType),
                               "width": width, "slot": slot, "code": code})
            offset += slot * sig.compCount
        if not attributes:
            raise ValueError("No vertex shader outputs available")
        return attributes

    @staticmethod
    def _indices(controller, pipe, mesh):
        if mesh.indexResourceId == rd.ResourceId.Null():
            return range(mesh.numIndices), None
        width = mesh.indexByteStride
        if width not in (1, 2, 4):
            raise ValueError("Unsupported post-VS index stride")
        size = mesh.numIndices * width
        if getattr(mesh, "indexByteSize", 0) and size > mesh.indexByteSize:
            raise ValueError("Post-VS index buffer is truncated")
        data = controller.GetBufferData(mesh.indexResourceId, mesh.indexByteOffset, size)
        if len(data) != size:
            raise ValueError("Post-VS index buffer is truncated")
        indices = [item[0] for item in struct.iter_unpack(
            "<" + {1: "B", 2: "H", 4: "I"}[width], data)]
        restart = (pipe.GetRestartIndex() & ((1 << (width * 8)) - 1)
                   if pipe.IsRestartEnabled() else None)
        return indices, restart

    def export_vs_output(self, event_id, output_path, instance=0, view=0, overwrite=False):
        for name, value, minimum in (("event_id", event_id, 1), ("instance", instance, 0),
                                     ("view", view, 0)):
            self._replay._integer(name, value, minimum)
        if not isinstance(output_path, str) or not os.path.isabs(output_path):
            raise ValueError("output_path must be an absolute file path")
        path = os.path.abspath(output_path)
        if os.path.splitext(path)[1].lower() != ".csv":
            raise ValueError("output_path must have a .csv extension")
        if not isinstance(overwrite, bool):
            raise ValueError("overwrite must be a boolean")
        if os.path.lexists(path) and not overwrite:
            raise ValueError("Output already exists; set overwrite=True to replace it")
        parent = os.path.dirname(path)
        if not os.path.isdir(parent):
            raise ValueError("Output directory does not exist: %s" % parent)

        def operation(controller):
            action = self._action(controller, event_id)
            if instance >= action.numInstances:
                raise ValueError("instance is out of range")
            pipe = controller.GetPipelineState()
            if view >= max(1, pipe.MultiviewBroadcastCount()):
                raise ValueError("view is out of range")
            mesh = controller.GetPostVSData(instance, view, rd.MeshDataStage.VSOut)
            if mesh.vertexResourceId == rd.ResourceId.Null() or mesh.numIndices == 0:
                raise ValueError("VS output unavailable: %s" % getattr(mesh, "status", "no data"))
            if mesh.vertexByteStride <= 0:
                raise ValueError("Invalid post-VS vertex stride")
            attributes = self._attributes(pipe, mesh.vertexByteStride)
            indices, restart = self._indices(controller, pipe, mesh)
            indexed = mesh.indexResourceId != rd.ResourceId.Null()
            base = mesh.baseVertex if indexed else 0
            last = max((index + base for index in indices if index != restart), default=-1)
            size = (last + 1) * mesh.vertexByteStride
            if getattr(mesh, "vertexByteSize", 0) and size > mesh.vertexByteSize:
                raise ValueError("Post-VS vertex buffer is truncated")
            data = (controller.GetBufferData(mesh.vertexResourceId, mesh.vertexByteOffset, size)
                    if size else b"")
            if len(data) != size:
                raise ValueError("Post-VS vertex buffer is truncated")
            columns = ["VTX", "IDX", "RESTART"]
            for attr in attributes:
                columns.extend("%s.%s" % (attr["name"], "xyzw"[component])
                               for component in range(attr["components"]))
            fd, temporary = tempfile.mkstemp(prefix=".renderdoc-mesh-", suffix=".csv", dir=parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="") as output:
                    writer = csv.writer(output)
                    writer.writerow(columns)
                    for row, index in enumerate(indices):
                        if index == restart:
                            writer.writerow([row, index, True] + [""] * (len(columns) - 3))
                            continue
                        vertex = index + base
                        if vertex < 0:
                            raise ValueError("Negative post-VS vertex index")
                        values = [row, vertex, False]
                        for attr in attributes:
                            start = vertex * mesh.vertexByteStride + attr["offset"]
                            values.extend(struct.unpack_from("<" + attr["code"], data,
                                          start + component * attr["slot"])[0]
                                          for component in range(attr["components"]))
                        writer.writerow(values)
                if overwrite:
                    os.replace(temporary, path)
                else:
                    with open(temporary, "rb") as source, open(path, "xb") as destination:
                        try:
                            shutil.copyfileobj(source, destination)
                        except Exception:
                            destination.close()
                            os.remove(path)
                            raise
                return {"output_path": path, "size_bytes": os.path.getsize(path),
                        "format": "csv", "event_id": event_id, "stage": "VSOut",
                        "instance": instance, "view": view, "row_count": mesh.numIndices,
                        "topology": str(mesh.topology), "columns": columns,
                        "vertex_byte_stride": mesh.vertexByteStride}
            finally:
                if os.path.exists(temporary):
                    os.remove(temporary)

        return self._replay._run(event_id, operation)