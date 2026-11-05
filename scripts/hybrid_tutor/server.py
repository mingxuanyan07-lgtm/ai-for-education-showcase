"""Loopback-only Hybrid Tutor review console. Starts no model on page load."""
from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
from urllib.parse import parse_qs, unquote, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hybrid_tutor.bridge import Bridge, BridgeError, ROOT, inside, read_json, sha

STATIC = Path(__file__).parent / "static"
MAX_BODY = 900000


def accepted_origin(host, origin, port):
    allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
    return host in allowed and (origin is None or origin in {"http://" + item for item in allowed})


def public_config(bridge):
    config = read_json(inside(bridge.root, bridge.config_path))
    proof = config.get("proof") or {}
    teaching = config.get("teaching") or {}
    # Explicit allowlist: never serialize provider connection settings/secrets.
    return {"workflow": "hybrid-rethlas-deeptutor-v1", "config_sha256": sha(bridge.config_path.read_bytes()),
            "proof": {key: proof.get(key) for key in ("model", "provider", "reasoning_effort", "architecture_profile", "max_rounds")},
            "teaching": {key: teaching.get(key) for key in ("model", "provider", "workflow_variant")},
            "budget": {key: proof.get(key) for key in ("max_model_calls", "max_tool_calls", "max_total_seconds", "max_observed_tokens")},
            "authorization_file_present": bridge.authorization_path.is_file(),
            "notice": "真实运行须匹配宿主授权的配置、原文哈希、题号、次数与有效期；网页不能自行授权。"}


def handler_class(bridge):
    class Handler(BaseHTTPRequestHandler):
        server_version = "HybridTutor/1.0"

        def log_message(self, fmt, *args):
            # URLs may contain record IDs but never request bodies or API keys.
            sys.stderr.write("hybrid-http " + fmt % args + "\n")

        def _headers(self, code, content_type, length, *, attachment=None, record=False):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; img-src 'self' data:; sandbox" if record
                             else "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            if attachment:
                self.send_header("Content-Disposition", 'attachment; filename="' + attachment + '"')
            self.end_headers()

        def _bytes(self, payload, content_type, *, code=200, **kwargs):
            self._headers(code, content_type, len(payload), **kwargs)
            self.wfile.write(payload)

        def _json(self, payload, code=200, attachment=None):
            self._bytes(json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"), "application/json; charset=utf-8",
                        code=code, attachment=attachment)

        def _local(self, post=False):
            if not accepted_origin(self.headers.get("Host"), self.headers.get("Origin"), self.server.server_port):
                raise BridgeError("仅允许本机同源请求")
            if self.headers.get("Sec-Fetch-Site") == "cross-site":
                raise BridgeError("拒绝跨站请求")
            if post and self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                raise BridgeError("POST 需要 application/json")

        def do_GET(self):
            try:
                self._local()
                parsed = urlsplit(self.path)
                route = unquote(parsed.path)
                if route in {"/", "/app.js", "/styles.css"}:
                    filename = "index.html" if route == "/" else route[1:]
                    mime = {"index.html": "text/html", "app.js": "text/javascript", "styles.css": "text/css"}[filename]
                    return self._bytes((STATIC / filename).read_bytes(), mime + "; charset=utf-8")
                if route == "/api/config":
                    return self._json(public_config(bridge))
                if route == "/api/tasks":
                    return self._json({"tasks": bridge.list_tasks()})
                if route == "/api/history":
                    return self._json({"records": bridge.history()})
                if route == "/api/replay":
                    query = parse_qs(parsed.query)
                    if set(query) != {"record_id"} or len(query["record_id"]) != 1:
                        raise BridgeError("需要一个历史 record_id")
                    return self._json(bridge.replay(query["record_id"][0]))
                if route.startswith("/api/tasks/"):
                    parts = route.split("/")
                    if len(parts) == 4:
                        return self._json(bridge.results(parts[3]))
                    if len(parts) == 5 and parts[4] == "export":
                        return self._json(bridge.results(parts[3]), attachment=parts[3] + ".json")
                if route.startswith("/records/"):
                    relative = route[len("/records/"):]
                    parts = relative.split("/")
                    if len(parts) < 4 or any(part in {"", ".", ".."} for part in parts):
                        raise BridgeError("非法记录路径")
                    record_id = "/".join(parts[:3])
                    bridge.record_path(record_id)
                    path = inside(bridge.root / "docs/experiments", relative)
                    allowed = {".html": "text/html", ".md": "text/plain", ".txt": "text/plain", ".log": "text/plain", ".json": "application/json", ".jsonl": "text/plain"}
                    if not path.is_file() or path.suffix not in allowed:
                        raise BridgeError("只允许读取归档文本材料")
                    if path.stat().st_size > 32 * 1024 * 1024:
                        raise BridgeError("归档文件超过网页可读取的 32 MiB 范围")
                    return self._bytes(path.read_bytes(), allowed[path.suffix] + "; charset=utf-8", record=True)
                self._json({"error": "接口不存在"}, 404)
            except (OSError, ValueError, TypeError) as exc:
                self._json({"error": str(exc)}, 400)

        def do_POST(self):
            try:
                self._local(post=True)
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= MAX_BODY:
                    raise BridgeError("请求体为空或超过可保存范围")
                payload = json.loads(self.rfile.read(size).decode("utf-8"))
                if not isinstance(payload, dict):
                    raise BridgeError("请求体须为对象")
                route = urlsplit(self.path).path
                if route == "/api/tasks":
                    allowed = {"problem", "requirements", "user_prompt", "idempotency_key", "question_id", "tags", "mode"}
                    if set(payload) - allowed:
                        raise BridgeError("存在不允许的字段；网页不能传入授权或命令")
                    return self._json(bridge.start(**payload), 202)
                parts = route.split("/")
                if len(parts) == 5 and parts[1:3] == ["api", "tasks"] and parts[4] == "cancel":
                    if payload:
                        raise BridgeError("取消请求不接收附加字段")
                    return self._json(bridge.cancel(parts[3]))
                self._json({"error": "接口不存在"}, 404)
            except (OSError, ValueError, TypeError) as exc:
                self._json({"error": str(exc)}, 400)

    return Handler


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", default=str(ROOT))
    parser.add_argument("--port", type=int, default=8108)
    args = parser.parse_args(argv)
    if not 1024 <= args.port <= 65535:
        parser.error("port must be 1024..65535")
    bridge = Bridge(args.project_root)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler_class(bridge))
    print(f"Hybrid Tutor review console: http://127.0.0.1:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
