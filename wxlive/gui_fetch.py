"""GUI 页签：视频号下载（自动解密）。

单独成一个 mixin 模块，避免 gui.py 继续膨胀。
App 继承本 mixin 后调用 _build_fetch_tab() 即可。

设计要点（含借鉴来源）
- 列表来自本地抓流工具的 /api/content/list，用户在微信里打开过的视频会自动出现
- 不需要点工具的下载按钮：工具的下载按钮只是它自己做 MP4+转MP3，
  我们要的是它已经捕获到的 url + key
- 下载全程后台线程，进度回主线程更新（tkinter 非线程安全）

删除 / 清空为什么是本侧的
    抓流工具没有删除内容的 API（核对过 ltaoo/wx_channels_download 的
    internal/api/routes.go，content 只有 list/detail/relations）。
    所以「删除选中 / 清空列表」= 记进 config/fetch_state.json 的 hidden 里，
    刷新时过滤掉。界面上明确写"仅隐藏，不影响工具"，
    并提供「恢复被删除的条目」，避免用户以为东西没了。

借鉴（GitHub 同类项目）
    1. gui8912/wxchannels-downloader
       - 按「标题 + 大小」认人去重（回放每请求一次就换一套签名，按 URL 去重等于没去重）
       - 「只留正在播放的那条」、体检、把 HTTP 错误翻译成可执行建议
       - 任务队列串行 + 全部暂停/恢复 + 任务列表持久化
    2. qiye45/wechatVideoDownload（6k star）
       - 清空列表、标题显示可选、下载进度条
    3. knowing-top/ChannelsDownloader
       - 表格化捕获列表、已下载的按钮从「下载」切换成「查看」+ 清空
"""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from . import fetchstate
from . import fetch as F

# 一页取多少条。工具的默认 page_size 是 20，这里略大一点少翻页，
# 但别太大：每条都带一个约 1000 字符的签名 URL，500 条就是 500KB。
PAGE_SIZE = 100


class FetchTabMixin:
    """需要宿主类提供：_log(str)、status_var、progress、_run_bg(fn)"""

    # ---------- 构建界面 ----------

    def _build_fetch_tab(self) -> None:
        f = self.tab_fetch

        # 本地状态（隐藏/已下载）在界面构建时就加载，后面所有刷新都基于它
        self._fetch_state = fetchstate.load_state()

        # 状态变量必须在**任何**可能触发的回调之前准备好。
        # 这里踩过一个真 bug：下面第 95 行附近会调 _fetch_refresh_proxy()，
        # 它内部走 _ui() → self._ui_queue.put()，而 _ui_queue 原先在本方法
        # 末尾才创建 —— 于是启动时抛 AttributeError（后台线程里，
        # 界面上只表现为状态条永远停在"读取中…"，很容易误判成"读得慢"）。
        self._fetch_items = {}
        self._fetch_busy = False
        self._fetch_proxy_after = None
        self._fetch_cancel = threading.Event()
        self._ui_queue = queue.Queue()

        # ===== 顶部：工具状态 =====
        top = ttk.Frame(f)
        top.pack(fill="x", padx=12, pady=(10, 4))

        ttk.Label(top, text="视频号下载", style="Title.TLabel").pack(side="left")

        self.fetch_api_var = tk.StringVar(value="http://127.0.0.1:2022")
        ttk.Label(top, text="  工具 API").pack(side="left")
        ttk.Entry(top, textvariable=self.fetch_api_var, width=22).pack(side="left", padx=4)

        self.fetch_status_btn = ttk.Button(top, text="体检", width=7,
                                           command=self._fetch_check_health)
        self.fetch_status_btn.pack(side="left", padx=(6, 0))

        self.fetch_refresh_btn = ttk.Button(top, text="刷新列表", command=self._fetch_refresh)
        self.fetch_refresh_btn.pack(side="right")

        self.fetch_hint_var = tk.StringVar(value="")
        ttk.Label(top, textvariable=self.fetch_hint_var, style="Hint.TLabel").pack(
            side="right", padx=(0, 10))

        # ===== 系统代理状态条 =====
        # 抓流工具会把系统代理改成 127.0.0.1:2023，工具一关就全网断。
        # 本机固定出口是 10808，所以这条要一直显示、随时可一键还原。
        px = ttk.Frame(f)
        px.pack(fill="x", padx=12, pady=(0, 4))

        self.fetch_proxy_var = tk.StringVar(value="系统代理：读取中…")
        self.fetch_proxy_label = ttk.Label(px, textvariable=self.fetch_proxy_var,
                                           style="Hint.TLabel")
        self.fetch_proxy_label.pack(side="left")

        self.fetch_proxy_btn = ttk.Button(px, text="还原系统代理", width=13,
                                         command=self._fetch_restore_proxy)
        self.fetch_proxy_btn.pack(side="right")
        self.fetch_takeover_btn = ttk.Button(px, text="接管代理(抓流)", width=14,
                                             command=self._fetch_takeover_proxy)
        self.fetch_takeover_btn.pack(side="right", padx=(0, 6))
        self.fetch_restart_tool_btn = ttk.Button(px, text="重启抓流工具", width=13,
                                                 command=self._fetch_restart_tool)
        self.fetch_restart_tool_btn.pack(side="right", padx=(0, 6))
        ttk.Button(px, text="重新检测", width=9,
                   command=self._fetch_refresh_proxy).pack(side="right", padx=(0, 6))

        # 启动时就探一次，并每分钟自检（工具可能在后台被关掉）
        self._fetch_refresh_proxy()
        self._fetch_proxy_poll()

        # ===== 搜索 + 视图开关 =====
        bar = ttk.Frame(f)
        bar.pack(fill="x", padx=12, pady=(0, 4))

        ttk.Label(bar, text="搜索").pack(side="left")
        self.fetch_search_var = tk.StringVar()
        search_entry = ttk.Entry(bar, textvariable=self.fetch_search_var, width=26)
        search_entry.pack(side="left", padx=(4, 6))
        search_entry.bind("<Return>", lambda e: self._fetch_refresh())
        # 输入即搜索的防抖：每敲一下就发请求会把工具压住（服务端 LIKE 查询）
        self._fetch_search_after = None
        self.fetch_search_var.trace_add("write", self._fetch_on_search_typed)

        ttk.Button(bar, text="搜索", width=6,
                   command=self._fetch_refresh).pack(side="left")
        ttk.Button(bar, text="清空搜索", width=8,
                   command=self._fetch_clear_search).pack(side="left", padx=(4, 0))

        self.fetch_show_hidden_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="显示已删除的",
                        variable=self.fetch_show_hidden_var,
                        command=self._fetch_refresh).pack(side="left", padx=(16, 0))

        self.fetch_only_new_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="只看未下载的",
                        variable=self.fetch_only_new_var,
                        command=self._fetch_refresh).pack(side="left", padx=(10, 0))

        # 翻页
        nav = ttk.Frame(f)
        nav.pack(fill="x", padx=12, pady=(0, 4))
        self.fetch_prev_btn = ttk.Button(nav, text="◀ 上一页", width=9,
                                         command=self._fetch_prev_page, state="disabled")
        self.fetch_prev_btn.pack(side="left")
        self.fetch_next_btn = ttk.Button(nav, text="下一页 ▶", width=9,
                                         command=self._fetch_next_page, state="disabled")
        self.fetch_next_btn.pack(side="left", padx=(4, 0))
        self.fetch_page_var = tk.StringVar(value="")
        ttk.Label(nav, textvariable=self.fetch_page_var, style="Hint.TLabel").pack(
            side="left", padx=(10, 0))

        ttk.Label(nav, text="每页").pack(side="left", padx=(16, 0))
        self.fetch_page_size_var = tk.StringVar(value=str(PAGE_SIZE))
        ps = ttk.Combobox(nav, textvariable=self.fetch_page_size_var, width=5,
                          values=("20", "50", "100", "200"), state="readonly")
        ps.pack(side="left", padx=(4, 0))
        ps.bind("<<ComboboxSelected>>", lambda e: self._fetch_refresh())

        self._fetch_page = 1
        self._fetch_total = 0

        # ===== 列表 =====
        mid = ttk.LabelFrame(f, text="已捕获的内容（在微信里打开过就会出现在这里）")
        mid.pack(fill="both", expand=True, padx=12, pady=4)

        cols = ("title", "author", "publish", "state")
        self.fetch_tree = ttk.Treeview(mid, columns=cols, show="headings",
                                       selectmode="extended", height=8)
        self.fetch_tree.heading("title", text="标题")
        self.fetch_tree.heading("author", text="作者")
        self.fetch_tree.heading("publish", text="发布时间")
        self.fetch_tree.heading("state", text="状态")
        self.fetch_tree.column("title", width=400, anchor="w")
        self.fetch_tree.column("author", width=130, anchor="w")
        self.fetch_tree.column("publish", width=110, anchor="center")
        self.fetch_tree.column("state", width=150, anchor="center")
        self.fetch_tree.tag_configure("hidden", foreground="#999")
        self.fetch_tree.tag_configure("done", foreground="#1a7f37")
        # 直播用醒目色：它需要「推 NAS」而不是「下载」，别让人点错
        self.fetch_tree.tag_configure("live", foreground="#c2410c")

        sb = ttk.Scrollbar(mid, orient="vertical", command=self.fetch_tree.yview)
        self.fetch_tree.configure(yscrollcommand=sb.set)
        self.fetch_tree.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=8)
        sb.pack(side="right", fill="y", padx=(0, 8), pady=8)

        # 双击 = 解析该条
        self.fetch_tree.bind("<Double-1>", lambda e: self._fetch_probe_selected())
        # Ctrl+A 全选（Treeview 默认不支持）
        self.fetch_tree.bind("<Control-a>", self._fetch_select_all)
        self.fetch_tree.bind("<Control-A>", self._fetch_select_all)

        # ===== 选中操作 =====
        row2 = ttk.Frame(f)
        row2.pack(fill="x", padx=12, pady=(2, 2))

        ttk.Button(row2, text="全选", width=6,
                   command=self._fetch_select_all).pack(side="left")
        ttk.Button(row2, text="反选", width=6,
                   command=self._fetch_invert_selection).pack(side="left", padx=(4, 0))
        ttk.Button(row2, text="解析选中", width=9,
                   command=self._fetch_probe_selected).pack(side="left", padx=(12, 0))
        # 显式的"取址"入口：选中指定一条，把真实流地址解析出来并告诉你
        # 能不能拉。比"必须先去微信点下载"少一个来回，也便于确认主播在不在播。
        ttk.Button(row2, text="解析直播地址", width=12,
                   command=self._fetch_resolve_live).pack(side="left", padx=(6, 0))
        ttk.Button(row2, text="删除选中", width=9,
                   command=self._fetch_delete_selected).pack(side="left", padx=(12, 0))
        ttk.Button(row2, text="清空列表", width=9,
                   command=self._fetch_clear_all).pack(side="left", padx=(4, 0))

        self.fetch_restore_btn = ttk.Button(row2, text="恢复被删除的", width=12,
                                            command=self._fetch_restore_hidden)
        self.fetch_restore_btn.pack(side="left", padx=(12, 0))

        # ===== 选项 =====
        opt = ttk.Frame(f)
        opt.pack(fill="x", padx=12, pady=(2, 2))

        self.fetch_probe_first_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(opt, text="下载前先解析（查地址是否过期/是否加密/多大）",
                        variable=self.fetch_probe_first_var).pack(side="left")

        self.fetch_keep_video_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(opt, text="保留视频文件（默认只留音频）",
                        variable=self.fetch_keep_video_var).pack(side="left", padx=(16, 0))

        self.fetch_auto_trans_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(opt, text="下载完自动转写",
                        variable=self.fetch_auto_trans_var).pack(side="left", padx=(16, 0))

        # ===== 直播 → NAS（方案 A 的分工：本机拿地址，NAS 长时间拉流转写）=====
        # 和上面"下载选中项的音频"是两件不同的事：
        #   · 下载 = 回放（已放完），本地拉一遍、抽音频、本地转写
        #   · 推 NAS = 直播（正在播），把流地址交给 NAS，本机可以关掉
        # 不做这个区分的话，直播就只能等播完再下，等于放弃了方案 A 的价值。
        nas = ttk.Frame(f)
        nas.pack(fill="x", padx=12, pady=(2, 4))

        ttk.Label(nas, text="NAS").pack(side="left")
        self.fetch_nas_var = tk.StringVar(value="http://192.168.68.49:8910")
        ttk.Entry(nas, textvariable=self.fetch_nas_var, width=30).pack(side="left", padx=(4, 8))

        self.fetch_nas_health_btn = ttk.Button(
            nas, text="体检", width=6, command=self._fetch_nas_health)
        self.fetch_nas_health_btn.pack(side="left")

        self.fetch_nas_live_btn = ttk.Button(
            nas, text="把正在播的直播推给 NAS", width=22,
            command=self._fetch_push_local_live)
        self.fetch_nas_live_btn.pack(side="left", padx=(10, 0))

        self.fetch_nas_sel_btn = ttk.Button(
            nas, text="推送选中项到 NAS", width=17,
            command=self._fetch_push_selected_to_nas)
        self.fetch_nas_sel_btn.pack(side="left", padx=(6, 0))

        self.fetch_nas_var_status = tk.StringVar(value="")
        ttk.Label(nas, textvariable=self.fetch_nas_var_status,
                  style="Hint.TLabel").pack(side="left", padx=(10, 0))

        # ===== 操作按钮 + 进度 =====
        btns = ttk.Frame(f)
        btns.pack(fill="x", padx=12, pady=(4, 6))

        self.fetch_start_btn = ttk.Button(btns, text="下载选中项的音频",
                                          style="Run.TButton", command=self._fetch_start)
        self.fetch_start_btn.pack(side="left")

        self.fetch_cancel_btn = ttk.Button(btns, text="中止", width=6,
                                           command=self._fetch_abort, state="disabled")
        self.fetch_cancel_btn.pack(side="left", padx=(8, 0))

        ttk.Button(btns, text="打开录制目录",
                   command=self._fetch_open_dir).pack(side="left", padx=(10, 0))

        self.fetch_progress = ttk.Progressbar(btns, mode="determinate", length=240)
        self.fetch_progress.pack(side="right")
        self.fetch_prog_var = tk.StringVar(value="")
        ttk.Label(btns, textvariable=self.fetch_prog_var, width=26).pack(
            side="right", padx=(0, 8))

        # ===== 说明（可关闭）=====
        self.fetch_note = ttk.LabelFrame(f, text="使用说明")
        self.fetch_note.pack(fill="x", padx=12, pady=(0, 10))

        txt = (
            "【准备抓流】\n"
            "1. 看上方状态条：显示「127.0.0.1:2023（抓流工具接管中）」才能抓；"
            "若不是，点「重启抓流工具」或「接管代理(抓流)」\n"
            "2. 若微信直播间在接管前就开着，需关闭后在微信里重新打开（停留 3-5 秒），"
            "再点「刷新列表」\n"
            "\n"
            "【回放/视频：已放完】点「刷新列表」→ 选中 →「下载选中项的音频」→ 本地转写\n"
            "【直播：正在播】点「刷新列表」→ 选中直播 →「推送选中项到 NAS」（本机可关，NAS 挂着录）\n"
            "\n"
            "用完抓流或关掉工具后，点「还原系统代理」即可切回日常代理 10808\n"
            "注：「删除选中 / 清空列表」只在本工具隐藏，不动工具数据库，随时可恢复"
        )
        self.fetch_note_label = ttk.Label(self.fetch_note, text=txt, justify="left",
                                          style="Hint.TLabel")
        self.fetch_note_label.pack(anchor="w", padx=10, pady=8)
        if fetchstate.hint_dismissed(self._fetch_state):
            self._fetch_hide_note()

        # 不能在这里直接调 _pump_ui_queue：窗口还没映射，after 可能注册失败，
        # 被吞掉后泵就永不再排程。用 after_idle 推迟到事件循环就绪后启动。
        try:
            self.after_idle(self._pump_ui_queue)
        except Exception:  # noqa: BLE001
            self._pump_ui_queue()

    def _fetch_hide_note(self) -> None:
        """收起说明区（用户看过了）。"""
        try:
            self.fetch_note.pack_forget()
        except Exception:  # noqa: BLE001
            pass
        try:
            st = self._fetch_state
            if not fetchstate.hint_dismissed(st):
                fetchstate.dismiss_hint(st)
                fetchstate.save_state(st)
        except Exception:  # noqa: BLE001
            pass

    # ---------- NAS：直播推流（方案 A）----------

    def _fetch_nas_base(self) -> str:
        return (self.fetch_nas_var.get() or "").strip().rstrip("/")

    def _fetch_nas_set_status(self, text: str, warn: bool = False) -> None:
        try:
            self.fetch_nas_var_status.set(text)
        except Exception:  # noqa: BLE001
            pass

    def _fetch_nas_health(self) -> None:
        """确认 NAS 服务在线，把关键信息显示在状态栏。"""
        import json
        import urllib.request

        base = self._fetch_nas_base()
        if not base:
            self._fetch_nas_set_status("请先填 NAS 地址", warn=True)
            return
        self._fetch_nas_set_status("检测中…")

        def work() -> None:
            try:
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with opener.open(base + "/api/health", timeout=10) as r:
                    h = json.loads(r.read().decode("utf-8"))
                ok = bool(h.get("ok"))
                msg = (f"在线 · ffmpeg={'有' if h.get('ffmpeg_available') else '缺'}"
                       f" · 磁盘 {h.get('disk_free_gb')}GB"
                       f" · 任务 {h.get('job_count')}（进行中 {h.get('active_count')}）")
                self._ui(lambda: self._fetch_nas_set_status(msg if ok else "服务异常"))
                self._log("[NAS] " + msg)
            except Exception as exc:  # noqa: BLE001
                m = f"{type(exc).__name__}: {exc}"
                self._ui(lambda: self._fetch_nas_set_status("连不上", warn=True))
                self._log(f"[NAS] 体检失败：{m}")

        self._run_bg(work)

    def _fetch_nas_push(self, url: str, name: str,
                        on_done=None, *,
                        is_live: bool = True,
                        decrypt_key: int = 0,
                        base: str = "") -> None:
        """把一条流地址交给 NAS 开录（停录后 NAS 自动转写）。"""
        import json
        import urllib.error
        import urllib.request

        from .stream_capture import extract_url, is_url

        base = (base or "").strip().rstrip("/") or self._fetch_nas_base()
        clean = extract_url(url)
        if not is_url(clean):
            messagebox.showwarning("地址无效", "没能从这一条里识别出流地址。")
            return

        self._fetch_nas_set_status("推送中…")

        def work() -> None:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            try:
                probe_payload = {"url": clean}
                if decrypt_key:
                    probe_payload["decrypt_key"] = int(decrypt_key)
                probe_req = urllib.request.Request(
                    base + "/api/probe",
                    data=json.dumps(probe_payload).encode(),
                    headers={"Content-Type": "application/json"},
                )
                with opener.open(probe_req, timeout=40) as r:
                    probe = json.loads(r.read().decode("utf-8"))
                if not probe.get("ok"):
                    err = probe.get("error") or "未知错误"
                    hint = (
                        "地址可能已过期，回微信重新打开该直播。"
                        if is_live else
                        "地址可能已过期或视频已加密，请回微信重新打开该视频/回放，或改用「下载选中项的音频」。"
                    )
                    self._ui(lambda: self._fetch_nas_set_status("探测失败", warn=True))
                    self._log(f"[NAS] 探测失败：{err}")
                    self._ui(lambda e=err, h=hint: messagebox.showwarning(
                        "NAS 探测失败", e + "\n\n" + h))
                    return

                start_payload = {
                    "url": clean,
                    "name": name or "",
                    "auto_transcribe": True,
                }
                if decrypt_key:
                    start_payload["decrypt_key"] = int(decrypt_key)
                start_req = urllib.request.Request(
                    base + "/api/jobs/stream",
                    data=json.dumps(start_payload).encode(),
                    headers={"Content-Type": "application/json"},
                )
                with opener.open(start_req, timeout=60) as r:
                    job = json.loads(r.read().decode("utf-8"))

                jid = job.get("id", "?")
                title = job.get("title", name)
                msg = f"NAS 已开始录制：{title}"
                self._ui(lambda: self._fetch_nas_set_status("录制中", warn=False))
                self._log(f"[NAS] {msg}（任务 {jid}，停录后自动转写）")
                self._ui(lambda b=base, m=msg, j=jid: messagebox.showinfo(
                    "已交给 NAS",
                    f"{m}\n\n任务 ID：{j}\n本机可以停止播放/关闭页面了。\n\n"
                    f"进度与逐字稿：{b}/"))
                if on_done:
                    self._ui(on_done)
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:300]
                self._ui(lambda: self._fetch_nas_set_status("推送失败", warn=True))
                self._log(f"[NAS] 推送失败 HTTP {exc.code}：{detail}")
                self._ui(lambda d=detail, c=exc.code: messagebox.showwarning(
                    "NAS 拒绝", f"HTTP {c}\n{d}"))
            except Exception as exc:  # noqa: BLE001
                m = f"{type(exc).__name__}: {exc}"
                self._ui(lambda: self._fetch_nas_set_status("推送失败", warn=True))
                self._log(f"[NAS] 推送失败：{m}")
                self._ui(lambda mm=m: messagebox.showwarning("推送到 NAS 失败", mm))

        self._run_bg(work)

    def _fetch_nas_push_video(self, it) -> None:
        """把回放/点播视频推给 NAS。

        微信视频号回放链接前 ~131KB 是 ISAAC64 加密的，直接把 URL 交给
        `ffmpeg` 探测必然报 `Invalid data found when processing input`。
        因此先做 HTTP 探测与加密判断：
        - 明文视频：直接走 NAS 拉流；
        - 加密视频且 NAS 支持 `decrypt_key`：把密钥一并交由 NAS 直拉解密；
        - 加密视频且 NAS 暂不支持 `decrypt_key`：本机解密抽 16k WAV 后通过 `/api/jobs/upload` 传给 NAS。
        """
        import tempfile
        import time

        from . import fetch as F
        from .stream_capture import extract_url, is_url

        base = self._fetch_nas_base()
        if not base:
            self._fetch_nas_set_status("请先填 NAS 地址", warn=True)
            messagebox.showwarning("请先填 NAS 地址", "请填写 NAS 服务地址后再推送。")
            return
        clean = extract_url(getattr(it, "url", "") or "")
        if not is_url(clean):
            messagebox.showwarning("地址无效", "没能从这一条里识别出视频地址。")
            return

        api = self.fetch_api_var.get().strip() or "http://127.0.0.1:2022"
        self._fetch_nas_set_status("检查视频地址…")
        self._log(f"[NAS] 《{it.title}》是回放/视频，正在检查可用性与加密状态…")

        def work() -> None:
            try:
                info = F.probe_item(it)
            except Exception as exc:  # noqa: BLE001
                m = f"{type(exc).__name__}: {exc}"
                self._ui(lambda: self._fetch_nas_set_status("探测失败", warn=True))
                self._log(f"[NAS] 检查视频失败：{m}")
                self._ui(lambda mm=m: messagebox.showwarning("检查视频失败", mm))
                return

            if not info.ok:
                self._ui(lambda: self._fetch_nas_set_status("地址不可用", warn=True))
                self._log(f"[NAS] 《{it.title}》不可用：{info.reason}")
                self._ui(lambda r=info.reason: messagebox.showwarning(
                    "视频地址不可用",
                    f"《{it.title}》\n{r}\n\n请回微信重新打开该视频/回放，再点「刷新列表」。"))
                return

            is_enc = bool(info.encrypted or getattr(it, "decrypt_key", 0))
            if not is_enc:
                self._ui(lambda: self._fetch_nas_push(
                    clean, it.title, is_live=False, base=base))
                return

            try:
                key = F.get_decrypt_key(it, api=api)
            except Exception as exc:  # noqa: BLE001
                key = 0
                key_err = str(exc)
            else:
                key_err = ""

            if not key:
                msg = key_err or "文件已加密，但未获取到解密密钥（decodeKey）。"
                self._ui(lambda: self._fetch_nas_set_status("缺少解密密钥", warn=True))
                self._log(f"[NAS] 无法推送加密视频：《{it.title}》{msg}")
                self._ui(lambda m=msg: messagebox.showwarning("无法推送加密视频", m))
                return

            if F.nas_supports_decrypt(base):
                self._log(f"[NAS] NAS 支持服务端解密，直接推送《{it.title}》…")
                self._ui(lambda k=key: self._fetch_nas_push(
                    clean, it.title, is_live=False, decrypt_key=k, base=base))
                return

            self._log(
                f"[NAS] 《{it.title}》为加密回放（{info.size_text}），"
                "当前 NAS 暂未开启服务端解密，自动在本机解密抽音频后上传 NAS…"
            )
            self._ui(lambda: self._fetch_nas_set_status("解密下载中…"))
            last_ui = [0.0]

            try:
                with tempfile.TemporaryDirectory(prefix="wxlive_nas_") as tmpdir:
                    td = Path(tmpdir)
                    stem = F.safe_filename(it.title)
                    tmp_mp4 = td / f"{stem}.mp4"
                    tmp_wav = td / f"{stem}.wav"

                    def on_dl(done_bytes: int, _secs: float) -> None:
                        now = time.monotonic()
                        if now - last_ui[0] < 0.4:
                            return
                        last_ui[0] = now
                        mb = done_bytes / 1048576.0
                        self._ui(lambda m=mb: self._fetch_nas_set_status(
                            f"解密下载 {m:.0f} MB…"))

                    F.download_decrypted(it, key, tmp_mp4, on_progress=on_dl)
                    self._ui(lambda: self._fetch_nas_set_status("提取 16k 音频…"))
                    wav = F.extract_audio(tmp_mp4, tmp_wav)
                    tmp_mp4.unlink(missing_ok=True)

                    last_ui[0] = 0.0

                    def on_up(sent: int, total: int) -> None:
                        now = time.monotonic()
                        if sent < total and now - last_ui[0] < 0.4:
                            return
                        last_ui[0] = now
                        pct = (sent * 100.0 / total) if total else 0.0
                        self._ui(lambda p=pct: self._fetch_nas_set_status(
                            f"上传 NAS {p:.0f}%…"))

                    job = F.upload_to_nas(
                        base, wav, name=it.title, auto_transcribe=True,
                        on_progress=on_up,
                    )
            except Exception as exc:  # noqa: BLE001
                m = str(exc) or type(exc).__name__
                self._ui(lambda: self._fetch_nas_set_status("推送失败", warn=True))
                self._log(f"[NAS] 解密上传失败：{m}")
                self._ui(lambda mm=m: messagebox.showwarning("推送到 NAS 失败", mm))
                return

            jid = job.get("id", "?")
            title = job.get("title", it.title)
            msg = f"已解密并上传给 NAS：{title}"
            self._ui(lambda: self._fetch_nas_set_status("NAS 转写队列中"))
            self._log(f"[NAS] {msg}（任务 {jid}，自动转写中）")
            self._ui(lambda: messagebox.showinfo(
                "已交给 NAS",
                f"{msg}\n\n任务 ID：{jid}\nNAS 正在排队转写，本机可以关闭了。\n\n"
                f"进度与逐字稿：{base}/"))

        self._run_bg(work)

    def _fetch_push_local_live(self) -> None:
        """把本机正在录的直播流推给 NAS。

        地址从运行中的 ffmpeg 命令行里读，不依赖那个藏得很深的下拉菜单。
        """
        from . import liveurl

        self._fetch_nas_set_status("查找本机直播流…")

        tool_api = (self.fetch_api_var.get() or "").strip() or liveurl.DEFAULT_TOOL_API

        def work() -> None:
            streams = liveurl.find_live_streams(tool_api=tool_api)
            if not streams:
                self._ui(lambda: self._fetch_nas_set_status("没有进行中的直播", warn=True))
                self._ui(lambda: messagebox.showinfo(
                    "没有找到直播流",
                    "本机现在没有正在拉取的视频号直播。\n\n"
                    "请在微信里打开直播页，先点一次抓流工具的下载按钮"
                    "（让它起 ffmpeg），再点这里。"))
                return
            pick = streams[0]
            note = ""
            if len(streams) > 1:
                note = f"（检测到 {len(streams)} 路，取第一路）"
            self._log(f"[NAS] 找到直播流 {pick.title_hint}{note}")
            self._ui(lambda p=pick: (
                self._fetch_nas_set_status(f"找到：{p.title_hint}"),
                self._fetch_nas_push(p.url, p.title_hint),
            ))

        self._run_bg(work)

    def _fetch_resolve_live(self) -> None:
        """把选中直播的真实流地址解析出来，并探测当下能不能拉。

        为什么单独给一个按钮（而不是只藏在"推送 NAS"里面）：
        用户想知道的是"这条到底还能不能录"，而不是先把它推出去再等失败。
        解析 + 探测一次就能给出确定答案，几秒内完成。

        三条路径的差别（实测 2026-11-23）：
        - 已入库的直播    -> detail.resources[].url
        - 未入库但打开过   -> 工具日志里的 liveCdnUrl（不用点下载）
        - 从没打开过      -> 三条都没有，只能提示回微信打开一次
        """
        targets = self._fetch_selected_items()
        if not targets:
            messagebox.showinfo("请先选择", "请在列表里选中要解析的条目。")
            return

        it = targets[0]
        if not getattr(it, "is_live", False):
            # 普通回放本来就有直链，直接走原有的 HEAD 解析
            self._fetch_probe_selected()
            return

        from . import fetch as fetch_mod

        api = self.fetch_api_var.get().strip() or "http://127.0.0.1:2022"
        self._fetch_set_state(it.content_id, "取址中…")
        self._log(f"[解析] 《{it.title}》是直播，正在取流地址…")

        def work() -> None:
            try:
                url, src, why = fetch_mod.resolve_playable_stream(it, api=api)
            except Exception as exc:  # noqa: BLE001
                msg = str(exc)
                self._ui(lambda: self._fetch_set_state(it.content_id, "取不到地址"))
                self._log(f"[解析] 失败：{msg}")
                self._ui(lambda m=msg: messagebox.showwarning("解析失败", m))
                return

            self._ui(lambda: self._fetch_set_state(
                it.content_id, "可录制（含音轨）"))
            self._log(f"[解析] 《{it.title}》OK（{src}，{why}）")
            short = url.split("?", 1)[0]
            self._ui(lambda s2=src, w=why, u=short: messagebox.showinfo(
                "解析成功",
                f"《{it.title}》\n\n"
                f"地址来源：{s2}\n"
                f"探测结果：{w}\n\n"
                f"流地址（去掉签名参数）：\n{u}\n\n"
                f"要录的话点「推送选中项到 NAS」，本机可以关掉。"))

        self._run_bg(work)

    def _fetch_push_selected_to_nas(self) -> None:
        """把列表里选中的条目推给 NAS 拉流。

        直播项（url 为空）会**自动取址**：不用去微信重新点工具的下载按钮，
        地址要么在工具详情里，要么已经躺在工具日志里（实测在微信里打开
        直播间那一刻就记上了）。取到后还会探一次能不能真拉，避免把一个
        「主播已下播」的过期地址交给 NAS，让它空转半小时。
        """
        targets = self._fetch_selected_items()
        if not targets:
            messagebox.showinfo("请先选择", "请在列表里选中要推给 NAS 的条目。")
            return
        # 直播优先：同时选中回放和直播时，用户要的显然是那场正在播的
        lives = [it for it in targets if getattr(it, "is_live", False)]
        if lives:
            it = lives[0]
        else:
            it = targets[0]
        if len(targets) > 1 and not lives:
            if not messagebox.askyesno(
                    "推送多条",
                    f"选中了 {len(targets)} 条，只推送第一条《{it.title}》。继续？"):
                return

        if not getattr(it, "is_live", False):
            self._fetch_nas_push_video(it)
            return

        # ---- 直播：先取一个当下真的能拉的地址 ----
        from . import fetch as F

        api = self.fetch_api_var.get().strip() or "http://127.0.0.1:2022"
        self._fetch_nas_set_status("直播取址中…")
        self._log(f"[NAS] 《{it.title}》是直播，正在自动取流地址…")

        def work() -> None:
            try:
                url, src, why = F.resolve_playable_stream(it, api=api)
            except Exception as exc:  # noqa: BLE001
                msg = str(exc)
                self._ui(lambda: self._fetch_nas_set_status("取址失败", warn=True))
                self._log(f"[NAS] 取址失败：{msg}")
                self._ui(lambda m=msg: messagebox.showwarning(
                    "这个直播取不到可用的流地址", m))
                return
            self._log(f"[NAS] 取址成功（{src}，{why}）")
            self._ui(lambda s=src, u=url: (
                self._fetch_nas_set_status(f"已取址（{s}）"),
                self._fetch_nas_push(u, it.title),
            ))

        self._run_bg(work)

    # ---------- 搜索 ----------

    def _fetch_on_search_typed(self, *args) -> None:
        """输入停顿 400ms 才真正查，避免每敲一个字就压一次服务端 LIKE。"""
        if self._fetch_search_after is not None:
            try:
                self.after_cancel(self._fetch_search_after)
            except Exception:  # noqa: BLE001
                pass
        try:
            self._fetch_search_after = self.after(400, self._fetch_refresh)
        except Exception:  # noqa: BLE001
            self._fetch_search_after = None

    def _fetch_clear_search(self) -> None:
        self.fetch_search_var.set("")
        self._fetch_page = 1
        self._fetch_refresh()

    # ---------- 系统代理（防断网）----------

    def _fetch_refresh_proxy(self) -> None:
        """读一次系统代理状态，写进状态条。

        ``read()`` 走 winreg 直读（~1ms、零子进程），``describe`` 与
        ``diagnose`` 共用同一份状态，所以整轮只开一次注册表项。
        仍然丢给后台线程，纯粹是为了不跟 Tk 主线程抢那 1ms。
        """
        from . import systemproxy

        def work() -> None:
            try:
                st = systemproxy.read()
                text = systemproxy.describe(st)
                diag = systemproxy.diagnose(st)
            except Exception as exc:  # noqa: BLE001
                text = f"系统代理：读取失败（{type(exc).__name__}）"
                diag = ""
            self._ui(lambda t=text, d=diag: self._fetch_apply_proxy(t, d))

        self._run_bg(work)

    def _fetch_apply_proxy(self, text: str, diag: str) -> None:
        """把状态与配色落到界面上。危险状态用橙色，抓流接管中用绿色。"""
        from . import systemproxy

        self.fetch_proxy_var.set(text)
        try:
            if diag.startswith("⚠"):
                # 警告：代理指着没人监听的端口，已经在断网了
                self.fetch_proxy_label.configure(style="Warn.TLabel")
                self._log("[代理] " + diag)
            elif systemproxy.TOOL_PROXY in text:
                self.fetch_proxy_label.configure(style="Ok.TLabel")
            else:
                self.fetch_proxy_label.configure(style="Hint.TLabel")
            # 任何状态下都允许点还原 —— 工具接管中也可能想主动收工
            self.fetch_proxy_btn.configure(state="normal")
        except Exception:  # noqa: BLE001
            pass

    def _fetch_takeover_proxy(self) -> None:
        """把系统代理切到 127.0.0.1:2023（抓流端口）；工具没跑时引导直接重启。"""
        from . import systemproxy

        def work() -> None:
            if not systemproxy.tool_still_running():
                def ask_start() -> None:
                    if messagebox.askyesno(
                            "抓流工具未就绪",
                            "2023 抓流端口当前没有监听。\n\n"
                            "要现在直接启动/重启抓流工具并接管系统代理吗？"):
                        self._fetch_restart_tool()
                self._ui(ask_start)
                return

            ok, detail = systemproxy.enable_tool_proxy()
            self._log("[代理] " + detail)
            if ok:
                self._ui(lambda d=detail: (
                    self._fetch_refresh_proxy(),
                    messagebox.showinfo(
                        "已接管代理",
                        d + "\n\n如果微信里的直播/回放刚才已经打开，"
                        "请先关闭再重新打开停留 3-5 秒，然后点「刷新列表」。"),
                ))
            else:
                self._ui(lambda d=detail: (
                    self._fetch_refresh_proxy(),
                    messagebox.showwarning("切换失败", d),
                ))

        self._run_bg(work)

    def _fetch_restart_tool(self) -> None:
        """一键重启抓流工具（无需命令行），自动恢复 2023 系统代理接管。"""
        from . import fetch as F

        api = self.fetch_api_var.get().strip() or "http://127.0.0.1:2022"
        self.fetch_restart_tool_btn.configure(state="disabled")
        self.fetch_proxy_var.set("抓流工具重启中，请稍候…")
        self._log("[代理] 正在重启抓流工具…")

        def work() -> None:
            try:
                ok, detail = F.restart_wxcd(api=api)
            except Exception as exc:  # noqa: BLE001
                ok, detail = False, f"{type(exc).__name__}: {exc}"

            def done() -> None:
                self.fetch_restart_tool_btn.configure(state="normal")
                self._fetch_refresh_proxy()
                self._log("[代理] " + detail)
                if ok:
                    messagebox.showinfo(
                        "抓流工具已就绪",
                        detail + "\n\n请在微信里重新打开要录的直播或回放"
                        "（停留 3-5 秒），再回来点「刷新列表」。")
                else:
                    messagebox.showwarning("重启失败", detail)

            self._ui(done)

        self._run_bg(work)

    def _fetch_proxy_poll(self) -> None:
        """每 60 秒自检一次。

        抓流工具可能在后台被关掉/崩溃，而系统代理还指着它的 2023 端口 ——
        这时用户会突然发现"上不了网"，却不知道原因。
        定时检查能在 1 分钟内主动发现并提示。

        两个省电/省事的细节：
        - **窗口不可见时跳过自检**（最小化到托盘、被隐藏）。用户看不见状态条，
          查了也没人看；等窗口重新显示时立刻补一次。
        - 自检本身零子进程（见 ``systemproxy`` 模块文档），所以就算每分钟都跑
          也不会有黑框闪烁 —— 这里跳过只是省那 1ms 和一次后台线程。
        """
        try:
            if self._fetch_view_visible():
                self._fetch_refresh_proxy()
            self._fetch_proxy_after = self.after(60000, self._fetch_proxy_poll)
        except Exception:  # noqa: BLE001
            self._fetch_proxy_after = None

    def _fetch_view_visible(self) -> bool:
        """当前下载页是否真的显示在屏幕上。判断不出来时按"可见"处理。"""
        try:
            return bool(self.winfo_viewable())
        except Exception:  # noqa: BLE001
            return True

    def _fetch_restore_proxy(self) -> None:
        """一键把系统代理还原成本机固定出口 10808。"""
        from . import systemproxy

        st = systemproxy.read()
        if st["enabled"] and st["server"] == systemproxy.FIXED_PROXY:
            messagebox.showinfo("无需还原",
                                "系统代理已经是 " + systemproxy.FIXED_PROXY)
            self._fetch_refresh_proxy()
            return

        tip = ""
        took = systemproxy.TOOL_PROXY in (st["server"] or "")
        if took and systemproxy.tool_still_running():
            tip = ("\n\n注意：抓流工具还在运行。还原后微信流量不再经过它，"
                   "会抓不到新内容；要继续抓流需重新让工具接管代理。")
        cur = st["server"] or "(空)"
        msg = ("当前：" + cur + "\n还原为：" + systemproxy.FIXED_PROXY + tip)
        if not messagebox.askyesno("还原系统代理", msg):
            return

        ok, detail = systemproxy.restore_fixed()
        self._log("[代理] " + detail)
        if ok:
            messagebox.showinfo("已还原", detail)
        else:
            messagebox.showwarning(
                "还原失败",
                detail + "\n\n可以手动在「设置 → 网络和 Internet → 代理」里改回 "
                + systemproxy.FIXED_PROXY)
        self._fetch_refresh_proxy()

    # ---------- 体检 ----------

    def _fetch_check_health(self) -> None:
        """探一下工具是否在跑、代理/API 状态、版本。

        借鉴 gui8912 的「体检」：缺什么就直接说缺什么，
        而不是等下载失败再让人猜。
        """
        api = self.fetch_api_var.get().strip() or "http://127.0.0.1:2022"
        self._run_bg(lambda: self._fetch_health_worker(api))

    def _fetch_health_worker(self, api: str) -> None:
        from . import fetch as F
        from . import systemproxy

        try:
            data = F._api_get(api, "/api/status")
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            self._ui(lambda m=msg: self._fetch_show_health_fail(m))
            return
        info = data.get("data") or {}
        api_part = info.get("api") or {}
        proxy = info.get("proxy") or {}
        version = info.get("version") or "?"
        srv = info.get("server_statuses") or {}

        st = systemproxy.read()
        proxy_listening = bool(proxy.get("listening")) or systemproxy.tool_still_running()
        diag = systemproxy.diagnose(st)
        taking_over = bool(st.get("enabled")) and systemproxy.TOOL_PROXY in (st.get("server") or "")

        text = (
            f"抓流工具版本：{version}\n"
            f"API   {api_part.get('addr', api)} -> {api_part.get('status', '?')}\n"
            f"抓流端口 {proxy.get('addr', '?')} -> "
            f"{proxy.get('status') or ('listening' if proxy_listening else '?')}\n"
            f"系统代理：{diag}\n"
            f"服务  {', '.join(f'{k}={v}' for k, v in srv.items()) or '?'}"
        )

        if not proxy_listening:
            reason = "抓流工具的 2023 代理端口未监听，建议点「重启抓流工具」。"
            self._ui(lambda t=text, r=reason: self._fetch_show_health_warn(t, r))
            return
        if not taking_over:
            reason = (
                "抓流工具在运行，但当前 Windows 系统代理没有指向 127.0.0.1:2023，"
                "微信流量会被直接旁路，无法解析出直播或回放。\n"
                "请点上方「接管代理(抓流)」后再在微信里重新打开直播间。"
            )
            self._ui(lambda t=text, r=reason: self._fetch_show_health_warn(t, r))
            return

        self._ui(lambda t=text: self._fetch_show_health_ok(t))

    def _fetch_show_health_ok(self, text: str) -> None:
        self._log("[体检] " + text.replace("\n", " | "))
        messagebox.showinfo("体检通过", text)

    def _fetch_show_health_warn(self, text: str, reason: str) -> None:
        self._log("[体检] 未通过：" + reason.replace("\n", " "))
        messagebox.showwarning("体检未通过：当前抓不到流", text + "\n\n问题：\n" + reason)

    def _fetch_show_health_fail(self, err: str) -> None:
        self._log(f"[体检] 失败：{err}")
        messagebox.showwarning(
            "工具没在运行",
            "连不上抓流工具。\n\n"
            "请尝试：\n"
            "1. 直接点页面上的「重启抓流工具」按钮（会自动启动并接管代理）\n"
            "2. 或确认 tools/wxcd 下的 wx_video_download.exe 正在运行\n"
            f"3. API 地址是否正确：{self.fetch_api_var.get()}\n\n"
            f"原始错误：{err}")

    # ---------- 刷新列表 ----------

    def _fetch_refresh(self) -> None:
        if self._fetch_busy:
            return
        api = self.fetch_api_var.get().strip() or "http://127.0.0.1:2022"
        keyword = self.fetch_search_var.get().strip()
        try:
            size = max(1, int(self.fetch_page_size_var.get()))
        except (TypeError, ValueError):
            size = PAGE_SIZE
        page = max(1, int(self._fetch_page or 1))
        self.fetch_hint_var.set("正在读取…")
        self._run_bg(lambda: self._fetch_refresh_worker(api, keyword, page, size))

    def _fetch_refresh_worker(self, api: str, keyword: str,
                              page: int, size: int) -> None:
        from . import fetch as F

        # 直播发现不能只靠 /api/content/list：content 表里**只有点过下载/
        # 打开过的直播**。用户只是打开直播间看、没点下载按钮，那条直播就
        # 根本不在列表里（实测 2026-11-23：正在播的"英语大招方法技巧"
        # 完全查不到）。但它已经躺在工具日志里，那条路不依赖任何点击。
        #
        # 发现后立刻并行探活，只把真能拉的交给 UI。串行实测要 17.8 秒
        # （每条失效地址都等满超时），并行后 ≈ 最慢一条。
        found_lives = []
        try:
            cands = F.discover_live_streams(api)[:8]
        except Exception:  # noqa: BLE001
            cands = []

        kw_low = (keyword or "").strip().lower()
        if kw_low:
            cands = [c for c in cands
                     if kw_low in c.title.lower() or kw_low in c.author.lower()]

        if cands:
            from concurrent.futures import ThreadPoolExecutor

            def _probe(cand):
                try:
                    return cand, F.probe_stream(cand.url, timeout=6)
                except Exception as exc:  # noqa: BLE001
                    return cand, (False, type(exc).__name__)

            with ThreadPoolExecutor(max_workers=min(8, len(cands))) as pool:
                for cand, (ok, why) in pool.map(_probe, cands):
                    if ok:
                        found_lives.append(cand)
                    else:
                        self._log(f"[直播] 跳过《{cand.title}》：{why}")

        found_videos = []
        try:
            vcands = F.discover_feed_videos(api)[:30]
            if kw_low:
                vcands = [v for v in vcands
                          if kw_low in v.title.lower() or kw_low in v.author.lower()]
            found_videos = vcands
        except Exception:  # noqa: BLE001
            found_videos = []

        try:
            result = F.list_page(api, keyword=keyword,
                                 limit=size, offset=(page - 1) * size)
        except F.FetchError as exc:
            # 注意：except 块结束会 del exc，lambda 若引用它会在稍后执行时
            # 抛 "cannot access free variable"。用默认参数即时绑定值。
            msg = str(exc)
            self._ui(lambda m=msg: self._fetch_refresh_done(None, m))
            return
        except Exception as exc:  # noqa: BLE001
            msg = f"{type(exc).__name__}: {exc}"
            self._ui(lambda m=msg: self._fetch_refresh_done(None, m))
            return
        if result is not None:
            if found_lives:
                result.discovered_lives = found_lives
            if found_videos:
                result.discovered_videos = found_videos
        self._ui(lambda: self._fetch_refresh_done(result, ""))

    def _fetch_refresh_done(self, result, err: str) -> None:
        from . import fetch as F

        for row in self.fetch_tree.get_children():
            self.fetch_tree.delete(row)
        self._fetch_items.clear()

        if err:
            self.fetch_hint_var.set("读取失败")
            self._log(f"[下载] 读取列表失败：{err}")
            self._fetch_show_health_fail(err)
            return

        items = result.items if result else []
        self._fetch_total = result.total if result else 0

        # 本侧视图过滤：隐藏 / 只看未下载
        state = self._fetch_state
        hidden = fetchstate.hidden_ids(state)
        done_ids = fetchstate.downloaded_ids(state)
        show_hidden = self.fetch_show_hidden_var.get()
        only_new = self.fetch_only_new_var.get()

        # ---- 先渲染"从日志发现的直播" ----
        # 这些直播不在工具 content 表里（用户没点过下载），但地址已经拿到、
        # 而且刚在后台探活确认能拉。放在最前面，因为用户刷新的动机通常就是它。
        discovered = list(getattr(result, "discovered_lives", None) or [])
        in_page = {it.content_id for it in items}
        in_page_media = {F.url_media_key(it.url) for it in items if it.url}
        fresh = []
        for it in discovered:
            if it.live_id in in_page or it.content_id in in_page:
                continue          # 已经在上面的列表里，别重复
            is_hidden = it.content_id in hidden
            if is_hidden and not show_hidden:
                continue
            if only_new and it.content_id in done_ids:
                continue
            self._fetch_add_row(it, is_hidden, discovered=True)
            fresh.append(it)
            in_page.add(it.content_id)
        shown_discovered = len(fresh)

        # ---- 再渲染"从日志发现的视频 / 直播回放" ----
        discovered_videos = list(getattr(result, "discovered_videos", None) or [])
        fresh_videos = []
        for it in discovered_videos:
            mkey = F.url_media_key(it.url)
            if it.content_id in in_page or (mkey and mkey in in_page_media):
                continue
            is_hidden = it.content_id in hidden
            if is_hidden and not show_hidden:
                continue
            if only_new and it.content_id in done_ids:
                continue
            self._fetch_add_row(it, is_hidden, discovered=True)
            fresh_videos.append(it)
            in_page.add(it.content_id)
            if mkey:
                in_page_media.add(mkey)
        shown_discovered_videos = len(fresh_videos)

        shown = 0
        hidden_here = 0
        for it in items:
            is_hidden = it.content_id in hidden
            if is_hidden:
                hidden_here += 1
                if not show_hidden:
                    continue
            if only_new and it.content_id in done_ids:
                continue
            self._fetch_add_row(it, is_hidden)
            shown += 1

        # 页码状态
        try:
            size = max(1, int(result.page_size or len(items) or 1))
        except (TypeError, ValueError):
            size = max(1, len(items) or 1)
        pages = max(1, (self._fetch_total + size - 1) // size)
        self.fetch_page_var.set(f"第 {self._fetch_page} / {pages} 页")
        self.fetch_prev_btn.configure(
            state="normal" if self._fetch_page > 1 else "disabled")
        self.fetch_next_btn.configure(
            state="normal" if self._fetch_page < pages else "disabled")

        bits = [f"共 {self._fetch_total} 条"]
        if shown_discovered:
            bits.append(f"{shown_discovered} 场直播是从日志发现的")
        if shown_discovered_videos:
            bits.append(f"{shown_discovered_videos} 条视频/回放是从日志发现的")
        if shown != len(items):
            bits.append(f"本页显示 {shown}/{len(items)}")
        if hidden_here and not show_hidden:
            bits.append(f"其中 {hidden_here} 条已删除（隐藏）")
        n_done = sum(1 for it in items if it.content_id in done_ids)
        if n_done:
            bits.append(f"{n_done} 条已下载")
        self.fetch_hint_var.set("，".join(bits))

        total_shown = shown + shown_discovered + shown_discovered_videos
        if not items and not discovered and not discovered_videos:
            if self.fetch_search_var.get().strip():
                self._log(f"[下载] 搜索「{self.fetch_search_var.get()}」无结果")
            else:
                self._log("[下载] 工具里还没有捕获到内容")
                messagebox.showinfo(
                    "还没有内容",
                    "工具里还没有捕获到任何视频。\n\n"
                    "请先确认上方系统代理处于「127.0.0.1:2023（抓流工具接管中）」，\n"
                    "然后在微信 PC 端重新打开目标直播或回放，再点「刷新列表」。")
        elif total_shown == 0:
            # 有数据但全被过滤掉了 —— 必须说清楚，否则用户以为坏了
            self._log("[下载] 有内容但被视图过滤掉了")
            messagebox.showinfo(
                "都被过滤掉了",
                f"本页读到 {len(items)} 条，但都被当前视图过滤掉了。\n\n"
                "· 勾选「显示已删除的」可以看回被删除的条目\n"
                "· 取消「只看未下载的」可以看已下载过的条目")

    def _fetch_add_row(self, it, is_hidden: bool,
                       discovered: bool = False) -> None:
        """插入一行。状态列区分「直播中」与「待下载/已下载」。

        直播为什么单列状态：直播项在工具库里 url 恒为空，点「下载」既下不了
        （没有文件），也不该下（要的是推给 NAS 长时间录）。以前这类条目被
        整个过滤掉，界面里根本看不到正在播的直播。
        """
        rec = fetchstate.downloaded_info(self._fetch_state, it.content_id)
        if is_hidden:
            state_text = "已删除（隐藏）"
            tags = ("hidden",)
        elif getattr(it, "is_live", False):
            if discovered:
                # 与"已入库的直播"区分：这条没点过下载，地址是从工具日志
                # 里取的。看到这个就知道"不用点下载也能推 NAS"。
                state_text = "直播中（日志发现）→ 推 NAS"
            else:
                state_text = "直播中 → 推 NAS"
            tags = ("live",)
        elif rec:
            dur = float(rec.get("duration") or 0.0)
            state_text = f"已下载 {dur/60:.0f} 分钟" if dur else "已下载"
            tags = ("done",)
        elif discovered:
            state_text = "待下载（日志发现）"
            tags = ()
        else:
            state_text = "待下载"
            tags = ()
        self.fetch_tree.insert("", "end",
                               values=(it.title, it.author or "—",
                                       it.publish_text or "—", state_text),
                               tags=tags)
        self._fetch_items[it.content_id] = it

    # ---------- 翻页 ----------

    def _fetch_prev_page(self) -> None:
        if self._fetch_page <= 1:
            return
        self._fetch_page -= 1
        self._fetch_refresh()

    def _fetch_next_page(self) -> None:
        self._fetch_page += 1
        self._fetch_refresh()

    # ---------- 选择 ----------

    def _fetch_select_all(self, event=None):
        self.fetch_tree.selection_set(self.fetch_tree.get_children())
        return "break"

    def _fetch_invert_selection(self) -> None:
        all_rows = set(self.fetch_tree.get_children())
        cur = set(self.fetch_tree.selection())
        self.fetch_tree.selection_set(list(all_rows - cur))

    def _fetch_row_cid(self, iid: str) -> str | None:
        """行 iid → content_id。iid 与 cid 的对应由 _fetch_items 保证。"""
        try:
            vals = self.fetch_tree.item(iid, "values")
        except tk.TclError:
            return None
        if len(vals) < 4:
            return None
        for cid, it in self._fetch_items.items():
            if (it.title == vals[0] and (it.author or "—") == vals[1]
                    and (it.publish_text or "—") == vals[2]):
                return cid
        return None

    def _fetch_selected_items(self) -> list:
        """选中行 → ContentItem 列表（按列表顺序）。"""
        out = []
        for iid in self.fetch_tree.selection():
            cid = self._fetch_row_cid(iid)
            if cid and cid in self._fetch_items:
                out.append(self._fetch_items[cid])
        return out

    def _fetch_selected_ids(self) -> list:
        return [it.content_id for it in self._fetch_selected_items()]

    # ---------- 解析（是否获取解析）----------

    def _fetch_probe_selected(self) -> None:
        """对选中项做一次 HEAD 解析，把结论写进状态列 + 日志。"""
        if self._fetch_busy:
            messagebox.showinfo("正在忙", "上一批还没处理完，请稍候。")
            return
        targets = self._fetch_selected_items()
        if not targets:
            messagebox.showinfo("请先选择", "请在列表里选中要解析的条目（可多选）。")
            return
        self._fetch_busy = True
        self.fetch_start_btn.configure(state="disabled")
        self._run_bg(lambda: self._fetch_probe_worker(targets))

    def _fetch_probe_worker(self, targets) -> None:
        from . import fetch as F

        total = len(targets)
        ok = 0
        for idx, it in enumerate(targets, 1):
            self._ui(lambda i=it.content_id, s="解析中…": self._fetch_set_state(i, s))
            try:
                info = F.probe_item(it)
            except Exception as exc:  # noqa: BLE001
                msg = f"{type(exc).__name__}: {exc}"
                self._log(f"[解析] [{idx}/{total}] 《{it.title}》异常：{msg}")
                self._ui(lambda i=it.content_id: self._fetch_set_state(i, "解析异常"))
                continue

            if info.ok:
                ok += 1
                summary = info.summary
                self._ui(lambda i=it.content_id, s=f"可下载 {summary}":
                         self._fetch_set_state(i, s))
                self._log(f"[解析] [{idx}/{total}] 《{it.title}》 {summary}"
                          f"（HTTP {info.status}）")
            else:
                self._ui(lambda i=it.content_id: self._fetch_set_state(i, "地址不可用"))
                self._log(f"[解析] [{idx}/{total}] 《{it.title}》 失败：{info.reason}")

        self._ui(lambda: self._fetch_probe_finish(ok, total))

    def _fetch_probe_finish(self, ok: int, total: int) -> None:
        self._fetch_busy = False
        self.fetch_start_btn.configure(state="normal")
        self._log(f"[解析] 完成 {ok}/{total} 条可用")
        if ok < total:
            messagebox.showwarning(
                "部分地址不可用",
                f"{total - ok} 条解析失败。\n\n"
                "常见原因：地址有时效（带 sign/svrnonce），"
                "过期后需要在微信里重新打开该视频，再回来「刷新列表」。")

    # ---------- 删除 / 清空 / 恢复 ----------

    def _fetch_delete_selected(self) -> None:
        """把选中项从列表里删掉（本侧隐藏，不动物具数据库）。"""
        items = self._fetch_selected_items()
        if not items:
            messagebox.showinfo("请先选择", "请在列表里选中要删除的条目（可多选）。")
            return

        # 已经在"显示已删除"视图里选中隐藏条目时，删除就等于恢复，容易搞混
        hidden = fetchstate.hidden_ids(self._fetch_state)
        if items and all(it.content_id in hidden for it in items):
            if messagebox.askyesno(
                    "这些已经是删除状态",
                    f"选中的 {len(items)} 条都已被删除（隐藏）。\n\n"
                    "要把它们恢复显示吗？"):
                self._fetch_restore_ids([it.content_id for it in items])
            return

        n = len(items)
        if not messagebox.askyesno(
                "删除选中条目",
                f"从列表隐藏 {n} 条？\n\n"
                "· 只影响本工具的列表显示\n"
                "· 不会删除抓流工具里的记录，也不会删磁盘上的文件\n"
                "· 随时可以点「恢复被删除的」找回"):
            return

        ids = [it.content_id for it in items]
        titles = {it.content_id: it.title for it in items}
        added = fetchstate.hide(self._fetch_state, ids, titles)
        fetchstate.save_state(self._fetch_state)
        self._log(f"[下载] 隐藏 {added} 条（重复的 {n - added} 条已忽略）")
        self._fetch_refresh()

    def _fetch_clear_all(self) -> None:
        """清空当前列表视图。两种语义要让用户明确选。"""
        shown = dict(self._fetch_items)
        if not shown:
            messagebox.showinfo("列表是空的", "当前没有可清空的条目。")
            return

        ans = messagebox.askyesnocancel(
            "清空列表",
            f"当前列表有 {len(shown)} 条。要怎么做？\n\n"
            "「是」= 全部从列表里删除（隐藏，可恢复）\n"
            "「否」= 只清掉「已下载」标记（列表长度不变）\n"
            "「取消」= 什么都不做")
        if ans is None:
            return
        if ans:
            titles = {cid: it.title for cid, it in shown.items()}
            added = fetchstate.hide(self._fetch_state, list(shown.keys()), titles)
            fetchstate.save_state(self._fetch_state)
            self._log(f"[下载] 清空列表：隐藏 {added} 条")
        else:
            n = fetchstate.clear_downloaded(self._fetch_state)
            fetchstate.save_state(self._fetch_state)
            self._log(f"[下载] 清掉 {n} 条已下载标记（文件未删）")
        self._fetch_refresh()

    def _fetch_restore_hidden(self) -> None:
        n = fetchstate.hidden_count(self._fetch_state)
        if not n:
            messagebox.showinfo("没有可恢复的", "当前没有被删除（隐藏）的条目。")
            return
        if not messagebox.askyesno(
                "恢复被删除的条目",
                f"恢复全部 {n} 条隐藏条目？\n\n它们会重新出现在列表里。"):
            return
        self._fetch_restore_ids(None)

    def _fetch_restore_ids(self, ids) -> None:
        if ids is None:
            n = fetchstate.unhide_all(self._fetch_state)
            self._log(f"[下载] 恢复全部隐藏：{n} 条")
        else:
            bucket = self._fetch_state.setdefault("hidden", {})
            n = 0
            for cid in ids:
                if bucket.pop(str(cid), None) is not None:
                    n += 1
            self._log(f"[下载] 恢复指定隐藏：{n} 条")
        fetchstate.save_state(self._fetch_state)
        self._fetch_refresh()

    # ---------- 下载 ----------

    def _fetch_start(self) -> None:
        if self._fetch_busy:
            messagebox.showinfo("正在忙", "上一批还没处理完，请稍候。")
            return
        targets = self._fetch_selected_items()
        if not targets:
            messagebox.showinfo("请先选择", "请在列表里选中要下载的条目（可多选）。")
            return

        # 直播不能"下载"：它没有静态文件，只有一个还在增长的流。
        # 硬下的话要么下到空文件，要么下到半场就断。
        # 正确动作是把地址交给 NAS 挂着录 —— 直接在这里引导过去。
        lives = [it for it in targets if getattr(it, "is_live", False)]
        if lives:
            rest = len(targets) - len(lives)
            tail = f"\n\n（另外 {rest} 条回放会照常下载）" if rest else ""
            if not messagebox.askyesno(
                    "其中包含直播",
                    f"选中的 {len(lives)} 条是**正在直播**，没有可直接下载的文件。\n\n"
                    "要录直播请用「推送选中项到 NAS」：本机可以关掉，NAS 挂着录，"
                    "停录后自动转写。" + tail + "\n\n先把直播跳过，继续下载其余回放吗？"):
                return
            targets = [it for it in targets if not getattr(it, "is_live", False)]
            if not targets:
                return

        api = self.fetch_api_var.get().strip() or "http://127.0.0.1:2022"
        keep_video = self.fetch_keep_video_var.get()
        auto_trans = self.fetch_auto_trans_var.get()
        probe_first = self.fetch_probe_first_var.get()

        self._fetch_busy = True
        self._fetch_cancel_clear()
        self.fetch_start_btn.configure(state="disabled")
        self.fetch_cancel_btn.configure(state="normal")
        rows = [(it.content_id, it) for it in targets]
        self._run_bg(lambda: self._fetch_worker(rows, api, keep_video,
                                                auto_trans, probe_first))

    def _fetch_cancel_clear(self) -> None:
        self._fetch_cancel.clear()

    def _fetch_abort(self) -> None:
        """中止：置位后 worker 在下一个可打断点退出。

        不能硬杀线程（会把 .part 文件留成半截），所以在循环里检查标志位。
        """
        if not self._fetch_busy:
            return
        self._fetch_cancel.set()
        self._log("[下载] 已请求中止，等当前这一条收尾…")
        self.fetch_cancel_btn.configure(state="disabled")

    def _fetch_worker(self, rows, api: str, keep_video: bool,
                      auto_trans: bool, probe_first: bool = True) -> None:
        from . import fetch as F
        from .config import RECORDINGS_DIR, ensure_dirs

        ensure_dirs()
        total = len(rows)
        done_ok = 0
        last_wav = None

        for idx, (cid, item) in enumerate(rows, 1):
            if self._fetch_cancel.is_set():
                self._log(f"[下载] 在第 {idx} 条前中止")
                break

            prefix = f"[{idx}/{total}] 《{item.title}》"
            self._ui(lambda i=cid, s="处理中…": self._fetch_set_state(i, s))
            self._log(f"{prefix} 开始")

            try:
                # 解析先行：几十毫秒就能发现"地址过期"，不用白下一遍
                if probe_first:
                    info = F.probe_item(item)
                    if not info.ok:
                        raise F.FetchError(f"解析失败：{info.reason}")
                    self._log(f"{prefix} 解析 {info.summary}")

                key = F.get_decrypt_key(item, api)
                flag, enc_len = F.probe_encryption(item.url)
                self._log(f"{prefix} 加密 flag={flag} enc_len={enc_len} "
                          f"key={'已取到' if key else '无'}")
                if flag and not key:
                    raise F.FetchError("文件已加密但拿不到密钥")

                stem = F.safe_filename(item.title)
                base = Path(RECORDINGS_DIR)
                video = F.unique_path(base / (stem + ".mp4"))
                # 同名 WAV 也要避让，否则会把上一场的音频覆盖掉
                wav_target = F.unique_path(base / (video.stem + ".wav"))

                def on_prog(done, secs, _ix=idx):
                    mb = done / 1048576
                    rate = mb / max(1e-6, secs)
                    self._ui(lambda: self._fetch_set_progress(_ix, total, mb, rate))

                video = F.download_decrypted(item, key, video, on_progress=on_prog)
                self._log(f"{prefix} 下载完成 {video.stat().st_size/1048576:.1f} MB")

                if self._fetch_cancel.is_set():
                    # 已经下完了就把它转成音频再收工，别浪费这几十 MB
                    self._log(f"{prefix} 已请求中止，本条仍会转出音频后停止")

                wav = F.extract_audio(video, wav_target)
                if not keep_video:
                    video.unlink(missing_ok=True)
                    self._log(f"{prefix} 已删除视频，保留音频")

                dur, has_audio = F.probe_media(wav)
                self._log(f"{prefix} 音频就绪 {dur/60:.1f} 分钟 "
                          f"({wav.stat().st_size/1048576:.1f} MB) 含音轨={has_audio}")
                if not has_audio:
                    self._log(f"{prefix} 警告：抽出的文件没有音轨，可能不是直播回放")

                # 记账：下次刷新这条会显示"已下载"
                try:
                    fetchstate.mark_downloaded(
                        self._fetch_state, cid, title=item.title,
                        path=str(wav), duration=dur,
                        size=wav.stat().st_size if wav.exists() else 0)
                    fetchstate.save_state(self._fetch_state)
                except Exception as exc:  # noqa: BLE001
                    self._log(f"{prefix} 记账失败（不影响文件）：{exc}")

                self._ui(lambda i=cid, d=dur: self._fetch_set_state(
                    i, f"完成 {d/60:.0f} 分钟"))
                done_ok += 1
                last_wav = wav

            except Exception as exc:  # noqa: BLE001
                self._log(f"{prefix} 失败：{exc}")
                self._ui(lambda i=cid: self._fetch_set_state(i, "失败"))

        self._ui(lambda: self._fetch_finish(done_ok, total, last_wav, auto_trans))

    def _fetch_finish(self, ok: int, total: int, wav, auto_trans: bool) -> None:
        cancelled = self._fetch_cancel.is_set()
        self._fetch_busy = False
        self._fetch_cancel.clear()
        self.fetch_start_btn.configure(state="normal")
        self.fetch_cancel_btn.configure(state="disabled")
        self.fetch_prog_var.set("")
        self.fetch_progress.stop()
        self.fetch_progress.configure(mode="determinate", value=0)
        self._log(f"[下载] {'中止' if cancelled else '完成'} {ok}/{total}")

        if auto_trans and wav and wav.exists():
            self._log("[下载] 转入转写流程…")
            try:
                self._start_transcribe_from_path(wav)
            except Exception as exc:  # noqa: BLE001
                self._log(f"[下载] 启动转写失败：{exc}")
        elif ok:
            messagebox.showinfo("下载完成", f"成功 {ok}/{total}。音频已保存到录制目录。")

    # ---------- 小工具 ----------

    def _fetch_set_state(self, cid, state: str) -> None:
        """按 content_id 更新状态列（表里可能有多行同名，靠 cid 定位）。"""
        try:
            item = self._fetch_items.get(str(cid))
            if item is None:
                return
            for iid in self.fetch_tree.get_children():
                vals = list(self.fetch_tree.item(iid, "values"))
                if len(vals) < 4:
                    continue
                if (vals[0] == item.title and vals[1] == (item.author or "—")
                        and vals[2] == (item.publish_text or "—")):
                    vals[3] = state
                    self.fetch_tree.item(iid, values=vals)
                    break
        except tk.TclError:
            pass

    def _fetch_set_progress(self, idx: int, total: int, mb: float, rate: float) -> None:
        self.fetch_prog_var.set(f"{idx}/{total}  {mb:.0f} MB  {rate:.1f} MB/s")
        if self.fetch_progress["mode"] != "indeterminate":
            self.fetch_progress.configure(mode="indeterminate")
            self.fetch_progress.start(40)

    def _fetch_open_dir(self) -> None:
        import os
        import sys

        from . import winproc
        from .config import RECORDINGS_DIR

        path = str(RECORDINGS_DIR)
        try:
            if sys.platform == "win32":
                os.startfile(path)  # noqa: S606
            elif sys.platform == "darwin":
                winproc.run(["open", path], check=False)
            else:
                winproc.run(["xdg-open", path], check=False)
        except Exception as exc:  # noqa: BLE001
            messagebox.showinfo("打不开", f"目录：{path}\n错误：{exc}")

    def _start_transcribe_from_path(self, wav) -> None:
        """复用宿主的转写流程：填好音频路径并触发起转。"""
        try:
            self.audio_path_var.set(str(wav))
        except Exception:  # noqa: BLE001
            pass
        self._start_transcribe()

    # ---------- 线程安全的主线程回投 ----------

    def _ui(self, fn) -> None:
        """后台线程调用：把动作塞进队列，由主线程执行。

        不要在后台线程直接 self.after()，tkinter 会抛
        "main thread is not in main loop"（隐藏窗口/无 mainloop 时必现）。
        """
        self._ui_queue.put(fn)

    def _pump_ui_queue(self) -> None:
        """主线程定时排空队列。"""
        try:
            for _ in range(64):
                fn = self._ui_queue.get_nowait()
                try:
                    fn()
                except Exception as exc:  # noqa: BLE001
                    self._last_ui_error = exc
                    try:
                        self._log(f"[下载] 界面更新失败：{exc!r}")
                    except Exception:  # noqa: BLE001
                        pass
        except queue.Empty:
            pass
        try:
            self._pump_after_id = self.after(60, self._pump_ui_queue)
        except Exception as exc:  # noqa: BLE001
            # 窗口正在销毁时会走到这里，不影响已有逻辑
            self._pump_after_id = None
            _ = exc

    def fetch_shutdown(self) -> None:
        """取消本页所有 after 定时器。

        为什么要这个：`after` 排下的回调会让 Tk 的事件循环**永远有事可做**。
        窗口 `destroy()` 后进程却不退出 —— 测试里表现为 pytest 跑完全部用例
        却卡住不结束（实测卡满 250 秒超时）。真实 GUI 里也会让 `pythonw`
        进程残留。所以关闭窗口时必须显式取消。
        """
        for attr in ("_fetch_proxy_after", "_fetch_search_after",
                     "_pump_after_id"):
            handle = getattr(self, attr, None)
            if not handle:
                continue
            try:
                self.after_cancel(handle)
            except Exception:  # noqa: BLE001
                pass
            try:
                setattr(self, attr, None)
            except Exception:  # noqa: BLE001
                pass
        # 让可能还在跑的后台线程尽快收工
        try:
            self._fetch_cancel.set()
        except Exception:  # noqa: BLE001
            pass

    def _run_bg(self, fn) -> None:
        """在后台线程跑 fn（宿主已有 worker 约定，这里保持一致）。"""
        self.worker = threading.Thread(target=fn, daemon=True)
        self.worker.start()
