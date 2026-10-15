"""Loopback-only persistence for the local LIRA brainstorm HTML.

Run with --html <file>. Requests are saved beside that file as .requests.json.
This bridge never executes prompts or calls a model; the user hands the saved
requests to the current chat using the page's copy-notification button.
"""
from __future__ import annotations

import argparse
from datetime import datetime
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import tempfile
import threading
from urllib.parse import urlparse
from zoneinfo import ZoneInfo


class BridgeConfig(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.active = False
        self.config = ""
        self.nodes = set()
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "script" and attrs.get("id") == "mindmap-bridge":
            self.active = True
        if tag == "details" and "node" in attrs.get("class", "").split():
            self.nodes.add(attrs["id"])

    def handle_endtag(self, tag):
        if tag == "script":
            self.active = False

    def handle_data(self, data):
        if self.active:
            self.config += data


def make_server(html_path: Path, port=None):
    html_path = html_path.resolve(strict=True)
    parsed = BridgeConfig(html_path.read_text(encoding="utf-8"))
    config = json.loads(parsed.config)
    base = urlparse(config["base"])
    if base.hostname != "127.0.0.1":
        raise ValueError("Bridge must bind to 127.0.0.1")
    token = config["token"]
    queue_path = html_path.with_suffix(".requests.json")
    lock = threading.Lock()
    origin = f"http://127.0.0.1:{port if port is not None else base.port}"

    def read_queue():
        if queue_path.exists():
            data = json.loads(queue_path.read_text(encoding="utf-8"))
            if data.get("schema") != "lira-mindmap-requests/v1" or not isinstance(data.get("requests"), list):
                raise ValueError("Invalid request queue; existing data was preserved")
            return data
        return {"schema": "lira-mindmap-requests/v1", "document": str(html_path), "requests": []}

    def write_queue(data):
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=queue_path.parent, delete=False) as f:
                temporary = f.name
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(temporary, queue_path)
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass  # Do not log user opinion text or capability tokens.

        def allowed_origin(self):
            return self.headers.get("Origin") in (None, "null", origin)

        def respond(self, status, data, content_type="application/json; charset=utf-8"):
            payload = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            if self.allowed_origin() and self.headers.get("Origin"):
                self.send_header("Access-Control-Allow-Origin", self.headers["Origin"])
                self.send_header("Vary", "Origin")
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(payload)

        def authenticated(self):
            supplied = self.headers.get("X-Mindmap-Token", "")
            if not self.allowed_origin() or not secrets.compare_digest(supplied, token):
                self.respond(403, {"error": "Local editor access denied"})
                return False
            return True

        def do_OPTIONS(self):
            if not self.allowed_origin():
                return self.respond(403, {"error": "Origin rejected"})
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", self.headers.get("Origin", origin))
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Mindmap-Token")
            self.send_header("Access-Control-Allow-Private-Network", "true")
            self.end_headers()

        def do_GET(self):
            path = urlparse(self.path).path
            if path in ("/", "/mindmap"):
                return self.respond(200, html_path.read_bytes(), "text/html; charset=utf-8")
            markdown_path = html_path.with_suffix(".md")
            if path == "/" + markdown_path.name:
                if not markdown_path.is_file():
                    return self.respond(404, {"error": "Markdown document has not been generated"})
                return self.respond(200, markdown_path.read_bytes(), "text/plain; charset=utf-8")
            if path != "/api/requests":
                return self.respond(404, {"error": "Not found"})
            if not self.authenticated():
                return
            try:
                with lock:
                    data = read_queue()
                return self.respond(200, {"path": str(queue_path), **data})
            except (OSError, ValueError) as exc:
                return self.respond(500, {"error": str(exc)})

        def do_POST(self):
            if urlparse(self.path).path != "/api/requests":
                return self.respond(404, {"error": "Not found"})
            if not self.authenticated():
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 80000:
                    return self.respond(413, {"error": "Request too large or empty"})
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError("Invalid request")
                code, kind, text, request_id = (body.get(k) for k in ("node", "kind", "text", "id"))
                current_nodes = BridgeConfig(html_path.read_text(encoding="utf-8")).nodes
                if code not in current_nodes or kind not in ("modify", "discuss", "idea"):
                    raise ValueError("Invalid node or action")
                if not isinstance(text, str) or not text.strip() or len(text) > 12000:
                    raise ValueError("Please enter 1–12000 characters")
                if not isinstance(request_id, str) or not re.fullmatch(r"[a-zA-Z0-9-]{16,80}", request_id):
                    raise ValueError("Invalid request ID")
                item = {"id": request_id, "node": code, "kind": kind, "text": text.strip(),
                        "created_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"),
                        "status": {"modify": "pending_modify", "discuss": "pending_discussion", "idea": "pending_brainstorm"}[kind]}
                with lock:
                    data = read_queue()
                    existing = next((x for x in data["requests"] if x["id"] == request_id), None)
                    if existing:
                        if any(existing[k] != item[k] for k in ("node", "kind", "text")):
                            return self.respond(409, {"error": "Request ID conflict"})
                        item = existing
                    else:
                        data["requests"].append(item)
                        write_queue(data)
                return self.respond(200, {"saved": True, "path": str(queue_path), "request": item})
            except (ValueError, TypeError, UnicodeError) as exc:
                return self.respond(400, {"error": str(exc)})
            except OSError as exc:
                return self.respond(500, {"error": str(exc)})

    server = ThreadingHTTPServer(("127.0.0.1", port if port is not None else base.port), Handler)
    server.queue_path = queue_path
    return server


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--html", type=Path, required=True)
    args = parser.parse_args()
    server = make_server(args.html)
    print(f"Local mindmap: http://127.0.0.1:{server.server_port}/", flush=True)
    print(f"Requests: {server.queue_path}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
