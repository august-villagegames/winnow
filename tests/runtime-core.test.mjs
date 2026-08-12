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
  assert.ok(patterns.some((pattern) => pattern.factorId === "covers" && pattern.supportCount === 3));
  assert.ok(patterns.every((pattern) => pattern.strength >= 0.2));
  const price = patterns.find((pattern) => pattern.factorId === "price" && pattern.polarity === "like");
  const covers = patterns.find((pattern) => pattern.factorId === "covers" && pattern.polarity === "like");
  assert.equal(price.compactLabel, "Higher prices");
  assert.equal(price.key, core.profilePatternKey("price", "like", "higher", null));
  assert.equal(covers.compactLabel, "Removable covers");
  assert.equal(covers.key, core.profilePatternKey("covers", "like", "include", true));
});

test("profile keys preserve semantic values and the profile remains bounded to six patterns", () => {
  const normalized = core.profilePatternKey("arms", "like", "include", " Track   Arms ");
  assert.equal(normalized, core.profilePatternKey("arms", "like", "include", "track arms"));

  const expanded = core.clone(seed);
  for (let index = 0; index < 7; index += 1) {
    const factorId = `signal-${index}`;
    expanded.round.factors.push({ id: factorId, label: `Signal ${index}`, valueType: "boolean", display: { style: "boolean", trueLabel: "Enabled", falseLabel: "Disabled" } });
    expanded.round.options.forEach((option, optionIndex) => option.values.push({ factorId, value: optionIndex < 3, sourceId: option.primarySourceId }));
  }
  const patterns = core.computeProfile(expanded, {
    "sofa-1": "like",
    "sofa-2": "dislike",
    "sofa-3": "like",
    "sofa-4": "dislike",
    "sofa-5": "like",
    "sofa-6": "dislike",
  });
  assert.equal(patterns.length, 6);
  assert.ok(patterns.every((pattern) => pattern.key && pattern.compactLabel));
});

test("active profile guidance omits only the excluded insight", () => {
  const patterns = core.computeProfile(seed, {
    "sofa-1": "like",
    "sofa-2": "dislike",
    "sofa-3": "like",
    "sofa-4": "dislike",
    "sofa-5": "like",
    "sofa-6": "skip",
  });
  const excluded = patterns.find((pattern) => pattern.factorId === "covers" && pattern.polarity === "like");
  const active = core.activeProfilePatterns(patterns, [excluded.key]);
  assert.equal(active.length, patterns.length - 1);
  assert.ok(!active.some((pattern) => pattern.key === excluded.key));
});

test("excluding a primary-factor insight never removes the primary factor", () => {
  const decisions = {
    "sofa-1": "like",
    "sofa-2": "dislike",
    "sofa-3": "like",
    "sofa-4": "dislike",
    "sofa-5": "like",
    "sofa-6": "skip",
  };
  const pricePattern = core.computeProfile(seed, decisions).find((pattern) => pattern.factorId === "price" && pattern.polarity === "like");
  const continuation = core.buildContinuation(seed, decisions, "b".repeat(64), "https://example.here.now/session", [pricePattern.key]);
  assert.ok(!core.activeProfilePatterns([pricePattern], [pricePattern.key]).length);
  assert.equal(seed.session.primaryFactorId, "price");
  assert.ok(continuation.completedRounds[0].factors.some((factor) => factor.id === "price"));
  assert.ok(continuation.completedRounds[0].options.every((option) => option.values.some((value) => value.factorId === "price")));
});

test("profile requires two supporting selections for either polarity", () => {
  const patterns = core.computeProfile(seed, {
    "sofa-1": "like",
    "sofa-2": "dislike",
    "sofa-3": "skip",
    "sofa-4": "skip",
    "sofa-5": "skip",
    "sofa-6": "skip",
  });
  assert.deepEqual(patterns, []);
});

test("a single like does not create a like pattern when dislikes are plentiful", () => {
  const patterns = core.computeProfile(seed, {
    "sofa-1": "like",
    "sofa-2": "dislike",
    "sofa-3": "dislike",
    "sofa-4": "dislike",
    "sofa-5": "dislike",
    "sofa-6": "skip",
  });
  assert.ok(patterns.length > 0);
  assert.ok(patterns.every((pattern) => pattern.polarity === "dislike"));
  assert.ok(patterns.every((pattern) => !pattern.text.startsWith("1 liked")));
});

test("profile reports counted like and dislike patterns without requiring both sides", () => {
  const patterns = core.computeProfile(seed, {
    "sofa-1": "like",
    "sofa-2": "dislike",
    "sofa-3": "like",
    "sofa-4": "dislike",
    "sofa-5": "skip",
    "sofa-6": "skip",
  });
  assert.ok(patterns.some((pattern) => pattern.polarity === "like" && pattern.text === "2 liked options include covers" && pattern.supportCount === 2));
  assert.ok(patterns.some((pattern) => pattern.polarity === "dislike" && pattern.text === "2 disliked options exclude covers" && pattern.supportCount === 2));
});

test("numeric one-sided patterns use a counted average", () => {
  const patterns = core.computeProfile(seed, {
    "sofa-1": "like",
    "sofa-2": "skip",
    "sofa-3": "like",
    "sofa-4": "skip",
    "sofa-5": "skip",
    "sofa-6": "skip",
  });
  assert.ok(patterns.some((pattern) => pattern.factorId === "price" && pattern.polarity === "like" && pattern.text === "2 liked options average price of $1,870"));
});

test("profile counts matching evidence across rounds", () => {
  const continued = core.clone(seed);
  const completedRound = core.clone(seed.round);
  completedRound.verdicts = seed.round.options.map((option) => ({ optionId: option.id, decision: option.id === "sofa-1" ? "like" : "skip" }));
  continued.history = [completedRound];
  continued.round.number = 2;
  const patterns = core.computeProfile(continued, { "sofa-3": "like" });
  assert.ok(patterns.some((pattern) => pattern.factorId === "covers" && pattern.polarity === "like" && pattern.supportCount === 2));
});

test("profile falls back when there is no repeated evidence", () => {
  const patterns = core.computeProfile(seed, {
    "sofa-1": "like",
    "sofa-2": "skip",
    "sofa-3": "skip",
    "sofa-4": "skip",
    "sofa-5": "skip",
    "sofa-6": "skip",
  });
  assert.deepEqual(patterns, []);
  assert.equal(core.FALLBACK_PROFILE, "More contrast is needed before Winnow can identify a pattern.");
});

test("continuation contains current profile exclusions alongside the immutable parent snapshot", () => {
  const exclusion = core.profilePatternKey("covers", "like", "include", true);
  const continuation = core.buildContinuation(seed, {
    "sofa-1": "like",
    "sofa-2": "dislike",
    "sofa-3": "skip",
    "sofa-4": "like",
    "sofa-5": "dislike",
    "sofa-6": "skip",
  }, "a".repeat(64), "https://example.here.now/session", [exclusion]);
  assert.equal(continuation.protocol, "winnow.continuation");
  assert.equal(continuation.nextRoundNumber, 2);
  assert.equal(continuation.completedRounds.length, 1);
  assert.equal(continuation.completedRounds[0].verdicts.length, 6);
  assert.equal(continuation.parent.roundNumber, 1);
  assert.equal(continuation.session.title, seed.session.title);
  assert.deepEqual(continuation.parentProfileExclusions, []);
  assert.deepEqual(continuation.profileExclusions, [exclusion]);
});

test("continuation handoff uses selected profile guidance and keeps the controls persistent", () => {
  assert.match(runtimeUi, /publish it through HereNow as a new anonymous hosted URL/);
  assert.match(runtimeUi, /never create, save, open, attach, or return a local HTML file or local file path/);
  assert.match(runtimeUi, /HTML may be compiled only in memory as part of publishing/);
  assert.match(runtimeUi, /data-profile-toggle/);
  assert.match(runtimeUi, /No profile patterns are selected for the next round/);
  assert.match(runtimeUi, /profileExclusions/);
});

test("image viewer keeps the existing carousel and uses accessible viewer hooks", () => {
  assert.match(runtimeUi, /data-viewer-open/);
  assert.match(runtimeUi, /data-image-viewer/);
  assert.match(runtimeUi, /data-viewer-image/);
  assert.match(runtimeUi, /data-viewer-prev/);
  assert.match(runtimeUi, /data-viewer-next/);
  assert.match(runtimeUi, /data-viewer-close/);
  assert.match(runtimeUi, /data-carousel-index/);
  assert.match(runtimeUi, /showModal\(\)/);
  assert.match(runtimeUi, /event\.key === "Escape"|cancel/);
  assert.match(runtimeUi, /data-carousel/);
  assert.match(runtimeUi, /referrerpolicy="no-referrer"/);
});
