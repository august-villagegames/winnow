import test from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const require = createRequire(import.meta.url);
const core = require("../assets/runtime-core.js");
const root = dirname(fileURLToPath(import.meta.url));
const seed = JSON.parse(readFileSync(join(root, "../fixtures/synthetic-seed.json"), "utf8"));
const runtimeUi = readFileSync(join(root, "../assets/runtime-ui.js"), "utf8");

test("typed formatting is runtime-owned", () => {
  const price = seed.round.factors.find((factor) => factor.id === "price");
  const covers = seed.round.factors.find((factor) => factor.id === "covers");
  const delivery = { valueType: "number", display: { style: "duration", unit: "week" } };
  assert.equal(core.formatValue(price, 1780), "$1,780");
  assert.equal(core.formatValue(covers, true), "Removable covers");
  assert.equal(core.formatValue(delivery, 2), "2 weeks");
});

test("profile scoring ignores skips and emits strong numeric and boolean patterns", () => {
  const patterns = core.computeProfile(seed, {
    "sofa-1": "like",
    "sofa-2": "dislike",
    "sofa-3": "like",
    "sofa-4": "dislike",
    "sofa-5": "like",
    "sofa-6": "skip",
  });
  assert.ok(patterns.length >= 2);
  assert.ok(patterns.some((pattern) => pattern.factorId === "price" && pattern.text.includes("higher prices")));
  assert.ok(patterns.some((pattern) => pattern.factorId === "covers" && pattern.text.includes("include covers")));
  assert.ok(patterns.every((pattern) => pattern.strength >= 0.2));
});

test("profile falls back when there is no contrast", () => {
  const patterns = core.computeProfile(seed, {
    "sofa-1": "like",
    "sofa-2": "like",
    "sofa-3": "like",
    "sofa-4": "skip",
    "sofa-5": "skip",
    "sofa-6": "skip",
  });
  assert.deepEqual(patterns, []);
  assert.equal(core.FALLBACK_PROFILE, "More contrast is needed before Winnow can identify a pattern.");
});

test("continuation contains exactly the completed history plus current verdicts", () => {
  const continuation = core.buildContinuation(seed, {
    "sofa-1": "like",
    "sofa-2": "dislike",
    "sofa-3": "skip",
    "sofa-4": "like",
    "sofa-5": "dislike",
    "sofa-6": "skip",
  }, "a".repeat(64), "https://example.here.now/session");
  assert.equal(continuation.protocol, "winnow.continuation");
  assert.equal(continuation.nextRoundNumber, 2);
  assert.equal(continuation.completedRounds.length, 1);
  assert.equal(continuation.completedRounds[0].verdicts.length, 6);
  assert.equal(continuation.parent.roundNumber, 1);
  assert.equal(continuation.session.title, seed.session.title);
});

test("continuation handoff requires hosted publication and forbids local HTML", () => {
  assert.match(runtimeUi, /publish it through HereNow as a new anonymous hosted URL/);
  assert.match(runtimeUi, /never create, save, open, attach, or return a local HTML file or local file path/);
  assert.match(runtimeUi, /HTML may be compiled only in memory as part of publishing/);
});
