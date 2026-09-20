# 视频翻译配音管线

把英文 YouTube 视频变成"中文字幕 + 中文配音"的本地管线，可反复用于同 类视频。

## 目录结构

```
视频翻译项目/
├── pipeline/              # 可复用脚本（主链路 + 共享模块）
│   ├── run_all.sh             # 预处理入口(URL形式): 解析视频ID后委托 prepare_video.sh
│   ├── prepare_video.sh       # 预处理唯一实现: 下载(分辨率保底)+双路音频+DTW转写(ASR缓存)
│   ├── produce_video.sh       # 正式生产: 首句校验->预检->克隆配音(固定情感向量)->混音->两版水印
│   ├── whisper_to_segments.py # whisper JSON -> 片段清单（过滤幻觉/合并碎片）
│   ├── validate_segments.py   # 翻译前出预算表(--budget) / 翻译后校验契约
│   ├── 翻译规范.md             # 中文翻译规则（译前必读：预算怎么用、优先级）
│   ├── build_subs.py          # 片段清单 -> zh/bilingual 的 SRT + ASS
│   │                          #   --plan: 按配音落点出 *_dub 字幕; --avoid-banners: 词卡避让
│   ├── asr_cache.py           # ASR 内容寻址缓存（音频md5+模型hash+cli hash）
│   ├── precheck_tts.py        # 合成前截断预检(秒级): 填窗越界→自动降级绝对落轨
│   ├── check_first_align.py   # 首句对齐校验(秒级): DTW首词被片头音乐带偏→拦截
│   ├── tts_calibration.py     # 估时器自校准(EMA): 每段实测回喂, precheck 读校准值
│   ├── tts_dub_indextts.py    # 配音: IndexTTS-2.5 克隆 -> dub_track.wav + plan.json(含段级指纹)
│   │                          #   --relayout-only: 不重新合成、只重排落轨（秒级）
│   ├── pick_reference.py      # 参考样本自动优选（VAD/停顿/基频打分，建系列音色库用）
│   ├── asr_audit.py           # 转写时间轴审计: spot 点切仲裁 / slice 切片重转写真值句子表
│   │                          #   （>3分钟片子必查：全片DTW超200s漂移4-7s+音效段幻觉复读）
│   ├── make_text_banners.py   # 画面词卡汉化: 差分掩膜擦英文 -> patch/text 双层横幅PNG
│   ├── render_localized_final.sh # 完全本地化一稿: 词卡横幅+母版+水印 一次合成（读 banner_filter.txt）
│   ├── mix_render.py          # 伴奏混音 + loudnorm + 渲染两版成品（水印由 produce 统一追加）
│   ├── add_watermark.sh       # 漂浮文字水印（A/B 两版都加）
│   ├── clean_run.sh           # 清理单 run 可重算产物回收磁盘（默认干跑, --apply 执行）
│   ├── run_tests.sh           # 一键回归测试（自动选 venv，改 layout/落轨后必跑）
│   ├── config.sh              # 生产参数单点: ffmpeg路径/情感向量/句读停顿/水印文案/音色库
│   ├── toolchain.py           # ffmpeg/ffprobe/whisper-cli 二进制解析唯一入口（禁裸 PATH）
│   ├── layout.py              # 落轨引擎纯函数: 窗口/顺延/落点契约（预检与生产同源）
│   ├── zh_duration.py         # 中文估时模型: 系数唯一装载(EMA校准) + est_dur
│   ├── tts_audio.py           # 音频 DSP: 时长/解码/变速/静音治理
│   ├── tts_cache.py           # TTS 段级缓存: 指纹/发布/物化
│   ├── audio_io.py            # 音频 I/O 与文件指纹原语
│   └── _legacy/               # 已退役脚本存档（见其 README, 勿在主链路引用）
├── pipeline/tests/        # 回归测试（5 套: layout/p0_fixes/tts_reliability/render_qc/subtitle）
├── models/                # whisper large-v3-turbo 模型
├── index-tts/             # IndexTTS-2.5（uv 环境 + checkpoints 权重约 8G）
├── runs/<视频ID>/          # 每个视频一个任务目录
│   ├── 01_raw/  原视频、info.json
│   ├── 02_audio/  16k(转写用) / 48k(混音用) wav
│   ├── 03_transcript/  转写 JSON / SRT / segments.en.json
│   ├── 04_translate/   segments.zh.json（大模型翻译）+ terminology.json + 字幕成品
│   ├── 05_tts/         ref_main.wav + cache/ + plan.json + report.json + dub_track.wav
│   ├── 06_mix/         demucs 人声/伴奏
│   └── 07_output/      成品：A_原声_中英字幕 / B_中文配音_中英字幕（各含 _水印 版）
└── .venv/                 # python3.12（demucs、yt-dlp、numpy、soundfile）
```

## 依赖（本机已装好）

- yt-dlp（.venv 内，带 curl-cffi 指纹模拟；YouTube 403 时靠它）
- whisper.cpp（brew：whisper-cli）+ ggml-large-v3-turbo + silero-v5.1.2 VAD
- ffmpeg-full（brew，**含 libass 烧字幕**；注意 brew 的精简版 ffmpeg 没有 ass/subtitles 滤镜，
  且 ffmpeg-full 安装会升级 x265 导致旧版 ffmpeg 崩溃——管线统一用 ffmpeg-full）
- demucs（人声/伴奏分离，MPS 加速）
- IndexTTS-2.5（`index-tts/`，B站开源音色克隆 TTS，ModelScope 下载权重，MPS 可跑；
  5 分钟视频配音约合成 16 分钟，RTF≈3.4）
- ~~edge-tts~~（备用配音线已退役，脚本存 _legacy/tts_dub.py）

## 运行流程

```bash
# ① 预处理（下载+转写）
zsh pipeline/run_all.sh "https://www.youtube.com/watch?v=XXXX"
# -> 让 ZCode 翻译 runs/<ID>/03_transcript/segments.en.json，
#    写出 runs/<ID>/04_translate/segments.zh.json（每段加 zh 字段）
# ② 翻译就绪后正式生产（字幕/配音/混音/水印一条龙）
zsh pipeline/produce_video.sh XXXX
```

**单一生产入口**：配音、混音只走 `produce_video.sh`（run_all.sh 已不含配音步骤，
两个入口曾经分叉导致部分片子没传情感向量——不要在 run_all.sh 里再加生产步骤）。
情感向量默认 `0.4,0,0,0,0,0,0.25,0.15`、强度 0.8（儿童动画配方），换题材用环境变量覆盖，
例如 TED-Ed：`EMO_VECTOR="0.1,0,0,0,0,0,0.15,0.3" EMO_ALPHA=0.6 zsh pipeline/produce_video.sh <id>`。

## 关键设计

- **翻译与节奏协同（译前预算表）**：翻译前先跑 `validate_segments.py <en.json> --budget`
  拿每段「可用窗 / 预算字数 / 可借静音」。预算由**实测校准模型**反推
  （`han_s×汉字 + punc_s×标点 + fixed_s`，出厂系数来自 7 部片 527 段真实合成回归，
  且每次生产后 EMA 自校准持续修正——账本、预检、生产闸门三处读同一份
  `.cache/calibration.json`，见 `zh_duration.py`；旧的 VideoLingo 系数 `0.21/0.1`
  系统性低估 29%，留一法误差 0.774s→0.375s，改善 52%）。
  关键认知：**短句时间几乎全是固定开销，写短没用**——窗 <1.9s 的段
  译文已是极限，不许为塞窗口删内容。完整规则见 `pipeline/翻译规范.md`。
- **对齐**：whisper DTW 词级时间戳给出句级时间窗（`-nfa` 必加，否则 DTW 被静默关闭）；
  每段 TTS 合成后**去首尾静音**再实测时长（IndexTTS 常在开头塞 0.2-0.7s 静音，
  不去会配音迟到）；新合成超窗时 best-of-2 重采选最短一条，仍超则 atempo ≤1.25 微调。
- **落轨（节奏铺开）**：每段可用时长 = 本段格子 + 可借的段间静音（`BORROW_SILENCE=0.2s`），
  再按全片余量摊开落点（`--fill-window`）。**顺延时窗口跟着挪、不截断**（截断=丢字，
  违反信息完整优先），段间靠 `--min-gap` 收口防重叠。修此逻辑前先读
  `tts_dub_indextts.py::assemble_track` 注释，并跑 `pipeline/tests/test_layout.py` 回归。
- **重排节奏不用重跑 TTS**：改完落轨参数用 `--relayout-only`（几秒完成、内存 <1GB）。
  注意：**不能用它验证落轨改动本身**——现成 seg wav 是加速过的，时长失真会让结论反掉
  （验证落轨改动要用未加速的合成产物，或直接读 layout.py 的纯函数单测）。
- **混音**：demucs 抽掉人声留伴奏；说话段伴奏压低到配音 -13dB；整轨 loudnorm -14 LUFS。
- **字幕**：中文一行 ≤16 字，超限按标点自动拆分；双语版英文按单词数比例分配。
  单条时长跟随配音实际落点（fill-window 铺开后可超 4.5s，以贴合配音为准）——
  真正的硬约束是每行字数，不是条时长。
- **音色**：默认 IndexTTS-2.5 克隆原片说话人。参考样本建议来自**系列音色库**
  （`config.sh` 的 `SERIES_REF_DIR`，默认 `assets/voice_library/`，存放
  筛选后的干净干音样本 ref_main/ref_alt + spk_1..N；
  由 `pick_reference.py` 按活动率/零停顿/基频稳定指标自动筛出）。
  情感驱动：`--emo-vector` 传 8 维向量时使用固定情感；**不传时 IndexTTS 会从参考音频
  自行推断情感**。克隆不可用时（无人声/多说话人/用户点名）回退 edge-tts
  zh-CN-XiaoxiaoNeural（温暖女声），可选 YunxiNeural（活泼男声）等。
- **许可**：IndexTTS 为 bilibili 自定协议，商用需联系 indexspeech@bilibili.com；
  纯搬运发布自担风险，GPT-SoVITS（MIT）是可替换的宽松备选。

## 已知坑

1. YouTube 下载 403 → 用 .venv 里的 yt-dlp（curl-cffi）；字幕请求易 429，没必要就别抓。
2. brew 精简版 ffmpeg 无 libass；ffmpeg-full 与旧版 ffmpeg 共存会弄坏旧版动态库。
   Python 侧经 `toolchain.py` 解析（env → ffmpeg-full → 报错，禁止裸 PATH）；
   shell 侧统一 `source pipeline/config.sh`。两个脚本曾裸调 PATH 的 ffmpeg，没炸是运气。
3. whisper 对音乐背景易幻觉（"Thanks for watching"等），whisper_to_segments.py 已做黑名单过滤。
4. **字幕字体**：libass 在 macOS 用 CoreText 选字体，ass 滤镜的 fontsdir 对它无效，
   自定义字体必须装进 ~/Library/Fonts（run_all.sh 会自动装）。装完可用
   `ffmpeg -v info` 看 `fontselect: (Smiley Sans,...) -> ~/Library/Fonts/...` 确认。
5. zsh 双引号里 `$'\n'` 不是换行而是字面量（水印分行 bug 的根源），文本换行用 `tr '|' '\n'`。

## 退出码速查（生产链路防呆）

| 脚本 | 退出码 | 含义 |
|---|---|---|
| precheck_tts.py | 0 / 1 / 2 | 通过 / 填窗必越界(绝对落轨可救→自动降级) / 临界(建议降级) |
| check_first_align.py | 0 / 3 | 正常 / 首词疑似误对齐片头音乐（人工核对段1） |
| validate_segments.py | 0 / 1 / 2 | 通过 / 破坏契约(改时间戳/段数/空段，必须修) / 仅警告可放行 |
| tts_dub_indextts.py | 2 / 5 | 有段被硬截断(坏产物不出门) / 防呆拦截(已有产物却跑全量合成) |
| qc_dub_track.py | 0 / 1 / 2 | 通过 / ASR 复核可疑(可继续) / 确定性缺陷(停止渲染) |
| asr_cache.py get | 1 | 缓存未命中（调用方须容忍） |

## 磁盘维护

runs/ 里全部产物按"可重算"分级：只有 01_raw 源视频和 05_tts 的合成结果
（seg wav + 段级缓存）是贵资产，其余随时可重算。回收空间：
`zsh pipeline/clean_run.sh <id>`（干跑预览）→ 加 `--apply` 执行；
`--deep` 连段级缓存一起删（下次 TTS 全量重合成 35-40 分钟，慎用）。
