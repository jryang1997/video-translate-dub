---
name: video-translate-dub
description: "Complete local English-to-Chinese video translation & voice clone dubbing pipeline for AI coding agents. Uses yt-dlp, whisper.cpp (word-level DTW timestamps), LLM translation with duration budgeting, IndexTTS-2.5 voice cloning, Demucs vocal separation, audio mix, and subtitle rendering. Optimized for Mac Apple Silicon (MPS)."
license: Apache-2.0
metadata:
  version: "1.0.0"
  tags: ["video-translation", "tts", "voice-clone", "whisper", "agent-skill", "indextts", "apple-silicon"]
---

# Video Translate & Dub Pipeline (Agent Skill)

把英文视频做成「中文原声音色克隆配音 + 中英双语字幕（可选水印与画面词卡汉化）」的完整自动化管线。
本技能面向 AI Coding Agent（如 Claude Code, Codex, Cursor, Windsurf），旨在赋予 Agent 工业级的音视频本地化生产与质检能力。

## 前置环境自检（Pre-flight Check）

在开始处理任务前，Agent 应先检查本地环境与工具链是否完备：

```bash
# 1. 检查 ffmpeg-full（必须支持 libass 烧字幕）
/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg -hide_banner -filters | grep -q " ass " && echo "ffmpeg-full ok"

# 2. 检查 whisper.cpp 命令行与模型
which whisper-cli && ls models/ggml-large-v3-turbo.bin && echo "whisper ok"

# 3. 检查中文字体（得意黑 Smiley Sans）
ls fonts/SmileySans-Oblique.ttf

# 4. 检查 IndexTTS 虚拟环境与模型权重
[ -x index-tts/.venv/bin/python ] && [ -f index-tts/checkpoints/config.yaml ] && echo "IndexTTS ok"
```

> **注意**：烧字幕必须使用带 `libass` 的 `ffmpeg-full`。系统默认的精简版 ffmpeg 通常不含 ass/subtitles 滤镜。

---

## 核心生产链路（8 步标准流）

### 1. 初始化任务目录
每个视频以视频唯一 ID（如 YouTube 视频 ID 或散列串）建目录：
```bash
mkdir -p runs/<id>/{01_raw,02_audio,03_transcript,04_translate,05_tts,06_mix,07_output,logs}
```

### 2. 视频下载与转码
支持 YouTube 链接或本地传入的 MP4。下载必须强制限定 `avc1`（H.264）编码：
```bash
.venv/bin/yt-dlp --no-playlist --no-write-subs \
  -f "bv*[vcodec^=avc1][height<=1080]+ba[ext=m4a]/b" --merge-output-format mp4 \
  --write-info-json -N 4 -o "runs/<id>/01_raw/%(title).60s.%(ext)s" "<URL>"
```
或直接运行一键预处理脚本：
```bash
zsh pipeline/prepare_video.sh "<URL_or_VID>"
```

### 3. 音频抽取
从视频中提取 16kHz 单声道 WAV（ASR 专用）和 48kHz 立体声 WAV（混音专用）：
```bash
ffmpeg -y -i "runs/<id>/01_raw/"*.mp4 -vn -ac 1 -ar 16000 "runs/<id>/02_audio/orig_16k.wav"
ffmpeg -y -i "runs/<id>/01_raw/"*.mp4 -vn -c:a pcm_s16le -ar 48000 "runs/<id>/02_audio/orig_48k.wav"
```

### 4. 词级时间戳转写（Whisper DTW）
**严禁开启 `--vad` 与 DTW 同开**（二者同开会导致时间戳错乱 170s）。必须加 `-nfa` 关闭 flash attention（因与 DTW 互斥）：
```bash
whisper-cli -m models/ggml-large-v3-turbo.bin -f runs/<id>/02_audio/orig_16k.wav -l en \
  --dtw large.v3.turbo -nfa -ojf -osrt -of runs/<id>/03_transcript/en -t 8 -bs 5

python3 pipeline/whisper_to_segments.py runs/<id>/03_transcript/en.json \
  runs/<id>/03_transcript/segments.en.json --video-id <id>
```

> **时间轴审计**：对于 > 3 分钟的视频，如果发现时间轴漂移或幻觉复读：
> ```bash
> python3 pipeline/asr_audit.py spot runs/<id> <时刻>
> ```

---

### 5. 大模型翻译与时长预算自校验（Agent 核心动作）

#### 步骤 5.1：生成时长预算表（译前必做）
```bash
python3 pipeline/validate_segments.py runs/<id>/03_transcript/segments.en.json --budget
```
预算基于实测回归模型：`预计时长 = 0.1801 × 汉字数 + 0.0537 × 标点数 + 1.1234`。
- **短句包含固定开销（≈1.12s）**：窗口 < 1.9s 的短句，写短没有用，**严禁为了压字数删减内容**。
- **信息完整优先**：超窗时优先照实翻译，管线会自动借用后续段落的自然静音或微调速（≤1.25x），宁可语速稍快也绝不漏译。

#### 步骤 5.2：Agent 逐段写出中文译文
输出到 `runs/<id>/04_translate/segments.zh.json`，格式契约：
```json
{
  "video_id": "<id>",
  "segments": [
    { "id": 1, "start": 0.55, "end": 6.76, "en": "...", "zh": "..." }
  ]
}
```
**翻译契约要求**：
1. 只能逐段增加 `zh` 字段；
2. **严禁拆段、合段、修改 id 或更改 start/end 时间戳**；
3. 配音文本**必须保留完整标点符号**（逗号、句号、问号），TTS 模型依靠标点控制停顿与呼吸；
4. 字幕层会自动去除标点，无需手动清除。

#### 步骤 5.3：执行翻译契约自检
```bash
python3 pipeline/validate_segments.py runs/<id>/03_transcript/segments.en.json \
  runs/<id>/04_translate/segments.zh.json
```
若返回非 0，Agent 必须根据报错提示修正译文。

---

### 6. 人声分离、原声克隆配音与混音

#### 步骤 6.1：Demucs 人声/伴奏分离
```bash
.venv/bin/python -m demucs -n htdemucs --two-stems=vocals -d mps \
  -o runs/<id>/06_mix/demucs runs/<id>/02_audio/orig_48k.wav
```

#### 步骤 6.2：优选克隆参考干音样本
克隆质量完全取决于参考音频的纯净度（严禁含有背景音乐或内部长停顿）：
```bash
python3 pipeline/pick_reference.py runs/<id>/04_translate/segments.zh.json \
  runs/<id>/06_mix/demucs/htdemucs/orig_48k/vocals.wav runs/<id>/05_tts --top 3

cp runs/<id>/05_tts/ref_auto_1.wav runs/<id>/05_tts/ref_main.wav
```

#### 步骤 6.3：IndexTTS-2.5 情感音色克隆与落轨
```bash
index-tts/.venv/bin/python pipeline/tts_dub_indextts.py \
  runs/<id>/04_translate/segments.zh.json runs/<id>/05_tts \
  --ref runs/<id>/05_tts/ref_main.wav --repo index-tts --model-dir index-tts/checkpoints \
  --pause-retake --compress-pauses --duration-factor 1.12 --fill-window 0
```
- 支持 `--relayout-only`：调参后重新落轨仅需数秒，无需重新合成音频。

#### 步骤 6.4：字幕生成与成片渲染
```bash
# 生成与配音真实落轨对齐的双语字幕
python3 pipeline/build_subs.py runs/<id>/04_translate/segments.zh.json runs/<id>/04_translate --plan runs/<id>/05_tts/plan.json

# 伴奏与配音轨智能动态对齐混音，并压制成品
python3 pipeline/mix_render.py "runs/<id>/01_raw/"*.mp4 runs/<id>/02_audio/orig_48k.wav \
  runs/<id>/05_tts/dub_track.wav runs/<id>/05_tts/plan.json runs/<id>/07_output \
  --subs-dir runs/<id>/04_translate --tts-dir runs/<id>/05_tts \
  --bg-wav runs/<id>/06_mix/demucs/htdemucs/orig_48k/no_vocals.wav
```

或直接执行一键生产收口：
```bash
zsh pipeline/produce_video.sh <id>
```

---

### 7. 质量检验（QC Checklist）

交付前，Agent 应核对以下 5 项指标：
1. **TTS 截断率**：`tts_dub_indextts.py` 报告末行应为「截断段: 0」；
2. **段落契约**：`validate_segments.py` 必须 exit 0；
3. **识别核验**：运行 `python3 pipeline/qc_dub_track.py runs/<id>/05_tts runs/<id>/04_translate/segments.zh.json`，检查有无漏配段；
4. **字体渲染**：确认日志中 `Smiley Sans` 成功加载，未降级到系统后备字体；
5. **音画同步**：成品音频响度均衡（约 -14 LUFS），人声发音与口型字幕严格贴合。
