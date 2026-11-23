#!/usr/bin/env python3
"""
VideoOps-MEP 本地可视化投屏演示服务端（纯 Python 3 标准库，零依赖）
启动后访问 http://127.0.0.1:8799
"""

from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "engine"))
from mep_gateway import MEPGateway  # noqa: E402

GATEWAY = MEPGateway(ROOT / "workspace")
WEB_INDEX = ROOT / "web" / "index.html"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args) -> None:
        return

    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path in ("/", "/index.html"):
            html = WEB_INDEX.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)
            return
        if self.path == "/api/state":
            state = GATEWAY.load_state()
            req_files = sorted(p.name for p in GATEWAY.requests_dir.glob("*.json"))[-8:]
            res_files = sorted(p.name for p in GATEWAY.results_dir.glob("*.json"))[-8:]
            self._send_json({"state": state, "request_files": req_files, "result_files": res_files})
            return
        self.send_error(404)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:
            self._send_json({"ok": False, "reason": "Invalid JSON"}, 400)
            return

        if self.path == "/api/submit":
            result = GATEWAY.process_request(body)
            self._send_json({"result": result, "state": GATEWAY.load_state()})
            return

        if self.path == "/api/reset":
            state = GATEWAY.reset_demo()
            self._send_json({"ok": True, "state": state})
            return

        self.send_error(404)


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8799
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"🎬 VideoOps-MEP 演示台已启动：http://127.0.0.1:{port}")
    print(f"📂 真实协议目录：{ROOT / 'workspace'}")
    server.serve_forever()


if __name__ == "__main__":
    main()
