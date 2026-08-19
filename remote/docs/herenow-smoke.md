# Redacted manual HereNow smoke procedure

Run this only from an approved operator environment with outbound HTTPS. It is a separate live-provider check; it is not a host-conformance test and it must not be part of CI.

The disposable probe creates a generic anonymous page, uploads a generic HTML file, updates the same slug, exercises finalize replay, and waits for the second marker to converge. HereNow does not document an anonymous deletion endpoint, so do not put user data, seed data, URLs, secret material, or a real comparison on the page. The page is intentionally left to the provider's documented expiry.

1. Confirm the terminal recorder is not retaining command output and that no HTTP debug/proxy capture is active.
2. From the repository root, run the bounded probe with a generic 1 MiB file:

   ```sh
   python3 remote/probes/herenow_anonymous_probe.py --target-bytes 1048576
   ```

3. Save only the final JSON result in the deployment evidence record. The probe deliberately omits the claim token, slug, public page URL, signed upload/finalize URLs, and response bodies. Do not add them by running it under verbose HTTP logging.
4. Require: anonymous create/update HTTP 2xx; two successful uploads/finalizes; `original_expiry_preserved: true`; update convergence before the bounded window; and no unexpected rate-limit behavior. Treat any omitted expiry, provider schema change, token exposure, or failure to converge as a stop.
5. Record date/time, probe revision, redacted status fields, byte size, and provider rate-limit headers only. Delete local captured output after the approved evidence summary is stored.

This confirms that Winnow's publisher can use anonymous HereNow create/update without an end-user account or API key. It does **not** authorize a public MCP endpoint or claim support for Claude, Cowork, or Claude Code.

