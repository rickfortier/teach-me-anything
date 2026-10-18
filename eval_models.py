"""Model eval: which cheap model writes good enough lessons and quizzes?

Builds one lesson and one quiz per topic with every candidate model, has a
stronger grader model check each against the day's plan, and reports the pass
rate and cost per candidate. Pick the cheapest model that passes; rerun when
prices or models change.

  python eval_models.py --estimate                      (cost estimate, no API calls)
  python eval_models.py                                 (50 topics, candidates from config.yaml)
  python eval_models.py --topics 5 --candidates openrouter/openai/gpt-6-luna,gemini/gemini-3.8-flash
  python eval_models.py --resume evals/20261016_1530    (finish an interrupted run)

Candidates and the grader come from `eval:` in config.yaml. Each topic's plan is
made once (by the plan task's model) and cached in evals/plans/, so every
candidate writes the same day. Results go to evals/<run>/results.jsonl and
evals/<run>/summary.md.
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

import course_builder as cb

EVALS = cb.ROOT / "evals"

# 50 topics across the site's categories, from a household fix to a technical subject.
TOPICS = [
    # Home Maintenance
    ("Home Maintenance", "Fix a running toilet"), ("Home Maintenance", "Patch a hole in drywall"),
    ("Home Maintenance", "Replace a light switch"), ("Home Maintenance", "Paint a room"),
    ("Home Maintenance", "Unclog a kitchen sink"), ("Home Maintenance", "Caulk a bathtub"),
    ("Home Maintenance", "Clean a dryer vent"),
    # History
    ("History", "The fall of Rome"), ("History", "The Cold War"), ("History", "The printing press"),
    ("History", "The French Revolution"), ("History", "Ancient Egypt"), ("History", "The Silk Road"),
    ("History", "The civil rights movement"),
    # Tech
    ("Tech", "Python basics"), ("Tech", "Build a home network"), ("Tech", "How the internet works"),
    ("Tech", "SQL for beginners"), ("Tech", "Git version control"), ("Tech", "How large language models work"),
    ("Tech", "Back up a computer"),
    # Math
    ("Math", "Fractions"), ("Math", "Intro to statistics"), ("Math", "Solving linear equations"),
    ("Math", "The Pythagorean theorem"), ("Math", "Probability basics"), ("Math", "Derivatives"),
    ("Math", "Percentages and interest"),
    # Science
    ("Science", "How vaccines work"), ("Science", "Plate tectonics"), ("Science", "Photosynthesis"),
    ("Science", "Newton's laws of motion"), ("Science", "The periodic table"), ("Science", "Climate and weather"),
    ("Science", "DNA and genes"),
    # Life Skills
    ("Life Skills", "Make a budget"), ("Life Skills", "Knife skills"), ("Life Skills", "Cook rice perfectly"),
    ("Life Skills", "Basic first aid"), ("Life Skills", "Negotiate a salary"), ("Life Skills", "File your taxes"),
    ("Life Skills", "Start running"),
    # Arts & Languages
    ("Arts & Languages", "Guitar chords"), ("Arts & Languages", "Spanish for travel"),
    ("Arts & Languages", "Drawing faces"), ("Arts & Languages", "Write a short story"),
    ("Arts & Languages", "Read sheet music"), ("Arts & Languages", "Photography composition"),
    ("Arts & Languages", "Public speaking"), ("Arts & Languages", "Calligraphy basics"),
]

GRADE_SYSTEM = """You review one day of a self-study course written by another model: a lesson in Markdown and a multiple-choice quiz. Check them against the day's plan and against what you know to be true.

Score each 0-10:
- lesson_score: 10 = accurate, complete for the objectives, right level, clear; 7 = good enough to publish with no edits; 4 = needs a rewrite; 0 = wrong or harmful.
- quiz_score: 10 = every answer key is correct, each explanation matches its answer, questions test the objectives; 0 = wrong keys.

`errors`: every factual mistake, wrong quiz key, or unsafe instruction, each as one short sentence quoting the problem. Empty if none. Don't list style preferences.
`unsafe`: true if following the lesson could hurt someone or damage property, or it omits a safety step a professional would insist on.
For history, politics, religion, economics and social policy, a lesson that presents only one side, skips the problem and its causes, or states contested opinions as fact is an error: list it and fail it.
`verdict`: "pass" only if you would publish both as they are."""

GRADE_SCHEMA = cb._obj(
    lesson_score={"type": "integer"}, quiz_score={"type": "integer"},
    errors=cb._STRS, unsafe={"type": "boolean"}, verdict={"type": "string", "enum": ["pass", "fail"]},
)

# Rough tokens per run, for --estimate: (in, out).
EST_TOKENS = {"lesson": (3_000, 8_000), "quiz": (3_000, 2_000), "grade": (12_000, 1_500), "plan": (2_000, 4_000)}


def parse_candidates(text: str) -> list[tuple]:
    """'openrouter/openai/gpt-6-luna@low,gemini/gemini-3.8-flash' -> [(provider, model, effort), ...]"""
    from llm import parse_route
    return [parse_route(item) for item in filter(None, (t.strip() for t in (text or "").split(",")))]


def eval_settings(config: dict, args) -> tuple[list[tuple], tuple]:
    from llm import parse_route
    settings = config.get("eval") or {}
    candidates = parse_candidates(args.candidates) if args.candidates else         [parse_route(c) for c in settings.get("candidates") or []]
    if not candidates:
        raise ValueError("No candidates: set eval.candidates in config.yaml or pass --candidates")
    return candidates, parse_route(settings.get("grader") or "anthropic/claude-sonnet-5@medium")


def estimate(n_topics: int, candidates, grader, plan_route) -> str:
    from llm import price_of
    lines, total = [], 0.0

    def cost(model, step, runs):
        price = price_of(model)
        if not price:
            return None
        tin, tout = EST_TOKENS[step]
        return runs * (tin * price[0] + tout * price[1]) / 1e6

    plan = cost(plan_route[1], "plan", n_topics)
    lines.append(f"plans ({plan_route[1]}): " + (f"${plan:.2f}" if plan is not None else "price unknown"))
    total += plan or 0
    for provider, model, *_ in candidates:
        c = [cost(model, "lesson", n_topics), cost(model, "quiz", n_topics)]
        known = [x for x in c if x is not None]
        lines.append(f"{provider}:{model}: " + (f"${sum(known):.2f}" if len(known) == 2 else "price unknown"))
        total += sum(known)
    grade = cost(grader[1], "grade", n_topics * len(candidates))
    lines.append(f"grading ({grader[1]}): " + (f"${grade:.2f}" if grade is not None else "price unknown"))
    total += grade or 0
    lines.append(f"total: about ${total:.2f} for {n_topics} topics x {len(candidates)} candidates "
                 "(thinking tokens not included)")
    return "\n".join(lines)


class Eval:
    def __init__(self, llm, config: dict, run_dir: Path, candidates, grader, level: str):
        self.llm, self.config, self.run_dir = llm, config, run_dir
        self.candidates, self.grader, self.level = candidates, grader, level
        self.lock = threading.Lock()
        self.results_path = run_dir / "results.jsonl"
        self.done = set()
        if self.results_path.exists():
            for line in self.results_path.read_text(encoding="utf-8").splitlines():
                row = json.loads(line)
                if row["verdict"] != "error":            # failed calls are tried again on --resume
                    self.done.add((row["topic"], row["candidate"]))

    # A one-day slice of a real course plan, shared by every candidate.
    def plan(self, topic: str) -> tuple[dict, dict]:
        plans = EVALS / "plans"
        plans.mkdir(parents=True, exist_ok=True)
        path = plans / f"{cb.slugify(topic)}_{self.level}.json"
        if path.exists():
            plan = json.loads(path.read_text(encoding="utf-8"))
        else:
            plan = self.llm.json("course plan", cb.PLAN_SYSTEM,
                                 cb.plan_prompt(topic, self.level, "mix", 10), cb.PLAN_SCHEMA)
            cb.write_json(path, plan)
        course = {"title": plan["title"], "topic": topic, "level": self.level, "mode": "mix", "placement": None,
                  "days": [{"n": i, **d} for i, d in enumerate(plan["days"], 1)]}
        if not course["days"]:
            raise ValueError("The plan has no days")
        # The second day when there is one: more substance than an introduction.
        return course, course["days"][min(1, len(course["days"]) - 1)]

    def run_one(self, category: str, topic: str, course: dict, day: dict, candidate: tuple) -> dict:
        provider, model = candidate[:2]
        name = f"{provider}:{model}"
        row = {"category": category, "topic": topic, "candidate": name, "day": day["title"]}
        before = self.llm.meter.model_cost(model)
        try:
            lesson = self.llm.json("eval lesson", cb.LESSON_SYSTEM, cb.lesson_prompt(course, day), cb.LESSON_SCHEMA,
                                   route=candidate, fallback=False)
            quiz = self.llm.json("eval quiz", cb.QUIZ_SYSTEM,
                                 cb.course_context(course, day) + "\n\nWrite exactly 5 questions.", cb.QUIZ_SCHEMA,
                                 route=candidate, fallback=False)
        except Exception as exc:
            from llm import safe_error
            return row | {"verdict": "error", "error": safe_error(exc)}
        row["cost"] = round(self.llm.meter.model_cost(model) - before, 5)
        figures = len(lesson.get("figures") or [])
        bad_svgs = sum(1 for f in lesson.get("figures") or [] if not cb.clean_svg(f.get("svg", "")))
        questions = cb.valid_questions(quiz.get("questions") or [])
        prompt = (cb.course_context(course, day) + "\n\n## Lesson to review\n\n" + lesson.get("markdown", "")
                  + "\n\n## Quiz to review\n\n" + json.dumps(questions, ensure_ascii=False, indent=1))
        try:
            grade = self.llm.json("eval grade", GRADE_SYSTEM, prompt, GRADE_SCHEMA, route=self.grader, fallback=False)
        except Exception as exc:
            from llm import safe_error
            return row | {"verdict": "error", "error": "grader: " + safe_error(exc)}
        verdict = "pass" if grade["verdict"] == "pass" and not grade["unsafe"] and questions else "fail"
        return row | {"verdict": verdict, "lesson_score": grade["lesson_score"], "quiz_score": grade["quiz_score"],
                      "unsafe": grade["unsafe"], "errors": grade["errors"], "questions": len(questions),
                      "figures": figures, "bad_svgs": bad_svgs, "words": len(lesson.get("markdown", "").split())}

    def record(self, row: dict) -> None:
        with self.lock:
            with self.results_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        score = f"{row.get('lesson_score', '-')}/{row.get('quiz_score', '-')}"
        print(f"{row['verdict']:<5} {score:<5} {row['candidate']:<36} {row['topic']}", flush=True)

    def run(self, topics, workers: int) -> None:
        def topic_job(item):
            category, topic = item
            todo = [c for c in self.candidates if (topic, f"{c[0]}:{c[1]}") not in self.done]
            if not todo:
                return
            try:
                course, day = self.plan(topic)
            except Exception as exc:
                from llm import safe_error
                print(f"plan failed for {topic}: {safe_error(exc)}", file=sys.stderr, flush=True)
                return
            for candidate in todo:
                self.record(self.run_one(category, topic, course, day, candidate))
        with ThreadPoolExecutor(max(1, workers)) as pool:
            list(pool.map(topic_job, topics))


def summarize(rows: list[dict], grader: str, min_pass: float) -> str:
    # A resumed run appends a fresh row after an error; the last one counts.
    rows = list({(r["topic"], r["candidate"]): r for r in rows}.values())
    by = {}
    for row in rows:
        by.setdefault(row["candidate"], []).append(row)
    table = []
    for name, items in by.items():
        graded = [r for r in items if r["verdict"] in ("pass", "fail")]
        passed = sum(r["verdict"] == "pass" for r in graded)
        costs = [r["cost"] for r in graded if isinstance(r.get("cost"), (int, float))]
        avg = lambda key: sum(r[key] for r in graded) / len(graded) if graded else 0
        table.append({
            "candidate": name, "runs": len(items), "errors": len(items) - len(graded),
            "pass_rate": passed / len(graded) if graded else 0.0,
            "lesson": avg("lesson_score"), "quiz": avg("quiz_score"),
            "unsafe": sum(bool(r.get("unsafe")) for r in graded),
            "bad_svgs": sum(r.get("bad_svgs", 0) for r in graded),
            "cost": sum(costs) / len(costs) if costs else None,
        })
    table.sort(key=lambda t: (t["cost"] is None, t["cost"] or 0))
    lines = [f"# Model eval", "", f"Graded by {grader}. A run passes when the grader would publish the lesson and "
             f"quiz unedited and finds nothing unsafe. Cost is the lesson plus quiz for one day.", "",
             "| Candidate | Pass rate | Lesson | Quiz | Unsafe | Broken SVGs | Call errors | $ per day | $ per 10 days |",
             "|---|---|---|---|---|---|---|---|---|"]
    for t in table:
        cost = f"${t['cost']:.4f}" if t["cost"] is not None else "unknown"
        course = f"${t['cost'] * 10:.3f}" if t["cost"] is not None else "unknown"
        lines.append(f"| {t['candidate']} | {t['pass_rate']:.0%} | {t['lesson']:.1f} | {t['quiz']:.1f} | "
                     f"{t['unsafe']} | {t['bad_svgs']} | {t['errors']} | {cost} | {course} |")
    winner = next((t for t in table if t["pass_rate"] >= min_pass and t["cost"] is not None), None)
    lines += ["", f"**Cheapest candidate passing {min_pass:.0%}:** " +
              (f"{winner['candidate']}" if winner else "none. Raise the bar on the prompts or try stronger models.")]
    failures = [r for r in rows if r["verdict"] == "fail" and r.get("errors")]
    if failures:
        lines += ["", "## Problems the grader found", ""]
        for r in failures[:60]:
            lines.append(f"- **{r['candidate']}**, {r['topic']}: " + " / ".join(r["errors"][:3]))
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--candidates", help="comma list of provider/model[@effort] (default: eval.candidates in config.yaml)")
    parser.add_argument("--topics", type=int, default=len(TOPICS), help=f"how many topics (max {len(TOPICS)})")
    parser.add_argument("--level", default="pure_beginner", choices=list(cb.LEVELS))
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--resume", help="an earlier run folder to finish")
    parser.add_argument("--estimate", action="store_true", help="print the expected cost and stop")
    args = parser.parse_args(argv)

    load_dotenv(cb.ensure_env())
    config = cb.load_config()
    candidates, grader = eval_settings(config, args)
    settings = config.get("eval") or {}
    # Spread the sample across categories rather than taking the first N.
    topics = sorted(TOPICS, key=lambda t: TOPICS.index(t) % 7 * 100 + TOPICS.index(t))[:max(1, args.topics)]
    from llm import task_chains
    plan_route = (task_chains(config).get("plan") or [("?", "?", None)])[0]
    if args.estimate:
        print(estimate(len(topics), candidates, grader, plan_route))
        return 0

    from llm import LLM
    llm = LLM(config, progress=lambda m: None)
    run_dir = Path(args.resume) if args.resume else EVALS / datetime.now().strftime("%Y%m%d_%H%M")
    run_dir.mkdir(parents=True, exist_ok=True)
    print(f"Eval run: {run_dir}\n{len(topics)} topics x {len(candidates)} candidates, graded by {grader[1]}",
          flush=True)
    Eval(llm, config, run_dir, candidates, grader, args.level).run(topics, args.workers)
    rows = [json.loads(line) for line in (run_dir / "results.jsonl").read_text(encoding="utf-8").splitlines()] \
        if (run_dir / "results.jsonl").exists() else []
    summary = summarize(rows, f"{grader[0]}:{grader[1]}", float(settings.get("min_pass_rate", 0.9)))
    cb.write_atomic(run_dir / "summary.md", summary)
    print(f"\n{summary}\nTotal spent this run: about ${llm.spent():.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
