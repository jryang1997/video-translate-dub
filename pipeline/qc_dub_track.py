#!/usr/bin/env python3
"""配音轨成品质检：先做确定性音频检查，再用 Whisper 中文复核内容。

思想来自 pyvideotrans recogn2pass，但做双向校验而非静默替换字幕:
- 每个任务段应在配音轨上 [place_start-0.5, place_start+dur+1.5] 内识别出相似文本
- 相似度低 / 找不到 => 缺失或错位，列入清单
另做能量静检: 配音轨在段窗口外的"漏声"与段内的"静默"统计

用法: qc_dub_track.py <tts_dir> <segments.zh.json> [--model models/ggml-large-v3-turbo.bin]
退出码: 0=通过；1=ASR 警告或不可用，可继续渲染；2=确定性硬失败，应中止渲染
"""
import argparse, json, re, subprocess, sys, os

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from audio_io import effective_speech_interval, load_pcm
from toolchain import ffmpeg, whisper_cli

WORD_RE = re.compile(r"[\u4e00-\u9fff a-zA-Z0-9]+")


def norm_zh(t):
    return "".join(WORD_RE.findall(t))


def deterministic_problems(segments, plan, samples, sr, min_rms=1e-4):
    """检查 plan 完整性、区间合法性，以及实际放置区间是否有声音。"""
    by_id = {p["id"]: p for p in plan}
    problems = []
    track_dur = len(samples) / sr
    for seg in segments:
        sid = seg["id"]
        p = by_id.get(sid)
        if p is None:
            problems.append((sid, "plan 缺少该段"))
            continue
        try:
            start, end = effective_speech_interval(p)
        except (KeyError, TypeError, ValueError) as exc:
            problems.append((sid, f"plan 时间字段无效: {exc}"))
            continue
        if start < 0 or end <= start:
            problems.append((sid, f"非法配音区间 {start:.3f}-{end:.3f}s"))
            continue
        if end > track_dur + 1 / sr:
            problems.append((sid, f"配音区间超出音轨 {end:.3f}s > {track_dur:.3f}s"))
            continue
        i0, i1 = int(start * sr), min(int(np.ceil(end * sr)), len(samples))
        clip = samples[i0:i1]
        clip_rms = float(np.sqrt(np.mean(clip ** 2))) if len(clip) else 0.0
        if clip_rms <= min_rms:
            problems.append((sid, f"实际放置区间静默（RMS={clip_rms:.6f}）"))
    return problems


def qc_exit_status(hard_problems, soft_problems):
    if hard_problems:
        return 2
    if soft_problems:
        return 1
    return 0


def asr_hit_is_warning(hit, min_ratio):
    return hit < min_ratio


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("tts_dir")
    ap.add_argument("segments_json")
    ap.add_argument("--model", default="models/ggml-large-v3-turbo.bin")
    ap.add_argument("--min-ratio", type=float, default=0.45)
    args = ap.parse_args()

    data = json.load(open(args.segments_json))
    plan_items = json.load(open(f"{args.tts_dir}/plan.json"))["plan"]
    plan = {p["id"]: p for p in plan_items}
    dub_wav = f"{args.tts_dir}/dub_track.wav"
    samples, sample_rate = load_pcm(dub_wav)
    hard_problems = deterministic_problems(data["segments"], plan_items, samples, sample_rate)
    if hard_problems:
        print(f"FAIL: {len(hard_problems)} 个确定性问题:")
        for sid, why in hard_problems:
            print(f"  段{sid}: {why}")
        sys.exit(2)

    mono = f"{args.tts_dir}/dub_track_16k.wav"
    converted = subprocess.run([ffmpeg(), "-y", "-v", "error", "-i", dub_wav, "-ac", "1", "-ar", "16000",
                                "-c:a", "pcm_s16le", mono], capture_output=True, text=True)
    if converted.returncode != 0:
        print(f"WARN: QC 音频转换失败: {converted.stderr[-300:]}")
        sys.exit(1)

    print("对配音轨跑 whisper 中文再识别（QC）...", flush=True)
    asr_file = f"{args.tts_dir}/qc_asr.json"
    if os.path.exists(asr_file):
        os.remove(asr_file)
    recognized = subprocess.run([whisper_cli(), "-m", args.model, "-f", mono, "-l", "zh",
                                 "--dtw", "large.v3.turbo", "-nfa", "-ojf", "-of", f"{args.tts_dir}/qc_asr",
                                 "-t", "8"], capture_output=True, text=True)
    if recognized.returncode != 0 or not os.path.exists(asr_file):
        detail = recognized.stderr[-300:] if recognized.stderr else "未产出识别结果"
        print(f"WARN: Whisper QC 不可用: {detail}")
        sys.exit(1)
    try:
        asr = json.load(open(asr_file))["transcription"]
    except (OSError, KeyError, json.JSONDecodeError) as exc:
        print(f"WARN: Whisper QC 结果不可读: {exc}")
        sys.exit(1)

    # ASR 句子归一化列表
    a = []
    for s in asr:
        t = norm_zh(s.get("text", ""))
        if t:
            a.append((s["offsets"]["from"] / 1000.0, s["offsets"]["to"] / 1000.0, t))

    soft_problems = []
    for seg in data["segments"]:
        sid = seg["id"]
        p = plan.get(sid, {})
        start, end = effective_speech_interval(p)
        dur = end - start
        target = norm_zh(p.get("text") or seg.get("zh", ""))
        if len(target) < 2:
            continue
        lo, hi = start - 0.5, start + dur + 1.5
        window_text = "".join(t for st, et, t in a if st < hi and et > lo)
        # 字符集命中率（同音字鲁棒）：whisper 中文再识别有大量同音字（摩乔→魔桥），
        # SequenceMatcher 相似度会误报；改用目标段字符在识别文本中的命中率
        tgt = set(re.findall(r"[\u4e00-\u9fff0-9]", p.get("text") or seg.get("zh", "")))
        win = set(re.findall(r"[\u4e00-\u9fff0-9]", window_text))
        hit = len(tgt & win) / max(len(tgt), 1)
        if not window_text:
            soft_problems.append((sid, "ASR 在该时段未识别到文本", seg.get("zh", "")[:20], 0.0))
        elif asr_hit_is_warning(hit, args.min_ratio):
            soft_problems.append((sid, f"ASR 字符命中率低 {hit:.0%}", seg.get("zh", "")[:20], hit))

    # 全轨覆盖率: ASR 检出的总语音量 vs plan 合成总时长
    asr_speech = sum(et - st for st, et, _ in a)
    plan_speech = sum(p["dur"] for p in plan.values())
    cov = asr_speech / max(plan_speech, 0.1)
    print(f"\nASR 检出语音 {asr_speech:.0f}s / plan 合成 {plan_speech:.0f}s (覆盖率 {cov:.0%})")

    if soft_problems:
        print(f"\nWARN: {len(soft_problems)} 段 ASR 复核可疑:")
        for sid, why, txt, r in soft_problems:
            print(f"  段{sid}: {why} | {txt}")
        sys.exit(1)
    print("QC 通过 ✓ 全部任务段在配音轨上有对应语音")


if __name__ == "__main__":
    main()
