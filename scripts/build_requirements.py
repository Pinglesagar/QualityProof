"""Generate the machine-readable requirement registry from the SRS document.

The SRS prose is authoritative. Hand-maintaining a parallel YAML file guarantees
the two drift, and a drifted registry is worse than none: it reports coverage of
requirements nobody wrote. Generating it means the document and the registry
cannot disagree, and CI can prove they don't.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]
DEFAULT_SOURCE = ROOT / "docs" / "project" / "SRS.md"
DEFAULT_TARGET = ROOT / "docs" / "project" / "requirements.yaml"

#: "### JS-CAT-1 — Catalogue is reachable without authentication"
HEADING = re.compile(r"^###\s+(?P<id>JS-[A-Z]+-\d+)\s+[—-]\s+(?P<title>.+?)\s*$")
PRIORITY = re.compile(r"\*Priority:\s*(?P<priority>P\d)\.", re.I)
AREA_HEADING = re.compile(r"^##\s+\d+\.\s+(?P<area>.+?)\s*$")


def parse_srs(text: str) -> list[dict[str, object]]:
    """Extract one requirement per level-three heading, with its body as description."""
    lines = text.splitlines()
    requirements: list[dict[str, object]] = []
    area = "General"
    current: dict[str, object] | None = None
    body: list[str] = []

    def flush() -> None:
        if current is None:
            return
        prose = " ".join(" ".join(body).split())
        # The description is the requirement statement with the trailing metadata
        # sentence removed, so a content hash covers the obligation itself and not
        # the annotation around it.
        statement = prose.split("*Priority:")[0].strip()
        priority = PRIORITY.search(prose)
        current["description"] = statement
        current["priority"] = priority.group("priority") if priority else "P2"
        requirements.append(current)

    for line in lines:
        area_match = AREA_HEADING.match(line)
        if area_match:
            area = area_match.group("area")
            continue
        heading = HEADING.match(line)
        if heading:
            flush()
            body = []
            current = {
                "id": heading.group("id"),
                "title": heading.group("title"),
                "area": area,
            }
            continue
        if current is not None:
            if line.startswith(("## ", "---")):
                flush()
                current = None
                body = []
                continue
            body.append(line)
    flush()
    return requirements


def build(source: Path, target: Path) -> list[dict[str, object]]:
    requirements = parse_srs(source.read_text(encoding="utf-8"))
    if not requirements:
        raise SystemExit(f"no requirements parsed from {source}")
    identifiers = [str(item["id"]) for item in requirements]
    duplicates = sorted({item for item in identifiers if identifiers.count(item) > 1})
    if duplicates:
        raise SystemExit(f"duplicate requirement ids in {source}: {', '.join(duplicates)}")
    payload = {
        "schema_version": "qualityproof-requirements/v1",
        "source_document": str(source.relative_to(ROOT).as_posix()),
        "notice": (
            "GENERATED from the SRS. Do not edit by hand; run "
            "`python -m scripts.build_requirements` instead."
        ),
        "requirements": requirements,
    }
    target.write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True, width=88),
        encoding="utf-8",
    )
    return requirements


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--target", type=Path, default=DEFAULT_TARGET)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Fail if the generated file would differ, for CI drift detection.",
    )
    args = parser.parse_args()
    if args.check:
        existing = args.target.read_text(encoding="utf-8") if args.target.is_file() else ""
        build(args.source, args.target)
        if args.target.read_text(encoding="utf-8") != existing:
            raise SystemExit(
                f"{args.target.name} is out of date with {args.source.name}; regenerate it"
            )
        print(f"{args.target.name} matches {args.source.name}")
        return
    requirements = build(args.source, args.target)
    print(f"Wrote {len(requirements)} requirements to {args.target}")
    for item in requirements:
        print(f"  {item['id']:<16}{item['priority']}  {item['title']}")


if __name__ == "__main__":
    main()
