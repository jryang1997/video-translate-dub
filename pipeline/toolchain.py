#!/usr/bin/env python3
"""ffmpeg / ffprobe / whisper-cli 二进制解析 —— 全项目唯一入口
（2026-09-19 审计 P1-2 收口：此前 ffmpeg 解析策略有四种、其中两个脚本裸调 PATH）

为什么禁止 ffmpeg 裸 PATH：PATH 里的 /opt/homebrew/bin/ffmpeg 是 brew 精简版，
缺 libass，烧字幕直接出坏片。历史上 asr_audit / make_text_banners 裸调 PATH 的
ffmpeg、mix_render / tts_audio 的兜底也是 PATH——没炸只是恰好没用到 libass，
是运气不是设计。

解析顺序（与 pipeline/config.sh 的 shell 侧一致）:
  ffmpeg/ffprobe: 环境变量（显式指定）→ brew ffmpeg-full 绝对路径 → 报错
  whisper-cli:    环境变量 WHISPER_CLI → PATH（正常安装位置）→ 报错

用法: from toolchain import ffmpeg, ffprobe, whisper_cli —— 都是函数，调用时解析，
环境变量在进程启动后设置（如测试里 setdefault）也生效。
"""
import os
import shutil

_FFMPEG_FULL_BIN = "/opt/homebrew/opt/ffmpeg-full/bin"


def _resolve(name, env_var, brew_dirs=(), purpose=""):
    p = os.environ.get(env_var)
    if p:
        if os.path.exists(p):
            return p
        raise FileNotFoundError(f"{env_var}={p} 指向的文件不存在")
    for d in brew_dirs:
        cand = os.path.join(d, name)
        if os.path.isfile(cand):
            return cand
    raise FileNotFoundError(
        f"找不到 {name}（{purpose}）。安装: brew install ffmpeg-full，"
        f"或设置环境变量 {env_var} 指向完整路径")


def ffmpeg():
    return _resolve("ffmpeg", "FFMPEG", (_FFMPEG_FULL_BIN,),
                    "管线统一用 ffmpeg-full（精简版缺 libass 会烧坏字幕）")


def ffprobe():
    return _resolve("ffprobe", "FFPROBE", (_FFMPEG_FULL_BIN,), "时长/格式探测")


def whisper_cli():
    p = os.environ.get("WHISPER_CLI")
    if p and os.path.exists(p):
        return p
    w = shutil.which(os.environ.get("WHISPER_CLI", "whisper-cli"))
    if w:
        return w
    raise FileNotFoundError(
        "找不到 whisper-cli（whisper.cpp 转写/质检用）。确认已安装并在 PATH，"
        "或设置 WHISPER_CLI 指向完整路径")
