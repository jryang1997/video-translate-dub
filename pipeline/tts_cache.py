#!/usr/bin/env python3
"""TTS 段级缓存子系统 —— 指纹、发布、物化的唯一定义（2026-09-19 审计 P1-1 收口）

设计（三级缓存之 TTS 层）：
  - 每段的「不可变合成基座」存 05_tts/cache/seg_XXXX.base.wav，发布用临时文件 +
    os.replace 原子落盘，永不写坏一半；
  - cache_payload 枚举"能改变合成结果或决定保留哪条采样"的全部输入
    （文本/说话人/参考音/参数/情感向量/模型身份），指纹 = 其规范化 JSON 的 SHA-256；
  - 命中 = wav 存在且 manifest 指纹一致 → 重跑只补缺失段，参数变更自动失效；
  - param_fingerprint 去掉段位置/文本只留合成参数，跨段可比（混合参数一眼可见）；
  - materialize_base 从不可变基座重置工作 WAV——工作副本会被 trim/压缩/atempo
    破坏性修改，重跑时从这里还原。

生产端（tts_dub_indextts）与查询端（脚本/审计）共用本模块，指纹口径只有一份。
"""
import hashlib
import json
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from audio_io import file_sha256

CACHE_SCHEMA = 1


def cache_payload(seg, next_start, hard_limit, window, seg_ref, args, gen_kwargs,
                  emo_vector, model_identity):
    """Inputs that can change synthesis or which sampled take is retained."""
    return {
        "schema": CACHE_SCHEMA,
        "segment": {
            "id": seg["id"], "text": seg["zh"], "speaker": seg.get("spk", 0),
            "start": seg["start"], "end": seg["end"], "next_start": next_start,
            "hard_limit": round(hard_limit, 6), "window": round(window, 6),
        },
        "reference_sha256": file_sha256(seg_ref),
        "lang": args.lang,
        "interval_silence": args.interval_silence,
        "max_text_tokens": args.max_text_tokens,
        "sentence_pause_ms": getattr(args, "sentence_pause_ms", 0),
        "gen_kwargs": dict(sorted(gen_kwargs.items())),
        "emo_vector": emo_vector,
        "emo_alpha": args.emo_alpha,
        "duration_factor": args.duration_factor,
        "pause_retake": args.pause_retake,
        "model": model_identity,
    }


def cache_fingerprint(payload):
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def param_fingerprint(payload):
    """参数级指纹（2026-09-18 P0）: 去掉段位置/文本后仅保留合成参数,
    跨段可比——同参数所有段应同指纹; 混合参数一眼可见。"""
    d = {k: v for k, v in payload.items() if k != "segment"}
    d["segment"] = {k: v for k, v in payload["segment"].items() if k == "speaker"}
    return hashlib.sha256(json.dumps(d, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()[:16]


def manifest_synth_fingerprint(manifest):
    """Return the parameter-level fingerprint represented by one cache manifest."""
    inputs = manifest.get("inputs", {}) if isinstance(manifest, dict) else {}
    return param_fingerprint(inputs) if inputs else manifest.get("fingerprint")


def backfill_plan_provenance(plan, tts_dir):
    """Make plan items reflect the immutable cache manifest actually used."""
    for item in plan:
        manifest_path = os.path.join(
            tts_dir, "cache", f"seg_{item['id']:04d}.json")
        if not os.path.exists(manifest_path):
            continue
        try:
            with open(manifest_path) as f:
                manifest = json.load(f)
        except (OSError, ValueError, TypeError):
            continue
        fp = manifest_synth_fingerprint(manifest)
        if fp:
            item["synth_fingerprint"] = fp
    return plan


def provenance_summary(plan):
    fps = {item["synth_fingerprint"] for item in plan
           if item.get("synth_fingerprint")}
    return {"synth_fingerprints": len(fps),
            "params_uniform": len(fps) == 1}


def cache_paths(tts_dir, sid):
    cache_dir = os.path.join(tts_dir, "cache")
    return (os.path.join(cache_dir, f"seg_{sid:04d}.base.wav"),
            os.path.join(cache_dir, f"seg_{sid:04d}.json"))


def cache_is_valid(base_path, manifest_path, payload):
    if not os.path.exists(base_path) or os.path.getsize(base_path) <= 1000:
        return False
    try:
        manifest = json.load(open(manifest_path))
    except (OSError, ValueError, TypeError):
        return False
    return (manifest.get("schema") == CACHE_SCHEMA
            and manifest.get("fingerprint") == cache_fingerprint(payload))


def atomic_copy(src, dst):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    tmp = dst + f".tmp.{os.getpid()}"
    shutil.copyfile(src, tmp)
    os.replace(tmp, dst)


def publish_cache(source_wav, base_path, manifest_path, payload):
    os.makedirs(os.path.dirname(base_path), exist_ok=True)
    atomic_copy(source_wav, base_path)
    manifest = {
        "schema": CACHE_SCHEMA,
        "fingerprint": cache_fingerprint(payload),
        "inputs": payload,
    }
    tmp = manifest_path + f".tmp.{os.getpid()}"
    with open(tmp, "w") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tmp, manifest_path)


def materialize_base(base_path, working_path):
    """Reset a destructive working WAV from its immutable synthesis base."""
    atomic_copy(base_path, working_path)
