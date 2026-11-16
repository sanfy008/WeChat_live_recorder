"""GUI 页签：视频号下载（自动解密）。

单独成一个 mixin 模块，避免 gui.py 继续膨胀。
App 继承本 mixin 后调用 `_build_fetch_tab()` 即可。

设计要点：
- 列表来自本地抓流工具的 /api/content/list，用户在微信里打开过的视频会自动出现
- 不需要点工具的下载按钮：工具的下载按钮只是它自己做 MP4+转MP3，
  我们要的是它已经捕获到的 url + key
- 下载全程后台线程，进度回主线程更新（tkinter 非线程安全）
"""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk


class FetchTabMixin:
    """需要宿主类提供：_log(str)、status_var、progress、_run_bg(fn)"""

    # ---------- 构建界面 ----------

    def _build_fetch_tab(self) -> None:
        f = self.tab_fetch

        # ===== 顶部：工具状态 + 刷新 =====
        top = ttk.Frame(f)
        top.pack(fill="x", padx=12, pady=(12, 6))

        ttk.Label(top, text="视频号下载", style="Title.TLabel").pack(side="left")

        self.fetch_api_var = tk.StringVar(value="http://127.0.0.1:2022")
        ttk.Label(top, text="  工具 API").pack(side="left")
        ttk.Entry(top, textvariable=self.fetch_api_var, width=24).pack(side="left", padx=4)

        self.fetch_refresh_btn = ttk.Button(top, text="刷新列表", command=self._fetch_refresh)
        self.fetch_refresh_btn.pack(side="right")
        self.fetch_hint_var = tk.StringVar(value="")
        ttk.Label(top, textvariable=self.fetch_hint_var, style="Hint.TLabel").pack(
            side="right", padx=(0, 10))

        # ===== 列表 =====
        mid = ttk.LabelFrame(f, text="已捕获的内容（在微信里打开过就会出现在这里）")
        mid.pack(fill="both", expand=True, padx=12, pady=6)

        cols = ("title", "cid", "state")
        self.fetch_tree = ttk.Treeview(mid, columns=cols, show="headings",
                                       selectmode="extended", height=9)
        self.fetch_tree.heading("title", text="标题")
        self.fetch_tree.heading("cid", text="内容 ID")
        self.fetch_tree.heading("state", text="状态")
        self.fetch_tree.column("title", width=420, anchor="w")
        self.fetch_tree.column("cid", width=200, anchor="w")
        self.fetch_tree.column("state", width=120, anchor="center")
        sb = ttk.Scrollbar(mid, orient="vertical", command=self.fetch_tree.yview)
        self.fetch_tree.configure(yscrollcommand=sb.set)
        self.fetch_tree.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=8)
        sb.pack(side="right", fill="y", padx=(0, 8), pady=8)

        # ===== 选项 =====
        opt = ttk.Frame(f)
        opt.pack(fill="x", padx=12, pady=(0, 6))

        self.fetch_keep_video_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(opt, text="保留视频文件（默认只留音频）",
                        variable=self.fetch_keep_video_var).pack(side="left")

        self.fetch_auto_trans_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(opt, text="下载完自动转写",
                        variable=self.fetch_auto_trans_var).pack(side="left", padx=(16, 0))

        # ===== 操作按钮 =====
        btns = ttk.Frame(f)
        btns.pack(fill="x", padx=12, pady=(0, 8))

        self.fetch_start_btn = ttk.Button(btns, text="下载选中项的音频",
                                          style="Run.TButton", command=self._fetch_start)
        self.fetch_start_btn.pack(side="left")

        ttk.Button(btns, text="打开录制目录",
                   command=self._fetch_open_dir).pack(side="left", padx=(10, 0))

        self.fetch_progress = ttk.Progressbar(btns, mode="determinate", length=260)
        self.fetch_progress.pack(side="right")
        self.fetch_prog_var = tk.StringVar(value="")
        ttk.Label(btns, textvariable=self.fetch_prog_var).pack(side="right", padx=(0, 8))

        # ===== 说明 =====
        note = ttk.LabelFrame(f, text="使用说明")
        note.pack(fill="x", padx=12, pady=(0, 12))
        txt = (
            "1. 先启动抓流工具（tools/wxcd 下用根命令启动，不要加 server 子命令）\n"
            "2. 在微信里打开目标回放/视频，工具会自动捕获地址与解密密钥\n"
            "3. 回到这里点「刷新列表」，选中目标，点「下载选中项的音频」\n"
            "4. 不需要点工具里的下载按钮 —— 那是它自己下 MP4 再转 MP3，我们不采用\n"
            "5. 视频号媒体前 128KB 是加密的，本工具会自动解密，无需手动处理"
        )
        ttk.Label(note, text=txt, justify="left", style="Hint.TLabel").pack(
            anchor="w", padx=10, pady=8)

        self._fetch_items: dict[str, object] = {}
        self._fetch_busy = False
        self._ui_queue: "queue.Queue" = queue.Queue()
        # 不能在这里直接调 _pump_ui_queue：窗口还没映射，after 可能注册失败，
        # 被吞掉后泵就永不再排程。用 after_idle 推迟到事件循环就绪后启动。
        try:
            self.after_idle(self._pump_ui_queue)
        except Exception:  # noqa: BLE001
            self._pump_ui_queue()

    # ---------- 刷新列表 ----------

    def _fetch_refresh(self) -> None:
        if self._fetch_busy:
            return
        api = self.fetch_api_var.get().strip() or "http://127.0.0.1:2022"
        self.fetch_hint_var.set("正在读取…")
        self._run_bg(lambda: self._fetch_refresh_worker(api))

    def _fetch_refresh_worker(self, api: str) -> None:
        from . import fetch as F

        try:
            items = F.list_contents(api)
        except F.FetchError as exc:
            # 注意：except 块结束会 del exc，lambda 若引用它会在稍后执行时
            # 抛 "cannot access free variable"。用默认参数即时绑定值。
            msg = str(exc)
            self._ui(lambda m=msg: self._fetch_refresh_done([], m))
            return
        except Exception as exc:  # noqa: BLE001
            msg = f"{type(exc).__name__}: {exc}"
            self._ui(lambda m=msg: self._fetch_refresh_done([], m))
            return
        self._ui(lambda: self._fetch_refresh_done(items, ""))

    def _fetch_refresh_done(self, items, err: str) -> None:
        for row in self.fetch_tree.get_children():
            self.fetch_tree.delete(row)
        self._fetch_items.clear()

        if err:
            self.fetch_hint_var.set("读取失败")
            self._log(f"[下载] 读取列表失败：{err}")
            messagebox.showwarning(
                "读取失败",
                "无法连接抓流工具。\n\n"
                "请确认：\n"
                "1. tools/wxcd 下的 wx_video_download.exe 正在运行\n"
                "2. 它是用根命令启动的（不是 server 子命令，那个会强制关掉代理）\n"
                f"3. API 地址是否正确：{self.fetch_api_var.get()}\n\n"
                f"原始错误：{err}")
            return

        for it in items:
            self.fetch_tree.insert("", "end", values=(it.title, it.content_id, "待下载"))
            self._fetch_items[it.content_id] = it
        self.fetch_hint_var.set(f"共 {len(items)} 条" if items else "还没有捕获到内容")
        if not items:
            messagebox.showinfo(
                "还没有内容",
                "工具里还没有捕获到任何视频。\n\n"
                "请在微信 PC 端打开目标回放/视频，让流量经过抓流工具，"
                "然后回来点「刷新列表」。")

    # ---------- 下载 ----------

    def _fetch_start(self) -> None:
        if self._fetch_busy:
            messagebox.showinfo("正在忙", "上一批还没处理完，请稍候。")
            return
        sel = self.fetch_tree.selection()
        if not sel:
            messagebox.showinfo("请先选择", "请在列表里选中要下载的条目（可多选）。")
            return

        targets = []
        for iid in sel:
            cid = self.fetch_tree.item(iid, "values")[1]
            it = self._fetch_items.get(cid)
            if it:
                targets.append((iid, it))

        api = self.fetch_api_var.get().strip() or "http://127.0.0.1:2022"
        keep_video = self.fetch_keep_video_var.get()
        auto_trans = self.fetch_auto_trans_var.get()

        self._fetch_busy = True
        self.fetch_start_btn.configure(state="disabled")
        self._run_bg(lambda: self._fetch_worker(targets, api, keep_video, auto_trans))

    def _fetch_worker(self, targets, api: str, keep_video: bool,
                      auto_trans: bool) -> None:
        from . import fetch as F
        from .config import RECORDINGS_DIR, ensure_dirs

        ensure_dirs()
        total = len(targets)
        done_ok = 0
        last_wav: Path | None = None

        for idx, (iid, item) in enumerate(targets, 1):
            prefix = f"[{idx}/{total}] 《{item.title}》"
            self._ui(lambda i=iid, s="处理中…": self._fetch_set_state(i, s))
            self._log(f"{prefix} 开始")

            try:
                key = F.get_decrypt_key(item, api)
                flag, enc_len = F.probe_encryption(item.url)
                self._log(f"{prefix} 加密 flag={flag} enc_len={enc_len} "
                          f"key={'已取到' if key else '无'}")
                if flag and not key:
                    raise F.FetchError("文件已加密但拿不到密钥")

                stem = F.safe_filename(item.title)
                base = Path(RECORDINGS_DIR)

                def on_prog(done: int, secs: float, _ix=idx) -> None:
                    mb = done / 1048576
                    rate = mb / max(1e-6, secs)
                    pct = 0.0
                    self._ui(lambda: self._fetch_set_progress(_ix, total, mb, rate))
                    _ = pct

                video = F.download_decrypted(item, key, base / (stem + ".mp4"),
                                             on_progress=on_prog)
                self._log(f"{prefix} 下载完成 {video.stat().st_size/1048576:.1f} MB")

                wav = F.extract_audio(video, base / (stem + ".wav"))
                if not keep_video:
                    video.unlink(missing_ok=True)
                    self._log(f"{prefix} 已删除视频，保留音频")

                dur, has_audio = F.probe_media(wav)
                self._log(f"{prefix} 音频就绪 {dur/60:.1f} 分钟 "
                          f"({wav.stat().st_size/1048576:.1f} MB) 含音轨={has_audio}")

                self._ui(lambda i=iid, d=dur: self._fetch_set_state(
                    i, f"完成 {d/60:.0f} 分钟"))
                done_ok += 1
                last_wav = wav

            except Exception as exc:  # noqa: BLE001
                self._log(f"{prefix} 失败：{exc}")
                self._ui(lambda i=iid: self._fetch_set_state(i, "失败"))

        self._ui(lambda: self._fetch_finish(done_ok, total, last_wav, auto_trans))

    def _fetch_finish(self, ok: int, total: int, wav: Path | None,
                      auto_trans: bool) -> None:
        self._fetch_busy = False
        self.fetch_start_btn.configure(state="normal")
        self.fetch_prog_var.set("")
        self.fetch_progress.configure(value=0)
        self._log(f"[下载] 完成 {ok}/{total}")

        if auto_trans and wav and wav.exists():
            self._log("[下载] 转入转写流程…")
            try:
                self._start_transcribe_from_path(wav)
            except Exception as exc:  # noqa: BLE001
                self._log(f"[下载] 启动转写失败：{exc}")
        elif ok:
            messagebox.showinfo("下载完成", f"成功 {ok}/{total}。音频已保存到录制目录。")

    # ---------- 小工具 ----------

    def _fetch_set_state(self, iid: str, state: str) -> None:
        try:
            vals = list(self.fetch_tree.item(iid, "values"))
            if len(vals) >= 3:
                vals[2] = state
                self.fetch_tree.item(iid, values=vals)
        except tk.TclError:
            pass

    def _fetch_set_progress(self, idx: int, total: int, mb: float, rate: float) -> None:
        self.fetch_prog_var.set(f"{idx}/{total}  {mb:.0f} MB  {rate:.1f} MB/s")
        # 只显示"进度感"，不确定总量，用脉冲式
        self.fetch_progress.configure(mode="indeterminate")
        self.fetch_progress.start(40)

    def _fetch_open_dir(self) -> None:
        import os
        import subprocess
        import sys

        from .config import RECORDINGS_DIR

        path = str(RECORDINGS_DIR)
        try:
            if sys.platform == "win32":
                os.startfile(path)  # noqa: S606
            elif sys.platform == "darwin":
                subprocess.run(["open", path], check=False)
            else:
                subprocess.run(["xdg-open", path], check=False)
        except Exception as exc:  # noqa: BLE001
            messagebox.showinfo("打不开", f"目录：{path}\n错误：{exc}")

    def _start_transcribe_from_path(self, wav: Path) -> None:
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

    def _run_bg(self, fn) -> None:
        """在后台线程跑 fn（宿主已有 worker 约定，这里保持一致）。"""
        self.worker = threading.Thread(target=fn, daemon=True)
        self.worker.start()
