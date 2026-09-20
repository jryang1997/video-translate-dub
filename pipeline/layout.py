#!/usr/bin/env python3
"""落轨引擎 —— 窗口、顺延、落点契约的唯一实现（2026-09-19 审计 P1-1/P0-3 收口）

三个纯函数，零第三方依赖（stdlib 即可 import），生产落轨（tts_dub_indextts 的
assemble_track）与合成前预检（precheck_tts 的干跑模拟）共用同一份：

  hard_limit(seg_end, next_start)        段硬边界：下一句开口 - 保底 + 可借静音
  segment_window(full_segs, index)       某段落轨窗口 (next_start, hard_limit, window)
  fill_window_shifts(wins, speech, ...)  填窗顺延量分配（唯一权威实现）
  compute_placements(...)                完整落点契约（shifts + cursor + min_gap +
                                         hard_limit + 零余量回退，预检/落轨逐段同源）

窗口常量在此定义（改任何一项全链路生效）；估时系数在 zh_duration.py，
两者互不依赖——"每句要多久"属于翻译账本，"每句放哪里"属于落轨契约。
"""

# 每段最多向后借用的「与下一段之间的静音」秒数。治短句被迫硬提速：
# 旧口径把窗口钉死在"下一句开口之前"，中文每段 ≈1.12s 固定开销撞上原片紧凑短句
# 就必须硬提速（实测 7 部片 20 段被迫提速、最高 1.57x，而全片还有 41-175s 余量没用）。
# 0.2 为实测最优（扫描 0~∞ 收益在 0.2s 饱和）；落轨侧顺序铺开、自动顺延下一句。
BORROW_SILENCE = 0.2
# 落点比下一段开口提前的保底秒数（硬边界的 0.04s 缓冲）
START_GUARD = 0.04
# 窗口 = min(本段 end + WINDOW_PAD, hard_limit)：中文比原话短时允许的自然延长
WINDOW_PAD = 1.2


def hard_limit(seg_end, next_start):
    """本段的硬边界（下一句开口 - 保底缓冲 + 可借静音）的唯一实现。"""
    gap = max(0.0, next_start - seg_end)
    return next_start + min(gap, BORROW_SILENCE) - START_GUARD


def segment_window(full_segs, index):
    """第 index 段的落轨窗口 (next_start, hard_limit, window)，按列表位置取下一段。"""
    seg = full_segs[index]
    nxt = full_segs[index + 1]["start"] if index + 1 < len(full_segs) else seg["end"] + 5
    hard = hard_limit(seg["end"], nxt)
    window = min(seg["end"] + WINDOW_PAD, hard) - seg["start"]
    return nxt, hard, window


def fill_window_shifts(wins, speech, fill_window, max_shift, min_gap, n_seg):
    """填窗顺延量的唯一权威实现（P0 修复: precheck 与 assemble_track 共用此函数）。

    与 assemble_track 的落点规则一致：第 k 段位移 = 前面所有段 headroom 之和
    （先使用后累加），保证预检模拟与正式落轨逐段完全同源。
    wins: 每段窗口秒数; speech: 语音总秒数。
    返回 (k_use, shifts) —— shifts[k] = 第 k 段的顺延秒数。
    """
    slack = sum(wins) - speech - min_gap * n_seg
    if slack <= 1.0:
        return 0.0, [0.0] * n_seg
    headroom = [w * (fill_window - 1.0) for w in wins]
    total_head = sum(headroom) or 1.0
    k_use = min(max_shift, slack / total_head)
    acc, shifts = 0.0, []
    for h in headroom:
        shifts.append(acc)
        acc += h * k_use
    return k_use, shifts


def compute_placements(full_segs, durations, fill_window, max_shift, min_gap,
                       track_end):
    """Compute the complete placement contract used by precheck and assembly.

    ``fill_window_shifts`` only computes the initial window shifts. The actual
    placement also has to apply the cursor, minimum gap, hard limit, and the
    zero-slack fallback in the same order. Keeping that second half here avoids
    a precheck that agrees on shifts but disagrees on real segment positions.

    ``durations`` is keyed by segment id and may contain only a subset of
    ``full_segs`` for partial reruns. The returned map is keyed by segment id.
    """
    active = [s for s in full_segs if s["id"] in durations]
    if not active:
        return 0.0, {}

    index_by_id = {s["id"]: i for i, s in enumerate(full_segs)}
    wins = []
    next_starts = {}
    for seg in active:
        idx = index_by_id[seg["id"]]
        nxt = (full_segs[idx + 1]["start"]
               if idx < len(full_segs) - 1 else track_end)
        next_starts[seg["id"]] = nxt
        wins.append(max(0.1, nxt - seg["start"]))

    speech = sum(float(durations[s["id"]]) for s in active)
    k_use, shifts = fill_window_shifts(
        wins, speech, fill_window, max_shift, min_gap, len(active))
    # fill-window 开启时始终使用 cursor 顺延；没有余量只意味着 shift=0，
    # 不能退回绝对落轨，否则长段会与下一段重叠。
    use_fill = fill_window > 0
    use_shift = use_fill and k_use > 0.0
    base_starts = {
        seg["id"]: (min(track_end - 1.0,
                        seg["start"] + shifts[i]) if use_shift else seg["start"])
        for i, seg in enumerate(active)
    }

    placements = {}
    cursor = 0.0
    for seg in active:
        sid = seg["id"]
        dur = float(durations[sid])
        nxt = next_starts[sid]
        base_start = base_starts[sid]
        if use_fill:
            start_pl = max(0.0, max(cursor, base_start))
            seg_capacity = (nxt + min(max(0.0, nxt - seg["end"]), BORROW_SILENCE)
                            - base_start)
            hard_limit_v = min(track_end - 0.02,
                               max(start_pl + dur + 0.02,
                                   start_pl + seg_capacity))
        else:
            start_pl = float(seg["start"])
            hard_limit_v = max(seg["start"] + dur + 0.02,
                               min(nxt - START_GUARD, track_end - 0.02))

        placements[sid] = {
            "place_start": start_pl,
            "base_start": base_start,
            "hard_limit": hard_limit_v,
            "dur": dur,
            "shift": shifts[len(placements)] if use_shift else 0.0,
            "overflow": max(0.0, start_pl + dur - hard_limit_v),
        }
        cursor = start_pl + dur + min_gap

    return k_use, placements
