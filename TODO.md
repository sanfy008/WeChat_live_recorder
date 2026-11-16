# 待办事项

## ✅ 已完成

### 1. 130 分钟回放全量转录（2026-11-16 12:54 完成）
- 音频：`output/recordings/20261116_111300_回放130min.wav`（66.5 分钟 @ 2倍速 = 约 133 分钟内容，365MB）
- 转写：1561 句，用时 9.1 分钟（RTF≈0.14，Gemini hydrogen）
- 纠错：修正 195 处（DeepSeek，22 分钟）
- 要点：已生成结构化整理（话题/学校评价/建议/存疑点）
- 产物：`output/transcripts/20261116_122111_*.{md,json,jsonl}`
- 关键结论：**2 倍速播放不影响转写质量**，无需速度矫正

### 2. 清理老虚拟环境 `.venv`（已回收约 1.3G）
- 原 1.6G，元凶是 FunASR 实验残留（torch 552M / transformers 123M 等）
- 主代码完全不用这些包（grep 验证）
- 现只保留 `.venv-trim`（316M），84 个测试全通过
- 启动脚本已优先指向 `.venv-trim`

### 3. 代码推到 GitHub
- 仓库：https://github.com/sanfy008/WeChat_live_recorder
- 43 个文件，6225 行
- 已排除：output/、.venv、tools/wxcd（第三方运行时）、测试音频

### 4. 110 分钟回放转写（2026-11-16 14:54 完成）
- 来源：微信视频号《广州小升初（新6专场）》，小林升学
- 视频 419MB / 94 分钟（工具下载，后手动抽音频）
- 转写：**1956 句**，用时 17 分钟（Gemini hydrogen，RTF≈0.14）
- 产物：`output/transcripts/20261116_143744_20261116_xiaolin_xsc_6.{md,json}`
- 质量：W3K / 30精装 / MK / 情书 / 中大附 / 西雅 等黑话全对

### 5. 视频号媒体解密打通（2026-11-16 15:06 完成）★
- **以前一直失败的原因找到了：媒体文件是加密的**
- 响应头 `X-encflag: 1` / `X-enclen: 131072`，前 128KB 是密文
- MP4 的 ftyp/moov 正好在密文区内，所以 ffmpeg 报 `moov atom not found`
- 新增 `wxlive/sphdecrypt.py`（ISAAC64 密钥流 XOR）+ `wxlive/fetch.py`
- 新增 CLI：`python -m wxlive fetch <关键词> --no-video [--transcribe]`
- 实测：94 分钟视频 **53 秒**拉完（9.9 MB/s），产出 180MB 16k WAV
- 测试 103 → **119 全通过**
- 文档：`docs/视频号解密拉流.md`
- 提交：cc464a0

## 📋 待办

### 1. 多清晰度选择（可选）
- `content.list` 只给一条 url，实测都是 xWT111
- 详情接口有 `detail.variants[]`，可列全部清晰度选最高
- 目前够用，未实现

### 2. 地址过期自动重取（可选）
- 流地址带 `sign` + `svrnonce`，有时效
- 过期后需重新在微信里打开视频让工具刷新
- 可在 fetch 失败时提示用户，暂未自动处理

### 3. 超长/分段直播验证
- 目前只验证了单文件 MP4 回放
- 未验证超长或多段场景（工具可能有 `content_asset` 分片逻辑）

### 2. 词表持续维护
- 每场直播后把读不准的地方补进 `lexicon/xsc.json`
- 跑两三场后准确率会明显稳定
