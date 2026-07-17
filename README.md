# wifi-webview-debugger

把本地构建（或本地服务）通过 **HTTP 代理 + 真证书 TLS** 投送到**同一局域网的设备**上做验证，
设备端**零装证书、免 sudo**。可选注入一个调试工具条（刷新 / 自动刷新 / 传截图回电脑）与
移动端 DevTools（内置 eruda）。

适用场景：手上有一台没法装自签 CA、没法 USB 调试、只能连 Wi-Fi 的设备（各类一体机 /
WebView 应用 / 老旧安卓机），但你拥有该域名的一张**公信 CA 真证书**，想让它加载你本地的构建。

## 原理

1. 设备把 Wi-Fi 的 HTTP 代理指向本机 `IP:8080`。
2. 本机的正向代理只对「你证书覆盖的域名」把 `CONNECT host:443` 重定向到本机 Caddy，
   其余流量原样直连（设备照常上网）。
3. Caddy 用该域名的**真证书**做 TLS 终止，设备天然信任，无需装任何 CA；
   背后服务你的 `dist/` 静态构建，或反代到你本地的服务。

因为代理只做原始字节转发、不解 TLS，所以设备上不需要装任何证书。

## 依赖

- **Python ≥ 3.8**（纯标准库，无 pip 依赖）
- **caddy** — TLS 终止 / 静态服务 / 反代：`brew install caddy`（或 https://caddyserver.com/docs/install ）
- **openssl** — 读取证书 SAN 域名与到期时间（macOS/Linux 一般自带）

缺少 caddy / openssl 时程序会在启动时报错并给出安装提示。

## 安装

```bash
pipx install wifi-webview-debugger
# 或从源码：
pipx install .
```

## 用法

在一个包含 `certs/` 和 `dist/` 的目录里运行：

```
./
  certs/     # 放 证书+私钥 成对文件(如 example.com_bundle.pem + example.com.key)
  dist/      # 你的静态构建
```

```bash
wifi-webview-debugger                 # 拦截 certs/ 里所有域名，服务 dist/
wifi-webview-debugger --probe         # 额外注入工具条 + eruda
wifi-webview-debugger --backend 127.0.0.1:3000   # 反代到本地服务，而非服务 dist
```

程序会打印本机 IP 与端口，按提示在设备 Wi-Fi 里设手动代理即可。`Ctrl-C` 退出并自动清理。

### 常用参数

| 参数 | 环境变量 | 默认 | 说明 |
|---|---|---|---|
| `--certs` | `CERTS_DIR` | `./certs` | 证书目录（证书+私钥成对） |
| `--dist` | `DIST` | `./dist` | 要服务的静态构建目录 |
| `--backend` | `BACKEND` | 空 | 反代到本地服务，设了就不服务 dist |
| `--hosts` | `INTERCEPT_HOSTS` | 自动 | 手动指定拦哪些域名，默认从证书 SAN 推断 |
| `--iface` | `IFACE` | 自动 | 取本机 IP 的网卡，默认自动检测 Wi-Fi |
| `--probe` | `PROBE` | 关 | 注入工具条 + eruda |
| `--proxy-port` | `PROXY_PORT` | 8080 | 代理监听端口 |
| `--caddy-port` | `CADDY_PORT` | 8443 | Caddy 监听端口 |
| `--probe-port` | `PROBE_PORT` | 3777 | 工具条服务端口 |

## 证书目录约定

`certs/` 下（含一层子目录）放置 **证书 + 对应私钥**：

- 证书：`fullchain.pem` / `*_bundle.pem` / `*_bundle.crt` / `*.pem` / `*.crt`
- 私钥：同前缀 `.key`，或 `privkey.pem`，或目录内任一 `.key`

程序会自动读取每张证书的 SAN 域名，把这些域名全部纳入拦截。

## 产物目录

运行时在当前目录生成：

- `shots/` — 设备上传的截图
- `.wifi-webview-debugger/` — Caddy 生成配置与数据目录

## License

MIT
