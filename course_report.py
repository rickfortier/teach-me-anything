"""The build report for a course: which models did what, what outside sources were
used, roughly what it cost, and where the course sits in the category tree.

It is saved as build_report.json in the course folder (one entry per build run, so
a "Retry failed parts" run adds to it) and emailed to REPORT_EMAIL from .env
after a course's first build.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("teachme")

REPORT = "build_report.json"

# The site's top level, with example subcategories (MASTERPLAN.md, Categories).
CATEGORIES = {
    "Home Maintenance": ["Plumbing", "Electrical", "Painting", "Appliances"],
    "History": ["Ancient", "Medieval", "Modern", "Regional"],
    "Tech": ["Programming", "Computers", "Networking", "AI"],
    "Math": ["Arithmetic", "Algebra", "Geometry", "Statistics"],
    "Science": ["Physics", "Chemistry", "Biology", "Earth & Space"],
    "Life Skills": ["Cooking", "Money", "Health", "Communication"],
    "Arts & Languages": ["Music", "Drawing", "Writing", "Languages"],
}

CATEGORY_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["category", "subcategory", "topic"],
    "properties": {"category": {"type": "string", "enum": list(CATEGORIES)},
                   "subcategory": {"type": "string"}, "topic": {"type": "string"}},
}

CATEGORY_SYSTEM = """You file a new course into a learning site's catalog.

Pick the one category that fits best, then a subcategory. Use one of the listed subcategories when it fits; otherwise name a new one in the same style (1-3 words, Title Case). `topic` is the course's canonical topic name as it would appear in the catalog: short, plain, no level ("Fix a running toilet", "The Cold War")."""


def category_prompt(course: dict) -> str:
    tree = "\n".join(f"- {c}: {', '.join(subs)}" for c, subs in CATEGORIES.items())
    return (f"Categories and example subcategories:\n{tree}\n\n"
            f"The learner asked for: {course['topic']}\nCourse title: {course['title']}\n"
            f"Summary: {course.get('summary', '')}")


# Course design is the plan (and the category); everything else is content.
DESIGN_TASKS = {"plan", "category"}


def group_calls(calls: list[dict]) -> list[dict]:
    """One row per (task, provider, model): calls, tokens and cost."""
    rows = {}
    for c in calls:
        key = (c["task"], c["provider"], c["model"])
        row = rows.setdefault(key, {"task": c["task"], "provider": c["provider"], "model": c["model"], "calls": 0,
                                    "tokens_in": 0, "tokens_out": 0, "web_searches": 0, "cost_usd": 0.0,
                                    "unpriced_calls": 0})
        row["calls"] += 1
        row["tokens_in"] += c["tokens_in"] or 0
        row["tokens_out"] += c["tokens_out"] or 0
        row["web_searches"] += c.get("searches") or 0
        if c["cost"] is None:
            row["unpriced_calls"] += 1
        else:
            row["cost_usd"] += c["cost"]
    for row in rows.values():
        row["cost_usd"] = round(row["cost_usd"], 4)
    return sorted(rows.values(), key=lambda r: (r["task"] not in DESIGN_TASKS, r["task"], r["model"]))


def models_used(rows: list[dict]) -> dict:
    """{"course_design": [...], "content": {task: [...]}} with provider:model names."""
    design = sorted({f"{r['provider']}:{r['model']}" for r in rows if r["task"] in DESIGN_TASKS})
    content = {}
    for r in rows:
        if r["task"] not in DESIGN_TASKS:
            content.setdefault(r["task"], [])
            name = f"{r['provider']}:{r['model']}"
            if name not in content[r["task"]]:
                content[r["task"]].append(name)
    return {"course_design": design, "content": content}


def external_refs(folder: Path, course: dict, tts: str | None, youtube_searches: int) -> dict:
    """Everything the course drew on from outside: the videos kept, every video
    looked at, searches run, and the voice used."""
    kept, checked = [], []
    for day in course.get("days") or []:
        for rel, bucket in ((day.get("videos"), kept), (day.get("video_check"), checked)):
            if not rel:
                continue
            try:
                data = json.loads((folder / rel).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            items = data if isinstance(data, list) else data.get("candidates") or []
            for v in items:
                bucket.append({"day": day["n"], "id": v.get("id"), "title": v.get("title"),
                               "channel": v.get("channel"), "url": v.get("url"), "score": v.get("score"),
                               "start": v.get("start"), "end": v.get("end"), "clips": v.get("clips") or []}
                              | ({"result": v.get("result")} if bucket is checked else {}))
    return {"youtube_videos_used": kept, "youtube_videos_checked": checked,
            "youtube_api_searches": youtube_searches, "narration_voice": tts}


def source_summary(sources: dict | None) -> dict:
    """Where the course material came from, as chosen in the new-course interview."""
    if not sources:
        return {"internet": True, "urls": [], "files": []}
    return {"internet": False, "urls": sources.get("urls") or [], "files": sources.get("files") or []}


def write(folder: Path, course: dict, run: dict) -> Path:
    """Add this build run to build_report.json and refresh the totals."""
    path = folder / REPORT
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        report = {"runs": []}
    report["runs"].append(run)
    all_rows = group_calls([c for r in report["runs"] for c in r.get("calls_raw", [])])
    report.update({
        "course": {k: course.get(k) for k in ("title", "topic", "description", "level_name", "mode_name", "language",
                                              "created", "status", "error")}
                  | {"folder": folder.name, "days": len(course.get("days") or []),
                     "sources": source_summary(course.get("sources"))},
        "category": course.get("category"),
        "models": models_used(all_rows),
        "cost": {"total_usd": round(sum(r["cost"]["run_usd"] for r in report["runs"]), 4),
                 "by_task_and_model": all_rows,
                 "note": "Approximate: list prices times reported tokens. Calls with no known price count as $0 "
                         "and are listed under unpriced_calls."},
        "external_refs": run["external_refs"],
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
    return path


def make_run(calls: list[dict], started: datetime, refs: dict, first: bool) -> dict:
    rows = group_calls(calls)
    return {"started": started.isoformat(timespec="seconds"),
            "finished": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "kind": "create" if first else "retry",
            "cost": {"run_usd": round(sum(r["cost_usd"] for r in rows), 4),
                     "unpriced_calls": sum(r["unpriced_calls"] for r in rows)},
            "models": models_used(rows), "by_task_and_model": rows, "calls_raw": calls,
            "external_refs": refs}


def email(path: Path, course: dict, to: str) -> str:
    """Send the report; returns a one-line status (never raises)."""
    import mailer
    title = course.get("title") or course.get("topic") or path.parent.name
    category = course.get("category") or {}
    where = " › ".join(filter(None, (category.get("category"), category.get("subcategory")))) or "uncategorized"
    report = json.loads(path.read_text(encoding="utf-8"))
    body = (f"Course: {title}\nFolder: {path.parent.name}\nCategory: {where}\n"
            f"Status: {course.get('status')}{' - ' + course['error'] if course.get('error') else ''}\n"
            f"Approximate cost: ${report['cost']['total_usd']:.2f}\n"
            f"Course design: {', '.join(report['models']['course_design']) or 'none'}\n"
            + "".join(f"{task}: {', '.join(names)}\n" for task, names in report["models"]["content"].items())
            + f"YouTube videos used: {len(report['external_refs']['youtube_videos_used'])}\n\n"
            "The full report is attached.\n")
    try:
        mailer.send_file(to, path, f"Teach Me build report: {title}", body, ("application", "json"))
    except mailer.NotConfigured as exc:
        return f"Build report saved; not emailed ({exc})"
    except Exception as exc:
        log.warning("build report email failed: %s", exc)
        return f"Build report saved; email to {to} failed ({type(exc).__name__}: {exc})"
    return f"Build report emailed to {to}"
