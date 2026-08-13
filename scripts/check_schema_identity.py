#!/usr/bin/env python3
"""Check that the published schema identity tracks its immutable artifact."""

import argparse
import json
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = Path(".agents/skills/winnow/references/seed.schema.json")
SCHEMA_URL = re.compile(
    r"^https://raw\.githubusercontent\.com/august-villagegames/winnow/"
    r"(v\d+\.\d+\.\d+)/\.agents/skills/winnow/references/seed\.schema\.json$"
)
ANY_SCHEMA_URL = re.compile(
    r"^https://raw\.githubusercontent\.com/august-villagegames/winnow/"
    r"(v\d+\.\d+\.\d+)/(?:\.agents/skills/winnow/)?references/seed\.schema\.json$"
)


def schema_version(data: bytes) -> str:
    value = json.loads(data)
    match = SCHEMA_URL.fullmatch(value.get("$id", ""))
    if not match:
        raise ValueError("$id must use an immutable vX.Y.Z tag and the canonical bundle path")
    return match.group(1)


def artifact_version(data: bytes) -> str:
    match = ANY_SCHEMA_URL.fullmatch(json.loads(data).get("$id", ""))
    if not match:
        raise ValueError("base schema has no immutable versioned $id")
    return match.group(1)


def require_new_identity(current: bytes, base: bytes) -> None:
    if current != base and schema_version(current) == artifact_version(base):
        raise ValueError("schema changed without changing its immutable $id")


def require_release(tag: str, current: bytes, tagged: bytes) -> None:
    if schema_version(current) != tag:
        raise ValueError(f"release tag {tag!r} does not match the schema $id tag")
    if current != tagged:
        raise ValueError("the release tag does not contain the schema referenced by $id")


def git_show(ref: str, path: Path) -> bytes:
    return subprocess.check_output(
        ["git", "show", f"{ref}:{path}"], cwd=ROOT, stderr=subprocess.DEVNULL
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-ref")
    parser.add_argument("--verify-tag")
    args = parser.parse_args()
    current_path = ROOT / SCHEMA_PATH
    current = current_path.read_bytes()
    schema_version(current)

    if args.base_ref:
        try:
            base = git_show(args.base_ref, SCHEMA_PATH)
        except subprocess.CalledProcessError:
            base = git_show(args.base_ref, Path("references/seed.schema.json"))
        require_new_identity(current, base)
    if args.verify_tag:
        require_release(args.verify_tag, current, git_show(args.verify_tag, SCHEMA_PATH))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, subprocess.CalledProcessError, json.JSONDecodeError) as error:
        raise SystemExit(f"schema-identity: {error}")
