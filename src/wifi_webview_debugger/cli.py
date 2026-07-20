#!/usr/bin/env python3
"""
一键在本机拦截「certs/ 里所有证书覆盖的域名」-> 本地 dist/ 构建（或反代到本地服务），
供设备通过 HTTP 代理加载本地页面做验证。设备端零装证书、免 sudo。

自动扫描 certs/ 里成对的 证书+私钥，取出各自 SAN 域名，全部一起拦截。
Caddy 为每个域名建一个 site 块、各用自己的证书，按 SNI 分流。

外部依赖：caddy(TLS 终止/静态服务/反代)、openssl(读证书 SAN 与到期)。
所有可写文件都落在当前工作目录：certs/、dist/ 为输入；shots/ 存截图；
.wifi-webview-debugger/ 存 Caddy 生成配置与数据目录。
"""

import os
import re
import sys
import glob
import time
import signal
import atexit
import argparse
import subprocess
from shutil import which

from . import __version__

GREEN, CYAN, RED, YELLOW, RESET = (
    "\033[32m",
    "\033[36m",
    "\033[31m",
    "\033[33m",
    "\033[0m",
)

# 运行期配置（在 main() 里由命令行参数/环境变量填充）
CFG = {}


def ok(m):
    print(f"{GREEN}[ok]{RESET} {m}", flush=True)


def info(m):
    print(f"{CYAN}[i]{RESET} {m}", flush=True)


def err(m):
    print(f"{RED}[ERR]{RESET} {m}", file=sys.stderr, flush=True)


def die(m):
    err(m)
    sys.exit(1)


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def cert_sans(path):
    out = run(
        ["openssl", "x509", "-in", path, "-noout", "-ext", "subjectAltName"]
    ).stdout
    return re.findall(r"DNS:([^,\s]+)", out)


def cert_enddate(path):
    out = run(["openssl", "x509", "-in", path, "-noout", "-enddate"]).stdout
    m = re.search(r"notAfter=(.+)", out)
    return m.group(1).strip() if m else ""


def find_key(cert_path):
    """在证书所在目录里找私钥：优先同前缀 .key，再退回 privkey.pem / 任意 .key。"""
    d = os.path.dirname(cert_path)
    base = os.path.basename(cert_path)
    base = re.sub(r"(_bundle)?\.(pem|crt)$", "", base)
    cands = [os.path.join(d, base + ".key"), os.path.join(d, "privkey.pem")]
    cands += sorted(glob.glob(os.path.join(d, "*.key")))
    for c in cands:
        if os.path.isfile(c):
            return c
    return None


def scan_certs():
    """扫描 certs/(含一层子目录，支持按域名建目录)，
    返回 {host: (cert, key)}；一个 host 只保留第一张覆盖它的完整证书。"""
    certs_dir = CFG["certs_dir"]
    order = []
    pats = ["fullchain.pem", "*_bundle.pem", "*_bundle.crt", "*.pem", "*.crt"]
    # 顶层 certs/*.pem 与 一层子目录 certs/*/*.pem 都扫
    for d in (certs_dir, os.path.join(certs_dir, "*")):
        for pat in pats:
            order += sorted(glob.glob(os.path.join(d, pat)))
    seen, files = set(), []
    for f in order:
        if os.path.isfile(f) and f not in seen:
            seen.add(f)
            files.append(f)

    hosts = {}
    for f in files:
        try:
            with open(f, "r", errors="ignore") as fh:
                content = fh.read()
        except OSError:
            continue
        # 必须是真证书(排除私钥、以及 CSR 的 "BEGIN CERTIFICATE REQUEST")
        if "-----BEGIN CERTIFICATE-----" not in content:
            continue
        sans = cert_sans(f)
        if not sans:
            continue
        key = find_key(f)
        if not key:
            continue  # 没有配对私钥 -> 不完整，跳过
        for h in sans:
            if h not in hosts:
                hosts[h] = (f, key)
    return hosts


def gen_caddyfile(hosts):
    # log_credentials: 关闭 Caddy 对 Authorization/Cookie 等头的默认脱敏，方便本地调试看全请求
    L = [
        "{",
        "\tadmin off",
        "\tauto_https disable_redirects",
        "\tservers {",
        "\t\tlog_credentials",
        "\t}",
        "}",
        "",
    ]
    for host, (cert, key) in hosts.items():
        L.append("%s:%d {" % (host, CFG["caddy_port"]))
        L.append("\ttls %s %s" % (cert, key))
        L.append("\tlog {")
        L.append("\t\toutput stderr")
        L.append("\t\tformat console")
        L.append("\t}")
        if CFG["backend"]:
            L.append("\treverse_proxy %s" % CFG["backend"])
        else:
            L.append("\troot * %s" % CFG["dist"])
            L.append("\ttry_files {path} /index.html")
            L.append("\tfile_server")
        L.append("}")
        L.append("")
    return "\n".join(L) + "\n"


def detect_wifi_iface():
    """macOS: 找 Wi-Fi 对应的网卡名(如 en0)；找不到就退回 en0。"""
    out = run(["networksetup", "-listallhardwareports"]).stdout
    m = re.search(r"Hardware Port:\s*Wi-Fi\s*\nDevice:\s*(\w+)", out)
    return m.group(1) if m else "en0"


def iface_ip(iface):
    ip = run(["ipconfig", "getifaddr", iface]).stdout.strip()
    if ip:
        return ip
    out = run(["ifconfig", iface]).stdout  # getifaddr 拿不到时(如网桥)用 ifconfig 兜底
    m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", out)
    return m.group(1) if m else ""


PROCS = []


def spawn(cmd, env=None):
    e = dict(os.environ)
    if env:
        e.update(env)
    p = subprocess.Popen(cmd, env=e)
    PROCS.append(p)
    return p


def cleanup():
    for p in PROCS:
        if p.poll() is None:
            p.terminate()
    t = time.time()
    for p in PROCS:
        try:
            p.wait(timeout=max(0, 3 - (time.time() - t)))
        except subprocess.TimeoutExpired:
            p.kill()
    try:
        os.remove(CFG["gen_caddyfile"])
    except (OSError, KeyError):
        pass


def parse_args(argv):
    p = argparse.ArgumentParser(
        prog="wifi-webview-debugger",
        description="把本地构建通过 HTTP 代理 + 真证书 TLS 投送到同网段设备，附带调试工具条与 eruda。",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "-V", "--version", action="version", version="%(prog)s " + __version__
    )
    p.add_argument(
        "--certs",
        default=os.path.join(os.getcwd(), "certs"),
        help="证书目录(内含 证书+私钥 成对文件)",
    )
    p.add_argument(
        "--dist", default=os.path.join(os.getcwd(), "dist"), help="要服务的静态构建目录"
    )
    p.add_argument(
        "--backend",
        default="",
        help="反代到本地服务(如 127.0.0.1:3000)，设了就不服务 dist",
    )
    p.add_argument(
        "--hosts",
        default="",
        help="手动指定要拦的域名(空格/逗号分隔)，默认自动从证书 SAN 推断",
    )
    p.add_argument("--iface", default="", help="取本机 IP 的网卡，默认自动检测 Wi-Fi")
    p.add_argument(
        "--probe",
        action="store_true",
        default=True,
        help="注入调试工具条(刷新/自动刷新/传截图) + eruda 移动端 DevTools",
    )
    p.add_argument("--proxy-port", type=int, default=8080, help="代理监听端口")
    p.add_argument("--caddy-port", type=int, default=8443, help="Caddy 监听端口")
    p.add_argument("--probe-port", type=int, default=3777, help="工具条服务端口")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)

    cwd = os.getcwd()
    work = os.path.join(cwd, ".wifi-webview-debugger")
    CFG.update(
        {
            "certs_dir": os.path.abspath(args.certs),
            "dist": os.path.abspath(args.dist),
            "backend": args.backend.strip(),
            "iface": args.iface.strip(),
            "probe": args.probe,
            "proxy_port": args.proxy_port,
            "caddy_port": args.caddy_port,
            "probe_port": args.probe_port,
            "work": work,
            "gen_caddyfile": os.path.join(work, "caddy.generated"),
            "caddy_data": os.path.join(work, "caddy-data"),
            "shots": os.path.join(work, "shots"),
        }
    )

    # 端口 < 1024 才需要 root（默认 8443/8080 免 sudo）
    if (CFG["caddy_port"] < 1024 or CFG["proxy_port"] < 1024) and os.geteuid() != 0:
        die("--caddy-port/--proxy-port < 1024 需要 root：请用 sudo，或用默认高端口")

    if not which("caddy"):
        die(
            "未找到 caddy，请先安装： brew install caddy  /  https://caddyserver.com/docs/install"
        )

    # openssl：仅用于读取证书的 SAN 域名与到期时间（系统一般自带）
    if not which("openssl"):
        die("未找到 openssl，请先安装（macOS 一般自带；Linux： apt install openssl）")

    # PROBE：起本地服务并让 Caddy 反代到它
    probe_serves_dist = False
    if CFG["probe"] and not CFG["backend"]:
        CFG["backend"] = "127.0.0.1:%d" % CFG["probe_port"]
        probe_serves_dist = True

    if (not CFG["backend"] or probe_serves_dist) and not os.path.isdir(CFG["dist"]):
        die(
            "缺少构建目录 %s ，请先构建，或用 --backend 127.0.0.1:3000 反代"
            % CFG["dist"]
        )

    if not os.path.isdir(CFG["certs_dir"]):
        die(
            "缺少证书目录 %s ，请用 --certs 指定，目录里放 证书+私钥 成对文件"
            % CFG["certs_dir"]
        )

    # 决定要拦的域名
    hosts_env = args.hosts.strip()
    if hosts_env:
        want = [h for h in hosts_env.replace(",", " ").split() if h]
        all_map = scan_certs()
        hosts = {}
        for h in want:
            match = all_map.get(h)
            if not match:
                die("%s 里没有覆盖 %s 的证书" % (CFG["certs_dir"], h))
            hosts[h] = match
    else:
        hosts = scan_certs()

    if not hosts:
        die("%s 下没有找到「证书+私钥」成对的完整证书" % CFG["certs_dir"])

    ok("将拦截 %d 个域名：" % len(hosts))
    for h, (cert, key) in hosts.items():
        info(
            "  %-34s 证书 %s  到期 %s" % (h, os.path.basename(cert), cert_enddate(cert))
        )

    iface = CFG["iface"] or detect_wifi_iface()
    ip = iface_ip(iface)
    if not ip:
        err("网卡 %s 上没拿到 IP(Wi-Fi 没连？)。可用网卡：" % iface)
        out = run(["ifconfig"]).stdout
        cur = None
        for line in out.splitlines():
            m = re.match(r"^([a-z0-9]+):", line)
            if m:
                cur = m.group(1)
            mi = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", line)
            if mi and mi.group(1) != "127.0.0.1":
                err("    %s %s" % (cur, mi.group(1)))
        die("请确认已连 Wi-Fi，或用 --iface <网卡> 指定。")
    ok("本机在 %s 上的 IP： %s" % (iface, ip))

    # 生成 Caddyfile
    os.makedirs(CFG["work"], exist_ok=True)
    with open(CFG["gen_caddyfile"], "w") as f:
        f.write(gen_caddyfile(hosts))

    atexit.register(cleanup)
    signal.signal(signal.SIGINT, lambda *a: sys.exit(0))
    signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))

    # 起 probe-server(可选)
    if probe_serves_dist:
        os.makedirs(CFG["shots"], exist_ok=True)
        spawn(
            [sys.executable, "-m", "wifi_webview_debugger.probe"],
            env={
                "DIST": CFG["dist"],
                "PORT": str(CFG["probe_port"]),
                "SHOTS": CFG["shots"],
            },
        )
        time.sleep(1)
        ok(
            "工具条 + eruda 已启用(刷新 / 自动刷新 / 传截图 / DevTools)，截图存到 %s"
            % CFG["shots"]
        )

    # 起代理(多 host)
    spawn(
        [sys.executable, "-m", "wifi_webview_debugger.proxy"],
        env={
            "INTERCEPT_HOSTS": " ".join(hosts.keys()),
            "LOCAL_HOST": "127.0.0.1",
            "LOCAL_PORT": str(CFG["caddy_port"]),
            "LISTEN": "0.0.0.0:%d" % CFG["proxy_port"],
        },
    )
    time.sleep(1)

    print()
    ok("==== 设备端设置 ====================================================")
    print("  1) 设备连接与本机同一网络(本机热点，或同一 Wi-Fi)")
    print("  2) 设备 WiFi 高级设置 -> 代理: 手动(Manual)")
    print("       主机名/服务器: %s" % ip)
    print("       端口:          %d" % CFG["proxy_port"])
    print("  3) 打开 App 或浏览器访问下列任一域名，都会命中本地：")
    for h in hosts:
        print("       https://%s" % h)
    if CFG["backend"] and not probe_serves_dist:
        info("后端：反代到 %s" % CFG["backend"])
    ok("===================================================================")
    print()
    info("Caddy 前台运行中，Ctrl-C 结束(会自动清理)。")
    print()

    # Caddy 数据目录用工作目录内的，避免依赖被 sudo 占为 root 的 ~/Library/.../Caddy
    env = {"XDG_DATA_HOME": CFG["caddy_data"]}
    os.makedirs(CFG["caddy_data"], exist_ok=True)
    caddy = spawn(
        ["caddy", "run", "--config", CFG["gen_caddyfile"], "--adapter", "caddyfile"],
        env=env,
    )
    caddy.wait()


if __name__ == "__main__":
    main()
