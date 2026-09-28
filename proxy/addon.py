"""改写 Jellyfin PlaybackInfo：可直链的本地路径换成 OpenList 地址。"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mitmproxy import http

from mapping import (
    ROOT,
    NET_PROTOCOLS,
    Mapping,
    enabled,
    join_url,
    load_mapping,
    mapping_path,
    relative_to_prefix,
)

LOG = logging.getLogger("jellyfin-openlist")

# 带横线的 GUID，或 32 位十六进制。前面可以有 /emby、/mediabrowser 或 BaseUrl。
_ITEM_ID = (
    r"(?:[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
    r"|[0-9a-fA-F]{32})"
)
_PLAYBACK = re.compile(rf"(?i).*/Items/(?P<item>{_ITEM_ID})/PlaybackInfo/?$")

# 改成直链后这些字段会让客户端退回 Jellyfin 拉流，或拿本地文件的缓存标记去请求 OpenList。
_DROP_KEYS = (
    "transcodingUrl",
    "TranscodingUrl",
    "transcodingContainer",
    "TranscodingContainer",
    "transcodingSubProtocol",
    "TranscodingSubProtocol",
    "eTag",
    "ETag",
)

_AUDIT_LOCK = threading.Lock()


def _field(obj: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in obj:
            return obj[name]
    return None


def _put(obj: dict[str, Any], camel: str, pascal: str, value: Any) -> None:
    # 默认响应是 PascalCase，这里补一份 camelCase。本来就只有 camelCase 时，不额外加 PascalCase。
    if pascal in obj or camel not in obj:
        obj[pascal] = value
    obj[camel] = value


def _direct_play(source: dict[str, Any]) -> bool:
    value = _field(source, "supportsDirectPlay", "SupportsDirectPlay")
    if value is True:
        return True
    return isinstance(value, str) and value.strip().lower() in {"true", "1"}


def _remote_path(path: str) -> bool:
    text = path.strip().lower()
    return text.startswith(("http://", "https://"))


def _rewrite_source(source: dict[str, Any], target_url: str) -> None:
    _put(source, "path", "Path", target_url)
    _put(source, "protocol", "Protocol", "Http")
    _put(source, "isRemote", "IsRemote", True)
    # 客户端要看到 SupportsDirectStream，才会把这条 Http 源拿去播放，实际用的是上面的 Path。
    _put(source, "supportsDirectStream", "SupportsDirectStream", True)
    _put(source, "supportsTranscoding", "SupportsTranscoding", False)
    for key in _DROP_KEYS:
        source.pop(key, None)


def _apply(payload: dict[str, Any], mapping: Mapping) -> int:
    sources = _field(payload, "mediaSources", "MediaSources")
    if not isinstance(sources, list) or not mapping.rules:
        return 0

    rewritten = 0
    for source in sources:
        if not isinstance(source, dict) or not _direct_play(source):
            continue
        raw_path = _field(source, "path", "Path")
        if not isinstance(raw_path, str) or not raw_path.strip() or _remote_path(raw_path):
            continue
        protocol = _field(source, "protocol", "Protocol")
        if protocol is not None and str(protocol).strip().lower() in NET_PROTOCOLS:
            continue
        for local_prefix, url_base in mapping.rules:
            relative = relative_to_prefix(raw_path, local_prefix, mapping.case_insensitive)
            if relative is None:
                continue
            target = join_url(url_base, relative)
            _rewrite_source(source, target)
            rewritten += 1
            LOG.debug("路径 %s -> %s", raw_path[:120], target[:120])
            break
    return rewritten


def _audit_path() -> Path | None:
    custom = os.environ.get("REWRITE_AUDIT_FILE", "").strip()
    if custom:
        return Path(custom).expanduser()
    if not enabled("REWRITE_AUDIT", True):
        return None
    return ROOT / "rewrite_audit.log"


def _audit(flow: http.HTTPFlow, count: int, item_id: str) -> None:
    path = _audit_path()
    if path is None:
        return
    line = json.dumps(
        {
            "ts": datetime.now(timezone.utc).isoformat(),
            "kind": "PlaybackInfo",
            "method": flow.request.method,
            "path": flow.request.path,
            "pretty_url": flow.request.pretty_url,
            "rewritten_media_sources": count,
            "item_id": item_id,
        },
        ensure_ascii=False,
    )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _AUDIT_LOCK, path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError as exc:
        LOG.error("写改写记录失败 %s: %s", path, exc)


def _file_mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


class PlaybackRewrite:
    def __init__(self) -> None:
        self._path = mapping_path()
        self._lock = threading.Lock()
        self._mapping = load_mapping(self._path)
        self._mtime = _file_mtime(self._path)

    def _current(self) -> Mapping:
        if not enabled("MAPPING_RELOAD", True):
            return self._mapping
        mtime = _file_mtime(self._path)
        with self._lock:
            if mtime is None or (self._mtime is not None and mtime <= self._mtime):
                return self._mapping
            self._mtime = mtime
            self._mapping = load_mapping(self._path)
            return self._mapping

    def request(self, flow: http.HTTPFlow) -> None:
        # Jellyfin 会用请求里的 Host 拼一些绝对地址。改成上游，避免指回这个代理。
        if not enabled("PROXY_FIX_HOST_HEADER", True):
            return
        mapping = self._current()
        if mapping.host is None or mapping.port is None:
            return
        flow.request.host = mapping.host
        flow.request.port = mapping.port

    def response(self, flow: http.HTTPFlow) -> None:
        response = flow.response
        if response is None or response.status_code != 200:
            return
        if flow.request.method.upper() not in {"GET", "POST"}:
            return

        path = flow.request.path.split("?", 1)[0]
        matched = _PLAYBACK.search(path)
        if matched is None:
            return

        mapping = self._current()
        content_type = (response.headers.get("content-type") or "").lower()
        if not mapping.rules or "json" not in content_type:
            return

        try:
            payload = json.loads(response.get_text(strict=False) or "")
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            LOG.debug("跳过非 JSON 响应: %s", exc)
            return
        if not isinstance(payload, dict):
            return

        count = _apply(payload, mapping)
        if count <= 0:
            return

        response.set_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        response.headers["X-Jellyfin-Playback-Rewrite"] = str(count)
        item_id = matched.group("item")
        LOG.info("PlaybackInfo %s 改写了 %d 个媒体源", item_id, count)
        _audit(flow, count, item_id)


addons = [PlaybackRewrite()]
