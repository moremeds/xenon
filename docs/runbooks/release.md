# Release Runbook

## Prerequisites (laptop)

- `git` and `gh` (GitHub CLI) installed. Authenticate `gh` once: `gh auth login`.
- `uv` on PATH (`cut.sh` runs stdlib Python through `uv run` to rewrite `package.json` and `CHANGELOG.md`).
- Clean working tree on `master`, synced with `origin/master`.

`cut.sh` will abort with a clear message if any of these are missing.

## Cutting a release

The release goes through a PR and the tag is pushed alone. Do not push `master`.

1. `git checkout master && git pull`
2. `scripts/release/cut.sh` — answer the bump prompt, review the diff, confirm. It commits `release: vX.Y.Z` on local `master` and creates a local annotated tag. Ignore its hint to push `master`.
3. Move the commit to a release branch and put local `master` back:
   `git switch -c release/vX.Y.Z && git tag -d vX.Y.Z && git branch -f master origin/master`
4. Push the branch and open the release PR: `git push -u origin release/vX.Y.Z`, then `gh pr create`.
5. When CI is green, merge the PR.
6. **Pause Watchtower on the mini and confirm it is stopped.** The tag push moves `:latest`, and a running Watchtower recreates the api on its own, with no socket check. Command and expected output: `docs/runbooks/remote-deploy.md` § Standard release flow, step 2 (`docker inspect -f '{{.State.Running}}' xenon-watchtower-1` must print `false`). Do not continue until it does.
7. Tag the merge commit:
   `git fetch origin && git tag -a vX.Y.Z -m vX.Y.Z $(gh pr view <PR> --json mergeCommit --jq .mergeCommit.oid)`
8. Push the tag alone: `git push origin refs/tags/vX.Y.Z`.
9. Watch the `Release` workflow in GitHub Actions (check the run for the tag commit: `gh run list --workflow Release --commit <sha>`). It runs the full test suite on the tag ref, creates the GitHub Release, and pushes the images to GHCR. Deploy to the mini with `docs/runbooks/remote-deploy.md` § Standard release flow, steps 4-9.

## Rolling back a bad cut (not yet deployed)

```bash
git push --delete origin refs/tags/vX.Y.Z
git tag -d vX.Y.Z
gh release delete vX.Y.Z --yes
```

Then revert the release commit through a PR and cut again when ready.

## When CI blocks the release

`cut.sh` refuses if the CI run for the `origin/master` head commit isn't green. Fix the failing test on master first (a regular PR), merge, wait for green, then cut.

## Version authority

`VERSION` is authoritative. `package.json` mirrors it. The `version-sync` CI job fails any PR that drifts them.
