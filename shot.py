#!/Users/shiyi/DeepSeek/量化/bot/.venv/bin/python
"""
页面截图工具（基于 Chrome DevTools Protocol）

为什么不用 chrome --screenshot：
  它依赖 --virtual-time-budget 判断"页面加载完成"，而本项目的页面有
  setInterval 轮询 + ECharts 动画，虚拟时间永远不会耗尽，命令会一直挂着。

CDP 方案改为「导航 → 真实等待 N 秒 → 主动截图」，时机完全可控。

用法:
    python3 shot.py                     # 截全部页面
    python3 shot.py market              # 只截指定页面
    python3 shot.py market --wait 8     # 自定义等待秒数
"""

import argparse
import asyncio
import base64
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))

CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
]

PAGES = [
    ("overview", "总览"),
    ("positions", "持仓与成交"),
    ("market", "行情图表"),
    ("tasks", "研究任务"),
    ("live", "实盘统计"),
    ("pairlist", "交易对与锁"),
    ("backtest", "回测与任务"),
    ("logs", "运行日志"),
    ("security", "密码管理"),
    ("detail", "回测明细"),
    ("research", "策略研究"),
    ("iteration", "模型迭代"),
    ("ops", "实盘运维"),
]


def find_chrome():
    for c in CHROME_CANDIDATES:
        if os.access(c, os.X_OK):
            return c
    return None


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_debugger(port, timeout=25):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1):
                return True
        except Exception:
            time.sleep(0.3)
    return False


def page_ws_url(port):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=3) as r:
        targets = json.load(r)
    pages = [t for t in targets if t.get("type") == "page"]
    return pages[0]["webSocketDebuggerUrl"] if pages else None


async def capture(ws_url, url, out_path, width, height, wait_s, token=None):
    import websockets

    async with websockets.connect(ws_url, max_size=200 * 1024 * 1024) as ws:
        async def cmd(mid, method, params=None):
            await ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
            while True:
                msg = json.loads(await ws.recv())
                if msg.get("id") == mid:
                    return msg

        await cmd(1, "Page.enable")
        await cmd(2, "Page.navigate", {"url": url})

        if token:
            # localStorage 按 origin 隔离 —— 必须先导航到目标 origin 才能注入
            await asyncio.sleep(3)
            await cmd(10, "Runtime.evaluate", {
                "expression": "localStorage.setItem('ft_access_token', %s); 'ok'"
                              % json.dumps(token)})
            await cmd(11, "Page.reload", {})
            await asyncio.sleep(wait_s)
        else:
            # 真实等待：让自动登录、接口请求、图表渲染都完成
            await asyncio.sleep(wait_s)

        res = await cmd(3, "Page.captureScreenshot", {
            "format": "png", "fromSurface": True,
        })
        data = res.get("result", {}).get("data")
        if not data:
            raise RuntimeError(f"截图失败: {res}")
        with open(out_path, "wb") as f:
            f.write(base64.b64decode(data))
        return os.path.getsize(out_path)


async def run(pages, args):
    chrome = find_chrome()
    if not chrome:
        print("❌ 未找到 Chrome/Chromium")
        return 1

    out_dir = args.out
    os.makedirs(out_dir, exist_ok=True)
    port = free_port()
    profile = tempfile.mkdtemp(prefix="shot-profile-")

    proc = subprocess.Popen(
        [
            chrome,
            "--headless=new",
            "--no-sandbox",
            "--hide-scrollbars",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile}",
            f"--window-size={args.width},{args.height}",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    rc = 0
    token = fetch_token()
    if token:
        print("  🔑 已取得自动登录令牌")
    else:
        print("  ⚠ 未取得令牌，页面可能停在登录页")
    try:
        if not wait_debugger(port):
            print("❌ Chrome 调试端口未就绪")
            return 1

        for key, name in pages:
            # key 以 "/" 开头时视为完整路由路径（用于 /profile 这类非 /quant 下的页面）
            if key.startswith("/"):
                url = f"{args.fe}/#{key}"
                out = os.path.join(out_dir, key.strip("/").replace("/", "_") + ".png")
            else:
                url = f"{args.fe}/#/quant/{key}"
                out = os.path.join(out_dir, f"{key}.png")
            ws_url = page_ws_url(port)
            if not ws_url:
                print(f"  ❌ {key:<10} 无法获取调试目标")
                rc = 1
                continue
            try:
                size = await capture(ws_url, url, out, args.width, args.height,
                                     args.wait, token=token)
                print(f"  ✅ {key:<10} ({name})  {size // 1024} KB")
            except Exception as exc:
                print(f"  ❌ {key:<10} ({name})  {exc}")
                rc = 1
    finally:
        try:
            proc.send_signal(signal.SIGTERM)
            proc.wait(timeout=8)
        except Exception:
            proc.kill()
        shutil.rmtree(profile, ignore_errors=True)

    print(f"\n输出目录: {out_dir}")
    return rc


def fetch_token(auth="http://127.0.0.1:8890"):
    """取自动登录令牌（前端把 token 存在 localStorage 的 ft_access_token）"""
    import urllib.request
    try:
        req = urllib.request.Request(
            f"{auth}/auth/auto-login", data=b"{}",
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.load(r).get("access_token")
    except Exception:
        return None


async def inject_token(ws_url, token):
    """通过 CDP 把 token 写进 localStorage（避免每次都要手动登录）"""
    import websockets
    async with websockets.connect(ws_url, max_size=50 * 1024 * 1024) as ws:
        await ws.send(json.dumps({"id": 9001, "method": "Runtime.evaluate",
                                  "params": {"expression":
                                             "localStorage.setItem('ft_access_token', %s); 'ok'"
                                             % json.dumps(token)}}))
        await asyncio.wait_for(ws.recv(), timeout=10)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pages", nargs="*", help="页面 key，留空则截全部")
    ap.add_argument("--fe", default=os.environ.get("FE", "http://127.0.0.1:8888"))
    ap.add_argument("--out", default=os.path.join(ROOT, "shots"))
    ap.add_argument("--width", type=int, default=1600)
    ap.add_argument("--height", type=int, default=1000)
    ap.add_argument("--wait", type=float, default=8.0, help="每页等待秒数")
    args = ap.parse_args()

    valid = {k: n for k, n in PAGES}
    if args.pages:
        picked = [(k, valid.get(k, k)) for k in args.pages]
    else:
        picked = PAGES

    sys.exit(asyncio.run(run(picked, args)))


if __name__ == "__main__":
    main()
