const { StringDecoder } = require("string_decoder");

function lineReader(onLine = () => {}) {
  const decoder = new StringDecoder("utf8");
  let pending = "";
  function consume(text) {
    pending += text;
    const lines = pending.split(/\r?\n/);
    pending = lines.pop();
    lines.filter(Boolean).forEach(onLine);
  }
  return {
    write: (chunk) => consume(decoder.write(chunk)),
    end: () => {
      consume(decoder.end());
      if (pending) onLine(pending);
      pending = "";
    },
  };
}

function createQueue() {
  let tail = Promise.resolve();
  return (job) => {
    const result = tail.then(job);
    tail = result.catch(() => {});
    return result;
  };
}

// "@@KIND payload" from teachme.py create -> {kind, data}. JSON payloads are
// parsed; STATUS / DONE / FAILED carry plain text. Other lines return null.
function buildEvent(line) {
  const m = /^@@([A-Z]+) (.*)$/.exec(line);
  if (!m) return null;
  const [, kind, payload] = m;
  if (["STATUS", "DONE", "FAILED"].includes(kind)) return { kind, data: payload };
  try {
    return { kind, data: JSON.parse(payload) };
  } catch {
    return null;
  }
}

module.exports = { lineReader, createQueue, buildEvent };
