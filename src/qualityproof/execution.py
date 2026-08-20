"""Execute generated and custom pytest-Playwright tests with normalized evidence."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from qualityproof.config import load_config
from qualityproof.generation import reconcile_generated
from qualityproof.models import TestRunResult, Verdict, VerdictStatus
from qualityproof.repository import SQLiteRepository
from qualityproof.scenarios import (
    assert_custom_unchanged,
    custom_tree_digest,
)
from qualityproof.security import ArtifactPolicy, EvidenceRedactor, reject_custom_path

_UNREDACTABLE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".zip", ".webp", ".webm"})


def _normalize(text: str, project: Path) -> str:
    return text.replace(str(project.resolve()), "<PROJECT>").replace("\\", "/")


def _custom_yaml_tests(project: Path) -> tuple[Path, ...]:
    custom = project / "scenarios" / "custom"
    destination = project / ".qualityproof" / "generated-custom"
    reject_custom_path(project, destination, "generated custom-test output")
    sources = tuple(sorted((*custom.glob("*.yaml"), *custom.glob("*.yml"))))
    return reconcile_generated(
        project,
        sources,
        destination,
        filename_prefix="test_custom_",
        validate=False,
    )


ALLOWED_EXTRA_ARGUMENTS = (
    "-k",
    "-m",
    "-x",
    "-q",
    "-v",
    "--maxfail",
    "--deselect",
    "--last-failed",
    "--lf",
    "--durations",
)


def validate_extra_arguments(extra_args: tuple[str, ...]) -> tuple[str, ...]:
    """Refuse pytest arguments that could redirect execution or capture.

    Every other input path in this project is validated before use; the test
    runner's argument list is no exception. An unchecked pass-through would let a
    caller re-enable artifact capture, change the output directory, or load an
    arbitrary plugin, all of which the artifact and evidence policies exist to
    control.
    """
    for argument in extra_args:
        head = argument.split("=", 1)[0]
        if head.startswith("-") and head not in ALLOWED_EXTRA_ARGUMENTS:
            raise ValueError(
                f"pytest argument {head!r} is not permitted; "
                f"allowed options are {', '.join(ALLOWED_EXTRA_ARGUMENTS)}"
            )
    return extra_args


def _rerun_nodeids(rerun_log: Path) -> frozenset[str]:
    """Read the retried-test node ids recorded by the fixtures hook."""
    if not rerun_log.is_file():
        return frozenset()
    nodeids: set[str] = set()
    for line in rerun_log.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        nodeid = record.get("nodeid")
        if isinstance(nodeid, str) and nodeid:
            nodeids.add(nodeid)
    return frozenset(nodeids)


def _junit_identifier(classname: str, name: str) -> str:
    return f"{classname}::{name}"


def _matches_nodeid(identifier: str, nodeid: str) -> bool:
    """Match a JUnit identifier against a pytest node id.

    JUnit reports ``path.to.module::test_name[param]`` while pytest node ids are
    ``path/to/module.py::test_name[param]``. Comparing the test-name tail is
    sufficient here and avoids depending on either writer's path spelling.
    """
    junit_tail = identifier.rsplit("::", 1)[-1]
    node_tail = nodeid.rsplit("::", 1)[-1]
    return junit_tail == node_tail


def _execution_verdicts(
    junit_path: Path,
    test_names: tuple[str, ...],
    return_code: int,
    run_id: str,
    evidence: tuple[str, ...],
    rerun_log: Path | None = None,
) -> tuple[tuple[str, Verdict], ...]:
    """Derive one verdict per test case, treating a rerun pass as FLAKY.

    JUnit records a rerun as an extra ``testcase`` element for the same
    identifier. Collapsing those to "passed" is the industry default and it is
    how real instability disappears from dashboards, so a test that needed a
    rerun is recorded FLAKY: still not a pass, and never silently green.
    """
    verdicts: list[tuple[str, Verdict]] = []
    retried = _rerun_nodeids(rerun_log) if rerun_log is not None else frozenset()
    if junit_path.is_file():
        root = ET.parse(junit_path).getroot()
        observations: dict[str, list[str]] = {}
        for case in root.iter("testcase"):
            classname = case.attrib.get("classname", "pytest")
            name = case.attrib.get("name", "unknown")
            identifier = _junit_identifier(classname, name)
            # Some writers do emit a <rerun> element; the sidecar covers the ones
            # that do not, which includes pytest's own.
            if case.find("rerun") is not None or any(
                _matches_nodeid(identifier, nodeid) for nodeid in retried
            ):
                observations.setdefault(identifier, []).append("rerun")
            if case.find("failure") is not None or case.find("error") is not None:
                observations.setdefault(identifier, []).append("failed")
            elif case.find("skipped") is not None:
                observations.setdefault(identifier, []).append("skipped")
            else:
                observations.setdefault(identifier, []).append("passed")
        for identifier, outcomes in observations.items():
            unique = set(outcomes)
            if "failed" in unique and "passed" in unique:
                status = VerdictStatus.FLAKY
                rationale = (
                    "pytest recorded both a failure and a pass for this test; "
                    "a rerun pass is reported as flaky, not as a pass"
                )
            elif "rerun" in unique and "passed" in unique:
                status = VerdictStatus.FLAKY
                rationale = "pytest needed a rerun before this test passed"
            elif "failed" in unique:
                status = VerdictStatus.FAIL
                rationale = "pytest JUnit recorded a failure or error"
            elif unique == {"skipped"}:
                status = VerdictStatus.INCONCLUSIVE
                rationale = "pytest JUnit recorded a skipped test"
            else:
                status = VerdictStatus.PASS
                rationale = "pytest JUnit recorded a passing test"
            verdicts.append(
                (
                    identifier,
                    Verdict(
                        assertion_id=identifier,
                        status=status,
                        rationale=f"{rationale} in run {run_id}",
                        evidence_ids=evidence,
                    ),
                )
            )
    if verdicts:
        return tuple(sorted(verdicts, key=lambda item: item[0]))
    fallback_status = VerdictStatus.PASS if return_code == 0 else VerdictStatus.FAIL
    return tuple(
        (
            test_path,
            Verdict(
                assertion_id=test_path,
                status=fallback_status,
                rationale=(
                    f"pytest run {run_id} exited {return_code}; "
                    "no JUnit cases were available"
                ),
                evidence_ids=evidence,
            ),
        )
        for test_path in test_names
    )


def _redact_junit(junit_path: Path, redactor: EvidenceRedactor, project: Path) -> None:
    """Rewrite the JUnit report with secrets removed, preserving well-formedness.

    Redaction walks the parsed tree rather than the raw bytes: the replacement
    token contains angle brackets, so a plain string substitution would inject
    what looks like markup and leave the file unparseable. Serializing from the
    tree escapes it correctly.
    """
    tree = ET.parse(junit_path)
    for element in tree.getroot().iter():
        if element.text:
            element.text = redactor.text(_normalize(element.text, project))
        if element.tail:
            element.tail = redactor.text(_normalize(element.tail, project))
        for name, value in list(element.attrib.items()):
            element.attrib[name] = redactor.text(_normalize(value, project))
    tree.write(junit_path, encoding="utf-8", xml_declaration=True)


def _collection_targets(paths: tuple[Path, ...]) -> tuple[str, ...]:
    """Collapse fully-selected directories into a single pytest argument.

    Passing one positional argument per test file makes pytest's collection
    quadratic in file count: every argument re-scans the directory it names. At
    800 generated files that measured 80 s serial against 1.75 s for the same
    files handed over as a directory. Individual files are still passed when only
    part of a directory is selected, which is what sharding does.
    """
    selected = {path.resolve() for path in paths}
    targets: list[str] = []
    consumed: set[Path] = set()
    for directory in sorted({path.parent.resolve() for path in paths}):
        on_disk = {
            candidate.resolve()
            for candidate in directory.glob("test_*.py")
            if candidate.is_file()
        }
        if on_disk and on_disk <= selected:
            targets.append(str(directory))
            consumed |= on_disk
    targets.extend(
        str(path) for path in sorted(selected - consumed)
    )
    return tuple(targets)


def _worker_argument(configured: str) -> tuple[str, ...]:
    """Translate the configured worker count into xdist arguments."""
    if configured in {"0", "1"}:
        return ()
    return ("-n", configured, "--dist", "loadscope")


def execute_tests(
    project: Path,
    extra_args: tuple[str, ...] = (),
    *,
    repository: SQLiteRepository | None = None,
    shard: tuple[int, int] | None = None,
) -> TestRunResult:
    custom_before = custom_tree_digest(project)
    redactor = EvidenceRedactor.from_environment()
    policy = ArtifactPolicy.from_environment()
    config = load_config(project)
    validate_extra_arguments(extra_args)
    generated = tuple(sorted((project / ".qualityproof" / "generated").glob("test_*.py")))
    custom_python = tuple(sorted((project / "scenarios" / "custom").glob("test_*.py")))
    paths = (*generated, *_custom_yaml_tests(project), *custom_python)
    if not paths:
        raise ValueError("no generated or custom tests found")
    if shard is not None:
        index, total = shard
        if not 1 <= index <= total:
            raise ValueError("shard index must be between 1 and the shard total")
        # Deterministic, stable partitioning: the same file always lands in the
        # same shard regardless of how many other files exist.
        paths = tuple(
            path
            for position, path in enumerate(sorted(paths))
            if position % total == index - 1
        )
        if not paths:
            raise ValueError(f"shard {index}/{total} selected no tests")
    started = datetime.now(UTC)
    run_id = f"run-{uuid4().hex}"
    run_directory = project / ".qualityproof" / "runs" / run_id
    evidence_directory = run_directory / "evidence"
    evidence_directory.mkdir(parents=True, exist_ok=True)
    junit_path = run_directory / "junit.xml"
    rerun_log = run_directory / "reruns.jsonl"
    command = [
        # Resolve the interpreter explicitly. Invoking a bare "pytest" from PATH
        # can run a different environment's runner than the one this process was
        # installed into, silently testing the wrong dependency set.
        sys.executable,
        "-m",
        "pytest",
        "-q",
        *policy.pytest_arguments(),
        *_worker_argument(config.workers),
        *(("--reruns", str(config.retries)) if config.retries else ()),
        f"--output={evidence_directory}",
        f"--junitxml={junit_path}",
        *_collection_targets(paths),
        *extra_args,
    ]
    completed = subprocess.run(
        command,
        cwd=project,
        env={
            **os.environ,
            "PYTHONDONTWRITEBYTECODE": "1",
            "QUALITYPROOF_PROJECT": str(project.resolve()),
            "QUALITYPROOF_RERUN_LOG": str(rerun_log),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    finished = datetime.now(UTC)
    quarantine = run_directory / "quarantine"
    for artifact in sorted(evidence_directory.rglob("*")):
        if not artifact.is_file():
            continue
        if artifact.suffix.casefold() in _UNREDACTABLE_SUFFIXES:
            if policy.quarantined:
                # Retained but never published: a trace zip and a screenshot
                # cannot be post-redacted, so the only honest options are to
                # discard them or to isolate and label them.
                quarantine.mkdir(parents=True, exist_ok=True)
                artifact.replace(quarantine / artifact.name)
            elif policy.traces_enabled:
                continue
            else:
                artifact.unlink()
            continue
        try:
            artifact.write_text(
                redactor.text(artifact.read_text(encoding="utf-8")),
                encoding="utf-8",
            )
        except UnicodeDecodeError:
            artifact.unlink()
    if quarantine.is_dir():
        (quarantine / "UNREDACTED.md").write_text(
            "# Unredacted artifacts\n\n"
            "These trace and image files were captured from an authenticated run and "
            "cannot be reliably redacted. They are excluded from ledger reports, "
            "evidence snapshots and any published output. Treat them as containing "
            "live session credentials and delete them once diagnosis is complete.\n",
            encoding="utf-8",
        )
    evidence = tuple(
        sorted(
            str(path.relative_to(project))
            for path in evidence_directory.rglob("*")
            if path.is_file()
        )
    )
    result_path = run_directory / "result.json"
    status = "passed" if completed.returncode == 0 else "failed"
    test_names = tuple(str(path.relative_to(project)) for path in paths)
    payload = {
        "version": 1,
        "run_id": run_id,
        "status": status,
        "exit_code": completed.returncode,
        "tests": list(test_names),
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "stdout": redactor.text(_normalize(completed.stdout, project)),
        "stderr": redactor.text(_normalize(completed.stderr, project)),
        "evidence": list(evidence),
        "junit_path": (
            str(junit_path.relative_to(project)) if junit_path.is_file() else None
        ),
        "artifact_policy": policy.describe(),
        "retried_tests": sorted(_rerun_nodeids(rerun_log)),
        "workers": config.workers,
        "reruns": config.retries,
        "shard": list(shard) if shard is not None else None,
    }
    result_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    assert_custom_unchanged(project, custom_before)
    result = TestRunResult(
        run_id=run_id,
        status=status,
        exit_code=completed.returncode,
        test_paths=test_names,
        result_path=str(result_path.relative_to(project)),
        evidence_paths=evidence,
        started_at=started,
        finished_at=finished,
    )
    if repository is not None:
        repository.put("test_run", run_id, result)
        repository.replace_sets(
            {
                "verdict": _execution_verdicts(
                    junit_path,
                    test_names,
                    completed.returncode,
                    run_id,
                    evidence,
                    rerun_log=rerun_log,
                )
            }
        )
    if junit_path.is_file():
        # Retained, not deleted: this file is the run's primary machine-readable
        # evidence, and an evidence-integrity tool destroying it after parsing
        # was indefensible. Redacted only after the verdicts are derived.
        _redact_junit(junit_path, redactor, project)
    return result
