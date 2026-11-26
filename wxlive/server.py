"""NAS 无头 Web 控制台（FastAPI + 单文件响应式前端）。

启动方式：
    python -m wxlive serve --host 0.0.0.0 --port 8910
    或
    uvicorn wxlive.server:app --host 0.0.0.0 --port 8910
"""

from __future__ import annotations

import os
import shutil
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from .nas_runner import NasJobRunner
from .stream_capture import extract_url, find_ffmpeg, is_url, probe_url


class ProbeReq(BaseModel):
    url: str
    decrypt_key: int = 0


class StreamStartReq(BaseModel):
    url: str
    name: str = ""
    auto_transcribe: bool | None = None
    decrypt_key: int = 0


class StopReq(BaseModel):
    transcribe: bool | None = None


class RenameJobReq(BaseModel):
    title: str


class SettingsUpdateReq(BaseModel):
    local_threads: int | None = None
    use_lexicon: bool | None = None
    enable_llm: bool | None = None
    auto_title: bool | None = None
    llm_endpoint: str | None = None
    llm_api_key: str | None = None
    clear_api_key: bool = False
    llm_model: str | None = None
    llm_reasoning_effort: str | None = None
    auto_transcribe: bool | None = None
    keep_audio: bool | None = None
    # 直播静音自动停止与录制保护
    auto_stop_on_silence: bool | None = None
    silence_stop_seconds: int | None = None
    silence_rms_threshold: float | None = None
    min_record_seconds: int | None = None
    max_record_hours: int | None = None
    max_record_gb: int | None = None
    # 磁盘保留策略
    retention_days: int | None = None
    cleanup_delete_transcripts: bool | None = None
    cleanup_interval_hours: int | None = None


class CleanupReq(BaseModel):
    dry_run: bool = True


_DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>WXLive NAS · 远程直播录制与转写台</title>
<style>
  :root {
    --bg: #080a0f;
    --surface-1: #0e121a;
    --surface-2: #141925;
    --surface-3: #1b2232;
    --surface-hover: #1e2638;
    --border: rgba(255, 255, 255, 0.08);
    --border-strong: rgba(255, 255, 255, 0.15);
    --text: #eef2f6;
    --text-2: #94a3b8;
    --muted: #64748b;
    --accent: #38bdf8;
    --accent-solid: #0284c7;
    --accent-hover: #0369a1;
    --accent-dim: rgba(56, 189, 248, 0.13);
    --emerald: #34d399;
    --emerald-dim: rgba(52, 211, 153, 0.14);
    --amber: #fbbf24;
    --amber-dim: rgba(251, 191, 36, 0.14);
    --rose: #f87171;
    --rose-dim: rgba(248, 113, 113, 0.14);
    --violet: #a78bfa;
    --violet-dim: rgba(167, 139, 250, 0.14);
    --radius-lg: 14px;
    --radius: 10px;
    --radius-sm: 8px;
    --font-sans: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
    --font-mono: "JetBrains Mono", "Cascadia Code", ui-monospace, SFMono-Regular, Consolas, monospace;
    --shadow-card: 0 1px 0 rgba(255, 255, 255, 0.03) inset, 0 12px 32px -12px rgba(0, 0, 0, 0.55);
  }
  * { box-sizing: border-box; }
  html, body { height: 100%; }
  body {
    margin: 0;
    font-family: var(--font-sans);
    background:
      radial-gradient(ellipse 75% 42% at 50% -12%, rgba(56, 189, 248, 0.11), transparent 70%),
      radial-gradient(ellipse 50% 30% at 90% 0%, rgba(167, 139, 250, 0.06), transparent 65%),
      var(--bg);
    background-attachment: fixed;
    color: var(--text);
    line-height: 1.55;
    -webkit-font-smoothing: antialiased;
  }
  .num { font-variant-numeric: tabular-nums; }
  .mono { font-family: var(--font-mono); }

  /* ---------- Top Shell ---------- */
  .topbar {
    position: sticky; top: 0; z-index: 40;
    background: rgba(10, 13, 20, 0.82);
    backdrop-filter: blur(16px);
    -webkit-backdrop-filter: blur(16px);
    border-bottom: 1px solid var(--border);
  }
  .topbar-inner {
    max-width: 1380px; margin: 0 auto; padding: 10px 20px;
    display: flex; align-items: center; justify-content: space-between; gap: 16px;
    flex-wrap: wrap;
  }
  .brand { display: flex; align-items: center; gap: 11px; min-width: 0; }
  .brand-mark {
    width: 34px; height: 34px; border-radius: 10px; flex-shrink: 0;
    background: linear-gradient(135deg, rgba(56,189,248,0.22), rgba(14,165,233,0.08));
    border: 1px solid rgba(56,189,248,0.32);
    display: flex; align-items: center; justify-content: center; color: var(--accent);
    box-shadow: 0 0 20px rgba(56,189,248,0.12);
  }
  .brand-title { font-size: 15px; font-weight: 700; letter-spacing: -0.01em; display: flex; align-items: center; gap: 8px; }
  .brand-sub { font-size: 11px; color: var(--muted); display: block; line-height: 1.25; }

  .nav-tabs {
    display: flex; align-items: center; gap: 4px;
    background: var(--surface-1); padding: 4px; border-radius: 11px;
    border: 1px solid var(--border);
    overflow-x: auto; max-width: 100%;
  }
  .nav-tab {
    display: inline-flex; align-items: center; gap: 7px;
    padding: 7px 14px; border-radius: 8px; border: 1px solid transparent;
    background: transparent; color: var(--text-2); font-size: 13px; font-weight: 600;
    cursor: pointer; white-space: nowrap; transition: all 0.16s ease;
  }
  .nav-tab:hover { color: var(--text); background: rgba(255,255,255,0.04); }
  .nav-tab.active {
    background: var(--surface-3); color: #fff;
    border-color: var(--border-strong);
    box-shadow: 0 2px 8px rgba(0,0,0,0.35);
  }
  .nav-tab svg { width: 15px; height: 15px; opacity: 0.85; flex-shrink: 0; }
  .nav-tab.active svg { color: var(--accent); opacity: 1; }
  .nav-count {
    font-size: 11px; font-weight: 600; padding: 1px 6px; border-radius: 999px;
    background: rgba(255,255,255,0.07); color: var(--text-2);
    font-variant-numeric: tabular-nums;
  }
  .nav-count.live { background: var(--rose-dim); color: var(--rose); }
  .dot-ind { width: 7px; height: 7px; border-radius: 50%; background: var(--muted); }
  .dot-ind.on { background: var(--emerald); box-shadow: 0 0 8px rgba(52,211,153,0.6); }

  .telemetry-strip { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
  .tpill {
    display: inline-flex; align-items: center; gap: 6px;
    font-size: 11.5px; color: var(--text-2); padding: 4px 10px; border-radius: 999px;
    background: var(--surface-1); border: 1px solid var(--border);
    white-space: nowrap; font-variant-numeric: tabular-nums;
  }
  .tpill.clickable { cursor: pointer; transition: 0.15s; }
  .tpill.clickable:hover { border-color: var(--border-strong); color: var(--text); }
  .tpill.ok { color: var(--emerald); border-color: rgba(52,211,153,0.28); background: rgba(52,211,153,0.06); }
  .tpill.warn { color: var(--amber); border-color: rgba(251,191,36,0.28); background: rgba(251,191,36,0.06); }

  /* ---------- Container & Views ---------- */
  .app-main { max-width: 1380px; margin: 0 auto; padding: 18px 20px 44px; }
  .view-pane { display: none; animation: fadeIn 0.16s ease; }
  .view-pane.active { display: block; }
  @keyframes fadeIn { from { opacity: 0; transform: translateY(3px); } to { opacity: 1; transform: translateY(0); } }

  /* ---------- Cards & Controls ---------- */
  .card {
    background: var(--surface-2);
    border: 1px solid var(--border);
    border-radius: var(--radius-lg);
    padding: 18px;
    box-shadow: var(--shadow-card);
  }
  .card-header {
    display: flex; align-items: center; justify-content: space-between;
    gap: 12px; flex-wrap: wrap; margin-bottom: 14px;
  }
  .card-title { font-size: 14.5px; font-weight: 700; display: flex; align-items: center; gap: 8px; }
  .card-sub { font-size: 12px; color: var(--muted); margin-top: 2px; }

  button, .btn {
    display: inline-flex; align-items: center; justify-content: center; gap: 6px;
    cursor: pointer; border: 1px solid var(--border-strong); border-radius: var(--radius-sm);
    padding: 8px 13px; font-size: 12.5px; font-weight: 600; font-family: inherit;
    background: var(--surface-3); color: var(--text); transition: all 0.15s ease;
    text-decoration: none; white-space: nowrap;
  }
  button:hover:not(:disabled), .btn:hover { border-color: rgba(56,189,248,0.5); background: var(--surface-hover); color: #fff; }
  button:disabled { opacity: 0.45; cursor: not-allowed; }
  button.primary, .btn.primary {
    background: var(--accent-solid); border-color: rgba(56,189,248,0.45); color: #fff;
  }
  button.primary:hover:not(:disabled), .btn.primary:hover { background: var(--accent-hover); border-color: var(--accent); }
  button.ghost { background: transparent; border-color: transparent; color: var(--text-2); }
  button.ghost:hover:not(:disabled) { background: rgba(255,255,255,0.06); color: var(--text); border-color: transparent; }
  button.danger { background: rgba(248,113,113,0.12); border-color: rgba(248,113,113,0.35); color: #fca5a5; }
  button.danger:hover:not(:disabled) { background: rgba(248,113,113,0.22); border-color: rgba(248,113,113,0.6); color: #fff; }
  button.sm, .btn.sm { padding: 5px 10px; font-size: 12px; border-radius: 7px; }
  button svg, .btn svg { width: 14px; height: 14px; flex-shrink: 0; }

  input[type=text], input[type=number], input[type=password], select, textarea {
    width: 100%; padding: 9px 12px; border-radius: var(--radius-sm);
    border: 1px solid var(--border-strong); background: var(--surface-1); color: var(--text);
    font-size: 13px; font-family: inherit; outline: none; transition: border-color 0.15s, box-shadow 0.15s;
  }
  input:focus, select:focus, textarea:focus {
    border-color: var(--accent);
    box-shadow: 0 0 0 3px rgba(56, 189, 248, 0.12);
  }
  input::placeholder { color: var(--muted); }
  input[type=file] {
    width: 100%; padding: 7px 10px; border-radius: var(--radius-sm);
    border: 1px dashed var(--border-strong); background: var(--surface-1); color: var(--text-2);
    font-size: 12.5px; cursor: pointer;
  }

  .field { display: flex; flex-direction: column; gap: 6px; }
  .field label { font-size: 12px; font-weight: 500; color: var(--text-2); }
  .field .hint { font-size: 11.5px; color: var(--muted); line-height: 1.4; }
  .row { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; }

  /* Custom Switch */
  .switch-row {
    display: flex; align-items: center; justify-content: space-between; gap: 12px;
    padding: 10px 12px; border-radius: var(--radius); background: var(--surface-1);
    border: 1px solid var(--border); cursor: pointer;
  }
  .switch-row:hover { border-color: var(--border-strong); }
  .switch-info { display: flex; flex-direction: column; gap: 2px; }
  .switch-title { font-size: 13px; font-weight: 600; color: var(--text); }
  .switch-desc { font-size: 11.5px; color: var(--muted); }
  .switch {
    position: relative; width: 38px; height: 22px; flex-shrink: 0;
  }
  .switch input { opacity: 0; width: 0; height: 0; position: absolute; }
  .slider {
    position: absolute; inset: 0; border-radius: 999px;
    background: #273145; border: 1px solid var(--border-strong); transition: 0.2s;
  }
  .slider::before {
    content: ""; position: absolute; left: 2px; top: 2px; width: 16px; height: 16px;
    border-radius: 50%; background: #cbd5e1; transition: 0.2s;
  }
  .switch input:checked + .slider { background: var(--accent-solid); border-color: var(--accent); }
  .switch input:checked + .slider::before { transform: translateX(16px); background: #fff; }

  .chk-inline {
    display: inline-flex; align-items: center; gap: 8px;
    font-size: 12.5px; color: var(--text-2); cursor: pointer; user-select: none;
  }
  .chk-inline input { width: 15px; height: 15px; accent-color: var(--accent); cursor: pointer; }

  .notice {
    font-size: 12.5px; padding: 9px 12px; border-radius: var(--radius-sm);
    margin-top: 10px; display: flex; align-items: center; justify-content: space-between; gap: 8px;
  }
  .notice.ok { background: var(--emerald-dim); color: #a7f3d0; border: 1px solid rgba(52,211,153,0.3); }
  .notice.err { background: var(--rose-dim); color: #fecaca; border: 1px solid rgba(248,113,113,0.3); }
  .notice.warn { background: var(--amber-dim); color: #fde68a; border: 1px solid rgba(251,191,36,0.3); }

  /* ---------- Segmented Sub-tabs ---------- */
  .seg {
    display: inline-flex; gap: 3px; padding: 3px; border-radius: 9px;
    background: var(--surface-1); border: 1px solid var(--border);
  }
  .seg-btn {
    border: 1px solid transparent; background: transparent; color: var(--text-2);
    padding: 5px 12px; border-radius: 7px; font-size: 12px; font-weight: 600; cursor: pointer;
  }
  .seg-btn:hover { color: var(--text); background: transparent; border-color: transparent; }
  .seg-btn.active {
    background: var(--surface-3); color: var(--accent); border-color: var(--border-strong);
  }

  /* ---------- Workspace Tab ---------- */
  .dock { margin-bottom: 16px; padding: 15px 18px; }
  .dock-head {
    display: flex; align-items: center; justify-content: space-between; gap: 12px;
    flex-wrap: wrap; margin-bottom: 12px;
  }
  .dock-grid { display: grid; grid-template-columns: 1fr 220px auto; gap: 10px; align-items: end; }
  @media (max-width: 900px) { .dock-grid { grid-template-columns: 1fr; } }

  .workspace-split {
    display: grid; grid-template-columns: 370px minmax(0, 1fr); gap: 16px; align-items: stretch;
  }
  @media (max-width: 980px) { .workspace-split { grid-template-columns: 1fr; } }

  .job-list {
    display: flex; flex-direction: column; gap: 8px;
    max-height: calc(100vh - 300px); min-height: 360px; overflow-y: auto; padding-right: 2px;
  }
  .job-card {
    border: 1px solid var(--border); background: var(--surface-1);
    border-radius: var(--radius); padding: 11px 13px; cursor: pointer;
    transition: all 0.14s ease;
  }
  .job-card:hover { border-color: var(--border-strong); background: var(--surface-hover); }
  .job-card.selected {
    border-color: rgba(56,189,248,0.55);
    background: linear-gradient(135deg, rgba(56,189,248,0.09), rgba(20,25,37,0.9));
    box-shadow: 0 0 0 1px rgba(56,189,248,0.2);
  }
  .job-top { display: flex; align-items: flex-start; justify-content: space-between; gap: 8px; margin-bottom: 6px; }
  .job-title { font-size: 13.5px; font-weight: 600; color: var(--text); word-break: break-word; line-height: 1.35; }
  .job-meta {
    display: flex; flex-wrap: wrap; gap: 8px 12px; font-size: 11.5px; color: var(--text-2);
    font-variant-numeric: tabular-nums;
  }
  .job-stop-note {
    margin-top: 6px; font-size: 11px; color: var(--muted);
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }

  .badge {
    display: inline-flex; align-items: center; gap: 5px;
    font-size: 11px; padding: 2px 8px; border-radius: 999px; font-weight: 600; white-space: nowrap;
  }
  .sdot { width: 6px; height: 6px; border-radius: 50%; background: currentColor; }
  .pulse .sdot { animation: pulse 1.4s ease-in-out infinite; }
  @keyframes pulse { 0%, 100% { opacity: 1; transform: scale(1); } 50% { opacity: 0.35; transform: scale(0.75); } }
  .st-recording { background: var(--rose-dim); color: #fca5a5; }
  .st-queued, .st-converting { background: var(--amber-dim); color: #fde68a; }
  .st-transcribing, .st-postprocessing { background: var(--accent-dim); color: #7dd3fc; }
  .st-completed { background: var(--emerald-dim); color: #6ee7b7; }
  .st-failed, .st-interrupted { background: rgba(148,163,184,0.16); color: #cbd5e1; }
  .st-recorded { background: var(--violet-dim); color: #ddd6fe; }

  .progress-bar {
    height: 4px; background: rgba(255,255,255,0.06); border-radius: 999px; overflow: hidden; margin-top: 8px;
  }
  .progress-fill {
    height: 100%; background: linear-gradient(90deg, #0284c7, #38bdf8);
    border-radius: 999px; transition: width 0.35s ease;
  }

  /* Detail Stage */
  .stage { display: flex; flex-direction: column; min-height: 540px; }
  .empty-stage {
    flex: 1; display: flex; flex-direction: column; align-items: center; justify-content: center;
    text-align: center; padding: 48px 24px; color: var(--muted); gap: 10px;
  }
  .empty-icon {
    width: 48px; height: 48px; border-radius: 14px; background: var(--surface-1);
    border: 1px solid var(--border); display: flex; align-items: center; justify-content: center;
    color: var(--text-2); margin-bottom: 4px;
  }
  .det-head {
    display: flex; justify-content: space-between; align-items: flex-start;
    gap: 12px 16px; flex-wrap: wrap; padding-bottom: 14px; border-bottom: 1px solid var(--border);
  }
  .det-title { font-size: 16px; font-weight: 700; letter-spacing: -0.01em; margin: 0 0 8px; overflow-wrap: anywhere; }
  .chips { display: flex; flex-wrap: wrap; gap: 6px; }
  .chip {
    font-size: 11.5px; color: var(--text-2); background: var(--surface-1);
    border: 1px solid var(--border); padding: 2px 9px; border-radius: 999px;
    font-variant-numeric: tabular-nums;
  }
  .det-toolbar {
    display: flex; align-items: center; justify-content: space-between;
    gap: 10px; flex-wrap: wrap; margin: 12px 0;
  }

  /* Editorial Markdown Reader */
  .reader-box {
    background: var(--surface-1); border: 1px solid var(--border); border-radius: var(--radius);
    padding: 18px 22px; max-height: calc(100vh - 340px); min-height: 360px; overflow-y: auto;
  }
  .md-prose { max-width: 78ch; font-size: 13.5px; line-height: 1.75; color: #dbe3ee; }
  .md-prose h1 { font-size: 18px; font-weight: 700; margin: 0 0 12px; color: #fff; padding-bottom: 8px; border-bottom: 1px solid var(--border); }
  .md-prose h2 { font-size: 15px; font-weight: 700; margin: 20px 0 8px; color: var(--accent); }
  .md-prose h3 { font-size: 13.5px; font-weight: 700; margin: 14px 0 6px; color: #e2e8f0; }
  .md-prose p { margin: 0 0 9px; }
  .md-prose ul { margin: 4px 0 12px; padding-left: 20px; }
  .md-prose li { margin: 4px 0; }
  .md-prose blockquote {
    margin: 10px 0; padding: 8px 14px; border-left: 3px solid var(--accent);
    background: rgba(56,189,248,0.06); border-radius: 0 8px 8px 0; color: var(--text-2);
  }
  .md-prose hr { border: none; border-top: 1px solid var(--border-strong); margin: 20px 0; }
  .md-prose code {
    font-family: var(--font-mono); font-size: 12px; background: rgba(255,255,255,0.07);
    padding: 1px 5px; border-radius: 4px; color: #bae6fd;
  }
  .md-prose .ts {
    font-family: var(--font-mono); font-size: 11px; color: var(--accent);
    background: var(--accent-dim); padding: 1px 6px; border-radius: 5px; margin-right: 6px;
  }
  pre.raw-viewer {
    margin: 0; white-space: pre-wrap; word-break: break-word;
    font-family: var(--font-mono); font-size: 12.5px; line-height: 1.65; color: #cbd5e1;
  }

  /* ---------- Library Tab ---------- */
  .stats-row {
    display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; margin-bottom: 16px;
  }
  @media (max-width: 720px) { .stats-row { grid-template-columns: 1fr; } }
  .stat-tile {
    background: var(--surface-2); border: 1px solid var(--border); border-radius: var(--radius-lg);
    padding: 15px 18px; display: flex; flex-direction: column; gap: 4px;
  }
  .stat-label { font-size: 12px; color: var(--muted); }
  .stat-val { font-size: 22px; font-weight: 700; letter-spacing: -0.02em; font-variant-numeric: tabular-nums; }
  .stat-sub { font-size: 11.5px; color: var(--text-2); }

  .lib-split {
    display: grid; grid-template-columns: minmax(320px, 430px) minmax(0, 1fr); gap: 16px; align-items: start;
  }
  @media (max-width: 980px) { .lib-split { grid-template-columns: 1fr; } }
  .lib-list {
    display: flex; flex-direction: column; gap: 8px;
    max-height: calc(100vh - 300px); min-height: 360px; overflow-y: auto; padding-right: 2px;
  }
  .lib-preview {
    font-size: 12px; color: var(--text-2); margin-top: 6px; line-height: 1.45;
    display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
  }

  /* ---------- Storage Tab ---------- */
  .table-wrap { overflow-x: auto; border: 1px solid var(--border); border-radius: var(--radius); margin-top: 12px; }
  table.clean { width: 100%; border-collapse: collapse; font-size: 12.5px; }
  table.clean th {
    text-align: left; padding: 10px 12px; font-size: 11.5px; font-weight: 600;
    color: var(--muted); background: var(--surface-1); border-bottom: 1px solid var(--border);
  }
  table.clean td { padding: 9px 12px; border-bottom: 1px solid var(--border); color: var(--text-2); }
  table.clean tr:last-child td { border-bottom: none; }
  table.clean tr:hover td { background: rgba(255,255,255,0.02); }

  /* ---------- Settings Bento ---------- */
  .settings-grid {
    display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; align-items: start;
  }
  @media (max-width: 920px) { .settings-grid { grid-template-columns: 1fr; } }
  .form-stack { display: flex; flex-direction: column; gap: 12px; }
  .form-2col { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
  @media (max-width: 540px) { .form-2col { grid-template-columns: 1fr; } }
  .save-bar {
    margin-bottom: 16px;
    background: var(--surface-2);
    border: 1px solid var(--border); border-radius: var(--radius-lg);
    padding: 13px 18px; display: flex; align-items: center; justify-content: space-between;
    gap: 12px; flex-wrap: wrap; box-shadow: var(--shadow-card);
  }

  /* ---------- Modal & Toast ---------- */
  .modal-bg {
    position: fixed; inset: 0; background: rgba(2, 6, 23, 0.72);
    backdrop-filter: blur(6px); display: none; align-items: center; justify-content: center;
    padding: 16px; z-index: 60;
  }
  .modal-bg.open { display: flex; }
  .modal {
    width: 100%; max-width: 440px; background: var(--surface-2);
    border: 1px solid var(--border-strong); border-radius: var(--radius-lg);
    padding: 20px; box-shadow: 0 24px 60px rgba(0,0,0,0.65);
  }
  .toast-wrap {
    position: fixed; right: 20px; bottom: 20px; z-index: 80;
    display: flex; flex-direction: column; gap: 8px; pointer-events: none;
  }
  .toast {
    pointer-events: auto; padding: 10px 15px; border-radius: 10px; font-size: 13px; font-weight: 500;
    background: var(--surface-3); border: 1px solid var(--border-strong); color: var(--text);
    box-shadow: 0 10px 28px rgba(0,0,0,0.5); animation: slideUp 0.18s ease;
    max-width: 360px;
  }
  .toast.ok { border-color: rgba(52,211,153,0.45); color: #a7f3d0; }
  .toast.err { border-color: rgba(248,113,113,0.45); color: #fecaca; }
  @keyframes slideUp { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: translateY(0); } }

  /* Subtle scrollbars */
  ::-webkit-scrollbar { width: 7px; height: 7px; }
  ::-webkit-scrollbar-track { background: transparent; }
  ::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.12); border-radius: 999px; }
  ::-webkit-scrollbar-thumb:hover { background: rgba(255,255,255,0.22); }
</style>
</head>
<body>

<header class="topbar">
  <div class="topbar-inner">
    <div class="brand">
      <div class="brand-mark" aria-hidden="true">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.1" stroke-linecap="round">
          <path d="M12 3v18M8 7v10M16 7v10M4 10v4M20 10v4"/>
        </svg>
      </div>
      <div>
        <div class="brand-title">WXLive NAS</div>
        <span class="brand-sub">飞牛 fnOS 远程直播录制与转写台</span>
      </div>
    </div>

    <nav class="nav-tabs" aria-label="主导航">
      <button class="nav-tab active" data-tab="workspace" onclick="switchTab('workspace')">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="9" rx="1.5"/><rect x="14" y="3" width="7" height="5" rx="1.5"/><rect x="14" y="12" width="7" height="9" rx="1.5"/><rect x="3" y="16" width="7" height="5" rx="1.5"/></svg>
        工作台
        <span id="nav-active-badge" class="nav-count" style="display:none;">0</span>
      </button>
      <button class="nav-tab" data-tab="library" onclick="switchTab('library')">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/></svg>
        文件库
        <span id="nav-lib-badge" class="nav-count">0</span>
      </button>
      <button class="nav-tab" data-tab="storage" onclick="switchTab('storage')">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/><path d="M3 12c0 1.66 4 3 9 3s9-1.34 9-3"/></svg>
        存储清理
        <span id="nav-clean-dot" class="dot-ind" title="自动清理状态"></span>
      </button>
      <button class="nav-tab" data-tab="settings" onclick="switchTab('settings')">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>
        系统设置
      </button>
    </nav>

    <div class="telemetry-strip">
      <span id="pill-ffmpeg" class="tpill">ffmpeg: --</span>
      <span id="pill-engine" class="tpill">SenseVoice: 待加载</span>
      <span id="pill-disk" class="tpill">磁盘: --</span>
      <span id="pill-cleanup" class="tpill clickable" onclick="switchTab('storage')">清理: --</span>
    </div>
  </div>
</header>

<main class="app-main">
  <!-- ==================== TAB 1: 工作台 ==================== -->
  <section id="view-workspace" class="view-pane active">
    <div class="card dock">
      <div class="dock-head">
        <div class="seg" role="tablist">
          <button id="tab-stream-btn" class="seg-btn active" onclick="switchInputTab('stream')">直播流直录</button>
          <button id="tab-upload-btn" class="seg-btn" onclick="switchInputTab('upload')">上传音频 / 回放</button>
        </div>
        <span style="font-size:12px;color:var(--muted);">支持粘贴纯流 URL 或抓流工具复制的整条 <span class="mono">ffmpeg -i ...</span> 命令</span>
      </div>

      <div id="pane-stream">
        <div class="dock-grid">
          <div class="field">
            <label>直播流地址</label>
            <input id="stream-url" type="text" placeholder='https://pull-m1.wxlivecdn.com/...flv?txTime=... 或 ffmpeg -i "https://..." -c copy -y live.flv'>
          </div>
          <div class="field">
            <label>场次名称（可选）</label>
            <input id="stream-name" type="text" placeholder="留空则转写整理后自动识别命名">
          </div>
          <div class="row">
            <button onclick="probeStream()">探测流</button>
            <button class="primary" onclick="startStream()">开始拉流录制</button>
          </div>
        </div>
        <div class="row" style="justify-content:space-between;margin-top:10px;">
          <label class="chk-inline"><input id="stream-auto-tx" type="checkbox" checked> 停录后自动 SenseVoice 转写与 AI 整理</label>
        </div>
        <div id="stream-feedback"></div>
      </div>

      <div id="pane-upload" style="display:none;">
        <div class="dock-grid">
          <div class="field">
            <label>本地音视频文件（.wav .mp3 .m4a .mp4 .aac .flac .flv .mkv）</label>
            <input id="upload-file" type="file" accept=".wav,.mp3,.m4a,.mp4,.aac,.flac,.ogg,.opus,.flv,.mkv,.webm">
          </div>
          <div class="field">
            <label>任务名称（可选）</label>
            <input id="upload-name" type="text" placeholder="留空则使用文件名">
          </div>
          <div class="row">
            <button id="upload-btn" class="primary" onclick="uploadFile()">上传并转写</button>
          </div>
        </div>
        <div style="font-size:12px;color:var(--muted);margin-top:8px;" id="upload-progress-text">上传后自动转为 16k 单声道 WAV 并进入后台串行转写队列</div>
        <div id="upload-feedback"></div>
      </div>
    </div>

    <div class="workspace-split">
      <!-- Left Rail: 任务列表 -->
      <div class="card" style="padding:14px;">
        <div class="card-header" style="margin-bottom:10px;">
          <div class="card-title">任务队列</div>
          <div class="row" style="gap:6px;">
            <div class="seg" style="padding:2px;">
              <button class="seg-btn active" data-filter="all" onclick="setJobFilter('all')">全部</button>
              <button class="seg-btn" data-filter="active" onclick="setJobFilter('active')">进行中</button>
              <button class="seg-btn" data-filter="done" onclick="setJobFilter('done')">已完成</button>
            </div>
            <button class="sm ghost" onclick="refreshAll()" title="刷新">↻</button>
          </div>
        </div>
        <div id="job-list" class="job-list">
          <div class="empty-stage" style="min-height:240px;">暂无任务，粘贴流地址或上传音频即可开始。</div>
        </div>
      </div>

      <!-- Right Stage: 任务详情 -->
      <div class="card stage">
        <div id="detail-empty" class="empty-stage">
          <div class="empty-icon">
            <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg>
          </div>
          <div style="font-size:14px;color:var(--text-2);font-weight:600;">选择一个任务查看详情</div>
          <div style="font-size:12.5px;">支持实时监控录制进度、查看运行日志、阅读逐字稿与 AI 结构化要点。</div>
        </div>

        <div id="detail-body" style="display:none;">
          <div class="det-head">
            <div style="min-width:0;flex:1;">
              <h2 id="det-title" class="det-title"></h2>
              <div id="det-meta" class="chips"></div>
            </div>
            <div id="det-actions" class="row" style="gap:6px;"></div>
          </div>

          <div class="det-toolbar">
            <div class="seg">
              <button id="vtab-md" class="seg-btn active" onclick="switchViewTab('md')">逐字稿 & AI 要点</button>
              <button id="vtab-logs" class="seg-btn" onclick="switchViewTab('logs')">运行日志</button>
            </div>
            <div id="md-tools" class="row" style="gap:6px;">
              <button id="md-mode-btn" class="sm" onclick="toggleMdMode()">查看源码</button>
              <button class="sm" onclick="copyMarkdown()">复制全文</button>
            </div>
          </div>

          <div id="view-md" class="reader-box">
            <div id="md-rendered" class="md-prose"></div>
            <pre id="md-raw" class="raw-viewer" style="display:none;"></pre>
          </div>
          <div id="view-logs" class="reader-box" style="display:none;">
            <pre id="log-viewer" class="raw-viewer"></pre>
          </div>
        </div>
      </div>
    </div>
  </section>

  <!-- ==================== TAB 2: 文件库 ==================== -->
  <section id="view-library" class="view-pane">
    <div class="stats-row">
      <div class="stat-tile">
        <span class="stat-label">已存档逐字稿</span>
        <span id="lib-stat-count" class="stat-val">--</span>
        <span class="stat-sub">按磁盘真实目录扫描（不受任务表限制）</span>
      </div>
      <div class="stat-tile">
        <span class="stat-label">文库占用空间</span>
        <span id="lib-stat-size" class="stat-val">--</span>
        <span class="stat-sub">纯文本体积小，自动清理默认永久保留</span>
      </div>
      <div class="stat-tile">
        <span class="stat-label">导出备份</span>
        <div class="row" style="margin-top:4px;">
          <a href="/api/library/export" class="btn primary sm">打包导出全部（ZIP）</a>
          <button class="sm" onclick="refreshLibrary()">刷新列表</button>
        </div>
        <span class="stat-sub">含全部 .md / .json / .jsonl 产物</span>
      </div>
    </div>

    <div class="lib-split">
      <div class="card" style="padding:14px;">
        <div class="row" style="margin-bottom:10px;gap:8px;">
          <input id="lib-search" type="text" placeholder="按标题或目录名搜索…" style="flex:1 1 160px;" oninput="renderLibrary()">
          <select id="lib-sort" style="width:auto;" onchange="renderLibrary()">
            <option value="mtime">最新优先</option>
            <option value="duration">时长最长</option>
            <option value="sentences">句数最多</option>
          </select>
        </div>
        <div id="lib-list" class="lib-list">
          <div class="empty-stage" style="min-height:220px;">加载中…</div>
        </div>
      </div>

      <div class="card stage">
        <div id="lib-reader-empty" class="empty-stage">
          <div class="empty-icon">
            <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M2 3h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2z"/><path d="M22 3h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z"/></svg>
          </div>
          <div style="font-size:14px;color:var(--text-2);font-weight:600;">在左侧选择一份逐字稿阅读</div>
          <div style="font-size:12.5px;">无需下载到本地，可直接在网页里排版阅读与复制全文。</div>
        </div>
        <div id="lib-reader-body" style="display:none;">
          <div class="det-head">
            <div style="min-width:0;flex:1;">
              <h2 id="lib-det-title" class="det-title"></h2>
              <div id="lib-det-meta" class="chips"></div>
            </div>
            <div id="lib-det-actions" class="row" style="gap:6px;"></div>
          </div>
          <div class="reader-box" style="margin-top:12px;">
            <div id="lib-md-rendered" class="md-prose"></div>
          </div>
        </div>
      </div>
    </div>
  </section>

  <!-- ==================== TAB 3: 存储与清理 ==================== -->
  <section id="view-storage" class="view-pane">
    <div class="stats-row">
      <div class="stat-tile">
        <span class="stat-label">NAS 可用磁盘空间</span>
        <span id="st-disk-free" class="stat-val">-- GB</span>
        <span class="stat-sub">数据目录：/app/data</span>
      </div>
      <div class="stat-tile">
        <span class="stat-label">自动保留策略</span>
        <span id="st-policy-title" class="stat-val" style="font-size:18px;">--</span>
        <span id="st-policy-sub" class="stat-sub">读取中…</span>
      </div>
      <div class="stat-tile">
        <span class="stat-label">最近自动/手动清理</span>
        <span id="st-last-title" class="stat-val" style="font-size:18px;">暂无记录</span>
        <span id="st-last-sub" class="stat-sub">后台按设置间隔自动巡检</span>
      </div>
    </div>

    <div class="card">
      <div class="card-header">
        <div>
          <div class="card-title">磁盘巡检与手工清理</div>
          <div class="card-sub">先预览将清理的过期音频与中断残骸，确认后再执行；默认保留全部逐字稿文本。</div>
        </div>
        <div class="row">
          <button onclick="switchTab('settings')">调整保留天数</button>
          <button onclick="previewCleanup()">扫描预览待清理项</button>
          <button class="danger" onclick="askRunCleanup()">立即执行清理</button>
        </div>
      </div>
      <div id="cleanup-feedback"></div>
      <div id="cleanup-preview">
        <div style="font-size:13px;color:var(--muted);padding:16px 0;">点击「扫描预览待清理项」查看当前可释放空间与文件明细。</div>
      </div>
    </div>
  </section>

  <!-- ==================== TAB 4: 系统设置 ==================== -->
  <section id="view-settings" class="view-pane">
    <div class="save-bar">
      <div style="font-size:12.5px;color:var(--text-2);" id="settings-dirty-hint">修改配置后点击右侧保存，立即对后续任务生效</div>
      <div class="row">
        <button onclick="loadSettingsForm(true)">重新加载</button>
        <button class="primary" onclick="saveSettings()">保存全部设置</button>
      </div>
    </div>

    <div class="settings-grid">
      <!-- Card 1 -->
      <div class="card form-stack">
        <div class="card-header" style="margin-bottom:4px;">
          <div>
            <div class="card-title">录制与静音保护</div>
            <div class="card-sub">控制直播流自动判停时机与防写满上限</div>
          </div>
        </div>
        <label class="switch-row">
          <div class="switch-info">
            <span class="switch-title">直播静音自动停止</span>
            <span class="switch-desc">连续静音达阈值后自动结束拉流并进入转写队列</span>
          </div>
          <span class="switch"><input id="set-auto-stop-silence" type="checkbox"><span class="slider"></span></span>
        </label>
        <div class="form-2col">
          <div class="field">
            <label>最短录制保护（分钟）</label>
            <input id="set-min-minutes" type="number" min="0" max="240" step="1" value="5">
            <span class="hint">开播前 N 分钟内不因静音自动收工（0 = 不保护，默认 5 分钟）</span>
          </div>
          <div class="field">
            <label>连续静音判定（秒）</label>
            <input id="set-silence-seconds" type="number" min="10" max="600" value="40">
            <span class="hint">本地实测直播结束通常有约 40 秒连续静音</span>
          </div>
        </div>
        <div class="form-2col">
          <div class="field">
            <label>静音阈值 RMS</label>
            <input id="set-silence-rms" type="number" min="0.000001" max="0.05" step="0.0005" value="0.002">
            <span class="hint">默认 0.002，越大越容易判为静音</span>
          </div>
          <div class="field">
            <label>单任务最长录制（小时，0=不限）</label>
            <input id="set-max-hours" type="number" min="0" max="72" value="8">
          </div>
        </div>
        <div class="field">
          <label>单任务最大体积（GB，0=不限）</label>
          <input id="set-max-gb" type="number" min="0" max="100" value="2">
          <span class="hint">兜底异常永不结束且持续有底噪的流，避免写满磁盘</span>
        </div>
      </div>

      <!-- Card 2 -->
      <div class="card form-stack">
        <div class="card-header" style="margin-bottom:4px;">
          <div>
            <div class="card-title">SenseVoice 转写引擎</div>
            <div class="card-sub">本地 ONNX 推理性能与产物保留选项</div>
          </div>
        </div>
        <div class="field">
          <label>SenseVoice 本地推理线程数</label>
          <input id="set-threads" type="number" min="1" max="16" value="4">
          <span class="hint">i7-6700 四核推荐设为 4，过高反而引发线程争用</span>
        </div>
        <label class="switch-row">
          <div class="switch-info">
            <span class="switch-title">启用 XSC 领域词表纠正与暗语展开</span>
            <span class="switch-desc">自动识别 SS/HF/GY 等升学简称与常见同音误识别</span>
          </div>
          <span class="switch"><input id="set-lexicon" type="checkbox"><span class="slider"></span></span>
        </label>
        <label class="switch-row">
          <div class="switch-info">
            <span class="switch-title">转写完成后保留原始音频</span>
            <span class="switch-desc">保留 16k WAV 以便重跑转写或人工核对（配合自动清理防爆盘）</span>
          </div>
          <span class="switch"><input id="set-keep-audio" type="checkbox"><span class="slider"></span></span>
        </label>
      </div>

      <!-- Card 3 -->
      <div class="card form-stack">
        <div class="card-header" style="margin-bottom:4px;">
          <div>
            <div class="card-title">AI 纠错与结构化要点（LLM）</div>
            <div class="card-sub">OpenAI 兼容接口，转写完成后自动纠错并提炼要点</div>
          </div>
          <button class="sm" onclick="testLlm()">测试连通性</button>
        </div>
        <label class="switch-row">
          <div class="switch-info">
            <span class="switch-title">启用 AI 纠错与要点整理</span>
            <span class="switch-desc">关闭时仅输出 SenseVoice 逐字稿，速度最快</span>
          </div>
          <span class="switch"><input id="set-enable-llm" type="checkbox"><span class="slider"></span></span>
        </label>
        <label class="switch-row">
          <div class="switch-info">
            <span class="switch-title">自动识别直播名称（AI 命名）</span>
            <span class="switch-desc">未指定名称（或仅为数字 ID）的任务，整理后自动提炼主播与主题</span>
          </div>
          <span class="switch"><input id="set-auto-title" type="checkbox"><span class="slider"></span></span>
        </label>
        <div class="field">
          <label>LLM Endpoint</label>
          <input id="set-endpoint" type="text" placeholder="https://api.deepseek.com/v1/chat/completions">
        </div>
        <div class="field">
          <label>API Key（留空保持不变）</label>
          <div class="row" style="gap:8px;">
            <input id="set-apikey" type="password" style="flex:1;" placeholder="输入新 Key 即替换">
            <button class="sm" onclick="markClearApiKey()" title="清除已保存的 API Key">清除 Key</button>
          </div>
        </div>
        <div class="form-2col">
          <div class="field">
            <label>文本模型</label>
            <input id="set-model" type="text" placeholder="group/deepseek">
          </div>
          <div class="field">
            <label>思考强度</label>
            <select id="set-effort">
              <option value="none">none（无推理）</option>
              <option value="low">low（推荐：快且稳）</option>
              <option value="high">high（深度慢）</option>
              <option value="max">max</option>
            </select>
          </div>
        </div>
        <div id="llm-test-feedback"></div>
      </div>

      <!-- Card 4 -->
      <div class="card form-stack">
        <div class="card-header" style="margin-bottom:4px;">
          <div>
            <div class="card-title">磁盘自动清理策略</div>
            <div class="card-sub">定期清理过期录音 WAV，防止 7×24 运行撑爆存储</div>
          </div>
        </div>
        <div class="form-2col">
          <div class="field">
            <label>音频保留天数（0 = 关闭自动清理）</label>
            <input id="set-retention" type="number" min="0" max="3650" value="0">
            <span class="hint">建议设为 90 天；1 小时音频约 110 MB</span>
          </div>
          <div class="field">
            <label>自动检查间隔（小时）</label>
            <input id="set-clean-interval" type="number" min="1" max="720" value="24">
          </div>
        </div>
        <label class="switch-row">
          <div class="switch-info">
            <span class="switch-title">同时清理过期逐字稿（不推荐）</span>
            <span class="switch-desc">默认仅清理大体积音频，永久保留文字稿与 AI 要点</span>
          </div>
          <span class="switch"><input id="set-clean-transcripts" type="checkbox"><span class="slider"></span></span>
        </label>
      </div>
    </div>
  </section>
</main>

<!-- Rename Modal -->
<div id="rename-modal" class="modal-bg" onclick="if(event.target===this)closeRename(false)">
  <div class="modal">
    <div style="font-size:16px;font-weight:700;margin-bottom:8px;">重命名任务</div>
    <div style="font-size:12.5px;color:var(--text-2);margin-bottom:10px;">将同步更新任务队列、文件库及逐字稿标题。</div>
    <input id="rename-input" type="text" maxlength="120" placeholder="输入新的直播场次名称" onkeydown="if(event.key==='Enter')closeRename(true);else if(event.key==='Escape')closeRename(false);">
    <div class="row" style="justify-content:flex-end;margin-top:14px;">
      <button onclick="closeRename(false)">取消</button>
      <button class="primary" onclick="closeRename(true)">保存</button>
    </div>
  </div>
</div>

<!-- Confirm Modal -->
<div id="confirm-modal" class="modal-bg" onclick="if(event.target===this)closeConfirm(false)">
  <div class="modal">
    <div style="font-size:16px;font-weight:700;margin-bottom:8px;" id="confirm-title">确认操作</div>
    <div style="font-size:13px;color:var(--text-2);line-height:1.6;margin-bottom:18px;" id="confirm-msg"></div>
    <div class="row" style="justify-content:flex-end;">
      <button onclick="closeConfirm(false)">取消</button>
      <button id="confirm-ok-btn" class="danger" onclick="closeConfirm(true)">确认</button>
    </div>
  </div>
</div>

<div id="toast-wrap" class="toast-wrap"></div>

<script>
const STATUS_LABEL = {
  recording: '录制中',
  recorded: '已录待转',
  queued: '排队中',
  converting: '音频转码',
  transcribing: '转写中',
  postprocessing: 'AI 整理中',
  completed: '已完成',
  failed: '失败',
  interrupted: '已中断'
};
const ACTIVE_SET = new Set(['recording', 'queued', 'converting', 'transcribing', 'postprocessing']);

let activeTab = 'workspace';
let jobs = [];
let jobFilter = 'all';
let selectedId = null;
let currentViewTab = 'md';
let mdRawMode = false;
let currentMdText = '';
let libItems = [];
let selectedLibStem = null;
let libCache = {};
let settingsLoaded = false;
let clearKeyPending = false;
let pollTimer = null;
let confirmResolver = null;

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function fmtDur(sec) {
  sec = Math.max(0, Math.round(sec || 0));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return h ? `${h}:${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}` : `${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`;
}
function fmtMB(bytes) {
  return ((bytes || 0) / 1048576).toFixed(1) + ' MB';
}
function fmtKB(bytes) {
  const v = (bytes || 0) / 1024;
  return v >= 1024 ? (v / 1024).toFixed(1) + ' MB' : v.toFixed(1) + ' KB';
}
function fmtTime(ts) {
  if (!ts) return '—';
  const d = new Date(ts * 1000);
  const p = n => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}
function toast(msg, kind = 'ok') {
  const wrap = document.getElementById('toast-wrap');
  const el = document.createElement('div');
  el.className = `toast ${kind}`;
  el.textContent = msg;
  wrap.appendChild(el);
  setTimeout(() => { el.style.opacity = '0'; el.style.transition = 'opacity 0.2s'; setTimeout(() => el.remove(), 220); }, 2800);
}
function askConfirm(title, msg, okText = '确认', isDanger = true) {
  return new Promise(resolve => {
    confirmResolver = resolve;
    document.getElementById('confirm-title').textContent = title;
    document.getElementById('confirm-msg').textContent = msg;
    const btn = document.getElementById('confirm-ok-btn');
    btn.textContent = okText;
    btn.className = isDanger ? 'danger' : 'primary';
    document.getElementById('confirm-modal').classList.add('open');
  });
}
function closeConfirm(ok) {
  document.getElementById('confirm-modal').classList.remove('open');
  if (confirmResolver) { confirmResolver(ok); confirmResolver = null; }
}
function showNotice(elId, kind, msg) {
  const cls = kind === true ? 'ok' : (kind === false ? 'err' : kind);
  document.getElementById(elId).innerHTML =
    `<div class="notice ${cls}"><span>${esc(msg)}</span><button class="sm ghost" onclick="this.parentElement.remove()">✕</button></div>`;
}
async function api(path, opts = {}) {
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || data.error || ('HTTP ' + res.status));
  return data;
}

/* ---------- Safe Editorial Markdown Renderer ---------- */
function renderMarkdownSafe(md) {
  if (!md) return '<p style="color:var(--muted);">（暂无内容）</p>';
  const lines = String(md).replace(/\r\n/g, '\n').split('\n');
  let html = '';
  let inUl = false;
  const closeUl = () => { if (inUl) { html += '</ul>'; inUl = false; } };
  const inlineFmt = raw => {
    let s = esc(raw);
    s = s.replace(/`([^`]+)`/g, '<code>$1</code>');
    s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    s = s.replace(/\[(\d{1,2}:\d{2}(?::\d{2})?)\]/g, '<span class="ts">$1</span>');
    return s;
  };
  for (const line of lines) {
    const t = line.trim();
    if (!t) { closeUl(); continue; }
    if (/^---+$/.test(t)) { closeUl(); html += '<hr>'; continue; }
    if (t.startsWith('### ')) { closeUl(); html += `<h3>${inlineFmt(t.slice(4))}</h3>`; continue; }
    if (t.startsWith('## '))  { closeUl(); html += `<h2>${inlineFmt(t.slice(3))}</h2>`; continue; }
    if (t.startsWith('# '))   { closeUl(); html += `<h1>${inlineFmt(t.slice(2))}</h1>`; continue; }
    if (t.startsWith('> '))   { closeUl(); html += `<blockquote>${inlineFmt(t.slice(2))}</blockquote>`; continue; }
    if (/^[-*]\s+/.test(t)) {
      if (!inUl) { html += '<ul>'; inUl = true; }
      html += `<li>${inlineFmt(t.replace(/^[-*]\s+/, ''))}</li>`;
      continue;
    }
    closeUl();
    html += `<p>${inlineFmt(line)}</p>`;
  }
  closeUl();
  return html;
}

/* ---------- Top Tab Navigation ---------- */
function switchTab(tab, pushHash = true) {
  if (!['workspace', 'library', 'storage', 'settings'].includes(tab)) tab = 'workspace';
  activeTab = tab;
  document.querySelectorAll('.nav-tab').forEach(b => b.classList.toggle('active', b.dataset.tab === tab));
  document.querySelectorAll('.view-pane').forEach(p => p.classList.toggle('active', p.id === 'view-' + tab));
  if (pushHash && location.hash !== '#' + tab) history.replaceState(null, '', '#' + tab);
  if (tab === 'library') refreshLibrary();
  if (tab === 'storage') refreshCleanup();
  if (tab === 'settings' && !settingsLoaded) loadSettingsForm(false);
}

function switchInputTab(t) {
  document.getElementById('pane-stream').style.display = t === 'stream' ? '' : 'none';
  document.getElementById('pane-upload').style.display = t === 'upload' ? '' : 'none';
  document.getElementById('tab-stream-btn').classList.toggle('active', t === 'stream');
  document.getElementById('tab-upload-btn').classList.toggle('active', t === 'upload');
}
function switchViewTab(t) {
  currentViewTab = t;
  document.getElementById('view-md').style.display = t === 'md' ? '' : 'none';
  document.getElementById('view-logs').style.display = t === 'logs' ? '' : 'none';
  document.getElementById('md-tools').style.display = t === 'md' ? 'flex' : 'none';
  document.getElementById('vtab-md').classList.toggle('active', t === 'md');
  document.getElementById('vtab-logs').classList.toggle('active', t === 'logs');
}
function toggleMdMode() {
  mdRawMode = !mdRawMode;
  document.getElementById('md-rendered').style.display = mdRawMode ? 'none' : '';
  document.getElementById('md-raw').style.display = mdRawMode ? '' : 'none';
  document.getElementById('md-mode-btn').textContent = mdRawMode ? '阅读视图' : '查看源码';
}
function setJobFilter(f) {
  jobFilter = f;
  document.querySelectorAll('[data-filter]').forEach(b => b.classList.toggle('active', b.dataset.filter === f));
  renderJobs();
}

/* ---------- Health & Jobs ---------- */
async function refreshHealth() {
  try {
    const h = await api('/api/health');
    const pf = document.getElementById('pill-ffmpeg');
    pf.textContent = 'ffmpeg: ' + (h.ffmpeg_available ? '可用' : '缺失');
    pf.className = 'tpill ' + (h.ffmpeg_available ? 'ok' : 'warn');
    const pe = document.getElementById('pill-engine');
    pe.textContent = 'SenseVoice: ' + (h.engine_loaded ? '已常驻' : '按需加载');
    pe.className = 'tpill ' + (h.engine_loaded ? 'ok' : '');
    document.getElementById('pill-disk').textContent = `剩余 ${h.disk_free_gb} GB`;
    document.getElementById('st-disk-free').textContent = `${h.disk_free_gb} GB`;
  } catch (e) {}
}

async function refreshJobs() {
  try {
    const data = await api('/api/jobs');
    jobs = data.jobs || [];
    const activeCnt = jobs.filter(j => ACTIVE_SET.has(j.status)).length;
    const badge = document.getElementById('nav-active-badge');
    if (activeCnt > 0) {
      badge.textContent = String(activeCnt);
      badge.className = 'nav-count live';
      badge.style.display = '';
    } else {
      badge.style.display = 'none';
    }
    renderJobs();
    if (selectedId) {
      const found = jobs.find(j => j.id === selectedId);
      if (found) await renderDetail(found);
      else {
        selectedId = jobs.length ? jobs[0].id : null;
        if (selectedId) await selectJob(selectedId);
      }
    } else if (jobs.length) {
      await selectJob(jobs[0].id);
    }
    clearTimeout(pollTimer);
    pollTimer = setTimeout(refreshAll, activeCnt > 0 ? 2500 : 10000);
  } catch (e) {}
}

function renderJobs() {
  const box = document.getElementById('job-list');
  const filtered = jobs.filter(j => {
    if (jobFilter === 'active') return ACTIVE_SET.has(j.status);
    if (jobFilter === 'done') return j.status === 'completed';
    return true;
  });
  if (!filtered.length) {
    box.innerHTML = `<div class="empty-stage" style="min-height:220px;">${
      jobs.length ? '当前筛选下无任务。' : '暂无任务，粘贴流地址或上传音频即可开始。'
    }</div>`;
    return;
  }
  box.innerHTML = filtered.map(j => {
    const sel = j.id === selectedId ? 'selected' : '';
    const isAct = ACTIVE_SET.has(j.status);
    const pct = j.status === 'completed' ? 100 : (j.progress_pct || 0);
    const bar = ['transcribing', 'postprocessing', 'converting'].includes(j.status)
      ? `<div class="progress-bar"><div class="progress-fill" style="width:${pct}%"></div></div>` : '';
    return `<div class="job-card ${sel}" onclick="selectJob('${esc(j.id)}')">
      <div class="job-top">
        <span class="job-title">${esc(j.title)}</span>
        <span class="badge st-${esc(j.status)} ${isAct ? 'pulse' : ''}"><span class="sdot"></span>${esc(STATUS_LABEL[j.status] || j.status)}</span>
      </div>
      <div class="job-meta">
        <span>${j.kind === 'stream' ? '直播流' : '上传'}</span>
        <span>${fmtDur(j.audio_seconds)}</span>
        <span>${fmtMB(j.audio_bytes)}</span>
        ${j.sentence_count ? `<span>${j.sentence_count} 句</span>` : ''}
        ${j.rtf ? `<span>RTF ${j.rtf}</span>` : ''}
      </div>
      ${j.stop_reason ? `<div class="job-stop-note">${esc(j.stop_reason_text || j.stop_reason)}</div>` : ''}
      ${j.error ? `<div style="color:var(--rose);font-size:11.5px;margin-top:5px;">${esc(j.error)}</div>` : ''}
      ${bar}
    </div>`;
  }).join('');
}

async function selectJob(id) {
  selectedId = id;
  renderJobs();
  const j = jobs.find(x => x.id === id);
  if (j) await renderDetail(j);
}

async function renderDetail(j) {
  document.getElementById('detail-empty').style.display = 'none';
  document.getElementById('detail-body').style.display = '';
  document.getElementById('det-title').textContent = j.title;
  const chips = [
    `状态: ${STATUS_LABEL[j.status] || j.status}`,
    `时长: ${fmtDur(j.audio_seconds)}`,
    `音频: ${fmtMB(j.audio_bytes)}`,
  ];
  if (j.sentence_count) chips.push(`句数: ${j.sentence_count}`);
  if (j.changed_count) chips.push(`AI 纠错: ${j.changed_count} 处`);
  if (j.llm_status && !j.llm_status.startsWith('skipped')) chips.push(`AI: ${j.llm_status}`);
  if (j.stop_reason) chips.push(j.stop_reason_text || j.stop_reason);
  if (j.truncated) chips.push('录音可能不完整');
  document.getElementById('det-meta').innerHTML = chips.map(c => `<span class="chip">${esc(c)}</span>`).join('');

  const acts = [];
  acts.push(`<button class="sm" onclick="renameJobPrompt('${esc(j.id)}')">重命名</button>`);
  if ((j.has_json || j.has_md) && !ACTIVE_SET.has(j.status)) {
    acts.push(`<button class="sm" onclick="autoTitleJob('${esc(j.id)}')">AI 识别命名</button>`);
  }
  if (j.status === 'recording') {
    acts.push(`<button class="primary sm" onclick="stopStream('${esc(j.id)}', true)">停止并转写</button>`);
    acts.push(`<button class="sm" onclick="stopStream('${esc(j.id)}', false)">仅停止</button>`);
  }
  if (['recorded','interrupted','failed'].includes(j.status) && j.has_audio) {
    acts.push(`<button class="primary sm" onclick="retranscribe('${esc(j.id)}')">开始转写</button>`);
  }
  if (j.has_json && j.status === 'completed') {
    acts.push(`<button class="sm" onclick="repostprocess('${esc(j.id)}')">重跑 AI 整理</button>`);
  }
  if (j.has_md) acts.push(`<a class="btn sm" href="/api/jobs/${encodeURIComponent(j.id)}/download/md">MD</a>`);
  if (j.has_json) acts.push(`<a class="btn sm" href="/api/jobs/${encodeURIComponent(j.id)}/download/json">JSON</a>`);
  if (j.has_audio && j.status !== 'recording') acts.push(`<a class="btn sm" href="/api/jobs/${encodeURIComponent(j.id)}/download/wav">WAV</a>`);
  if (!['recording','transcribing','converting','postprocessing'].includes(j.status)) {
    acts.push(`<button class="danger sm" onclick="delJob('${esc(j.id)}')">删除</button>`);
  }
  document.getElementById('det-actions').innerHTML = acts.join('');

  try {
    const det = await api(`/api/jobs/${encodeURIComponent(j.id)}`);
    document.getElementById('log-viewer').textContent = (det.logs || []).join('\n') || '（暂无日志）';
    if (j.has_md) {
      const tr = await api(`/api/jobs/${encodeURIComponent(j.id)}/transcript`);
      currentMdText = tr.md_text || '';
      document.getElementById('md-rendered').innerHTML = renderMarkdownSafe(currentMdText);
      document.getElementById('md-raw').textContent = currentMdText || '（逐字稿为空）';
    } else {
      currentMdText = '';
      const placeholder = j.status === 'recording'
        ? '正在拉流录制中… 直播结束后将自动收工并进入 SenseVoice 转写。'
        : '（尚未生成逐字稿，可切换至「运行日志」查看当前进度）';
      document.getElementById('md-rendered').innerHTML = `<p style="color:var(--muted);">${esc(placeholder)}</p>`;
      document.getElementById('md-raw').textContent = placeholder;
    }
  } catch (e) {}
}

/* ---------- Stream & Upload Actions ---------- */
async function probeStream() {
  const url = document.getElementById('stream-url').value.trim();
  if (!url) return showNotice('stream-feedback', false, '请先粘贴流地址或 ffmpeg 命令');
  showNotice('stream-feedback', 'warn', '正在用 ffprobe 探测流地址…');
  try {
    const r = await api('/api/probe', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({url})
    });
    if (r.ok) showNotice('stream-feedback', true, `探测成功：格式 ${r.format || '未知'}，含音轨=${r.has_audio}`);
    else showNotice('stream-feedback', false, '探测失败：' + (r.error || '未知错误'));
  } catch (e) { showNotice('stream-feedback', false, e.message); }
}
async function startStream() {
  const url = document.getElementById('stream-url').value.trim();
  const name = document.getElementById('stream-name').value.trim();
  const auto_transcribe = document.getElementById('stream-auto-tx').checked;
  if (!url) return showNotice('stream-feedback', false, '请先粘贴流地址或 ffmpeg 命令');
  try {
    const j = await api('/api/jobs/stream', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({url, name, auto_transcribe})
    });
    document.getElementById('stream-url').value = '';
    document.getElementById('stream-name').value = '';
    showNotice('stream-feedback', true, `已启动拉流录制：${j.title}`);
    selectedId = j.id;
    await refreshAll();
  } catch (e) { showNotice('stream-feedback', false, e.message); }
}
async function stopStream(id, transcribe) {
  try {
    await api(`/api/jobs/${encodeURIComponent(id)}/stop`, {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({transcribe})
    });
    toast('已发送停止指令');
    await refreshAll();
  } catch (e) { toast(e.message, 'err'); }
}
async function retranscribe(id) {
  try {
    await api(`/api/jobs/${encodeURIComponent(id)}/transcribe`, {method:'POST'});
    toast('已重新加入转写队列');
    await refreshAll();
  } catch (e) { toast(e.message, 'err'); }
}
let renameJobId = null;
function renameJobPrompt(id) {
  const j = jobs.find(x => x.id === id);
  renameJobId = id;
  const inp = document.getElementById('rename-input');
  inp.value = j ? (j.title || '') : '';
  document.getElementById('rename-modal').classList.add('open');
  setTimeout(() => { inp.focus(); inp.select(); }, 30);
}
async function closeRename(save) {
  const modal = document.getElementById('rename-modal');
  const id = renameJobId;
  const title = document.getElementById('rename-input').value.trim();
  modal.classList.remove('open');
  renameJobId = null;
  if (!save || !id) return;
  if (!title) return toast('名称不能为空', 'err');
  try {
    await api(`/api/jobs/${encodeURIComponent(id)}`, {
      method: 'PATCH', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({title})
    });
    toast('已重命名');
    await refreshAll();
  } catch (e) { toast(e.message, 'err'); }
}
async function autoTitleJob(id) {
  toast('正在用 AI 识别直播名称…');
  try {
    const r = await api(`/api/jobs/${encodeURIComponent(id)}/auto-title`, {method:'POST'});
    toast(`已识别命名：${r.title}`);
    await refreshAll();
  } catch (e) { toast(e.message, 'err'); }
}
async function repostprocess(id) {
  try {
    await api(`/api/jobs/${encodeURIComponent(id)}/postprocess`, {method:'POST'});
    toast('AI 重新整理完成');
    await refreshAll();
  } catch (e) { toast(e.message, 'err'); }
}
async function delJob(id) {
  const ok = await askConfirm('删除任务', '确认删除该任务记录及关联音频/逐字稿文件？此操作不可恢复。', '确认删除', true);
  if (!ok) return;
  try {
    await api(`/api/jobs/${encodeURIComponent(id)}?delete_files=true`, {method:'DELETE'});
    if (selectedId === id) selectedId = null;
    toast('任务已删除');
    await refreshAll();
  } catch (e) { toast(e.message, 'err'); }
}
function uploadFile() {
  const fileEl = document.getElementById('upload-file');
  const file = fileEl.files && fileEl.files[0];
  if (!file) return showNotice('upload-feedback', false, '请先选择音频或视频文件');
  const fd = new FormData();
  fd.append('file', file);
  fd.append('name', document.getElementById('upload-name').value.trim());
  const btn = document.getElementById('upload-btn');
  const prog = document.getElementById('upload-progress-text');
  btn.disabled = true;
  const xhr = new XMLHttpRequest();
  xhr.open('POST', '/api/jobs/upload');
  xhr.upload.onprogress = e => {
    if (e.lengthComputable) prog.textContent = `正在上传：${Math.round(e.loaded / e.total * 100)}%`;
  };
  xhr.onload = async () => {
    btn.disabled = false;
    prog.textContent = '上传后自动转为 16k 单声道 WAV 并进入后台串行转写队列';
    try {
      const data = JSON.parse(xhr.responseText || '{}');
      if (xhr.status >= 400) throw new Error(data.detail || ('HTTP ' + xhr.status));
      fileEl.value = '';
      document.getElementById('upload-name').value = '';
      showNotice('upload-feedback', true, `已加入转写队列：${data.title}`);
      selectedId = data.id;
      await refreshAll();
    } catch (err) {
      showNotice('upload-feedback', false, err.message);
    }
  };
  xhr.onerror = () => {
    btn.disabled = false;
    showNotice('upload-feedback', false, '上传网络中断');
  };
  xhr.send(fd);
}
async function copyMarkdown() {
  if (!currentMdText) return toast('当前没有可复制的内容', 'err');
  await navigator.clipboard.writeText(currentMdText);
  toast('已复制全文到剪贴板');
}

/* ---------- Library Tab ---------- */
async function refreshLibrary() {
  try {
    const data = await api('/api/library');
    libItems = data.items || [];
    document.getElementById('nav-lib-badge').textContent = String(data.count || 0);
    document.getElementById('lib-stat-count').textContent = `${data.count || 0} 份`;
    document.getElementById('lib-stat-size').textContent = fmtKB(data.total_bytes || 0);
    renderLibrary();
  } catch (e) {
    document.getElementById('lib-list').innerHTML = `<div class="notice err">读取失败：${esc(e.message)}</div>`;
  }
}
function renderLibrary() {
  const q = (document.getElementById('lib-search').value || '').trim().toLowerCase();
  const sortBy = document.getElementById('lib-sort').value;
  const box = document.getElementById('lib-list');
  let items = q
    ? libItems.filter(i => (i.title || '').toLowerCase().includes(q) || (i.stem || '').toLowerCase().includes(q))
    : [...libItems];
  if (sortBy === 'duration') items.sort((a, b) => (b.audio_seconds || 0) - (a.audio_seconds || 0));
  else if (sortBy === 'sentences') items.sort((a, b) => (b.sentence_count || 0) - (a.sentence_count || 0));
  else items.sort((a, b) => (b.mtime || 0) - (a.mtime || 0));

  if (!items.length) {
    box.innerHTML = `<div class="empty-stage" style="min-height:220px;">${
      libItems.length ? '没有匹配的条目。' : '暂无转录文件，跑完一场转写后会出现在这里。'
    }</div>`;
    return;
  }
  box.innerHTML = items.map(it => {
    const sel = it.stem === selectedLibStem ? 'selected' : '';
    const stemEnc = encodeURIComponent(it.stem);
    return `<div class="job-card ${sel}" onclick="openLibItem('${esc(it.stem)}')">
      <div class="job-top">
        <span class="job-title">${esc(it.title)}</span>
        <span class="chip">${fmtKB(it.size_bytes)}</span>
      </div>
      <div class="job-meta">
        <span>${fmtTime(it.mtime)}</span>
        ${it.audio_seconds ? `<span>${fmtDur(it.audio_seconds)}</span>` : ''}
        ${it.sentence_count ? `<span>${it.sentence_count} 句</span>` : ''}
        <span>${it.file_count} 文件</span>
      </div>
      ${it.preview ? `<div class="lib-preview">${esc(it.preview)}</div>` : ''}
      <div class="row" style="margin-top:8px;gap:6px;" onclick="event.stopPropagation()">
        ${it.has_md ? `<button class="sm primary" onclick="openLibItem('${esc(it.stem)}')">阅读</button>` : ''}
        ${it.has_md ? `<a class="btn sm" href="/api/library/${stemEnc}/download/md">MD</a>` : ''}
        ${it.has_json ? `<a class="btn sm" href="/api/library/${stemEnc}/download/json">JSON</a>` : ''}
      </div>
    </div>`;
  }).join('');
}
async function openLibItem(stem) {
  selectedLibStem = stem;
  renderLibrary();
  const it = libItems.find(x => x.stem === stem);
  if (!it) return;
  document.getElementById('lib-reader-empty').style.display = 'none';
  document.getElementById('lib-reader-body').style.display = '';
  document.getElementById('lib-det-title').textContent = it.title;
  const meta = [fmtTime(it.mtime), fmtKB(it.size_bytes)];
  if (it.audio_seconds) meta.push(`时长 ${fmtDur(it.audio_seconds)}`);
  if (it.sentence_count) meta.push(`${it.sentence_count} 句`);
  document.getElementById('lib-det-meta').innerHTML = meta.map(m => `<span class="chip">${esc(m)}</span>`).join('');
  const stemEnc = encodeURIComponent(stem);
  const acts = [`<button class="sm" onclick="copyLibMd('${esc(stem)}')">复制全文</button>`];
  if (it.has_md) acts.push(`<a class="btn sm" href="/api/library/${stemEnc}/download/md">下载 MD</a>`);
  if (it.has_json) acts.push(`<a class="btn sm" href="/api/library/${stemEnc}/download/json">下载 JSON</a>`);
  document.getElementById('lib-det-actions').innerHTML = acts.join('');

  if (!it.has_md) {
    document.getElementById('lib-md-rendered').innerHTML = '<p style="color:var(--muted);">该条目暂无 Markdown 文件。</p>';
    return;
  }
  if (!libCache[stem]) {
    document.getElementById('lib-md-rendered').innerHTML = '<p style="color:var(--muted);">正在加载逐字稿…</p>';
    try {
      const res = await fetch(`/api/library/${stemEnc}/download/md`);
      if (!res.ok) throw new Error('HTTP ' + res.status);
      libCache[stem] = await res.text();
    } catch (e) {
      document.getElementById('lib-md-rendered').innerHTML = `<div class="notice err">加载失败：${esc(e.message)}</div>`;
      return;
    }
  }
  document.getElementById('lib-md-rendered').innerHTML = renderMarkdownSafe(libCache[stem]);
}
async function copyLibMd(stem) {
  const txt = libCache[stem];
  if (!txt) return toast('请等待逐字稿加载完成', 'err');
  await navigator.clipboard.writeText(txt);
  toast('已复制全文到剪贴板');
}

/* ---------- Storage & Cleanup Tab ---------- */
async function refreshCleanup() {
  try {
    const s = await api('/api/cleanup/status');
    const dot = document.getElementById('nav-clean-dot');
    const pill = document.getElementById('pill-cleanup');
    dot.className = 'dot-ind ' + (s.auto_enabled ? 'on' : '');
    if (s.auto_enabled) {
      pill.textContent = `清理: ${s.retention_days}天`;
      pill.className = 'tpill clickable ok';
      document.getElementById('st-policy-title').textContent = `保留 ${s.retention_days} 天`;
      document.getElementById('st-policy-sub').textContent =
        `每 ${s.cleanup_interval_hours} 小时巡检 · ${s.cleanup_delete_transcripts ? '音频+逐字稿' : '仅清音频，永久保留逐字稿'}`;
    } else {
      pill.textContent = '清理: 关闭';
      pill.className = 'tpill clickable';
      document.getElementById('st-policy-title').textContent = '已关闭';
      document.getElementById('st-policy-sub').textContent = '保留天数 = 0（可到「系统设置」开启）';
    }
    const last = s.last_cleanup || {};
    if (last.at && (last.ok !== false || s.auto_enabled)) {
      document.getElementById('st-last-title').textContent =
        `删 ${last.deleted_count || 0} 项 · 释放 ${last.freed_human || ((last.freed_mb || 0) + ' MB')}`;
      document.getElementById('st-last-sub').textContent =
        `${last.at}${last.error ? '（异常：' + last.error + '）' : ''}`;
    } else {
      document.getElementById('st-last-title').textContent = '暂无执行记录';
      document.getElementById('st-last-sub').textContent = s.auto_enabled ? '后台将按周期自动巡检' : '自动清理未开启';
    }
  } catch (e) {}
}
async function previewCleanup() {
  const box = document.getElementById('cleanup-preview');
  box.innerHTML = '<div style="color:var(--muted);font-size:13px;padding:12px 0;">正在扫描目录并计算可释放空间…</div>';
  try {
    const p = await api('/api/cleanup/plan', {method:'POST'});
    if (!p.enabled) {
      box.innerHTML = '<div class="notice warn">当前保留天数设为 0，自动清理处于关闭状态。可前往「系统设置」设置天数（建议 90 天）。</div>';
      return;
    }
    if (!p.count) {
      box.innerHTML = '<div class="notice ok">扫描完成：没有过期内容，所有录音均在保留期内。</div>';
      return;
    }
    const rows = (p.candidates || []).slice(0, 100).map(c => `<tr>
      <td style="color:var(--text);font-weight:500;">${esc(c.name)}</td>
      <td class="num">${c.size_mb} MB</td>
      <td class="num">${c.age_days} 天</td>
      <td>${esc(c.reason)}</td>
    </tr>`).join('');
    const skipped = (p.skipped || []).length
      ? `<div class="notice warn">${p.skipped.length} 项被跳过（任务进行中或路径受保护）</div>` : '';
    box.innerHTML = `
      <div class="notice ok">
        <span>预计清理 <b>${p.count}</b> 项，合计可释放 <b>${esc(p.total_human || (p.total_mb + ' MB'))}</b>（保留阈值 ${p.retention_days} 天${p.delete_transcripts ? '，含逐字稿' : '，不含逐字稿'}）</span>
      </div>
      <div class="table-wrap">
        <table class="clean">
          <thead><tr><th>文件</th><th>大小</th><th>已存在</th><th>原因</th></tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
      ${skipped}`;
  } catch (e) {
    box.innerHTML = `<div class="notice err">扫描失败：${esc(e.message)}</div>`;
  }
}
async function askRunCleanup() {
  const p = await api('/api/cleanup/plan', {method:'POST'}).catch(() => null);
  if (!p || !p.enabled) return toast('自动清理未开启（保留天数为 0），请先在系统设置中配置天数', 'err');
  if (!p.count) return toast('没有需要清理的内容');
  const ok = await askConfirm(
    '确认执行磁盘清理',
    `将永久删除 ${p.count} 个过期文件，预计释放 ${p.total_human || (p.total_mb + ' MB')}。此操作不可撤销。`,
    '立即删除',
    true
  );
  if (!ok) return;
  try {
    const r = await api('/api/cleanup/run', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({dry_run: false})
    });
    const success = r.ok !== false;
    showNotice('cleanup-feedback', success,
      `已清理 ${r.deleted_count} 项，释放 ${r.freed_human || (r.freed_mb + ' MB')}` +
      (r.failed_count ? `，${r.failed_count} 项失败` : '') +
      (r.error ? `（${r.error}）` : ''));
    document.getElementById('cleanup-preview').innerHTML = '';
    await Promise.all([refreshLibrary(), refreshCleanup(), refreshHealth()]);
  } catch (e) { showNotice('cleanup-feedback', false, e.message); }
}

/* ---------- Settings Tab ---------- */
async function loadSettingsForm(showToast = false) {
  try {
    const s = await api('/api/settings');
    document.getElementById('set-threads').value = s.local_threads || 4;
    document.getElementById('set-lexicon').checked = !!s.use_lexicon;
    document.getElementById('set-keep-audio').checked = !!s.keep_audio;
    document.getElementById('set-enable-llm').checked = !!s.enable_llm;
    document.getElementById('set-auto-title').checked = s.auto_title !== false;
    document.getElementById('set-endpoint').value = s.llm_endpoint || '';
    document.getElementById('set-apikey').value = '';
    document.getElementById('set-apikey').placeholder = s.has_api_key ? `已保存 (${s.llm_api_key_masked})` : '输入 API Key';
    clearKeyPending = false;
    document.getElementById('set-model').value = s.llm_model || '';
    document.getElementById('set-effort').value = s.llm_reasoning_effort || 'low';
    document.getElementById('set-auto-stop-silence').checked = s.auto_stop_on_silence !== false;
    document.getElementById('set-min-minutes').value = Math.round(((s.min_record_seconds ?? 300) / 60) * 10) / 10;
    document.getElementById('set-silence-seconds').value = (s.silence_stop_seconds ?? 40);
    document.getElementById('set-silence-rms').value = (s.silence_rms_threshold ?? 0.002);
    document.getElementById('set-max-hours').value = (s.max_record_hours ?? 8);
    document.getElementById('set-max-gb').value = (s.max_record_gb ?? 2);
    document.getElementById('set-retention').value = (s.retention_days ?? 0);
    document.getElementById('set-clean-transcripts').checked = !!s.cleanup_delete_transcripts;
    document.getElementById('set-clean-interval').value = (s.cleanup_interval_hours ?? 24);
    settingsLoaded = true;
    document.getElementById('settings-dirty-hint').textContent = '当前表单与服务端配置同步';
    if (showToast) toast('已重新加载服务端配置');
  } catch (e) { toast('读取设置失败：' + e.message, 'err'); }
}
function markClearApiKey() {
  clearKeyPending = true;
  document.getElementById('set-apikey').value = '';
  document.getElementById('set-apikey').placeholder = '保存后将清除已配置的 API Key';
  document.getElementById('settings-dirty-hint').textContent = '待保存：将清除 API Key';
}
async function saveSettings(silent = false) {
  const minMins = Number(document.getElementById('set-min-minutes').value || 0);
  const body = {
    local_threads: Number(document.getElementById('set-threads').value || 4),
    use_lexicon: document.getElementById('set-lexicon').checked,
    keep_audio: document.getElementById('set-keep-audio').checked,
    enable_llm: document.getElementById('set-enable-llm').checked,
    auto_title: document.getElementById('set-auto-title').checked,
    llm_endpoint: document.getElementById('set-endpoint').value.trim(),
    llm_model: document.getElementById('set-model').value.trim(),
    llm_reasoning_effort: document.getElementById('set-effort').value,
    auto_stop_on_silence: document.getElementById('set-auto-stop-silence').checked,
    min_record_seconds: Math.max(0, Math.round(minMins * 60)),
    silence_stop_seconds: Number(document.getElementById('set-silence-seconds').value || 40),
    silence_rms_threshold: Number(document.getElementById('set-silence-rms').value || 0.002),
    max_record_hours: Number(document.getElementById('set-max-hours').value || 0),
    max_record_gb: Number(document.getElementById('set-max-gb').value || 0),
    retention_days: Number(document.getElementById('set-retention').value || 0),
    cleanup_delete_transcripts: document.getElementById('set-clean-transcripts').checked,
    cleanup_interval_hours: Number(document.getElementById('set-clean-interval').value || 24),
  };
  const key = document.getElementById('set-apikey').value.trim();
  if (clearKeyPending && !key) body.clear_api_key = true;
  else if (key) body.llm_api_key = key;

  try {
    await api('/api/settings', {
      method: 'PUT', headers: {'Content-Type':'application/json'},
      body: JSON.stringify(body)
    });
    clearKeyPending = false;
    await loadSettingsForm(false);
    await Promise.all([refreshLibrary(), refreshCleanup(), refreshHealth()]);
    if (!silent) toast('全部设置已保存');
  } catch (e) {
    toast('保存失败：' + e.message, 'err');
    throw e;
  }
}
async function testLlm() {
  try {
    await saveSettings(true);
    showNotice('llm-test-feedback', 'warn', '正在测试 LLM 接口连通性…');
    const r = await api('/api/settings/test-llm', {method:'POST'});
    if (r.ok) showNotice('llm-test-feedback', true, `连通正常（耗时 ${r.seconds}s，模型 ${r.model}）：${r.reply}`);
    else showNotice('llm-test-feedback', false, `测试失败（${r.seconds}s）：${r.error}`);
  } catch (e) { showNotice('llm-test-feedback', false, e.message); }
}

document.getElementById('view-settings').addEventListener('input', () => {
  document.getElementById('settings-dirty-hint').textContent = '有未保存的更改，记得点击「保存全部设置」';
});

async function refreshAll() {
  await Promise.all([refreshHealth(), refreshJobs(), refreshLibrary(), refreshCleanup()]);
}
(function init() {
  const h = (location.hash || '').replace(/^#/, '');
  if (h) switchTab(h, false);
  refreshAll();
})();
</script>
</body>
</html>
"""


def _valid_library_stem(stem: str) -> bool:
    """校验文件库目录名（允许中文，拒绝路径穿越）。

    目录名来自 Session 的 `_safe_part(audio_stem)`：保留全部 Unicode
    字母数字、`-`、`_`，因此中文标题是合法且常见的情况。
    这里只拦真正危险的东西：路径分隔符、空字节、控制字符、
    以点开头的隐藏/相对路径名、过长名字。
    """
    if not stem or len(stem) > 120:
        return False
    if stem.startswith("."):
        return False
    if any(ch in stem for ch in ("/", "\\", "\x00")):
        return False
    if any(ord(ch) < 32 for ch in stem):
        return False
    return True


def create_app(
    data_dir: Path | str | None = None,
    runner: NasJobRunner | None = None,
    require_token: str | None = None,
) -> FastAPI:
    """创建 FastAPI 实例；支持注入 runner / data_dir 以便测试隔离。

    ## 鉴权（对立性审查 C3）

    服务默认绑 `0.0.0.0`，且 `/api/cleanup/run` 是**破坏性**端点。
    局域网内任何主机都能直接 POST 删数据，恶意网页还能借 DNS rebinding 绕过同源。

    所以加一层可选 token：
    - `require_token` 为空字符串/None → 不校验（保持兼容，本地/隧道场景）
    - 非空 → 所有 `POST/PUT/DELETE` 必须带 `X-WXLIVE-Token` 头或 `?token=` 查询参数

    启动时用 `--token` 或环境变量 `WXLIVE_TOKEN` 开启。
    另外始终校验 `Host` 头，挡住 DNS rebinding（只放行 IP 字面量与 localhost）。
    """
    job_runner = runner or NasJobRunner(data_dir=data_dir)
    _token = (require_token if require_token is not None
              else os.environ.get("WXLIVE_TOKEN", "")).strip()
    _MUTATING = {"POST", "PUT", "DELETE", "PATCH"}

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        if runner is None:
            job_runner.shutdown()

    app = FastAPI(title="WXLive NAS Service", version="0.1.0", lifespan=lifespan)
    app.state.runner = job_runner

    @app.middleware("http")
    async def _guard(request, call_next):
        """Host 白名单 + 可选 token 校验。

        Host 白名单防 DNS rebinding：攻击者把自己的域名解析到 192.168.x.x，
        浏览器就会认为是同源。只放行 IP 字面量 / localhost / 配置的额外域名。
        """
        host = (request.headers.get("host") or "").split(":")[0].strip().lower()

        # 允许：IP 字面量、localhost、容器内主机名
        import re as _re
        is_ip = bool(_re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", host or ""))
        is_ipv6 = ":" in (request.headers.get("host") or "")
        allowed_host = (
            not host
            or is_ip
            or is_ipv6
            or host in (
                "localhost",
                "wxlive-nas",
                "host.docker.internal",
                # TestClient（httpx）默认用 testserver 作为 Host，
                # 不放行的话所有单元测试都会被自己的防护挡住
                "testserver",
            )
            or host.endswith(".local")
        )
        if not allowed_host:
            from fastapi.responses import JSONResponse

            return JSONResponse(
                {"detail": f"拒绝的 Host 头：{host}（防 DNS rebinding）。"
                           "如需域名访问请通过反向代理并配置白名单。"},
                status_code=400,
            )

        # token 校验只作用于写操作；GET 保持开放，便于直接用浏览器/curl 查看
        if _token and request.method in _MUTATING:
            provided = (
                request.headers.get("X-WXLIVE-Token")
                or request.query_params.get("token")
                or ""
            )
            if provided != _token:
                from fastapi.responses import JSONResponse

                return JSONResponse(
                    {"detail": "需要有效的 X-WXLIVE-Token 头或 ?token= 参数"},
                    status_code=401,
                )

        return await call_next(request)

    @app.get("/", response_class=HTMLResponse)
    def index():
        return HTMLResponse(_DASHBOARD_HTML)

    @app.get("/api/health")
    def health():
        usage = shutil.disk_usage(str(job_runner.data_dir))
        jobs = job_runner.list_jobs()
        active = sum(
            1
            for j in jobs
            if j.get("status") in ("recording", "queued", "converting", "transcribing", "postprocessing")
        )
        # WXLIVE_FFMPEG 配置失效时 find_ffmpeg 会抛错（有意为之，避免静默回退）。
        # 健康接口不能因此 500 —— 相反要把原因摆在明面上。
        ff_error = ""
        try:
            ff = find_ffmpeg()
        except Exception as exc:  # noqa: BLE001
            ff = None
            ff_error = str(exc)[:300]
        return {
            "ok": True,
            "ffmpeg_available": bool(ff),
            "ffmpeg_path": ff or "",
            "ffmpeg_error": ff_error,
            "engine_loaded": job_runner.engine_loaded(),
            "disk_free_gb": round(usage.free / (1024**3), 1),
            "job_count": len(jobs),
            "active_count": active,
        }

    @app.post("/api/probe")
    def api_probe(req: ProbeReq):
        # 允许直接粘贴整条 ffmpeg 命令（微信下载按钮复制出来的就是这个形态）
        url = extract_url(req.url)
        if not is_url(url):
            return {
                "ok": False,
                "error": "没识别出流地址。可以粘贴纯 URL，或整条 `ffmpeg -i \"https://...\" ...` 命令。",
            }
        return probe_url(url, timeout=15, decrypt_key=req.decrypt_key)

    @app.get("/api/jobs")
    def api_list_jobs():
        return {"jobs": job_runner.list_jobs()}

    @app.get("/api/jobs/{job_id}")
    def api_get_job(job_id: str):
        j = job_runner.get_job(job_id)
        if not j:
            raise HTTPException(status_code=404, detail="任务不存在")
        return j

    @app.post("/api/jobs/stream")
    def api_start_stream(req: StreamStartReq):
        try:
            return job_runner.start_stream_job(
                url=req.url,
                name=req.name,
                auto_transcribe=req.auto_transcribe,
                decrypt_key=req.decrypt_key,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except RuntimeError as e:
            raise HTTPException(status_code=500, detail=str(e))

    @app.post("/api/jobs/{job_id}/stop")
    def api_stop_stream(job_id: str, req: StopReq | None = None):
        try:
            return job_runner.stop_stream_job(
                job_id, transcribe=req.transcribe if req else None
            )
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.post("/api/jobs/upload")
    def api_upload(
        file: UploadFile = File(...),
        name: str = Form(""),
        auto_transcribe: bool = Form(True),
    ):
        try:
            return job_runner.create_upload_job(
                filename=file.filename or "audio.wav",
                file_obj=file.file,
                name=name,
                auto_transcribe=auto_transcribe,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        finally:
            try:
                file.file.close()
            except Exception:
                pass

    @app.post("/api/jobs/{job_id}/transcribe")
    def api_retranscribe(job_id: str):
        try:
            return job_runner.enqueue_transcribe(job_id)
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except FileNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.post("/api/jobs/{job_id}/postprocess")
    def api_repostprocess(job_id: str):
        try:
            return job_runner.repostprocess_job(job_id)
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except FileNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.patch("/api/jobs/{job_id}")
    def api_rename_job(job_id: str, req: RenameJobReq):
        try:
            return job_runner.rename_job(job_id, req.title)
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.post("/api/jobs/{job_id}/auto-title")
    def api_auto_title_job(job_id: str):
        try:
            return job_runner.auto_title_job(job_id)
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except RuntimeError as e:
            raise HTTPException(status_code=502, detail=str(e))

    @app.get("/api/jobs/{job_id}/transcript")
    def api_transcript(job_id: str):
        try:
            return job_runner.get_transcript(job_id)
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e))

    @app.get("/api/jobs/{job_id}/download/{kind}")
    def api_download(job_id: str, kind: str):
        j = job_runner.get_job(job_id)
        if not j:
            raise HTTPException(status_code=404, detail="任务不存在")
        field_map = {"md": "md_path", "json": "json_path", "wav": "audio_path"}
        if kind not in field_map:
            raise HTTPException(status_code=400, detail="只支持下载 md / json / wav")
        raw_p = j.get(field_map[kind]) or ""
        p = Path(raw_p)
        if not raw_p or not p.exists():
            raise HTTPException(status_code=404, detail="文件尚未生成或已被清理")
        return FileResponse(path=str(p), filename=p.name)

    @app.delete("/api/jobs/{job_id}")
    def api_delete(job_id: str, delete_files: bool = False):
        try:
            ok = job_runner.delete_job(job_id, delete_files=delete_files)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        if not ok:
            raise HTTPException(status_code=404, detail="任务不存在")
        return {"ok": True}

    @app.get("/api/settings")
    def api_get_settings():
        return job_runner.get_settings()

    @app.put("/api/settings")
    def api_put_settings(req: SettingsUpdateReq):
        try:
            return job_runner.update_settings(req.model_dump(exclude_unset=True))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.post("/api/settings/test-llm")
    def api_test_llm():
        return job_runner.test_llm()

    # ---------------- 磁盘保留策略 ----------------

    @app.get("/api/cleanup/status")
    def api_cleanup_status():
        return job_runner.cleanup_status()

    @app.post("/api/cleanup/plan")
    def api_cleanup_plan():
        """预览：将删除什么（不执行）。"""
        return job_runner.run_cleanup(dry_run=True, trigger="manual")

    @app.post("/api/cleanup/run")
    def api_cleanup_run(req: CleanupReq | None = None):
        """执行清理。默认 dry_run=True 防误触；真删必须显式传 false。"""
        dry = True if req is None else bool(req.dry_run)
        return job_runner.run_cleanup(dry_run=dry, trigger="manual")

    # ---------------- 转录文件库 ----------------

    @app.get("/api/library")
    def api_library():
        items = job_runner.library()
        total = sum(i.get("size_bytes", 0) for i in items)
        return {"count": len(items), "total_bytes": total,
                "total_kb": round(total / 1024, 1), "items": items}

    @app.get("/api/library/export")
    def api_library_export():
        try:
            payload, filename = job_runner.export_library()
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except ValueError as exc:
            # 超出内存上限：给 413 而不是 500，前端能给出可执行的提示
            raise HTTPException(status_code=413, detail=str(exc))
        from fastapi.responses import Response

        return Response(
            content=payload,
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.get("/api/library/{stem}/download/{kind}")
    def api_library_download(stem: str, kind: str):
        """下载单个转录产物的某个文件（md/json/jsonl）。"""
        # stem 来自用户输入，必须先校验，避免目录穿越。
        # 注意：**不能用 ASCII 正则** —— 目录名来自录音文件名/直播标题，
        # 中文是常态（如 `20261123_103958_家长必看专场_stream`），
        # 一旦只放行 [A-Za-z0-9]，中文场次的单条下载会全部 400。
        if not _valid_library_stem(stem):
            raise HTTPException(status_code=400, detail="非法的目录名")
        if kind not in ("md", "json", "jsonl"):
            raise HTTPException(status_code=400, detail="只支持 md / json / jsonl")

        target_dir = job_runner.transcripts_dir / stem
        if not target_dir.is_dir():
            raise HTTPException(status_code=404, detail="目录不存在")
        # 再确认解析后的路径仍在 transcripts 下
        try:
            target_dir.resolve().relative_to(job_runner.transcripts_dir.resolve())
        except ValueError:
            raise HTTPException(status_code=400, detail="非法路径")

        hits = sorted(target_dir.glob(f"*.{kind}"))
        if not hits:
            raise HTTPException(status_code=404, detail=f"没有找到 .{kind} 文件")
        return FileResponse(path=str(hits[0]), filename=hits[0].name)

    return app


# 供 `uvicorn wxlive.server:app` 直接挂载
app = create_app()
