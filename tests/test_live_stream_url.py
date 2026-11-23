"""\u76F4\u64AD\u53D6\u5740 + \u5217\u8868\u53EF\u89C1\u6027 \u7684\u56DE\u5F52\u6D4B\u8BD5\uFF082026-11-23 \u6839\u56E0\u4FEE\u590D\uFF09\u3002

\u6BCF\u4E2A\u7528\u4F8B\u5BF9\u5E94\u4E00\u4E2A\u5B9E\u6D4B\u590D\u73B0\u8FC7\u7684\u771F\u5B9E\u7F3A\u9677\uFF0C\u522B\u518D\u8BA9\u5B83\u4EEC\u6084\u6084\u56DE\u6765\u3002

\u80CC\u666F\uFF1Adocs/\u76F4\u64AD\u53D6\u5740\u6839\u56E0-2026-11-23.md
1. \u5217\u8868\u91CC\u76F4\u64AD\u9879 url \u6052\u4E3A\u7A7A \u2192 \u65E7\u4EE3\u7801\u6574\u6761\u4E22\u5F03 \u2192 \u754C\u9762\u770B\u4E0D\u5230\u6B63\u5728\u64AD\u7684\u76F4\u64AD
2. \u76F4\u64AD\u5730\u5740\u5176\u5B9E\u4E00\u76F4\u5728\uFF08detail.resources / \u5DE5\u5177\u65E5\u5FD7\uFF09\uFF0C\u6CA1\u4EBA\u53BB\u53D6
3. \u65E5\u5FD7\u91CC\u5C01\u9762\u56FE\u57DF\u540D\u4E0E\u76F4\u64AD\u6D41\u540C\u57DF\uFF0C\u5BBD\u677E\u5224\u636E\u4F1A\u628A\u5C01\u9762\u5F53\u6D41\uFF08\u63A8 NAS \u5FC5\u5931\u8D25\uFF09
4. "\u65E5\u5FD7\u91CC\u6700\u65B0\u4E00\u6761"\u515C\u5E95\u4F1A\u4E32\u53F0\uFF1A\u628A B \u4E3B\u64AD\u7684\u6D41\u8BB0\u6210 A \u7684\u6807\u9898
5. \u9636\u68AF\u5F0F WAV \u589E\u957F\u4F1A\u88AB\u8BEF\u62A5\u6210\u65AD\u6D41
"""

from __future__ import annotations

import pytest

from wxlive import fetch as F


# ---------------- 1. \u76F4\u64AD\u9879\u4E0D\u80FD\u518D\u88AB\u4E22\u5F03 ----------------

def test_parse_item_keeps_live_with_empty_url():
    """\u76F4\u64AD\u9879 url \u6052\u4E3A\u7A7A \u2014\u2014 \u8FD9\u662F\u6B63\u5E38\u5F62\u6001\uFF0C\u4E0D\u662F\u810F\u6570\u636E\u3002"""
    item = F._parse_item({
        "external_id": "2079009824565503324",
        "title": "\u538C\u5B66\u7684\u4E94\u5927\u7C7B\u578B",
        "url": "",
        "type": "live",
    })
    assert item is not None, "\u76F4\u64AD\u9879\u88AB\u4E22\u5F03\u4E86\uFF08\u5386\u53F2 bug #1\uFF09"
    assert item.is_live is True
    assert item.needs_stream_url is True
    assert item.live_id == "2079009824565503324"


def test_parse_item_still_drops_entry_without_id():
    """\u6CA1\u6709 id \u7684\u624D\u662F\u771F\u810F\u6570\u636E\uFF0C\u4ECD\u7136\u4E22\u5F03\u3002"""
    assert F._parse_item({"title": "x", "url": "http://a/b.flv"}) is None


def test_parse_item_video_is_not_live():
    item = F._parse_item({
        "external_id": "14529997747777707195",
        "title": "\u56DE\u653E",
        "url": "https://finder.video.qq.com/x/y?encfilekey=abc",
        "type": "video",
    })
    assert item is not None
    assert item.is_live is False
    assert item.needs_stream_url is False


def test_parse_item_live_id_is_external_id():
    """live_id 必须取 external_id。

    实测（2026-11-23）：直播项的 external_id 与流地址里 orig_<id>_ 那段
    **完全相同**。这个值用来精确匹配候选地址，取错或取空都会导致
    串台或取不到——所以不能从 url 里反推（直播项 url 本来就是空的）。
    """
    item = F._parse_item({
        "external_id": "2079009824565503324",
        "title": "t",
        "url": "",
        "type": "live",
    })
    assert item.live_id == "2079009824565503324"


# ---------------- 2. \u5C01\u9762\u56FE\u4E0D\u80FD\u88AB\u5F53\u6210\u76F4\u64AD\u6D41 ----------------

@pytest.mark.parametrize("url", [
    "https://finder.video.qq.com/292/20350/stodownload?encfilekey=oibeqy&token=x",
    "https://finder.video.qq.com/251/20304/stodownload?m=1a3e&filekey=3034",
])
def test_cover_image_is_not_live_stream(url):
    """\u5B9E\u6D4B\uFF1A\u65E5\u5FD7\u91CC 39 \u6761\u5019\u9009\u6709\u4E00\u534A\u662F\u5C01\u9762\u56FE\uFF0C\u5168\u88AB\u5F53\u6D41\u6293\u4E86\u51FA\u6765\u3002"""
    assert F._looks_like_live(url) is False


@pytest.mark.parametrize("url", [
    "http://voipfinderliveplaythird.wxqcloud.qq.com/trtc_1400419933/orig_2079009824565503324_orig.flv?cdntagname=orig",
    "http://pull-m1.wxlivecdn.com/trtc_1400419933/orig_2042980977478071617_orig.flv?txSecret=x",
    "https://pull-m1.wxlivecdn.com/trtc_1400439699/live_2079009830844233127_wjf521.flv",
])
def test_real_live_stream_is_recognised(url):
    assert F._looks_like_live(url) is True


def test_non_http_is_not_live():
    assert F._looks_like_live("/app/data/x.flv") is False
    assert F._looks_like_live("") is False


# ---------------- 3. \u65E5\u5FD7\u9012\u5F52\u63D0\u53D6 ----------------

def test_collect_live_urls_walks_nested_json():
    """liveCdnUrl \u53EF\u80FD\u5728 feed.live.liveSdkInfo\uFF0C\u4E5F\u53EF\u80FD\u5728 profile.liveInfo\u3002"""
    obj = {
        "feed": {
            "live": {
                "liveSdkInfo": {
                    "liveCdnUrl": "http://pull-m1.wxlivecdn.com/trtc_1/orig_111_orig.flv?a=1"
                },
                "coverInfo": {
                    "coverUrl": "https://finder.video.qq.com/292/stodownload?encfilekey=x"
                },
            }
        }
    }
    out = []
    F._collect_live_urls(obj, out)
    assert len(out) == 1
    assert "orig_111_orig.flv" in out[0]


def test_collect_live_urls_dedupes_by_full_url():
    u = "http://pull-m1.wxlivecdn.com/trtc_1/orig_111_orig.flv?a=1"
    out = []
    F._collect_live_urls({"a": u, "b": [u]}, out)
    assert out == [u]


# ---------------- 4. \u53D6\u5740\uFF1A\u53EA\u8BA4\u81EA\u5DF1\u7684 live_id\uFF0C\u7EDD\u4E0D\u4E32\u53F0 ----------------

class _Stub:
    """\u6700\u5C0F ContentItem \u66FF\u8EAB\uFF08\u907F\u514D\u6BCF\u6B21\u6784\u9020\u5B8C\u6574 dataclass\uFF09\u3002"""

    def __init__(self, cid, api_id=None, live_id=None):
        self.content_id = cid
        self.api_id = api_id or ("wxchannels:" + cid)
        self.live_id = live_id if live_id is not None else cid
        self.url = ""
        self.is_live = True


def test_candidates_never_borrow_another_streamers_url(monkeypatch):
    """\u5B9E\u6D4B\u4E8B\u6545\uFF1A\u4E24\u4E2A\u4E3B\u64AD\u540C\u65F6\u5728\u64AD\u65F6\uFF0C\u65E7\u4EE3\u7801\u53D6"\u6700\u65B0\u90A3\u6761"\u2192 \u5F55\u5230\u522B\u4EBA\u7684\u76F4\u64AD\u3002"""
    monkeypatch.setattr(F, "_api_get", lambda api, path, timeout=20: (
        {"code": 500, "msg": "not found"} if "detail" in path else {
            "code": 0,
            "data": {"entries": [
                # \u6392\u5E8F\u7B2C\u4E00 = \u6700\u65B0\uFF0C\u5C5E\u4E8E**\u53E6\u4E00\u4E2A**\u4E3B\u64AD
                {"raw": '{"feed":{"live":{"liveSdkInfo":'
                        '{"liveCdnUrl":"http://pull-m1.wxlivecdn.com/trtc_1400419933/orig_999_orig.flv?txSecret=a"}}}}'},
                # \u81EA\u5DF1\u90A3\u6761\uFF0C\u8F83\u65E7
                {"raw": '{"feed":{"live":{"liveSdkInfo":'
                        '{"liveCdnUrl":"http://pull-m1.wxlivecdn.com/trtc_1400419933/orig_111_orig.flv?txSecret=b"}}}}'},
            ]}
        }
    ))

    cands = F.list_stream_candidates(_Stub("111"))
    urls = [u for u, _ in cands]
    assert urls, "\u81EA\u5DF1\u7684\u5730\u5740\u6CA1\u88AB\u627E\u5230"
    assert all("orig_111_orig.flv" in u for u in urls), \
        "\u4E32\u53F0\u4E86\uFF1A\u62FF\u5230\u4E86\u522B\u7684 live_id \u7684\u5730\u5740"
    assert not any("orig_999_orig.flv" in u for u in urls)
