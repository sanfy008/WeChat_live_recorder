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
