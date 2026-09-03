"""Tests for the integration-test configuration helper."""

import pytest

from target_bigquery.tests.config import integration_target_config


def test_omits_credentials_when_bq_creds_unset(monkeypatch):
    """Without BQ_CREDS the config carries no credentials, so clients use ADC."""
    monkeypatch.setenv("BQ_PROJECT", "test-project")
    monkeypatch.setenv("BQ_DATASET", "test_dataset")
    monkeypatch.delenv("BQ_CREDS", raising=False)

    config = integration_target_config()

    assert config == {"project": "test-project", "dataset": "test_dataset"}
    assert "credentials_json" not in config


def test_includes_credentials_when_bq_creds_set(monkeypatch):
    """When BQ_CREDS is set it is passed through as credentials_json."""
    monkeypatch.setenv("BQ_PROJECT", "test-project")
    monkeypatch.setenv("BQ_DATASET", "test_dataset")
    monkeypatch.setenv("BQ_CREDS", '{"type": "service_account"}')

    config = integration_target_config()

    assert config["credentials_json"] == '{"type": "service_account"}'


def test_overrides_are_merged(monkeypatch):
    """Callers can add or replace keys such as bucket and method."""
    monkeypatch.setenv("BQ_PROJECT", "test-project")
    monkeypatch.setenv("BQ_DATASET", "test_dataset")
    monkeypatch.delenv("BQ_CREDS", raising=False)

    config = integration_target_config(bucket="test-bucket", method="batch_job")

    assert config["bucket"] == "test-bucket"
    assert config["method"] == "batch_job"
    assert config["project"] == "test-project"


def test_requires_project_and_dataset(monkeypatch):
    """A missing variable fails loudly rather than producing a half-built config."""
    monkeypatch.delenv("BQ_PROJECT", raising=False)
    monkeypatch.setenv("BQ_DATASET", "test_dataset")

    with pytest.raises(KeyError):
        integration_target_config()
