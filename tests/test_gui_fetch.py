"""GUI 视频号下载页签的测试。

注意：必须用真实 mainloop。之前踩过坑——用 self.after 做异步初始化时，
不进 mainloop 会抛 "main thread is not in main loop"，导致误判。
所以这里用 update() + after 轮询的方式在真事件循环里验证。

第二个坑：**测试绝不能碰真实的抓流工具**。GUI 的刷新走
`fetch.list_page`，如果桩打错（比如还在打 list_contents），
请求会真的发到 127.0.0.1:2022，拿到真实数据，测试变成"看运气"。
所以下面每个用例都显式替换 list_page。
"""

import tkinter as tk

import pytest


@pytest.fixture
def app(tmp_path, monkeypatch):
    """构造 App 并进入真实事件循环一次，用完销毁。

    顺手把本地状态文件指到 tmp_path，避免测试污染真实的
    config/fetch_state.json（否则跑一次测试就把用户的隐藏列表改了）。
    """
    import wxlive.fetchstate as S

    monkeypatch.setattr(S, "STATE_PATH", tmp_path / "fetch_state.json")

    # 关键：把所有 messagebox 换成 noop。
    # 真实弹窗是**模态**的，测试里没人点，_pump 会一直等 → 整个用例卡死。
    # 实测未替换时单跑 test_fetch_clear_search_resets_page 就卡满 60 秒。
    # 需要断言弹窗内容的用例再自己 monkeypatch 覆盖。
    import wxlive.gui_fetch as GF
    for fn in ("showinfo", "showwarning", "showerror"):
        monkeypatch.setattr(GF.messagebox, fn, lambda *a, **kw: None)
    monkeypatch.setattr(GF.messagebox, "askyesno", lambda *a, **kw: True)
    monkeypatch.setattr(GF.messagebox, "askyesnocancel", lambda *a, **kw: True)

    # 刷新列表现在还会调 fetch.discover_live_streams / discover_feed_videos。
    # 这里必须一并桩掉，否则每个 GUI 用例都会真的去连 127.0.0.1:2022，
    # 把真实直播/回放混进断言里 —— 正是本文件开头警告过的那个坑。
    import wxlive.fetch as F
    monkeypatch.setattr(F, "discover_live_streams", lambda *a, **kw: [])
    monkeypatch.setattr(F, "discover_feed_videos", lambda *a, **kw: [])

    from wxlive.gui import App

    a = App()
    a.withdraw()          # 不弹窗
    a._fetch_state = S._empty()   # 每个用例从干净状态开始
    a.update()            # 真正跑一遍事件循环
    yield a
    # 必须先取消 after 定时器。否则 Tk 事件循环永远有事可做，
    # destroy() 后进程不退出 —— 实测 30 个用例跑完卡满 250 秒。
    try:
        a.fetch_shutdown()
    except Exception:  # noqa: BLE001
        pass
    try:
        a.destroy()
    except tk.TclError:
        pass


def _pump(app, timeout: float = 5.0) -> None:
    """推进事件循环，直到后台线程结束且 UI 队列被排空。

    _fetch_refresh 起后台线程 → 线程把动作塞进 _ui_queue →
    主线程 after 定时取出执行。测试里必须真的跑事件循环。
    """
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        app.update()
        q = getattr(app, "_ui_queue", None)
        w = getattr(app, "worker", None)
        busy_thread = w is not None and w.is_alive()
        pending = q is not None and not q.empty()
        if not busy_thread and not pending:
            for _ in range(5):
                app.update()
                time.sleep(0.01)
            return
        time.sleep(0.01)


def _fake_page(items, total=None, page_size=None):
    """造一个 ContentPage，省得每个用例都写全参数。"""
    from wxlive.fetch import ContentPage

    return ContentPage(items=list(items),
                       total=total if total is not None else len(items),
                       page=1,
                       page_size=page_size if page_size is not None else len(items))


@pytest.fixture
def stub_list(monkeypatch):
    """把 fetch.list_page 换成可编程的桩，记录被调用的参数。"""
    import wxlive.fetch as F

    calls = []
    # page_size 默认给 20：为 0 时 _fetch_refresh_done 算页数会退回 len(items)=0，
    # 页数算成 1/1，翻页用例的期望就对不上。
    holder = {"page": _fake_page([], page_size=20)}

    def fake(api=None, keyword="", limit=None, offset=None, **kw):
        calls.append({"api": api, "keyword": keyword,
                      "limit": limit, "offset": offset})
        return holder["page"]

    monkeypatch.setattr(F, "list_page", fake)
    # 发现直播/回放默认空；需要验证"日志发现"渲染的用例自己覆盖 holder["lives"] / holder["videos"]
    monkeypatch.setattr(F, "discover_live_streams",
                        lambda *a, **kw: holder.get("lives") or [])
    monkeypatch.setattr(F, "discover_feed_videos",
                        lambda *a, **kw: holder.get("videos") or [])
    return holder, calls


def test_fetch_tab_exists(app):
    """页签应被创建并挂在 Notebook 里。"""
    assert hasattr(app, "tab_fetch")
    assert app.tab_fetch.winfo_exists()


def test_fetch_widgets_created(app):
    """关键控件都在，且属性名与 mixin 约定一致。"""
    for attr in ("fetch_tree", "fetch_start_btn", "fetch_refresh_btn",
                 "fetch_progress", "fetch_api_var", "fetch_hint_var",
                 "fetch_keep_video_var", "fetch_auto_trans_var"):
        assert hasattr(app, attr), f"缺少控件 {attr}"


def test_fetch_state_ready_before_any_callback(app):
    """构建下载页时，状态变量必须先于任何回调就绪。

    这里踩过一个真 bug（2026-11-16 修黑框闪烁时撞出来的）：
    ``_build_fetch_tab`` 里的"启动就探一次代理"会走 ``_ui()`` →
    ``self._ui_queue.put()``，但 ``_ui_queue`` 原先在本方法**末尾**才创建。
    于是启动时后台线程抛 AttributeError，界面上只表现为
    状态条永远停在「读取中…」—— 看起来像"读得慢"，实际是崩了。

    所以断言两件事：属性存在（不抛），以及启动探测真的落到了界面上。
    """
    for attr in ("_ui_queue", "_fetch_cancel", "_fetch_items",
                 "_fetch_proxy_after"):
        assert hasattr(app, attr), f"启动时缺少 {attr}，回调会抛 AttributeError"

    _pump(app)
    text = app.fetch_proxy_var.get()
    assert text and "读取中" not in text, (
        f"状态条还停在初始值 {text!r} —— 启动时的代理探测没有完成"
    )


    # 本轮新增的控件：搜索 / 翻页 / 删除恢复 / 解析开关 / 中止
    for attr in ("fetch_search_var", "fetch_show_hidden_var", "fetch_only_new_var",
                 "fetch_page_size_var", "fetch_page_var",
                 "fetch_prev_btn", "fetch_next_btn",
                 "fetch_restore_btn", "fetch_status_btn", "fetch_cancel_btn",
                 "fetch_probe_first_var", "fetch_prog_var"):
        assert hasattr(app, attr), f"缺少控件 {attr}"


def test_fetch_tree_columns(app):
    """列表四列：标题 / 作者 / 发布时间 / 状态。"""
    cols = app.fetch_tree["columns"]
    assert tuple(cols) == ("title", "author", "publish", "state")


def test_fetch_default_options(app):
    """默认只留音频、不自动转写、下载前先解析。"""
    assert app.fetch_keep_video_var.get() is False
    assert app.fetch_auto_trans_var.get() is False
    assert app.fetch_probe_first_var.get() is True


def test_fetch_busy_flag_starts_false(app):
    assert app._fetch_busy is False


def test_fetch_refresh_without_tool_does_not_crash(app, monkeypatch):
    """工具没运行时刷新应优雅提示，不抛出、不阻塞。

    坑：不能用 monkeypatch.setattr(F, "list_contents", boom) 后再抛
    F.FetchError——如果 FetchError 也被替换过，worker 里的
    `except F.FetchError` 捕获不到同一个类，异常会逃逸到线程顶层。
    这里只替换函数，异常类保持原样。
    """
    import wxlive.fetch as F
    import wxlive.gui_fetch as GF

    real_err = F.FetchError          # 先抓真类，避免被后续替换影响

    def boom(*a, **kw):
        raise real_err("连接失败测试桩")

    monkeypatch.setattr(F, "list_page", boom)
    monkeypatch.setattr(GF.messagebox, "showwarning", lambda *a, **kw: None)

    app._fetch_refresh()
    _pump(app)

    assert app.fetch_hint_var.get() == "读取失败"


def test_fetch_refresh_populates_tree(app, stub_list):
    """拿到内容后应填进列表，并能按 id 反查。"""
    from wxlive.fetch import ContentItem

    holder, calls = stub_list
    holder["page"] = _fake_page([
        ContentItem(content_id="111", title="测试视频A", url="http://x/a",
                    author="作者甲", publish_time=1700000000),
        ContentItem(content_id="222", title="测试视频B", url="http://x/b"),
    ])

    app._fetch_refresh()
    _pump(app)

    rows = app.fetch_tree.get_children()
    assert len(rows) == 2
    titles = [app.fetch_tree.item(r, "values")[0] for r in rows]
    assert titles == ["测试视频A", "测试视频B"]
    assert "111" in app._fetch_items and "222" in app._fetch_items
    # 作者 / 发布时间进了列；没有的用破折号占位
    first = app.fetch_tree.item(rows[0], "values")
    assert first[1] == "作者甲"
    assert first[2] != "—"
    second = app.fetch_tree.item(rows[1], "values")
    assert second[1] == "—" and second[2] == "—"
    assert calls and calls[0]["keyword"] == ""


def test_fetch_empty_list_shows_hint(app, stub_list, monkeypatch):
    """没有内容时给出提示而不是空白无反应。"""
    import wxlive.gui_fetch as GF

    holder, _ = stub_list
    holder["page"] = _fake_page([])
    monkeypatch.setattr(GF.messagebox, "showinfo", lambda *a, **kw: None)

    app._fetch_refresh()
    _pump(app)

    assert app.fetch_tree.get_children() == ()
    assert "共 0 条" in app.fetch_hint_var.get()
    assert "还没有捕获" not in app.fetch_hint_var.get()


def test_fetch_set_state_updates_row(app, stub_list):
    """状态列能单独更新，不影响其他列。"""
    from wxlive.fetch import ContentItem

    holder, _ = stub_list
    holder["page"] = _fake_page([
        ContentItem(content_id="999", title="X", url="http://x", author="Y"),
    ])
    app._fetch_refresh()
    _pump(app)

    row = app.fetch_tree.get_children()[0]
    app._fetch_set_state("999", "完成 80 分钟")
    vals = app.fetch_tree.item(row, "values")
    assert vals[0] == "X" and vals[1] == "Y" and vals[3] == "完成 80 分钟"


def test_fetch_set_state_bad_row_is_safe(app):
    """未知 cid 更新状态不应抛异常。"""
    app._fetch_set_state("nonexistent-cid", "x")  # 不应崩


# ---------- 搜索 / 翻页 ----------

def test_fetch_search_passed_to_server(app, stub_list):
    """搜索词要交给服务端过滤（工具支持 keyword），不是前端过滤。"""
    holder, calls = stub_list
    app.fetch_search_var.set("越秀")
    app._fetch_refresh()
    _pump(app)

    assert calls, "没有发出请求"
    assert calls[-1]["keyword"] == "越秀"


def test_fetch_paging_offset_follows_page(app, stub_list):
    """翻页靠 offset 推进，且 offset = (page-1) * page_size。"""
    from wxlive.fetch import ContentItem

    holder, calls = stub_list
    holder["page"] = _fake_page(
        [ContentItem(content_id="1", title="T", url="http://x")],
        total=250, page_size=100)

    app._fetch_page = 2
    app._fetch_refresh()
    _pump(app)

    assert calls[-1]["limit"] == 100
    assert calls[-1]["offset"] == 100
    assert "第 2 / 3 页" in app.fetch_page_var.get()


def test_fetch_page_nav_buttons_state(app, stub_list):
    """首页时上一页灰掉；最后一页时下一页灰掉。"""
    from wxlive.fetch import ContentItem

    holder, _ = stub_list
    holder["page"] = _fake_page(
        [ContentItem(content_id="1", title="T", url="http://x")],
        total=100, page_size=100)

    app._fetch_page = 1
    app._fetch_refresh()
    _pump(app)
    assert str(app.fetch_prev_btn["state"]) == "disabled"
    assert str(app.fetch_next_btn["state"]) == "disabled"   # 1/1 页


def test_fetch_next_page_increments(app, stub_list):
    """下一页按钮把页码 +1 后刷新。"""
    holder, _ = stub_list
    app._fetch_page = 1
    app._fetch_next_page()
    _pump(app)
    assert app._fetch_page == 2


def test_fetch_prev_page_never_below_one(app, stub_list):
    """首页点上一页不应变成第 0 页。"""
    holder, _ = stub_list
    app._fetch_page = 1
    app._fetch_prev_page()
    _pump(app)
    assert app._fetch_page == 1


def test_fetch_clear_search_resets_page(app, stub_list):
    """清空搜索时页码也要回到第 1 页。"""
    holder, _ = stub_list
    app.fetch_search_var.set("关键词")
    app._fetch_page = 3
    app._fetch_clear_search()
    _pump(app)
    assert app.fetch_search_var.get() == ""
    assert app._fetch_page == 1


# ---------- 删除 / 清空 / 恢复 ----------

def test_fetch_hide_filters_row_out(app, stub_list, monkeypatch):
    """删除选中 = 本侧隐藏，刷新后不再出现在列表里。"""
    import wxlive.gui_fetch as GF
    from wxlive.fetch import ContentItem

    holder, _ = stub_list
    holder["page"] = _fake_page([
        ContentItem(content_id="aaa", title="甲", url="http://x/a"),
        ContentItem(content_id="bbb", title="乙", url="http://x/b"),
    ])
    monkeypatch.setattr(GF.messagebox, "askyesno", lambda *a, **kw: True)
    monkeypatch.setattr(GF.messagebox, "showinfo", lambda *a, **kw: None)

    app._fetch_refresh()
    _pump(app)
    rows = app.fetch_tree.get_children()
    assert len(rows) == 2

    # 选中第一行然后删除
    app.fetch_tree.selection_set(rows[0])
    app._fetch_delete_selected()
    _pump(app)

    left = [app.fetch_tree.item(r, "values")[0] for r in app.fetch_tree.get_children()]
    assert left == ["乙"]
    assert "aaa" in app._fetch_state["hidden"]


def test_fetch_hidden_can_be_shown(app, stub_list, monkeypatch):
    """勾「显示已删除的」后隐藏条目应重新出现，并标注状态。"""
    import wxlive.gui_fetch as GF
    import wxlive.fetchstate as S
    from wxlive.fetch import ContentItem

    holder, _ = stub_list
    holder["page"] = _fake_page([
        ContentItem(content_id="aaa", title="甲", url="http://x/a"),
        ContentItem(content_id="bbb", title="乙", url="http://x/b"),
    ])
    monkeypatch.setattr(GF.messagebox, "showinfo", lambda *a, **kw: None)
    S.hide(app._fetch_state, ["aaa"], {"aaa": "甲"})

    app._fetch_refresh()
    _pump(app)
    assert len(app.fetch_tree.get_children()) == 1

    app.fetch_show_hidden_var.set(True)
    app._fetch_refresh()
    _pump(app)
    rows = app.fetch_tree.get_children()
    assert len(rows) == 2
    states = [app.fetch_tree.item(r, "values")[3] for r in rows]
    assert "已删除（隐藏）" in states


def test_fetch_restore_hidden(app, stub_list, monkeypatch):
    """恢复被删除的：hidden 清空，条目回到列表。"""
    import wxlive.gui_fetch as GF
    import wxlive.fetchstate as S
    from wxlive.fetch import ContentItem

    holder, _ = stub_list
    holder["page"] = _fake_page([
        ContentItem(content_id="aaa", title="甲", url="http://x/a")])
    monkeypatch.setattr(GF.messagebox, "askyesno", lambda *a, **kw: True)
    monkeypatch.setattr(GF.messagebox, "showinfo", lambda *a, **kw: None)
    S.hide(app._fetch_state, ["aaa"], {"aaa": "甲"})

    app._fetch_restore_hidden()
    _pump(app)

    assert S.hidden_count(app._fetch_state) == 0
    assert len(app.fetch_tree.get_children()) == 1


def test_fetch_restore_when_nothing_hidden(app, monkeypatch):
    """没有隐藏条目时点恢复应直接提示，不弹确认框。"""
    import wxlive.gui_fetch as GF

    asked = {"n": 0}

    def fake_yes(*a, **kw):
        asked["n"] += 1
        return True

    monkeypatch.setattr(GF.messagebox, "askyesno", fake_yes)
    monkeypatch.setattr(GF.messagebox, "showinfo", lambda *a, **kw: None)

    app._fetch_restore_hidden()
    assert asked["n"] == 0


def test_fetch_clear_all_hides_everything(app, stub_list, monkeypatch):
    """清空列表选「是」= 全部隐藏。"""
    import wxlive.gui_fetch as GF
    from wxlive.fetch import ContentItem

    holder, _ = stub_list
    holder["page"] = _fake_page([
        ContentItem(content_id="aaa", title="甲", url="http://x/a"),
        ContentItem(content_id="bbb", title="乙", url="http://x/b"),
    ])
    monkeypatch.setattr(GF.messagebox, "askyesnocancel",
                        lambda *a, **kw: True)   # True = 隐藏
    app._fetch_refresh()
    _pump(app)

    app._fetch_clear_all()
    _pump(app)

    assert app.fetch_tree.get_children() == ()
    assert app._fetch_state["hidden"]


def test_fetch_clear_all_cancel_does_nothing(app, stub_list, monkeypatch):
    """清空列表选「取消」不应改动任何状态。"""
    import wxlive.gui_fetch as GF
    from wxlive.fetch import ContentItem

    holder, _ = stub_list
    holder["page"] = _fake_page([
        ContentItem(content_id="aaa", title="甲", url="http://x/a")])
    monkeypatch.setattr(GF.messagebox, "askyesnocancel",
                        lambda *a, **kw: None)   # None = 取消
    app._fetch_refresh()
    _pump(app)

    app._fetch_clear_all()
    _pump(app)

    assert "aaa" not in app._fetch_state["hidden"]
    assert len(app.fetch_tree.get_children()) == 1


def test_fetch_downloaded_marked_and_shown(app, stub_list, monkeypatch):
    """下载过的条目标「已下载」，且「只看未下载的」能过滤掉它。"""
    import wxlive.fetchstate as S
    from wxlive.fetch import ContentItem

    holder, _ = stub_list
    holder["page"] = _fake_page([
        ContentItem(content_id="aaa", title="甲", url="http://x/a"),
        ContentItem(content_id="bbb", title="乙", url="http://x/b"),
    ])
    S.mark_downloaded(app._fetch_state, "aaa", title="甲",
                      path="a.wav", duration=600.0, size=1000)

    app._fetch_refresh()
    _pump(app)
    states = {app.fetch_tree.item(r, "values")[0]:
              app.fetch_tree.item(r, "values")[3]
              for r in app.fetch_tree.get_children()}
    assert states["甲"] == "已下载 10 分钟"
    assert states["乙"] == "待下载"

    app.fetch_only_new_var.set(True)
    app._fetch_refresh()
    _pump(app)
    left = [app.fetch_tree.item(r, "values")[0] for r in app.fetch_tree.get_children()]
    assert left == ["乙"]


def test_fetch_all_filtered_explains_why(app, stub_list, monkeypatch):
    """有数据但被过滤光了，必须弹窗解释，而不是显示空列表。"""
    import wxlive.gui_fetch as GF
    import wxlive.fetchstate as S
    from wxlive.fetch import ContentItem

    holder, _ = stub_list
    holder["page"] = _fake_page([
        ContentItem(content_id="aaa", title="甲", url="http://x/a")])
    S.hide(app._fetch_state, ["aaa"], {"aaa": "甲"})
    shown = []
    monkeypatch.setattr(GF.messagebox, "showinfo",
                        lambda title, msg, **kw: shown.append((title, msg)))

    app._fetch_refresh()
    _pump(app)

    assert app.fetch_tree.get_children() == ()
    assert shown, "应当弹窗解释为什么是空的"
    assert "过滤" in shown[0][0] or "过滤" in shown[0][1]


# ---------- 解析 ----------

def test_fetch_probe_marks_unreachable(app, stub_list, monkeypatch):
    """解析失败（地址过期）应把状态写成「地址不可用」并记日志。"""
    import wxlive.fetch as F
    import wxlive.gui_fetch as GF
    from wxlive.fetch import ContentItem, ProbeInfo

    holder, _ = stub_list
    holder["page"] = _fake_page([
        ContentItem(content_id="aaa", title="甲", url="http://x/a")])
    monkeypatch.setattr(GF.messagebox, "showinfo", lambda *a, **kw: None)
    monkeypatch.setattr(GF.messagebox, "showwarning", lambda *a, **kw: None)
    monkeypatch.setattr(F, "probe_item",
                        lambda item, **kw: ProbeInfo(ok=False, status=403,
                                                     reason="地址可能已过期"))
    app._fetch_refresh()
    _pump(app)

    row = app.fetch_tree.get_children()[0]
    app.fetch_tree.selection_set(row)
    app._fetch_probe_selected()
    _pump(app)

    assert app.fetch_tree.item(row, "values")[3] == "地址不可用"


def test_fetch_probe_marks_downloadable(app, stub_list, monkeypatch):
    """解析成功应显示加密状态与大小。"""
    import wxlive.fetch as F
    import wxlive.gui_fetch as GF
    from wxlive.fetch import ContentItem, ProbeInfo

    holder, _ = stub_list
    holder["page"] = _fake_page([
        ContentItem(content_id="aaa", title="甲", url="http://x/a")])
    monkeypatch.setattr(GF.messagebox, "showinfo", lambda *a, **kw: None)
    monkeypatch.setattr(F, "probe_item",
                        lambda item, **kw: ProbeInfo(ok=True, status=206,
                                                     encrypted=True, enc_len=131072,
                                                     size_bytes=419430400))
    app._fetch_refresh()
    _pump(app)

    row = app.fetch_tree.get_children()[0]
    app.fetch_tree.selection_set(row)
    app._fetch_probe_selected()
    _pump(app)

    state = app.fetch_tree.item(row, "values")[3]
    assert "可下载" in state and "加密" in state and "400" in state


def test_fetch_probe_without_selection(app, monkeypatch):
    """没选中就点解析应提示，不启动线程。"""
    import wxlive.gui_fetch as GF

    shown = []
    monkeypatch.setattr(GF.messagebox, "showinfo",
                        lambda title, msg, **kw: shown.append(title))
    app._fetch_probe_selected()
    assert shown and app._fetch_busy is False


# ---------- 中止 ----------

def test_fetch_abort_flag(app):
    """中止按钮置位标志，_fetch_finish 会把它清掉。"""
    app._fetch_busy = True
    app.fetch_cancel_btn.configure(state="normal")
    app._fetch_abort()
    assert app._fetch_cancel.is_set()


def test_fetch_abort_without_busy_is_noop(app):
    """不在下载时点中止不应置位。"""
    app._fetch_busy = False
    app._fetch_abort()
    assert not app._fetch_cancel.is_set()


# ---------- 体检 ----------

def test_fetch_health_failure_prompts(app, monkeypatch):
    """工具没跑时体检要给出可执行的排查步骤。"""
    import wxlive.fetch as F
    import wxlive.gui_fetch as GF

    real_err = F.FetchError

    def boom(*a, **kw):
        raise real_err("连不上")

    monkeypatch.setattr(F, "_api_get", boom)
    seen = []
    monkeypatch.setattr(GF.messagebox, "showwarning",
                        lambda title, msg, **kw: seen.append((title, msg)))

    app._fetch_check_health()
    _pump(app)

    assert seen, "应当弹窗提示"
    assert "tools/wxcd" in seen[0][1]


# ---------- 系统代理（防断网）----------

def test_proxy_status_shown(app):
    """下载页必须显示系统代理状态 —— 工具接管后关掉会全网断。"""
    assert hasattr(app, "fetch_proxy_var")
    # 等一次后台探测回投
    _pump(app)
    assert app.fetch_proxy_var.get()


def test_proxy_restore_when_already_fixed(app, monkeypatch):
    """已经是 10808 时点还原应提示"无需还原"，不写注册表。"""
    import wxlive.gui_fetch as GF
    import wxlive.systemproxy as P

    monkeypatch.setattr(P, "read",
                        lambda: {"enabled": True, "server": P.FIXED_PROXY,
                                 "override": ""})
    called = {"n": 0}
    monkeypatch.setattr(P, "restore_fixed",
                        lambda: called.__setitem__("n", called["n"] + 1)
                        or (True, "x"))
    seen = []
    monkeypatch.setattr(GF.messagebox, "showinfo",
                        lambda title, msg, **kw: seen.append(title))

    app._fetch_restore_proxy()
    _pump(app)
    assert seen and "无需还原" in seen[0]
    assert called["n"] == 0, "不该去写注册表"


def test_proxy_restore_user_cancels(app, monkeypatch):
    """确认框点「否」时不应调用 restore_fixed。"""
    import wxlive.gui_fetch as GF
    import wxlive.systemproxy as P

    monkeypatch.setattr(P, "read",
                        lambda: {"enabled": True, "server": P.TOOL_PROXY,
                                 "override": ""})
    monkeypatch.setattr(P, "tool_still_running", lambda: False)
    called = {"n": 0}
    monkeypatch.setattr(P, "restore_fixed",
                        lambda: called.__setitem__("n", called["n"] + 1)
                        or (True, "x"))
    monkeypatch.setattr(GF.messagebox, "askyesno", lambda *a, **kw: False)

    app._fetch_restore_proxy()
    assert called["n"] == 0


def test_proxy_restore_confirmed(app, monkeypatch):
    """确认后调用 restore_fixed 并刷新状态。"""
    import wxlive.gui_fetch as GF
    import wxlive.systemproxy as P

    monkeypatch.setattr(P, "read",
                        lambda: {"enabled": True, "server": P.TOOL_PROXY,
                                 "override": ""})
    monkeypatch.setattr(P, "tool_still_running", lambda: False)
    called = {"n": 0}

    def fake_restore():
        called["n"] += 1
        return True, "已还原系统代理为 127.0.0.1:10808"

    monkeypatch.setattr(P, "restore_fixed", fake_restore)
    monkeypatch.setattr(GF.messagebox, "askyesno", lambda *a, **kw: True)
    monkeypatch.setattr(GF.messagebox, "showinfo", lambda *a, **kw: None)

    app._fetch_restore_proxy()
    _pump(app)
    assert called["n"] == 1


def test_fetch_health_warns_when_proxy_not_2023(app, monkeypatch):
    """工具 API 正常但系统代理指着 10808 时，体检必须告警而不是报「体检通过」。"""
    import wxlive.fetch as F
    import wxlive.gui_fetch as GF
    import wxlive.systemproxy as P

    monkeypatch.setattr(F, "_api_get", lambda *a, **kw: {
        "code": 0,
        "data": {
            "version": "261106",
            "api": {"addr": "127.0.0.1:2022", "status": "running"},
            "proxy": {"addr": "127.0.0.1:2023", "listening": True, "status": "running"},
            "server_statuses": {"api": "running", "interceptor": "running"},
        },
    })
    monkeypatch.setattr(P, "read", lambda: {
        "enabled": True, "server": P.FIXED_PROXY, "override": ""
    })
    monkeypatch.setattr(P, "tool_still_running", lambda: True)

    warns = []
    oks = []
    monkeypatch.setattr(GF.messagebox, "showwarning", lambda t, m, **kw: warns.append((t, m)))
    monkeypatch.setattr(GF.messagebox, "showinfo", lambda t, m, **kw: oks.append((t, m)))

    app._fetch_check_health()
    _pump(app)

    assert not oks, "系统代理没走 2023 时不应报体检通过"
    assert warns and "抓不到流" in warns[0][0]


def test_fetch_health_passes_when_proxy_is_2023(app, monkeypatch):
    """工具在线且系统代理指着 2023 时，体检通过。"""
    import wxlive.fetch as F
    import wxlive.gui_fetch as GF
    import wxlive.systemproxy as P

    monkeypatch.setattr(F, "_api_get", lambda *a, **kw: {
        "code": 0,
        "data": {
            "version": "261106",
            "api": {"addr": "127.0.0.1:2022", "status": "running"},
            "proxy": {"addr": "127.0.0.1:2023", "listening": True, "status": "running"},
            "server_statuses": {"api": "running", "interceptor": "running"},
        },
    })
    monkeypatch.setattr(P, "read", lambda: {
        "enabled": True, "server": P.TOOL_PROXY, "override": ""
    })
    monkeypatch.setattr(P, "tool_still_running", lambda: True)

    oks = []
    monkeypatch.setattr(GF.messagebox, "showinfo", lambda t, m, **kw: oks.append((t, m)))

    app._fetch_check_health()
    _pump(app)

    assert oks and "体检通过" in oks[0][0]


def test_fetch_refresh_shows_discovered_videos_and_dedups_by_encfilekey(app, stub_list, monkeypatch):
    """set_feed 发现的回放/视频：在 list_page 为空时不弹「还没有内容」，且按 encfilekey 去重。"""
    import wxlive.gui_fetch as GF
    from wxlive.fetch import ContentItem

    holder, _ = stub_list
    # Case 1: content/list 为空，但日志发现一条回放
    holder["page"] = _fake_page([])
    holder["videos"] = [
        ContentItem(
            content_id="14883581847679010000",
            title="小升初数学回放",
            url="https://finder.video.qq.com/251/20302/stodownload?encfilekey=ENC1&token=T1",
            author="名师",
            decrypt_key=123456,
        )
    ]
    infos = []
    monkeypatch.setattr(GF.messagebox, "showinfo", lambda t, m, **kw: infos.append((t, m)))

    app._fetch_refresh()
    _pump(app)

    rows = app.fetch_tree.get_children()
    assert len(rows) == 1
    assert app.fetch_tree.item(rows[0], "values")[0] == "小升初数学回放"
    assert "日志发现" in app.fetch_tree.item(rows[0], "values")[3]
    assert not infos, "日志已经发现了回放，不该弹「还没有内容」"

    # Case 2: 同一条视频也出现在 content/list 里（id 精度不同但 encfilekey 相同）→ 去重只留 1 行
    holder["page"] = _fake_page([
        ContentItem(
            content_id="14883581847679010351",
            title="小升初数学回放",
            url="https://finder.video.qq.com/251/20302/stodownload?encfilekey=ENC1&token=T2",
            author="名师",
        )
    ])
    app._fetch_refresh()
    _pump(app)
    assert len(app.fetch_tree.get_children()) == 1


def test_push_encrypted_replay_to_nas_falls_back_to_decrypt_and_upload(
        app, stub_list, monkeypatch, tmp_path):
    """推送加密回放时，若 NAS 暂不支持 decrypt_key，自动本机解密抽音频后上传 /api/jobs/upload。"""
    import wxlive.fetch as F
    import wxlive.gui_fetch as GF
    from wxlive.fetch import ContentItem, ProbeInfo

    holder, _ = stub_list
    it = ContentItem(
        content_id="14789966503387730000",
        title="【小升初不焦虑】答疑专场",
        url="https://finder.video.qq.com/251/20302/stodownload?encfilekey=ENC99&token=T1",
        author="粤知升初",
        is_live=False,
        decrypt_key=1902534306,
    )
    holder["page"] = _fake_page([it])
    app._fetch_refresh()
    _pump(app)

    row = app.fetch_tree.get_children()[0]
    app.fetch_tree.selection_set([row])

    monkeypatch.setattr(F, "probe_item", lambda item, timeout=20: ProbeInfo(
        ok=True, status=200, encrypted=True, enc_len=131072, size_bytes=1024000))
    monkeypatch.setattr(F, "nas_supports_decrypt", lambda base: False)

    calls = {"dl": 0, "ex": 0, "up": []}

    def fake_dl(item, key, dest, on_progress=None):
        calls["dl"] += 1
        assert key == 1902534306
        dest.write_bytes(b"fake-mp4")
        if on_progress:
            on_progress(1024 * 1024, 0.5)
        return dest

    def fake_ex(video, wav=None, **kw):
        calls["ex"] += 1
        assert video.exists()
        wav.write_bytes(b"RIFF" + b"\x00" * 64)
        return wav

    def fake_up(base, file_path, name="", auto_transcribe=True, on_progress=None, **kw):
        assert file_path.exists()
        calls["up"].append((base, name, auto_transcribe, file_path.suffix))
        if on_progress:
            on_progress(68, 68)
        return {"id": "job_enc_1", "title": name}

    monkeypatch.setattr(F, "download_decrypted", fake_dl)
    monkeypatch.setattr(F, "extract_audio", fake_ex)
    monkeypatch.setattr(F, "upload_to_nas", fake_up)

    infos = []
    warns = []
    monkeypatch.setattr(GF.messagebox, "showinfo", lambda t, m, **kw: infos.append((t, m)))
    monkeypatch.setattr(GF.messagebox, "showwarning", lambda t, m, **kw: warns.append((t, m)))

    app._fetch_push_selected_to_nas()
    _pump(app)

    assert not warns, f"不应报失败弹窗: {warns}"
    assert calls["dl"] == 1 and calls["ex"] == 1
    assert len(calls["up"]) == 1
    assert calls["up"][0][1] == "【小升初不焦虑】答疑专场"
    assert calls["up"][0][3] == ".wav"
    assert infos and "已交给 NAS" in infos[0][0]
    assert "job_enc_1" in infos[0][1]


def test_push_encrypted_replay_uses_native_nas_decrypt_when_supported(
        app, stub_list, monkeypatch):
    """若 NAS 支持 decrypt_key，则直接把 url + decrypt_key 传给 /api/probe 与 /api/jobs/stream。"""
    import json as _j
    import urllib.request as _ur
    import wxlive.fetch as F
    import wxlive.gui_fetch as GF
    from wxlive.fetch import ContentItem, ProbeInfo

    holder, _ = stub_list
    it = ContentItem(
        content_id="14789966503387730000",
        title="加密回放",
        url="https://finder.video.qq.com/251/20302/stodownload?encfilekey=ENC99",
        is_live=False,
        decrypt_key=987654321,
    )
    holder["page"] = _fake_page([it])
    app._fetch_refresh()
    _pump(app)
    app.fetch_tree.selection_set([app.fetch_tree.get_children()[0]])

    monkeypatch.setattr(F, "probe_item", lambda item, timeout=20: ProbeInfo(
        ok=True, status=200, encrypted=True, enc_len=131072, size_bytes=1024000))
    monkeypatch.setattr(F, "nas_supports_decrypt", lambda base: True)

    sent = []

    class _FakeResp:
        def __init__(self, body):
            self._b = _j.dumps(body).encode("utf-8")

        def read(self):
            return self._b

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    class _FakeOpener:
        def open(self, req, timeout=40):
            payload = _j.loads(req.data.decode("utf-8"))
            sent.append((req.full_url, payload))
            if req.full_url.endswith("/api/probe"):
                return _FakeResp({"ok": True, "encrypted": True})
            return _FakeResp({"id": "job_native_1", "title": payload.get("name")})

    monkeypatch.setattr(_ur, "build_opener", lambda *a, **kw: _FakeOpener())

    infos = []
    monkeypatch.setattr(GF.messagebox, "showinfo", lambda t, m, **kw: infos.append((t, m)))

    app._fetch_push_selected_to_nas()
    _pump(app)

    assert len(sent) == 2
    assert sent[0][0].endswith("/api/probe") and sent[0][1]["decrypt_key"] == 987654321
    assert sent[1][0].endswith("/api/jobs/stream") and sent[1][1]["decrypt_key"] == 987654321
    assert infos and "job_native_1" in infos[0][1]


def test_push_expired_replay_fails_fast_with_clear_video_hint(
        app, stub_list, monkeypatch):
    """回放地址过期时直接提示回微信重开视频/回放，而不是误报"重开直播"。"""
    import wxlive.fetch as F
    import wxlive.gui_fetch as GF
    from wxlive.fetch import ContentItem, ProbeInfo

    holder, _ = stub_list
    it = ContentItem(
        content_id="123",
        title="过期回放",
        url="https://finder.video.qq.com/251/20302/stodownload?encfilekey=DEAD",
        is_live=False,
    )
    holder["page"] = _fake_page([it])
    app._fetch_refresh()
    _pump(app)
    app.fetch_tree.selection_set([app.fetch_tree.get_children()[0]])

    monkeypatch.setattr(F, "probe_item", lambda item, timeout=20: ProbeInfo(
        ok=False, status=403, reason="HTTP 403：地址可能已过期"))

    warns = []
    monkeypatch.setattr(GF.messagebox, "showwarning", lambda t, m, **kw: warns.append((t, m)))

    app._fetch_push_selected_to_nas()
    _pump(app)

    assert warns
    assert "403" in warns[0][1]
    assert "视频/回放" in warns[0][1]
    assert "重新打开该直播" not in warns[0][1]


