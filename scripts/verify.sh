#!/usr/bin/env bash
# 本地一键验证 —— 替代 GitHub Actions（额度不可用时的等价入口）。
#
# 为什么有它：desktop-windows.yml 已改成只手动触发（不再随 push 自动跑），
# 因为账号没有可用的 Actions 额度。CI 那套步骤不能凭空消失，
# 否则「有没有退化」就完全没判据了。这里把同样的检查搬到本地。
#
# 用法：
#   ./scripts/verify.sh            # 全量：后端全量 pytest + 前端全量 vitest + typecheck
#   ./scripts/verify.sh --quick    # 快速：只跑改动相关的后端测试 + 前端全量 + typecheck
#
# 退出码非 0 表示有检查失败（可安全用作提交前的门禁）。

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PY="./.venv/Scripts/python.exe"
[ -x "$PY" ] || PY="./.venv/bin/python"
if [ ! -x "$PY" ]; then
  echo "找不到仓库根的 .venv（$PY）。按 AGENTS.md：必须用仓库根的 .venv，PATH 上的 python 是 Anaconda。" >&2
  exit 2
fi

QUICK=0
for a in "$@"; do
  case "$a" in
    --quick) QUICK=1 ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    *) echo "未知参数：$a" >&2; exit 2 ;;
  esac
done

FAILED=()
step() { printf '\n\033[1m== %s ==\033[0m\n' "$1"; }

# ---------------------------------------------------------------- 后端
step "后端 pytest（$([ $QUICK -eq 1 ] && echo 定向子集 || echo 全量)）"
if [ $QUICK -eq 1 ]; then
  # 只跑与当前工作区改动相关的测试文件；没有改动就跳过（不假装跑过）
  CHANGED=$(git diff --name-only HEAD 2>/dev/null | grep -E '^ee/(ee|tests)/.*\.py$' || true)
  TESTS=""
  for f in $CHANGED; do
    case "$f" in
      ee/tests/*) TESTS="$TESTS $f" ;;
      ee/ee/*)
        mod=$(basename "$f" .py)
        cand="ee/tests/test_${mod}.py"
        [ -f "$cand" ] && TESTS="$TESTS $cand"
        ;;
    esac
  done
  # shellcheck disable=SC2086
  if [ -n "${TESTS// /}" ]; then
    $PY -m pytest -c ee/pyproject.toml $TESTS -q || FAILED+=("后端 pytest（定向）")
  else
    echo "（没有相关的后端改动，跳过；用全量模式可强制跑）"
  fi
else
  $PY -m pytest -c ee/pyproject.toml -q || FAILED+=("后端 pytest（全量）")
fi

# ---------------------------------------------------------------- 前端
step "前端 vitest"
( cd desktop && npm test --silent ) || FAILED+=("前端 vitest")

step "类型检查"
( cd desktop && npm run typecheck --silent ) || FAILED+=("typecheck")

# ---------------------------------------------------------------- 汇总
printf '\n'
if [ ${#FAILED[@]} -eq 0 ]; then
  printf '\033[32m全部通过\033[0m\n'
  exit 0
fi
printf '\033[31m失败项：\033[0m\n'
for f in "${FAILED[@]}"; do printf '  - %s\n' "$f"; done
exit 1
