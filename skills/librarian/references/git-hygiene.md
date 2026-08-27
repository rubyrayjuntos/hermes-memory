# Git hygiene — trunk-based (from CONTRIBUTING.md, .cursor/rules/git-trunk-hygiene.mdc)

`main` is the only durable branch. `wip/<card>` are disposable, deleted same day they squash-merge.

Before/after:

```bash
git fetch --prune && git status && git worktree list && git branch -vv
git config --global fetch.prune true && git config --global pull.rebase true
```

Maintainers: `git checkout main && git pull origin main` → `git branch -d wip/x` → `git push origin --delete wip/x` → `git fetch --prune`.

Forks: once `gh repo fork; git remote add upstream ...`; every time `git fetch --all --prune && git checkout main && git pull upstream main && git push origin main`; stale? `gh repo sync` or `git reset --hard upstream/main`.

Never push `stash/local-sync/snapshot` branches.
