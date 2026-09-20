#!/bin/zsh
# 视频翻译管线 · 预处理入口（URL 形式，内部委托 prepare_video.sh 单一事实源）
# 用法: zsh pipeline/run_all.sh <YouTube链接或本地视频路径>
# 注意: 下载/音频/转写逻辑唯一实现 在 prepare_video.sh（带 ASR 缓存+分辨率保底）。
#       本脚本只做"从 URL 解析出视频 ID"这一件事，避免两套预处理各自漂移
#       （历史上两入口并发 N=4/N=2、有无 ASR 缓存 曾经分叉）。
#       翻译就绪后的正式生产一律走 produce_video.sh。
set -e
cd "$(dirname "$0")/.."

URL="$1"
if [[ -z "$URL" ]]; then
  echo "用法: zsh pipeline/run_all.sh <YouTube链接或本地视频路径>"; exit 1
fi

# 字幕字体：libass 在 mac 上用 CoreText 选字体（忽略 fontsdir），
# 必须把得意黑装进 ~/Library/Fonts 才能生效
mkdir -p ~/Library/Fonts
[[ -f ~/Library/Fonts/SmileySans-Oblique.ttf ]] || \
  cp "$(dirname "$0")/../fonts/SmileySans-Oblique.ttf" ~/Library/Fonts/

if [[ "$URL" =~ "youtube\.com|youtu\.be" ]]; then
  unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy
  VID=$(.venv/bin/yt-dlp --no-playlist --print id "$URL" 2>/dev/null | head -1)
  if [[ -z "$VID" ]]; then echo "无法从 URL 解析视频 ID: $URL"; exit 1; fi
else
  # 本地文件: 直接复制进 run 目录后按 prepare 的既有逻辑走
  VID=$(basename "$URL" | sed 's/\.[^.]*$//')
  mkdir -p "runs/$VID/01_raw"
  cp "$URL" "runs/$VID/01_raw/"
fi

zsh pipeline/prepare_video.sh "$VID"

echo "=== 预处理完成: $VID ==="
echo "下一步:"
echo "  1) 把 runs/$VID/03_transcript/segments.en.json 交给大模型翻译，"
echo "     生成 runs/$VID/04_translate/segments.zh.json（每段加 zh 字段）"
echo "  2) 翻译就绪后正式生产:  zsh pipeline/produce_video.sh $VID"
