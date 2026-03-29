# jellyfin-playback-proxy

**HTTP 反向代理**：中间件 **对外端口固定 `8080`**，请求转发到 **`config/mapping.json` 里配置的 Jellyfin 地址**（`proxy.upstream`），返回体在经过 `PlaybackInfo` 时按 **`pathRules`** 把本地路径批量换成网盘/AList URL。

全程 **明文 HTTP**，无需 mitm 证书。

实现：**mitmdump**（`reverse` 模式）+ **`addon.py`**；镜像基于 **python:slim** + `pip install mitmproxy`。

## 工作流程

1. 客户端里 Jellyfin 服务器地址填 **`http://<中间件IP>:8080`**（不要直连 Jellyfin 端口）。
2. **`run.py`** 固定监听 **8080**，`mitmdump --mode reverse:<upstream>` 的上游为 **`proxy.upstream`**。
3. **`addon.py`** 改写 `PlaybackInfo` JSON；其它接口透传。

## 配置文件 `config/mapping.json`

路径：**`<项目>/config/mapping.json`**（容器内 **`/app/config/mapping.json`**，挂载 **`./config`**）。模板：**`mapping.example.json`**。

### `proxy`（只需 Jellyfin 地址）

| 字段 | 含义 |
|------|------|
| **`upstream`** | Jellyfin **完整 HTTP 根地址**，须含协议与端口，例如 **`http://jellyfin:8096`**、**`http://192.168.1.10:8096`**。 |

未写 **`upstream`** 时，可用环境变量 **`JELLYFIN_UPSTREAM`** 兜底（默认 `http://127.0.0.1:8096`）。

### `pathRules`（播放地址改写）

`localPath` ↔ `urlBase`；更长 `localPath` 优先。

### `Host` 头

默认把请求 **`Host`** 改为 **`upstream` 的主机名与端口**；关闭请设 **`PROXY_FIX_HOST_HEADER=0`**。

## 构建与运行

```bash
cd jellyfin-playback-proxy
mkdir -p config && cp mapping.example.json config/mapping.json
# 编辑 config/mapping.json：至少设置 proxy.upstream
docker build -t jellyfin-playback-proxy:local .
docker run --rm \
  -p 8080:8080 \
  -v "$(pwd)/config:/app/config" \
  jellyfin-playback-proxy:local
```

Compose 见 **`docker-compose.example.yml`**。

- Linux 使用 **`http://host.docker.internal:8096`** 时，可加 **`--add-host=host.docker.internal:host-gateway`**，或改为宿主机 IP。

## 可选环境变量

| 变量 | 说明 |
|------|------|
| `MAPPING_FILE` | 默认 `<脚本目录>/config/mapping.json` |
| `MITM_CONFDIR` | 默认 `<脚本目录>/config/mitm` |
| `JELLYFIN_UPSTREAM` | mapping 未写 `proxy.upstream` 时兜底 |
| `MAPPING_RELOAD` | 默认 `1`：热加载 pathRules（**改 upstream 须重启容器**） |
| `PROXY_FIX_HOST_HEADER` | 默认 `1` |

## 本地调试

```bash
pip install -r requirements.txt
mkdir -p config && cp mapping.example.json config/mapping.json
python run.py
```

## 限制

- 仅改写 **PlaybackInfo**。
- 上游与客户端均为 **HTTP**。
