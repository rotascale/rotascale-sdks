# Releasing

Two packages, released independently:

| Package | Directory | Tag prefix |
|---|---|---|
| `rotascale` | `python/` | `python-v` |
| `rotascale-mcp` | `mcp-python/` | `mcp-v` |

They version separately on purpose. `rotascale-mcp` tracks a spec that revises
on its own schedule, and pinning them together would force pointless releases of
one to keep up with the other.

## Releasing

1. Bump `version` in that package's `pyproject.toml`. It is the **only** place
   the number appears — `_version.py` reads it back from installed metadata.
2. Update that package's `CHANGELOG.md`.
3. Tag and push:

```bash
git tag python-v0.2.1 && git push origin python-v0.2.1
```

That is the whole process. The workflow lints, tests, builds, verifies, and
publishes.

## What the pipeline refuses to let through

- **A tag that disagrees with `pyproject.toml`.** `python-v0.3.0` against a
  pyproject saying `0.2.0` fails before anything is built. A PyPI version can
  never be replaced, so a wrong number is permanent.
- **A wheel that does not import in a clean environment.** This is the check
  that catches the real failure: a package imports fine from its source tree,
  because the source tree is on `sys.path`, and is missing a subpackage from the
  wheel. Both packages have one.
- **A provider dependency leaking into `rotascale`.** A governance library that
  forces a version conflict on the customer is a governance library that does
  not get installed.
- **An unwired console script in `rotascale-mcp`.** An MCP host launches
  `rotascale-mcp` and nothing else; unwired, the package installs cleanly and is
  completely unusable.
- **Metadata PyPI would reject** (`twine check`).

## One-time setup on PyPI

Per package, at `https://pypi.org/manage/project/<name>/settings/publishing/`:

| Field | Value |
|---|---|
| Owner | `rotascale` |
| Repository | `rotascale-sdks` |
| Workflow | `release.yml` |
| Environment | `pypi` |

Until this exists the publish step fails with an authentication error. That is
the correct behaviour — no credential means no upload.

## Why trusted publishing, and not a token

On 2026-08-01 a release was pushed from a laptop and landed on the **wrong PyPI
account**, because the token used was chosen from a section name in `~/.pypirc`
rather than from a verified identity. The section was called `[rotascale]`; the
token was account-wide and belonged to somebody else. Nothing about the file
made that visible.

Trusted publishing removes the class of error rather than mitigating it. The
identity comes from this repository over OIDC. There is no long-lived token to
leak, to mislabel, or to reach for by mistake, and `~/.pypirc` stops being part
of the release path at all.

**Do not publish from a laptop.** If the pipeline is broken, fix the pipeline.

## Dry run

Actions → Release → *Run workflow* → pick a package. It builds and verifies and
publishes nothing; the publish job only runs for a tag.

## Checking the providers have not moved

```bash
cd python
uv run --with openai --with anthropic --with google-genai \
    python scripts/validate_providers.py
```

Calls each real API once and asserts every field the middlewares read is
actually there. Every other test uses a fake written from the documentation,
which proves a middleware handles the shape we *believe* a provider returns —
and that belief is the thing worth checking, because a wrong one records `None`
silently.

Its first run found two bugs nothing else could: Anthropic writing
non-normalised token field names, and the Gemini wrapper letting the provider's
client be garbage-collected mid-call. It also found `gemini-2.0-flash` had been
retired.

It runs weekly in CI (`.github/workflows/providers.yml`) with repository
secrets, and never gates a pull request — it spends money and needs
credentials, and a contributor without secrets should not see a red build they
cannot fix. A provider whose key is absent is reported SKIPPED, never as
passing.

**Provider keys belong in repository secrets, not on the demo host.** The demo
uses a local Ollama, so a reseed costs nothing and cannot silently thin out when
a card expires.
