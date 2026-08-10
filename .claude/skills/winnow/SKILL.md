---
name: winnow
description: Research and publish a strict, self-contained anonymous Winnow v2 comparison session.
---

# Winnow portable skill adapter

Use the canonical workflow in `portable-poc/.agents/skills/winnow/SKILL.md`.
Read `references/protocol.md` and `references/seed.schema.json`; author a
closed v2 seed with an immutable session, 1–6 typed factors, and 4–6 new
source-supported options. Prefer 3–5 useful images per option when available,
with 1–5 images allowed and counts free to vary. Before linking or publishing
any round, run `python3 scripts/winnow.py verify-images seed.json`; this hard
gate fetches every unique image URL and rejects broken redirects, non-image
responses, HTML/JSON masquerades, unsupported content types, signature
mismatches, empty responses, and oversized files. Compile the committed runtime
and publish Round 1 only after that check passes.

For a continuation, validate and preserve the session plus all completed
rounds, research 4–6 entirely new options, validate the successor, run
`python3 scripts/winnow.py verify-images next-seed.json`, and publish with
`python3 scripts/winnow.py publish next-seed.json --continuation continuation.json`.
Every successor gets a new anonymous URL. Never expose claim tokens, source
HTML/scripts, hidden candidates, agent-authored layout, or raw continuation
JSON.
