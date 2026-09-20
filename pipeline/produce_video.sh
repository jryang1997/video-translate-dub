#!/bin/zsh
# 单条视频生产：字幕 -> demucs -> 克隆配音(复用系列音色+固定情感向量) -> 混音渲染 -> 水印
# 用法: zsh pipeline/produce_video.sh <yt_id>  （前提: 04_translate/segments.zh.json 已就绪）
# 情感向量默认 0.4,0,0,0,0,0,0.25,0.15 强度0.8；换题材时用环境变量覆盖：
#   EMO_VECTOR="0.1,0,0,0,0,0,0.15,0.3" EMO_ALPHA=0.6 zsh pipeline/produce_video.sh <yt_id>
set -e
cd "$(dirname "$0")/.."
VID=$1
source pipeline/config.sh   # FFMPEG/EMO_VECTOR/SENTENCE_PAUSE_MS/水印/音色库 单点定义

RUN=runs/$VID
mkdir -p $RUN/logs

.venv/bin/python pipeline/build_subs.py $RUN/04_translate/segments.zh.json $RUN/04_translate \
  --font "$SUB_FONT"

if [[ ! -f $RUN/06_mix/demucs/htdemucs/orig_48k/no_vocals.wav ]]; then
  .venv/bin/python -m demucs -n htdemucs --two-stems=vocals -d mps \
    -o $RUN/06_mix/demucs $RUN/02_audio/orig_48k.wav > $RUN/logs/04_demucs.log 2>&1
fi

mkdir -p $RUN/05_tts
# 同系列复用音色: 若本片已有专属参考样本则不覆盖，否则从系列音色库补充默认参考
[[ -f $RUN/05_tts/ref_main.wav ]] || cp $SERIES_REF_DIR/ref_main.wav $RUN/05_tts/ref_main.wav 2>/dev/null || true
[[ -f $RUN/05_tts/spk_1.wav ]] || cp $SERIES_REF_DIR/spk_1.wav $RUN/05_tts/spk_1.wav 2>/dev/null || true

# 首句对齐校验：防止 DTW 首词被片头强烈音乐带偏导致配音抢跑
# exit 3 = 疑似误对齐：需人工核对第一句时间戳后修复再继续
set +e
index-tts/.venv/bin/python pipeline/check_first_align.py $RUN/02_audio/orig_48k.wav \
  $RUN/03_transcript/segments.en.json --whisper-json $RUN/03_transcript/en.json
FA_STATUS=$?
set -e
if [[ $FA_STATUS -eq 3 ]]; then
  echo "FAIL: 首词疑似误对齐（见上方首句校验输出），先修 segments 段1 start 再重跑"
  exit 2
fi

# 合成前预检：防止顺延窗口把片尾段落推出视频末尾导致耗费 GPU 合成后失败
# 使用估时模型秒级模拟，若填窗有越界风险则自动平滑降级为绝对落轨
# exit 1 = 填窗必越界（绝对落轨可救）；exit 2 = 临界（同样降级）。
PC_ARGS=()
set +e
index-tts/.venv/bin/python pipeline/precheck_tts.py $RUN/04_translate/segments.zh.json \
  --fill-window 1.25 --max-shift 0.5 --min-gap 0.22
PC_STATUS=$?
set -e
if [[ $PC_STATUS -eq 1 || $PC_STATUS -eq 2 ]]; then
  echo ">>> 预检判定填窗有越界风险，自动切换 --fill-window 0（绝对落轨）"
  PC_ARGS=(--fill-window 0)
fi

# 翻译契约校验：破坏契约(改时间戳/段数/空段, exit 1)直接终止；
# exit 2 = 仅 WARN（片尾循环句等物理极限段），照常放行进 TTS
# （zsh 的 set -e 会在 $?!=0 时立即退出，必须先 set +e 捕获再恢复）
set +e
.venv/bin/python pipeline/validate_segments.py $RUN/03_transcript/segments.en.json \
  $RUN/04_translate/segments.zh.json
VS_STATUS=$?
set -e
if [[ $VS_STATUS -eq 1 ]]; then
  echo "FAIL: 翻译契约校验未通过，先修复 segments.zh.json"; exit 2
fi

TTS_LOG=$RUN/logs/05_tts.log
set +e
index-tts/.venv/bin/python pipeline/tts_dub_indextts.py \
  $RUN/04_translate/segments.zh.json $RUN/05_tts \
  --ref $RUN/05_tts/ref_main.wav --repo index-tts --model-dir index-tts/checkpoints \
  --emo-vector "$EMO_VECTOR" --emo-alpha "$EMO_ALPHA" \
  --sentence-pause-ms "$SENTENCE_PAUSE_MS" ${PC_ARGS[@]} \
  > $TTS_LOG 2>&1
TTS_STATUS=$?
set -e
(grep -v "^>>" $TTS_LOG || true) | tail -2
if [[ $TTS_STATUS -eq 5 ]]; then
  # 防呆拦截: 05_tts 已有产物但直接跑了全量合成（疑似忘了 --relayout-only）
  echo "FAIL: TTS 防呆拦截——已有 plan.json/seg_*.wav 却跑了全量合成。"
  echo "  只想重排落轨: 命令里加 --relayout-only"
  echo "  确要全量重合成: TTS_FORCE_RESYNTH=1 zsh pipeline/produce_video.sh $VID"
  exit 5
fi
if [[ $TTS_STATUS -ne 0 ]]; then
  echo "FAIL: IndexTTS 配音失败，见 $TTS_LOG"
  exit $TTS_STATUS
fi

.venv/bin/python pipeline/build_subs.py $RUN/04_translate/segments.zh.json \
  $RUN/04_translate --plan $RUN/05_tts/plan.json --avoid-banners --font "$SUB_FONT"

# 先做配音轨质检：确定性缺音/计划损坏阻止昂贵渲染；ASR 低命中只提醒复核。
set +e
.venv/bin/python pipeline/qc_dub_track.py $RUN/05_tts $RUN/04_translate/segments.zh.json \
  > $RUN/logs/07_qc.log 2>&1
QC_STATUS=$?
set -e
if [[ $QC_STATUS -eq 1 ]]; then
  echo "WARN: QC 有文本识别可疑段，见 $RUN/logs/07_qc.log"
elif [[ $QC_STATUS -ge 2 ]]; then
  tail -20 $RUN/logs/07_qc.log
  echo "FAIL: QC 发现确定性配音缺陷，停止渲染"
  exit $QC_STATUS
fi

RAW=$(ls $RUN/01_raw/*.mp4 | head -1)
.venv/bin/python pipeline/mix_render.py "$RAW" $RUN/02_audio/orig_48k.wav \
  $RUN/05_tts/dub_track.wav $RUN/05_tts/plan.json $RUN/07_output \
  --subs-dir $RUN/04_translate --tts-dir $RUN/05_tts \
  --bg-wav $RUN/06_mix/demucs/htdemucs/orig_48k/no_vocals.wav > $RUN/logs/05_render.log 2>&1

# 水印两版都加（交付口径：中文配音版+原声版都要带账号水印）
WM_LOG=$RUN/logs/06_watermark.log
WM_TXT="$WATERMARK_TEXT"
zsh pipeline/add_watermark.sh $RUN/07_output/B_中文配音_中英字幕.mp4 "$WM_TXT" > $WM_LOG 2>&1
zsh pipeline/add_watermark.sh $RUN/07_output/A_原声_中英字幕.mp4 "$WM_TXT" >> $WM_LOG 2>&1
echo "PRODUCE DONE $VID"
