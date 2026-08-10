(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.WinnowCore = factory();
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const PROTOCOL = "winnow.portable-session";
  const CONTINUATION_PROTOCOL = "winnow.continuation";
  const SCHEMA_VERSION = 3;
  const RUNTIME_VERSION = "3.0.0";
  const FALLBACK_PROFILE = "More contrast is needed before Winnow can identify a pattern.";

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

  function patternForFactor(factor, rows) {
    const reacted = rows.filter((row) => valueComparable(row.value) && (row.decision === "like" || row.decision === "dislike"));
    const liked = reacted.filter((row) => row.decision === "like");
    const disliked = reacted.filter((row) => row.decision === "dislike");
    if (reacted.length < 3 || !liked.length || !disliked.length) return null;

    if (factor.valueType === "number") {
      const values = reacted.map((row) => row.value).filter((value) => typeof value === "number" && Number.isFinite(value));
      if (values.length < 3) return null;
      const min = Math.min(...values);
      const max = Math.max(...values);
      if (max === min) return null;
      const likedMean = liked.reduce((total, row) => total + row.value, 0) / liked.length;
      const dislikedMean = disliked.reduce((total, row) => total + row.value, 0) / disliked.length;
      const difference = Math.abs(likedMean - dislikedMean) / (max - min);
      if (difference < .20) return null;
      const lower = likedMean < dislikedMean;
      return {
        factorId: factor.id,
        strength: difference,
        direction: lower ? "lower" : "higher",
        tone: lower ? "positive" : "negative",
        icon: lower ? "trending-down" : "trending-up",
        text: `Liked options trend toward ${lower ? "lower" : "higher"} ${labelNoun(factor.label)}`,
      };
    }

    if (factor.valueType === "boolean" || factor.valueType === "category") {
      const values = [...new Set(reacted.map((row) => JSON.stringify(row.value)))].map((value) => JSON.parse(value));
      let best = null;
      for (const value of values) {
        const likesAtValue = liked.filter((row) => JSON.stringify(row.value) === JSON.stringify(value)).length / liked.length;
        const dislikesAtValue = disliked.filter((row) => JSON.stringify(row.value) === JSON.stringify(value)).length / disliked.length;
        const difference = Math.abs(likesAtValue - dislikesAtValue);
        if (!best || difference > best.strength) best = { value, likesAtValue, strength: difference };
      }
      if (!best || best.strength < .25) return null;
      if (factor.valueType === "boolean") {
        const include = Boolean(best.value);
        return {
          factorId: factor.id,
          strength: best.strength,
          direction: include ? "include" : "exclude",
          tone: include ? "positive" : "negative",
          icon: include ? "trending-up" : "trending-down",
          text: `Liked options more often ${include ? "include" : "exclude"} ${String(factor.label).toLowerCase()}`,
        };
      }
      return {
        factorId: factor.id,
        strength: best.strength,
        direction: "include",
        tone: "positive",
        icon: "trending-up",
        text: `Liked options more often include ${String(best.value).toLowerCase()}`,
      };
    }
    return null;
  }

  function computeProfile(seed, currentVerdicts) {
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
    const patterns = [...byFactor.values()].map(({ factor, rows }) => patternForFactor(factor, rows)).filter(Boolean);
    patterns.sort((left, right) => right.strength - left.strength || (order.get(left.factorId) ?? 999) - (order.get(right.factorId) ?? 999) || left.factorId.localeCompare(right.factorId));
    return patterns.slice(0, 3);
  }

  function buildContinuation(seed, currentVerdicts, seedHash, url) {
    assertRuntimeSeed(seed);
    const verdicts = currentVerdicts || {};
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
    validateRuntimeSeed,
    assertRuntimeSeed,
    currentDecisionMap,
    computeProfile,
    buildContinuation,
    iconMarkup,
  };
});
