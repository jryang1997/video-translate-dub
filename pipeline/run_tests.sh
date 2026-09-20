#!/bin/zsh
# 一键回归测试（2026-09-19 审计阶段4）：自动给每个测试选对 venv
# 用法: zsh pipeline/run_tests.sh [测试名前缀，如 layout]
set -e
cd "$(dirname "$0")/.."

# 需要合成器闭包（numpy/ffmpeg 全家桶）的测试走 index-tts venv；其余走主 venv
declare -A VENV
VENV[test_layout]=index-tts/.venv
VENV[test_p0_fixes]=index-tts/.venv
VENV[test_tts_reliability]=.venv
VENV[test_render_qc_timing]=.venv
VENV[test_subtitle_timing]=.venv

FILTER="${1:-}"
pass=0; fail=0; failed=()
for t in test_layout test_p0_fixes test_tts_reliability test_render_qc_timing test_subtitle_timing; do
  [[ -n "$FILTER" && "$t" != *"$FILTER"* ]] && continue
  if "${VENV[$t]%/bin*}"/bin/python "pipeline/tests/$t.py" > /tmp/run_tests_$t.log 2>&1; then
    echo "✅ $t"; pass=$((pass+1))
  else
    echo "❌ $t（日志: /tmp/run_tests_$t.log）"; fail=$((fail+1)); failed+=("$t")
  fi
done
echo
if (( fail == 0 )); then
  echo "全部通过（$pass 项）"
else
  echo "失败 $fail 项: ${failed[*]}"
  exit 1
fi
