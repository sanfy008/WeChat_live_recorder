"""\u672C\u5730\u8BB0\u5F55\uFF1A\u9690\u85CF\u3001\u5DF2\u4E0B\u8F7D\u3001\u53BB\u91CD\u3002

**\u4E3A\u4EC0\u4E48\u9700\u8981\u8FD9\u4E2A\u6A21\u5757**
    \u6293\u6D41\u5DE5\u5177\uFF08tools/wxcd\uFF0C\u5373 ltaoo/wx_channels_download\uFF09\u7684\u5185\u5BB9 API \u53EA\u6709
    list / detail / relations \u2014\u2014 \u6211\u6838\u5BF9\u8FC7\u5B83 internal/api/routes.go \u7684\u5168\u91CF\u8DEF\u7531\uFF0C
    \u6CA1\u6709 delete / clear / remove \u4E4B\u7C7B\u7684\u7AEF\u70B9\u3002\u5DE5\u5177\u81EA\u5DF1\u7684 SQLite \u91CC\u90A3\u4E9B\u6355\u83B7\u8BB0\u5F55
    \u53EA\u80FD\u7531\u5DE5\u5177\u81EA\u5DF1\u6E05\u7406\uFF08\u5B83\u754C\u9762\u4E0A\u4E5F\u6CA1\u6709\uFF09\u3002

    \u6240\u4EE5\u300C\u5220\u9664 / \u6E05\u7A7A\u5217\u8868\u300D\u5728\u672C\u9879\u76EE\u91CC\u53EA\u80FD\u662F**\u672C\u4FA7\u89C6\u56FE\u64CD\u4F5C**\uFF1A
    \u628A\u67D0\u4E9B content_id \u8BB0\u6210"\u5DF2\u9690\u85CF"\uFF0C\u5237\u65B0\u5217\u8868\u65F6\u8FC7\u6EE4\u6389\u3002
    \u8FD9\u6837\u65E2\u6EE1\u8DB3\u4E86\u7528\u6237"\u5217\u8868\u592A\u4E71\u60F3\u6E05\u6389"\u7684\u771F\u5B9E\u8BC9\u6C42\uFF0C\u4E5F\u4E0D\u4F1A\u53BB\u52A8\u5DE5\u5177\u7684\u6570\u636E\u5E93
    \uFF08\u52A8\u5B83\u7684 data.db \u98CE\u9669\u592A\u5927\uFF1A\u5B83\u662F WAL \u6A21\u5F0F\u4E14\u5728\u8FD0\u884C\u4E2D\uFF0C\u5199\u574F\u4E86\u4F1A\u4E22\u5168\u90E8\u6355\u83B7\u8BB0\u5F55\uFF09\u3002

**\u5B58\u54EA\u91CC**
    config/fetch_state.json \u2014\u2014 \u4E0E config/user.json \u540C\u7EA7\u3002
    \u7ED3\u6784\uFF1A
        {
          "hidden":    {"<content_id>": {"at": 1699999999000, "title": "\u2026"}},
          "downloaded": {"<content_id>": {"at": \u2026, "title": \u2026, "path": "\u2026",
                                          "duration": 1234.5, "size": 123456}},
          "dismissed_hint": true
        }

    \u8BFB\u5199\u90FD\u8981\u5BB9\u9519\uFF1A\u8FD9\u4E2A\u6587\u4EF6\u662F\u672C\u7A0B\u5E8F\u7684\u8F85\u52A9\u72B6\u6001\uFF0C\u635F\u574F\u4E86\u4E5F\u7EDD\u4E0D\u80FD\u62D6\u57AE\u4E3B\u6D41\u7A0B
    \uFF08\u4E22\u4E86\u6700\u591A\u662F\u5217\u8868\u91CD\u65B0\u53D8\u957F\uFF0C\u4E0D\u5F71\u54CD\u5F55\u97F3/\u8F6C\u5199\uFF09\u3002
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = ROOT / "config" / "fetch_state.json"

# \u4FDD\u7559\u591A\u5C11\u4E2A\u5DF2\u9690\u85CF/\u5DF2\u4E0B\u8F7D\u6761\u76EE\u3002\u592A\u591A\u4E86\u6587\u4EF6\u4F1A\u4E00\u76F4\u6DA8\uFF0C\u4F46\u8BB0\u5F55\u672C\u8EAB\u5F88\u5C0F
# \uFF08\u6BCF\u6761\u7EA6 100 \u5B57\u8282\uFF09\uFF0C\u4E0A\u9650 2000 \u6761 \u2248 200KB\uFF0C\u591F\u7528\u4E14\u4E0D\u4F1A\u5931\u63A7\u3002
MAX_ENTRIES = 2000


def _empty() -> dict:
    return {"hidden": {}, "downloaded": {}, "dismissed_hint": False}


def load_state() -> dict:
    """\u8BFB\u72B6\u6001\uFF1B\u6587\u4EF6\u7F3A\u5931/\u635F\u574F/\u7ED3\u6784\u4E0D\u5BF9\u90FD\u9000\u56DE\u7A7A\u72B6\u6001\uFF0C\u7EDD\u4E0D\u629B\u5F02\u5E38\u3002"""
    state = _empty()
    try:
        if not STATE_PATH.exists():
            return state
        raw = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return state
    if not isinstance(raw, dict):
        return state
    for key in ("hidden", "downloaded"):
        val = raw.get(key)
        if isinstance(val, dict):
            state[key] = {str(k): v for k, v in val.items()
                          if isinstance(v, dict)}
    state["dismissed_hint"] = bool(raw.get("dismissed_hint"))
    return state


def save_state(state: dict) -> None:
    """\u539F\u5B50\u5199\uFF1A\u5148\u5199 .tmp \u518D replace\u3002

    \u76F4\u63A5\u8986\u76D6\u5199\u6709\u98CE\u9669 \u2014\u2014 \u5199\u5230\u4E00\u534A\u5D29\u6E83\u4F1A\u7559\u4E0B\u534A\u4E2A JSON\uFF0C
    \u4E0B\u6B21\u8BFB\u56DE\u6765\u5C31\u662F\u7A7A\u7684\uFF08\u7528\u6237\u7684\u9690\u85CF\u5217\u8868\u5168\u6CA1\u4E86\uFF09\u3002
    """
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        for key in ("hidden", "downloaded"):
            bucket = state.get(key)
            if isinstance(bucket, dict) and len(bucket) > MAX_ENTRIES:
                # \u6309\u65F6\u95F4\u5012\u5E8F\u4FDD\u7559\u6700\u65B0\u7684 MAX_ENTRIES \u6761
                ordered = sorted(
                    bucket.items(),
                    key=lambda kv: float((kv[1] or {}).get("at") or 0),
                    reverse=True)[:MAX_ENTRIES]
                state[key] = dict(ordered)
        tmp = STATE_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, STATE_PATH)
    except Exception:  # noqa: BLE001
        # \u72B6\u6001\u6587\u4EF6\u5199\u5931\u8D25\u4E0D\u8BE5\u5F71\u54CD\u4E1A\u52A1\uFF08\u5217\u8868\u91CD\u65B0\u53D8\u957F\u800C\u5DF2\uFF09\uFF0C\u9759\u9ED8\u653E\u5F03
        pass


# ---------- \u9690\u85CF ----------

def hide(state: dict, content_ids, titles: dict | None = None) -> int:
    """\u628A\u82E5\u5E72 content_id \u8BB0\u4E3A\u9690\u85CF\uFF0C\u8FD4\u56DE\u672C\u6B21\u5B9E\u9645\u65B0\u589E\u7684\u6761\u6570\u3002

    \u91CD\u590D\u9690\u85CF\u540C\u4E00\u6761\u4E0D\u7B97\u65B0\u589E\uFF08\u5E42\u7B49\uFF09\uFF0C\u8FD9\u6837"\u5220\u9664\u9009\u4E2D"\u8FDE\u70B9\u4E24\u6B21\u4E0D\u4F1A\u91CD\u590D\u8BA1\u6570\u3002
    """
    titles = titles or {}
    bucket = state.setdefault("hidden", {})
    now = int(time.time() * 1000)
    added = 0
    for cid in content_ids:
        cid = str(cid)
        if not cid or cid in bucket:
            continue
        bucket[cid] = {"at": now, "title": str(titles.get(cid) or "")}
        added += 1
    return added


def unhide_all(state: dict) -> int:
    """\u6062\u590D\u5168\u90E8\u9690\u85CF\u6761\u76EE\uFF0C\u8FD4\u56DE\u6062\u590D\u7684\u6761\u6570\u3002"""
    bucket = state.get("hidden") or {}
    n = len(bucket)
    state["hidden"] = {}
    return n


def hidden_ids(state: dict) -> set[str]:
    return {str(k) for k in (state.get("hidden") or {})}


def hidden_count(state: dict) -> int:
    return len(state.get("hidden") or {})


# ---------- \u5DF2\u4E0B\u8F7D ----------

def mark_downloaded(state: dict, content_id: str, *, title: str = "",
                    path: str = "", duration: float = 0.0,
                    size: int = 0) -> None:
    bucket = state.setdefault("downloaded", {})
    bucket[str(content_id)] = {
        "at": int(time.time() * 1000),
        "title": str(title or ""),
        "path": str(path or ""),
        "duration": float(duration or 0.0),
        "size": int(size or 0),
    }


def downloaded_ids(state: dict) -> set[str]:
    return {str(k) for k in (state.get("downloaded") or {})}


def downloaded_info(state: dict, content_id: str) -> dict | None:
    rec = (state.get("downloaded") or {}).get(str(content_id))
    return rec if isinstance(rec, dict) else None


def clear_downloaded(state: dict) -> int:
    """\u6E05\u7A7A\u5DF2\u4E0B\u8F7D\u6807\u8BB0\uFF08\u53EA\u6E05\u6807\u8BB0\uFF0C\u4E0D\u5220\u78C1\u76D8\u4E0A\u7684\u97F3\u9891\uFF09\u3002"""
    n = len(state.get("downloaded") or {})
    state["downloaded"] = {}
    return n


# ---------- \u5F15\u5BFC\u63D0\u793A ----------

def hint_dismissed(state: dict) -> bool:
    return bool(state.get("dismissed_hint"))


def dismiss_hint(state: dict) -> None:
    state["dismissed_hint"] = True
