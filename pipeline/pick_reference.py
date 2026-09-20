#!/usr/bin/env python3
"""参考音色克隆样本的自动优选（治"克隆音色学会乱停顿"）

选样标准（依据 IndexTTS issue #337 维护者确认 + 实测相关性 r=-0.97）:
  1. 活动率(VAD) 越高越好——低于 -35dB 的静音占比越低，克隆越不会学着停顿
  2. 零内部长停顿(>0.25s) —— prompt 里的停顿会被学走
  3. 时长 2-6s（官方 demo 样本中位 2.7s；>15s 会被硬切）
  4. 基频稳定（std 小 = 情绪平稳，避免把"喊口号"的腔调学走）
  5. 无削波、响度一致

用法: python pick_reference.py <segments.zh.json> <vocals.wav> <outdir> [--top N] [--ids 1,2,3]
输出: <outdir>/ref_auto_1.wav ... + ref_report.json（含每个候选的全部指标）
"""
import argparse, json, os, subprocess, sys
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from audio_io import decode_audio
from toolchain import ffmpeg


def load_mono(path, sr_target=22050):
    """用 ffmpeg 解码为 float32 单声道（不依赖 wave 模块，规避 192k/WAVE_EXTENSIBLE 坑）"""
    raw = decode_audio(path, ac=1, ar=sr_target)
    return np.frombuffer(raw, dtype=np.float32), sr_target


def profile(x, sr, sil_db=-35.0, sil_min=0.25):
    hop = int(sr * 0.01)
    if len(x) < hop * 2:
        return None
    fr = [x[i:i + hop] for i in range(0, len(x) - hop, hop)]
    db = 20 * np.log10(np.array([float(np.sqrt((f ** 2).mean() + 1e-12)) for f in fr]) + 1e-12)
    sil = db < sil_db
    gaps, cur = [], None
    for i, v in enumerate(sil):
        if v and cur is None:
            cur = i
        elif not v and cur is not None:
            if (i - cur) * 0.01 >= sil_min:
                gaps.append(round((i - cur) * 0.01, 2))
            cur = None
    return dict(dur=round(len(x) / sr, 2), vad=round(float(1 - sil.mean()), 3),
                ngap=len(gaps), gaps=gaps, peak=round(float(np.abs(x).max()), 3))


def f0_stats(x, sr):
    """基频中位数与标准差（pyin 太慢，用自相关近似）"""
    try:
        import librosa
        f0, _, _ = librosa.pyin(x, fmin=70, fmax=500, sr=sr, frame_length=1024)
        v = f0[~np.isnan(f0)]
        if len(v) < 5:
            return None, None
        return float(np.median(v)), float(np.std(v))
    except Exception:
        return None, None


def cut(src, dst, start, dur):
    r = subprocess.run([ffmpeg(), "-y", "-v", "error", "-i", src,
                        "-ss", str(start), "-t", str(dur),
                        "-ac", "1", "-ar", "22050", dst,
                        "-af", ("silenceremove=start_periods=1:start_threshold=-45dB,"
                                "areverse,silenceremove=start_periods=1:start_threshold=-45dB,"
                                "areverse,loudnorm=I=-20:TP=-3")],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"切片失败 @{start}({dur}s): {r.stderr[-300:]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("segments_json")
    ap.add_argument("vocals")
    ap.add_argument("outdir")
    ap.add_argument("--top", type=int, default=3)
    ap.add_argument("--ids", default=None, help="只评估指定段 id（逗号分隔）")
    ap.add_argument("--min-dur", type=float, default=2.0)
    ap.add_argument("--max-dur", type=float, default=6.0)
    args = ap.parse_args()

    segs = json.load(open(args.segments_json))["segments"]
    only = {int(x) for x in args.ids.split(",")} if args.ids else None
    os.makedirs(args.outdir, exist_ok=True)

    x, sr = load_mono(args.vocals)
    total = len(x) / sr
    rows = []
    for s in segs:
        if only and s["id"] not in only:
            continue
        st, en = s["start"], s["end"]
        if en > total:
            continue
        clip = x[int(st * sr):int(en * sr)]
        p = profile(clip, sr)
        if not p or not (args.min_dur <= p["dur"] <= args.max_dur + 1.0):
            continue
        f0m, f0s = f0_stats(clip, sr)
        p.update(id=s["id"], start=st, end=en, zh=s.get("zh", ""),
                 en=s.get("en", ""), f0_med=f0m, f0_std=f0s)
        rows.append(p)

    # 打分: 活动率为主，内部停顿重罚，基频稳定性次之
    def score(r):
        sc = r["vad"]
        sc -= 0.25 * r["ngap"]
        if r["f0_std"] is not None:
            sc -= min(r["f0_std"], 120) / 120 * 0.15
        if r["peak"] > 0.98:
            sc -= 0.2
        return sc

    rows.sort(key=score, reverse=True)
    print(f"{'rank':>4} {'seg':>4} {'start':>7} {'dur':>5} {'VAD':>5} {'内部停顿':>8} {'f0中位':>6} {'f0std':>6}  文本")
    for i, r in enumerate(rows[:12], 1):
        print(f"{i:>4} {r['id']:>4} {r['start']:>7.1f} {r['dur']:>5.2f} {r['vad']:>5.0%} "
              f"{r['ngap']:>8} {r['f0_med'] or 0:>6.0f} {r['f0_std'] or 0:>6.1f}  {r['zh'][:28]}")

    made = []
    for i, r in enumerate(rows[:args.top], 1):
        dst = f"{args.outdir}/ref_auto_{i}.wav"
        cut(args.vocals, dst, r["start"], r["end"] - r["start"])
        made.append(dst)
        print(f"已生成 {dst}  (段{r['id']} VAD {r['vad']:.0%})")

    json.dump(dict(top=[r for r in rows[:args.top]], all=rows),
              open(f"{args.outdir}/ref_report.json", "w"), ensure_ascii=False, indent=1)
    print(f"报告: {args.outdir}/ref_report.json")


if __name__ == "__main__":
    main()
