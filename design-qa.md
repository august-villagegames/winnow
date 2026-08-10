# Winnow v2 design QA

Final result: passed

## Evidence

- Reference: `references/visual/winnow-layout-v2.png` (opened at full resolution).
- Updated summary reference: `references/visual/winnow-summary-v2.png` (opened at full resolution).
- Round 1 capture: `/tmp/winnow-round1.png`.
- Updated summary capture: `/tmp/winnow-frame-fix.png`.
- Width-cap capture: `/tmp/winnow-width-cap.png` (500px shell centered in a wider viewport).
- PS5 image/factor-label capture: `/tmp/winnow-ps5-images-fields.png`.
- Browser: Codex in-app browser, local HTTP preview on port 4173.
- Verified at the default 1280 × 720 viewport and exercised the Round 1 card,
  final summary, profile patterns, external option links, and clipboard CTA.

## Comparison

- The viewport-owned surface fills the available viewport with no outer frame,
  radius, shadow, or canvas-colored gutter. Inner surface, ink, muted copy,
  border, radii, Space Grotesk typography, card stack, factor pills, circular
  verdict controls, progress dots, profile panel, mini-card rows, and sticky CTA
  match the supplied visual direction and measured values.
- On wider viewports, the same surface is capped at 500px and centered; smaller
  viewports continue to use the full available width.
- Factor pills render as `Field: value`, and source-backed option images render
  in the card and summary mini-card when present in the seed.
- The runtime intentionally omits the wireframe-only explanatory canvas,
  external query block, numbered labels, arrows, phone notch, and device handle.
- The synthetic fixture supplies no product images, so the runtime collapses
  the optional image slot as specified rather than rendering a placeholder.
- Primary interactions passed: like, dislike, skip, keyboard `S`, automatic
  summary transition, horizontally scrolling mini-card layout, and clipboard
  success state. No browser console errors or warnings were observed.

## Automated gates

- `python3 -m unittest discover -s tests -v` — passed (15 tests).
- `node --test tests/runtime-core.test.mjs` — passed (4 tests).
- `python3 scripts/winnow.py validate fixtures/synthetic-seed.json` — passed.
- `python3 scripts/winnow.py validate-successor fixtures/synthetic-continuation.json fixtures/synthetic-successor-seed.json` — passed.
