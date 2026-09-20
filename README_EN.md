# Video Translate & Dub Pipeline

[English](README_EN.md) | [中文说明](README.md)

A 100% local English-to-Chinese video localization and voice-clone dubbing pipeline designed for **AI Coding Agents** (Claude Code, Codex, Cursor, Windsurf) and developers.

Powered by whisper.cpp word-level alignment, IndexTTS-2.5 voice cloning, Demucs vocal separation, and FFmpeg cinematic rendering. Fully accelerated on Mac Apple Silicon (MPS) with zero cloud API token costs.

---

## Why This Project?

While numerous automated video translation tools exist, real-world production runs into recurring bottlenecks:

1. **Audio-Visual Desync**: Most tools rely on coarse VAD silences, causing dubbed speech to drift by seconds when background music or pacing fluctuates;
2. **Robotic Voices**: Typical setups use generic Edge-TTS text-to-speech, stripping the original speaker's timbre, warmth, and emotion;
3. **Information Loss from Rigid Constraints**: Enforcing arbitrary speed caps (e.g., "max 4.2 characters/sec") forces LLMs to aggressively prune meaningful context;
4. **Unlocalized Visual Cards**: On-screen English pedagogical graphics clash with Chinese subtitles;
5. **Prohibitive Cloud Costs**: Commercial cloning APIs quickly become unfeasible when processing multi-episode video libraries.

This pipeline tackles these challenges with 5 core engineering decisions:

- **Strict DTW Word-Level Alignment**: `whisper.cpp --dtw` extracts true phoneme timestamps to clamp segment boundaries, immune to musical interference;
- **IndexTTS-2.5 Clean Reference Selection & 8-D Emotion Control**: Automated VAD purity and pause scoring pick cleanest dry reference samples, coupled with 8-dimensional emotion vectors to preserve natural vocal expression;
- **Regression Duration Budgeting & Silence Borrowing**: Grounded in empirical regression (`0.1801 × chars + 0.0537 × punctuation + 1.1234`). Segment overflow borrows natural inter-sentence pauses or applies subtle pacing scaling (≤1.25x) rather than deleting clauses;
- **Dual-Layer Differential Banner Inpainting**: Erases on-screen English text using clean background patches while fading in localized Chinese typography and dynamically dodging subtitle placement;
- **Fully Local Apple Silicon MPS Pipeline**: ASR, vocal separation, neural synthesis, and video transcoding run entirely on Mac unified memory.

---

## Installation

This repository acts both as a standalone CLI toolchain and as an official **Agent Skill**.

### Method 1: Install via `npx skills` (Recommended for AI Agents)

If you use Claude Code, Cursor, or any agent compliant with the Agent Skills standard:

```bash
npx skills add jryang1997/video-translate-dub
```

An interactive CLI prompt will automatically link `video-translate-dub` into your project or global agent environment. You can then prompt your agent:
> *"Download and localize this YouTube video with Chinese voice cloning: https://www.youtube.com/watch?v=XXXX"*

The agent will execute pre-flight checks, transcription, translation budgeting, contract validation, and final mastering autonomously.

### Method 2: Source Clone (For Human Developers)

```bash
git clone https://github.com/jryang1997/video-translate-dub.git
cd video-translate-dub
```

---

## Hardware & Environment Setup

### 1. Hardware Baseline
- **Recommended**: Apple Silicon Mac (M1/M2/M3/M4 series), **≥ 32GB Unified Memory**;
- **Concurrency Rule**: IndexTTS-2.5 peak synthesis consumes 25~35GB memory per process. **Never run concurrent dubbing jobs** on a single workstation; serialize runs to prevent OOM / swap thrashing.
- **Runtime Reference**: On an M1 Pro, a 5-minute video takes ~1.5 min ASR, ~2 min vocal isolation, and ~16 min voice cloning (~30-35 mins end-to-end).

### 2. Dependencies Installation

```bash
# 1. Install ffmpeg-full (with libass subtitle support) and whisper.cpp
brew install ffmpeg-full whisper-cli

# 2. Main Python virtual environment (transcription, Demucs, mixing)
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-main.txt

# 3. IndexTTS dedicated environment & checkpoints
git clone https://github.com/index-tts/index-tts.git
cd index-tts
python3 -m venv .venv
source .venv/bin/activate
pip install -r ../requirements-indextts.txt
cd ..

# 4. Assets & Fonts
# Place Whisper weights in models/ggml-large-v3-turbo.bin
# Smiley Sans font is bundled under fonts/SmileySans-Oblique.ttf
cp fonts/*.ttf ~/Library/Fonts/
```

---

## CLI Usage Guide

### Phase 1: Preprocessing (Download + Audio Extraction + DTW Transcription)

```bash
zsh pipeline/prepare_video.sh "https://www.youtube.com/watch?v=XXXX"
```
Artifacts are generated in `runs/<VIDEO_ID>/`:
- `01_raw/`: Source MP4;
- `02_audio/`: 16kHz & 48kHz WAV audio tracks;
- `03_transcript/segments.en.json`: Accurate word-level timestamps.

### Phase 2: Translation & Contract Self-Checking

1. **Inspect Duration Budget**:
   ```bash
   python3 pipeline/validate_segments.py runs/<VIDEO_ID>/03_transcript/segments.en.json --budget
   ```
2. **Generate Chinese Translation**:
   Translate segments into `runs/<VIDEO_ID>/04_translate/segments.zh.json` by populating the `zh` field.
   > **Contract Requirement**: Full punctuation marks (commas, periods, questions) must be kept for TTS prosody. Do not split, merge, or alter segment IDs or timestamps.
3. **Verify Contract Compliance**:
   ```bash
   python3 pipeline/validate_segments.py runs/<VIDEO_ID>/03_transcript/segments.en.json runs/<VIDEO_ID>/04_translate/segments.zh.json
   ```

### Phase 3: Production (Voice Clone + Dub Layout + Mastering)

```bash
zsh pipeline/produce_video.sh <VIDEO_ID>
```

Outputs in `runs/<VIDEO_ID>/07_output/`:
- `B_中文配音_中英字幕_水印.mp4` (Release candidate with watermark)
- `B_中文配音_中英字幕.mp4` (Clean dubbed master)
- `A_原声_中英字幕.mp4` (Bilingual original audio)

---

## Repository Structure

```text
.
├── SKILL.md                 # Agent Skill definition for AI coding assistants
├── pipeline/                # Core processing scripts
│   ├── prepare_video.sh     # Preprocessing pipeline
│   ├── produce_video.sh     # Production pipeline (TTS, mix, render)
│   ├── whisper_to_segments.py # ASR segmentation & word-level bounding
│   ├── validate_segments.py # Budgeting & translation contract validation
│   ├── tts_dub_indextts.py  # IndexTTS-2.5 driver & dynamic layout engine
│   ├── pick_reference.py    # Clean vocal reference scoring & extraction
│   ├── layout.py            # Layout pure functions (extension, borrowing)
│   ├── zh_duration.py       # Duration regression model
│   ├── mix_render.py        # Subtitle burning, loudnorm & final muxing
│   ├── make_text_banners.py # On-screen card inpainting & dual-layer banners
│   ├── config.sh            # Global defaults and toolchain configurations
│   └── tests/               # Regression test suites
├── assets/voice_library/    # Multi-speaker voice profile directory guidelines
├── fonts/                   # Open-source typography
├── NOTICE                   # Third-party notices and acknowledgements
└── LICENSE                  # Apache 2.0 License
```

---

## Acknowledgements

We acknowledge and appreciate the foundations laid by the following open-source projects and creators:

- **[atelierAnchor / Smiley Sans (得意黑)](https://github.com/atelier-anchor/smiley-sans)**: Special thanks to oseq, Yuli Liu, and contributors for creating the open-source typeface that defines our dual-language visual presentation;
- **[bilibili / IndexTTS-2.5](https://github.com/index-tts/index-tts)**: Outstanding open-source autoregressive voice cloning;
- **[whisper.cpp](https://github.com/ggerganov/whisper.cpp)**: High-performance C/C++ Whisper inference with native DTW token timestamps;
- **[VideoLingo](https://github.com/Huanshere/VideoLingo)**: Pioneering multi-step translation reflection and terminology consistency;
- **[KrillinAI](https://github.com/KrillinAI/KrillinAI)**: Empirical duration regression concepts and robust ASR caching architectures;
- **[facebookresearch / Demucs](https://github.com/facebookresearch/demucs)**: Studio-grade vocal and accompaniment isolation;
- **ZCOOL KuaiLe (站酷快乐体)**: Free commercial open-source typography designed by ZCOOL & Liu Bingke.

---

## Disclaimer

1. **Research & Educational Scope**: This project is provided solely for educational, academic, and technical exploration purposes.
2. **User Responsibility**: Users must ensure they have acquired appropriate authorization and licensing from content copyright holders before processing any third-party media.
3. **Derivative Notice**: The voice cloning engine in this pipeline is derived from bilibili IndexTTS-2.5. Any modifications made by this project are unaffiliated with the original right-holder. The original right-holder does not endorse, guarantee, or assume liability for this derivative work.
4. **Limitation of Liability**: The authors and contributors assume no liability for any copyright infringement, platform policy violation, or legal dispute arising from the use of this software.

---

## License

Distributed under the [Apache License 2.0](LICENSE).
