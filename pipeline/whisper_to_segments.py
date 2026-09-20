#!/usr/bin/env python3
"""whisper.cpp 全量 JSON（--dtw 词级时间戳版）-> segments.en.json

输入: whisper-cli --dtw large.v3.turbo -ojf 输出的 JSON（纯 DTW，无 VAD）
输出: {"video_id":..., "segments":[{"id","start","end","en"}]}

DTW 特性:
- token 有独立词级时间戳（首 token 时间 ≈ 真实发声起点）
- 无 no_speech_prob/avg_logprob 字段，置信度过滤失效，仅黑名单生效
- 段更碎（按句切），且无 VAD 挡幻觉，须保留黑名单 + 短段清洗

清洗规则（抄 ZastTranslate 8 步稳定化 + VideoLingo 合并思想）:
1. 黑名单过滤幻觉段
2. 空段/纯标点段删除，窗口归还前一条
3. 间隙 <0.35s 且合并不超限的碎片合并成句（上限: 9s / 130 字符）
4. 孤儿保护: 合并产生的 <0.4s / <3词 尾巴并回前段
5. normalize_timecodes: 严格 end[i] <= start[i+1] - 0.04s，最短 0.4s
6. 段窗口收缩: 用 token 实词时间收缩 start/end（贴真实发声）
"""
import argparse, json, re, sys

BLACKLIST = [
    r"thanks? (for|a lot).*(watch|view)", r"subscribe", r"like and", r"bye ?bye",
    r"see you (next|in the)", r"amara\.org", r"subtitles? by", r"translated by",
    r"^\[?music\]?$", r"^\[?(applause|laughter)\]?$", r"^\W*$", r"^♪+$",
    r"^you$", r"^\.+$", r"^thank you\.?$",
]
BLACK_RE = [re.compile(p, re.I) for p in BLACKLIST]

MAX_MERGE_GAP = 0.35     # 间隙小于此值视为同句碎片
MAX_SEG_DUR = 9.0        # 单段最长秒数
MAX_SEG_CHARS = 130      # 单段最长字符
MIN_CUE_DUR = 0.4        # 最短段时长（Zast: MIN_CUE_DURATION_MS）
MIN_GAP = 0.04           # 相邻段最小间隔 40ms（Zast: normalize_timecodes）
WORD_RE = re.compile(r"[a-z0-9']+", re.I)


def bad_text(t):
    if not t.strip():
        return True
    return any(r.search(t) for r in BLACK_RE)


def segment_tokens(seg):
    """从段 tokens 提取实词的 (start,end) 列表，跳过特殊标记 [_TT_xxx]"""
    out = []
    for t in seg.get("tokens") or []:
        txt = t.get("text", "")
        if not txt or txt.startswith("[_"):
            continue
        st, et = t["offsets"]["from"] / 1000.0, t["offsets"]["to"] / 1000.0
        if et < st:  # DTW 偶发零长/倒挂 token
            et = st
        out.append((st, et, txt))
    return out


def tight_window(seg):
    """用实词 token 时间收缩段窗口，返回 (start, end, text)。
    DTW 词时间戳准，段边界以首/末实词为准（外扩 0.10/0.15s 呼吸位）。
    token 全异常时回退段级 offsets。"""
    toks = segment_tokens(seg)
    seg_st = seg["offsets"]["from"] / 1000.0
    seg_et = seg["offsets"]["to"] / 1000.0
    text = re.sub(r"\s+", " ", seg.get("text", "").strip())
    if not toks:
        return seg_st, seg_et, text
    words = [(st, et) for st, et, tx in toks if WORD_RE.search(tx)]
    if not words:
        # 只有标点 token（如 "!"），用标点时间
        words = [(st, et) for st, et, _ in toks]
    st0 = min(w[0] for w in words)
    et0 = max(w[1] for w in words)
    if et0 <= st0:  # 全零长: 回退
        return seg_st, seg_et, text
    return max(0.0, st0 - 0.10), et0 + 0.15, text


def merge_pass(cues):
    """碎片合并: 间隙小/不超限 -> 合并。返回新列表。"""
    if not cues:
        return cues
    merged = [dict(cues[0])]
    for c in cues[1:]:
        prev = merged[-1]
        gap = c["start"] - prev["end"]
        same_cue = gap < MAX_MERGE_GAP
        fits = (c["end"] - prev["start"] <= MAX_SEG_DUR
                and len(prev["en"]) + len(c["en"]) + 1 <= MAX_SEG_CHARS)
        if same_cue and fits:
            prev["en"] = (prev["en"] + " " + c["en"]).strip()
            prev["end"] = c["end"]
        else:
            merged.append(dict(c))
    return merged


def orphan_pass(cues):
    """孤儿保护: <MIN_CUE_DUR 或 <2词 的段并回时间上最近的邻居。"""
    out = []
    for c in cues:
        nw = len(WORD_RE.findall(c["en"]))
        dur = c["end"] - c["start"]
        if out and (dur < MIN_CUE_DUR or nw < 2) and nw < 6:
            prev = out[-1]
            if c["start"] - prev["end"] < 2.5 and \
               (c["end"] - prev["start"] <= MAX_SEG_DUR + 2):
                gap_txt = " " if prev["en"] and c["en"] else ""
                prev["en"] = (prev["en"] + gap_txt + c["en"]).strip()
                prev["end"] = max(prev["end"], c["end"])
                continue
        out.append(c)
    return out


def normalize_timecodes(cues):
    """Zast normalize_timecodes: end[i] <= start[i+1]-40ms 严格单调；
    不足 MIN_CUE_DUR 的段与下一条合并（下一条 start 前移），最后兜底强制。"""
    changed = True
    rounds = 0
    while changed and rounds < 300:
        changed = False
        rounds += 1
        for i in range(len(cues) - 1):
            cur, nxt = cues[i], cues[i + 1]
            if cur["end"] > nxt["start"] - MIN_GAP:
                if cur["end"] - cur["start"] >= MIN_CUE_DUR:
                    cur["end"] = max(cur["start"] + MIN_CUE_DUR, nxt["start"] - MIN_GAP)
                else:
                    nxt["start"] = max(0.0, cur["start"])
                    nxt["end"] = max(nxt["end"], cur["end"])
                    nxt["en"] = (cur["en"] + " " + nxt["en"]).strip()
                    cues.pop(i)
                    changed = True
                    break
    # 兜底: 单调 + 正时长
    for i in range(len(cues) - 1):
        if cues[i]["end"] > cues[i + 1]["start"] - MIN_GAP:
            cues[i]["end"] = cues[i + 1]["start"] - MIN_GAP
    return [c for c in cues if c["end"] - c["start"] > 0.05]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("json_in")
    ap.add_argument("out_json")
    ap.add_argument("--video-id", default="")
    args = ap.parse_args()

    data = json.load(open(args.json_in))
    segs = data.get("transcription", [])
    cues = []
    for s in segs:
        st, et, text = tight_window(s)
        if bad_text(text) or et - st <= 0.05:
            continue
        cues.append({"start": round(st, 3), "end": round(et, 3), "en": text})
    if not cues:
        print("没有可用片段", file=sys.stderr)
        sys.exit(1)

    cues = merge_pass(cues)
    cues = orphan_pass(cues)
    cues = normalize_timecodes(cues)

    out = {"video_id": args.video_id,
           "segments": [{"id": i + 1, **c} for i, c in enumerate(cues)]}
    json.dump(out, open(args.out_json, "w"), ensure_ascii=False, indent=1)
    n = len(out["segments"])
    speech = sum(c["end"] - c["start"] for c in cues)
    print(f"片段数: {n}  语音总时长: {speech:.1f}s")
    print(f"输出: {args.out_json}")


if __name__ == "__main__":
    main()
