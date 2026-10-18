const { app, BrowserWindow, dialog, shell, ipcMain, session } = require("electron");
const path = require("path");
const fs = require("fs");
const os = require("os");
const { spawn } = require("child_process");
const { lineReader, createQueue, buildEvent } = require("./jobs");
const { courseHtml } = require("./pdf");
const { createWebServer } = require("./web");

// `electron . --server`: the web site only, no window.
const HEADLESS = process.argv.includes("--server");

const ROOT = path.join(__dirname, "..");
const COURSES = path.join(ROOT, "courses");
const PYTHON = process.platform === "win32"
  ? path.join(ROOT, ".venv", "Scripts", "python.exe")
  : path.join(ROOT, ".venv", "bin", "python");
const TEACHME = path.join(ROOT, "teachme.py");
const LEVELS = ["pure_beginner", "beyond_basics", "good_to_great", "expert"];
const MODES = ["docs", "videos", "mix"];
// Any language name may be typed in; Python checks it (letters only, 40 at most). Blank means English.
const languageArg = (language) =>
  typeof language === "string" && language.trim() ? [`--language=${language.trim().slice(0, 60)}`] : [];
// The learner's own words about the course (optional).
const descriptionArg = (text) =>
  typeof text === "string" && text.trim() ? [`--description=${text.trim().slice(0, 1000)}`] : [];

const LOG_DIR = path.join(ROOT, "logs");
const LOG_PATH = path.join(LOG_DIR, "app.log");

const queueBuild = createQueue();
const queueProgress = createQueue();
let win = null;
let activeBuild = null; // folder name of the course being built right now

// App log: crashes, errors and build output land in logs/app.log.
function log(...parts) {
  const text = parts.map((p) => (p instanceof Error ? p.stack : typeof p === "string" ? p : JSON.stringify(p))).join(" ");
  try {
    fs.mkdirSync(LOG_DIR, { recursive: true });
    fs.appendFileSync(LOG_PATH, `${new Date().toISOString()} ${text}\n`);
  } catch {}
}

process.on("uncaughtException", (err) => log("uncaughtException", err));
process.on("unhandledRejection", (err) => log("unhandledRejection", err));
app.on("render-process-gone", (_e, _wc, details) => log("renderer gone", details));
app.on("child-process-gone", (_e, details) => log("child process gone", details));
app.on("before-quit", () => log("app quitting", { activeBuild }));

function createWindow() {
  win = new BrowserWindow({
    width: 1200,
    height: 850,
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
    },
  });
  win.setMenu(null);
  // Links from lesson content open in the real browser, never inside the app.
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:\/\//i.test(url)) shell.openExternal(url);
    return { action: "deny" };
  });
  win.webContents.on("will-navigate", (event) => event.preventDefault());
  win.webContents.on("console-message", (_e, level, message, line, source) => {
    if (level >= 3) log("renderer error:", message, `(${source}:${line})`);
  });
  win.on("unresponsive", () => log("window unresponsive"));
  win.on("closed", () => log("window closed"));
  win.loadFile(path.join(__dirname, "index.html"));
}

let web = null;          // the web server, when WEB_PASSWORD is set in .env

function send(channel, payload) {
  if (win && !win.isDestroyed()) win.webContents.send(channel, payload);
  if (web && channel === "build-event") web.broadcast(payload);
}

// Every handler is reachable from the window (IPC) and, if listed in WEB_CALLS,
// from the web site. `evt.remote` is true for web calls.
const handlers = {};
function handle(name, fn) {
  handlers[name] = fn;
  ipcMain.handle(name, fn);
}
// Not open-url / open-folder / open-pdf: those would open things on this PC, not the visitor's.
// Not pick-source-files / save-gemini-key / open-env either: the site uploads files instead, and keys
// are edited in .env on this PC.
const WEB_CALLS = ["list-courses", "load-course", "set-progress", "make-placement", "create-course",
  "delete-course", "resume-course", "make-pdf", "last-email", "email-pdf", "provider-status"];

// Minimal .env reader for the few settings Node needs (Python loads its own).
function readEnv() {
  const env = {};
  try {
    for (const line of fs.readFileSync(path.join(ROOT, ".env"), "utf-8").split(/\r?\n/)) {
      const m = line.match(/^\s*([A-Z0-9_]+)\s*=\s*(.*?)\s*$/);
      if (m) env[m[1]] = m[2].replace(/^(["'])(.*)\1$/, "$2");
    }
  } catch {}
  return { ...env, ...process.env };
}

function runPython(scriptArgs, { onLine, onErrLine } = {}) {
  return new Promise((resolve) => {
    if (!fs.existsSync(PYTHON)) {
      resolve({ code: -1, stdout: "", stderr: `Python venv not found at ${PYTHON}` });
      return;
    }
    // Unbuffered so progress lines arrive while the script is still running.
    const proc = spawn(PYTHON, scriptArgs, {
      cwd: ROOT,
      windowsHide: true,
      env: { ...process.env, PYTHONUNBUFFERED: "1", PYTHONIOENCODING: "utf-8" },
    });
    let stderr = "";
    let stdout = "";
    const outputLines = lineReader((line) => {
      stdout += line + "\n";
      if (onLine) onLine(line);
    });
    const errorLines = lineReader((line) => {
      stderr += line + "\n";
      if (onErrLine) onErrLine(line);
    });
    proc.stdout.on("data", (d) => outputLines.write(d));
    proc.stderr.on("data", (d) => errorLines.write(d));
    proc.on("error", (e) => resolve({ code: -1, stdout, stderr: stderr + e.message }));
    proc.on("close", (code) => {
      outputLines.end();
      errorLines.end();
      resolve({ code, stdout, stderr });
    });
  });
}

// ---- reading courses (read-only; Python is the only writer) ----

function coursePath(folder) {
  if (typeof folder !== "string" || !/^[a-z0-9_]+$/.test(folder)) return null;
  const dir = path.join(COURSES, folder);
  return fs.existsSync(path.join(dir, "course.json")) ? dir : null;
}

// Only plain file names inside the course folder (and its figures/ dir).
function readInside(dir, rel, parse) {
  if (typeof rel !== "string" || !/^(figures\/)?[A-Za-z0-9_.-]+$/.test(rel)) return null;
  try {
    const text = fs.readFileSync(path.join(dir, rel), "utf-8");
    return parse ? JSON.parse(text) : text;
  } catch {
    return null;
  }
}

// Figures are inlined as data URIs so the renderer never touches the file system.
function inlineFigures(dir, markdown) {
  if (!markdown) return markdown;
  return markdown.replace(/\]\((figures\/[A-Za-z0-9_.-]+\.svg)\)/g, (whole, rel) => {
    const uri = svgDataUri(dir, rel);
    return uri ? `](${uri})` : whole;
  });
}

function svgDataUri(dir, rel) {
  const svg = readInside(dir, rel);
  return svg ? `data:image/svg+xml;base64,${Buffer.from(svg, "utf-8").toString("base64")}` : null;
}

// Narration clips (audio/dayNN_sceneK.mp3), inlined like the figures.
function audioDataUri(dir, rel) {
  if (typeof rel !== "string" || !/^audio\/[A-Za-z0-9_.-]+\.mp3$/.test(rel)) return null;
  try {
    return `data:audio/mpeg;base64,${fs.readFileSync(path.join(dir, rel)).toString("base64")}`;
  } catch {
    return null;
  }
}

function loadStoryboard(dir, rel) {
  const board = readInside(dir, rel, true);
  if (!board || !Array.isArray(board.scenes)) return null;
  const scenes = board.scenes.map((s) => ({ ...s, image: svgDataUri(dir, s.image), audio: audioDataUri(dir, s.audio) }))
    .filter((s) => s.image);
  return scenes.length ? { title: board.title || "", scenes } : null;
}

function loadCourse(folder) {
  const dir = coursePath(folder);
  if (!dir) return { error: "Course not found" };
  const course = readInside(dir, "course.json", true);
  if (!course) return { error: "course.json is unreadable" };
  const progress = readInside(dir, "progress.json", true) || { days: {} };
  return {
    folder,
    building: activeBuild === folder,
    course,
    progress,
    outline: inlineFigures(dir, readInside(dir, course.outline)),
    days: (course.days || []).map((d) => ({
      n: d.n,
      lesson: inlineFigures(dir, readInside(dir, d.lesson)),
      videos: readInside(dir, d.videos, true),
      videoCheck: readInside(dir, d.video_check, true),
      storyboard: loadStoryboard(dir, d.storyboard),
      quiz: readInside(dir, d.quiz, true),
    })),
  };
}

handle("list-courses", async () => {
  const { code, stdout, stderr } = await runPython([TEACHME, "list", "--json"]);
  if (code !== 0) return { error: (stderr || `exit ${code}`).trim() };
  try {
    const courses = JSON.parse(stdout.trim().split(/\r?\n/).pop());
    return { courses: courses.map((c) => ({ ...c, building: c.folder === activeBuild })) };
  } catch (err) {
    return { error: `bad course list: ${err.message}` };
  }
});

handle("load-course", async (_evt, folder) => loadCourse(folder));

handle("set-progress", async (_evt, { folder, day, complete, quizScore, quizTotal }) =>
  queueProgress(async () => {
    if (!coursePath(folder) || !Number.isInteger(day)) return { error: "Invalid progress request" };
    const args = [TEACHME, "progress", folder, "--day", String(day)];
    if (complete === true) args.push("--complete");
    if (complete === false) args.push("--incomplete");
    if (Number.isInteger(quizScore) && Number.isInteger(quizTotal)) {
      args.push("--quiz-score", String(quizScore), "--quiz-total", String(quizTotal));
    }
    const { code, stdout, stderr } = await runPython(args);
    if (code !== 0) return { error: (stderr || `exit ${code}`).trim() };
    try {
      return { progress: JSON.parse(stdout.trim().split(/\r?\n/).pop()) };
    } catch {
      return { error: "Progress was saved but could not be read back" };
    }
  }));

handle("make-placement", async (_evt, { topic, language, description } = {}) => {
  topic = typeof topic === "string" ? topic.trim().slice(0, 300) : "";
  if (!topic) return { error: "Type what you want to learn first" };
  const { code, stdout, stderr } = await runPython([TEACHME, "placement", "--topic", topic, ...languageArg(language),
    ...descriptionArg(description)]);
  if (code !== 0) return { error: (stderr.trim().split(/\r?\n/).pop() || `exit ${code}`) };
  try {
    return { quiz: JSON.parse(stdout.trim().split(/\r?\n/).pop()) };
  } catch {
    return { error: "The quiz could not be read" };
  }
});

// Source files: only paths the user picked in the file dialog (or uploaded to the
// web site) may be passed to a build.
const pickedFiles = new Set();
const SOURCE_FILE_TYPES = ["md", "markdown", "txt", "docx", "pdf"];
const UPLOAD_DIR = path.join(ROOT, "logs", "uploads");

// A file uploaded from a web visitor's computer: kept under logs/uploads/<random>/ until
// the build copies it into the course folder.
function saveUpload(name, data) {
  name = path.basename(String(name || "")).replace(/[^\w .()-]/g, "_").slice(-120);
  if (!SOURCE_FILE_TYPES.includes(path.extname(name).slice(1).toLowerCase())) {
    return { error: `${name || "That file"}: only Markdown, text, Word (.docx) and PDF files can be used` };
  }
  const dir = path.join(UPLOAD_DIR, require("crypto").randomBytes(8).toString("hex"));
  fs.mkdirSync(dir, { recursive: true });
  const file = path.join(dir, name);
  fs.writeFileSync(file, data);
  pickedFiles.add(file);
  log("upload", { name, bytes: data.length });
  return { path: file, name };
}

handle("pick-source-files", async () => {
  const result = await dialog.showOpenDialog(win, {
    title: "Files to base the course on",
    properties: ["openFile", "multiSelections"],
    filters: [{ name: "Markdown, text, Word, PDF", extensions: SOURCE_FILE_TYPES }],
  });
  if (result.canceled) return [];
  result.filePaths.forEach((file) => pickedFiles.add(file));
  return result.filePaths.map((file) => ({ path: file, name: path.basename(file) }));
});

handle("create-course", async (_evt, { topic, level, mode, placement, sources, files, language, description }) => {
  topic = typeof topic === "string" ? topic.trim().slice(0, 300) : "";
  if (!topic || !LEVELS.includes(level) || !MODES.includes(mode)) return { error: "Pick a topic, a level and a format" };
  const args = ["create", "--topic", topic, "--level", level, "--mode", mode, ...languageArg(language),
    ...descriptionArg(description)];
  // Source websites: plain web addresses only; Python checks them again before reading anything.
  const urls = (Array.isArray(sources) ? sources : []).filter((u) => typeof u === "string" && u.trim());
  if (urls.length > 5) return { error: "At most 5 source websites" };
  for (const url of urls) {
    if (url.length > 500 || !/^(https?:\/\/)?[a-z0-9.-]+\.[a-z]{2,}(\/\S*)?$/i.test(url.trim())) {
      return { error: `Not a web address: ${url.slice(0, 80)}` };
    }
    args.push("--source", url.trim());
  }
  const chosen = Array.isArray(files) ? files : [];
  if (chosen.length > 20) return { error: "At most 20 source files" };
  for (const file of chosen) {
    if (!pickedFiles.has(file)) return { error: "Pick source files with the Add files button" };
    args.push("--source-file", file);
  }
  if (!placement) return runBuild(args);
  // The answered quiz goes to Python as a file; Python checks it and keeps a copy in the course.
  const file = path.join(os.tmpdir(), `teachme_placement_${process.pid}_${Date.now()}.json`);
  fs.writeFileSync(file, JSON.stringify({ questions: Array.isArray(placement.questions) ? placement.questions : [] }));
  try {
    return await runBuild([...args, "--placement", file]);
  } finally {
    fs.rm(file, { force: true }, () => {});
  }
});

handle("delete-course", async (_evt, folder) => {
  if (!coursePath(folder)) return { error: "Course not found" };
  if (activeBuild === folder) return { error: "This course is still being built. Wait for it to finish." };
  return queueProgress(async () => {
    const { code, stderr } = await runPython([TEACHME, "delete", folder]);
    log("delete course", { folder, code });
    return code === 0 ? { deleted: folder } : { error: (stderr || `exit ${code}`).trim() };
  });
});

// Builds only what is missing or failed in an existing course.
handle("resume-course", async (_evt, folder) => {
  if (!coursePath(folder)) return { error: "Course not found" };
  return runBuild(["resume", folder], folder);
});

function runBuild(args, folder = "") {
  if (activeBuild !== null) return { error: "A course is already being built. Wait for it to finish." };
  activeBuild = folder;
  return queueBuild(async () => {
    let result = null;
    log("build start", args);
    const onLine = (line) => {
      const event = buildEvent(line);
      if (!event) return;
      if (event.kind !== "STATUS") log("build", line);
      if (event.kind === "COURSE") activeBuild = event.data.folder;
      if (event.kind === "DONE" || event.kind === "FAILED") result = event;
      send("build-event", event);
    };
    const onErrLine = (line) => {
      log("build stderr:", line);
      send("build-event", { kind: "LOG", data: line });
    };
    try {
      const { code, stderr } = await runPython([TEACHME, ...args], { onLine, onErrLine });
      log("build exit", { code, result });
      if (result && result.kind === "DONE") return { folder: result.data };
      return { error: result ? result.data : (stderr.trim().split(/\r?\n/).pop() || `Build exited with code ${code}`) };
    } finally {
      activeBuild = null;
    }
  });
}

// ---- PDF of the whole course, and emailing it ----

const pdfPath = (dir) => path.join(dir, `${path.basename(dir)}.pdf`);

handle("make-pdf", async (evt, folder) => {
  const dir = coursePath(folder);
  if (!dir) return { error: "Course not found" };
  const data = loadCourse(folder);
  if (data.error) return data;
  // Printed from a hidden window with scripts off; the HTML goes through a temp file
  // because figures are inlined and a data: URL that size is unreliable.
  const html = path.join(os.tmpdir(), `teachme_pdf_${process.pid}_${Date.now()}.html`);
  const printer = new BrowserWindow({ show: false, webPreferences: { javascript: false, sandbox: true } });
  printer.webContents.on("will-navigate", (event) => event.preventDefault());
  try {
    fs.writeFileSync(html, courseHtml(data), "utf-8");
    await printer.loadFile(html);
    const pdf = await printer.webContents.printToPDF({
      pageSize: "Letter", printBackground: true,
      margins: { top: 0.6, bottom: 0.7, left: 0.7, right: 0.7 },
      displayHeaderFooter: true, headerTemplate: "<span></span>",
      footerTemplate: '<div style="font-size:8px;color:#888;width:100%;text-align:center">' +
        '<span class="pageNumber"></span> / <span class="totalPages"></span></div>',
    });
    const out = pdfPath(dir);
    fs.writeFileSync(out, pdf);
    log("pdf made", { folder, bytes: pdf.length });
    // Web visitors fetch it from /api/pdf; they don't need this PC's file path.
    return evt && evt.remote ? { bytes: pdf.length } : { file: out, bytes: pdf.length };
  } catch (err) {
    log("pdf failed", err);
    return { error: `The PDF could not be made: ${err.message}` };
  } finally {
    printer.destroy();
    fs.rm(html, { force: true }, () => {});
  }
});

handle("open-pdf", async (_evt, folder) => {
  const dir = coursePath(folder);
  if (!dir || !fs.existsSync(pdfPath(dir))) return false;
  return (await shell.openPath(pdfPath(dir))) === "";
});

// Small settings file in the app's data folder (survives restarts and updates).
const prefsPath = () => path.join(app.getPath("userData"), "prefs.json");

function readPrefs() {
  try { return JSON.parse(fs.readFileSync(prefsPath(), "utf-8")) || {}; } catch { return {}; }
}

function savePref(key, value) {
  try {
    fs.writeFileSync(prefsPath(), JSON.stringify({ ...readPrefs(), [key]: value }, null, 2));
  } catch (err) {
    log("prefs not saved", err);
  }
}

handle("last-email", async () => readPrefs().lastEmail || "");

handle("email-pdf", async (evt, { folder, to }) => {
  const dir = coursePath(folder);
  to = typeof to === "string" ? to.trim() : "";
  if (!dir) return { error: "Course not found" };
  if (!/^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$/.test(to)) return { error: "That doesn't look like an email address" };
  const { code, stderr } = await runPython([TEACHME, "email", folder, "--to", to]);
  log("email pdf", { folder, code });
  // Remembered once it has actually been used (sent, or handed to the mail program).
  if (code === 0 || code === 3) savePref("lastEmail", to);
  if (code === 0) return { sent: to };
  if (code !== 3) return { error: stderr.trim().split(/\r?\n/).pop() || `exit ${code}` };
  // A web visitor's own browser opens their mail program (see web-api.js).
  if (evt && evt.remote) return { manual: true };
  // No SMTP settings: open a new message in the user's mail program and show the
  // PDF in Explorer so it can be dragged in (mailto: can't carry attachments).
  const title = (readInside(dir, "course.json", true) || {}).title || folder;
  await shell.openExternal(`mailto:${encodeURIComponent(to)}?subject=${encodeURIComponent(`Teach Me course: ${title}`)}` +
    `&body=${encodeURIComponent(`Attached: ${path.basename(pdfPath(dir))}`)}`);
  shell.showItemInFolder(pdfPath(dir));
  return { manual: true };
});

// ---- AI setup: which providers are available, and saving a free Gemini key ----

handle("provider-status", async () => {
  const { code, stdout, stderr } = await runPython([TEACHME, "providers"]);
  if (code !== 0) return { error: (stderr.trim().split(/\r?\n/).pop() || `exit ${code}`) };
  try {
    return { status: JSON.parse(stdout.trim().split(/\r?\n/).pop()) };
  } catch {
    return { error: "Could not read the provider status" };
  }
});

// Writes the key into .env (created from .env.example if missing). The key never
// goes back to the renderer and is not logged.
handle("save-gemini-key", async (_evt, value) => {
  value = typeof value === "string" ? value.trim() : "";
  if (!/^AIza[0-9A-Za-z_-]{30,60}$/.test(value)) {
    return { error: "That doesn't look like a Gemini API key. They start with AIza and are about 39 characters." };
  }
  const envPath = path.join(ROOT, ".env");
  const example = path.join(ROOT, ".env.example");
  try {
    let text = fs.existsSync(envPath) ? fs.readFileSync(envPath, "utf-8")
      : fs.existsSync(example) ? fs.readFileSync(example, "utf-8") : "";
    const line = `GEMINI_API_KEY=${value}`;
    const existing = /^GEMINI_API_KEY=.*$/m;
    text = existing.test(text) ? text.replace(existing, () => line) : `${text.replace(/\n*$/, "\n")}${line}\n`;
    fs.writeFileSync(envPath, text, { encoding: "utf-8", mode: 0o600 });
    log("saved GEMINI_API_KEY to .env");
    return { ok: true };
  } catch (err) {
    return { error: `Could not write .env: ${err.message}` };
  }
});

// Opens .env in the system's text editor so the blanks can be filled in.
handle("open-env", async () => {
  const envPath = path.join(ROOT, ".env");
  if (!fs.existsSync(envPath)) await runPython([TEACHME, "providers"]);   // creates it, all blank
  const error = await shell.openPath(envPath);
  return error ? { error } : { ok: true };
});

handle("open-url", async (_evt, url) => {
  // Only allow http(s) — never open schemes that come from data files.
  if (typeof url !== "string" || !/^https?:\/\//i.test(url)) return false;
  await shell.openExternal(url);
  return true;
});

handle("open-folder", async (_evt, folder) => {
  const dir = coursePath(folder);
  if (!dir) return false;
  await shell.openPath(dir);
  return true;
});

// One Teach Me at a time: a second copy can't see the first one's build and
// would show it as interrupted (and deletable). Reopening focuses the window.
const primary = app.requestSingleInstanceLock();
if (!primary) app.quit();
app.on("second-instance", () => {
  // Running as the background web server: opening Teach Me shows a window here.
  if (!win || win.isDestroyed()) {
    if (app.isReady()) createWindow();
    return;
  }
  if (win.isMinimized()) win.restore();
  win.focus();
});

app.whenReady().then(() => {
  if (!primary) return;
  // YouTube's embedded player refuses to play without a Referer, and pages
  // loaded from file:// send none. Give embed requests a stable one.
  session.defaultSession.webRequest.onBeforeSendHeaders(
    { urls: ["https://www.youtube-nocookie.com/*", "https://www.youtube.com/*"] },
    (details, callback) => {
      const headers = details.requestHeaders;
      if (!headers.Referer && !headers.referer) headers.Referer = "https://teachme.local/";
      callback({ requestHeaders: headers });
    });
  log("app start", { electron: process.versions.electron, crashDumps: app.getPath("crashDumps"), headless: HEADLESS });
  startWeb();
  if (!HEADLESS) createWindow();
});
// Headless, the hidden PDF window closing must not end the server.
app.on("window-all-closed", () => { if (!HEADLESS) app.quit(); });

function startWeb() {
  const env = readEnv();
  const password = env.WEB_PASSWORD || "";
  // The owner picks the password; sign-in is rate-limited per visitor (see web.js).
  if (password.length < 4) {
    if (password || HEADLESS) log("web site off: WEB_PASSWORD in .env must be at least 4 characters");
    if (HEADLESS) {
      console.error(`Set WEB_PASSWORD (4+ characters) in ${path.join(ROOT, ".env")} to run the web site.`);
      app.quit();
    }
    return;
  }
  const port = Number(env.WEB_PORT) || 8795;
  web = createWebServer({
    dir: __dirname, handlers, allowed: WEB_CALLS, password, port, log, upload: saveUpload,
    secretFile: path.join(app.getPath("userData"), "web-secret"),
    pdfFile: (folder) => {
      const dir = coursePath(folder);
      return dir && fs.existsSync(pdfPath(dir)) ? pdfPath(dir) : null;
    },
  });
  web.server.on("listening", () => console.log(`Teach Me web site: http://127.0.0.1:${port}`));
}
