# Portable Winnow pilot

This directory is a risk-isolated, zero-dependency prototype of Winnow. It is
not imported by the MCP application and is intentionally publishable as a
future standalone `winnow-skill` repository.

The agent researches a broad candidate corpus, validates a JSON seed, compiles
one self-contained HTML file, and anonymously publishes a new here.now Site.
After publication, rounds run locally from researched facts and structured
feedback. IndexedDB is the preferred event store, with localStorage and an
in-memory fallback. Small histories are mirrored in a URL fragment; complete
exports and continuation packages are available from the page.

## Safe local pilot

From this directory:

```sh
python3 scripts/winnow.py validate fixtures/synthetic-seed.json
python3 scripts/winnow.py build fixtures/synthetic-seed.json /tmp/winnow-index.html
python3 -m unittest discover -s tests -v
```

Open the built HTML in a browser or serve it from a local static server. Rate
all four initial cards, use More/Less on factors, add a note, and repeat for at
least three rounds. Reload to test persistence. Use the session-link and full
export controls to test transfer, then give the continuation JSON to a fresh
agent.

Only after the synthetic flow passes should an agent run `publish`. Publishing
creates a new anonymous Site, uploads no credentials, and returns a URL that
expires after 24 hours. The URL exposes the embedded research to anyone who has
it.

## Boundaries

- All prototype files are under `portable-poc/`.
- The runtime has no Winnow backend, polling, MCP transport, or agent
  connection. Its CSP sets `connect-src 'none'`.
- Every `publish` call creates a new anonymous Site; the CLI has no update or
  claim operation and discards claim-token data.
- Free text is persisted as unresolved context for the assistant and never
  affects local ranking.
- This pilot does not change `server.ts`, MCP modules/transports, Next routes,
  session persistence, root dependencies, build scripts, Docker, or deploy
  configuration.

## Test and isolation gate

Run the Python tests, then inspect the branch before committing:

```sh
git diff --name-only 41e7597...HEAD
```

Every path must begin with `portable-poc/`.
