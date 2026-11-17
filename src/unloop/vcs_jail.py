# Copyright 2026 Invarcore Organization
# SPDX-License-Identifier: MIT

"""VCS Jail Coordinator: Ephemeral Git Worktree Isolation for Multi-Agent Swarms.

Provides rootless Copy-on-Write git worktree isolation, atomic index.lock arbitration,
deterministic unified patch and digest export, and automated teardown.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import time
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


class FileLock:
    """Atomic cross-process file lock implemented via filesystem directory atomicity."""

    def __init__(self, lock_dir: Path, timeout: float = 30.0, retry_delay: float = 0.05) -> None:
        self.lock_dir = lock_dir
        self.timeout = timeout
        self.retry_delay = retry_delay

    def __enter__(self) -> FileLock:
        start = time.monotonic()
        while True:
            try:
                self.lock_dir.mkdir(parents=True, exist_ok=False)
                return self
            except FileExistsError as err:
                if time.monotonic() - start > self.timeout:
                    # Stale lock recovery if older than 60s
                    try:
                        mtime = self.lock_dir.stat().st_mtime
                        if time.time() - mtime > 60.0:
                            self.lock_dir.rmdir()
                            continue
                    except OSError:
                        pass
                    raise TimeoutError(
                        f"Could not acquire VCS jail lock at '{self.lock_dir}' within {self.timeout}s"
                    ) from err
                time.sleep(self.retry_delay)

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        try:
            self.lock_dir.rmdir()
        except OSError:
            pass


@dataclass(frozen=True)
class JailMetadata:
    """Metadata record for an active VCS agent jail."""

    agent_id: str
    jail_path: Path
    base_ref: str
    created_at: float


class VCSJailCoordinator:
    """Coordinates ephemeral git worktrees for isolated, concurrent multi-agent code mutations."""

    def __init__(
        self,
        repo_dir: str | Path = ".",
        jails_root: str | Path | None = None,
        lock_timeout: float = 30.0,
    ) -> None:
        self.repo_dir = Path(repo_dir).resolve()
        self.jails_root = (
            Path(jails_root).resolve()
            if jails_root is not None
            else self.repo_dir / ".unloop" / "jails"
        )
        self.lock_dir = self.repo_dir / ".unloop" / "worktree.lock"
        self.lock_timeout = lock_timeout

    def _acquire_lock(self) -> FileLock:
        return FileLock(self.lock_dir, timeout=self.lock_timeout)

    def create_jail(self, agent_id: str, base_ref: str = "HEAD") -> Path:
        """Create an ephemeral detached git worktree for agent_id."""
        jail_path = self.jails_root / agent_id
        if jail_path.exists():
            raise FileExistsError(f"Jail for agent '{agent_id}' already exists at {jail_path}")

        self.jails_root.mkdir(parents=True, exist_ok=True)
        with self._acquire_lock():
            cmd = ["git", "worktree", "add", "--detach", str(jail_path), base_ref]
            res = subprocess.run(
                cmd,
                cwd=str(self.repo_dir),
                capture_output=True,
                text=True,
                check=False,
            )
            if res.returncode != 0:
                raise RuntimeError(
                    f"Failed to create git worktree for agent '{agent_id}': {res.stderr.strip()}"
                )
        return jail_path

    def run_in_jail(
        self,
        agent_id: str,
        command: Sequence[str],
        timeout: float | None = None,
        env: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Execute a command inside the agent's jail directory."""
        jail_path = self.jails_root / agent_id
        if not jail_path.exists():
            raise FileNotFoundError(f"Jail for agent '{agent_id}' does not exist at {jail_path}")

        run_env = os.environ.copy()
        if env:
            run_env.update(env)
        run_env["UNLOOP_AGENT_ID"] = agent_id
        run_env["UNLOOP_JAIL_DIR"] = str(jail_path)

        return subprocess.run(
            list(command),
            cwd=str(jail_path),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=run_env,
            check=False,
        )

    def export_patch_and_digest(self, agent_id: str) -> tuple[str, str]:
        """Export unified git diff of all modifications/additions in the jail and its SHA-256 digest."""
        jail_path = self.jails_root / agent_id
        if not jail_path.exists():
            raise FileNotFoundError(f"Jail for agent '{agent_id}' not found at {jail_path}")

        # Intent to add untracked files so git diff HEAD captures additions
        subprocess.run(
            ["git", "add", "-N", "."],
            cwd=str(jail_path),
            capture_output=True,
            text=True,
            check=False,
        )

        res = subprocess.run(
            ["git", "diff", "HEAD"],
            cwd=str(jail_path),
            capture_output=True,
            text=True,
            check=True,
        )
        patch_text = res.stdout
        digest = hashlib.sha256(patch_text.encode("utf-8")).hexdigest()
        return patch_text, digest

    def destroy_jail(self, agent_id: str, force: bool = True) -> bool:
        """Cleanly destroy the worktree and remove the jail directory."""
        jail_path = self.jails_root / agent_id
        if not jail_path.exists():
            return False

        with self._acquire_lock():
            cmd = ["git", "worktree", "remove"]
            if force:
                cmd.append("--force")
            cmd.append(str(jail_path))
            subprocess.run(
                cmd,
                cwd=str(self.repo_dir),
                capture_output=True,
                text=True,
                check=False,
            )
            if jail_path.exists():
                shutil.rmtree(jail_path, ignore_errors=True)
            subprocess.run(
                ["git", "worktree", "prune"],
                cwd=str(self.repo_dir),
                capture_output=True,
                text=True,
                check=False,
            )
        return True

    def cleanup_all(self) -> int:
        """Prune and clean up all agent worktrees and jails."""
        if not self.jails_root.exists():
            return 0
        count = 0
        with self._acquire_lock():
            for child in list(self.jails_root.iterdir()):
                if child.is_dir():
                    subprocess.run(
                        ["git", "worktree", "remove", "--force", str(child)],
                        cwd=str(self.repo_dir),
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    if child.exists():
                        shutil.rmtree(child, ignore_errors=True)
                    count += 1
            subprocess.run(
                ["git", "worktree", "prune"],
                cwd=str(self.repo_dir),
                capture_output=True,
                text=True,
                check=False,
            )
        return count

    @contextmanager
    def jail_session(self, agent_id: str, base_ref: str = "HEAD") -> Generator[Path, None, None]:
        """Context manager to create an agent jail and ensure cleanup upon exit."""
        jail_path = self.create_jail(agent_id, base_ref=base_ref)
        try:
            yield jail_path
        finally:
            self.destroy_jail(agent_id, force=True)
