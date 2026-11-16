"""图形界面。

设计原则：
- 所有耗时操作跑在子线程，界面永不冻结
- 每个设置旁边写清楚"为什么"，不让人靠猜
- 状态栏始终显示"现在在干什么"，长任务可随时中止
- 配置自动保存，下次打开还是上次的选择
"""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import _force_utf8  # noqa: F401
from .gui_fetch import FetchTabMixin
from .config import OUTPUT_DIR, RECORDINGS_DIR, TRANSCRIPTS_DIR, Config
from . import model_discovery, postprocess, userconfig

APP_TITLE = "微信直播录音转写"
APP_VER = "1.0"


class App(FetchTabMixin, tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"{APP_TITLE} v{APP_VER}")
        self.geometry("1080x760")
        self.minsize(920, 640)

        # 运行时状态
        self.cfg = userconfig.build_config_from_user(Config())
        self.log_queue: queue.Queue[str] = queue.Queue()
        self.worker: threading.Thread | None = None
        self.stop_flag = threading.Event()
        self.last_output: Path | None = None
        self.discovered_models: list[str] = []

        self._build_style()
        self._build_ui()
        self._load_settings_into_ui()
        self._poll_log()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        # Tk 变量只能在主线程读取，先取值再交给后台线程
        _m_base = self.api_url_var.get()
        _m_key = self.api_key_var.get()
        threading.Thread(target=self._discover_models,
                         args=(_m_base, _m_key), daemon=True).start()

    # ================= 外观 =================

    def _build_style(self) -> None:
        st = ttk.Style(self)
        try:
            st.theme_use("vista")
        except Exception:
            pass
        st.configure(".", font=("Microsoft YaHei UI", 10))
        st.configure("Title.TLabel", font=("Microsoft YaHei UI", 13, "bold"))
        st.configure("Hint.TLabel", foreground="#666", font=("Microsoft YaHei UI", 9))
        st.configure("Ok.TLabel", foreground="#1a7f37")
        st.configure("Warn.TLabel", foreground="#b54708")
        st.configure("Run.TButton", font=("Microsoft YaHei UI", 11, "bold"))

    def _build_ui(self) -> None:
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=10, pady=(10, 6))

        self.tab_record = ttk.Frame(nb)
        self.tab_fetch = ttk.Frame(nb)
        self.tab_trans = ttk.Frame(nb)
        self.tab_settings = ttk.Frame(nb)
        self.tab_log = ttk.Frame(nb)
        nb.add(self.tab_record, text="  录制  ")
        nb.add(self.tab_fetch, text="  视频号下载  ")
        nb.add(self.tab_trans, text="  转写与整理  ")
        nb.add(self.tab_settings, text="  设置  ")
        nb.add(self.tab_log, text="  运行日志  ")

        self._build_record_tab()
        self._build_fetch_tab()
        self._build_transcribe_tab()
        self._build_settings_tab()
        self._build_log_tab()

        # 底部状态栏
        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=10, pady=(0, 10))
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(bar, textvariable=self.status_var).pack(side="left")
        self.progress = ttk.Progressbar(bar, mode="determinate", length=220)
        self.progress.pack(side="right")

    # ================= 录制页 =================

    def _build_record_tab(self) -> None:
        f = self.tab_record

        # ===== 音源方式选择 =====
        src = ttk.LabelFrame(f, text="音源方式")
        src.pack(fill="x", padx=12, pady=(12, 8))

        r0 = ttk.Frame(src)
        r0.pack(fill="x", padx=10, pady=(8, 4))
        self.source_mode = tk.StringVar(value="loopback")
        ttk.Radiobutton(r0, text="声卡回环（通用，无需地址）", variable=self.source_mode,
                        value="loopback", command=self._sync_source_mode).pack(side="left")
        ttk.Radiobutton(r0, text="流地址直录（音质最好，需先拿到地址）",
                        variable=self.source_mode, value="stream",
                        command=self._sync_source_mode).pack(side="left", padx=(18, 0))

        # 设备行
        self.loop_row = ttk.Frame(src)
        self.loop_row.pack(fill="x", padx=10, pady=6)
        ttk.Label(self.loop_row, text="采集设备：").pack(side="left")
        self.device_var = tk.StringVar()
        self.device_cb = ttk.Combobox(self.loop_row, textvariable=self.device_var, width=44,
                                      state="readonly")
        self.device_cb.pack(side="left", padx=6)
        ttk.Button(self.loop_row, text="刷新", width=6,
                   command=self._refresh_devices).pack(side="left")
        self.device_status = ttk.Label(self.loop_row, text="", style="Hint.TLabel")
        self.device_status.pack(side="left", padx=8)

        # 流地址行
        self.stream_row = ttk.Frame(src)
        ttk.Label(self.stream_row, text="流地址：").pack(side="left")
        self.stream_url_var = tk.StringVar()
        ttk.Entry(self.stream_row, textvariable=self.stream_url_var).pack(
            side="left", fill="x", expand=True, padx=6)
        ttk.Button(self.stream_row, text="测试", width=6,
                   command=self._test_stream).pack(side="left")
        self.stream_status = ttk.Label(self.stream_row, text="", style="Hint.TLabel")
        self.stream_status.pack(side="left", padx=6)

        self.hint_label = ttk.Label(src, text="", style="Hint.TLabel", wraplength=940,
                                    justify="left")
        self.hint_label.pack(anchor="w", padx=10, pady=(0, 8))

        # 时长与模式
        box2 = ttk.LabelFrame(f, text="录制方式")
        box2.pack(fill="x", padx=12, pady=8)
        r1 = ttk.Frame(box2)
        r1.pack(fill="x", padx=10, pady=8)

        self.mode_var = tk.StringVar(value="timed")
        ttk.Radiobutton(r1, text="定时录制", variable=self.mode_var, value="timed",
                        command=self._sync_mode).pack(side="left")
        self.minutes_var = tk.StringVar(value="130")
        self.minutes_spin = ttk.Spinbox(r1, from_=1, to=600, width=6,
                                        textvariable=self.minutes_var)
        self.minutes_spin.pack(side="left", padx=(4, 2))
        ttk.Label(r1, text="分钟").pack(side="left")
        ttk.Radiobutton(r1, text="录到手动停止", variable=self.mode_var, value="manual",
                        command=self._sync_mode).pack(side="left", padx=(20, 0))

        r2 = ttk.Frame(box2)
        r2.pack(fill="x", padx=10, pady=(0, 8))
        self.live_asr_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(r2, text="录制同时实时转写", variable=self.live_asr_var).pack(side="left")
        ttk.Label(r2, text="（长回放建议关闭：先纯粹录音更稳，录完再批量转）",
                  style="Hint.TLabel").pack(side="left", padx=6)

        r3 = ttk.Frame(box2)
        r3.pack(fill="x", padx=10, pady=(0, 10))
        self.auto_stop_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(r3, text="静音自动停止，连续静音", variable=self.auto_stop_var).pack(side="left")
        self.silence_var = tk.StringVar(value="40")
        ttk.Spinbox(r3, from_=5, to=600, width=5, textvariable=self.silence_var).pack(side="left", padx=4)
        ttk.Label(r3, text="秒（回放播完自动收工，不用守着）", style="Hint.TLabel").pack(side="left")

        r4 = ttk.Frame(box2)
        r4.pack(fill="x", padx=10, pady=(0, 10))
        ttk.Label(r4, text="标记名称：").pack(side="left")
        self.name_var = tk.StringVar(value="")
        ttk.Entry(r4, textvariable=self.name_var, width=30).pack(side="left", padx=4)
        ttk.Label(r4, text="（会写进文件名，方便日后找）", style="Hint.TLabel").pack(side="left")

        # 控制
        ctrl = ttk.Frame(f)
        ctrl.pack(fill="x", padx=12, pady=(4, 8))
        self.rec_btn = ttk.Button(ctrl, text="● 开始录制", style="Run.TButton", width=16,
                                  command=self._toggle_record)
        self.rec_btn.pack(side="left")
        self.rec_info = ttk.Label(ctrl, text="", style="Hint.TLabel")
        self.rec_info.pack(side="left", padx=12)

        # 实时预览
        pv = ttk.LabelFrame(f, text="实时转写预览")
        pv.pack(fill="both", expand=True, padx=12, pady=(4, 12))
        self.preview = tk.Text(pv, height=10, wrap="word", font=("Microsoft YaHei UI", 10))
        sb = ttk.Scrollbar(pv, command=self.preview.yview)
        self.preview.configure(yscrollcommand=sb.set)
        self.preview.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=8)
        sb.pack(side="right", fill="y", pady=8, padx=(0, 8))

    def _sync_mode(self) -> None:
        self.minutes_spin.configure(state="normal" if self.mode_var.get() == "timed" else "disabled")

    def _sync_source_mode(self) -> None:
        """切换音源方式时，只显示相关的输入行。"""
        is_stream = self.source_mode.get() == "stream"
        if is_stream:
            self.loop_row.pack_forget()
            self.stream_row.pack(fill="x", padx=10, pady=6)
            self.hint_label.configure(
                text="流地址直录：直接从直播流取原始音轨，不经过声卡，音质更好、可静音录、"
                     "CPU 占用极低。但微信视频号不公开地址，需先用抓包工具拿到带签名的 "
                     "flv/m3u8 地址（地址有时效，过期会 403）。拿不到就用「声卡回环」兜底。")
        else:
            self.stream_row.pack_forget()
            self.loop_row.pack(fill="x", padx=10, pady=6)
            self.hint_label.configure(
                text="声卡回环：从声卡输出端录音，不侵入微信，微信改版也不受影响。"
                     "需有声音正在播放，且不能用系统静音。")

    def _test_stream(self) -> None:
        url = self.stream_url_var.get().strip()
        if not url:
            messagebox.showinfo("提示", "请先填入流地址")
            return
        from .stream_capture import is_url, probe_url

        if not is_url(url):
            self.stream_status.configure(text="不是有效的流地址", style="Warn.TLabel")
            return

        self.stream_status.configure(text="探测中…", style="Hint.TLabel")

        def work():
            r = probe_url(url)

            def apply():
                if r.get("ok"):
                    extra = "，含音轨" if r.get("has_audio") else "，⚠ 未检测到音轨"
                    fmt = r.get("format", "")
                    self.stream_status.configure(
                        text=f"✓ 可拉流{f'（{fmt}）' if fmt else ''}{extra}",
                        style="Ok.TLabel")
                    self._log(f"[流探测] 成功: {r.get('info', '')[:200]}")
                else:
                    self.stream_status.configure(text="✗ 无法拉流", style="Warn.TLabel")
                    self._log(f"[流探测] 失败: {r.get('error')}")
                    messagebox.showerror("探测失败", r.get("error", "未知错误"))
            self._ui(apply)

        threading.Thread(target=work, daemon=True).start()

    # ================= 转写页 =================

    def _build_transcribe_tab(self) -> None:
        f = self.tab_trans

        box = ttk.LabelFrame(f, text="选择音频")
        box.pack(fill="x", padx=12, pady=(12, 8))
        r = ttk.Frame(box)
        r.pack(fill="x", padx=10, pady=8)
        self.audio_path_var = tk.StringVar()
        ttk.Entry(r, textvariable=self.audio_path_var).pack(side="left", fill="x", expand=True)
        ttk.Button(r, text="浏览…", width=8, command=self._pick_audio).pack(side="left", padx=6)
        ttk.Button(r, text="用最近录制", width=12, command=self._use_latest).pack(side="left")

        box2 = ttk.LabelFrame(f, text="转写设置")
        box2.pack(fill="x", padx=12, pady=8)
        r2 = ttk.Frame(box2)
        r2.pack(fill="x", padx=10, pady=8)
        ttk.Label(r2, text="转写模型：").pack(side="left")
        self.asr_model_var = tk.StringVar()
        self.asr_cb = ttk.Combobox(r2, textvariable=self.asr_model_var, width=44,
                                   state="readonly")
        self.asr_cb.pack(side="left", padx=6)
        ttk.Label(r2, text="引擎：").pack(side="left", padx=(14, 2))
        self.engine_var = tk.StringVar()
        ttk.Combobox(r2, textvariable=self.engine_var, width=8, state="readonly",
                     values=["auto", "gemini", "local"]).pack(side="left")
        ttk.Label(r2, text="auto=云端优先，失败自动切本地",
                  style="Hint.TLabel").pack(side="left", padx=6)

        box3 = ttk.LabelFrame(f, text="文字整理（可选，推荐开启）")
        box3.pack(fill="x", padx=12, pady=8)
        r3 = ttk.Frame(box3)
        r3.pack(fill="x", padx=10, pady=6)
        self.do_fix_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(r3, text="① 同音字纠错", variable=self.do_fix_var).pack(side="left")
        self.do_codes_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(r3, text="② 暗语展开", variable=self.do_codes_var).pack(side="left", padx=14)
        self.do_digest_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(r3, text="③ 生成要点", variable=self.do_digest_var).pack(side="left")

        r4 = ttk.Frame(box3)
        r4.pack(fill="x", padx=10, pady=(0, 8))
        ttk.Label(r4, text="整理模型：").pack(side="left")
        self.text_model_var = tk.StringVar()
        self.text_cb = ttk.Combobox(r4, textvariable=self.text_model_var, width=44,
                                    state="readonly")
        self.text_cb.pack(side="left", padx=6)
        ttk.Label(r4, text="（用 LLM 拿领域词表再校对一遍，能修掉专有名词错误）",
                  style="Hint.TLabel").pack(side="left")

        ctrl = ttk.Frame(f)
        ctrl.pack(fill="x", padx=12, pady=(4, 8))
        self.run_btn = ttk.Button(ctrl, text="开始转写", style="Run.TButton", width=14,
                                  command=self._start_transcribe)
        self.run_btn.pack(side="left")
        self.open_btn = ttk.Button(ctrl, text="打开输出目录", width=14,
                                   command=self._open_output)
        self.open_btn.pack(side="left", padx=8)
        self.stop_btn = ttk.Button(ctrl, text="中止", width=8, command=self._abort, state="disabled")
        self.stop_btn.pack(side="left")

        out = ttk.LabelFrame(f, text="结果预览（完成后自动打开 md）")
        out.pack(fill="both", expand=True, padx=12, pady=(4, 12))
        self.result = tk.Text(out, height=12, wrap="word", font=("Microsoft YaHei UI", 10))
        sb = ttk.Scrollbar(out, command=self.result.yview)
        self.result.configure(yscrollcommand=sb.set)
        self.result.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=8)
        sb.pack(side="right", fill="y", pady=8, padx=(0, 8))

    # ================= 设置页 =================

    def _build_settings_tab(self) -> None:
        f = self.tab_settings
        canvas = tk.Canvas(f, highlightthickness=0)
        sb = ttk.Scrollbar(f, command=canvas.yview)
        inner = ttk.Frame(canvas)
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=sb.set)
        canvas.pack(side="left", fill="both", expand=True, padx=(12, 0), pady=12)
        sb.pack(side="right", fill="y", pady=12, padx=(0, 12))

        # --- API 配置 ---
        api = ttk.LabelFrame(inner, text="① API 接入")
        api.pack(fill="x", pady=(0, 12))

        r = ttk.Frame(api)
        r.pack(fill="x", padx=10, pady=8)
        ttk.Label(r, text="网关地址：").pack(side="left")
        self.api_url_var = tk.StringVar()
        ttk.Entry(r, textvariable=self.api_url_var, width=48).pack(side="left", padx=6)
        ttk.Label(r, text="例如 http://127.0.0.1:3425/v1",
                  style="Hint.TLabel").pack(side="left")

        r = ttk.Frame(api)
        r.pack(fill="x", padx=10, pady=(0, 8))
        ttk.Label(r, text="API Key　：").pack(side="left")
        self.api_key_var = tk.StringVar()
        self.key_entry = ttk.Entry(r, textvariable=self.api_key_var, width=48, show="•")
        self.key_entry.pack(side="left", padx=6)
        self.show_key = tk.BooleanVar(value=False)
        ttk.Checkbutton(r, text="显示", variable=self.show_key,
                        command=self._toggle_key).pack(side="left")

        r = ttk.Frame(api)
        r.pack(fill="x", padx=10, pady=(0, 10))
        ttk.Button(r, text="测试连接并拉取模型", command=self._test_and_discover).pack(side="left")
        self.api_status = ttk.Label(r, text="未测试", style="Hint.TLabel")
        self.api_status.pack(side="left", padx=10)

        # --- 转写默认 ---
        asrb = ttk.LabelFrame(inner, text="② 转写默认设置")
        asrb.pack(fill="x", pady=(0, 12))
        r = ttk.Frame(asrb)
        r.pack(fill="x", padx=10, pady=8)
        ttk.Label(r, text="默认转写模型：").pack(side="left")
        self.def_asr_var = tk.StringVar()
        ttk.Combobox(r, textvariable=self.def_asr_var, width=42,
                     state="readonly").pack(side="left", padx=6)
        self.def_asr_cb = r.winfo_children()[-1]

        r = ttk.Frame(asrb)
        r.pack(fill="x", padx=10, pady=(0, 8))
        ttk.Label(r, text="默认整理模型：").pack(side="left")
        self.def_text_var = tk.StringVar()
        ttk.Combobox(r, textvariable=self.def_text_var, width=42,
                     state="readonly").pack(side="left", padx=6)
        self.def_text_cb = r.winfo_children()[-1]

        r = ttk.Frame(asrb)
        r.pack(fill="x", padx=10, pady=(0, 8))
        ttk.Label(r, text="单次音频上限（秒）：").pack(side="left")
        self.chunk_var = tk.StringVar()
        ttk.Spinbox(r, from_=30, to=600, width=6, textvariable=self.chunk_var).pack(side="left", padx=4)
        ttk.Label(r, text="云端 120s 上限，留余量用 110",
                  style="Hint.TLabel").pack(side="left", padx=6)

        r = ttk.Frame(asrb)
        r.pack(fill="x", padx=10, pady=(0, 10))
        self.lexicon_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(r, text="启用 XSC 领域词表（校名 / 暗语 / 错字纠正）",
                        variable=self.lexicon_var).pack(side="left")

        # --- 音频与切分 ---
        ab = ttk.LabelFrame(inner, text="③ 音频与切分")
        ab.pack(fill="x", pady=(0, 12))
        for label, var, lo, hi, hint in [
            ("片段最长（秒）", "maxseg", 10, 120, "超过就强制切，防止内存堆积"),
            ("静音判定（秒）", "minsil", 0.3, 3.0, "连续静音超过它才断句"),
            ("云端最小块（秒）", "cloudmin", 3, 30, "太碎会让云端固定开销拖慢"),
        ]:
            r = ttk.Frame(ab)
            r.pack(fill="x", padx=10, pady=4)
            ttk.Label(r, text=f"{label}：", width=16).pack(side="left")
            v = tk.StringVar()
            setattr(self, f"{var}_var", v)
            ttk.Spinbox(r, from_=lo, to=hi, width=6, textvariable=v).pack(side="left", padx=4)
            ttk.Label(r, text=hint, style="Hint.TLabel").pack(side="left", padx=6)

        # --- 保存 ---
        save = ttk.Frame(inner)
        save.pack(fill="x", pady=(0, 20))
        ttk.Button(save, text="保存设置", command=self._save_settings).pack(side="left")
        ttk.Button(save, text="恢复默认", command=self._reset_settings).pack(side="left", padx=8)
        self.save_hint = ttk.Label(save, text="", style="Ok.TLabel")
        self.save_hint.pack(side="left", padx=10)

        # --- 词表 ---
        lx = ttk.LabelFrame(inner, text="④ 领域词表")
        lx.pack(fill="x", pady=(0, 20))
        r = ttk.Frame(lx)
        r.pack(fill="x", padx=10, pady=8)
        ttk.Label(r, text="词表文件：").pack(side="left")
        self.lex_path_var = tk.StringVar()
        ttk.Entry(r, textvariable=self.lex_path_var, width=46).pack(side="left", padx=6)
        ttk.Button(r, text="浏览…", width=8, command=self._pick_lexicon).pack(side="left")
        ttk.Button(r, text="用默认", width=8, command=self._default_lexicon).pack(side="left", padx=4)
        ttk.Label(lx, text="JSON 格式：schools / terms / codes / corrections 四类。"
                           "每场直播后把读不通的地方补进来，准确率会越来越高。",
                  style="Hint.TLabel").pack(anchor="w", padx=10, pady=(0, 8))

    def _build_log_tab(self) -> None:
        f = self.tab_log
        self.log_text = tk.Text(f, wrap="word", font=("Consolas", 9))
        sb = ttk.Scrollbar(f, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=sb.set)
        self.log_text.pack(side="left", fill="both", expand=True, padx=(10, 0), pady=10)
        sb.pack(side="right", fill="y", pady=10, padx=(0, 10))

    # ================= 设置加载/保存 =================

    def _load_settings_into_ui(self) -> None:
        c = self.cfg
        self.api_url_var.set(c.asr.gemini_endpoint.replace("/chat/completions", ""))
        self.api_key_var.set(c.asr.gemini_api_key)
        self.def_asr_var.set(c.asr.gemini_model)
        self.def_text_var.set(postprocess.DEFAULT_MODEL)
        self.asr_model_var.set(c.asr.gemini_model)
        self.text_model_var.set(postprocess.DEFAULT_MODEL)
        self.engine_var.set(c.asr.engine)
        self.chunk_var.set("110")
        self.lexicon_var.set(c.asr.use_lexicon)
        self.maxseg_var.set(str(c.vad.max_segment_seconds))
        self.minsil_var.set(str(c.vad.min_silence_seconds))
        self.cloudmin_var.set(str(c.vad.cloud_min_segment_seconds))
        self.lex_path_var.set(c.asr.lexicon_path or str(
            Path(__file__).resolve().parent.parent / "lexicon" / "xsc.json"))
        self._sync_source_mode()
        self._sync_mode()
        self._refresh_devices()
        self._apply_model_options([o[0] for o in model_discovery.KNOWN_ASR]
                                  + [o[0] for o in model_discovery.LOCAL_WHISPER],
                                  [o[0] for o in model_discovery.KNOWN_TEXT])

    def _apply_model_options(self, asr_ids: list[str], text_ids: list[str]) -> None:
        self.asr_cb["values"] = asr_ids
        self.text_cb["values"] = text_ids
        try:
            self.def_asr_cb["values"] = asr_ids
            self.def_text_cb["values"] = text_ids
        except Exception:
            pass

    def _collect_settings(self) -> dict:
        return {
            "audio": {
                "device_hint": self.cfg.audio.device_hint,
            },
            "vad": {
                "max_segment_seconds": _f(self.maxseg_var.get(), 28.0),
                "min_silence_seconds": _f(self.minsil_var.get(), 0.7),
                "cloud_min_segment_seconds": _f(self.cloudmin_var.get(), 8.0),
            },
            "asr": {
                "engine": self.engine_var.get() or "auto",
                "gemini_model": self.def_asr_var.get() or "google-vertex/gemini-4-hydrogen",
                "gemini_endpoint": (self.api_url_var.get().rstrip("/")
                                    + "/chat/completions"),
                "gemini_api_key": self.api_key_var.get() or "magpie-alma",
                "use_lexicon": bool(self.lexicon_var.get()),
                "lexicon_path": self.lex_path_var.get(),
            },
        }

    def _save_settings(self) -> None:
        try:
            userconfig.save_user_config(self._collect_settings())
            self.cfg = userconfig.build_config_from_user(Config())
            self.save_hint.configure(text="✓ 已保存")
            self.after(2200, lambda: self.save_hint.configure(text=""))
            self._log("设置已保存到 config/user.json")
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("保存失败", str(e))

    def _reset_settings(self) -> None:
        if not messagebox.askyesno("恢复默认", "确定清除所有自定义设置？"):
            return
        userconfig.reset_user_config()
        self.cfg = userconfig.build_config_from_user(Config())
        self._load_settings_into_ui()
        self.save_hint.configure(text="✓ 已恢复默认")

    def _toggle_key(self) -> None:
        self.key_entry.configure(show="" if self.show_key.get() else "•")

    def _pick_lexicon(self) -> None:
        p = filedialog.askopenfilename(title="选择词表 JSON",
                                       filetypes=[("JSON", "*.json"), ("全部", "*.*")])
        if p:
            self.lex_path_var.set(p)

    def _default_lexicon(self) -> None:
        self.lex_path_var.set(str(Path(__file__).resolve().parent.parent
                                  / "lexicon" / "xsc.json"))

    # ================= 模型发现 =================

    def _discover_models(self, base_url: str = "", api_key: str = "") -> None:
        """后台线程：拉模型列表。不碰任何 Tk 对象。"""
        base = base_url or "http://127.0.0.1:3425/v1"
        key = api_key or "magpie-alma"
        found = model_discovery.fetch_models(base, key)
        self.discovered_models = found
        asr_ids = [o[0] for o in model_discovery.merge_asr_options(found)]
        text_ids = [o[0] for o in model_discovery.merge_text_options(found)]

        def apply():
            self._apply_model_options(asr_ids, text_ids)
            if found:
                self.api_status.configure(
                    text=f"✓ 发现 {len(found)} 个模型", style="Ok.TLabel")
            else:
                self.api_status.configure(
                    text="未连上网关，使用内置列表", style="Warn.TLabel")

        self._ui(apply)

    def _test_and_discover(self) -> None:
        self.api_status.configure(text="测试中…", style="Hint.TLabel")
        base = self.api_url_var.get()
        key = self.api_key_var.get()
        threading.Thread(target=self._discover_models,
                         args=(base, key), daemon=True).start()

    # ================= 设备 =================

    def _refresh_devices(self) -> None:
        self.device_status.configure(text="枚举中…", style="Hint.TLabel")

        def work():
            names: list[str] = []
            default = ""
            err = ""
            try:
                from .audio_capture import list_loopback_devices, pick_device

                names = [d.name for d in list_loopback_devices()]
                try:
                    default = pick_device("").name
                except Exception:
                    pass
            except Exception as e:  # noqa: BLE001
                err = str(e)[:60]

            def apply():
                if err:
                    self.device_status.configure(text=f"枚举失败: {err}", style="Warn.TLabel")
                else:
                    self._set_devices(names, default)

            # 后台线程里不能直接 self.after（Tk 只允许主线程），走队列
            self._ui(apply)

        threading.Thread(target=work, daemon=True).start()

    def _set_devices(self, names: list[str], default: str) -> None:
        if not names:
            self.device_status.configure(text="未找到回环设备", style="Warn.TLabel")
            return
        self.device_cb["values"] = names
        self.device_var.set(default or names[0])
        self.device_status.configure(text=f"✓ {len(names)} 个可用",
                                     style="Ok.TLabel")

    # ================= 录制 =================

    def _toggle_record(self) -> None:
        if self.worker and self.worker.is_alive():
            self.stop_flag.set()
            self.rec_btn.configure(text="正在停止…", state="disabled")
            return

        name = self.name_var.get().strip()
        manual = self.mode_var.get() == "manual"
        minutes = _f(self.minutes_var.get(), 130.0)
        live = self.live_asr_var.get()
        silence = _f(self.silence_var.get(), 0.0) if self.auto_stop_var.get() else 0.0
        device = self.device_var.get()
        use_stream = self.source_mode.get() == "stream"
        url = self.stream_url_var.get().strip()

        if use_stream and not url:
            messagebox.showwarning("缺少地址", "流地址直录需要先填入流地址")
            return
        if use_stream and live:
            messagebox.showinfo(
                "提示",
                "流地址直录暂不支持边录边转（ffmpeg 直接出音频文件更稳）。\n"
                "录完后到「转写与整理」页转写即可。")
            self.live_asr_var.set(False)
            live = False

        self.stop_flag.clear()
        self.save_settings_silent()
        self.preview.delete("1.0", "end")
        self.rec_btn.configure(text="■ 停止录制")
        self.stop_btn.configure(state="normal")

        target = self._record_stream if use_stream else self._record_worker
        args = ((name, url) if use_stream
                else (name, 0 if manual else minutes * 60, live, silence, device))

        self.worker = threading.Thread(target=target, args=args, daemon=True)
        self.worker.start()

    # ---------- 流地址直录 ----------

    def _record_stream(self, name: str, url: str) -> None:
        from .stream_capture import StreamRecorder

        rec = StreamRecorder(
            url, RECORDINGS_DIR, name=name or "stream",
            progress_cb=lambda size: self.after(
                0, lambda s=size: self._update_stream_status(s)),
            log_cb=self._log,
        )
        self.stream_recorder = rec
        try:
            rec.start()
            # 等到用户停止或 ffmpeg 自己结束
            while not self.stop_flag.is_set():
                time.sleep(0.8)
                if not rec.running:
                    self._log("[stream] ffmpeg 已结束")
                    break
        except Exception as e:  # noqa: BLE001
            self._log(f"[stream] 错误: {e}")
            self.after(0, lambda: messagebox.showerror("拉流失败", str(e)[:300]))
        finally:
            rec.stop()
            self.last_output = rec.out_path
            if rec.out_path and rec.out_path.exists():
                self.after(0, lambda: self.audio_path_var.set(str(rec.out_path)))
                self._log(f"[stream] 已保存 {rec.out_path.name}")
            self.after(0, self._record_finished)

    def _update_stream_status(self, size: int) -> None:
        secs = max(0, size - 44) / 32000  # 16k mono 16bit
        m, s = divmod(int(secs), 60)
        self.rec_info.configure(
            text=f"拉流录音 {m:02d}:{s:02d}   {size/1024/1024:.1f} MB")
        self.status_var.set(f"拉流录音中… {m:02d}:{s:02d}")

    def save_settings_silent(self) -> None:
        try:
            userconfig.save_user_config(self._collect_settings())
        except Exception:
            pass

    def _record_worker(self, name: str, limit: float, live: bool,
                       silence: float, device: str) -> None:
        try:
            if live:
                self._run_live(name, limit, silence, device)
            else:
                self._run_plain(name, limit, silence, device)
        except Exception as e:  # noqa: BLE001
            self._log(f"[错误] {e}")
        finally:
            self.after(0, self._record_finished)

    def _run_plain(self, name: str, limit: float, silence: float, device: str) -> None:
        """纯录音（不转写）—— 长回放推荐。"""
        import numpy as np
        import soundfile as sf
        from .audio_capture import LoopbackRecorder

        RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
        rec = LoopbackRecorder(device_hint=device, chunk_seconds=0.1)
        rec.start()
        sr = rec.device.rate
        stamp = time.strftime("%Y%m%d_%H%M%S")
        safe = "".join(c for c in (name or "rec") if c.isalnum() or c in "-_") or "rec"
        out = RECORDINGS_DIR / f"{stamp}_{safe}.wav"
        wav = sf.SoundFile(str(out), mode="w", samplerate=sr, channels=1, subtype="PCM_16")

        t0 = time.time()
        peak, silent_run, written = 0.0, 0.0, 0
        self._log(f"[录制] 开始 → {out.name}")
        try:
            while not self.stop_flag.is_set():
                el = time.time() - t0
                if limit and el >= limit:
                    self._log("[录制] 到达设定时长，停止")
                    break
                chunk = rec.read(timeout=0.5)
                if chunk is None:
                    break
                if len(chunk) == 0:
                    continue
                wav.write(np.clip(chunk, -1.0, 1.0))
                written += len(chunk)
                peak = max(peak, float(np.max(np.abs(chunk))))
                rms = float(np.sqrt(np.mean(chunk**2)))
                silent_run = 0.0 if rms >= 0.002 else silent_run + len(chunk) / sr
                if silence and silent_run >= silence:
                    self._log(f"[录制] 静音 {silent_run:.0f}s，自动停止")
                    break
                self.after(0, lambda e=el, w=written, p=peak: self._update_rec_status(e, w, p))
        finally:
            wav.close()
            rec.stop()

        self.last_output = out
        self._log(f"[录制] 完成 {written/sr/60:.1f} 分钟, {written*2/1024/1024:.0f} MB")
        self.after(0, lambda: self.audio_path_var.set(str(out)))

    def _run_live(self, name: str, limit: float, silence: float, device: str) -> None:
        """录制 + 实时转写。"""
        from .pipeline import Pipeline

        cfg = userconfig.build_config_from_user(Config())
        cfg.asr.engine = self.engine_var.get() or "auto"
        cfg.asr.gemini_model = self.asr_model_var.get() or cfg.asr.gemini_model
        cfg.audio.device_hint = device
        cfg.session_name = name
        p = Pipeline(cfg)

        def hook():
            # 定期把已出句子刷到预览
            shown = 0
            while not self.stop_flag.is_set() and p._asr_thread and p._asr_thread.is_alive():
                time.sleep(1.2)
                segs = p.session.segments
                if len(segs) > shown:
                    new = segs[shown:]
                    shown = len(segs)
                    text = "\n".join(f"[{_mmss(s['start'])}] {s['text']}" for s in new)
                    self.after(0, lambda t=text: self._append_preview(t))

        threading.Thread(target=hook, daemon=True).start()
        doc = p.run(max_seconds=limit)
        self._log(f"[实时转写] 完成，共 {len(doc['segments'])} 句")
        self.last_output = Path(TRANSCRIPTS_DIR) / f"{p.session.id}.md"
        self.after(0, lambda: self.audio_path_var.set(str(p.session.audio_path)))

    def _update_rec_status(self, elapsed: float, written: int, peak: float) -> None:
        m, s = divmod(int(elapsed), 60)
        mb = written * 2 / 1024 / 1024
        self.rec_info.configure(text=f"已录 {m:02d}:{s:02d}   {mb:.0f} MB   峰值 {peak:.2f}")
        self.status_var.set(f"录制中… {m:02d}:{s:02d}")
        self.progress.configure(mode="indeterminate")

    def _append_preview(self, text: str) -> None:
        self.preview.insert("end", text + "\n")
        self.preview.see("end")

    def _record_finished(self) -> None:
        self.rec_btn.configure(text="● 开始录制", state="normal")
        self.stop_btn.configure(state="disabled")
        self.progress.configure(mode="determinate")
        self.progress["value"] = 0
        self.status_var.set("录制结束")
        if self.last_output:
            self.rec_info.configure(text=f"已保存：{Path(self.last_output).name}")

    # ================= 转写 =================

    def _pick_audio(self) -> None:
        p = filedialog.askopenfilename(
            title="选择音频文件", initialdir=str(RECORDINGS_DIR),
            filetypes=[("音频", "*.wav *.mp3 *.m4a *.flac *.aac"), ("全部", "*.*")])
        if p:
            self.audio_path_var.set(p)

    def _use_latest(self) -> None:
        files = sorted(RECORDINGS_DIR.glob("*.wav"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not files:
            messagebox.showinfo("没有录制", "录制目录里还没有音频文件")
            return
        self.audio_path_var.set(str(files[0]))

    def _start_transcribe(self) -> None:
        src = self.audio_path_var.get().strip()
        if not src or not Path(src).exists():
            messagebox.showwarning("请先选音频", "请选择要转写的音频文件")
            return
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("正在忙", "当前有任务在跑，先等它结束或中止")
            return

        self.stop_flag.clear()
        self.result.delete("1.0", "end")
        self.run_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.save_settings_silent()

        self.worker = threading.Thread(target=self._transcribe_worker, args=(Path(src),),
                                       daemon=True)
        self.worker.start()

    def _transcribe_worker(self, src: Path) -> None:
        try:
            self._do_transcribe(src)
        except Exception as e:  # noqa: BLE001
            self._log(f"[错误] {e}")
            self.after(0, lambda: messagebox.showerror("转写失败", str(e)))
        finally:
            self.after(0, lambda: (self.run_btn.configure(state="normal"),
                                   self.stop_btn.configure(state="disabled"),
                                   self.progress.configure(mode="determinate"),
                                   self.progress.configure(value=0)))

    def _do_transcribe(self, src: Path) -> None:
        import numpy as np
        import soundfile as sf

        from .config import AsrConfig
        from .pipeline import build_engine
        from .resample import StreamResampler
        from .session import Session
        from . import lexicon

        self._log(f"===== 开始处理 {src.name} =====")
        t_all = time.time()

        audio, sr = sf.read(str(src), dtype="float32", always_2d=False)
        if audio.ndim > 1:
            audio = audio.mean(axis=1)
        dur = len(audio) / sr
        self._log(f"音频 {dur/60:.1f} 分钟，{sr}Hz")

        cfg = userconfig.build_config_from_user(Config())
        cfg.asr.engine = self.engine_var.get() or "auto"
        cfg.asr.gemini_model = self.asr_model_var.get() or cfg.asr.gemini_model
        engine = build_engine(cfg)

        TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
        sess = Session(TRANSCRIPTS_DIR, name=src.stem, sample_rate=sr, record_audio=False)
        sess.source_audio_name = src.name

        rs = StreamResampler(in_rate=sr, out_rate=16000)
        block = sr * 300
        total = len(audio)
        n_blocks = (total + block - 1) // block

        for bi, i in enumerate(range(0, total, block)):
            if self.stop_flag.is_set():
                self._log("[中止] 用户中断转写")
                break
            piece = audio[i : i + block]
            pct = int(bi / max(1, n_blocks) * 100)
            self.after(0, lambda p=pct, b=bi: (
                self.progress.configure(mode="determinate", value=p),
                self.status_var.set(f"转写中… {p}%  (第 {b+1}/{n_blocks} 块)")))
            pcm = rs.process(piece)
            if len(pcm) == 0:
                continue
            res = engine.transcribe(pcm, 16000)
            if res:
                sess.add_segments(res, offset=i / sr,
                                  expand_codes=self.lexicon_var.get())
            self._log(f"  块 {bi+1}/{n_blocks} 完成，累计 {len(sess.segments)} 句")

        tail = rs.flush()
        if len(tail):
            res = engine.transcribe(tail, 16000)
            if res:
                sess.add_segments(res, offset=total / sr,
                                  expand_codes=self.lexicon_var.get())

        self._log(f"转写完成：{len(sess.segments)} 句")

        # --- 后处理 ---
        lines = [s["text"] for s in sess.segments]
        if self.do_fix_var.get() and lines:
            self.after(0, lambda: self.status_var.set("LLM 纠错中…"))
            self._log("→ 同音字纠错")
            try:
                lexicon.apply_loaded(lexicon.load(
                    Path(cfg.asr.lexicon_path) if cfg.asr.lexicon_path else None))
                vocab = "、".join(lexicon.SCHOOLS[:60] + lexicon.TERMS[:40])
            except Exception:
                vocab = ""
            fixed = postprocess.fix_errors(
                lines, model=self.text_model_var.get() or postprocess.DEFAULT_MODEL,
                endpoint=cfg.asr.gemini_endpoint, api_key=cfg.asr.gemini_api_key,
                vocabulary=vocab)
            for seg, new in zip(sess.segments, fixed):
                if new and new != seg["text"]:
                    seg["raw_text"] = seg["text"]
                    seg["text"] = new
            self._log(f"   纠错 {sum(1 for a,b in zip(lines, fixed) if a!=b)} 处")

        digest = ""
        if self.do_digest_var.get() and sess.segments:
            self.after(0, lambda: self.status_var.set("生成要点中…"))
            self._log("→ 生成结构化要点")
            digest = postprocess.build_digest(
                sess.segments, model=self.text_model_var.get() or postprocess.DEFAULT_MODEL,
                endpoint=cfg.asr.gemini_endpoint, api_key=cfg.asr.gemini_api_key,
                title=src.stem)

        doc = sess.close(extra_meta={"engine": "gui", "asr": getattr(engine, "stats", {})})
        if digest:
            md = sess.md_path
            md.write_text(md.read_text(encoding="utf-8") + "\n\n---\n\n"
                          "# AI 整理要点\n\n" + digest + "\n", encoding="utf-8")
            self.after(0, lambda d=digest: (
                self.result.delete("1.0", "end"),
                self.result.insert("1.0", d)))

        self.last_output = sess.md_path
        self._log(f"===== 全部完成，用时 {(time.time()-t_all)/60:.1f} 分钟 =====")
        self._log(f"逐字稿: {sess.md_path}")
        self._log(f"结构化: {sess.json_path}")
        self.after(0, lambda: self.status_var.set("完成"))
        self.after(0, lambda: self._offer_open(sess.md_path))

    def _offer_open(self, md: Path) -> None:
        if messagebox.askyesno("完成", f"处理完成。\n\n{md.name}\n\n现在打开逐字稿？"):
            self._open_path(md)

    def _open_output(self) -> None:
        self._open_path(self.last_output or TRANSCRIPTS_DIR)

    def _open_path(self, p) -> None:
        try:
            p = Path(p)
            if p.is_file():
                os.startfile(str(p))  # noqa: S606
            else:
                os.startfile(str(p))  # noqa: S606
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("打开失败", str(e))

    def _abort(self) -> None:
        self.stop_flag.set()
        self.status_var.set("正在中止…")
        self._log("[中止] 已发送停止信号")

    # ================= 日志 =================

    def _log(self, msg: str) -> None:
        self.log_queue.put(msg)

    def _poll_log(self) -> None:
        try:
            while True:
                msg = self.log_queue.get_nowait()
                ts = time.strftime("%H:%M:%S")
                self.log_text.insert("end", f"[{ts}] {msg}\n")
                self.log_text.see("end")
        except queue.Empty:
            pass
        self.after(300, self._poll_log)

    def _on_close(self) -> None:
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno("任务进行中", "当前有任务在跑，确定退出？"):
                return
            self.stop_flag.set()
        self.destroy()


def _f(s, default: float) -> float:
    try:
        return float(str(s).strip())
    except Exception:
        return default


def _mmss(sec: float) -> str:
    s = max(0, int(sec))
    h, rem = divmod(s, 3600)
    m, x = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{x:02d}" if h else f"{m:02d}:{x:02d}"


def main() -> int:
    app = App()
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
