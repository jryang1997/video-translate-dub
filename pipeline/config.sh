# pipeline/config.sh — 生产参数单点定义（2026-09-19 审计阶段3收口）
# 用法: shell 脚本在 cd 到项目根后 `source pipeline/config.sh`（add_watermark.sh
#       等不切目录的脚本用 `source "$(dirname "$0")/config.sh"`）。
# 所有变量都用 ${VAR:-默认} 形式：调用方环境变量仍可临时覆盖（如换题材的情感向量）。
# Python 侧的二进制解析对应 pipeline/toolchain.py；改这里的默认值全链生效。

# ---- 二进制 ----
# 必须用 ffmpeg-full：brew 精简版 ffmpeg 缺 libass，烧字幕直接出坏片（README 实测）。
# 禁止裸 PATH 兜底——PATH 里的 /opt/homebrew/bin/ffmpeg 恰好就是那个坏的。
FFMPEG="${FFMPEG:-/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg}"
FFPROBE="${FFPROBE:-/opt/homebrew/opt/ffmpeg-full/bin/ffprobe}"
export FFMPEG FFPROBE
WHISPER_CLI="${WHISPER_CLI:-whisper-cli}"   # /opt/homebrew/bin 正常版，走 PATH 即可
export WHISPER_CLI
# Whisper 模型文件（相对项目根）+ DTW 对应前缀；换模型时两个一起改。
WHISPER_MODEL="${WHISPER_MODEL:-models/ggml-large-v3-turbo.bin}"
WHISPER_DTW="${WHISPER_DTW:-large.v3.turbo}"
# 字幕字体名（build_subs.py 默认 Smiley Sans，这里可全局覆盖）。
SUB_FONT="${SUB_FONT:-Smiley Sans}"

# ---- 配音情感（8维: 喜,怒,哀,惧,厌恶,低落,惊喜,平静）----
# 默认 = 儿童动画实测配方：偏开心+惊喜+平静，强度 0.8。
# 换题材用环境变量覆盖，如 EMO_VECTOR="0.1,0,0,0,0,0,0.15,0.3" EMO_ALPHA=0.6（科普片实测）。
EMO_VECTOR="${EMO_VECTOR:-0.4,0,0,0,0,0,0.25,0.15}"
EMO_ALPHA="${EMO_ALPHA:-0.8}"
# 句读停顿毫秒数：段内句末标点（？。！）强制切块、块间插显式静音，治"问号后连读"。0=关闭
SENTENCE_PAUSE_MS="${SENTENCE_PAUSE_MS:-400}"

# ---- 品牌与素材 ----
# 视频水印文案（| 表示换行，空值则不打水印）。可通过环境变量 WATERMARK_TEXT 动态覆盖。
WATERMARK_TEXT="${WATERMARK_TEXT:-YourChannel|SubtitleDub}"
# 系列音色库目录：存放 ref_main.wav 及各角色干音样本（已筛过停顿脏样本）。
SERIES_REF_DIR="${SERIES_REF_DIR:-assets/voice_library}"
