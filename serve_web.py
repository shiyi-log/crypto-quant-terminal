#!/usr/bin/env python3
"""
前端静态服务器（带正确的缓存策略）

为什么不用 python -m http.server：
  index.html 会被浏览器缓存，改了代码后不强制刷新就看不到新版，
  表现就是「登录没反应」—— 因为跑的还是旧的 JS。

策略：
  - .html 与 _app-config*.js  → no-store，每次都取最新
  - /js/ /jse/ /css/ 下的带哈希资源 → 长期缓存（文件名变了自然失效）

用法:
  python3 serve_web.py [--port 8888] [--dir ./web]
"""

import argparse
import functools
import http.client
import http.server
import os
import socket
import select
import sys
import urllib.parse

UPSTREAM = ("127.0.0.1", 8890)
PROXY_PREFIXES = ("/api/", "/auth/")

NO_STORE_SUFFIX = (".html",)
NO_STORE_PREFIX = ("_app-config",)


class Handler(http.server.SimpleHTTPRequestHandler):
    # 浏览器会预开连接（preconnect），若不设超时，单线程服务器会被
    # 一个不发请求的空连接永久阻塞。这里配合 ThreadingHTTPServer 使用。
    timeout = 15

    def end_headers(self):
        path = self.path.split("?")[0]
        name = os.path.basename(path)
        no_store = (
            path.endswith("/")
            or name.endswith(NO_STORE_SUFFIX)
            or name.startswith(NO_STORE_PREFIX)
        )
        if no_store:
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
        else:
            # 带内容哈希的资源可以长期缓存
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        super().end_headers()

    def handle_one_request(self):
        # 空连接/客户端提前断开不应打断服务
        try:
            super().handle_one_request()
        except (ConnectionResetError, BrokenPipeError, TimeoutError):
            self.close_connection = True


    def _proxy(self, method: str) -> bool:
        """Reverse-proxy /api/* and /auth/* to auth_service (8890)."""
        parsed = urllib.parse.urlsplit(self.path)
        if not parsed.path.startswith(PROXY_PREFIXES):
            return False
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        headers = {k: v for k, v in self.headers.items()
                   if k.lower() not in ("host", "content-length", "connection")}
        headers["Host"] = "127.0.0.1:8890"
        conn = http.client.HTTPConnection(*UPSTREAM, timeout=30)
        path = parsed.path + (("?" + parsed.query) if parsed.query else "")
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        data = resp.read()
        self.send_response(resp.status)
        for key, value in resp.getheaders():
            if key.lower() in ("content-length", "transfer-encoding", "connection"):
                continue
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if method != "HEAD":
            self.wfile.write(data)
        conn.close()
        return True

    def do_GET(self):
        if self._proxy("GET"):
            return
        super().do_GET()

    def do_HEAD(self):
        if self._proxy("HEAD"):
            return
        super().do_HEAD()

    def do_POST(self):
        if self._proxy("POST"):
            return
        self.send_error(404)

    def do_PUT(self):
        if self._proxy("PUT"):
            return
        self.send_error(404)

    def do_DELETE(self):
        if self._proxy("DELETE"):
            return
        self.send_error(404)

    def do_OPTIONS(self):
        if self._proxy("OPTIONS"):
            return
        self.send_error(404)

    def log_message(self, fmt, *args):
        # 只记录错误，保持终端清爽
        status = str(args[1]) if len(args) > 1 else ""
        if status.startswith(("4", "5")):
            sys.stderr.write("  %s\n" % (fmt % args))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8888)
    ap.add_argument("--dir", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "web"))
    ap.add_argument("--bind", default="127.0.0.1")
    args = ap.parse_args()

    if not os.path.isdir(args.dir):
        raise SystemExit(f"目录不存在: {args.dir}")

    handler = functools.partial(Handler, directory=args.dir)
    http.server.ThreadingHTTPServer.allow_reuse_address = True
    # 多线程：浏览器并发加载上百个静态资源，单线程会被预连接阻塞
    with http.server.ThreadingHTTPServer((args.bind, args.port), handler) as httpd:
        httpd.daemon_threads = True
        print(f"前端服务已启动: http://{args.bind}:{args.port}  (目录: {args.dir}, 多线程)")
        httpd.serve_forever()


if __name__ == "__main__":
    main()

