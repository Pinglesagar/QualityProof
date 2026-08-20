"""Execution policy: honest flake accounting, argument safety, deterministic shards."""

from __future__ import annotations

import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from qualityproof.execution import (
    _execution_verdicts,
    _matches_nodeid,
    _rerun_nodeids,
    _worker_argument,
    validate_extra_arguments,
)
from qualityproof.models import VerdictStatus
from qualityproof.security import ArtifactMode, ArtifactPolicy


def _junit(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "junit.xml"
    path.write_text(
        f'<?xml version="1.0" encoding="utf-8"?><testsuites><testsuite>{body}'
        "</testsuite></testsuites>",
        encoding="utf-8",
    )
    return path


def test_pytest_junit_alone_cannot_reveal_a_rerun(tmp_path: Path) -> None:
    """Pin the runner behaviour this design exists to work around.

    A genuinely flapping test under --reruns is written to JUnit as a single
    clean <testcase>: no <rerun> child, no duplicate entry. Anything that reads
    only the XML therefore reports a retried test as a straight pass. This test
    fails if pytest ever starts recording retries in its XML, which would mean
    the sidecar could be retired.
    """
    marker = tmp_path / "attempts"
    spec = tmp_path / "test_flap.py"
    spec.write_text(
        "import pathlib\n"
        f"MARKER = pathlib.Path({str(marker)!r})\n"
        "def test_flaps():\n"
        "    n = int(MARKER.read_text()) if MARKER.exists() else 0\n"
        "    MARKER.write_text(str(n + 1))\n"
        "    assert n >= 1\n",
        encoding="utf-8",
    )
    junit = tmp_path / "junit.xml"
    completed = subprocess.run(
        [
            sys.executable, "-m", "pytest", str(spec),
            "--reruns", "2", "-q", "-p", "no:cacheprovider",
            f"--junitxml={junit}",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout
    assert "rerun" in completed.stdout
    root = ET.parse(junit).getroot()
    cases = list(root.iter("testcase"))
    assert len(cases) == 1
    assert cases[0].find("rerun") is None
    # And so, read alone, the XML claims a clean pass.
    verdicts = dict(_execution_verdicts(junit, ("test_flap.py",), 0, "run-1", ()))
    assert next(iter(verdicts.values())).status is VerdictStatus.PASS


def test_a_test_that_only_passes_after_a_rerun_is_recorded_flaky(tmp_path: Path) -> None:
    """End-to-end: a real flapping test must land as FLAKY, never PASS.

    Driven through an actual pytest run rather than fabricated XML, because the
    previous version of this test asserted on a <rerun> element that pytest never
    emits — it validated the assumption instead of the runner.
    """
    marker = tmp_path / "attempts"
    spec = tmp_path / "test_flap.py"
    spec.write_text(
        "import pathlib\n"
        f"MARKER = pathlib.Path({str(marker)!r})\n"
        "def test_flaps():\n"
        "    n = int(MARKER.read_text()) if MARKER.exists() else 0\n"
        "    MARKER.write_text(str(n + 1))\n"
        "    assert n >= 1\n",
        encoding="utf-8",
    )
    conftest = tmp_path / "conftest.py"
    conftest.write_text(
        "from qualityproof.fixtures import pytest_runtest_logreport  # noqa: F401\n",
        encoding="utf-8",
    )
    junit = tmp_path / "junit.xml"
    rerun_log = tmp_path / "reruns.jsonl"
    completed = subprocess.run(
        [
            sys.executable, "-m", "pytest", str(spec),
            "--reruns", "2", "-q", "-p", "no:cacheprovider",
            f"--junitxml={junit}",
        ],
        cwd=tmp_path,
        env={**os.environ, "QUALITYPROOF_RERUN_LOG": str(rerun_log)},
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout
    assert rerun_log.is_file(), "the fixtures hook must record the retry"

    verdicts = dict(
        _execution_verdicts(junit, ("test_flap.py",), 0, "run-1", (), rerun_log=rerun_log)
    )

    verdict = next(iter(verdicts.values()))
    assert verdict.status is VerdictStatus.FLAKY
    assert "rerun" in verdict.rationale


def test_a_failure_and_a_pass_for_one_test_is_flaky_not_passing(tmp_path: Path) -> None:
    junit = _junit(
        tmp_path,
        '<testcase classname="t" name="cart"><failure message="boom"/></testcase>'
        '<testcase classname="t" name="cart"/>',
    )

    verdicts = dict(_execution_verdicts(junit, ("t.py",), 1, "run-1", ()))

    assert verdicts["t::cart"].status is VerdictStatus.FLAKY


def test_consistent_outcomes_keep_their_plain_verdicts(tmp_path: Path) -> None:
    junit = _junit(
        tmp_path,
        '<testcase classname="t" name="ok"/>'
        '<testcase classname="t" name="bad"><error message="boom"/></testcase>'
        '<testcase classname="t" name="skipped_case"><skipped/></testcase>',
    )

    verdicts = dict(_execution_verdicts(junit, ("t.py",), 1, "run-1", ()))

    assert verdicts["t::ok"].status is VerdictStatus.PASS
    assert verdicts["t::bad"].status is VerdictStatus.FAIL
    assert verdicts["t::skipped_case"].status is VerdictStatus.INCONCLUSIVE


def test_unsafe_pytest_arguments_are_refused() -> None:
    """Arguments that could re-enable capture or redirect output are rejected."""
    validate_extra_arguments(("-k", "checkout", "--maxfail=1"))

    for hostile in ("--tracing=on", "--output=/tmp/elsewhere", "-p", "--co"):
        with pytest.raises(ValueError, match="not permitted"):
            validate_extra_arguments((hostile,))


def test_worker_argument_disables_parallelism_only_when_asked() -> None:
    assert _worker_argument("1") == ()
    assert _worker_argument("auto") == ("-n", "auto", "--dist", "loadscope")


def test_artifact_policy_defaults_closed_for_authenticated_runs() -> None:
    """An authenticated run must not capture unredactable artifacts by accident."""
    authenticated = ArtifactPolicy.from_environment({"QUALITYPROOF_STORAGE_STATE": "auth.json"})
    assert authenticated.mode is ArtifactMode.OFF
    assert authenticated.authenticated is True
    assert authenticated.traces_enabled is False

    acknowledged = ArtifactPolicy.from_environment(
        {
            "QUALITYPROOF_STORAGE_STATE": "auth.json",
            "QUALITYPROOF_ALLOW_UNREDACTABLE_ARTIFACTS": "1",
            "QUALITYPROOF_ARTIFACTS": "on_failure",
        }
    )
    assert acknowledged.traces_enabled is True
    assert acknowledged.quarantined is True

    anonymous = ArtifactPolicy.from_environment({})
    assert anonymous.mode is ArtifactMode.ON_FAILURE
    assert anonymous.quarantined is False
    assert "retain-on-failure" in " ".join(anonymous.pytest_arguments())


def test_rerun_sidecar_tolerates_partial_and_corrupt_lines(tmp_path: Path) -> None:
    """Parallel workers append concurrently; a torn line must not lose the rest."""
    log = tmp_path / "reruns.jsonl"
    log.write_text(
        '{"nodeid": "tests/test_a.py::test_one", "when": "call"}\n'
        "{not valid json\n"
        "\n"
        '{"nodeid": "tests/test_b.py::test_two", "when": "call"}\n',
        encoding="utf-8",
    )

    assert _rerun_nodeids(log) == frozenset(
        {"tests/test_a.py::test_one", "tests/test_b.py::test_two"}
    )
    assert _rerun_nodeids(tmp_path / "absent.jsonl") == frozenset()


def test_nodeid_matching_bridges_junit_and_pytest_spellings() -> None:
    """JUnit writes dotted module paths; pytest writes filesystem paths."""
    assert _matches_nodeid("tests.test_a::test_one", "tests/test_a.py::test_one")
    assert _matches_nodeid("t::test_x[chromium]", "tests/t.py::test_x[chromium]")
    assert not _matches_nodeid("t::test_x", "tests/t.py::test_y")
    # Parametrisation must not be conflated across cases.
    assert not _matches_nodeid("t::test_x[firefox]", "tests/t.py::test_x[chromium]")
