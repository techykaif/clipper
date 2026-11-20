"""Read-only worker and resource health snapshot for the local dashboard."""
import html
import os
import time
from pathlib import Path

import psutil

from . import metrics, quality_feedback, services, storage
from .common import JOBS, ROOT, read_json, set_status

STALE_AFTER_S = 180


def classify_job_state(stage, is_live, process_alive=False, ended=False, age_s=None, paused=False):
    """Stale means no recent proof of life; it is never an instruction to kill a worker."""
    if stage == "error":
        return "ERROR"
    if stage == "done" or ended:
        return "STOPPED"
    if paused and process_alive:
        return "PAUSED"
    if is_live:
        if not process_alive:
            return "STALE"
        return "STALE" if age_s is None or age_s > STALE_AFTER_S else "RUNNING"
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
    is_live = job.get("live") is True
    heartbeat_path = jd / "heartbeat"
    heartbeat_required = is_live and live_info.get("heartbeat_enabled") is True
    freshness_path = heartbeat_path if heartbeat_required else status_path
    age = max(0.0, time.time() - freshness_path.stat().st_mtime) if freshness_path.exists() else None
    alive = False
    is_paused = False
    if is_live:
        from . import live
        alive = live.running(jd)
        is_paused = live.paused(jd) if alive else False
    state = classify_job_state(status.get("stage"), is_live, alive, bool(live_info.get("ended")), age, is_paused)
    clips = (read_json(jd / "clips.json", {}) or {}).get("clips", []) or []
    queue = read_json(jd / "candidates.json", {}) or {}
    queue_items = queue.get("items") if isinstance(queue, dict) else None
    candidate_count = len(queue_items) if isinstance(queue_items, list) else len(clips)
    meta = read_json(jd / "meta.json", {}) or {}
    title = ((read_json(jd / "clips.json", {}) or {}).get("title")
             or job.get("title") or meta.get("title") or jd.name)
    return {
        "key": jd.name, "title": str(title), "state": state,
        "stage": str(status.get("stage") or ""), "message": str(status.get("msg") or ""),
        "error": str(status.get("error") or ""), "pid": live_info.get("pid") if is_live else None,
        "clip_count": len(clips), "candidate_count": candidate_count, "is_live": is_live,
    }


def _recover_crashed_analysis(record):
    job_key = str(record.get("job_key") or "")
    if not job_key or Path(job_key).name != job_key or job_key in (".", ".."):
        return False
    jd = JOBS / job_key
    job = read_json(jd / "job.json", {}) or {}
    try:
        pid, created = int(record.get("pid")), float(record.get("create_time"))
    except (TypeError, ValueError):
        return False
    if job.get("runtime_service_id") != record.get("id") or job.get("live") is True:
        try:
            services.annotate_recovery(
                str(record.get("id") or ""), pid, created,
                "This crash record belongs to an older analysis run; current job files were left untouched.",
                jobs_dir=JOBS,
            )
        except Exception:
            pass
        return False
    status = read_json(jd / "status.json", {}) or {}
    note = (
        "The Clipper process exited before this analysis completed. Existing chat, metadata, clips and "
        "downloaded media were preserved where possible. Run Find clips again to retry the analysis."
    )
    if status.get("stage") not in ("done", "error"):
        set_status(jd, "error", status.get("pct", 0), status.get("msg", ""), note)
    services.annotate_recovery(str(record.get("id") or ""), pid, created, note, jobs_dir=JOBS)
    return True


def _recover_crashed_services(service_data):
    for record in service_data.get("services", {}).values():
        if not isinstance(record, dict):
            continue
        if record.get("state") != "crashed" or record.get("recovered_at"):
            continue
        job_key = str(record.get("job_key") or "")
        if not job_key or Path(job_key).name != job_key or job_key in (".", ".."):
            continue
        try:
            if record.get("type") == "live-worker":
                from . import live
                live.recover_crashed_worker(JOBS / job_key, record)
            elif record.get("type") == "render":
                from . import render
                render.recover_crashed_render(record, jobs_dir=JOBS)
            elif record.get("type") == "analysis":
                _recover_crashed_analysis(record)
        except Exception:
            # One damaged job must not prevent the rest of the dashboard from loading.
            continue


def snapshot():
    # Reconcile process identities before reading jobs. Only a positively dead/reused PID
    # can trigger recovery; stale or inaccessible processes are surfaced, never killed.
    try:
        service_data = services.snapshot(jobs_dir=JOBS)
        _recover_crashed_services(service_data)
    except Exception as exc:
        service_data = {
            "services": {},
            "counts": {"active": 0, "stale": 0, "unverified": 0, "crashed": 0, "failed": 0,
                       "attention": 1, "error": f"{type(exc).__name__}: {exc}"},
        }

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
                             "pid": None, "clip_count": 0, "candidate_count": 0, "is_live": False})

    active = [j for j in jobs if j["state"] in ("RUNNING", "QUEUED", "PAUSED")]
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

    disk_warning = ""
    try:
        disk_state = storage.snapshot(ROOT)
        free_gib = disk_state["free_bytes"] / storage.GIB
        reserve_gib = disk_state["reserve_bytes"] / storage.GIB
        disk_text = f"{free_gib:.1f} GiB free · {disk_state['used_percent']:.0f}% used"
        if disk_state["below_reserve"]:
            disk_warning = (f"Only {free_gib:.1f} GiB free; Clipper's safety reserve is "
                            f"{reserve_gib:.1f} GiB. New media work is blocked until space is freed.")
    except storage.StorageSafetyError as exc:
        disk_text = "Unavailable"
        disk_warning = f"Disk safety check failed: {exc}"

    production_metrics = metrics.snapshot(jobs_dir=JOBS)
    return {
        "jobs": jobs, "active": active, "stale": stale, "errors": errors,
        "production_metrics": production_metrics,
        "clips": sum(j["clip_count"] for j in jobs),
        "candidates": sum(j["candidate_count"] for j in jobs),
        "services": service_data.get("services", {}),
        "service_counts": service_data.get("counts", {}),
        "cpu": cpu_text, "memory": memory_text, "disk": disk_text, "disk_warning": disk_warning,
    }


def html_panel():
    data = snapshot()
    active, stale, errors = data["active"], data["stale"], data["errors"]
    service_counts = data.get("service_counts") or {}
    service_attention = bool(service_counts.get("attention") or service_counts.get("error"))
    is_paused = any(j["state"] == "PAUSED" for j in active)
    disk_warning = str(data.get("disk_warning") or "")
    needs_attention = bool(stale or errors or service_attention or disk_warning)
    health = "Attention needed" if needs_attention else ("Paused" if is_paused and len(active) == 1 else ("Running" if active else "Idle"))
    tone = "np-err" if needs_attention else ("np-ok" if active else "")
    service_text = (
        f"{service_counts.get('active', 0)} active · {service_counts.get('stale', 0)} stale · "
        f"{service_counts.get('crashed', 0)} crashed · {service_counts.get('unverified', 0)} unverified"
    )
    if service_counts.get("failed"):
        service_text += f" · {service_counts.get('failed', 0)} failed"
    if service_counts.get("error"):
        service_text = "Registry unavailable"
    cells = [
        ("Health", health),
        ("Workers", f"{len(active)} active · {len(stale)} stale · {len(errors)} errors"),
        ("Services", service_text),
        ("Candidates", str(data["candidates"])),
        ("CPU", data["cpu"]),
        ("App + workers memory", data["memory"]),
        ("Disk", data["disk"]),
        ("Clip feedback", quality_feedback.summary_text()),
        ("Production", f"{data['production_metrics']['rendered']} Shorts rendered · {data['production_metrics']['render_failed']} failed"),
        ("Review outcomes", f"{data['production_metrics']['review_keep']} kept · {data['production_metrics']['review_reject']} rejected · {data['production_metrics']['review_pending']} pending"),
        ("Media stored", metrics.human_bytes(data["production_metrics"]["media_bytes"])),
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
    if disk_warning:
        rows.insert(0, f"<li><b>DISK SAFETY</b> · {html.escape(disk_warning[:180])}</li>")
    if not rows:
        rows.append("<li>No active or stale workers.</li>")
    return (
        f"<div class='np-runtime'><div class='np-runtime-head'>"
        f"<span class='{tone}'>{health}</span></div>"
        f"<dl class='np-stats'>{stats}</dl><ul class='np-runtime-jobs'>{''.join(rows)}</ul></div>"
    )
