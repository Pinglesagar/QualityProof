"""Static auditing of Python pytest and Playwright test source."""

from __future__ import annotations

import ast
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import ValidationError

from qualityproof.models import (
    AuditedTest,
    AuditEvent,
    LedgerEntry,
    LedgerStatus,
    Provenance,
    ProvenanceKind,
    Requirement,
    SourceAssertion,
    TestMetadata,
)
from qualityproof.repository import SQLiteRepository
from qualityproof.security import is_within

#: Provenance sources are read whole, so an unbounded read is a denial-of-service
#: surface as well as a correctness risk.
MAX_SOURCE_BYTES = 5_000_000


@dataclass(frozen=True)
class _Context:
    name: str
    metadata: TestMetadata | None


def _decorator_call(node: ast.expr) -> ast.Call | None:
    if not isinstance(node, ast.Call):
        return None
    name = ast.unparse(node.func)
    if name == "qualityproof" or name.endswith(".qualityproof"):
        return node
    return None


def _literal_keyword(call: ast.Call, name: str, default: object) -> object:
    for keyword in call.keywords:
        if keyword.arg == name:
            return ast.literal_eval(keyword.value)
    return default


def _metadata_from_decorators(decorators: list[ast.expr]) -> TestMetadata | None:
    for decorator in decorators:
        call = _decorator_call(decorator)
        if call is None:
            continue
        try:
            requirements = _literal_keyword(call, "requirements", ())
            provenance = _literal_keyword(call, "provenance", ())
            if not isinstance(requirements, (list, tuple)) or not isinstance(
                provenance, (list, tuple)
            ):
                return None
            return TestMetadata(
                requirement_ids=tuple(str(item) for item in requirements),
                provenance=tuple(Provenance.model_validate(item) for item in provenance),
            )
        except (ValueError, TypeError, ValidationError):
            return None
    return None


def _merge_metadata(parent: TestMetadata | None, child: TestMetadata | None) -> TestMetadata | None:
    if parent is None:
        return child
    if child is None:
        return parent
    return TestMetadata(
        requirement_ids=tuple(dict.fromkeys((*parent.requirement_ids, *child.requirement_ids))),
        provenance=(*parent.provenance, *child.provenance),
    )


def _is_expect_call(node: ast.Call) -> bool:
    """Recognise a matcher call terminating an ``expect`` chain.

    The subject must be an *invoked* expect, which covers ``expect(x)`` and also
    the modifier forms ``expect.soft(x)`` and ``expect.poll(f)``. An earlier
    version required the invocation's callee to be the bare name ``expect``, so
    every soft assertion was invisible — including the ones this project's own
    generator emits, which reported generated tests as having no assertions at
    all. Requiring an invocation also keeps ``expect.soft(x)`` on its own from
    counting as an assertion in its own right.
    """
    if not isinstance(node.func, ast.Attribute):
        return False
    subject: ast.expr = node.func.value
    # Descend modifier chains such as expect(x).not_to_...
    while isinstance(subject, ast.Attribute):
        subject = subject.value
    if not isinstance(subject, ast.Call):
        return False
    callee: ast.expr = subject.func
    while isinstance(callee, ast.Attribute):
        callee = callee.value
    return isinstance(callee, ast.Name) and callee.id == "expect"


def _assertions(function: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[SourceAssertion, ...]:
    class ScopedAssertionVisitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.found: list[SourceAssertion] = []

        def visit_Assert(self, node: ast.Assert) -> None:
            self.found.append(
                SourceAssertion(kind="assert", line=node.lineno, expression=ast.unparse(node.test))
            )
            self.generic_visit(node)

        def visit_Call(self, node: ast.Call) -> None:
            if _is_expect_call(node):
                self.found.append(
                    SourceAssertion(kind="expect", line=node.lineno, expression=ast.unparse(node))
                )
            self.generic_visit(node)

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            del node

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            del node

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            del node

        def visit_Lambda(self, node: ast.Lambda) -> None:
            del node

    visitor = ScopedAssertionVisitor()
    for statement in function.body:
        visitor.visit(statement)
    return tuple(sorted(visitor.found, key=lambda item: item.line))


def _framework(assertions: tuple[SourceAssertion, ...], tree: ast.Module) -> str:
    if any(item.kind == "expect" for item in assertions):
        return "playwright"
    imported = {
        ast.unparse(node)
        for node in tree.body
        if isinstance(node, (ast.Import, ast.ImportFrom))
    }
    if any("pytest" in item for item in imported):
        return "pytest"
    return "python"


def _audit_function(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    source_path: Path,
    tree: ast.Module,
    context: _Context | None,
) -> AuditedTest | None:
    own_metadata = _metadata_from_decorators(node.decorator_list)
    metadata = _merge_metadata(context.metadata if context else None, own_metadata)
    if not node.name.startswith("test_") and metadata is None:
        return None
    qualified_name = f"{context.name}.{node.name}" if context else node.name
    assertions = _assertions(node)
    return AuditedTest(
        id=f"{source_path.as_posix()}::{qualified_name}",
        path=source_path.as_posix(),
        name=qualified_name,
        line=node.lineno,
        framework=_framework(assertions, tree),
        assertions=assertions,
        metadata=metadata,
    )


def audit_file(path: Path, root: Path | None = None) -> tuple[AuditedTest, ...]:
    """Parse one Python file; syntax errors are surfaced to the caller."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    display_path = path.relative_to(root) if root is not None else path
    tests: list[AuditedTest] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            audited = _audit_function(node, display_path, tree, None)
            if audited is not None:
                tests.append(audited)
        elif isinstance(node, ast.ClassDef):
            context = _Context(node.name, _metadata_from_decorators(node.decorator_list))
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    audited = _audit_function(child, display_path, tree, context)
                    if audited is not None:
                        tests.append(audited)
    return tuple(tests)


def audit_path(path: Path) -> tuple[AuditedTest, ...]:
    """Audit a file or recursively audit Python files under a directory."""
    resolved = path.resolve()
    if resolved.is_file():
        return audit_file(resolved, resolved.parent)
    if not resolved.is_dir():
        raise FileNotFoundError(path)
    tests: list[AuditedTest] = []
    for source in sorted(resolved.rglob("*.py")):
        if any(part.startswith(".") for part in source.relative_to(resolved).parts):
            continue
        tests.extend(audit_file(source, resolved))
    return tuple(tests)


def _find_identifier(value: object, collection: str, identifier: str) -> object | None:
    if isinstance(value, dict):
        entries = value.get(collection)
        if isinstance(entries, list):
            for entry in entries:
                if isinstance(entry, dict) and str(entry.get("id", "")) == identifier:
                    return entry
        for child in value.values():
            found = _find_identifier(child, collection, identifier)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_identifier(child, collection, identifier)
            if found is not None:
                return found
    return None


def _find_operation(value: object, identifier: str) -> object | None:
    if isinstance(value, dict):
        if value.get("operationId") == identifier:
            return value
        for child in value.values():
            found = _find_operation(child, identifier)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_operation(child, identifier)
            if found is not None:
                return found
    return None


def _resolve_locator(content: str, locator: str, suffix: str) -> tuple[bytes | None, str | None]:
    if locator.startswith(("requirement:", "seed:", "operation:")):
        try:
            payload = (
                json.loads(content)
                if suffix == ".json"
                else yaml.safe_load(content)
            )
        except (json.JSONDecodeError, yaml.YAMLError):
            return None, None
        kind, identifier = locator.split(":", 1)
        located = (
            _find_operation(payload, identifier)
            if kind == "operation"
            else _find_identifier(
                payload,
                "requirements" if kind == "requirement" else "seeds",
                identifier,
            )
        )
        if located is None:
            return None, None
        if kind == "requirement" and isinstance(located, dict):
            value: object = located.get("description", located)
        else:
            value = located
        rendered = (
            value
            if isinstance(value, str)
            else json.dumps(value, sort_keys=True, separators=(",", ":"))
        )
        return str(rendered).encode(), identifier if kind in {"requirement", "seed"} else None
    heading = locator.removeprefix("heading:").removeprefix("#")
    if heading != locator or locator.startswith("#"):
        lines = content.splitlines()
        for index, line in enumerate(lines):
            if line.lstrip("# ").split(":", 1)[0].strip() == heading:
                body: list[str] = []
                for following in lines[index + 1 :]:
                    if following.startswith("#"):
                        break
                    body.append(following)
                resolved = "\n".join(body).strip() or line.lstrip("# ").strip()
                return resolved.encode(), heading
    return None, None


@dataclass(frozen=True)
class ProvenanceResolver:
    """Decide whether one provenance record actually establishes what it claims.

    Three properties are load-bearing and were previously weaker than documented:

    * A human approval is only credited when a persisted review event corroborates
      it. Trusting the record itself made ``HUMAN_APPROVED`` self-certifying.
    * When a locator names a fragment, the recorded hash must match *that
      fragment*. Accepting a whole-file digest let a hash pass while the locator
      pointed somewhere else entirely.
    * Sources resolve inside the project only. A working-directory fallback meant
      the same audit could pass or fail depending on where it was invoked from.
    """

    project: Path
    requirements: tuple[Requirement, ...] = ()
    review_events: tuple[AuditEvent, ...] = ()
    #: True when a repository was consulted, so an absence of review events is
    #: evidence that no approval was recorded rather than a lack of information.
    has_repository: bool = False

    @property
    def registered_ids(self) -> frozenset[str]:
        return frozenset(requirement.id for requirement in self.requirements)

    def _approval_is_corroborated(self, provenance: Provenance) -> bool:
        if not provenance.is_approved:
            return False
        relevant = tuple(
            event
            for event in self.review_events
            if event.event_type
            in {
                "scenario_approved",
                "scenario_edited",
                "healing_proposal_approved",
            }
        )
        if not relevant:
            # A repository that holds no approval events is a repository in which
            # nothing was approved. Only a resolver with no repository at all may
            # fall back to trusting the record.
            return not self.has_repository
        # Identity, not containment. A substring test meant `source="e"` matched
        # every review event, so one genuine approval anywhere in the project could
        # be borrowed to mark an unrelated test verified.
        return any(
            event.actor == provenance.approved_by
            and event.occurred_at == provenance.approved_at
            and (
                provenance.source == event.id
                or provenance.source
                in {str(value) for value in event.details.values() if value is not None}
            )
            for event in relevant
        )

    def _resolve_source(self, source_value: str) -> Path | None:
        """Locate a provenance source, refusing anything outside the project."""
        source = Path(source_value)
        path = source if source.is_absolute() else self.project / source
        # Containment applies to absolute paths too. Exempting them was an escape
        # hatch that let the evidence for a VERIFIED requirement live entirely
        # outside the repository, and therefore outside code review.
        if not is_within(path, self.project):
            return None
        if not path.is_file() or path.stat().st_size > MAX_SOURCE_BYTES:
            return None
        return path

    def _binds_registered_requirement(
        self, associated_id: str, path: Path, located: bytes | None
    ) -> bool:
        """Require a located claim to point at the requirement's own source.

        The registry records where each requirement is specified. A test citing an
        identifier must resolve it in *that* place, and where the registry holds a
        digest for it, the located fragment must match. Otherwise a locator is
        merely a string that happens to appear in a file of the test's choosing.
        """
        if not self.requirements:
            return True
        matches = [item for item in self.requirements if item.id == associated_id]
        if not matches:
            return False
        requirement = matches[0]
        declared = tuple(item.source for item in requirement.provenance)
        if declared:
            resolved = path.resolve()
            permitted = set()
            for source in declared:
                candidate = Path(source)
                permitted.add(
                    (candidate if candidate.is_absolute() else self.project / candidate).resolve()
                )
            if resolved not in permitted:
                return False
        recorded = tuple(
            item.content_hash for item in requirement.provenance if item.content_hash
        )
        if recorded and located is not None:
            digest = hashlib.sha256(located).hexdigest()
            if not any(
                digest == value.removeprefix("sha256:").lower() for value in recorded
            ):
                return False
        return True

    def resolve(self, provenance: Provenance, requirement_ids: tuple[str, ...]) -> bool:
        if provenance.is_expired():
            return False
        if provenance.kind is ProvenanceKind.HUMAN_APPROVED or (
            provenance.kind is ProvenanceKind.AI_HYPOTHESIS and provenance.is_approved
        ):
            return self._approval_is_corroborated(provenance)
        if provenance.kind not in {ProvenanceKind.REQUIREMENT, ProvenanceKind.API_SPEC}:
            return False
        path = self._resolve_source(provenance.source)
        if path is None:
            return False
        content = path.read_text(encoding="utf-8")
        located: bytes | None = None
        associated_id: str | None = None
        if provenance.locator is not None:
            located, associated_id = _resolve_locator(
                content,
                provenance.locator,
                path.suffix.casefold(),
            )
            if located is None:
                return False
        if provenance.content_hash is not None:
            # A locator narrows the claim to a fragment, so the digest must cover
            # that fragment. Only an unlocated source may be hashed whole.
            target = located if located is not None else content.encode()
            if not provenance.validates_content(target):
                return False
        registered = self.registered_ids
        if provenance.kind is ProvenanceKind.REQUIREMENT:
            if associated_id is not None:
                if associated_id not in requirement_ids:
                    return False
                if registered and associated_id not in registered:
                    # The registry is the authority on which requirements exist.
                    return False
                # A locator must not make the check weaker than no locator at all.
                # Without this, any self-authored file containing the identifier
                # satisfied the gate, so a test could cite a description it wrote
                # itself and be credited against the real requirement.
                return self._binds_registered_requirement(
                    associated_id, path, located
                )
            return any(
                requirement.id in requirement_ids
                and any(item.source == provenance.source for item in requirement.provenance)
                for requirement in self.requirements
            )
        # API_SPEC attests to a specific operation. Without a resolvable locator
        # the claim was satisfiable by hashing any readable file in the tree.
        if provenance.locator is None or associated_id is not None or located is None:
            return False
        if registered:
            return bool(requirement_ids) and set(requirement_ids).issubset(registered)
        return False


def classify(
    test: AuditedTest,
    *,
    resolver: ProvenanceResolver | None = None,
) -> LedgerEntry:
    """Apply conservative source-evidence rules to an audited test."""
    metadata = test.metadata
    if metadata is None or (not metadata.requirement_ids and not metadata.provenance):
        return LedgerEntry(
            id=test.id,
            status=LedgerStatus.UNKNOWN,
            reason="No QualityProof metadata; zero-config classification is unknown.",
            test=test,
        )
    if not test.assertions:
        return LedgerEntry(
            id=test.id,
            status=LedgerStatus.PARTIAL,
            reason=(
                "Traceability metadata exists, but no source assertion or expect call was found."
            ),
            test=test,
        )
    if not metadata.requirement_ids:
        return LedgerEntry(
            id=test.id,
            status=LedgerStatus.PARTIAL,
            reason="Assertions exist, but no requirement identifier is linked.",
            test=test,
        )
    authoritative = (
        any(
            item.is_authoritative()
            and resolver.resolve(item, metadata.requirement_ids)
            for item in metadata.provenance
        )
        if resolver is not None
        else any(item.is_authoritative() for item in metadata.provenance)
    )
    if authoritative:
        return LedgerEntry(
            id=test.id,
            status=LedgerStatus.VERIFIED,
            reason="Assertions and requirement links have active authoritative provenance.",
            test=test,
        )
    return LedgerEntry(
        id=test.id,
        status=LedgerStatus.PARTIAL,
        reason=(
            "Assertions and requirement links exist, but provenance is absent, unresolved, "
            "mismatched, expired, observational/baseline-only, or lacks a persisted approval."
        ),
        test=test,
    )


def build_ledger(
    tests: tuple[AuditedTest, ...],
    *,
    project: Path | None = None,
    repository: SQLiteRepository | None = None,
) -> tuple[LedgerEntry, ...]:
    resolver = (
        ProvenanceResolver(
            project=project,
            requirements=repository.list("requirement", Requirement) if repository else (),
            review_events=repository.list_events() if repository else (),
            has_repository=repository is not None,
        )
        if project is not None
        else None
    )
    return tuple(classify(test, resolver=resolver) for test in tests)
