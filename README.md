# Portable Winnow v2

This directory is the isolated, zero-dependency Portable Winnow runtime. It
compiles a strict v2 session seed into exactly one self-contained HTML file.
Only the current page's verdicts are persisted in the browser; later rounds
are researched and published by an agent from a copied continuation package.

## Local checks

```sh
python3 scripts/winnow.py validate fixtures/synthetic-seed.json
python3 scripts/winnow.py verify-images fixtures/synthetic-seed.json
python3 scripts/winnow.py build fixtures/synthetic-seed.json /tmp/winnow-index.html
python3 scripts/winnow.py inspect-continuation fixtures/synthetic-continuation.json
python3 scripts/winnow.py validate-successor fixtures/synthetic-continuation.json fixtures/synthetic-successor-seed.json
python3 -m unittest discover -s tests -v
node --test tests/runtime-core.test.mjs
```

Open `/tmp/winnow-index.html` or serve it from a local static server. Rate all
six cards to see the summary, then use `Generate a better round →` to copy the
fixed agent handoff and continuation package.

Options may use the legacy singular `image` field or the preferred `images`
array with up to five images. Multiple images appear as an accessible carousel;
the runtime falls back cleanly when a source-backed image fails to load.

## Agent workflow

For an initial request, read `references/protocol.md` and
`references/seed.schema.json`, establish the immutable session fields, research
broadly in private, select only 4–6 representative source-supported options,
choose 1–6 comparison factors, validate, compile, and publish Round 1.

For a continuation, validate the package, preserve the session and all
completed rounds exactly, use the full verdict history as evidence, research
4–6 new options, validate the successor, and publish it as a new anonymous URL:

```sh
python3 scripts/winnow.py publish next-seed.json --continuation continuation.json
```

Never add runtime network calls, agent-authored layout/CSS, hidden candidates,
ranking weights, raw source HTML, credentials, or continuation JSON to the
page. The only external runtime resources are cited HTTPS images.
