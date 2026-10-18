// In a browser (the web site) there is no Electron preload, so window.api is
// built here on top of the server's /api routes. In the desktop app this file
// does nothing: preload.js has already defined window.api.
(function () {
  if (window.api) return;

  async function post(path, body) {
    const res = await fetch(path, {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-Teachme": "1" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (res.status === 401) {
      location.reload();
      return { error: "Signed out" };
    }
    return res.json();
  }

  // Long calls come back as a job id; keep asking until the result is in.
  async function call(name, arg) {
    try {
      let reply = await post(`/api/call/${name}`, { arg });
      while (reply.job) reply = await post(`/api/job/${reply.job}`);
      return reply.error && reply.result === undefined ? reply : reply.result;
    } catch (err) {
      return { error: `The server couldn't be reached (${err.message})` };
    }
  }

  const listeners = new Set();
  const events = new EventSource("/api/events");
  events.onmessage = (msg) => {
    let event;
    try { event = JSON.parse(msg.data); } catch { return; }
    for (const cb of listeners) cb(event);
  };

  const pdfUrl = (folder) => `/api/pdf?folder=${encodeURIComponent(folder)}`;

  window.api = {
    remote: true,
    listCourses: () => call("list-courses"),
    loadCourse: (folder) => call("load-course", folder),
    createCourse: (topic, level, mode, placement, sources, files, language, description) =>
      call("create-course", { topic, level, mode, placement, sources, files, language, description }),
    makePlacement: (topic, language, description) => call("make-placement", { topic, language, description }),
    // Files from the visitor's computer are uploaded one by one; the server keeps them for the build.
    pickSourceFiles: () => new Promise((resolve) => {
      const input = Object.assign(document.createElement("input"),
        { type: "file", multiple: true, accept: ".md,.markdown,.txt,.docx,.pdf" });
      input.addEventListener("cancel", () => resolve([]));
      input.addEventListener("change", async () => {
        const picked = [];
        const errors = [];
        for (const file of input.files) {
          try {
            const res = await fetch(`/api/upload?name=${encodeURIComponent(file.name)}`, {
              method: "POST", credentials: "same-origin", headers: { "X-Teachme": "1" }, body: file,
            });
            const result = await res.json();
            if (result.error) errors.push(result.error);
            else picked.push(result);
          } catch (err) {
            errors.push(`${file.name}: ${err.message}`);
          }
        }
        resolve(Object.assign(picked, { errors }));
      });
      input.click();
    }),
    providerStatus: () => call("provider-status"),
    saveGeminiKey: async () => ({ error: "Add GEMINI_API_KEY to .env on the server, then restart the web site." }),
    openEnv: async () => ({ error: "Edit .env on the server, then restart the web site." }),
    resumeCourse: (folder) => call("resume-course", folder),
    deleteCourse: (folder) => call("delete-course", folder),
    setProgress: (folder, day, { complete, quizScore, quizTotal } = {}) =>
      call("set-progress", { folder, day, complete, quizScore, quizTotal }),
    openUrl: async (url) => {
      if (typeof url !== "string" || !/^https?:\/\//i.test(url)) return false;
      window.open(url, "_blank", "noopener");
      return true;
    },
    openFolder: async () => false,
    makePdf: (folder) => call("make-pdf", folder),
    openPdf: async (folder) => { window.open(pdfUrl(folder), "_blank", "noopener"); return true; },
    emailPdf: async (folder, to) => {
      const result = await call("email-pdf", { folder, to });
      // The server can't open a mail program for you; do it here.
      if (result && result.manual) {
        location.href = `mailto:${encodeURIComponent(to)}?subject=${encodeURIComponent("Teach Me course")}`;
      }
      return result;
    },
    lastEmail: () => call("last-email"),
    onBuildEvent: (cb) => {
      listeners.add(cb);
      return () => listeners.delete(cb);
    },
  };
})();
