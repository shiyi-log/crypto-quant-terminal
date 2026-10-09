#!/usr/bin/env bash
#
# codex-bridge 插件卸载脚本
# ============================================================================
# 从 DSH 的 desktop profile 补丁层里，精确删除 install.sh 插入的那个块：
#
#   * 以 "# >>> codex-bridge ..." 与 "# <<< codex-bridge ..." 为界的整块
#   * 以及块内 id: codex-bridge 的 insert 条目
#
# 安全措施：
#   * 删除前先备份为 cordis.patch.yml.bak-codex-bridge-uninstall-<时间戳>
#   * 只删自己的块，其余内容逐字保留
#   * 删除后校验 YAML 仍合法；不合法立刻还原备份
#   * 没装过就什么都不做（幂等）
#
# 用法：
#   bash tools/codex_bridge/dsh_plugin/uninstall.sh
#   DSH_PROFILE_DIR=/tmp/mock-profile bash tools/codex_bridge/dsh_plugin/uninstall.sh
#
set -euo pipefail

PROFILE_DIR="${DSH_PROFILE_DIR:-${HOME}/.dsh/profiles/desktop}"
PATCH_FILE="${PROFILE_DIR}/cordis.patch.yml"
ID="codex-bridge"
BEGIN_MARK="# >>> ${ID} (managed by dsh_plugin/install.sh) >>>"
END_MARK="# <<< ${ID} (managed by dsh_plugin/install.sh) <<<"

die() { printf 'uninstall.sh: 错误：%s\n' "$1" >&2; exit 1; }
info() { printf 'uninstall.sh: %s\n' "$1"; }

# ---------------------------------------------------------------- 前置检查

[ -d "${PROFILE_DIR}" ] || die "profile 目录不存在：${PROFILE_DIR}
  desktop profile 由 DSH 桌面应用创建。请先启动一次 DSH 应用再重试，
  或用 DSH_PROFILE_DIR=<某目录> 指定别的 profile 目录。"
[ -f "${PATCH_FILE}" ] || die "补丁文件不存在：${PATCH_FILE}"

if ! grep -Fq "${BEGIN_MARK}" "${PATCH_FILE}" && ! grep -Fq "id: ${ID}" "${PATCH_FILE}"; then
  info "没有找到 codex-bridge 安装痕迹，无需卸载。"
  exit 0
fi

# ---------------------------------------------------------------- 备份

STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP="${PATCH_FILE}.bak-${ID}-uninstall-${STAMP}"
cp -p "${PATCH_FILE}" "${BACKUP}" || die "备份失败：${BACKUP}"
info "已备份：${BACKUP}"

# ---------------------------------------------------------------- 生成删除后的内容

TMP_NEW="$(mktemp "${PROFILE_DIR}/.cordis.patch.yml.tmp.XXXXXX")"
cleanup_tmp() { rm -f "${TMP_NEW}" 2>/dev/null || true; }
trap cleanup_tmp EXIT

# 用 awk 删除块：优先按标记行成对删除；没有标记时退回"删除包含 id: codex-bridge
# 的那个顶层 insert 条目及其全部缩进子行"。
awk -v begin="${BEGIN_MARK}" -v end="${END_MARK}" -v id="${ID}" '
  # 结算缓冲：目标块丢弃，其他原样输出（先条目、后空行，保持原始顺序）
  function settle() {
    if (pending_has_id) { removed += 1; pending = ""; pending_has_id = 0; blanks = ""; return }
    if (pending != "") { printf "%s", pending; pending = "" }
    if (blanks != "") { printf "%s", blanks; blanks = "" }
  }
  BEGIN { in_marked = 0; pending = ""; pending_has_id = 0; removed = 0; blanks = "" }

  # 1) 标记块：先丢掉块前紧邻的空行（install.sh 会加一个），再整块丢弃
  index($0, begin) == 1 { blanks = ""; settle(); in_marked = 1; next }
  in_marked {
    if (index($0, end) == 1) { in_marked = 0; removed += 1 }
    next
  }

  # 2) 顶层条目：结算上一条，开始缓冲本条
  /^-/ { settle(); pending = $0 ORS; next }

  # 3) 空行单独攒着，settle 时按序输出
  /^[ \t]*$/ { blanks = blanks $0 ORS; next }

  # 4) 条目内部行（无标记时靠 id 判定）
  pending != "" {
    pending = pending $0 ORS
    if ($0 ~ ("^[ \t]+-?[ \t]*id:[ \t]*" id "[ \t]*$")) { pending_has_id = 1 }
    next
  }

  # 5) 条目之外的散行（文件头部注释等）
  { printf "%s", blanks; blanks = ""; printf "%s", $0 ORS }

  END { settle(); if (removed == 0) exit 3 }
' "${PATCH_FILE}" > "${TMP_NEW}" || {
  status=$?
  if [ "${status}" -eq 3 ]; then
    die "找到了 id: ${ID} 的痕迹，但无法定位成完整的补丁块（可能缺 END 标记）；请手动检查 ${PATCH_FILE}（备份：${BACKUP}）"
  fi
  die "生成删除后的内容失败（awk 退出码 ${status}），原文件未改动。"
}

# ---------------------------------------------------------------- 校验

if ! command -v python3 >/dev/null 2>&1; then
  # 纯 shell 兜底：非空 + 有顶层条目
  if ! grep -qE '^[[:space:]]*[^#[:space:]]' "${TMP_NEW}"; then
    die "删除后文件只剩注释/空行（会让 DSH 启动失败），已放弃。备份：${BACKUP}"
  fi
  info "提示：没有 python3，跳过 YAML 解析，仅做非空检查"
elif python3 -c 'import yaml' >/dev/null 2>&1; then
  python3 - "${TMP_NEW}" <<'PY' || die "删除后 YAML 不再合法，已放弃（原文件未改动）。"
import sys, yaml
with open(sys.argv[1], "r", encoding="utf-8") as handle:
    data = yaml.safe_load(handle)
if data is None:
    sys.exit("删除后文件只剩注释/空行（会让 DSH 启动失败）")
if not isinstance(data, list):
    sys.exit("顶层不再是 YAML 数组")
print("YAML 校验：PyYAML 解析通过，剩余 %d 个顶层条目" % len(data))
PY
else
  python3 - "${TMP_NEW}" <<'PY' || die "删除后结构检查失败，已放弃（原文件未改动）。"
import re, sys
with open(sys.argv[1], "r", encoding="utf-8") as handle:
    lines = handle.read().split("\n")
problems, entries, content = [], 0, False
for number, line in enumerate(lines, 1):
    stripped = line.strip()
    if stripped == "" or stripped.startswith("#"):
        continue
    content = True
    if re.match(r"^[ \t]*\t", line):
        problems.append("第 %d 行：缩进含 tab" % number)
    if not line.startswith("-") and not line[0].isspace():
        problems.append("第 %d 行：非法行 %r" % (number, line[:60]))
    if re.match(r"^-(\s|$)", line):
        entries += 1
if not content:
    sys.exit("删除后文件只剩注释/空行")
if entries == 0:
    problems.append("没有剩余的顶层 '-' 条目")
if problems:
    sys.exit("；".join(problems[:8]))
print("YAML 校验：结构化检查通过，剩余 %d 个顶层条目" % entries)
PY
fi

# 确认目标真的没了
if grep -Fq "${BEGIN_MARK}" "${TMP_NEW}" || grep -Fq "id: ${ID}" "${TMP_NEW}"; then
  die "删除后仍能找到 codex-bridge 痕迹，已放弃写入。备份：${BACKUP}"
fi

# ---------------------------------------------------------------- 落盘

ORIG_MODE="$(stat -f '%Lp' "${PATCH_FILE}" 2>/dev/null || stat -c '%a' "${PATCH_FILE}" 2>/dev/null || true)"
if [ -n "${ORIG_MODE}" ]; then chmod "${ORIG_MODE}" "${TMP_NEW}" 2>/dev/null || true; fi

mv -f "${TMP_NEW}" "${PATCH_FILE}"

info "已卸载：从 ${PATCH_FILE} 移除了 codex-bridge 条目"
printf '\n'
printf '  ⚠️  需要重启 DSH 应用才会生效（profile 只在启动时合成）。\n'
printf '     重启后端口 %s 应该不再被监听。\n\n' "8898"
