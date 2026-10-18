// Teach Me as a web site, for use behind a Cloudflare Tunnel.
//
// Serves the same page as the desktop window, and turns the same IPC handlers
// into POST /api/call/<name>. It listens on 127.0.0.1 only (cloudflared connects
// locally) and everything except the login page needs the password from .env.
const http = require("http");
const fs = require("fs");
const path = require("path");
const crypto = require("crypto");

const SESSION_DAYS = 30;
const COOKIE = "tm_session";
const JOB_WAIT_MS = 20000;      // Cloudflare drops responses after 100 s; long calls become jobs
const MAX_BODY = 1024 * 1024;
const MAX_UPLOAD = 50 * 1024 * 1024;   // same cap as sources.py; Cloudflare's free plan allows 100 MB

const STATIC = {
  "/": ["index.html", "text/html; charset=utf-8"],
  "/renderer.js": ["renderer.js", "text/javascript; charset=utf-8"],
  "/web-api.js": ["web-api.js", "text/javascript; charset=utf-8"],
  "/node_modules/marked/lib/marked.umd.js": ["node_modules/marked/lib/marked.umd.js", "text/javascript; charset=utf-8"],
  "/node_modules/dompurify/dist/purify.min.js": ["node_modules/dompurify/dist/purify.min.js", "text/javascript; charset=utf-8"],
};

const CSP = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: https://i.ytimg.com; " +
  "frame-src https://www.youtube-nocookie.com; connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'";

function loginPage(message) {
  return `<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Teach Me</title><style>
:root { color-scheme: dark; }
body { margin: 0; min-height: 100vh; display: grid; place-items: center; background: #0f1115; color: #e8eaf0;
       font: 15px/1.5 "Segoe UI", system-ui, sans-serif; padding: 0 16px; box-sizing: border-box; }
form { width: 100%; max-width: 340px; } h1 { font-size: 28px; margin: 0 0 18px; }
input, button { width: 100%; box-sizing: border-box; padding: 11px 12px; font-size: 15px; border-radius: 8px; }
input { border: 1px solid #2a2f3a; background: #171a21; color: inherit; margin-bottom: 10px; }
button { border: 0; background: #3b82f6; color: white; font-weight: 600; cursor: pointer; }
.error { color: #fca5a5; margin-bottom: 10px; }
</style></head><body><form method="post" action="/login">
<h1>Teach Me</h1>${message ? `<div class="error">${message}</div>` : ""}
<input type="password" name="password" placeholder="Password" autofocus autocomplete="current-password" required>
<button type="submit">Sign in</button></form></body></html>`;
}

function createWebServer({ dir, handlers, allowed, password, port, secretFile, pdfFile, upload, log }) {
  // A random secret kept on disk signs session cookies; the password is mixed
  // in, so changing WEB_PASSWORD signs everyone out.
  let secret;
  try { secret = fs.readFileSync(secretFile); } catch {}
  if (!secret || secret.length < 32) {
    secret = crypto.randomBytes(32);
    fs.writeFileSync(secretFile, secret);
  }
  const sign = (text) => crypto.createHmac("sha256", secret).update(`${text}|${password}`).digest("base64url");

  function newSession() {
    const expires = Date.now() + SESSION_DAYS * 86400000;
    return `${expires}.${sign(String(expires))}`;
  }

  function validSession(req) {
    const match = (req.headers.cookie || "").match(new RegExp(`(?:^|;\\s*)${COOKIE}=([^;]+)`));
    if (!match) return false;
    const [expires, mac] = match[1].split(".");
    if (!expires || !mac || Number(expires) < Date.now()) return false;
    const want = Buffer.from(sign(expires));
    const got = Buffer.from(mac);
    return got.length === want.length && crypto.timingSafeEqual(got, want);
  }

  function passwordOk(given) {
    const a = crypto.createHash("sha256").update(String(given)).digest();
    const b = crypto.createHash("sha256").update(password).digest();
    return crypto.timingSafeEqual(a, b);
  }

  // Failed sign-ins per visitor: 10 per 15 minutes.
  const failures = new Map();
  const clientIp = (req) => req.headers["cf-connecting-ip"] || req.socket.remoteAddress || "";
  function tooManyFailures(ip) {
    const now = Date.now();
    const recent = (failures.get(ip) || []).filter((t) => now - t < 15 * 60000);
    failures.set(ip, recent);
    return recent.length >= 10;
  }

  const secure = (req) => req.headers["x-forwarded-proto"] === "https" || !!req.headers["cf-visitor"];
  function cookie(req, value, maxAge) {
    return `${COOKIE}=${value}; Path=/; HttpOnly; SameSite=Lax; Max-Age=${maxAge}${secure(req) ? "; Secure" : ""}`;
  }

  function send(res, status, body, type = "application/json; charset=utf-8", extra = {}) {
    res.writeHead(status, {
      "Content-Type": type, "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
      "X-Frame-Options": "DENY", "Referrer-Policy": "strict-origin-when-cross-origin", "Content-Security-Policy": CSP,
      ...extra,
    });
    res.end(typeof body === "string" || Buffer.isBuffer(body) ? body : JSON.stringify(body));
  }

  function readBody(req, limit = MAX_BODY, raw = false) {
    return new Promise((resolve, reject) => {
      const chunks = [];
      let size = 0;
      req.on("data", (c) => {
        size += c.length;
        if (size > limit) { reject(new Error("too large")); req.destroy(); } else chunks.push(c);
      });
      req.on("end", () => resolve(raw ? Buffer.concat(chunks) : Buffer.concat(chunks).toString("utf-8")));
      req.on("error", reject);
    });
  }

  // Long calls (a whole course build) outlive one HTTP request: after JOB_WAIT_MS
  // the client gets a job id and asks again until the result is in.
  const jobs = new Map();
  function waitForJob(job, ms) {
    return Promise.race([job.promise, new Promise((r) => setTimeout(r, ms))]).then(() => job.done);
  }
  async function callResult(job, res) {
    if (await waitForJob(job, JOB_WAIT_MS)) send(res, 200, { result: job.result });
    else send(res, 200, { job: job.id });
  }

  // Build progress goes to every open browser as server-sent events.
  const streams = new Set();
  function broadcast(event) {
    const text = `data: ${JSON.stringify(event)}\n\n`;
    for (const res of streams) res.write(text);
  }
  setInterval(() => { for (const res of streams) res.write(": ping\n\n"); }, 25000).unref();

  async function handle(req, res) {
    const url = new URL(req.url, "http://localhost");
    const route = url.pathname;

    if (route === "/login" && req.method === "POST") {
      const ip = clientIp(req);
      if (tooManyFailures(ip)) return send(res, 429, loginPage("Too many tries. Wait 15 minutes."), "text/html; charset=utf-8");
      const given = new URLSearchParams(await readBody(req)).get("password") || "";
      if (!passwordOk(given)) {
        failures.get(ip).push(Date.now());
        log("web sign-in failed", { ip });
        await new Promise((r) => setTimeout(r, 600));
        return send(res, 401, loginPage("Wrong password."), "text/html; charset=utf-8");
      }
      failures.delete(ip);
      log("web sign-in", { ip });
      res.writeHead(303, { Location: "/", "Set-Cookie": cookie(req, newSession(), SESSION_DAYS * 86400), "Cache-Control": "no-store" });
      return res.end();
    }

    if (!validSession(req)) {
      if (route.startsWith("/api/")) return send(res, 401, { error: "Signed out. Reload the page to sign in." });
      if (req.method === "GET" && route === "/") return send(res, 200, loginPage(""), "text/html; charset=utf-8");
      res.writeHead(303, { Location: "/" });
      return res.end();
    }

    if (route === "/logout" && req.method === "POST") {
      res.writeHead(303, { Location: "/", "Set-Cookie": cookie(req, "", 0) });
      return res.end();
    }

    if (req.method === "GET" && STATIC[route]) {
      const [file, type] = STATIC[route];
      let body = fs.readFileSync(path.join(dir, file));
      return send(res, 200, body, type);
    }

    if (req.method === "GET" && route === "/api/events") {
      res.writeHead(200, { "Content-Type": "text/event-stream", "Cache-Control": "no-store", "X-Accel-Buffering": "no" });
      res.write(": connected\n\n");
      streams.add(res);
      req.on("close", () => streams.delete(res));
      return;
    }

    if (req.method === "GET" && route === "/api/pdf") {
      const file = pdfFile(url.searchParams.get("folder"));
      if (!file) return send(res, 404, { error: "Make the PDF first" });
      return send(res, 200, fs.readFileSync(file), "application/pdf",
        { "Content-Disposition": `inline; filename="${path.basename(file)}"` });
    }

    // Calls change things, so they need a header a cross-site form or image can't
    // send (a page on another site would trigger a CORS preflight we never allow).
    if (req.method === "POST" && req.headers["x-teachme"] !== "1") return send(res, 403, { error: "Forbidden" });

    // A source file for a new course, from the visitor's computer (the body is the file).
    if (req.method === "POST" && route === "/api/upload" && upload) {
      let data;
      try {
        data = await readBody(req, MAX_UPLOAD, true);
      } catch {
        return send(res, 413, { error: "That file is over 50 MB" });
      }
      return send(res, 200, upload(url.searchParams.get("name"), data));
    }

    const jobMatch = route.match(/^\/api\/job\/([a-f0-9]{32})$/);
    if (req.method === "POST" && jobMatch) {
      const job = jobs.get(jobMatch[1]);
      if (!job) return send(res, 404, { error: "That job is gone (the server may have restarted)" });
      return callResult(job, res);
    }

    const callMatch = route.match(/^\/api\/call\/([a-z-]+)$/);
    if (req.method === "POST" && callMatch && allowed.includes(callMatch[1]) && handlers[callMatch[1]]) {
      let arg;
      try {
        const text = await readBody(req);
        arg = text ? JSON.parse(text).arg : undefined;
      } catch {
        return send(res, 400, { error: "Bad request" });
      }
      const job = { id: crypto.randomBytes(16).toString("hex"), done: false, result: null };
      job.promise = Promise.resolve()
        .then(() => handlers[callMatch[1]]({ remote: true }, arg))
        .catch((err) => { log("web call failed", callMatch[1], err); return { error: "Something went wrong on the server" }; })
        .then((result) => {
          job.done = true;
          job.result = result;
          setTimeout(() => jobs.delete(job.id), 10 * 60000).unref();
        });
      jobs.set(job.id, job);
      return callResult(job, res);
    }

    send(res, 404, { error: "Not found" });
  }

  const server = http.createServer((req, res) => {
    handle(req, res).catch((err) => {
      log("web error", err);
      if (!res.headersSent) send(res, 500, { error: "Server error" });
      else res.end();
    });
  });
  server.on("error", (err) => log("web server error", err));
  server.listen(port, "127.0.0.1", () => log("web server listening", { url: `http://127.0.0.1:${port}` }));
  return { broadcast, server };
}

module.exports = { createWebServer };
