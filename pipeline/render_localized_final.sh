#!/bin/zsh
# 最终"完全本地化"稿渲染：词卡横幅(汉化) + 中英字幕 + 漂浮水印，一次合成。
# 输入: <run>/07_output/B_中文配音_中英字幕.mp4（mix_render 母版；其烧录的 bilingual_dub.ass
#       已由 build_subs --plan --avoid-banners 在渲染前完成词卡避让，无需再跑单独的 rebuild 步骤）
# 时段: 读 <run>/06_mix/banner_filter.txt，每行: tag: <tag> patch <a> <b> text <c> <d>
#       patch=补丁层硬切窗口（盖英文）  text=中文词淡入淡出窗口
# 横幅: <run>/04_translate/banners/<tag>_patch.png + <tag>_text.png（make_text_banners.py 产出）
# 用法: zsh pipeline/render_localized_final.sh <run_dir> [水印文案，|换行，默认取 config.sh 的 WATERMARK_TEXT]
# 注意: -loop 1 图片流必须配 -t 母版时长，否则主视频结束后滤镜链不终止、无限渲染
set -e
cd "$(dirname "$0")/.."
RUN="${1%/}"
source pipeline/config.sh   # FFMPEG/FFPROBE/WATERMARK_TEXT 单点定义
WM="${2:-$WATERMARK_TEXT}"
B="$RUN/07_output/B_中文配音_中英字幕.mp4"
OUT="$RUN/07_output/完全本地化_中英字幕_水印.mp4"
BAN="$RUN/04_translate/banners"
FILTER="$RUN/06_mix/banner_filter.txt"
FONTDIR="$PWD/fonts"
[ -f "$B" ] || { echo "缺母版 $B（先跑 mix_render）"; exit 1; }
[ -f "$FILTER" ] || { echo "缺 $FILTER"; exit 1; }

DUR=$($FFPROBE -v error -show_entries format=duration -of csv=p=0 "$B")
TXT=$(mktemp /tmp/wm_XXXXXX)  # X 必须在结尾（BSD mktemp）
printf '%s' "$WM" | tr '|' '\n' > "$TXT"

INPUTS=(); CHAIN=""; PREV="[0:v]"; IDX=1; N=0
while read -r line; do
  set -- ${=line}
  [[ "$1" == "tag:" ]] || continue
  # 行格式: tag: <tag> patch <a> <b> text <c> <d> → $1..$8
  tag=$2; pa=$4; pb=$5; ta=$7; tb=$8
  [ -f "$BAN/${tag}_patch.png" ] || { echo "缺 $BAN/${tag}_patch.png"; exit 1; }
  INPUTS+=("-loop" "1" "-i" "$BAN/${tag}_patch.png" "-loop" "1" "-i" "$BAN/${tag}_text.png")
  CHAIN+="${PREV}[$IDX:v]overlay=0:0:enable='between(t,${pa},${pb})'[v${IDX}];"
  FADE_D=$(python3 -c "print(min(0.45,(${tb}-${ta})/3))")
  OUT_ST=$(python3 -c "print(max(0,${tb}-0.55))")
  CHAIN+="[$((IDX+1)):v]format=rgba,fade=t=in:st=$(python3 -c "print(${ta}+0.05)"):d=${FADE_D}:alpha=1,fade=t=out:st=${OUT_ST}:d=0.5:alpha=1[t${IDX}];"
  PREV="[v${IDX}]"
  CHAIN+="${PREV}[t${IDX}]overlay=0:0:enable='between(t,${ta},${tb})'[v$((IDX+1))];"
  PREV="[v$((IDX+1))]"
  IDX=$((IDX+2)); N=$((N+1))
done < "$FILTER"
[ "$N" -gt 0 ] || { echo "banner_filter.txt 里没有 tag: 行"; exit 1; }
CHAIN+="${PREV}drawtext=textfile=$TXT:fontfile=$FONTDIR/SmileySans-Oblique.ttf:fontsize=36:fontcolor=white@0.45:text_align=C:line_spacing=6:x=(w-tw)*(0.5+0.425*sin(2*PI*t/23)):y=(h-th)*(0.5+0.32*sin(2*PI*t/31+1.3))[vout]"

$FFMPEG -y -v info -i "$B" ${INPUTS} \
  -filter_complex "$CHAIN" \
  -map "[vout]" -map 0:a -t "$DUR" \
  -c:v libx264 -crf 20 -preset fast -pix_fmt yuv420p -c:a copy \
  "$OUT" 2> "$RUN/logs/04_localized_render.log"
rm -f "$TXT"
echo "完成: $OUT（${N} 张词卡，母版 ${DUR}s）"
