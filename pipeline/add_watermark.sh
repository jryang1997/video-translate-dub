#!/bin/zsh
# 给视频加漂浮文字水印（防盗用）
# 用法: zsh pipeline/add_watermark.sh <输入视频> "<账号名>" [输出路径] [字号]
#   账号名可用 | 表示换行，如 "忘记被重置前的|名字了"
# 默认: 得意黑(半暗斜体) 半透明白@0.45 无描边 两行居中 李萨如漂浮
set -e
source "$(dirname "$0")/config.sh"   # FFMPEG 单点定义（本脚本不切目录，按脚本位置 source）
IN="$1"
OUT="${3:-${IN:r}_水印.mp4}"
FONT_SIZE="${4:-36}"
FONT="$(dirname "$0")/../fonts/SmileySans-Oblique.ttf"

# 账号名写入临时文本文件（| 转换为换行），避免 drawtext 转义问题
# X 必须在结尾：BSD mktemp 不认 /tmp/wm_XXXX.txt 这种带后缀模板（2026-09-19 实测报 File exists）
TXT=$(mktemp /tmp/wm_XXXXXX)
printf '%s' "$2" | tr '|' '\n' > "$TXT"

"$FFMPEG" -y -v error -i "$IN" -vf "drawtext=textfile=$TXT:fontfile=$FONT:fontsize=$FONT_SIZE:fontcolor=white@0.45:text_align=C:line_spacing=6:x=(w-tw)*(0.5+0.425*sin(2*PI*t/23)):y=(h-th)*(0.5+0.32*sin(2*PI*t/31+1.3))" \
  -c:v libx264 -crf 20 -preset fast -pix_fmt yuv420p -c:a copy "$OUT"

rm -f "$TXT"
echo "完成: $OUT"
