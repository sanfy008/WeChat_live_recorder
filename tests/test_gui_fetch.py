"""GUI 视频号下载页签的测试。

注意：必须用真实 mainloop。之前踩过坑——用 self.after 做异步初始化时，
不进 mainloop 会抛 "main thread is not in main loop"，导致误判。
所以这里用 update() + after 轮询的方式在真事件循环里验证。
"""

import tkinter as tk

import pytest


@pytest.fixture
def app():
    """构造 App 并进入真实事件循环一次，用完销毁。"""
    from wxlive.gui import App

    a = App()
    a.withdraw()          # 不弹窗
    a.update()            # 真正跑一遍事件循环
    yield a
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
            # 再多跑几轮，确保回调全部落地
            for _ in range(5):
                app.update()
                time.sleep(0.01)
            return
        time.sleep(0.01)


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


def test_fetch_tree_columns(app):
    """列表三列：标题 / 内容ID / 状态。"""
    cols = app.fetch_tree["columns"]
    assert tuple(cols) == ("title", "cid", "state")


def test_fetch_default_options(app):
    """默认只留音频（不保留视频）、不自动转写。"""
    assert app.fetch_keep_video_var.get() is False
    assert app.fetch_auto_trans_var.get() is False


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

    monkeypatch.setattr(F, "list_contents", boom)
    monkeypatch.setattr(GF.messagebox, "showwarning", lambda *a, **kw: None)

    app._fetch_refresh()
    _pump(app)

    hint = app.fetch_hint_var.get()
    assert hint == "读取失败", f"期望提示读取失败，实际 {hint!r}"


def test_fetch_refresh_populates_tree(app, monkeypatch):
    """拿到内容后应填进列表，并能按 id 反查。"""
    import wxlive.fetch as F

    fake = [
        F.ContentItem(content_id="111", title="测试视频A", url="http://x/a"),
        F.ContentItem(content_id="222", title="测试视频B", url="http://x/b"),
    ]
    monkeypatch.setattr(F, "list_contents", lambda *a, **kw: fake)

    app._fetch_refresh()
    _pump(app)

    rows = app.fetch_tree.get_children()
    assert len(rows) == 2
    titles = [app.fetch_tree.item(r, "values")[0] for r in rows]
    assert titles == ["测试视频A", "测试视频B"]
    assert "111" in app._fetch_items and "222" in app._fetch_items


def test_fetch_empty_list_shows_hint(app, monkeypatch):
    """没有内容时给出提示而不是空白无反应。"""
    import wxlive.fetch as F
    import wxlive.gui_fetch as GF

    monkeypatch.setattr(F, "list_contents", lambda *a, **kw: [])
    monkeypatch.setattr(GF.messagebox, "showinfo", lambda *a, **kw: None)

    app._fetch_refresh()
    _pump(app)

    assert app.fetch_tree.get_children() == ()
    assert "还没有捕获" in app.fetch_hint_var.get()


def test_fetch_set_state_updates_row(app, monkeypatch):
    """状态列能单独更新，不影响其他列。"""
    import wxlive.fetch as F

    fake = [F.ContentItem(content_id="999", title="X", url="http://x")]
    monkeypatch.setattr(F, "list_contents", lambda *a, **kw: fake)
    app._fetch_refresh()
    _pump(app)

    row = app.fetch_tree.get_children()[0]
    app._fetch_set_state(row, "完成 80 分钟")
    vals = app.fetch_tree.item(row, "values")
    assert vals[0] == "X" and vals[1] == "999" and vals[2] == "完成 80 分钟"


def test_fetch_set_state_bad_row_is_safe(app):
    """行已被删除时更新状态不应抛异常。"""
    app._fetch_set_state("nonexistent-row", "x")  # 不应崩
