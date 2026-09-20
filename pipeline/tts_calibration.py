#!/usr/bin/env python3
"""估时器自校准（借鉴 KrillinAI StatisticalEstimator.Calibrate 的 EMA 思想）

维护全局系数文件 .cache/calibration.json：
  han_s / punc_s / fixed_s —— 中文朗读时长模型三系数
每次 TTS 实测一段（汉字数/标点数/实测秒），用 EMA 修正系数：
  新系数 = 旧 × 0.7 + 本段反推 × 0.3，并把反推值钳位在合理区间防单段噪声。

防噪声措施:
  - fixed_s 只用「正观测」（实测-字数部分>0）更新, 负值丢弃
  - 反推值钳位 [0.5×, 1.5×] 当前系数（与 Krillin 钳位思想一致）
  - han_s 只在字数 ≥6 时更新（短句几乎全是固定开销, 动字速必失真）

用法:
  python3 tts_calibration.py load                     # 打印当前系数 JSON
  python3 tts_calibration.py update <han> <punc> <dur_s> [<dur_s> ...]
      # dur_s 可传多次=同一文本多条实测 take, 全部纳入
"""
import json, os, sys

CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".cache")
CAL_PATH = os.path.join(CACHE_DIR, "calibration.json")

DEFAULTS = {"han_s": 0.1801, "punc_s": 0.0537, "fixed_s": 1.1234, "n": 0}
EMA_NEW = 0.3
CLAMP_LO, CLAMP_HI = 0.5, 1.5


def load():
    if os.path.exists(CAL_PATH):
        try:
            d = json.load(open(CAL_PATH))
            return {**DEFAULTS, **d}
        except (OSError, ValueError):
            pass
    return dict(DEFAULTS)


def save(cal):
    os.makedirs(CACHE_DIR, exist_ok=True)
    tmp = CAL_PATH + f".tmp.{os.getpid()}"
    json.dump(cal, open(tmp, "w"), indent=1)
    os.replace(tmp, CAL_PATH)


def update(han, punc, durs):
    cal = load()
    est = cal["han_s"] * han + cal["punc_s"] * punc + cal["fixed_s"]
    # 每条 take 反推: 实测 - 字数部分 = fixed 部分
    fixed_obs = [d - (cal["han_s"] * han + cal["punc_s"] * punc) for d in durs]
    fixed_obs = [f for f in fixed_obs if f > 0]
    if fixed_obs:
        obs = sum(fixed_obs) / len(fixed_obs)
        new = cal["fixed_s"] * (1 - EMA_NEW) + obs * EMA_NEW
        cal["fixed_s"] = round(max(cal["fixed_s"] * CLAMP_LO,
                                   min(cal["fixed_s"] * CLAMP_HI, new)), 4)
    # han/punc 系数只在字数足够时更新（短句 3 字以下全是开销, 不动字速）
    if han >= 6 and est > 0:
        ratio = sum(durs) / len(durs) / est
        ratio = max(CLAMP_LO, min(CLAMP_HI, ratio))
        cal["han_s"] = round(cal["han_s"] * (1 - EMA_NEW) + cal["han_s"] * ratio * EMA_NEW, 4)
    cal["n"] = cal.get("n", 0) + 1
    save(cal)
    return cal


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "load":
        print(json.dumps(load(), indent=1))
    elif cmd == "update" and len(sys.argv) >= 5:
        han, punc = int(sys.argv[2]), int(sys.argv[3])
        durs = [float(x) for x in sys.argv[4:]]
        print(json.dumps(update(han, punc, durs), indent=1))
    else:
        sys.exit(__doc__)
