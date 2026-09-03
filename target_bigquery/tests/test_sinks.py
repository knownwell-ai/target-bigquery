"""Tests for what the sinks do to a record on its way to BigQuery.

These are unit tests: they run in GitHub Actions with no credentials. Every sink's
``__init__`` opens a BigQuery or GCS client and creates the target table, none of which
the record-handling methods touch, so the sinks here are built without running it. That
keeps the tests free of both credentials and mocks -- the methods under test run for
real. See ``_sink``.

Regression cover for the three KPD-4933 defects, all in write paths that only the
``integration`` suite reached:

* singer-sdk 0.50 validates key properties *after* ``preprocess_record``
  (``singer_sdk/target_base.py``), by which point a fixed-schema sink has moved the
  tap's fields into a single ``data`` column.
* singer-sdk deserializes every JSON ``number`` as ``decimal.Decimal``
  (``singer_sdk/singerlib/json.py``), which orjson cannot encode without a
  ``default=`` handler.
* ``AppendRowsStream.close`` raises on a stream that is not active, and at end of pipe
  the storage-write sink always meets one that the worker has already closed.
"""

import decimal
import gzip
from typing import Any, Dict

import orjson
import pytest
from google.cloud.bigquery_storage_v1 import types, writer
from google.cloud.bigquery_storage_v1.exceptions import StreamClosedError
from singer_sdk.exceptions import MissingKeyPropertiesError

from target_bigquery.batch_job import (
    BigQueryBatchJobDenormalizedSink,
    BigQueryBatchJobSink,
)
from target_bigquery.core import Compressor, default_json_serializer
from target_bigquery.gcs_stage import (
    BigQueryGcsStagingDenormalizedSink,
    BigQueryGcsStagingSink,
)
from target_bigquery.storage_write import (
    BigQueryStorageWriteDenormalizedSink,
    BigQueryStorageWriteSink,
    close_write_stream,
)
from target_bigquery.streaming_insert import (
    BigQueryStreamingInsertDenormalizedSink,
    BigQueryStreamingInsertSink,
)

# The four load methods in their default, fixed-schema (``denormalized: false``) form.
FIXED_SINKS = {
    "batch_job": BigQueryBatchJobSink,
    "gcs_stage": BigQueryGcsStagingSink,
    "streaming_insert": BigQueryStreamingInsertSink,
    "storage_write_api": BigQueryStorageWriteSink,
}
DENORMALIZED_SINKS = {
    "batch_job": BigQueryBatchJobDenormalizedSink,
    "gcs_stage": BigQueryGcsStagingDenormalizedSink,
    "streaming_insert": BigQueryStreamingInsertDenormalizedSink,
    "storage_write_api": BigQueryStorageWriteDenormalizedSink,
}
# Two sinks serialize the ``data`` column in ``preprocess_record``; the other two buffer
# the whole record in ``process_record`` instead. Decimals have to survive both.
SERIALIZE_IN_PREPROCESS = {
    "streaming_insert": BigQueryStreamingInsertSink,
    "storage_write_api": BigQueryStorageWriteSink,
}
SERIALIZE_IN_PROCESS_RECORD = {
    "batch_job": BigQueryBatchJobSink,
    "gcs_stage": BigQueryGcsStagingSink,
}


def _sink(sink_cls, key_properties=("id",)):
    """Build a sink without running its constructor.

    ``BaseBigQuerySink.__init__`` builds a live client and creates the target table.
    The record-handling methods only read ``_key_properties`` (and, for the buffered
    sinks, ``buffer``), so bypassing the constructor exercises them honestly rather
    than mocking out GCP.
    """
    sink = object.__new__(sink_cls)
    sink._key_properties = list(key_properties)
    return sink


def _payload(record: Dict[str, Any]) -> Dict[str, Any]:
    """The tap's fields as the sink left them in the ``data`` column.

    ``streaming_insert`` and ``storage_write_api`` encode ``data`` to a JSON string in
    ``preprocess_record``; the other two leave it as a dict.
    """
    data = record["data"]
    return orjson.loads(data) if isinstance(data, (str, bytes)) else data


def _buffered_json(sink) -> Dict[str, Any]:
    """Read back what a buffered sink wrote, as parsed JSON."""
    sink.buffer.close()
    return orjson.loads(gzip.decompress(sink.buffer.getvalue()))


class TestKeyPropertyValidation:
    """KPD-4933 bug 1: the SDK validates key properties after ``preprocess_record``."""

    @pytest.mark.parametrize("sink_cls", FIXED_SINKS.values(), ids=FIXED_SINKS)
    def test_fixed_sink_record_passes_the_sdk_validation_step(self, sink_cls):
        """A preprocessed record still satisfies the check the SDK runs after it."""
        sink = _sink(sink_cls)

        record = sink.preprocess_record({"id": 7, "load_id": "abc"}, {})

        assert _payload(record)["id"] == 7
        sink._singer_validate_message(record)

    @pytest.mark.parametrize("sink_cls", FIXED_SINKS.values(), ids=FIXED_SINKS)
    def test_fixed_sink_rejects_a_record_missing_its_key_property(self, sink_cls):
        """The check still has teeth: a record with no ``id`` must be refused."""
        sink = _sink(sink_cls)

        with pytest.raises(MissingKeyPropertiesError):
            sink.preprocess_record({"load_id": "abc"}, {})

    @pytest.mark.parametrize("sink_cls", DENORMALIZED_SINKS.values(), ids=DENORMALIZED_SINKS)
    def test_denormalized_sink_validates_top_level_key_properties(self, sink_cls):
        """A denormalized sink keeps the tap's fields at the top level, so the SDK's
        own post-``preprocess_record`` check applies to it unchanged."""
        sink = _sink(sink_cls)

        sink._singer_validate_message({"id": 7, "load_id": "abc"})

        with pytest.raises(MissingKeyPropertiesError):
            sink._singer_validate_message({"load_id": "abc"})


class TestDecimalSerialization:
    """KPD-4933 bug 2: every JSON ``number`` reaches a sink as a ``decimal.Decimal``."""

    def test_default_json_serializer_renders_decimal_without_losing_precision(self):
        value = decimal.Decimal("0.7920345506520963")

        assert default_json_serializer(value) == "0.7920345506520963"

    def test_default_json_serializer_rejects_types_it_does_not_handle(self):
        with pytest.raises(TypeError, match="not JSON serializable"):
            default_json_serializer(object())

    @pytest.mark.parametrize(
        "sink_cls", SERIALIZE_IN_PREPROCESS.values(), ids=SERIALIZE_IN_PREPROCESS
    )
    def test_preprocess_record_serializes_decimals(self, sink_cls):
        sink = _sink(sink_cls)

        record = sink.preprocess_record({"id": 7, "amount": decimal.Decimal("1.25")}, {})

        assert _payload(record)["amount"] == "1.25"

    @pytest.mark.parametrize(
        "sink_cls",
        SERIALIZE_IN_PROCESS_RECORD.values(),
        ids=SERIALIZE_IN_PROCESS_RECORD,
    )
    def test_process_record_serializes_decimals(self, sink_cls):
        sink = _sink(sink_cls)
        sink.buffer = Compressor()

        sink.process_record(
            sink.preprocess_record({"id": 7, "amount": decimal.Decimal("1.25")}, {}), {}
        )

        assert _buffered_json(sink)["data"]["amount"] == "1.25"


class TestStreamClosing:
    """KPD-4933 bug 3: closing an append-rows stream that is not open.

    Only reachable with ``method: storage_write_api`` and
    ``options.storage_write_batch_mode: true``, which uses application-created PENDING
    streams; the default stream is filtered out of the sink's commit entirely.
    """

    def test_closing_a_stream_that_was_never_opened_is_not_an_error(self):
        """`drain_all` joins the workers, each of which closes every stream it cached,
        before calling `clean_up` -- so the stream the sink is about to finalize is
        already closed. An unguarded `close` aborted the commit, losing every row in
        the PENDING stream."""
        stream = writer.AppendRowsStream(None, types.AppendRowsRequest())
        assert not stream.is_active

        close_write_stream(stream)

    def test_the_library_really_does_raise_on_an_inactive_stream(self):
        """`AppendRowsStream.close` documents itself as idempotent but is not. If a
        future version becomes idempotent, this fails and the wrapper can go."""
        stream = writer.AppendRowsStream(None, types.AppendRowsRequest())

        with pytest.raises(StreamClosedError):
            stream.close()

    def test_other_close_failures_still_propagate(self):
        """The tolerance is specific to "not open"; nothing else is swallowed."""

        class StreamThatFailsToClose:
            def close(self):
                raise RuntimeError("connection reset")

        with pytest.raises(RuntimeError):
            close_write_stream(StreamThatFailsToClose())
