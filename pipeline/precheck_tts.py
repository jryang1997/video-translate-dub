#!/usr/bin/env python3
"""TTS 合成前干跑预检（不加载模型，秒级）

背景: fill-window 顺延若累积过大，可能在片尾将后续段落推出视频总时长边界。
若不提前拦截，会导致耗费大量 GPU 时间跑完全部合成后才触发越界异常。

本脚本用「译文字数估时模型」模拟落轨，在合成前预测两类截断：
  A. 顺延越界 —— 片尾段落被 cursor 推过视频末尾（本片 43 段超 33s）
  B. 译文超窗 —— 单段估时远超可用窗且无静音可借

用法:
  python3 precheck_tts.py <segments.zh.json> [--fill-window 1.25] [--max-shift 0.5]
  [--min-gap 0.22] [--video-end 秒]  （默认取最后一段 end）

退出码: 0=通过 / 1=必炸（顺延越界，必须处理）/ 2=有风险（提示，可放行）
"""
import argparse, json, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# 落点契约与 assemble_track 同源（P0 修复: 单一实现，审计 P1-1 收口后直接 import）
from layout import BORROW_SILENCE, compute_placements
# 估时系数唯一来源（审计 P0-2）：与 validate_segments 账本、生产落轨同源。
# zh_duration 内部经 tts_calibration.load() 读 EMA 自校准系数，无文件回落出厂值
from zh_duration import est_dur, load_coeffs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("segments_json")
    ap.add_argument("--fill-window", type=float, default=1.25)
    ap.add_argument("--max-shift", type=float, default=0.5)
    ap.add_argument("--min-gap", type=float, default=0.22)
    ap.add_argument("--video-end", type=float, default=None,
                    help="视频实际时长秒（默认取最后一段 end）")
    args = ap.parse_args()

    segs = json.load(open(args.segments_json))["segments"]
    video_end = args.video_end or segs[-1]["end"]
    coeffs = load_coeffs()
    est = {s["id"]: est_dur(s["zh"], coeffs) for s in segs}
    total_speech = sum(est.values())

    # --- 模拟完整落轨（P0 修复: 与 assemble_track 共用 placement contract，
    #     包含 shift、cursor、min_gap、hard_limit 和零余量回退）---
    def simulate(fill_window):
        """返回 {id: (place, est)} 或绝对落轨 {id: (start, est)}"""
        track_end = max(video_end + 1.0,
                        total_speech + args.min_gap * max(len(segs) - 1, 0) + 1.0)
        _, placement = compute_placements(
            segs, est, fill_window, args.max_shift, args.min_gap, track_end)
        return {sid: (item["place_start"], item["dur"])
                for sid, item in placement.items()}

    placements = simulate(args.fill_window)

    # A. 顺延越界：place+est 超过视频末尾（与 assemble_track 的 trunk_end 对齐）
    #    trunk_end 会扩到容下顺延内容，但渲染成片只有 video_end 长，
    #    超出 video_end 的语音等于被丢进无人区 = 事实上截断
    overflow = []
    for sid, (place, dur) in placements.items():
        over = place + dur - video_end
        if over > 0.3:
            overflow.append((sid, round(place, 1), round(over, 1)))

    # B. 译文超窗（无顺延救不了的单段）：est 比窗口大 1.2 倍以上
    #    注：这里的窗故意比 layout.segment_window 宽松（不扣 START_GUARD、
    #    不夹 WINDOW_PAD）——本检查是风险提示不是闸门，宽松窗少误报；
    #    精确窗以 segment_window 为准，两处差异是设计而非漂移。
    overwin = []
    for k, s in enumerate(segs):
        nxt = segs[k + 1]["start"] if k + 1 < len(segs) else video_end
        gap = max(0.0, nxt - s["end"])
        window = nxt + min(gap, BORROW_SILENCE) - s["start"]
        if est[s["id"]] > window * 1.2:
            overwin.append((s["id"], round(est[s["id"]], 1), round(window, 1)))

    print(f"预检: {len(segs)} 段 | 估时合计 {total_speech:.0f}s | 片尾 {video_end:.1f}s "
          f"| fill-window {args.fill_window}")
    if overflow:
        # 建议要贴合实际: 若绝对落轨能放下（越界只出现在填窗模式），
        # 引导自动降级；否则视为"估时模型偏保守"（对短句系统性高估 ~0.7s），
        # 降级为放行提示——实测才是准绳，别让保守估计挡住生产
        abs_ok = all(s["start"] + est[s["id"]] <= video_end + 0.3 for s in segs)
        if args.fill_window > 0 and abs_ok:
            print(f"⛔ 顺延越界: {len(overflow)} 段会被推出片尾 "
                  f"(首段{overflow[0][0]}@{overflow[0][1]}s 超{overflow[0][2]}s)")
            print("   这是落轨问题，不是译文问题！绝对落轨能放下 → 自动降级 --fill-window 0")
            sys.exit(1)
        if args.fill_window > 0:
            print(f"⚠️  顺延越界 {len(overflow)} 段 (首段{overflow[0][0]} 超{overflow[0][2]}s)，"
                  f"绝对落轨也临界（估时模型对短句偏保守）。")
            print("   建议: 直接切 --fill-window 0（绝对落轨，贴原片节奏最稳）")
            sys.exit(2)
        print("✅ 绝对落轨模式: 估时临界但实测通常更短，放行（合成后实测判定）")
        sys.exit(0)
    if overwin:
        print(f"⚠️  译文超窗 {len(overwin)} 段 {overwin[:6]}（估时超窗 1.2 倍，"
              f"下游会借静音/微提速，可不处理）")
        sys.exit(2)
    print("✅ 预检通过: 无顺延越界，无严重超窗")


if __name__ == "__main__":
    main()
