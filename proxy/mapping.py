"""读取 mapping.json，并做路径、地址的规范化。"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlparse

LOG = logging.getLogger("jellyfin-openlist")

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MAPPING = ROOT / "config" / "mapping.json"
DEFAULT_MITM_DIR = ROOT / "config" / "mitm"
DEFAULT_UPSTREAM = "http://127.0.0.1:8096"
LISTEN_PORT = 8080
CONFIG_PORT = 8081

# PlaybackInfo 里这些协议已经是网络地址，不再按本地盘路径改写。
NET_PROTOCOLS = frozenset({"http", "https", "rtmp", "rtsp", "udp", "rtp", "ftp"})


@dataclass(frozen=True)
class Mapping:
    rules: tuple[tuple[str, str], ...]
    case_insensitive: bool
    upstream: str
    host: str | None
    port: int | None


def mapping_path() -> Path:
    return Path(os.environ.get("MAPPING_FILE", str(DEFAULT_MAPPING)))


def enabled(name: str, default: bool = True) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def as_bool(value: object) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes"}
    return bool(value)


def http_url(url: str) -> str:
    text = url.strip()
    if text.startswith(("http://", "https://")):
        return text
    return "http://" + text.lstrip("/")


def parse_host_port(url: str) -> tuple[str, int] | None:
    parsed = urlparse(http_url(url))
    if not parsed.hostname:
        return None
    if parsed.port is not None:
        port = parsed.port
    elif (parsed.scheme or "").lower() == "https":
        port = 443
    else:
        port = 80
    return parsed.hostname, port


def upstream_from(explicit: str = "") -> str:
    raw = explicit.strip() or os.environ.get("JELLYFIN_UPSTREAM", "").strip() or DEFAULT_UPSTREAM
    return http_url(raw)


def norm_fs_path(path: str) -> str:
    text = path.strip().replace("\\", "/")
    absolute = text.startswith("/")
    parts = [part for part in text.split("/") if part and part != "."]
    normalized = "/".join(parts)
    if absolute:
        normalized = "/" + normalized
    return normalized.rstrip("/")


def relative_to_prefix(full: str, prefix: str, case_insensitive: bool) -> str | None:
    """命中前缀时返回后面的相对路径；路径就等于前缀时返回空字符串。"""
    full_n = norm_fs_path(full)
    prefix_n = norm_fs_path(prefix)
    if not full_n or not prefix_n:
        return None

    if case_insensitive:
        full_cmp, prefix_cmp = full_n.casefold(), prefix_n.casefold()
    else:
        full_cmp, prefix_cmp = full_n, prefix_n

    if full_cmp == prefix_cmp:
        return ""
    if not full_cmp.startswith(prefix_cmp + "/"):
        return None
    return full_n[len(prefix_n) + 1 :]


def join_url(url_base: str, suffix: str) -> str:
    base = url_base.strip().rstrip("/")
    suffix = suffix.strip("/")
    if not suffix:
        return base
    encoded = "/".join(quote(part, safe="") for part in suffix.split("/"))
    return f"{base}/{encoded}"


def load_mapping(path: Path) -> Mapping:
    upstream_text = ""
    rules: list[tuple[str, str]] = []
    case_insensitive = False

    if not path.is_file():
        LOG.warning("找不到映射文件 %s，不改写路径", path)
    else:
        raw = _read_json(path)
        if isinstance(raw, dict):
            case_insensitive = as_bool(raw.get("pathRulesCaseInsensitive"))
            for item in raw.get("pathRules") or []:
                if not isinstance(item, dict):
                    continue
                local_path, url_base = item.get("localPath"), item.get("urlBase")
                if not local_path or not url_base:
                    continue
                local_norm = norm_fs_path(str(local_path))
                if local_norm:
                    rules.append((local_norm, str(url_base).strip().rstrip("/")))
            # 长前缀优先，避免 /电影 盖住 /电影/4K。
            rules.sort(key=lambda item: len(item[0]), reverse=True)
            proxy = raw.get("proxy")
            if isinstance(proxy, dict) and proxy.get("upstream"):
                upstream_text = str(proxy["upstream"]).strip()

    upstream = upstream_from(upstream_text)
    host, port = parse_host_port(upstream) or (None, None)
    LOG.info(
        "映射 %s：%d 条路径规则，忽略大小写=%s，上游=%s",
        path,
        len(rules),
        case_insensitive,
        upstream,
    )
    return Mapping(tuple(rules), case_insensitive, upstream, host, port)


def read_upstream(path: Path) -> str:
    """启动 mitmdump 时用。映射无效则用 JELLYFIN_UPSTREAM，再不行用本机 8096。"""
    if path.is_file():
        data = _read_json(path)
        proxy = data.get("proxy") if isinstance(data, dict) else None
        if isinstance(proxy, dict) and proxy.get("upstream"):
            text = str(proxy["upstream"]).strip()
            if text:
                return http_url(text)
    return upstream_from()


def _read_json(path: Path) -> object | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        LOG.error("读取映射失败 %s: %s", path, exc)
        return None
