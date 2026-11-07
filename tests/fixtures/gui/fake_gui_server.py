from __future__ import annotations

import json
import os
import signal
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path


running = True


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, _format: str, *_args: object) -> None:
        return


def stop(_signum: int, _frame: object) -> None:
    global running
    running = False


server = HTTPServer(("127.0.0.1", 0), Handler)
server.timeout = 0.1
signal.signal(signal.SIGTERM, stop)
url = f"http://127.0.0.1:{server.server_port}"
Path(os.environ["HYPRIAL_GUI_LAUNCH_INFO_FILE"]).write_text(
    json.dumps({"schema": "hyprial.gui-launch/v1", "url": url}),
    encoding="utf-8",
)
while running:
    server.handle_request()
server.server_close()
