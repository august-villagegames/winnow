from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "schema_identity", ROOT / "scripts" / "check_schema_identity.py"
)
assert SPEC and SPEC.loader
schema_identity = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(schema_identity)


def schema(identifier: str, title: str = "Portable Winnow v4 session seed") -> bytes:
    return json.dumps(
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": identifier,
            "title": title,
        },
        separators=(",", ":"),
    ).encode("utf-8")


V2_ID = "https://raw.githubusercontent.com/august-villagegames/winnow/v0.1.0/references/seed.schema.json"
V4_ID = "https://raw.githubusercontent.com/august-villagegames/winnow/v0.2.0/references/seed.schema.json"


class SchemaIdentityTests(unittest.TestCase):
    def test_mutable_ref_is_rejected(self):
        with self.assertRaisesRegex(schema_identity.SchemaIdentityError, "immutable vX.Y.Z"):
            schema_identity.schema_id(
                schema("https://raw.githubusercontent.com/august-villagegames/winnow/main/references/seed.schema.json")
            )

    def test_schema_changes_require_a_new_identity(self):
        with self.assertRaisesRegex(schema_identity.SchemaIdentityError, "without changing \\$id"):
            schema_identity.check_base(schema(V2_ID, "changed"), schema(V2_ID), "v0.1.0")

        schema_identity.check_base(schema(V4_ID), schema(V2_ID), "v0.1.0")

    def test_release_tag_must_contain_the_checked_in_schema(self):
        current = schema(V4_ID)
        schema_identity.check_tag(current, "v0.2.0", current)

        with self.assertRaisesRegex(schema_identity.SchemaIdentityError, "does not match"):
            schema_identity.check_tag(current, "v0.3.0", current)
        with self.assertRaisesRegex(schema_identity.SchemaIdentityError, "differs"):
            schema_identity.check_tag(current, "v0.2.0", schema(V4_ID, "different"))


if __name__ == "__main__":
    unittest.main()
