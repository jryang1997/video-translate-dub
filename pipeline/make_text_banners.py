#!/usr/bin/env python3
"""画面英文词卡汉化：差分掩膜擦英文 -> 铺中文，产出双层 overlay 横幅。

做法（差分补丁 + 动态文字分层渲染）：
  掩膜 = |文字帧 - 干净帧| > thr，膨胀 4px 盖住抗锯齿边和阴影；
  擦字 = 掩膜区用干净帧像素替换。干净帧必须取**同一镜头内**的文字消失帧！
  双层输出：
    <tag>_patch.png  净背景补丁——overlay 硬切 enable，全程不透明（彻底盖住英文，无残影）
    <tag>_text.png   中文词——单独 fade alpha 淡入淡出（模仿原卡节奏）
用法:
  python3 pipeline/make_text_banners.py <run_dir> <cards.json> [--raw <mp4>] [--no-residue-check]
cards.json 每张卡:
  {"tag":"squares", "zh":"正方形", "during":15.2, "clean":16.44,
   "bbox":[500,830,1400,1060], "thr":26, "fill":[110,33,158],
   "outline":[255,255,255], "ow":4, "size":200, "center":[952,950]}
  during/clean = 秒；bbox=[x0,y0,x1,y1] 全帧坐标；center=中文词中心点。
  残差检查不过（mean>6 或 p99>30）多半是 clean 取到了切镜之后——换帧。
输出: <run>/04_translate/banners/{tag}_patch.png + {tag}_text.png + logs/banner_check_*.png
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageFilter
from scipy import ndimage

ROOT = Path(__file__).resolve().parent.parent
FONT = ROOT / "fonts/SmileySans-Oblique.ttf"
sys.path.insert(0, str(ROOT / "pipeline"))
from toolchain import ffmpeg


def grab_frame(raw: Path, t: float, out: Path):
    if out.exists():
        return
    subprocess.run([ffmpeg(), "-y", "-v", "error", "-ss", str(t), "-i", str(raw),
                    "-frames:v", "1", str(out)], check=True)


def load(p: Path) -> np.ndarray:
    return np.asarray(Image.open(p).convert("RGB"), dtype=np.float64)


def build_card(card: dict, frames_dir: Path, raw: Path, run: Path, check: bool) -> None:
    x0, y0, x1, y1 = card["bbox"]
    d_img = frames_dir / f"full_{card['during']}.png"
    k_img = frames_dir / f"full_{card['clean']}.png"
    grab_frame(raw, card["during"], d_img)
    grab_frame(raw, card["clean"], k_img)
    d, k = load(d_img)[y0:y1, x0:x1], load(k_img)[y0:y1, x0:x1]
    diff = np.abs(d - k).mean(axis=2)
    mask_d = ndimage.binary_dilation(diff > card["thr"], iterations=4)

    if check:
        resid = diff[~mask_d]
        mean, p99 = resid.mean(), np.percentile(resid, 99)
        print(f"[{card['tag']}] 掩膜 {mask_d.mean():.1%}  掩膜外残差 mean={mean:.2f} p99={p99:.1f}"
              + ("  ⚠️ 残差偏大：clean 帧可能取到切镜后/手入镜，换一帧" if mean > 6 or p99 > 30 else ""))
    plate = d.copy()
    plate[mask_d] = k[mask_d]
    plate_u8 = np.clip(plate, 0, 255).astype(np.uint8)

    patch = Image.new("RGBA", (1920, 1080), (0, 0, 0, 0))
    patch.paste(Image.fromarray(plate_u8), (x0, y0))

    text = Image.new("RGBA", (1920, 1080), (0, 0, 0, 0))
    font = ImageFont.truetype(str(FONT), card["size"])
    tw, th = ImageDraw.Draw(text).textbbox((0, 0), card["zh"], font=font)[2:]
    ox, oy = card["center"][0] - tw // 2, card["center"][1] - th // 2
    sh = Image.new("RGBA", (1920, 1080), (0, 0, 0, 0))
    ImageDraw.Draw(sh).text((ox + 6, oy + 8), card["zh"], font=font, fill=(0, 0, 0, 110))
    text = Image.alpha_composite(text, sh.filter(ImageFilter.GaussianBlur(3)))
    ImageDraw.Draw(text).text((ox, oy), card["zh"], font=font,
                              fill=tuple(card["fill"]) + (255,),
                              stroke_width=card["ow"], stroke_fill=tuple(card["outline"]) + (255,))

    out_dir = run / "04_translate/banners"
    out_dir.mkdir(parents=True, exist_ok=True)
    patch.save(out_dir / f"{card['tag']}_patch.png")
    text.save(out_dir / f"{card['tag']}_text.png")

    comp = Image.open(d_img).convert("RGBA")
    comp.alpha_composite(patch)
    comp.alpha_composite(text)
    chk = Image.new("RGB", (1920, 480), (30, 30, 30))
    chk.paste(Image.fromarray(plate_u8).resize((960, 240)), (0, 0))
    chk.paste(comp.convert("RGB").crop((x0, y0, x1, y1)).resize((960, 240)), (960, 0))
    chk.paste(comp.convert("RGB").resize((640, 360)), (0, 240))
    log_dir = run / "logs"
    log_dir.mkdir(exist_ok=True)
    chk.save(log_dir / f"banner_check_{card['tag']}.png")
    print(f"[{card['tag']}] -> patch/text 已输出")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir")
    ap.add_argument("cards_json")
    ap.add_argument("--raw", default=None, help="缺省取 01_raw/*.mp4")
    ap.add_argument("--no-residue-check", action="store_true")
    args = ap.parse_args()
    run = Path(args.run_dir.rstrip("/"))
    raw = Path(args.raw) if args.raw else next((run / "01_raw").glob("*.mp4"))
    frames_dir = run / "08_localize/frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    for card in json.loads(Path(args.cards_json).read_text()):
        build_card(card, frames_dir, raw, run, check=not args.no_residue_check)


if __name__ == "__main__":
    main()
