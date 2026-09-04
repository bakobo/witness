"""OpenTelemetry metrics for the control plane (@wea6qjmk).

ops.md §7: OpenTelemetry SDK inside code Bakobo writes, OTLP on the wire, a Collector on every
host, a hosted backend. So there is no scrape endpoint here — a Prometheus text surface would be
cheaper and would be against the standard.

Every instrument is **observable**: a callback that reads LMDB and the telemetry segment when the
exporter asks, rather than a counter this process increments and hopes stays in step with the
source. The values already exist; a shadow copy could only ever disagree with them.

Export is off unless an OTLP endpoint is configured, so the image runs perfectly well with no
collector anywhere. And a callback that raises yields nothing rather than propagating: a witness
that is down is precisely what these metrics exist to report, so its being down must not also
break the reporting.
"""

from __future__ import annotations

import os

from .errors import WitnessError

_ENDPOINT_VARS = ("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT", "OTEL_EXPORTER_OTLP_ENDPOINT")
_DEFAULT_INTERVAL_MS = 60_000


def configured_endpoint(environ=None):
    """The OTLP endpoint from the environment, or None. Standard variable names, so an operator
    configures this the way they configure everything else that speaks OTLP."""
    environ = os.environ if environ is None else environ
    for name in _ENDPOINT_VARS:
        value = environ.get(name)
        if value:
            return value
    return None


def _observation(observation_class, value, attributes=None):
    return observation_class(value, attributes or {})


def build_callbacks(reader, observation_class, counter=None):
    """The gauge callbacks, as ``(name, unit, description, callback)`` tuples.

    Separated from the SDK wiring so the interesting part — what is measured, and that a failing
    witness degrades to no data rather than to an exception — is testable without an exporter.
    """

    def guarded(produce):
        def callback(_options):
            try:
                return list(produce())
            except WitnessError:
                # The witness being unreadable is data, not an error to propagate. Yielding
                # nothing leaves a gap in the series, which is what a gap means.
                return []

        return callback

    def up():
        yield _observation(observation_class, 1 if reader.health()["status"] == "ok" else 0)

    def loop_lag():
        yield _observation(observation_class, reader.loop()["loop_lag"])

    def loop_ticks():
        yield _observation(observation_class, reader.loop()["ticks"])

    def doer_seconds():
        for doer in reader.loop()["doers"]:
            yield _observation(
                observation_class, doer["max_seconds"], {"doer": doer["name"]}
            )

    def escrow_depth():
        for store, depth in reader.escrow()["depths"].items():
            yield _observation(observation_class, depth, {"store": store})

    def database_used():
        yield _observation(observation_class, reader.database()["used_fraction"])

    def process_resident():
        yield _observation(observation_class, reader.process()["resident_bytes"])

    def process_cpu():
        yield _observation(observation_class, reader.process()["cpu_seconds"])

    def requests():
        for (route, status), count in (counter.counts if counter else {}).items():
            yield _observation(
                observation_class, count, {"route": route, "status": str(status)}
            )

    return [
        ("witness.up", "1", "Whether the witness is serving.", guarded(up)),
        ("witness.loop.lag", "s", "How far behind its tock the hio loop is running.",
         guarded(loop_lag)),
        ("witness.loop.ticks", "1", "Loop passes since the witness started.", guarded(loop_ticks)),
        ("witness.loop.doer.max_seconds", "s", "Slowest observed pass, per doer.",
         guarded(doer_seconds)),
        ("witness.escrow.depth", "1", "Entries parked in each escrow store.",
         guarded(escrow_depth)),
        ("witness.database.used_fraction", "1",
         "Database size against keripy's fixed map ceiling.", guarded(database_used)),
        ("witness.process.resident_bytes", "By", "Resident memory of the witness process.",
         guarded(process_resident)),
        ("witness.process.cpu_seconds", "s", "CPU consumed by the witness process.",
         guarded(process_cpu)),
        ("witness.controlplane.requests", "1",
         "Control-plane requests, by route template and status.", guarded(requests)),
    ]


def configure(reader, endpoint=None, interval_ms=_DEFAULT_INTERVAL_MS, environ=None,
              counter=None):
    """Start OTLP metric export for ``reader``, or return None when no endpoint is configured.

    Imported lazily so the SDK is not paid for by a deployment that has no collector, and so a
    control plane can start on a host where the packages are absent.
    """
    endpoint = endpoint or configured_endpoint(environ)
    if endpoint is None:
        return None

    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
    from opentelemetry.metrics import Observation
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.resources import Resource

    provider = MeterProvider(
        resource=Resource.create({"service.name": "witness-control-plane"}),
        metric_readers=[
            PeriodicExportingMetricReader(
                OTLPMetricExporter(endpoint=endpoint),
                export_interval_millis=interval_ms,
            )
        ],
    )
    meter = provider.get_meter("witness")
    for name, unit, description, callback in build_callbacks(reader, Observation, counter):
        meter.create_observable_gauge(
            name=name, unit=unit, description=description, callbacks=[callback]
        )
    return provider
