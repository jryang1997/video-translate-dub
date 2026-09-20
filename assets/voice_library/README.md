# 系列音色库说明 (Voice Library)

本目录用于存放多角色视频翻译或特定系列视频的**参考干音切片**（用于 IndexTTS-2.5 进行原声音色克隆）。

## 目录规范与文件格式

- **格式要求**：16-bit 24kHz 或 48kHz 单声道/立体声 WAV 文件。
- **干音质量黄金准则**（实测对克隆质量至关重要）：
  1. **纯净度**：无背景音乐（BGM）、无强烈音效、无回声混响；
  2. **VAD 有效发音率**：≥ 90%，无内部长停顿（> 0.25s 会被模型学走导致语顿）；
  3. **样本时长**：单条参考音频建议在 2~6 秒之间，为单一完整语义句子；
  4. **语调自然**：尽量选用情绪平稳的旁白或对白，避免使用过度夸张的喊叫声。

## 命名约定

若使用 `SERIES_REF_DIR` 指定系列音色库：
- `ref_main.wav`：主说话人/旁白基准干音；
- `ref_alt.wav`：次要说话人/备用干音；
- `spk_1.wav` ~ `spk_N.wav`：多说话人场景下的角色独立音色切片。

> **提示**：您可以通过管线内置的 `pipeline/pick_reference.py` 脚本，从已分离的人声轨（vocals.wav）中自动按 VAD 与停顿得分筛选出最优质的参考切片：
> ```bash
> python3 pipeline/pick_reference.py <segments.zh.json> <vocals.wav> assets/voice_library/ --top 3
> ```
