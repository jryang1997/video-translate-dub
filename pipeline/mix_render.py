#!/usr/bin/env python3
"""混音 + 渲染两版成品（水印由 produce_video.sh 统一追加）

输入: 原视频 mp4 / orig_48k.wav / demucs 伴奏 / dub_track.wav + plan.json
输出: 07_output/
  A_原声_中英字幕.mp4       原音 + 中英双语字幕
  B_中文配音_中英字幕.mp4    伴奏 + 中文配音 + 中文字幕
响度策略: 伴奏在说话段压到配音下方约 -13dB，整轨 loudnorm 到 -14 LUFS
"""
import argparse, json, os, subprocess, sys, wave
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from audio_io import effective_speech_interval, load_wav
from toolchain import ffmpeg, ffprobe

SR = 48000


def sh(*cmd, **kw):
    return subprocess.run(list(cmd), capture_output=True, text=True, **kw)


def rms(x):
    return float(np.sqrt((x ** 2).mean()) + 1e-9)


def subtitle_files(subs_dir):
    return (os.path.join(subs_dir, "bilingual.ass"),
            os.path.join(subs_dir, "bilingual_dub.ass"))


def ensure_48k(path):
    """不是 48k 立体声就转成 48k（demucs 输出为 44.1k）"""
    r = sh(ffprobe(), "-v", "error", "-show_entries",
           "stream=sample_rate,channels", "-of", "csv=p=0", path)
    try:
        sr, ch = r.stdout.strip().splitlines()[0].split(",")
        if int(sr) == SR and int(ch) == 2:
            return path
    except Exception:
        pass
    out = path.replace(".wav", "_48k.wav")
    sh(ffmpeg(), "-y", "-v", "error", "-i", path, "-ar", str(SR), "-ac", "2",
       "-c:a", "pcm_s16le", out)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("raw_mp4")
    ap.add_argument("orig_48k")
    ap.add_argument("dub_track")
    ap.add_argument("plan_json")
    ap.add_argument("outdir")
    ap.add_argument("--subs-dir", required=True)
    ap.add_argument("--bg-wav", default=None, help="demucs no_vocals.wav；缺省用原始音轨")
    ap.add_argument("--tts-dir", required=True)
    args = ap.parse_args()

    source_ass, dubbed_ass = subtitle_files(args.subs_dir)
    for label, path in (("原声", source_ass), ("配音", dubbed_ass)):
        if not os.path.exists(path):
            sys.exit(f"缺少{label}字幕: {path}")

    plan = json.load(open(args.plan_json))
    dub = load_wav(ensure_48k(args.dub_track))
    bg = load_wav(ensure_48k(args.bg_wav)) if args.bg_wav else load_wav(ensure_48k(args.orig_48k))

    n = max(len(dub), len(bg))
    if len(dub) < n: dub = np.vstack([dub, np.zeros((n - len(dub), 2), np.float32)])
    if len(bg) < n:  bg = np.vstack([bg, np.zeros((n - len(bg), 2), np.float32)])

    # 说话窗口内计算响度比
    voice_rms, bg_rms = [], []
    for p in plan["plan"]:
        start, end = effective_speech_interval(p)
        i0, i1 = int(start * SR), min(int(end * SR), n)
        if i1 <= i0: continue
        voice_rms.append(rms(dub[i0:i1]))
        bg_rms.append(rms(bg[i0:i1]))
    vr, br = np.median(voice_rms), np.median(bg_rms)
    # 目标: 说话时 bg 比 voice 低 13dB
    gain = float(np.clip(vr * 10 ** (-13 / 20) / max(br, 1e-9), 0.15, 0.95))
    print(f"配音 RMS={vr:.4f}  伴奏 RMS={br:.4f}  伴奏增益={gain:.3f}")
    mix = bg * gain + dub
    peak = np.abs(mix).max()
    if peak > 0.99: mix *= 0.99 / peak
    mix_wav = f"{args.outdir}/../06_mix/dubbed_mix.wav"
    with wave.open(mix_wav, "w") as w:
        w.setnchannels(2); w.setsampwidth(2); w.setframerate(SR)
        w.writeframes((np.clip(mix, -1, 1) * 32767).astype(np.int16).tobytes())

    # loudnorm 单遍（两遍更精确，先单遍可用）
    norm_wav = f"{args.outdir}/../06_mix/dubbed_norm.wav"
    r = sh(ffmpeg(), "-y", "-v", "error", "-i", mix_wav,
           "-af", "loudnorm=I=-14:TP=-1.5:LRA=11", "-ar", str(SR), "-ac", "2", norm_wav)
    if r.returncode != 0:
        print("loudnorm 失败，使用未归一化混音", r.stderr[-300:]); norm_wav = mix_wav

    fonts_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fonts")
    source_vf = f"ass=filename={source_ass}:fontsdir={fonts_dir}"
    dubbed_vf = f"ass=filename={dubbed_ass}:fontsdir={fonts_dir}"
    # 渲染标准双版本：
    #   B_中文配音_中英字幕.mp4 —— 中文配音母版，高码率保真
    #   A_原声_中英字幕.mp4     —— 双语原声版，用于对照学习与原声留存
    # 软编 x264 两路并行跑满多核（每路内存 ~0.5GB，对 32GB 机器无压力）
    enc = ["-c:v", "libx264", "-preset", "fast", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "192k"]
    enc_b = ["-c:v", "libx264", "-b:v", "8M", "-preset", "fast", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-b:a", "192k"]

    jobs = [
        ("B_中文配音_中英字幕.mp4",
         ["-i", args.raw_mp4, "-i", norm_wav,
          "-map", "0:v:0", "-map", "1:a:0", "-vf", dubbed_vf, *enc_b]),
        ("A_原声_中英字幕.mp4",
         ["-i", args.raw_mp4, "-i", args.orig_48k,
          "-map", "0:v:0", "-map", "1:a:0", "-vf", source_vf, *enc]),
    ]
    procs = []
    for name, cmd in jobs:
        print("渲染(并行):", name, flush=True)
        procs.append((name, subprocess.Popen(
            [ffmpeg(), "-y", "-v", "error", *cmd, f"{args.outdir}/{name}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)))
    failed = []
    for name, p in procs:
        _, err = p.communicate()
        if p.returncode != 0:
            failed.append((name, (err or b"").decode()[-400:]))
    if failed:
        for name, err in failed:
            print(f"  {name} 失败: {err}")
        sys.exit(1)
    print("全部渲染完成")


if __name__ == "__main__":
    main()
