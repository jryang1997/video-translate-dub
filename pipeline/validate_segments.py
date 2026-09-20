#!/usr/bin/env python3
"""翻译契约校验：segments.zh.json 必须与 segments.en.json 段一一对应

规则（VideoLingo 行数一致性 + clean_text 累加匹配思想）:
1. 段数一致、id 序列一致、时间戳与 en 一致（容 0.06s，见下方注释）
2. zh 非空、无残留英文长句、无未译标记
3. 估时预警: 用 zh_duration 实测校准模型（han_s×汉字 + punc_s×标点 + fixed_s）
   预估/窗口 > 1.2 的段列出，建议翻译缩短而非依赖提速
4. 顺序单调（时间不回跳）
5. **内容覆盖预警**：英文≥4 词且「英文词数/中文字数」≥1.8 的段
   列为"疑似漏译/压缩过度"。正常中译英比值约 0.5-1.6；一旦超过 1.8，通常存在
   大模型因追求短窗口而过度删减信息的问题。
   判据经多部视频 500+ 段真实语料回归验证，有效平衡信息完整性与窗口安全。
   （注：避免使用"句数差"判断，中文逗号密度通常高于英文断句。）

用法: validate_segments.py <en_json> <zh_json> [--window-pad 1.2]
退出码: 0=通过 1=不通过（硬错误） 2=仅有警告
"""
import argparse, json, os, re, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# 落轨窗口公式唯一来源（审计 P0-3/P1-1）：译前账本与生产落轨/预检同源
from layout import WINDOW_PAD, hard_limit, segment_window
# 估时系数唯一来源（审计 P0-2）：EMA 校准更新系数后账本自动跟进，
# 不再存在"硬编码旧系数"的分叉
from zh_duration import BUDGET_FILL, est_dur, load_coeffs

WORD_EN = re.compile(r"[a-zA-Z]")
HAN = re.compile(r"[\u4e00-\u9fff]")
RESIDUAL_EN = re.compile(r"[A-Za-z][A-Za-z ,']{11,}")
EN_WORDS = re.compile(r"[A-Za-z']+")

# 内容覆盖判据（见文件头规则 5）
COVER_MIN_WORDS = 4      # 英文短于 4 词不判（避免误伤 "Ha ha ha." 这类）
COVER_RATIO = 1.8        # 英文词数 / 中文字数 的警戒线


def coverage_ratio(en, zh):
    """返回 (去重英文词数, 中文字数)。

    先把"逐词重复"折叠掉：例如对话中连续重复的叠词（如 "Look, look, look!"）
    中文通常意译合并，去重可有效避免此类情况被误判为漏译。
    """
    words = [w.lower() for w in EN_WORDS.findall(en)]
    uniq = []
    for w in words:
        if not uniq or uniq[-1] != w:
            uniq.append(w)
    return len(uniq), len(HAN.findall(zh))


def budget_table(en, coeffs=None):
    """译前预算表：每段该写多长，用「实测校准的中文朗读模型」反推。

    这是「翻译提示词与配音节奏协同」的核心工具：把下游 TTS 的真实时长账本
    提前交给翻译，而不是让翻译凭感觉写、再被下游提速或截断。

    每段的可用时长直接取 layout.segment_window()（与生产落轨同源，
    含 BORROW_SILENCE 可借静音）：
      hard_limit = next_start + min(gap, BORROW_SILENCE) - START_GUARD
      可用窗     = min(end + WINDOW_PAD, hard_limit) - start
    预算字数取 `est_dur(字数) <= 可用窗 × BUDGET_FILL` 的最大汉字数，
    BUDGET_FILL 留 8% 余量吸收合成波动。

    注意：预算小 ≠ 必须删内容。窗太紧时先去「可借静音」列看能否向后借时间，
    最后才精简措辞，且只砍语气词不砍信息词（见 pipeline/翻译规范.md）。
    """
    if coeffs is None:
        coeffs = load_coeffs()
    rows = []
    for k, s in enumerate(en):
        nxt, hard, window = segment_window(en, k)
        # 防御下限：退化段（start≥end 或零长）不出现负窗。生产落轨侧无此下限
        # （其 atempo 计算自带 max(0.3) 护栏），账本仅用于展示与预算
        window = max(0.3, window)
        en_words = len(EN_WORDS.findall(s.get("en", "")))
        # 反推预算汉字数：window×fill = h×han_s + p×punc_s + fixed_s
        # 标点数按「约每 8 字一个断句」预估，解一次近似即可
        avail = window * BUDGET_FILL - coeffs["fixed_s"]
        if avail <= 0:
            h = 1
        else:
            p_est = max(1, int(avail / (coeffs["han_s"] * 8)))
            h = int((avail - p_est * coeffs["punc_s"]) / coeffs["han_s"])
            h = max(1, h)
        # 可借静音：本段起点就落后真实发声音多少（DTW 已收缩窗口，这里看段前空隙）
        prev_end = en[k - 1]["end"] if k > 0 else 0.0
        slack = max(0.0, s["start"] - prev_end)
        rows.append(dict(id=s["id"], start=s["start"], end=s["end"],
                         dur=round(s["end"] - s["start"], 2),
                         window=round(window, 2),
                         budget_h=h, budget_s=round(est_dur("汉" * h, coeffs), 2),
                         en_words=en_words, slack=round(slack, 2)))
    return rows


def print_budget(en, video_id=""):
    coeffs = load_coeffs()
    rows = budget_table(en, coeffs)
    print(f"# 译前时长预算表{('  ' + video_id) if video_id else ''}"
          f"  （共 {len(rows)} 段）\n")
    print("| 段 | 原片时刻 | 可用窗 | 预算字数 | 原文词数 | 段前可借静音 |")
    print("|---|---|---|---|---|---|")
    for r in rows:
        flag = " ⚠️窄" if r["window"] < 1.2 else ""
        print(f"| {r['id']} | {r['start']:.2f}-{r['end']:.2f} | "
              f"{r['window']:.2f}s{flag} | ≤{r['budget_h']}字 | {r['en_words']} | "
              f"{r['slack']:.2f}s |")
    wide = sum(1 for r in rows if r["window"] < 1.2)
    tight = [r for r in rows if r["window"] < 1.2]
    print(f"\n统计：窄窗段（<1.2s）{wide} 段；"
          f"窗口合计 {sum(r['window'] for r in rows):.0f}s；"
          f"预算合计 {sum(r['budget_s'] for r in rows):.0f}s")
    print("\n用法：翻译时**对着「预算字数」写**，可低于预算，原则上不超。")
    print("窗太窄写不下时，先看「段前可借静音」——有静音就可以多写几个字，")
    print("系统会自动把这句话往后铺；实在写不下才精简措辞（只砍语气词）。")
    if tight:
        ids = ", ".join(str(r["id"]) for r in tight[:40])
        print(f"\n⚠️ 窄窗段（译文务必短，且优先用短词）共 {len(tight)} 段：{ids}")
        print("  这些段原片说话很紧凑，中文稍长就会触发下游提速。"
              "宁可换更短的说法（如「奶奶练过！」而非「奶奶以前肯定练过这个！」）。")
    # 极端窄窗：连最小中文短句都放不下，只能靠下游借静音/微提速，翻译不必硬挤
    tiny_floor = coeffs["fixed_s"] + 3 * coeffs["han_s"] + 0.2
    tiny = [r for r in rows if r["window"] < tiny_floor]
    if tiny:
        print(f"\n🔴 极窄段（窗 <1.9s，物理上放不下任何完整中文句）共 {len(tiny)} 段："
              f"{', '.join(str(r['id']) for r in tiny[:40])}")
        print("  这些段**不要为了塞窗口而删信息**，按最自然的口语写短句即可；")
        print("  下游会自动提速（≤1.25x）或向后借静音，硬挤反而让听感发赶。")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("en_json")
    ap.add_argument("zh_json", nargs="?",
                    help="翻译后的 segments.zh.json；用 --budget 时可省略")
    ap.add_argument("--window-pad", type=float, default=WINDOW_PAD,
                    help="窗口外扩秒数（与 layout.segment_window 的 WINDOW_PAD 一致）")
    ap.add_argument("--budget", action="store_true",
                    help="译前模式：只读 en_json，输出每段中文时长预算表（翻译前用）")
    args = ap.parse_args()

    if args.budget:
        en_only = json.load(open(args.en_json))
        print_budget(en_only["segments"], en_only.get("video_id", ""))
        sys.exit(0)

    if not args.zh_json:
        ap.error("需要 zh_json；只做译前预算请加 --budget")

    en = json.load(open(args.en_json))["segments"]
    zh = json.load(open(args.zh_json))["segments"]
    errs, warns = [], []
    coeffs = load_coeffs()

    # 1) 段数与 id
    if len(en) != len(zh):
        errs.append(f"段数不一致: en={len(en)} zh={len(zh)}")
    en_ids = [s["id"] for s in en]
    zh_ids = [s["id"] for s in zh]
    if en_ids != zh_ids:
        miss = sorted(set(en_ids) - set(zh_ids))[:10]
        extra = sorted(set(zh_ids) - set(en_ids))[:10]
        errs.append(f"id 序列不一致: zh缺 {miss}… zh多 {extra}…")

    en_map = {s["id"]: s for s in en}
    fast, unsorted_prev_end, tight_ids = [], None, []
    for s in zh:
        e = en_map.get(s["id"])
        if e:
            # 时间戳比对：两侧都归一到 0.01s 再比，容忍 0.06s。
            # 背景：segments.zh.json 历史上被写成两位小数（另一条工具链的产物），
            # 与 en 的三位小数最多差 0.05s；再叠加 round() 的银行家舍入
            # （5.795 -> 5.79 而 5.80 -> 5.8），旧阈值 0.02 会让 105/128 段整片误报。
            # 0.06s 只放过"两位小数round"，真实改动（重排/挪时间）远超此值。
            ds = abs(e["start"] - s["start"])
            de = abs(e["end"] - s["end"])
            if ds > 0.06 or de > 0.06:
                errs.append(f"段{s['id']} 时间戳被改动: ({s['start']},{s['end']}) != en ({e['start']},{e['end']})")
        zt = (s.get("zh") or "").strip()
        if not zt:
            errs.append(f"段{s['id']} zh 为空")
            continue
        if RESIDUAL_EN.search(zt) and not HAN.search(zt):
            warns.append(f"段{s['id']} 疑似未翻译: {zt[:30]!r}")
        # 内容覆盖：英文长、中文异常短 => 整句内容被删（不只是"压缩"）
        # 注：zh 段可能自带宽 en 字段而 en_json 里没有同 id 的段（历史产物），
        # 所以英文优先取 en_json 的，取不到退回 zh 段自带的 en，再取不到按空串。
        en_text = (e or s).get("en", "") or ""
        wen, han = coverage_ratio(en_text, zt)
        if wen >= COVER_MIN_WORDS and han and wen / han >= COVER_RATIO:
            warns.append(f"段{s['id']} 疑似漏译/压缩过度: 英文{wen}词(去重后) 中文{han}字"
                         f"（比值{wen/han:.2f}≥{COVER_RATIO}）EN={e.get('en','')[:50]!r} "
                         f"ZH={zt[:24]!r} — 请补全内容，别靠删句塞进时间窗")
        # 时间回跳
        if unsorted_prev_end is not None and s["start"] < unsorted_prev_end - 0.05:
            warns.append(f"段{s['id']} start={s['start']} 早于前段结束 {unsorted_prev_end:.2f}")
        unsorted_prev_end = s["end"]
        # 估时 vs 窗口（hard_limit 与生产落轨同源，layout.hard_limit 唯一实现；
        # 本段按 id 找下一段而非按位置——历史产物 id 可能不连续，语义保持原样）
        nxt = next((x["start"] for x in zh if x["id"] == s["id"] + 1), None)
        if nxt is not None:
            hard = hard_limit(s["end"], nxt)
        else:
            hard = s["end"] + 5
        window = min(s["end"] + args.window_pad, hard) - s["start"]
        est = est_dur(zt, coeffs)
        if est / 1.2 > window:
            # 分级：区分「内容写太长，可精简」与「窗太窄，译文已是极限」
            # 后者中文只有 2-3 个字（如"小心！""倒车喽！"），再精简就丢信息了，
            # 喊"缩短译文"是误导——这类段的超窗是中文 TTS 固定开销（≈1.12s）
            # 撞上原片紧凑短句的物理结果，只能靠下游借静音/微提速吸收。
            physical = window < coeffs["fixed_s"] + 3 * coeffs["han_s"] + 0.2
            if physical:
                tight_ids.append(s["id"])
            else:
                warns.append(f"段{s['id']} 估时{est:.1f}s/窗{window:.1f}s "
                             f"超1.2倍变速上限，建议缩短译文: {zt[:24]!r}")
        elif est > window:
            fast.append(s["id"])

    if fast:
        print(f"需提速段（est>窗，1.2x内可救）: {len(fast)} 段 {fast[:12]}")
    for w in warns:
        print("WARN:", w)
    if tight_ids:
        print(f"物理极限段（窗<1.9s，译文已是极限字数，别为塞窗删内容）: "
              f"{len(tight_ids)} 段 {tight_ids[:14]}")
        print("  这些段下游会借静音或微提速（≤1.25x）吸收，属设计内行为；"
              "只在不删信息的前提下换更短的同义说法。")
    for e in errs:
        print("ERROR:", e)
    if errs:
        sys.exit(1)
    if warns:
        sys.exit(2)
    print("翻译契约校验通过 ✓")
    sys.exit(0)


if __name__ == "__main__":
    main()
