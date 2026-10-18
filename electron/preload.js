const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("api", {
  listCourses: () => ipcRenderer.invoke("list-courses"),
  loadCourse: (folder) => ipcRenderer.invoke("load-course", folder),
  createCourse: (topic, level, mode, placement, sources, files, language, description) =>
    ipcRenderer.invoke("create-course", { topic, level, mode, placement, sources, files, language, description }),
  pickSourceFiles: () => ipcRenderer.invoke("pick-source-files"),
  makePlacement: (topic, language, description) => ipcRenderer.invoke("make-placement", { topic, language, description }),
  resumeCourse: (folder) => ipcRenderer.invoke("resume-course", folder),
  deleteCourse: (folder) => ipcRenderer.invoke("delete-course", folder),
  setProgress: (folder, day, { complete, quizScore, quizTotal } = {}) =>
    ipcRenderer.invoke("set-progress", { folder, day, complete, quizScore, quizTotal }),
  openUrl: (url) => ipcRenderer.invoke("open-url", url),
  openFolder: (folder) => ipcRenderer.invoke("open-folder", folder),
  makePdf: (folder) => ipcRenderer.invoke("make-pdf", folder),
  openPdf: (folder) => ipcRenderer.invoke("open-pdf", folder),
  emailPdf: (folder, to) => ipcRenderer.invoke("email-pdf", { folder, to }),
  lastEmail: () => ipcRenderer.invoke("last-email"),
  providerStatus: () => ipcRenderer.invoke("provider-status"),
  saveGeminiKey: (key) => ipcRenderer.invoke("save-gemini-key", key),
  openEnv: () => ipcRenderer.invoke("open-env"),
  // Build events ({kind, data}) from a running course build; returns an unsubscribe fn.
  onBuildEvent: (cb) => {
    const handler = (_evt, event) => cb(event);
    ipcRenderer.on("build-event", handler);
    return () => ipcRenderer.removeListener("build-event", handler);
  },
});
