# Changelog

All notable changes to `lamotel` are documented here.

## 2026-09-25

### Added

- Added an offline composed tracing test covering environment/resource configuration, nested trace context, payload structure, headers, and successful and rejected exports through an ephemeral local collector.

### Changed

- Updated optional environment reads to use explicit `Env.getOr` defaults.
- Updated OTLP export to consume `HttpClient.post -> Result[HttpResponse]` and preserve transport failures as `OtelExportError` causes.
