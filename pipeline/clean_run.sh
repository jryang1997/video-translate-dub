#!/bin/zsh
# 清理单个 run 的可重算产物，回收磁盘（2026-09-19 审计阶段3：runs/ 曾 20G 无保留契约）
# 用法: zsh pipeline/clean_run.sh <vid> [--deep] [--apply]（两个 flag 顺序随意）
#   默认干跑: 只打印将删除的内容与可回收空间，不做任何改动
#   --deep:   只扩充删除清单——额外含 05_tts/cache（⚠️ 段级缓存基座没了，下次重跑 TTS
#             会全量重新合成约 35-40 分钟/部；只在该视频确定不再改配音参数时用。
#             干跑预览同样可见 --deep 会多删什么）
#   --apply: 唯一的执行开关。任何模式（含 --deep）都必须显式 --apply 才会动文件
#             （2026-09-20 复核修复: 旧版 --deep 单独出现会绕过预览直接 rm -rf）
#
# 保留（永远不动）:
#   01_raw 源视频 / 03_transcript / 04_translate / 05_tts 的 plan.json、report.json、
#   seg_*.wav、cache（除非 --deep）、ref/spk 样本、logs
# 清理（全部可重算）:
#   02_audio          从 01_raw 重抽（prepare）
#   06_mix            demucs 伴奏从 02_audio 重算；混音/响度从 plan 重算
#   07_output         只保留 *_水印.mp4 交付对（配音版+原声版）；无水印母版可由
#                     relayout-only + mix_render 几分钟重渲，不再囤多轮渲染
#   04_translate_v*_backup / _dubtest* / _tts_* 实验目录    历史备份与试验残留
#
# 注: 审计原稿建议连 05_tts/cache 一起清；这里默认保留——段级缓存是指纹化重跑
#     （补段/调参只重合成差异段）的基础，删它省 ~60M 却毁掉 35-40 分钟的合成资产，
#     与"三级缓存体系"的定位矛盾。确要删用 --deep，责任自负。
set -e
cd "$(dirname "$0")/.."
VID="${1%/}"
DEEP=0; APPLY=0
for arg in "${@:2}"; do
  case "$arg" in
    --deep)  DEEP=1 ;;
    --apply) APPLY=1 ;;
    *) echo "未知参数: $arg（用法: clean_run.sh <vid> [--deep] [--apply]）"; exit 1 ;;
  esac
done
RUN="runs/$VID"
[[ -d "$RUN" ]] || { echo "run 不存在: $RUN"; exit 1; }

targets=()
add() { [[ -e "$1" ]] && targets+=("$1"); }
add "$RUN/02_audio"
add "$RUN/06_mix"
for f in "$RUN"/07_output/*(N); do
  [[ "$f" == *_水印.mp4 ]] || targets+=("$f")
done
for d in "$RUN"/04_translate_v*_backup(N) "$RUN"/_dubtest*(N) "$RUN"/_tts_*(N); do
  targets+=("$d")
done
(( DEEP )) && add "$RUN/05_tts/cache"

size() { du -sh "$1" 2>/dev/null | cut -f1; }
echo "== clean_run $VID$( ((DEEP)) && echo ' [--deep]') =="
if (( ${#targets[@]} == 0 )); then
  echo "没有可清理的产物，已是干净状态"
  exit 0
fi
for t in $targets; do
  printf "  删 %-60s %s\n" "$t" "$(size "$t")"
done
if (( ! APPLY )); then
  echo "（干跑预览，未做任何改动。确认执行: 加 --apply；连段级缓存一起删: 再加 --deep）"
  exit 0
fi
for t in $targets; do rm -rf "$t"; done
echo "已清理 ${#targets} 项。重算路径: prepare(音频) → demucs(伴奏) → relayout-only+mix_render(母版/交付)"
