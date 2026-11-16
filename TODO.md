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

## 📋 待办

### 1. 抓流自动化（可选）
- 目前需手动用抓包工具拿流地址
- 参见 `docs/抓流操作手册.md`（已实测记录 UA 伪装无效、必须走 JSBridge 注入）
- 若要自动化，需集成 mitmproxy + 注入脚本

### 2. 词表持续维护
- 每场直播后把读不准的地方补进 `lexicon/xsc.json`
- 跑两三场后准确率会明显稳定
