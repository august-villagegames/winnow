(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.WinnowRolling = api;
  if (root && root.document) api.bootstrap(root);
})(typeof window === "undefined" ? null : window, function () {
  "use strict";

  const PROTOCOL = "winnow.rolling-page";
  const VERSION = 1;
  const STATE_PROTOCOL = "winnow.rolling-local-state";
  const STATE_VERSION = 1;
  const STATUS_VALUES = new Set(["connected", "connecting", "researching", "ready_to_reveal", "complete", "expired", "failed", "circuit_open", "disconnected"]);
  const ACTIVE_STATUS_VALUES = new Set(["connected", "connecting", "researching", "ready_to_reveal"]);
  const TERMINAL_STATUS_VALUES = new Set(["complete", "expired", "failed", "circuit_open", "disconnected"]);
  const HASH = /^[0-9a-f]{64}$/;
  const ICONS = __WINNOW_ICONS__;
  const EMBEDDED_SEED = "__WINNOW_SEED_BASE64__";
  const EMBEDDED_HASH = "__WINNOW_SEED_HASH__";

  function object(value) { return value !== null && typeof value === "object" && !Array.isArray(value); }
  function escapeHtml(value) { return String(value ?? "").replace(/[&<>"']/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[character]); }
  function icon(name) { return rootCore().iconMarkup(name, ICONS); }
  function rootCore() { return (typeof window === "undefined" ? globalThis : window).WinnowCore; }

  function decodeBase64Json(value) {
    if (typeof value !== "string" || !value) throw new Error("missing encoded configuration");
    const binary = typeof atob === "function" ? atob(value) : Buffer.from(value, "base64").toString("binary");
    const bytes = Uint8Array.from(binary, (character) => character.charCodeAt(0));
    return JSON.parse(new TextDecoder().decode(bytes));
  }

  function coordinatorOrigin(value) {
    if (typeof value !== "string" || !value || value.length > 512) throw new Error("invalid coordinator origin");
    let parsed;
    try { parsed = new URL(value); } catch (_) { throw new Error("invalid coordinator origin"); }
    if (parsed.protocol !== "https:" || !parsed.hostname || parsed.username || parsed.password || parsed.port || parsed.pathname !== "/" || parsed.search || parsed.hash || !/^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$/i.test(parsed.hostname)) throw new Error("invalid coordinator origin");
    return `https://${parsed.hostname.toLowerCase()}/`;
  }

  function browserCapability(value) {
    if (typeof value !== "string" || !value || value.length > 512 || value.trim() !== value || /[\u0000-\u0020\u007f]/.test(value)) throw new Error("invalid browser capability");
    return value;
  }

  function positiveInteger(value, label) {
    if (!Number.isSafeInteger(value) || value < 1) throw new Error(`invalid ${label}`);
    return value;
  }

  function validateEnvelope(value) {
    if (!object(value) || Object.keys(value).length !== 5 || !["protocol", "version", "coordinatorOrigin", "browserCapability", "publishedRevision"].every((key) => Object.prototype.hasOwnProperty.call(value, key))) throw new Error("invalid rolling page envelope");
    if (value.protocol !== PROTOCOL || value.version !== VERSION) throw new Error("invalid rolling page envelope");
    return {
      protocol: PROTOCOL,
      version: VERSION,
      coordinatorOrigin: coordinatorOrigin(value.coordinatorOrigin),
      browserCapability: browserCapability(value.browserCapability),
      publishedRevision: positiveInteger(value.publishedRevision, "published revision"),
    };
  }

  function emptyState(seed, seedHash, revision) {
    return {
      protocol: STATE_PROTOCOL,
      version: STATE_VERSION,
      sessionId: seed.session.id,
      lastSeenRound: seed.round.number,
      lastSeenSeedHash: seedHash,
      lastSeenPublishedRevision: revision,
      events: [],
      profileExclusions: [...seed.profileExclusions],
      reveal: { state: "current" },
      request: { state: "idle", idempotencyKey: null },
    };
  }

  function canonicalEvents(candidate, seed) {
    if (!Array.isArray(candidate)) return null;
    const ids = seed.round.options.map((option) => option.id);
    const expected = new Set(ids);
    const found = new Set();
    const events = [];
    for (const event of candidate) {
      if (!object(event) || event.type !== "verdict" || !expected.has(event.optionId) || found.has(event.optionId) || !["like", "dislike", "skip"].includes(event.decision) || typeof event.createdAt !== "string" || event.createdAt.length > 64) return null;
      found.add(event.optionId);
      events.push({ type: "verdict", optionId: event.optionId, decision: event.decision, createdAt: event.createdAt });
    }
    return ids.filter((id) => found.has(id)).map((id) => events.find((event) => event.optionId === id));
  }

  function safeState(candidate, seed, seedHash, revision) {
    if (!object(candidate) || candidate.protocol !== STATE_PROTOCOL || candidate.version !== STATE_VERSION || candidate.sessionId !== seed.session.id || !Number.isSafeInteger(candidate.lastSeenRound) || candidate.lastSeenRound < 1 || typeof candidate.lastSeenSeedHash !== "string" || !HASH.test(candidate.lastSeenSeedHash) || !Number.isSafeInteger(candidate.lastSeenPublishedRevision) || candidate.lastSeenPublishedRevision < 1) return null;
    const sameEmbeddedRevision = candidate.lastSeenRound === seed.round.number && candidate.lastSeenSeedHash === seedHash && candidate.lastSeenPublishedRevision === revision;
    const parsedEvents = canonicalEvents(candidate.events, seed);
    // A changed page may have entirely different option IDs.  Preserve its
    // metadata long enough for reconciliation to mark it stale instead of
    // treating an old page as an unrelated fresh session.
    if ((parsedEvents === null && sameEmbeddedRevision) || !Array.isArray(candidate.profileExclusions) || candidate.profileExclusions.some((key) => typeof key !== "string" || !key || key.length > 500) || new Set(candidate.profileExclusions).size !== candidate.profileExclusions.length) return null;
    const events = parsedEvents || [];
    const request = object(candidate.request) && ["idle", "submitting", "accepted"].includes(candidate.request.state) && (candidate.request.idempotencyKey === null || typeof candidate.request.idempotencyKey === "string") ? candidate.request : { state: "idle", idempotencyKey: null };
    const reveal = object(candidate.reveal) && ["current", "ready"].includes(candidate.reveal.state) ? candidate.reveal : { state: "current" };
    return { ...candidate, events, profileExclusions: [...candidate.profileExclusions], request: { ...request }, reveal: { ...reveal } };
  }

  function reconcileState(candidate, seed, seedHash, revision) {
    const initial = emptyState(seed, seedHash, revision);
    const stored = safeState(candidate, seed, seedHash, revision);
    if (!stored) return { state: initial, action: "fresh" };
    if (stored.lastSeenRound > seed.round.number) return { state: { ...initial, reveal: { state: "stale" } }, action: "stale_cache" };
    if (stored.lastSeenRound < seed.round.number) return { state: initial, action: "new_round" };
    if (stored.lastSeenSeedHash !== seedHash || stored.lastSeenPublishedRevision !== revision) return { state: { ...initial, reveal: { state: "stale" } }, action: "divergent" };
    return { state: { ...stored, lastSeenRound: seed.round.number, lastSeenSeedHash: seedHash, lastSeenPublishedRevision: revision }, action: "resume" };
  }

  function decisionMap(state) { return rootCore().currentDecisionMap(state.events); }

  function browserRequest(seed, seedHash, envelope, state, idempotencyKey) {
    const decisions = decisionMap(state);
    const verdicts = seed.round.options.map((option) => {
      const decision = decisions[option.id];
      if (!decision) throw new Error("every option needs a verdict");
      return { optionId: option.id, decision };
    });
    const profiles = rootCore().computeProfile(seed, decisions, seed.profilePatterns, state.profileExclusions);
    return {
      protocol: "winnow.browser-request",
      version: 1,
      idempotencyKey,
      roundNumber: seed.round.number,
      seedHash,
      publishedRevision: envelope.publishedRevision,
      verdicts,
      selectedProfileKeys: profiles.map((profile) => profile.key),
    };
  }

  function publicLinkNotice(expiresAt) {
    const value = typeof expiresAt === "string" ? expiresAt : "";
    const date = new Date(value);
    const expiry = Number.isNaN(date.valueOf()) ? "the scheduled expiry" : date.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
    return `<aside class="public-link-notice" aria-label="Public-link notice"><strong>Shared page</strong> Anyone with this link can view this comparison. While Winnow is waiting, a link holder can guide future rounds. This page expires <time datetime="${escapeHtml(value)}">${escapeHtml(expiry)}</time>.</aside>`;
  }

  function parseStatus(value) {
    const keys = new Set(["status", "roundNumber", "seedHash", "publishedRevision", "expiresAt", "agentLeaseExpiresAt", "remainingOptionCapacity"]);
    if (!object(value) || Object.keys(value).some((key) => !keys.has(key)) || !STATUS_VALUES.has(value.status)) throw new Error("invalid status response");
    if (ACTIVE_STATUS_VALUES.has(value.status)) {
      if (!Number.isSafeInteger(value.roundNumber) || value.roundNumber < 1 || typeof value.seedHash !== "string" || !HASH.test(value.seedHash) || !Number.isSafeInteger(value.publishedRevision) || value.publishedRevision < 1 || typeof value.expiresAt !== "string" || (value.agentLeaseExpiresAt !== null && typeof value.agentLeaseExpiresAt !== "string") || !Number.isSafeInteger(value.remainingOptionCapacity) || value.remainingOptionCapacity < 0 || value.remainingOptionCapacity > 100) throw new Error("invalid status response");
    } else if (value.expiresAt !== undefined && typeof value.expiresAt !== "string") {
      throw new Error("invalid status response");
    }
    return value;
  }

  function parseNextRoundResult(value) {
    const keys = new Set(["status", "roundNumber", "publishedRevision"]);
    if (!object(value) || Object.keys(value).some((key) => !keys.has(key)) || !["accepted", ...TERMINAL_STATUS_VALUES].includes(value.status)) throw new Error("invalid next-round response");
    if (value.status === "accepted" && (!Number.isSafeInteger(value.roundNumber) || value.roundNumber < 1 || !Number.isSafeInteger(value.publishedRevision) || value.publishedRevision < 1)) throw new Error("invalid next-round response");
    return value;
  }

  function cacheBuster(url, revision) {
    const target = new URL(url);
    target.searchParams.set("winnowRevision", String(positiveInteger(revision, "published revision")));
    return target.toString();
  }

  function consumeCacheBuster(location, history, revision) {
    const target = new URL(location.href);
    const supplied = target.searchParams.get("winnowRevision");
    if (supplied === null) return { consumed: false, valid: true };
    const valid = supplied === String(revision);
    if (valid) {
      target.searchParams.delete("winnowRevision");
      history.replaceState(null, "", `${target.pathname}${target.search}${target.hash}`);
    }
    return { consumed: valid, valid };
  }

  function pollingDelay({ status, visible, staleCache = false, failures = 0, random = Math.random }) {
    if (!visible || TERMINAL_STATUS_VALUES.has(status) || status === "ready_to_reveal") return null;
    if (!staleCache && !["connecting", "researching"].includes(status)) return null;
    if (!Number.isSafeInteger(failures) || failures < 0) throw new Error("invalid polling failures");
    if (!failures) return 5000;
    const ceiling = Math.min(30000, 5000 * (2 ** Math.min(failures, 3)));
    const value = Number(random());
    const jitter = Number.isFinite(value) ? Math.min(1, Math.max(0, value)) : 0.5;
    return Math.min(30000, Math.round(ceiling * (0.8 + jitter * 0.4)));
  }

  function reconcileRemoteStatus(state, response, expected) {
    const remote = parseStatus(response);
    let status = remote.status;
    let nextState = state;
    let remainingCapacity = null;
    if (ACTIVE_STATUS_VALUES.has(status)) {
      remainingCapacity = remote.remainingOptionCapacity;
      if (remote.publishedRevision > expected.publishedRevision || status === "ready_to_reveal") {
        status = "ready_to_reveal";
        nextState = { ...state, reveal: { state: "ready" } };
      } else if (remote.roundNumber !== expected.roundNumber || remote.seedHash !== expected.seedHash || remote.publishedRevision < expected.publishedRevision) {
        nextState = { ...state, reveal: { state: "stale" } };
      }
    }
    return { status, state: nextState, remainingCapacity };
  }

  function applyNextRoundResult(state, result) {
    const parsed = parseNextRoundResult(result);
    if (parsed.status === "accepted") return { status: "researching", state: { ...state, request: { state: "accepted", idempotencyKey: state.request.idempotencyKey } } };
    return { status: parsed.status, state: { ...state, request: { state: "idle", idempotencyKey: null } } };
  }

  function nextRoundControl({ status, revealState, remainingCapacity, requestInFlight, requestState }) {
    if (TERMINAL_STATUS_VALUES.has(status) || remainingCapacity < 4) return { label: status === "complete" || remainingCapacity < 4 ? "Session complete" : "Session unavailable", disabled: true, help: "This session is now read-only." };
    if (revealState === "stale") return { label: "Checking for the latest round", disabled: true, help: "This cached page cannot request another round." };
    if (status === "ready_to_reveal") return { label: "Continue", disabled: false, help: "A new round is ready. Continue when you want to reveal it." };
    if (status === "connecting") return { label: "Connecting to agent", disabled: true, help: "Waiting for the agent’s next-round connection." };
    if (status === "researching" || requestInFlight || requestState === "accepted") return { label: "Researching next round", disabled: true, help: "Your choices are committed while the next round is researched." };
    return { label: "Generate another round →", disabled: false, help: "Your completed choices will request the next round." };
  }

  function uuid(win) {
    if (win.crypto && typeof win.crypto.randomUUID === "function") return win.crypto.randomUUID();
    if (win.crypto && typeof win.crypto.getRandomValues === "function") {
      const bytes = new Uint8Array(16);
      win.crypto.getRandomValues(bytes);
      bytes[6] = (bytes[6] & 15) | 64;
      bytes[8] = (bytes[8] & 63) | 128;
      const hex = [...bytes].map((item) => item.toString(16).padStart(2, "0")).join("");
      return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
    }
    throw new Error("secure browser randomness is unavailable");
  }

  function storageKey(sessionId) { return `winnow:rolling:v1:${sessionId}`; }

  function bootstrap(win) {
    const document = win.document;
    const app = document.getElementById("app");
    if (!app) return;
    const core = rootCore();
    let seed;
    let seedHash;
    let envelope;
    try {
      seed = decodeBase64Json(EMBEDDED_SEED);
      core.assertRuntimeSeed(seed);
      seedHash = EMBEDDED_HASH;
      if (!HASH.test(seedHash)) throw new Error("invalid seed hash");
      const envelopeNode = document.getElementById("winnow-rolling-page");
      envelope = validateEnvelope(decodeBase64Json(envelopeNode?.textContent || ""));
      if (document.querySelector('meta[name="winnow-rolling-version"]')?.content !== String(VERSION) || document.querySelector('meta[name="winnow-published-revision"]')?.content !== String(envelope.publishedRevision)) throw new Error("rolling markers do not match envelope");
    } catch (error) {
      app.innerHTML = '<div class="app-shell"><p class="error-state">This Winnow session could not be opened.</p></div>';
      return;
    }

    const key = storageKey(seed.session.id);
    let state = emptyState(seed, seedHash, envelope.publishedRevision);
    let persistence = { kind: "memory", db: null };
    let status = "connecting";
    let remainingCapacity = 100;
    let pollTimer = null;
    let failures = 0;
    let visible = document.visibilityState !== "hidden";
    let requestInFlight = false;
    let persistenceWarning = "";

    function announce(message) { const live = document.getElementById("winnow-live"); if (live) live.textContent = message; }
    function now() { return new Date().toISOString(); }
    function workInProgress() { return ["connecting", "researching"].includes(status) || state.reveal.state === "stale"; }
    function terminal() { return TERMINAL_STATUS_VALUES.has(status); }
    function allDecided() { return seed.round.options.every((option) => decisionMap(state)[option.id]); }
    function currentOption() { const decisions = decisionMap(state); return seed.round.options.find((option) => !decisions[option.id]); }

    function openDb() {
      if (!win.indexedDB) return Promise.reject(new Error("IndexedDB unavailable"));
      return new Promise((resolve, reject) => {
        const request = win.indexedDB.open("winnow-rolling-v1", 1);
        request.onupgradeneeded = () => request.result.createObjectStore("states");
        request.onsuccess = () => resolve(request.result);
        request.onerror = () => reject(request.error || new Error("IndexedDB unavailable"));
      });
    }
    function dbValue(mode, value) {
      return new Promise((resolve, reject) => {
        const transaction = persistence.db.transaction("states", mode);
        const request = mode === "readonly" ? transaction.objectStore("states").get(key) : transaction.objectStore("states").put(value, key);
        request.onsuccess = () => resolve(request.result);
        request.onerror = () => reject(request.error || new Error("IndexedDB storage failed"));
      });
    }
    async function loadState() {
      try {
        persistence = { kind: "indexeddb", db: await openDb() };
        const result = reconcileState(await dbValue("readonly"), seed, seedHash, envelope.publishedRevision);
        state = result.state;
        return result.action;
      } catch (_) {
        try {
          persistence = { kind: "localStorage", db: null };
          const raw = win.localStorage.getItem(key);
          const result = reconcileState(raw ? JSON.parse(raw) : null, seed, seedHash, envelope.publishedRevision);
          state = result.state;
          return result.action;
        } catch (_) {
          persistence = { kind: "memory", db: null };
          persistenceWarning = "Your choices will stay on this page because browser storage is unavailable.";
          return "fresh";
        }
      }
    }
    async function persist() {
      try {
        if (persistence.kind === "indexeddb") await dbValue("readwrite", state);
        else if (persistence.kind === "localStorage") win.localStorage.setItem(key, JSON.stringify(state));
        else persistenceWarning = "Your choices will stay on this page because browser storage is unavailable.";
      } catch (_) {
        try {
          persistence = { kind: "localStorage", db: null };
          win.localStorage.setItem(key, JSON.stringify(state));
        } catch (_) {
          persistence = { kind: "memory", db: null };
          persistenceWarning = "Your choices will stay on this page because browser storage is unavailable.";
        }
      }
    }

    function renderRequirements() { return seed.session.requirements.length ? `<ul class="requirements" aria-label="Session requirements">${seed.session.requirements.map((item) => `<li class="pill">${escapeHtml(item)}</li>`).join("")}</ul>` : ""; }
    function renderPublicLinkNotice() { return publicLinkNotice(document.querySelector('meta[name="winnow-expires-at"]')?.content || ""); }
    function optionImage(option, compact = false) {
      const images = Array.isArray(option.images) ? option.images : option.image ? [option.image] : [];
      if (!images.length) return "";
      if (compact) return `<img class="mini-card-image" data-image src="${escapeHtml(images[0].url)}" alt="${escapeHtml(images[0].alt)}" loading="lazy" referrerpolicy="no-referrer">`;
      const multiple = images.length > 1;
      return `<div class="image-carousel${multiple ? " has-controls" : " is-single"}" data-rolling-carousel aria-label="Images for ${escapeHtml(option.title)}"><div class="carousel-viewport">${images.map((image, index) => `<div class="carousel-slide${index === 0 ? " is-active" : ""}" data-carousel-slide="${index}"${index ? ' aria-hidden="true"' : ""}><img class="option-image" data-image src="${escapeHtml(image.url)}" alt="${escapeHtml(image.alt)}" loading="${index ? "lazy" : "eager"}" referrerpolicy="no-referrer"><span class="image-fallback" hidden>Image unavailable</span></div>`).join("")}</div>${multiple ? `<div class="carousel-controls"><button class="carousel-arrow carousel-prev" type="button" data-carousel-prev aria-label="Previous image">‹</button><span class="carousel-counter" data-carousel-counter aria-live="polite">1 / ${images.length}</span><button class="carousel-arrow carousel-next" type="button" data-carousel-next aria-label="Next image">›</button></div><div class="carousel-dots" data-carousel-dots>${images.map((_, index) => `<button class="carousel-dot${index === 0 ? " is-active" : ""}" type="button" data-carousel-dot="${index}" aria-label="Show image ${index + 1} of ${images.length}" aria-current="${index === 0 ? "true" : "false"}"></button>`).join("")}</div>` : ""}</div>`;
    }
    function optionValue(option, factorId) { return option.values.find((value) => value.factorId === factorId)?.value; }
    function progress() { const count = state.events.length; return `card ${Math.min(count + 1, seed.round.options.length)} of ${seed.round.options.length} · <span class="progress-dots" aria-hidden="true">${seed.round.options.map((_, index) => `<span class="${index < count ? "done" : index === count ? "current" : "remaining"}">●</span>`).join("")}</span>`; }
    function renderCard() {
      const option = currentOption();
      if (!option) return renderSummary();
      const primary = seed.round.factors.find((factor) => factor.id === seed.session.primaryFactorId);
      const otherFactors = seed.round.factors.filter((factor) => factor.id !== seed.session.primaryFactorId);
      app.innerHTML = `<section class="screen" aria-labelledby="session-title"><header class="session-header"><p class="caption">Round ${seed.round.number}</p><h1 class="session-title" id="session-title">${escapeHtml(seed.session.title)}</h1>${renderRequirements()}${renderPublicLinkNotice()}</header><div class="deck-stage"><div class="deck-ghost" aria-hidden="true"></div><article class="option-card" data-card-surface><div class="option-heading"><h2 class="option-title">${escapeHtml(option.title)}</h2><span class="primary-value">${escapeHtml(core.formatValue(primary, optionValue(option, primary.id)))}</span></div>${optionImage(option)}<p class="option-description">${escapeHtml(option.description.text)}</p><ul class="factor-values">${otherFactors.map((factor) => `<li class="pill"><span>${escapeHtml(factor.label)}</span> · ${escapeHtml(core.formatValue(factor, optionValue(option, factor.id)))}</li>`).join("")}</ul></article></div><nav class="verdict-controls" aria-label="React to option"><button class="verdict-button dislike" type="button" data-decision="dislike" aria-label="Don’t like">${icon("x")}</button><button class="verdict-button like" type="button" data-decision="like" aria-label="Like">${icon("heart")}</button></nav><p class="card-progress">${progress()}</p><button class="skip-button sr-only" type="button" data-decision="skip">Skip</button><p class="sr-only">Swipe left to dislike, right to like, or up to skip. Keyboard shortcuts: left arrow to dislike, right arrow to like, S or up arrow to skip.</p><div id="winnow-live" class="sr-only" aria-live="polite">${escapeHtml(persistenceWarning)}</div></section>`;
      bindImages();
      bindCarousels();
    }
    function profileView(patterns) {
      if (!patterns.length) return `<p class="profile-empty">${escapeHtml(core.FALLBACK_PROFILE)}</p>`;
      return `<p class="profile-hint">Remove any pattern you don’t want to guide future rounds.</p><ul class="profile-list">${patterns.map((pattern) => { const excluded = !core.activeProfilePatterns([pattern], state.profileExclusions).length; return `<li class="profile-item ${pattern.tone}${excluded ? " is-excluded" : ""}"><span class="profile-icon">${icon(pattern.icon)}</span><span class="profile-label">${escapeHtml(pattern.compactLabel)}</span><span class="profile-support" aria-label="${pattern.supportCount} supporting selections">${pattern.supportCount}</span><button class="profile-control" type="button" data-profile-key="${escapeHtml(pattern.key)}" ${requestInFlight || terminal() ? "disabled" : ""} aria-pressed="${String(!excluded)}" aria-label="${escapeHtml(`${excluded ? "Restore" : "Exclude"} ${pattern.text} ${excluded ? "for future rounds" : "from future rounds"}`)}">${icon(excluded ? "rotate-ccw" : "x")}</button></li>`; }).join("")}</ul>`;
    }
    function control() {
      return nextRoundControl({ status, revealState: state.reveal.state, remainingCapacity, requestInFlight, requestState: state.request.state });
    }
    function renderSummary() {
      const decisions = decisionMap(state);
      const patterns = core.computeProfileDisplay(seed, decisions, seed.profilePatterns, state.profileExclusions);
      const liked = seed.round.options.filter((option) => decisions[option.id] === "like");
      const disliked = seed.round.options.filter((option) => decisions[option.id] === "dislike");
      const button = control();
      const cards = (options, dislike) => options.length ? options.map((option) => `<article class="mini-card${dislike ? " disliked" : ""}"><span class="mini-card-top">${optionImage(option, true) || escapeHtml(option.title)}</span><span class="mini-card-footer">${escapeHtml(option.title)}</span></article>`).join("") : '<p class="profile-empty">None this round.</p>';
      app.innerHTML = `<section class="summary-screen" aria-labelledby="summary-title"><header class="session-header"><p class="caption">Round ${seed.round.number} complete</p><h1 class="session-title" id="summary-title">${escapeHtml(seed.session.title)}</h1>${renderPublicLinkNotice()}</header><div class="summary-scroll"><section class="profile-panel" aria-labelledby="profile-title"><h2 class="section-caption" id="profile-title">Your profile so far</h2>${profileView(patterns)}</section><section class="summary-group" aria-labelledby="liked-title"><h2 class="section-caption" id="liked-title">Liked this round · ${liked.length}</h2><div class="mini-card-grid">${cards(liked, false)}</div></section><section class="summary-group" aria-labelledby="disliked-title"><h2 class="section-caption" id="disliked-title">Disliked this round · ${disliked.length}</h2><div class="mini-card-grid">${cards(disliked, true)}</div></section></div><div class="summary-dock"><button class="continuation-button" type="button" id="rolling-next-round" ${button.disabled ? "disabled" : ""} aria-describedby="rolling-status">${escapeHtml(button.label)}</button><p class="rolling-status" id="rolling-status" aria-live="polite">${escapeHtml(button.help)}</p></div><div id="winnow-live" class="sr-only" aria-live="polite">${escapeHtml(persistenceWarning)}</div></section>`;
      bindImages();
    }
    function render() { if (allDecided()) renderSummary(); else renderCard(); }

    function bindImages() {
      app.querySelectorAll("img[data-image]").forEach((image) => image.addEventListener("error", () => { image.hidden = true; image.nextElementSibling?.removeAttribute("hidden"); announce(`Image unavailable: ${image.alt || "this option"}.`); }, { once: true }));
    }
    function bindCarousels() {
      app.querySelectorAll("[data-rolling-carousel]").forEach((carousel) => {
        const slides = [...carousel.querySelectorAll("[data-carousel-slide]")];
        const dots = [...carousel.querySelectorAll("[data-carousel-dot]")];
        const counter = carousel.querySelector("[data-carousel-counter]");
        if (slides.length < 2 || !counter) return;
        let current = 0;
        const select = (requested) => {
          current = ((requested % slides.length) + slides.length) % slides.length;
          slides.forEach((slide, index) => { const active = index === current; slide.classList.toggle("is-active", active); slide.toggleAttribute("aria-hidden", !active); });
          dots.forEach((dot, index) => { const active = index === current; dot.classList.toggle("is-active", active); dot.setAttribute("aria-current", String(active)); });
          counter.textContent = `${current + 1} / ${slides.length}`;
        };
        carousel.querySelector("[data-carousel-prev]")?.addEventListener("click", () => select(current - 1));
        carousel.querySelector("[data-carousel-next]")?.addEventListener("click", () => select(current + 1));
        dots.forEach((dot) => dot.addEventListener("click", () => select(Number(dot.dataset.carouselDot))));
        carousel.addEventListener("keydown", (event) => { if (event.key === "ArrowLeft" || event.key === "ArrowRight") { event.preventDefault(); select(current + (event.key === "ArrowLeft" ? -1 : 1)); } });
      });
    }

    function commit(decision) {
      if (!currentOption() || !["like", "dislike", "skip"].includes(decision) || terminal()) return;
      const option = currentOption();
      state = { ...state, events: [...state.events, { type: "verdict", optionId: option.id, decision, createdAt: now() }] };
      persist();
      render();
      if (allDecided()) statusRequest();
    }
    function toggleProfile(key) {
      if (requestInFlight || terminal() || typeof key !== "string") return;
      const exclusions = new Set(state.profileExclusions);
      const pattern = core.computeProfileDisplay(seed, decisionMap(state), seed.profilePatterns, state.profileExclusions).find((item) => item.key === key);
      if (!pattern) return;
      const restored = !core.activeProfilePatterns([pattern], exclusions).length;
      if (restored) {
        // Boolean profile keys may have an older normalized spelling.  Mirror
        // the legacy UI by removing whichever stored key excludes this pattern.
        for (const excludedKey of exclusions) {
          if (!core.activeProfilePatterns([pattern], [excludedKey]).length) exclusions.delete(excludedKey);
        }
      } else {
        exclusions.add(pattern.key);
      }
      state = { ...state, profileExclusions: [...exclusions] };
      persist();
      renderSummary();
    }
    function endpoint(path) { return new URL(path, envelope.coordinatorOrigin).toString(); }
    async function responseJson(response) {
      const type = response.headers.get("content-type") || "";
      if (!type.toLowerCase().startsWith("application/json")) throw new Error("invalid response content type");
      return response.json();
    }
    async function statusRequest() {
      if (!visible || terminal()) return;
      const params = new URLSearchParams({ roundNumber: String(seed.round.number), seedHash, publishedRevision: String(envelope.publishedRevision) });
      try {
        const response = await win.fetch(`${endpoint("v1/session/status")}?${params}`, { method: "GET", credentials: "omit", headers: { Accept: "application/json", Authorization: `Bearer ${envelope.browserCapability}` } });
        if (!response.ok) throw new Error("status request failed");
        const next = reconcileRemoteStatus(state, await responseJson(response), { roundNumber: seed.round.number, seedHash, publishedRevision: envelope.publishedRevision });
        failures = 0;
        status = next.status;
        state = next.state;
        if (next.remainingCapacity !== null) remainingCapacity = next.remainingCapacity;
        persist();
        if (allDecided()) renderSummary();
      } catch (_) {
        failures += 1;
        if (allDecided()) { announce("Connection is unavailable. Retrying shortly."); renderSummary(); }
      } finally { schedulePoll(); }
    }
    function stopPoll() { if (pollTimer !== null) { win.clearTimeout(pollTimer); pollTimer = null; } }
    function schedulePoll() {
      stopPoll();
      const delay = pollingDelay({ status, visible, staleCache: state.reveal.state === "stale", failures });
      if (delay === null || !workInProgress()) return;
      pollTimer = win.setTimeout(statusRequest, delay);
    }
    async function nextRound() {
      if (!allDecided() || requestInFlight || terminal() || state.reveal.state === "stale") return;
      if (status === "ready_to_reveal") { win.location.assign(cacheBuster(win.location.href, envelope.publishedRevision + 1)); return; }
      if (status !== "connected") return;
      requestInFlight = true;
      const idempotencyKey = state.request.idempotencyKey || uuid(win);
      state = { ...state, request: { state: "submitting", idempotencyKey } };
      persist(); renderSummary();
      try {
        const body = browserRequest(seed, seedHash, envelope, state, idempotencyKey);
        const response = await win.fetch(endpoint("v1/session/next-round"), { method: "POST", credentials: "omit", headers: { Accept: "application/json", "Content-Type": "application/json", Authorization: `Bearer ${envelope.browserCapability}` }, body: JSON.stringify(body) });
        if (!response.ok) throw new Error("next-round request failed");
        const result = applyNextRoundResult(state, await responseJson(response));
        status = result.status;
        state = result.state;
        if (result.status === "researching") {
          announce("Your choices are committed. Researching the next round.");
        }
      } catch (_) {
        state = { ...state, request: { state: "idle", idempotencyKey: null } };
        announce("That request was not accepted. Checking the current session state.");
      } finally {
        requestInFlight = false;
        persist(); renderSummary(); statusRequest();
      }
    }

    app.addEventListener("click", (event) => {
      const decision = event.target.closest?.("[data-decision]")?.dataset.decision;
      if (decision) { commit(decision); return; }
      const profile = event.target.closest?.("[data-profile-key]")?.dataset.profileKey;
      if (profile) { toggleProfile(profile); return; }
      if (event.target.closest?.("#rolling-next-round")) nextRound();
    });
    document.addEventListener("keydown", (event) => {
      const target = event.target;
      if (target && target.closest?.("button, a, input, textarea, select")) return;
      if (event.key === "ArrowLeft") { event.preventDefault(); commit("dislike"); }
      else if (event.key === "ArrowRight") { event.preventDefault(); commit("like"); }
      else if (event.key === "ArrowUp" || event.key.toLowerCase() === "s") { event.preventDefault(); commit("skip"); }
    });
    document.addEventListener("visibilitychange", () => { visible = document.visibilityState !== "hidden"; if (visible) statusRequest(); else stopPoll(); });

    (async () => {
      const cache = consumeCacheBuster(win.location, win.history, envelope.publishedRevision);
      const action = await loadState();
      if (!cache.valid || action === "stale_cache" || action === "divergent") state = { ...state, reveal: { state: "stale" } };
      render();
      if (action === "new_round") announce("A committed new round replaced unfinished local choices.");
      else if (state.reveal.state === "stale") announce("This page may be cached. Checking for the latest round.");
      statusRequest();
    })();
  }

  return { PROTOCOL, VERSION, STATE_PROTOCOL, STATE_VERSION, validateEnvelope, emptyState, reconcileState, browserRequest, publicLinkNotice, parseStatus, parseNextRoundResult, cacheBuster, consumeCacheBuster, pollingDelay, reconcileRemoteStatus, applyNextRoundResult, nextRoundControl, bootstrap };
});
