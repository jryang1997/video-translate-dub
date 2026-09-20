#!/bin/zsh
# 单条视频预处理：下载 -> 双路音频 -> DTW 词级转写（无 VAD，词级时间戳内置对齐）
# 用法: zsh pipeline/prepare_video.sh <yt_id>
set -e
cd "$(dirname "$0")/.."
unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy  # 本地代理可能已挂，直连可达
VID=$1
source pipeline/config.sh   # FFMPEG/FFPROBE 等单点定义
mkdir -p runs/$VID/{01_raw,02_audio,03_transcript,04_translate,05_tts,06_mix,07_output,logs}
if [[ ! -f runs/$VID/03_transcript/segments.en.json ]]; then
  .venv/bin/yt-dlp --no-playlist --no-write-subs \
    -f "bv*[vcodec^=avc1][height<=1080]+ba[ext=m4a]/bv*+ba/b" \
    --merge-output-format mp4 --write-info-json --retries 8 -N 2 \
    -o "runs/$VID/01_raw/%(title).60s.%(ext)s" \
    "https://www.youtube.com/watch?v=$VID" > runs/$VID/logs/01_download.log 2>&1
  RAW=$(ls runs/$VID/01_raw/*.mp4 | head -1)
  # 分辨率保底：360p 通常是网络超时后 yt-dlp 回退到唯一可直连合并流的产物，
  # 真源几乎都有 720/1080p。低于 720p 就删掉重下（-F 里挑 bv*+ba 分离流）。
  VRES=$($FFPROBE -v error -select_streams v:0 -show_entries stream=height -of csv=p=0 "$RAW")
  if [[ "$VRES" -lt 720 ]]; then
    echo "WARN: 下载到的原片只有 ${VRES}p，删除重下 1080p 分离流..."
    rm -f runs/$VID/01_raw/*.mp4
    .venv/bin/yt-dlp --no-playlist --no-write-subs \
      -f "bv*[vcodec^=avc1][height<=1080]+ba[ext=m4a]/bv*[height<=1080]+ba" \
      --merge-output-format mp4 --write-info-json --retries 8 -N 2 \
      -o "runs/$VID/01_raw/%(title).60s.%(ext)s" \
      "https://www.youtube.com/watch?v=$VID" >> runs/$VID/logs/01_download.log 2>&1
    RAW=$(ls runs/$VID/01_raw/*.mp4 | head -1)
  fi
  $FF -y -v error -i "$RAW" -vn -ac 1 -ar 16000 -c:a pcm_s16le runs/$VID/02_audio/orig_16k.wav
  $FF -y -v error -i "$RAW" -vn -ar 48000 -c:a pcm_s16le runs/$VID/02_audio/orig_48k.wav
  # ASR 内容寻址缓存（借鉴 VideoLingo）: 音频md5+模型+参数指纹, 重跑不重复转写
  ASR_KEY=$(python3 pipeline/asr_cache.py key runs/$VID/02_audio/orig_16k.wav \
    "$WHISPER_MODEL" "$WHISPER_DTW")
  ASR_DIR=$(python3 pipeline/asr_cache.py get "$ASR_KEY" || true)
  if [[ -n "$ASR_DIR" ]]; then
    cp "$ASR_DIR/en.json" runs/$VID/03_transcript/en.json
    cp "$ASR_DIR/segments.en.json" runs/$VID/03_transcript/segments.en.json
    echo "[asr-cache] 命中缓存 $ASR_KEY 前缀, 跳过转写"
  else
    whisper-cli -m "$WHISPER_MODEL" -f runs/$VID/02_audio/orig_16k.wav -l en \
      --dtw "$WHISPER_DTW" -nfa -ojf -osrt -of runs/$VID/03_transcript/en -t 8 -bs 5 \
      > runs/$VID/logs/02_whisper.log 2>&1
    python3 pipeline/whisper_to_segments.py runs/$VID/03_transcript/en.json \
      runs/$VID/03_transcript/segments.en.json --video-id $VID
    python3 pipeline/asr_cache.py put "$ASR_KEY" runs/$VID/03_transcript/en.json \
      runs/$VID/03_transcript/segments.en.json > /dev/null
  fi
fi
# DTW 词级时间戳已内置对齐，无需 retime（旧 retime 步骤已移除）
echo "PREP DONE $VID"
