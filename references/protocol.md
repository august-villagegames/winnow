# Portable Winnow v3 protocol

Portable Winnow is a strict, immutable session seed compiled in memory into one
self-contained `index.html`, then uploaded to an anonymous hosted Site. The
runtime owns all layout, formatting, ordering, interaction, profile prose, and
continuation construction. Agent-authored data is limited to the typed
session, round, source, option, and verdict evidence defined by
`seed.schema.json`. A local HTML file is never a supported deliverable.

## Seed

```json
{
  "protocol": "winnow.portable-session",
  "schemaVersion": 3,
  "runtimeVersion": "3.0.2",
  "session": {
    "id": "session-id",
    "title": "Durable couch under $2,000",
    "query": "Original user request",
    "requirements": ["Under $2,000", "Leather"],
    "primaryFactorId": "price",
    "imagePolicy": {"mode": "required"}
  },
  "profileExclusions": [],
  "history": [],
  "round": {"number": 1, "generatedAt": "…", "factors": [], "sources": [], "options": []}
}
```

Every object is closed. Unknown keys, raw HTML, control characters, unsafe or
credential-bearing URLs, missing sources, missing factor values, and undeclared
factor values are rejected. A round has 1–6 factors, 4–10 options, and one
typed value per factor on every option. A primary factor is present in every
round and is rendered only in the large value slot. Reused factor IDs retain
the same label, value type, and display definition. Option IDs, normalized
titles, and canonical option URLs cannot be reused anywhere in a session.

`profileExclusions` is a runtime-owned array of opaque profile-pattern keys.
It starts empty. The runtime uses it to remember insights the user has removed
from future-round guidance; it does not change verdict history or whether the
primary factor appears in later rounds.

Every session declares an image policy. Use `{"mode":"required"}` by default:
each option in every round must provide at least one source-backed image. Use
`{"mode":"notApplicable","reason":"…"}` only when the decision is clearly
non-visual, such as a purely textual or factual comparison; the reason is
preserved with the immutable session. Visual shopping and recommendation
decisions (products, shoes, clothing, travel, homes, people, styles, and
designs) require images.

An option may provide either the legacy singular `image` object or the
preferred `images` array. Prefer one strong, source-backed image per option;
additional images are allowed only when they show materially distinct,
decision-relevant information. `images` contains 1–5 source-backed images;
each image may use a different count across options, but a required-image
session must include at least one for every option. An image's `sourceId`
identifies the cited source page; the direct image URL may be hosted on a
separate CDN and does not need to share the source page's hostname. Before a
session is linked or published, every unique image URL in the active round must
pass a full HTTPS GET—not only a HEAD request. The final response must be 2xx
and credential-free, use an allowed raster content type (`image/png`,
`image/jpeg`, `image/gif`, `image/webp`, or `image/avif`), remain below the
verifier's size limit, and contain bytes matching the declared type. Redirects
must remain HTTPS; DNS, TLS, network, status, length, signature, and type
failures block publication, as do HTML, JSON, empty, and oversized responses.
Duplicate URLs within the active round are fetched once per invocation. Images
carried in completed history rounds were verified with their original rounds
and are not refetched for a successor.

`publish` runs a fresh active-round image check automatically on every
invocation. The optional `verify-images` diagnostic also performs a fresh,
independent check; it creates no state for a later publication attempt. The
response verifier checks image bytes without adding runtime network calls or
local media artifacts.

## History and continuation

Completed history rounds add exactly one verdict for every option:

```json
{"number":1,"generatedAt":"…","factors":[],"sources":[],"options":[],"verdicts":[{"optionId":"sofa-1","decision":"like"}]}
```

The runtime copies the immutable session and completed rounds into:

```json
{
  "protocol": "winnow.continuation",
  "schemaVersion": 3,
  "parent": {"sessionId":"session-id","roundNumber":1,"seedHash":"sha256","url":"https://example.here.now/"},
  "session": {},
  "parentProfileExclusions": [],
  "profileExclusions": [],
  "completedRounds": [],
  "nextRoundNumber": 2
}
```

The clipboard handoff contains selected runtime-generated profile patterns
before the fenced package. Those patterns are the only inferred preference
guidance for the next round; the agent must not infer more preferences from
verdict history or removed patterns. When no pattern is selected, the handoff
explicitly prohibits preference inference from history. The package never
renders in the page and never contains weights, hidden ranking fields,
source-page instructions, or a claim token.

`parentProfileExclusions` exactly records the exclusions embedded in the
hashed parent seed. `profileExclusions` records the user’s latest selection.
A successor must copy the latter exactly into its root `profileExclusions`;
the runtime will record that successor value as the next parent snapshot. A
successor must preserve the session byte-for-byte after canonical JSON
normalization and copy `completedRounds` exactly into `history`; it may evolve
only the non-primary factors and must research 4–10 entirely new options. It
must retain the session image policy and collect verified images for every new
option when the policy is `required`.

The normal initial workflow is:

```sh
python3 scripts/winnow.py publish seed.json
```

For a later round, pass the copied continuation package:

```sh
python3 scripts/winnow.py publish next-seed.json --continuation continuation.json
```

Use the other commands only as optional diagnostics:

```sh
python3 scripts/winnow.py validate seed.json
python3 scripts/winnow.py verify-images seed.json
python3 scripts/winnow.py inspect-continuation continuation.json
python3 scripts/winnow.py validate-successor continuation.json next-seed.json
```

Publishing is the only delivery workflow. The publisher compiles the page in
memory, uploads it to HereNow, and returns the hosted URL. Do not invoke a
local build step or create, open, attach, or return an HTML file or local file
path. This keeps the renderer reusable if another hosted provider is added in
the future. Before returning, it deterministically verifies the hosted page's
exact session ID, seed hash, runtime version, and normalized expiration meta
tags. This exact-marker check is the publication verification step; no
browser-based visual QA is required. Publish output includes the existing
publication fields, current-round image and unique-image counts, and
non-negative `timingsMs` for validation, image verification, site publication,
and total work.

Round 1 may publish without a continuation. Later rounds require the matching
continuation and are always published as a new anonymous HereNow URL.

## Runtime behavior

The browser stores current-page verdict events and profile exclusions, keyed by
seed hash, with IndexedDB first, localStorage fallback, then memory. It resumes
the first unrated option after reload. Left/right reactions mean dislike/like;
upward swipe, `ArrowUp`, or `S` means skip. Reactions are final and the
completed round automatically becomes a summary.

The local profile combines all reacted options across history and the current
round. Likes and dislikes are evaluated independently, and a profile pattern
requires at least two supporting selections of the same polarity. Boolean and
category patterns require a frequency difference of at least 0.25 when both
polarities have evidence; numeric patterns use a counted average when only one
polarity has enough evidence and retain a directional trend when both sides
provide a strong contrast. Skips and free-text factors do not create patterns.
The runtime sorts patterns by support count and strength, shows at most six,
and otherwise shows the contrast-needed message. The summary renders each
pattern as a compact pill with its support count. Removing a pill excludes only
that same factor, polarity, direction, and semantic value from future guidance;
it stays visibly restorable while the current evidence still supports it.
