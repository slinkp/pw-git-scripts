#!/usr/bin/env python3
"""
git-iter: Like `git bisect`, but iterates linearly instead of bisecting.

This script implements a linear commit iterator according to the plan in
git-iter-plan.md and the spec in git-iter-spec.txt.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import List, Optional

INTRO_TEXT = """Like `git bisect`, but iterates linearly instead of bisecting.

This can be especially useful for `git iter run` when the commit history
is dirty (a mix of bad and good commits) and you want to find the first bad
commit; dirty history means `git bisect` may not find the earliest bad commit.

Also useful if you just want to walk through the history for whatever reason,
without having to look at revision numbers to check out.

This is simpler than `git bisect` and does not support all of the same features/options.
"""

# State paths will be under git_dir()/iter
STATE_FILENAME = "state.json"
LOCK_FILENAME = "lock"


def run_git(args: List[str], check: bool = True) -> str:
    """Run git with the provided args, return stdout.strip().

    On non-zero exit and check=True, prints git stderr and exits(1).
    """
    proc = subprocess.run(
        ["git"] + args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    if check and proc.returncode != 0:
        msg = (
            proc.stderr.strip()
            or proc.stdout.strip()
            or f"git {' '.join(args)} failed with code {proc.returncode}"
        )
        print(msg, file=sys.stderr)
        sys.exit(1)
    return proc.stdout.strip()


def git_dir() -> Path:
    """Return the repository's git dir as a Path."""
    gd = run_git(["rev-parse", "--git-dir"])
    return Path(gd)


def resolve_rev(rev: str) -> str:
    """Resolve a rev to a full SHA, exit on failure."""
    return run_git(["rev-parse", "--verify", rev])


def head_sha() -> str:
    return resolve_rev("HEAD")


def symbolic_head() -> Optional[str]:
    """Return the short symbolic head name (branch) or None if detached."""
    proc = subprocess.run(
        ["git", "symbolic-ref", "-q", "--short", "HEAD"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def is_worktree_clean() -> bool:
    """Return True if working tree has no modifications (like git bisect)."""
    # Refresh index
    subprocess.run(
        ["git", "update-index", "-q", "--refresh"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    rc1 = subprocess.run(
        ["git", "diff-files", "--quiet", "--ignore-submodules", "--"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode
    rc2 = subprocess.run(
        ["git", "diff-index", "--cached", "--quiet", "HEAD", "--"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode
    return rc1 == 0 and rc2 == 0


def short_commit(sha: str) -> str:
    return run_git(["show", "-s", "--format=%h", sha])


def commit_subject(sha: str) -> str:
    return run_git(["show", "-s", "--format=%s", sha])


def checkout_detached(sha: str) -> None:
    run_git(["checkout", "--detach", sha])


# State management


def iter_paths():
    gd = git_dir()
    iter_dir = gd / "iter"
    state_path = iter_dir / STATE_FILENAME
    lock_path = iter_dir / LOCK_FILENAME
    return iter_dir, state_path, lock_path


class IterLock:
    def __init__(self, lock_path: Path):
        self.lock_path = lock_path
        self._locked = False

    def __enter__(self):
        # ensure parent
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(str(self.lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            with os.fdopen(fd, "w") as f:
                f.write(str(os.getpid()))
            self._locked = True
        except FileExistsError:
            print(
                f"git-iter is already in use (lock found at {self.lock_path})",
                file=sys.stderr,
            )
            sys.exit(1)
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._locked:
            try:
                os.unlink(self.lock_path)
            except FileNotFoundError:
                pass


def load_state() -> Optional[dict]:
    iter_dir, state_path, lock_path = iter_paths()
    if not state_path.exists():
        return None
    try:
        with state_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
            # Loose validation and normalization
            if not isinstance(data, dict):
                return None
            # Ensure keys exist
            data.setdefault("original_ref", None)
            data.setdefault("original_head", None)
            data.setdefault("first", None)
            data.setdefault("last", None)
            data.setdefault("pathspec", [])
            data.setdefault("sequence", None)
            data.setdefault("index", -1)
            return data
    except Exception:
        return None


def atomic_write(path: Path, content: str) -> None:
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        f.write(content)
    os.replace(str(tmp), str(path))


def save_state(state: dict) -> None:
    iter_dir, state_path, lock_path = iter_paths()
    iter_dir.mkdir(parents=True, exist_ok=True)
    txt = json.dumps(state, indent=2)
    atomic_write(state_path, txt)


def clear_state() -> None:
    iter_dir, state_path, lock_path = iter_paths()
    try:
        if state_path.exists():
            state_path.unlink()
    except Exception:
        pass
    try:
        if lock_path.exists():
            lock_path.unlink()
    except Exception:
        pass
    # attempt to rmdir iter dir if empty
    try:
        iter_dir.rmdir()
    except Exception:
        pass


# Sequence construction


def build_sequence(first_sha: str, last_sha: str, pathspec: List[str]) -> List[str]:
    # verify ancestry along general ancestry (merge-base)
    proc = subprocess.run(["git", "merge-base", "--is-ancestor", first_sha, last_sha])
    if proc.returncode != 0:
        print(
            f"{first_sha[:12]} is not an ancestor of {last_sha[:12]} (first-parent path required)",
            file=sys.stderr,
        )
        sys.exit(1)
    if first_sha == last_sha:
        return [first_sha]
    cmd = [
        "rev-list",
        "--reverse",
        "--first-parent",
        "--ancestry-path",
        f"{first_sha}..{last_sha}",
    ]
    if pathspec:
        cmd += ["--"] + pathspec
    out = run_git(cmd)
    revs = [r for r in out.splitlines() if r]
    seq = [first_sha] + revs
    # If pathspec filtered everything out but ancestry passed, it's acceptable to return [first_sha]
    if not seq:
        return [first_sha]
    return seq


def print_status(sha: str, idx: int, total: int) -> None:
    print(f"Checked out {short_commit(sha)} — {commit_subject(sha)} ({idx+1}/{total})")


def maybe_warn_last_moved(state: dict) -> None:
    cur_head = head_sha()
    last = state.get("last")
    if not last:
        return
    if cur_head != last and state.get("_warned_head") != cur_head:
        print(
            f"Warning: HEAD has moved since 'start'; using saved <last> {short_commit(last)} (current HEAD is {short_commit(cur_head)}).",
            file=sys.stderr,
        )
        state["_warned_head"] = cur_head  # remember we warned for this HEAD
        save_state(state)


# Command handlers


def cmd_help(args):
    parser = build_parser()
    topic = getattr(args, "topic", None) if args else None
    if topic:
        # find the subparser for the topic
        sp = None
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                sp = action.choices.get(topic)
                choices = sorted(action.choices.keys())
                break
        if sp is None:
            print(f"Unknown topic '{topic}'. Available topics: {', '.join(choices)}", file=sys.stderr)
            sys.exit(1)
        print(INTRO_TEXT)
        print()
        sp.print_help()
        sys.exit(0)
    parser.print_help()
    sys.exit(0)


def cmd_start(args):
    # args.revs is a list possibly containing '--'
    revs = args.revs or []
    # split at '--' if present
    pathspec: List[str] = []
    if "--" in revs:
        i = revs.index("--")
        revs_part = revs[:i]
        pathspec = revs[i + 1 :]
    else:
        revs_part = revs

    if len(revs_part) < 1:
        print("start requires at least <first> revision", file=sys.stderr)
        sys.exit(1)
    first = revs_part[0]
    last = revs_part[1] if len(revs_part) >= 2 else "HEAD"
    if not is_worktree_clean():
        print(
            "Working tree has uncommitted changes; please commit or stash before starting.",
            file=sys.stderr,
        )
        sys.exit(1)
    first_sha = resolve_rev(first)
    last_sha = resolve_rev(last)
    seq = build_sequence(first_sha, last_sha, pathspec)
    orig_ref = symbolic_head()
    orig_head = head_sha()
    state = {
        "original_ref": orig_ref,
        "original_head": orig_head,
        "first": first_sha,
        "last": last_sha,
        "pathspec": pathspec,
        "sequence": seq,
        "index": -1,
    }
    save_state(state)
    print(
        f"Sequence prepared: {len(seq)} commits from {short_commit(first_sha)} to {short_commit(seq[-1])}."
    )


def ensure_state_exists_or_die():
    state = load_state()
    if not state or not state.get("first") or not state.get("last"):
        print(
            "No iter state; run 'git iter start <first> [<last>] [--] [<pathspec>...]' first.",
            file=sys.stderr,
        )
        sys.exit(1)
    return state


def cmd_first(args):
    if not args.rev:
        print("first requires a revision", file=sys.stderr)
        sys.exit(1)
    sha = resolve_rev(args.rev)
    state = load_state() or {}
    if not state.get("original_head"):
        state["original_ref"] = symbolic_head()
        state["original_head"] = head_sha()
    state["first"] = sha
    if not state.get("last"):
        state["last"] = head_sha()
    state["sequence"] = None
    state["index"] = -1
    save_state(state)
    print(f"First set to {short_commit(sha)}")


def cmd_last(args):
    rev = args.rev or "HEAD"
    sha = resolve_rev(rev)
    state = load_state() or {}
    if not state.get("original_head"):
        state["original_ref"] = symbolic_head()
        state["original_head"] = head_sha()
    state["last"] = sha
    state["sequence"] = None
    state["index"] = -1
    save_state(state)
    print(f"Last set to {short_commit(sha)}")


def cmd_next(args):
    state = ensure_state_exists_or_die()
    maybe_warn_last_moved(state)
    if not is_worktree_clean():
        print(
            "Working tree has uncommitted changes; please commit or stash before proceeding.",
            file=sys.stderr,
        )
        sys.exit(1)
    if not state.get("sequence"):
        seq = build_sequence(state["first"], state["last"], state.get("pathspec", []))
        state["sequence"] = seq
    else:
        seq = state["sequence"]
    idx = state.get("index", -1)
    if idx == -1:
        new_idx = 0
    elif idx < len(seq) - 1:
        new_idx = idx + 1
    else:
        print("Already at last commit.")
        sys.exit(0)
    target = seq[new_idx]
    checkout_detached(target)
    state["index"] = new_idx
    save_state(state)
    print_status(target, new_idx, len(seq))


def cmd_prev(args):
    state = load_state()
    if not is_worktree_clean():
        print(
            "Working tree has uncommitted changes; please commit or stash before proceeding.",
            file=sys.stderr,
        )
        sys.exit(1)
    if state:
        maybe_warn_last_moved(state)
    if not state:
        # infer sequence from root -> HEAD along first-parent
        out = run_git(["rev-list", "--reverse", "--first-parent", "HEAD"])
        seq = [r for r in out.splitlines() if r]
        if not seq:
            print("Nothing to iterate.", file=sys.stderr)
            sys.exit(0)
        first = seq[0]
        last = head_sha()
        orig_ref = symbolic_head()
        orig_head = head_sha()
        idx = seq.index(last) if last in seq else len(seq) - 1
        if idx > 0:
            idx -= 1
            target = seq[idx]
            checkout_detached(target)
            state = {
                "original_ref": orig_ref,
                "original_head": orig_head,
                "first": first,
                "last": last,
                "pathspec": [],
                "sequence": seq,
                "index": idx,
            }
            save_state(state)
            print_status(target, idx, len(seq))
            return
        else:
            state = {
                "original_ref": orig_ref,
                "original_head": orig_head,
                "first": first,
                "last": last,
                "pathspec": [],
                "sequence": seq,
                "index": 0,
            }
            save_state(state)
            print("Already at first commit.")
            return
    # state exists
    if not state.get("sequence"):
        seq = build_sequence(state["first"], state["last"], state.get("pathspec", []))
        state["sequence"] = seq
    else:
        seq = state["sequence"]
    idx = state.get("index", -1)
    if idx == -1:
        cur = head_sha()
        if cur in seq:
            idx = seq.index(cur)
        else:
            idx = 0
    if idx > 0:
        idx -= 1
        target = seq[idx]
        checkout_detached(target)
        state["index"] = idx
        save_state(state)
        print_status(target, idx, len(seq))
    else:
        print("Already at first commit.")
        state["index"] = 0
        save_state(state)


def cmd_reset(args):
    state = load_state()
    target = None
    if args.rev:
        target = resolve_rev(args.rev)
    else:
        if state:
            if state.get("original_ref"):
                # checkout symbolic ref
                run_git(["checkout", state["original_ref"]])
                clear_state()
                print(f"Reset to {state['original_ref']} and cleared git-iter state.")
                return
            elif state.get("original_head"):
                target = state["original_head"]
    if not target:
        print("Nothing to reset.", file=sys.stderr)
        sys.exit(0)
    run_git(["checkout", target])
    clear_state()
    print(f"Reset to {short_commit(target)} and cleared git-iter state.")


def cmd_run(args):
    state = load_state()
    if not state or not state.get("first"):
        print(
            "No iter state; set 'first' (or run 'git iter start') before run.",
            file=sys.stderr,
        )
        sys.exit(1)
    if not is_worktree_clean():
        print(
            "Working tree has uncommitted changes; please commit or stash before running.",
            file=sys.stderr,
        )
        sys.exit(1)
    maybe_warn_last_moved(state)
    if not state.get("sequence"):
        seq = build_sequence(state["first"], state["last"], state.get("pathspec", []))
        state["sequence"] = seq
        save_state(state)
    else:
        seq = state["sequence"]
    if not seq:
        print("No commits in sequence.", file=sys.stderr)
        sys.exit(1)
    cmd = args.cmd or []
    if not cmd:
        print("run requires a command to execute.", file=sys.stderr)
        sys.exit(1)
    # If cmd starts with '--', drop it (argparse REMAINDER may include it)
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    order = list(range(len(seq)))
    if args.reverse:
        order = list(reversed(order))
    for idx in order:
        sha = seq[idx]
        checkout_detached(sha)
        state["index"] = idx
        save_state(state)
        # Execute command
        # Use shell=False to avoid surprises; but preserve user's tokenization
        # If they passed a single string, it will be a single token in cmd; respect that.
        proc = subprocess.run(cmd)
        if proc.returncode != 0:
            print(
                f"Command exited {proc.returncode} at {short_commit(sha)} — {commit_subject(sha)}",
                file=sys.stderr,
            )
            sys.exit(proc.returncode)
    print(f"Completed run across {len(seq)} commits.")
    sys.exit(0)


def build_parser():
    parser = argparse.ArgumentParser(
        prog="git iter",
        description=INTRO_TEXT,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="subcmd", title="subcommands")

    sp_help = subparsers.add_parser("help", help="show this help message")
    sp_help.add_argument("topic", nargs="?", help="subcommand to show help for")
    sp_help.set_defaults(func=cmd_help)

    sp_start = subparsers.add_parser(
        "start",
        help="reset iter state and start iteration",
        description=(
            "Reset iter state and start iteration.\n"
            "By default, <last> is the currently checked out commit."
        ),
    )
    sp_start.add_argument(
        "revs", nargs=argparse.REMAINDER, help="first [last] [-- pathspec...]"
    )
    sp_start.set_defaults(func=cmd_start)

    sp_first = subparsers.add_parser(
        "first",
        help="mark <rev> as the oldest commit to consider",
        description="Mark <rev> as the oldest commit to consider.",
    )
    sp_first.add_argument("rev", nargs=1)
    sp_first.set_defaults(
        func=lambda args: cmd_first(arg_obj_from_namespace(args, "rev"))
    )

    sp_last = subparsers.add_parser(
        "last",
        help="mark <rev> as the newest commit to consider",
        description="Mark <rev> as the newest commit to consider.",
    )
    sp_last.add_argument("rev", nargs="?", default=None)
    sp_last.set_defaults(
        func=lambda args: cmd_last(arg_obj_from_namespace(args, "rev"))
    )

    sp_next = subparsers.add_parser(
        "next",
        help="check out the next commit in the sequence",
        description=(
            "Check out the next commit in the sequence. <first> must be set.\n"
            "The first time this is called, it checks out <first>."
        ),
    )
    sp_next.set_defaults(func=cmd_next)

    sp_prev = subparsers.add_parser(
        "prev",
        help="check out the previous commit in the sequence",
        description=(
            "Check out the previous commit in the sequence.\n"
            "This can be called without setting <first>."
        ),
    )
    sp_prev.set_defaults(func=cmd_prev)

    sp_reset = subparsers.add_parser(
        "reset",
        help="finish iteration and go back to commit",
        description="Finish iteration search and go back to commit.",
    )
    sp_reset.add_argument("rev", nargs="?", default=None)
    sp_reset.set_defaults(
        func=lambda args: cmd_reset(arg_obj_from_namespace(args, "rev"))
    )

    sp_run = subparsers.add_parser(
        "run",
        help="automatically iterate and run a command",
        description=(
            "Use <cmd>... to automatically iterate linearly from <first> to <last>.\n"
            "`-r` makes it iterate from <last> to <first> in reverse order.\n"
            "Stops the first time <cmd> exits with a nonzero status."
        ),
    )
    sp_run.add_argument("-r", "--reverse", action="store_true")
    sp_run.add_argument("cmd", nargs=argparse.REMAINDER)
    sp_run.set_defaults(func=cmd_run)

    return parser


def parse_args(argv: List[str]):
    parser = build_parser()

    # fallback help when no subcommand given
    if len(argv) == 0:
        cmd_help(None)

    ns = parser.parse_args(argv)
    return ns


# Helpers to coerce argparse namespace shapes used above
def arg_obj_from_namespace(ns: argparse.Namespace, key: str):
    """Return a simple object with attribute key (unpack single-element lists)."""

    class _A:
        pass

    a = _A()
    val = getattr(ns, key)
    # if it's a list of one, simplify
    if isinstance(val, list) and len(val) == 1:
        val = val[0]
    setattr(a, key, val)
    return a


def main(argv: List[str]):
    ns = parse_args(argv)
    if ns.subcmd == "help":
        cmd_help(ns)
    # For all other commands, acquire the lock.
    _, state_path, lock_path = iter_paths()
    with IterLock(lock_path):
        # dispatch
        try:
            if hasattr(ns, "func"):
                ns.func(ns)
            else:
                print("Unknown command. Use 'git iter help'.", file=sys.stderr)
                sys.exit(1)
        except KeyboardInterrupt:
            print("Interrupted.", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main(sys.argv[1:])
