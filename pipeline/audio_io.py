#!/usr/bin/env python3
"""音频 I/O 与文件指纹原语 —— 全项目唯一定义（2026-09-19 审计 P1-5 收口）

历史问题：file_sha256 两份（tts_dub / asr_cache）、effective_speech_interval 两份
（mix_render / qc_dub_track）、WAV 装载四份且语义各异。本模块只收口真正共享的原语，
语义不同的装载方式各自具名，不硬揉成一个函数：

  file_sha256(path)                文件 SHA-256（缓存指纹 / 模型身份）
  effective_speech_interval(item)  plan 段实际发声区间（place_start 优先于 start）
  load_wav(path)                   wave 模块读 48k 立体声 -> float32 (N,2)，混音路径
  load_pcm(path)                   wave 模块读 16-bit 任意 sr/ch -> 单声道均值 + sr，分析路径
  decode_audio(path, ac, ar, t)    ffmpeg 解码为 f32le 原始字节，调用方自行
                                   np.frombuffer —— 覆盖 wav_to_np / load_mono /
                                   audio_rms_curve 三种"ffmpeg 解码到内存"的共性

numpy 按需导入：asr_cache 等纯 stdlib 调用方 import 本模块不会拖入 numpy。
ffmpeg 二进制经 toolchain 解析（env 优先 → ffmpeg-full 兜底，禁止裸 PATH）。
"""
import hashlib
import os
import subprocess
import sys
import wave

# path 自举：本模块可被 importlib 从任意 CWD 加载（与其它共享模块一致）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from toolchain import ffmpeg


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def effective_speech_interval(plan_item):
    """plan 段的实际发声区间 [start, end)：重落轨后以 place_start 为准。"""
    start = float(plan_item.get("place_start", plan_item["start"]))
    return start, start + float(plan_item["dur"])


def load_wav(path):
    """wave 读 48k 立体声 int16 -> float32 (N,2)，范围 [-1,1)。非 48k 立体声直接拒绝。"""
    import numpy as np
    w = wave.open(path, "rb")
    assert w.getframerate() == 48000 and w.getnchannels() == 2, f"{path} 非 48k 立体声"
    x = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
    w.close()
    return x.reshape(-1, 2)


def load_pcm(path):
    """wave 读 16-bit PCM（任意 sr/ch）-> (float32 单声道均值, sr)，范围 [-1,1)。"""
    import numpy as np
    with wave.open(path, "rb") as w:
        if w.getsampwidth() != 2:
            raise ValueError(f"仅支持 16-bit PCM WAV: {path}")
        sr = w.getframerate()
        channels = w.getnchannels()
        x = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32)
    if channels > 1:
        x = x.reshape(-1, channels).mean(axis=1)
    return x / 32768.0, sr


def decode_audio(path, ac=1, ar=16000, t=None):
    """ffmpeg 解码为 f32le 原始样本字节，失败抛错（旧 wav_to_np 静默吞掉坏文件，
    会产出 0 秒空段混进音轨——坏产物不出门，这里必须炸）。

    t: 只解码前 t 秒（-t 放在 -i 前为输入侧截断，长片取能量曲线时避免整片解码）。
    """
    cmd = [ffmpeg(), "-v", "error"]
    if t is not None:
        cmd += ["-t", str(t)]
    cmd += ["-i", path, "-f", "f32le", "-acodec", "pcm_f32le",
            "-ac", str(ac), "-ar", str(ar), "pipe:1"]
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(f"解码失败 {path}: {r.stderr[-200:]}")
    return r.stdout
