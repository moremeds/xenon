# Release Runbook

## Prerequisites (laptop)

- `git` and `gh` (GitHub CLI) installed. Authenticate `gh` once: `gh auth login`.
- `python3.13` on PATH (used by `cut.sh` to rewrite `package.json` and `CHANGELOG.md`).
- Clean working tree on `master`, synced with `origin/master`.

`cut.sh` will abort with a clear message if any of these are missing.

## Cutting a release

1. `git checkout master && git pull`
2. `scripts/release/cut.sh` — answer bump prompt, review diff, confirm.
3. Review the new commit and tag: `git log -1 --stat && git show v0.0.2`
4. Push: `git push origin master --follow-tags`
5. Watch the `Release` workflow in GitHub Actions. It runs the full test suite on the tag ref, then creates the GitHub Release.

## Rolling back a bad cut (not yet deployed)

```bash
git tag -d v0.0.2
git push --delete origin v0.0.2
gh release delete v0.0.2 --yes
git revert <release-commit-sha>
git push origin master
```

Then re-run `scripts/release/cut.sh` when ready.

## When CI blocks the release

`cut.sh` refuses if the latest master run isn't green. Fix the failing test on master first (a regular PR), merge, wait for green, then cut.

## Version authority

`VERSION` is authoritative. `package.json` mirrors it. The `version-sync` CI job fails any PR that drifts them.
