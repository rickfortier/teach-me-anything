// Turns a loaded course (see loadCourse in main.js) into one printable HTML page.
const { marked } = require("marked");

const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

// Lesson text comes from the LLM; the print window runs no scripts and its CSP
// allows only inline styles and data: images, so raw HTML in it can't do anything.
const md = (text) => marked.parse(text || "");

const LETTERS = "ABCDEFGHIJ";

const clock = (s) => `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;

// The parts worth watching, each linked to the moment it starts.
function clipsHtml(v) {
  if (!Array.isArray(v.clips) || !v.clips.length) return "";
  const base = `https://www.youtube.com/watch?v=${encodeURIComponent(v.id)}`;
  return `<ul class="clips">${v.clips.map((c) => `<li><a href="${esc(`${base}&t=${c.start}s`)}">${clock(c.start)}–${
    clock(c.end)}</a>${c.label ? ` ${esc(c.label)}` : ""}</li>`).join("")}</ul>`;
}

function videosHtml(videos) {
  if (!videos || !videos.length) return "";
  return `<h3>Videos</h3><ul class="videos">${videos.map((v) => `<li>
    <a href="${esc(v.url)}">${esc(v.title)}</a>
    <span class="muted">${esc([v.channel, v.duration].filter(Boolean).join(" · "))}</span>
    ${v.why ? `<div>${esc(v.why)}</div>` : ""}${clipsHtml(v)}</li>`).join("")}</ul>`;
}

function storyboardHtml(board) {
  if (!board) return "";
  return `<h3>Walkthrough${board.title ? `: ${esc(board.title)}` : ""}</h3>${board.scenes.map((s, i) => `
    <div class="scene"><img src="${s.image}" alt="">
      <div><b>${i + 1}. ${esc(s.heading)}</b><p>${esc(s.narration)}</p></div></div>`).join("")}`;
}

function quizHtml(quiz) {
  if (!quiz || !quiz.questions || !quiz.questions.length) return "";
  const questions = quiz.questions.map((q, i) => `<li><p>${esc(q.question)}</p><ol class="choices">${
    q.choices.map((c) => `<li>${esc(c)}</li>`).join("")}</ol></li>`).join("");
  const answers = quiz.questions.map((q, i) =>
    `<li><b>${LETTERS[q.answer_index] || q.answer_index + 1}.</b> ${esc(q.explanation)}</li>`).join("");
  return `<div class="quiz"><h3>Quiz</h3><ol>${questions}</ol>
    <div class="answers"><b>Answers</b><ol>${answers}</ol></div></div>`;
}

function courseHtml(data) {
  const { course } = data;
  const days = course.days.map((d, i) => {
    const files = data.days[i] || {};
    return `<section class="day">
      <div class="kicker">Day ${d.n} of ${course.days.length}</div>
      <h1>${esc(d.title)}</h1>
      ${d.summary ? `<p class="muted">${esc(d.summary)}</p>` : ""}
      ${files.lesson ? `<div class="md">${md(files.lesson)}</div>` : ""}
      ${videosHtml(files.videos)}
      ${storyboardHtml(files.storyboard)}
      ${quizHtml(files.quiz)}
    </section>`;
  }).join("");

  return `<!doctype html><html><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src data:; style-src 'unsafe-inline'">
<title>${esc(course.title)}</title>
<style>
  body { font: 11pt/1.5 "Segoe UI", Arial, sans-serif; color: #1a1a1a; margin: 0; }
  h1 { font-size: 20pt; margin: 4px 0 6px; } h2 { font-size: 15pt; } h3 { font-size: 13pt; margin-top: 22px; }
  a { color: #1d4ed8; } .muted { color: #666; font-size: 10pt; }
  .kicker { color: #2563eb; font-size: 9pt; text-transform: uppercase; letter-spacing: .08em; }
  .cover { page-break-after: always; padding-top: 30vh; }
  .cover h1 { font-size: 30pt; }
  .day { page-break-before: always; }
  img { max-width: 100%; }
  .md img { display: block; margin: 10px auto; max-height: 340px; }
  pre, code { background: #f3f4f6; border-radius: 4px; font-size: 9.5pt; }
  pre { padding: 8px; white-space: pre-wrap; }
  table { border-collapse: collapse; } td, th { border: 1px solid #ccc; padding: 4px 8px; }
  .videos li { margin-bottom: 8px; }
  .scene { display: flex; gap: 14px; align-items: flex-start; margin: 10px 0; page-break-inside: avoid; }
  .scene img { width: 46%; flex: none; border: 1px solid #ddd; border-radius: 6px; }
  .scene p { margin: 4px 0; }
  .quiz > ol > li { page-break-inside: avoid; margin-bottom: 8px; } .quiz p { margin: 0 0 2px; }
  .choices { list-style: upper-alpha; }
  .answers { margin-top: 14px; padding: 8px 12px; background: #f6f7f9; border-radius: 6px; font-size: 10pt;
             page-break-inside: avoid; }
</style></head><body>
<section class="cover">
  <div class="kicker">Teach Me</div>
  <h1>${esc(course.title)}</h1>
  <p class="muted">${esc([course.level_name, course.mode_name, `${course.days.length} days`].filter(Boolean).join(" · "))}</p>
</section>
${data.outline ? `<section class="md">${md(data.outline)}</section>` : ""}
${days}
</body></html>`;
}

module.exports = { courseHtml };
