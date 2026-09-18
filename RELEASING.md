# Releasing `aether-nano`

Nano publishes from one reviewed commit to PyPI and GitHub Releases.

## One-time setup

Configure `aether-nano` on PyPI with a trusted publisher matching:

| Field | Value |
| --- | --- |
| Owner | `AetherAI3` |
| Repository | `Nano` |
| Workflow | `publish.yml` |
| Environment | `pypi-production` |

Create the matching `pypi-production` environment in the GitHub repository.
No long-lived PyPI token is required.

## Cut a release

1. Update the identical version strings in `pyproject.toml` and
   `nano/__init__.py`.
2. Move the release notes from `Unreleased` into a dated section in
   `CHANGELOG.md`.
3. Regenerate version-bound receipts with `python tests/regen_goldens.py`.
4. Run `python -m pytest -q`, build the distributions, and inspect them with
   `twine check`.
5. Merge the release PR after every required check passes.
6. Create an annotated `vX.Y.Z` tag on that merge commit.
7. Dispatch `publish.yml` with the tag as `ref`.
8. Create the GitHub Release from the same tag and paste the matching changelog
   section into its notes.

PyPI files are immutable. Never reuse a version after any artifact for it has
been uploaded.
