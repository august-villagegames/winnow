import test from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const require = createRequire(import.meta.url);
const core = require("../.agents/skills/winnow/assets/runtime-core.js");
const root = dirname(fileURLToPath(import.meta.url));
const seed = JSON.parse(readFileSync(join(root, "../fixtures/synthetic-seed.json"), "utf8"));
const source = readFileSync(join(root, "../.agents/skills/winnow/assets/rolling-runtime-ui.js"), "utf8")
  .replace("__WINNOW_ICONS__", "{}")
  .replace("__WINNOW_SEED_BASE64__", "")
  .replace("__WINNOW_SEED_HASH__", "a".repeat(64));
const context = {
  module: { exports: {} },
  globalThis: { WinnowCore: core },
  TextDecoder,
  Uint8Array,
  URL,
  Set,
  Math,
  Object,
  Array,
  Number,
  String,
  Error,
};
vm.runInNewContext(source, context, { filename: "rolling-runtime-ui.js" });
const rolling = context.module.exports;
const hash = "a".repeat(64);
const envelope = {
  protocol: "winnow.rolling-page",
  version: 1,
  coordinatorOrigin: "https://mcp.example.test/",
  browserCapability: "browser-capability-only-" + "b".repeat(48),
  publishedRevision: 3,
};

test("rolling envelope is closed and coordinator origin is normalized", () => {
  assert.equal(JSON.stringify(rolling.validateEnvelope({ ...envelope, coordinatorOrigin: "https://MCP.example.test" })), JSON.stringify(envelope));
  assert.throws(() => rolling.validateEnvelope({ ...envelope, claimToken: "no" }), /invalid rolling page envelope/);
  assert.throws(() => rolling.validateEnvelope({ ...envelope, coordinatorOrigin: "https://mcp.example.test/path" }), /invalid coordinator origin/);
  assert.throws(() => rolling.validateEnvelope({ ...envelope, browserCapability: " capability" }), /invalid browser capability/);
});

test("rolling runtime exposes the browser bootstrap entry point", () => {
  assert.equal(typeof rolling.bootstrap, "function");
});

test("session-keyed rolling state resumes only an exact embedded revision", () => {
  const initial = rolling.emptyState(seed, hash, 3);
  const selected = {
    ...initial,
    events: [{ type: "verdict", optionId: seed.round.options[0].id, decision: "like", createdAt: "2026-08-18T00:00:00.000Z" }],
  };
  const resumed = rolling.reconcileState(selected, seed, hash, 3);
  assert.equal(resumed.action, "resume");
  assert.equal(resumed.state.events.length, 1);

  const changedHash = rolling.reconcileState(selected, seed, "b".repeat(64), 3);
  assert.equal(changedHash.action, "divergent");
  assert.equal(changedHash.state.events.length, 0);
  assert.equal(changedHash.state.reveal.state, "stale");

  const changedOptions = structuredClone(seed);
  changedOptions.round.options[0].id = "replacement-option";
  const changedPage = rolling.reconcileState(selected, changedOptions, "b".repeat(64), 3);
  assert.equal(changedPage.action, "divergent");
  assert.equal(changedPage.state.reveal.state, "stale");

  const futureRound = rolling.reconcileState({ ...selected, lastSeenRound: 2 }, seed, hash, 3);
  assert.equal(futureRound.action, "stale_cache");
  assert.equal(futureRound.state.reveal.state, "stale");
});

test("browser next-round request is the bounded selection contract only", () => {
  const state = rolling.emptyState(seed, hash, 3);
  state.events = seed.round.options.map((option, index) => ({ type: "verdict", optionId: option.id, decision: index % 3 === 0 ? "like" : index % 3 === 1 ? "dislike" : "skip", createdAt: "2026-08-18T00:00:00.000Z" }));
  const request = rolling.browserRequest(seed, hash, envelope, state, "2ab5e251-9c44-4ec9-afbf-0b3f80addce9");
  assert.deepEqual(Object.keys(request).sort(), ["idempotencyKey", "protocol", "publishedRevision", "roundNumber", "seedHash", "selectedProfileKeys", "verdicts", "version"]);
  assert.deepEqual(request.verdicts.map((verdict) => verdict.optionId), seed.round.options.map((option) => option.id));
  assert.ok(request.selectedProfileKeys.length <= 6);
  const body = JSON.stringify(request);
  assert.equal(body.includes("continuation"), false);
  assert.equal(body.includes("profileExclusions"), false);
  assert.equal(body.includes("profilePatterns"), false);
  assert.throws(() => rolling.browserRequest(seed, hash, envelope, { ...state, events: state.events.slice(1) }, "2ab5e251-9c44-4ec9-afbf-0b3f80addce9"), /every option needs a verdict/);
});

test("status response validation, cache busting, and terminal shapes remain strict", () => {
  const connected = rolling.parseStatus({ status: "connected", roundNumber: 1, seedHash: hash, publishedRevision: 3, expiresAt: "2026-08-19T00:00:00Z", agentLeaseExpiresAt: "2026-08-18T01:00:00Z", remainingOptionCapacity: 94 });
  assert.equal(connected.status, "connected");
  assert.equal(rolling.parseStatus({ status: "failed", expiresAt: "2026-08-19T00:00:00Z" }).status, "failed");
  assert.throws(() => rolling.parseStatus({ ...connected, continuation: {} }), /invalid status response/);
  assert.throws(() => rolling.parseStatus({ status: "connected" }), /invalid status response/);
  assert.equal(rolling.cacheBuster("https://demo.here.now/?x=1", 4), "https://demo.here.now/?x=1&winnowRevision=4");
  const calls = [];
  const history = { replaceState: (...args) => calls.push(args) };
  assert.equal(JSON.stringify(rolling.consumeCacheBuster({ href: "https://demo.here.now/?winnowRevision=3" }, history, 3)), JSON.stringify({ consumed: true, valid: true }));
  assert.deepEqual(calls, [[null, "", "/"]]);
  assert.equal(JSON.stringify(rolling.consumeCacheBuster({ href: "https://demo.here.now/?winnowRevision=4" }, history, 3)), JSON.stringify({ consumed: false, valid: false }));
});

test("mocked browser status lifecycle polls only while work is active and backs off safely", () => {
  const normal = { visible: true, staleCache: false, failures: 0, random: () => 0.5 };
  assert.equal(rolling.pollingDelay({ ...normal, status: "connecting" }), 5000);
  assert.equal(rolling.pollingDelay({ ...normal, status: "researching" }), 5000);
  assert.equal(rolling.pollingDelay({ ...normal, status: "connected" }), null);
  assert.equal(rolling.pollingDelay({ ...normal, status: "ready_to_reveal" }), null);
  assert.equal(rolling.pollingDelay({ ...normal, status: "failed" }), null);
  assert.equal(rolling.pollingDelay({ ...normal, status: "connected", staleCache: true }), 5000);
  assert.equal(rolling.pollingDelay({ ...normal, status: "researching", failures: 2, random: () => 0 }), 16000);
  assert.equal(rolling.pollingDelay({ ...normal, status: "researching", failures: 99, random: () => 1 }), 30000);
  assert.equal(rolling.pollingDelay({ ...normal, visible: false, status: "researching" }), null);
});

test("mocked competing browser responses converge without auto-revealing a new revision", () => {
  const tabOne = rolling.emptyState(seed, hash, 3);
  tabOne.request = { state: "submitting", idempotencyKey: "2ab5e251-9c44-4ec9-afbf-0b3f80addce9" };
  const first = rolling.applyNextRoundResult(tabOne, { status: "accepted", roundNumber: 1, publishedRevision: 3 });
  assert.equal(first.status, "researching");
  assert.equal(first.state.request.state, "accepted");

  // A losing tab receives the coordinator's canonical researching status. It
  // freezes rather than creating a second request, and no successor seed is
  // injected into either tab.
  const tabTwo = rolling.reconcileRemoteStatus(rolling.emptyState(seed, hash, 3), { status: "researching", roundNumber: 1, seedHash: hash, publishedRevision: 3, expiresAt: "2026-08-19T00:00:00Z", agentLeaseExpiresAt: null, remainingOptionCapacity: 94 }, { roundNumber: 1, seedHash: hash, publishedRevision: 3 });
  assert.equal(tabTwo.status, "researching");
  assert.equal(rolling.nextRoundControl({ status: tabTwo.status, revealState: tabTwo.state.reveal.state, remainingCapacity: 94, requestInFlight: false, requestState: "idle" }).disabled, true);

  const newer = rolling.reconcileRemoteStatus(first.state, { status: "ready_to_reveal", roundNumber: 2, seedHash: "b".repeat(64), publishedRevision: 4, expiresAt: "2026-08-19T00:00:00Z", agentLeaseExpiresAt: null, remainingOptionCapacity: 88 }, { roundNumber: 1, seedHash: hash, publishedRevision: 3 });
  assert.equal(newer.status, "ready_to_reveal");
  assert.equal(newer.state.reveal.state, "ready");
  assert.equal(newer.state.events.length, 0, "the old page retains no new round content");
  assert.equal(rolling.nextRoundControl({ status: newer.status, revealState: newer.state.reveal.state, remainingCapacity: 88, requestInFlight: false, requestState: newer.state.request.state }).label, "Continue");
  assert.equal(rolling.nextRoundControl({ status: "failed", revealState: "current", remainingCapacity: 88, requestInFlight: false, requestState: "idle" }).label, "Session unavailable");
});

test("rolling source does not include the legacy clipboard handoff", () => {
  assert.equal(source.includes("navigator.clipboard"), false);
  assert.equal(source.includes("Return to the agent"), false);
  assert.match(source, /escapeHtml\(option\.description\.text\)/);
  assert.doesNotMatch(source, /escapeHtml\(option\.description\)(?!\.)/);
  assert.match(source, /credentials: "omit"/);
  assert.match(source, /Authorization: `Bearer \$\{envelope\.browserCapability\}`/);
});
