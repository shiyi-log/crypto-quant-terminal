#!/usr/bin/env python3
"""验证前端页面是否真的渲染出了新卡片（DOM 文本提取，比截图可靠）"""
import asyncio, json, os, subprocess, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import shot

KEYS = ["程序运行状况", "服务运行中", "任务进度", "因子健康度", "实盘策略与风控", "当前持仓"]

async def check(page_key="iteration"):
    port = shot.free_port()
    profile = tempfile.mkdtemp(prefix="verify-")
    chrome = shot.find_chrome()
    token = shot.fetch_token()
    proc = subprocess.Popen(
        [chrome, "--headless=new", "--no-sandbox", "--no-first-run",
         "--no-default-browser-check", f"--remote-debugging-port={port}",
         f"--user-data-dir={profile}", "--window-size=1600,1000", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(80):
            if shot.wait_debugger(port):
                break
            await asyncio.sleep(0.5)
        else:
            print("  ❌ Chrome 未就绪"); return 1
        ws = shot.page_ws_url(port)
        import websockets
        async with websockets.connect(ws, max_size=200*1024*1024) as w:
            async def cmd(i, m, p=None):
                await w.send(json.dumps({"id": i, "method": m, "params": p or {}}))
                while True:
                    r = json.loads(await w.recv())
                    if r.get("id") == i:
                        return r
            await cmd(1, "Page.enable")
            await cmd(2, "Page.navigate", {"url": f"http://127.0.0.1:8888/#/quant/{page_key}"})
            await asyncio.sleep(5)
            if token:
                await cmd(3, "Runtime.evaluate", {
                    "expression": "localStorage.setItem('ft_access_token', %s); 'ok'"
                                  % json.dumps(token)})
                await cmd(4, "Page.reload", {})
            await asyncio.sleep(14)
            r = await cmd(5, "Runtime.evaluate", {
                "expression": "document.body.innerText", "returnByValue": True})
            txt = (r.get("result", {}).get("result", {}) or {}).get("value") or ""
            url = await cmd(6, "Runtime.evaluate", {
                "expression": "location.hash", "returnByValue": True})
            hashv = (url.get("result", {}).get("result", {}) or {}).get("value") or ""
            print(f"  当前路由: {hashv}")
            print(f"  页面文本 {len(txt)} 字符")
            print()
            ok = 0
            for k in KEYS:
                hit = k in txt
                ok += hit
                print(f"    {'✅' if hit else '❌'} {k}")
            print()
            # 关键数据是否出现
            for probe in ["第 21 轮", "trial", "逐窗口 IC"]:
                if probe in txt:
                    print(f"    ✅ 含进度信息「{probe}」")
            if "登录" in txt and "密码" in txt:
                print("    ⚠ 页面仍显示登录表单")
            print(f"\n  关键词命中 {ok}/{len(KEYS)}")
            return 0 if ok >= 4 else 1
    finally:
        proc.terminate()

if __name__ == "__main__":
    sys.exit(asyncio.run(check(sys.argv[1] if len(sys.argv) > 1 else "iteration")))
