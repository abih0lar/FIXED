# Security runbook — secrets, licensing & git history

## What happened

Commit `b10c58f` (the repo's initial commit) contained the license master
secret hardcoded in `app.py` (referred to below as `<LEAKED_SECRET>` — the
literal string is deliberately not reproduced in this file, so a history
purge leaves zero occurrences in the tree):

```python
_EMBEDDED_SECRET = "<LEAKED_SECRET>"
```

Anyone with repo access (or the distributed `ALADIN.exe`, which bundles the
same code) could mint unlimited license keys with `keygen.py`. The committed
`dist/ALADIN.exe` and `build/` artifacts contain the same secret inside their
bundled bytecode.

**Fixed on this branch:**

* `app.py` now ships with an **empty** `_EMBEDDED_SECRET` placeholder; the
  key is resolved at runtime from `ALADIN_DRM_KEY` (env / `data/.env`) and
  injected into the exe only at **build time** by
  `inject_env.py --embed-drm-key`.
* `build.bat` updated; it reminds you not to commit the post-build `app.py`.
* `build/`, `dist/` are no longer tracked; `.gitignore` now covers `data/`,
  `db/`, `*.key`, `CLIENT_LICENSE_KEY.txt`, and all build output.

## 1. Rotate the compromised key (do this first)

Rotation, not history deletion, is what actually neutralizes a leaked secret.

1. Generate a new secret:
   ```
   python -c "import secrets; print(secrets.token_urlsafe(32))"
   ```
2. Put it in `data/.env` as `ALADIN_DRM_KEY=<new secret>`.
3. **Regenerate every client license** with `python keygen.py` and
   redistribute the new keys. Licenses made with the old secret will fail
   validation against the new one — coordinate the swap with clients.
4. If `<LEAKED_SECRET>` is used anywhere else (email, other accounts, other
   bots), change it **there** too. Treat it as public from the moment it
   was committed.

## 2. Purge the secret from git history (repo owner action)

`main` still carries the old commit (and the 176 MB of `build/`/`dist/`
artifacts). This session works exclusively on the branch
`arena/01a03e9c-fixed` and cannot rewrite or force-push `main` — run this
yourself from a clone.

### Option A — fresh single-commit history (recommended)

The repo has exactly one initial commit, so history preservation buys you
nothing. From a machine with the final, fixed working tree (after merging
the fix branch — **use a squash merge** so the old history isn't grafted
back in):

```bat
git checkout main
:: make sure the working tree is the fixed version (secret removed,
:: artifacts untracked) — e.g. after squash-merging the fix branch
git checkout --orphan clean-main
git add -A
git commit -m "ALADIN: fresh history (secret purged, build artifacts untracked)"
git branch -D main
git branch -m main
git push -f origin main
```

This also shrinks the repo from ~275 MB to a few MB (old blobs become
unreachable and GitHub GC reclaims them).

### Option B — keep commit history, scrub the string

Requires [git-filter-repo](https://github.com/newren/git-filter-repo):

```bash
echo 'PASTE_THE_OLD_SECRET_HERE==>***REMOVED***' > replacements.txt
git filter-repo --replace-text replacements.txt \
                --invert-paths --path build --path dist --force
git push -f origin main
```

### After either option

* Collaborators must **re-clone** (old clones hold the old history).
* GitHub may retain cached/attributed views of old commits for a while; for
  a private repo that is low risk **once the key is rotated**. To be
  thorough, ask GitHub Support to garbage-collect unreachable objects.

## 3. Ongoing rules

| Never commit | Instead |
|---|---|
| `data/.env`, root `.env` | `.env.example` as the documented template |
| `app.py` right after a build (embeds the DRM key) | `git checkout -- app.py`, then commit |
| `dist/ALADIN.exe`, `build/` | rebuild with `build.bat`; they're gitignored |
| `CLIENT_LICENSE_KEY.txt`, `*.key` | share out-of-band |
| `data/`, `db/` (trade history, logs) | they contain account metadata |

`inject_env.py` blanks `MT5_*`, `TELEGRAM_*`, `GEMINI_*` and `LICENSE_KEY`
in every build by default; the DRM key is embedded **only** when you pass
`--embed-drm-key` (the exe cannot validate licenses offline without it —
that is inherent to the symmetric Fernet scheme, so treat every exe you
distribute as "contains the license validation key").
