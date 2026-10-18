"""Teach Me command line, also used by the Electron app.

  teachme.py create --topic "wash dishes" --level pure_beginner --mode mix [--language Spanish] [--description "..."] [--placement answers.json] [--source https://example.com ...] [--source-file guide.pdf ...]
  teachme.py placement --topic "wash dishes"   (quiz from simple to expert, as JSON)
  teachme.py resume <folder>       (build only missing / failed parts)
  teachme.py list --json
  teachme.py progress <folder> --day 2 --complete [--quiz-score 4 --quiz-total 5]
  teachme.py delete <folder>
  teachme.py email <folder> --to you@example.com   (sends <folder>.pdf; SMTP settings in .env)
  teachme.py providers             (which AI keys / local models are available, as JSON; never the keys)
  teachme.py check                 (each task's models: usable or not, and why)

`create` prints @@COURSE / @@STATUS / @@PLAN / @@ARTIFACT lines while it works
and ends with @@DONE <folder> or @@FAILED <reason>.
"""
from __future__ import annotations

import argparse
import json
import sys

from dotenv import load_dotenv

import course_builder as cb

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def create(args) -> int:
    from llm import LLM, safe_error
    load_dotenv(cb.ensure_env())
    config = cb.load_config()
    builder = None
    try:
        llm = LLM(config, progress=lambda m: print(m, file=sys.stderr, flush=True))
        if args.cmd == "resume":
            builder = cb.CourseBuilder.resume(args.folder, config, llm)
        else:
            placement = None
            if args.placement:
                with open(args.placement, encoding="utf-8") as f:
                    placement = json.load(f)
            builder = cb.CourseBuilder(args.topic, args.level, args.mode, config, llm, placement=placement,
                                       sources=args.source, files=args.source_file, language=args.language,
                                       description=args.description)
        folder = builder.build()
    except Exception as exc:
        email_report(builder, config)
        cb.emit("FAILED", safe_error(exc))
        return 1
    email_report(builder, config)
    cb.emit("DONE", folder.name)
    return 0


def email_report(builder, config: dict) -> None:
    """Email build_report.json to REPORT_EMAIL (.env) after a course's first build
    (and after retries too when `report_email_retries` is true). Unset: no email."""
    import os
    to = (os.getenv("REPORT_EMAIL") or config.get("report_email") or "").strip()
    if not (builder and builder.report_path and to):
        return
    if not (builder.first_build or config.get("report_email_retries")):
        return
    import course_report
    cb.emit("STATUS", course_report.email(builder.report_path, builder.course, to))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("create")
    c.add_argument("--topic", required=True)
    c.add_argument("--level", required=True, choices=list(cb.LEVELS))
    c.add_argument("--mode", required=True, choices=list(cb.MODES))
    c.add_argument("--source", action="append", default=[],
                   help="a website to write the course from (repeat for up to 5); its linked pages are read too")
    c.add_argument("--source-file", action="append", default=[],
                   help="a Markdown, text, Word (.docx) or PDF file to write the course from (repeatable)")
    c.add_argument("--placement", help="JSON file: the answered placement quiz, to build a course for the gaps")
    c.add_argument("--language", default="English", help="language the course is written in (any language name)")
    c.add_argument("--description", default="", help="the learner's goals, focus and background, in their words")

    pq = sub.add_parser("placement", help="write a placement quiz (simple to expert) as JSON")
    pq.add_argument("--topic", required=True)
    pq.add_argument("--language", default="English")
    pq.add_argument("--description", default="")

    r = sub.add_parser("resume", help="build only the missing or failed parts of a course")
    r.add_argument("folder")

    ls = sub.add_parser("list")
    ls.add_argument("--json", action="store_true")

    p = sub.add_parser("progress")
    p.add_argument("folder")
    p.add_argument("--day", type=int, required=True)
    done = p.add_mutually_exclusive_group()
    done.add_argument("--complete", dest="complete", action="store_true", default=None)
    done.add_argument("--incomplete", dest="complete", action="store_false")
    p.add_argument("--quiz-score", type=int)
    p.add_argument("--quiz-total", type=int)

    d = sub.add_parser("delete", help="delete a course folder and everything in it")
    d.add_argument("folder")

    sub.add_parser("providers", help="which AI keys and local models are available (JSON, no secrets)")
    sub.add_parser("check", help="for each task, which of its models can be used and why not")

    e = sub.add_parser("email", help="email the course PDF (made by the app) over SMTP; exit 3 if not set up")
    e.add_argument("folder")
    e.add_argument("--to", required=True)

    args = parser.parse_args(argv)
    cb.ensure_env()           # first run: a .env with every setting blank, ready to fill in
    if args.cmd in ("create", "resume"):
        return create(args)
    if args.cmd == "placement":
        from llm import LLM, safe_error
        load_dotenv(cb.ensure_env())
        config = cb.load_config()
        try:
            llm = LLM(config, progress=lambda m: print(m, file=sys.stderr, flush=True))
            quiz = cb.make_placement(args.topic, llm, config, language=args.language,
                                     description=args.description)
        except Exception as exc:
            print(safe_error(exc), file=sys.stderr)
            return 1
        print(f"Placement quiz cost about ${llm.spent():.3f}", file=sys.stderr)
        print(json.dumps(quiz, ensure_ascii=False))
        return 0
    if args.cmd == "check":
        from llm import extra_problems, ignored_env_models, task_problems, task_status
        load_dotenv(cb.ensure_env())
        config = cb.load_config()
        status = task_status(config)
        for task, rows in status.items():
            print(task)
            for i, row in enumerate(rows, 1):
                print(f"  {i}. {'✓' if row['ok'] else '✗'} {row['model']}" + ("" if row["ok"] else f"  ({row['problem']})"))
            if not rows:
                print("  (no models listed in config.yaml)")
        problems = task_problems(status) + extra_problems(config)
        for name in ignored_env_models():
            print(f"\nNote: {name} in .env is ignored; each task's models are listed in config.yaml.")
        print("\n" + ("\n".join(["Problems:"] + [f"- {line}" for line in problems]) if problems
                      else "Every task has a usable model."))
        return 1 if problems else 0
    if args.cmd == "providers":
        from llm import provider_status
        load_dotenv(cb.ensure_env())
        print(json.dumps(provider_status(cb.load_config())))
        return 0
    if args.cmd == "email":
        import mailer
        load_dotenv(cb.ensure_env())
        try:
            path = cb.course_path(args.folder)
            pdf = path / f"{path.name}.pdf"
            if not pdf.is_file():
                raise ValueError("Make the PDF first")
            title = json.loads((path / "course.json").read_text(encoding="utf-8")).get("title") or path.name
            mailer.send_pdf(args.to, pdf, title)
        except mailer.NotConfigured as exc:
            print(str(exc), file=sys.stderr)
            return 3
        except Exception as exc:
            print(f"Email failed: {exc}", file=sys.stderr)
            return 2
        print(json.dumps({"sent": args.to}))
        return 0
    if args.cmd == "delete":
        try:
            cb.delete_course(args.folder)
        except (ValueError, OSError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(json.dumps({"deleted": args.folder}))
        return 0
    if args.cmd == "list":
        courses = cb.list_courses()
        if args.json:
            print(json.dumps(courses, ensure_ascii=False))
        else:
            for c in courses:
                print(f"{c['completed']}/{c['days']}  {c['status']:<8} {c['title']}  ({c['folder']})")
            if not courses:
                print(f"No courses yet. Create one with \"teachme.py create\", or browse ready-made "
                      f"courses at {cb.CATALOG_URL}")
        return 0
    try:
        progress = cb.set_progress(args.folder, args.day, args.complete, args.quiz_score, args.quiz_total)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(progress))
    return 0


if __name__ == "__main__":
    sys.exit(main())
