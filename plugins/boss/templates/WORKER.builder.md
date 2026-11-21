# Builder rules (commits, git, coverage; read after WORKER.md)

- Commits: one commit per mutation row in a git repo: path-scoped, with the `Orch-Attempt:` trailer. Otherwise the result says `sha: none` with the reason, plus the artifact paths. Read-only rows don't commit. Stage files by path and give the SHA in the summary.
- Git identity: commit with the identity the repo's config sets; in a new clone or worktree check `git config user.email` first.
- Never `git commit --amend` or rebase in a shared repo; make a new commit, and never `reset --hard`.
- Concurrent agents in one checkout → isolated git worktrees; commit path-scoped, never `git add -A` in a shared tree.
- A result with `sha: none` also gives `sha_reason: <why>` (read-only row, no git repo, nothing to commit).
- When you change behaviour, carry the rule to every place it applies: `rg` every reader, writer and route, and list each in the result as covered or out of scope (path:line).
- Reuse first: name the existing pieces you reused (path:line), and test against one real sample, not only fixtures.
