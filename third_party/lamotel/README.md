# lamotel

`lamotel` is an opinionated, lightweight OpenTelemetry helper for Lammergeier.
It focuses on a small Lam-native API and exports traces as OTLP/HTTP JSON.
It intentionally avoids the heavy Go OpenTelemetry SDK so applications can add
basic tracing without pulling a large dependency graph.

## Configuration

```bash
export OTEL_SERVICE_NAME="my-lam-app"
export OTEL_SERVICE_VERSION="1.2.3"
export OTEL_DEPLOYMENT_ENVIRONMENT="prod"
export OTEL_RESOURCE_ATTRIBUTES="team=payments,region=eu"
export OTEL_EXPORTER_OTLP_ENDPOINT="http://localhost:4318"
export OTEL_EXPORTER_OTLP_HEADERS="Authorization=Bearer token"
```

## Quick start

```lammergeier
from lamotel import OtelTracer, OtelSpanBatch
from lamerrors import Result

func main() {
    tracer: OtelTracer = OtelTracer.fromEnv()

    parent = tracer.startSpan("checkout")
    child = tracer.childSpan(parent, "stripe.create_payment_intent")
    child.setAttribute("order.id", "ord_123")
    child.end()
    parent.end()

    batch: OtelSpanBatch = tracer.batch()
    batch.add(parent)
    batch.add(child)

    sent: Result = batch.export(tracer)
    if not sent.ok() {
        print(str(sent.error))
    }
}
```

## Opinionated helpers

- `OtelConfig.fromEnv()` reads service name, service version, deployment environment, resource attributes, endpoint, and exporter headers.
- `cfg.resource()` merges `service.name`, `service.version`, `deployment.environment`, and `OTEL_RESOURCE_ATTRIBUTES` into the exported resource.
- `OtelSpan.setStatus(code, message="")` records span status.
- `OtelSpan.addEvent(name)` records a timestamped event.
- `OtelSpan.recordException(message)` sets error status, records `exception.message`, and adds an `exception` event.
- `OtelTracer.childSpan(parent, name, kind="internal")` creates a child span sharing the parent trace ID.
- `OtelTracer.batch()` creates an `OtelSpanBatch` for scoped export.

## API reference

### `OtelConfig`

- `OtelConfig.fromEnv()` reads `OTEL_SERVICE_NAME`, `OTEL_SERVICE_VERSION`, `OTEL_DEPLOYMENT_ENVIRONMENT`, `OTEL_RESOURCE_ATTRIBUTES`, `OTEL_EXPORTER_OTLP_ENDPOINT`, and `OTEL_EXPORTER_OTLP_HEADERS`.
- `cfg.tracesUrl()` returns the OTLP traces URL.
- `cfg.resource()` returns resource attributes used in exported payloads.
- `cfg.isReady()` returns whether an endpoint is configured.

### `OtelSpan`

- `setAttribute(key, value)` records a string attribute.
- `setStatus(code, message="")` records status.
- `addEvent(name)` records an event.
- `recordException(message)` records an exception-style error.
- `end()` records end time.
- `durationMs()` returns elapsed milliseconds.
- `toDict(serviceName)` returns a JSON-ready representation.

### `OtelTracer`

- `OtelTracer.fromEnv()` creates a tracer from env config.
- `startSpan(name, kind="internal")` creates a span.
- `childSpan(parent, name, kind="internal")` creates a trace-linked child span.
- `batch()` creates an `OtelSpanBatch`.
- `exportSpan(span)` POSTs a single span to `/v1/traces`.
- `exportSpans(spans)` POSTs a batch.

## Testing

```bash
/usr/bin/python3 third_party/lamotel/tests/run_lamotel_tests.py --verbose
```

The guarded live test compiles and skips by default. To send to a real local or
remote OTLP/HTTP collector:

```bash
export LAMOTEL_LIVE_TESTS=1
export OTEL_EXPORTER_OTLP_ENDPOINT="http://localhost:4318"
/usr/bin/python3 third_party/lamotel/tests/run_lamotel_tests.py --live --verbose
```
