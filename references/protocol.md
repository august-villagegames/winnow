# Portable Winnow v2 protocol

Portable Winnow is a strict, immutable session seed compiled into one
self-contained `index.html`. The runtime owns all layout, formatting, ordering,
interaction, profile prose, and continuation construction. Agent-authored data
is limited to the typed session, round, source, option, and verdict evidence
defined by `seed.schema.json`.

## Seed

```json
{
  "protocol": "winnow.portable-session",
  "schemaVersion": 2,
  "runtimeVersion": "2.0.0",
  "session": {
    "id": "session-id",
    "title": "Durable couch under $2,000",
    "query": "Original user request",
    "requirements": ["Under $2,000", "Leather"],
    "primaryFactorId": "price"
  },
  "history": [],
  "round": {"number": 1, "generatedAt": "…", "factors": [], "sources": [], "options": []}
}
```

Every object is closed. Unknown keys, raw HTML, control characters, unsafe or
credential-bearing URLs, missing sources, missing factor values, and undeclared
factor values are rejected. A round has 1–6 factors, 4–6 options, and one
typed value per factor on every option. A primary factor is present in every
round and is rendered only in the large value slot. Reused factor IDs retain
the same label, value type, and display definition. Option IDs, normalized
titles, and canonical option URLs cannot be reused anywhere in a session.

An option may provide either the legacy singular `image` object or the
preferred `images` array. `images` contains 1–5 source-backed images; each
image may use a different count across options. Before a session is linked or
published, every unique image URL must be fetched over HTTPS and verified as a
successful, credential-free response with an allowed raster image content type
(`image/png`, `image/jpeg`, `image/gif`, `image/webp`, or `image/avif`) whose
bytes match the declared type and remain below the verifier's size limit.
Redirects must remain HTTPS and the final response must not be HTML, JSON,
empty, oversized, or a content-type mismatch. Run
`python3 scripts/winnow.py verify-images seed.json` before publishing; publish
also runs this gate automatically.

## History and continuation

Completed history rounds add exactly one verdict for every option:

```json
{"number":1,"generatedAt":"…","factors":[],"sources":[],"options":[],"verdicts":[{"optionId":"sofa-1","decision":"like"}]}
```

The runtime copies the immutable session and completed rounds into:

```json
{
  "protocol": "winnow.continuation",
  "schemaVersion": 2,
  "parent": {"sessionId":"session-id","roundNumber":1,"seedHash":"sha256","url":"https://example.here.now/"},
  "session": {},
  "completedRounds": [],
  "nextRoundNumber": 2
}
```

The clipboard handoff contains one fixed instruction plus the fenced package.
It never renders JSON in the page and never includes profile prose, weights,
hidden ranking fields, source-page instructions, or a claim token. A successor
must preserve the session byte-for-byte after canonical JSON normalization and
copy `completedRounds` exactly into `history`; it may evolve only the
non-primary factors and must research 4–6 entirely new options.

Validate the workflow with:

```sh
python3 scripts/winnow.py validate seed.json
python3 scripts/winnow.py inspect-continuation continuation.json
python3 scripts/winnow.py validate-successor continuation.json next-seed.json
python3 scripts/winnow.py publish next-seed.json --continuation continuation.json
```

Round 1 may publish without a continuation. Later rounds require the matching
continuation and are always published as a new anonymous here.now URL.

## Runtime behavior

The browser stores only current-page verdict events, keyed by seed hash, with
IndexedDB first, localStorage fallback, then memory. It resumes the first
unrated option after reload. Left/right reactions mean dislike/like; upward
swipe, `ArrowUp`, or `S` means skip. Reactions are final and the completed
round automatically becomes a summary.

The local profile combines all reacted options across history and the current
round. Numeric factors require three comparable reactions, both a like and a
dislike, and normalized strength of at least 0.20. Boolean/category factors
require a frequency difference of at least 0.25. Skips and free-text factors
do not create patterns. The runtime sorts patterns by strength and shows at
most three; otherwise it shows the contrast-needed message.
