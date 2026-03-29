#!/usr/bin/env python3
"""
启动 mitmdump：HTTP 反向代理到 Jellyfin，并加载同目录下的 addon.py 做 PlaybackInfo 改写。

对外监听端口固定为 8080。仅需在 config/mapping.json 的 proxy.upstream 填写 Jellyfin 根地址（含 http:// 与端口）。
若未写 upstream，可用环境变量 JELLYFIN_UPSTREAM 兜底。
可选：MAPPING_FILE、MITM_CONFDIR、MAPPING_RELOAD。
改写审计默认写 addon 旁 rewrite_audit.log；关闭设 REWRITE_AUDIT=0（见 addon.py）。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# 与 Docker WORKDIR /app、Compose 挂载 ./config:/app/config 一致
_APP_DIR = Path(__file__).resolve().parent
_DEFAULT_MAPPING = _APP_DIR / "config" / "mapping.json"
_DEFAULT_MITM = _APP_DIR / "config" / "mitm"

# 中间件对外端口写死，与客户端、docker -p 一致
LISTEN_PORT_FIXED = 8080


def _load_upstream_from_mapping(mapping_path: Path) -> str | None:
    """从 mapping.json 的 proxy.upstream 读取 Jellyfin 地址；无效则返回 None。"""
    if not mapping_path.is_file():
        return None
    try:
        data = json.loads(mapping_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"Warning: cannot read mapping for proxy.upstream: {e}", file=sys.stderr)
        return None
    p = data.get("proxy")
    if not isinstance(p, dict):
        return None
    upstream = p.get("upstream")
    if upstream is None:
        return None
    s = str(upstream).strip()
    return s or None


def main() -> None:
    """
    固定监听 8080，上游来自 mapping 的 proxy.upstream 或 JELLYFIN_UPSTREAM；exec 为 mitmdump。
    """
    mapping_path = Path(os.environ.get("MAPPING_FILE", str(_DEFAULT_MAPPING)))
    mu = _load_upstream_from_mapping(mapping_path)

    if mu:
        upstream = mu
    else:
        upstream = os.environ.get("JELLYFIN_UPSTREAM", "http://127.0.0.1:8096").strip()

    if not upstream.startswith(("http://", "https://")):
        upstream = "http://" + upstream.lstrip("/")

    mitm_conf = os.environ.get("MITM_CONFDIR", str(_DEFAULT_MITM))
    Path(mitm_conf).mkdir(parents=True, exist_ok=True)

    addon = _APP_DIR / "addon.py"
    argv = [
        "mitmdump",
        "-s",
        str(addon),
        "--set",
        f"confdir={mitm_conf}",
        "--listen-host",
        "0.0.0.0",
        "--listen-port",
        str(LISTEN_PORT_FIXED),
        "--mode",
        f"reverse:{upstream}",
    ]
    print(
        f"jellyfin-playback-proxy: listen=0.0.0.0:{LISTEN_PORT_FIXED} reverse -> {upstream}",
        file=sys.stderr,
    )
    os.execvp("mitmdump", argv)


if __name__ == "__main__":
    main()
