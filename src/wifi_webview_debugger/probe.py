#!/usr/bin/env python3
"""
本地调试服务器：服务 dist/，并向页面注入一个小工具条(刷新 / 自动刷新 / 传截图到电脑)。
由 CLI 在 --probe 模式下启动，Caddy 反代到这里。

功能路由：
  GET  /__buildid  返回当前主 bundle 文件名(变了说明重新 build 了，用于自动刷新)
  POST /__shot     接收设备上传的截图原始字节，存到 SHOTS 目录
  GET  /__eruda.js 打包内置的 eruda(移动端 DevTools)
  其它             服务 DIST 静态站点，并向 HTML 注入 eruda + 工具条

环境变量：
  DIST   dist 目录(默认 ./dist)
  PORT   监听端口(默认 3777)
  SHOTS  截图保存目录(默认 ./shots)
"""
import os
import re
import time
import pkgutil
import http.server
import socketserver
import urllib.parse

DIST = os.path.abspath(os.environ.get("DIST", os.path.join(os.getcwd(), "dist")))
PORT = int(os.environ.get("PORT", "3777"))
SHOTS = os.path.abspath(os.environ.get("SHOTS", os.path.join(os.getcwd(), ".wifi-webview-debugger/shots")))
os.makedirs(SHOTS, exist_ok=True)

# eruda 从打包内置的资产读取(离线可用)
ERUDA_BYTES = pkgutil.get_data("wifi_webview_debugger", "assets/eruda.js") or b""

# 注入的工具条：只用旧版浏览器也支持的语法(fetch/var/function)
TOOLBAR = """
<script>
(function(){
  if (window.__probeBar) return; window.__probeBar = 1;
  function el(tag, css){ var e=document.createElement(tag); if(css)e.style.cssText=css; return e; }
  function btn(label, fn){
    var b=el('button','padding:6px 10px;margin-left:6px;background:#222;color:#fff;border:1px solid #666;border-radius:6px;font:12px/1 sans-serif');
    b.textContent=label; b.addEventListener('click',fn); return b;
  }
  function toast(msg){
    var t=el('div','position:fixed;left:50%;top:12px;transform:translateX(-50%);background:#000;color:#fff;padding:8px 14px;border-radius:6px;z-index:2147483647;font:13px sans-serif');
    t.textContent=msg; document.body.appendChild(t);
    setTimeout(function(){ if(t.parentNode) t.parentNode.removeChild(t); }, 2500);
  }
  function ready(fn){ if(document.body) fn(); else setTimeout(function(){ready(fn);},50); }
  ready(function(){
    var bar=el('div','position:fixed;right:8px;bottom:8px;z-index:2147483647;display:flex;align-items:center');
    // 刷新
    bar.appendChild(btn('刷新', function(){ location.reload(); }));
    // 自动刷新
    var auto=false, timer=null, lastId=null;
    function getId(){ return fetch('/__buildid',{cache:'no-store'}).then(function(r){return r.text();}); }
    var ab=btn('自动刷新:关', function(){
      auto=!auto; ab.textContent='自动刷新:'+(auto?'开':'关');
      if(auto){ getId().then(function(id){lastId=id;}); timer=setInterval(function(){
        getId().then(function(id){ if(lastId && id && id!==lastId){ location.reload(); } lastId=id; });
      },2000); } else { clearInterval(timer); }
    });
    bar.appendChild(ab);
    // 传截图
    var input=el('input'); input.type='file'; input.accept='image/*'; input.style.display='none';
    input.addEventListener('change', function(){
      var f=input.files && input.files[0]; if(!f) return;
      fetch('/__shot?name='+encodeURIComponent(f.name),{method:'POST',body:f})
        .then(function(r){return r.json();})
        .then(function(j){ toast('已存到电脑: '+j.name); })
        .catch(function(){ toast('上传失败'); });
      input.value='';
    });
    bar.appendChild(input);
    bar.appendChild(btn('传截图', function(){ input.click(); }));
    document.body.appendChild(bar);
  });
})();
</script>
"""

# 注入 eruda：移动端 DevTools(Elements/Computed 看 CSS、Console 看报错、Network 等)。
# eruda 本地托管在 /__eruda.js。同步 <script> 在
# App 的 deferred 模块前执行，所以能接管 console / 捕获报错。
ERUDA_INJECT = """
<script src="/__eruda.js"></script>
<script>try{eruda.init({defaults:{displaySize:50,transparency:0.9}});}catch(e){}</script>
"""


def build_id():
    try:
        with open(os.path.join(DIST, "index.html"), "r", encoding="utf-8") as f:
            html = f.read()
        m = re.search(r"assets/[^\"']*\.js", html)
        if m:
            return m.group(0)
        return str(os.path.getmtime(os.path.join(DIST, "index.html")))
    except OSError:
        return "0"


class Handler(http.server.SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def __init__(self, *a, **k):
        super().__init__(*a, directory=DIST, **k)

    def log_message(self, format, *a):
        pass  # 静默，Caddy 已有访问日志

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/__buildid":
            self._send(200, build_id().encode(), "text/plain")
            return
        if path == "/__eruda.js":
            if ERUDA_BYTES:
                self._send(200, ERUDA_BYTES, "application/javascript")
            else:
                self._send(404, b"eruda.js not bundled", "text/plain")
            return
        # 判断是否该返回(注入过的) HTML
        fs = self.translate_path(self.path)
        html_file = None
        if path in ("", "/") or path.endswith("/"):
            html_file = os.path.join(DIST, "index.html")
        elif path.endswith(".html"):
            html_file = fs
        elif not os.path.exists(fs):
            html_file = os.path.join(DIST, "index.html")  # SPA 回落
        if html_file and os.path.isfile(html_file):
            with open(html_file, "rb") as f:
                data = f.read()
            # eruda + 工具条注入 </body> 前（同步 <script> 会在 App 的 deferred 模块前执行）
            inj = ERUDA_INJECT.encode() + TOOLBAR.encode()
            if b"</body>" in data:
                data = data.replace(b"</body>", inj + b"</body>", 1)
            else:
                data = data + inj
            self._send(200, data, "text/html; charset=utf-8")
            return
        return super().do_GET()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/__shot":
            q = urllib.parse.parse_qs(parsed.query)
            name = q.get("name", ["shot"])[0].replace("/", "_").replace("\\", "_")
            n = int(self.headers.get("Content-Length", "0"))
            data = self.rfile.read(n) if n > 0 else b""
            fn = time.strftime("%Y%m%d-%H%M%S-") + name
            with open(os.path.join(SHOTS, fn), "wb") as f:
                f.write(data)
            print("\033[32m[shot]\033[0m 已保存 %s (%d bytes)" % (fn, len(data)), flush=True)
            self._send(200, ('{"ok":true,"name":"%s"}' % fn).encode(), "application/json")
            return
        self._send(404, b"not found", "text/plain")


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    print("probe-server: 服务 %s  监听 127.0.0.1:%d  截图存 %s" % (DIST, PORT, SHOTS))
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
