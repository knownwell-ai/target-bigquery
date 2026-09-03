"""Tests standard target features using the built-in SDK tests library."""

import pytest
from singer_sdk.testing import get_standard_target_tests

from target_bigquery.target import TargetBigQuery
from target_bigquery.tests.config import integration_target_config

pytestmark = pytest.mark.integration


# Run standard built-in target tests from the SDK:
def test_standard_target_tests():
    """Run standard target tests from the SDK."""
    tests = get_standard_target_tests(
        TargetBigQuery,
        config=integration_target_config(),
    )
    for test in tests:
        test()
