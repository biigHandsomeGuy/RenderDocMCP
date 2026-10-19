# RenderDoc MCP Server

Expose RenderDoc capture data to AI assistants through the Model Context Protocol (MCP). Inspect draw calls, shaders, resource bindings, pipeline state, and GPU timings from an MCP-compatible client.

The project consists of a standalone Python MCP server and a RenderDoc UI extension. Capture loading and replay queries run inside RenderDoc, so RenderDoc must be running with the extension enabled.

## Features

- List and open `.rdc` captures and check capture status.
- Inspect the action tree with marker, event ID, and action flag filters.
- Retrieve frame summaries, event details, and GPU timings.
- Find draws and dispatches by shader name, texture name, or resource ID.
- Read embedded shader source when available, alongside disassembly, constant buffer values, and resource bindings.
- Inspect full pipeline state and texture metadata.
- Read raw buffer and texture data as Base64-encoded bytes.
- Export textures and event color outputs to PNG, JPG, HDR, or DDS, and return PNG previews as MCP image content.

## Architecture

```text
AI / MCP client
       | stdio
       v
MCP server process (Python + FastMCP 2.x)
       | File-based IPC in the system temporary directory
       v
RenderDoc extension (QTimer polling)
       v
RequestHandler -> RenderDocFacade -> Services
       | Replay queries through Replay().BlockInvoke
       v
RenderDoc ReplayController
```

The bridge uses files rather than network sockets to accommodate RenderDoc's embedded Python environment:

| File | Purpose |
|------|---------|
| `request.json` | Request from the MCP server |
| `response.json` | Result or error from the extension |
| `lock` | Indicates that the client is writing a request |

The IPC directory is `renderdoc_mcp` under `tempfile.gettempdir()`, typically `%TEMP%\renderdoc_mcp` on Windows. The extension polls every 100 ms, the client checks for responses every 50 ms, and requests time out after 30 seconds.

> `socket_server.py` is a legacy filename: the implementation uses file polling. `RENDERDOC_MCP_HOST` and `RENDERDOC_MCP_PORT` are retained for compatibility and do not control the current IPC transport.

## Requirements

- Python 3.10 or later for the external MCP server.
- RenderDoc 1.20 or later, as declared by the extension manifest. This does not mean every subsequent version has been tested.
- `uv` for the installation and development commands below.
- An MCP client that supports stdio servers.
- A RenderDoc embedded Python environment providing `renderdoc`, `qrenderdoc`, and `PySide2.QtCore`.

The external server depends on `fastmcp>=2.0.0,<3.0.0` and `pydantic>=2.0.0`. The package manager installs these dependencies.

> The original project documented testing on Windows with DirectX 11. Compatibility with other operating systems and graphics APIs has not been verified here.

## Installation and Setup

Clone the repository from https://github.com/biigHandsomeGuy/RenderDocMCP.

### 1. Setup the environment
We need the python and [Node.js](https://nodejs.org/).
```powershell
python -m pip install uv
```
### 2. Install the RenderDoc extension

```powershell
python "scripts\install_extension.py"
```

On Windows, the installer copies the extension to `%APPDATA%\qrenderdoc\extensions\renderdoc_mcp_bridge`. It replaces an existing installation and excludes Python cache files.

### 3. Enable the extension

1. Start or restart RenderDoc.
2. Open **Tools > Manage Extensions**.
3. Enable **RenderDoc MCP Bridge**.


### 4. Install the MCP server

```powershell
cd RenderDocMCP
uv tool install .
uv tool update-shell
```

Reopen your terminal to make `renderdoc-mcp` available on `PATH`. This command starts a stdio MCP server; it is normally launched by your MCP client, not used as an interactive command-line application.

For development, use an editable installation so source changes take effect after restarting the server:

```powershell
uv tool install --editable "RenderDocMCP"
```

> The Python package includes only `mcp_server`; it does not install the RenderDoc extension. After changing extension code, rerun the extension installer and reload the extension.

### 5. Configure your MCP client

The repository's `RenderDocMCP\.mcp.json` contains this configuration. Add the same server definition to your client's MCP settings if needed:

```json
{
  "mcpServers": {
    "renderdoc": {
      "command": "renderdoc-mcp"
    }
  }
}
```

If the client cannot find the command, ensure its process can access the updated `PATH`, or replace `command` with the absolute path to the installed executable.

### 6. Verify the connection

With RenderDoc running, the extension enabled, and your MCP client connected, call `get_capture_status`. A response of `{"loaded": false}` is normal when no capture is open. Then open a capture in RenderDoc or call `open_capture`.

## Available MCP Tools

The server provides 18 tools:

| Tool | Description |
|------|-------------|
| `get_capture_status` | Check capture loading status, filename, and graphics API |
| `list_captures` | List `.rdc` files in a directory without searching subdirectories |
| `open_capture` | Open a capture, replacing the currently loaded capture |
| `get_draw_calls` | Retrieve the action tree with marker, event range, and flag filters |
| `get_frame_summary` | Get action statistics, top-level markers, and resource counts |
| `get_draw_call_details` | Inspect draw parameters, output resources, and other event details |
| `get_action_timings` | Get GPU timings for all or selected events, with optional marker filters |
| `find_draws_by_shader` | Find draws and dispatches by partial shader name or entry point |
| `find_draws_by_texture` | Find SRV, UAV, and render target bindings by partial resource name |
| `find_draws_by_resource` | Match a resource ID in shader, SRV, UAV, render target, and depth target bindings |
| `get_shader_info` | Retrieve embedded source when available, disassembly, constant buffers, and resource bindings |
| `get_buffer_contents` | Read Base64-encoded buffer bytes with optional offset and length |
| `get_texture_info` | Inspect dimensions, format, mip levels, array size, and MSAA metadata |
| `get_texture_data` | Read texture data with mip, array slice, MSAA sample, and 3D depth slice selection |
| `get_pipeline_state` | Inspect shaders, resources, samplers, constant buffers, and output state at an event |
| `export_texture` | Save a texture subresource to PNG, JPG, HDR, or DDS and return file metadata |
| `preview_texture` | Return a bounded PNG preview as MCP image content for visual inspection |
| `export_event_output` | Save a bound color output texture at a specified event |

## Image Export and Preview

Export uses RenderDoc's native encoder, without an additional image library. These examples show MCP tool calls, not standalone Python scripts:

```python
export_texture(
    resource_id="ResourceId::12345",
    output_path="E:\\wangsc\\exports\\texture.png",
    event_id=123,
    format="png",
    mip=0,
    slice=0,
    sample=0,
)

preview_texture(
    resource_id="ResourceId::12345",
    event_id=123,
    max_dimension=1024,
)

export_event_output(
    event_id=123,
    target_index=0,
    output_path="E:\\wangsc\\exports\\event_123.png",
)
```

- **Paths and overwrites:** Use an absolute output path on the RenderDoc host and create its parent directory first. The extension must match `format` (`png`, `jpg`, `hdr`, or `dds`; JPG also accepts `.jpeg`). Files are not overwritten unless `overwrite=True`. Saving uses a temporary file; failed encoding does not replace an existing destination.
- **Event selection:** If `event_id` is omitted, the texture is exported at the UI current event. All three tools return replay to the UI current event after the operation, including save failures. Existing inspection tools may have moved replay independently of the UI.
- **Subresources:** `mip`, `slice`, and `sample` default to zero. `slice` selects an array slice, cube face (X+, X-, Y+, Y-, Z+, Z-), or 3D depth slice at the selected mip. Cube array sizes already count faces. MSAA exports select one sample; automatic resolve and exporting all mips/slices are not exposed by these tools.
- **Color output selection:** `target_index` is a zero-based color attachment slot; depth outputs are not selected by this helper. An unbound or invalid slot produces an error. `mip` and `slice` are absolute texture subresources, not offsets relative to the bound view. Output textures represent their contents after the selected event, not an isolated contribution from that draw.
- **Channels and range:** `channel=-1` exports RGBA; `channel=0..3` extracts one channel as grayscale. `black_point` and `white_point` default to `0.0` and `1.0` and control RenderDoc's mapping for 8-bit output. They must be finite and increasing. PNG/JPG do not preserve HDR precision; use an appropriate native format when precision matters. Export does not reproduce every Texture Viewer display setting.
- **Preview limits:** Preview is PNG image content, not raw texture bytes. Starting at `mip`, it selects an existing lower-resolution mip that fits `max_dimension` (default 1024; allowed 1..4096). It does not resize arbitrarily: a texture without a sufficiently small mip produces an error. PNG payloads larger than 4 MiB are rejected. Temporary preview files are deleted. Actual image display depends on the MCP client.
- **Return values:** File exports return the absolute path, byte size, texture ID, dimensions, event, format, and selected conversion parameters. Preview returns an MCP image content block rather than a file path.

After upgrading, reinstall/reload the RenderDoc extension and update/restart the MCP server so the new tools are registered.

## Tests

Run the standard-library regression suite from your checkout using an absolute path:

```powershell
uv run --project "E:\wangsc\RenderDocMCP" python -m unittest discover -s "E:\wangsc\RenderDocMCP\tests" -v
```

Export service tests use mocked RenderDoc bindings and real temporary files. MCP integration tests exercise tool registration, argument forwarding, and image content serialization with the installed FastMCP dependency. Real GPU replay and format conversion still require validation inside RenderDoc with a compatible capture.

## Project Structure

```text
RenderDocMCP\
|-- mcp_server\                  External MCP server
|   |-- server.py                Tool registration and entry point
|   |-- config.py                Compatibility settings
|   `-- bridge\client.py         File-based IPC client
|-- renderdoc_extension\         RenderDoc UI extension
|   |-- __init__.py              Registration and menu integration
|   |-- extension.json           Extension manifest
|   |-- socket_server.py         File polling server
|   |-- request_handler.py       Request routing and error responses
|   |-- renderdoc_facade.py      Service facade and replay thread invocation
|   |-- services\
|   |   |-- capture_manager.py   Capture management
|   |   |-- action_service.py    Actions, frame summaries, and GPU timings
|   |   |-- search_service.py    Resource binding searches
|   |   |-- resource_service.py  Texture and buffer data
|   |   |-- pipeline_service.py  Pipeline state and shader information
|   |   `-- texture_export_service.py Native image export and bounded previews
|   `-- utils\                  Parsing, serialization, and action tree helpers
|-- scripts\install_extension.py
|-- tests\test_shader_source.py  Shader source regression tests
|-- pyproject.toml               Dependencies, build settings, and entry point
|-- uv.lock                      Dependency lockfile
`-- .mcp.json                    MCP client configuration
```


## Limitations and Troubleshooting

- **Connection failures or timeouts:** Ensure RenderDoc is running, the extension is enabled, and both processes can access the same system temporary directory. Changing port settings does not resolve file-based IPC issues.
- **No capture loaded:** Most analysis tools require an open `.rdc` capture. Capture status and directory listing do not.
- **Avoid concurrent calls:** IPC shares one set of request and response files without reliable multi-client or multi-instance isolation. Use one server/extension pair and make calls sequentially.
- **Large capture searches can be slow:** Binding searches visit individual draw or dispatch events and inspect pipeline state, which can exceed the 30-second timeout.
- **Queries can change the replay event:** Search, shader, and pipeline queries switch the replay event without automatically restoring the previous event.
- **Response sizes are not uniformly capped:** Full action trees, textures, and buffers can produce large responses. Prefer filters and partial reads.
- **Use only in a trusted local environment:** The extension can list directories and open capture files. The IPC layer does not provide authentication.

## License

This project is licensed under the MIT License.
