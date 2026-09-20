#!/usr/bin/env python3
"""segments.zh.json -> IndexTTS-2.5 音色克隆配音 + dub_track.wav（48k 立体声）+ plan.json

与 tts_dub.py 同接口，产物可直供 mix_render.py：
- 参考音频来自原视频人声（demucs vocals 截段），克隆原说话人音色
- 窗口不够时用 ffmpeg atempo 提速（<=1.25），与 edge 版策略一致
- 必须在 index-tts 仓库目录内 import（HF_HUB_CACHE 相对路径），脚本自行 os.chdir
"""
import argparse, json, os, re, sys, time, wave
import numpy as np

# 同目录共享模块（脚本可被 importlib 从任意 CWD 加载，先自举 path）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from audio_io import file_sha256
# 音频 DSP 原语唯一来源（审计 P1-1 收口）：时长/解码/变速/静音治理
from tts_audio import (SR, audio_dur, atempo_file, compress_pauses,
                       pause_gaps, sh, trim_silence, wav_to_np)
from toolchain import ffprobe
# 缓存子系统唯一来源（审计 P1-1 收口）：指纹/发布/物化与查询端共用
from tts_cache import (atomic_copy, backfill_plan_provenance, cache_is_valid,
                       cache_payload, cache_paths, materialize_base,
                       param_fingerprint, provenance_summary, publish_cache)
# 落轨契约唯一来源（审计 P1-1/P0-3）：窗口/顺延/落点与 precheck 同源
from layout import START_GUARD, compute_placements, segment_window
# 估时系数唯一来源（审计 P0-2）：校准回喂与 precheck/账本同一套计数口径
from zh_duration import count_han, count_punct


def finalize_run(data, plan, tts_dir, args, metadata, partial=False):
    """Write formal full-track artifacts; partial previews must leave them untouched."""
    if partial:
        return None
    backfill_plan_provenance(plan, tts_dir)
    truncated, truncated_ids = assemble_track(data, plan, tts_dir, args)
    output = dict(metadata)
    output["plan"] = plan
    with open(os.path.join(tts_dir, "plan.json"), "w") as f:
        json.dump(output, f, ensure_ascii=False, indent=1)
    return truncated, truncated_ids


def pause_budget(text):
    """允许的句中长停顿数 = 标点数（每个标点最多一次停顿）。
    标点计数复用 zh_duration.count_punct——与估时/校准回喂同一口径，改一处全生效。"""
    return count_punct(text)


def text_has_sentence_split(text):
    """段文本是否含 ≥2 个语义句（即合成时走了切块拼接, 块间停顿是显式设计的）"""
    return len(split_sentences(text)) > 1


def split_sentences(text):
    """按句末标点切分（？。！!?），保留标点在前块尾部。单块返回 [text]。"""
    parts = re.split(r"(?<=[？。！!?])", text)
    return [p for p in parts if p.strip()]


def tts_infer_chunked(tts, text, out_path, seg_ref, args, emo_vector, gen_kwargs,
                      synth_timer=None):
    """句末标点强制切块合成（治"问号后连读"听感）。

    单块整段合成时 IndexTTS 对句末标点的停顿不受控（实测 0.09-0.23s 随机），
    与段间 1.5-3s 的空隙形成悬殊反差——听者把段间空隙当"句界"，段内停顿就像没有。
    这里按句末标点把段拆成多句、逐句合成、块间插 sentence-pause-ms 显式静音，
    句读节奏精确可控。单句段或 sentence-pause-ms=0 时退化为普通整段合成。
    """
    pause_ms = getattr(args, "sentence_pause_ms", 0)
    sents = split_sentences(text) if pause_ms > 0 else [text]
    if len(sents) <= 1:
        if synth_timer is not None:
            ts = time.time()
        tts.infer(spk_audio_prompt=seg_ref, text=text,
                  output_path=out_path, lang=args.lang,
                  interval_silence=args.interval_silence,
                  max_text_tokens_per_segment=args.max_text_tokens,
                  emo_vector=emo_vector, emo_alpha=args.emo_alpha,
                  duration_factor=args.duration_factor, **gen_kwargs)
        if synth_timer is not None:
            synth_timer.append(time.time() - ts)
        return
    tmp_dir = os.path.dirname(out_path)
    pieces = []
    for i, sent in enumerate(sents):
        piece = out_path + f".s{i}.{os.getpid()}.wav"
        if synth_timer is not None:
            ts = time.time()
        tts.infer(spk_audio_prompt=seg_ref, text=sent,
                  output_path=piece, lang=args.lang,
                  interval_silence=args.interval_silence,
                  max_text_tokens_per_segment=args.max_text_tokens,
                  emo_vector=emo_vector, emo_alpha=args.emo_alpha,
                  duration_factor=args.duration_factor, **gen_kwargs)
        if synth_timer is not None:
            synth_timer.append(time.time() - ts)
        trim_silence(piece)
        pieces.append(piece)
    # 拼接: 块间插显式静音
    r = sh(ffprobe(), "-v", "error", "-show_entries", "stream=sample_rate,channels",
           "-of", "csv=p=0", pieces[0])
    try:
        sr, ch = (int(x) for x in r.stdout.strip().splitlines()[0].split(","))
    except Exception:
        sr, ch = SR, 2
    sil = b"\x00" * int(sr * pause_ms / 1000) * ch * 2
    tmp_join = out_path + f".join.{os.getpid()}.wav"
    with wave.open(tmp_join, "w") as w:
        w.setnchannels(ch); w.setsampwidth(2); w.setframerate(sr)
        for i, piece in enumerate(pieces):
            with wave.open(piece, "rb") as r2:
                frames = r2.readframes(r2.getnframes())
                if r2.getframerate() != sr or r2.getnchannels() != ch:
                    raise RuntimeError(f"分块采样率不一致: {piece}")
            w.writeframes(frames)
            if i < len(pieces) - 1:
                w.writeframes(sil)
    os.replace(tmp_join, out_path)
    for piece in pieces:
        if os.path.exists(piece):
            os.remove(piece)


def assemble_track(data, plan, tts_dir, args):
    """把每段 seg_XXXX.wav 按节奏铺成 dub_track.wav，返回 (截断段数, 截断段id列表)。

    落轨策略（--fill-window，默认 1.25）：
      旧的「绝对落轨」把每句钉死在原片说话时刻，中文比英文短时句尾留一大段空白
      （实测语音占比只有 51%，原片英语旁白占 75%，听感"断断续续"）。
      现在按比例分配句间空隙：先算全片余量，按各段窗口大小摊到每句之间，
      再逐句生成落点（浮动量 = 窗口 × (fill_window-1) × 该段在余量中的占比），
      效果是全片铺开、句间节奏接近原片。

    2026-09-17 修「短句被硬提速」：增加 BORROW_SILENCE 可借静音 + 顺延不压缩。
    详见循环内注释；改这里前先看 pipeline/README.md 的落轨说明。
    """
    full_segs = data["segments"]
    # 音轨长度必须覆盖三种情况，否则末尾配音会被裁掉：
    #   ① 最后一段的结束时间；
    #   ② 已落轨内容的末尾（分段补跑场景）；
    #   ③ 落点顺延后的理论末尾 —— 语音总长 + 段间最小间隔（修 2026-09-17：
    #      顺延会让末尾后移，旧口径只按原片末尾算长度，顺延到片尾附近的句子会被裁掉）。
    track_end = max(
        full_segs[-1]["end"] + 1.0,
        max((p.get("place_start", p["start"]) + p["dur"] for p in plan), default=0.0) + 1.0,
        sum(p["dur"] for p in plan) + args.min_gap * max(len(plan) - 1, 0) + 1.0,
    )
    track = np.zeros((int(SR * track_end), 2), dtype=np.float32)
    trunk_end = track_end
    truncated, truncated_ids = 0, []
    durations = {p["id"]: float(p["dur"]) for p in plan}
    k_use, placements = compute_placements(
        full_segs, durations, args.fill_window, args.max_shift,
        args.min_gap, trunk_end)
    if k_use > 0.0 and placements:
        wins = [max(0.1, (full_segs[k + 1]["start"]
                          if k + 1 < len(full_segs) else trunk_end)
                    - s["start"])
                for k, s in enumerate(full_segs) if s["id"] in durations]
        speech = sum(durations.values())
        pause_now = (sum(wins) - speech) / max(len(durations), 1)
        slack = sum(wins) - speech - args.min_gap * len(durations)
        print(f"填窗: 余量{slack:.0f}s 浮动{k_use:.0%} 语音占比"
              f"{100*speech/sum(wins):.0f}% 平均句间停顿{pause_now:.2f}s", flush=True)
    # 按 id 定位原文段，不按索引——分段补跑（--only）时 plan 会短于 full_segs，
    # 用 plan 的下标去索引 full_segs 会错位，拿错格子和下一段起点。
    seg_by_id = {x["id"]: x for x in full_segs}
    order = [x["id"] for x in full_segs]
    for p in plan:
        seg_path = f"{tts_dir}/seg_{p['id']:04d}.wav"
        if not os.path.exists(seg_path):
            print(f"  跳过段{p['id']}：缺 {seg_path}")
            continue
        a = wav_to_np(seg_path)
        seg_dur = len(a) / SR
        cur = seg_by_id.get(p["id"])
        idx = order.index(p["id"]) if cur is not None else -1
        nxt = (full_segs[idx + 1]["start"]
               if 0 <= idx < len(full_segs) - 1 else trunk_end)
        placement = placements.get(p["id"])
        if placement is None:
            start_pl = float(p["start"])
            hard_limit = max(p["start"] + seg_dur + 0.02,
                             min(nxt - START_GUARD, trunk_end - 0.02))
        else:
            start_pl = placement["place_start"]
            hard_limit = placement["hard_limit"]
        p["place_start"] = round(start_pl, 3)
        p["hard_limit"] = round(hard_limit, 3)
        i0 = int(start_pl * SR)
        # 硬边界截断：宁可在句尾收掉，也不压进下一句（下一句开头叠上来会像吃字）
        limit = int(hard_limit * SR)
        grace = int(0.15 * SR) if start_pl + seg_dur - hard_limit <= 0.15 else 0
        if i0 + len(a) > limit + grace:
            a = a[:max(0, limit + grace - i0)]
            truncated += 1
            truncated_ids.append(p["id"])
        n = min(len(a), len(track) - i0)
        if n > 0:
            track[i0:i0 + n] += a[:n]
    peak = np.abs(track).max()
    if peak > 0.99:
        track *= 0.98 / peak
    with wave.open(f"{tts_dir}/dub_track.wav", "w") as w:
        w.setnchannels(2); w.setsampwidth(2); w.setframerate(SR)
        w.writeframes((np.clip(track, -1, 1) * 32767).astype(np.int16).tobytes())
    return truncated, truncated_ids


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("segments_json")
    ap.add_argument("tts_dir")
    ap.add_argument("--ref", required=True, help="克隆参考音频 wav（原视频人声截段）")
    ap.add_argument("--repo", required=True, help="index-tts 仓库目录")
    ap.add_argument("--model-dir", required=True, help="checkpoints 目录（含 config.yaml）")
    ap.add_argument("--lang", default="zh")
    ap.add_argument("--only", default=None, help="只合成指定段 id（逗号分隔），试听用")
    ap.add_argument("--fit", action="store_true",
                    help="读取已有 plan.json，对超窗段用 duration_factor 二次合成，替代 atempo 提速")
    ap.add_argument("--emo-vector", default=None,
                    help="8维情感向量(喜,怒,哀,惧,厌恶,低落,惊喜,平静)，如 0.6,0,0,0,0,0,0.3,0.1；全0=关闭")
    ap.add_argument("--emo-alpha", type=float, default=1.0,
                    help="情感强度 0-1（默认 1.0）")
    ap.add_argument("--interval-silence", type=int, default=200,
                    help="IndexTTS 内部分段拼接时插入的静音毫秒数（默认 200）。"
                         "句子被按标点切成多块时，块间静音就是这个值——太大听起来像"
                         "句中乱停顿，太小会粘连。可用 INDEXTTS_INTERVAL_SILENCE 覆盖")
    ap.add_argument("--max-text-tokens", type=int, default=120,
                    help="单个内部合成段的最大 token 数（默认 120）")
    ap.add_argument("--sentence-pause-ms", type=int, default=0,
                    help="段内句末标点（？。！）强制切块后的块间停顿毫秒数。"
                         "0=关闭（整段合成，标点停顿由模型自由发挥 0.1-0.3s 不受控）；"
                         "建议 350-450：与段间空隙形成合理层级，治'问号后连读'。"
                         "开启后含多句的段会逐句合成，耗时约增加句数倍")
    ap.add_argument("--gen", default=None,
                    help="IndexTTS 生成参数覆盖，逗号分隔 k=v，如 "
                         "num_beams=3,top_k=1,length_penalty=-0.8,repetition_penalty=1.1。"
                         "用于压制「乱拖长/爱插停顿」（length_penalty 取负 = 别拖长）")
    ap.add_argument("--fill-window", type=float, default=1.25,
                    help="落轨填窗倍率（默认 1.25）: 每句最多往后铺到「段窗×该值」，"
                         "治「句间留白过多」。0 = 退回旧的绝对时刻落轨")
    ap.add_argument("--max-shift", type=float, default=0.5,
                    help="每句最多后移「段窗 × 该值」的比例（默认 0.5），防止短句被推过头")
    ap.add_argument("--min-gap", type=float, default=0.22,
                    help="相邻两句之间的最小间隔秒数（默认 0.22）")
    ap.add_argument("--relayout-only", action="store_true",
                    help="不加载模型、不合成，只用现有 seg_*.wav + plan.json 重跑落轨与混音。"
                         "改落轨逻辑或只想重排节奏时用（几秒完成，内存 <1GB）")
    ap.add_argument("--duration-factor", type=float, default=1.0,
                    help="IndexTTS 语速: >1 更慢、<1 更快（默认 1.0，有效 0.5-2.0）")
    ap.add_argument("--compress-pauses", action=argparse.BooleanOptionalAction, default=True,
                    help="段内过长静音压到 0.26s（默认开；确定性兜底，治停顿拖沓）")
    ap.add_argument("--pause-retake", action=argparse.BooleanOptionalAction, default=True,
                    help="句中长停顿超过「标点数+1」时自动重采（默认开，最多 2 次，"
                         "选停顿最少且不超窗的一条）")
    args = ap.parse_args()
    if os.environ.get("INDEXTTS_INTERVAL_SILENCE"):
        args.interval_silence = int(os.environ["INDEXTTS_INTERVAL_SILENCE"])

    # 防呆（2026-09-17 教训: 想重排落轨却忘了 --relayout-only，会触发全量重合成
    # 且当前参数会覆盖 cache 指纹 → 白烧半小时 GPU）。满足全部条件才算"疑似"：
    # 有现成 plan + 有现成 seg wav + 没说 --only/--fit，提醒一次，可用 --yes 确认真要重合成。
    tts_dir_probe = os.path.abspath(args.tts_dir)
    if (not args.relayout_only and not args.only and not args.fit
            and os.path.exists(os.path.join(tts_dir_probe, "plan.json"))
            and os.path.exists(os.path.join(tts_dir_probe, "seg_0001.wav"))
            and not os.environ.get("TTS_FORCE_RESYNTH")):
        print("⚠️  05_tts 里已有 plan.json 和 seg_*.wav —— 你是想 --relayout-only 重排落轨吗？")
        print("    真要全量重合成: 重跑并加环境变量 TTS_FORCE_RESYNTH=1（cache 指纹会变，")
        print("    与本次参数不同的段会重新合成）。脚本本次停止，未做任何修改。")
        sys.exit(5)

    gen_kwargs = {}
    if args.gen:
        for kv in args.gen.split(","):
            k, _, v = kv.partition("=")
            k = k.strip()
            try:
                gen_kwargs[k] = int(v)
            except ValueError:
                try:
                    gen_kwargs[k] = float(v)
                except ValueError:
                    gen_kwargs[k] = v.strip()
    if gen_kwargs:
        print(f"生成参数覆盖: {gen_kwargs}", flush=True)

    emo_vector = None
    if args.emo_vector:
        vals = [float(x) for x in args.emo_vector.split(",")]
        assert len(vals) == 8, "emo-vector 需要 8 个值(喜,怒,哀,惧,厌恶,低落,惊喜,平静)"
        if any(v > 0.001 for v in vals):
            emo_vector = vals

    os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
    # 相对路径先全部转绝对，os.chdir 之后相对路径会失效
    segments_json = os.path.abspath(args.segments_json)
    tts_dir = os.path.abspath(args.tts_dir)
    ref = os.path.abspath(args.ref)
    repo = os.path.abspath(args.repo)
    model_dir = os.path.abspath(args.model_dir)
    model_identity = {
        "engine": "IndexTTS-2.5",
        "config_sha256": file_sha256(os.path.join(model_dir, "config.yaml")),
    }
    os.chdir(repo)  # infer_v2_5 在 import 时把 HF_HUB_CACHE 设为 ./checkpoints/hf_cache
    sys.path.insert(0, repo)

    # 纯重落轨：不加载模型，用现有 seg_*.wav 重新铺轨（改落轨逻辑 / 只想重排节奏时用）
    if args.relayout_only:
        data = json.load(open(segments_json))
        old = json.load(open(f"{tts_dir}/plan.json"))
        plan = old["plan"]
        # 回填段级 provenance: 从 cache manifest 取各段真实合成指纹
        backfill_plan_provenance(plan, tts_dir)
        print(f"重落轨: {len(plan)} 段（不加载模型、不重新合成）", flush=True)
        truncated, truncated_ids = assemble_track(data, plan, tts_dir, args)
        # 合成履历（ref/emo_vector/emo_alpha）必须原样保留——这些参数描述的是
        # 各 seg_*.wav 的真实来源，本次调用没重新合成，不能拿当前命令行参数覆盖
        output = {k: old[k] for k in old if k != "plan"}
        output.setdefault("voice", "IndexTTS-2.5-clone")
        output["relayout"] = {"emo_vector_this_call": emo_vector,
                              "emo_alpha_this_call": args.emo_alpha,
                              "fill_window": args.fill_window}
        output["plan"] = plan
        with open(f"{tts_dir}/plan.json", "w") as f:
            json.dump(output, f, ensure_ascii=False, indent=1)
        print(f"重落轨完成 | 截断段: {truncated} {truncated_ids if truncated else ''} "
              f"| 填窗: {'on' if args.fill_window>0 else 'off'}", flush=True)
        if truncated > 0:
            print(f"FAIL: {truncated} 段仍被截断，需缩短这些段译文后重跑", flush=True)
            sys.exit(2)
        return

    t0 = time.time()
    print("加载 IndexTTS-2.5（首次要加载多个子模型）...", flush=True)
    from indextts.infer_v2_5 import IndexTTS2
    try:
        tts = IndexTTS2(cfg_path=f"{model_dir}/config.yaml",
                        model_dir=model_dir, use_bf16=True)
    except Exception as e:
        print(f"bf16 加载失败({e})，回退 fp32", flush=True)
        tts = IndexTTS2(cfg_path=f"{model_dir}/config.yaml",
                        model_dir=model_dir, use_bf16=False)
    print(f"模型就绪 device={tts.device} 用时 {time.time()-t0:.0f}s", flush=True)

    data = json.load(open(segments_json))
    full_segs = data["segments"]
    seg_index = {s["id"]: i for i, s in enumerate(full_segs)}
    segs = full_segs
    if args.only:
        keep = {int(x) for x in args.only.split(",")}
        segs = [s for s in full_segs if s["id"] in keep]
        if not segs:
            sys.exit(f"--only 没有匹配到任何段: {sorted(keep)}")

    os.makedirs(tts_dir, exist_ok=True)
    plan, synth_s = [], 0.0
    retaken, pause_fixed, pause_bad, compressed = 0, 0, [], 0

    if args.fit:
        # 二次合成: 超窗段 best-of-N 重采（生成有随机性），保留最短一条，尽量免变速
        old = json.load(open(f"{tts_dir}/plan.json"))["plan"]
        todo = [p for p in old if p["atempo"] > 1.1]
        print(f"fit 模式: {len(todo)} 段超窗(>1.1x)，best-of-2 重采", flush=True)
        for p in todo:
            sid = p["id"]
            s = full_segs[seg_index[sid]]
            text = s["zh"]
            nxt, hard_limit, window = segment_window(full_segs, seg_index[sid])
            spk = s.get("spk", 0)
            seg_ref = ref
            if spk:
                speaker_ref = os.path.join(tts_dir, f"spk_{spk}.wav")
                if os.path.exists(speaker_ref):
                    seg_ref = speaker_ref
            payload = cache_payload(s, nxt, hard_limit, window, seg_ref, args,
                                    gen_kwargs, emo_vector, model_identity)
            base_path, manifest_path = cache_paths(tts_dir, sid)
            os.makedirs(os.path.dirname(base_path), exist_ok=True)
            if not cache_is_valid(base_path, manifest_path, payload):
                initial = base_path + f".initial.{os.getpid()}.wav"
                tts_infer_chunked(tts, text, initial, seg_ref, args, emo_vector,
                                    gen_kwargs)
                publish_cache(initial, base_path, manifest_path, payload)
                os.remove(initial)
            raw = f"{tts_dir}/seg_{sid:04d}.wav"
            materialize_base(base_path, raw)
            trim_silence(raw)
            best_dur = audio_dur(raw)
            best_clean = None
            for take in range(2):
                cand_clean = base_path + f".fit.{take}.{os.getpid()}.wav"
                cand = f"{tts_dir}/tmp_take_{sid}_{take}.wav"
                ts = time.time()
                tts_infer_chunked(tts, text, cand_clean, seg_ref, args, emo_vector,
                                    gen_kwargs)
                synth_s += time.time() - ts
                atomic_copy(cand_clean, cand)
                trim_silence(cand)
                d = audio_dur(cand)
                if d < best_dur:
                    if best_clean and os.path.exists(best_clean):
                        os.remove(best_clean)
                    best_dur, best_clean = d, cand_clean
                elif os.path.exists(cand_clean):
                    os.remove(cand_clean)
                if os.path.exists(cand):
                    os.remove(cand)
                if best_dur <= window:
                    break
            if best_clean:
                publish_cache(best_clean, base_path, manifest_path, payload)
                os.remove(best_clean)
            materialize_base(base_path, raw)
            trim_silence(raw)
            print(f"[fit] seg{sid:3d} -> {best_dur:.2f}s / 窗{window:.2f}s"
                  + (" 免变速" if best_dur <= window else ""), flush=True)
        print(f"fit 完成，请重跑普通模式重组音轨 | 二次合成耗时 {synth_s:.0f}s", flush=True)
        return

    for k, s in enumerate(segs):
        sid, start, end = s["id"], s["start"], s["end"]
        text = s["zh"]
        full_index = seg_index[sid]
        nxt, hard_limit, window = segment_window(full_segs, full_index)
        # 窗口 = 本段格子 + 可借的「段间静音」，最多借 BORROW_SILENCE 秒。
        #
        # 旧口径 `hard_limit = nxt - 0.04` 把窗口钉死在「下一句开口之前」，导致
        # 中文段间静音完全浪费：中文每段有 ≈1.12s 固定开销（起音/收尾/块间静音，
        # 见 validate_segments.py 的实测模型），撞上原片 1s 级的紧凑短句就必须硬提速。
        # 实测 7 部片子：旧口径让 20 段被强制提速（最狠 atempo 1.57x，"摩乔回来啦！"
        # 6 个字被压进 1.24s），而全片实际还有 41-175s 余量没被利用。
        # 现在允许向后借用与下一段之间的静音（落轨侧顺序铺开、自动顺延下一句，
        # 不会真实重叠），超窗段从 7 降到 1，严重提速段清零。
        # 上限 0.2s 是实测最优值（扫描 0~无限：0.2s 起收益饱和），既救回短句，
        # 又不会让语音过度侵入下一句的节奏。
        raw = f"{tts_dir}/seg_{sid:04d}.wav"
        # 按段选说话人参考：spk 0=旁白(主参考)；其余用 spk_N.wav，缺文件回退主参考
        spk = s.get("spk", 0)
        seg_ref = ref
        if spk:
            cand = os.path.join(tts_dir, f"spk_{spk}.wav")
            if os.path.exists(cand):
                seg_ref = cand
        payload = cache_payload(s, nxt, hard_limit, window, seg_ref, args,
                                gen_kwargs, emo_vector, model_identity)
        base_path, manifest_path = cache_paths(tts_dir, sid)
        fresh = not cache_is_valid(base_path, manifest_path, payload)

        if fresh:
            os.makedirs(os.path.dirname(base_path), exist_ok=True)
            initial = base_path + f".initial.{os.getpid()}.wav"
            ts = time.time()
            tts_infer_chunked(tts, text, initial, seg_ref, args, emo_vector,
                                gen_kwargs)
            synth_s += time.time() - ts
            publish_cache(initial, base_path, manifest_path, payload)
            os.remove(initial)
        materialize_base(base_path, raw)
        trim_silence(raw)
        dur = audio_dur(raw)
        raw_synth_dur = dur   # 裸合成时长（trim 后, 压缩/atempo 前）—— 校准回喂用这个
        budget = pause_budget(text)
        n_gap = pause_gaps(raw)
        retakes = 0
        if fresh and args.pause_retake:
            # 双闸门重采（生成是采样的，重采能换出更好的）:
            #   ① 超窗 -> 少变速/少截断（旧逻辑）
            #   ② 句中长停顿数 > 标点数+1 -> 克隆把参考里的停顿学走了（issue #337）
            # 选片标准: 总代价（停顿超标惩罚 + 超窗惩罚）更小者胜。
            def cost(d, g):
                return (max(0, g - budget) * 2.0) / max(d, 0.1) \
                    + max(0.0, d - window) / max(window, 0.3)
            score = cost(dur, n_gap)
            while retakes < 2 and (dur > window + 0.02 or n_gap > budget):
                retakes += 1
                cand_clean = base_path + f".retake.{os.getpid()}.wav"
                cand = f"{tts_dir}/tmp_take_{sid}.wav"
                ts = time.time()
                tts_infer_chunked(tts, text, cand_clean, seg_ref, args, emo_vector,
                                    gen_kwargs)
                synth_s += time.time() - ts
                atomic_copy(cand_clean, cand)
                trim_silence(cand)
                cand_dur = audio_dur(cand)
                cand_ngap = pause_gaps(cand)
                cand_score = cost(cand_dur, cand_ngap)
                if cand_score < score - 1e-9:
                    publish_cache(cand_clean, base_path, manifest_path, payload)
                    materialize_base(base_path, raw)
                    trim_silence(raw)
                    dur, n_gap, score = cand_dur, cand_ngap, cand_score
                if os.path.exists(cand):
                    os.remove(cand)
                if os.path.exists(cand_clean):
                    os.remove(cand_clean)
                if dur <= window and n_gap <= budget:
                    break
            if retakes:
                print(f"    重采{retakes}次 -> {dur:.2f}s/窗{window:.2f}s 停顿{n_gap}/{budget}",
                      flush=True)
                retaken += 1
        if n_gap > budget:
            pause_bad.append(sid)
        # 停顿压缩：把段内 >0.36s 的静音收到 0.26s（重采是概率性的，这步是确定性兜底）
        # 句读切块段豁免：块间停顿是显式设计的句读节奏（sentence-pause-ms），
        # 压缩会把它打回 0.26s，正好毁掉这次修的目标
        if args.compress_pauses and not text_has_sentence_split(text):
            n_cut = compress_pauses(raw)
            if n_cut:
                dur = audio_dur(raw)
                n_gap = pause_gaps(raw)
                compressed += n_cut
                print(f"    压缩{n_cut}处长停顿 -> {dur:.2f}s 余{n_gap}/{budget}", flush=True)
        atempo = 1.0
        if dur > window + 0.02:
            atempo = min(1.25, dur / max(window - 0.02, 0.3))
            tmp = f"{tts_dir}/tmp_atempo_{sid}.wav"
            atempo_file(raw, tmp, atempo)
            dur = audio_dur(tmp)
            os.replace(tmp, raw)
        overflow = max(0.0, start + dur - hard_limit)
        done = f"{int((k+1)/len(segs)*100)}%"
        print(f"[{done}] seg{sid:3d} {dur:.1f}s/{window:.1f}s"
              + (f" 提速x{atempo:.2f}" if atempo > 1.001 else ""), flush=True)
        plan.append({"id": sid, "start": start, "end": end,
                     "window": round(window, 3), "rate": "clone",
                     "atempo": round(atempo, 4), "dur": round(dur, 3),
                     "overflow": round(overflow, 3),
                     "hard_limit": round(hard_limit, 3), "text": text,
                     # 段级 provenance（2026-09-18 P0）: 该 wav 真实合成参数指纹,
                     # 与 cache manifest 一致; relayout-only 原样保留
                     "synth_fingerprint": param_fingerprint(payload)})
        # 估时器自校准（借鉴 KrillinAI EMA）: 实测时长回喂全局系数,
        # precheck 的模拟随生产越来越准。直接进程内调用（旧版每段 spawn 一个
        # Python 进程只为改一个 JSON，128 段 = 128 次进程启动）。
        # 回喂失败不致命（系数文件只影响预检精度），但要出声，不许静默吞掉。
        if fresh and dur > 0.2:
            try:
                import tts_calibration
                tts_calibration.update(count_han(text), count_punct(text),
                                       [raw_synth_dur])
            except Exception as e:
                print(f"WARN: 校准回喂失败（不影响本次合成）: {e}", flush=True)

    metadata = {"voice": "IndexTTS-2.5-clone", "ref": ref,
                "emo_vector": emo_vector, "emo_alpha": args.emo_alpha}
    result = finalize_run(data, plan, tts_dir, args, metadata, partial=bool(args.only))
    if result is None:
        print(f"试听合成完成: {len(plan)} 段；正式 plan.json 与 dub_track.wav 未改动", flush=True)
        return

    truncated, truncated_ids = result
    total = time.time() - t0
    # 段级 provenance 一致性校验（2026-09-18 P0）: plan 顶层参数只描述"本次调用",
    # 每段真实参数看 synth_fingerprint；混合属合法但必须显式可见
    provenance = provenance_summary(plan)
    fps = {p["synth_fingerprint"] for p in plan if p.get("synth_fingerprint")}
    if len(fps) > 1:
        print(f"⚠️  段级合成参数不一致: {len(fps)} 种指纹（多次补跑/参数变更混用）。"
              f"各段真实参数见 plan 的 synth_fingerprint 与 cache manifest", flush=True)
    # 生产报告（借鉴 KrillinAI Report）: 质量水位落盘, 发布前扫一眼即知
    report = {
        "video_id": data.get("video_id"),
        "segments": len(plan),
        "emo_vector": emo_vector, "emo_alpha": args.emo_alpha,
        "ref": ref,
        "ref_sha256": file_sha256(ref),
        "model": model_identity,
        "speed_factor": args.duration_factor,
        "fill_window": args.fill_window,
        "atempo_segments": sum(1 for p in plan if p["atempo"] > 1.001),
        "max_atempo": max((p["atempo"] for p in plan), default=1.0),
        "retaken": retaken,
        "compressed_pauses": compressed,
        "pause_bad_ids": pause_bad,
        "truncated": truncated,
        "truncated_ids": truncated_ids,
        "synth_seconds": round(synth_s),
        "total_seconds": round(total),
        "synth_fingerprints": provenance["synth_fingerprints"],
        "params_uniform": provenance["params_uniform"],
    }
    with open(os.path.join(tts_dir, "report.json"), "w") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    print(f"合成 {len(plan)} 段 | 提速段: {sum(1 for p in plan if p['atempo']>1.001)} "
          f"| 截断段: {truncated} {truncated_ids if truncated else ''} "
          f"| 重采段: {retaken} | 压缩停顿: {compressed} 处 "
          f"| 填窗: {'on' if args.fill_window>0 else 'off'} | 语速因子: {args.duration_factor} "
          f"| 停顿超标残留: {len(pause_bad)} {pause_bad if pause_bad else ''} "
          f"| TTS纯耗时 {synth_s:.0f}s | 总耗时 {total:.0f}s")
    # 失败哲学（抄 VideoLingo）: 截断说明译文塞不下，坏产物不出门
    if truncated > 0:
        # 分流诊断（2026-09-17 教训: 笼统说"缩短译文"会误导排查方向——
        # 真正原因可能是落轨顺延把片尾段推出视频末尾，与译文长短无关）
        # 判据: 最末段也在截断列表里（顺延越界必然炸到 plan 末段）且开着填窗
        if args.fill_window > 0 and truncated_ids \
                and max(truncated_ids) == plan[-1]["id"]:
            last = plan[-1]
            print(f"FAIL: 截断集中在片尾（最后段 place {last.get('place_start', last['start']):.1f}s "
                  f"+ 顺延 > 视频末尾 {full_segs[-1]['end']:.1f}s）—— 这是落轨顺延越界，不是译文问题！", flush=True)
            print("修复: 加 --fill-window 0 重跑（绝对落轨）；已有 seg wav 不会重合成", flush=True)
        else:
            print(f"FAIL: {truncated} 段被硬截断 {truncated_ids}，缩短这些段译文后重跑 --fit 或删除对应 seg wav 重跑", flush=True)
        sys.exit(2)


if __name__ == "__main__":
    main()
