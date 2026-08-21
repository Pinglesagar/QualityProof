"""What the engine says it ran must be what it ran.

Three defects lived here at once, none of them with any test. They were found by
executing the documented capability rather than by reading its tests, and each one
made the tool's headline claim -- that a requirement is demonstrated by evidence --
report something the run itself contradicted.

These tests use real pytest subprocesses. Mocking ``subprocess.run`` is what let
the first defect survive: the bug was in what pytest does with the argument it is
handed, so a test that never hands pytest an argument cannot see it.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from qualityproof.config import write_default_config
from qualityproof.execution import (
    _collection_targets,
    _verdict_namespace,
    execute_tests,
)
from qualityproof.repository import SQLiteRepository


def _project(tmp_path: Path, files: dict[str, str]) -> Path:
    write_default_config(tmp_path)
    for relative, body in files.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    return tmp_path


def _passing(name: str) -> str:
    return f"def {name}() -> None:\n    assert True\n"


def _verdict_ids(project: Path) -> list[str]:
    database = project / ".qualityproof" / "qualityproof.db"
    connection = sqlite3.connect(database)
    try:
        return sorted(
            str(row[0])
            for row in connection.execute(
                "select record_id from records where kind='verdict'"
            )
        )
    finally:
        connection.close()


def test_a_directory_is_only_collapsed_when_it_holds_nothing_extra(
    tmp_path: Path,
) -> None:
    """The collapse membership test must match how pytest collects a directory.

    It globbed one level while pytest collects recursively, so handing over the
    directory as one argument selected strictly more than the engine had chosen.
    """
    assert _collection_targets(()) == ()
    top = tmp_path / "test_a.py"
    top.write_text(_passing("test_a"), encoding="utf-8")
    nested = tmp_path / "sub" / "test_b.py"
    nested.parent.mkdir()
    nested.write_text(_passing("test_b"), encoding="utf-8")

    # Only the top-level file selected: collapsing would smuggle in the nested one.
    assert _collection_targets((top,)) == (str(top.resolve()),)
    # Everything selected: collapsing is safe and is the fast path worth keeping.
    # One target, not the parent plus the subdirectory, so pytest is never handed
    # the same file twice.
    assert _collection_targets((top, nested)) == (str(tmp_path.resolve()),)


def test_a_subdirectory_test_is_selected_rather_than_riding_along(
    tmp_path: Path,
) -> None:
    """Every test pytest runs must appear in the run record.

    Before the fix the run record listed one file, pytest reported two tests, and
    a verdict was persisted for a test the same run said it had not run. The
    engine's own static side already read these trees recursively -- the custom
    tree digest and ``audit`` both use rglob -- so a test in a subdirectory was
    audited into the ledger and then never executed.
    """
    project = _project(
        tmp_path,
        {
            "scenarios/custom/test_owned.py": _passing("test_owned"),
            "scenarios/custom/regression/test_nested.py": _passing("test_nested"),
        },
    )
    repository = SQLiteRepository(project / ".qualityproof" / "qualityproof.db")
    repository.initialize()

    result = execute_tests(project, repository=repository)

    assert set(result.test_paths) == {
        "scenarios/custom/test_owned.py",
        "scenarios/custom/regression/test_nested.py",
    }
    payload = json.loads((project / result.result_path).read_text(encoding="utf-8"))
    assert "2 passed" in payload["stdout"], payload["stdout"]
    assert len(result.test_paths) == 2
    assert _verdict_ids(project) == [
        "scenarios.custom.regression.test_nested::test_nested",
        "scenarios.custom.test_owned::test_owned",
    ]


def test_the_union_of_every_shard_is_the_whole_suite(tmp_path: Path) -> None:
    """Sharding must be exactly-once: no test lost, none run twice.

    The collapse broke this in both directions. A subdirectory test rode along in
    whichever shard happened to select some directory in full, so it could run in
    one shard, in none, or in all -- and the union of all shards was not the suite.
    """
    files = {
        f"scenarios/custom/test_f{index}.py": _passing(f"test_t{index}")
        for index in range(1, 5)
    }
    files["scenarios/custom/regression/test_nested.py"] = _passing("test_nested")
    project = _project(tmp_path, files)
    repository = SQLiteRepository(project / ".qualityproof" / "qualityproof.db")
    repository.initialize()

    unsharded = execute_tests(project, repository=repository)
    everything = set(unsharded.test_paths)
    assert len(everything) == 5

    seen: list[str] = []
    for index in (1, 2, 3):
        result = execute_tests(project, repository=repository, shard=(index, 3))
        seen.extend(result.test_paths)

    assert sorted(seen) == sorted(everything), "shards must partition the suite"
    assert len(seen) == len(set(seen)), "no test may run in more than one shard"


def test_one_shard_does_not_erase_another_shards_verdicts(tmp_path: Path) -> None:
    """Each shard owns a slice, so it may only replace its own records.

    Persistence replaced the entire verdict kind, so running shard 1 then shard 2
    left only shard 2's verdicts. Half the suite then read as never executed, and
    `coverage --require-demonstrated` reported requirements as NOT_RUN whose tests
    had in fact just passed. A correct fan-out produced a false coverage result.
    """
    project = _project(
        tmp_path,
        {
            f"scenarios/custom/test_f{index}.py": _passing(f"test_t{index}")
            for index in range(1, 5)
        },
    )
    repository = SQLiteRepository(project / ".qualityproof" / "qualityproof.db")
    repository.initialize()

    execute_tests(project, repository=repository, shard=(1, 2))
    execute_tests(project, repository=repository, shard=(2, 2))

    assert _verdict_ids(project) == [
        "scenarios.custom.test_f1::test_t1",
        "scenarios.custom.test_f2::test_t2",
        "scenarios.custom.test_f3::test_t3",
        "scenarios.custom.test_f4::test_t4",
    ]


def test_re_running_one_file_replaces_only_that_files_verdicts(tmp_path: Path) -> None:
    """Scoped replacement must still be a replacement, not an append.

    A stale verdict surviving a re-run would be worse than the bug it fixes: the
    report would credit a result the current source no longer produces.
    """
    project = _project(
        tmp_path,
        {
            "scenarios/custom/test_a.py": _passing("test_first"),
            "scenarios/custom/test_b.py": _passing("test_other"),
        },
    )
    repository = SQLiteRepository(project / ".qualityproof" / "qualityproof.db")
    repository.initialize()
    execute_tests(project, repository=repository)
    assert _verdict_ids(project) == [
        "scenarios.custom.test_a::test_first",
        "scenarios.custom.test_b::test_other",
    ]

    # Rename the test inside one file. Its old verdict must not survive.
    (project / "scenarios/custom/test_a.py").write_text(
        _passing("test_renamed"), encoding="utf-8"
    )
    execute_tests(project, repository=repository)

    assert _verdict_ids(project) == [
        "scenarios.custom.test_a::test_renamed",
        "scenarios.custom.test_b::test_other",
    ]


def test_a_namespace_is_not_a_prefix_of_a_longer_sibling_name(tmp_path: Path) -> None:
    """``test_a`` must not own ``test_abc``'s verdicts.

    Scoped deletion by plain string prefix would silently delete a sibling's
    records, which is how a fix for one erasure bug becomes another.
    """
    project = _project(
        tmp_path,
        {
            "scenarios/custom/test_a.py": _passing("test_short"),
            "scenarios/custom/test_abc.py": _passing("test_long"),
        },
    )
    repository = SQLiteRepository(project / ".qualityproof" / "qualityproof.db")
    repository.initialize()
    execute_tests(project, repository=repository)
    before = _verdict_ids(project)
    assert before == [
        "scenarios.custom.test_a::test_short",
        "scenarios.custom.test_abc::test_long",
    ]

    execute_tests(project, repository=repository, shard=(1, 2))
    execute_tests(project, repository=repository, shard=(2, 2))
    assert _verdict_ids(project) == before


def test_an_empty_shard_is_recorded_rather_than_failing_the_job(tmp_path: Path) -> None:
    """A CI matrix may over-provision, and a shrinking suite is not a failure.

    This raised, so a fan-out pinned at a fixed TOTAL red-failed the moment the
    suite held fewer files than shards.
    """
    project = _project(tmp_path, {"scenarios/custom/test_only.py": _passing("test_only")})
    repository = SQLiteRepository(project / ".qualityproof" / "qualityproof.db")
    repository.initialize()

    result = execute_tests(project, repository=repository, shard=(3, 3))

    assert result.exit_code == 0
    assert result.test_paths == ()
    payload = json.loads((project / result.result_path).read_text(encoding="utf-8"))
    assert payload["tests"] == []
    assert payload["shard"] == [3, 3]
    assert "selected no tests" in payload["stdout"]
    # An empty shard must not be mistaken for evidence, so it persists no verdict.
    assert _verdict_ids(project) == []
    assert repository.get("test_run", result.run_id, type(result)) is not None


def test_an_empty_project_still_fails_loudly(tmp_path: Path) -> None:
    """Tolerating an empty shard must not tolerate an empty project."""
    project = _project(tmp_path, {})
    with pytest.raises(ValueError, match="no generated or custom tests found"):
        execute_tests(project)


def test_verdict_namespace_mirrors_the_junit_classname(tmp_path: Path) -> None:
    assert (
        _verdict_namespace(tmp_path, tmp_path / "scenarios" / "custom" / "test_a.py")
        == "scenarios.custom.test_a"
    )
    assert (
        _verdict_namespace(tmp_path, tmp_path / "scenarios" / "custom" / "sub" / "test_b.py")
        == "scenarios.custom.sub.test_b"
    )
