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
    ) -> None:
        self.primary = primary
        self.fallback = fallback
        self.max_failures = max_consecutive_failures
        self._failures = 0
        self._degraded = False
        self._primary_ok = 0
        self._primary_fail = 0

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
            if self.fallback is not None:
                base["fallback"] = self.fallback.stats
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
        return base
