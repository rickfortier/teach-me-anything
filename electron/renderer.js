/* global marked, DOMPurify */
"use strict";

const LEVELS = [
  ["pure_beginner", "Pure beginner", "Never done it. Start from zero."],
  ["beyond_basics", "Beyond basics", "Know the basics, want to do it better."],
  ["good_to_great", "From good to great", "Already good. Want polish and pro habits."],
  ["expert", "Expert", "Advanced techniques, edge cases, trade-offs."],
];
const LEVEL_CHOICES = [...LEVELS,
  ["quiz", "Quiz me", "Not sure? Answer questions from simple to expert; the course fills your gaps."]];
const MODES = [
  ["docs", "Just written documents", "Daily lessons to read, with diagrams."],
  ["videos", "Only relevant videos", "Hand-picked videos for each day, with a short intro."],
  ["mix", "Documents, videos & quizzes", "Lessons, videos, and a quiz to check yourself."],
];

// The public site with ready-made courses, suggested while the course list is empty.
const CATALOG_URL = "http://teach-me-anything.com";

const $ = (id) => document.getElementById(id);
const state = { view: null, data: null, day: null, tab: null, quiz: null };

// Small DOM builder: el("div", {class: "x", onclick: fn}, child, "text", ...)
function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value == null || value === false) continue;
    if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (key === "class") node.className = value;
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat(Infinity)) {
    if (child == null || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function markdown(text) {
  const div = el("div", { class: "md" });
  div.innerHTML = DOMPurify.sanitize(marked.parse(text || ""));
  // A lone image paragraph followed by its alt text reads better as a caption.
  div.querySelectorAll("p > img:only-child").forEach((img) => {
    if (img.alt) img.parentElement.after(el("p", { class: "figure-caption" }, img.alt));
  });
  return div;
}

// Links inside lessons open in the real browser.
document.addEventListener("click", (event) => {
  const link = event.target.closest("a[href]");
  if (!link) return;
  event.preventDefault();
  window.api.openUrl(link.href);
});

// ------------------------------------------------------------------- home

function showSection(id) {
  for (const name of ["home", "placement", "course"]) $(name).classList.toggle("hidden", name !== id);
}

async function showHome() {
  stopPlayer();
  state.view = "home";
  state.data = null;
  state.placement = null;
  showSection("home");
  const result = await window.api.listCourses();
  const list = $("course-list");
  if (result.error) {
    list.replaceChildren(el("div", { class: "error" }, result.error));
    return;
  }
  if (!result.courses.length) {
    list.replaceChildren(el("div", { class: "empty" }, "No courses yet. Start one above, or browse ready-made ",
      "courses at ", el("button", { class: "link", onclick: () => window.api.openUrl(CATALOG_URL) },
        CATALOG_URL.replace(/^https?:\/\//, "")), "."));
    return;
  }
  list.replaceChildren(...result.courses.map(courseCard));
}

function statusBadge(course) {
  if (course.building) return el("span", { class: "badge building" }, "building…");
  if (course.status === "failed") return el("span", { class: "badge failed" }, "failed");
  if (course.status === "building") return el("span", { class: "badge failed" }, "interrupted");
  if (course.days && course.completed === course.days) return el("span", { class: "badge" }, "✔ finished");
  return null;
}

function courseCard(c) {
  const pct = c.days ? Math.round((100 * c.completed) / c.days) : 0;
  const created = c.created ? new Date(c.created).toLocaleDateString() : "";
  return el("div", { class: "course-card", onclick: () => openCourse(c.folder) },
    el("div", { class: "info" },
      el("div", { class: "title" }, c.title || c.topic, statusBadge(c)),
      el("div", { class: "meta" }, [c.level_name, c.mode_name, c.language !== "English" && c.language, `${c.days} days`, created,
        typeof c.cost_usd === "number" && `$${c.cost_usd.toFixed(2)}`].filter(Boolean).join(" · "))),
    el("div", { class: "bar" }, el("div", { style: `width:${pct}%` })),
    el("div", { class: "count" }, `${c.completed}/${c.days}`),
    c.building ? null : deleteButton(c));
}

// Two clicks to delete: the first arms the button for a few seconds.
function deleteButton(c) {
  let armed = null;
  const btn = el("button", { class: "delete", title: "Delete this course" }, "Delete");
  btn.addEventListener("click", async (event) => {
    event.stopPropagation();
    if (!armed) {
      btn.textContent = "Click again to delete";
      btn.classList.add("armed");
      armed = setTimeout(() => { armed = null; btn.textContent = "Delete"; btn.classList.remove("armed"); }, 4000);
      return;
    }
    clearTimeout(armed);
    btn.disabled = true;
    btn.textContent = "Deleting…";
    const result = await window.api.deleteCourse(c.folder);
    if (result.error) {
      btn.textContent = "Delete";
      btn.disabled = false;
      btn.classList.remove("armed");
      armed = null;
      btn.after(el("div", { class: "error" }, result.error));
      return;
    }
    await showHome();
  });
  return btn;
}

// ------------------------------------------------------------ new course: the interview
// One question at a time: title, description, sources, format, level (or Quiz me), language.
// Earlier answers stay listed above the current question and can be changed.

function newInterview() {
  return { step: 0, topic: "", description: "", useSites: false, sites: "", useFiles: false, files: [],
    mode: null, level: null, language: "", busy: false, error: null };
}
state.iv = newInterview();

const sourceSites = (iv) => iv.useSites ? iv.sites.split(/\r?\n/).map((l) => l.trim()).filter(Boolean) : [];
const sourceFiles = (iv) => iv.useFiles ? iv.files.map((f) => f.path) : [];
const choiceName = (items, value) => (items.find(([v]) => v === value) || [])[1] || "";

function sourcesSummary(iv) {
  const parts = [];
  if (sourceSites(iv).length) parts.push(`${sourceSites(iv).length} website${sourceSites(iv).length > 1 ? "s" : ""}`);
  if (sourceFiles(iv).length) parts.push(`${sourceFiles(iv).length} file${sourceFiles(iv).length > 1 ? "s" : ""}`);
  return parts.length ? parts.join(" + ") : "The internet";
}

// A text box whose value lives in state.iv[key]; Enter (without Shift in a textarea) moves on.
function ivText(tag, key, attrs) {
  const box = el(tag, { ...attrs, class: "iv-input" });
  box.value = state.iv[key];
  box.addEventListener("input", () => { state.iv[key] = box.value; state.iv.error = null; updateNext(); });
  box.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (tag === "input" || e.ctrlKey)) { e.preventDefault(); nextStep(); }
  });
  return box;
}

function ivChoices(items, key, cls) {
  return el("div", { class: `choices ${cls}` }, ...items.map(([value, name, desc]) => el("button", {
    class: "choice" + (value === "quiz" ? " quiz-me" : "") + (state.iv[key] === value ? " selected" : ""),
    onclick: () => { state.iv[key] = value; state.iv.error = null; nextStep(); },
  }, el("b", {}, name), el("small", {}, desc))));
}

function sourceToggle(key, name, desc, onSelect) {
  const iv = state.iv;
  const selected = key === "internet" ? !iv.useSites && !iv.useFiles : iv[key];
  return el("button", { class: "choice" + (selected ? " selected" : ""), onclick: onSelect },
    el("b", {}, name), el("small", {}, desc));
}

function sourcesBody() {
  const iv = state.iv;
  const pick = (key) => () => {
    if (key === "internet") { iv.useSites = false; iv.useFiles = false; } else iv[key] = !iv[key];
    iv.error = null;
    renderInterview();
  };
  const parts = [el("div", { class: "choices modes" },
    sourceToggle("internet", "The internet", "The AI writes from what it knows and finds videos online.", pick("internet")),
    sourceToggle("useSites", "Specific websites", "Up to 5 sites. Their linked pages are read too.", pick("useSites")),
    sourceToggle("useFiles", "Files on my computer", "Markdown, text, Word or PDF, up to 20.", pick("useFiles")))];
  if (iv.useSites) {
    parts.push(el("div", { class: "iv-sub" }, el("div", { class: "iv-sub-label" }, "Websites, one per line"),
      ivText("textarea", "sites", { rows: 3, spellcheck: "false", placeholder: "https://yourbusiness.com" })));
  }
  if (iv.useFiles) {
    parts.push(el("div", { class: "iv-sub" },
      el("div", { class: "files-row" }, el("button", { type: "button", onclick: addSourceFiles }, "Add files…"),
        el("span", { class: "fine" }, iv.files.length ? `${iv.files.length} added` : "None added yet")),
      el("ul", { class: "source-files" }, ...iv.files.map((f) => el("li", {}, el("span", {}, f.name),
        el("button", { title: "Remove", onclick: () => { iv.files = iv.files.filter((x) => x.path !== f.path); renderInterview(); } }, "✕"))))));
  }
  if (iv.useSites || iv.useFiles) {
    parts.push(el("div", { class: "fine" }, "Prices, products and policies come only from your pages and files, " +
      "with links or file and page references back. Scanned PDFs (pictures of pages) can't be read."));
  }
  return parts;
}

async function addSourceFiles() {
  const iv = state.iv;
  const picked = await window.api.pickSourceFiles();
  const have = new Set(iv.files.map((f) => f.path));
  iv.files = [...iv.files, ...picked.filter((f) => !have.has(f.path))].slice(0, 20);
  iv.error = picked.errors && picked.errors.length ? picked.errors.join(" · ") : null;
  renderInterview();
}

const STEPS = [
  { label: "Title",
    question: "What do you want to learn?",
    help: "A few words is enough, like \"how to wash dishes\" or \"Excel pivot tables\".",
    body: () => [ivText("input", "topic", { placeholder: "how to wash dishes", maxlength: 300 })],
    ok: (iv) => iv.topic.trim() ? null : "Type what you want to learn",
    answer: (iv) => iv.topic.trim() },
  { label: "Description",
    question: "Tell me a bit more about it.",
    help: "Optional. What you want to be able to do, what to focus on, what you already know. Ctrl+Enter to go on.",
    body: () => [ivText("textarea", "description", { rows: 4, maxlength: 1000,
      placeholder: "I want to cook weeknight dinners for a family of four. I can boil pasta and that's about it." })],
    ok: () => null,
    skippable: true,
    answer: (iv) => iv.description.trim() || "(skipped)" },
  { label: "Sources",
    question: "Where should the course material come from?",
    help: "Pick the internet, or your own websites and files (you can pick both of those).",
    body: sourcesBody,
    ok: (iv) => iv.useSites && !sourceSites(iv).length ? "Add at least one website, or unselect Specific websites"
      : iv.useSites && sourceSites(iv).length > 5 ? "At most 5 websites"
      : iv.useFiles && !iv.files.length ? "Add at least one file, or unselect Files on my computer" : null,
    answer: sourcesSummary },
  { label: "Format",
    question: "How do you want to learn?",
    body: () => [ivChoices(MODES, "mode", "modes")],
    ok: (iv) => iv.mode ? null : "Pick a format",
    answer: (iv) => choiceName(MODES, iv.mode) },
  { label: "Level",
    question: "What's your level?",
    help: "Not sure? Choose Quiz me: a short quiz from simple to expert, and the course fills your gaps.",
    body: () => [ivChoices(LEVEL_CHOICES, "level", "levels")],
    ok: (iv) => iv.level ? null : "Pick a level, or Quiz me",
    answer: (iv) => choiceName(LEVEL_CHOICES, iv.level) },
  { label: "Language",
    question: "What language do you want the course to be written in?",
    help: "Pick from the list or type any other language. Leave it blank for English.",
    body: () => [ivText("input", "language", { list: "languages", placeholder: "English", maxlength: 40, autocomplete: "off" })],
    ok: () => null,
    answer: (iv) => iv.language.trim() || "English" },
];

function renderInterview() {
  const iv = state.iv;
  const step = STEPS[iv.step];
  const last = iv.step === STEPS.length - 1;
  const answers = STEPS.slice(0, iv.step).map((s, i) => el("div", { class: "iv-answer" },
    el("span", { class: "iv-a-label" }, s.label),
    el("span", { class: "iv-a-text" }, s.answer(iv)),
    iv.busy ? null : el("button", { class: "link", onclick: () => { iv.step = i; iv.error = null; renderInterview(); } }, "Change")));
  const next = el("button", { class: "primary", id: "iv-next", onclick: nextStep, disabled: iv.busy },
    iv.busy ? "Starting…" : !last ? "Next" : iv.level === "quiz" ? "Start the quiz" : "Build my course");
  $("interview").replaceChildren(...[
    answers.length ? el("div", { class: "iv-answers" }, ...answers) : null,
    el("div", { class: "iv-card" },
      el("div", { class: "iv-step" }, `Question ${iv.step + 1} of ${STEPS.length}`),
      el("h2", { class: "iv-q" }, step.question),
      step.help ? el("p", { class: "iv-help" }, step.help) : null,
      ...step.body(),
      el("div", { class: "iv-nav" },
        iv.step > 0 && !iv.busy ? el("button", { onclick: () => { iv.step--; iv.error = null; renderInterview(); } }, "Back") : null,
        step.skippable && !iv.busy ? el("button", { class: "link", onclick: () => { iv.description = ""; nextStep(); } }, "Skip") : null,
        next),
      iv.error ? el("div", { class: "error" }, iv.error) : null)].filter(Boolean));
  updateNext();
  const input = $("interview").querySelector(".iv-card .iv-input");
  if (input && document.activeElement?.tagName !== "TEXTAREA") input.focus();
}

// Next stays clickable for choice steps (it explains what's missing); text steps grey it out until filled.
function updateNext() {
  const btn = $("iv-next");
  if (btn && !state.iv.busy) btn.disabled = STEPS[state.iv.step].label === "Title" && !state.iv.topic.trim();
}

function nextStep() {
  const iv = state.iv;
  if (iv.busy) return;
  const problem = STEPS[iv.step].ok(iv);
  if (problem) { iv.error = problem; renderInterview(); return; }
  iv.error = null;
  if (iv.step < STEPS.length - 1) { iv.step++; renderInterview(); return; }
  startCourse();
}

async function startCourse() {
  const iv = state.iv;
  if (state.providers && !state.providers.can_build) {
    showSetup(true);
    return;
  }
  if (iv.level === "quiz") {
    startPlacement();
    return;
  }
  iv.busy = true;
  renderInterview();
  $("build-log").textContent = "";
  $("build-now").textContent = "Planning the course…";
  state.pendingBuild = true;
  const result = await window.api.createCourse(iv.topic.trim(), iv.level, iv.mode, null, sourceSites(iv), sourceFiles(iv),
    iv.language.trim(), iv.description.trim());
  state.pendingBuild = false;
  iv.busy = false;
  if (result.error) {
    $("build-now").textContent = "Build failed";
    if (state.view === "course") {
      await refreshCourse();
    } else {
      iv.error = result.error;
      renderInterview();
    }
    return;
  }
  state.iv = newInterview();
  renderInterview();
  if (state.data && state.data.folder === result.folder) await refreshCourse();
}

// --------------------------------------------------------- placement quiz

async function startPlacement() {
  stopPlayer();
  state.view = "placement";
  const iv = state.iv;
  const p = { topic: iv.topic.trim(), description: iv.description.trim(), mode: iv.mode, language: iv.language.trim(),
    sources: sourceSites(iv), files: sourceFiles(iv), quiz: null, index: 0, answers: [], level: null,
    error: null, building: false };
  state.placement = p;
  showSection("placement");
  renderPlacement();
  const result = await window.api.makePlacement(p.topic, p.language, p.description);
  if (state.placement !== p) return;                // went back while it was being written
  if (result.error) p.error = result.error;
  else p.quiz = result.quiz;
  renderPlacement();
}

function renderPlacement() {
  const p = state.placement;
  const box = $("p-content");
  if (!p) return;
  if (p.error) {
    box.replaceChildren(el("h1", {}, "The quiz couldn't be written"), el("div", { class: "error-box" }, p.error),
      el("button", { onclick: () => startPlacement() }, "Try again"));
    return;
  }
  if (!p.quiz) {
    box.replaceChildren(el("h1", {}, `Quiz: ${p.topic}`),
      el("p", { class: "muted" }, "Writing questions from simple to expert… this takes about half a minute."));
    return;
  }
  const questions = p.quiz.questions;
  box.replaceChildren(el("div", {}, p.index < questions.length ? placementQuestion(p, questions) : placementResults(p, questions)));
}

function placementQuestion(p, questions) {
  const item = questions[p.index];
  const answer = (given) => { p.answers[p.index] = given; p.index++; renderPlacement(); };
  return [
    el("h1", {}, `Quiz: ${p.topic}`),
    el("p", { class: "muted" }, "Questions start simple and get harder. Choose \"I don't know\" instead of guessing — " +
      "that's how the gaps are found. Answers are shown at the end."),
    el("div", { class: "p-meter" }, el("div", { style: `width:${(100 * p.index) / questions.length}%` })),
    el("div", { class: "quiz-card" },
      el("div", { class: "qnum" }, `Question ${p.index + 1} of ${questions.length}`),
      el("h3", {}, item.question),
      item.choices.map((choice, i) => el("button", { class: "answer", onclick: () => answer(i) }, choice)),
      el("button", { class: "answer idk", onclick: () => answer(null) }, "I don't know")),
    p.index ? el("button", { class: "link", onclick: () => { p.index--; renderPlacement(); } }, "← Previous question") : null,
  ];
}

// Pass a level with two thirds right; the course starts at the first level not passed.
function placementSummary(p, questions) {
  const graded = questions.map((q, i) => ({ ...q, given: p.answers[i] ?? null }));
  const levels = LEVELS.map(([key, name]) => {
    const qs = graded.filter((q) => q.level === key);
    const right = qs.filter((q) => q.given === q.answer_index).length;
    return { key, name, right, total: qs.length, passed: qs.length > 0 && right >= Math.ceil(qs.length * 2 / 3) };
  });
  const firstGap = levels.find((l) => l.total && !l.passed);
  return { graded, levels, suggested: firstGap ? firstGap.key : "expert" };
}

function placementResults(p, questions) {
  const { graded, levels, suggested } = placementSummary(p, questions);
  if (!p.level) p.level = suggested;
  const right = graded.filter((q) => q.given === q.answer_index);
  const missed = graded.filter((q) => q.given !== q.answer_index);
  const levelButtons = LEVELS.map(([key, name]) => el("button", {
    class: "choice" + (p.level === key ? " selected" : ""), onclick: () => { p.level = key; renderPlacement(); },
  }, key === suggested ? el("div", { class: "tag" }, "suggested") : null, el("b", {}, name)));
  const mode = MODES.find(([key]) => key === p.mode);
  return [
    el("h1", {}, `Your results: ${right.length} of ${graded.length}`),
    el("p", { class: "muted" }, missed.length
      ? `Found ${missed.length} gap${missed.length === 1 ? "" : "s"}. Your course will focus on ${missed.length === 1 ? "it" : "them"} and skip what you already know.`
      : "No gaps found — you know this well. The course will go deeper than the quiz did."),
    levels.map((l) => el("div", { class: "level-row" },
      el("div", {}, l.passed ? "✓ " : "", l.name),
      el("div", { class: "bar" }, el("div", { class: l.passed ? "" : "miss", style: `width:${l.total ? (100 * l.right) / l.total : 0}%` })),
      el("div", { class: "n" }, `${l.right}/${l.total}`))),
    missed.length ? [el("div", { class: "choice-label" }, "Gaps to fill"),
      el("div", { class: "concepts gaps" }, missed.map((q) => el("span", {}, q.concept)))] : null,
    right.length ? [el("div", { class: "choice-label" }, "You already know"),
      el("div", { class: "concepts" }, right.map((q) => el("span", {}, q.concept)))] : null,
    missed.length ? el("details", { class: "review" }, el("summary", {}, "See the questions you missed, with answers"),
      missed.map((q) => el("div", { class: "item" },
        el("div", { class: "q" }, q.question),
        el("div", { class: "muted" }, q.given == null ? "You didn't know." : `You chose: ${q.choices[q.given]}`),
        el("div", {}, "Answer: ", el("b", {}, q.choices[q.answer_index])),
        el("div", { class: "muted" }, q.explanation)))) : null,
    el("div", { class: "choice-label" }, "Start the course at"),
    el("div", { class: "choices small" }, levelButtons),
    el("div", { class: "start-row" },
      el("button", { class: "primary", disabled: p.building, onclick: () => buildFromPlacement(p, graded) },
        p.building ? "Starting…" : "Build my course"),
      el("span", { class: "muted" }, mode ? mode[1] : ""),
      el("button", { class: "link", onclick: () => { p.index = 0; p.answers = []; p.level = null; renderPlacement(); } }, "Retake quiz")),
    p.buildError ? el("div", { class: "error-box" }, p.buildError) : null,
  ];
}

async function buildFromPlacement(p, graded) {
  p.building = true;
  p.buildError = null;
  renderPlacement();
  $("build-log").textContent = "";
  $("build-now").textContent = "Planning a course around your gaps…";
  state.pendingBuild = true;
  // The course opens on its own as soon as the build reports its folder.
  const result = await window.api.createCourse(p.topic, p.level, p.mode, { questions: graded }, p.sources, p.files, p.language, p.description);
  state.pendingBuild = false;
  p.building = false;
  if (result.error) {
    if (state.view === "course") await refreshCourse();
    else if (state.placement === p) { p.buildError = result.error; renderPlacement(); }
    return;
  }
  if (state.data && state.data.folder === result.folder) await refreshCourse();
}

// ------------------------------------------------------------- build events

window.api.onBuildEvent(async (event) => {
  const log = $("build-log");
  const line = event.kind === "LOG" ? event.data
    : event.kind === "ARTIFACT" ? `Saved ${event.data.path}`
    : typeof event.data === "string" ? event.data : null;
  if (line) {
    log.textContent += line + "\n";
    log.scrollTop = log.scrollHeight;
  }
  if (event.kind === "STATUS") $("build-now").textContent = event.data;
  if (event.kind === "COURSE") {
    // A retry keeps the reader where they are; a new course opens at its outline.
    if (state.data && state.data.folder === event.data.folder) await refreshCourse();
    else await openCourse(event.data.folder);
  }
  if (event.kind === "PLAN" || event.kind === "ARTIFACT") {
    if (state.data && state.data.building) await refreshCourse();
  }
  if (event.kind === "DONE") $("build-now").textContent = "Your course is ready.";
  if (event.kind === "FAILED") $("build-now").textContent = "Build failed: " + event.data;
});

// ----------------------------------------------------------------- course

async function openCourse(folder) {
  const data = await window.api.loadCourse(folder);
  if (data.error) {
    alertBox(data.error);
    return;
  }
  state.view = "course";
  state.data = data;
  state.day = null;
  state.tab = null;
  state.placement = null;
  showSection("course");
  renderCourse();
}

async function refreshCourse() {
  if (!state.data) return;
  const data = await window.api.loadCourse(state.data.folder);
  if (data.error) return;
  state.data = data;
  renderCourse(true);
}

function alertBox(message) {
  state.iv.error = message;
  renderInterview();
}

function dayProgress(n) {
  return (state.data.progress.days || {})[String(n)] || {};
}

function dayReady(day, files) {
  const c = state.data.course;
  if (!files || !day.lesson) return false;
  if (c.mode !== "docs" && !day.videos && !day.storyboard) return false;
  if (c.mode === "mix" && !day.quiz) return false;
  return true;
}

// Failed, interrupted, or missing pieces that a resume build would fill in.
function needsRetry() {
  const { course } = state.data;
  if (course.status !== "ready" || !course.days.length || course.narration_pending) return true;
  // Days watching a walkthrough still count: real videos can be fetched later.
  return course.days.some((d) => d.errors.length || !dayReady(d, true) ||
    (course.mode !== "docs" && (!d.videos || !d.storyboard)));
}

async function retryCourse() {
  const folder = state.data.folder;
  $("retry").classList.add("hidden");
  $("build-log").textContent = "";
  $("build-now").textContent = "Retrying failed parts…";
  $("build-panel").classList.remove("hidden");
  const result = await window.api.resumeCourse(folder);
  if (state.data && state.data.folder === folder) await refreshCourse();
  if (result.error) {
    $("build-now").textContent = "Retry failed: " + result.error;
    $("build-panel").classList.remove("hidden");
  }
}

// ------------------------------------------------------- PDF and email

function pdfBox(folder, ...children) {
  const box = $("pdf-box");
  box.dataset.folder = folder;
  box.replaceChildren(...children.flat(Infinity).filter(Boolean));
  box.classList.remove("hidden");
}

async function makePdf() {
  const folder = state.data.folder;
  const btn = $("make-pdf");
  btn.disabled = true;
  btn.textContent = "Making the PDF…";
  pdfBox(folder, "Putting every day into one PDF…");
  const result = await window.api.makePdf(folder);
  btn.disabled = false;
  btn.textContent = "Make the course into a PDF";
  if (result.error) return pdfBox(folder, el("div", { class: "error" }, result.error));
  askEmail(folder, `PDF saved (${(result.bytes / 1048576).toFixed(1)} MB).`, await window.api.lastEmail());
}

// `address` starts as the most recent recipient; after a failed send it keeps what was typed.
function askEmail(folder, note, address, error) {
  const input = el("input", { type: "email", placeholder: "you@example.com", value: address || "" });
  const send = async () => {
    const to = input.value.trim();
    pdfBox(folder, `Sending to ${to}…`);
    const result = await window.api.emailPdf(folder, to);
    if (result.error) return askEmail(folder, note, to, result.error);
    if (result.manual) {
      return pdfBox(folder, window.api.remote
        ? ["Email sending isn't set up on the server, so a new message opened in your mail program. ",
          el("button", { class: "link", onclick: () => window.api.openPdf(folder) }, "Open the PDF"),
          ", download it, and attach it to the message."]
        : "Email sending isn't set up, so a new message opened in your mail program " +
          "and the PDF's folder opened next to it. Drag the PDF into the message to attach it.");
    }
    pdfBox(folder, `Emailed to ${result.sent}.`, el("div", { class: "row" },
      el("button", { class: "link", onclick: () => window.api.openPdf(folder) }, "Open the PDF")));
  };
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") send(); });
  pdfBox(folder, el("div", {}, note, " ",
    el("button", { class: "link", onclick: () => window.api.openPdf(folder) }, "Open it")),
    el("div", { style: "margin-top:6px" }, el("b", {}, "Should it be emailed?")),
    input,
    error ? el("div", { class: "error" }, error) : null,
    el("div", { class: "row" },
      el("button", { class: "primary", onclick: send }, "Yes, email it"),
      el("button", { onclick: () => $("pdf-box").classList.add("hidden") }, "No thanks")));
  input.focus();
}

function renderCourse(keepContent = false) {
  const { course, building } = state.data;
  const total = course.days.length;
  const done = course.days.filter((d) => dayProgress(d.n).complete).length;
  $("c-title").textContent = course.title;
  $("c-sub").textContent = [course.level_name, course.mode_name,
    course.language && course.language !== "English" && course.language,
    typeof course.cost_usd === "number" && `cost $${course.cost_usd.toFixed(2)}`].filter(Boolean).join(" · ");
  $("c-bar").style.width = total ? `${(100 * done) / total}%` : "0";
  $("c-count").textContent = total ? `${done}/${total} days` : "";
  $("build-panel").classList.toggle("hidden", !building);
  $("retry").classList.toggle("hidden", building || !needsRetry());
  $("make-pdf").disabled = building || !course.days.length;
  if ($("pdf-box").dataset.folder !== state.data.folder) $("pdf-box").classList.add("hidden");

  const items = [el("button", {
    class: "nav-item" + (state.day === null ? " active" : ""), onclick: () => selectDay(null),
  }, el("span", { class: "mark" }, "≡"), el("span", { class: "label" }, "Course outline"))];
  course.days.forEach((d, i) => {
    const files = state.data.days[i];
    const complete = dayProgress(d.n).complete;
    const ready = dayReady(d, files);
    const pending = !ready && building && !d.errors.length;
    const mark = complete ? el("span", { class: "mark done" }, "✓")
      : d.errors.length ? el("span", { class: "mark warn", title: d.errors.join("\n") }, "!")
      : el("span", { class: "mark" + (pending ? " wait" : "") });
    items.push(el("button", {
      class: "nav-item" + (state.day === d.n ? " active" : ""), onclick: () => selectDay(d.n),
    }, mark, el("span", { class: "label" }, el("small", {}, `Day ${d.n}`), d.title)));
  });
  $("nav").replaceChildren(...items);

  // During a build, only redraw the page when what it shows has changed, so a
  // playing video or the reader's scroll position isn't reset.
  const key = contentKey();
  if (!keepContent || key !== state.contentKey) renderContent();
}

function contentKey() {
  const { course, days, outline } = state.data;
  if (state.day === null) return JSON.stringify([null, outline, course.error, course.status]);
  const i = course.days.findIndex((d) => d.n === state.day);
  return JSON.stringify([state.day, days[i], course.days[i] && course.days[i].errors]);
}

function selectDay(n) {
  state.day = n;
  state.tab = null;
  state.quiz = null;
  renderCourse();
  $("main").scrollTop = 0;
  if (window.api.remote) window.scrollTo(0, 0);
}

function renderContent() {
  const { course } = state.data;
  const content = $("content");
  state.contentKey = contentKey();
  stopPlayer();
  if (course.status === "failed" && !course.days.length) {
    content.replaceChildren(el("h1", {}, course.title),
      el("div", { class: "error-box" }, "The course could not be planned:\n" + (course.error || "unknown error")));
    return;
  }
  if (state.day === null) {
    const box = [];
    if (course.error) box.push(el("div", { class: "error-box" }, course.error));
    content.replaceChildren(...box, state.data.outline ? markdown(state.data.outline)
      : el("div", { class: "empty" }, "Planning the course…"),
      window.api.remote ? null
        : el("p", {}, el("button", { class: "link", onclick: () => window.api.openFolder(state.data.folder) }, "Open course folder")));
    return;
  }
  const index = course.days.findIndex((d) => d.n === state.day);
  const day = course.days[index];
  const files = state.data.days[index] || {};
  const complete = !!dayProgress(day.n).complete;

  const tabs = [];
  if (files.lesson) tabs.push(["lesson", course.mode === "videos" ? "Intro" : "Lesson"]);
  if (course.mode !== "docs") tabs.push(["videos", "Videos"]);
  if (course.mode === "mix") tabs.push(["quiz", "Quiz"]);
  if (!tabs.length) tabs.push(["lesson", "Lesson"]);
  if (!tabs.some(([t]) => t === state.tab)) state.tab = tabs[0][0];

  const head = el("div", { class: "day-head" },
    el("div", {}, el("div", { class: "kicker" }, `Day ${day.n} of ${course.days.length}`), el("h1", {}, day.title),
      el("div", { class: "muted" }, day.summary)),
    el("button", { class: "complete-btn" + (complete ? " done" : ""), onclick: () => setComplete(day.n, !dayProgress(day.n).complete) },
      complete ? "✓ Completed" : "Mark complete"));

  const errors = day.errors.length ? el("div", { class: "error-box" }, "Some parts of this day failed to build:\n" + day.errors.join("\n")) : null;
  const tabBar = tabs.length > 1 ? el("div", { class: "tabs" }, tabs.map(([t, label]) =>
    el("button", { class: t === state.tab ? "active" : "", onclick: () => { state.tab = t; renderContent(); } }, label))) : null;

  let body;
  if (state.tab === "lesson") body = files.lesson ? markdown(files.lesson) : waiting("lesson");
  else if (state.tab === "videos") body = renderVideos(day, files);
  else body = renderQuiz(day, files.quiz);

  const footer = el("p", { style: "margin-top:40px" },
    el("button", { class: "complete-btn" + (complete ? " done" : ""), onclick: () => setComplete(day.n, !dayProgress(day.n).complete) },
      complete ? "✓ Completed — undo" : "Mark day complete"));
  content.replaceChildren(...[head, errors, tabBar, body, footer].filter(Boolean));
}

function waiting(what) {
  return el("div", { class: "empty" }, state.data.building ? `The ${what} is still being written…` : `No ${what} for this day.`);
}

function renderVideos(day, files) {
  const videos = files.videos || [];
  const parts = [];
  if (day.video_note) {
    parts.push(el("div", { class: "note-box" }, day.video_note,
      files.storyboard ? " Meanwhile, here is an animated walkthrough." : ""));
  }
  if (videos.length) parts.push(videoList(videos));
  if (files.videoCheck) parts.push(rejectedVideos(files.videoCheck));
  if (files.storyboard) parts.push(storyboardPlayer(files.storyboard, videos.length > 0));
  if (!parts.length || (!videos.length && !files.storyboard)) {
    parts.push(files.videos ? el("div", { class: "empty" }, "No suitable videos were found for this day.")
      : waiting("video list"));
  }
  return el("div", {}, parts);
}

// ------------------------------------------------------- walkthrough player

let player = null; // the one storyboard player on screen (stopped on redraw)

function stopPlayer() {
  if (player) player.stop();
  player = null;
}

function storyboardPlayer(board, afterVideos) {
  stopPlayer();
  const scenes = board.scenes;
  let index = 0;
  let playing = false;
  let timer = null;
  const img = el("img", { class: "scene-img", alt: "" });
  const heading = el("h3", {});
  const narration = el("p", { class: "narration" });
  const counter = el("span", { class: "muted" });
  const playBtn = el("button", { class: "primary" });
  const dots = el("div", { class: "dots" });
  const voice = el("label", { class: "muted voice" });
  const voiceBox = el("input", { type: "checkbox" });
  voiceBox.checked = true;
  voice.append(voiceBox, " Narrate aloud");
  voiceBox.addEventListener("change", () => { if (!voiceBox.checked) { audio.pause(); speechSynthesis.cancel(); } });

  // Recorded narration (Kokoro) when the build made it; the browser voice otherwise.
  const audio = new Audio();
  function silence() {
    clearTimeout(timer);
    audio.onended = audio.onerror = null;
    audio.pause();
    speechSynthesis.cancel();
  }

  function speak(scene, done) {
    silence();
    const text = scene.narration;
    const fallback = () => { timer = setTimeout(done, Math.max(4000, text.split(/\s+/).length * 400)); };
    if (!voiceBox.checked) return fallback();
    if (scene.audio) {
      audio.src = scene.audio;
      audio.onended = () => { timer = setTimeout(done, 500); };
      audio.onerror = fallback;
      audio.play().catch(fallback);
      return;
    }
    if (!window.speechSynthesis) return fallback();
    const u = new SpeechSynthesisUtterance(text);
    u.rate = 1;
    u.onend = () => { timer = setTimeout(done, 700); };
    u.onerror = fallback;
    speechSynthesis.speak(u);
  }

  function show(i) {
    index = Math.max(0, Math.min(scenes.length - 1, i));
    const s = scenes[index];
    img.classList.remove("shown");
    img.src = s.image;
    requestAnimationFrame(() => requestAnimationFrame(() => img.classList.add("shown")));
    heading.textContent = s.heading;
    narration.textContent = s.narration;
    counter.textContent = `Scene ${index + 1} of ${scenes.length}`;
    dots.replaceChildren(...scenes.map((_, j) => el("button", {
      class: "dot" + (j === index ? " on" : ""), title: `Scene ${j + 1}`, onclick: () => jump(j),
    })));
  }

  let ended = false;

  function narrate() {
    speak(scenes[index], () => {
      if (!playing) return;
      if (index + 1 < scenes.length) {
        show(index + 1);
        narrate();
      } else {
        ended = true;
        setPlaying(false);
      }
    });
  }

  function setPlaying(on) {
    playing = on;
    if (on) {
      if (ended) show(0);
      ended = false;
      narrate();
    } else {
      silence();
    }
    playBtn.textContent = on ? "❚❚ Pause" : ended ? "↺ Replay" : "▶ Play";
  }

  const jump = (i) => {
    ended = false;
    show(i);
    if (playing) narrate();
  };
  playBtn.addEventListener("click", () => setPlaying(!playing));
  const prev = el("button", { title: "Previous scene", onclick: () => jump(index - 1) }, "◀");
  const next = el("button", { title: "Next scene", onclick: () => jump(index + 1) }, "▶");
  player = { stop: () => { playing = false; silence(); } };
  show(0);
  setPlaying(false);

  return el("div", { class: "walkthrough" },
    el("div", { class: "kicker" }, afterVideos ? "Also: animated walkthrough" : "Animated walkthrough"),
    board.title ? el("h2", {}, board.title) : null,
    el("div", { class: "screen" }, img),
    el("div", { class: "controls" }, prev, playBtn, next, counter, voice),
    dots, heading, narration);
}

// The judge picks the parts of each video worth the learner's time (up to 3 clips).
// Each clip gets its own player, which starts and stops at that part.
function embedUrl(id, start, end) {
  const params = new URLSearchParams({ rel: "0" });
  if (Number.isInteger(start) && start > 0) params.set("start", String(start));
  if (Number.isInteger(end) && end > (start || 0)) params.set("end", String(end));
  return `https://www.youtube-nocookie.com/embed/${encodeURIComponent(id)}?${params}`;
}

function clock(seconds) {
  const h = Math.floor(seconds / 3600), m = Math.floor((seconds % 3600) / 60), s = seconds % 60;
  const ss = String(s).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${ss}` : `${m}:${ss}`;
}

// The clips to show: the judge's, or (older courses) one part from start to end, or the whole video.
function clipsOf(v) {
  return Array.isArray(v.clips) && v.clips.length ? v.clips : [{ start: v.start, end: v.end, label: "" }];
}

function videoList(videos) {
  return el("div", {}, videos.map((v) => {
    const clips = clipsOf(v);
    const cut = Array.isArray(v.clips) && v.clips.length > 0;
    const players = clips.map((c, i) => el("div", { class: "clip" },
      cut ? el("div", { class: "clip-label" }, [clips.length > 1 && `Part ${i + 1} of ${clips.length}`,
        `${clock(c.start)}–${clock(c.end)}`, c.label].filter(Boolean).join(" · ")) : null,
      el("div", { class: "frame" }, el("iframe", {
        src: embedUrl(v.id, c.start, c.end),
        title: c.label ? `${v.title}: ${c.label}` : v.title,
        allow: "encrypted-media; picture-in-picture; fullscreen", allowfullscreen: true,
        referrerpolicy: "strict-origin-when-cross-origin",
      }))));
    const length = cut && Number.isInteger(v.watch_seconds)
      ? `watch ${clock(v.watch_seconds)}${v.duration ? ` of ${v.duration}` : ""}` : v.duration;
    const head = [
      el("h3", {}, v.title),
      el("div", { class: "muted" }, [v.channel, length, v.views && `${v.views} views`,
        Number.isInteger(v.score) && `${v.checked === "transcript" ? "transcript" : "lesson"} match ${v.score}/10`,
        !cut && Number.isInteger(v.start) && v.start > 0 && `starts at ${clock(v.start)}`].filter(Boolean).join(" · "),
        " · ", el("button", { class: "link", onclick: () => window.api.openUrl(v.url) }, "Open in YouTube")),
      v.why ? el("p", { class: "why" }, v.why) : null,
    ];
    const note = v.concerns ? el("p", { class: "muted" }, "Note: ", v.concerns) : null;
    // With several clips the title comes first, so it's clear the parts belong together.
    return el("div", { class: "video" }, ...(clips.length > 1 ? [head, players, note] : [players, head, note]));
  }));
}

// Every candidate the web search turned up that didn't make the cut, and why.
function rejectedVideos(check) {
  const others = (check.candidates || []).filter((c) => c.result !== "kept");
  if (!others.length) return null;
  return el("details", { class: "checked" },
    el("summary", {}, `${others.length} other video${others.length === 1 ? "" : "s"} checked and not used`),
    el("ul", {}, others.map((c) => el("li", {},
      el("button", { class: "link", onclick: () => window.api.openUrl(c.url) }, c.title || c.id),
      " ", el("span", { class: "muted" }, [c.channel, c.duration, Number.isInteger(c.score) && `${c.score}/10`,
        c.result.replace(/^rejected: /, "")].filter(Boolean).join(" · ")),
      c.concerns ? el("div", { class: "muted" }, c.concerns) : null))));
}

// ------------------------------------------------------------------- quiz

function renderQuiz(day, quiz) {
  if (!quiz) return waiting("quiz");
  if (!state.quiz || state.quiz.day !== day.n) state.quiz = { day: day.n, index: 0, score: 0, picked: null, finished: false };
  const q = state.quiz;
  const total = quiz.questions.length;
  const previous = dayProgress(day.n).quiz;

  if (q.finished) {
    const passed = q.score >= quiz.pass;
    return el("div", { class: "quiz-card" },
      el("div", { class: "qnum" }, "Quiz result"),
      el("div", { class: "score" }, `${q.score} / ${total}`),
      el("p", {}, passed ? "Passed! This day is marked complete." : `You need ${quiz.pass} to pass. Review the lesson and try again.`),
      el("button", { onclick: () => { state.quiz = null; renderContent(); } }, "Retake quiz"));
  }

  const item = quiz.questions[q.index];
  const answered = q.picked !== null;
  const answers = item.choices.map((choice, i) => {
    let cls = "answer";
    if (answered && i === item.answer_index) cls += " right";
    else if (answered && i === q.picked) cls += " wrong";
    return el("button", {
      class: cls, disabled: answered,
      onclick: () => { q.picked = i; if (i === item.answer_index) q.score++; renderContent(); },
    }, choice);
  });
  const next = answered ? el("button", { class: "primary", onclick: () => nextQuestion(day, quiz) },
    q.index + 1 < total ? "Next question" : "See result") : null;
  return el("div", {},
    previous ? el("p", { class: "muted" }, `Last score: ${previous.score}/${previous.total}`) : null,
    el("div", { class: "quiz-card" },
      el("div", { class: "qnum" }, `Question ${q.index + 1} of ${total}`),
      el("h3", {}, item.question),
      answers,
      answered ? el("p", { class: "explain" },
        el("b", {}, q.picked === item.answer_index ? "Correct. " : "Not quite. "), item.explanation) : null,
      next));
}

async function nextQuestion(day, quiz) {
  const q = state.quiz;
  q.picked = null;
  if (q.index + 1 < quiz.questions.length) {
    q.index++;
    renderContent();
    return;
  }
  q.finished = true;
  const passed = q.score >= quiz.pass;
  const result = await window.api.setProgress(state.data.folder, day.n, {
    quizScore: q.score, quizTotal: quiz.questions.length, complete: passed ? true : undefined,
  });
  if (result.progress) state.data.progress = result.progress;
  renderCourse();
}

async function setComplete(n, complete) {
  const result = await window.api.setProgress(state.data.folder, n, { complete });
  if (result.error) {
    alertBox(result.error);
    return;
  }
  state.data.progress = result.progress;
  renderCourse(true);
  // Update the complete buttons in place; redrawing would restart any playing video.
  document.querySelectorAll(".complete-btn").forEach((btn, i) => {
    btn.classList.toggle("done", complete);
    btn.textContent = complete ? (i ? "✓ Completed — undo" : "✓ Completed") : (i ? "Mark day complete" : "Mark complete");
  });
}

// ------------------------------------------------------------------- AI setup

const GEMINI_KEY_URL = "https://aistudio.google.com/apikey";
const SETUP_DISMISSED = "teachme.geminiTipDismissed";

function remembered(key) {
  try { return localStorage.getItem(key); } catch { return null; }
}

function remember(key, value) {
  try { localStorage.setItem(key, value); } catch {}
}

async function checkProviders() {
  const result = await window.api.providerStatus();
  if (result.error) return null;
  state.providers = result.status;
  return state.providers;
}

// The popup: shown at start when a task has no usable model or a key is missing
// (until dismissed for that same list of problems), always when no course can be
// built, and from the "AI setup" link.
async function showSetup(force = false) {
  const s = state.providers || await checkProviders();
  if (!s) return;
  const needsAi = !s.can_build;
  const problems = s.problems || [];
  const seen = JSON.stringify(problems);
  if (!force && !needsAi && (!problems.length || remembered(SETUP_DISMISSED) === seen)) return;
  const ollama = s.ollama || {};
  let title;
  let intro;
  if (needsAi) {
    title = "Teach Me needs an AI model";
    intro = "No AI key was found and no local model is running. The quickest free option is a Google Gemini key:";
  } else if (s.local_only) {
    title = "You're using a free local model";
    intro = `Courses are written by ${ollama.model} on this computer: free, but slower and simpler. ` +
      "A free Google Gemini key gives better lessons and lets Teach Me watch videos to pick good ones:";
  } else if (!(s.tasks?.video_judge || []).some((r) => r.ok)) {
    title = "Better videos with Gemini";
    intro = "Gemini can watch each YouTube video, check it against the lesson, and open it at the most useful " +
      "part. It works through OpenRouter or with a free Gemini key. Without either, videos are chosen by title only.";
  } else {
    title = "AI setup";
    intro = problems.length ? "Every course can be built. Some extras are missing:" : "Every task has a working model.";
  }

  const input = el("input", { type: "password", placeholder: "Paste your key (starts with AIza)", autocomplete: "off" });
  const msg = el("div", { class: "fine" });
  const save = el("button", { class: "primary", onclick: async () => {
    save.disabled = true;
    const result = await window.api.saveGeminiKey(input.value);
    save.disabled = false;
    if (result.error) {
      msg.className = "error";
      msg.textContent = result.error;
      return;
    }
    input.value = "";
    state.providers = null;
    await checkProviders();
    msg.className = "ok";
    msg.textContent = "Saved to .env. Gemini is ready; new courses will use it.";
    setTimeout(() => showSetup(true), 1200);
  } }, "Save key");

  const videoOk = (s.tasks?.video_judge || []).some((r) => r.ok);
  const steps = s.keys.gemini || videoOk ? null : el("ol", {},
    el("li", {}, "Open ", el("button", { class: "link", onclick: () => window.api.openUrl(GEMINI_KEY_URL) },
      "Google AI Studio"), " and sign in with any Google account."),
    el("li", {}, "Click ", el("b", {}, "Create API key"), ". It's free and needs no credit card. ",
      el("span", { class: "fine" }, "If it asks you to choose a project and none is listed, cancel, open the ",
        el("b", {}, "Projects"), " page in AI Studio, create a project, then create the key in it.")),
    el("li", {}, "Copy the key and paste it here:", el("div", { class: "key-row" }, input, save), msg));

  const local = needsAi ? el("div", {},
    el("p", {}, el("b", {}, "Or run a free model on this computer"), " (no account; needs a reasonably modern PC):"),
    el("ol", {},
      el("li", {}, "Install ", el("button", { class: "link", onclick: () => window.api.openUrl("https://ollama.com/download") },
        "Ollama"), "."),
      el("li", {}, "In a terminal, run ", el("code", {}, "ollama pull qwen3:8b"), " (about 5 GB)."),
      el("li", {}, "Come back here. Teach Me finds it automatically.")),
    ollama.running && !ollama.models.length
      ? el("p", { class: "fine" }, "Ollama is running but has no models yet: run the pull command above.") : null) : null;

  const yes = (v) => (v ? "✓" : "✗");
  const found = el("div", { class: "found" },
    `Found: Anthropic ${yes(s.keys.anthropic)} · OpenAI ${yes(s.keys.openai)} · OpenRouter ${yes(s.keys.openrouter)} · ` +
    `Gemini ${yes(s.keys.gemini)} · YouTube ${yes(s.keys.youtube)} · Local model ${ollama.model || "✗"}`,
    problems.length ? el("div", { class: "error" }, el("b", {}, "Missing:"),
      el("ul", {}, problems.map((line) => el("li", {}, line)))) : el("div", {}, "Every task has a usable model."),
    el("details", {}, el("summary", {}, "Models per task (tried in order, set in config.yaml)"),
      el("ul", {}, Object.entries(s.tasks || {}).map(([task, rows]) => el("li", {}, el("b", {}, task), ": ",
        rows.length ? rows.map((r, i) => el("span", { class: r.ok ? "" : "muted", title: r.problem || "" },
          i ? " → " : "", r.ok ? "✓ " : "✗ ", r.model)) : "none listed")))),
    (s.ignored_env_models || []).length ? el("div", { class: "fine" },
      `${s.ignored_env_models.join(", ")} in .env is ignored now; models are chosen per task in config.yaml.`) : null,
    el("div", {}, "Keys are stored only in the .env file on this computer. Other keys (Anthropic, OpenAI, ",
      "OpenRouter, YouTube) go there too: ",
      el("button", { class: "link", onclick: () => window.api.openEnv() }, "open .env to fill them in"),
      ", then reopen AI setup."));

  const close = () => $("setup").classList.add("hidden");
  const actions = el("div", { class: "actions" },
    !needsAi && problems.length ? el("button", { onclick: () => { remember(SETUP_DISMISSED, seen); close(); } },
      "Don't show again") : null,
    el("button", { onclick: close }, needsAi ? "Close" : problems.length ? "Not now" : "Done"));

  $("setup-box").replaceChildren(...[el("h2", {}, title), el("p", {}, intro), steps,
    steps ? el("p", { class: "fine" }, "The free tier has daily limits. Google may use free-tier requests to " +
      "improve its products, so keep personal details out of your topics.") : null,
    local, found, actions].filter(Boolean));
  $("setup").classList.remove("hidden");
  if (steps) input.focus();
}

// ------------------------------------------------------------------- init

renderInterview();
$("p-back").addEventListener("click", showHome);
$("back").addEventListener("click", showHome);
$("retry").addEventListener("click", retryCourse);
$("make-pdf").addEventListener("click", makePdf);
$("ai-setup").addEventListener("click", async () => { state.providers = null; await checkProviders(); showSetup(true); });
$("setup").addEventListener("click", (e) => { if (e.target === $("setup")) $("setup").classList.add("hidden"); });
showHome();
checkProviders().then(() => showSetup(false));
