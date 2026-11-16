# 微信直播「录音 → 转录 → 文本」GitHub 现成方案调研

调研时间：2026-11-16 · 检索源：GitHub Search API
结论先行：**没有一条完整的现成流水线**。社区在「抓流/录制」和「流式转写」两端都有成熟件，但中间是断开的，
需要自己拼。好消息是拼装成本很低。

---

## 一、三条可行技术路线

### 路线 A：系统音频采集（WASAPI loopback）★ 推荐起步
直接从声卡输出端抓声音，**完全不碰微信**：不装证书、不改代理、不担心版本更新失效。

- 原理：Windows WASAPI 的 Loopback 模式，录制「正在播放的声音」，与播放器无关
- 优点：零侵入、与微信版本解耦、微信直播/群直播/语音通话全都通吃
- 缺点：必须真的在放声音（不能静音、不能独占音频）；音质是重编码后的
- 关键库：[PyAudioWPatch](https://github.com/s0d3s/PyAudioWPatch)（243★，PortAudio 的 WASAPI loopback fork，事实标准）

### 路线 B：拉流直录（FLV / HLS）★ 音质最优、最省资源
拿到直播流的真实地址，`ffmpeg -c copy` 直接录，只保留音轨。

- 优点：原始音轨、CPU/内存极低、可 24h 无人值守、可断流重连
- 缺点：必须先拿到带签名的流地址（见路线 C）；微信端风控一变就可能失效

### 路线 C：MITM 代理嗅探 + JSBridge 注入（拿地址的正规做法）
本地起 mitmproxy 作系统代理 → 解密 HTTPS → 改写页面注入钩子 → 把播放器内部的流地址回传。

- 这是所有「视频号下载器」的共同底层，已经非常成熟
- 代价：要装根证书、要有管理员权限、要跟微信的页面结构对抗

---

## 二、值得借鉴的具体项目

### 1. 抓流 / 录制类

| 项目 | ★ | 语言 | 可借鉴什么 |
|---|---|---|---|
| [putyy/res-downloader](https://github.com/putyy/res-downloader) | 21.3k | Go | 通用资源嗅探器，代理抓包 + 资源分类，支持视频号/小程序/直播流。工程化程度最高，可参考 UI 与代理管理 |
| [ltaoo/wx_channels_download](https://github.com/ltaoo/wx_channels_download) | 10.6k | Go | 一手抓包 + 解密，自动装证书、Windows/macOS 双端。**实际在用最多的方案** |
| [qiye45/wechatVideoDownload](https://github.com/qiye45/wechatVideoDownload) | 6.1k | — | 明确支持「直播分段时间录制」，即长直播切片落盘，思路值得抄 |
| [gui8912/wxchannels-downloader](https://github.com/gui8912/wxchannels-downloader) | 1 | Python | **README 把机制讲得最透**（见下节「关键机制」），双产线共用本地网页，直播边拉边存 + 断流重连 + 开播蹲守 |
| [yszhu09/wechat-video-recorder](https://github.com/yszhu09/wechat-video-recorder) | 10 | Python | mitmproxy + ffmpeg 最小实现（Linux），**结构最简单，最适合当改造起点** |
| [Laihiujin/LIVE](https://github.com/Laihiujin/LIVE) | 4 | Rust | 「矩阵可视化直播录制，开箱即录」，多直播间并发录制的调度设计 |
| [banfg56/wx_channels](https://github.com/banfg56/wx_channels) | 6 | Go | 视频号后端接口模拟，直接调接口拿直播数据与回放地址（不经浏览器）|

### 2. 音频采集 + 流式转写类

| 项目 | ★ | 可借鉴什么 |
|---|---|---|
| [QuentinFuxa/WhisperLiveKit](https://github.com/QuentinFuxa/WhisperLiveKit) | 11.3k | **流式 ASR 的天花板**：超低延迟、说话人分离、翻译、OpenAI/Deepgram 兼容 API。自托管 |
| [TheDeathDragon/LiveTranslate](https://github.com/TheDeathDragon/LiveTranslate) | 914 | **和你的需求重合度最高**：Windows + WASAPI loopback 抓系统音频 + Silero VAD(32ms 分块) + 多引擎 ASR（faster-whisper / SenseVoice / FunASR Nano）+ 可选麦克风混音。把「翻译」换成「转写落盘」基本就是成品 |
| [jianminggan/wechat-video-subtitle](https://github.com/jianminggan/wechat-video-subtitle) | 13 | 视频号视频/直播回放 → Markdown 逐字稿的 AI Agent Skill。**输出的逐字稿格式可直接参考** |
| [jun7799/scribe-transcribe](https://github.com/jun7799/scribe-transcribe) | 146 | Python CLI 封装，下载 + 转写二合一，适合看 CLI 交互设计 |

---

## 三、关键机制（别人踩过的坑，直接抄）

来自 [gui8912/wxchannels-downloader](https://github.com/gui8912/wxchannels-downloader) 的实测结论：

1. **直播就是一条带签名的 FLV 长连接**
   `https://pull-m1.wxlivecdn.com/trtc_xxx/orig_xxx.flv?txSecret=...&txTime=6AE401F7`
   - `txTime` 是十六进制过期时间戳，配套 `txSecret` 签名
   - 该项目称**不需要登录态**即可拉流（需自行验证，微信常改）
2. **断流是常态**（切片 / 负载均衡都会关连接），必须内置重连
3. **重连时服务端会重发 13 字节 FLV 文件头**（`FLV`+版本+标志+头长度+`PreviousTagSize0`），
   必须丢掉，否则文件中间插头会导致时间戳错乱、音频不同步 —— **这是最容易翻车的一点**
4. **回放文件前 128 KB 被逐字节加密**（Isaac64 密钥流 XOR，用微信自带 `wasm_video_decode.wasm` 解密）。
   服务端用 `X-encflag: 1` / `X-enclen: 131072` 明示。只做直播录音的话可完全绕开
5. **`decode_key` 抓不到 HTTP 包**：页面通过 `WeixinJSBridge.invoke('finderH5ExtTransfer', ...)`
   向微信原生层要数据，走 WebView 内部通道。只能注入钩子替换 `WeixinJSBridge.invoke` 再回传
6. **URL 每次请求都换一套 nonce**，按 URL 去重等于没去重 → 按「标题 + 文件大小」认人
7. `url` 只有半截，签名参数在 `url_token` 里，两者拼接才能拿到 206

---

## 四、推荐落地方案

针对「只要录音 + 文字，供后续 LLM 分析」，建议 **A 为主 + B 为可选增强**：

```
┌─ 采集层 ────────────────────────────────┐
│ 首选 WASAPI loopback 实时抓系统音频       │
│   PyAudioWPatch / soundcard  →  16kHz mono│
│ 增强：抓到 FLV 地址时改用 ffmpeg -vn 直抽音轨│
└──────────────┬──────────────────────────┘
               │ PCM 流（分块）
┌──────────────▼──────────────────────────┐
│ VAD（Silero，32ms 帧）→ 切出发言片段      │
└──────────────┬──────────────────────────┘
               │
┌──────────────▼──────────────────────────┐
│ ASR：faster-whisper / SenseVoice(中文更准)│
│  流式增量出字 + 时间戳；录音同时留 WAV 备份 │
└──────────────┬──────────────────────────┘
               │
┌──────────────▼──────────────────────────┐
│ 落盘 → 结构化 JSON + Markdown             │
│  后续喂 LLM 做摘要/要点/QA 抽取            │
└─────────────────────────────────────────┘
```

### 为什么这个顺序
- **先 A 后 B**：路线 A 半天能跑通，不碰微信风控；路线 B 音质更好但工程量大、随时可能因微信改版失效
- **录音一定要留原始 WAV**：ASR 引擎会换、模型会升级，原始音频是唯一不会贬值的资产
- **不做实时字幕也可以先做准实时**：直播场景对「延迟」的容忍度远高于会议同传，30s 一批离线转写准确率显著更高

### 输出数据结构建议（为 LLM 分析预留）
```json
{
  "source": "wechat_channels",
  "room": "直播间标题",
  "started_at": "2026-11-16T20:00:00+08:00",
  "audio_file": "recordings/20261116_200000.wav",
  "segments": [
    {"start": 12.4, "end": 18.9, "speaker": "S1", "text": "..."}
  ],
  "full_text": "...",
  "meta": {"asr_model": "sensevoice-small", "duration": 3600}
}
```
带 `speaker` + 秒级 `start/end`，LLM 才能做「谁在什么时候说了什么」的溯源式分析。

---

## 五、立即可跑的最小验证（建议第一步）

1. `pip install pyaudiowpatch soundfile` → 录 30 秒系统声音存 WAV，确认能抓到微信直播的声
2. `faster-whisper` 转这一段，看中文识别质量是否够用
3. 通了之后再决定是否上流式 + 是否要做 FLV 直录

这一步 1 小时内能出结论，且失败成本极低。
