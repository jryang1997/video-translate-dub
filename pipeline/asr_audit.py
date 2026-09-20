#!/usr/bin/env python3
"""ASR 时间轴审计：全片 DTW 转写在 ~200s 后会漂移 4-7s，音效段会幻觉复读。

两个子命令（都用切片单独转写拿真值，切片内 DTW 不受长音频累积误差影响）：
  spot  <run> <t0> [t1]   点切仲裁：切片转写 [t0,t1)（默认4s），打印内容。
                          用于抽查全片转写是否可信：抽 2-3 个锚点对得上就可信。
  slice <run> <t0> <t1>   切片重转写：输出 (t0,t1) 的句子级真值时间轴，
                          供人工把 segments.en.json 受影响段「保内容换时间」重排。

用法: python3 pipeline/asr_audit.py spot runs/<id> 214
      python3 pipeline/asr_audit.py slice runs/<id> 198 264
依赖: runs/<id>/02_audio/orig_16k.wav；幻觉复读的特征是 token 带 [_TT_xx] 兜底桶
      （whisper_to_segments 已剥掉，用 en.json 原文件或 spot/slice 直接看）。
"""
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from toolchain import ffmpeg, whisper_cli

ROOT = Path(__file__).resolve().parent.parent
MODEL = ROOT / "models/ggml-large-v3-turbo.bin"


def transcribe_slice(wav: Path, t0: float, dur: float, tmp: Path) -> list[dict]:
    """切片→whisper 转写→返回 [{start,end,text}]，时间为全片绝对秒。"""
    clip = tmp / "slice.wav"
    subprocess.run(
        [ffmpeg(), "-y", "-v", "error", "-ss", str(t0), "-t", str(dur),
         "-i", str(wav), "-c:a", "pcm_s16le", str(clip)],
        check=True)
    base = tmp / "slice"
    r = subprocess.run(
        [whisper_cli(), "-m", str(MODEL), "-f", str(clip), "-l", "en",
         "--dtw", "large.v3.turbo", "-nfa", "-ojf", "-of", str(base), "-t", "8", "-bs", "5"],
        capture_output=True, text=True)
    if "dtw_token_timestamps is not supported" in r.stderr:
        sys.exit("DTW 未生效（flash attn 没关），切片转写无效")
    out = json.loads(Path(f"{base}.json").read_text())
    sents, cur = [], []
    for item in out["transcription"]:
        for tk in item["tokens"]:
            t = tk["text"]
            if t == "[_BEG_]" or t.startswith("[_TT_"):
                continue
            a = tk["offsets"]["from"] / 1000 + t0
            b = tk["offsets"]["to"] / 1000 + t0
            cur.append((a, b, t))
            if re.search(r"[.!?]$", t.strip()):
                sents.append((cur[0][0], b, "".join(x[2] for x in cur).strip()))
                cur = []
    if cur:
        sents.append((cur[0][0], cur[-1][1], "".join(x[2] for x in cur).strip()))
    return sents


def show_full_asr(run: Path, t0: float, t1: float):
    """打印全片转写在同一区间的段落，方便与切片真值对比。"""
    p = run / "03_transcript/segments.en.json"
    if not p.exists():
        return
    for s in json.loads(p.read_text())["segments"]:
        if s["end"] > t0 and s["start"] < t1:
            print(f"  全片: {s['start']:7.2f}-{s['end']:7.2f} {s['en']}")


def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    mode, run = sys.argv[1], Path(sys.argv[2].rstrip("/"))
    wav = run / "02_audio/orig_16k.wav"
    if not wav.exists():
        sys.exit(f"缺少 {wav}")
    if mode == "spot":
        t0 = float(sys.argv[3])
        dur = float(sys.argv[4]) - t0 if len(sys.argv) > 4 else 4.0
        with tempfile.TemporaryDirectory() as td:
            sents = transcribe_slice(wav, t0, dur, Path(td))
        print(f"== 点切真值 {t0}-{t0 + dur:.2f}s ==")
        for a, b, t in sents:
            print(f"  {a:7.2f}-{b:7.2f} {t}")
        show_full_asr(run, t0, t0 + dur)
    elif mode == "slice":
        t0, t1 = float(sys.argv[3]), float(sys.argv[4])
        with tempfile.TemporaryDirectory() as td:
            sents = transcribe_slice(wav, t0, t1 - t0, Path(td))
        print(f"== 切片真值 {t0}-{t1}s（句子级，用于重排 segments.en.json）==")
        for a, b, t in sents:
            print(f"  {a:7.2f}-{b:7.2f} ({b - a:5.2f}s) {t}")
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
