#!/usr/bin/env python3
"""claude-sessions: Manage multiple Claude Code sessions on the same codebase using git worktrees."""

import argparse
import json
import os
import subprocess
import sys
import textwrap
from datetime import datetime, timezone
from pathlib import Path

VERSION = "0.1.0"


# ── Git helpers ──────────────────────────────────────────────────────────────

def git(*args, cwd=None, check=True, capture=True):
    """Run a git command and return stdout."""
    cmd = ["git"] + list(args)
    result = subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=capture,
        text=True,
        check=False,
    )
    if check and result.returncode != 0:
        stderr = result.stderr.strip() if result.stderr else ""
        raise RuntimeError(f"git {' '.join(args)} failed: {stderr}")
    return result.stdout.strip() if result.stdout else ""


def git_common_dir(cwd=None):
    """Get the shared git directory (works from any worktree)."""
    return Path(git("rev-parse", "--git-common-dir", cwd=cwd)).resolve()


def git_toplevel(cwd=None):
    """Get the repo root of the current worktree."""
    return Path(git("rev-parse", "--show-toplevel", cwd=cwd))


def git_current_branch(cwd=None):
    """Get the current branch name."""
    return git("rev-parse", "--abbrev-ref", "HEAD", cwd=cwd)


def git_default_branch(cwd=None):
    """Detect the default branch (main/master)."""
    for candidate in ("main", "master"):
        ret = subprocess.run(
            ["git", "rev-parse", "--verify", f"refs/heads/{candidate}"],
            cwd=cwd, capture_output=True, text=True, check=False,
        )
        if ret.returncode == 0:
            return candidate
    return "main"


def require_git_repo():
    """Ensure we're inside a git repo. Returns (toplevel, common_dir)."""
    try:
        toplevel = git_toplevel()
        common = git_common_dir()
        return toplevel, common
    except RuntimeError:
        print("Error: not inside a git repository.", file=sys.stderr)
        sys.exit(1)


# ── Registry ─────────────────────────────────────────────────────────────────

def registry_path(common_dir):
    """Path to the session registry file."""
    return common_dir / "claude-sessions.json"


def load_registry(common_dir):
    """Load the session registry, creating it if missing."""
    path = registry_path(common_dir)
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return {"sessions": {}, "next_id": 1}


def save_registry(common_dir, registry):
    """Persist the session registry."""
    path = registry_path(common_dir)
    with open(path, "w") as f:
        json.dump(registry, f, indent=2)
        f.write("\n")


# ── Worktree path logic ─────────────────────────────────────────────────────

def sessions_dir(toplevel):
    """Directory that holds all worktrees for this repo."""
    parent = toplevel.parent
    repo_name = toplevel.name
    return parent / f"{repo_name}-sessions"


def worktree_path(toplevel, session_id):
    """Path for a specific session's worktree."""
    return sessions_dir(toplevel) / str(session_id)


# ── Conflict detection ───────────────────────────────────────────────────────

def get_modified_files(wt_path, base_branch):
    """Get files modified in a worktree relative to the base branch."""
    try:
        output = git("diff", "--name-only", f"{base_branch}...HEAD", cwd=wt_path)
        return set(output.splitlines()) if output else set()
    except RuntimeError:
        return set()


def check_scope_overlap(scope_a, scope_b):
    """Check if two scope lists have any overlapping paths."""
    overlaps = []
    for a in scope_a:
        for b in scope_b:
            a_norm = a.rstrip("/") + "/"
            b_norm = b.rstrip("/") + "/"
            if a_norm.startswith(b_norm) or b_norm.startswith(a_norm) or a_norm == b_norm:
                overlaps.append((a, b))
    return overlaps


def detect_conflicts(registry, toplevel, exclude_id=None):
    """Detect file-level conflicts between active sessions."""
    conflicts = []
    active = {
        sid: s for sid, s in registry["sessions"].items()
        if s["status"] == "active" and sid != exclude_id
    }

    file_owners = {}
    for sid, session in active.items():
        wt = Path(session["worktree"])
        if not wt.exists():
            continue
        modified = get_modified_files(wt, session["base_branch"])
        for f in modified:
            if f in file_owners:
                conflicts.append((f, file_owners[f], sid))
            else:
                file_owners[f] = sid

    return conflicts, file_owners


# ── Commands ─────────────────────────────────────────────────────────────────

def cmd_new(args):
    """Create a new session with a git worktree."""
    toplevel, common = require_git_repo()
    registry = load_registry(common)

    session_id = str(registry["next_id"])
    base_branch = args.base or git_default_branch(str(toplevel))
    branch = args.branch or f"session/{session_id}"
    scope = args.scope or []
    task = args.task

    # Check scope overlap with existing sessions
    if scope:
        for sid, s in registry["sessions"].items():
            if s["status"] != "active":
                continue
            overlaps = check_scope_overlap(scope, s.get("scope", []))
            if overlaps:
                print(f"Warning: scope overlap with session {sid} ({s['task']}):")
                for a, b in overlaps:
                    print(f"  {a} <-> {b}")
                resp = input("Continue anyway? [y/N] ").strip().lower()
                if resp != "y":
                    print("Aborted.")
                    return

    # Create worktree
    wt = worktree_path(toplevel, session_id)
    wt.parent.mkdir(parents=True, exist_ok=True)

    try:
        git("worktree", "add", "-b", branch, str(wt), base_branch, cwd=str(toplevel))
    except RuntimeError as e:
        print(f"Error creating worktree: {e}", file=sys.stderr)
        sys.exit(1)

    # Register session
    registry["sessions"][session_id] = {
        "task": task,
        "branch": branch,
        "worktree": str(wt),
        "scope": scope,
        "status": "active",
        "created": datetime.now(timezone.utc).isoformat(),
        "base_branch": base_branch,
    }
    registry["next_id"] = int(session_id) + 1
    save_registry(common, registry)

    print(f"Session {session_id} created:")
    print(f"  Task:     {task}")
    print(f"  Branch:   {branch}")
    print(f"  Worktree: {wt}")
    if scope:
        print(f"  Scope:    {', '.join(scope)}")
    print()
    print(f"To start working:")
    print(f"  cd {wt}")
    print(f"  claude")


def cmd_list(args):
    """List all sessions for the current repo."""
    _, common = require_git_repo()
    registry = load_registry(common)

    sessions = registry.get("sessions", {})
    if not sessions:
        print("No sessions.")
        return

    # Table header
    fmt = "{:<4} {:<10} {:<30} {:<40} {:<8}"
    print(fmt.format("ID", "Status", "Branch", "Task", "Scope"))
    print("-" * 96)

    for sid, s in sorted(sessions.items(), key=lambda x: int(x[0])):
        scope_str = ", ".join(s.get("scope", [])) or "-"
        task_str = s["task"][:38] + ".." if len(s["task"]) > 40 else s["task"]
        print(fmt.format(sid, s["status"], s["branch"][:28], task_str, scope_str))


def cmd_status(args):
    """Show detailed status of a session (or all sessions)."""
    toplevel, common = require_git_repo()
    registry = load_registry(common)

    session_id = args.id
    if session_id and session_id not in registry["sessions"]:
        print(f"Error: session {session_id} not found.", file=sys.stderr)
        sys.exit(1)

    targets = {session_id: registry["sessions"][session_id]} if session_id else {
        sid: s for sid, s in registry["sessions"].items() if s["status"] == "active"
    }

    for sid, session in sorted(targets.items(), key=lambda x: int(x[0])):
        print(f"Session {sid}: {session['task']}")
        print(f"  Branch:     {session['branch']}")
        print(f"  Worktree:   {session['worktree']}")
        print(f"  Base:       {session['base_branch']}")
        print(f"  Status:     {session['status']}")
        print(f"  Created:    {session['created']}")

        scope = session.get("scope", [])
        if scope:
            print(f"  Scope:      {', '.join(scope)}")

        wt = Path(session["worktree"])
        if wt.exists() and session["status"] == "active":
            # Modified files
            modified = get_modified_files(wt, session["base_branch"])
            if modified:
                print(f"  Modified files ({len(modified)}):")
                for f in sorted(modified):
                    print(f"    {f}")
            else:
                print("  Modified files: none")

            # Recent commits
            try:
                log = git("log", "--oneline", "-5", f"{session['base_branch']}..HEAD", cwd=str(wt))
                if log:
                    print("  Recent commits:")
                    for line in log.splitlines():
                        print(f"    {line}")
            except RuntimeError:
                pass
        elif not wt.exists():
            print("  Worktree: MISSING")

        print()

    # Conflict detection
    conflicts, _ = detect_conflicts(registry, toplevel)
    if conflicts:
        print("File conflicts detected:")
        for filepath, sid_a, sid_b in conflicts:
            print(f"  {filepath} — session {sid_a} vs session {sid_b}")
    else:
        print("No file conflicts detected.")


def cmd_merge(args):
    """Merge a session back into its base branch."""
    toplevel, common = require_git_repo()
    registry = load_registry(common)

    session_id = args.id
    if session_id not in registry["sessions"]:
        print(f"Error: session {session_id} not found.", file=sys.stderr)
        sys.exit(1)

    session = registry["sessions"][session_id]
    if session["status"] != "active":
        print(f"Error: session {session_id} is not active (status: {session['status']}).", file=sys.stderr)
        sys.exit(1)

    strategy = args.strategy or "auto-rebase"
    branch = session["branch"]
    base = session["base_branch"]
    wt = Path(session["worktree"])

    if strategy == "auto-rebase":
        print(f"Auto-rebasing session {session_id} ({branch}) onto {base}...")

        # Fetch latest
        git("fetch", "origin", base, cwd=str(toplevel))

        # Rebase the session branch onto the latest base
        try:
            git("rebase", f"origin/{base}", cwd=str(wt))
        except RuntimeError as e:
            print(f"Rebase failed: {e}", file=sys.stderr)
            print("Resolve conflicts in the worktree, then run merge again.", file=sys.stderr)
            print(f"  cd {wt}")
            sys.exit(1)

        # Switch to base branch in main worktree and merge
        git("checkout", base, cwd=str(toplevel))
        git("merge", "--ff-only", branch, cwd=str(toplevel))

        print(f"Merged {branch} into {base} (fast-forward).")

        # Cleanup
        _cleanup_session(toplevel, common, registry, session_id)
        print(f"Session {session_id} cleaned up.")

    elif strategy == "pr":
        print(f"Creating PR for session {session_id} ({branch})...")

        # Push the branch
        git("push", "-u", "origin", branch, cwd=str(wt))

        # Create PR via gh
        try:
            pr_url = subprocess.run(
                ["gh", "pr", "create",
                 "--title", f"{session['task']}",
                 "--body", f"Session {session_id}: {session['task']}",
                 "--base", base,
                 "--head", branch],
                cwd=str(wt),
                capture_output=True, text=True, check=True,
            ).stdout.strip()
            print(f"PR created: {pr_url}")
        except FileNotFoundError:
            print("Error: 'gh' CLI not found. Install it: https://cli.github.com/", file=sys.stderr)
            sys.exit(1)
        except subprocess.CalledProcessError as e:
            print(f"Error creating PR: {e.stderr}", file=sys.stderr)
            sys.exit(1)

        # Mark completed but don't cleanup worktree (PR still open)
        session["status"] = "pr-created"
        save_registry(common, registry)

    elif strategy == "manual":
        print(f"Session {session_id} ({branch}) ready for manual merge.")
        print(f"  Worktree: {wt}")
        print(f"  Branch:   {branch}")
        print(f"  Base:     {base}")
        print()
        print("To merge manually:")
        print(f"  cd {toplevel}")
        print(f"  git checkout {base}")
        print(f"  git merge {branch}")
        print()
        print(f"When done, run: claude-sessions remove {session_id}")

    else:
        print(f"Error: unknown strategy '{strategy}'.", file=sys.stderr)
        sys.exit(1)


def cmd_remove(args):
    """Remove a session (cleanup worktree, branch, registry)."""
    toplevel, common = require_git_repo()
    registry = load_registry(common)

    session_id = args.id
    if session_id not in registry["sessions"]:
        print(f"Error: session {session_id} not found.", file=sys.stderr)
        sys.exit(1)

    force = args.force
    session = registry["sessions"][session_id]

    if session["status"] == "active" and not force:
        wt = Path(session["worktree"])
        if wt.exists():
            modified = get_modified_files(wt, session["base_branch"])
            if modified:
                print(f"Warning: session {session_id} has {len(modified)} modified files:")
                for f in sorted(modified)[:10]:
                    print(f"  {f}")
                if len(modified) > 10:
                    print(f"  ... and {len(modified) - 10} more")
                resp = input("Remove anyway? [y/N] ").strip().lower()
                if resp != "y":
                    print("Aborted.")
                    return

    _cleanup_session(toplevel, common, registry, session_id)
    print(f"Session {session_id} removed.")


def cmd_check(args):
    """Check if a file is claimed or modified by any session."""
    toplevel, common = require_git_repo()
    registry = load_registry(common)

    filepath = args.path
    found = False

    for sid, session in registry["sessions"].items():
        if session["status"] != "active":
            continue

        # Check scope
        for scope in session.get("scope", []):
            scope_norm = scope.rstrip("/") + "/"
            if filepath.startswith(scope_norm) or filepath == scope.rstrip("/"):
                print(f"Session {sid} ({session['task']}): path in scope [{scope}]")
                found = True

        # Check modified files
        wt = Path(session["worktree"])
        if wt.exists():
            modified = get_modified_files(wt, session["base_branch"])
            if filepath in modified:
                print(f"Session {sid} ({session['task']}): file modified in worktree")
                found = True

    if not found:
        print(f"No sessions claim or modify: {filepath}")


# ── Cleanup helper ───────────────────────────────────────────────────────────

def _cleanup_session(toplevel, common, registry, session_id):
    """Remove worktree, delete branch, update registry."""
    session = registry["sessions"][session_id]
    wt = Path(session["worktree"])
    branch = session["branch"]

    # Remove worktree
    if wt.exists():
        git("worktree", "remove", str(wt), "--force", cwd=str(toplevel))

    # Delete branch (local only)
    try:
        git("branch", "-D", branch, cwd=str(toplevel))
    except RuntimeError:
        pass  # Branch may already be gone

    # Remove from registry
    del registry["sessions"][session_id]
    save_registry(common, registry)


# ── CLI setup ────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        prog="claude-sessions",
        description="Manage multiple Claude Code sessions using git worktrees.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")

    subparsers = parser.add_subparsers(dest="command", required=True)

    # new
    p_new = subparsers.add_parser("new", help="Create a new session")
    p_new.add_argument("task", help="Description of what this session will work on")
    p_new.add_argument("--scope", nargs="+", metavar="PATH", help="File/directory scope (for conflict detection)")
    p_new.add_argument("--branch", help="Branch name (default: session/<id>)")
    p_new.add_argument("--base", help="Base branch (default: auto-detect main/master)")
    p_new.set_defaults(func=cmd_new)

    # list
    p_list = subparsers.add_parser("list", help="List all sessions")
    p_list.set_defaults(func=cmd_list)

    # status
    p_status = subparsers.add_parser("status", help="Show session status")
    p_status.add_argument("id", nargs="?", help="Session ID (default: all active)")
    p_status.set_defaults(func=cmd_status)

    # merge
    p_merge = subparsers.add_parser("merge", help="Merge a session back")
    p_merge.add_argument("id", help="Session ID")
    p_merge.add_argument(
        "--strategy",
        choices=["auto-rebase", "pr", "manual"],
        default="auto-rebase",
        help="Merge strategy (default: auto-rebase)",
    )
    p_merge.set_defaults(func=cmd_merge)

    # remove
    p_remove = subparsers.add_parser("remove", help="Remove a session")
    p_remove.add_argument("id", help="Session ID")
    p_remove.add_argument("--force", action="store_true", help="Skip safety checks")
    p_remove.set_defaults(func=cmd_remove)

    # check
    p_check = subparsers.add_parser("check", help="Check file ownership")
    p_check.add_argument("path", help="File path to check")
    p_check.set_defaults(func=cmd_check)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
