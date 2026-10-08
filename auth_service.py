#!/usr/bin/env python3
"""
本地认证与代理服务（端口 8890）

为什么需要它：
  Freqtrade 的 api_server 没有密码管理接口 —— 账号密码写在配置文件里，
  改了必须重启进程。所以这里独立维护一份 UI 用户库，并代理 Freqtrade
  的数据接口。好处：
    1. 改密码 / 重置密码立即生效，不需要重启 Freqtrade
    2. 前端永远拿不到 Freqtrade 的真实凭据（服务端代持 token）
    3. 数据接口也受认证保护，不再是谁都能访问

接口：
    POST /auth/login            {username, password}        -> {access_token, username}
    GET  /auth/me               Bearer                       -> {username}
    POST /auth/change-password  Bearer {old_password,new_password}
    POST /auth/reset-password   仅限本机 {new_password?}      -> {username, password}
    POST /auth/logout           Bearer
    GET  /auth/health
    *    /api/v1/**             代理到 Freqtrade（8889）
    GET  /run_progress.json     代理到 Freqtrade

用法:
    python3 auth_service.py [--port 8890] [--freqtrade http://127.0.0.1:8889]
"""

import argparse
import base64
import glob
import hashlib
import hmac
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))
AUTH_DIR = os.path.join(ROOT, "auth")
USERS_FILE = os.path.join(AUTH_DIR, "users.json")
SECRET_FILE = os.path.join(AUTH_DIR, "secret.key")
FT_CONFIG = os.path.join(ROOT, "bot", "user_data", "config_dashboard.private.json")
# freqtrade webserver 实例（只跑回测/下载数据/分析，不下单）用的配置。
# 它与实盘 bot 用同一个 api_server 账号密码，因此凭据读取逻辑可以复用。
FT_WEB_CONFIG = os.path.join(
    ROOT, "bot", "user_data", "config_trend_webserver.json"
)
# 前端用 /api/web/... 访问 webserver 实例；该前缀下的请求转发到 8891 并去掉 /web。
# 为什么需要两个上游：Freqtrade 把回测/下载/分析类端点挂在 is_webserver_mode 依赖下，
# 实盘 bot（runmode=trade）访问这些端点会 503 "Bot is not in the correct state."
FT_WEB_PREFIX = "/api/web"

TOKEN_TTL = 12 * 3600
PBKDF2_ROUNDS = 200_000
ALLOWED_ORIGINS = {
    "http://127.0.0.1:8888",
    "http://localhost:8888",
    # 开发模式（pnpm dev:antd）
    "http://127.0.0.1:5666",
    "http://localhost:5666",
}
LOCAL_ADDRS = {"127.0.0.1", "::1"}

DEFAULT_USER = "admin"
# 新环境可显式设置初始密码；未设置时生成随机密码，已有用户不受影响。
DEFAULT_PASS = os.environ.get("QUANT_BOOTSTRAP_PASSWORD")

# ── 深度学习迭代的「方法论」文案 ──
# 这些是口径说明（框架定义、迭代纪律），不随数据变化，因此放在常量里而不是
# 混进 /locals/ml 的返回值；凡是【会被数据改变】的字段（轮次、IC、t 值、
# 结论、通过组数）一律在 _local_ml() 里从 ml_*.jsonl 与模型注册表推导。
ML_FRAMING = "元标记（趋势定方向，ML 判断该不该做）"
ML_RULES = [
    "IC 必须逐窗口算再平均（池化会触发辛普森悖论）",
    "多组检验必须做多重比较校正",
    "组合层指标先做统计功效检查（Calmar 区间过宽）",
    "单次切分的样本外不可信，必须扩展窗口走查",
    "随机种子必须作为实验因子：同一配置换种子，t 值波动 2 倍以上（实测 1.94~4.47）",
]

# ── 实时行情源（Binance 公共接口，服务端代理）──
# 为什么不让浏览器直接请求 Binance：
#   1. 浏览器跨域（CORS）会被拦；
#   2. 币安在不同网络下可达性不同，放服务端好统一兜底；
#   3. 本地 feather 是「下载时点」的快照，机器人不重启就不更新，
#      图表会一直停在旧数据上。
BINANCE_SPOT = "https://api.binance.com"
BINANCE_FAPI = "https://fapi.binance.com"
BINANCE_INTERVALS = {
    "1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h",
    "6h", "8h", "12h", "1d", "3d", "1w", "1M",
}
KLINE_TTL = 20          # 秒：页面 30s 轮询，缓存 20s 足以避免打爆接口
KLINE_TIMEOUT = 8
_kline_cache: dict = {}


# ══════════════════════════ 用户库 ══════════════════════════

def _load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), PBKDF2_ROUNDS)
    return salt, dk.hex()


def verify_password(password, salt, expected_hex):
    _, got = hash_password(password, salt)
    return hmac.compare_digest(got, expected_hex)


def load_users():
    data = _load_json(USERS_FILE, None)
    if not data or "users" not in data:
        initial_password = DEFAULT_PASS or secrets.token_urlsafe(18)
        salt, h = hash_password(initial_password)
        data = {
            "users": {
                DEFAULT_USER: {
                    "salt": salt,
                    "hash": h,
                    "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                }
            }
        }
        _save_json(USERS_FILE, data)
        print(f"[auth] 已创建账号 {DEFAULT_USER} -> {USERS_FILE}")
        if not DEFAULT_PASS:
            print(f"[auth] 本机初始随机密码: {initial_password}")
        print("[auth] 请登录后在「密码管理」中修改，或使用登录页的重置入口。")
    return data


def save_users(data):
    _save_json(USERS_FILE, data)


# ══════════════════════════ 会话令牌 ══════════════════════════

def get_secret():
    if os.path.exists(SECRET_FILE):
        with open(SECRET_FILE, "rb") as f:
            return f.read()
    s = secrets.token_bytes(32)
    os.makedirs(AUTH_DIR, exist_ok=True)
    with open(SECRET_FILE, "wb") as f:
        f.write(s)
    os.chmod(SECRET_FILE, 0o600)
    return s


SECRET = get_secret()


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64d(txt: str) -> bytes:
    return base64.urlsafe_b64decode(txt + "=" * (-len(txt) % 4))


def make_token(username):
    payload = json.dumps({"u": username, "exp": int(time.time()) + TOKEN_TTL}).encode()
    body = _b64e(payload)
    sig = _b64e(hmac.new(SECRET, body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}"


def verify_token(token):
    if not token or "." not in token:
        return None
    body, sig = token.rsplit(".", 1)
    expect = _b64e(hmac.new(SECRET, body.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expect):
        return None
    try:
        payload = json.loads(_b64d(body))
    except Exception:
        return None
    if payload.get("exp", 0) < time.time():
        return None
    return payload.get("u")


# ══════════════════════════ Freqtrade 令牌代持 ══════════════════════════

_ft_cache = {"token": None, "ts": 0}
_ft_web_cache = {"token": None, "ts": 0}


def ft_credentials():
    cfg = _load_json(FT_CONFIG, {})
    api = cfg.get("api_server", {})
    user, password = api.get("username"), api.get("password")
    if not user or not password:
        raise RuntimeError("Freqtrade API 凭据未配置，请检查本地私有配置")
    return user, password


def _login_token(base, cache, label, force=False):
    """向某个 Freqtrade 实例用 HTTP Basic 换 JWT，并按 30 分钟缓存。"""
    if not force and cache["token"] and time.time() - cache["ts"] < 1800:
        return cache["token"]
    user, pwd = ft_credentials()
    basic = base64.b64encode(f"{user}:{pwd}".encode()).decode()
    req = urllib.request.Request(
        f"{base}/api/v1/token/login",
        method="POST",
        headers={"Authorization": f"Basic {basic}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        cache["token"] = data.get("access_token")
        cache["ts"] = time.time()
        return cache["token"]
    except Exception as exc:
        print(f"[auth] 无法获取 Freqtrade token（{label}）: {exc}", file=sys.stderr)
        cache["token"] = None
        return None


def ft_token(force=False):
    """实盘 bot（8889）的令牌"""
    return _login_token(FT_BASE, _ft_cache, "实盘 8889", force)


def ft_web_token(force=False):
    """
    webserver 实例（8891）的令牌。

    回测 / 下载数据 / 前瞻分析 / 递归分析 / pairlist 评估这些端点挂在
    Freqtrade 的 is_webserver_mode 依赖下，实盘 bot 访问会 503，
    必须走这个只做研究工作、不下单的实例。
    """
    return _login_token(FT_WEB_BASE, _ft_web_cache, "webserver 8891", force)


# ══════════════════════════ HTTP 处理 ══════════════════════════

FT_BASE = "http://127.0.0.1:8889"
FT_WEB_BASE = "http://127.0.0.1:8891"
START_TS = time.time()


class Handler(BaseHTTPRequestHandler):
    server_version = "QuantAuth/1.0"
    # ⚠️ 用 HTTP/1.0（每请求关闭连接）而不是 1.1 长连接。
    # 长连接下，只要有一个请求的 body 没被完整消费，残留字节就会串到下一个
    # 请求上（实测出现 `{}OPTIONS /... HTTP/1.1` → 501 → 预检失败 → 前端报 CORS 错误）。
    # 本地工具每秒几个请求，关闭连接的代价可忽略，但彻底消除这类串包问题。
    protocol_version = "HTTP/1.0"

    # ---------- 工具 ----------
    def _cors(self):
        origin = self.headers.get("Origin", "")
        if origin in ALLOWED_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Credentials", "true")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")

    def _send(self, code, payload=None, raw=None, ctype="application/json; charset=utf-8"):
        body = raw if raw is not None else (
            json.dumps(payload, ensure_ascii=False).encode() if payload is not None else b""
        )
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _read_body(self) -> bytes:
        """
        读取并缓存请求体。

        ⚠️ 必须对所有请求都调用一次：HTTP/1.1 是长连接，若某个请求的 body
        没有被读取，残留字节会被当成下一个请求的请求行 —— 表现为
        `{}OPTIONS /... HTTP/1.1` 这种错位，进而 501、鉴权失败、CORS 预检失败。
        """
        cached = getattr(self, "_body_cache", None)
        if cached is not None:
            return cached
        length = int(self.headers.get("Content-Length") or 0)
        self._body_cache = self.rfile.read(length) if length else b""
        return self._body_cache

    def _json_body(self):
        raw = self._read_body()
        if not raw:
            return {}
        try:
            return json.loads(raw.decode() or "{}")
        except Exception:
            return {}

    def _bearer(self):
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth[7:].strip()
        return None

    def _is_local(self):
        return self.client_address[0] in LOCAL_ADDRS

    def log_message(self, fmt, *args):
        msg = fmt % args
        if any(code in msg for code in (" 4", " 5")):
            sys.stderr.write("[auth] %s\n" % msg)

    # ---------- 路由 ----------
    def do_OPTIONS(self):
        self._read_body()
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self._cors()
        self.end_headers()

    def do_GET(self):
        self._read_body()
        if self.path.startswith("/auth/"):
            return self._auth_get()
        if self.path.startswith("/api/locals/klines"):
            return self._local_klines()
        if self.path.startswith("/api/locals/ohlcv"):
            return self._local_ohlcv()
        if self.path.startswith("/api/locals/backtest_detail"):
            return self._local_backtest_detail()
        if self.path.startswith("/api/locals/backtest_list"):
            return self._local_backtest_list()
        # ⚠ 必须排在 /api/locals/research 之前：前缀匹配会互相吞掉
        if self.path.startswith("/api/locals/research_progress"):
            return self._local_research_progress()
        if self.path.startswith("/api/locals/research_status"):
            return self._local_research_status()
        if self.path.startswith("/api/locals/research"):
            return self._local_research()
        if self.path.startswith("/api/locals/ops"):
            return self._local_ops()
        if self.path.startswith("/api/locals/iteration"):
            return self._local_iteration()
        if self.path.startswith("/api/locals/auto_iterate"):
            return self._local_auto_iterate()
        if self.path.startswith("/api/locals/risk_curve"):
            return self._local_json("risk_return_curve.json")
        if self.path.startswith("/api/locals/ml"):
            return self._local_ml()
        if self.path.startswith("/api/locals/services"):
            return self._local_services()
        if self.path.startswith("/api/locals/model_versions"):
            return self._local_model_versions()
        if self.path.startswith(FT_WEB_PREFIX + "/"):
            return self._proxy_web()
        # 必须排在通用 /api/ 代理之前，否则会被转发给 Freqtrade（那边没有这个路径）
        if self.path.startswith("/api/ws-token"):
            return self._ws_token()
        if self.path.startswith("/run_progress.json"):
            return self._proxy()
        if self.path.startswith("/api/"):
            return self._proxy()
        return self._send(404, {"detail": "Not Found"})

    # ---------- WebSocket 凭据 ----------
    def _ws_token(self):
        """
        下发实盘 bot 的 WebSocket 订阅地址（含 ws_token）。

        为什么必须这样做：本服务基于 BaseHTTPRequestHandler，只能处理普通 HTTP，
        无法完成 WebSocket 的 Upgrade 握手，所以浏览器没法通过这里转发 WS。
        而 Freqtrade 的 /api/v1/message/ws 用配置里的 ws_token 鉴权，
        于是由本服务在**确认登录态之后**把这个地址发给前端，浏览器直连 8889。

        安全边界：下发的是只读消息流凭据（白名单、成交/撤单回报、新 K 线、保护锁触发），
        内容不超出前端已通过 REST 能读到的范围；账号密码依旧不出服务端。
        """
        if not verify_token(self._bearer()):
            return self._send(401, {"detail": "未登录或登录已过期"})
        cfg = _load_json(FT_CONFIG, {})
        api = cfg.get("api_server", {})
        token = api.get("ws_token") or ""
        host = api.get("listen_ip_address", "127.0.0.1")
        try:
            port = int(api.get("listen_port", 8889))
        except (TypeError, ValueError):
            port = 8889
        return self._send(200, {
            "available": bool(token),
            "ws_url": (
                f"ws://{host}:{port}/api/v1/message/ws?token={token}" if token else ""
            ),
        })

    # ---------- 回测明细（含每笔交易的决策依据） ----------

    def _models_dir(self):
        return os.path.join(ROOT, "bot", "user_data", "models")

    def _local_backtest_list(self):
        if not verify_token(self._bearer()):
            return self._send(401, {"detail": "未登录或登录已过期"})
        items = []
        for d in sorted(glob.glob(os.path.join(self._models_dir(), "*"))):
            f = os.path.join(d, "backtest_detail.json")
            if not os.path.exists(f):
                continue
            try:
                with open(f, encoding="utf-8") as fh:
                    j = json.load(fh)
                items.append({
                    "identifier": j.get("identifier"),
                    "strategy": j.get("strategy"),
                    "summary": j.get("summary"),
                })
            except Exception:
                continue
        items.sort(key=lambda x: (x["summary"] or {}).get("profit_pct") or -999, reverse=True)
        return self._send(200, {"items": items})

    def _local_backtest_detail(self):
        if not verify_token(self._bearer()):
            return self._send(401, {"detail": "未登录或登录已过期"})
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        ident = (qs.get("identifier") or [""])[0].strip()
        if not ident or "/" in ident or ".." in ident:
            return self._send(400, {"detail": "identifier 非法"})
        f = os.path.join(self._models_dir(), ident, "backtest_detail.json")
        if not os.path.exists(f):
            return self._send(404, {"detail": f"未找到 {ident} 的交易明细"})
        with open(f, encoding="utf-8") as fh:
            data = fh.read().encode()
        return self._send(200, raw=data, ctype="application/json; charset=utf-8")

    # ---------- 策略研究汇总（模型对比 + 路线结论 + Carry 回测） ----------
    def _local_research(self):
        if not verify_token(self._bearer()):
            return self._send(401, {"detail": "未登录或登录已过期"})
        f = os.path.join(ROOT, "bot", "user_data", "research_summary.json")
        if not os.path.exists(f):
            return self._send(404, {
                "detail": "尚未生成研究汇总，请先运行 bot/build_research.py",
            })
        with open(f, encoding="utf-8") as fh:
            data = fh.read().encode()
        return self._send(200, raw=data, ctype="application/json; charset=utf-8")

    # ---------- 实盘运维状态（策略信号 / 波动率中枢 / 因子健康度） ----------
    def _local_ops(self):
        if not verify_token(self._bearer()):
            return self._send(401, {"detail": "未登录或登录已过期"})
        f = os.path.join(ROOT, "bot", "user_data", "ops_status.json")
        if not os.path.exists(f):
            return self._send(404, {
                "detail": "尚无运维快照，请先运行 bot/monitor.py",
            })
        with open(f, encoding="utf-8") as fh:
            data = fh.read().encode()
        return self._send(200, raw=data, ctype="application/json; charset=utf-8")

    # ---------- 模型迭代结果（走查四变量对比） ----------
    def _local_iteration(self):
        if not verify_token(self._bearer()):
            return self._send(401, {"detail": "未登录或登录已过期"})
        f = os.path.join(ROOT, "bot", "user_data", "iteration_summary.json")
        if not os.path.exists(f):
            return self._send(404, {
                "detail": "尚无迭代汇总，请先运行 bot/build_iteration.py",
            })
        with open(f, encoding="utf-8") as fh:
            data = fh.read().encode()
        return self._send(200, raw=data, ctype="application/json; charset=utf-8")

    def _local_ml(self):
        """深度学习迭代状态汇总

        全部字段由文件推导：轮次取模型注册表，逐窗口 IC/t 取 ml_seq_results.jsonl，
        通过组数取 ml_regime_results.jsonl（基率中性化后 t>2）。
        只有口径说明类文案（ML_FRAMING / ML_RULES）是常量。
        """
        if not verify_token(self._bearer()):
            return self._send(401, {"detail": "未登录或登录已过期"})
        base = os.path.join(ROOT, "bot", "user_data")

        def tail(name, n=5):
            f = os.path.join(base, name)
            if not os.path.exists(f):
                return []
            out = []
            with open(f, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        try:
                            out.append(json.loads(line))
                        except Exception:
                            pass
            return out[-n:]

        exp = tail("ml_experiments.jsonl", 8)
        seq = tail("ml_seq_results.jsonl", 1)
        seedens = tail("ml_seed_ensemble.jsonl", 1)
        ic = tail("ml_ic_results.jsonl", 3)
        regime = tail("ml_regime_results.jsonl", 2)
        oosr = tail("ml_oos_results.jsonl", 3)

        latest_ic = ic[-1] if ic else {}
        seq_results = seq[-1].get("results", {}) if seq else {}

        # 逐窗口口径下表现最好的序列模型：按 t 值选，不写死 LSTM / t=3.02
        def _by_t(item):
            v = (item[1] or {}).get("t_period")
            return v if v is not None else float("-inf")

        best_seq = max(seq_results.items(), key=_by_t) if seq_results else None
        best_name, best_m = (best_seq[0], best_seq[1] or {}) if best_seq else (None, {})
        wf_t = best_m.get("t_period")
        wf_ic = best_m.get("ic_period")
        pos_w = best_m.get("pos_windows")
        n_w = best_m.get("n_windows")

        # 当前轮次：模型注册表是唯一权威来源（UI 的「模型版本」也读它）
        rounds = None
        try:
            reg_data = self._model_registry().load()
            rounds = max(
                (v.get("round") for v in reg_data.get("versions", [])
                 if v.get("layer") == "ml_model" and v.get("round")),
                default=None,
            )
        except Exception:
            rounds = None
        if rounds is None:
            rounds = (seq[-1].get("round") if seq else None) or (
                regime[-1].get("round") if regime else None
            )

        groups = regime[-1].get("groups", []) if regime else []
        # 「通过」= 基率中性化后 t>2（与 ml_regime.py 的判定口径一致）
        regime_ic_pass = [g for g in groups if (g.get("t") or 0) > 2]
        regime_passed = [g for g in groups if (g.get("n_t") or 0) > 2]

        if wf_t is None:
            verdict = "暂无逐窗口走查结果，先运行 bot/ml_seq.py"
        elif wf_t > 2 and n_w and pos_w is not None and pos_w / n_w >= 0.6:
            verdict = f"逐窗口 IC 显著（t={wf_t:.2f}，正窗口 {pos_w}/{n_w}），继续做样本外确认"
        elif wf_t > 2:
            verdict = f"t={wf_t:.2f} 显著，但正窗口占比偏低，先补齐窗口稳定性"
        else:
            verdict = f"尚无稳定样本外信号（t={wf_t:.2f}），继续迭代"

        seq_note = "暂无序列模型结果"
        if best_name:
            seq_note = f"序列模型（{best_name.upper()}）逐窗口 IC t={wf_t:.2f}"
            if n_w:
                seq_note += f"，正窗口 {pos_w}/{n_w}={pos_w / n_w * 100:.0f}%"

        # ── 模型自动迭代进度（第 24/25 轮新增：新判据 + 多种子）──
        iter_prog = None
        try:
            fp = os.path.join(base, "research_progress.json")
            if os.path.exists(fp):
                with open(fp, encoding="utf-8") as fh:
                    iter_prog = json.load(fh)
        except Exception:
            pass
        try:
            trials = tail("research_trials.jsonl", 400)
        except Exception:
            trials = []
        # 归一化每条 trial 的判定依据
        norm = []
        for t in trials:
            c = t.get("config") or {}
            norm.append({
                "kind": c.get("kind"), "seq_len": c.get("seq_len"),
                "hidden": c.get("hidden"), "layers": c.get("layers"),
                "dropout": c.get("dropout"), "lr": c.get("lr"),
                "n_seeds": t.get("n_seeds"),
                "ic": t.get("ic_period"), "t_quarter": t.get("t_quarter"),
                "t_month": t.get("t_month"), "t_half": t.get("t_half"),
                "robust": t.get("robust"),
                "robust_pass": t.get("robust_pass"),
                "robust_total": t.get("robust_total"),
                "q": t.get("q_value"),
                "passed": bool(t.get("passed")),
                "at": t.get("run_id"),
            })
        passing = [x for x in norm if x["passed"]]
        try:
            daemon = None
            fp = os.path.join(base, "auto_research_daemon.json")
            if os.path.exists(fp):
                with open(fp, encoding="utf-8") as fh:
                    daemon = json.load(fh)
        except Exception:
            daemon = None

        payload = {
            "iteration": {
                "progress": iter_prog,
                "daemon": daemon,
                "criteria": ("逐窗口 IC 的 t > 2（季度口径）且 FDR 校正后 q < 0.05 "
                             "且 多窗口宽度稳健（月度/季度/半年三种宽度下 t 都 > 2）"),
                "n_trials": len(norm),
                "n_passing": len(passing),
                "passing": passing[-20:],
                "recent": norm[-40:],
            },
            "rounds": rounds,
            "experiments": len(tail("ml_experiments.jsonl", 10_000)),
            "best_auc": max((e.get("auc", 0) for e in exp), default=None),
            "ic_models": latest_ic.get("results", []),
            "oracle_ic": latest_ic.get("oracle_ic"),
            "regime_groups": groups,
            "regime_ic_t_gt2": len(regime_ic_pass),
            "regime_neutral_t_gt2": len(regime_passed),
            "regime_threshold": 2.0,
            "oos": oosr[-1].get("results", []) if oosr else [],
            "seq": seq_results,
            "seed_ensemble": (seedens[-1] if seedens else None),
            "seq_meta": ({"seq_len": seq[-1].get("seq_len"),
                          "n_feat": seq[-1].get("n_feat"),
                          "n": (seq_results.get("lstm") or {}).get("n")}
                         if seq else {}),
            "conclusion": {
                "framing": ML_FRAMING,
                "walkforward_ic": round(float(wf_ic), 4) if wf_ic is not None else None,
                "walkforward_ic_t": round(float(wf_t), 2) if wf_t is not None else None,
                "walkforward_model": best_name,
                "seq_note": seq_note,
                "verdict": verdict,
                "rules": ML_RULES,
            },
        }
        return self._send(200, payload)

    def _local_research_status(self):
        """
        策略研究数据的刷新状态。

        为什么需要这个接口：build_research.py 原先是纯手工脚本，没有任何调度，
        面板就长期停在旧数据上且看不出原因。现在由 bot/refresh_research.py
        定期重建，这里把「最近一次刷新的成败 + 当前 summary 的生成时间」暴露给页面。
        """
        if not verify_token(self._bearer()):
            return self._send(401, {"detail": "未登录或登录已过期"})
        out = {"attempt_at": None, "ok": None, "error": None,
               "duration_s": None, "summary_generated_at": None}
        sf = os.path.join(ROOT, "bot", "user_data", "research_refresh.json")
        if os.path.exists(sf):
            try:
                with open(sf, encoding="utf-8") as fh:
                    rec = json.load(fh)
                for k in out:
                    if k in rec:
                        out[k] = rec[k]
            except Exception:
                pass
        # summary 自身的生成时间是权威值（状态文件可能被删）
        mf = os.path.join(ROOT, "bot", "user_data", "research_summary.json")
        if os.path.exists(mf):
            try:
                with open(mf, encoding="utf-8") as fh:
                    out["summary_generated_at"] = json.load(fh).get("generated_at")
                out["summary_mtime"] = int(os.path.getmtime(mf))
            except Exception:
                pass
        out["refresh_running"] = bool(
            os.popen("pgrep -f 'refresh_research.py --daemon' 2>/dev/null").read().strip()
        )
        return self._send(200, out)

    def _local_json(self, name):
        """透传 bot/user_data 下的某个 json"""
        if not verify_token(self._bearer()):
            return self._send(401, {"detail": "未登录或登录已过期"})
        f = os.path.join(ROOT, "bot", "user_data", name)
        if not os.path.exists(f):
            return self._send(404, {"detail": f"缺少 {name}"})
        with open(f, encoding="utf-8") as fh:
            data = fh.read().encode()
        return self._send(200, raw=data, ctype="application/json; charset=utf-8")

    # ---------- 模型版本注册表（champion / challenger） ----------
    def _model_registry(self):
        """惰性加载 bot/model_registry.py —— 与命令行共用同一份注册表"""
        bot = os.path.join(ROOT, "bot")
        if bot not in sys.path:
            sys.path.insert(0, bot)
        import model_registry as mr

        return mr

    def _auto_research_running(self):
        try:
            out = os.popen("pgrep -f 'auto_research.py' 2>/dev/null").read().strip()
        except Exception:
            return None
        if not out:
            return None
        pid = out.split()[0]
        return {"pid": int(pid) if pid.isdigit() else pid}

    def _trial_stats(self):
        """自动迭代账本统计（全部 trial，含失败 —— 防选择性报告）"""
        st = {"total": 0, "runs": 0, "best": None, "recent": []}
        f = os.path.join(ROOT, "bot", "user_data", "research_trials.jsonl")
        if not os.path.exists(f):
            return st
        rows = []
        with open(f, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    pass
        st["total"] = len(rows)
        st["runs"] = len({r.get("run_id") for r in rows if r.get("run_id")})
        ok = [r for r in rows if r.get("t_period") is not None]
        if ok:
            best = max(ok, key=lambda r: r.get("t_period") or -9e9)
            st["best"] = {k: best.get(k) for k in (
                "config", "ic_period", "t_period", "pos_windows", "n_windows",
                "q_value", "passed", "run_id")}
        keep = ("config", "ic_period", "t_period", "pos_windows", "n_windows",
                "q_value", "passed", "phase")
        st["recent"] = [{k: r.get(k) for k in keep} for r in rows[-8:]][::-1]
        return st

    def _local_services(self):
        """
        整个程序的运行状况（供「总览」页展示）

        与 start.sh status 同源：有端口的按端口探活，无常驻端口的按进程名匹配。
        再附带各服务的关键状态文件，让总览一眼看出「谁在跑、跑到哪」。
        """
        if not verify_token(self._bearer()):
            return self._send(401, {"detail": "未登录或登录已过期"})
        import subprocess

        def port_pid(port):
            try:
                out = subprocess.run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
                                     capture_output=True, text=True, timeout=5)
                return out.stdout.strip().split("\n")[0] or None
            except Exception:
                return None

        def proc_pid(pat):
            try:
                out = subprocess.run(["pgrep", "-f", pat], capture_output=True,
                                     text=True, timeout=5)
                return out.stdout.strip().split("\n")[0] or None
            except Exception:
                return None

        def read_json(name):
            f = os.path.join(ROOT, "bot", "user_data", name)
            if not os.path.exists(f):
                return None
            try:
                with open(f, encoding="utf-8") as fh:
                    return json.load(fh)
            except Exception:
                return None

        # 端口服务
        ports = [(8888, "前端 Vben", "web"), (8890, "认证服务", "auth"),
                 (8889, "Freqtrade", "freqtrade"), (8891, "研究服务", "research_api")]
        out = []
        for port, name, key in ports:
            pid = port_pid(port)
            out.append({"key": key, "name": name, "port": port,
                        "running": bool(pid), "pid": pid})

        # 进程服务
        procs = [
            ("monitor.py --daemon", "运维监控", "monitor", None),
            ("auto_iterate.py --daemon", "自动迭代", "auto_iterate", "auto_iterate_status.json"),
            ("refresh_research.py --daemon", "研究刷新", "refresh_research", "research_refresh.json"),
            ("auto_research_daemon.py", "模型迭代", "auto_research", "auto_research_daemon.json"),
        ]
        for pat, name, key, sf in procs:
            pid = proc_pid(pat)
            item = {"key": key, "name": name, "port": None,
                    "running": bool(pid), "pid": pid}
            if sf:
                item["state"] = read_json(sf)
            out.append(item)

        # 按需运行的研究轮次
        rp = proc_pid("auto_research.py --max-trials")
        prog = read_json("research_progress.json")
        out.append({"key": "auto_research_run", "name": "模型研究轮次", "port": None,
                    "running": bool(rp), "pid": rp, "state": prog})

        n_run = sum(1 for x in out if x["running"])
        return self._send(200, {
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "running": n_run,
            "total": len(out),
            "services": out,
        })

    def _local_research_progress(self):
        """
        模型自动迭代任务的实时进度（供「回测与任务」页轮询）
        结构对齐 run_backtest_task.py 的 run_progress.json，便于复用同一套卡片。
        """
        if not verify_token(self._bearer()):
            return self._send(401, {"detail": "未登录或登录已过期"})
        f = os.path.join(ROOT, "bot", "user_data", "research_progress.json")
        prog = None
        if os.path.exists(f):
            try:
                with open(f, encoding="utf-8") as fh:
                    prog = json.load(fh)
            except Exception:
                prog = None
        return self._send(200, {"progress": prog, "trials": self._trial_stats()})

    def _local_model_versions(self):
        """
        版本管理：正在使用的版本（champion）vs 最新迭代的版本（challenger）
        只读接口 —— 上线必须走 POST /api/locals/model_versions/promote（人工填理由）
        """
        if not verify_token(self._bearer()):
            return self._send(401, {"detail": "未登录或登录已过期"})
        try:
            mr = self._model_registry()
        except Exception as e:
            return self._send(500, {"detail": f"加载注册表失败: {e}"})
        reg = mr.load()
        out = {"updated": reg.get("updated"), "layers": {}}
        for layer in ("live_strategy", "ml_model"):
            vs = [v for v in reg["versions"] if v.get("layer") == layer]
            cid = reg["champions"].get(layer)
            out["layers"][layer] = {
                "champion": next((v for v in vs if v["id"] == cid), None),
                "challengers": [v for v in vs if v.get("status") == "challenger"],
                "archived": [v for v in vs if v.get("status") == "archived"],
            }
        out["trials"] = self._trial_stats()
        out["running"] = self._auto_research_running()
        return self._send(200, out)

    def _local_promote_version(self):
        """人工上线闸门：设为「正在使用」。必须填理由；未达标需显式确认。"""
        user = verify_token(self._bearer())
        if not user:
            return self._send(401, {"detail": "未登录或登录已过期"})
        body = self._json_body()
        vid = str(body.get("id") or "").strip()
        note = str(body.get("note") or "").strip()
        if not vid or len(note) < 4:
            return self._send(400, {"detail": "缺少版本 id，或上线理由过短（至少 4 个字）"})
        try:
            mr = self._model_registry()
        except Exception as e:
            return self._send(500, {"detail": f"加载注册表失败: {e}"})
        reg = mr.load()
        entry = mr.get(reg, vid)
        if entry is None:
            return self._send(404, {"detail": f"版本不存在: {vid}"})
        passed = bool((entry.get("gate") or {}).get("passed"))
        if not passed and not body.get("confirm_unpassed"):
            return self._send(409, {
                "detail": "该版本未通过冻结判据，需显式确认后才能设为正在使用",
                "gate": entry.get("gate"),
            })
        mr.set_champion(reg, vid, f"{note}（操作人 {user}）")
        mr.save(reg)
        return self._send(200, {
            "status": "ok", "id": vid, "note": note, "operator": user,
            "gate_passed": passed, "champions": reg["champions"],
            "reminder": "仅更新版本注册表；实盘配置仍需人工修改",
        })

    # ---------- 自动迭代巡检状态 ----------
    def _local_auto_iterate(self):
        if not verify_token(self._bearer()):
            return self._send(401, {"detail": "未登录或登录已过期"})
        cur = os.path.join(ROOT, "bot", "user_data", "auto_iterate_status.json")
        hist = os.path.join(ROOT, "bot", "user_data", "iteration_history.jsonl")
        if not os.path.exists(cur):
            return self._send(404, {"detail": "尚未运行自动迭代，请执行 python auto_iterate.py"})
        with open(cur, encoding="utf-8") as fh:
            data = json.load(fh)
        # 附上历史（最近 20 条摘要）
        if os.path.exists(hist):
            recs = []
            with open(hist, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        try:
                            recs.append(json.loads(line))
                        except Exception:
                            pass
            data["history"] = [
                {"t": r.get("t"), "all_ok": r.get("all_ok"),
                 "failed": r.get("failed", [])}
                for r in recs[-20:]
            ]
        return self._send(200, data)

    # ---------- 实时行情（Binance 公共接口，服务端代理） ----------
    def _local_klines(self):
        """
        实时 K 线。交易对带 `:USDT` 后缀视为永续（fapi），否则现货（api）。
        返回结构与 /locals/ohlcv 完全一致，前端可复用同一套解析；
        失败返回 502，由前端回落到本地 feather。
        """
        if not verify_token(self._bearer()):
            return self._send(401, {"detail": "未登录或登录已过期"})

        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        pair = (qs.get("pair") or ["BTC/USDT:USDT"])[0]
        tf = (qs.get("timeframe") or ["1h"])[0]
        try:
            limit = int((qs.get("limit") or ["200"])[0])
        except ValueError:
            limit = 200
        limit = max(1, min(limit, 1000))

        raw = pair.split(":")[0].strip()
        if "/" not in raw:
            return self._send(400, {"detail": f"无法解析交易对: {pair}"})
        base, quote = raw.split("/", 1)
        sym = f"{base.strip().upper()}{quote.strip().upper()}"
        if tf not in BINANCE_INTERVALS:
            return self._send(400, {"detail": f"不支持的周期: {tf}"})

        market = "futures" if ":" in pair else "spot"
        host = BINANCE_FAPI if market == "futures" else BINANCE_SPOT
        path = "/fapi/v1/klines" if market == "futures" else "/api/v3/klines"
        url = f"{host}{path}?symbol={sym}&interval={tf}&limit={limit}"

        key = (market, sym, tf, limit)
        hit = _kline_cache.get(key)
        if hit and time.time() - hit[0] < KLINE_TTL:
            return self._send(200, hit[1])

        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "quant-terminal/1.0"}
            )
            with urllib.request.urlopen(req, timeout=KLINE_TIMEOUT) as resp:
                rows = json.loads(resp.read().decode())
        except Exception as exc:
            return self._send(502, {
                "detail": f"实时行情不可用（{market} {sym} {tf}）: "
                          f"{type(exc).__name__}: {exc}",
            })

        try:
            data = [[
                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(r[0] / 1000)),
                float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5]),
            ] for r in rows]
        except Exception as exc:
            return self._send(502, {"detail": f"实时行情解析失败: {exc}"})

        payload = {
            "pair": pair,
            "timeframe": tf,
            "columns": ["date", "open", "high", "low", "close", "volume"],
            "data": data,
            "last_candle": data[-1][0] if data else None,
            "source": f"binance-{market}",
            "length": len(data),
        }
        _kline_cache[key] = (time.time(), payload)
        return self._send(200, payload)

    # ---------- 本地行情（读已下载的 feather，不受机器人周期限制） ----------
    def _local_ohlcv(self):
        """
        Freqtrade 的 /pair_candles 只返回「策略自身周期」的数据，
        机器人跑 5m 策略时 1h/4h 都取不到。
        这里直接读本地已下载的历史数据，任意周期都能出图。
        """
        if not verify_token(self._bearer()):
            return self._send(401, {"detail": "未登录或登录已过期"})

        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        pair = (qs.get("pair") or ["BTC/USDT:USDT"])[0]
        tf = (qs.get("timeframe") or ["1h"])[0]
        try:
            limit = int((qs.get("limit") or ["200"])[0])
        except ValueError:
            limit = 200
        limit = max(1, min(limit, 2000))

        symbol = pair.split("/")[0].upper()
        base = os.path.join(ROOT, "bot", "user_data", "data", "binance")
        candidates = [
            os.path.join(base, "futures", f"{symbol}_USDT_USDT-{tf}-futures.feather"),
            os.path.join(base, f"{symbol}_USDT-{tf}.feather"),
        ]
        path = next((p for p in candidates if os.path.exists(p)), None)
        if not path:
            return self._send(404, {
                "detail": f"本地无 {symbol} 的 {tf} 数据，可用 freqtrade download-data 补齐",
            })

        try:
            import pandas as pd  # 延迟导入：缺 pandas 时服务仍可启动
        except Exception:
            return self._send(500, {"detail": "服务端缺少 pandas，无法读取本地行情"})

        try:
            df = pd.read_feather(path)
            cols = ["date", "open", "high", "low", "close", "volume"]
            have = [c for c in cols if c in df.columns]
            df = df[have].tail(limit).copy()
            df["date"] = pd.to_datetime(df["date"], utc=True).dt.strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            for c in have:
                if c != "date":
                    df[c] = df[c].astype(float)
            payload = {
                "pair": pair,
                "timeframe": tf,
                "columns": have,
                "data": df.values.tolist(),
                "source": os.path.basename(path),
                "length": len(df),
            }
            return self._send(200, payload)
        except Exception as exc:
            return self._send(500, {"detail": f"读取本地行情失败: {exc}"})

    def do_POST(self):
        self._read_body()
        if self.path.startswith("/auth/"):
            return self._auth_post()
        if self.path.startswith("/api/locals/model_versions/promote"):
            return self._local_promote_version()
        if self.path.startswith(FT_WEB_PREFIX + "/"):
            return self._proxy_web()
        if self.path.startswith("/api/"):
            return self._proxy()
        return self._send(404, {"detail": "Not Found"})

    def do_PUT(self):
        self._read_body()
        if self.path.startswith(FT_WEB_PREFIX + "/"):
            return self._proxy_web()
        if self.path.startswith("/api/"):
            return self._proxy()
        return self._send(404, {})

    def do_PATCH(self):
        """
        PATCH 也必须转发。

        BaseHTTPRequestHandler 只认它定义过的 do_XXX，缺 do_PATCH 时
        请求会直接得到 501，且响应体不是 JSON —— 前端「编辑回测备注」
        （PATCH /backtest/history/{file}）就是这么静默失效的。
        """
        self._read_body()
        if self.path.startswith(FT_WEB_PREFIX + "/"):
            return self._proxy_web()
        if self.path.startswith("/api/"):
            return self._proxy()
        return self._send(404, {})

    def do_DELETE(self):
        self._read_body()
        if self.path.startswith(FT_WEB_PREFIX + "/"):
            return self._proxy_web()
        if self.path.startswith("/api/"):
            return self._proxy()
        return self._send(404, {})

    # ---------- /auth ----------
    def _auth_get(self):
        path = self.path.split("?")[0]
        if path == "/auth/health":
            return self._send(200, {
                "status": "ok",
                "uptime_s": int(time.time() - START_TS),
                "freqtrade_reachable": bool(ft_token()),
                # 回测/下载/分析类功能依赖这个实例；不可达时前端应给出提示
                "webserver_reachable": bool(ft_web_token()),
            })
        if path == "/auth/me":
            user = verify_token(self._bearer())
            if not user:
                return self._send(401, {"detail": "未登录或登录已过期"})
            return self._send(200, {"username": user})
        return self._send(404, {"detail": "Not Found"})

    def _auth_post(self):
        path = self.path.split("?")[0]
        if path == "/auth/login":
            return self._login()
        if path == "/auth/auto-login":
            return self._auto_login()
        if path == "/auth/change-password":
            return self._change_password()
        if path == "/auth/reset-password":
            return self._reset_password()
        if path == "/auth/logout":
            return self._send(200, {"status": "ok"})
        return self._send(404, {"detail": "Not Found"})

    def _auto_login(self):
        """
        本机自动登录：不需要密码，仅允许从 127.0.0.1 / ::1 发起。
        存在的意义是让本地前端开箱即用，且不受密码变更影响。
        """
        if not self._is_local():
            return self._send(403, {"detail": "自动登录仅允许从本机操作"})
        data = load_users()
        username = DEFAULT_USER
        if username not in data["users"]:
            return self._send(404, {"detail": f"用户 {username} 不存在"})
        return self._send(200, {
            "access_token": make_token(username),
            "auto": True,
            "username": username,
        })

    def _login(self):
        body = self._json_body()
        username = (body.get("username") or "").strip()
        password = body.get("password") or ""
        users = load_users()["users"]
        rec = users.get(username)
        if not rec or not verify_password(password, rec["salt"], rec["hash"]):
            return self._send(401, {"detail": "用户名或密码错误"})
        return self._send(200, {"access_token": make_token(username), "username": username})

    def _change_password(self):
        user = verify_token(self._bearer())
        if not user:
            return self._send(401, {"detail": "未登录或登录已过期"})
        body = self._json_body()
        old = body.get("old_password") or ""
        new = body.get("new_password") or ""
        if len(new) < 6:
            return self._send(400, {"detail": "新密码至少 6 位"})
        if new == old:
            return self._send(400, {"detail": "新密码不能与旧密码相同"})
        data = load_users()
        rec = data["users"].get(user)
        if not rec or not verify_password(old, rec["salt"], rec["hash"]):
            return self._send(400, {"detail": "当前密码不正确"})
        salt, h = hash_password(new)
        data["users"][user] = {
            "salt": salt,
            "hash": h,
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        save_users(data)
        return self._send(200, {"status": "ok", "message": "密码已修改，请重新登录"})

    def _reset_password(self):
        # 重置不需要旧密码，因此严格限制只能从本机发起
        if not self._is_local():
            return self._send(403, {"detail": "重置密码仅允许从本机操作"})
        body = self._json_body()
        username = (body.get("username") or DEFAULT_USER).strip()
        new = (body.get("new_password") or "").strip()
        generated = False
        if not new:
            new = secrets.token_urlsafe(9)
            generated = True
        if len(new) < 6:
            return self._send(400, {"detail": "新密码至少 6 位"})
        data = load_users()
        if username not in data["users"]:
            return self._send(404, {"detail": f"用户 {username} 不存在"})
        salt, h = hash_password(new)
        data["users"][username] = {
            "salt": salt,
            "hash": h,
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        save_users(data)
        print(f"[auth] 已重置 {username} 的密码")
        return self._send(200, {
            "status": "ok",
            "username": username,
            "password": new,
            "generated": generated,
        })

    # ---------- 代理 ----------
    def _proxy(self):
        """实盘 bot（8889）：/api/v1/* 原样转发"""
        return self._proxy_upstream(FT_BASE, ft_token, "Freqtrade（8889）")

    def _proxy_web(self):
        """
        webserver 实例（8891）：/api/web/v1/* → 8891 的 /api/v1/*

        前端不需要知道 8891 的存在，也不需要持有它的凭据 ——
        令牌由本服务代持，和实盘那条链路完全一致。
        """
        return self._proxy_upstream(
            FT_WEB_BASE, ft_web_token, "Freqtrade webserver（8891）", FT_WEB_PREFIX
        )

    def _proxy_upstream(self, base, token_fn, label, strip_prefix=""):
        user = verify_token(self._bearer())
        if not user:
            return self._send(401, {"detail": "未登录或登录已过期"})

        token = token_fn()
        if not token:
            return self._send(502, {"detail": f"无法连接 {label}，请确认服务已启动"})

        path = self.path
        if strip_prefix and path.startswith(strip_prefix):
            rest = path[len(strip_prefix) :]
            if not rest.startswith("/"):
                rest = "/" + rest
            url = base + "/api" + rest
        else:
            url = base + path
        payload = self._read_body() or None

        def call(tok):
            headers = {"Authorization": f"Bearer {tok}"}
            ct = self.headers.get("Content-Type")
            if ct:
                headers["Content-Type"] = ct
            req = urllib.request.Request(url, data=payload, method=self.command, headers=headers)
            return urllib.request.urlopen(req, timeout=30)

        try:
            try:
                resp = call(token)
            except urllib.error.HTTPError as he:
                if he.code == 401:  # token 过期，刷新一次
                    token2 = token_fn(force=True)
                    if not token2:
                        raise
                    resp = call(token2)
                else:
                    raise
            with resp:
                data = resp.read()
                ctype = resp.headers.get("Content-Type", "application/json; charset=utf-8")
            return self._send(200, raw=data, ctype=ctype)
        except urllib.error.HTTPError as he:
            try:
                data = he.read()
            except Exception:
                data = json.dumps({"detail": str(he)}).encode()
            return self._send(he.code, raw=data, ctype="application/json; charset=utf-8")
        except Exception as exc:
            return self._send(502, {"detail": f"代理请求失败: {exc}"})


def main():
    global FT_BASE, FT_WEB_BASE
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8890)
    ap.add_argument("--bind", default="127.0.0.1")
    ap.add_argument("--freqtrade", default=FT_BASE)
    ap.add_argument("--freqtrade-web", default=FT_WEB_BASE)
    args = ap.parse_args()
    FT_BASE = args.freqtrade.rstrip("/")
    FT_WEB_BASE = args.freqtrade_web.rstrip("/")

    load_users()

    srv = ThreadingHTTPServer((args.bind, args.port), Handler)
    print(f"认证与代理服务已启动: http://{args.bind}:{args.port}")
    print(f"  用户库: {USERS_FILE}")
    print(f"  代理至（实盘）: {FT_BASE}")
    print(f"  代理至（webserver）: {FT_WEB_BASE}  ← /api/web/ 前缀")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
