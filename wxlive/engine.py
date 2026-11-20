"""ASR 引擎选择与自动降级。

设计原则：**录音链路不能因为云端不确定性而中断。**

所以这里做双引擎包装：
- 主引擎（默认 gemini，质量高）
- 备引擎（本地 whisper，永远可用）
- 主引擎连续失败 N 次 → 本场会话自动切换到备引擎，不再重试

为什么不是简单 try/except 每段都重试：
实测云端会因 content_filter / 额度 / 网络 持续失败，
每段都先试云端再回退，会让整场延迟翻倍且日志噪音极大。
所以用"熔断"策略：连续失败 3 次就整场降级。
"""

from __future__ import annotations

import numpy as np


class FallbackEngine:
    """主引擎优先、失败自动熔断降级到本地引擎。"""

    def __init__(
        self,
        primary=None,
        fallback=None,
        max_consecutive_failures: int = 3,
        fallback_factory=None,
    ) -> None:
        self.primary = primary
        self._fallback = fallback
        self._fallback_factory = fallback_factory
        self.max_failures = max_consecutive_failures
        self._failures = 0
        self._degraded = False
        self._primary_ok = 0
        self._primary_fail = 0

    @property
    def fallback(self):
        """惰性获取备引擎：只有真正需要降级时才实例化本地模型，避免主引擎正常时白占内存。"""
        if self._fallback is None and self._fallback_factory is not None:
            self._fallback = self._fallback_factory()
        return self._fallback

    @fallback.setter
    def fallback(self, value) -> None:
        self._fallback = value

    def transcribe(self, audio: np.ndarray, sample_rate: int = 16000) -> list[dict]:
        # 已降级或没有主引擎 → 直接走本地
        if self._degraded or self.primary is None:
            return self.fallback.transcribe(audio, sample_rate) if self.fallback else []

        try:
            res = self.primary.transcribe(audio, sample_rate)
            self._failures = 0
            self._primary_ok += 1
            return res
        except Exception as e:  # noqa: BLE001
            self._failures += 1
            self._primary_fail += 1
            print(f"[engine] 主引擎失败({self._failures}/{self.max_failures}): {str(e)[:150]}")

            if self._failures >= self.max_failures:
                self._degraded = True
                print(
                    "[engine] ⚠ 主引擎连续失败，本场会话降级为本地引擎。"
                    "后续片段不再尝试云端，保证录音不断流。"
                )

            if self.fallback is None:
                return []
            return self.fallback.transcribe(audio, sample_rate)

    @property
    def stats(self) -> dict:
        base = {}
        try:
            # 未触发降级时不访问 self.fallback，避免为了取统计而误加载本地模型
            if self._fallback is not None:
                base["fallback"] = self._fallback.stats
        except Exception:
            pass
        try:
            if self.primary is not None and not self._degraded:
                base["primary"] = self.primary.stats
        except Exception:
            pass
        base["degraded"] = self._degraded
        base["primary_ok"] = self._primary_ok
        base["primary_fail"] = self._primary_fail

        # ---- 不能只看 primary_ok/primary_fail ----
        # 2026-11-17 实测踩到：4 个音频块**全部**转写失败（落的都是占位符），
        # 但 stats 显示 primary_ok=4 / primary_fail=0。
        # 原因：gemini_asr 在内部把"整块失败"吞掉了（返回占位符而非抛异常），
        # 所以 FallbackEngine 层面看到的是"调用成功"。
        # 结果：转写全废，监控指标却一片绿 —— 这类"假健康"最危险。
        # 这里把下游记录的 failed_chunks 数并入统计，并给出一个直接可判的字段。
        failed_chunks: list = []
        try:
            primary = base.get("primary") or {}
            failed_chunks = primary.get("failed_chunks") or []
        except Exception:
            pass
        base["failed_chunk_count"] = len(failed_chunks)
        base["calls_with_failures"] = len(failed_chunks)
        # 一眼可判：本次转写是否有内容真的丢了。
        base["has_content_loss"] = bool(failed_chunks)
        return base
