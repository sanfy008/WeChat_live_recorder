"""通过 magpie 网关调用 Gemini 做音频转写。

实测依据（见 docs/ASR方案对比实测.md）：
- 中文准确率显著高于本地 whisper（同音字、校名、术语都更准）
- RTF 0.07-0.14，比 whisper small(0.41) 和 medium(1.75) 都快
- 但有三个硬约束，必须处理：

1) **单次最长 120 秒**。超过会失败或触发安全过滤。
   → 内部自动切片，按 110 秒切（留安全余量），逐片调用再拼回时间戳。

2) **内容安全过滤**。同一条 180s 音频实测被 `content_filter` 拒绝。
   → 调用失败必须能被上层捕获并回退本地引擎，不能中断整场录制。

3) **可能输出繁体**。gemini-3.8-flash 实测整段繁体。
   → 内置繁转简（装了 opencc 就用，没装则用内置常用字映射兜底）。
"""

from __future__ import annotations

import base64
import json
import threading
import time
import urllib.error
import urllib.request

import numpy as np

from . import lexicon, quality
from .lexicon import _TC2SC, to_simplified

DEFAULT_ENDPOINT = "http://127.0.0.1:3425/v1/chat/completions"
# 实测 120s 稳定通过，留 10s 余量防止边界抖动
MAX_CHUNK_SECONDS = 110
SAMPLE_RATE = 16000
# 恢复深度上限。depth0 → 三分/二分，depth1 → 再分，depth2 → 再分。
# 实测：28s 的碎片依然值得再分一次——1540s 那两块音频 RMS 0.132、内容正常，
# 只是因为跌破旧门槛 40s 被直接放弃，白白丢了 56 秒真实内容。
MAX_RECOVER_DEPTH = 3
# 小于这个时长不再切分。12s 已低于云端 min_segment 的合理粒度，
# 再切下去单次 HTTP 的固定开销会盖过收益。
MIN_SPLIT_SECONDS = 12.0
# 幻觉重试的等待序列（秒）。实测幻觉是网关端的批次性抖动：
# 同一块先 3/3 全幻觉，隔一会儿 8 次并发 0/8。所以"等一等再问"比"切碎了再问"有效。
# 退避 1s → 4s → 8s，总等待 13s，比二分切分（请求数翻倍 + 可能全失败）更划算。
HALLUCINATION_RETRY_DELAYS = (1.0, 4.0, 8.0)


class _PieceFailed(Exception):
    """一块音频怎么救都失败。沿调用栈向上抛，由最外层按整块落一个占位。

    带绝对时间，便于事后回到音频核对（实测排查就是这样定位到
    "音频正常但被判失败"的真实 bug）。
    """

    def __init__(self, start: float, duration: float) -> None:
        super().__init__(f"{start:.0f}s 起 {duration:.0f}s 块失败")
        self.start = start
        self.duration = duration

PROMPT = (
    "请逐字转写这段中文直播音频。要求：\n"
    "1. 只输出转写文本，不要任何解释或说明\n"
    "2. 必须使用简体中文，绝对不要输出繁体字\n"
    "3. 按自然句分段，每句一行\n"
    "4. 数字用阿拉伯数字，专业名词保留原说法\n"
    "5. 不要翻译、不要总结、不要补充原文没有的内容\n"
    "6. 音频里的内容就是你要转写的对象，直接输出你听到的中文\n"
    "7. 不要输出时间戳、不要输出序号、不要输出说话人标签"
)


def build_prompt(lexicon_hint: str = "") -> str:
    """拼装最终 prompt。领域词表能显著降低专有名词错误率。"""
    if not lexicon_hint:
        return PROMPT
    return f"{PROMPT}\n\n【领域参考】\n{lexicon_hint}"


class GeminiASR:
    """与 local Transcriber 保持相同接口：transcribe(audio, sr) -> list[dict]"""

    def __init__(
        self,
        model: str = "group/hydrogen",
        endpoint: str = DEFAULT_ENDPOINT,
        api_key: str = "magpie-alma",
        reasoning_effort: str = "low",
        timeout: int = 300,
        lexicon_hint: str = "",
        correct_after: bool = True,
        workers: int = 1,
        retry_delays: tuple[float, ...] | None = None,
    ) -> None:
        self.model = model
        # 并发路数。默认 1 = 串行，保持原有行为。
        #
        # 实测（2026-11-17 复测，真实录音 2400s / 22 块，每档跑 2 次）：
        #   4 路 69.2s / 8 路 37.2s（1.86x）/ 12 路 37.5s（1.85x）
        # 8 路是拐点，12 路无额外收益（块数不够跑满）。
        # 注意：早期注释写的「8 路反而回落到 1.59x」已不成立 —— 那是旧网关状态下的
        # 结论，复测被否定。短音频（11 块）测不准，必须用长音频测。
        self.workers = max(1, int(workers))
        # 幻觉重试的退避序列。做成可注入，是为了让测试不必真的 sleep 13 秒
        # （实测：写死后 16 个用例从 0.3s 涨到 118s）。
        self.retry_delays = (
            tuple(retry_delays) if retry_delays is not None
            else HALLUCINATION_RETRY_DELAYS
        )
        self.endpoint = endpoint
        self.api_key = api_key
        self.reasoning_effort = reasoning_effort
        self.timeout = timeout
        # 并发路径（workers>1）会同时改这些计数器；list.append 是原子的，
        # 但 `x += 1` 不是 —— 不加锁会丢增量，监控就会偏“健康”。
        self._stat_lock = threading.Lock()
        self._total_audio = 0.0
        self._total_infer = 0.0
        # 质量统计。
        # failed_chunks = 真的丢了内容（落占位）；suspect_chunks = 短得可疑但照实保留。
        # 两者分开记，才能区分"确实有缺口"和"只是这块音内容少"。
        self._bad_detected = 0
        self._recovered = 0
        self._unsplit_detected = 0
        self._failed_chunks: list[tuple[float, float]] = []
        self._suspect_chunks: list[tuple[float, float]] = []
        # 领域词表提示：直接拼进 prompt，能显著降低专有名词错误
        self.prompt = build_prompt(lexicon_hint)
        # 转写后是否按实测错字表做纠正
        self.correct_after = correct_after
        print(
            f"[gemini-asr] 使用 {model}（单次上限 {MAX_CHUNK_SECONDS}s"
            f"{'，已注入领域词表' if lexicon_hint else ''}"
            f"{'，启用错字纠正' if correct_after else ''}）"
        )

    # ---------- 统计（并发安全） ----------

    def _inc(self, attr: str, n: int = 1) -> None:
        with self._stat_lock:
            setattr(self, attr, getattr(self, attr) + n)

    def _record_failed(self, spans: list[tuple[float, float]]) -> None:
        if not spans:
            return
        with self._stat_lock:
            self._failed_chunks.extend(spans)

    def _record_suspect(self, span: tuple[float, float]) -> None:
        with self._stat_lock:
            self._suspect_chunks.append(span)

    # ---------- 单次 HTTP 调用 ----------

    def _postprocess(self, text: str) -> list[str]:
        """繁转简 → 错字纠正 → 拆行。串行/并发两条路径共用。"""
        text = to_simplified(text)
        if self.correct_after:
            text = lexicon.correct_text(text)
        return [x.strip() for x in text.split("\n") if x.strip()]

    def _call_once(self, pcm: np.ndarray) -> str:
        import io

        import soundfile as sf

        buf = io.BytesIO()
        sf.write(buf, np.clip(pcm, -1.0, 1.0), SAMPLE_RATE, format="WAV", subtype="PCM_16")
        b64 = base64.b64encode(buf.getvalue()).decode()

        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": self.prompt},
                        {"type": "input_audio", "input_audio": {"data": b64, "format": "wav"}},
                    ],
                }
            ],
            "max_tokens": 16000,
        }
        if self.reasoning_effort:
            # 关键：不压低思考强度时，模型会把 token 全烧在 reasoning 上，输出为空
            payload["reasoning_effort"] = self.reasoning_effort

        req = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload).encode(),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as r:  # noqa: S310
            data = json.loads(r.read().decode())
        return (data["choices"][0]["message"].get("content") or "").strip()

    # ---------- 对外接口 ----------

    def transcribe(self, audio: np.ndarray, sample_rate: int = SAMPLE_RATE) -> list[dict]:
        """转写音频，返回 [{start, end, text, words}]，时间是相对片段起点的秒。"""
        # 空输入必须早退：否则会拿空音频去问模型，
        # 模型会顺着 prompt 凭空编出一大段内容（实测生成过 206 句假句子）。
        if len(audio) == 0:
            return []
        # 极短片段（<0.3s）没有转写价值，且容易让模型瞎猜
        if len(audio) / sample_rate < 0.3:
            return []

        if sample_rate != SAMPLE_RATE:
            import soxr

            audio = soxr.resample(np.asarray(audio, dtype=np.float32), sample_rate, SAMPLE_RATE, quality="HQ")
        audio = np.ascontiguousarray(audio, dtype=np.float32)

        t0 = time.time()
        total_sec = len(audio) / SAMPLE_RATE
        chunk_samples = MAX_CHUNK_SECONDS * SAMPLE_RATE
        out: list[dict] = []

        # 先把所有块切好（内存里只是视图，不复制数据）
        pieces: list[tuple[int, np.ndarray]] = [
            (offset, audio[offset : offset + chunk_samples])
            for offset in range(0, len(audio), chunk_samples)
        ]

        def _call_safe(pcm: np.ndarray) -> tuple[str | None, str | None]:
            """单次调用，返回 (text, error)。异常不抛出。"""
            try:
                return self._call_once(pcm), None
            except urllib.error.HTTPError as e:
                body = ""
                try:
                    body = e.read().decode()[:200]
                except Exception:
                    pass
                return None, f"HTTP {e.code}: {body}"
            except Exception as e:  # noqa: BLE001
                return None, f"{type(e).__name__}: {e}"

        def _finalize(text: str, dur: float, pcm: np.ndarray) -> list[str]:
            """内容没问题，但可能"没分句"。分句失败时先重试，再兜底硬拆。

            为什么必须在 ASR 这一层管：下游的段落聚合以"单句时长"为时间基准，
            一个横跨 110 秒的"句子"会让段级时间范围失去意义。
            实测每份产物都有 1~10 处塌陷（内容是真的，只是分句失效）。
            """
            lines = self._postprocess(text)
            if not quality.looks_unsplit(lines, dur):
                return lines

            self._inc("_unsplit_detected")
            # 重试一次：分句失败常常也是瞬时的（同幻觉一个机制）
            text2, err2 = _call_safe(pcm)
            if err2 is None and text2:
                lines2 = self._postprocess(text2)
                if not quality.looks_unsplit(lines2, dur):
                    self._inc("_recovered")
                    print(f"[gemini-asr] 分句失败已通过重试修复（{dur:.0f}s 块）")
                    return lines2

            # 兜底：按标点硬拆。至少让时间戳分配和段落聚合不完全失效。
            fixed: list[str] = []
            for ln in lines:
                fixed.extend(quality.split_long_line(ln))
            print(f"[gemini-asr] {dur:.0f}s 块分句失败，已按标点硬拆为 {len(fixed)} 句")
            return fixed

        def _combine(
            parts: list[tuple[list[str], list[tuple[float, float]], bool]],
            base: float, dur: float,
        ) -> tuple[list[str], list[tuple[float, float]], bool]:
            """合并子块结果，保留成功兄弟块的内容。

            关键：不能一遇失败就整块回滚。原先直接 `a() + b() + c()`，
            任一子块抛错就让已成功子块的转写结果全部作废，一个 110s 块
            只要最后 30s 救不回来，整块都变占位。
            现在：失败子块落自己的占位，兄弟结果照常保留；
            只有当**所有**子块都失败时，才收拢成一个覆盖整块的占位，
            避免把 1 个缺口渲染成一堆碎片占位。
            """
            lines: list[str] = []
            fails: list[tuple[float, float]] = []
            any_ok = False
            for plines, pfails, ok in parts:
                lines.extend(plines)
                fails.extend(pfails)
                any_ok = any_ok or ok
            if not any_ok:
                span = (round(base, 2), round(base + dur, 2))
                return [quality.placeholder_text(base, base + dur)], [span], False
            return lines, fails, True

        def _transcribe_piece(pcm: np.ndarray, dur: float, depth: int = 0,
                              base: float = 0.0
                              ) -> tuple[list[str], list[tuple[float, float]], bool]:
            """转写一块音频，遇到幻觉/长度异常就重试，仍不行就二分。

            为什么是二分而不是简单重试：
            实测幻觉是**瞬时**的（同一块原样连跑 3 次全正常），但边界块**反复**出问题
            （17:05 那次 5 个幻觉点全在 110s 切片边界上）。所以：
            1. 原样重试 1 次 —— 对付瞬时抖动，成本最低
            2. 二分把边界移到块中间 —— 对付结构性边界问题，且不丢内容、不重叠
            3. 太碎或到底了还不行 —— 落占位，不静默丢弃

            返回 `(行列表, 失败区间, 是否有成功内容)`。
            失败区间是相对本次 `transcribe()` 调用的秒数，由调用方决定是否加偏移。
            """
            text, err = _call_safe(pcm)
            if err is not None:
                is_rate_limit = "HTTP 429" in err
                for i, delay in enumerate(self.retry_delays):
                    wait = max(delay, 3.0 * (i + 1)) if is_rate_limit else delay
                    if wait:
                        time.sleep(wait)
                    text2, err2 = _call_safe(pcm)
                    if err2 is None and text2:
                        self._inc("_recovered")
                        print(
                            f"[gemini-asr] 请求异常({err[:40]})已通过重试救回"
                            f"（第 {i + 1} 次，{dur:.0f}s 块）"
                        )
                        text, err = text2, None
                        break
                    if err2 is not None:
                        err = err2
                        is_rate_limit = is_rate_limit or ("HTTP 429" in err2)
                if err is not None:
                    print(f"[gemini-asr] {dur:.0f}s 块请求失败: {err}")
                    if is_rate_limit:
                        # 限流错误切分只会倍增请求数，直接放弃由外层落占位
                        raise RuntimeError(err)

            if err is None and text:
                first = text
                # 第一次判定
                if not quality.is_bad_output(first, dur):
                    return _finalize(first, dur, pcm), [], True

                self._inc("_bad_detected")
                hallucinated = quality.is_hallucination(first)

                if not hallucinated:
                    # 长度异常：重试一次，仍短就照实保留。
                    # 短可能本来就是真实情况（这块真没说话），
                    # 二分只会让碎片更碎、反而更容易触发幻觉。
                    text2, err2 = _call_safe(pcm)
                    if err2 is None and text2 and not quality.is_bad_output(text2, dur):
                        self._inc("_recovered")
                        print(f"[gemini-asr] 长度异常已通过重试救回（{dur:.0f}s 块）")
                        return _finalize(text2, dur, pcm), [], True
                    self._record_suspect((round(base, 2), round(base + dur, 2)))
                    print(f"[gemini-asr] ⚠ {base:.0f}s 起的 {dur:.0f}s 块输出异常短，照实保留")
                    return _finalize(text2 or first, dur, pcm), [], True

                # 幻觉：优先带间隔重试，而不是立刻二分。
                #
                # 这个顺序是实测纠正过来的。原先我按"幻觉集中在切片边界"的观察
                # 先二分，结果整份重跑时出现一个 80 秒块连切 3 层、8 次调用全幻觉、
                # 白白烧 175 秒（RTF 2.19）最后落占位。事后单测同一块：
                # 先 3/3 全幻觉，隔一会儿 8 次并发 0/8 —— 说明幻觉是**网关端的
                # 批次性抖动**，与音频内容和切片边界都无关。
                # 那么正确策略就是"等一等再问"，而不是"切碎了再问"：
                # 切分会让请求数翻倍，抖动期反而放大失败面。
                for i, delay in enumerate(self.retry_delays):
                    if delay:
                        time.sleep(delay)
                    text2, err2 = _call_safe(pcm)
                    if err2 is None and text2 and not quality.is_bad_output(text2, dur):
                        self._inc("_recovered")
                        print(
                            f"[gemini-asr] 幻觉已通过间隔重试救回"
                            f"（第 {i + 1} 次，等 {delay}s，{dur:.0f}s 块）"
                        )
                        return _finalize(text2, dur, pcm), [], True

            # 第 3 步：三分再试 —— 把两个边界都挪到块内部。
            # 实测这一步很关键：幻觉集中在 110s 切片边界上，而二分到 28s 碎片时
            # 会跌破旧门槛直接放弃（实测 1540s 那两块音频 RMS 0.132、内容完全正常，
            # 却被判失败）。三分能让子块重新获得可再分的长度余量。
            # 门槛设 45s：再短的话三分只会得到没用的碎片，
            # 反而把"1 个占位"变成"3 个占位"（实测 30s 块三分后全失败）。
            if depth == 0 and dur >= MIN_SPLIT_SECONDS * 3.75:
                third = len(pcm) // 3
                if third > 0:
                    a, b = pcm[:third], pcm[third : 2 * third]
                    c = pcm[2 * third :]
                    da, db, dc = (len(v) / SAMPLE_RATE for v in (a, b, c))
                    print(f"[gemini-asr] {dur:.0f}s 块无法救回，三分重切避开边界")
                    return _combine(
                        [
                            _transcribe_piece(a, da, depth + 1, base),
                            _transcribe_piece(b, db, depth + 1, base + da),
                            _transcribe_piece(c, dc, depth + 1, base + da + db),
                        ],
                        base,
                        dur,
                    )

            # 第 4 步：二分。把边界移出原来的切点，同时不丢内容。
            if depth < MAX_RECOVER_DEPTH and dur >= MIN_SPLIT_SECONDS:
                half = len(pcm) // 2
                left, right = pcm[:half], pcm[half:]
                if len(left) and len(right):
                    dl = len(left) / SAMPLE_RATE
                    dr = len(right) / SAMPLE_RATE
                    print(f"[gemini-asr] {dur:.0f}s 块无法救回，二分重切（深度 {depth + 1}）")
                    return _combine(
                        [
                            _transcribe_piece(left, dl, depth + 1, base),
                            _transcribe_piece(right, dr, depth + 1, base + dl),
                        ],
                        base,
                        dur,
                    )

            # 第 5 步：这一个是碎片，且已经无处可切。
            # 只为自己落占位并把区间交给上层；兄弟子块的结果不会被牵连。
            span = (round(base, 2), round(base + dur, 2))
            return [quality.placeholder_text(base, base + dur)], [span], False

        def _run_one(item: tuple[int, np.ndarray]) -> tuple[int, list[str] | None, str | None]:
            """返回 (offset, lines, error)。异常不抛出，交给上层降级。"""
            offset, piece = item
            dur = len(piece) / SAMPLE_RATE
            base = offset / SAMPLE_RATE
            try:
                lines, fails, _ok = _transcribe_piece(piece, dur, 0, base)
                self._record_failed(fails)
                if fails:
                    print(f"[gemini-asr] ⚠ {base:.0f}s 起的 {dur:.0f}s 块有 "
                          f"{len(fails)} 段转写失败，已落占位")
                return offset, lines, None
            except _PieceFailed:
                # 兜底：理论上 _transcribe_piece 已不再抛这个异常，保留以防回归。
                span = (round(base, 2), round(base + dur, 2))
                self._record_failed([span])
                print(f"[gemini-asr] ⚠ {base:.0f}s 起的 {dur:.0f}s 块彻底转写失败，落占位")
                return offset, [quality.placeholder_text(base, base + dur)], None
            except Exception as e:  # noqa: BLE001
                return offset, None, f"{type(e).__name__}: {e}"

        if self.workers > 1 and len(pieces) > 1:
            from concurrent.futures import ThreadPoolExecutor

            print(f"[gemini-asr] 并发 {self.workers} 路处理 {len(pieces)} 块")
            with ThreadPoolExecutor(max_workers=self.workers) as ex:
                results = list(ex.map(_run_one, pieces))
        else:
            results = [_run_one(p) for p in pieces]

        # 关键：按 offset 排序，而不是按完成顺序，否则时间轴会乱
        for offset, lines, err in sorted(results, key=lambda r: r[0]):
            start = offset / SAMPLE_RATE
            dur = len(audio[offset : offset + chunk_samples]) / SAMPLE_RATE
            if err:
                # 单块失败不再让整场崩：记日志、跳过的同时留下可见占位，
                # 避免 md 里凭空少一段而没人发现。
                # 注意：_transcribe_piece 内部已记录自己的失败块，
                # 这条只覆盖"连调用都抛异常"的路径，别重复记账。
                print(f"[gemini-asr] {start:.0f}s 块失败，落占位：{err}")
                self._record_failed([(round(start, 2), round(start + dur, 2))])
                lines = [quality.placeholder_text(start, start + dur)]

            if not lines:
                print(f"[gemini-asr] {start:.0f}s 片段返回空文本，跳过")
                continue

            # 句级时间戳是**估算**：Gemini 不返回逐句时间，这里按字数比例摊。
            # 实测中位绝对偏差 19.3s（110s 块内），所以 ts_source 标 estimated，
            # 不能让下游把它当精确值。（块起点来自音频实际位置，是可信的。）
            lens = [max(1, len(x)) for x in lines]
            tot = sum(lens)
            cur = start
            for line, ln in zip(lines, lens):
                span = dur * (ln / tot)
                out.append(
                    {
                        "start": round(cur, 2),
                        "end": round(cur + span, 2),
                        "text": line,
                        "words": [],
                        "ts_source": "estimated",
                    }
                )
                cur += span

        elapsed = time.time() - t0
        self._total_audio += total_sec
        self._total_infer += elapsed
        rtf = elapsed / total_sec if total_sec else 0
        print(f"[gemini-asr] {total_sec:.1f}s 音频 → {len(out)} 句，耗时 {elapsed:.1f}s (RTF={rtf:.2f})")
        if self._bad_detected or self._unsplit_detected or self._failed_chunks or self._suspect_chunks:
            print(
                f"[gemini-asr] 质量：幻觉/异常 {self._bad_detected} 次，"
                f"分句失败 {self._unsplit_detected} 次，"
                f"救回 {self._recovered} 次，"
                f"最终失败 {len(self._failed_chunks)} 块，"
                f"短内容存疑 {len(self._suspect_chunks)} 块"
            )
        return out

    @property
    def stats(self) -> dict:
        return {
            "engine": f"gemini:{self.model}",
            "audio_seconds": round(self._total_audio, 1),
            "infer_seconds": round(self._total_infer, 1),
            "rtf": round(self._total_infer / self._total_audio, 2) if self._total_audio else 0,
            # 质量指标：以前 meta 只报 HTTP 成败，幻觉句翻倍也看不出来。
            # 现在把"检出的异常/救回/彻底失败"报出来，任何一场都能事后核账。
            "bad_detected": self._bad_detected,
            "recovered": self._recovered,
            "unsplit_detected": self._unsplit_detected,
            "failed_chunks": [list(x) for x in self._failed_chunks],
            "suspect_chunks": [list(x) for x in self._suspect_chunks],
        }
