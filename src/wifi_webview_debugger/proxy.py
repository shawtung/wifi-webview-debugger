#!/usr/bin/env python3
"""
极简 HTTP/HTTPS 正向代理，用于把某个域名的流量劫持到本机 Caddy。

用途：设备在 WiFi 里把 HTTP 代理设成 本机IP:8080。
  - 对目标 host 的 HTTPS(CONNECT host:443) -> 转发到本机 Caddy(127.0.0.1:443)，
    Caddy 用该域名的真证书应答，设备天然信任，无需装任何证书。
  - 其它所有 host(CONNECT / 普通 HTTP) -> 正常直连，设备照常上网。

因为只做原始字节转发、不解 TLS，所以不需要在设备上装 CA。

环境变量：
  INTERCEPT_HOSTS  要劫持的域名，空格或逗号分隔（优先）
  INTERCEPT_HOST   单个域名（INTERCEPT_HOSTS 未设时的回退）
  LOCAL_HOST       本机 Caddy 地址（默认 127.0.0.1）
  LOCAL_PORT       本机 Caddy 端口（默认 443）
  LISTEN           监听地址:端口（默认 0.0.0.0:8080）
"""
import os
import select
import socket
import threading

_hosts = os.environ.get("INTERCEPT_HOSTS") or os.environ.get("INTERCEPT_HOST", "")
INTERCEPT_HOSTS = set(h for h in _hosts.replace(",", " ").split() if h)
LOCAL_HOST = os.environ.get("LOCAL_HOST", "127.0.0.1")
LOCAL_PORT = int(os.environ.get("LOCAL_PORT", "443"))
_listen = os.environ.get("LISTEN", "0.0.0.0:8080")
LISTEN_HOST, LISTEN_PORT = _listen.rsplit(":", 1)
LISTEN_PORT = int(LISTEN_PORT)

GREEN, CYAN, RED, RESET = "\033[32m", "\033[36m", "\033[31m", "\033[0m"


def pipe(a, b):
    """双向转发直到任一端关闭。"""
    conns = [a, b]
    try:
        while True:
            r, _, x = select.select(conns, [], conns, 60)
            if x:
                break
            if not r:
                continue
            for s in r:
                data = s.recv(65536)
                if not data:
                    return
                (b if s is a else a).sendall(data)
    except OSError:
        return
    finally:
        for s in conns:
            try:
                s.close()
            except OSError:
                pass


def handle(client, addr):
    try:
        client.settimeout(30)
        # 读取请求行 + 头部
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = client.recv(4096)
            if not chunk:
                client.close()
                return
            buf += chunk
            if len(buf) > 65536:
                break
        line = buf.split(b"\r\n", 1)[0].decode("latin1", "replace")
        parts = line.split()
        if len(parts) < 2:
            client.close()
            return
        method, target = parts[0], parts[1]

        if method.upper() == "CONNECT":
            # target = host:port
            host, _, port = target.partition(":")
            port = int(port or "443")
            if host in INTERCEPT_HOSTS:
                up_host, up_port, tag = LOCAL_HOST, LOCAL_PORT, GREEN + "[HIT ]" + RESET
            else:
                up_host, up_port, tag = host, port, CYAN + "[pass]" + RESET
            print(f"{tag} CONNECT {host}:{port}  <- {addr[0]}", flush=True)
            try:
                upstream = socket.create_connection((up_host, up_port), timeout=15)
            except OSError as e:
                print(f"{RED}[err ]{RESET} 连接 {up_host}:{up_port} 失败: {e}", flush=True)
                client.close()
                return
            client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            pipe(client, upstream)
        else:
            # 普通 HTTP：从 Host 头取目标，原样转发
            host_hdr = ""
            for h in buf.split(b"\r\n"):
                if h.lower().startswith(b"host:"):
                    host_hdr = h.split(b":", 1)[1].strip().decode("latin1", "replace")
                    break
            host, _, port = host_hdr.partition(":")
            port = int(port or "80")
            if not host:
                client.close()
                return
            tag = CYAN + "[pass]" + RESET
            print(f"{tag} HTTP {method} {host}:{port}  <- {addr[0]}", flush=True)
            try:
                upstream = socket.create_connection((host, port), timeout=15)
            except OSError as e:
                print(f"{RED}[err ]{RESET} 连接 {host}:{port} 失败: {e}", flush=True)
                client.close()
                return
            upstream.sendall(buf)  # 转发已读取的请求
            pipe(client, upstream)
    except OSError:
        try:
            client.close()
        except OSError:
            pass


def main():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((LISTEN_HOST, LISTEN_PORT))
    srv.listen(128)
    print(f"{GREEN}代理已启动{RESET} {LISTEN_HOST}:{LISTEN_PORT}")
    print(f"  劫持 {', '.join(sorted(INTERCEPT_HOSTS))} -> {LOCAL_HOST}:{LOCAL_PORT}（其它域名直连）")
    print(f"  设备 WiFi 代理设为： 本机IP:{LISTEN_PORT}")
    print("  Ctrl-C 退出\n")
    try:
        while True:
            client, addr = srv.accept()
            threading.Thread(target=handle, args=(client, addr), daemon=True).start()
    except KeyboardInterrupt:
        print("\n已退出。")
    finally:
        srv.close()


if __name__ == "__main__":
    main()
