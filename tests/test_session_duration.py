"""回归：duration_seconds 必须是音频时长，不是墙钟耗时。

背景：2026-11-16 实测 66.5 分钟回放被记成 33.7 分钟（00:33:40）。
根因：close() 用 (ended_at - started_at) 计算时长，而 ended_at 是
转写+纠错跑完之后的时间，把处理耗时也算进去了。
"""
from datetime import timedelta

from wxlive.session import _audio_duration


def test_uses_last_segment_end_not_wall_clock():
    """有分段时，取末段 end，而不是墙钟差。"""
    wall = timedelta(seconds=2020.9)  # 处理耗时 33.7 分钟
    segments = [
        {"start": 0.0, "end": 5.0, "text": "一"},
        {"start": 3974.0, "end": 3987.28, "text": "二"},
    ]
    assert _audio_duration(wall, segments) == 3987.3


def test_falls_back_to_wall_clock_when_no_segments():
    """无分段（如空录音）时退回墙钟差，保持原有行为。"""
    wall = timedelta(seconds=120.5)
    assert _audio_duration(wall, []) == 120.5


def test_ignores_segments_with_bad_end():
    """end 缺失或非法时不能被带偏。"""
    wall = timedelta(seconds=60)
    segments = [
        {"start": 0.0, "end": 100.0, "text": "好"},
        {"start": 90.0, "text": "缺 end"},
        {"start": 95.0, "end": None, "text": "end 为 None"},
        {"start": 99.0, "end": "abc", "text": "end 非数字"},
    ]
    assert _audio_duration(wall, segments) == 100.0


def test_takes_max_not_last():
    """分段顺序异常时也要取最大值，不能只信最后一条。"""
    wall = timedelta(seconds=10)
    segments = [
        {"start": 100.0, "end": 120.0, "text": "后面的"},
        {"start": 0.0, "end": 5.0, "text": "前面的"},
    ]
    assert _audio_duration(wall, segments) == 120.0
