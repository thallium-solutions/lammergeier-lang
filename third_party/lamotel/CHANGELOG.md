# Changelog

All notable changes to `lamotel` are documented here.

## 0.2.1 - 2026-10-15

### Fixed

- Convert span attribute maps with `Json.fromValue(...)` when building the OTLP
  payload; the implicit `json(dict[str, any])` conversion is rejected by the
  semantic check.
- The OTLP instrumentation scope and `User-Agent` report the package version
  (`0.2.1`) instead of the stale `0.1.0`.

## 2026-09-25

### Added

- Added an offline composed tracing test covering environment/resource configuration, nested trace context, payload structure, headers, and successful and rejected exports through an ephemeral local collector.

### Changed

- Updated optional environment reads to use explicit `Env.getOr` defaults.
- Updated OTLP export to consume `HttpClient.post -> Result[HttpResponse]` and preserve transport failures as `OtelExportError` causes.
