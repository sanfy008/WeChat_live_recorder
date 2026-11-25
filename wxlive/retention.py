"""NAS 磁盘保留策略：定期清理旧录音，避免长期跑直播把盘撑爆。

## 为什么单独成一个模块

清理是**不可逆的破坏性操作**，必须满足三条：

1. **可预览**：先算出"将要删什么、共多少 MB"，让人看过再执行。
2. **可测试**：全是纯函数（输入 jobs + 目录，输出计划），不起线程、不看真实时钟。
3. **有硬边界**：路径必须落在预期根目录内、跳过正在跑的任务、不碰转录产物（除非显式要求）。

把这三条塞进 `nas_runner` 会让那个文件更难维护，也让测试必须构造整个 runner。
所以拆出来，`NasJobRunner` 只负责调用和调度。

## 清理范围的取舍（重要）

| 对象 | 默认 | 理由 |
|---|---|---|
| 旧音频 WAV | **删** | 体积大（1.83MB/分钟，2 小时约 220MB），是空间浪费的主因 |
| 上传转码源文件 | **删** | `.mp4/.m4a` 原片转码后不再被 audio_path 引用，不清理就永久占盘 |
| 转录 md/json | **保留** | 体积小（一场几十 KB），是真正有价值的产物 |
| 孤儿 WAV（无对应任务） | **删** | 进程崩溃/上传中断留下的碎片 |
| 孤儿 .part/.tmp | **删** | 下载中断的残骸 |

用户要的是"防止空间浪费"，浪费的是**音频**，不是文字。所以默认只清音频，
清转录需要显式开启 `delete_transcripts`。

## 时间基准

年龄用 `job["created_at"]`（unix 秒），而不是文件 mtime ——
mtime 会被 `touch`、同步工具、容器重建影响，`created_at` 才是"这场直播是什么时候的"。
读不到 `created_at` 时才退回 mtime。
"""

from __future__ import annotations

import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

# 这些状态的任务正在跑，绝对不能动它的文件
ACTIVE_STATUSES = {"recording", "queued", "converting", "transcribing", "postprocessing"}

SECONDS_PER_DAY = 86400.0

# 孤儿文件只认这些后缀，避免误删别的东西
_ORPHAN_SUFFIXES = (".wav",)
_ORPHAN_JUNK_SUFFIXES = (".part", ".tmp", ".16k.wav.part")

# 本程序产出的音频文件名规则。孤儿清理**只认这两类**，
# 避免误删别人放进同一目录的文件（对立性审查 M7 ——
# 抓流工具的 download.dir 就指向 recordings/，它会往里放 .mkv/.mp4）。
#
#   stream:  20261123_103958_名字_stream.wav
#   upload:  20261123_103958_名字.wav（用户上传后转的 16k wav）
_OWN_AUDIO_RE = re.compile(
    r"^\d{8}_\d{6}_.+(_stream)?\.(wav|16k\.wav)$", re.IGNORECASE
)

# 孤儿音频的**额外**保护期（小时）。
#
# 为什么需要：上传是"先落盘、后建 job"的（create_upload_job 里
# shutil.copyfileobj 写完才写 jobs.json）。若清理恰好在这中间跑，
# 这个已落盘但还没登记的文件会被当成孤儿删掉 —— 实测可复现。
#
# 光看 mtime 不够：大文件上传耗时长、或宿主/容器时钟有漂移时，
# mtime 可能已经很老。所以对孤儿音频额外要求"比保留期再老 24 小时"，
# 把这种窗口彻底盖住。真正崩溃留下的残骸等一天无伤大雅。
ORPHAN_GRACE_HOURS = 24


@dataclass
class Candidate:
    """一条待清理项。"""

    kind: str                 # audio / transcript / orphan_audio / junk
    path: Path
    size_bytes: int
    age_days: float
    job_id: str = ""
    title: str = ""
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "path": str(self.path),
            "name": self.path.name,
            "size_bytes": self.size_bytes,
            "size_mb": round(self.size_bytes / 1048576, 2),
            "age_days": round(self.age_days, 1),
            "job_id": self.job_id,
            "title": self.title,
            "reason": self.reason,
        }


@dataclass
class CleanupPlan:
    """清理计划（预览与执行共用同一份，保证"看到的就是删掉的"）。"""

    retention_days: float = 0.0
    delete_transcripts: bool = False
    candidates: list[Candidate] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    @property
    def enabled(self) -> bool:
        return self.retention_days > 0

    @property
    def total_bytes(self) -> int:
        return sum(c.size_bytes for c in self.candidates)

    def to_dict(self) -> dict[str, Any]:
        by_kind: dict[str, dict[str, float]] = {}
        for c in self.candidates:
            slot = by_kind.setdefault(c.kind, {"count": 0, "size_mb": 0.0})
            slot["count"] += 1
            slot["size_mb"] = round(slot["size_mb"] + c.size_bytes / 1048576, 2)
        return {
            "ok": self.ok,
            "enabled": self.enabled,
            "retention_days": self.retention_days,
            "delete_transcripts": self.delete_transcripts,
            "count": len(self.candidates),
            "total_bytes": self.total_bytes,
            # 保留 3 位小数：小文件（几十 KB）在 2 位精度下会被 round 成 0.0，
            # 界面上就会显示"共 0 MB"，看着像什么都没删。
            "total_mb": round(self.total_bytes / 1048576, 3),
            "total_human": _human_size(self.total_bytes),
            "by_kind": by_kind,
            "candidates": [c.to_dict() for c in self.candidates],
            "skipped_count": len(self.skipped),
            "skipped": self.skipped[:50],
            "error": self.error,
        }


def _human_size(n: int) -> str:
    """0.1 MB / 12.3 MB / 1.20 GB 这种可读写法。

    单独给一个 human 字段，避免前端为了"4KB 别显示成 0MB"再写一遍分支。
    """
    v = float(max(0, n))
    for unit in ("B", "KB", "MB", "GB"):
        if v < 1024 or unit == "GB":
            return f"{v:.0f} {unit}" if unit in ("B", "KB") else f"{v:.1f} {unit}"
        v /= 1024.0
    return f"{v:.1f} GB"


def _is_within(path: Path, root: Path) -> bool:
    """路径是否在 root 之内（防目录穿越）。

    `Path.is_relative_to` 在 3.9+ 可用，但这里显式 resolve 一次，
    因为 `..` 在未解析前会让前缀判断失效。
    """
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def _file_size(p: Path) -> int:
    try:
        return p.stat().st_size
    except OSError:
        return 0


def _age_days(mtime: float, now: float) -> float:
    return max(0.0, (now - mtime) / SECONDS_PER_DAY)


def _job_created_ts(job: dict[str, Any]) -> float:
    try:
        ts = float(job.get("created_at") or 0)
    except (TypeError, ValueError):
        ts = 0.0
    return ts


def plan_cleanup(
    jobs: dict[str, dict[str, Any]],
    recordings_dir: Path | str,
    transcripts_dir: Path | str,
    *,
    retention_days: float,
    delete_transcripts: bool = False,
    now: float | None = None,
) -> CleanupPlan:
    """计算一次清理计划（不删任何东西）。

    Args:
        jobs: job_id -> job dict（来自 jobs.json）
        recordings_dir: 音频根目录
        transcripts_dir: 逐字稿根目录
        retention_days: 保留天数；<=0 表示禁用
        delete_transcripts: 是否连逐字稿一起清（默认否）
        now: 当前时间戳（测试注入用）
    """
    rec_root = Path(recordings_dir)
    tr_root = Path(transcripts_dir)
    ts_now = time.time() if now is None else float(now)

    plan = CleanupPlan(
        retention_days=float(retention_days),
        delete_transcripts=bool(delete_transcripts),
    )
    if plan.retention_days <= 0:
        plan.error = ""
        return plan

    cutoff = plan.retention_days * SECONDS_PER_DAY

    # 所有"被任务引用"的音频路径，用来识别孤儿
    referenced_audio: set[str] = set()
    referenced_tr_dirs: set[str] = set()

    for job_id, job in (jobs or {}).items():
        if not isinstance(job, dict):
            continue
        status = str(job.get("status") or "")
        audio_raw = str(job.get("audio_path") or "")
        md_raw = str(job.get("md_path") or "")

        if audio_raw:
            referenced_audio.add(str(Path(audio_raw)))
        # 转码源文件也要算"被引用"：否则若原片恰好是 .wav（48k 上传），
        # 孤儿扫描会把它当无人认领再删一遍，绕过任务级的年龄判定。
        src_raw = str(job.get("source_path") or "")
        if src_raw:
            referenced_audio.add(str(Path(src_raw)))
        if md_raw:
            # 转录产物按 <transcripts>/<stem>/ 目录组织
            referenced_tr_dirs.add(str(Path(md_raw).parent))

        if status in ACTIVE_STATUSES:
            plan.skipped.append({
                "job_id": job_id,
                "title": job.get("title"),
                "reason": f"任务进行中（{status}），跳过",
            })
            continue
        if not audio_raw:
            continue

        audio_p = Path(audio_raw)
        if not _is_within(audio_p, rec_root):
            plan.skipped.append({
                "job_id": job_id,
                "title": job.get("title"),
                "reason": f"音频不在录制目录内，跳过：{audio_p}",
            })
            continue

        # 年龄基准：优先 job.created_at，其次音频 mtime，最后转录 mtime。
        # 音频可能已经被上一轮清理删掉了，这时不能因为"文件不存在"就跳过 ——
        # 否则"先只清音频、后勾选连转录一起清"这条最自然的使用路径会永远失效
        # （对立性审查 H5）。
        created = _job_created_ts(job)
        if created > 0:
            base_ts = created
        elif audio_p.exists():
            try:
                base_ts = audio_p.stat().st_mtime
            except OSError:
                base_ts = ts_now
        else:
            md_for_ts = Path(md_raw) if md_raw else None
            if md_for_ts and md_for_ts.exists():
                try:
                    base_ts = md_for_ts.stat().st_mtime
                except OSError:
                    base_ts = ts_now
            else:
                continue          # 什么都没有：无从判断年龄，跳过
        age = _age_days(base_ts, ts_now)
        if age < plan.retention_days:
            continue

        # 音频：存在才删
        if audio_p.exists():
            plan.candidates.append(Candidate(
                kind="audio",
                path=audio_p,
                size_bytes=_file_size(audio_p),
                age_days=age,
                job_id=job_id,
                title=str(job.get("title") or ""),
                reason=f"音频已 {age:.0f} 天（阈值 {plan.retention_days:.0f} 天）",
            ))

        # 转录：**独立于音频是否存在**判定
        if delete_transcripts and md_raw:
            tr_dir = Path(md_raw).parent
            if tr_dir.exists() and _is_within(tr_dir, tr_root) and tr_dir != tr_root:
                tr_size = sum(_file_size(f) for f in tr_dir.rglob("*") if f.is_file())
                if tr_size > 0:
                    plan.candidates.append(Candidate(
                        kind="transcript",
                        path=tr_dir,
                        size_bytes=tr_size,
                        age_days=age,
                        job_id=job_id,
                        title=str(job.get("title") or ""),
                        reason=f"逐字稿已 {age:.0f} 天（阈值 {plan.retention_days:.0f} 天）",
                    ))

    # ---- 上传转码的源文件 ----
    # 单独一遍：即使 audio_path 已被上一轮清掉，源文件仍要按任务年龄清理。
    plan.candidates.extend(
        _plan_sources(jobs, rec_root,
                      retention_days=plan.retention_days, now=ts_now)
    )

    # ---- 孤儿文件：目录里有、但没有任务引用 ----
    plan.candidates.extend(
        _plan_orphans(rec_root, referenced_audio, referenced_tr_dirs,
                      retention_days=plan.retention_days, now=ts_now)
    )
    return plan


def _plan_sources(
    jobs: dict[str, dict[str, Any]],
    rec_root: Path,
    *,
    retention_days: float,
    now: float,
) -> Iterable[Candidate]:
    """上传任务转码后留下的原始媒体文件（.mp4/.m4a/...）。

    这些文件转码完成后不再是 `audio_path`，此前完全不受保留策略管理，
    属于"删任务才消失、平时只增不减"的盲区。
    与音频一样受保留天数约束，且必须落在录制目录内。
    """
    out: list[Candidate] = []
    for job_id, job in (jobs or {}).items():
        if not isinstance(job, dict):
            continue
        raw = str(job.get("source_path") or "")
        if not raw:
            continue
        if str(job.get("status") or "") in ACTIVE_STATUSES:
            continue
        sp = Path(raw)
        audio_raw = str(job.get("audio_path") or "")
        if audio_raw and str(Path(audio_raw)) == str(sp):
            continue                      # 源就是当前音频：不要重复计
        if not sp.is_file() or not _is_within(sp, rec_root):
            continue

        created = _job_created_ts(job)
        if created > 0:
            base_ts = created
        else:
            try:
                base_ts = sp.stat().st_mtime
            except OSError:
                continue
        age = _age_days(base_ts, now)
        if age < retention_days:
            continue
        out.append(Candidate(
            kind="source",
            path=sp,
            size_bytes=_file_size(sp),
            age_days=age,
            job_id=job_id,
            title=str(job.get("title") or ""),
            reason=f"上传转码源文件已 {age:.0f} 天（阈值 {retention_days:.0f} 天）",
        ))
    return out


def _plan_orphans(
    rec_root: Path,
    referenced_audio: set[str],
    referenced_tr_dirs: set[str],
    *,
    retention_days: float,
    now: float,
) -> Iterable[Candidate]:
    """扫描无人认领的音频与残骸。

    只认 `.wav` 与明确的临时后缀 —— 抓流工具可能把别的类型的文件
    也放进同一个目录（实测它的 `download.dir` 就指向 recordings/），
    那些不能由我们删。
    """
    if not rec_root.is_dir():
        return []

    out: list[Candidate] = []
    for p in rec_root.rglob("*"):
        if not p.is_file():
            continue
        name = p.name
        is_junk = any(name.endswith(s) for s in _ORPHAN_JUNK_SUFFIXES)
        # 只认本程序的命名规则，别碰别人放进来的 .wav（审查 M7）
        is_orphan_audio = bool(_OWN_AUDIO_RE.match(name)) and not is_junk
        if not (is_junk or is_orphan_audio):
            continue
        key = str(p)
        if not is_junk and key in referenced_audio:
            continue
        # 转录目录被子目录引用时不要动（虽然音频不该在那，防守一下）
        if any(key.startswith(d) for d in referenced_tr_dirs):
            continue

        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        age = _age_days(mtime, now)

        if is_junk:
            if age < retention_days:
                continue
            out.append(Candidate(
                kind="junk",
                path=p,
                size_bytes=_file_size(p),
                age_days=age,
                reason="临时文件残骸",
            ))
            continue

        # 孤儿音频：额外保护期，避开"已落盘但还没登记任务"的上传窗口
        orphan_threshold = retention_days + ORPHAN_GRACE_HOURS / 24.0
        if age < orphan_threshold:
            continue
        out.append(Candidate(
            kind="orphan_audio",
            path=p,
            size_bytes=_file_size(p),
            age_days=age,
            reason=(f"孤儿音频（无对应任务记录，且已超过"
                    f" {retention_days:.0f}+{ORPHAN_GRACE_HOURS} 天）"),
        ))
    return out


@dataclass
class CleanupResult:
    deleted: list[dict[str, Any]] = field(default_factory=list)
    failed: list[dict[str, Any]] = field(default_factory=list)
    freed_bytes: int = 0
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "deleted_count": len(self.deleted),
            "failed_count": len(self.failed),
            "freed_bytes": self.freed_bytes,
            "freed_mb": round(self.freed_bytes / 1048576, 3),
            "freed_human": _human_size(self.freed_bytes),
            "deleted": self.deleted,
            "failed": self.failed[:50],
            "error": self.error,
        }


def execute_cleanup(plan: CleanupPlan) -> CleanupResult:
    """执行计划。逐项容错：一项失败不影响其他项。"""
    result = CleanupResult()
    if not plan.ok:
        result.error = plan.error or "计划无效"
        return result
    if not plan.enabled:
        result.error = "保留天数未设置（<=0），清理已禁用"
        return result

    for c in plan.candidates:
        try:
            if c.kind == "transcript":
                if c.path.is_dir():
                    shutil.rmtree(c.path)
                elif c.path.exists():
                    c.path.unlink()
            else:
                if c.path.exists():
                    c.path.unlink()
            # 先确认文件真的还不在，再计入释放量：
            # 两次 cleanup 并发时，路径已被删但仍 append 会让报表虚高（审查 L4）
            still_there = c.path.exists()
            result.deleted.append(c.to_dict())
            if not still_there:
                result.freed_bytes += c.size_bytes
        except OSError as exc:
            result.failed.append({**c.to_dict(), "error": str(exc)[:200]})
    return result


def scan_library(
    transcripts_dir: Path | str,
    jobs: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """列出转录文件库（以磁盘为准，任务记录只用来补充标题）。

    为什么不以 jobs.json 为准：文件可能被外部工具/用户手动整理过，
    `jobs.json` 会失真。**磁盘上真实存在的才是能导出的**。

    每个 `<stem>/` 目录算一条，收集其中的 md/json/jsonl 与总体积。
    """
    tr_root = Path(transcripts_dir)
    jobs = jobs or {}
    out: list[dict[str, Any]] = []

    # 先建索引：md_path 的父目录 -> job
    by_dir: dict[str, dict[str, Any]] = {}
    for jid, job in jobs.items():
        if not isinstance(job, dict):
            continue
        md = str(job.get("md_path") or "")
        if md:
            by_dir[str(Path(md).parent)] = {**job, "job_id": jid}

    if not tr_root.is_dir():
        return []

    for d in sorted(tr_root.iterdir()):
        # 跳过符号链接/junction 目录：itetdir() 会穿过它们，
        # 把库外目录的文件名和 md 预览读出来（对立性审查 M3）。
        # 下载端点有 resolve().relative_to() 兜底，但列表接口会泄露内容。
        if d.is_symlink() or not d.is_dir():
            continue
        files = [f for f in d.iterdir() if f.is_file() and not f.is_symlink()]
        if not files:
            continue
        md = next((f for f in files if f.suffix == ".md"), None)
        js = next((f for f in files if f.suffix == ".json"), None)
        if md is None and js is None:
            continue

        job = by_dir.get(str(d), {})
        total = sum(_file_size(f) for f in files)
        # stat 可能因清理线程同时删文件而抛 FileNotFoundError（对立性审查 M8）
        def _mtime(f: Path) -> float:
            try:
                return f.stat().st_mtime
            except OSError:
                return 0.0
        newest = max((_mtime(f) for f in files), default=0.0)

        preview = ""
        if md is not None:
            try:
                with md.open("r", encoding="utf-8") as fh:
                    head = fh.read(1200)
                # 跳过标题/元数据块，取第一段正文
                for block in head.split("\n\n"):
                    t = block.strip()
                    if t and not t.startswith("#") and not t.startswith("- ") \
                            and not t.startswith(">") and not t.startswith("---"):
                        preview = t[:120]
                        break
            except OSError:
                preview = ""

        out.append({
            "stem": d.name,
            "job_id": job.get("job_id", ""),
            "title": job.get("title") or d.name,
            "status": job.get("status", ""),
            "created_at": job.get("created_at", 0),
            "mtime": newest,
            "audio_seconds": job.get("audio_seconds", 0),
            "sentence_count": job.get("sentence_count", 0),
            "file_count": len(files),
            "size_bytes": total,
            "size_kb": round(total / 1024, 1),
            "files": sorted(f.name for f in files),
            "has_md": md is not None,
            "has_json": js is not None,
            "preview": preview,
        })

    out.sort(key=lambda x: x.get("mtime", 0), reverse=True)
    return out
