"""
RenderDoc Bridge Client
Communicates with the RenderDoc extension via file-based IPC.
"""

import json
import os
import tempfile
import time
import threading
import uuid
from typing import Any


# IPC directory (must match renderdoc_extension/socket_server.py)
IPC_DIR = os.path.join(tempfile.gettempdir(), "renderdoc_mcp")
REQUEST_FILE = os.path.join(IPC_DIR, "request.json")
RESPONSE_FILE = os.path.join(IPC_DIR, "response.json")
LOCK_FILE = os.path.join(IPC_DIR, "lock")
_CALL_LOCK = threading.Lock()


class RenderDocBridgeError(Exception):
    """Error communicating with RenderDoc bridge"""

    pass


class RenderDocBridge:
    """Client for communicating with RenderDoc extension via file-based IPC"""

    def __init__(self, host: str = "127.0.0.1", port: int = 19876):
        # host/port are kept for API compatibility but not used
        self.host = host
        self.port = port
        self.timeout = 30.0  # seconds

    def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """Call a method on the RenderDoc extension"""
        # Multiple tools in this process share the same request/response files.
        with _CALL_LOCK:
            return self._call(method, params)

    def _call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        # Check if IPC directory exists
        if not os.path.exists(IPC_DIR):
            raise RenderDocBridgeError(
                f"Cannot connect to RenderDoc MCP Bridge at {self.host}:{self.port}. "
                "Make sure RenderDoc is running with the MCP Bridge extension loaded."
            )

        request = {
            "id": str(uuid.uuid4()),
            "method": method,
            "params": params or {},
        }

        try:
            # Clean up any stale response file
            if os.path.exists(RESPONSE_FILE):
                os.remove(RESPONSE_FILE)

            # Create lock file to signal we're writing
            payload = json.dumps(request, allow_nan=False)
            with open(LOCK_FILE, "w") as f:
                f.write("lock")
            try:
                with open(REQUEST_FILE, "w", encoding="utf-8") as f:
                    f.write(payload)
            finally:
                os.remove(LOCK_FILE)

            # Wait for response
            start_time = time.monotonic()
            last_read_error = None
            while True:
                if time.monotonic() - start_time > self.timeout:
                    detail = f"; last response read error: {last_read_error}" if last_read_error else ""
                    raise RenderDocBridgeError(f"Request timed out ({method}){detail}")
                if os.path.exists(RESPONSE_FILE):
                    try:
                        with open(RESPONSE_FILE, "r", encoding="utf-8") as f:
                            response = json.load(f)
                    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
                        # Allows a bounded transition from an older extension
                        # which still writes its final response file in place.
                        last_read_error = str(exc)
                        time.sleep(0.05)
                        continue

                    # Clean up response file
                    os.remove(RESPONSE_FILE)

                    if not isinstance(response, dict):
                        raise RenderDocBridgeError("Invalid response: expected a JSON object")
                    if response.get("id") != request["id"]:
                        # A previous timed-out request can finish after ours.
                        last_read_error = "Ignored response with a different request ID"
                        continue

                    if "error" in response:
                        error = response["error"]
                        raise RenderDocBridgeError(f"[{error['code']}] {error['message']}")

                    return response.get("result")

                # Poll interval
                time.sleep(0.05)

        except RenderDocBridgeError:
            raise
        except Exception as e:
            raise RenderDocBridgeError(f"Communication error: {e}")
