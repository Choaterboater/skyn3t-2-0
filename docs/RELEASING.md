# Releasing SkyN3t

Releases are built from version tags by `.github/workflows/release.yml`. The
workflow tests the repository, rebuilds the committed dashboard, builds the
wheel and source distribution twice, and requires byte-identical output after
normalizing source-archive member order, timestamps, ownership metadata, and
gzip header to the commit epoch.

## Prepare a release

1. Update `project.version` in `pyproject.toml` and finish the changelog or
   release notes on `main`.
2. Require the normal CI and golden-suite validation checks on the release
   commit. The release workflow repeats lint, type checking, Python tests,
   frontend tests, the frontend build, wheel inspection, and sdist-to-wheel
   parity before publishing.
3. Create a tag that exactly matches the package version, such as `v2.0.0`.
   `scripts/prepare_release.py` rejects a tag/version mismatch.
4. Push the tag. Do not rebuild or upload distributions from a workstation.

Rerunning a release is byte-idempotent. Existing assets are downloaded and
compared with the reproducible rebuild; a changed asset is never overwritten,
while an asset missing from a partially completed release may be added.

The build job has read-only repository permission and no OIDC permission. The
separate attestation and publishing jobs download the already-built artifact,
so package build scripts never run in a job that can mint a signing or PyPI
identity token.

## PyPI setup

GitHub releases work without a package-registry secret. PyPI publishing is
deliberately disabled until the repository owner completes both controls below:

1. Configure a PyPI **Trusted Publisher** for repository
   `Choaterboater/skyn3t-2-0`, workflow `release.yml`, environment `pypi`.
2. Create the GitHub Actions repository variable
   `PYPI_PUBLISH_ENABLED=true` and protect the `pypi` environment as required.

The publishing job uses short-lived OIDC credentials through
`pypa/gh-action-pypi-publish`; no long-lived PyPI token belongs in repository
secrets.

## Provenance setup

Artifact attestations are available for public repositories on GitHub Free,
Pro, and Team, but private repositories require GitHub Enterprise Cloud. This
repository is currently private, so the release workflow always verifies
`SHA256SUMS` but skips the GitHub attestation step by default.

After making the repository public or moving it to an Enterprise Cloud
organization, create the Actions repository variable
`ARTIFACT_ATTESTATION_ENABLED=true`. The attestation job already carries the
required short-lived OIDC, attestations, and artifact-metadata permissions; no
long-lived signing secret is needed.

## Verify a release

Download the wheel, source archive, and `SHA256SUMS` from the GitHub release,
then verify both integrity and signed provenance:

```bash
sha256sum --check SHA256SUMS
# Run these when ARTIFACT_ATTESTATION_ENABLED was set for the release:
gh attestation verify skyn3t-2.0.0-py3-none-any.whl \
  --repo Choaterboater/skyn3t-2-0
gh attestation verify skyn3t-2.0.0.tar.gz \
  --repo Choaterboater/skyn3t-2-0
```

An attestation binds an artifact digest to this repository, commit, tag-triggered
workflow, and GitHub OIDC identity. It complements the checksum; it does not
replace code review, tests, or dependency review.
