# Portable Winnow protocol

Portable Winnow is an immutable research seed plus a browser-local event log.
The seed is compiled into `assets/runtime.html`; no Winnow backend is needed
after publication.

## Seed

The top-level object must have:

```json
{
  "protocol": "winnow.portable-session",
  "schemaVersion": 1,
  "runtimeVersion": "1.0.0",
  "sessionId": "stable-agent-generated-id",
  "createdAt": "2026-08-08T12:00:00Z",
  "query": "the user's request",
  "research": {"asOf":"…","assumptions":[],"summary":"…","sources":[],"factors":[],"candidates":[]},
  "presentations": [],
  "initialRound": {"candidateIds":[],"factorIds":[],"generationExplanation":"…"},
  "localStrategy": {
    "roundSize": 4,
    "factorLimit": 6,
    "factorWeightStep": 1.5,
    "factorWeightMin": 0.25,
    "factorWeightMax": 4,
    "relevanceWeight": 0.75,
    "diversityWeight": 0.15,
    "evidenceWeight": 0.1
  }
}
```

The compiler requires 12–24 candidates, 6–10 factors, one presentation per
candidate, four initial candidates, HTTPS sources, and usable facts for at
least 70% of all candidate/factor pairs. `unknown` is the only permitted
explicit absence of evidence. IDs are unique and candidate facts can only use
declared factor IDs.

Presentation blocks are safe primitives: `title`, `text`, `metric-grid`,
`badge-list`, `link`, and `image`. Every claim-bearing block cites a source.
The title must equal the researched candidate name. Image and link hosts must
match the cited source host. The compiler rejects raw HTML, non-HTTPS URLs,
missing references, duplicate IDs, and unsupported versions.

## Local state

The browser stores this append-only state, keyed by the SHA-256 hash of the
seed:

```json
{
  "protocol": "winnow.local-state",
  "schemaVersion": 1,
  "seedHash": "sha256",
  "revision": 3,
  "status": "ready",
  "events": []
}
```

IndexedDB is preferred, followed by localStorage and then an in-memory
fallback. Events are replayed deterministically. BroadcastChannel messages
allow another tab to refresh instead of overwriting a newer revision. Small
event logs are mirrored to `#w1=<base64url>.<sha256-prefix>`; larger logs are
kept in IndexedDB and the user is directed to the complete export.

## Selection

Similarity is computed only from researched facts. Numbers are normalized over
the corpus, arrays use Jaccard similarity, and strings/booleans/categories use
exact equality. Unknown values do not enter the denominator. A candidate's
preference is the mean similarity to likes and mean dissimilarity to dislikes;
with no verdicts it is 0.5. Greedy selection uses:

```text
0.75 × preference + 0.15 × within-deck diversity + 0.10 × evidence coverage
```

Ties break by candidate ID. Skips mark a candidate seen but contribute no
preference. The latest verdict wins. More/Less factor controls multiply or
divide weights by 1.5, clamped to 0.25–4; rejecting a learned pattern resets
that factor to 1.

Free text is persisted as an unresolved note. It is surfaced in the
continuation package and never changes ranking through keyword heuristics.

## Continuation

The runtime can produce `winnow.continuation` JSON containing the parent
lineage, active patterns, factor weights, latest verdict history, seen IDs,
unresolved notes, and reasons for new research. A receiving agent must validate
it, research new evidence/candidates, exclude exhausted candidates, produce a
complete successor seed, and publish a new anonymous Site. It must never
update or depend on the parent Site.
