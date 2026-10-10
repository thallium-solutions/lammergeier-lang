# Changelog

All notable changes to `lamstripe` are documented here.

## 2026-09-25

### Added

- Added an offline composed checkout/refund test covering environment configuration, form edge cases, request headers, typed responses, API/transport errors, and a realistic multi-step flow through an ephemeral local Stripe mock.

### Fixed

- Reject non-letter three-character values during ISO currency validation instead of accepting punctuation and digits.

### Changed

- Updated optional environment reads to use explicit `Env.getOr` defaults.
- Updated HTTP calls to consume `Result[HttpResponse]` and preserve transport failures.
- Updated JSON decoding and collection conversion for the stdlib's unsuffixed Result contracts.
