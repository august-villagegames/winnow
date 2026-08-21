import copy
import importlib.util
import json
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = ROOT / ".agents" / "skills" / "winnow"
SCRIPT = SKILL_DIR / "scripts" / "winnow.py"
SPEC = importlib.util.spec_from_file_location("portable_winnow_contract", SCRIPT)
assert SPEC and SPEC.loader
winnow = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(winnow)
SCHEMA = json.loads((SKILL_DIR / "references" / "seed.schema.json").read_text(encoding="utf-8"))


def fixture(name: str = "synthetic-seed.json") -> dict:
    return json.loads((ROOT / "fixtures" / name).read_text(encoding="utf-8"))


def resized_seed(option_count: int) -> dict:
    seed = fixture()
    options = seed["round"]["options"]
    while len(options) < option_count:
        option_number = len(options) + 1
        option = copy.deepcopy(options[(option_number - 1) % len(options)])
        option["id"] = f"sofa-{option_number}"
        option["title"] = f"Additional sofa {option_number}"
        option["optionUrl"]["url"] = f"https://example.com/sofas/additional-{option_number}"
        options.append(option)
    seed["round"]["options"] = options[:option_count]
    return seed


class ContractTripwireTests(unittest.TestCase):
    def test_schema_identity_and_limits_match_the_bundled_validator(self):
        properties = SCHEMA["properties"]
        self.assertEqual(properties["protocol"]["const"], winnow.PROTOCOL)
        self.assertEqual(properties["schemaVersion"]["const"], winnow.SCHEMA_VERSION)
        self.assertEqual(properties["runtimeVersion"]["const"], winnow.RUNTIME_VERSION)

        continuation = SCHEMA["$defs"]["continuation"]["properties"]
        self.assertEqual(continuation["protocol"]["const"], winnow.CONTINUATION_PROTOCOL)
        self.assertEqual(continuation["schemaVersion"]["const"], winnow.SCHEMA_VERSION)

        session = SCHEMA["$defs"]["session"]["properties"]
        self.assertEqual(session["requirements"]["maxItems"], winnow._MAX_REQUIREMENTS)

        profile_patterns = SCHEMA["$defs"]["profilePatterns"]
        self.assertEqual(profile_patterns["maxItems"], winnow.MAX_PROFILE_PATTERNS)

        for definition_name in ("round", "completedRound"):
            round_properties = SCHEMA["$defs"][definition_name]["properties"]
            self.assertEqual(round_properties["factors"]["minItems"], winnow._MIN_FACTORS)
            self.assertEqual(round_properties["factors"]["maxItems"], winnow._MAX_FACTORS)
            self.assertEqual(round_properties["sources"]["minItems"], winnow._MIN_SOURCES)
            self.assertEqual(round_properties["options"]["minItems"], winnow._MIN_OPTIONS)
            self.assertEqual(round_properties["options"]["maxItems"], winnow._MAX_OPTIONS)

        self.assertEqual(SCHEMA["$defs"]["option"]["properties"]["images"]["minItems"], winnow._MIN_IMAGES)
        self.assertEqual(SCHEMA["$defs"]["option"]["properties"]["images"]["maxItems"], winnow._MAX_IMAGES)

    def test_fixtures_and_boundaries_use_the_current_executable_contract(self):
        seed = fixture()
        self.assertIs(winnow.validate_seed(seed), seed)
        for option_count in (winnow._MIN_OPTIONS, winnow._MAX_OPTIONS):
            candidate = resized_seed(option_count)
            self.assertIs(winnow.validate_seed(candidate), candidate)

        with self.assertRaisesRegex(winnow.ValidationError, rf"requires {winnow._MIN_OPTIONS}–{winnow._MAX_OPTIONS} options"):
            winnow.validate_seed(resized_seed(winnow._MIN_OPTIONS - 1))
        with self.assertRaisesRegex(winnow.ValidationError, rf"requires {winnow._MIN_OPTIONS}–{winnow._MAX_OPTIONS} options"):
            winnow.validate_seed(resized_seed(winnow._MAX_OPTIONS + 1))

    def test_bundled_cli_exposes_the_documented_commands(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--help"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        for command in ("validate", "verify-images", "inspect-continuation", "validate-successor", "publish"):
            self.assertIn(command, result.stdout)

    def test_high_risk_instruction_anchors_use_the_bundled_contract(self):
        skill = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        protocol = (SKILL_DIR / "references" / "protocol.md").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn("4–10", skill)
        self.assertIn("4–10", protocol)
        self.assertIn("$SKILL_DIR/scripts/winnow.py", skill)
        self.assertIn("$SKILL_DIR/scripts/winnow.py", protocol)
        self.assertIn('literal string `"rolling"`', skill)
        self.assertIn("Winnow coordinates publication and browser state only", skill)
        self.assertIn("page-bound browser credential", skill)
        self.assertIn("It is not user\nidentity or owner authority", skill)
        self.assertIn("100-option cap", skill)
        self.assertIn("Winnow itself does not research or invoke a model", protocol)
        self.assertIn("Host support is a release-gated claim", protocol)
        self.assertIn("Connector setup alone does not make a host supported", readme)
        self.assertIn("https://winnow-mcp.onrender.com/mcp", readme)
        for text in (skill, protocol, readme):
            self.assertNotIn("python3 scripts/winnow.py", text)

    def test_runtime_generated_continuation_validates_with_python_validator(self):
        node_script = r'''
const crypto = require("node:crypto");
const fs = require("node:fs");
const core = require("./.agents/skills/winnow/assets/runtime-core.js");
const seed = JSON.parse(fs.readFileSync("./fixtures/synthetic-seed.json", "utf8"));
const decisions = Object.fromEntries(seed.round.options.map((option) => [option.id, "skip"]));
const seedHash = crypto.createHash("sha256").update(core.canonicalJson(seed)).digest("hex");
const continuation = core.buildContinuation(seed, decisions, seedHash, "https://example.here.now/round-1");
process.stdout.write(JSON.stringify(continuation));
'''
        result = subprocess.run(
            ["node", "-e", node_script],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        continuation = json.loads(result.stdout)
        self.assertIs(winnow.validate_continuation(continuation), continuation)


if __name__ == "__main__":
    unittest.main()
