#!/usr/bin/env python3
"""前端健康检查：逐页打开，收集 JS 报错 + 页面文本长度 + 关键内容"""
import asyncio, json, os, subprocess, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import shot

PAGES = [("overview","总览"),("positions","持仓与成交"),("market","行情图表"),
         ("backtest","回测与任务"),("detail","回测明细"),("research","策略研究"),
         ("ops","实盘运维"),("pairlist","交易对与锁"),("security","密码管理"),
         ("logs","运行日志"),
         ("tasks","研究任务(已隐藏)"),("iteration","模型迭代(已隐藏)"),("live","实盘统计(已隐藏)")]

async def main():
    port = shot.free_port(); profile = tempfile.mkdtemp(prefix="chk-")
    chrome = shot.find_chrome(); token = shot.fetch_token()
    proc = subprocess.Popen([chrome,"--headless=new","--no-sandbox","--no-first-run",
        "--no-default-browser-check",f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}","--window-size=1600,1000","about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    bad = []
    try:
        for _ in range(80):
            if shot.wait_debugger(port): break
            await asyncio.sleep(0.5)
        ws = shot.page_ws_url(port)
        import websockets
        async with websockets.connect(ws, max_size=200*1024*1024) as w:
            mid = [0]
            errs = []
            async def cmd(method, params=None):
                mid[0]+=1; i=mid[0]
                await w.send(json.dumps({"id":i,"method":method,"params":params or {}}))
                while True:
                    r=json.loads(await w.recv())
                    if r.get("method") in ("Runtime.consoleAPICalled",) :
                        a=r["params"]
                        if a.get("type")=="error":
                            errs.append(" ".join(str(x.get("value") or x.get("description") or "") for x in a.get("args",[]))[:140])
                    if r.get("method")=="Runtime.exceptionThrown":
                        errs.append(str(r["params"].get("exceptionDetails",{}).get("text"))[:140])
                    if r.get("id")==i: return r
            await cmd("Page.enable"); await cmd("Runtime.enable")
            await cmd("Page.navigate",{"url":"http://127.0.0.1:8888/"})
            await asyncio.sleep(4)
            if token:
                await cmd("Runtime.evaluate",{"expression":
                    "localStorage.setItem('ft_access_token', %s); 'ok'" % json.dumps(token)})
            print(f"  {'页面':<18}{'路由':<26}{'文本':>7}{'报错':>6}  状态")
            print("  "+"-"*74)
            for key,name in PAGES:
                errs.clear()
                await cmd("Page.navigate",{"url":f"http://127.0.0.1:8888/#/quant/{key}"})
                await cmd("Page.reload",{})
                await asyncio.sleep(7)
                r = await cmd("Runtime.evaluate",{"expression":"document.body.innerText",
                                                  "returnByValue":True})
                txt=(r.get("result",{}).get("result",{}) or {}).get("value") or ""
                h = await cmd("Runtime.evaluate",{"expression":"location.hash","returnByValue":True})
                hashv=(h.get("result",{}).get("result",{}) or {}).get("value") or ""
                # 登录页特征：文本很短且含「忘记密码」；不能只看「登录/密码」二字
                # （「密码管理」页本身就有这两个词）
                login = ("忘记密码" in txt and "重置密码" in txt and len(txt) < 400)
                empty = len(txt) < 120
                st = "❌ 停在登录页" if login else ("⚠ 内容过少" if empty else "✅")
                if login or empty: bad.append((key,st))
                print(f"  {name:<18}{hashv:<26}{len(txt):>7}{len(errs):>6}  {st}")
                if errs:
                    for e in errs[:2]:
                        print(f"      ⚠ {e}")
    finally:
        proc.terminate()
    print()
    if bad:
        print(f"  ❌ 异常页面 {len(bad)} 个: {bad}")
        return 1
    print("  ✅ 全部页面正常渲染")
    return 0

sys.exit(asyncio.run(main()))
