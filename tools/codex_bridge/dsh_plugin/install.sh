#!/usr/bin/env bash
#
# codex-bridge 插件安装脚本
# ============================================================================
# 把 dsh_plugin/codex-bridge-plugin.mjs 以**绝对路径**插入到 DSH 的 desktop
# profile 补丁层：
#
#   ~/.dsh/profiles/desktop/cordis.patch.yml
#
# 安全措施：
#   * 写入前先备份为 cordis.patch.yml.bak-codex-bridge-<时间戳>
#   * 幂等：已装过就跳过，不重复插入
#   * 写入后校验 YAML 合法；不合法立刻放弃写入（原文件不动）
#   * 绝不覆盖/清空既有内容，只做**追加**
#
# 该文件为空或只剩注释会导致 DSH 启动失败，所以脚本宁可失败也不写坏它。
#
# 用法：
#   bash tools/codex_bridge/dsh_plugin/install.sh
#   # dry-run 到 mock profile（不碰真实配置）：
#   DSH_PROFILE_DIR=/tmp/mock-profile bash tools/codex_bridge/dsh_plugin/install.sh
#
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
PLUGIN_PATH="${SCRIPT_DIR}/codex-bridge-plugin.mjs"
PROFILE_DIR="${DSH_PROFILE_DIR:-${HOME}/.dsh/profiles/desktop}"
PATCH_FILE="${PROFILE_DIR}/cordis.patch.yml"
ID="codex-bridge"
BEGIN_MARK="# >>> ${ID} (managed by dsh_plugin/install.sh) >>>"
END_MARK="# <<< ${ID} (managed by dsh_plugin/install.sh) <<<"

# 可被环境变量覆盖的插件配置（写进 insert 条目的 config:）
BRIDGE_PORT="${BRIDGE_PORT:-8898}"
BRIDGE_WORKSPACE="${BRIDGE_WORKSPACE:-/Users/shiyi/DeepSeek/量化}"
BRIDGE_PERMISSION_PRESET="${BRIDGE_PERMISSION_PRESET:-danger-full-access}"

die() { printf 'install.sh: 错误：%s\n' "$1" >&2; exit 1; }
info() { printf 'install.sh: %s\n' "$1"; }

# ---------------------------------------------------------------- 前置检查

[ -f "${PLUGIN_PATH}" ] || die "找不到插件文件：${PLUGIN_PATH}"
[ -d "${PROFILE_DIR}" ] || die "profile 目录不存在：${PROFILE_DIR}
  desktop profile 由 DSH 桌面应用创建。请先启动一次 DSH 应用再重试，
  或用 DSH_PROFILE_DIR=<某目录> 指定别的 profile 目录。"
[ -f "${PATCH_FILE}" ] || die "补丁文件不存在：${PATCH_FILE}"

case "${PLUGIN_PATH}" in
  /*) ;;
  *) die "插件路径不是绝对路径：${PLUGIN_PATH}" ;;
esac

# 路径/配置里出现单引号会破坏 YAML 单引号字符串，直接拒绝。
case "${PLUGIN_PATH}" in *"'"*) die "插件路径含单引号，无法安全写入 YAML：${PLUGIN_PATH}" ;; esac
case "${BRIDGE_WORKSPACE}" in *"'"*) die "workspace 路径含单引号，无法安全写入 YAML：${BRIDGE_WORKSPACE}" ;; esac
case "${BRIDGE_PORT}" in ''|*[!0-9]*) die "BRIDGE_PORT 必须是数字：${BRIDGE_PORT}" ;; esac

# ---------------------------------------------------------------- 幂等检查

if grep -Fq "id: ${ID}" "${PATCH_FILE}" 2>/dev/null || grep -Fq "${BEGIN_MARK}" "${PATCH_FILE}" 2>/dev/null; then
  info "已经安装过（在 ${PATCH_FILE} 里找到 id: ${ID}），跳过，未做任何改动。"
  info "如需重装：先 bash \"${SCRIPT_DIR}/uninstall.sh\"，再运行本脚本。"
  exit 0
fi

# ---------------------------------------------------------------- 备份

STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP="${PATCH_FILE}.bak-${ID}-${STAMP}"
cp -p "${PATCH_FILE}" "${BACKUP}" || die "备份失败：${BACKUP}"
info "已备份：${BACKUP}"

# ---------------------------------------------------------------- 组装新内容
# 临时文件放在**同一目录**，这样最后的 mv 是同文件系统的原子替换。

TMP_NEW="$(mktemp "${PROFILE_DIR}/.cordis.patch.yml.tmp.XXXXXX")"
cleanup_tmp() { rm -f "${TMP_NEW}" 2>/dev/null || true; }
trap cleanup_tmp EXIT

cp -p "${PATCH_FILE}" "${TMP_NEW}"

# 保证文件末尾有换行，否则新块会和最后一行粘在一起。
if [ -s "${TMP_NEW}" ] && [ -n "$(tail -c 1 "${TMP_NEW}")" ]; then
  printf '\n' >> "${TMP_NEW}"
fi

# 注意：这里用的是**裸 insert**（补丁条目本身不带 id）。
# 实测（dsh --dump-config）：
#   - insert: [...]                       ✅ 生效，插件出现在合成配置里
#   - id: codex-bridge + insert: [...]    ❌ "patch insert: entry "codex-bridge" not found"
# 带 id 的补丁语义是"指向已存在的条目"，而根条目里并没有 codex-bridge。
# DSH 自己的 dsh-base 补丁层同样使用裸 insert。
cat >> "${TMP_NEW}" <<YAML

${BEGIN_MARK}
- insert:
    - id: ${ID}
      name: '${PLUGIN_PATH}'
      config:
        port: ${BRIDGE_PORT}
        workspacePath: '${BRIDGE_WORKSPACE}'
        permissionPreset: '${BRIDGE_PERMISSION_PRESET}'
${END_MARK}
YAML

# ---------------------------------------------------------------- 校验

# 优先用 PyYAML 真解析；不可用时退化为结构化检查（本机两个 python 都没有 PyYAML）。
validate_yaml() {
  local file="$1"

  if command -v python3 >/dev/null 2>&1 && python3 -c 'import yaml' >/dev/null 2>&1; then
    python3 - "${file}" <<'PY'
import sys, yaml
with open(sys.argv[1], "r", encoding="utf-8") as handle:
    data = yaml.safe_load(handle)
if data is None:
    sys.exit("YAML 解析结果为空（只有注释/空行会让 DSH 启动失败）")
if not isinstance(data, list):
    sys.exit("顶层必须是 YAML 数组")
print("YAML 校验：PyYAML 解析通过，%d 个顶层条目" % len(data))
PY
    return $?
  fi

  if command -v python3 >/dev/null 2>&1; then
    info "提示：本机 python3 没有 PyYAML，改用结构化检查"
    python3 - "${file}" <<'PY'
import re, sys

with open(sys.argv[1], "r", encoding="utf-8") as handle:
    lines = handle.read().split("\n")

problems = []
entries = 0
saw_content = False
for number, line in enumerate(lines, 1):
    stripped = line.strip()
    if stripped == "" or stripped.startswith("#"):
        continue
    saw_content = True
    if re.match(r"^[ \t]*\t", line):
        problems.append("第 %d 行：缩进含 tab（YAML 非法）" % number)
    if not line.startswith("-") and not line[0].isspace():
        problems.append("第 %d 行：既不是顶层 '-' 条目也不是缩进行：%r" % (number, line[:60]))
    if re.match(r"^-(\s|$)", line):
        entries += 1

if not saw_content:
    sys.exit("文件只有注释/空行（会让 DSH 启动失败）")
if entries == 0:
    problems.append("没有找到任何顶层 '-' 条目")
if problems:
    sys.exit("；".join(problems[:8]))

print("YAML 校验：结构化检查通过，%d 个顶层条目" % entries)
PY
    return $?
  fi

  # 连 python3 都没有：纯 shell 兜底。
  info "提示：没有 python3，改用纯 shell 结构检查"
  local content=0 entries=0 line
  while IFS= read -r line || [ -n "${line}" ]; do
    case "${line}" in
      ''|'#'*) continue ;;
    esac
    content=$((content + 1))
    case "${line}" in
      $'\t'*) die "缩进行以 tab 开头（YAML 非法）：${line}" ;;
    esac
    case "${line}" in
      -*) entries=$((entries + 1)) ;;
      [[:space:]]*) ;;
      *) die "非法行（既不是顶层 '-' 条目也不是缩进行）：${line}" ;;
    esac
  done < "${file}"
  [ "${content}" -gt 0 ] || die "文件只有注释/空行（会让 DSH 启动失败）"
  [ "${entries}" -gt 0 ] || die "没有找到任何顶层 '-' 条目"
  info "YAML 校验：纯 shell 检查通过，${entries} 个顶层条目"
  return 0
}

info "校验追加后的 YAML ..."
if ! validate_yaml "${TMP_NEW}"; then
  printf 'install.sh: 错误：追加后的 YAML 校验失败，已放弃写入（原文件未改动）。\n' >&2
  printf '           备份仍在：%s\n' "${BACKUP}" >&2
  exit 1
fi

# 再确认新块确实在文件里，防止 heredoc 出意外。
grep -Fq "id: ${ID}" "${TMP_NEW}" || die "写入内容里找不到 id: ${ID}，已放弃"
grep -Fq "${BEGIN_MARK}" "${TMP_NEW}" || die "写入内容里找不到起始标记，已放弃"

# ---------------------------------------------------------------- 落盘（原子替换）

# 保留原文件权限位（macOS 与 GNU 的 stat 参数不同）。
ORIG_MODE="$(stat -f '%Lp' "${PATCH_FILE}" 2>/dev/null || stat -c '%a' "${PATCH_FILE}" 2>/dev/null || true)"
if [ -n "${ORIG_MODE}" ]; then chmod "${ORIG_MODE}" "${TMP_NEW}" 2>/dev/null || true; fi

mv -f "${TMP_NEW}" "${PATCH_FILE}"

info "已写入：${PATCH_FILE}"
info "插入条目 id=${ID} -> ${PLUGIN_PATH}"
info "端口 ${BRIDGE_PORT}（仅 127.0.0.1）、workspace ${BRIDGE_WORKSPACE}、权限预设 ${BRIDGE_PERMISSION_PRESET}"
printf '\n'
printf '  ⚠️  需要重启 DSH 应用才会生效（profile 只在启动时合成，热改不生效）。\n'
printf '     重启后验证：curl -s http://127.0.0.1:%s/health\n\n' "${BRIDGE_PORT}"
