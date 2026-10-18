const test = require("node:test");
const assert = require("node:assert/strict");
const { lineReader, createQueue, buildEvent } = require("./jobs");

test("progress survives arbitrary byte boundaries and trailing partial line", () => {
  const lines = [];
  const reader = lineReader((line) => lines.push(line));
  const text = '@@STATUS Writing Day 1 lesson… café ✓\r\nlast';
  for (const byte of Buffer.from(text)) reader.write(Buffer.from([byte]));
  reader.end();
  assert.deepEqual(lines, ["@@STATUS Writing Day 1 lesson… café ✓", "last"]);
});

test("build events parse JSON and text payloads", () => {
  assert.deepEqual(buildEvent('@@ARTIFACT {"day":2,"kind":"quiz","path":"day02_quiz.json"}'),
    { kind: "ARTIFACT", data: { day: 2, kind: "quiz", path: "day02_quiz.json" } });
  assert.deepEqual(buildEvent('@@COURSE {"folder":"wash_dishes_expert_20261014_13"}'),
    { kind: "COURSE", data: { folder: "wash_dishes_expert_20261014_13" } });
  assert.deepEqual(buildEvent("@@DONE wash_dishes_expert_20261014_13"),
    { kind: "DONE", data: "wash_dishes_expert_20261014_13" });
  assert.deepEqual(buildEvent("@@FAILED No provider is available"), { kind: "FAILED", data: "No provider is available" });
});

test("non-event and malformed lines are ignored", () => {
  assert.equal(buildEvent("Running course plan with anthropic"), null);
  assert.equal(buildEvent("@@ARTIFACT {not json"), null);
});

test("queue serializes writes and continues after failure", async () => {
  const queue = createQueue();
  const events = [];
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  const first = queue(async () => { events.push("start"); await gate; events.push("finish"); throw Error("failed"); });
  const handled = first.catch(() => {});
  const second = queue(async () => { events.push("second"); });
  await Promise.resolve();
  assert.deepEqual(events, ["start"]);
  release();
  await Promise.all([handled, second]);
  assert.deepEqual(events, ["start", "finish", "second"]);
});
