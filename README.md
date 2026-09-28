# jellyfin-openlist

<p align="center">
  <a href="https://github.com/kuke2733/jellyfin-openlist"><img src="https://img.shields.io/badge/GitHub-kuke2733%2Fjellyfin--openlist-181717?logo=github&logoColor=white" alt="GitHub"></a>
  &nbsp;
  <a href="https://hub.docker.com/r/guyongbo/jellyfin-openlist"><img src="https://img.shields.io/badge/DockerHub-guyongbo%2Fjellyfin--openlist-2496ED?logo=docker&logoColor=white" alt="Docker Hub"></a>
</p>

客户端把 Jellyfin 地址填成 `http://<本机>:8080`。请求转到 `config/mapping.json` 的 `proxy.upstream`。`PlaybackInfo` 里可直链、且命中 `pathRules` 的本地路径，会换成 OpenList 地址。

## 运行

```bash
docker run --rm -p 8080:8080 -p 8081:8081 -v "$(pwd)/config:/app/config" guyongbo/jellyfin-openlist:latest
```

```yaml
services:
  jellyfin-openlist:
    image: guyongbo/jellyfin-openlist:latest
    ports:
      - "8080:8080"
      - "8081:8081"
    volumes:
      - ./config:/app/config
```

配置页面是 `http://<本机>:8081`。

Linux 上 `upstream` 写成 `http://host.docker.internal:8096` 时，加上 `--add-host=host.docker.internal:host-gateway`。

```bash
pip install -r proxy/requirements.txt
python proxy/run.py
```

## 配置

把 `config/mapping.example.json` 复制为 `config/mapping.json`，再挂到容器内 `/app/config/mapping.json`。

```json
{
  "version": 1,
  "proxy": {
    "upstream": "http://192.168.1.10:8096"
  },
  "pathRulesCaseInsensitive": true,
  "pathRules": [
    {
      "localPath": "/媒体库/电影",
      "urlBase": "https://openlist.example:5244/d/媒体库/电影"
    }
  ]
}
```

| 字段 | 含义 |
|------|------|
| `proxy.upstream` | Jellyfin 的 HTTP 根地址。没写时用 `JELLYFIN_UPSTREAM`，默认 `http://127.0.0.1:8096`。在配置页保存后会重启反代。 |
| `pathRules[].localPath` | 库内路径前缀，更长的优先。保存后热加载。 |
| `pathRules[].urlBase` | OpenList 前缀，不要末尾斜杠。 |
| `pathRulesCaseInsensitive` | 前缀比较是否忽略大小写。默认否。 |

## 改写

只处理返回 200 的 `GET`/`POST` `/Items/{id}/PlaybackInfo`。媒体源需同时满足：

- `SupportsDirectPlay` 为 `true`
- `Path` 是本地路径，并落在某条 `localPath` 下
- `Protocol` 还不是网络协议

改写后 `Path` 为 OpenList 地址，`Protocol` 为 `Http`，`IsRemote` 为 `true`，`SupportsDirectStream` 为 `true`，`SupportsTranscoding` 为 `false`。没有命中的源保持原样。

改写记录追加到 `rewrite_audit.log`。

## 环境变量

| 变量 | 默认 | 说明 |
|------|------|------|
| `MAPPING_FILE` | `<程序目录>/config/mapping.json` | 映射文件 |
| `JELLYFIN_UPSTREAM` | `http://127.0.0.1:8096` | `proxy.upstream` 未配置时的地址 |
| `MAPPING_RELOAD` | 开 | 热加载 `pathRules` |
| `PROXY_FIX_HOST_HEADER` | 开 | 把 `Host` 改成上游主机名和端口 |
| `MITM_CONFDIR` | `<程序目录>/config/mitm` | mitmproxy 配置目录 |
| `REWRITE_AUDIT` | 开 | 写改写记录 |
| `REWRITE_AUDIT_FILE` | `<程序目录>/rewrite_audit.log` | 记录文件 |
| `CONFIG_PORT` | `8081` | 配置页面端口 |

## 源码

`proxy/` 是反向代理和配置页，`web/` 是配置页面。`jellyfin/` 指向 [jellyfin/jellyfin](https://github.com/jellyfin/jellyfin)。

```bash
git clone --recurse-submodules <仓库地址>
git submodule update --init
```