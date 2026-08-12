(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.WinnowCore = factory();
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const PROTOCOL = "winnow.portable-session";
  const CONTINUATION_PROTOCOL = "winnow.continuation";
  const SCHEMA_VERSION = 4;
  const RUNTIME_VERSION = "4.0.0";
  const FALLBACK_PROFILE = "More contrast is needed before Winnow can identify a pattern.";
  const MIN_PATTERN_SUPPORT = 2;
  const MAX_PROFILE_PATTERNS = 6;

  function clone(value) {
    return value == null ? value : JSON.parse(JSON.stringify(value));
  }

  function stable(value) {
    if (Array.isArray(value)) return value.map(stable);
    if (value && typeof value === "object") {
      return Object.fromEntries(Object.keys(value).sort().map((key) => [key, stable(value[key])]));
    }
    return value;
  }

  function canonicalJson(value) {
    return JSON.stringify(stable(value));
  }

  function normalizeUrl(value) {
    try {
      const url = new URL(value);
      url.hash = "";
      return url.href;
    } catch (_) {
      return String(value || "");
    }
  }

  function formatNumber(value, maximumFractionDigits) {
    return new Intl.NumberFormat("en-US", { maximumFractionDigits }).format(value);
  }

  function formatValue(factor, value) {
    if (!factor || value === undefined || value === null) return "";
    const display = factor.display || { style: "text" };
    if (factor.valueType === "boolean") {
      return value ? display.trueLabel : display.falseLabel;
    }
    if (factor.valueType === "number") {
      if (display.style === "currency") return new Intl.NumberFormat("en-US", { style: "currency", currency: display.currency, maximumFractionDigits: 0 }).format(value);
      if (display.style === "percent") return `${formatNumber(value, 1)}%`;
      if (display.style === "duration") {
        const unit = display.unit;
        return `${formatNumber(value, 1)} ${unit}${value === 1 ? "" : "s"}`;
      }
      return `${formatNumber(value, display.style === "decimal" ? 2 : 2)}${display.unit ? ` ${display.unit}` : ""}`;
    }
    return String(value);
  }

  function factorDefinition(factor) {
    return JSON.stringify({ id: factor.id, label: factor.label, valueType: factor.valueType, display: factor.display });
  }

  function factorMap(round) {
    return Object.fromEntries((round && round.factors || []).map((factor) => [factor.id, factor]));
  }

  function optionMap(round) {
    return Object.fromEntries((round && round.options || []).map((option) => [option.id, option]));
  }

  function valueMap(option) {
    return Object.fromEntries((option && option.values || []).map((item) => [item.factorId, item.value]));
  }

  function currentDecisionMap(events) {
    return Object.fromEntries((events || []).filter((event) => event && event.type === "verdict").map((event) => [event.optionId, event.decision]));
  }

  function seedRounds(seed, currentVerdicts) {
    const current = clone(seed.round);
    current.verdicts = (current.options || []).map((option) => ({ optionId: option.id, decision: currentVerdicts[option.id] })).filter((verdict) => verdict.decision);
    return [...(seed.history || []).map(clone), current];
  }

  function validateRuntimeSeed(seed) {
    const errors = [];
    if (!seed || typeof seed !== "object") return ["seed must be an object"];
    if (seed.protocol !== PROTOCOL) errors.push("unsupported protocol");
    if (seed.schemaVersion !== SCHEMA_VERSION) errors.push("unsupported schema version");
    if (seed.runtimeVersion !== RUNTIME_VERSION) errors.push("unsupported runtime version");
    if (!seed.session || typeof seed.session !== "object") errors.push("missing session");
    if (!seed.round || typeof seed.round !== "object") errors.push("missing round");
    if (!validProfileExclusions(seed.profileExclusions)) errors.push("invalid profile exclusions");
    if (!validProfilePatterns(seed.profilePatterns, seed, seed.profileExclusions)) errors.push("invalid profile patterns");
    if (Array.isArray(seed.history) && seed.round && seed.round.number !== seed.history.length + 1) errors.push("round number is not contiguous");
    if (seed.session && seed.session.primaryFactorId) {
      const rounds = [...(seed.history || []), seed.round];
      if (rounds.some((round) => !(round.factors || []).some((factor) => factor.id === seed.session.primaryFactorId))) errors.push("primary factor is missing from a round");
    }
    return errors;
  }

  function assertRuntimeSeed(seed) {
    const errors = validateRuntimeSeed(seed);
    if (errors.length) throw new Error(errors.join("; "));
    return seed;
  }

  function valueComparable(value) {
    return value !== undefined && value !== null && value !== "unknown";
  }

  function labelNoun(label) {
    const lower = String(label || "factor").toLowerCase();
    if (lower === "price") return "prices";
    if (lower.endsWith("s")) return lower;
    return lower;
  }

  function normalizePatternValue(value) {
    if (typeof value === "string") return value.normalize("NFKC").trim().replace(/\s+/g, " ").toLowerCase();
    if (typeof value === "boolean") return value;
    return null;
  }

  function profilePatternKey(factorId, polarity, direction, value) {
    return canonicalJson({ factorId, polarity, direction, value: normalizePatternValue(value) });
  }

  function validProfileExclusions(value) {
    return Array.isArray(value) && value.every((item) => typeof item === "string" && item.length > 0 && item.length <= 500) && new Set(value).size === value.length;
  }

  function patternSupportPhrase(decision, count) {
    return `${count} ${decision === "like" ? "liked" : "disliked"} options`;
  }

  function patternTone(decision) {
    return decision === "like" ? "positive" : "negative";
  }

  function patternIcon(decision) {
    return decision === "like" ? "trending-up" : "trending-down";
  }

  function patternMeta(factor, decision, supportCount, strength, direction, value, mean, compactLabel) {
    return {
      factorId: factor.id,
      polarity: decision,
      supportCount,
      strength,
      direction,
      value,
      mean,
      key: profilePatternKey(factor.id, decision, direction, value),
      compactLabel,
      tone: patternTone(decision),
      icon: patternIcon(decision),
    };
  }

  function categoryPattern(factor, decision, value, supportCount, strength) {
    const prefix = patternSupportPhrase(decision, supportCount);
    if (factor.valueType === "boolean") {
      const action = Boolean(value) ? "include" : "exclude";
      return {
        ...patternMeta(factor, decision, supportCount, strength, action, Boolean(value), null, formatValue(factor, Boolean(value))),
        text: `${prefix} ${action} ${String(factor.label).toLowerCase()}`,
      };
    }
    return {
      ...patternMeta(factor, decision, supportCount, strength, "include", value, null, formatValue(factor, value)),
      text: `${prefix} include ${String(value).toLowerCase()}`,
    };
  }

  function numericPattern(factor, decision, values, oppositeValues) {
    if (values.length < MIN_PATTERN_SUPPORT) return null;
    const mean = values.reduce((total, value) => total + value, 0) / values.length;
    const prefix = patternSupportPhrase(decision, values.length);
    const allValues = [...values, ...oppositeValues];
    const min = Math.min(...allValues);
    const max = Math.max(...allValues);
    if (oppositeValues.length >= MIN_PATTERN_SUPPORT && max !== min) {
      const oppositeMean = oppositeValues.reduce((total, value) => total + value, 0) / oppositeValues.length;
      const difference = Math.abs(mean - oppositeMean) / (max - min);
      if (difference >= .20) {
        const lower = mean < oppositeMean;
        return {
          ...patternMeta(factor, decision, values.length, difference, lower ? "lower" : "higher", null, null, `${lower ? "Lower" : "Higher"} ${labelNoun(factor.label)}`),
          text: `${prefix} trend toward ${lower ? "lower" : "higher"} ${labelNoun(factor.label)}`,
        };
      }
    }
    return {
      ...patternMeta(factor, decision, values.length, 1, "average", null, mean, `Average ${String(factor.label).toLowerCase()} ${formatValue(factor, mean)}`),
      text: `${prefix} average ${String(factor.label).toLowerCase()} of ${formatValue(factor, mean)}`,
    };
  }

  function patternForFactor(factor, rows) {
    const reacted = rows.filter((row) => valueComparable(row.value) && (row.decision === "like" || row.decision === "dislike"));
    const patterns = [];
    const decisions = ["like", "dislike"];
    for (const decision of decisions) {
      const sameDecision = reacted.filter((row) => row.decision === decision);
      if (factor.valueType === "number") {
        const values = sameDecision.map((row) => row.value).filter((value) => typeof value === "number" && Number.isFinite(value));
        const oppositeValues = reacted.filter((row) => row.decision !== decision).map((row) => row.value).filter((value) => typeof value === "number" && Number.isFinite(value));
        const pattern = numericPattern(factor, decision, values, oppositeValues);
        if (pattern) patterns.push(pattern);
        continue;
      }
      if (factor.valueType !== "boolean" && factor.valueType !== "category") continue;
      const groups = new Map();
      for (const row of sameDecision) {
        const key = JSON.stringify(normalizePatternValue(row.value));
        if (!groups.has(key)) groups.set(key, { value: row.value, rows: [] });
        groups.get(key).rows.push(row);
      }
      const opposite = reacted.filter((row) => row.decision !== decision);
      const decisionCount = sameDecision.length;
      const oppositeCount = opposite.length;
      for (const [key, group] of groups.entries()) {
        if (group.rows.length < MIN_PATTERN_SUPPORT) continue;
        const oppositeMatches = opposite.filter((row) => JSON.stringify(normalizePatternValue(row.value)) === key).length;
        const difference = Math.abs(group.rows.length / decisionCount - (oppositeCount ? oppositeMatches / oppositeCount : 0));
        if (oppositeCount && difference < .25) continue;
        patterns.push(categoryPattern(factor, decision, group.value, group.rows.length, difference || 1));
      }
    }
    return patterns;
  }

  function profileFactorMap(seed) {
    const factors = {};
    for (const round of [...(seed.history || []), seed.round].filter(Boolean)) {
      for (const factor of round.factors || []) factors[factor.id] = factor;
    }
    return factors;
  }

  function patternView(seed, record) {
    const factor = profileFactorMap(seed)[record.factorId];
    if (!factor) return null;
    const prefix = patternSupportPhrase(record.polarity, record.supportCount);
    if (factor.valueType === "number") {
      if (record.direction === "average") {
        return {
          ...record,
          compactLabel: `Average ${String(factor.label).toLowerCase()} ${formatValue(factor, record.mean)}`,
          tone: patternTone(record.polarity),
          icon: patternIcon(record.polarity),
          text: `${prefix} average ${String(factor.label).toLowerCase()} of ${formatValue(factor, record.mean)}`,
        };
      }
      const lower = record.direction === "lower";
      return {
        ...record,
        compactLabel: `${lower ? "Lower" : "Higher"} ${labelNoun(factor.label)}`,
        tone: patternTone(record.polarity),
        icon: patternIcon(record.polarity),
        text: `${prefix} trend toward ${lower ? "lower" : "higher"} ${labelNoun(factor.label)}`,
      };
    }
    if (factor.valueType === "boolean") {
      const action = Boolean(record.value) ? "include" : "exclude";
      return {
        ...record,
        compactLabel: formatValue(factor, Boolean(record.value)),
        tone: patternTone(record.polarity),
        icon: patternIcon(record.polarity),
        text: `${prefix} ${action} ${String(factor.label).toLowerCase()}`,
      };
    }
    return {
      ...record,
      compactLabel: formatValue(factor, record.value),
      tone: patternTone(record.polarity),
      icon: patternIcon(record.polarity),
      text: `${prefix} include ${String(record.value).toLowerCase()}`,
    };
  }

  function profilePatternRecord(pattern) {
    return {
      key: pattern.key,
      factorId: pattern.factorId,
      polarity: pattern.polarity,
      direction: pattern.direction,
      value: pattern.value,
      mean: pattern.mean,
      supportCount: pattern.supportCount,
      strength: pattern.strength,
    };
  }

  function validProfilePatternRecord(record, factors) {
    if (!record || typeof record !== "object") return false;
    const allowed = ["key", "factorId", "polarity", "direction", "value", "mean", "supportCount", "strength"];
    if (Object.keys(record).sort().join("|") !== allowed.slice().sort().join("|")) return false;
    if (typeof record.key !== "string" || !record.key || record.key.length > 500) return false;
    if (!factors[record.factorId] || !["like", "dislike"].includes(record.polarity)) return false;
    if (!Number.isInteger(record.supportCount) || record.supportCount < MIN_PATTERN_SUPPORT) return false;
    if (typeof record.strength !== "number" || !Number.isFinite(record.strength) || record.strength < 0 || record.strength > 1) return false;
    if (profilePatternKey(record.factorId, record.polarity, record.direction, record.value) !== record.key) return false;
    const factorType = factors[record.factorId].valueType;
    if (factorType === "number") {
      if (!["lower", "higher", "average"].includes(record.direction) || record.value !== null) return false;
      if (record.direction === "average") return typeof record.mean === "number" && Number.isFinite(record.mean);
      return record.mean === null;
    }
    if (record.mean !== null) return false;
    if (factorType === "boolean") return ["include", "exclude"].includes(record.direction) && typeof record.value === "boolean";
    return factorType === "category" && record.direction === "include" && typeof record.value === "string" && Boolean(record.value.trim());
  }

  function validProfilePatterns(value, seed, profileExclusions) {
    if (!Array.isArray(value) || value.length > MAX_PROFILE_PATTERNS) return false;
    const factors = profileFactorMap(seed);
    const exclusions = new Set(profileExclusions || []);
    const keys = new Set();
    return value.every((record) => {
      if (!validProfilePatternRecord(record, factors) || keys.has(record.key) || exclusions.has(record.key)) return false;
      keys.add(record.key);
      return true;
    });
  }

  function computeProfileCandidates(seed, currentVerdicts) {
    const verdictMap = currentVerdicts || {};
    const allRows = [];
    const currentFactorOrder = (seed.round.factors || []).map((factor) => factor.id);
    const rounds = seedRounds(seed, verdictMap);
    for (const round of rounds) {
      const factors = factorMap(round);
      const options = optionMap(round);
      const verdicts = Object.fromEntries((round.verdicts || []).map((verdict) => [verdict.optionId, verdict.decision]));
      for (const [optionId, decision] of Object.entries(verdicts)) {
        if (decision === "skip") continue;
        const option = options[optionId];
        if (!option) continue;
        const values = valueMap(option);
        for (const [factorId, factor] of Object.entries(factors)) {
          if (valueComparable(values[factorId])) allRows.push({ factorId, factor, value: values[factorId], decision });
        }
      }
    }
    const byFactor = new Map();
    for (const row of allRows) {
      if (!byFactor.has(row.factorId)) byFactor.set(row.factorId, { factor: row.factor, rows: [] });
      byFactor.get(row.factorId).rows.push(row);
    }
    const order = new Map(currentFactorOrder.map((id, index) => [id, index]));
    const patterns = [...byFactor.values()].flatMap(({ factor, rows }) => patternForFactor(factor, rows));
    patterns.sort((left, right) => right.supportCount - left.supportCount || right.strength - left.strength || (order.get(left.factorId) ?? 999) - (order.get(right.factorId) ?? 999) || left.factorId.localeCompare(right.factorId) || left.polarity.localeCompare(right.polarity));
    return patterns;
  }

  function mergeProfilePatterns(seed, persistedPatterns, candidates, profileExclusions, includeExcluded) {
    const excluded = new Set(profileExclusions || []);
    const candidateByKey = new Map((candidates || []).map((pattern) => [pattern.key, pattern]));
    const active = [];
    const excludedViews = [];
    const seen = new Set();
    const seenExcluded = new Set();
    const addExcluded = (pattern) => {
      if (!seenExcluded.has(pattern.key)) {
        seenExcluded.add(pattern.key);
        excludedViews.push(pattern);
      }
    };
    for (const record of persistedPatterns || []) {
      const pattern = candidateByKey.get(record.key) || patternView(seed, record);
      if (!pattern) continue;
      if (excluded.has(record.key)) {
        if (includeExcluded) addExcluded(pattern);
        continue;
      }
      if (!seen.has(pattern.key) && active.length < MAX_PROFILE_PATTERNS) {
        seen.add(pattern.key);
        active.push(pattern);
      }
    }
    for (const pattern of candidates || []) {
      if (excluded.has(pattern.key)) {
        if (includeExcluded) addExcluded(pattern);
        continue;
      }
      if (seen.has(pattern.key)) continue;
      if (active.length >= MAX_PROFILE_PATTERNS) continue;
      seen.add(pattern.key);
      active.push(pattern);
    }
    return includeExcluded ? [...active, ...excludedViews] : active;
  }

  function computeProfile(seed, currentVerdicts, persistedPatterns, profileExclusions) {
    const persisted = persistedPatterns === undefined ? seed.profilePatterns : persistedPatterns;
    const exclusions = profileExclusions === undefined ? seed.profileExclusions : profileExclusions;
    return mergeProfilePatterns(seed, persisted, computeProfileCandidates(seed, currentVerdicts), exclusions, false);
  }

  function computeProfileDisplay(seed, currentVerdicts, persistedPatterns, profileExclusions) {
    const persisted = persistedPatterns === undefined ? seed.profilePatterns : persistedPatterns;
    const exclusions = profileExclusions === undefined ? seed.profileExclusions : profileExclusions;
    return mergeProfilePatterns(seed, persisted, computeProfileCandidates(seed, currentVerdicts), exclusions, true);
  }

  function activeProfilePatterns(patterns, profileExclusions) {
    const excluded = new Set(profileExclusions || []);
    return (patterns || []).filter((pattern) => !excluded.has(pattern.key));
  }

  function profileGuidance(patterns, profileExclusions) {
    const active = activeProfilePatterns(patterns, profileExclusions);
    return active.length
      ? `The selected profile patterns below are the only inferred preference guidance for the next round:\n${active.map((pattern) => `- ${pattern.text}`).join("\n")}\n\nDo not infer additional preferences from verdict history, including any pattern the user removed.`
      : "No profile patterns are selected for the next round. Do not infer preferences from verdict history.";
  }

  function buildContinuation(seed, currentVerdicts, seedHash, url, profileExclusions, profilePatterns) {
    assertRuntimeSeed(seed);
    const verdicts = currentVerdicts || {};
    const exclusions = profileExclusions === undefined ? seed.profileExclusions : profileExclusions;
    const mergedPatterns = profilePatterns === undefined ? computeProfile(seed, verdicts, seed.profilePatterns, exclusions) : profilePatterns;
    const persistedPatterns = mergedPatterns.filter((pattern) => !exclusions.includes(pattern.key)).map(profilePatternRecord);
    if (!validProfilePatterns(persistedPatterns, seed, exclusions)) throw new Error("invalid profile patterns in continuation");
    const completedRound = clone(seed.round);
    completedRound.verdicts = completedRound.options.map((option) => ({ optionId: option.id, decision: verdicts[option.id] || "skip" }));
    return {
      protocol: CONTINUATION_PROTOCOL,
      schemaVersion: SCHEMA_VERSION,
      parent: {
        sessionId: seed.session.id,
        roundNumber: seed.round.number,
        seedHash: seedHash,
        url: url,
      },
      session: clone(seed.session),
      parentProfilePatterns: clone(seed.profilePatterns),
      parentProfileExclusions: clone(seed.profileExclusions),
      profileExclusions: clone(exclusions),
      profilePatterns: persistedPatterns,
      completedRounds: [...(seed.history || []).map(clone), completedRound],
      nextRoundNumber: seed.round.number + 1,
    };
  }

  function iconMarkup(name, icons) {
    const svg = icons && icons[name];
    if (!svg) return "";
    return svg.replace("<svg ", '<svg aria-hidden="true" focusable="false" ');
  }

  return {
    PROTOCOL,
    CONTINUATION_PROTOCOL,
    SCHEMA_VERSION,
    RUNTIME_VERSION,
    FALLBACK_PROFILE,
    canonicalJson,
    clone,
    normalizeUrl,
    formatValue,
    profilePatternKey,
    profilePatternRecord,
    validateRuntimeSeed,
    assertRuntimeSeed,
    currentDecisionMap,
    computeProfileCandidates,
    computeProfile,
    computeProfileDisplay,
    mergeProfilePatterns,
    activeProfilePatterns,
    profileGuidance,
    buildContinuation,
    iconMarkup,
  };
});
