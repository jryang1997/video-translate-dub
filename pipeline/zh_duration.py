#!/usr/bin/env python3
"""中文朗读估时模型 —— 全链路唯一来源（2026-09-19 审计 P0-2 收口）

历史问题：估时系数曾在 4 个文件各持一份拷贝——
  tts_dub_indextts.py（生产，回喂校准）/ precheck_tts.py（生产预检，读校准文件）/
  validate_segments.py（译前预算 + 契约校验，硬编码出厂系数、从不读校准）。
EMA 校准每部片都在改系数，硬编码方永远拿旧值——翻译账本与生产闸门分叉，
翻译按账本写合规的译文会被闸门拦下（与 2026-09-17 "预检/生产双实现漂移"同病）。
现在所有调用方从本模块 import，系数只有一处装载。

系数装载：tts_calibration.load() 读 .cache/calibration.json（EMA 自校准产物，
每次 TTS 实测回喂），无文件/损坏时回落出厂默认值。
计数口径（HAN_RE / PUNCT_RE）与 tts_calibration.update() 的回喂口径一致，
保证"估时用的特征"与"校准用的特征"不脱节——历史上 validate 的标点集比回喂多算
…—，含省略号/破折号的段会被系统性高估，已统一为回喂口径。

落轨窗口公式（窗口/借静音/落点）在 layout.py——"每句要多久"归本模块，
"每句放哪里"归 layout，两者互不依赖。

诚实边界：est_dur 是译前预算与预检模拟用的事前估计；真实时长以合成后的
plan.json 为准（校准回喂的输入正是它）。
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tts_calibration

# 出厂默认系数：7 部片子 527 段真实 IndexTTS 合成时长最小二乘拟合
# （纯字速 1/han_s ≈ 5.55 字/秒，不含段间静音；固定开销含起音/收尾/块间 200ms 静音）。
# EMA 校准会在其基础上继续修正，本模块不做二次兜底——一切以 tts_calibration.load() 为准。
DEFAULTS = tts_calibration.DEFAULTS

# 计数口径必须与 tts_calibration.update() 的回喂口径逐字符一致，否则系数会漂
HAN_RE = re.compile(r"[\u4e00-\u9fff]")
PUNCT_RE = re.compile(r"[，。！？、：；]")

# 估时/窗口 超过此倍数 = 1.25x atempo 也救不回，须缩译文（1.2×1.25≈1.5 的物理上限口径）
SPEED_CAP = 1.2
# 译前预算 = 可用窗 × 此值，剩 8% 吸收合成波动与 ASR 边界误差
BUDGET_FILL = 0.92


def load_coeffs():
    """当前校准系数 {han_s, punc_s, fixed_s, n}。每次调用读盘（文件几百字节）。"""
    return tts_calibration.load()


def count_han(text):
    return len(HAN_RE.findall(text))


def count_punct(text):
    return len(PUNCT_RE.findall(text))


def est_dur(text, coeffs=None):
    """估中文朗读秒数 = han×han_s + punct×punc_s + fixed_s；空文本返回 0。

    三元线性模型的来历（为什么不用更朴素的 汉字×0.21+标点×0.1）：
    旧公式在本项目语料上系统性低估——7 部片实测/估算比中位 1.29，85% 的段被低估，
    中位绝对误差 0.774s；被低估最狠的是短句（"小心！"旧公式估 0.73s，实测中位
    1.39s）。成因是漏掉固定开销（段首起音+句尾收尾+切块间静音）。改三元模型后
    留一法中位绝对误差 0.774s -> 0.375s（改善 52%），短句段 0.79s -> 0.23s。
    """
    c = coeffs if coeffs is not None else load_coeffs()
    h = count_han(text)
    if h == 0:
        return 0.0
    return h * c["han_s"] + count_punct(text) * c["punc_s"] + c["fixed_s"]
