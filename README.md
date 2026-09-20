# Video Translate & Dub Pipeline

[English](README_EN.md) | [中文说明](README.md)

专为 **AI Coding Agent**（Claude Code, Codex, Cursor, Windsurf 等）与开发者打造的**全本地英文视频汉化与原声音色克隆配音管线**。

基于 whisper.cpp 词级对齐、IndexTTS-2.5 音色克隆、Demucs 人声分离与 FFmpeg 影视级渲染，100% 运行在 Mac Apple Silicon 本地，无需充值商业翻译或语音合成 API。

---

## 为什么做这个项目？

市面上的自动化视频翻译工具不少，但在实际工业化出片中，普遍存在几个让开发者头疼的硬伤：

1. **配音总是音画错位**：传统工具依赖粗粒度的 VAD 静音切分，遇到背景音乐或语速突变，配音往往提前抢跑或滞后数秒；
2. **配音机械生硬**：大部分工具采用死板的 Edge-TTS 播音腔，既丢掉了原视频主讲人的声线特质，又缺乏情绪起伏；
3. **中文译文机械超窗**：很多管线要求“中文语速压到 4.2 字/秒以内”，导致大模型为了塞进时间窗口大肆删减原文核心信息，造成短句孤立、内容漏译；
4. **画面英文词卡无法汉化**：只压字幕不改画面，视频中原本出现的英文教学卡片与中文字幕打架冲突；
5. **云端 API 账单昂贵**：5 分钟的高清视频，商业克隆配音动辄数元到数十元，批量处理成本难以承受。

本项目通过 5 项核心机制解决上述问题：

- **纯 DTW 词级时间戳锚定**：采用 `whisper.cpp --dtw` 提取首尾实词 token 真实发声起点收缩窗口，彻底杜绝背景音带偏；
- **IndexTTS-2.5 纯净干音克隆 + 8 维情感微调**：内置 VAD 纯度与零停顿打分算法，自动优选最干净的参考切片，配合 8 维情感向量控制（支持喜悦、平静、惊喜等微调），还原原片说话人音色；
- **实测回归估时模型与顺延借静音**：基于 7 部片子 527 段真实合成数据训练估时公式（`0.1801×汉字 + 0.0537×标点 + 1.1234`）。明确短句固定开销，超窗自动借用段间自然静音或微调速（≤1.25x），宁保信息完整绝不删句；
- **差分掩膜双层横幅汉化**：自动提取画面净背景帧作为硬切补丁（Patch），文字层淡入淡出，彻底解决画面英文残留与重影，同时驱动字幕智能抬高避让；
- **零 Token 消耗，全链路 Mac 本地加速**：转写、人声分离、克隆合成、视频压制全部基于 Apple Silicon MPS 硬件加速。

---

## 安装方式

本项目既是一个可直接在终端执行的 CLI 工具链，也是一个遵循通用 Agent 规范的 **Agent Skill**。

### 方式一：通过 `npx skills` 一键安装到您的 AI Agent（推荐）

如果您使用 Claude Code、Cursor 或支持 Agent Skills 的智能体，可在终端执行：

```bash
npx skills add jryang1997/video-translate-dub
```

终端将弹出交互界面，自动为您把 `video-translate-dub` 技能软链接到您的全局或项目 Agent 配置中。此后只需对 Agent 说：
> “*帮我把这个英文视频下载并汉化配音：https://www.youtube.com/watch?v=XXXX*”

Agent 将自主完成环境自检、分步执行、翻译预算校验与成品交付。

### 方式二：本地源码克隆（人类开发者模式）

```bash
git clone https://github.com/jryang1997/video-translate-dub.git
cd video-translate-dub
```

---

## 硬件要求与环境准备

### 1. 硬件基准
- **推荐机型**：Apple Silicon Mac（M1/M2/M3/M4 系列），统一内存 **≥ 32GB**；
- **并发约束**：IndexTTS-2.5 在合成时单进程独占约 25~35GB 内存。**严禁并行多进程跑合成**，必须串行处理单个视频，否则必触发系统 Swap 耗尽被杀进程；
- **耗时基线**：以 M1 Pro 为例，一段 5 分钟的视频，ASR 转写约 1.5 分钟，Demucs 人声分离约 2 分钟，IndexTTS 克隆合成约 16 分钟，总耗时约 30~35 分钟。

### 2. 系统依赖安装

```bash
# 1. 安装 ffmpeg-full（必须支持 libass 烧录字幕）
brew install ffmpeg-full whisper-cli

# 2. 准备 Python 主环境（用于下载、转写分段、人声分离、混音）
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-main.txt

# 3. 准备 IndexTTS 独立环境与模型
# 请拉取 IndexTTS-2.5 官方仓库并下载对应权重至 index-tts/ 目录
git clone https://github.com/index-tts/index-tts.git
cd index-tts
python3 -m venv .venv
source .venv/bin/activate
pip install -r ../requirements-indextts.txt
cd ..

# 4. 准备模型文件与字体
# Whisper 模型放置于 models/ggml-large-v3-turbo.bin
# 得意黑字体已内置于 fonts/SmileySans-Oblique.ttf，可复制到系统字体库：
cp fonts/*.ttf ~/Library/Fonts/
```

---

## 命令行使用指南

项目设计了清晰的阶段式解耦架构：

### 第一步：预处理（下载 + 音频分离 + DTW 词级转写）

```bash
zsh pipeline/prepare_video.sh "https://www.youtube.com/watch?v=XXXX"
```
产物将输出在 `runs/<视频ID>/`：
- `01_raw/`：原视频 MP4；
- `02_audio/`：16kHz 与 48kHz WAV；
- `03_transcript/segments.en.json`：带有精准词级发声起止的时间轴片段。

### 第二步：大模型翻译与契约校验

1. **获取时长预算表**（译前必看，了解各句字数安全阈值）：
   ```bash
   python3 pipeline/validate_segments.py runs/<视频ID>/03_transcript/segments.en.json --budget
   ```
2. **生成中文译文**：
   让您的 AI Agent（或自行调用 LLM）阅读 `segments.en.json`，在每段后追加 `zh` 译文字段，保存为 `runs/<视频ID>/04_translate/segments.zh.json`。
   > **翻译契约规则**：配音文本**必须保留完整标点符号**（逗号、句号），供 TTS 断句换气；严禁合并或拆分段落；严禁为塞窗口过度压缩内容。
3. **运行契约自检**：
   ```bash
   python3 pipeline/validate_segments.py runs/<视频ID>/03_transcript/segments.en.json runs/<视频ID>/04_translate/segments.zh.json
   ```

### 第三步：正式生产（克隆配音 + 动态落轨 + 混音压制）

```bash
zsh pipeline/produce_video.sh <视频ID>
```

脚本将全自动串联执行：
1. 首句对齐与 DTW 漂移预检；
2. Demucs 提取人声与伴奏；
3. 算法自动打分选出纯净干音切片；
4. IndexTTS-2.5 进行情感克隆配音与智能借静音顺延落轨；
5. 自动合成双语落轨字幕；
6. 压制最终成品至 `runs/<视频ID>/07_output/`：
   - `B_中文配音_中英字幕_水印.mp4`（发布版本）
   - `B_中文配音_中英字幕.mp4`（无水印母版）
   - `A_原声_中英字幕.mp4`（双语原声版）

---

## 项目架构与目录索引

```text
.
├── SKILL.md                 # Agent 专属技能定义（面向 Coding Agent）
├── pipeline/                # 核心处理工具链
│   ├── prepare_video.sh     # 预处理入口（下载 + DTW 词级转写）
│   ├── produce_video.sh     # 生产主入口（预检 -> 配音 -> 混音 -> 渲染）
│   ├── whisper_to_segments.py # ASR 结果分段与实词发声窗口收缩
│   ├── validate_segments.py # 译前预算表生成与翻译契约校验
│   ├── tts_dub_indextts.py  # IndexTTS-2.5 驱动与动态落轨引擎
│   ├── pick_reference.py    # 纯净参考干音自动打分筛选器
│   ├── layout.py            # 落轨引擎纯函数（顺延、借静音与防重叠）
│   ├── zh_duration.py       # 中文时长回归模型
│   ├── mix_render.py        # 伴奏/配音动态对齐与响度均衡渲染
│   ├── make_text_banners.py # 画面英文词卡差分擦除与双层汉化
│   ├── config.sh            # 全局默认参数单点定义
│   └── tests/               # 5 套回归测试套件（改动落轨算法必跑）
├── assets/voice_library/    # 系列角色参考音色库说明
├── fonts/                   # 开源中文字体（得意黑、站酷快乐体）
├── NOTICE                   # 依赖开源声明与致谢
└── LICENSE                  # Apache 2.0 许可证
```

---

## 灵感与致谢 (Acknowledgements)

在管线的设计与演进过程中，我们深度学习和吸收了开源社区诸多先驱项目与优秀创作者的成果，特此致以崇高的敬意：

- **[atelierAnchor / Smiley Sans (得意黑)](https://github.com/atelier-anchor/smiley-sans)**：感谢厉向晨、刘育黎及贡献者开源的极富表现力与美感的大河字体，为本项目的双语字幕与汉化横幅提供了核心视觉灵魂；
- **[bilibili / IndexTTS-2.5](https://github.com/index-tts/index-tts)**：提供了卓越的开源自回归音色克隆能力；
- **[whisper.cpp](https://github.com/ggerganov/whisper.cpp)**：提供了高性能的 C/C++ Whisper 推理与开箱即用的 DTW 词级时间戳能力；
- **[VideoLingo](https://github.com/Huanshere/VideoLingo)**：其多步反思翻译与术语一致性设计为本项目提供了极具价值的启发；
- **[KrillinAI](https://github.com/KrillinAI/KrillinAI)**：其估时自校准机制与内容寻址 ASR 缓存架构为本项目的工程稳健性提供了宝贵参考；
- **[facebookresearch / Demucs](https://github.com/facebookresearch/demucs)**：提供了影视级的高精度人声伴奏分离；
- **站酷快乐体 (ZCOOL KuaiLe)**：站酷网与刘兵克团队开源的免费商用字体。

---

## 免责声明 (Disclaimer)

1. **技术研究属性**：本项目仅作为音视频自动化处理技术的研究、学习与个人交流之用。
2. **版权自负原则**：使用者利用本项目处理任何第三方视频、音频素材时，应自行确保已获得原权利人的合法授权。严禁将本项目用于未经许可的商业侵权、恶意洗稿、违规搬运或任何侵犯他人肖像权、声音权与著作权之行为。
3. **衍生声明**：本项目使用的配音模型基于 bilibili IndexTTS-2.5。该衍生品对原模型所作的任何改动与原模型原始权利人无关，原始权利人对该衍生品不背书、不担保、不承担责任。
4. **免责条款**：因使用者违规使用或不当利用本项目技术而导致的任何法律纠纷、监管处罚或平台处置，均由使用者自行承担，本项目开发者与贡献者概不承担任何法律责任。

---

## 开源协议 (License)

本项目采用 [Apache License 2.0](LICENSE) 授权许可。
