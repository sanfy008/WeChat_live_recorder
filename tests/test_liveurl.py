"""从 ffmpeg 命令行提取直播流地址的测试（纯字符串 + 去重逻辑，不碰真实进程）。"""

from __future__ import annotations

import json
import threading
import time
import urllib.request

import pytest

from wxlive import liveurl

LIVE_URL = (
    "http://pull-m1.wxlivecdn.com/trtc_1400419933/orig_2042980977478071617_orig.flv"
    "?cdntagname=orig&txSecret=e59abc&txTime=6AE401F7&q=4"
)
OTHER_URL = "https://pull-m2.wxlivecdn.com/trtc_1400419933/orig_999_orig.flv?txSecret=zzz"


# ---------------- 命令解析 ----------------

def test_extract_plain_dash_i():
    assert liveurl.extract_urls_from_cmdline(f"ffmpeg -i {LIVE_URL} -c copy out.flv") == [LIVE_URL]


def test_extract_quoted_dash_i():
    cmd = f'ffmpeg -i "{LIVE_URL}" -c copy -y out.flv'
    assert liveurl.extract_urls_from_cmdline(cmd) == [LIVE_URL]


def test_extract_multi_input_preserves_order():
    cmd = f'ffmpeg -i "{LIVE_URL}" -i "{OTHER_URL}" -map 0 -c copy'
    got = liveurl.extract_urls_from_cmdline(cmd)
    assert got == [LIVE_URL, OTHER_URL]


def test_extract_dedupes_same_url():
    cmd = f'ffmpeg -i "{LIVE_URL}" -i "{LIVE_URL}"'
    assert liveurl.extract_urls_from_cmdline(cmd) == [LIVE_URL]


def test_extract_ignores_output_path():
    """-y 后面的输出文件不能被当成输入。"""
    cmd = f'ffmpeg -i "{LIVE_URL}" -c copy -y "D:\\rec\\out.flv"'
    assert liveurl.extract_urls_from_cmdline(cmd) == [LIVE_URL]


def test_extract_ignores_non_url_tokens():
    assert liveurl.extract_urls_from_cmdline("ffmpeg -i input.wav -c copy out.mp3") == []
    assert liveurl.extract_urls_from_cmdline("") == []
    assert liveurl.extract_urls_from_cmdline("随便一段文字") == []


def test_extract_real_wxcd_command_line():
    """真实抓流工具的 ffmpeg 命令行形态（截取自实测）。"""
    cmd = (
        "ffmpeg -hide_banner -loglevel warning -nostats -progress pipe:1 "
        "-rw_timeout 15000000 -reconnect 1 -reconnect_streamed 1 "
        f'-i "{LIVE_URL}" '
        "-map 0:v:0? -map 0:a:0? -c copy -f segment -segment_time 600 "
        "-y D:\\rec\\segment-%06d.mkv"
    )
    got = liveurl.extract_urls_from_cmdline(cmd)
    assert got == [LIVE_URL]
    assert "txSecret=" in got[0] and "txTime=" in got[0]


# ---------------- 直播流判定 ----------------

def test_looks_like_live_url_accepts_wxlivecdn():
    assert liveurl.looks_like_live_url(LIVE_URL)


@pytest.mark.parametrize("url", [
    "http://pull-m1.wxlivecdn.com/trtc_1400/orig_1_orig.flv?txSecret=a",
    "https://finder.video.qq.com/251/20302/stodownload?encfilekey=x",
    "https://liveplay.myqcloud.com/live/a.flv",
])
def test_looks_like_live_url_known_hosts(url):
    assert liveurl.looks_like_live_url(url)


@pytest.mark.parametrize("text", [
    "",
    "D:/rec/a.wav",
    "not-a-url",
])
def test_looks_like_live_url_rejects_non_streams(text):
    assert not liveurl.looks_like_live_url(text)


# ---------------- LiveStream 属性 ----------------

def test_stream_key_strips_query():
    s = liveurl.LiveStream(url=LIVE_URL)
    assert "?" not in s.stream_key
    assert s.stream_key.endswith(".flv")


def test_stream_id_stable_across_signature_change():
    """签名变了但流路径没变 → 视为同一条直播（去重依据）。"""
    a = liveurl.LiveStream(url=LIVE_URL)
    rotated = LIVE_URL.replace("txSecret=e59abc", "txSecret=ROTATED")
    b = liveurl.LiveStream(url=rotated)
    assert a.stream_id == b.stream_id
    assert a.url != b.url


def test_stream_id_differs_for_different_streams():
    a = liveurl.LiveStream(url=LIVE_URL)
    b = liveurl.LiveStream(url=OTHER_URL)
    assert a.stream_id != b.stream_id


def test_has_signature_flag():
    assert liveurl.LiveStream(url=LIVE_URL).has_signature
    assert not liveurl.LiveStream(url="http://pull-m1.wxlivecdn.com/a.flv").has_signature


def test_title_hint_from_path():
    s = liveurl.LiveStream(url=LIVE_URL)
    assert s.title_hint == "orig_2042980977478071617_orig"


# ---------------- 从多进程命令行聚合 ----------------

def test_live_streams_from_cmdlines_dedupes_by_stream_key():
    rotated = LIVE_URL.replace("txSecret=e59abc", "txSecret=ANOTHER")
    cmdlines = [
        (100, f'ffmpeg -i "{LIVE_URL}" -c copy a.mkv'),
        (101, f'ffmpeg -i "{rotated}" -f hls b.m3u8'),  # 同一场直播的第二个进程
        (102, f'ffmpeg -i "{OTHER_URL}" -c copy c.mkv'),
        (103, "ffmpeg -i input.wav output.mp3"),        # 普通转码，不算
    ]
    got = liveurl.live_streams_from_cmdlines(cmdlines)
    assert len(got) == 2
    assert got[0].pid == 100  # 保留最先出现的那条
    assert {s.stream_id for s in got} == {
        liveurl.LiveStream(url=LIVE_URL).stream_id,
        liveurl.LiveStream(url=OTHER_URL).stream_id,
    }


def test_live_streams_from_cmdlines_empty():
    assert liveurl.live_streams_from_cmdlines([]) == []
    assert liveurl.live_streams_from_cmdlines([(1, "ffmpeg -version")]) == []


# ---------------- 本机代理服务 ----------------

def test_localagent_serves_live_list(monkeypatch):
    """代理端点应返回结构化的流列表，且不因底层扫描失败而崩。"""
    from wxlive import localagent

    fake = [liveurl.LiveStream(url=LIVE_URL, pid=4242, source="ffmpeg")]
    monkeypatch.setattr(liveurl, "find_live_streams", lambda: fake)

    httpd_holder = {}

    def run():
        from http.server import ThreadingHTTPServer

        srv = ThreadingHTTPServer(("127.0.0.1", 0), localagent._Handler)
        httpd_holder["srv"] = srv
        httpd_holder["port"] = srv.server_address[1]
        srv.serve_forever()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    for _ in range(50):
        if "port" in httpd_holder:
            break
        time.sleep(0.05)
    assert "port" in httpd_holder, "本地代理没起来"

    try:
        port = httpd_holder["port"]
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/live", timeout=5) as r:
            data = json.loads(r.read().decode("utf-8"))
        assert data["ok"] is True
        assert data["count"] == 1
        assert data["streams"][0]["pid"] == 4242
        assert data["streams"][0]["title_hint"] == "orig_2042980977478071617_orig"
        assert data["streams"][0]["has_signature"] is True

        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5) as r:
            assert json.loads(r.read().decode("utf-8"))["ok"] is True
    finally:
        httpd_holder["srv"].shutdown()


def test_localagent_reports_error_without_crashing(monkeypatch):
    from wxlive import localagent

    def boom():
        raise RuntimeError("扫描失败")

    monkeypatch.setattr(liveurl, "find_live_streams", boom)
    payload = localagent._payload()
    assert payload["ok"] is False
    assert "扫描失败" in payload["error"]
    assert payload["streams"] == []
