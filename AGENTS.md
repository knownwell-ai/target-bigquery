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
poetry run pytest                          # 56 unit tests pass; 13 integration tests skip without BQ_PROJECT/BQ_DATASET/GCS_BUCKET
poetry run pytest -m "not integration"     # exactly what GitHub Actions runs
poetry run pytest target_bigquery/tests/test_utils.py -k transform_column_name
```

- Tests live in `target_bigquery/tests/`. `test_utils.py`, `test_config.py` and `test_sinks.py` are pure. `test_core.py` and `test_sync.py` are marked `integration` at module level and write to a real BigQuery dataset and GCS bucket; they read `BQ_PROJECT`, `BQ_DATASET`, `GCS_BUCKET`.
- `test_sinks.py` covers what a sink does to a record: the key-property check, Decimal serialization and stream closing (the three KPD-4933 defects). It builds sinks with `object.__new__` instead of mocking GCP, because every sink's `__init__` opens a live client and creates the target table while the record-handling methods touch neither. Prefer that over mocking a Google client when adding to it.
- **Credentials are optional.** `target_bigquery/tests/config.py` passes `BQ_CREDS` through as `credentials_json` only when it is set; otherwise no credentials are supplied and every client factory falls back to Application Default Credentials (`core.py:627`, `:637`, `:649` — only `project` is required by the config schema). That is how CI authenticates: the Cloud Build service account is the identity. Build test configs with `integration_target_config(**overrides)` rather than reading the environment inline.
- `target_bigquery/tests/conftest.py` skips `integration` tests when any of the three location variables is unset, so a credential-less run is green rather than a `KeyError`. Markers are strict (`addopts = "--strict-markers"` in `pyproject.toml`); register new markers there.
- Anything that needs network, GCP, or credentials must carry `pytestmark = pytest.mark.integration`. The marker decides **where** a test runs, and that is a security boundary: GitHub Actions runs `-m "not integration"` on pull requests and holds no credentials, while `cloudbuild-deploy.yaml` runs the whole suite on merge to main as a service account that can write to BigQuery and GCS. A test that needs credentials but lacks the marker would run on untrusted pull-request code.
- Where each suite runs: `.github/workflows/ci.yml` — unit tests only, Python 3.13, and the only check on a pull request. `cloudbuild-deploy.yaml` — full suite on merge to main, in three sequential `pytest` invocations (test_core, then test_utils + test_config + test_sinks, then test_sync), preserving the order the GitHub workflow used historically. They share one Cloud Build step because each step starts a fresh container and only `/workspace` persists, so the dev-group install would otherwise be lost.
- The GitHub matrix is **3.13 only**. Python 3.14 is blocked by the lock file: `pyarrow` is pinned at 21.0.0 and its first cp314 wheel is 22.0.0, so a 3.14 install tries to build Arrow from source. Nothing constrains `pyarrow` upward — it is just an old lock — but `poetry lock --regenerate` moves 48 packages including protobuf 6→7 (this repo does protobuf codegen) and singer-sdk 0.51→0.54.5, so treat the 3.14 matrix leg as its own dependency-bump ticket.

## Code style

- Formatter: black, line length 100, preview mode (`[tool.black]` in `pyproject.toml`). The locked black 21.12b0 crashes on import with current click, and the tree was formatted with it, so **do not run a newer black over a whole file**: `pipx run black --line-length 100 --preview` reformats unrelated lines throughout (all five sink modules fail its `--check` at HEAD). Format the lines you changed by hand to match, then confirm black wants no further change to them with `pipx run black --line-length 100 --preview --diff <file>` and check that no `+`/`-` hunk touches your lines. Reformatting the tree is its own ticket, alongside bumping the dev group.
- Imports: isort, profile black (`poetry run isort target_bigquery`).
- Lint / types: `poetry run flake8 target_bigquery`, `poetry run mypy target_bigquery` (mypy 0.910; expect noise).
- Poetry warns that the `[tool.poetry]` metadata tables are deprecated. Migrating to `[project]` is a known follow-up; do not do it as a side effect of another change, and re-lock if you do.

## Project layout

- `target_bigquery/target.py` — `TargetBigQuery` entry point and config schema.
- `target_bigquery/core.py` — sink base classes, schema translation, column-name transforms, credentials.
- `target_bigquery/storage_write.py`, `batch_job.py`, `gcs_stage.py`, `streaming_insert.py` — one sink per load method.
- `target_bigquery/proto_gen.py` — protobuf schema generation for the Storage Write API.
- `target_bigquery/tests/` — pytest suite (see Testing).
- `cloudbuild-deploy.yaml` (merge to main: full test suite), `cloudbuild-release.yaml` (version tags: build and publish) — Cloud Build configs (see below).
- `.changes/` + `.changie.yaml` — changelog fragments; `CHANGELOG.md` is generated by changie.
- `meltano.yml` — Meltano project for local end-to-end runs; `input/` — sample Singer stream.

## Conventions and gotchas

### Cloud Build (learned the hard way)

Both configs build the repo with Google Cloud buildpacks (`gcr.io/k8s-skaffold/pack` + `gcr.io/buildpacks/builder:google-24`, `GOOGLE_PYTHON_VERSION=3.13.*`) instead of installing Poetry by hand, then run a step **inside the built image**.

- The Python buildpack picks Poetry because `poetry.lock` exists and runs `poetry install --sync --only main --no-root`. The image has Poetry and the runtime deps but **not the dev group**; the test step adds it with `poetry install --with dev --no-root`. A `requirements.txt` at the repo root would take precedence and silently switch the build to pip. Do not add one.
- Steps on the buildpack image must use `entrypoint: launcher` with a leading `--` argument (`args: ["--", "bash", "-c", ...]`). Without `--` the CNB launcher rewrites every argument through `eval echo`; an inline script with quotes, `$(...)` or `#` comments becomes an empty command that **exits 0 without running anything**. A step that finishes in under a second is the tell.
- The image runs as uid 33 and Cloud Build sets `HOME=/builder/home` (root-owned). Poetry therefore needs `POETRY_CACHE_DIR=/tmp/...` (an unwritable cache surfaces as "All attempts to connect to pypi.org failed") and `VIRTUAL_ENV` exported to the buildpack venv, which is first on `PATH`: `export VIRTUAL_ENV="$$(dirname "$$(dirname "$$(command -v python)")")"`. `/workspace` is root-owned too, so pytest runs with `-p no:cacheprovider` and the deploy config has a root step that creates and `chmod 0777`s `dist/` before `poetry build`.
- Shell `$` must be written `$$` in Cloud Build YAML; a bare `$NAME` or `${NAME}` is a Cloud Build substitution. The intended substitutions are `$TAG_NAME`, `${_PYTHON_REPOSITORY_URL}` (release) and `${_BQ_PROJECT}` / `${_BQ_DATASET}` / `${_GCS_BUCKET}` (merge-to-main). The three CI ones are declared with empty defaults in a `substitutions:` block so a manual submit without them still works — the integration tests simply skip.
- The integration tests in Cloud Build authenticate as the build's service account via ADC, so that account is the identity that writes to BigQuery and GCS. As of 2026-09-03 this is **applied**: a **dedicated CI service account** rather than added grants on `kw_cloud_deploy_sa_roles` (that account also holds Cloud Deploy and Artifact Registry access, and the pull-request trigger runs untrusted code from a public repo), carrying five **project-level** bindings: `roles/bigquery.dataEditor`, `roles/bigquery.jobUser`, `roles/storage.objectUser`, `roles/storage.bucketViewer`, `roles/logging.logWriter`. Scoping is service-level by decision, not dataset- or bucket-scoped. Two consequences: project-level `dataEditor` carries `bigquery.datasets.create`, so the suite creates its dataset on the first green build rather than needing one pre-created; and project-level `objectUser` already covers the build's log-object writes, so `defaultLogsBucketBehavior: REGIONAL_USER_OWNED_BUCKET` costs no extra binding. The staging bucket must still pre-exist, in a location matching the target's `location` option (default `US`), because `create_bucket_if_not_exists` in `gcs_stage.py` is unreachable - `Client.get_bucket` raises `NotFound` instead of returning `None`. Nothing deletes staged blobs or test tables afterwards, and the CI dataset is unmanaged with no default table expiry.
- **There is no Cloud Build trigger on pull requests.** Every Cloud Build trigger fires on a trusted ref -- `deploy-lib-target-bigquery` on pushes to `main` (`cloudbuild-deploy.yaml`, the CI service account, the three location substitutions set) and `release-lib-target-bigquery` on `v<semver>` tags (`cloudbuild-release.yaml`, the Artifact Registry service account). Pull requests are checked only by GitHub Actions, which runs `-m "not integration"` and holds no credentials. This replaced a pull-request trigger that ran the write-capable account behind comment control, which a Codex review correctly flagged as P1: comment control gates *when* a build starts, not what identity it runs as, and the setting in use (`COMMENTS_ENABLED_FOR_EXTERNAL_CONTRIBUTORS_ONLY`) exempts collaborators entirely -- ~10 people with push access whose pull requests built automatically. The exposure was concrete: project-level `roles/storage.objectUser` carries `storage.objects.delete` over all 11 buckets in the project, including the Terraform state bucket. Do not reintroduce a pull-request trigger carrying either identity.
  - singer-sdk 0.50 runs `_singer_validate_message` **after** `preprocess_record` (`singer_sdk/target_base.py`), where the fixed-schema strategy has already replaced the record with `{data, _sdc_*}` and the key properties are no longer top-level keys. `BaseBigQuerySink` therefore validates them *in* `preprocess_record`, while the record still has the tap's shape, and only delegates to the SDK's check for the denormalized strategy — each strategy validates exactly once. Validating `record["data"]` in the override instead is a trap: `streaming_insert` and `storage_write` have already serialized it to a JSON string by then, so a membership test matches substrings (`"id" in '{"not_id": 1}'` is true) and the check silently passes anything.
  - singer-sdk deserializes every JSON `number` as `decimal.Decimal` (`singer_sdk/singerlib/json.py`), which orjson cannot encode. `default_json_serializer` in `core.py` is the single handler for all five `orjson.dumps` call sites. KPD-2877 had added the same closure by hand to two of them and missed three, so keep it shared. Consequence worth knowing: in fixed-schema mode `data` is a BigQuery `JSON` column and a Decimal is written as a JSON **string**, so `STRING(data.x)` reads it and `FLOAT64(data.x)` does not. Precision is exact. That is KPD-2877's choice, kept deliberately; changing it to unquoted numbers via `orjson.Fragment` would be a behaviour change for existing tables.
  - `AppendRowsStream.close` calls itself idempotent but raises `StreamClosedError` unless the stream is active, which covers both an already-closed stream and one never sent to. `drain_all` poison-pills and **joins** the workers — each closing every stream it cached — before any `sink.clean_up`, so `commit_streams` always meets a closed stream. Unguarded, it raised before `finalize_write_stream`, so PENDING streams were never batch-committed and every row was lost: `storage_write_api_batch` held 0 rows where the others held 5. `close_write_stream` tolerates exactly that one exception. This only bites `method: storage_write_api` + `options.storage_write_batch_mode: true`; the default stream is filtered out of the commit. It stayed hidden because the Decimal failure aborted the run earlier — expect fixing one masked defect to reveal the next in these paths.
  - The staging-bucket write path is now proven: `storage.objects.create`/`get` work under the CI service account (blobs land under `gs://<bucket>/target_bigquery/`), so the earlier worry about a 403 there is settled. Nothing deletes staged blobs or test tables afterwards.
- `cloudbuild-release.yaml` refuses to publish when `TAG_NAME` is set and does not equal `v<pyproject version>`; manual `gcloud builds submit` runs have an empty `TAG_NAME` and skip the check. Uploads use `artifacts.pythonPackages` with the repository URL from the `_PYTHON_REPOSITORY_URL` substitution (substitutions work inside `artifacts`). Artifact Registry rejects re-uploading an existing version, so every release needs a version bump.
- To test a config by hand: `gcloud builds submit --config cloudbuild-deploy.yaml .` and `gcloud builds submit --config cloudbuild-release.yaml --substitutions=_PYTHON_REPOSITORY_URL=<url> .`. Note `gcloud builds triggers run` does **not** work on these triggers (`RunTrigger is not supported for GCB v2 repo PullRequest Triggers`), and there is no `gcloud builds retry`, so a manual submit is the only way to re-run a build locally. Testing the deploy config publishes for real; use a scratch version such as `0.7.2.dev0` (`poetry version 0.7.2.dev0`, submit, `poetry version 0.7.2`) and delete it from the registry afterwards.
- `pack` can be run locally with rootless podman for fast iteration: `DOCKER_HOST=unix:///run/user/$UID/podman/podman.sock pack build <name> --builder gcr.io/buildpacks/builder:google-24 --docker-host inherit --env 'GOOGLE_PYTHON_VERSION=3.13.*'`, then `podman run --rm -v "$PWD":/workspace:ro -w /workspace -e HOME=/builder/home --entrypoint launcher <name> -- bash -c '...'` reproduces the Cloud Build environment closely. Always test the exact inline invocation, not a script file, or the `eval echo` problem hides.

### Packaging

- Keep the distribution name `kw-target-bigquery`; never rename the `target_bigquery` package or the `target-bigquery` script.
- Poetry's lock content-hash ignores the project name, so a rename does not require re-locking; changing dependencies does (`poetry lock`, then `poetry check --lock`).
- Renaming a Poetry project changes its venv name; run `poetry install` again afterwards.
- Consumers may still have `z3-target-bigquery` installed; both distributions own the same files, so the old one must be uninstalled first. Say so in changelog entries that affect installation.

## Pull requests and commits

- Branches: `KPD-<ticket>-<short-slug>` (e.g. `KPD-3429-cicd`). Commits: `KPD-<ticket> <Imperative summary>`; upstream-merged commits use Conventional Commits, do not mix the two on one branch.
- `.changes/` and `.changie.yaml` are inherited from upstream; the fork does not currently maintain `CHANGELOG.md`. Do not add changie fragments unless a release is being prepared and the user asks for them.
- CI to pass before merge: the GitHub Actions unit-test run. The integration tests run **after** merge, from `cloudbuild-deploy.yaml`, so a red main is possible and blocks the next release; check it after merging.
- Releases are tag-triggered: bump `version` in `pyproject.toml`, merge, then push `v<major>.<minor>.<patch>` matching it. `cloudbuild-release.yaml` refuses to publish when `TAG_NAME` disagrees with the `pyproject.toml` version.
- Do not force-push. Do not commit or push unless asked.

## Security

- Credentials live only in `.secrets/` (gitignored) or environment variables (`BQ_CREDS` holds service-account JSON). Never commit them, never paste them into logs, configs, or tests.
- Because the repo is public, a pull-request build would run untrusted code, so **no Cloud Build trigger may fire on a pull-request event**. Write-capable builds run only on trusted refs: pushes to `main` and `v<semver>` tags. Pull requests are checked by GitHub Actions with no credentials. If a pull-request trigger is ever genuinely needed, it must carry a log-only service account (`roles/logging.logWriter`, plus object write scoped to the logs bucket if `REGIONAL_USER_OWNED_BUCKET` is kept) and comment control -- comment control alone is not sufficient, since it exempts collaborators and gates initiation rather than privilege. Only the release trigger's service account may write to Artifact Registry.
- Never hardcode Knownwell project IDs, bucket names, registry URLs, or service-account emails anywhere in the repo, including README examples (use `<placeholder>` values).
