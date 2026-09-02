"""Shared pytest configuration for the target-bigquery test suite."""

import os

import pytest

# Environment variables read by the tests marked ``integration`` (test_core.py and
# test_sync.py). They point at a real BigQuery project and GCS bucket.
INTEGRATION_ENV_VARS = ("BQ_CREDS", "BQ_PROJECT", "BQ_DATASET", "GCS_BUCKET")


def pytest_collection_modifyitems(config, items):
    """Skip integration tests when the BigQuery credentials are not configured.

    Without this, a plain ``pytest`` run on a machine (or CI job) without credentials
    fails with ``KeyError`` instead of reporting which tests could not run.
    """
    missing = [name for name in INTEGRATION_ENV_VARS if not os.environ.get(name)]
    if not missing:
        return
    skip = pytest.mark.skip(reason=f"integration test; set {', '.join(missing)} to run it")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)
