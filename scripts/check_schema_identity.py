#!/usr/bin/env python3
"""Check that the published Winnow schema identity matches its contents."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path


SCHEMA_PATH = "references/seed.schema.json"
SCHEMA_ID_RE = re.compile(
    r"^https://raw\.githubusercontent\.com/"
    r"august-villagegames/winnow/"
    r"(?P<tag>v\d+\.\d+\.\d+)/references/seed\.schema\.json$"
)


class SchemaIdentityError(ValueError):
    pass


def _schema(data: bytes, source: str) -> dict:
    try:
        value = json.loads(data)
    except json.JSONDecodeError as exc:
        raise SchemaIdentityError(f"{source}: invalid JSON ({exc})") from exc
    if not isinstance(value, dict):
        raise SchemaIdentityError(f"{source}: expected a JSON object")
    return value


def schema_id(data: bytes, source: str = SCHEMA_PATH) -> str:
    value = _schema(data, source)
    identifier = value.get("$id")
    if not isinstance(identifier, str):
        raise SchemaIdentityError(f"{source}: $id must be a string")
    match = SCHEMA_ID_RE.fullmatch(identifier)
    if not match:
        raise SchemaIdentityError(
            f"{source}: $id must use an immutable vX.Y.Z tag URL, got {identifier!r}"
        )
    return identifier


def _git_show(ref: str) -> bytes:
    try:
        result = subprocess.run(
            ["git", "show", f"{ref}:{SCHEMA_PATH}"],
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.decode("utf-8", errors="replace").strip()
        raise SchemaIdentityError(
            f"cannot read {SCHEMA_PATH} from git ref {ref!r}: {detail}"
        ) from exc
    return result.stdout


def check_base(current: bytes, base: bytes, base_ref: str) -> None:
    current_id = schema_id(current, SCHEMA_PATH)
    base_id = schema_id(base, f"{base_ref}:{SCHEMA_PATH}")
    if current != base and current_id == base_id:
        raise SchemaIdentityError(
            f"schema changed since {base_ref} without changing $id ({current_id})"
        )


def check_tag(current: bytes, tag: str, tagged: bytes) -> None:
    current_id = schema_id(current, SCHEMA_PATH)
    expected_tag = SCHEMA_ID_RE.fullmatch(current_id).group("tag")
    if tag != expected_tag:
        raise SchemaIdentityError(
            f"release tag {tag!r} does not match schema $id tag {expected_tag!r}"
        )
    if current != tagged:
        raise SchemaIdentityError(
            f"{tag}:{SCHEMA_PATH} differs from the checked-in schema"
        )


def check_remote(current: bytes) -> None:
    identifier = schema_id(current, SCHEMA_PATH)
    try:
        with urllib.request.urlopen(identifier, timeout=20) as response:
            remote = response.read()
    except (OSError, urllib.error.URLError) as exc:
        raise SchemaIdentityError(
            f"could not fetch schema $id {identifier}: {exc}"
        ) from exc
    if remote != current:
        raise SchemaIdentityError(
            f"schema fetched from $id differs from {SCHEMA_PATH}"
        )


def main(argv: list[str]) -> int:
    try:
        current = Path(SCHEMA_PATH).read_bytes()
        current_id = schema_id(current, SCHEMA_PATH)
        if len(argv) >= 2 and argv[0] == "--base-ref":
            check_base(current, _git_show(argv[1]), argv[1])
            argv = argv[2:]
        if len(argv) >= 2 and argv[0] == "--verify-tag":
            check_tag(current, argv[1], _git_show(argv[1]))
            argv = argv[2:]
        if argv == ["--verify-remote"]:
            check_remote(current)
            argv = []
        if argv:
            raise SchemaIdentityError(f"unsupported arguments: {' '.join(argv)}")
        print(json.dumps({"valid": True, "schemaId": current_id}, separators=(",", ":")))
        return 0
    except (OSError, SchemaIdentityError) as exc:
        print(f"schema-identity: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
