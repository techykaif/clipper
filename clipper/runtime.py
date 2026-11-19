"""Read-only worker and resource health snapshot for the local dashboard."""
import html
import os
import time

import psutil

from .common import JOBS, ROOT, read_json

STALE_AFTER_S = 180


def classify_job_state(stage, is_live, process_alive=False, ended=False, age_s=None):
    """Stale means no recent proof of life; it is never an instruction to kill a worker."""
    if stage == "error":
        return "ERROR"
    if stage == "done" or ended:
        return "STOPPED"
    if is_live:
        return "RUNNING" if process_alive else "STALE"
    if not stage:
        return "UNKNOWN"
    if stage == "queued":
        return "QUEUED" if age_s is None or age_s <= STALE_AFTER_S else "STALE"
    return "RUNNING" if age_s is not None and age_s <= STALE_AFTER_S else "STALE"


def _job_snapshot(jd):
    job = read_json(jd / "job.json", {}) or {}
    status = read_json(jd / "status.json", {}) or {}
    live_info = read_json(jd / "live.json", {}) or {}
    status_path = jd / "status.json"
    age = max(0.0, time.time() - status_path.stat().st_mtime) if status_path.exists() else None
    is_live = bool(job.get("live"))
    alive = False
    if is_live:
        from . import live
        alive = live.running(jd)
    state = classify_job_state(status.get("stage"), is_live, alive, bool(live_info.get("ended")), age)
    clips = (read_json(jd / "clips.json", {}) or {}).get("clips", []) or []
    meta = read_json(jd / "meta.json", {}) or {}
    title = ((read_json(jd / "clips.json", {}) or {}).get("title")
             or job.get("title") or meta.get("title") or jd.name)
    return {
        "key": jd.name, "title": str(title), "state": state,
        "stage": str(status.get("stage") or ""), "message": str(status.get("msg") or ""),
        "error": str(status.get("error") or ""), "pid": live_info.get("pid") if is_live else None,
        "clip_count": len(clips), "is_live": is_live,
    }


def snapshot():
    jobs = []
    if JOBS.is_dir():
        for jd in sorted(JOBS.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if not jd.is_dir() or not (jd / "job.json").is_file():
                continue
            try:
                jobs.append(_job_snapshot(jd))
            except (OSError, ValueError, TypeError):
                jobs.append({"key": jd.name, "title": jd.name, "state": "UNKNOWN",
                             "stage": "", "message": "Could not read job status.", "error": "",
                             "pid": None, "clip_count": 0, "is_live": False})

    active = [j for j in jobs if j["state"] in ("RUNNING", "QUEUED")]
    stale = [j for j in jobs if j["state"] in ("STALE", "UNKNOWN")]
    errors = [j for j in jobs if j["state"] == "ERROR" or j["error"]]
    cpu_text, memory_text = "Unavailable", "Unavailable"
    try:
        processes = {}
        own = psutil.Process(os.getpid())
        processes[own.pid] = own
        for job in active:
            if job.get("pid"):
                try:
                    proc = psutil.Process(int(job["pid"]))
                    processes[proc.pid] = proc
                    for child in proc.children(recursive=True):
                        processes[child.pid] = child
                except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError):
                    pass
        cpu_total, rss_total = 0.0, 0
        for proc in processes.values():
            try:
                cpu_total += proc.cpu_percent(interval=None)
                rss_total += proc.memory_info().rss
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        cpu_text = f"{cpu_total:.1f}% app + workers"
        memory_text = f"{rss_total / (1024 * 1024):.0f} MB"
    except (psutil.Error, OSError):
        pass

    try:
        usage = psutil.disk_usage(str(ROOT))
        disk_text = f"{usage.free / (1024 ** 3):.1f} GB free · {usage.percent:.0f}% used"
    except (psutil.Error, OSError):
        disk_text = "Unavailable"

    return {
        "jobs": jobs, "active": active, "stale": stale, "errors": errors,
        "clips": sum(j["clip_count"] for j in jobs),
        "cpu": cpu_text, "memory": memory_text, "disk": disk_text,
    }


def html_panel():
    data = snapshot()
    active, stale, errors = data["active"], data["stale"], data["errors"]
    health = "Attention needed" if stale or errors else ("Running" if active else "Idle")
    tone = "np-err" if stale or errors else ("np-ok" if active else "")
    cells = [
        ("Health", health),
        ("Workers", f"{len(active)} active · {len(stale)} stale · {len(errors)} errors"),
        ("Candidates", str(data["clips"])),
        ("CPU", data["cpu"]),
        ("Worker memory", data["memory"]),
        ("Disk", data["disk"]),
    ]
    stats = "".join(
        f"<div><dt>{html.escape(k)}</dt><dd>{html.escape(v)}</dd></div>" for k, v in cells
    )
    rows = []
    for job in (active + stale + errors)[:5]:
        detail = job["error"] or job["message"] or job["stage"] or job["state"].lower()
        rows.append(
            f"<li><b>{html.escape(job['state'])}</b> · {html.escape(job['title'])}"
            f"<small>{html.escape(detail[:180])}</small></li>"
        )
    if not rows:
        rows.append("<li>No active or stale workers.</li>")
    return (
        f"<div class='np-runtime'><div class='np-runtime-head'><b>Runtime health</b>"
        f"<span class='{tone}'>{health}</span></div>"
        f"<dl class='np-stats'>{stats}</dl><ul class='np-runtime-jobs'>{''.join(rows)}</ul></div>"
    )
