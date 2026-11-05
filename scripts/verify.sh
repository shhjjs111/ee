#!/usr/bin/env bash
# 本地一键验证 —— 与 GitHub Actions 等价的本地入口。
#
# 定位：CI（.github/workflows/desktop-windows.yml）在 push/PR 上自动跑，是主门禁。
# 这里提供同时可以离线、只跑子集的等价入口：没网、想提交前先自查、
# 或 CI 挂了要本地复现同一批步骤时用它。两边的检查项要同步（test_ci_offline_gate.py 守着 CI 那侧）。
#
# 用法：
#   ./scripts/verify.sh            # 全量：后端 lint + 全量 pytest + 前端 vitest + typecheck
#   ./scripts/verify.sh --quick    # 快速：lint + 只跑改动相关的后端测试 + 前端全量 + typecheck
#   ./scripts/verify.sh --e2e      # 额外跑真实 Electron 的 Playwright（会先 npm run build）
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

# 并行跑 pytest（pytest-xdist）。本机全量 1064s → ~150s（实测两轮 2132 passed）。
# 不写死 -n 8 而按 CPU 算：测试里有 PowerShell 5.1 子进程（单次约 5s），
# 并发太高会把 10s 级的 wait_for 等超时——本机 -n auto（16 worker）实测红 2 条
# （test_server 的 websocket 闭环、test_bgshell_guidance），-n 8 两轮全绿。
# 取 min(8, CPU-1)，留一个核给脚本自己与前端 npm；CI 上 2 核就只用 1 个 worker。
CPUS=$($PY -c "import os; print(os.cpu_count() or 1)" 2>/dev/null || echo 1)
WORKERS=$(( CPUS > 8 ? 8 : (CPUS > 1 ? CPUS - 1 : 1) ))
XDIST="-n $WORKERS"
$PY -c "import xdist" 2>/dev/null || XDIST=""
[ -n "$XDIST" ] || echo "（没装 pytest-xdist，串行跑；uv pip install --python .venv/Scripts/python.exe 'ee[test]' 装上可快 6 倍）"

QUICK=0
E2E=0
for a in "$@"; do
  case "$a" in
    --quick) QUICK=1 ;;
    --e2e) E2E=1 ;;
    -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
    *) echo "未知参数：$a" >&2; exit 2 ;;
  esac
done

FAILED=()
UNMAPPED=""          # --quick：有后端改动却没匹配到任何测试文件的模块（要显式警告，别报假绿）
step() { printf '\n\033[1m== %s ==\033[0m\n' "$1"; }

# ---------------------------------------------------------------- 后端
step "后端 lint（ruff）"
if command -v ruff >/dev/null 2>&1; then
  ruff check ee/ || FAILED+=("后端 lint")
else
  echo "（没装 ruff，跳过：uv tool install ruff，或 pip install ruff）"
fi

step "后端 pytest（$([ $QUICK -eq 1 ] && echo 定向子集 || echo 全量)）"
if [ $QUICK -eq 1 ]; then
  # 只跑与当前工作区改动相关的测试文件；没有改动就跳过（不假装跑过）
  CHANGED=$(git diff --name-only HEAD 2>/dev/null | grep -E '^ee/(ee|tests)/.*\.py$' || true)
  # 未跟踪的新文件也要算：git diff 看不到它们，新模块 + 新测试会被一起漏掉
  CHANGED="$CHANGED $(git ls-files --others --exclude-standard -- 'ee/ee/*.py' 'ee/tests/*.py' 2>/dev/null || true)"
  TESTS=""
  for f in $CHANGED; do
    case "$f" in
      ee/tests/*) TESTS="$TESTS $f" ;;
      ee/ee/*)
        mod=$(basename "$f" .py)
        cand="ee/tests/test_${mod}.py"
        if [ -f "$cand" ]; then
          TESTS="$TESTS $cand"
          continue
        fi
        # 没有同名测试是常态（core/session.py、server/app.py、tools/shell.py 都没有）。
        # 退一步找「直接 import 了这个模块」的测试——否则改最核心的几个文件时
        # 一条后端测试都不跑，脚本却照常打印「全部通过」并以 0 退出。
        dotted=$(printf '%s' "${f#ee/}" | sed 's#/#.#g; s#\.py$##')
        hits=$(grep -rl --include='*.py' -e "from ${dotted} import" -e "^import ${dotted}" ee/tests 2>/dev/null || true)
        if [ -n "$hits" ]; then
          for h in $hits; do TESTS="$TESTS $h"; done
        else
          UNMAPPED="$UNMAPPED $f"
        fi
        ;;
    esac
  done
  TESTS=$(printf '%s\n' $TESTS | sort -u | tr '\n' ' ')
  if [ -n "$UNMAPPED" ]; then
    printf '\033[33m⚠ 这些后端模块没有匹配到任何测试文件，本次 --quick 不覆盖它们：%s\033[0m\n' "$UNMAPPED"
    printf '\033[33m  别把「全部通过」当成它们被验证过的证据；改动面涉及它们时用全量模式。\033[0m\n'
  fi
  # shellcheck disable=SC2086
  if [ -n "${TESTS// /}" ]; then
    $PY -m pytest -c ee/pyproject.toml $TESTS -q $XDIST || FAILED+=("后端 pytest（定向）")
  else
    echo "（没有相关的后端改动，跳过；用全量模式可强制跑）"
  fi
else
  $PY -m pytest -c ee/pyproject.toml -q $XDIST || FAILED+=("后端 pytest（全量）")
fi

# ---------------------------------------------------------------- 前端
step "前端 vitest"
( cd desktop && npm test --silent ) || FAILED+=("前端 vitest")

step "类型检查"
( cd desktop && npm run typecheck --silent ) || FAILED+=("typecheck")

# e2e 不默认跑：要真实 Electron 窗口 + 先构建，且比前几项慢一个量级。
# 它守的是 jsdom 测不出来的东西（布局、滚动链、深色实背景、真实回合闭环），
# 改动界面或 Composer/store 时应当显式带上。
if [ $E2E -eq 1 ]; then
  step "e2e（真实 Electron）"
  ( cd desktop && npm run build --silent && npx playwright test ) || FAILED+=("e2e")
fi

# ---------------------------------------------------------------- 汇总
printf '\n'
if [ ${#FAILED[@]} -eq 0 ]; then
  if [ -n "${UNMAPPED:-}" ]; then
    printf '\033[32m全部通过\033[0m\033[33m（但上面有模块没匹配到测试，后端覆盖不完整）\033[0m\n'
  else
    printf '\033[32m全部通过\033[0m\n'
  fi
  exit 0
fi
printf '\033[31m失败项：\033[0m\n'
for f in "${FAILED[@]}"; do printf '  - %s\n' "$f"; done
exit 1
