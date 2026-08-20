"""Fixed, discoverable v4 authoring material for MCP hosts.

The portable skill remains the one canonical source for the JSON Schema.  This
module only serves that source and a server-owned, deliberately non-publishable
example; it does not add a second validation contract.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


SEED_SCHEMA_RESOURCE_URI = "winnow://contracts/v4/seed-schema.json"
ROUND_ONE_AUTHORING_GUIDE_RESOURCE_URI = "winnow://contracts/v4/round-one-authoring-guide"


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[3]


def canonical_seed_schema_path() -> Path:
    """Return the portable skill's one authoritative v4 schema path."""

    return _repository_root() / ".agents" / "skills" / "winnow" / "references" / "seed.schema.json"


def canonical_seed_schema_bytes() -> bytes:
    """Read exact canonical schema bytes without reserializing them."""

    value = canonical_seed_schema_path().read_bytes()
    # Fail closed during startup/read if a deployment accidentally packages a
    # non-JSON file at the canonical path.  The original bytes are still what
    # the resource serves, so schema formatting can never silently drift.
    json.loads(value.decode("utf-8"))
    return value


def canonical_seed_schema_text() -> str:
    """Return the exact UTF-8 schema text for a model-readable resource."""

    return canonical_seed_schema_bytes().decode("utf-8")


ROUND_ONE_EXAMPLE: dict[str, Any] = {
    "protocol": "winnow.portable-session",
    "schemaVersion": 4,
    "runtimeVersion": "4.0.0",
    "session": {
        "id": "illustrative-prioritization-frameworks",
        "title": "Illustrative product-prioritization frameworks",
        "query": "Compare common product-prioritization frameworks for a team choosing a planning method.",
        "requirements": ["Text-only comparison", "Public sources"],
        "primaryFactorId": "approach",
        "imagePolicy": {
            "mode": "notApplicable",
            "reason": "This is a text-only comparison of decision methods; product imagery would not add decision-relevant evidence.",
        },
    },
    "profileExclusions": [],
    "profilePatterns": [],
    "history": [],
    "round": {
        "number": 1,
        "generatedAt": "2026-01-01T00:00:00Z",
        "factors": [
            {"id": "approach", "label": "Approach", "valueType": "category", "display": {"style": "text"}},
            {"id": "needs-research", "label": "Needs customer research", "valueType": "boolean", "display": {"style": "boolean", "trueLabel": "Needs research", "falseLabel": "Can start with team estimates"}},
            {"id": "uses-numeric-score", "label": "Uses a numeric score", "valueType": "boolean", "display": {"style": "boolean", "trueLabel": "Numeric score", "falseLabel": "No numeric score"}},
        ],
        "sources": [
            {"id": "source-rice", "title": "Illustrative RICE reference", "url": "https://example.com/rice", "retrievedAt": "2026-01-01T00:00:00Z"},
            {"id": "source-moscow", "title": "Illustrative MoSCoW reference", "url": "https://example.com/moscow", "retrievedAt": "2026-01-01T00:00:00Z"},
            {"id": "source-kano", "title": "Illustrative Kano reference", "url": "https://example.com/kano", "retrievedAt": "2026-01-01T00:00:00Z"},
            {"id": "source-wsjf", "title": "Illustrative WSJF reference", "url": "https://example.com/wsjf", "retrievedAt": "2026-01-01T00:00:00Z"},
        ],
        "options": [
            {
                "id": "rice",
                "title": "RICE",
                "primarySourceId": "source-rice",
                "description": {"text": "An illustrative scoring framework entry.", "sourceId": "source-rice"},
                "values": [
                    {"factorId": "approach", "value": "Reach, impact, confidence, effort", "sourceId": "source-rice"},
                    {"factorId": "needs-research", "value": False, "sourceId": "source-rice"},
                    {"factorId": "uses-numeric-score", "value": True, "sourceId": "source-rice"},
                ],
            },
            {
                "id": "moscow",
                "title": "MoSCoW",
                "primarySourceId": "source-moscow",
                "description": {"text": "An illustrative categorization framework entry.", "sourceId": "source-moscow"},
                "values": [
                    {"factorId": "approach", "value": "Must, should, could, will not", "sourceId": "source-moscow"},
                    {"factorId": "needs-research", "value": False, "sourceId": "source-moscow"},
                    {"factorId": "uses-numeric-score", "value": False, "sourceId": "source-moscow"},
                ],
            },
            {
                "id": "kano",
                "title": "Kano model",
                "primarySourceId": "source-kano",
                "description": {"text": "An illustrative satisfaction-category framework entry.", "sourceId": "source-kano"},
                "values": [
                    {"factorId": "approach", "value": "Satisfaction categories", "sourceId": "source-kano"},
                    {"factorId": "needs-research", "value": True, "sourceId": "source-kano"},
                    {"factorId": "uses-numeric-score", "value": False, "sourceId": "source-kano"},
                ],
            },
            {
                "id": "wsjf",
                "title": "WSJF",
                "primarySourceId": "source-wsjf",
                "description": {"text": "An illustrative cost-of-delay framework entry.", "sourceId": "source-wsjf"},
                "values": [
                    {"factorId": "approach", "value": "Cost of delay divided by job size", "sourceId": "source-wsjf"},
                    {"factorId": "needs-research", "value": False, "sourceId": "source-wsjf"},
                    {"factorId": "uses-numeric-score", "value": True, "sourceId": "source-wsjf"},
                ],
            },
        ],
    },
}


def round_one_authoring_guide() -> str:
    """Return fixed v4 instructions and a safe structural example for hosts."""

    example = json.dumps(ROUND_ONE_EXAMPLE, ensure_ascii=False, indent=2, sort_keys=True)
    return f"""# Winnow v4 round-one authoring guide

Read `winnow://contracts/v4/seed-schema.json` before calling
`create_winnow_session`. The host researches and selects the comparison
options; Winnow validates, publishes, and coordinates browser reactions.

Create a complete v4 round-one seed with `protocol` set to
`winnow.portable-session`, `schemaVersion` 4, `runtimeVersion` `4.0.0`, empty
`history`, `profilePatterns`, and `profileExclusions`, one current round, and
4–10 representative options. Every current-round claim and factor value needs
an HTTPS source in that round. Use 1–6 useful factors and give every option one
correctly typed value for every factor. Use `imagePolicy.mode: required` for
visual decisions; use `notApplicable` only when imagery adds no meaningful
decision evidence.

Do not put any session handle, capability, claim, token, browser request,
continuation package, hidden candidate, or private user data in a seed. This
example uses `example.com` references and fixed illustrative facts. It is
structurally valid but **illustrative only — do not publish unchanged**.

```json
{example}
```
"""


def seed_contract_payload() -> dict[str, Any]:
    """Return the one fixed fallback-tool payload, without session state."""

    return {
        "schemaResourceUri": SEED_SCHEMA_RESOURCE_URI,
        "seedSchema": json.loads(canonical_seed_schema_text()),
        "authoringGuideResourceUri": ROUND_ONE_AUTHORING_GUIDE_RESOURCE_URI,
        "roundOneAuthoringGuide": round_one_authoring_guide(),
    }
