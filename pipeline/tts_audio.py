#!/usr/bin/env python3
"""TTS 音频 DSP 原语 —— 时长探测/解码/变速/静音治理的唯一定义
（2026-09-19 审计 P1-1 收口：从 tts_dub_indextts 抽出，拼轨与主循环留在主文件）

所有函数都是"一个 wav 进、处理或测量出"的文件级操作，不持有合成器状态：
  audio_dur(path)        ffprobe 实测时长（秒）
  wav_to_np(path)        ffmpeg 解码为 float32 (N,2) 48k（拼轨加法混音用）
  atempo_file(src,dst,f) 不变调提速（<=1.25 档）
  trim_silence(path)     去首尾静音（IndexTTS 开头常塞 0.2-0.7s 静音导致配音迟到）
  pause_gaps(path)       段内 >0.36s 静音计数（克隆把参考里的停顿学走，issue #337）
  compress_pauses(path)  段内过长静音压到 0.26s（重采是概率性的，这步是确定性兜底）

停顿治理的分工：重采（pause_retake）按代价函数换样本 → 压缩（compress_pauses）
确定性兜底 → 句读切块（sentence-pause-ms）的块间静音是显式设计，两者都不碰它。
"""
import os
import re
import subprocess
import sys
import wave

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from audio_io import decode_audio
from toolchain import ffmpeg, ffprobe

SR = 48000


def sh(*cmd):
    return subprocess.run(list(cmd), capture_output=True, text=True)


def audio_dur(path):
    r = sh(ffprobe(), "-v", "error", "-show_entries", "format=duration",
           "-of", "csv=p=0", path)
    if r.returncode != 0 or not r.stdout.strip():
        raise RuntimeError(f"ffprobe 失败: {r.stderr.strip()[-200:]}")
    return float(r.stdout.strip())


def wav_to_np(path):
    raw = decode_audio(path, ac=2, ar=SR)
    return np.frombuffer(raw, dtype=np.float32).reshape(-1, 2)


def atempo_file(src, dst, factor):
    r = sh(ffmpeg(), "-y", "-v", "error", "-i", src,
           "-filter:a", f"atempo={factor:.4f}", "-ar", str(SR), "-ac", "2",
           "-c:a", "pcm_s16le", dst)
    if r.returncode != 0:
        raise RuntimeError(f"atempo 失败: {r.stderr[-200:]}")


def trim_silence(path):
    """去首尾静音（-45dB 阈值）。IndexTTS 常在开头塞 0.2-0.7s 静音导致配音迟到。
    重复执行为无害空操作（静音已去则不变）。"""
    tmp = path + ".trim.wav"
    r = sh(ffmpeg(), "-y", "-v", "error", "-i", path, "-af",
           "silenceremove=start_periods=1:start_threshold=-45dB,"
           "areverse,silenceremove=start_periods=1:start_threshold=-45dB,areverse",
           "-c:a", "pcm_s16le", tmp)
    if r.returncode == 0 and os.path.getsize(tmp) > 1000:
        os.replace(tmp, path)
    else:
        if os.path.exists(tmp):
            os.remove(tmp)


def pause_gaps(path, thr=-35.0, min_d=0.36):
    """段内 >min_d 秒的静音个数（= 不自然的句中长停顿）。

    治"克隆音色爱插停顿"：IndexTTS 参考音频里的停顿会被学走（issue #337 维护者确认），
    且生成是采样的（do_sample=True），所以"重采"能换出更干净的一条。
    阈值取 0.36s：中文自然朗读在标点处约 0.15-0.35s，超过即偏长。
    （旧脚本 audit_tts_pauses.py 用 0.28s 但按"数量不超标点"判断，实测漏报严重——
     本集成片 128 段里有 184 处 >0.36s 停顿、其中 164 处 >0.5s，它却报"可疑段 0"。）
    """
    r = sh(ffmpeg(), "-hide_banner", "-i", path, "-af",
           f"silencedetect=noise={thr}dB:d={min_d}", "-f", "null", "-")
    return len(re.findall(r"silence_duration:", r.stderr))


def compress_pauses(path, thr_db=-35.0, min_d=0.36, target=0.26):
    """把段内过长的静音压到 target 秒（治"停顿拖沓"）。

    参考音频的停顿会被克隆学走，重采只能概率性缓解；后处理压缩是确定性的兜底。
    只压"内部"静音（前后都还有语音的部分），首尾静音由 trim_silence 负责。
    用短交叉淡化拼接，避免爆音。
    2026-09-18 P0 修复: 旧实现把 WAV 文件头当 PCM 读、再用 tofile() 写裸样本，
    产出头部 size 与数据不符的坏 WAV（ffprobe/wave 读出时长不一致）。现用
    wave 模块读写，保证容器合法、采样率声道不变。
    """
    r = sh(ffprobe(), "-v", "error", "-show_entries", "stream=sample_rate,channels",
           "-of", "csv=p=0", path)
    try:
        sr, ch = (int(x) for x in r.stdout.strip().splitlines()[0].split(","))
    except Exception:
        return 0
    try:
        with wave.open(path, "rb") as w:
            sampwidth = w.getsampwidth()
            nfr = w.getnframes()
            x = np.frombuffer(w.readframes(nfr), dtype=np.int16).astype(np.float32)
    except (wave.Error, EOFError, OSError):
        return 0
    if sampwidth != 2 or nfr < 10:
        return 0
    x = x[:nfr * ch].reshape(nfr, ch)
    r2 = sh(ffmpeg(), "-hide_banner", "-i", path, "-af",
            f"silencedetect=noise={thr_db}dB:d={min_d}", "-f", "null", "-")
    spans = [(float(a), float(b)) for a, b in
             re.findall(r"silence_start: ([-0-9.]+).*?silence_end: ([-0-9.]+)", r2.stderr, re.S)]
    if not spans:
        return 0
    n = len(x)
    keep, cut, cursor = [], 0, 0
    target_n = int(target * sr)
    for s, e in spans:
        i0, i1 = max(0, int(s * sr)), min(n, int(e * sr))
        if i0 - cursor < int(0.05 * sr) or n - i1 < int(0.05 * sr):
            continue          # 首尾静音不归这里管
        if (i1 - i0) / sr <= target + 0.02:
            continue
        keep.append((cursor, i0))
        # 压缩不是全删：保留 target 秒静音维持句读节奏（2026-09-18 修正）
        keep.append((i0, i0 + target_n))
        cursor = i1
        cut += 1
    if not cut:
        return 0
    keep.append((cursor, n))
    cross = max(1, int(0.008 * sr))
    out = []
    for idx, (a, b) in enumerate(keep):
        seg = x[a:b]
        if idx > 0 and len(out) and len(seg) > cross:
            tail = out.pop()
            ct = min(cross, len(tail), len(seg))
            out.append(tail[:len(tail) - ct])
            out.append((tail[len(tail) - ct:] + seg[:ct]) / 2)
            out.append(seg[ct:])
        else:
            out.append(seg)
    merged = np.clip(np.concatenate(out), -32768, 32767).astype(np.int16)
    tmp = path + ".cp.wav"
    with wave.open(tmp, "w") as w:
        w.setnchannels(ch); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes(merged.tobytes())
    os.replace(tmp, path)
    return cut
