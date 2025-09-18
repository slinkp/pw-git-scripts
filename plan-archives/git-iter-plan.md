Here’s a minimal, iterative implementation plan for a Python 3 script named
`git-iter` that behaves like git bisect but walks commits linearly, following the
spec in git-script-spec.txt. Keep each step small, runnable, and
testable. I’ll also note key design choices and edge cases.

# High-level behavior and design choices


 * The file is named `git-iter.py` so our linter finds it.
 * It is symlinked to `git-iter` so git can run it like `git iter ...`
 * Command dispatch: git-iter  [options]. Subcommands: help, start, first, last, next, prev, reset, run.
 * Commit sequence definition: follow the first-parent ancestry path from last
   to first, optionally filtered
   by a pathspec (after --). Sequence order is the reverse (first to last,
   oldest to newest).
 * Defaults:
    * <last> defaults to HEAD at the time of start (resolved to a specific commit SHA).
    * If no state exists and prev is called, infer a linear sequence along HEAD’s first-parent chain (root→HEAD).
 * State storage: .git/iter/state.json with:
    * original_ref (symbolic ref if on a branch, else SHA)
    * first, last (as SHAs)
    * pathspec (array), used to regenerate or for reference
    * sequence (list of SHAs including first and last)
    * index (int; -1 = none checked out yet; 0..len-1 = current position)
 * Checkout style: detached HEAD checkout of target commit.
 * Dirty worktree policy: refuse to proceed if there are uncommitted changes
   (like git bisect). No automatic stash/auto-save.
 * Help text: Use the text from git-script-spec.txt to build help strings for argparse.

## Implementation steps

Step 0: Scaffolding

 * Create executable script git-iter with shebang: #!/usr/bin/env python3.
 * Add basic argparse to route subcommands; stub handlers that print “not implemented” with exit
   code 2.
 * Implement help that prints the arg help and leading content as per git-script-spec.txt

Step 1: Git helpers


 * Add small functions:
    * run_git(args, cwd) → stdout string, raises on nonzero.
    * repo_root() → path via git rev-parse --show-toplevel.
    * git_dir() → repo_root/.git (handle worktrees via git rev-parse --git-dir).
    * resolve_rev(rev) → SHA via git rev-parse --verify .
    * is_worktree_clean() → git diff-index --quiet HEAD -- and git diff --cached --quiet.
    * short_commit(sha) → git show -s --format=%h .
    * commit_subject(sha) → git show -s --format=%s .
    * symbolic_head() → git symbolic-ref -q --short HEAD (may fail).
    * head_sha() → resolve_rev("HEAD").

* Add error formatting and consistent exit codes (1 for usage/logic errors, pass-through for “run”).

Step 2: State management

 * Create .git/iter/ directory as needed.
 * Implement `load_state()/save_state()` on .git/iter/state.json.
 * Validate schema on load (missing keys → treat as no state).
 * Locking: simple best-effort lock file .git/iter/lock (create+pid; if exists, warn and proceed or fail — default to fail to avoid races).

Step 3: Commit sequence construction


 * Implement build_sequence(first, last, pathspec):
    * Verify both SHAs exist.
    * Verify ancestry: ensure first is an ancestor of last along first-parent chain. Use:
       * git merge-base --is-ancestor   to ensure first is ancestor (general ancestry).
       * For sequence, use first-parent path: revs = git rev-list --reverse --first-parent
         --ancestry-path  ^ -- [pathspec...] Then sequence = [first] + revs
    * If pathspec is provided, apply it after -- as shown above; note:  may not touch the pathspec —
      still include it as the starting point.
    * If resulting sequence length == 0 or == 1 but last != first, surface a clear error (no linear path found).
 * Keep sequence as SHAs (not refs).

Step 4: start subcommand

 * Parse: git-iter start  [] [--] [...]
 * Resolve first (SHA), last (SHA or default to current HEAD SHA).
 * Ensure clean worktree (refuse otherwise).
 * Build sequence; store state:
    * original_ref: symbolic HEAD ref if available else starting HEAD SHA
    * first, last, pathspec, sequence, index=-1
 * Do not checkout any commit yet.
 * Print summary: “sequence prepared: N commits from <short(first)> to <short(last)>”.

Step 5: first subcommand

 * Parse: `git-iter first <rev>`
 * rev is required
 * Resolve to SHA, store/overwrite first in state (create state if not exists).
 * Set last to current HEAD SHA if not already set.
 * Clear any existing sequence/index so it will be rebuilt on next/prev as needed.
 * Do not checkout.

Step 5.b: last subcommand

 * Parse: `git-iter last [<rev>]`
 * rev is optional, defaults to current checkout
 * Resolve to SHA, store/overwrite last in state (create state if not exists).
 * Clear any existing sequence/index so it will be rebuilt on next/prev as needed.
 * Do not checkout.

Step 6: next subcommand

 * Preconditions: state must have first and last. If sequence missing, build it (using stored pathspec, if any).
 * Ensure clean worktree.
 * Move index:
    * If index == -1: index = 0 (checkout first)
    * Else if index < len(sequence)-1: index += 1
    * Else: print “Already at last commit” and exit 0 without changing checkout.
 * Checkout sequence[index] detached; print short SHA and subject.

Step 7: prev subcommand

 * Ensure clean worktree.
 * If state exists:
    * If sequence missing, rebuild.
    * If index == -1: initialize index to current checkout’s position if in sequence (find HEAD SHA in sequence). If not found, set to 0 and proceed.
    * If index > 0: index -= 1; checkout sequence[index].
    * Else: print “Already at first commit”.
 * If no state exists:
    * Infer default sequence from root→HEAD along first-parent:
       * first = first commit along first-parent: git rev-list --max-parents=0 --first-parent HEAD |
         tail -n1 (if multiple, choose the one that’s an ancestor).
       * last = HEAD
       * sequence = git rev-list --reverse --first-parent HEAD
    * Set index to position of HEAD in sequence; then if index > 0, index -= 1 and checkout; save state with inferred original_ref.
 * Print short SHA and subject on checkout.

Step 8: reset subcommand

 * Parse: `git-iter reset [<rev>]`
 * Determine target:
    * If `rev`  provided: resolve and checkout that.
    * Else if state has original_ref (symbolic): checkout that ref.
    * Else if state has original HEAD SHA: checkout that SHA.
    * Else: print “Nothing to reset” and exit 0.
 * Remove .git/iter/ state (state.json and lock).
 * Print confirmation.

Step 9: run subcommand


 * Parse: `git-iter run [-r] <cmd>`
 * Preconditions: first must be set (via start or first). If sequence missing, build it.
 * Determine order: normal (index from first -> last) or reverse (last -> first)
 * Before running, ensure clean worktree.
 * Loop:
    * For each commit in order:
       * Checkout commit (detached), update state index accordingly.
       * Execute `cmd` as a subprocess (shell=True; accept everything
         after run as cmd argv).
       * If exit != 0: print stop message to stderr with commit short SHA; exit with that code (leave HEAD at failing commit; do not reset state).
 * If loop completes: print success message; exit 0.
 * Note: do not auto-reset; user can git iter reset to go back.

Step 10: Pathspec handling and argument parsing details

 * Ensure a literal `--` separates revision arguments from pathspec (as in the spec).
 * Accept empty pathspec (default to all paths).
 * Store pathspec in state; always pass it to rev-list when rebuilding sequences.

Step 11: Safety and UX improvements

 * Detect non-ancestor first/last and give a clear message suggesting to set a different pair or use
   start with explicit last.
 * Warn if branch moves after start (i.e., last SHA recorded differs from current HEAD). We use
   stored last SHA to be deterministic.
 * Print concise status on every checkout: `“Checked out <rev> —  (i/k total)”`.

Step 12: Minimal internal tests/manual checks

 * On a test repo:
    * git-iter help prints spec.
    * git-iter start HEAD~5 → creates state with sequence of 6 commits, index -1.
    * git-iter next (x6) walks to last, then “Already at last commit”.
    * git-iter prev (x6) walks back, then "Already at first commit."
    * git-iter reset returns to original branch.
    * git-iter start A B -- path/to/file(s) → sequence respects pathspec, only
      includes commits that touch the specified file(s)
    * `git-iter run pytest -q` stops on first failing commit with that exit code; reverse order with
      -r.
    * Dirty worktree → next/prev/run refuse with explanatory error.

Non-goals for v1 (explicitly not supported now)

 * skip, bad, good, log, replay, and other bisect-like features explicitly marked as “may support
   later” in the spec.

Questions / confirmations

 * Sequence policy: I plan to use the first-parent ancestry path to keep
   iteration predictable across merge-heavy histories. Is that acceptable? YES

 * Dirty worktree: Should we offer a --force to proceed anyway or stash
   automatically? Default is to refuse like bisect. NO
