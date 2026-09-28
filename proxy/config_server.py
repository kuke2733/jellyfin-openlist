"""在 8081 提供配置页面，读写 config/mapping.json。"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from mapping import CONFIG_PORT, ROOT, as_bool, http_url, mapping_path, read_upstream

WEB_DIR = ROOT / "web"
_MAX_BODY = 1_000_000
_LOCK = threading.Lock()

_STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/main.js": ("main.js", "text/javascript; charset=utf-8"),
}


def config_port() -> int:
    raw = os.environ.get("CONFIG_PORT", str(CONFIG_PORT)).strip() or str(CONFIG_PORT)
    try:
        port = int(raw)
    except ValueError as exc:
        raise OSError(f"CONFIG_PORT 无效: {raw}") from exc
    if not 1 <= port <= 65535:
        raise OSError(f"CONFIG_PORT 无效: {raw}")
    return port


def _empty() -> dict:
    return {
        "version": 1,
        "comment": "",
        "proxy": {"upstream": ""},
        "pathRulesCaseInsensitive": False,
        "pathRules": [],
    }


def _read_file(path: Path) -> tuple[dict, str | None]:
    if not path.is_file():
        return _empty(), None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return _empty(), str(exc)
    if not isinstance(raw, dict):
        return _empty(), "映射文件不是 JSON 对象"
    return raw, None


def _view(raw: dict, parse_error: str | None) -> dict:
    proxy = raw.get("proxy") if isinstance(raw.get("proxy"), dict) else {}
    rules = []
    for item in raw.get("pathRules") or []:
        if not isinstance(item, dict):
            continue
        rules.append(
            {
                "localPath": str(item.get("localPath") or ""),
                "urlBase": str(item.get("urlBase") or ""),
                "comment": str(item.get("comment") or ""),
            }
        )
    body = {
        "version": raw.get("version", 1) if isinstance(raw.get("version"), int) else 1,
        "comment": str(raw.get("comment") or ""),
        "upstream": str(proxy.get("upstream") or ""),
        "pathRulesCaseInsensitive": as_bool(raw.get("pathRulesCaseInsensitive")),
        "pathRules": rules,
    }
    if parse_error:
        body["parseError"] = parse_error
    return body


def _document(payload: object) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("请求体不是 JSON 对象")
    upstream = str(payload.get("upstream") or "").strip()
    if not upstream:
        raise ValueError("填写上游地址")
    rules_in = payload.get("pathRules")
    if not isinstance(rules_in, list):
        raise ValueError("路径规则格式不对")

    rules: list[dict[str, str]] = []
    for item in rules_in:
        if not isinstance(item, dict):
            raise ValueError("路径规则格式不对")
        local_path = str(item.get("localPath") or "").strip()
        url_base = str(item.get("urlBase") or "").strip().rstrip("/")
        comment = str(item.get("comment") or "").strip()
        if not local_path and not url_base and not comment:
            continue
        if not local_path or not url_base:
            raise ValueError("路径规则要同时填写本地路径和 OpenList 前缀")
        row = {"localPath": local_path, "urlBase": url_base}
        if comment:
            row["comment"] = comment
        rules.append(row)

    version = payload.get("version")
    comment = str(payload.get("comment") or "").strip()
    document: dict = {"version": version if isinstance(version, int) and version > 0 else 1}
    if comment:
        document["comment"] = comment
    document["proxy"] = {"upstream": http_url(upstream)}
    document["pathRulesCaseInsensitive"] = as_bool(payload.get("pathRulesCaseInsensitive"))
    document["pathRules"] = rules
    return document


def _write(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


class Handler(BaseHTTPRequestHandler):
    on_upstream_change: Callable[[], None] | None = None

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/api/mapping":
            self._send_mapping()
            return
        static = _STATIC.get(path)
        if static is None:
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        name, content_type = static
        file_path = WEB_DIR / name
        try:
            data = file_path.read_bytes()
        except OSError:
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        self._send(200, data, content_type)

    def do_PUT(self) -> None:
        if self.path.split("?", 1)[0] != "/api/mapping":
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._json(400, {"error": "请求体无效"})
            return
        if length < 0 or length > _MAX_BODY:
            self._json(400, {"error": "请求体过大"})
            return
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
            document = _document(payload)
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._json(400, {"error": "请求体不是 JSON"})
            return
        except ValueError as exc:
            self._json(400, {"error": str(exc)})
            return

        path = mapping_path()
        with _LOCK:
            previous = read_upstream(path)
            try:
                _write(path, document)
            except OSError as exc:
                self._json(500, {"error": f"写入失败: {exc}"})
                return
        changed = document["proxy"]["upstream"] != previous
        upstream = document["proxy"]["upstream"]
        callback = self.on_upstream_change
        if changed and callback is not None:
            try:
                callback()
            except Exception as exc:
                self._json(500, {"error": f"配置已写入，反代重启失败: {exc}"})
                return
            self._json(200, {"ok": True, "restarted": True, "upstream": upstream})
            return
        self._json(200, {"ok": True, "restart": changed, "upstream": upstream})

    def _send_mapping(self) -> None:
        with _LOCK:
            raw, parse_error = _read_file(mapping_path())
        self._json(200, _view(raw, parse_error))

    def _json(self, status: int, body: dict) -> None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self._send(status, data, "application/json; charset=utf-8")

    def _send(self, status: int, data: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt: str, *args: object) -> None:
        return


class _Server(ThreadingHTTPServer):
    allow_reuse_address = True


def start_config_server(on_upstream_change: Callable[[], None] | None = None) -> int:
    port = config_port()
    Handler.on_upstream_change = on_upstream_change
    server = _Server(("0.0.0.0", port), Handler)
    threading.Thread(target=server.serve_forever, name="config-http", daemon=True).start()
    return port


if __name__ == "__main__":
    bound = start_config_server()
    print(f"配置页面 http://127.0.0.1:{bound}", flush=True)
    threading.Event().wait()
