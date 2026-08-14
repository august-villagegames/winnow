import test from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const require = createRequire(import.meta.url);
const core = require("../.agents/skills/winnow/assets/runtime-core.js");
const root = dirname(fileURLToPath(import.meta.url));
const seed = JSON.parse(readFileSync(join(root, "../fixtures/synthetic-seed.json"), "utf8"));
const runtimeUi = readFileSync(join(root, "../.agents/skills/winnow/assets/runtime-ui.js"), "utf8");
const runtimeCore = readFileSync(join(root, "../.agents/skills/winnow/assets/runtime-core.js"), "utf8");

test("typed formatting is runtime-owned", () => {
  const price = seed.round.factors.find((factor) => factor.id === "price");
  const covers = seed.round.factors.find((factor) => factor.id === "covers");
  const delivery = { valueType: "number", display: { style: "duration", unit: "week" } };
  assert.equal(core.formatValue(price, 1780), "$1,780");
  assert.equal(core.formatValue(covers, true), "Removable covers");
  assert.equal(core.formatValue(delivery, 2), "2 weeks");
});

test("carousel media height preserves ratios within the stage budget", () => {
  assert.equal(core.computeCarouselMediaHeight({ availableWidth: 440, naturalWidth: 1600, naturalHeight: 900, stageBudget: 600 }), 248);
  assert.equal(core.computeCarouselMediaHeight({ availableWidth: 440, naturalWidth: 400, naturalHeight: 1200, stageBudget: 600 }), 420);
  assert.equal(core.computeCarouselMediaHeight({ availableWidth: 440, naturalWidth: 3000, naturalHeight: 100, stageBudget: 600 }), 128);
  assert.equal(core.computeCarouselMediaHeight({ availableWidth: 440, naturalWidth: 1600, naturalHeight: 900, stageBudget: 90 }), 90);
  assert.equal(core.computeCarouselMediaHeight({ availableWidth: 300, naturalWidth: 1600, naturalHeight: 900, stageBudget: 600 }), 169);
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
  assert.ok(patterns.some((pattern) => pattern.factorId === "covers" && pattern.text.includes("reactions favor removable covers")));
  assert.ok(patterns.some((pattern) => pattern.factorId === "covers" && pattern.supportCount === 5));
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

test("runtime profile validation rejects text-factor patterns", () => {
  const invalid = core.clone(seed);
  invalid.round.factors.find((factor) => factor.id === "material").valueType = "text";
  invalid.profilePatterns = [{
    key: core.profilePatternKey("material", "like", "include", "leather"),
    factorId: "material",
    polarity: "like",
    direction: "include",
    value: "leather",
    mean: null,
    supportCount: 2,
    strength: 1,
  }];
  assert.match(core.validateRuntimeSeed(invalid).join(";"), /invalid profile patterns/);
});

test("category candidates merge case and whitespace variants by semantic key", () => {
  const variant = core.clone(seed);
  variant.round.options.find((option) => option.id === "sofa-5").values.find((value) => value.factorId === "seats").value = " 3-SEAT ";
  const patterns = core.computeProfileCandidates(variant, {
    "sofa-1": "like",
    "sofa-2": "dislike",
    "sofa-3": "like",
    "sofa-4": "dislike",
    "sofa-5": "like",
    "sofa-6": "skip",
  });
  const seats = patterns.find((pattern) => pattern.factorId === "seats" && pattern.polarity === "like");
  assert.equal(seats.supportCount, 3);
  assert.equal(seats.key, core.profilePatternKey("seats", "like", "include", "3-seat"));
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

test("category patterns require two direct selections", () => {
  const patterns = core.computeProfile(seed, {
    "sofa-1": "like",
    "sofa-2": "dislike",
    "sofa-3": "skip",
    "sofa-4": "skip",
    "sofa-5": "skip",
    "sofa-6": "skip",
  });
  assert.deepEqual(patterns.filter((pattern) => pattern.factorId === "seats"), []);
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

test("boolean reactions that favor the same value combine into one preference", () => {
  const decisions = {
    "sofa-1": "like",
    "sofa-2": "dislike",
    "sofa-3": "like",
    "sofa-4": "dislike",
    "sofa-5": "skip",
    "sofa-6": "skip",
  };
  const candidates = core.computeProfileCandidates(seed, decisions).filter((pattern) => pattern.factorId === "covers");
  const patterns = core.computeProfile(seed, decisions).filter((pattern) => pattern.factorId === "covers");
  assert.equal(candidates.length, 1);
  assert.equal(patterns.length, 1);
  assert.equal(patterns[0].key, candidates[0].key);
  assert.equal(patterns[0].value, true);
  assert.equal(patterns[0].supportCount, 4);
  assert.equal(patterns[0].strength, 1);
});

test("boolean preferences require a two-thirds majority of reaction votes", () => {
  const tie = core.computeProfile(seed, {
    "sofa-1": "like",
    "sofa-2": "like",
    "sofa-3": "dislike",
    "sofa-4": "dislike",
    "sofa-5": "skip",
    "sofa-6": "skip",
  }).filter((pattern) => pattern.factorId === "covers");
  assert.deepEqual(tie, []);

  const threeToTwo = core.computeProfile(seed, {
    "sofa-1": "like",
    "sofa-2": "dislike",
    "sofa-3": "like",
    "sofa-4": "like",
    "sofa-5": "dislike",
    "sofa-6": "skip",
  }).filter((pattern) => pattern.factorId === "covers");
  assert.deepEqual(threeToTwo, []);

  const fourToTwo = core.computeProfile(seed, {
    "sofa-1": "like",
    "sofa-2": "dislike",
    "sofa-3": "like",
    "sofa-4": "dislike",
    "sofa-5": "dislike",
    "sofa-6": "like",
  }).filter((pattern) => pattern.factorId === "covers");
  assert.equal(fourToTwo.length, 1);
  assert.equal(fourToTwo[0].value, true);
  assert.equal(fourToTwo[0].supportCount, 4);
  assert.equal(fourToTwo[0].strength, 2 / 3);

  const fourToTwoFalse = core.computeProfile(seed, {
    "sofa-1": "dislike",
    "sofa-2": "like",
    "sofa-3": "dislike",
    "sofa-4": "like",
    "sofa-5": "like",
    "sofa-6": "dislike",
  }).filter((pattern) => pattern.factorId === "covers");
  assert.equal(fourToTwoFalse.length, 1);
  assert.equal(fourToTwoFalse[0].value, false);
  assert.equal(fourToTwoFalse[0].polarity, "like");
  assert.equal(fourToTwoFalse[0].supportCount, 4);
});

test("numeric and boolean factors keep one preference while categories retain multiple values", () => {
  const expanded = core.clone(seed);
  expanded.round.factors.push({ id: "finish", label: "Finish", valueType: "category", display: { style: "text" } });
  expanded.round.options.forEach((option, index) => option.values.push({
    factorId: "finish",
    value: index < 3 ? "Velvet" : "Linen",
    sourceId: option.primarySourceId,
  }));
  const decisions = {
    "sofa-1": "like",
    "sofa-2": "like",
    "sofa-3": "like",
    "sofa-4": "dislike",
    "sofa-5": "dislike",
    "sofa-6": "dislike",
  };
  const candidates = core.computeProfileCandidates(expanded, decisions);
  const patterns = core.computeProfile(expanded, decisions);
  for (const factorId of ["price", "covers"]) {
    const factorCandidates = candidates.filter((pattern) => pattern.factorId === factorId);
    const factorPatterns = patterns.filter((pattern) => pattern.factorId === factorId);
    assert.ok(factorCandidates.length >= 1);
    assert.equal(factorPatterns.length, 1);
    assert.equal(factorPatterns[0].key, factorCandidates[0].key);
  }
  assert.deepEqual(
    patterns.filter((pattern) => pattern.factorId === "finish").map((pattern) => pattern.value).sort(),
    ["Linen", "Velvet"],
  );
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

test("persisted numeric patterns survive when current evidence no longer qualifies", () => {
  const decisions = {
    "sofa-1": "like",
    "sofa-2": "dislike",
    "sofa-3": "like",
    "sofa-4": "dislike",
    "sofa-5": "like",
    "sofa-6": "skip",
  };
  const candidate = core.computeProfileCandidates(seed, decisions).find((pattern) => pattern.factorId === "price" && pattern.polarity === "like");
  const continued = core.clone(seed);
  continued.profilePatterns = [core.profilePatternRecord(candidate)];
  const persisted = core.computeProfile(continued, Object.fromEntries(seed.round.options.map((option) => [option.id, "skip"])), continued.profilePatterns, []);
  assert.equal(persisted.length, 1);
  assert.equal(persisted[0].key, candidate.key);
  assert.equal(persisted[0].compactLabel, "Higher prices");
});

test("persisted boolean preferences withdraw when cumulative evidence loses its majority", () => {
  const priorDecisions = {
    "sofa-1": "like",
    "sofa-2": "dislike",
    "sofa-3": "like",
    "sofa-4": "dislike",
    "sofa-5": "skip",
    "sofa-6": "skip",
  };
  const candidate = core.computeProfile(seed, priorDecisions).find((pattern) => pattern.factorId === "covers");
  const continued = core.clone(seed);
  const completedRound = core.clone(seed.round);
  completedRound.verdicts = completedRound.options.map((option) => ({ optionId: option.id, decision: priorDecisions[option.id] }));
  continued.history = [completedRound];
  continued.round.number = 2;
  continued.profilePatterns = [core.profilePatternRecord(candidate)];
  const oppositeDecisions = Object.fromEntries(continued.round.options.map((option) => {
    const covers = option.values.find((value) => value.factorId === "covers").value;
    return [option.id, covers ? "dislike" : "like"];
  }));
  const patterns = core.computeProfile(continued, oppositeDecisions, continued.profilePatterns, []);
  assert.deepEqual(patterns.filter((pattern) => pattern.factorId === "covers"), []);
});

test("legacy boolean exclusions remain effective after normalized vote inference", () => {
  const decisions = {
    "sofa-1": "like",
    "sofa-2": "dislike",
    "sofa-3": "like",
    "sofa-4": "dislike",
    "sofa-5": "skip",
    "sofa-6": "skip",
  };
  const legacyExclusion = core.profilePatternKey("covers", "dislike", "exclude", false);
  const continuation = core.buildContinuation(seed, decisions, "f".repeat(64), "https://example.here.now/round-1", [legacyExclusion]);
  assert.deepEqual(continuation.profilePatterns.filter((pattern) => pattern.factorId === "covers"), []);
  const display = core.computeProfileDisplay(seed, decisions, [], [legacyExclusion]);
  assert.ok(display.some((pattern) => pattern.factorId === "covers"));
  assert.ok(!core.activeProfilePatterns(display, [legacyExclusion]).some((pattern) => pattern.factorId === "covers"));

  const successor = core.clone(seed);
  successor.history = continuation.completedRounds;
  successor.round.number = continuation.nextRoundNumber;
  successor.profilePatterns = continuation.profilePatterns;
  successor.profileExclusions = continuation.profileExclusions;
  const patterns = core.computeProfile(successor, Object.fromEntries(successor.round.options.map((option) => [option.id, "skip"])));
  assert.deepEqual(patterns.filter((pattern) => pattern.factorId === "covers"), []);
});

test("persisted patterns keep priority over six newly inferred candidates", () => {
  const candidate = core.computeProfileCandidates(seed, {
    "sofa-1": "like",
    "sofa-2": "dislike",
    "sofa-3": "like",
    "sofa-4": "dislike",
    "sofa-5": "like",
    "sofa-6": "skip",
  }).find((pattern) => pattern.factorId === "price" && pattern.polarity === "like");
  const persisted = core.profilePatternRecord(candidate);
  const newCandidates = Array.from({ length: 6 }, (_, index) => ({
    ...candidate,
    key: `new-pattern-${index}`,
    factorId: `new-factor-${index}`,
    compactLabel: `New pattern ${index}`,
    text: `New pattern ${index}`,
  }));
  const merged = core.mergeProfilePatterns(seed, [persisted], newCandidates, [], false);
  assert.equal(merged.length, 6);
  assert.equal(merged[0].key, persisted.key);
  assert.equal(merged.filter((pattern) => pattern.key === persisted.key).length, 1);
});

test("eligible candidates fill slots after an excluded high-ranked candidate", () => {
  const expanded = core.clone(seed);
  for (let index = 0; index < 7; index += 1) {
    const factorId = `signal-${index}`;
    expanded.round.factors.push({ id: factorId, label: `Signal ${index}`, valueType: "boolean", display: { style: "boolean", trueLabel: "Enabled", falseLabel: "Disabled" } });
    expanded.round.options.forEach((option, optionIndex) => option.values.push({ factorId, value: optionIndex < 3, sourceId: option.primarySourceId }));
  }
  const decisions = {
    "sofa-1": "like",
    "sofa-2": "dislike",
    "sofa-3": "like",
    "sofa-4": "dislike",
    "sofa-5": "like",
    "sofa-6": "dislike",
  };
  const candidates = core.computeProfileCandidates(expanded, decisions);
  assert.ok(candidates.length > 6);
  const active = core.computeProfile(expanded, decisions, [], [candidates[0].key]);
  assert.equal(active.length, 6);
  assert.ok(!active.some((pattern) => pattern.key === candidates[0].key));
});

test("matching candidates refresh persisted support and numeric means", () => {
  const decisions = {
    "sofa-1": "like",
    "sofa-2": "skip",
    "sofa-3": "like",
    "sofa-4": "skip",
    "sofa-5": "skip",
    "sofa-6": "skip",
  };
  const candidate = core.computeProfileCandidates(seed, decisions).find((pattern) => pattern.factorId === "price" && pattern.direction === "average");
  const persisted = { ...core.profilePatternRecord(candidate), supportCount: 2, strength: 0.25, mean: candidate.mean - 100 };
  const continued = core.clone(seed);
  continued.profilePatterns = [persisted];
  const refreshed = core.computeProfile(continued, decisions, continued.profilePatterns, []);
  assert.equal(refreshed[0].supportCount, candidate.supportCount);
  assert.equal(refreshed[0].mean, candidate.mean);
  assert.match(refreshed[0].text, /\$1,870/);
});

test("legacy exclusive records retain their first persisted preference and normalize continuations", () => {
  const decisions = {
    "sofa-1": "like",
    "sofa-2": "dislike",
    "sofa-3": "like",
    "sofa-4": "dislike",
    "sofa-5": "skip",
    "sofa-6": "skip",
  };
  const priceCandidates = core.computeProfileCandidates(seed, decisions).filter((pattern) => pattern.factorId === "price");
  const continued = core.clone(seed);
  continued.profilePatterns = [
    core.profilePatternRecord(priceCandidates[1]),
    core.profilePatternRecord(priceCandidates[0]),
  ];
  assert.deepEqual(core.validateRuntimeSeed(continued), []);
  const patterns = core.computeProfile(continued, decisions, continued.profilePatterns, []);
  assert.deepEqual(patterns.filter((pattern) => pattern.factorId === "price").map((pattern) => pattern.key), [priceCandidates[1].key]);
  const continuation = core.buildContinuation(continued, decisions, "d".repeat(64), "https://example.here.now/round-1");
  assert.deepEqual(continuation.profilePatterns.filter((pattern) => pattern.factorId === "price").map((pattern) => pattern.key), [priceCandidates[1].key]);
  const excludedDisplay = core.computeProfileDisplay(continued, decisions, continued.profilePatterns, [priceCandidates[1].key]);
  assert.deepEqual(excludedDisplay.filter((pattern) => pattern.factorId === "price").map((pattern) => pattern.key), [priceCandidates[1].key]);
});

test("excluded exclusive records reserve the current summary and later allow a different candidate", () => {
  const decisions = {
    "sofa-1": "like",
    "sofa-2": "dislike",
    "sofa-3": "like",
    "sofa-4": "dislike",
    "sofa-5": "skip",
    "sofa-6": "skip",
  };
  const priceCandidates = core.computeProfileCandidates(seed, decisions).filter((pattern) => pattern.factorId === "price");
  const excluded = priceCandidates[1];
  const active = core.computeProfile(seed, decisions, [], [excluded.key]);
  const display = core.computeProfileDisplay(seed, decisions, [], [excluded.key]);
  assert.deepEqual(active.filter((pattern) => pattern.factorId === "price"), []);
  assert.deepEqual(display.filter((pattern) => pattern.factorId === "price").map((pattern) => pattern.key), [excluded.key]);

  const continuation = core.buildContinuation(seed, decisions, "e".repeat(64), "https://example.here.now/round-1", [excluded.key]);
  assert.deepEqual(continuation.profilePatterns.filter((pattern) => pattern.factorId === "price"), []);
  const successor = core.clone(seed);
  successor.history = continuation.completedRounds;
  successor.round.number = continuation.nextRoundNumber;
  successor.profilePatterns = continuation.profilePatterns;
  successor.profileExclusions = continuation.profileExclusions;
  successor.round.options.forEach((option) => {
    option.values.find((value) => value.factorId === "price").value = 1000;
  });
  const laterPatterns = core.computeProfile(
    successor,
    Object.fromEntries(successor.round.options.map((option) => [option.id, "like"])),
    successor.profilePatterns,
    successor.profileExclusions,
  ).filter((pattern) => pattern.factorId === "price");
  assert.equal(laterPatterns.length, 1);
  assert.notEqual(laterPatterns[0].key, excluded.key);
});

test("dismissed patterns stay restorable in the current display but never return to guidance", () => {
  const decisions = {
    "sofa-1": "like",
    "sofa-2": "dislike",
    "sofa-3": "like",
    "sofa-4": "dislike",
    "sofa-5": "like",
    "sofa-6": "skip",
  };
  const candidate = core.computeProfileCandidates(seed, decisions).find((pattern) => pattern.factorId === "covers" && pattern.polarity === "like");
  const continued = core.clone(seed);
  continued.profilePatterns = [core.profilePatternRecord(candidate)];
  const excluded = core.computeProfile(continued, decisions, continued.profilePatterns, [candidate.key]);
  const display = core.computeProfileDisplay(continued, decisions, continued.profilePatterns, [candidate.key]);
  assert.ok(!excluded.some((pattern) => pattern.key === candidate.key));
  assert.ok(display.some((pattern) => pattern.key === candidate.key));
  assert.ok(core.computeProfile(continued, decisions, continued.profilePatterns, []).some((pattern) => pattern.key === candidate.key));
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
  assert.deepEqual(continuation.parentProfilePatterns, []);
  assert.ok(continuation.profilePatterns.every((pattern) => pattern.key !== exclusion));
});

test("continuation handoff uses selected profile guidance and keeps the controls persistent", () => {
  assert.match(runtimeUi, /publish it through HereNow as a new anonymous hosted URL/);
  assert.match(runtimeUi, /never create, save, open, attach, or return a local HTML file or local file path/);
  assert.match(runtimeUi, /HTML may be compiled only in memory as part of publishing/);
  assert.match(runtimeUi, /data-profile-toggle/);
  assert.match(runtimeUi, /profileExclusions/);
  assert.match(runtimeUi, /Core\.activeProfilePatterns\(\[pattern\], state\.profileExclusions\)/);
  assert.match(runtimeUi, /profilePatterns/);
  assert.match(runtimeUi, /Copy continuation\.profilePatterns exactly/);
  assert.match(runtimeCore, /No profile patterns are selected for the next round/);
});

test("round one continuation state renders in round two and supplies clipboard guidance", () => {
  const decisions = {
    "sofa-1": "like",
    "sofa-2": "dislike",
    "sofa-3": "like",
    "sofa-4": "dislike",
    "sofa-5": "like",
    "sofa-6": "skip",
  };
  const continuation = core.buildContinuation(seed, decisions, "c".repeat(64), "https://example.here.now/round-1", [], core.computeProfile(seed, decisions, [], []));
  const successor = core.clone(seed);
  successor.history = continuation.completedRounds;
  successor.round.number = continuation.nextRoundNumber;
  successor.profileExclusions = continuation.profileExclusions;
  successor.profilePatterns = continuation.profilePatterns;
  const renderedPatterns = core.computeProfileDisplay(successor, Object.fromEntries(successor.round.options.map((option) => [option.id, "skip"])), successor.profilePatterns, successor.profileExclusions);
  const persisted = renderedPatterns.find((pattern) => pattern.key === continuation.profilePatterns[0].key);
  assert.ok(persisted);
  const guidance = core.profileGuidance(renderedPatterns, successor.profileExclusions);
  assert.match(guidance, /selected profile patterns/);
  assert.match(guidance, new RegExp(persisted.text));
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
