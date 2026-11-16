# 待办事项

## 🔥 现在可以做（流地址直录已打通）

### 0. 微信里取流地址 → 接 pipeline
**状态**：工具和链路已验证，等你在微信里实际操作一次

工具已在后台跑着（如已退出，重新启动）：
```bash
cd /d/File/01_Vibe_Coding/14-WeChat_live/tools/wxcd
./wx_video_download.exe            # 注意：不要加 server 子命令！
```

然后：微信 PC 端 → 进直播 → "更多"按钮多出**下载图标** → 点它选清晰度
→ 自动复制 ffmpeg 命令 → 把 URL 抠出来：

```bash
.venv-trim/Scripts/python.exe -m wxlive stream "<URL>" --name 直播名
```

完整说明见 `docs/抓流操作手册.md`。

**待验证**：
- [ ] 微信里是否真的出现下载按钮
- [ ] 拿到的 FLV 地址 ffmpeg 能否直拉（txSecret 时效/UA 限制未知）

---

## ✅ 已完成（2026-11-16 12:36）

### A. 回放 66.5 分钟全量转写
- 音频：`output/recordings/20261116_111300_回放130min.wav`（365 MB，48k）
- 产物：`output/transcripts/20261116_122111_...回放130min.{md,jsonl}`
- **1561 句**，转写 9.1 分钟完成（130 分钟回放 @2 倍速 → 66.5 分钟音频）
- 质量抽查：暗语展开生效，如 `烧鸡（白云华赋）择高`
- LLM 纠错仍在跑（40 批 × 23 秒 ≈ 15 分钟），**不影响主产物可用**

### B. 老环境 `.venv` 清理完成：1.6G → 5K
- 确认无进程占用后执行（录音进程 4104 已于 12:19 退出）
- 根目录因被系统句柄锁定无法删除，仅清空内容，保留了空的 `.venv/`
- `启动.bat` 已优先使用 `.venv-trim`，无需改动
- 回收约 **1.6 GB**

---

## 📚 本次调研结论（2026-11-16 12:00-12:20）

### UA 伪装不可行（已实测证伪）
网传"F12 改手机 UA 就能看视频号直播"对**取流**无效：
1. 检测要求 UA 同时含 `MicroMessenger` + `WindowsWechat`（PC 微信专用标记）
2. 真正取流靠 `WeixinPrefecherJSBridge.invoke` / `wxNative.webTransfer` 原生桥，
   Chrome 里是 `undefined`
3. 实测未登录 POST `live/join_live` → `errCode 300330`（`MinNeedLogin`）

### 正确路线：本地 MITM + 脚本注入
- 工具：`tools/wxcd/wx_video_download.exe` v261106
- 关键配置：`proxy.upstreamProxy: http://127.0.0.1:10808`（否则断网）
- **大坑**：`server` 子命令硬编码 `proxy.enabled=false`，必须用根命令启动
- 已验证：双端口监听 / 上游透传正常 / MITM 解密 1.16MB / 注入标齐全 /
  系统代理未被改动 / 录音未受影响
