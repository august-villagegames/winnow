import importlib.util
import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "check_schema_identity", ROOT / "scripts" / "check_schema_identity.py"
)
assert SPEC and SPEC.loader
checker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checker)


SCHEMA = (ROOT / checker.SCHEMA_PATH).read_bytes()


class SchemaIdentityChecks(unittest.TestCase):
    def test_schema_is_v4_at_the_canonical_immutable_artifact(self):
        value = json.loads(SCHEMA)
        self.assertEqual(
            value["$id"],
            "https://raw.githubusercontent.com/august-villagegames/winnow/v0.2.0/.agents/skills/winnow/references/seed.schema.json",
        )
        self.assertEqual(value["properties"]["schemaVersion"]["const"], 4)

    def test_rejects_mutable_schema_identity(self):
        with self.assertRaises(ValueError):
            checker.schema_version(json.dumps({"$id": "https://raw.githubusercontent.com/august-villagegames/winnow/main/.agents/skills/winnow/references/seed.schema.json"}).encode())

    def test_requires_identity_change_when_schema_changes(self):
        base = json.dumps({"$id": "https://raw.githubusercontent.com/august-villagegames/winnow/v0.1.0/.agents/skills/winnow/references/seed.schema.json", "value": 1}).encode()
        changed = json.dumps({"$id": "https://raw.githubusercontent.com/august-villagegames/winnow/v0.1.0/.agents/skills/winnow/references/seed.schema.json", "value": 2}).encode()
        with self.assertRaises(ValueError):
            checker.require_new_identity(changed, base)

    def test_release_must_match_tag_and_content(self):
        with self.assertRaises(ValueError):
            checker.require_release("v0.1.0", SCHEMA, SCHEMA)


if __name__ == "__main__":
    unittest.main()
