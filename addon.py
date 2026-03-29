"""
mitmdump 反向 Jellyfin/Emby：修正 Host；仅改写 PlaybackInfo JSON 的 MediaSources（pathRules → 网盘 URL，
Protocol→Http），且仅 SupportsDirectPlay=true 的源。
环境：MAPPING_FILE、MAPPING_RELOAD、PROXY_FIX_HOST_HEADER、JELLYFIN_UPSTREAM。
审计默认写 addon 旁 rewrite_audit.log；REWRITE_AUDIT=0 关闭；REWRITE_AUDIT_FILE 指定路径。
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

from mitmproxy import http

LOG = logging.getLogger("jellyfin-playback-proxy")
_APP_DIR = Path(__file__).resolve().parent
_DEFAULT_MAPPING = _APP_DIR / "config" / "mapping.json"

_ITEM_ID = r"(?:[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}|[0-9a-fA-F]{32})"
PLAYBACK_PATH = re.compile(rf"(?i).*/Items/(?P<item>{_ITEM_ID})/PlaybackInfo(?:\?|$)")

_NET_PROTO = frozenset({"http", "https", "rtmp", "rtsp", "udp", "rtp", "ftp"})
_TX_KEYS = (
    ("transcodingUrl", "TranscodingUrl"),
    ("transcodingContainer", "TranscodingContainer"),
    ("transcodingSubProtocol", "TranscodingSubProtocol"),
    ("eTag", "ETag"),
)

_AUDIT_LOCK = threading.Lock()


def _audit_file() -> Path | None:
    custom = os.environ.get("REWRITE_AUDIT_FILE", "").strip()
    if custom:
        return Path(custom).expanduser()
    if os.environ.get("REWRITE_AUDIT", "1").strip().lower() in ("0", "false", "no"):
        return None
    return _APP_DIR / "rewrite_audit.log"


def _audit_append(flow: http.HTTPFlow, count: int, item_id: str) -> None:
    path = _audit_file()
    if path is None:
        return
    line = (
        json.dumps(
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
        + "\n"
    )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    try:
        with _AUDIT_LOCK, path.open("a", encoding="utf-8") as f:
            f.write(line)
    except OSError as e:
        LOG.error("REWRITE_AUDIT write failed %s: %s", path, e)


def _env_off(name: str, default_on: bool = True) -> bool:
    d = "1" if default_on else "0"
    return os.environ.get(name, d).lower() in ("0", "false", "no")


def _norm_fs_path(p: str) -> str:
    s = p.strip().replace("\\", "/")
    while "//" in s:
        s = s.replace("//", "/")
    return s.rstrip("/")


def _under_prefix(full: str, prefix: str, case_insensitive: bool) -> str | None:
    f, pr = _norm_fs_path(full), _norm_fs_path(prefix)
    if not f or not pr:
        return None
    if case_insensitive:
        fl, pl = f.lower(), pr.lower()
        if fl == pl:
            return ""
        if fl.startswith(pl + "/"):
            return f[len(pr) :].lstrip("/")
        return None
    if f == pr:
        return ""
    if f.startswith(pr + "/"):
        return f[len(pr) + 1 :]
    return None


def _remote_url_base(url_base: str, suffix: str) -> str:
    base = url_base.strip().rstrip("/")
    suf = suffix.strip("/")
    if not suf:
        return base
    enc = "/".join(quote(seg, safe="") for seg in suf.split("/"))
    return f"{base}/{enc}"


def _parse_host_port(url: str) -> tuple[str, int] | None:
    u = urlparse(url.strip())
    if not u.hostname:
        return None
    p = u.port or (443 if (u.scheme or "").lower() == "https" else 80)
    return (u.hostname, p)


def _http_url(url: str) -> str:
    u = url.strip()
    return u if u.startswith(("http://", "https://")) else "http://" + u.lstrip("/")


def _host_fix_env() -> tuple[str, int] | None:
    up = os.environ.get("JELLYFIN_UPSTREAM", "").strip()
    return _parse_host_port(_http_url(up)) if up else None


def _load_mapping(path: Path) -> tuple[list[tuple[str, str]], bool, tuple[str, int] | None]:
    if not path.is_file():
        LOG.warning("Mapping file not found: %s (no path rewrites)", path)
        return [], False, _host_fix_env()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        LOG.error("Failed to read mapping %s: %s", path, e)
        return [], False, _host_fix_env()

    ci_raw = raw.get("pathRulesCaseInsensitive")
    ci = (
        str(ci_raw).lower() in ("1", "true", "yes") if isinstance(ci_raw, str) else bool(ci_raw)
    )

    rules: list[tuple[str, str]] = []
    for r in raw.get("pathRules") or []:
        if not isinstance(r, dict):
            continue
        lp, ub = r.get("localPath"), r.get("urlBase")
        if not lp or not ub:
            continue
        lp_n = _norm_fs_path(str(lp))
        if lp_n:
            rules.append((lp_n, str(ub).strip().rstrip("/")))
    rules.sort(key=lambda x: len(x[0]), reverse=True)

    up_url = ""
    proxy = raw.get("proxy")
    if isinstance(proxy, dict) and proxy.get("upstream"):
        up_url = str(proxy["upstream"]).strip()
    if not up_url:
        up_url = os.environ.get("JELLYFIN_UPSTREAM", "").strip()
    host_fix = _parse_host_port(_http_url(up_url)) if up_url else None

    LOG.info(
        "Loaded mapping %s: %d path rule(s), case_insensitive=%s, host_header_fix=%s",
        path,
        len(rules),
        ci,
        host_fix,
    )
    return rules, ci, host_fix


def _get(d: dict[str, Any], *names: str) -> Any:
    for n in names:
        if n in d:
            return d[n]
    return None


def _set(d: dict[str, Any], camel: str, pascal: str, value: Any) -> None:
    if pascal in d or camel not in d:
        d[pascal] = value
    d[camel] = value


def _supports_direct_play(ms: dict[str, Any]) -> bool:
    v = _get(ms, "supportsDirectPlay", "SupportsDirectPlay")
    if v is True:
        return True
    return isinstance(v, str) and v.strip().lower() in ("true", "1")


def _rewrite_ms(ms: dict[str, Any], target_url: str) -> None:
    _set(ms, "path", "Path", target_url)
    _set(ms, "protocol", "Protocol", "Http")
    _set(ms, "isRemote", "IsRemote", True)
    _set(ms, "supportsDirectStream", "SupportsDirectStream", True)
    _set(ms, "supportsTranscoding", "SupportsTranscoding", False)
    for c, p in _TX_KEYS:
        ms.pop(c, None)
        ms.pop(p, None)


def _path_is_remote(p: str) -> bool:
    p = p.strip().lower()
    if p.startswith(("http://", "https://")):
        return True
    u = urlparse(p)
    return u.scheme in ("http", "https") and bool(u.netloc)


def _apply_rules(payload: dict[str, Any], rules: list[tuple[str, str]], ci: bool) -> int:
    sources = _get(payload, "mediaSources", "MediaSources")
    if not isinstance(sources, list) or not rules:
        return 0
    if not any(isinstance(ms, dict) and _supports_direct_play(ms) for ms in sources):
        return 0

    n = 0
    for ms in sources:
        if not isinstance(ms, dict) or not _supports_direct_play(ms):
            continue
        raw_path = _get(ms, "path", "Path")
        if not isinstance(raw_path, str) or not raw_path.strip() or _path_is_remote(raw_path):
            continue
        proto = _get(ms, "protocol", "Protocol")
        if proto is not None and str(proto).strip().lower() in _NET_PROTO:
            continue
        for local_prefix, url_base in rules:
            rel = _under_prefix(raw_path, local_prefix, ci)
            if rel is None:
                continue
            target = _remote_url_base(url_base, rel)
            _rewrite_ms(ms, target)
            n += 1
            LOG.debug("Rewrote path %s -> %s", raw_path[:120], target[:120])
            break
    return n


class PlaybackRewrite:
    def __init__(self) -> None:
        self._path = Path(os.environ.get("MAPPING_FILE", str(_DEFAULT_MAPPING)))
        self._lock = threading.Lock()
        self._rules, self._ci, self._host_fix = _load_mapping(self._path)
        self._mtime: float | None = None
        if self._path.is_file():
            try:
                self._mtime = self._path.stat().st_mtime
            except OSError:
                pass

    def _reload_if_needed(self) -> None:
        if _env_off("MAPPING_RELOAD", True):
            return
        try:
            mtime = self._path.stat().st_mtime
        except OSError:
            return
        with self._lock:
            if self._mtime is not None and mtime <= self._mtime:
                return
            self._mtime = mtime
            self._rules, self._ci, self._host_fix = _load_mapping(self._path)

    def request(self, flow: http.HTTPFlow) -> None:
        if _env_off("PROXY_FIX_HOST_HEADER", True):
            return
        self._reload_if_needed()
        with self._lock:
            fix = self._host_fix
        if fix:
            flow.request.host, flow.request.port = fix[0], fix[1]

    def response(self, flow: http.HTTPFlow) -> None:
        r = flow.response
        if r is None or r.status_code != 200:
            return
        if flow.request.method.upper() not in ("GET", "POST"):
            return

        m_pb = PLAYBACK_PATH.search(flow.request.path.split("?", 1)[0])
        if not m_pb:
            return

        self._reload_if_needed()
        with self._lock:
            rules, ci = self._rules[:], self._ci
        if not rules or "json" not in (r.headers.get("content-type") or "").lower():
            return

        try:
            payload = json.loads(r.get_text(strict=False) or "")
        except (json.JSONDecodeError, TypeError, ValueError) as e:
            LOG.debug("Skip non-JSON: %s", e)
            return
        if not isinstance(payload, dict):
            return

        count = _apply_rules(payload, rules, ci)
        if count <= 0:
            return

        r.set_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        r.headers["X-Jellyfin-Playback-Rewrite"] = str(count)
        iid = m_pb.group("item")
        LOG.info("PlaybackInfo item %s: rewrote %d media source(s)", iid, count)
        LOG.warning(
            "[rewrite-audit] kind=PlaybackInfo count=%d item=%s %s %s",
            count,
            iid,
            flow.request.method,
            flow.request.path,
        )
        _audit_append(flow, count, iid)


addons = [PlaybackRewrite()]
