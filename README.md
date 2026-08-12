# Portable Winnow v4

Portable Winnow compiles a strict v4 comparison seed in memory and publishes
one self-contained page to an anonymous hosted Site. A local HTML file is never
a supported deliverable. Later rounds are researched from a copied continuation
package and published to new anonymous URLs.

The canonical standalone repository is
<https://github.com/august-villagegames/winnow>. This content is also kept at
`portable-poc/` inside the full `winnow-dev` repository for integration work.
Commands below assume this directory is the current working directory.

## Quick start

The normal initial-round workflow is one command:

```sh
python3 scripts/winnow.py publish seed.json
```

`publish` validates the seed, freshly verifies every unique image URL in the
current round, compiles and uploads the page, and checks the hosted page for the
exact session ID, seed hash, runtime version, and normalized expiration markers.
Any image or hosted-marker failure blocks publication. Completed-round images
are not network-refetched, and no browser-based visual QA is required.

`validate` and `verify-images` are optional diagnostics. Each `verify-images`
invocation performs a fresh check; it does not create reusable state for
`publish`.

```sh
python3 scripts/winnow.py validate fixtures/synthetic-seed.json
python3 scripts/winnow.py verify-images seed.json
```

The included synthetic fixture uses reserved `example.com` image URLs, so it is
suitable for schema and runtime tests but not image verification or publication.
Use researched, direct, source-backed image URLs for a real session; an image
may be hosted on a different CDN from its cited source page.

For a later round, publish the successor with its copied continuation package:

```sh
python3 scripts/winnow.py publish next-seed.json --continuation continuation.json
```

Continuation inspection and successor validation are also available as optional
diagnostics:

```sh
python3 scripts/winnow.py inspect-continuation continuation.json
python3 scripts/winnow.py validate-successor continuation.json next-seed.json
```

The publish result includes the hosted URL and expiration, current-round image
and unique-image counts, and non-negative timings for validation, image
verification, site publication, and total work. Never create, open, attach, or
return a local HTML file or path.

## Authoring contract

Read `references/protocol.md` and `references/seed.schema.json` before authoring
a seed. Sessions require images by default. Prefer one strong, source-backed
image per option; use the supported 1–5 `images` range only when additional
images add materially distinct, decision-relevant evidence. Use
`session.imagePolicy.mode: "notApplicable"` with a reason only for a clearly
non-visual decision.

The runtime owns layout, interactions, ordering, profile logic, and continuation
construction. Profile patterns are runtime-generated and carried explicitly in
the v4 seed/continuation state; do not add runtime network calls, agent-authored presentation,
hidden candidates, ranking weights, raw source HTML, credentials, or
continuation JSON to a seed. The hosted URL is the deliverable.

## Tests

```sh
python3 -m unittest discover -s tests -v
node --test tests/runtime-core.test.mjs
```

## License

Winnow-authored code and documentation are licensed under Apache-2.0. The
bundled Space Grotesk font and Lucide icons retain their upstream licenses; see
[NOTICE](NOTICE), `assets/fonts/OFL.txt`, and `assets/icons/LICENSE`.
