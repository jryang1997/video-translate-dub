#!/usr/bin/env python3
"""ASR 转写缓存（借鉴 VideoLingo transcription_cache.py）

按「音频文件 md5 + 模型 + 转写参数」做内容寻址缓存到 .cache/asr/，
重跑同一视频（换输出目录/重灌系统）不重复转写；参数变了自动失效。

用法:
  python3 asr_cache.py key <orig_16k.wav> <模型名> <dtw预设>   # 打印缓存key
  python3 asr_cache.py get <key>                              # 命中则打印路径
  python3 asr_cache.py put <key> <en.json> <segments.en.json> # 写入缓存
"""
import hashlib, json, os, shutil, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from audio_io import file_sha256
from toolchain import whisper_cli

CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".cache", "asr")
SCHEMA = 1


def audio_md5(path):
    h = hashlib.md5(usedforsecurity=False)
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def binary_version():
    """whisper-cli 版本指纹（二进制 hash 优先，拿不到就跳过——版本未知时缓存仍可用但跨机不保证）"""
    try:
        return file_sha256(whisper_cli())
    except (FileNotFoundError, OSError):
        return None


def make_key(wav, model, dtw):
    # 2026-09-18 P0 修复: model 由"路径字符串"升级为"模型文件内容 hash"，
    # 并纳入 whisper-cli 二进制 hash——换模型/换二进制后缓存自动失效
    identity = {"schema": SCHEMA, "audio_md5": audio_md5(wav),
                "engine": "whisper.cpp",
                "model_sha256": file_sha256(model) if os.path.exists(model) else model,
                "cli_sha256": binary_version() or "unknown",
                "dtw": dtw}
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "key" and len(sys.argv) == 5:
        print(make_key(sys.argv[2], sys.argv[3], sys.argv[4]))
    elif cmd == "get" and len(sys.argv) == 3:
        d = os.path.join(CACHE_DIR, sys.argv[2])
        if os.path.exists(os.path.join(d, "en.json")) and os.path.exists(os.path.join(d, "segments.en.json")):
            print(d)
            sys.exit(0)
        sys.exit(1)  # 未命中（调用方 set -e 环境, 必须 || 容忍）
    elif cmd == "put" and len(sys.argv) == 5:
        d = os.path.join(CACHE_DIR, sys.argv[2])
        os.makedirs(d, exist_ok=True)
        for src, name in ((sys.argv[3], "en.json"), (sys.argv[4], "segments.en.json")):
            tmp = os.path.join(d, name + ".tmp")
            shutil.copyfile(src, tmp)
            os.replace(tmp, os.path.join(d, name))
        meta = {"schema": SCHEMA, "cached_at": __import__("time").strftime("%Y-%m-%d %H:%M")}
        json.dump(meta, open(os.path.join(d, "meta.json"), "w"))
        print(f"cached -> {d}")
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
