#!/usr/bin/env python3
"""转写后首句对齐校验（秒级，防"配音在片头 logo 期就开口"）

背景: DTW 偶发将首词对齐到片头音乐（片头音量突增处），导致段 1 起点被异常提前。
本脚本在转写完成后秒级比对音频真实发声拐点与转写首词时间戳，拦截抢跑异常。

原理: 语音拐点 = RMS 从静音(<0.05) 突增到语音(>0.15) 的时刻。
whisper 首词对齐比拐点早 2s 以上 → 首词疑似贴到片头音乐上。

用法: python3 check_first_align.py <orig_48k.wav> <segments.en.json> [--audio-lead 秒]
      （需 numpy：跑在 index-tts/.venv 或 .venv 里均可；produce_video.sh 已配置）
退出码: 0=正常 / 3=首词疑似误对齐（人工核对第一句）
"""
import argparse, json, os, sys
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from audio_io import decode_audio


def audio_rms_curve(path, win=0.5, max_sec=20, sr=16000):
    raw = decode_audio(path, ac=1, ar=sr, t=max_sec)
    x = np.frombuffer(raw, dtype=np.float32)
    n = min(len(x) // int(win * sr), int(max_sec / win))
    return [float(np.sqrt((x[i * int(win * sr):(i + 1) * int(win * sr)] ** 2).mean()))
            for i in range(n)], win


def first_onset(rms, win):
    """静音→语音突增拐点: 前窗 <0.05 且当前窗 >0.15"""
    for i in range(1, len(rms)):
        if rms[i - 1] < 0.05 and rms[i] > 0.15:
            return i * win
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("orig_wav")
    ap.add_argument("segments_en_json")
    ap.add_argument("--audio-lead", type=float, default=2.0,
                    help="首词比拐点早多少秒算可疑（默认 2s）")
    ap.add_argument("--whisper-json", default=None,
                    help="en.json（whisper 全量输出）——用于取首词置信度")
    args = ap.parse_args()

    segs = json.load(open(args.segments_en_json))["segments"]
    first = segs[0]

    rms, win = audio_rms_curve(args.orig_wav)
    onset = first_onset(rms, win)
    if onset is None:
        print("✅ 首句校验: 前 20s 未检出静音→语音拐点（开头即连续语音），跳过")
        return

    lead = onset - first["start"]
    print(f"首句校验: 段1 start={first['start']:.1f}s | 能量拐点={onset:.1f}s "
          f"| 语音提前量={lead:.1f}s")

    # 置信度旁证
    p_info = ""
    suspicious = lead > args.audio_lead
    if args.whisper_json:
        try:
            w = json.load(open(args.whisper_json))["transcription"][0]
            toks = [t for t in w.get("tokens", []) if not t["text"].startswith("[_")]
            if toks:
                p = toks[0]["p"]
                p_info = f" | 首词置信度={p:.2f}"
                if p < 0.5 and lead > 0.5:
                    suspicious = True
        except (OSError, KeyError, ValueError):
            pass

    if suspicious:
        print(f"⛔ 首词疑似误对齐到片头音乐{p_info}！")
        print("   处理: 对原声 0-10s 逐 0.25s 看 RMS 找真实开口，")
        print("   同步改 zh/en segments.json 与 plan.json 段1 的 start 后 relayout-only。")
        sys.exit(3)
    print("✅ 首句校验通过")


if __name__ == "__main__":
    main()
