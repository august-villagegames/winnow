# Portable Winnow v3

This directory is the isolated, zero-dependency Portable Winnow runtime. It
compiles a strict v3 session seed into exactly one self-contained HTML page in
memory, then publishes that page to an anonymous hosted Site. There is no
supported local HTML artifact workflow. Only the current page's verdicts and
profile selections are persisted in the browser; later rounds are researched
and published by an agent from a copied continuation package.

The canonical standalone repository is
<https://github.com/august-villagegames/winnow>. This content is also kept at
`portable-poc/` inside the full `winnow-dev` repository for integration work.
The commands below assume this directory is the current working directory.

## Local checks

Run the initial-round commands in this order. `publish` is the only delivery
workflow; it compiles the page in memory, uploads it to HereNow, and verifies
the live hosted page.

```sh
python3 scripts/winnow.py validate seed.json
python3 scripts/winnow.py verify-images seed.json
python3 scripts/winnow.py publish seed.json
```

For the included synthetic fixture, the schema-only check is:

```sh
python3 scripts/winnow.py validate fixtures/synthetic-seed.json
```

Its reserved `example.com` image URLs are intentionally not fetchable, so use
a researched seed with real source-backed image URLs for `verify-images` and
`publish`. For a continuation, validate the copied package and successor
before publishing a new anonymous URL:

```sh
python3 scripts/winnow.py inspect-continuation fixtures/synthetic-continuation.json
python3 scripts/winnow.py validate-successor fixtures/synthetic-continuation.json fixtures/synthetic-successor-seed.json
python3 scripts/winnow.py verify-images next-seed.json
python3 scripts/winnow.py publish next-seed.json --continuation continuation.json
```

For a successor, `verify-images` checks the new current round. Images retained
in `history` were verified when their original rounds were published and are
not fetched again.

Run the repository tests as well:

```sh
python3 -m unittest discover -s tests -v
node --test tests/runtime-core.test.mjs
```

The publish command returns the hosted HereNow URL; do not create, open, or
return a local HTML file or local file path. Rate all six cards at the hosted
URL to see the summary, then use `Generate a better round →` to copy the fixed
agent handoff and continuation package.

The synthetic fixture uses reserved `example.com` image URLs to exercise the
compiled media and browser fallback; run `verify-images` against a researched
seed with real source-backed URLs before publishing. An image may be hosted on
a separate CDN from its cited source page.

Options may use the legacy singular `image` field or the preferred `images`
array with up to five images. Every session is image-required by default;
declare `session.imagePolicy.mode` as `notApplicable` with a reason only for a
clearly non-visual decision. Initialize the required root
`profileExclusions` field to `[]`. Multiple images appear as an accessible
carousel. If a verified image later fails in the browser, the runtime keeps the
media slot and announces `Image unavailable` instead of silently removing it.

## Agent workflow

For an initial request, read `references/protocol.md` and
`references/seed.schema.json`, establish the immutable session fields, research
broadly in private, select only 4–6 representative source-supported options,
decide the session image policy, choose 1–6 comparison factors, collect at
least one direct source-backed image for every option unless the policy is
explicitly `notApplicable`, validate, verify images, and publish Round 1
through HereNow.
The image `sourceId` identifies the cited source page; it does not require the
direct image URL to share that page's hostname. Run `verify-images` before
publishing, then open the hosted URL in a browser and confirm that every image
loads and renders, including each image in a carousel. If a browser check
fails, replace or remove the image and publish again before delivering the URL.
The hosted URL is the only deliverable: never create, open, attach, or return
a local HTML file or local file path. Treat
shopping for products, shoes, clothing, travel, homes, people, styles, and
designs as visual decisions that require images.

For a continuation, validate the package, preserve the session and all
completed rounds exactly, use only the selected profile patterns in the copied
handoff as preference guidance, and never infer further preferences from
verdict history. Copy `continuation.profileExclusions` exactly into the
successor seed’s `profileExclusions`, research 4–6 new options, preserve the
session image policy, add verified images for every new option when required,
validate the successor, and publish it as a new anonymous HereNow URL. Return
the hosted URL, never a generated local HTML artifact:

```sh
python3 scripts/winnow.py publish next-seed.json --continuation continuation.json
```

Never add runtime network calls, agent-authored layout/CSS, hidden candidates,
ranking weights, raw source HTML, credentials, or continuation JSON to the
page. The only external runtime resources are cited HTTPS images.

## License

Winnow-authored code and documentation in this portable runtime are licensed
under Apache-2.0. The bundled Space Grotesk font and Lucide icons retain their
upstream licenses; see [NOTICE](NOTICE), `assets/fonts/OFL.txt`, and
`assets/icons/LICENSE`.
