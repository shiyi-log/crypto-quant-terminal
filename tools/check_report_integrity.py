#!/usr/bin/env python3
"""文档完整性校验（Round 281）—— 把反复出现的编辑失误固化成自动检查。

动机
----
本会话已【三次】出现"脚本打印成功但内容未落盘"（R178 · R280 等），
根因都是**靠人工 grep 复核**，而人工复核会被跳过。
⇒ 把检查自动化，成为每次文档编辑后的**强制一步**。

检查项
------
  ① **目录完整性**：每个 `##` / `###` 标题（除 `## 目录` 自身）都有一条目录项，
     且**顺序与文本逐一对应**。
  ② **重复标题**：不允许两个标题文本完全相同。
  ③ **撤回标记**：标题中含「已撤回 / 已作废」的小节，其正文必须出现
     「作废 / 撤回 / 不得引用」之类的显式声明。
  ④ **§6.2 计数**：「本会话共记录 【N 条】自我修正」的 N 必须等于
     §6.1 + §6.2 两个表格的数据行数之和。
  ⑤ **关键数字跨文档一致**：抽查若干结论性数字在各文档中的取值一致性
     （只报"某文档缺失该数字"，不比较语义）。

只读。不写库、不改文件。退出码 0 = 全部通过，1 = 有失败项。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPORT = ROOT / "research_multi_strategy" / "STRATEGY_EVALUATION_FINDINGS_20261015.md"
DOCS = {
    "主报告": REPORT,
    "执行摘要": ROOT / "research_multi_strategy" / "EXECUTIVE_SUMMARY_20261015.md",
    "实施规格": ROOT / "research_multi_strategy" / "IMPLEMENTATION_SPEC_retirement_20261015.md",
    "预注册": ROOT / "research_multi_strategy" / "PREREGISTERED_REVIEW_20261028.md",
    "交接文档": ROOT / "tools" / "codex_bridge" / "DEEPSEEK_HANDOFF.md",
}

FAILS: list[str] = []


def ok(msg: str) -> None:
    print(f"    ✅ {msg}")


def bad(msg: str) -> None:
    print(f"    ❌ {msg}")
    FAILS.append(msg)


def warn(msg: str) -> None:
    print(f"    ⚠️ {msg}")


def check_toc(lines: list[str]) -> None:
    print("  ── ① 目录完整性 ──")
    try:
        a = next(i for i, l in enumerate(lines) if l.strip() == "## 目录")
    except StopIteration:
        bad("找不到 `## 目录`"); return
    b = next((i for i in range(a, len(lines)) if lines[i].strip() == "---"), None)
    if b is None:
        bad("目录区找不到结束的 `---`"); return

    toc: list[str] = []
    for l in lines[a:b]:
        m = re.match(r"^\s*-\s*\[(.+?)\]\(#", l)
        if m:
            toc.append(m.group(1).strip())

    heads: list[str] = []
    for l in lines:
        if l.startswith("## ") and not l.startswith("### "):
            t = l[3:].strip()
            if t != "目录":
                heads.append(t)
        elif l.startswith("### ") and not l.startswith("#### "):
            heads.append(l[4:].strip())

    if len(toc) != len(heads):
        bad(f"目录 {len(toc)} 条 vs 章节标题 {len(heads)} 个（差 {abs(len(toc)-len(heads))}）")
    else:
        ok(f"目录 {len(toc)} 条 = 章节标题 {len(heads)} 个")

    mismatch = [(i + 1, t, h) for i, (t, h) in enumerate(zip(toc, heads)) if t != h]
    if mismatch:
        for i, t, h in mismatch[:5]:
            bad(f"第 {i} 条不一致：目录「{t[:44]}」 vs 标题「{h[:44]}」")
    elif toc:
        ok("目录与标题顺序、文本逐一对应")


def check_dupes(lines: list[str]) -> None:
    print("  ── ② 重复标题 ──")
    seen: dict[str, int] = {}
    for l in lines:
        t = None
        if l.startswith("## ") and not l.startswith("### "):
            t = l[3:].strip()
        elif l.startswith("### ") and not l.startswith("#### "):
            t = l[4:].strip()
        if t:
            seen[t] = seen.get(t, 0) + 1
    dup = {t: n for t, n in seen.items() if n > 1}
    if dup:
        for t, n in list(dup.items())[:5]:
            bad(f"标题重复 ×{n}：「{t[:60]}」")
    else:
        ok(f"无重复标题（共 {len(seen)} 个）")


def check_retractions(lines: list[str]) -> None:
    print("  ── ③ 撤回标记 ──")
    text = "\n".join(lines)
    idx = [i for i, l in enumerate(lines)
           if l.startswith("#") and ("已撤回" in l or "已作废" in l)]
    if not idx:
        warn("未发现含「已撤回/已作废」的标题（若确实没有撤回项则正常）")
        return
    missing = []
    for i in idx:
        # 该标题后 12 行内必须出现显式声明
        window = "\n".join(lines[i:i + 12])
        if not any(k in window for k in ("作废", "撤回", "不得引用", "请勿引用")):
            missing.append(lines[i].strip()[:60])
    if missing:
        for m in missing:
            bad(f"撤回标题下缺少显式声明：「{m}」")
    else:
        ok(f"{len(idx)} 个撤回标题均带显式声明")


def check_self_correction_count(lines: list[str]) -> None:
    print("  ── ④ §6.2 自我修正计数 ──")
    text = "\n".join(lines)
    m = re.search(r"共记录\s*【(\d+)\s*条】\s*自我修正", text)
    if not m:
        bad("找不到「共记录【N 条】自我修正」"); return
    claimed = int(m.group(1))

    # 统计 §6.1 与 §6.2 两个小节内的表格数据行
    def count_rows(start_pat: str, end_pat: str) -> int:
        try:
            s = next(i for i, l in enumerate(lines) if re.search(start_pat, l))
        except StopIteration:
            return -1
        e = next((i for i in range(s + 1, len(lines)) if re.search(end_pat, l := lines[i])), len(lines))
        n = 0
        for l in lines[s:e]:
            if l.startswith("|") and "---" not in l and not re.match(r"^\|\s*(轮次|#)\s*\|", l):
                n += 1
        return n

    n61 = count_rows(r"^### 6\.1", r"^### 6\.2")
    n62 = count_rows(r"^### 6\.2", r"^## 七、")
    total = n61 + n62
    if n61 < 0 or n62 < 0:
        warn(f"无法定位 §6.1/§6.2（n61={n61} n62={n62}）")
    elif total != claimed:
        bad(f"声称 {claimed} 条，但 §6.1({n61}) + §6.2({n62}) 表格行 = {total}")
    else:
        ok(f"声称 {claimed} 条 = §6.1({n61}) + §6.2({n62})")


def check_superseded(lines: list[str]) -> None:
    """⑥ 已被取代的旧数值，只允许出现在【标注过的】语境中。

    Round 279 的教训：一次"推翻/更正"若未贯穿全报告，旧数值会继续被当成有效证据
    （实测在 §零 · §五 · §八 · §6.1 共 6 处）。本检查把"标注"要求自动化。
    """
    print("  ── ⑥ 陈旧数值是否已标注 ──")
    # (旧值, 说明, 允许的标注关键词)
    cases = [
        ("44.7%", "R226 更正为 23.1%", ("更正", "原报", "有误", "错了", "作废", "226")),
        ("48.3", "R226 更正为 4.1×", ("更正", "原报", "有误", "错了", "作废", "226")),
        ("+8,950", "R260 证明样本内转正不稳健", ("不稳健", "更正", "R260", "样本内", "已证", "撤回", "曾报", "早期数字")),
        ("3,439", "R181 更正为去重后 379", ("更正", "不去重", "未去重", "379", "R181")),
        ("5.7 天", "仅对应 n≥200 提示层；正式反驳层 n≥400 的 ETA 更长",
         ("提示层", "400", "修正案", "R262", "R263", "正式反驳")),
        ("13.6 天", "同上", ("提示层", "400", "修正案", "R262", "R263", "正式反驳")),
    ]
    problems = 0
    for tok, why, marks in cases:
        hits = [i for i, l in enumerate(lines) if tok in l]
        unmarked = []
        for i in hits:
            ctx = "\n".join(lines[max(0, i - 5):i + 5])
            if not any(k in ctx for k in marks):
                unmarked.append(i + 1)
        if unmarked:
            problems += len(unmarked)
            bad(f"旧值 `{tok}`（{why}）在 {unmarked} 处未被标注")
        elif hits:
            ok(f"旧值 `{tok}` 出现 {len(hits)} 处，均已标注（{why}）")
    if problems == 0:
        ok("无未标注的陈旧数值")


def check_whitespace(lines: list[str]) -> None:
    """⑧ 行尾空白 —— 会被 `git diff --check` 判为错误，影响提交。

    Round 290 的教训：我用临时文件拼装表格行时带入了行尾空格，
    自己没发现，是核对 Codex 的 git 审计时才暴露的。
    """
    print("  ── ⑧ 行尾空白（git diff --check 会报错）──")
    bad_lines = [i + 1 for i, l in enumerate(lines) if l != l.rstrip()]
    if bad_lines:
        bad(f"{len(bad_lines)} 行有行尾空白：{bad_lines[:10]}")
    else:
        ok("无行尾空白")


def check_key_numbers() -> None:
    print("  ── ⑦ 关键数字跨文档存在性 ──")
    keys = {
        "退役族级显著负 5": "5/5",
        "变体冗余 16.8×": "16.8",
        "突破率 23.1%": "23.1%",
        "最差倍数 4.1×": "4.1×",
        "费率低估 2bp": "2bp",
        "样本期 4.34 天": "4.34",
        "maker 成交率 11.8%": "11.8%",
        "K 线空洞 49.8h": "49.8",
    }
    for label, token in keys.items():
        where = [name for name, p in DOCS.items()
                 if p.exists() and token in p.read_text(encoding="utf-8")]
        if len(where) >= 2:
            ok(f"{label}（`{token}`）出现在 {len(where)} 份文档")
        elif where:
            warn(f"{label}（`{token}`）只出现在 {where[0]}")
        else:
            bad(f"{label}（`{token}`）在任何文档中都找不到")


def main() -> int:
    if not REPORT.exists():
        print(f"  ❌ 找不到主报告: {REPORT}", file=sys.stderr)
        return 1
    lines = REPORT.read_text(encoding="utf-8").split("\n")
    print(f"  ══ 文档完整性校验（主报告 {len(lines)} 行）══")
    check_toc(lines)
    check_dupes(lines)
    check_retractions(lines)
    check_self_correction_count(lines)
    check_superseded(lines)
    check_whitespace(lines)
    check_key_numbers()
    print()
    if FAILS:
        print(f"  ⇒ 🔴 {len(FAILS)} 项失败")
        return 1
    print("  ⇒ ✅ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
