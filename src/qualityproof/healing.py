"""Governed, deterministic locator-healing proposals."""

from __future__ import annotations

import ast
import difflib
import hashlib
import json
from pathlib import Path

from qualityproof.models import (
    AuditEvent,
    FailedLocatorEvidence,
    HealingReview,
    LocatorHealingProposal,
    SemanticCandidate,
)
from qualityproof.repository import SQLiteRepository
from qualityproof.security import EvidenceRedactor, is_within

MAX_CANDIDATES = 10
_WEIGHTS = {"role": 0.25, "name": 0.30, "test_id": 0.30, "context": 0.15}


def _similarity(left: str | None, right: str | None) -> float:
    if not left or not right:
        return 0.0
    return difflib.SequenceMatcher(None, left.casefold(), right.casefold()).ratio()


def _context_score(left: tuple[str, ...], right: tuple[str, ...]) -> float:
    left_set = {item.casefold() for item in left}
    right_set = {item.casefold() for item in right}
    union = left_set | right_set
    return len(left_set & right_set) / len(union) if union else 0.0


def propose_locator_healing(
    failed: FailedLocatorEvidence,
    candidates: tuple[SemanticCandidate, ...],
    *,
    limit: int = 3,
) -> tuple[LocatorHealingProposal, ...]:
    """Rank only candidates preserving the exact behavioral contract."""
    if not 1 <= limit <= MAX_CANDIDATES:
        raise ValueError(f"limit must be between 1 and {MAX_CANDIDATES}")
    if len(candidates) > MAX_CANDIDATES:
        raise ValueError(f"candidate count must not exceed {MAX_CANDIDATES}")
    ast.parse(failed.old_locator, mode="eval")
    ranked: list[tuple[float, str, SemanticCandidate, dict[str, float]]] = []
    for candidate in candidates:
        ast.parse(candidate.locator, mode="eval")
        if candidate.semantics != failed.semantics:
            continue
        evidence = {
            "role": _similarity(failed.role, candidate.role),
            "name": _similarity(failed.name, candidate.name),
            "test_id": _similarity(failed.test_id, candidate.test_id),
            "context": _context_score(failed.context, candidate.context),
        }
        score = sum(evidence[key] * weight for key, weight in _WEIGHTS.items())
        ranked.append((score, candidate.locator, candidate, evidence))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    proposals: list[LocatorHealingProposal] = []
    for score, _, candidate, evidence in ranked[:limit]:
        digest = hashlib.sha256(
            f"{failed.test_path}:{failed.line}:{failed.old_locator}:{candidate.locator}".encode()
        ).hexdigest()[:20]
        proposals.append(
            LocatorHealingProposal(
                id=f"heal-{digest}",
                failed=failed,
                candidate=candidate,
                confidence=round(score, 6),
                score_evidence={key: round(value, 6) for key, value in evidence.items()},
                locator_diff=(
                    f"- {failed.old_locator}\n"
                    f"+ {candidate.locator}\n"
                    f"  assertion unchanged: {failed.assertion}"
                ),
            )
        )
    return tuple(proposals)


def write_proposals(
    project: Path, proposals: tuple[LocatorHealingProposal, ...]
) -> tuple[Path, ...]:
    destination = project / ".qualityproof" / "healing" / "proposals"
    destination.mkdir(parents=True, exist_ok=True)
    redactor = EvidenceRedactor.from_environment()
    paths: list[Path] = []
    for proposal in proposals:
        sanitized = proposal.model_copy(
            update={
                "failed": proposal.failed.model_copy(
                    update={"evidence": redactor.value(proposal.failed.evidence)}
                )
            }
        )
        path = destination / f"{proposal.id}.json"
        if path.exists():
            existing = LocatorHealingProposal.model_validate_json(path.read_text(encoding="utf-8"))
            if existing != sanitized:
                raise ValueError(f"immutable proposal already exists: {path}")
        else:
            path.write_text(sanitized.model_dump_json(indent=2) + "\n", encoding="utf-8")
        paths.append(path)
    return tuple(paths)


def review_proposal(
    project: Path,
    repository: SQLiteRepository,
    proposal_path: Path,
    decision: str,
    actor: str,
    reason: str,
) -> HealingReview:
    """Record human review; approval writes a patch and never edits test sources."""
    if decision not in {"approve", "reject"}:
        raise ValueError("decision must be approve or reject")
    proposal = LocatorHealingProposal.model_validate_json(
        proposal_path.read_text(encoding="utf-8")
    )
    patch_path: Path | None = None
    if decision == "approve":
        source_path = (project / proposal.failed.test_path).resolve()
        if not is_within(source_path, project) or not source_path.is_file():
            raise ValueError("healing source must be an existing file inside the project")
        if source_path.stat().st_size > 1_000_000:
            raise ValueError("healing source exceeds the 1 MB safety limit")
        source = source_path.read_text(encoding="utf-8")
        ast.parse(source, filename=str(source_path))
        lines = source.splitlines(keepends=True)
        line_index = proposal.failed.line - 1
        if line_index < 0 or line_index >= len(lines):
            raise ValueError("failed locator line is outside the source file")
        original_line = lines[line_index]
        if original_line.count(proposal.failed.old_locator) != 1:
            raise ValueError("failed locator must occur exactly once on the recorded source line")
        if original_line.strip() != proposal.failed.assertion:
            raise ValueError("recorded assertion context does not exactly match the source line")
        replacement_line = original_line.replace(
            proposal.failed.old_locator,
            proposal.candidate.locator,
            1,
        )
        if len(replacement_line) > 10_000:
            raise ValueError("healing replacement exceeds the line length safety limit")
        updated_lines = list(lines)
        updated_lines[line_index] = replacement_line
        ast.parse("".join(updated_lines), filename=str(source_path))
        patch = "".join(
            difflib.unified_diff(
                lines,
                updated_lines,
                fromfile=f"a/{proposal.failed.test_path}",
                tofile=f"b/{proposal.failed.test_path}",
                n=3,
            )
        )
        if not patch or len(patch.encode("utf-8")) > 100_000:
            raise ValueError("healing patch is empty or exceeds the 100 KB safety limit")
        patch_path = project / ".qualityproof" / "healing" / "patches" / f"{proposal.id}.patch"
        patch_path.parent.mkdir(parents=True, exist_ok=True)
        if patch_path.exists() and patch_path.read_text(encoding="utf-8") != patch:
            raise ValueError(f"immutable patch already exists: {patch_path}")
        patch_path.write_text(patch, encoding="utf-8")
    review = HealingReview(
        proposal_id=proposal.id,
        decision=decision,
        actor=actor,
        reason=reason,
        patch_path=str(patch_path.relative_to(project)) if patch_path else None,
    )
    repository.append_event(
        AuditEvent(
            id=f"{proposal.id}-{review.reviewed_at.timestamp()}",
            event_type=f"healing_proposal_{decision}d",
            actor=actor,
            details={
                "proposal_id": proposal.id,
                "reason": reason,
                "patch_path": review.patch_path,
            },
        )
    )
    review_path = project / ".qualityproof" / "healing" / "reviews" / f"{proposal.id}.json"
    review_path.parent.mkdir(parents=True, exist_ok=True)
    review_path.write_text(
        json.dumps(review.model_dump(mode="json"), indent=2) + "\n",
        encoding="utf-8",
    )
    return review
