# Handover Book — 微信直播录音转录（wxlive）

> 交接簿：给下一个接手的 AI 或未来的自己看。
> 架构：Zone A 活性仪表板（每次 save 更新）｜Zone B 累积知识库（只增不删）｜Zone C 会话归档（冻结）

---

## Zone A — Living Dashboard（每次更新为最新状态）

### A1. 🎯 Core Intent Anchor（意图锚点）

**把电脑端微信直播的声音变成可信的文字。**

三根支柱，任何改动都不许破坏：

1. **不碰微信进程。** 走 WASAPI loopback 从声卡输出端录，不注入、不 hook、不改微信。
   微信升级不影响录制。
2. **"能跑"不等于"可信"。** 采集要能数学级验证（已知信号 → 峰值/RMS 对得上），
   ASR 产物要有第三方基准对照（whisper）才算修对。
3. **原始音频不贬值。** `<会话>.wav` 永不为省空间删除——模型会升级，音频不会。

**反面清单（明确不做）**：不做"看起来成功"的假验证；不做牺牲可复现性的优化；
不引入常驻系统级副作用（如自动改注册表）而不告知用户。

### A2. 📝 Active Handoff Status（任务接力板）

**✅ Milestones Achieved（已达成）**

- WASAPI loopback 采集链路**数学级验证通过**（0.25 振幅正弦 → 峰值精确 0.2500）
- 多引擎 ASR + 词表纠错，XSC 暗语词表 194 校名 / 123 术语 / 247 暗语 / 51 纠错
- ASR 质量治理：幻觉拦截 + 分句修复 + `ts_source` 诚信标注（密度双向收敛 ≈5.46-5.47 字/秒）
- 视频号下载：解密拉流 + GUI 列表/下载/解析 + 系统代理防断网
- GUI 子进程黑框闪烁已消除（S-20261116，一次自检子进程 6 → 0）
- 环境精简 `.venv-trim`（316MB，启动脚本优先用）
- **[S-20261117] 后处理从 615s 降到 51–80s（8–12 倍）**。真凶是串行 27 批，
  不是思考强度；并发 + 退避重试后行数恒为 1048
- **[S-20261117] 录音改 16k**（1.82MB/分钟，省 67%），消费侧软件重采样，
  合成信号验证 440Hz → 440.0Hz / RMS 0.2121
- **[S-20261117] 全流程真实录制 10 分钟跑通**：VAD 自适应、170 校名词表、
  LLM 纠错 103 行 0 失败、meta 全可追溯
- **[S-20261117] 产物与音频分目录**：WAV → `recordings/`，
  md/json/jsonl → `transcripts/`，实际运行验证正确
- **[S-20261126] 三路 Swarm 并行治理 + NAS 端重建验收通过**：
  代理上游 10808 探活与 `txTime` 剩余有效期、NAS 断点续转/原片保留/`.dockerignore`、
  统一完整性 `compute_completeness()` + `json`/`md` 原子写盘 + `asr_text` 保留 +
  `max_tokens` 逐轮放大；全量测试 **512 passed, 2 skipped**，NAS 容器重建后健康检查/宿主机 ffmpeg 编码/中文库下载/出网全绿
- **[S-20261126 午间] 思考强度实测定锚 + 产物自检落地**：
  39 轮档位实测确认保持 `low`（`max` 耗时 10x 且静默零纠错）；新增 `wxlive/artifacts.py` 与 `scripts/lexicon_retro.py`，补全 `_TC2SC` 7 个漏网繁体字并拦截幻觉残余
- **[S-20261126~1127] 加密回放直推 + 手机端联调 + 全端鉴权与摘要重试闭环（v0.4.0）**：
  PC 端补齐代理 2023 一键接管、加密回放 `decrypt_key` 自动解密推送至 NAS（`0b08ed4`、`834d5d3`）；扩充 XSC 词表并统一 `build_vocabulary(60, 40)` 注入（`adfa9b9`）；补齐 `summarize_digest` 分块退避重试、禁用 `max` 思考档位、打通 `WXLIVE_TOKEN` / `WXLIVE_ALLOWED_HOSTS` / `WXLIVE_READ_AUTH` 全端鉴权（`043429f`）；排查今早 `08:07` 串台假停播并输出手机端交接清单，随后在 NAS 重建容器并生效强随机 `WXLIVE_TOKEN` + `WXLIVE_ALLOWED_HOSTS=one.20161213.xyz` + `WXLIVE_READ_AUTH=false`，全量测试 **570 passed**
- **[S-20261128] NAS 令牌可用性修复 + 版本号治理（v0.5.0）**：把「推送 401 / 找不到令牌框」
  从「缺功能」翻案为「旧进程跑旧代码」，并顺着修掉三个隐藏缺陷 ——
  ①令牌框 `width=12` 装不下 43 位令牌且无显示开关；②地址/令牌从不落盘（每次重填）；
  ③「体检」只打免令牌的 `/api/health`，令牌错也显示"在线"（**假绿灯**）。
  体检改为 health + 空 body `POST /api/probe` 两步探针，`401`=令牌问题、`422`=通过；
  真实 NAS 三向验证（空/错/对）全部符合预期。版本号收口到 `wxlive.__version__`
  单一真相源，标题栏改为 `v0.5.0 · <源码指纹>`，指纹可在「改了代码没重启」时一眼识别旧窗口。
  全量 **581 passed**（`21858e6`、`a0a8d31`、`05127e2`）

**🚧 Work In Progress（进行中）**

- **[S-20261128] v0.5.0 已交付并可用**：NAS 令牌可见/可存、体检能鉴别令牌对错、版本号收口单一真相源 + 标题栏源码指纹。全量 **591 passed**。
- **[S-20261128] 2.6 小时真实长播全链路跑通**（`20261128_073432_00dbef`《让孩子爱上学习》）：
  录制 158.5 分 → ASR 266.4s（RTF 0.028）→ 纠错 349 句 → 摘要 4 分钟。
  `transcript_complete=True`、`tail_gap=1.5s`、`llm_status=ok`、1980 句、繁体 0、幻觉 0。
  **补齐了缺两轮的「真实 1h+ 长播」样本**，DEBT-005 转为 `Partially-closed`。
- **[S-20261128] 新增两个只读运维脚本**：`scripts/watch_nas_job.py`（盯任务到终态）、
  `scripts/audit_nas_job.py`（结构层/内容层两段验收，10 个单测）。
- **手机端优化验收完成**（King 于 08:30 告知），DEBT-009 → `User-verified`。

**🧱 Active Blockers（当前阻塞）**

- **🔴 DEBT-011（本轮新发现，高优先）**：NAS 容器镜像未重建，`meta.quality` 自检数据全缺。
  需 `docker compose up -d --build`，`restart` 无效。
- 其余待观察项见 A3：DEBT-004（`tk.tcl` 偶发）、DEBT-005（还差 1-2 场长播确认）、
  DEBT-007（字段精简）、DEBT-008（GUI `_run_live`）。
- ~~DEBT-009（手机端）~~ → 已验收，关闭。~~DEBT-010（`git push`）~~ → 已推送，关闭。

**🗂️ Latest Daily Review（最近一份日复盘）**

- `docs/daily__pipeline_reviews/2026-11-28.md`（S-20261128-0824）

**🚀 Next Prompt Recommendations（下一轮建议 Prompt）**

1. **🔴 重建 NAS 容器镜像（DEBT-011，最高优先）**
   > "在 NAS 上 `cd /vol1/1000/docker/wxlive-nas/docker && sudo docker compose up -d --build`。
   > 注意 `restart` 无效。重建后按三层验收：①宿主机 `wxlive/artifacts.py` 的 MD5 与本地一致；
   > ②`docker exec wxlive-nas python -c 'import wxlive.artifacts; print(hasattr(wxlive.artifacts,\"check_segments\"))'` 返回 True；
   > ③推一场短录（或对已有任务点『重新后处理』），确认产物 json 的 `meta` 里有 `quality` 键，
   > 且任务日志出现 `check_segments` 的 summary 行。"
2. **再收集 1-2 场真实长播（1h+）确认 DEBT-005 不是偶然**
   > "当前只有 `20261128_073432_00dbef`（2h38m）一个样本，`transcript_complete=True` / `tail_gap=1.5s`。N=1 不足以定论。下一场长播结束用 `python scripts/audit_nas_job.py --job <id>` 取数，对比 `tail_gap_seconds` 与 `rtf`，两场都正常即可关闭 DEBT-005。"
3. **字段精简（DEBT-007，已分析未实施，连续两轮遗留）**
   > "删掉 segments 里的 `speaker`（实测 0/1048 非空）和逐句 `ts_source`（提到 meta 记一次）；`raw_text` 改为非空才写；json 写出从 `indent=2` 改 `separators`。改完必须验证句子一条不少——现在有了 1980 句的真实长播产物作基准，比之前的 1048 句样本更有说服力。"
4. **`txTime` 剩余有效期 + Clash 断网橙色告警条真机实测**（连续两轮遗留）
   > "在正式目录启动桌面 GUI：①查看直播条目上的 `txTime` 剩余有效期显示；②退出 Clash 让 10808 断掉，确认顶部代理状态条变橙色告警且「一键还原」可用；③恢复 Clash 后告警解除。"

### A3. 🔍 Quality & Gap Analysis（质量缺口）

**已有的好习惯（继续保持）**

- 定量验证优先于"看起来对了"（正弦信号验证采集链路、whisper 基准验证 ASR）
- 桩函数只能验证分支，判据松紧必须用真实数据校准
- 防回归测试盯"因"不盯"果"（盯子进程数，不盯"快不快"）

**当前缺口**

- **真实长跑样本不足**。验证窗口普遍偏短（75 秒、10 分钟），
  真机连续数小时的行为只有回放场景验证过。**S-20261117 又验证一次这个缺口**：
  10 分钟录制里就遇到网关 RTF 飙到 3.8 导致收尾截断，2.4 小时场景下概率更高。
- **GUI 侧测试的环境依赖脆弱**。`tk.tcl` 偶发失败说明 Tk 测试环境不够稳。
- **`README.md` 承担了双重职责**（GitHub 说明 + 事实上的唯一文档入口），
  但缺少面向"下一个接手者"的状态快照——本 Handover Book 是补充。
- **[S-20261117 新增] 产物完整性缺自动校验。** 现在要靠人拿
  `音频时长` vs `转写末句时间` 对比才能发现截断。
  应加一条自动检查：两者差值 > 阈值就告警。
- **[S-20261128 新增] 假绿灯仍是最高频的一类错误。** 本轮又抓到一例：
  「体检」打的是免令牌的 `/api/health`，令牌错也显示"在线"，一推送就 401。
  **可复用判据：绿灯必须来自与失败同一层的检查**；
  用上一层（机器在线）的绿灯推断下一层（鉴权通过）= 假绿灯。
- **[S-20261128 新增] 前端硬编码版本号必然过期。** 标题栏 `1.0` 从首次提交
  起 12 天没动过，同期包版本已到 0.4。**判据：版本号只允许一处定义**，
  并加测试断言"标题栏版本 == 包版本"+"源码里不存在硬编码版本字面量"。

**🏗️ 技术债务台账**

- **[DEBT-001]** 系统代理还原依赖 GUI 在线 | Introduced: S-20261116-1612 | Status: `Open`
  - 📍 Location: `wxlive/systemproxy.py`
  - ⚡ Impact: Medium。GUI 没开时若抓流工具崩溃，用户不知道代理停在 2023
  - 💡 Suggested Fix: 已有 GUI 状态条兜底（本轮修好后才真正生效），暂不加常驻守护

- **[DEBT-002]** 词表靠手工维护，无自动发现新词 | Introduced: 早期 | Status: `Open`
  - 📍 Location: `lexicon/`
  - ⚡ Impact: Low。新暗语需人工补充
  - 💡 Suggested Fix: 从高频未登录词里半自动挖掘候选

- **[DEBT-003]** 句级时间戳是估算值（`ts_source="estimated"`） | Status: `Open`（有意为之）
  - 📍 Location: `wxlive/gemini_asr.py`
  - ⚡ Impact: Low。已明确标注，块级锚点可信
  - 💡 Suggested Fix: 不修。实测误差中位 19.3s/最大 59.0s，改成"精确"是伪精度

- **[DEBT-004]** pytest 全量偶发 `TclError: Can't find a usable tk.tcl` | Introduced: 未知（早于 S-20261116） | Status: `Open`
  - 📍 Location: Tk 测试环境（`tests/test_gui_fetch.py` fixture）
  - ⚡ Impact: Low（不影响产品，只影响 CI 稳定性）。约 1/3 概率 1 个用例失败
  - 💡 Suggested Fix: 单独开一轮专查。**注意：不要用 skip 掩盖**
  - 🗓️ S-20261117 复查：**仍未定位**。本轮三种结果都出现过
    （`277 passed,1 error` / `292 passed,1 error` / `293 passed`）。

- **[DEBT-005]** 收尾超时的实际触发率未知 | Introduced: S-20261117 | Status: **`Partially-closed`**（2026-11-28 首个真实长场样本通过）
  - 📍 Location: `wxlive/pipeline.py`（`drain_timeout` / `transcript_complete`）
  - ⚡ Impact: Medium。已知 10 分钟测试里会触发；真实 1–2.4 小时场景未测
  - 💡 Suggested Fix: 收集 2–3 次真实长场次的 `meta.transcript_complete`，
    若频繁为 False 则需要更激进的方案（如录制结束自动排队补转）
  - ✅ **2026-11-28 首个样本（`20261128_073432_00dbef`，2 小时 38.5 分）通过**：
    `transcript_complete=True`、`tail_gap_seconds=1.5s`、`rtf=0.028`、
    `llm_status=ok`、1980 句、无 `error`。
    → 说明 1h+ 场景下收尾**没有**出现截断，D-011 的 `drain_timeout` 改造有效。
  - ⏳ 仍需再收集 **1-2 场**长播确认不是偶然（N=1 不足以定论）。
  - ⚠️ 该场 `meta.quality` 缺失，原因是 NAS 容器未重建（见 DEBT-011），与本条无关。

- **[DEBT-006]** 看门狗只检测 stream 存活，不检测"活着但静音" | Introduced: S-20261117 | Status: `Open`（**有意不改**）
  - 📍 Location: `wxlive/audio_capture.py::_guard_loop`
  - ⚡ Impact: Medium。微信暂停/切设备时不会自动重连，只能靠静音自动停止兜底
  - 💡 Suggested Fix: **先不改**。直播有正常静默期（念评论/喝水/看弹幕），
    贸然重连会打断正常录制。现在有 `stop_reason` 可归因，
    先收集 2–3 次真实 stop_reason 再决定

- **[DEBT-007]** 字段冗余未清理 | Introduced: S-20261117 | Status: `Open`
  - 📍 Location: `wxlive/session.py::add_segments`
  - ⚡ Impact: Low。`speaker` 0/1048 非空、`ts_source` 全为常量；
    json 336KB 可降到 132KB
  - 💡 Suggested Fix: 删 `speaker`、`ts_source` 提 meta、`raw_text` 非空才写、
    json 去 `indent=2`。**改完必须验证 1048 句一条不少**

- **[DEBT-008]** `_run_live`（GUI 录制+转写）缺端到端实测 | Introduced: S-20261117 | Status: `Open`
  - 📍 Location: `wxlive/gui.py::_run_live`
  - ⚡ Impact: Medium。`audio_dir` 修复对它有效，但未经真实点击验证
  - 💡 Suggested Fix: GUI 点一次 5 分钟录制，确认产物落点与"打开逐字稿"

- **[DEBT-009]** 手机端插件硬编码微信 `8.0.62`（`versionCode=2881`）R8 混淆符号且暂缺加密回放提取 | Introduced: S-20261127-1430 | Status: `User-verified`（2026-11-28 关闭）
  - 📍 Location: 手机端 `wechat_live_nas_pusher` (`HookEntry.java`)
  - ⚡ Impact: Medium。微信大版本升级即失效，且手机端暂不能直接推送加密回放的 `decrypt_key`
  - 💡 Suggested Fix: 手机端补齐微信版本校验、特征签名动态查找及回放 `decodeKey` 提取
  - ✅ 关闭依据：King 于 2026-11-28 08:30 明确告知「手机端已经完成优化验收」。
    **证据级别：用户口头确认**，手机端为独立项目不在本仓库，本机无法复现验证。
    风险描述保留 —— 「微信大版本升级即失效」是结构性约束，不会因本次验收消失。

- **[DEBT-011]** NAS 容器镜像未重建，导致 `meta.quality` 自检数据缺失 | Introduced: S-20261128-0824 | Status: **`Open`（高优先，需下一轮首件事）**
  - 📍 Location: NAS `wxlive-nas` 容器镜像（`/vol1/1000/docker/wxlive-nas/docker/docker-compose.yml`）
  - ⚡ Impact: **High**。`nas_runner.py:1505` 的 `doc["meta"]["quality"] = q.to_meta()`
    自 `49cecb6`（2026-11-26 12:28）就在代码里，但容器未重建 → 该行从未执行。
    后果：产出物**永久缺失 `quality` 字段**（`trad_residual` / `hallucination_residual` /
    `ok`），无法跨场次比对内容质量，也无法自动发现「574 句繁体」「幻觉混入正文」这类缺陷。
  - 🔍 决定性证据（非推断）：`20261128_073432_00dbef` 的任务日志从
    「ASR 完成 1980 句，RTF=0.03」**直接跳到**「AI 要点生成完毕 → 全部完成」，
    中间没有 `q.summary()` 那行输出；且产物 json 的 `meta` 键里无 `quality`。
    同时宿主机 `artifacts.py` 存在、`nas_runner.py` 含 `check_segments` → 文件是对的，镜像旧。
  - 💡 Suggested Fix:
    ```
    cd /vol1/1000/docker/wxlive-nas/docker && sudo docker compose up -d --build
    ```
    **`docker restart` 无效** —— 容器是 `COPY` 代码进镜像的，不是挂载。
    验收三层：①宿主机文件 MD5 与本地一致 ②容器内 `import wxlive.artifacts` 成功
    ③跑一场短录，产物 `meta` 含 `quality` 键且日志出现 `summary()` 行。

- **[DEBT-010]** 公网只读免密暴露面与本地 9 个提交未推送远端 | Introduced: S-20261127-1430 | Status: `Open`
  - 📍 Location: NAS `.env` (`WXLIVE_READ_AUTH=false`) / 本地 `main` 分支
  - ⚡ Impact: Medium。公网知晓地址/端口者可免密浏览任务与下载逐字稿；本地提交尚无远端备份
  - 💡 Suggested Fix: 立即 `git push` 同步远端；若后续公网访问人数扩大，可随时切回 `WXLIVE_READ_AUTH=true` 或在反代层加独立鉴权

### A4. 📊 Skill Dashboard（效用仪表板）

| Skill 名称 | 累计触发 | 用户满意 | 用户纠正 | 效用评分 |
|---|---|---|---|---|
| king-workstyle | 多次（跨会话） | — | — | ⭐⭐⭐⭐⭐ |
| project-save | 3 | — | — | — |
| wxlive-nas-deploy-verify | 2（S-20261126 午间 / S-20261127） | — | — | ⭐⭐⭐⭐⭐ |
| wechat-live-recorder | 多次 | — | — | ⭐⭐⭐⭐⭐ |
| windows-audio-capture | 多次 | — | — | ⭐⭐⭐⭐⭐ |
| tkinter-gui-testing | 多次 | — | — | ⭐⭐⭐⭐⭐ |
| herdr | 2（S-20261117 两次派工） | — | — | ⭐⭐⭐⭐ |

> 本轮未触发的 Skill 保持空白由后续 session 补齐。满意度需用户显式反馈后才计分，不自行判定。
>
> **herdr 评四星的理由**：两次派工都一次做对（4 项改动 / 并发改造），
> 且主动报告了自己测出的数字比预期差 —— 这是诚实的。
> 但**它的端到端验证只跑 1 次，不能替代多次交叉验证**：
> S-20261117 的失败批问题就是复跑才发现的。
> → 验收惯例：**agent 的验证结果必须由我独立复跑一次才算数。**

### A5. 🧪 Experiment Log（实验日志）

| 时间 | Skill | 尝试 | 结果 | 保留? |
|------|-------|------|------|-------|
| 23:10 | — | 探针用 `sys.executable` 起子进程测控制台 | **假阴性**：`sys.executable` 是 `pythonw.exe`，GUI 子系统永不分配控制台，测出"都有/都没有" | discard |
| 23:15 | — | 探针用 `EnumWindows` 抓 `ConsoleWindowClass` 按 PID 过滤 | **假阴性**：控制台窗口归属 `conhost.exe` 不是子进程，抓到 0 个 | discard |
| 23:20 | — | 子进程 `sleep 1.2s` 后再枚举 + 改用 `GetConsoleWindow()` | **成功**：三组对照精确复现差异（python.exe/0 → False；pythonw/0 → True；pythonw/no-window → False） | keep |
| 23:28 | — | 修复候选：给 `reg query` 加 `CREATE_NO_WINDOW` 即可 | **不彻底**：止住闪烁但仍每分钟起 6 个子进程，耗时 180ms | discard |
| 23:30 | — | 修复方案：改 `winreg` 直读 + 统一 `winproc` 封装 | **采纳**：6 → 0 子进程，180ms → 0.94ms，且全部调用点收编 | keep |
| 11:20 | — | 思考强度：直接信 `reasoning_effort="medium"` 生效 | **假象**：magpie 返回 200 但档位非法，两次跑 33.7s/63.6s 不稳；`max` 直接 403 | discard |
| 11:25 | — | 思考强度：三档各跑 40 行做逐行 diff | **成功**：low 改对 2 行 > medium/high 各 1 行 → 退回 low | keep |
| 12:40 | — | 格式压缩：各格式转写一次算相似度（86.6–92.1%） | **不可用**：没测 A/A 基线，噪声 ±3.8% 淹没差异（FAIL-004） | discard |
| 13:00 | — | 格式压缩：同格式跑 6 次 + 格式 A×B 交叉 36 对 | **成功**：10 组全在 ±1pp 内 → 证明格式差异测不出来 | keep |
| 14:00 | — | 格式选型：按体积最小选 Opus-24k（3%） | **否决**：崩溃后丢约 1 秒，直播最怕丢结论那句 | discard |
| 14:05 | — | 格式选型：FLAC（无损） | **否决**：崩溃后 ffprobe duration=N/A，彻底损坏 | discard |
| 14:10 | — | 格式选型：16k WAV（21%） | **采纳**：无损 + 崩溃 30s 完整可读 + 省 67% | keep |
| 15:00 | — | 录制端直接以 16k 打开 WASAPI | **失败**：`[Errno -9997]`，只能原生率打开 | discard |
| 15:05 | — | 消费侧 `StreamResampler` 软件重采样 | **采纳**：0.050ms/块，SNR 181dB，440Hz→440.0Hz 验证通过 | keep |
| 15:40 | — | 用 PowerShell 播放器播测试音验证采集 | **假阴性**：录到自己 `[console]::beep` 的 523Hz（FAIL-005） | discard |
| 15:45 | — | 合成信号直接走代码路径 | **成功**：绕开不确定的播放端 | keep |
| 17:00 | — | 全流程实测：看到 md/jsonl 正常即认为跑通 | **错判**：音频 592s 而转写只到 404.9s，json 根本未生成 | discard |
| 17:05 | — | 补转 405s 之后 + 重建完整产物 | **成功**：证实音频完好，问题在收尾被截断 | keep |
| 11-26 12:20 | — | 39 轮档位实测 + `group/deepseek` `max` 档对比 | **证伪 `max`**：`high` 与 `low` 在 A/A 噪声内；`max` 耗时 10 倍且 2 批零纠错，全端禁用 `max` | keep |
| 11-27 11:00 | — | 检查 NAS 08:07 任务《小林升学》不自动停止原因 | **证伪“静音判停坏了”**：末尾 RMS 0.017~0.096 且实时音频是英语课《滚雪球造句法》，根因是手机端未修串台前推错了流 | keep |
| 11-27 12:55 | wxlive-nas-deploy-verify | 公网鉴权初配 `WXLIVE_READ_AUTH=true` | **违背浏览习惯**：打开网页即弹密码框，用户反馈太繁琐 | discard |
| 11-27 13:02 | wxlive-nas-deploy-verify | 调整为 `WXLIVE_READ_AUTH=false` + `WXLIVE_TOKEN` 锁写操作 + `WXLIVE_ALLOWED_HOSTS` | **采纳**：网页免密只读 200，写操作无令牌 401 / 有令牌通过 | keep |

---

## Zone B — Cumulative Knowledge Base（累积模式，只增不删）

### B1. 🧠 Decision Ledger（决策账本）

- **[DEC-001] 读系统代理改用 winreg 直读，不用 reg.exe** | Session: S-20261116-2345 | 2026-11-16
  - 🔍 Context: 每 60 秒自检起 6 个 `reg query`，在 `pythonw` 下每个都分配控制台窗口 → 屏幕每分钟闪 6 下
  - ✅ Decision: `systemproxy.read_all()` 用 `winreg.OpenKey` 同进程读三个值
  - ❌ Alternatives: ①只给 `reg query` 加 `CREATE_NO_WINDOW`（仍起 6 进程、180ms，浪费且脆弱）；②降低自检频率到 5 分钟（牺牲防断网时效）
  - 📌 Status: `Active`

- **[DEC-002] 子进程统一走 wxlive/winproc.py，不给扫描器开例外** | Session: S-20261116-2345 | 2026-11-16
  - 🔍 Context: 全仓扫描测试拦下 3 处裸 `subprocess`（macOS/Linux 分支 + ffmpeg 录流），当时那些平台不受影响
  - ✅ Decision: 全部收编进 `winproc.run/popen`，测试不留白名单
  - ❌ Alternatives: ①给扫描器加平台判断跳过非 Windows 分支（口子一开规则形同虚设，且 ffmpeg 录流是真的会闪）；②只修 Windows 相关的那处
  - 📌 Status: `Active`

- **[DEC-003] 不覆盖 README.md，状态快照另存 Handover Book** | Session: S-20261116-2345 | 2026-11-16
  - 🔍 Context: project-save skill 要求维护 `ReadMe.md`，但项目已有 `README.md`（21KB，面向 GitHub 用户）。Windows 文件名大小写不敏感，实测两者 inode 相同（`1688849861429484`）→ **是同一个文件**
  - ✅ Decision: 保留 `README.md` 原样不动；交接状态写进 `Handover Book.md` + `docs/daily__pipeline_reviews/`
  - ❌ Alternatives: ①按 skill 覆盖 `README.md`（会摧毁 GitHub 说明，不可接受）；②重命名现有 README（破坏 GitHub 仓库导航和外部链接）
  - 📌 Status: `Active`。**下次接手者不要"按 skill 补 ReadMe.md"，那会覆盖 GitHub 文档**

- **[DEC-004] 修黑框时连带修了 _ui_queue 初始化顺序 bug** | Session: S-20261116-2345 | 2026-11-16
  - 🔍 Context: 验证修复效果时发现状态条恒为「读取中…」，追下去是既有 bug（启动探测早于 `_ui_queue` 创建），导致防断网自检**从上线起就没生效过**
  - ✅ Decision: 一并修，并把 5 个状态变量提前到 `_build_fetch_tab()` 开头
  - ❌ Alternatives: 只报不改（问题会被交接遗漏，而且"防断网"是 DEC 里明确要的中心能力，不能是假的）
  - 📌 Status: `Active`

- **[DEC-005] 思考强度退回 `low`，不在 deepseek 上塞非法档位** | Session: S-20261117-1900 | 2026-11-17
  - 🔍 Context: 用户把 `reasoning_effort` 改成 `medium` 想试效果，但 magpie 调用日志显示仍走 low
  - ✅ Decision: 退回 `low`。`/v1/models` 声明 `group/deepseek` 只有 `["low","high","max"]`，
    **没有 medium**；实测传 medium 返回 200 但行为不稳（33.7s/63.6s，reasoning 2751/5559），
    传 max 直接 403。质量对比 40 行：low 改对 2 行、medium/high 各 1 行，**low 反而更准**
  - ❌ Alternatives: ①继续用 medium（行为不保证，且更慢）；②换 `group/hydrogen`
    （支持真 medium/xhigh，但 ASR 已在用它，混用会争抢）——若要更强推理应换模型而非塞非法档
  - 📌 Status: `Active`。**判断档位合法性只看 `/v1/models` 的 `reasoning_levels`，
    不要凭"传了不报错"当支持**

- **[DEC-006] 后处理慢的根因是串行批处理，用并发 + 退避重试解决** | Session: S-20261117-1900 | 2026-11-17
  - 🔍 Context: 用户问"22 分钟纠错是否必须"。时间线证明 10 分 15 秒全在后处理
    （1048 行 ÷ 40 = 27 批串行 HTTP）
  - ✅ Decision: `ThreadPoolExecutor` 并发（最终 12 路）+ `retry_delays=(1.0,4.0)` 退避重试
  - ❌ Alternatives: ①减少行数/合并批（会降低纠错精度）；②换更快模型（质量未知）；
    ③只并发不重试（实测会漏纠 4–12%）
  - 📌 Status: `Active`。实测 615s → **51–80s**，行数恒 1048
  - ⚠️ 关键教训：**并发改造会掩盖失败率问题**。原逻辑把网关瞬时抖动
    （返回 23 行/40、HTTP 502/403）当永久失败整批丢弃，
    表现是"纠错处数变少"而非报错。**改性能前必须先测失败率**

- **[DEC-007] ASR 并发从 4 提到 8（推翻代码里的旧注释）** | Session: S-20261117-1900 | 2026-11-17
  - 🔍 Context: `gemini_asr.py` 原注释写「实测 4 路最优（2.04x），**8 路反而因争抢回落到 1.59x**」
  - ✅ Decision: 复测后提到 **8 路**。真实录音 2400s / 22 块，每档跑 2 次：
    4 路 69.2s / **8 路 37.2s（1.86x）** / 12 路 37.5s（1.85x）
  - ❌ Alternatives: 保持 4 路（慢一倍）；提到 12 路（无额外收益，块数不够跑满）
  - 📌 Status: `Active`
  - ⚠️ 教训：**代码注释里的"实测数据"会过期。** 那条注释可能是旧网关状态下的结论。
    不能因为"注释说测过"就跳过复测。短音频（11 块）测不准，必须用长音频测

- **[DEC-008] 录音改 16k，采用 16k WAV 而非 Opus/FLAC** | Session: S-20261117-1900 | 2026-11-17
  - 🔍 Context: 用户问能否改 mp3/ogg 省空间。四层验证（确定性测量/噪声基线/交叉比对/工程可行性）
  - ✅ Decision: 改 **16k WAV**（省 67%）
  - ❌ Alternatives 及否决理由:
    - **Opus-24k（3% 体积）**：崩溃后丢最后约 1 秒。直播最怕丢的正是结论那一句
    - **FLAC**：**崩溃后彻底损坏**（ffprobe duration=N/A），长度写在头部未 close 就丢
    - 保持 48k：上传前一律重采样 16k，48k 从未进模型，纯冗余
  - 📌 Status: `Active`。实测 1.82MB/分钟，18MB / 9.9min
  - ⚠️ **硬约束**：WASAPI loopback **无法直接以 16k 打开**
    （实测 16000/32000/44100 全抛 `[Errno -9997]`），只能软件重采样，
    且必须放消费侧而非 PortAudio 回调

- **[DEC-009] 音频与文字产物分目录（Session.audio_dir）** | Session: S-20261117-1900 | 2026-11-17
  - 🔍 Context: 体检发现 `pipeline.py` 给 Session 传了 `RECORDINGS_DIR`，
    而 cli/gui/auto_pipeline 都传 `TRANSCRIPTS_DIR`。后果：产物混进 recordings/、
    GUI「打开逐字稿」指向不存在的文件
  - ✅ Decision: `Session` 新增 `audio_dir` 参数（默认等于 `base_dir` 保持兼容）；
    pipeline 改 `TRANSCRIPTS_DIR + audio_dir=RECORDINGS_DIR`
  - ❌ Alternatives: ①录完把 wav 移过去（多一步磁盘操作，且崩溃时两处都不完整）；
    ②全部塞 transcripts/（音频和文字混在一起，语义混乱）
  - 📌 Status: `Active`。实测产物进 transcripts/、音频进 recordings/，向后兼容

- **[DEC-010] 收尾超时不能静默，必须留下"未完成"标记** | Session: S-20261117-1900 | 2026-11-17
  - 🔍 Context: 10 分钟实测中网关 RTF 从 0.35 飙到 3.8，收尾时队列堆到 9，
    进程被外部超时 kill → 音频 592s 但转写只到 404.9s，json 完全没生成，
    **而 md/jsonl 看起来正常**
  - ✅ Decision: `drain_timeout` 600 → 可配置 900；join 超时后 warn +
    `meta.transcript_complete=False` + 结束时打印补转命令；队列 ≥5 提前 warn
  - ❌ Alternatives: ①无限等（进程永远不退出）；②直接 kill ASR 线程
    （线程无法安全终止，且会丢更多）
  - 📌 Status: `Active`
  - ⚠️ 教训：**这类问题最危险——产物看起来正常，只有对比音频时长才能发现少内容。**

- **[DEC-011] 两跳代理必须连带探测上游 10808 存活，列表行以 content_id 精确绑定** | Session: S-20261126-0030 | 2026-11-26
  - 🔍 Context: 抓流代理 2023 存活但 Clash 10808 退出时，旧诊断仍报绿；列表反查依赖“标题+作者+时间”易串台
  - ✅ Decision: `systemproxy.py` 新增 `upstream_fixed_alive()` 探测 `127.0.0.1:10808`，`gui_fetch.py` 直接输出诊断原话并用 `iid=str(content_id)` 配合 `_fetch_rows` 映射，新增 `_tx_remaining_text()` 显示 `txTime` 剩余有效期
  - 📌 Status: `Active`

- **[DEC-012] 统一完整性判据 `compute_completeness()` + 原子写盘 + 保留 `asr_text`** | Session: S-20261126-0030 | 2026-11-26
  - 🔍 Context: 旧版只按 `tail_gap < 120` 判完整，会漏掉丢块/队列丢弃/spill/占位符；`json`/`md` 直接覆盖写有中断损坏风险；词表展开覆盖了引擎原始输出 `raw_text`
  - ✅ Decision: `quality.py` 统一收口 `compute_completeness()`；`session.py` 临时文件 + `os.replace` 原子写盘并保留 `asr_text`；`postprocess.py` 按 `finish_reason=="length"` 逐轮放大 `max_tokens`（8k→16k→32k）
  - 📌 Status: `Active`

- **[DEC-013] `.dockerignore` 置于仓库根目录，NAS 原片按保留天数清理** | Session: S-20261126-0030 | 2026-11-26
  - 🔍 Context: `docker-compose.yml` 的 `build.context` 为 `..`（仓库根），BuildKit 不读 `docker/.dockerignore`；NAS 原片长期堆积需受控清理
  - ✅ Decision: `.dockerignore` 放仓库根；`retention.py` 按任务年龄/保留天数清理原片，跳过进行中任务并把源文件加入 `referenced_audio` 防孤儿扫描二次处理
  - 📌 Status: `Active`

- **[DEC-014] 全端禁用 LLM `max` 思考档位，统一收紧至 `none/low/high`** | Session: S-20261127-1430 | 2026-11-27
  - 🔍 Context: 2026-11-26 午间 39 轮实测确认 `high` 与 `low` 差异落在 A/A 噪声内（0.9933 vs 基线 0.9908），而 `max` 在 `group/deepseek` 上耗时 10 倍（493.8s）且 2 批全部失败、零纠错静默通过
  - ✅ Decision: `wxlive/config.py` 将 `VALID_REASONING_EFFORTS` 收紧为 `("none", "low", "high")`，`server.py` 设置下拉移除 `max`，`nas_runner.py` 读取旧 `settings.json` 中的 `max` 时安全降级为 `low`
  - ❌ Alternatives: 仅在文档口头提醒勿选 `max`（用户或旧配置仍会踩到 10 倍耗时零纠错陷阱）
  - 📌 Status: `Active`

- **[DEC-015] 统一 `lexicon.build_vocabulary(60, 40)` 注入并补齐分块摘要退避重试** | Session: S-20261127-1430 | 2026-11-27
  - 🔍 Context: 昨晚 2.6h 长直播 `20261126_200453_2cea98` ASR 完整但 `build_digest` 第 5 块 `read timeout` 直接落成 `partial`；同时扩充后的 `lexicon/xsc.json` 需要各端统一控制提示词词表规模
  - ✅ Decision: `postprocess.py` 新增 `_complete_multi_with_retry` 与 `DIGEST_RETRY_DELAYS = (2.0, 6.0)`；`lexicon.py` 新增 `build_vocabulary(60, 40)` 并统一接入 `auto_pipeline.py`、`auto_transcribe_after_stop.py`、`gui.py`、`nas_runner.py`
  - ❌ Alternatives: 仅增大单次 timeout（无法抵御网关偶发连接中断或 502）
  - 📌 Status: `Active`

- **[DEC-016] 公网 NAS 采用“网页免密只读 + 写操作/推送强制 Token + Host 白名单”鉴权策略** | Session: S-20261127-1430 | 2026-11-27
  - 🔍 Context: `one.20161213.xyz:16666` 暴露到公网后需防止未授权删改/发起录制，但用户明确拒绝每次打开网页还要弹窗输密码（FAIL-011）
  - ✅ Decision: `server.py` 补全 `WXLIVE_ALLOWED_HOSTS`、`WXLIVE_READ_AUTH` 与 Web UI「🔑 令牌」弹窗/`localStorage`；`fetch.py`/`gui_fetch.py` 透传 `X-WXLIVE-Token`；NAS `.env` 配置 43 位随机 `WXLIVE_TOKEN`、`WXLIVE_ALLOWED_HOSTS=one.20161213.xyz`、`WXLIVE_READ_AUTH=false`
  - ❌ Alternatives: ①强制 `WXLIVE_READ_AUTH=true`（新浏览器/清缓存打开即弹窗阻断，用户否决）；②不设 `WXLIVE_TOKEN`（公网可任意删除任务与启动录制，风险过高）
  - 📌 Status: `Active`

### B2. 🎨 Style DNA（用户偏好档案）

- `[S-20261116-2345]` **用户先提出自己的因果假设，但允许被推翻。** 本轮他说"是不是 GOI 调用了什么工具"，实际是工具在轮询自己的状态条。他接受纠正，关注点在"能不能优化"。→ 不要因为用户给了假设就顺着编解释，要独立验证。
- `[S-20261116-2345]` **对"隔段时间闪一下"这类间歇现象，用户要的是根因 + 定量前后对比**，不是"已经修好了"的一句话。
- `[S-20261116-2345]` 说"复盘总结沉淀经验教训"时，期待的是**带证据路径的结构化档案**，而不是聊天里的一段总结。
- `[S-20261117-1900]` **会为"测不出来的差异"主动踩刹车。** 格式压缩能省 97% 体积，
  但他说"验不出来先不改"——**判据立不住就不动**，这比他自己的直觉更优先。
- `[S-20261117-1900]` **要求把"验证流程设计"本身当作交付物。**
  不是说"跑个对比看看"，而是要设计出能穿透噪声的实验（四层验证法）。
- `[S-20261117-1900]` **会追问"有没有必要"而不只是"能不能"。**
  格式压缩一问里明确问了"多维度因素下你觉得有价值这么做吗"——期待的是判断，不是执行。
- `[S-20261117-1900]` **给指令极简，且信任执行。** "可以"、"去完善项目"、
  "改 16k 吧"——两三个字就放手，不逐步确认。
- `[跨会话]` 偏好 GUI 操作技术工作流；技术讨论到代码/测试/系统配置级别。
- `[跨会话]` 喜欢"双线赛马"——同一任务并行跑多个 AI 再对擂取优。
- `[跨会话]` 说"go ahead"时期待直接从评审进入实现 + 配套回归测试。
- `[S-20261127-1430]` **拒绝把安全策略做得影响日常顺手体验。** 当 `WXLIVE_READ_AUTH=true` 导致打开 NAS 网页要输密码时，明确要求调成只读免密、只锁写操作。→ 上线带“拦截日常只读浏览”的开关前，先给用户说清弹窗感知并确认默认档位。
- `[S-20261127-1430]` **跨设备联调采用“PC 端出交接清单 → 手机端整改确认 → 服务端切配置闭环”的节奏。** 不代猜对端进展，等对方确认修完再合闸开启鉴权。

### B3. ❌ Failed Attempts Archive（失败档案）

- **[FAIL-001] 探针用 `sys.executable` 起子进程，测不出控制台分配** | Session: S-20261116-2345 | 2026-11-16
  - 🔧 Approach: 父进程 `pythonw`，子进程用 `sys.executable`
  - 💥 Failure Reason: `sys.executable` 在 `pythonw` 进程里就是 `pythonw.exe`，GUI 子系统程序**永远不会被分配控制台**。测出 noflag 和 withflag 都是 `has_console=False`，得出"现象无法复现"的错误结论
  - 💡 Lesson: **验证 A 机制时，激励源/被测对象必须是能体现差异的那一类。** 这里子进程必须是 console 子系统（`python.exe`）。同 MEMORY.md 里 `Start-Process` 播 WAV 没发声那次是同一类错误——**验证链路时，两端的行为都必须确定**

- **[FAIL-002] 用 `EnumWindows` 抓 `ConsoleWindowClass` 判断有无控制台** | Session: S-20261116-2345 | 2026-11-16
  - 🔧 Approach: 子进程枚举顶层窗口，按 PID 过滤找控制台类窗口
  - 💥 Failure Reason: 控制台窗口归属 `conhost.exe` 而非子进程，按子进程 PID 过滤恒为 0
  - 💡 Lesson: 判断"进程有没有控制台"用 `GetConsoleWindow()`，不要用窗口枚举

- **[FAIL-003] 只加 `CREATE_NO_WINDOW` 的修复方案被否决** | Session: S-20261116-2345 | 2026-11-16
  - 🔧 Approach: 认为"止住闪烁就够了"，计划只给 `reg query` 加 flags
  - 💥 Failure Reason: 定量的角度看这仍不对——每分钟仍起 6 个子进程、耗 180ms 去做一件 0.94ms 能完成的事。本质是"轮询方式选错了"，不是"调用姿势不对"
  - 💡 Lesson: **修到"现象消失"不够，要修到"这件事本来就不该这么做"。** 问一句"这个操作有没有零成本的替代实现"

- **[FAIL-004] 格式压缩前后对比只跑一次，差点得出错误结论** | Session: S-20261117-1900 | 2026-11-17
  - 🔧 Approach: 各格式转写一次，与原始 48k 的文本算相似度，得到 86.6–92.1%，看着"压缩掉了点质量"
  - 💥 Failure Reason: **没有先测 A/A 基线。** 补测后同一份无损音频连跑 5–6 次，
    组内两两相似度只有 92.14%–97.65%（均值 95.42%）—— **ASR 自身随机性比任何格式差异都大**。
    原测法的 86.6–92.1% 完全落在噪声里，既证明不了"掉了"也证明不了"没掉"
  - 💡 Lesson: **测 A/B 差异前必须先测 A/A 基线。**
    和 FAIL-001/002 是同一类错误：验证一条链路前，必须先量化噪声/
    确认激励源能体现差异

- **[FAIL-005] 用 PowerShell 播放器播测试音，录到的是自己的蜂鸣声** | Session: S-20261117-1900 | 2026-11-17
  - 🔧 Approach: 用 `MediaPlayer` / `SoundPlayer` 播 440Hz WAV，同时用 `LoopbackRecorder` 采集
  - 💥 Failure Reason: 录到 RMS=0.027（测试音本身 0.212，真实直播 0.145），
    主频 523Hz 而非 440Hz —— **那是我自己先前 `[console]::beep` 的蜂鸣声**，
    测试音根本没通过 loopback 播出来。差点误判"重采样坏了"
  - 💡 Lesson: 这是**第三次**踩同类坑（前两次：`Start-Process` 播 WAV 没发声、
    探针子进程用 `pythonw`）。这台机器上 PowerShell 播放器对 WASAPI loopback 不可靠。
    **可靠做法是绕过播放，直接用合成信号走代码路径** ——
    能绕开不确定环节时，绕开比"确保它正确"更省事

- **[FAIL-006] 静态扫描 import 出现次数，误判"未使用的导入"** | Session: S-20261117-1900 | 2026-11-17
  - 🔧 Approach: 体检时用 grep 数 `timedelta` 等符号出现次数，判为未使用导入，写进体检报告
  - 💥 Failure Reason: 复查发现 `time` / `timedelta` / `np` / `sf` **四个全在用**，
    `timedelta` 用于 `_audio_duration` 的类型注解 —— 类型注解也算使用
  - 💡 Lesson: **"出现次数少" ≠ "未使用"。** 静态扫描的结论必须人工复核才能写进报告。
    已在体检报告里明确更正，没让错误待办留在项目里

- **[FAIL-007] ASR 统计口径没考虑"降级不报错"** | Session: S-20261117-1900 | 2026-11-17
  - 🔧 Approach: 依赖 `FallbackEngine` 的 `primary_ok` / `primary_fail` 判断转写健康度
  - 💥 Failure Reason: 90 秒短测时 4 个片段**全部落占位符**，
    但 stats 显示 `primary_ok=4 / primary_fail=0`。根因是 `gemini_asr` 内部
    把「整块失败」吞成占位符**返回值**（不抛异常），上层看到的是"调用成功"
  - 💡 Lesson: **凡是"降级不报错"的设计，都要单独检查统计口径是否还真实。**
    已加 `failed_chunk_count` / `has_content_loss` 字段修正

- **[FAIL-008] `.dockerignore` 放在 `docker/` 子目录下不生效** | Session: S-20261126-0030 | 2026-11-26
  - 🔧 Approach: Swarm-2 初稿将 `.dockerignore` 建在 `docker/.dockerignore`
  - 💥 Failure Reason: `docker-compose.yml` 的 `build.context` 是 `..`（仓库根目录），BuildKit 只认上下文根目录的 `.dockerignore`（或 `docker/Dockerfile.dockerignore`）
  - 💡 Lesson: **先看 `build.context` 再放 `.dockerignore`。** 已移至仓库根目录并同步至 NAS，重建时 `COPY wxlive` 实测仅 2.7s

- **[FAIL-009] 本机 `curl` 探测局域网 NAS 未加 `--noproxy '*'`** | Session: S-20261126-0030 | 2026-11-26
  - 🔧 Approach: 直接在 Windows Git Bash 下 `curl http://192.168.68.49:8910/api/health`
  - 💥 Failure Reason: 系统代理 `10808` 会接管非 `127.0.0.1` 的局域网请求
  - 💡 Lesson: **探测局域网服务必须加 `curl --noproxy '*'`（或容器内 localhost 复测）**

- **[FAIL-010] 档位对比与词表复盘脚本初版度量失真** | Session: S-20261126-1215 | 2026-11-26
  - 🔧 Approach: 统计数字保真时未剥离格式序号 `9.`/`12.`，列表正则漏掉 `1.` 编号，A/A 基线跨语段算出 `0.03`
  - 💥 Failure Reason: 度量口径本身被文本格式污染，产生误导性指标
  - 💡 Lesson: **看到反常指标先查度量脚本本身，再下产品/模型结论**

- **[FAIL-011] 公网开启鉴权时默认打开 `WXLIVE_READ_AUTH=true` 阻断网页免密浏览** | Session: S-20261127-1430 | 2026-11-27
  - 🔧 Approach: 为追求“公网全锁”默认将 `.env` 的 `WXLIVE_READ_AUTH` 设为 `true`
  - 💥 Failure Reason: 用户打开 NAS 网页直接弹密码框，反馈“不想登录网页还要输密码，太复杂了”，随即改为 `false` 重建
  - 💡 Lesson: **对改变日常浏览门槛的安全开关，上线前应确认只读是否需要鉴权，默认保持读写分级**

- **[FAIL-012] 在工作树 CWD 下以绝对路径运行正式目录的 `pytest`** | Session: S-20261127-1430 | 2026-11-27
  - 🔧 Approach: 未切换工作目录，直接在 worktree 中通过绝对路径参数对正式目录 `tests/` 运行 `pytest -q`
  - 💥 Failure Reason: Python 按 CWD 优先导入旧 worktree 里的 `wxlive` 包，收集阶段即报 `ModuleNotFoundError: No module named 'wxlive.artifacts'`
  - 💡 Lesson: **运行测试前必须显式 `cd` 到目标仓库根目录**

### B4. 🏗️ Technical Debt Register

见 Zone A3 的台账（DEBT-001 ~ DEBT-010）。此处不重复，保持单一事实来源。

---

## Zone C — Session Archive（冻结区，新快照在最上方）

---

### 📸 Session Snapshot: S-20261128-0824 | v0.5.0 | 2026-11-28 08:24

**Session Summary**: 由 King 一句「推送 NAS 有错误，需要 token，但 GUI 没有输入 token 的选项」触发的排障。
查实**不是缺功能，而是屏幕上那个 GUI 进程是 11-26 17:55 启动的，比令牌输入框的重码提交
（`043429f`，11-27 11:14）早 17 小时** —— 功能在代码里，只是从未被加载。
顺着查下去修掉三个隐藏缺陷：令牌框宽 12 字符装不下 43 位令牌且无显示开关；地址与令牌从不落盘
（每次开窗口重填）；「体检」只打免令牌的 `/api/health`，令牌错也显示"在线"（假绿灯）。
体检改为 `health` + 空 body `POST /api/probe` 两步探针。随后应 King 追问「版本号是不是一直没变」，
查实项目里并存三个互不相干的版本号（标题栏 `1.0` 从首提交起 12 天未动 / 包 `0.2.0` / RESUME `v0.4.0`），
收口到 `wxlive.__version__` 单一真相源，并给标题栏加**源码指纹**以识别「改了代码没重启」的旧窗口。
最后修掉自己引入的测试污染（启动日志写进了生产日志，一轮全量 107 条）。

**Delta from Previous Save (增量变化)**

- 📁 New files:
  - `tests/test_version.py`（版本号一致性 5 用例）
  - `docs/daily__pipeline_reviews/2026-11-28.md`（本轮日复盘）
- ✏️ Modified files:
  - `wxlive/__init__.py`：新增 `__version__ = "0.5.0"`、`VERSION_LABEL`、`build_stamp()`
  - `wxlive/config.py`：新增 `NasConfig`（`base_url` / `token`）
  - `wxlive/userconfig.py`：`SAVEABLE` 增 `nas` 段，`build_config_from_user` 合并 nas
  - `wxlive/gui.py`：删硬编码 `APP_VER = "1.0"`，标题栏改 `v0.5.0 · <指纹>`；启动写日志；加载/回填 nas
  - `wxlive/gui_fetch.py`：NAS 目标框改两行布局（令牌 width 44 + 显示开关）、落盘、体重写为两步探针、401 改可执行指引
  - `tests/test_gui_fetch.py`：+6 NAS 用例；fixture 隔离 `OUTPUT_DIR`；修 2 个旧桩签名
- 🔄 Superseded:
  - 「GUI 令牌输入框待真机实测」这条遗留项**表述有误** —— 不是"没测"，
    而是当时跑的进程里根本没有该控件。已更正。

**Decisions Made**

- **[DEC-017] 访问令牌不做"自动获取"，只做"存一次免填"**
  | Session: S-20261128-0824 | 2026-11-28
  - ❌ Problem: King 提出能否「自动获取解密 token」。核实后发现混淆了两个 token：
    `decodeKey`（加密回放的 DRM key，早已全自动）与 `X-WXLIVE-Token`（NAS 鉴权，43 位随机）。
  - ✅ Decision: `X-WXLIVE-Token` **原理上不可自动获取**（它是用户自己设在 NAS `.env` 的密钥，
    能自动拿到 = 鉴权失效）。实施的是「写入 `config/user.json`（已 gitignore）+ 启动回填」。
  - 🔒 Constraint: 令牌不得进版本库、不得回显在日志/对话中。
  - 📌 Evidence: `wxlive/config.py#NasConfig`、`wxlive/userconfig.py#SAVEABLE`、`tests/test_version.py`

- **[DEC-018] 健康检查必须能鉴别鉴权，不能只探活**
  | Session: S-20261128-0824 | 2026-11-28
  - ❌ Problem: 「体检」只打 `/api/health`（免令牌端点），令牌错也报"在线"，实测推送仍 401。
  - ✅ Decision: 两步探针 —— `health` 判在线，空 body `POST /api/probe` 判鉴权。
    `401` = 令牌问题，`422`/`2xx` = 通过。探针无副作用（不建任务、不碰磁盘）。
  - 📌 Evidence: `wxlive/gui_fetch.py#_fetch_nas_health`；真实 NAS 三向实测
    （空令牌 / 错令牌 / 正确令牌 分别 False/False/True）

- **[DEC-019] 版本号单一真相源 + 标题栏带源码指纹**
  | Session: S-20261128-0824 | 2026-11-28
  - ❌ Problem: 三个版本号并存且标题栏 12 天未变，无法回答「屏幕上是哪一版」。
  - ✅ Decision: `wxlive.__version__` 为唯一真相源；`build_stamp()` 对 `wxlive/*.py`
    的 mtime+size 取 6 位哈希，用于识别「改了代码没重启」的旧窗口。
    刻意不依赖 git（`pythonw` 下跑 git 会弹黑框）。
  - 🔒 Constraint: 新增 `tests/test_version.py` 锁死一致性，防止再次漂移。
  - 📌 Evidence: `wxlive/__init__.py`、`wxlive/gui.py#APP_STAMP`、`tests/test_version.py`

**Verification Evidence**

- 全量 `pytest -q`：**581 passed**（基线 570，+6 NAS +5 版本，另修 2 个旧桩）
- 新测试有效性反证：`git stash` 回退修复后 6 个 NAS 用例 **6/6 失败**，恢复后 6/6 通过
- 真实 NAS 三向探针：空令牌→`令牌为空`/False；错令牌→`与服务端不一致`/False；正确令牌→`令牌有效`/True
- 推送链路：带真实令牌 `POST /api/probe` 返回 **200**（修复前必然 401）
- 持久化：磁盘 `config/user.json` 的 `nas` 段写入且重开回填；`_save_settings()` 前后令牌长度不变（41→41）
- 版本：真实窗口标题 `微信直播录音转写 v0.5.0 · 72698a`；`touch` 任一 `.py` → 指纹变 `41ab05`
- 测试零污染：跑 `tests/test_gui_fetch.py` 前后生产日志行数均为 **206**
- 运行确认：GUI 进程 PID 25768 @ 08:20:10，日志 `启动 … v0.5.0 · 源码指纹 4b20f7 · PID 25768`

**后续追加（本 Session 后半段，King 要求盯直播 + 整体规划）**

- **[新增] 2.6 小时真实长播全链路验收**：`20261128_073432_00dbef`《让孩子爱上学习》，
  07:34:32 → 10:13:02（158.5 分钟 / 290.3 MB / 16k）。
  ASR 266.4s（RTF 0.028）、1980 句、纠错 349 句（17.6%，与 18% 基线吻合）、
  后处理约 4 分钟。`transcript_complete=True`、`tail_gap=1.5s`、`llm_status=ok`。
  独立内容层扫描：繁体 0、幻觉 0、正文密度 5.91 字/秒、无复读。
  → **DEBT-005 转 `Partially-closed`**（还需 1-2 场确认）。
- **[新增] 发现 DEBT-011（High）**：NAS 容器镜像未重建 → `meta.quality` 永久缺失。
  决定性证据是任务日志从「ASR 完成」直接跳到「全部完成」，中间没有 `q.summary()` 输出。
- **[新增] 两个只读运维脚本**：`scripts/watch_nas_job.py`、`scripts/audit_nas_job.py`
  （+`tests/test_audit_nas_job.py` 10 用例）。
- **手机端优化验收完成**（King 于 08:30 告知）→ DEBT-009 `User-verified`。

**Open Items Carried Forward**

- 🔴 DEBT-011 重建 NAS 容器镜像（下一轮首件事）
- 再收集 1-2 场长播确认 DEBT-005
- `txTime` 剩余有效期 + Clash 断网告警条真机实测（连续两轮遗留）
- DEBT-007 字段精简（连续两轮遗留）

---

### 📸 Session Snapshot: S-20261127-1430 | v0.4.0 | 2026-11-27 14:30

**Session Summary**: 围绕“项目演进复盘 + 手机端公网直推短板修补 + 全端鉴权闭环”展开工作。在 11-26 下午完成代理 2023 体检接管与加密回放自动解密推 NAS（`0b08ed4`、`834d5d3`）的基础上，今日整理提交 XSC 词表扩充与 `build_vocabulary(60, 40)` 统一注入（`adfa9b9`），修复长直播分块摘要超时无重试、全端禁用 `max` 思考档位并打通 `WXLIVE_TOKEN` / `WXLIVE_ALLOWED_HOSTS` / `WXLIVE_READ_AUTH` 全端鉴权闭环（`043429f`）；排查今早 `08:07` 假停播实为手机端修复前预加载串台，指导手机端完成整改后在 NAS 重建容器（`sha256:dca75fdff`）并按用户偏好调优为“网页免密只读 + 写操作 Token 保护”。

**Delta from Previous Save (增量变化)**

- 📁 New files:
  - `wxlive/artifacts.py`、`tests/test_artifacts.py`（产物繁简/幻觉自检，11-26 午间合入）
  - `scripts/bench_effort.py`、`scripts/lexicon_retro.py`
  - `docs/思考强度档位复测-2026-11-26.md`、`docs/词表定期复盘可行性-2026-11-26.md`
  - `docs/daily__pipeline_reviews/2026-11-27.md`（本轮日复盘）
- ✏️ Modified files:
  - 本地抓流/加密回放推 NAS：`wxlive/systemproxy.py`、`wxlive/fetch.py`、`wxlive/gui_fetch.py`、`wxlive/nas_runner.py`、`wxlive/stream_capture.py`
  - 词表与 ASR 注入：`lexicon/xsc.json`、`wxlive/lexicon.py`、`auto_pipeline.py`、`scripts/auto_transcribe_after_stop.py`、`wxlive/gui.py`
  - 分块重试、禁用 `max` 与全端鉴权：`wxlive/postprocess.py`、`wxlive/config.py`、`wxlive/server.py`、`docker/docker-compose.yml`、`docker/.env.example`
  - 测试与档案：`tests/test_{artifacts,gui_fetch,lexicon,live_stream_url,nas_server,postprocess_truncation,text_meta_config}.py`、`Handover Book.md`、`docs/project-state/RESUME.md`
- 🔑 Key changes:
  - 全量单元测试从 512 passed, 2 skipped 增长至 **570 passed**（正式目录 `.venv-trim` 实测 `96.92s`）
  - NAS 容器使用最新代码重建并完成内网/公网鉴权实测（`GET /` & `GET /api/jobs` 无令牌 `200`；`POST /api/probe` 无令牌 `401`、带有效 `X-WXLIVE-Token` 达 `422`）

**Decisions Made This Session**: DEC-014, DEC-015, DEC-016

**Failed Attempts This Session**: FAIL-010, FAIL-011, FAIL-012

**Cognitive State at Close（会话关闭时的思维状态）**

- PC 端与 NAS 端代码及 `.env` 鉴权配置已全部落盘并验证；手机端 `wechat_live_nas_pusher` 已由用户确认按交接清单修改，但尚未在开启 `WXLIVE_TOKEN` 后的真实直播中做端到端滑切推送实测。
- 本地 `main` 分支已有 9 个提交领先 `origin/main`（含本次存档待提交），尚未 `git push`。

**Context Window Highlights**

- 用户对“打开 NAS 网页还要输密码”明确表示太复杂，要求关闭 `WXLIVE_READ_AUTH`，保留写操作 `WXLIVE_TOKEN`；这一权衡已在 `DEC-016` 与 `RESUME.md` 固定，避免后续会话误翻回。
- 对 `08:07` 任务持续录制 3 小时的排查证明了音频抽样转写（RMS + 实际内容识别）比猜开关状态更可靠。

**Session Reflect Report**

- **R1 — 成功模式**:
  - **真音频抽样定位**：直接拉取现网进行中任务的末尾/分段音频跑 RMS 与本地转写，一举排除“静音判停失效”的假象，定位出“手机端串台导致标题与流错配”的真根因。
  - **`king-workstyle` 凭据保护 + 可逆部署**：NAS `.env` 先备份再改，43 位强随机 Token 全程不进对话或日志，从 `WXLIVE_READ_AUTH=true` 秒级回切 `false` 并双端验证。
- **R2 — 边界案例**:
  - **`WXLIVE_READ_AUTH` 默认值冲突**：从纯公网安全角度默认开了 `true`，但与用户“网页免密看列表、只锁写入/推送”的直觉冲突（FAIL-011）。已沉淀到风格档案和 `DEC-016`。
  - **跨工作树跑测试的 CWD 陷阱**：worktree 目录下直接用绝对路径跑主目录 tests 会加载旧代码（FAIL-012）。
- **R3 — 改进信号**:
  - 下次跨端（PC/NAS + 手机 Termux）联调涉及鉴权开关时，先明确读写分级策略，再落盘配置。
  - `project-save` 的校验器 `validate_closure.py` 仍期待 `ReadMe.md` 含版本头、且对 `Handover Book - Archive.md` 在未归档时也报缺少文件；在本项目仍坚持 `DEC-003` 不覆盖 `README.md`，验证时需明确说明该预期差异，不把脚本失败粉饰为全部通过。

---

### 📸 Session Snapshot: S-20261126-0030 | v0.3.0 | 2026-11-26 00:30

**Session Summary**: 围绕“微信直播解析 → 飞牛 NAS 远程录音转码 → 文字整理”全链路做架构体检与 GitHub 同类经验对照，分三路 Swarm（`hc:t9` / `hc:t10` / `hc:t12`）并行修复并补充测试，随后将改动同步至飞牛 NAS 重建 `wxlive-nas` 容器并完成全套验收。

**Delta from Previous Save (增量变化)**

- 📁 New files:
  - `.dockerignore`（仓库根构建排除规则）
  - `docs/daily__pipeline_reviews/2026-11-26.md`（当日复盘）
- ✏️ Modified files:
  - 本地抓流/代理：`wxlive/systemproxy.py`、`wxlive/gui_fetch.py`、`wxlive/fetch.py`
  - NAS 拉流/转码/保留：`wxlive/nas_runner.py`、`wxlive/stream_capture.py`、`wxlive/server.py`、`wxlive/retention.py`
  - ASR/后处理/完整性：`wxlive/quality.py`、`wxlive/session.py`、`wxlive/engine.py`、`wxlive/pipeline.py`、`wxlive/postprocess.py`、`wxlive/lexicon.py`、`wxlive/gemini_asr.py`、`scripts/auto_transcribe_after_stop.py`
  - 对应测试：`tests/test_{engine,gemini_asr,gui_fetch,gui_support,lexicon_path,nas_server,postprocess_truncation,quality,retention,stream_capture}.py`
  - 档案：`Handover Book.md`、`docs/project-state/RESUME.md`

**📊 Metrics**

| 指标 | 结果 |
|---|---|
| 全量测试 (`python -m pytest -q`) | **512 passed, 2 skipped** (55.43s) |
| NAS 健康检查 (`/api/health`) | 200 OK (`ok: true`, `ffmpeg_path: "/host-ffmpeg/ffmpeg"`, `job_count: 10`) |
| NAS 宿主机 ffmpeg 编码 | `8.1.1-mediasrv` 产出 `59844B` flv |
| NAS 中文库下载回归 | `200` (`112544B`) |
| NAS 容器出网 | `baidu.com` / `channels.weixin.qq.com` 均 `200` |

**Decisions Made This Session**: DEC-011, DEC-012, DEC-013

**Failed Attempts This Session**: FAIL-008, FAIL-009

**📋 Session Reflect Report**

- **R1 — 成功模式**: Swarm 三路按“代理抓流 / NAS 拉流保留 / ASR 后处理”边界清晰拆分并行，主会话统一收口 `.dockerignore` 位置与全量测试，最后用 NAS 实机重建闭环验收。
- **R2 — 边界案例**: `wong` 不在 NAS `docker` 组导致普通 SSH `docker` 命令报错，经用户授权后通过 `sudo -S` 完成重建；容器内无 `curl`，改用 Python `urllib` 完成容器内出网与健康检查。
- **R3 — 改进信号**: 真实桌面 GUI（Clash 断网态）与新版 NAS 容器真实 1h+ 直播推流仍待下一场开播实机验证。

---

### 📸 Session Snapshot: S-20261117-1900 | v0.2.0 | 2026-11-17 19:00

**Session Summary**: 用户提了四个问题（思考强度档位 / 音频格式 / 字段冗余 / 纠错耗时），
随后追加「设计验证流程跑格式对比」「改 16k」「完善项目」「跑一次全流程实测」「复盘」。

三个最有价值的发现都不是从代码里读出来的，而是实测逼出来的：

1. **22 分钟纠错耗时的真凶是串行批处理**（27 批），不是思考强度。
   并发 + 退避重试后 **615s → 51–80s**。顺带发现原逻辑把网关瞬时抖动
   当永久失败，导致 **4–12% 句子静默漏纠**。
2. **格式压缩"省 97%"是个陷阱**。四层验证后发现 ASR 自身噪声（±3.8%）
   比任何格式差异都大；而崩溃恢复测试中 **FLAC 彻底损坏、Opus 丢约 1 秒**。
   结论是**不改压缩格式**，改 16k WAV（无损、崩溃安全、省 67%）。
3. **全流程实测暴露「产物看起来正常但少了 3 分钟」**。网关 RTF 飙到 3.8 时
   收尾被截断，json 完全没生成，而 md/jsonl 看起来分布正常。

**Delta from Previous Save (增量变化)**

- 📁 New files:
  - `tests/test_output_layout.py`（11 条：分目录/兼容性/收尾落盘/停止原因/统计真实性）
  - `tests/test_lexicon_path.py`（4 条：路径归一化）
  - `docs/思考强度与后处理耗时实测.md`（436 行，四层验证法完整数据）
  - `docs/项目体检-2026-11-17.md`（165 行，含 7 项发现 + 修复记录）
  - `docs/全流程实测-2026-11-17.md`（130 行，10 分钟真实录制数据）
  - `docs/daily__pipeline_reviews/2026-11-17.md`
- ✏️ Modified files:
  - `wxlive/postprocess.py`（并发 + 退避重试 + `out_stats` 可追溯）
  - `wxlive/gemini_asr.py`（workers 4→8，推翻旧注释）
  - `wxlive/audio_capture.py`（`out_rate` 消费侧重采样 + `stop(reason=)`）
  - `wxlive/pipeline.py`（产物目录修正 + `_enqueue_tail` + `drain_timeout`）
  - `wxlive/session.py`（`audio_dir` 支持）
  - `wxlive/engine.py`（`failed_chunk_count` / `has_content_loss`）
  - `wxlive/config.py` / `userconfig.py`（`TextConfig` + 词表路径归一化）
  - `record_only.py` / `wxlive/gui.py` / `wxlive/cli.py`（16k + 停止原因）
- 🗑️ Deleted: `output/recordings/20261116_100221_wechat_live_real.wav`（16.5MB，
  零引用测试录音，用户批准）；6 个错位产物迁移到 `output/transcripts/`

**📊 Metrics**

| 指标 | 变化 |
|---|---|
| 测试数 | 277 → **293 passed** |
| 后处理耗时（1048 行） | 615s → **51–80s**（8–12x） |
| 录音体积 | 5.76 → **1.82 MB/分钟**（-67%） |
| ASR 并发 | 4 → 8 路（1.86x） |
| 提交数 | 10 个（`3260cab` → `6969cd4`） |

**⚠️ Failed Attempts This Session**

7 条，见 Zone B3 的 FAIL-004 ~ FAIL-007（本轮新增 4 条）。
其中 3 条是同一类错误：**验证链路前没有先量化噪声/确认激励源**。

**📋 Session Reflect Report**

**R1 — 成功模式（Success Patterns）**

- **`king-workstyle` 的"证据优先"规则在本轮价值最大。** 格式压缩那个任务，
  如果直接按"省 97% 就改"执行，会引入「崩溃丢最后 1 秒」的故障模式而不自知。
  正是因为坚持"先测噪声基线"，才发现 ASR 噪声比格式差异还大，
  从而得出"测不出来就不该改"的正确结论。
- **`wechat-live-recorder` 技能的"已确认实测结论"表避免了重复劳动。**
  多次想重新验证"2 倍速是否影响质量"时被这张表拦住，直接引用既有结论。

**R2 — 边界案例（Edge Cases）**

- **「跑一次全流程」这个要求让我纠结过要不要报告失败。**
  第一次跑 10 分钟，进程退出码 1、转写少 3 分钟、json 没生成。
  当时有两个选择：①报告"跑通了"（因为音频完整、md 有内容）；
  ②报告真实缺陷。选了 ②，并定位到根因是外部超时 + 收尾无告警。
  **这个选择是对的还是靠"用户要求实测"这个外部约束推着走的** ——
  如果用户只说"跑一下看看效果"，很可能就滑过去了。
- **用户在格式压缩上明确问了"有没有价值做"，而我第一版分析给的是"技术可行"。**
  这两者不是一回事：可行 ≠ 值得。补了工程维度（崩溃恢复）才回答问题。

**R3 — 改进信号（Improvement Signals）**

| 规则 | 本 Session 是否应触发 | 是否实际执行 |
|---|---|---|
| A1 先述计划 | Y（四个任务） | Y（先给 plan + AskUserQuestion） |
| A4 先问后做 | Y（格式策略/字段兼容/落地范围/派活方式） | Y |
| B1 强制核查 | Y（多处） | **部分 Y** —— 见下 |

**漏触发项**：`B1 强制核查` 在"体检报告写 session.py 有未使用导入"时没做到。
用 grep 数出现次数就下了结论，没做人工复核 → FAIL-006。
这是本轮唯一的"未核查就下结论"。

**需要细化的规则**：现有规则没覆盖「静态扫描结论的可信度」这一类 ——
建议加一条：**任何基于 grep / 正则的自动化结论，写进报告前必须至少抽查一处原文。**

**Skill 触发精度问题**：`herdr` 派工两次都成功（一次做对 4 项改动），
但**它的端到端验证只跑 1 次，不能替代我的多次交叉验证** ——
本轮失败批问题就是我复跑才发现的。这条应写进 herdr 派工的验收惯例：
**要求 agent 的验证结果，必须由我独立复跑一次才算数。**

---

### 📸 Session Snapshot: S-20261116-2345 | v0.1.0 | 2026-11-16 23:45

**Session Summary**: 用户报「GUI 隔段时间闪一下黑框」。定位到根因不是后台进程，而是状态条每 60 秒自检时用 `reg query` 读注册表——`pythonw` 无控制台，每个 console 子进程都会分配一个新控制台窗口。改用 `winreg` 直读（一次自检子进程 6 → 0）+ 新增 `winproc` 统一注入 `CREATE_NO_WINDOW`。途中撞出一个既有 bug：`_ui_queue` 初始化顺序错误导致防断网自检**从上线起从未生效**，一并修复。

**Delta from Previous Save (增量变化)**

- 📁 New files:
  - `wxlive/winproc.py`（54 行，统一隐藏窗口封装）
  - `tests/test_no_console_flash.py`（210 行 / 9 条断言）
  - `docs/GUI黑框闪烁治理.md`（142 行）
  - `Handover Book.md`（本文件）
  - `docs/daily__pipeline_reviews/2026-11-16.md` + `_TEMPLATE.md`
  - `docs/project-state/RESUME.md`
- ✏️ Modified files:
  - `wxlive/systemproxy.py`（+110/-58，`winreg` 直读 + 共享状态）
  - `wxlive/gui_fetch.py`（+48/-23，状态变量前移 + 不可见时跳过自检）
  - `wxlive/fetch.py`（ffmpeg/ffprobe 改 `winproc`）
  - `wxlive/stream_capture.py`（ffprobe/ffmpeg 改 `winproc`）
  - `auto_pipeline.py`（powershell 改 `winproc`）
  - `tests/test_gui_fetch.py`（+1 用例）
  - `TODO.md`
- 🗑️ Deleted files: 无
- 🔑 Key changes: 一次完整自检的子进程调用次数 **6 → 0**，耗时 180ms → 0.94ms；
  防回归测试盯"子进程个数"这个因，而非"看起来不闪"这个果

**Decisions Made This Session**: DEC-001, DEC-002, DEC-003, DEC-004

**Failed Attempts This Session**: FAIL-001, FAIL-002, FAIL-003

**Cognitive State at Close（会话关闭时的思维状态）**

- 主线已收口：修复、测试、文档、提交全部完成，工作区干净，236 passed 连跑三轮。
- 一条未闭合的线索：`tk.tcl` 偶发错误仍未定位。已确认是既有问题（HEAD 对照），
  但根因不明。**下一位接手者别急着用 skip 消掉它**。
- 另一个待验证的细节：`_fetch_view_visible()` 在"最小化到任务栏"时是否真返回 False。
  本轮只在一个 `withdraw()` 的场景验证过不抛异常，**没有验证跳过逻辑真的生效**。

**Context Window Highlights**

- 用户一开始猜"是不是 Go 之类的在后台跑"，这个假设被证伪。真正的洞察是：
  **间歇性 UI 现象先怀疑周期性动作，而周期性动作先查自己代码里的 `after()` 定时器。**
- 用户明确要求"我不想每次隔段时间屏幕就闪烁一下"——这不是洁癖，是可用性问题。

**Session Reflect Report**

- **R1 — 成功模式**
  - **"定量验证优先"救了这个任务。** 如果只满足于"黑框不见了"，就会停在
    "加个 flags"（FAIL-003）。正是因为绕回来看"一次自检到底起了几个进程"，
    才发现根因是轮询方式选错，而不是调用姿势不对。
  - **防回归测试盯因不盯果。** 把 `subprocess.run` patch 成炸弹 + 全仓扫描，
    这两条断言以后能自动拦住同类问题，而不是等用户再报"又闪了"。
  - **HEAD worktree 对照实验**（`git worktree add /tmp/head-probe HEAD`）是判断
    "这个 bug 是不是我改出来的"的高效手段，比反复 stash 干净。本轮用它证明了
    `tk.tcl` 错误与改动无关。

- **R2 — 边界案例**
  - **两次探针假阴性**（FAIL-001/002）。连续两次测出"现象无法复现"，
    差点得出"用户描述有误"的错误结论。转折点是想清楚"我测的对象本身
    就不可能体现出差异"。
  - **`ReadMe.md` / `README.md` 冲突**。skill 要求写 `ReadMe.md`，
    但那个名字在 Windows 上就是 GitHub 的 `README.md`。
    直接按 skill 执行会摧毁已提交的文档——这类"规则与现实的冲突"需要停下来判断，
    不能机械执行（见 DEC-003）。

- **R3 — 改进信号**
  - **下次做"间歇性 UI 现象"排查时，第一步就该问"有没有周期性定时器"，而不是
    "有没有后台进程"。** 本轮绕了远路（先去想 Go 后台），虽然最终收敛，但
    次序可以更好。
  - **验证探针本身要先自检。** 两次假阴性都是"探针没有能力体现差异"。
    以后写验证脚本前先问一句："如果 bug 存在，我这段代码能看出来吗？"
  - **project-save skill 的文件名假设需要适配 Windows**（`ReadMe.md` vs `README.md`）。
    已在 DEC-003 记录，但 skill 层面若继续在其他 Windows 项目复用，会重复踩。

**Compact File Tree Snapshot**

```
14-WeChat_live/
├── README.md                    # GitHub 面向用户说明（勿覆盖）
├── Handover Book.md             # ← 本文件（交接簿）
├── TODO.md
├── auto_pipeline.py
├── 启动.bat / 启动-调试.bat
├── wxlive/                      # 27 个模块
│   ├── winproc.py               # ★本轮新增：统一隐藏窗口
│   ├── systemproxy.py           # ★winreg 直读
│   ├── gui_fetch.py             # ★状态变量前移
│   ├── audio_capture.py / stream_capture.py
│   ├── gemini_asr.py / quality.py / transcriber.py
│   ├── lexicon.py / postprocess.py / render.py
│   └── ...
├── tests/                       # 18 个文件 / 236 用例
│   ├── test_no_console_flash.py # ★本轮新增 9 条
│   └── test_gui_fetch.py        # ★+1 条
├── docs/                        # 13 篇文档
│   ├── GUI黑框闪烁治理.md        # ★本轮新增
│   ├── daily__pipeline_reviews/ # ★本轮建立
│   └── project-state/RESUME.md  # ★本轮建立
├── lexicon/ / config/ / output/
└── tools/
```

---
