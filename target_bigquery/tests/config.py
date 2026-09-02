"""Target configuration shared by the integration tests.

The integration tests read the BigQuery project, dataset and GCS bucket from the
environment. Credentials are optional: when ``BQ_CREDS`` is set it is passed through
as ``credentials_json``, and otherwise no credentials are supplied at all so the
BigQuery and GCS clients fall back to Application Default Credentials. That is how
CI authenticates, as the build already runs as a service account.
"""

import os
from typing import Any, Dict


def integration_target_config(**overrides: Any) -> Dict[str, Any]:
    """Build a target config from the environment, plus any per-test overrides.

    Raises:
        KeyError: if ``BQ_PROJECT`` or ``BQ_DATASET`` is not set.
    """
    config: Dict[str, Any] = {
        "project": os.environ["BQ_PROJECT"],
        "dataset": os.environ["BQ_DATASET"],
    }
    credentials_json = os.environ.get("BQ_CREDS")
    if credentials_json:
        config["credentials_json"] = credentials_json
    config.update(overrides)
    return config
