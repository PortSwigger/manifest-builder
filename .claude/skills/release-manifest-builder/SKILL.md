---
name: release-manifest-builder
description: Release a new manifest-builder version to PyPI and roll it out through relcoord into the system repo — tag and publish manifest-builder, bump the dependency in portswigger-cloud/relcoord, land that PR, pick up the container image it builds, and pin the new image in portswigger-cloud/system. Use when the user asks to release, ship, publish, or deploy a new manifest-builder version, to get a manifest-builder change into relcoord or into production, or to bump the relcoord image in the system repo.
metadata:
  short-description: Ship a manifest-builder release out through relcoord to the system repo
---

# Release manifest-builder and deploy it through relcoord

Four phases across three repositories, each phase gated on the previous one's CI:

```
manifest-builder tag vX.Y.Z  →  PyPI
        ↓
relcoord PR: manifest-builder>=X.Y.Z  →  merged to main
        ↓
publish-image.yml  →  public.ecr.aws/portswigger-platform/relcoord:<ver>-<hash>
        ↓
system PR: image pin in platform/section.toml  →  ArgoCD
```

Repositories, at their usual local paths:

- **manifest-builder** — this repo (`PortSwigger/manifest-builder`). Version comes
  from the git tag via `hatch-vcs`; there is no version to edit in a file.
- **relcoord** — `~/work/relcoord` (`portswigger-cloud/relcoord`). Depends on
  manifest-builder from PyPI and drives it through its Python API.
- **system** — `~/work/system` (`portswigger-cloud/system`). Pins the relcoord
  container image that runs in the cluster.

If a repo is not at that path, locate it or ask the user before starting.

**Each phase ends at a checkpoint.** Tagging, merging and landing are the user's
calls — propose the action, show what it will do, and wait for a clear yes. Never
merge a PR without being told to.

## Before starting

1. Ask what the release is *for* — which manifest-builder change relcoord needs
   and why. Every commit message and PR body below depends on that answer, and
   "bump the version" is not a description of a change.
2. Ask whether there is a JIRA ticket. The system repo's history prefixes almost
   every commit subject with one (`PLAT-734: Deploy relcoord …`); relcoord and
   manifest-builder do not require it.
3. `git status` in all three repos. Do not stomp on uncommitted work.
4. Per the user's global git workflow: never commit to `main`. Create a fresh
   short-named branch in relcoord and in system, with no `claude/` prefix.
5. Do not add a `Co-Authored-By` line to any commit in these repos.

## Phase 1 — tag manifest-builder and publish to PyPI

1. Release from `origin/main`, and confirm the change being released is actually
   on it:

   ```bash
   git fetch origin && git switch main && git pull --ff-only && git log --oneline -5
   ```

2. Run the repo's checks locally first — the publish workflow runs the same ones,
   and a failure after the tag is pushed is expensive (see step 6):

   ```bash
   uv run ruff check && uv run ruff format --check && uv run ty check && uv run pytest
   ```

3. Work out the next version: highest existing tag, patch level plus one.

   ```bash
   git tag --sort=-v:refname | head -5
   ```

   Sort with `-v:refname`, not lexically — `v0.7.10` sorts below `v0.7.9`
   otherwise. Unless the user says otherwise, only the patch level moves.

4. **Checkpoint.** Tell the user the version you intend to tag and the commit it
   points at, and wait. A pushed tag and a PyPI upload are both effectively
   irreversible: PyPI will not accept a re-upload of a version it already has, so
   a mistake costs another patch number rather than a fix.

5. Tag and push:

   ```bash
   git tag vX.Y.Z && git push origin vX.Y.Z
   ```

   The tag is a plain lightweight tag on the merge commit; no annotation or
   signature is used here.

6. Watch `publish.yml`, which triggers on `v*` tags:

   ```bash
   gh run watch "$(gh run list --workflow=publish.yml --limit 1 --json databaseId -q '.[0].databaseId')"
   ```

   It lints, formats, tests, type-checks, builds the wheel, then publishes from
   the `pypi` environment via trusted publishing. If the environment requires a
   review, the publish job waits — tell the user rather than assuming it hung.

   If the build job fails, the version was never published. Fix the problem on
   `main`, then either delete and re-push the tag on the new commit
   (`git push origin :refs/tags/vX.Y.Z`) or move to the next patch number. Ask
   which; deleting a public tag is the user's call.

7. Confirm the version is resolvable before touching relcoord:

   ```bash
   curl -sf https://pypi.org/pypi/manifest-builder/X.Y.Z/json | head -c 80
   ```

## Phase 2 — raise and land the relcoord dependency bump

1. Branch from up-to-date `main` in `~/work/relcoord`.

2. Raise the floor in `pyproject.toml` — the dependency is a `>=` constraint, not
   a pin:

   ```
   "manifest-builder>=X.Y.Z",
   ```

3. Update the lock file. `[tool.uv.sources]` routes manifest-builder to the
   `upstream` (PyPI) index, because the `reposnake` index still carries older
   versions; keep that intact.

   ```bash
   uv lock --upgrade-package manifest-builder
   git diff --stat   # expect pyproject.toml and uv.lock only
   ```

   Verify the lock really moved: `grep -A2 'name = "manifest-builder"' uv.lock`.
   If it still shows the old version, PyPI has not caught up — wait and retry
   rather than editing the lock by hand.

4. Run what `pr-checks.yml` runs, with the same flags:

   ```bash
   uv run --locked --group dev pytest
   uv run --locked --group dev ruff check
   uv run --locked --group dev ruff format --check
   uv run --locked --group dev ty check
   ```

5. Commit. The house style for these is a subject of `Require manifest-builder
   X.Y.Z`, and a body that says what the new version gives relcoord, why relcoord
   needs it now, and that no source changed — describing the new behaviour, not
   the old bug. `git log -1 b3ae0c6` in relcoord is a good model.

6. Push and open the PR:

   ```bash
   git push -u origin <branch>
   gh pr create --fill
   gh pr checks --watch
   ```

7. **Checkpoint.** Report the PR link and the check results. Ask before merging.
   When told to, merge it the way the repo's history does — squashed:

   ```bash
   gh pr merge <n> --squash --delete-branch
   ```

## Phase 3 — pick up the container image the merge builds

Merging to `main` triggers `publish-image.yml`, which builds and pushes
`public.ecr.aws/portswigger-platform/relcoord:<version>-<hash>`, where `<version>`
is the nearest reachable `vX.Y.Z` tag in relcoord and `<hash>` is a hash of the
build context. A dependency bump changes the hash, not the version. The workflow
skips the build entirely if that exact tag is already in the registry, which is a
success, not a failure.

1. Watch the run:

   ```bash
   gh run watch "$(gh run list --workflow=publish-image.yml --limit 1 --json databaseId -q '.[0].databaseId')"
   ```

2. Read the image tag out of the run. The workflow writes a **publish summary**
   to the run's page — a `## relcoord <version>-<hash>` heading with the full
   image reference, version and commit under it — so the version a run produced
   is on the run's own page rather than buried in a step's log:

   ```bash
   gh run view <id> --web
   ```

   That summary is the thing to read and to quote to the user. It is not exposed
   by the REST API, so from the terminal take the same value out of the log — one
   unique match, and it is the reference the build actually pushed:

   ```bash
   gh run view <id> --log | grep -oE 'public\.ecr\.aws/portswigger-platform/relcoord:[0-9]+\.[0-9]+\.[0-9]+-[0-9a-f]+' | sort -u
   ```

   When the run skipped the build because the tag was already published, that
   line is absent; grep the bare tag instead, which appears either way:

   ```bash
   gh run view <id> --log | grep -oE '[0-9]+\.[0-9]+\.[0-9]+-[0-9a-f]{8}' | sort -u
   ```

   Either grep must yield exactly one value. More than one means you are looking
   at the wrong run, or at a log carrying two builds — open the run page and read
   the summary rather than guessing.

## Phase 4 — pin the new image in the system repo

1. Branch from up-to-date `main` in `~/work/system`.

2. Update the single pin:

   ```bash
   grep -n 'relcoord:' platform/section.toml
   ```

   ```
   image = "public.ecr.aws/portswigger-platform/relcoord:<version>-<hash>"
   ```

3. Prove generation still works and changes nothing else. The system repo's root
   is deliberately bare — a `.venv` with manifest-builder installed into it, no
   `pyproject.toml`. Capture the baseline *before* editing the pin if you can;
   otherwise use `git stash`:

   ```bash
   cd ~/work/system
   uv pip install --upgrade manifest-builder
   uv run manifest-builder -c . -o /tmp/before --target platform-prod   # pin unchanged
   # apply the pin edit, then:
   uv run manifest-builder -c . -o /tmp/after --target platform-prod
   diff -r /tmp/before /tmp/after
   ```

   The only differences should be the relcoord image reference and any checksum
   annotation that follows from it. Anything else means the release changed
   generation output, which the user needs to know about before this lands.

4. Commit with the ticket prefix if there is one, e.g.
   `PLAT-741: Deploy relcoord <what it fixes>`. Say which relcoord PR the image
   was published from and what the new behaviour is.

5. Push and open the PR:

   ```bash
   git push -u origin <branch>
   gh pr create --fill
   gh pr checks --watch
   ```

   A system PR gets a manifest diff comment posted by the *currently deployed*
   relcoord, not the new one. Read it: it should show only the image change.

6. **Checkpoint.** Report the PR link, the check results and the diff comment.
   The user lands it; ArgoCD reconciles from the generated manifests afterwards.

## Gotchas

- **Network git operations may need the sandbox disabled.** On a sandboxed
  session, SSH to github.com is blocked and `git push`/`git fetch`/`gh` hang
  until they time out rather than failing fast. Pass
  `dangerouslyDisableSandbox: true` for anything touching a remote.
- **A Secure-Enclave SSH key needs the user's touch.** `agent refused operation`
  followed by `Permission denied (publickey)` is a Touch ID prompt the user has
  to grant. Try once, then hand them the command — do not loop.
- **Never push a follow-up to a merged PR's branch.** These PRs get merged
  quickly. `gh pr view <n> --json state` first; if merged, branch fresh from
  `origin/main` and open a separate PR.
- **The version chain is one-directional.** There is no way to unpublish a PyPI
  version or move a consumed tag. Every correction is a new patch release.
