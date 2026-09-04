"""OTLP metric export for the control plane (@wea6qjmk).

The interesting behaviour is not the SDK wiring, which is a handful of constructor calls, but
what the callbacks do when the witness is unreadable — which is the moment the metrics matter
most and the moment the reader raises. A callback that propagated would take the exporter down
exactly when it was needed.
"""

import pytest

from witness import metrics
from witness.errors import DbUnavailable


class Observation:
    """Stands in for opentelemetry.metrics.Observation, whose shape is (value, attributes)."""

    def __init__(self, value, attributes=None):
        self.value = value
        self.attributes = attributes or {}

    def __repr__(self):  # pragma: no cover - debugging aid only
        return f"Observation({self.value!r}, {self.attributes!r})"


class StubReader:
    def __init__(self, failing=()):
        self._failing = set(failing)

    def _guard(self, name):
        if name in self._failing:
            raise DbUnavailable("The witness is not running.")

    def health(self):
        self._guard("health")
        return {"status": "ok", "ticks": 5}

    def loop(self):
        self._guard("loop")
        return {
            "ticks": 5,
            "loop_lag": 0.5,
            "doers": [
                {"name": "Alpha", "last_seconds": 0.1, "max_seconds": 0.2},
                {"name": "Beta", "last_seconds": 0.3, "max_seconds": 0.4},
            ],
        }

    def escrow(self):
        self._guard("escrow")
        return {"depths": {"query_not_found": 7, "out_of_order": 0}, "total": 7}

    def database(self):
        self._guard("database")
        return {"used_fraction": 0.25}

    def process(self):
        self._guard("process")
        return {"resident_bytes": 1024, "cpu_seconds": 3.5}


def collect(reader, name):
    for gauge_name, _unit, _description, callback in metrics.build_callbacks(reader, Observation):
        if gauge_name == name:
            return callback(None)
    raise AssertionError(f"no gauge named {name}")


def test_every_gauge_is_named_and_documented():
    """A metric nobody can interpret is noise, and ops.md §7 says to alert only on what a person
    would act on — which requires knowing what each series means."""
    for name, unit, description, _callback in metrics.build_callbacks(StubReader(), Observation):
        assert name.startswith("witness.")
        assert unit
        assert description.endswith(".")


def test_the_signals_infra_asked_for_are_all_present():
    names = {name for name, _u, _d, _c in metrics.build_callbacks(StubReader(), Observation)}

    assert {"witness.escrow.depth", "witness.loop.lag", "witness.database.used_fraction"} <= names


def test_escrow_depth_is_reported_per_store_as_one_series_with_attributes():
    """One series keyed by store rather than ten metric names, so a new escrow in keripy shows up
    without a code change here."""
    observed = collect(StubReader(), "witness.escrow.depth")

    assert {(o.attributes["store"], o.value) for o in observed} == {
        ("query_not_found", 7),
        ("out_of_order", 0),
    }


def test_per_doer_timings_are_reported_with_the_doer_as_an_attribute():
    observed = collect(StubReader(), "witness.loop.doer.max_seconds")

    assert {(o.attributes["doer"], o.value) for o in observed} == {("Alpha", 0.2), ("Beta", 0.4)}


def test_up_is_one_when_the_witness_is_serving():
    assert [o.value for o in collect(StubReader(), "witness.up")] == [1]


def test_a_degraded_witness_reports_down_rather_than_absent():
    """A wedged witness answers health, so `up` can honestly say 0 — which is different from the
    gap a completely unreachable witness leaves, and an alert should be able to tell them apart."""

    class Degraded(StubReader):
        def health(self):
            return {"status": "degraded", "reason": "stuck in Alpha", "ticks": 5}

    assert [o.value for o in collect(Degraded(), "witness.up")] == [0]


@pytest.mark.parametrize(
    ("failing", "gauge"),
    [
        ("health", "witness.up"),
        ("loop", "witness.loop.lag"),
        ("escrow", "witness.escrow.depth"),
        ("database", "witness.database.used_fraction"),
        ("process", "witness.process.resident_bytes"),
    ],
)
def test_an_unreadable_witness_yields_a_gap_not_an_exception(failing, gauge):
    """The moment these metrics matter most is the moment the reader raises. A callback that
    propagated would take the exporter down exactly when it was needed."""
    assert collect(StubReader(failing=[failing]), gauge) == []


def test_export_is_off_when_no_endpoint_is_configured():
    """The image must run with no collector anywhere, so absence of configuration is not an
    error — it is the common case for a developer and for a first deployment."""
    assert metrics.configure(StubReader(), environ={}) is None


@pytest.mark.parametrize(
    "variable", ["OTEL_EXPORTER_OTLP_METRICS_ENDPOINT", "OTEL_EXPORTER_OTLP_ENDPOINT"]
)
def test_the_endpoint_is_read_from_the_standard_variables(variable):
    """Standard names, so an operator configures this the way they configure anything OTLP."""
    assert metrics.configured_endpoint({variable: "http://collector:4318"}) == (
        "http://collector:4318"
    )


def test_the_metrics_specific_variable_wins_over_the_general_one():
    endpoint = metrics.configured_endpoint(
        {
            "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT": "http://metrics:4318",
            "OTEL_EXPORTER_OTLP_ENDPOINT": "http://general:4318",
        }
    )

    assert endpoint == "http://metrics:4318"


def test_an_empty_endpoint_is_treated_as_unset():
    assert metrics.configured_endpoint({"OTEL_EXPORTER_OTLP_ENDPOINT": ""}) is None


def test_configure_builds_a_provider_carrying_every_gauge():
    provider = metrics.configure(StubReader(), endpoint="http://localhost:4318", interval_ms=60000)

    try:
        assert provider is not None
    finally:
        provider.shutdown()
