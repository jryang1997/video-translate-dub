#!/usr/bin/env python3
"""segments.zh.json -> zh.srt / bilingual.srt / zh.ass / bilingual.ass

- 中文一行 <=16 字，超限按标点切分，时长按字数比例分配（条时长跟随配音落点，
#   可超 4.5s 以贴合配音）
- 双语版英文按比例跟随切分（单词边界对齐）
- --plan: 用 TTS plan 的实际落点（place_start+dur）生成 *_dub 字幕，不覆盖原声字幕
- --avoid-banners: 卡片避让——读 <run>/06_mix/banner_filter.txt，与词卡窗口有区间
  重叠的 cue 抬高 MarginV（zh_dub_localized.ass 抬到 270、bilingual_dub.ass 抬到 330）。
"""
import argparse, json, os, re, shutil

# 单条字幕的硬约束是每行字数；条时长跟随配音实际落点（fill-window 铺开后
# 可超 4.5s，以贴合配音为准）——旧代码里的 MAX_DUR/MIN_DUR 从未被执行，已删（审计 P2-4）
MAX_CHARS = 16
SPLIT_RE = re.compile(r"(?<=[，。！？；…—])")

# 字幕显示规范（广电/Netflix 中文规范）：中文不用标点，用空格分隔语句；
# 英文去句号逗号冒号，保留问号感叹号
ZH_PUNCT_RE = re.compile(r"[，。！？；：、…—―～·“”‘’《》「」『』（）【】]")
EN_PUNCT_RE = re.compile(r"[,.;:]")


def clean_sub_zh(t):
    return re.sub(r"\s+", " ", ZH_PUNCT_RE.sub(" ", t)).strip()


def clean_sub_en(t):
    return re.sub(r"\s+", " ", EN_PUNCT_RE.sub(" ", t)).strip()


def split_text(text, max_chars=MAX_CHARS):
    """按标点把长句切成 <=max_chars 的小块"""
    if len(text) <= max_chars:
        return [text]
    parts = [p for p in SPLIT_RE.split(text) if p]
    # 合并过短的块
    merged = []
    for p in parts:
        if merged and len(merged[-1]) + len(p) <= max_chars:
            merged[-1] += p
        elif merged and len(p) < 4 and len(merged[-1]) + len(p) <= max_chars + 3:
            merged[-1] += p
        else:
            merged.append(p)
    # 仍然超长：硬切
    out = []
    for p in merged:
        while len(p) > max_chars + 2:
            cut = max_chars
            for m in re.finditer(r"[，。！？；…—]", p):
                if max_chars * 0.6 < m.start() <= max_chars + 1:
                    cut = m.start() + 1
                    break
            out.append(p[:cut]); p = p[cut:]
        if p:
            out.append(p)
    return out or [text]


def split_english(en, n_parts, zh_lens=None):
    """英文按单词数（参考中文各块字数比例）切成 n_parts，保证单词边界完整"""
    words = en.split()
    if n_parts <= 1 or not words:
        return [en] + [""] * (n_parts - 1)
    weights = list(zh_lens) if zh_lens and sum(zh_lens) > 0 else [1] * n_parts
    total_w = sum(weights)
    total_words = len(words)
    parts, wi = [], 0
    used = 0
    for i in range(n_parts):
        goal = used + int(round(total_words * weights[i] / total_w)) \
            if i < n_parts - 1 else total_words
        goal = min(max(goal, used + 1 if used < total_words else used), total_words)
        parts.append(" ".join(words[wi:goal]))
        used = goal
        wi = goal
    return parts


def fmt_srt(t):
    h = int(t // 3600); m = int(t % 3600 // 60); s = int(t % 60)
    ms = int(round((t - int(t)) * 1000))
    if ms == 1000: s += 1; ms = 0
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def fmt_ass(t):
    h = int(t // 3600); m = int(t % 3600 // 60); s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def apply_plan_timing(segments, plan):
    """Return copied segments timed to the final dubbed placement."""
    by_id = {p["id"]: p for p in plan}
    missing = [s["id"] for s in segments if s["id"] not in by_id]
    if missing:
        raise ValueError(f"配音 plan 缺少段: {missing[:12]}")
    out = []
    for seg in segments:
        p = by_id[seg["id"]]
        start = float(p.get("place_start", p["start"]))
        dur = float(p["dur"])
        if dur <= 0:
            raise ValueError(f"配音 plan 段{seg['id']} dur 必须为正数")
        item = dict(seg)
        item["start"], item["end"] = start, start + dur
        out.append(item)
    return out


def build_cues(segments):
    cues, cid = [], 1
    for seg in segments:
        start, end = seg["start"], seg["end"]
        zh_parts = split_text(seg["zh"])
        en_parts = split_english(seg.get("en", ""), len(zh_parts),
                                 [len(p) for p in zh_parts])
        n = len(zh_parts)
        total = sum(len(p) for p in zh_parts) or n
        # 时长按字数比例分配；所有子 cue 必须留在父段时间窗内。
        t = start
        used_chars = 0
        for i, zp in enumerate(zh_parts):
            used_chars += len(zp)
            ce = end if i == n - 1 else start + (end - start) * used_chars / total
            ce = min(end, max(t, ce))
            cues.append({"id": cid, "start": t, "end": ce,
                         "zh": clean_sub_zh(zp.strip()), "en": clean_sub_en(en_parts[i].strip())})
            t = ce; cid += 1
    # 相邻去重叠
    for a, b in zip(cues, cues[1:]):
        if b["start"] < a["end"]:
            a["end"] = max(a["start"], b["start"])
    return cues


ASS_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: ZH,{font},62,&H00FFFFFF,&H00FFFFFF,&H00101010,&H7F000000,0,0,0,0,100,100,0.5,0,1,3.2,1.2,2,90,90,{mv},1
Style: EN,{font},36,&H00E8E8E8,&H00E8E8E8,&H00101010,&H7F000000,0,0,0,0,100,100,0,0,1,2.2,1,2,90,90,{mv2},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def output_paths(outdir, suffix=""):
    return {
        "zh": os.path.join(outdir, f"zh{suffix}.srt"),
        "bilingual": os.path.join(outdir, f"bilingual{suffix}.srt"),
        "zh_ass": os.path.join(outdir, f"zh{suffix}.ass"),
        "bilingual_ass": os.path.join(outdir, f"bilingual{suffix}.ass"),
        "cues": os.path.join(outdir, f"cues{suffix}.json"),
    }


def write_outputs(cues, outdir, font="Smiley Sans", suffix=""):
    paths = output_paths(outdir, suffix)
    # SRT
    with open(paths["zh"], "w") as f:
        for c in cues:
            f.write(f"{c['id']}\n{fmt_srt(c['start'])} --> {fmt_srt(c['end'])}\n{c['zh']}\n\n")
    with open(paths["bilingual"], "w") as f:
        for c in cues:
            en = f"\n{c['en']}" if c["en"] else ""
            f.write(f"{c['id']}\n{fmt_srt(c['start'])} --> {fmt_srt(c['end'])}\n{c['zh']}{en}\n\n")
    # ASS（双语版 EN 用小号灰字第二行，中英文同字体不同字号）
    for key, bilingual in (("zh_ass", False), ("bilingual_ass", True)):
        mv = 150 if bilingual else 66   # 双语留更多下方空间
        with open(paths[key], "w") as f:
            f.write(ASS_HEADER.format(font=font, mv=mv, mv2=mv - 78))
            for c in cues:
                if bilingual:
                    text = c["zh"] + (f"\\N{{\\fs36\\bord2.2\\c&HE8E8E8&}}{c['en']}" if c["en"] else "")
                else:
                    text = c["zh"]
                f.write(f"Dialogue: 0,{fmt_ass(c['start'])},{fmt_ass(c['end'])},ZH,,0,0,0,,{text}\n")
    with open(paths["cues"], "w") as f:
        json.dump(cues, f, ensure_ascii=False, indent=1)
    return paths


def load_banner_spans(run_dir):
    """读词卡过滤表的"中文词可见"窗口。

    格式每行: tag: <tag> patch <a> <b> text <c> <d> —— 避让按 text 窗口；
    兼容旧格式（overlay enable='between(t,a,b)' 行）。
    """
    filt = os.path.join(run_dir, "06_mix", "banner_filter.txt")
    spans = []
    if os.path.exists(filt):
        with open(filt) as f:
            filter_text = f.read()
        spans = [(float(a), float(b)) for a, b in
                 re.findall(r"text\s+([0-9.]+)\s+([0-9.]+)", filter_text)]
        if not spans:
            spans = [(float(m.group(1)), float(m.group(2))) for m in
                     re.finditer(r"enable='between\(t,([0-9.]+),([0-9.]+)\)'", filter_text)]
    return spans


def raise_margin(ass_path, margin, spans):
    """与词卡窗口**有区间重叠**（不是只看起点）的 Dialogue 行抬高 MarginV。"""
    if not spans:
        return 0
    n = 0

    def t2s(ts):
        h, m, s = ts.split(":")
        return int(h) * 3600 + int(m) * 60 + float(s)

    with open(ass_path) as f:
        lines = f.read().split("\n")
    for i, line in enumerate(lines):
        if not line.startswith("Dialogue:"):
            continue
        parts = line.split(",", 9)
        try:
            t0, t1 = t2s(parts[1]), t2s(parts[2])
        except Exception:
            continue
        if any(t0 < b and t1 > a for a, b in spans):
            parts[7] = str(margin)
            lines[i] = ",".join(parts)
            n += 1
    with open(ass_path, "w") as f:
        f.write("\n".join(lines))
    return n


def avoid_banners(paths, run_dir):
    """卡片避让（词卡汉化双层横幅的上层避让 pass）。

    本地化版：zh_dub.ass 复制为 zh_dub_localized.ass 后在其上避让（zh_dub.ass 保持纯净）；
    bilingual_dub.ass 是 mix_render 烧字幕的输入，直接在其上避让。
    """
    spans = load_banner_spans(run_dir)
    localized = paths["zh_ass"].replace("_dub.ass", "_dub_localized.ass")
    shutil.copyfile(paths["zh_ass"], localized)
    n_zh = raise_margin(localized, 270, spans)
    n_bi = raise_margin(paths["bilingual_ass"], 330, spans)
    print(f"本地化字幕（卡片避让 {len(spans)} 时段：中文 {n_zh} 行/双语 {n_bi} 行）: {localized}")
    return localized


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("segments_json")
    ap.add_argument("outdir")
    ap.add_argument("--font", default="Smiley Sans")
    ap.add_argument("--plan", default=None,
                    help="使用完成的 TTS plan 生成 *_dub 字幕，不覆盖原声字幕")
    ap.add_argument("--avoid-banners", action="store_true",
                    help="卡片避让：读 <run>/06_mix/banner_filter.txt 抬高重叠 cue 的 MarginV"
                         "（需 --plan；run 目录取 outdir 的上级）")
    args = ap.parse_args()
    data = json.load(open(args.segments_json))
    segments = data["segments"]
    suffix = ""
    if args.plan:
        plan = json.load(open(args.plan))["plan"]
        segments = apply_plan_timing(segments, plan)
        suffix = "_dub"
    cues = build_cues(segments)
    paths = write_outputs(cues, args.outdir, args.font, suffix)
    if args.avoid_banners:
        if suffix != "_dub":
            ap.error("--avoid-banners 需要配合 --plan（避让的是配音字幕）")
        run_dir = os.path.dirname(os.path.abspath(args.outdir).rstrip("/"))
        localized = avoid_banners(paths, run_dir)
        paths = {**paths, "localized_ass": localized}
    long_cues = [c for c in cues if len(c["zh"]) > MAX_CHARS + 2]
    print(f"字幕条数: {len(cues)} (来自 {len(data['segments'])} 段), 超长条: {len(long_cues)}")
    print("输出:", " ".join(paths.values()))


if __name__ == "__main__":
    main()
