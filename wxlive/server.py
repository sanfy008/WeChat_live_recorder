"""NAS 无头 Web 控制台（FastAPI + 单文件响应式前端）。

启动方式：
    python -m wxlive serve --host 0.0.0.0 --port 8910
    或
    uvicorn wxlive.server:app --host 0.0.0.0 --port 8910
"""

from __future__ import annotations

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


class StreamStartReq(BaseModel):
    url: str
    name: str = ""
    auto_transcribe: bool | None = None


class StopReq(BaseModel):
    transcribe: bool | None = None


class SettingsUpdateReq(BaseModel):
    local_threads: int | None = None
    use_lexicon: bool | None = None
    enable_llm: bool | None = None
    llm_endpoint: str | None = None
    llm_api_key: str | None = None
    clear_api_key: bool = False
    llm_model: str | None = None
    llm_reasoning_effort: str | None = None
    auto_transcribe: bool | None = None
    keep_audio: bool | None = None


_DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>WXLive NAS · 远程直播录制与转写台</title>
<style>
  :root {
    --bg: #0f172a;
    --panel: #1e293b;
    --panel2: #162032;
    --border: #334155;
    --text: #e2e8f0;
    --muted: #94a3b8;
    --accent: #38bdf8;
    --accent-dim: rgba(56, 189, 248, 0.14);
    --green: #22c55e;
    --amber: #f59e0b;
    --red: #ef4444;
    --purple: #a855f7;
    --radius: 12px;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
    background: var(--bg);
    color: var(--text);
    line-height: 1.5;
  }
  .wrap { max-width: 1280px; margin: 0 auto; padding: 16px; }
  header {
    display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between;
    gap: 12px; padding: 14px 18px; background: var(--panel);
    border: 1px solid var(--border); border-radius: var(--radius); margin-bottom: 16px;
  }
  .brand h1 { margin: 0; font-size: 18px; font-weight: 700; display: flex; align-items: center; gap: 8px; }
  .brand small { color: var(--muted); font-size: 12px; }
  .pills { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
  .pill {
    font-size: 12px; padding: 4px 10px; border-radius: 999px;
    background: var(--panel2); border: 1px solid var(--border); color: var(--muted);
  }
  .pill.ok { color: var(--green); border-color: rgba(34,197,94,0.35); }
  .pill.warn { color: var(--amber); border-color: rgba(245,158,11,0.35); }
  button {
    cursor: pointer; border: 1px solid var(--border); border-radius: 8px;
    padding: 8px 14px; font-size: 13px; font-weight: 600;
    background: var(--panel2); color: var(--text); transition: 0.15s ease;
  }
  button:hover:not(:disabled) { border-color: var(--accent); color: #fff; }
  button:disabled { opacity: 0.5; cursor: not-allowed; }
  button.primary { background: #0284c7; border-color: #0284c7; color: #fff; }
  button.primary:hover:not(:disabled) { background: #0369a1; }
  button.danger { background: rgba(239,68,68,0.16); border-color: rgba(239,68,68,0.45); color: #fca5a5; }
  button.sm { padding: 5px 10px; font-size: 12px; }
  .card {
    background: var(--panel); border: 1px solid var(--border);
    border-radius: var(--radius); padding: 16px; margin-bottom: 16px;
  }
  .tabs { display: flex; gap: 8px; margin-bottom: 14px; border-bottom: 1px solid var(--border); padding-bottom: 10px; }
  .tab {
    background: transparent; border: 1px solid transparent; color: var(--muted); padding: 6px 14px;
  }
  .tab.active { background: var(--accent-dim); border-color: rgba(56,189,248,0.4); color: var(--accent); }
  .row { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; }
  .field { flex: 1 1 260px; display: flex; flex-direction: column; gap: 6px; margin-bottom: 10px; }
  label { font-size: 12px; color: var(--muted); }
  input[type=text], input[type=number], input[type=password], select, textarea {
    width: 100%; padding: 9px 12px; border-radius: 8px;
    border: 1px solid var(--border); background: var(--panel2); color: var(--text);
    font-size: 13px; outline: none;
  }
  input:focus, select:focus, textarea:focus { border-color: var(--accent); }
  .grid { display: grid; grid-template-columns: 420px 1fr; gap: 16px; align-items: start; }
  @media (max-width: 960px) { .grid { grid-template-columns: 1fr; } }
  .job-list { display: flex; flex-direction: column; gap: 10px; max-height: 68vh; overflow-y: auto; }
  .job-item {
    border: 1px solid var(--border); background: var(--panel2); border-radius: 10px;
    padding: 12px; cursor: pointer; transition: 0.15s;
  }
  .job-item.selected { border-color: var(--accent); background: rgba(56,189,248,0.08); }
  .job-head { display: flex; justify-content: space-between; align-items: center; gap: 8px; margin-bottom: 6px; }
  .job-title { font-weight: 600; font-size: 14px; word-break: break-all; }
  .badge {
    font-size: 11px; padding: 2px 8px; border-radius: 999px; font-weight: 600; white-space: nowrap;
  }
  .st-recording { background: rgba(239,68,68,0.2); color: #fca5a5; }
  .st-queued, .st-converting { background: rgba(245,158,11,0.2); color: #fcd34d; }
  .st-transcribing, .st-postprocessing { background: rgba(56,189,248,0.2); color: #7dd3fc; }
  .st-completed { background: rgba(34,197,94,0.2); color: #86efac; }
  .st-failed, .st-interrupted { background: rgba(148,163,184,0.2); color: #cbd5e1; }
  .st-recorded { background: rgba(168,85,247,0.2); color: #d8b4fe; }
  .meta { font-size: 12px; color: var(--muted); display: flex; flex-wrap: wrap; gap: 10px; }
  .progress-bar {
    height: 5px; background: #0b1220; border-radius: 999px; overflow: hidden; margin-top: 8px;
  }
  .progress-fill { height: 100%; background: var(--accent); transition: width 0.3s ease; }
  .detail-box { min-height: 460px; }
  pre.viewer {
    background: #0b1220; border: 1px solid var(--border); border-radius: 8px;
    padding: 14px; white-space: pre-wrap; word-break: break-word;
    font-family: "JetBrains Mono", "Cascadia Code", Consolas, monospace;
    font-size: 13px; max-height: 60vh; overflow-y: auto; margin: 0;
  }
  .notice { font-size: 12px; padding: 8px 12px; border-radius: 8px; margin-top: 8px; }
  .notice.ok { background: rgba(34,197,94,0.12); color: #86efac; border: 1px solid rgba(34,197,94,0.3); }
  .notice.err { background: rgba(239,68,68,0.12); color: #fca5a5; border: 1px solid rgba(239,68,68,0.3); }
  .modal-bg {
    position: fixed; inset: 0; background: rgba(2,6,23,0.75);
    display: none; align-items: center; justify-content: center; padding: 16px; z-index: 50;
  }
  .modal-bg.open { display: flex; }
  .modal {
    width: 100%; max-width: 560px; background: var(--panel); border: 1px solid var(--border);
    border-radius: var(--radius); padding: 18px; max-height: 90vh; overflow-y: auto;
  }
  .chk { display: flex; align-items: center; gap: 8px; font-size: 13px; color: var(--text); cursor: pointer; }
  .chk input { width: auto; }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="brand">
      <h1>🎙️ WXLive NAS 远程转写台</h1>
      <small>飞牛 fnOS 独立引擎 · ffmpeg 拉流 + SenseVoice INT8 + 可选 AI 纠错/要点</small>
    </div>
    <div class="pills">
      <span id="pill-ffmpeg" class="pill">ffmpeg: 检测中</span>
      <span id="pill-engine" class="pill">SenseVoice: 待加载</span>
      <span id="pill-disk" class="pill">磁盘: --</span>
      <button onclick="openSettings()">⚙ 设置</button>
    </div>
  </header>

  <div class="card">
    <div class="tabs">
      <button id="tab-stream-btn" class="tab active" onclick="switchInputTab('stream')">📡 直播流地址直录</button>
      <button id="tab-upload-btn" class="tab" onclick="switchInputTab('upload')">📁 上传音频 / 视频回放</button>
    </div>

    <div id="pane-stream">
      <div class="row">
        <div class="field" style="flex:3 1 420px;">
          <label>直播流地址（纯 URL 或直接粘贴整条 ffmpeg 命令都可以）</label>
          <input id="stream-url" type="text" placeholder='https://pull-m1.wxlivecdn.com/xxx.flv?txTime=... 或 ffmpeg -i "https://..." -c copy -y live.flv'>
        </div>
        <div class="field" style="flex:1 1 200px;">
          <label>场次名称（可选）</label>
          <input id="stream-name" type="text" placeholder="如：20261123_升学直播">
        </div>
      </div>
      <div class="row" style="justify-content: space-between;">
        <label class="chk"><input id="stream-auto-tx" type="checkbox" checked> 停录后自动 SenseVoice 转写与整理</label>
        <div class="row">
          <button onclick="probeStream()">🔍 探测流地址</button>
          <button class="primary" onclick="startStream()">▶ 开始拉流录制</button>
        </div>
      </div>
      <div id="stream-feedback"></div>
    </div>

    <div id="pane-upload" style="display:none;">
      <div class="row">
        <div class="field" style="flex:2 1 320px;">
          <label>选择本地音频或视频（支持 .wav .mp3 .m4a .mp4 .aac .flac .flv）</label>
          <input id="upload-file" type="file" accept=".wav,.mp3,.m4a,.mp4,.aac,.flac,.ogg,.opus,.flv,.mkv,.webm">
        </div>
        <div class="field" style="flex:1 1 200px;">
          <label>任务名称（可选，默认用文件名）</label>
          <input id="upload-name" type="text" placeholder="留空则使用文件名">
        </div>
      </div>
      <div class="row" style="justify-content: space-between;">
        <span id="upload-progress-text" style="font-size:12px;color:var(--muted);">上传后自动转为 16k WAV 并进入后台转写队列</span>
        <button id="upload-btn" class="primary" onclick="uploadFile()">⬆ 上传并开始转写</button>
      </div>
      <div id="upload-feedback"></div>
    </div>
  </div>

  <div class="grid">
    <div class="card">
      <div class="row" style="justify-content:space-between; margin-bottom:10px;">
        <strong>任务列表</strong>
        <button class="sm" onclick="refreshAll()">↻ 刷新</button>
      </div>
      <div id="job-list" class="job-list">
        <div style="color:var(--muted);font-size:13px;">暂无任务，粘贴流地址或上传音频即可开始。</div>
      </div>
    </div>

    <div class="card detail-box">
      <div id="detail-empty" style="color:var(--muted);font-size:13px;padding:40px 0;text-align:center;">
        在左侧选择一个任务，查看实时状态、日志或完整逐字稿/AI要点。
      </div>
      <div id="detail-body" style="display:none;">
        <div class="row" style="justify-content:space-between; margin-bottom:10px;">
          <div>
            <strong id="det-title" style="font-size:16px;"></strong>
            <div id="det-meta" class="meta" style="margin-top:4px;"></div>
          </div>
          <div id="det-actions" class="row"></div>
        </div>
        <div class="tabs" style="margin-bottom:10px;">
          <button id="vtab-md" class="tab active" onclick="switchViewTab('md')">📄 逐字稿 & AI要点</button>
          <button id="vtab-logs" class="tab" onclick="switchViewTab('logs')">🪵 运行日志</button>
        </div>
        <div id="view-md">
          <div class="row" style="justify-content:flex-end; margin-bottom:8px;">
            <button class="sm" onclick="copyMarkdown()">📋 复制全文</button>
          </div>
          <pre id="md-viewer" class="viewer">（尚未生成逐字稿）</pre>
        </div>
        <div id="view-logs" style="display:none;">
          <pre id="log-viewer" class="viewer"></pre>
        </div>
      </div>
    </div>
  </div>
</div>

<div id="settings-modal" class="modal-bg" onclick="if(event.target===this)closeSettings()">
  <div class="modal">
    <div class="row" style="justify-content:space-between;margin-bottom:12px;">
      <strong style="font-size:16px;">⚙ NAS 转录服务设置</strong>
      <button class="sm" onclick="closeSettings()">✕</button>
    </div>
    <div class="field">
      <label>SenseVoice 本地推理线程数（i7-6700 推荐 4）</label>
      <input id="set-threads" type="number" min="1" max="16" value="4">
    </div>
    <div class="row" style="margin-bottom:12px;">
      <label class="chk"><input id="set-lexicon" type="checkbox"> 启用 XSC 领域词表纠正与暗语展开</label>
      <label class="chk"><input id="set-keep-audio" type="checkbox"> 转写完成后保留原始音频</label>
    </div>
    <hr style="border:none;border-top:1px solid var(--border);margin:14px 0;">
    <label class="chk" style="margin-bottom:10px;"><input id="set-enable-llm" type="checkbox"> 启用 AI 纠错与结构化要点整理（OpenAI 兼容接口）</label>
    <div class="field">
      <label>LLM Endpoint（如 https://api.deepseek.com/v1/chat/completions 或内网网关）</label>
      <input id="set-endpoint" type="text" placeholder="http://192.168.68.x:3425/v1/chat/completions">
    </div>
    <div class="field">
      <label>API Key（留空表示保持原密码不变）</label>
      <input id="set-apikey" type="password" placeholder="已配置时显示掩码，输入新 Key 即替换">
    </div>
    <div class="row">
      <div class="field">
        <label>文本模型</label>
        <input id="set-model" type="text" placeholder="workbuddy/deepseek-v4.1-flash">
      </div>
      <div class="field">
        <label>思考强度</label>
        <select id="set-effort">
          <option value="none">none</option>
          <option value="low">low</option>
          <option value="high">high</option>
          <option value="max">max</option>
        </select>
      </div>
    </div>
    <div id="settings-feedback"></div>
    <div class="row" style="justify-content:space-between;margin-top:14px;">
      <button onclick="testLlm()">⚡ 测试 LLM 连通性</button>
      <div class="row">
        <button onclick="closeSettings()">取消</button>
        <button class="primary" onclick="saveSettings()">保存设置</button>
      </div>
    </div>
  </div>
</div>

<script>
const STATUS_LABEL = {
  recording: '录制中',
  recorded: '已录待转',
  queued: '排队中',
  converting: '音频转码',
  transcribing: 'SenseVoice转写中',
  postprocessing: 'AI整理中',
  completed: '已完成',
  failed: '失败',
  interrupted: '已中断'
};
let jobs = [];
let selectedId = null;
let currentViewTab = 'md';
let currentMdText = '';
let pollTimer = null;

function esc(s) {
  return String(s || '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function fmtDur(sec) {
  sec = Math.max(0, Math.round(sec || 0));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return h ? `${h}:${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}` : `${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`;
}
function fmtMB(bytes) {
  return ((bytes || 0) / 1048576).toFixed(1) + ' MB';
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
  document.getElementById('vtab-md').classList.toggle('active', t === 'md');
  document.getElementById('vtab-logs').classList.toggle('active', t === 'logs');
}
function showNotice(elId, ok, msg) {
  document.getElementById(elId).innerHTML = `<div class="notice ${ok ? 'ok' : 'err'}">${esc(msg)}</div>`;
}
async function api(path, opts = {}) {
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || data.error || ('HTTP ' + res.status));
  return data;
}
async function refreshHealth() {
  try {
    const h = await api('/api/health');
    const pf = document.getElementById('pill-ffmpeg');
    pf.textContent = 'ffmpeg: ' + (h.ffmpeg_available ? '可用' : '缺失');
    pf.className = 'pill ' + (h.ffmpeg_available ? 'ok' : 'warn');
    const pe = document.getElementById('pill-engine');
    pe.textContent = 'SenseVoice: ' + (h.engine_loaded ? '已常驻内存' : '按需加载');
    pe.className = 'pill ' + (h.engine_loaded ? 'ok' : '');
    document.getElementById('pill-disk').textContent = `剩余磁盘: ${h.disk_free_gb} GB`;
  } catch (e) {}
}
async function refreshJobs() {
  try {
    const data = await api('/api/jobs');
    jobs = data.jobs || [];
    renderJobs();
    if (selectedId) {
      const found = jobs.find(j => j.id === selectedId);
      if (found) await renderDetail(found);
    } else if (jobs.length) {
      await selectJob(jobs[0].id);
    }
    const hasActive = jobs.some(j => ['recording','queued','converting','transcribing','postprocessing'].includes(j.status));
    clearTimeout(pollTimer);
    pollTimer = setTimeout(refreshAll, hasActive ? 2500 : 10000);
  } catch (e) {}
}
function renderJobs() {
  const box = document.getElementById('job-list');
  if (!jobs.length) {
    box.innerHTML = '<div style="color:var(--muted);font-size:13px;">暂无任务，粘贴流地址或上传音频即可开始。</div>';
    return;
  }
  box.innerHTML = jobs.map(j => {
    const sel = j.id === selectedId ? 'selected' : '';
    const pct = j.status === 'completed' ? 100 : (j.progress_pct || 0);
    const bar = ['transcribing','postprocessing','converting'].includes(j.status)
      ? `<div class="progress-bar"><div class="progress-fill" style="width:${pct}%"></div></div>` : '';
    return `<div class="job-item ${sel}" onclick="selectJob('${esc(j.id)}')">
      <div class="job-head">
        <span class="job-title">${esc(j.title)}</span>
        <span class="badge st-${esc(j.status)}">${esc(STATUS_LABEL[j.status] || j.status)}</span>
      </div>
      <div class="meta">
        <span>${j.kind === 'stream' ? '📡 直播流' : '📁 上传'}</span>
        <span>⏱ ${fmtDur(j.audio_seconds)}</span>
        <span>💾 ${fmtMB(j.audio_bytes)}</span>
        ${j.sentence_count ? `<span>📝 ${j.sentence_count} 句</span>` : ''}
        ${j.rtf ? `<span>⚡ RTF ${j.rtf}</span>` : ''}
      </div>
      ${j.error ? `<div style="color:#fca5a5;font-size:12px;margin-top:4px;">${esc(j.error)}</div>` : ''}
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
  document.getElementById('det-meta').innerHTML = `
    <span>状态: ${esc(STATUS_LABEL[j.status] || j.status)}</span>
    <span>时长: ${fmtDur(j.audio_seconds)}</span>
    <span>音频: ${fmtMB(j.audio_bytes)}</span>
    ${j.sentence_count ? `<span>句数: ${j.sentence_count}</span>` : ''}
    ${j.changed_count ? `<span>LLM纠错: ${j.changed_count}处</span>` : ''}
    ${j.llm_status ? `<span>AI状态: ${esc(j.llm_status)}</span>` : ''}
  `;
  const acts = [];
  if (j.status === 'recording') {
    acts.push(`<button class="primary sm" onclick="stopStream('${esc(j.id)}', true)">⏹ 停止并转写</button>`);
    acts.push(`<button class="sm" onclick="stopStream('${esc(j.id)}', false)">仅停止</button>`);
  }
  if (['recorded','interrupted','failed'].includes(j.status) && j.has_audio) {
    acts.push(`<button class="primary sm" onclick="retranscribe('${esc(j.id)}')">▶ 开始转写</button>`);
  }
  if (j.has_json && j.status === 'completed') {
    acts.push(`<button class="sm" onclick="repostprocess('${esc(j.id)}')">✨ 重新 AI 整理</button>`);
  }
  if (j.has_md) acts.push(`<a href="/api/jobs/${encodeURIComponent(j.id)}/download/md"><button class="sm">⬇ MD</button></a>`);
  if (j.has_json) acts.push(`<a href="/api/jobs/${encodeURIComponent(j.id)}/download/json"><button class="sm">⬇ JSON</button></a>`);
  if (j.has_audio && j.status !== 'recording') acts.push(`<a href="/api/jobs/${encodeURIComponent(j.id)}/download/wav"><button class="sm">⬇ 音频</button></a>`);
  if (!['recording','transcribing','converting','postprocessing'].includes(j.status)) {
    acts.push(`<button class="danger sm" onclick="delJob('${esc(j.id)}')">🗑 删除</button>`);
  }
  document.getElementById('det-actions').innerHTML = acts.join('');

  try {
    const det = await api(`/api/jobs/${encodeURIComponent(j.id)}`);
    document.getElementById('log-viewer').textContent = (det.logs || []).join('\n') || '（暂无日志）';
    if (j.has_md) {
      const tr = await api(`/api/jobs/${encodeURIComponent(j.id)}/transcript`);
      currentMdText = tr.md_text || '';
      document.getElementById('md-viewer').textContent = currentMdText || '（逐字稿为空）';
    } else {
      currentMdText = '';
      document.getElementById('md-viewer').textContent = j.status === 'recording'
        ? '正在拉流录制中… 点击「停止并转写」后将自动生成逐字稿。'
        : '（尚未生成逐字稿，可切换至「运行日志」查看进度）';
    }
  } catch (e) {}
}
async function probeStream() {
  const url = document.getElementById('stream-url').value.trim();
  if (!url) return showNotice('stream-feedback', false, '请先粘贴流地址或 ffmpeg 命令');
  showNotice('stream-feedback', true, '正在用 ffprobe 探测流地址…');
  try {
    const r = await api('/api/probe', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({url})
    });
    if (r.ok) {
      showNotice('stream-feedback', true, `探测成功：格式 ${r.format || '未知'}，含音轨=${r.has_audio}`);
    } else {
      showNotice('stream-feedback', false, '探测失败：' + (r.error || '未知错误'));
    }
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
    await refreshAll();
  } catch (e) { alert(e.message); }
}
async function retranscribe(id) {
  try {
    await api(`/api/jobs/${encodeURIComponent(id)}/transcribe`, {method:'POST'});
    await refreshAll();
  } catch (e) { alert(e.message); }
}
async function repostprocess(id) {
  try {
    await api(`/api/jobs/${encodeURIComponent(id)}/postprocess`, {method:'POST'});
    await refreshAll();
  } catch (e) { alert(e.message); }
}
async function delJob(id) {
  if (!confirm('确认删除该任务及关联文件？')) return;
  try {
    await api(`/api/jobs/${encodeURIComponent(id)}?delete_files=true`, {method:'DELETE'});
    if (selectedId === id) selectedId = null;
    await refreshAll();
  } catch (e) { alert(e.message); }
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
    if (e.lengthComputable) prog.textContent = `上传进度：${Math.round(e.loaded / e.total * 100)}%`;
  };
  xhr.onload = async () => {
    btn.disabled = false;
    prog.textContent = '上传后自动转为 16k WAV 并进入后台转写队列';
    try {
      const data = JSON.parse(xhr.responseText || '{}');
      if (xhr.status >= 400) throw new Error(data.detail || ('HTTP ' + xhr.status));
      fileEl.value = '';
      document.getElementById('upload-name').value = '';
      showNotice('upload-feedback', true, `已加入队列：${data.title}`);
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
  if (!currentMdText) return;
  await navigator.clipboard.writeText(currentMdText);
  alert('已复制全文到剪贴板');
}
async function openSettings() {
  document.getElementById('settings-feedback').innerHTML = '';
  const s = await api('/api/settings');
  document.getElementById('set-threads').value = s.local_threads || 4;
  document.getElementById('set-lexicon').checked = !!s.use_lexicon;
  document.getElementById('set-keep-audio').checked = !!s.keep_audio;
  document.getElementById('set-enable-llm').checked = !!s.enable_llm;
  document.getElementById('set-endpoint').value = s.llm_endpoint || '';
  document.getElementById('set-apikey').value = '';
  document.getElementById('set-apikey').placeholder = s.has_api_key ? `已设置 (${s.llm_api_key_masked})` : '输入 API Key';
  document.getElementById('set-model').value = s.llm_model || '';
  document.getElementById('set-effort').value = s.llm_reasoning_effort || 'low';
  document.getElementById('settings-modal').classList.add('open');
}
function closeSettings() {
  document.getElementById('settings-modal').classList.remove('open');
}
async function saveSettings() {
  const body = {
    local_threads: Number(document.getElementById('set-threads').value || 4),
    use_lexicon: document.getElementById('set-lexicon').checked,
    keep_audio: document.getElementById('set-keep-audio').checked,
    enable_llm: document.getElementById('set-enable-llm').checked,
    llm_endpoint: document.getElementById('set-endpoint').value.trim(),
    llm_model: document.getElementById('set-model').value.trim(),
    llm_reasoning_effort: document.getElementById('set-effort').value,
  };
  const key = document.getElementById('set-apikey').value.trim();
  if (key) body.llm_api_key = key;
  try {
    await api('/api/settings', {
      method: 'PUT', headers: {'Content-Type':'application/json'},
      body: JSON.stringify(body)
    });
    showNotice('settings-feedback', true, '设置已保存');
  } catch (e) { showNotice('settings-feedback', false, e.message); }
}
async function testLlm() {
  await saveSettings();
  showNotice('settings-feedback', true, '正在测试 LLM 接口…');
  try {
    const r = await api('/api/settings/test-llm', {method:'POST'});
    if (r.ok) {
      showNotice('settings-feedback', true, `连通正常 (${r.seconds}s)：${r.reply}`);
    } else {
      showNotice('settings-feedback', false, `测试失败：${r.error}`);
    }
  } catch (e) { showNotice('settings-feedback', false, e.message); }
}
async function refreshAll() {
  await Promise.all([refreshHealth(), refreshJobs()]);
}
refreshAll();
</script>
</body>
</html>
"""


def create_app(
    data_dir: Path | str | None = None,
    runner: NasJobRunner | None = None,
) -> FastAPI:
    """创建 FastAPI 实例；支持注入 runner / data_dir 以便测试隔离。"""
    job_runner = runner or NasJobRunner(data_dir=data_dir)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        if runner is None:
            job_runner.shutdown()

    app = FastAPI(title="WXLive NAS Service", version="0.1.0", lifespan=lifespan)
    app.state.runner = job_runner

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
            if j["status"] in ("recording", "queued", "converting", "transcribing", "postprocessing")
        )
        return {
            "ok": True,
            "ffmpeg_available": bool(find_ffmpeg()),
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
        return probe_url(url, timeout=15)

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

    return app


# 供 `uvicorn wxlive.server:app` 直接挂载
app = create_app()
