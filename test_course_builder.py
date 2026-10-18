import json
import shutil
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import course_builder as cb

PLAN = {"title": "Washing Dishes by Hand", "summary": "Clean dishes safely.", "goals": ["Wash a full load"],
        "days": [{"title": "Setup", "summary": "Tools and order.", "objectives": ["Set up two basins"],
                  "key_points": ["Hot water"], "video_queries": ["how to wash dishes by hand"]},
                 {"title": "Technique", "summary": "Scrub and rinse.", "objectives": ["Rinse well"],
                  "key_points": ["Order matters"], "video_queries": ["dish washing technique"]}]}
SVG = '<svg viewBox="0 0 800 400" onload="alert(1)"><script>x()</script><rect width="10" height="10"/><a href="https://evil">x</a></svg>'


class FakeLLM:
    def __init__(self, fail=()):
        self.fail = set(fail)
        self.labels = []
        self.prompts = {}

    def json(self, label, system, prompt, schema):
        self.labels.append(label)
        self.prompts[label] = prompt
        if label in self.fail:
            raise RuntimeError(f"boom {label}")
        if label == "course plan":
            return PLAN
        if label == "course category":
            return {"category": "Life Skills", "subcategory": "  Cleaning ", "topic": "Wash dishes by hand"}
        if label == "placement quiz":
            def q(level, concept, answer=0):
                return {"level": level, "concept": concept, "question": f"About {concept}?",
                        "choices": [f"{concept} right", "x", "y", "z"], "answer_index": answer, "explanation": "e"}
            return {"questions": [q("expert", "grease chemistry"), q("pure_beginner", "soap"),
                                  q("pure_beginner", "hot water"), q("pure_beginner", "too many"),
                                  q("beyond_basics", "order of washing"), q("wizard", "magic"),
                                  q("good_to_great", "bad answer", answer=7), q("good_to_great", "drying")]}
        if label.endswith("lesson"):
            return {"markdown": "## Lesson\n\n![Sink setup](figure:sink)\n\n![Missing](figure:nope)\n\n## Practice\n\nDo it.\n",
                    "figures": [{"id": "sink", "caption": "Sink setup", "svg": SVG},
                                {"id": "unplaced", "caption": "Drying rack", "svg": "<svg viewBox='0 0 8 8'></svg>"}]}
        if label.endswith("videos"):
            return {"picks": [{"id": "vid00000001", "why": "Shows it."}, {"id": "notacandidate", "why": "x"}]}
        if label.endswith("walkthrough"):
            return {"title": "Walkthrough", "scenes": [
                {"heading": "Fill the sink", "narration": "Fill the sink with warm water.", "svg": SVG},
                {"heading": "Broken", "narration": "No picture here.", "svg": "not svg"}]}
        if label.endswith("quiz"):
            return {"questions": [
                {"question": "Q1", "choices": ["a", "b", "c", "d"], "answer_index": 2, "explanation": "c."},
                {"question": "Bad", "choices": ["a", "b"], "answer_index": 5, "explanation": ""}]}
        raise AssertionError(label)


class FakeRequest:
    def __init__(self, data):
        self.data = data

    def execute(self):
        return self.data


class FakeYT:
    def search(self):
        return self

    def videos(self):
        return self

    def list(self, **kw):
        if "q" in kw:
            return FakeRequest({"items": [
                {"id": {"videoId": "vid00000001"}, "snippet": {"title": "Good", "channelTitle": "C", "description": ""}},
                {"id": {"videoId": "short000001"}, "snippet": {"title": "Short", "channelTitle": "C", "description": ""}},
                {"id": {"videoId": "long0000001"}, "snippet": {"title": "Long", "channelTitle": "C", "description": ""}}]})
        durations = {"vid00000001": "PT8M", "short000001": "PT45S", "long0000001": "PT2H"}
        return FakeRequest({"items": [
            {"id": i, "contentDetails": {"duration": durations[i]}, "statistics": {"viewCount": "1500"},
             "snippet": {"thumbnails": {"medium": {"url": "t"}}}} for i in kw["id"].split(",")]})


class QuotaError(Exception):
    resp = type("Resp", (), {"status": 403})()


class QuotaYT(FakeYT):
    searches = 0

    def list(self, **kw):
        if "q" in kw:
            QuotaYT.searches += 1
            raise QuotaError("<HttpError 403 when requesting https://youtube.googleapis.com/youtube/v3/search?"
                             "q=x&key=AIzaSyDMfSECRETSECRET returned \"quotaExceeded\">")
        return super().list(**kw)


class StatsYT(FakeYT):
    """Search results with the details videos.list gives for free."""
    VIDEOS = {"good0000001": ("PT8M", True, True), "weak0000001": ("PT6M", True, True),
              "short000001": ("PT45S", True, True), "noembed0001": ("PT5M", False, True),
              "broken00001": ("PT9M", True, False)}

    def list(self, **kw):
        if "q" in kw:
            return FakeRequest({"items": [{"id": {"videoId": i}, "snippet": {"title": i, "channelTitle": "C",
                                                                           "description": ""}} for i in self.VIDEOS]})
        items = []
        for i in kw["id"].split(","):
            duration, embeddable, captions = self.VIDEOS.get(i, ("PT8M", True, True))
            items.append({"id": i, "contentDetails": {"duration": duration, "caption": str(captions).lower()},
                          "statistics": {"viewCount": "1500"}, "status": {"embeddable": embeddable},
                          "snippet": {"thumbnails": {"medium": {"url": "t"}}}})
        return FakeRequest({"items": items})


class WatchingLLM(FakeLLM):
    """A FakeLLM with a Gemini key: it 'watches' videos."""
    SCORES = {"good0000001": 9, "weak0000001": 4}

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.watched = []

    def can_watch_videos(self):
        return True

    def video_json(self, label, system, prompt, schema, url):
        vid = url.rsplit("=", 1)[1]
        self.watched.append(vid)
        self.prompts[f"{label} {vid}"] = prompt
        if vid not in self.SCORES:
            raise RuntimeError("400 video unavailable")
        return {"score": self.SCORES[vid], "why": "Shows it.", "concerns": "", "clips": [
            {"start_seconds": 300, "end_seconds": 400, "label": "Rinsing"},
            {"start_seconds": 30, "end_seconds": 120, "label": "  Filling   the sink "}]}


class CourseBuilderTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        quiet = patch("course_builder.emit")
        self.events = quiet.start()
        self.addCleanup(quiet.stop)

    def build(self, mode, llm=None, yt=FakeYT, workers=2, **config):
        builder = cb.CourseBuilder("Teach me how to wash dishes", "pure_beginner", mode,
                                   {"workers": workers, "quiz_pass": 4, **config}, llm or FakeLLM(), yt,
                                   base=self.base, now=datetime(2026, 10, 14, 13, 5))
        folder = builder.build()
        return folder, json.loads((folder / "course.json").read_text(encoding="utf-8"))

    def test_folder_naming_and_collisions(self):
        now = datetime(2026, 10, 14, 13, 5)
        self.assertEqual(cb.folder_name("Teach me how to wash dishes!", "expert", now), "wash_dishes_expert_20261014_13")
        self.assertLessEqual(len(cb.slugify("a" * 10 + " " + "b" * 50)), 40)
        self.assertEqual(cb.slugify("How can I cook chicken breast?"), "cook_chicken_breast")
        self.assertEqual(cb.slugify("I want to learn how to juggle"), "juggle")
        first = cb.create_folder(self.base, "wash dishes", "expert", now)
        second = cb.create_folder(self.base, "wash dishes", "expert", now)
        self.assertEqual(second.name, first.name + "_2")

    def test_mix_mode_builds_everything(self):
        folder, course = self.build("mix")
        self.assertEqual(folder.name, "wash_dishes_pure_beginner_20261014_13")
        self.assertEqual(course["status"], "ready")
        self.assertEqual(course["outline"], "outline.md")
        day = course["days"][0]
        self.assertEqual((day["lesson"], day["videos"], day["quiz"]), ("day01.md", "day01_videos.json", "day01_quiz.json"))
        videos = json.loads((folder / "day01_videos.json").read_text(encoding="utf-8"))
        self.assertEqual([v["id"] for v in videos], ["vid00000001"])   # short, long and invented ids dropped
        quiz = json.loads((folder / "day01_quiz.json").read_text(encoding="utf-8"))
        self.assertEqual(len(quiz["questions"]), 1)
        self.assertEqual(quiz["pass"], 1)

    def test_lesson_figures_are_saved_and_sanitized(self):
        folder, course = self.build("docs")
        lesson = (folder / "day01.md").read_text(encoding="utf-8")
        self.assertIn("![Sink setup](figures/day01_fig1.svg)", lesson)
        self.assertNotIn("figure:", lesson)
        # A figure the model forgot to place still appears, before Practice.
        self.assertLess(lesson.index("![Drying rack](figures/day01_fig2.svg)"), lesson.index("## Practice"))
        svg = (folder / "figures" / "day01_fig1.svg").read_text(encoding="utf-8")
        for bad in ("script", "onload", "evil"):
            self.assertNotIn(bad, svg)
        self.assertIn('xmlns="http://www.w3.org/2000/svg"', svg)
        self.assertEqual(course["days"][0]["figures"], ["figures/day01_fig1.svg", "figures/day01_fig2.svg"])

    def test_artifacts_per_mode(self):
        _, docs = self.build("docs")
        self.assertIsNone(docs["days"][0]["videos"])
        self.assertIsNone(docs["days"][0]["quiz"])
        _, videos = self.build("videos")
        self.assertEqual(videos["days"][1]["videos"], "day02_videos.json")
        self.assertIsNone(videos["days"][1]["quiz"])
        self.assertEqual(videos["days"][1]["lesson"], "day02.md")

    def test_partial_failure_keeps_valid_manifest(self):
        _, course = self.build("mix", FakeLLM(fail={"day 2 quiz"}))
        self.assertEqual(course["status"], "ready")
        self.assertIsNone(course["days"][1]["quiz"])
        self.assertTrue(course["days"][1]["errors"])
        self.assertIn("1 day(s)", course["error"])

    def test_quota_gives_walkthrough_and_stops_searching(self):
        QuotaYT.searches = 0
        folder, course = self.build("videos", yt=QuotaYT, workers=1)
        for day in course["days"]:
            self.assertEqual(day["errors"], [])
            self.assertIsNone(day["videos"])                 # still missing, so Retry can add them
            self.assertIn("daily search quota is used up", day["video_note"])
            self.assertEqual(day["storyboard"], f"day{day['n']:02d}_storyboard.json")
        self.assertEqual(QuotaYT.searches, 1)                # second day never hit the API
        board = json.loads((folder / "day01_storyboard.json").read_text(encoding="utf-8"))
        self.assertEqual([s["image"] for s in board["scenes"]], ["figures/day01_scene1.svg"])   # bad scene dropped
        self.assertNotIn("script", (folder / "figures" / "day01_scene1.svg").read_text(encoding="utf-8"))
        log = (folder / "build.log").read_text(encoding="utf-8")
        self.assertNotIn("SECRET", log + json.dumps(course))

    def test_no_matching_videos_also_gets_walkthrough(self):
        class NoResultsYT(FakeYT):
            def list(self, **kw):
                return FakeRequest({"items": []}) if "q" in kw else super().list(**kw)
        _, course = self.build("mix", yt=NoResultsYT)
        self.assertEqual(course["days"][0]["videos"], "day01_videos.json")
        self.assertEqual(course["days"][0]["storyboard"], "day01_storyboard.json")

    def test_walkthrough_not_rebuilt_on_retry(self):
        llm = FakeLLM()
        folder, _ = self.build("videos", llm, yt=QuotaYT)
        llm.labels.clear()
        cb.CourseBuilder.resume(folder.name, {"workers": 1}, llm, FakeYT, base=self.base).build()
        self.assertEqual(sorted(llm.labels), ["day 1 videos", "day 2 videos"])   # real videos only
        course = json.loads((folder / "course.json").read_text(encoding="utf-8"))
        self.assertEqual((course["days"][0]["videos"], course["days"][0]["video_note"]), ("day01_videos.json", None))

    def test_web_videos_are_watched_and_judged(self):
        found = ["good0000001", "weak0000001", "gone0000001", "short000001", "broken00001", "good0000001"]

        class WebLLM(WatchingLLM):
            def web_json(self, label, system, prompt, schema):
                self.labels.append(label)
                return {"videos": [{"url": f"https://www.youtube.com/watch?v={i}&t=5", "title": "?", "channel": "?"}
                                   for i in found] + [{"url": "https://youtube.com/shorts/abcdefghijk",
                                                       "title": "?", "channel": "?"}]}

        def oembed(vid):
            return None if vid.startswith("gone") else {
                "id": vid, "title": f"Title {vid}", "channel": "Chan", "thumbnail": "thumb", "url": "u"}

        llm = WebLLM()
        with patch("youtube.oembed", oembed), patch.dict("os.environ", {"YOUTUBE_API_KEY": "k"}):
            folder, course = self.build("mix", llm, yt=StatsYT, video_source="web", judge_candidates=5)
        day = course["days"][0]
        self.assertEqual((day["errors"], day["video_note"]), ([], None))
        videos = json.loads((folder / day["videos"]).read_text(encoding="utf-8"))
        self.assertEqual([(v["id"], v["title"], v["score"], v["duration"], v["start"], v["end"]) for v in videos],
                         [("good0000001", "Title good0000001", 9, "8:00", 30, 400)])
        self.assertEqual(videos[0]["clips"], [{"start": 30, "end": 120, "label": "Filling the sink"},
                                              {"start": 300, "end": 400, "label": "Rinsing"}])
        self.assertEqual(videos[0]["watch_seconds"], 190)                   # only the clips, not all 8 minutes
        self.assertEqual(videos[0]["url"], "https://www.youtube.com/watch?v=good0000001&t=30s")
        self.assertEqual(videos[0]["checked"], "watched")
        check = json.loads((folder / day["video_check"]).read_text(encoding="utf-8"))
        results = {c["id"]: c["result"] for c in check["candidates"]}
        self.assertEqual(results["good0000001"], "kept")
        self.assertEqual(results["weak0000001"], "rejected: weak match")
        self.assertIn("embedded", results["gone0000001"])
        self.assertEqual(results["short000001"], "rejected: too short")
        self.assertIn("couldn't be watched", results["broken00001"])
        self.assertEqual(len(results), 5)                       # duplicate and Shorts link dropped
        self.assertEqual(sorted(set(llm.watched)), ["broken00001", "good0000001", "weak0000001"])   # filtered first
        prompt = llm.prompts["day 1 video judge good0000001"]
        self.assertIn("## Lesson", prompt)                      # judged against the finished lesson
        self.assertIn("length: 8:00", prompt)
        self.assertEqual(day["storyboard"], "day01_storyboard.json")   # walkthrough made as well

    def test_youtube_videos_are_watched_up_to_the_daily_limit(self):
        llm = WatchingLLM()
        folder, course = self.build("videos", llm, yt=StatsYT, judge_candidates=2)
        day = course["days"][0]
        videos = json.loads((folder / day["videos"]).read_text(encoding="utf-8"))
        self.assertEqual([v["id"] for v in videos], ["good0000001"])
        check = {c["id"]: c["result"] for c in json.loads((folder / day["video_check"]).read_text(encoding="utf-8"))
                 ["candidates"]}
        self.assertEqual(check["noembed0001"], "rejected: can't be embedded")
        self.assertIn("judging stops at 2", check["broken00001"])     # captioned videos were watched first
        self.assertNotIn("day 1 videos", llm.labels)                  # no title-only pick when Gemini can watch

    def test_judge_outage_leaves_the_day_for_retry(self):
        class DownLLM(WatchingLLM):
            def video_json(self, label, system, prompt, schema, url):
                raise RuntimeError("503 overloaded")
        _, course = self.build("videos", DownLLM(), yt=StatsYT)
        day = course["days"][0]
        self.assertIsNone(day["videos"])                         # Retry will check again
        self.assertIn("couldn't be checked", day["video_note"])
        self.assertEqual(day["errors"], [])
        self.assertEqual(day["storyboard"], "day01_storyboard.json")

    def test_clips_are_sanity_checked(self):
        def clips(*spans, seconds=480):
            found = cb.video_clips({"clips": [{"start_seconds": a, "end_seconds": b, "label": "x"} for a, b in spans]},
                                   seconds)
            return [(c["start"], c["end"]) for c in found]
        self.assertEqual(clips((30, 400)), [(30, 400)])
        self.assertEqual(clips((0, 480)), [])                                  # the whole video
        self.assertEqual(clips((500, 600), (60, 10), (100, 105)), [])         # outside, backwards, too short
        self.assertEqual(clips((200, 300), (20, 80), (290, 350), (85, 120)), [(20, 120), (200, 350)])   # joined
        self.assertEqual(clips((10, 40), (60, 90), (110, 140), (200, 260)), [(10, 40), (60, 90), (110, 140)])
        self.assertEqual(clips((400, 999)), [(400, 480)])                      # cut at the video's end
        self.assertEqual(clips((0, 3000), seconds=None), [(0, 3000)])         # length unknown: as given
        self.assertEqual(cb.watch_seconds([], 480), 480)

    def test_narration_is_recorded_last(self):
        class FakeNarrator:
            enabled, label = True, "kokoro:af_heart"
            spoken = []

            def unavailable_reason(self):
                return None

            def speak(self, text):
                self.spoken.append(text)
                return b"ID3fake", [{"word": "Fill", "start": 0.0, "end": 0.3}]

        with patch("narration.Narrator", lambda config: FakeNarrator()):
            folder, course = self.build("videos", yt=QuotaYT, tts_engine="kokoro")
        day = course["days"][0]
        self.assertEqual(day["narration"], "kokoro:af_heart")
        board = json.loads((folder / day["storyboard"]).read_text(encoding="utf-8"))
        self.assertEqual(board["scenes"][0]["audio"], "audio/day01_scene1.mp3")
        self.assertEqual(board["scenes"][0]["words"][0]["word"], "Fill")
        self.assertEqual((folder / "audio" / "day01_scene1.mp3").read_bytes(), b"ID3fake")

    def test_narration_skipped_when_server_is_down(self):
        class DownNarrator:
            enabled, label = True, "kokoro:af_heart"

            def unavailable_reason(self):
                return "the Kokoro server at http://localhost:8880 is not answering"

            def speak(self, text):
                raise AssertionError("must not be called")

        with patch("narration.Narrator", lambda config: DownNarrator()):
            folder, course = self.build("videos", yt=QuotaYT, tts_engine="kokoro")
        day = course["days"][0]
        self.assertIsNone(day["narration"])                      # Retry records it once the server is up
        self.assertEqual(day["errors"], [])
        self.assertIsNone(course["error"])
        self.assertIn("not answering", course["narration_pending"])   # so the app offers Retry

    def test_resume_builds_only_missing_parts(self):
        llm = FakeLLM(fail={"day 2 quiz"})
        folder, course = self.build("mix", llm)
        self.assertIsNone(course["days"][1]["quiz"])
        llm.fail.clear()
        llm.labels.clear()
        cb.CourseBuilder.resume(folder.name, {"workers": 2}, llm, FakeYT, base=self.base).build()
        self.assertEqual(llm.labels, ["day 2 quiz"])      # nothing else is regenerated
        course = json.loads((folder / "course.json").read_text(encoding="utf-8"))
        self.assertEqual(course["days"][1]["quiz"], "day02_quiz.json")
        self.assertEqual((course["status"], course["error"], course["days"][1]["errors"]), ("ready", None, []))

    def test_quiz_answers_are_shuffled(self):
        import random
        rng = random.Random(1)
        positions = set()
        for _ in range(40):
            choices, answer = cb.shuffle_choices(["right", "w1", "w2", "All of the above"], 0, rng)
            self.assertEqual(choices[answer], "right")
            self.assertEqual(choices[3], "All of the above")        # stays last
            positions.add(answer)
        self.assertEqual(positions, {0, 1, 2})
        choices, answer = cb.shuffle_choices(["w", "None of the above"], 1, rng)
        self.assertEqual((choices, answer), (["w", "None of the above"], 1))

    def test_placement_quiz_runs_simple_to_expert(self):
        quiz = cb.make_placement("  wash   dishes ", FakeLLM(), {"placement_questions": 8})
        self.assertEqual(quiz["topic"], "wash dishes")
        qs = quiz["questions"]
        self.assertEqual([q["level"] for q in qs],          # sorted; 2 per level; bad level/answer dropped
                         ["pure_beginner", "pure_beginner", "beyond_basics", "good_to_great", "expert"])
        self.assertEqual([q["concept"] for q in qs[:2]], ["soap", "hot water"])
        for q in qs:
            self.assertEqual(q["choices"][q["answer_index"]], f"{q['concept']} right")   # shuffle kept the answer

    def test_interview_answers_reach_every_prompt(self):
        # The new-course interview: title, description, sources, format, level, language.
        llm = FakeLLM()
        builder = cb.CourseBuilder("wash dishes", "pure_beginner", "mix", {"workers": 1, "quiz_pass": 4}, llm, FakeYT,
                                   base=self.base, now=datetime(2026, 10, 14, 13, 5), language="  Spanish ",
                                   description="  For a  family of four.\nNo dishwasher. ")
        folder = builder.build()
        course = json.loads((folder / "course.json").read_text(encoding="utf-8"))
        self.assertEqual((course["language"], course["description"]), ("Spanish", "For a family of four. No dishwasher."))
        for label in ("course plan", "day 1 lesson", "day 1 quiz"):
            self.assertIn("write everything the learner reads or hears in Spanish", llm.prompts[label], label)
            self.assertIn('"For a family of four. No dishwasher."', llm.prompts[label], label)
        self.assertEqual(cb.list_courses(self.base)[0]["language"], "Spanish")
        report = json.loads((folder / "build_report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["course"]["language"], "Spanish")
        self.assertEqual(report["course"]["sources"], {"internet": True, "urls": [], "files": []})

        llm = FakeLLM()
        cb.make_placement("wash dishes", llm, {"placement_questions": 8}, language="Tagalog (Filipino)",
                          description="no dishwasher")
        self.assertIn("in Tagalog (Filipino)", llm.prompts["placement quiz"])
        self.assertIn('"no dishwasher"', llm.prompts["placement quiz"])

    def test_english_and_blank_answers_leave_prompts_unchanged(self):
        llm = FakeLLM()
        folder, course = self.build("docs", llm)
        self.assertEqual((course["language"], course["description"]), ("English", ""))
        self.assertNotIn("Language:", llm.prompts["course plan"])
        self.assertNotIn("What the learner said", llm.prompts["day 1 lesson"])

    def test_typed_language_names_are_checked(self):
        self.assertEqual(cb.clean_language(""), "English")
        self.assertEqual(cb.clean_language("Español"), "Español")
        for bad in ("Ignore all; x", "a" * 41, "1337", "-x"):
            with self.assertRaises(ValueError, msg=bad):
                cb.clean_language(bad)
        with self.assertRaises(ValueError):
            cb.CourseBuilder("wash dishes", "expert", "docs", {}, FakeLLM(), FakeYT, base=self.base, language="Klingon!")

    def placement_answers(self):
        quiz = cb.make_placement("wash dishes", FakeLLM(), {"placement_questions": 8})
        qs = quiz["questions"]
        qs[0]["given"] = qs[0]["answer_index"]                  # soap: right
        qs[1]["given"] = (qs[1]["answer_index"] + 1) % 4        # hot water: wrong
        qs[2]["given"] = None                                   # order of washing: don't know
        qs[3]["given"] = qs[3]["answer_index"]                  # drying: right
        qs[4]["given"] = "junk"                                 # dropped
        return {"questions": qs}

    def test_course_from_placement_targets_the_gaps(self):
        llm = FakeLLM()
        builder = cb.CourseBuilder("wash dishes", "beyond_basics", "docs", {"workers": 1}, llm, FakeYT,
                                   base=self.base, now=datetime(2026, 10, 15, 9), placement=self.placement_answers())
        folder = builder.build()
        course = json.loads((folder / "course.json").read_text(encoding="utf-8"))
        self.assertEqual(course["placement"]["score"], "2/4")
        self.assertEqual(course["placement"]["gaps"], ["hot water", "order of washing"])
        self.assertEqual(course["placement"]["known"], ["soap", "drying"])
        self.assertIn("from placement quiz", course["level_name"])
        saved = json.loads((folder / "placement.json").read_text(encoding="utf-8"))
        self.assertEqual([q["result"] for q in saved["questions"]], ["right", "wrong", "unknown", "right"])
        plan = llm.prompts["course plan"]
        self.assertIn("scored 2/4", plan)
        self.assertIn("hot water (Pure beginner)", plan)
        self.assertIn("didn't know", plan)
        self.assertIn("correct: \"hot water right\"", plan)
        self.assertIn("fill exactly these gaps", plan)
        self.assertIn("Knowledge gaps found by the learner's placement quiz", llm.prompts["day 1 lesson"])
        with self.assertRaises(ValueError):
            cb.grade_placement({"questions": [{"level": "expert", "question": "q", "choices": ["a", "b"],
                                               "answer_index": 0, "given": 5}]})

    def test_cost_is_tracked_per_course(self):
        class CostLLM(FakeLLM):
            def spent(self):
                return 1.234
        llm = CostLLM(fail={"day 2 quiz"})
        folder, course = self.build("mix", llm)
        self.assertEqual(course["cost_usd"], 1.23)
        llm.fail.clear()
        cb.CourseBuilder.resume(folder.name, {"workers": 1}, llm, FakeYT, base=self.base).build()
        course = json.loads((folder / "course.json").read_text(encoding="utf-8"))
        self.assertEqual(course["cost_usd"], 2.46)                 # a retry adds to the total
        self.assertEqual(cb.list_courses(self.base)[0]["cost_usd"], 2.46)

    def test_task_models_and_prices(self):
        from types import SimpleNamespace
        import llm as llm_mod
        self.assertEqual([llm_mod.task_of(x) for x in ("course plan", "day 12 lesson", "day 2 video check",
                                                       "placement quiz", "day 1 walkthrough")],
                         ["plan", "lesson", "video_check", "placement", "walkthrough"])
        meter = llm_mod.Meter()
        usage = SimpleNamespace(input_tokens=1_000_000, output_tokens=100_000, cache_read_input_tokens=0,
                                cache_creation_input_tokens=0, server_tool_use=SimpleNamespace(web_search_requests=3))
        meter.anthropic("x", SimpleNamespace(model="claude-sonnet-5", usage=usage))
        self.assertAlmostEqual(meter.total, 2 + 1 + 0.03)          # $2 in + $1 out + 3 searches
        meter.http("y", "openai/gpt-4.1-mini", {"usage": {"prompt_tokens": 5, "completion_tokens": 5, "cost": 0.5}})
        self.assertAlmostEqual(meter.total, 3.53)

        seen = []

        def fake_call(effort, on_message=None):
            def call(system, prompt, schema, model):
                seen.append((model, effort))
                return '{"title": "t", "summary": "s", "goals": [], "days": []}'
            return call
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "k"}, clear=False), patch("llm.anthropic_call", fake_call):
            real = llm_mod.LLM({"effort": "low", "tasks": {"plan": ["anthropic/claude-opus-5-5@high"],
                                                           "quiz": ["anthropic/claude-sonnet-5"]}},
                               progress=lambda m: None)
            real.json("course plan", "s", "p", cb.PLAN_SCHEMA)
            real.json("day 1 quiz", "s", "p", cb.PLAN_SCHEMA)
        self.assertEqual(seen, [("claude-opus-5-5", "high"), ("claude-sonnet-5", "low")])
        self.assertEqual(llm_mod.price_of("openai/gpt-6-luna"), (0.10, 0.50))   # OpenRouter ids priced too

    def test_model_ids_are_checked(self):
        import llm as llm_mod
        warnings = llm_mod.check_models({"tasks": {"plan": ["gemini/gemini-3.8-flash", "anthropic/claude-sonnet-5-5"],
                                                   "quiz": ["anthropic/claude-haiku-4-5"]}})
        self.assertEqual(len(warnings), 1)
        self.assertIn("tasks.plan", warnings[0])
        config = cb.load_config()                          # the shipped config.yaml must be clean
        self.assertEqual(llm_mod.check_models(config), [])
        for task in llm_mod.TASKS:                         # ...and list models for every task
            self.assertTrue(llm_mod.task_chains(config).get(task), task)
        with self.assertRaisesRegex(ValueError, "provider/model"):
            llm_mod.parse_route("gpt-6-luna")

    def test_each_task_tries_its_models_in_order(self):
        import llm as llm_mod
        from summary_providers import ProviderError
        env = {"ANTHROPIC_API_KEY": "a", "OPENAI_API_KEY": "o", "GEMINI_API_KEY": "g", "OPENROUTER_API_KEY": ""}
        config = {"tasks": {"lesson": ["openai/gpt-6-luna@none", "anthropic/claude-sonnet-5"],
                            "plan": ["openrouter/z-ai/glm-5.3", "gemini/gemini-3.8-flash@low",
                                     "anthropic/claude-sonnet-5@medium"]}}
        calls = []

        def http(provider, model, key, system, prompt, schema, max_tokens, on_usage=None, effort=None):
            calls.append((provider, model, effort))
            if provider == "gemini":
                raise ProviderError("The provider returned invalid JSON")
            on_usage({"usage": {"input_tokens": 1_000_000, "output_tokens": 1_000_000}})
            return '{"title": "t", "summary": "s", "goals": [], "days": []}'

        def anthropic(effort, on_message=None):
            def call(system, prompt, schema, model):
                calls.append(("anthropic", model, effort))
                return '{"title": "t", "summary": "s", "goals": [], "days": []}'
            return call
        with patch.dict("os.environ", env), patch("summary_providers.structured_http", http), \
                patch("llm.anthropic_call", anthropic):
            real = llm_mod.LLM(config, progress=lambda m: None)
            real.json("day 3 lesson", "s", "p", cb.PLAN_SCHEMA)
            real.json("course plan", "s", "p", cb.PLAN_SCHEMA)
            with self.assertRaisesRegex(ProviderError, "lists no models for the task 'quiz'"):
                real.json("day 3 quiz", "s", "p", cb.PLAN_SCHEMA)
            with self.assertRaises(ProviderError):
                real.json("eval lesson", "s", "p", cb.PLAN_SCHEMA, route=("gemini", "gemini-3.8-flash"),
                          fallback=False)
        self.assertEqual(calls, [
            ("openai", "gpt-6-luna", "none"),                                  # lesson: its 1st choice
            # plan: openrouter has no key (skipped), gemini fails twice, then the 3rd choice
            ("gemini", "gemini-3.8-flash", "low"), ("gemini", "gemini-3.8-flash", "low"),
            ("anthropic", "claude-sonnet-5", "medium"),
            ("gemini", "gemini-3.8-flash", None), ("gemini", "gemini-3.8-flash", None)])   # eval: that model only
        self.assertAlmostEqual(real.meter.model_cost("gpt-6-luna"), 0.60)   # priced from the table

    def test_missing_keys_are_reported_per_task(self):
        import llm as llm_mod
        config = {"tasks": {"lesson": ["openrouter/openai/gpt-6-luna", "gemini/gemini-3.8-flash"],
                            "video_judge": ["gemini/gemini-3.5-flash-lite", "openrouter/openai/gpt-6-luna"]}}
        with patch.dict("os.environ", {"OPENROUTER_API_KEY": "r", "GEMINI_API_KEY": ""}):
            status = llm_mod.task_status(config, online=False)
            problems = llm_mod.task_problems(status)
            config["tasks"]["video_judge"].append("openrouter/google/gemini-3.8-flash")
            self.assertTrue(llm_mod.task_status(config, online=False)["video_judge"][2]["ok"])   # Gemini via OpenRouter
        self.assertEqual([r["ok"] for r in status["lesson"]], [True, False])
        self.assertEqual(status["lesson"][1]["problem"], "no GEMINI_API_KEY in .env")
        self.assertTrue(status["video_judge"][1]["problem"].startswith("only Gemini can watch YouTube videos"))
        self.assertTrue(any(p.startswith("video_judge: no usable model") and "picked by title" in p
                            for p in problems))
        self.assertTrue(any(p.startswith("plan: no usable model (no models listed") for p in problems))
        self.assertFalse(any(p.startswith("lesson") for p in problems))

    def test_gemini_wire_format(self):
        from unittest.mock import Mock
        import llm as llm_mod
        from summary_providers import structured_http
        response = Mock(status_code=200)
        response.json.return_value = {
            "candidates": [{"finishReason": "STOP", "content": {"parts": [
                {"text": "thinking...", "thought": True}, {"text": '{"score": 8}'}]}}],
            "usageMetadata": {"promptTokenCount": 1_000_000, "candidatesTokenCount": 100_000,
                              "thoughtsTokenCount": 100_000}}
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        with patch("summary_providers.requests.post", return_value=response) as post:
            text = structured_http("gemini", "gemini-3.8-flash", "key", "sys", "prompt", {"type": "object"},
                                   1000, effort="low")
            self.assertEqual(text, '{"score": 8}')
            url, kwargs = post.call_args.args[0], post.call_args.kwargs
            self.assertTrue(url.endswith("/models/gemini-3.8-flash:generateContent"))
            self.assertEqual(kwargs["headers"], {"x-goog-api-key": "key"})
            config = kwargs["json"]["generationConfig"]
            self.assertEqual((config["responseMimeType"], config["thinkingConfig"]),
                             ("application/json", {"thinkingLevel": "LOW"}))
            llm_mod.gemini_video_call("key", "gemini-3.5-flash-lite", "sys", "p", {"type": "object"},
                                      "https://www.youtube.com/watch?v=abc", resolution="low")
            parts = post.call_args.kwargs["json"]["contents"][0]["parts"]
            self.assertEqual(parts[0], {"fileData": {"fileUri": "https://www.youtube.com/watch?v=abc"}})
            self.assertEqual(post.call_args.kwargs["json"]["generationConfig"]["mediaResolution"],
                             "MEDIA_RESOLUTION_LOW")
        meter = llm_mod.Meter()
        meter.http("x", "gemini-3.8-flash", response.json.return_value)
        self.assertAlmostEqual(meter.total, 0.75 + 0.2 * 3.75)              # thinking billed as output

    def test_eval_summary_picks_cheapest_passing_model(self):
        import eval_models
        rows = [{"candidate": "a:cheap", "topic": t, "verdict": v, "lesson_score": 8, "quiz_score": 8,
                 "unsafe": False, "cost": 0.001, "errors": []} for t, v in (("t1", "pass"), ("t2", "fail"))]
        rows += [{"candidate": "b:ok", "topic": t, "verdict": "pass", "lesson_score": 9, "quiz_score": 9,
                  "unsafe": False, "cost": 0.01, "errors": []} for t in ("t1", "t2")]
        rows += [{"candidate": "c:broken", "topic": "t1", "verdict": "error", "error": "x"},
                 {"candidate": "b:ok", "topic": "t3", "verdict": "error", "error": "timeout"},
                 {"candidate": "b:ok", "topic": "t3", "verdict": "pass", "lesson_score": 9, "quiz_score": 9,
                  "unsafe": False, "cost": 0.01, "errors": []}]                 # resumed: the retry counts
        summary = eval_models.summarize(rows, "anthropic:claude-sonnet-5", 0.9)
        self.assertIn("**Cheapest candidate passing 90%:** b:ok", summary)
        self.assertIn("| a:cheap | 50% |", summary)
        self.assertIn("| b:ok | 100% | 9.0 | 9.0 | 0 | 0 | 0 |", summary)
        self.assertEqual(eval_models.parse_candidates("openrouter/openai/gpt-6-luna@low, gemini/gemini-3.8-flash"),
                         [("openrouter", "openai/gpt-6-luna", "low"), ("gemini", "gemini-3.8-flash", None)])
        self.assertEqual(len(eval_models.TOPICS), 50)

    def test_build_report_logs_models_sources_cost_and_category(self):
        import llm as llm_mod

        class MeteredLLM(WatchingLLM):
            def __init__(self):
                super().__init__()
                self.meter = llm_mod.Meter()

            def json(self, label, system, prompt, schema):
                model, provider = {"course plan": ("gemini-3.8-flash", "gemini"),
                                   "course category": ("gpt-5-nano", "openai")}.get(label, ("gpt-6-luna", "openai"))
                self.meter.add(label, model, 1000, 2000, 0.01, provider=provider)
                return super().json(label, system, prompt, schema)

            def video_json(self, label, system, prompt, schema, url):
                self.meter.add(label, "gemini-3.5-flash-lite", 60_000, 500, None, provider="gemini")
                return super().video_json(label, system, prompt, schema, url)

        llm = MeteredLLM()
        folder, course = self.build("mix", llm, yt=StatsYT)
        self.assertEqual(course["category"], {"category": "Life Skills", "subcategory": "Cleaning",
                                              "topic": "Wash dishes by hand", "source": "model"})
        report = json.loads((folder / "build_report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["category"]["category"], "Life Skills")
        self.assertEqual(report["models"]["course_design"], ["gemini:gemini-3.8-flash", "openai:gpt-5-nano"])
        self.assertEqual(report["models"]["content"]["lesson"], ["openai:gpt-6-luna"])
        self.assertEqual(report["models"]["content"]["video_judge"], ["gemini:gemini-3.5-flash-lite"])
        judge = next(r for r in report["cost"]["by_task_and_model"] if r["task"] == "video_judge")
        self.assertEqual((judge["calls"], judge["unpriced_calls"]), (6, 6))   # 3 watched a day, no price yet
        llm_calls = sum(r["calls"] for r in report["cost"]["by_task_and_model"] if r["task"] != "video_judge")
        self.assertAlmostEqual(report["cost"]["total_usd"], 0.01 * llm_calls)
        refs = report["external_refs"]
        self.assertEqual([(v["day"], v["id"], v["start"]) for v in refs["youtube_videos_used"]],
                         [(1, "good0000001", 30), (2, "good0000001", 30)])
        self.assertEqual(len(refs["youtube_videos_checked"]), 10)
        self.assertEqual(refs["youtube_api_searches"], 2)
        self.assertEqual(report["runs"][0]["kind"], "create")

        # A retry adds a run and keeps the totals.
        llm.meter = llm_mod.Meter()
        llm.fail.add("never")
        cb.CourseBuilder.resume(folder.name, {"workers": 1}, llm, StatsYT, base=self.base).build()
        report = json.loads((folder / "build_report.json").read_text(encoding="utf-8"))
        self.assertEqual([r["kind"] for r in report["runs"]], ["create", "retry"])
        self.assertAlmostEqual(report["cost"]["total_usd"], 0.01 * llm_calls)

    def test_build_report_is_emailed_after_the_first_build(self):
        import teachme
        import mailer
        folder, course = self.build("docs")
        builder = cb.CourseBuilder.resume(folder.name, {"workers": 1}, FakeLLM(), FakeYT, base=self.base)
        builder.build()
        sent = []
        with patch("mailer.send_file", lambda to, path, subject, body, mime: sent.append((to, path.name, subject))):
            teachme.email_report(builder, {"report_email": "me@example.com"})       # a retry: not sent
            builder.first_build = True
            teachme.email_report(builder, {"report_email": "me@example.com"})
        self.assertEqual(sent, [("me@example.com", "build_report.json",
                                 "Teach Me build report: Washing Dishes by Hand")])
        with patch("mailer.send_file", side_effect=mailer.NotConfigured("Email isn't set up")):
            import course_report
            status = course_report.email(builder.report_path, builder.course, "me@example.com")
        self.assertIn("not emailed", status)

    def test_svg_text_is_repaired_or_rejected(self):
        fixed = cb.clean_svg('<svg viewBox="0 0 9 9"><text>Before & After, x < 5 &amp; done</text></svg>')
        self.assertIn("Before &amp; After, x &lt; 5 &amp; done", fixed)
        self.assertIsNone(cb.clean_svg('<svg viewBox="0 0 9 9"><g><text>unclosed</svg>'))

    def test_secrets_are_masked(self):
        from llm import mask_secrets
        text = mask_secrets("https://x/search?q=a&key=AIzaSyDMfABCDEFGHIJ sk-or-v1-abcdef123456 AIzaSyZZZZZZZZZZZZ")
        for secret in ("AIzaSyDMf", "abcdef123456", "ZZZZZZ"):
            self.assertNotIn(secret, text)

    def test_plan_failure_marks_course_failed(self):
        with self.assertRaises(RuntimeError):
            self.build("mix", FakeLLM(fail={"course plan"}))
        course = json.loads(next(self.base.glob("*/course.json")).read_text(encoding="utf-8"))
        self.assertEqual(course["status"], "failed")

    def test_progress_and_listing(self):
        folder, _ = self.build("docs")
        cb.set_progress(folder.name, 1, quiz_score=4, quiz_total=5, base=self.base)
        progress = cb.set_progress(folder.name, 1, True, base=self.base)
        self.assertTrue(progress["days"]["1"]["complete"])
        self.assertEqual(progress["days"]["1"]["quiz"]["score"], 4)
        listing = cb.list_courses(self.base)
        self.assertEqual((listing[0]["completed"], listing[0]["days"]), (1, 2))
        cb.set_progress(folder.name, 1, False, base=self.base)
        self.assertEqual(cb.list_courses(self.base)[0]["completed"], 0)
        with self.assertRaises(ValueError):
            cb.set_progress(folder.name, 9, True, base=self.base)
        with self.assertRaises(ValueError):
            cb.set_progress("../evil", 1, True, base=self.base)

    def test_delete_course(self):
        folder, _ = self.build("docs")
        with self.assertRaises(ValueError):
            cb.delete_course("..", base=self.base)
        manifest = folder / "course.json"
        course = json.loads(manifest.read_text(encoding="utf-8"))
        manifest.write_text(json.dumps(course | {"status": "building"}), encoding="utf-8")
        with self.assertRaises(ValueError):                    # a build is writing to it right now
            cb.delete_course(folder.name, base=self.base)
        manifest.write_text(json.dumps(course), encoding="utf-8")
        cb.delete_course(folder.name, base=self.base)
        self.assertFalse(folder.exists())
        self.assertEqual(list(self.base.iterdir()), [])
        self.assertEqual(cb.list_courses(self.base), [])


if __name__ == "__main__":
    unittest.main()
