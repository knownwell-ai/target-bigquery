# AGENTS.md

> Instructions for AI coding agents working in this repo. Follows the [agents.md](https://agents.md) open standard. User prompts always override anything written here.

## Project overview

`kw-target-bigquery` is Knownwell's variant of [z3z1ma/target-bigquery](https://github.com/z3z1ma/target-bigquery), a Singer target that loads data into BigQuery via the Storage Write API, batch load jobs, GCS staging, or the legacy streaming API. It is a Python library plus a `target-bigquery` console script built on the Meltano Singer SDK (`singer-sdk ~=0.50`) and packaged with Poetry. The import package is `target_bigquery`; only the distribution name carries the `kw-` prefix (Singer's convention for alternate implementations, like `z3-` upstream). Consumers are Knownwell connector jobs running Python 3.13.

This is a **public** repository. Nothing organisation-specific (GCP project IDs, bucket names, Artifact Registry URLs, service accounts) may be committed; such values reach CI only through Cloud Build trigger substitutions.

## Setup

Requires Python 3.10+ (`pyproject.toml`); CI and consumers use 3.13, so prefer 3.13 locally.

```bash
pipx install poetry            # Poetry 2.x
poetry install                 # creates the venv and installs the dev group
poetry run target-bigquery --about
```

Gotcha: if `poetry` fails with `Command '.../pyenv/shims/python' ... exit status 127`, pyenv has no `python` for this directory. Run `pyenv local 3.13.3` (or another installed version). `.python-version` is gitignored, so this stays local. Do not commit it.

## Build and run

```bash
poetry build --clean                              # sdist + wheel in dist/ (gitignored)
poetry run target-bigquery --config .secrets/config.json < input/tap-carbon-intensity.jsonl
meltano install && meltano elt tap-carbon-intensity target-bigquery   # optional, see meltano.yml
```

`.vscode/launch.json` runs the target against `input/tap-carbon-intensity.jsonl` with `.secrets/config.json`.

## Testing

```bash
poetry run pytest                          # 35 unit tests pass; 13 integration tests skip without BQ_PROJECT/BQ_DATASET/GCS_BUCKET
poetry run pytest -m "not integration"     # exactly what GitHub Actions runs
poetry run pytest target_bigquery/tests/test_utils.py -k transform_column_name
```

- Tests live in `target_bigquery/tests/`. `test_utils.py` and `test_config.py` are pure. `test_core.py` and `test_sync.py` are marked `integration` at module level and write to a real BigQuery dataset and GCS bucket; they read `BQ_PROJECT`, `BQ_DATASET`, `GCS_BUCKET`.
- **Credentials are optional.** `target_bigquery/tests/config.py` passes `BQ_CREDS` through as `credentials_json` only when it is set; otherwise no credentials are supplied and every client factory falls back to Application Default Credentials (`core.py:627`, `:637`, `:649` — only `project` is required by the config schema). That is how CI authenticates: the Cloud Build service account is the identity. Build test configs with `integration_target_config(**overrides)` rather than reading the environment inline.
- `target_bigquery/tests/conftest.py` skips `integration` tests when any of the three location variables is unset, so a credential-less run is green rather than a `KeyError`. Markers are strict (`addopts = "--strict-markers"` in `pyproject.toml`); register new markers there.
- Anything that needs network, GCP, or credentials must carry `pytestmark = pytest.mark.integration`. The marker decides **where** a test runs: GitHub Actions runs `-m "not integration"` and holds no BigQuery secrets, while `cloudbuild.yaml` runs the whole suite.
- Where each suite runs: `.github/workflows/ci.yml` — unit tests only, Python 3.13. `cloudbuild.yaml` — full suite in three sequential `pytest` invocations (test_core, then test_utils + test_config, then test_sync), preserving the order the GitHub workflow used historically. They share one Cloud Build step because each step starts a fresh container and only `/workspace` persists, so the dev-group install would otherwise be lost.
- The GitHub matrix is **3.13 only**. Python 3.14 is blocked by the lock file: `pyarrow` is pinned at 21.0.0 and its first cp314 wheel is 22.0.0, so a 3.14 install tries to build Arrow from source. Nothing constrains `pyarrow` upward — it is just an old lock — but `poetry lock --regenerate` moves 48 packages including protobuf 6→7 (this repo does protobuf codegen) and singer-sdk 0.51→0.54.5, so treat the 3.14 matrix leg as its own dependency-bump ticket.

## Code style

- Formatter: black, line length 100, preview mode (`[tool.black]` in `pyproject.toml`). The locked black 21.12b0 crashes on import with current click; run a newer black via `pipx run black --line-length 100 target_bigquery` until the dev group is bumped.
- Imports: isort, profile black (`poetry run isort target_bigquery`).
- Lint / types: `poetry run flake8 target_bigquery`, `poetry run mypy target_bigquery` (mypy 0.910; expect noise).
- Poetry warns that the `[tool.poetry]` metadata tables are deprecated. Migrating to `[project]` is a known follow-up; do not do it as a side effect of another change, and re-lock if you do.

## Project layout

- `target_bigquery/target.py` — `TargetBigQuery` entry point and config schema.
- `target_bigquery/core.py` — sink base classes, schema translation, column-name transforms, credentials.
- `target_bigquery/storage_write.py`, `batch_job.py`, `gcs_stage.py`, `streaming_insert.py` — one sink per load method.
- `target_bigquery/proto_gen.py` — protobuf schema generation for the Storage Write API.
- `target_bigquery/tests/` — pytest suite (see Testing).
- `cloudbuild.yaml`, `cloudbuild-deploy.yaml` — Cloud Build CI and release configs (see below).
- `.changes/` + `.changie.yaml` — changelog fragments; `CHANGELOG.md` is generated by changie.
- `meltano.yml` — Meltano project for local end-to-end runs; `input/` — sample Singer stream.

## Conventions and gotchas

### Cloud Build (learned the hard way)

Both configs build the repo with Google Cloud buildpacks (`gcr.io/k8s-skaffold/pack` + `gcr.io/buildpacks/builder:google-24`, `GOOGLE_PYTHON_VERSION=3.13.*`) instead of installing Poetry by hand, then run a step **inside the built image**.

- The Python buildpack picks Poetry because `poetry.lock` exists and runs `poetry install --sync --only main --no-root`. The image has Poetry and the runtime deps but **not the dev group**; the test step adds it with `poetry install --with dev --no-root`. A `requirements.txt` at the repo root would take precedence and silently switch the build to pip. Do not add one.
- Steps on the buildpack image must use `entrypoint: launcher` with a leading `--` argument (`args: ["--", "bash", "-c", ...]`). Without `--` the CNB launcher rewrites every argument through `eval echo`; an inline script with quotes, `$(...)` or `#` comments becomes an empty command that **exits 0 without running anything**. A step that finishes in under a second is the tell.
- The image runs as uid 33 and Cloud Build sets `HOME=/builder/home` (root-owned). Poetry therefore needs `POETRY_CACHE_DIR=/tmp/...` (an unwritable cache surfaces as "All attempts to connect to pypi.org failed") and `VIRTUAL_ENV` exported to the buildpack venv, which is first on `PATH`: `export VIRTUAL_ENV="$$(dirname "$$(dirname "$$(command -v python)")")"`. `/workspace` is root-owned too, so pytest runs with `-p no:cacheprovider` and the deploy config has a root step that creates and `chmod 0777`s `dist/` before `poetry build`.
- Shell `$` must be written `$$` in Cloud Build YAML; a bare `$NAME` or `${NAME}` is a Cloud Build substitution. The intended substitutions are `$TAG_NAME`, `${_PYTHON_REPOSITORY_URL}` (deploy) and `${_BQ_PROJECT}` / `${_BQ_DATASET}` / `${_GCS_BUCKET}` (CI). The three CI ones are declared with empty defaults in a `substitutions:` block so a manual submit without them still works — the integration tests simply skip.
- The integration tests in Cloud Build authenticate as the build's service account via ADC, so that account is the identity that writes to BigQuery and GCS. As of 2026-09-03 this is **applied**: a **dedicated CI service account** rather than added grants on `kw_cloud_deploy_sa_roles` (that account also holds Cloud Deploy and Artifact Registry access, and the pull-request trigger runs untrusted code from a public repo), carrying five **project-level** bindings: `roles/bigquery.dataEditor`, `roles/bigquery.jobUser`, `roles/storage.objectUser`, `roles/storage.bucketViewer`, `roles/logging.logWriter`. Scoping is service-level by decision, not dataset- or bucket-scoped. Two consequences: project-level `dataEditor` carries `bigquery.datasets.create`, so the suite creates its dataset on the first green build rather than needing one pre-created; and project-level `objectUser` already covers the build's log-object writes, so `defaultLogsBucketBehavior: REGIONAL_USER_OWNED_BUCKET` costs no extra binding. The staging bucket must still pre-exist, in a location matching the target's `location` option (default `US`), because `create_bucket_if_not_exists` in `gcs_stage.py` is unreachable - `Client.get_bucket` raises `NotFound` instead of returning `None`. Nothing deletes staged blobs or test tables afterwards, and the CI dataset is unmanaged with no default table expiry.
- The pull-request trigger runs as that dedicated service account, but its `_BQ_PROJECT` / `_BQ_DATASET` / `_GCS_BUCKET` substitutions are **deliberately unset**, so the integration tests skip on pull requests. **Do not set them until the two failures below are fixed (KPD-4933).** Verified 2026-09-03 by running `cloudbuild.yaml` as that account against a real project and bucket: `test_core` passed, `test_utils` + `test_config` passed 35, `test_sync` was 9 failed / 3 passed, with no permission errors anywhere - both failures are code, not IAM.
  - All eight `test_basic_sync` cases (four load methods x two batch modes) raise `singer_sdk.exceptions.MissingKeyPropertiesError`. singer-sdk 0.50 validates the **post-transform** record against `key_properties` - `singer_sdk/target_base.py:334` calls `sink._singer_validate_message(transformed_record)` - and the non-denormalized path has already replaced the record with `{data, _sdc_*}`, so `id` is absent. This arrived with the `singer-sdk ~=0.50` upgrade and stayed invisible because the tests were skipped.
  - `test_basic_denorm_sync[gcs_stage]` raises `TypeError: Type is not JSON serializable: decimal.Decimal` at `gcs_stage.py:170`. `batch_job.py:118` got an orjson `default=` handler for `Decimal`; the identically shaped `process_record` in `gcs_stage.py` did not. `storage_write.py:320` has the same bare `orjson.dumps` and is unreachable until the first failure is fixed. This is a production defect for `method: gcs_stage` with numeric columns, not only a test artifact.
  - What that run did **not** exercise: `storage.objects.create` / `objects.get` on the staging bucket. Both failures fire before any blob is uploaded, so the write-then-load-from-URI path is still unproven; a 403 there on the first green run would be IAM, not a new bug. These bindings live in Terraform, not in gcloud: `ops_center/main.tf` in `knownwell-gcp-foundation`, and applies there are manual.
- `cloudbuild-deploy.yaml` refuses to publish when `TAG_NAME` is set and does not equal `v<pyproject version>`; manual `gcloud builds submit` runs have an empty `TAG_NAME` and skip the check. Uploads use `artifacts.pythonPackages` with the repository URL from the `_PYTHON_REPOSITORY_URL` substitution (substitutions work inside `artifacts`). Artifact Registry rejects re-uploading an existing version, so every release needs a version bump.
- To test a config by hand: `gcloud builds submit --config cloudbuild.yaml .` and `gcloud builds submit --config cloudbuild-deploy.yaml --substitutions=_PYTHON_REPOSITORY_URL=<url> .`. Testing the deploy config publishes for real; use a scratch version such as `0.7.2.dev0` (`poetry version 0.7.2.dev0`, submit, `poetry version 0.7.2`) and delete it from the registry afterwards.
- `pack` can be run locally with rootless podman for fast iteration: `DOCKER_HOST=unix:///run/user/$UID/podman/podman.sock pack build <name> --builder gcr.io/buildpacks/builder:google-24 --docker-host inherit --env 'GOOGLE_PYTHON_VERSION=3.13.*'`, then `podman run --rm -v "$PWD":/workspace:ro -w /workspace -e HOME=/builder/home --entrypoint launcher <name> -- bash -c '...'` reproduces the Cloud Build environment closely. Always test the exact inline invocation, not a script file, or the `eval echo` problem hides.

### Packaging

- Keep the distribution name `kw-target-bigquery`; never rename the `target_bigquery` package or the `target-bigquery` script.
- Poetry's lock content-hash ignores the project name, so a rename does not require re-locking; changing dependencies does (`poetry lock`, then `poetry check --lock`).
- Renaming a Poetry project changes its venv name; run `poetry install` again afterwards.
- Consumers may still have `z3-target-bigquery` installed; both distributions own the same files, so the old one must be uninstalled first. Say so in changelog entries that affect installation.

## Pull requests and commits

- Branches: `KPD-<ticket>-<short-slug>` (e.g. `KPD-3429-cicd`). Commits: `KPD-<ticket> <Imperative summary>`; upstream-merged commits use Conventional Commits, do not mix the two on one branch.
- `.changes/` and `.changie.yaml` are inherited from upstream; the fork does not currently maintain `CHANGELOG.md`. Do not add changie fragments unless a release is being prepared and the user asks for them.
- CI to pass before merge: the Cloud Build pull-request build (`cloudbuild.yaml`) and, where secrets are available, the GitHub Actions integration run.
- Releases are tag-triggered: bump `version` in `pyproject.toml`, merge, then push `v<major>.<minor>.<patch>` matching it.
- Do not force-push. Do not commit or push unless asked.

## Security

- Credentials live only in `.secrets/` (gitignored) or environment variables (`BQ_CREDS` holds service-account JSON). Never commit them, never paste them into logs, configs, or tests.
- Because the repo is public, a pull-request trigger runs untrusted code: PR triggers must use comment control and a log-only service account; only the tag trigger's service account may write to Artifact Registry.
- Never hardcode Knownwell project IDs, bucket names, registry URLs, or service-account emails anywhere in the repo, including README examples (use `<placeholder>` values).
