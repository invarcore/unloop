# Copyright 2026 Invarcore Organization
# SPDX-License-Identifier: MIT

"""Unit and concurrency tests for VCSJailCoordinator and `unloop jail` CLI subcommand."""

import os
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from click.testing import CliRunner

from unloop.cli.main import cli
from unloop.compaction_slots import _normalize_arcbench_path, _normalize_jcs
from unloop.vcs_jail import FileLock, JailMetadata, VCSJailCoordinator


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "test_repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Agent Tester"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.email", "agent@invarcore.com"], cwd=str(repo), check=True)
    (repo / "base.py").write_text("def base_fn():\n    return 'base'\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(repo), check=True)
    subprocess.run(["git", "commit", "-m", "Initial base commit"], cwd=str(repo), check=True, capture_output=True)
    return repo


def test_vcs_jail_lifecycle_and_patch_export(git_repo: Path) -> None:
    coord = VCSJailCoordinator(repo_dir=git_repo)
    agent_id = "agent-alpha"

    jail_path = coord.create_jail(agent_id)
    assert jail_path.exists()
    assert (jail_path / "base.py").exists()

    meta = JailMetadata(
        agent_id=agent_id,
        jail_path=jail_path,
        base_ref="HEAD",
        created_at=time.time(),
    )
    assert meta.agent_id == agent_id

    # Attempting duplicate jail creation raises FileExistsError
    with pytest.raises(FileExistsError, match="already exists"):
        coord.create_jail(agent_id)

    # Modify base.py and create new file inside jail
    (jail_path / "base.py").write_text("def base_fn():\n    return 'mutated_by_alpha'\n", encoding="utf-8")
    (jail_path / "untracked.py").write_text("# Untracked addition\n", encoding="utf-8")

    patch, digest = coord.export_patch_and_digest(agent_id)
    assert "mutated_by_alpha" in patch
    assert "untracked.py" in patch
    assert len(digest) == 64

    # Run command inside jail with custom env
    res = coord.run_in_jail(
        agent_id,
        ["python", "-c", "import os, base; print(base.base_fn(), os.environ.get('TEST_VAR'))"],
        env={"TEST_VAR": "custom_val"},
    )
    assert res.returncode == 0
    assert "mutated_by_alpha custom_val" in res.stdout

    # Destroy jail
    assert coord.destroy_jail(agent_id) is True
    assert not jail_path.exists()
    assert coord.destroy_jail(agent_id) is False


def test_vcs_jail_parallel_concurrency_no_lock_collision(git_repo: Path) -> None:
    coord = VCSJailCoordinator(repo_dir=git_repo)
    agent_ids = [f"worker-{i}" for i in range(4)]

    def _worker_task(aid: str) -> tuple[str, str]:
        path = coord.create_jail(aid)
        target = path / "base.py"
        target.write_text(f"def base_fn():\n    return '{aid}'\n", encoding="utf-8")
        _patch, digest = coord.export_patch_and_digest(aid)
        return aid, digest

    # Concurrently spin up 4 agent worktree jails
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(_worker_task, agent_ids))

    assert len(results) == 4
    digests = {r[1] for r in results}
    # Each agent produced a distinct patch digest because worker IDs differed
    assert len(digests) == 4

    # Clean up all concurrently created jails
    cleaned = coord.cleanup_all()
    assert cleaned == 4


def test_vcs_jail_context_manager(git_repo: Path) -> None:
    coord = VCSJailCoordinator(repo_dir=git_repo)
    agent_id = "agent-ephemeral"

    with coord.jail_session(agent_id) as jpath:
        assert jpath.exists()
        (jpath / "base.py").write_text("ephemeral", encoding="utf-8")

    assert not (coord.jails_root / agent_id).exists()


def test_vcs_jail_error_cases(git_repo: Path, tmp_path: Path) -> None:
    coord = VCSJailCoordinator(repo_dir=git_repo)

    with pytest.raises(FileNotFoundError, match="does not exist"):
        coord.run_in_jail("non-existent-agent", ["ls"])

    with pytest.raises(FileNotFoundError, match="not found"):
        coord.export_patch_and_digest("non-existent-agent")

    # Invalid base_ref fails worktree creation
    with pytest.raises(RuntimeError, match="Failed to create git worktree"):
        coord.create_jail("agent-bad-ref", base_ref="invalid-branch-xyz")

    # Empty jails root cleanup returns 0
    empty_coord = VCSJailCoordinator(repo_dir=git_repo, jails_root=tmp_path / "empty_jails")
    assert empty_coord.cleanup_all() == 0


def test_vcs_jail_file_lock_timeout_and_stale_recovery(tmp_path: Path) -> None:
    lock_dir = tmp_path / "test.lock"

    # Direct lock acquisition
    with FileLock(lock_dir, timeout=2.0) as fl:
        assert fl.lock_dir.exists()

    assert not lock_dir.exists()

    # Timeout when lock is held
    lock_dir.mkdir()
    try:
        with pytest.raises(TimeoutError, match="Could not acquire VCS jail lock"):
            with FileLock(lock_dir, timeout=0.1, retry_delay=0.02):
                pass
    finally:
        lock_dir.rmdir()

    # Stale lock recovery (> 60s mtime)
    lock_dir.mkdir()
    stale_time = time.time() - 70.0
    os.utime(lock_dir, (stale_time, stale_time))

    with FileLock(lock_dir, timeout=1.0) as fl2:
        assert fl2.lock_dir.exists()

    assert not lock_dir.exists()


def test_unloop_jail_cli_subcommand(git_repo: Path) -> None:
    runner = CliRunner()

    # Happy path execution with stdout, stderr, and patch digest
    result = runner.invoke(
        cli,
        [
            "jail",
            "--agent-id",
            "cli-agent-1",
            "--repo",
            str(git_repo),
            "--",
            "python",
            "-c",
            "import sys, pathlib; sys.stdout.write('hello stdout\\n'); sys.stderr.write('hello stderr\\n'); pathlib.Path('base.py').write_text('cli_mod\\n')",
        ],
    )
    assert result.exit_code == 0
    assert "VCS Jail created for agent 'cli-agent-1'" in result.output
    assert "hello stdout" in result.output
    assert "hello stderr" in result.output
    assert "Captured Unified Diff Digest" in result.output
    assert "VCS Jail for 'cli-agent-1' destroyed." in result.output

    # Non-zero exit code propagation
    result_err = runner.invoke(
        cli,
        [
            "jail",
            "--agent-id",
            "cli-agent-fail",
            "--repo",
            str(git_repo),
            "--",
            "python",
            "-c",
            "import sys; sys.exit(7)",
        ],
    )
    assert result_err.exit_code == 7

    # Keep flag leaves jail intact
    result_keep = runner.invoke(
        cli,
        [
            "jail",
            "--agent-id",
            "cli-agent-keep",
            "--repo",
            str(git_repo),
            "--keep",
        ],
    )
    assert result_keep.exit_code == 0
    kept_jail = git_repo / ".unloop" / "jails" / "cli-agent-keep"
    assert kept_jail.exists()
    shutil.rmtree(kept_jail, ignore_errors=True)


def test_coverage_boosters_compaction_slots() -> None:
    # JCS float normalization coverage
    assert _normalize_jcs(0.0) == 0
    assert _normalize_jcs(5.0) == 5
    assert _normalize_jcs(3.14) == 3.14
    with pytest.raises(ValueError, match="cannot encode NaN"):
        _normalize_jcs(float("nan"))
    with pytest.raises(ValueError, match="cannot encode NaN"):
        _normalize_jcs(float("inf"))

    # ArcBench path normalization edge cases
    assert _normalize_arcbench_path("/a/b") == "/a/b"
    assert _normalize_arcbench_path("foo/..") == ""
    assert _normalize_arcbench_path("dir/") == "dir"
    assert _normalize_arcbench_path("C:") == "c:/"
