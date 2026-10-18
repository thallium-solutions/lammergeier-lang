# Lammergeier Roadmap

This file collects deferred work that is still referenced across the docs.
It is not a release promise; it is a practical backlog of features whose
syntax, command shape, or operational behavior is already hinted at in the
documentation.

## Package and Library Workflows

### Workspaces

`[workspace]` is reserved today and rejected by the manifest parser. The future
feature would support multi-package repositories without forcing each package
to duplicate dependency and tooling configuration.

Possible manifest shape:

```toml
[workspace]
members = ["apps/api", "apps/worker", "libs/shared"]

[workspace.dependencies]
lamhttp = "^1.4"
lamjson = "^0.8"
```

Implementation notes:

- Define package-root discovery when multiple `lamlib.toml` files are present.
- Decide whether each member keeps its own lockfile or the workspace has one
  shared lockfile.
- Teach `lamc install`, `tidy`, `verify`, `list`, `tree`, and `why` to operate
  on either one package or the workspace.
- Ensure member-local dependencies can override workspace-level defaults
  without surprising conflict resolution.

## Release Hygiene

### `scripts/release_check.sh`

The release checklist is currently manual. A script should run the documented
non-publishing checks in the right order.

Expected command:

```bash
sh scripts/release_check.sh
```

Implementation notes:

- Run `git status --short`, `git diff --check`, tests, benchmarks, installer
  smoke tests, and extension build checks.
- Print each command before running it.
- Exit on the first failing command unless a `--keep-going` flag is provided.

### Changelog validator

Before tagging a release, tooling should verify that `CHANGELOG.md` contains an
entry for the intended version or release date.

Expected command:

```bash
python3 scripts/check_changelog.py v0.5.0
```

Implementation notes:

- Accept either version headings or date headings while the project is not fully
  version-tagged.
- Fail with a clear message when no matching entry is found.

### Tag guard

Release tooling should verify that the intended Git tag does not already exist
locally or remotely.

Expected command:

```bash
python3 scripts/check_tag.py v0.5.0
```

Implementation notes:

- Check `git rev-parse --verify refs/tags/<tag>` locally.
- Check `git ls-remote --tags origin <tag>` remotely.
- Print the existing commit when a collision is found.

### Fresh checkout smoke test

Post-release verification should clone the tagged repository into a temporary
directory, install from that checkout, and compile a hello-world program.

Implementation notes:

- Use a temporary install prefix so the user's active `lamc` is not replaced.
- Run `./install.sh --prefix "$tmpdir/prefix" --no-editor`.
- Run the installed `lamc` against `tests/rosetta_tests/hello_world.lam`.
- Clean up the temporary clone and install prefix on success.

## Possible new libraries

Two sources of new official packages: the packages still vendored in
`third_party/`, and generic code that grew inside the managed-file-transfer
(MFT) application. Every new package follows the existing conventions: scoped
`@lam/<name>` import, repository `thallium-solutions/lam-<name>` (private
Bitbucket source plus GitHub mirror through `mirror-to-github.py`), plain
version tags (`1.0.0`, no `v`), `compatibility.lamc = "^1.18"`, pinned
`[go-deps]`, its own `CHANGELOG.md`/`LICENSE`, and tests that run through the
installed-package boundary (`lamc` from `PATH`, package staged into a
temporary `extlibs/@lam/<name>`, as `lam-jwt/tests/run_lamjwt_tests.py` does).
All modules compile into one Go package, so raw Go helpers keep a package
prefix (`stripe…`, `otel…`, `netguard…`).

### Moving `third_party/` packages out of the compiler repo

Only two packages remain in `third_party/`; `lams3` already became `@lam/s3`
(the remaining `lams3` mentions are stale).

| Today | Proposed | Contents | Dependencies | Tests |
|---|---|---|---|---|
| `third_party/lamstripe` 0.2.0 (MIT, 350-line `__init__.lam`) | `@lam/stripe` / `lam-stripe` | `StripeConfig` (`fromEnv`, `isReady`), `StripeCustomerOptions`, `StripePaymentIntentOptions`, `StripeCheckoutSessionOptions` (`setMetadata`, `fields`), `StripeResponse` (`ok`, `json`, `field`, `id`, `url`, `clientSecret`), `Stripe` statics (`encodeForm`, `errorMessage`, `normalizeCurrency`, `addMetadata`, `validateAmount`, `validateCurrency`), `StripeClient` (`fromEnv`, `setIdempotencyKey`, `rawGet`, `rawPostForm`, `createCustomer[WithOptions]`, `createPaymentIntent[WithOptions]`, `retrievePaymentIntent`, `createCheckoutSession[WithOptions]`) | `lamenv`, `lamerrors`, `lamhttp`, `lamjson`; Go stdlib `net/url` only, no `[go-deps]` | 5 `offline_*.lam` (form encoding, env config, mock API, helpers, composed checkout/refund against an ephemeral local mock) and guarded `live_balance.lam` (`--live`, `LAMSTRIPE_LIVE_TESTS`) |
| `third_party/lamotel` 0.2.1 (MIT, 293 lines) | `@lam/otel` / `lam-otel` | `OtelConfig` (`fromEnv`, `isReady`, `tracesUrl`, `resource`), `OtelSpan` (`setParent`, `setAttribute`, `setStatus`, `addEvent`, `recordException`, `end`, `durationMs`, `toDict`), `OtelSpanBatch` (`add`, `size`, `export`), `Otel` statics (`idsTrace`, `idsSpan`, `parsePairs`, `parseHeaders`, `tracesPayload`, `tracesPayloadWithResource`), `OtelTracer` (`fromEnv`, `startSpan`, `childSpan`, `batch`, `exportSpan`, `exportSpans`) | `lamenv`, `lamjson`, `lamerrors`, `lamhttp`, `lamtime`, `lamuuid`; no Go modules | 4 `offline_*.lam` (config/payload, mock export, tracing, composed) and guarded `live_export.lam` (`LAMOTEL_LIVE_TESTS`) |

Per-package work:

- Create the repository from the package tree (without the untracked
  `tests/__pycache__/`), add `LICENSE`, `.gitignore`, `.gitattributes` and
  `mirror-to-github-repo.sh`, and add the name to `REPOSITORIES` in
  `mirror-to-github.py` (lines 49-61). Neither package ships a `LICENSE` file
  today; the manifests say MIT while the other `@lam/...` packages are
  Apache-2.0. Decide before the first tag.
- `lamlib.toml`: `name = "@lam/stripe"` / `"@lam/otel"`, `repository`,
  `homepage`, `[scripts] test = "python3 tests/run_lam<name>_tests.py --verbose"`.
- Rewrite `tests/run_lamstripe_tests.py` and `tests/run_lamotel_tests.py`:
  they currently call `PROJECT_ROOT/compiler/lammergeier.py` with
  `--extlibs third_party`, so they only work inside a compiler checkout. Port
  the `lam-jwt` runner (version check, staged `extlibs/@lam/<name>`) and change
  every test import to `from @lam/stripe import ...` / `from @lam/otel import ...`.
- Release as `1.0.0` (the import path change is breaking anyway) and use the
  `@lam/s3` shape: return payload-typed `Result[StripeResponse]` /
  `Result[...]` from client and export methods instead of untyped `Result`,
  which also removes the `stripeResultJson` / `stripeResultDict` casts.
- Fix while moving: `StripeClient` sends `User-Agent: lamstripe/0.1.0`
  (stale), and `docs/third_party_libraries.md` lists `lamotel` as 0.2.0.
- `@lam/stripe` should absorb the Stripe code MFT wrote around it: webhook
  signature verification (`StripeWebhook.verify(payload, header, secret,
  toleranceSeconds, nowUnix)` and a `testHeader` helper, from MFT
  `security.lam:67-124`) and typed calls for what `billing.lam` does through
  `rawPostForm` (billing meter events, billing portal sessions, subscription
  updates). Keep the package dependency-free.

References to update in this repository:

| Location | Action |
|---|---|
| `docs/third_party_libraries.md:70-71`, `:287` and `website/content/third_party_libraries.md` (same lines) | Replace the `third_party/...README.md` rows with `@lam/stripe` / `@lam/otel` GitHub rows; refresh the stale versions of the other rows in that table (all still say 2.0.0). |
| `docs/libraries.md` and `website/content/libraries.md` | No entry today: add both to the release table, the catalog table, an install/usage section, and the legacy-import table (`from lamstripe import ...` -> `from @lam/stripe import ...`). |
| `website/build.sh:12`, `:128`, `:141` | Drop the `../third_party/...` link rewrites once no doc links there. |
| `AGENTS.md:26`, `.windsurf/rules/lammergeier.md:28`, `.devin/rules/lammergeier.md:28` | "`third_party/` contains `lams3`, `lamstripe`, `lamotel`" -> official libraries live in sibling `lam-*` repositories. |
| `CHANGELOG.md` | New entry for the removal; keep the historical entries. |
| `tests/tests/test_github_lams3_install.py` (run by `scripts/test.sh:99`) | Still targets `thallium-solutions/lams3.git` and `from lams3 import S3`; retarget to `lam-s3` / `@lam/s3` or generalize into a guarded smoke over all official repositories, including the two new ones. |
| `third_party/` | Delete as the last step. |

No suite in `scripts/test.sh` runs `third_party/*/tests/run_*_tests.py` today,
so these packages are currently untested in the compiler pipeline; after the
move they are tested in their own repositories. These matches are unrelated
and need no change: `tests/tests/test_go_pins.py:230` (Go third-party imports
of the stdlib), `tests/tests/test_extlibs_resolution.py:224` (temporary
directory name), `tests/tests/test_manifest.py:240`, and all links to
`third_party_libraries.md` (the authoring spec, its `third_party` website
route and sitemap entry keep their names).

Consumers outside this repository: MFT is the only known user of
`lamstripe` and nothing uses `lamotel`. MFT changes: `lamlib.toml:16-17`
(path dependency -> `"@lam/stripe" = { git =
"https://github.com/thallium-solutions/lam-stripe.git", ref = "1.0.0" }`),
the path pin in `lamlib.lock.toml:58-63`, committed `extlibs/lamstripe/` ->
`extlibs/@lam/stripe/` (`lamc install --force`), the import in
`lib/mft/billing.lam:6` (it only uses `StripeClient(StripeConfig(...))`,
`setIdempotencyKey`, `rawGet`, `rawPostForm` and `StripeResponse.json()`, so
`Billing._json` at `billing.lam:45-51` simplifies with typed Results),
`Signatures.verifyStripe` / `stripeHeader` in `security.lam` (replaced by
`StripeWebhook`), and the notes in `README.md:81-83` and `AGENTS.md:8,22`.

Migration order:

1. `@lam/otel` 1.0.0 first: no consumers, so it rehearses the repository,
   runner, docs and mirror steps with no risk.
2. `@lam/stripe` 1.0.0 with typed Results and webhook verification.
3. Switch MFT to the git dependency, remove its Stripe signature code, run
   its unit tests and `tests/integration/it_03_billing.lam`.
4. Delete `third_party/` and update docs, website, rules and changelog in
   one commit; run `tests/tests/test_website_consistency.py`.

Risks and breaking changes:

- Import paths change (`lamstripe` -> `@lam/stripe`, `lamotel` -> `@lam/otel`),
  and so do return types if typed Results land in 1.0.0.
- Path dependencies on `.../lammergeier-lang/third_party/...` break when the
  directory goes. MFT Docker builds keep working from the committed `extlibs/`,
  but `lamc install` fails, so delete only after MFT has switched.
- License choice (MIT as declared, or Apache-2.0 like the other packages).
- Keep the environment variable names (`STRIPE_*`, the standard `OTEL_*`
  names); the live-test switches can keep their names or get aliases.

### Libraries that could be extracted from the managed-file-transfer app

`lib/mft/*.lam` is about 14k lines. The best sign that code is generic is that
the app already has several copies of it: three SSRF-safe dialers
(`sso.lam:29` `mftOidcClient`, `worker.lam:35` `mftWebhookClient`,
`importer.lam:38` `mftImportAddress`) and three glob matchers
(`automation.lam:21-65` `pathMatches`/`globFrom`, `policy.lam:9-40,184-195`
`mftGlobSegments`/`Policy.globMatch`, `workload.lam:13-32` `globMatch`, each
with different semantics). The existing unit tests in `tests/unit/`
(`test_secret_box`, `test_keyring`, `test_totp`, `test_ip_allowlist`,
`test_units_security`, `test_paths`, `test_policy`, `test_metrics`,
`test_upload_buffer`, `test_automation`, `test_pricing`) can seed the library
test suites.

Recommended libraries:

| Proposed package | Source in MFT | API sketch | Dependencies | How generic | Effort | Priority |
|---|---|---|---|---|---|---|
| `@lam/netguard` (SSRF guard + IP allowlists) | `security.lam:22-61` (`mftHostIP`, `mftIsPublicIP`), `:132-196` `IpAllowlist`, `:321-373` `NetGuard`; dialers in `sso.lam:29-79`, `worker.lam:35-67`, `importer.lam:38-52` | `NetGuard.isPublicAddress(ip)`, `NetGuard.checkUrl(url, allowPrivate) -> Result[bool]`, `NetGuard.resolvePublic(host, port, allowPrivate) -> Result[str]` (for SSH and other non-HTTP dials), `SafeHttpClient(allowPrivate, timeoutMs)` with `get`/`postForm`/`postJson` (checks at dial time against DNS rebinding, no redirects, response size cap), `IpAllowlist.parse/format/allows` | stdlib only (Go `net`, `net/http`) | Very: any webhook sender, URL fetcher, OIDC client or importer | S-M | High |
| `@lam/keyring` (sealing secrets at rest) | `security.lam:198-319` `SecretBox` (`keyFrom`, `ring`, `seal`, `open`, `needsReseal`), `config.lam:182-206` `secretBoxKey`, `reseal.lam` | `KeyRing.parse("ring:<id>=<hex>;...") -> Result[KeyRing]`, `KeyRing.derive(secret, label)`, `ring.seal(plaintext, aad="")`, `ring.open(sealed)`, `ring.needsReseal(sealed)` | `lamcrypto` / Go `crypto/aes`, `crypto/cipher` | High (credentials, TOTP secrets, API tokens). Must keep MFT's format (`v2:<id>:<base64(nonce‖ct)>` plus unprefixed legacy values), which differs from the `lamcrypto:v1:` envelope of `Crypto.encrypt`. The derivation label `mft:secret-box:v1` becomes a parameter. | S | High |
| `@lam/totp` | `team.lam:23-33`, `:39-96` `Totp` (`newSecret`, `codeAt`, `matchStep` with replay guard, `uri`); recovery-code generation in `Mfa.enable` (`team.lam:197-221`) | `Totp(digits=6, period=30, algorithm="SHA1")`, `newSecret()`, `codeAt(secret, step)`, `verify(secret, code, nowUnix, lastStep) -> int`, `uri(secret, issuer, account)`, `RecoveryCodes.generate(count)` | stdlib only | High (any app with MFA). `Mfa`, `Sessions` and `Team` stay in the app (own tables). | S | High |
| `@lam/webhooks` (signing and safe delivery) | `security.lam:64-130` `Signatures.webhookHeader`; `worker.lam:189-252` `deliverWebhooks` (headers, 12 attempts, backoff from 30 s doubling to a 6 h cap) | `WebhookSigner(secret).header(body, nowUnix)`, `WebhookSigner.verify(body, header, secret, toleranceSeconds, nowUnix) -> Result[bool]`, `WebhookSender(client).deliver(url, body, headers) -> WebhookAttempt`, `Backoff.delay(attempt, baseSeconds, maxSeconds)` | `@lam/netguard`, `lamhash` | High. Uses the same `t=...,v1=...` scheme as Stripe; consider a Standard Webhooks option. The queue tables (`webhooks`, `webhook_deliveries`) stay in the app. | M | High-Medium |
| `@lam/oidc` (OpenID Connect relying party) | `sso.lam:56-100` (fetch, PKCE `mftPkceChallenge`, `OidcProvider`, `audienceList`), `:135-193` `SsoNetwork`, `:207-250` discovery and JWKS refresh, `:275-294` authorization URL, `:296-368` code exchange, `verifyIdToken`, `claimProblem` (OIDC Core 3.1.3.7); JWKS cache from `workload.lam:34-46,111-146` | `OidcClient(issuer, clientId, clientSecret, redirectUri, client)`, `discover() -> Result[OidcProvider]`, `authorizationUrl(state, nonce, verifier, extra)`, `Pkce.challenge(verifier)`, `exchangeCode(code, verifier) -> Result[OidcTokens]`, `verifyIdToken(token, nonce) -> Result[dict[str, any]]`, `JwksCache.keysFor(issuer, jwksUri, refresh)` | `@lam/jwt` (`JwtKeySet.loadJwks`, `decode`), `@lam/netguard`, `lamjson`, `lamurl` | High. Tenant routing, the `sso_requests` table, account linking/JIT and DNS TXT domain checks stay in the app. | M | Medium-High |
| `@lam/prometheus` | `metrics.lam:31-150` `Metrics` (`addCounters`, `addGauges`, `render`, `respondTo`, `serve`); TTL cache pattern from `opsgauges.lam:12-36` | `Registry(namespace)`, `counter/gauge/histogram(name, help, labels)`, `addSource(prefix, help, kind, read)`, `CachedSource(ttlSeconds, read)`, `render()`, `handler(token)`, `serve(address, token)` | `lamconcurrency` | High, but the metric names are hard-coded today (`mft_build_info`, `mft_go_*`) and there are no labels or histograms. Overlaps `@lam/vulture/std-plugin` `metrics(srv, path)` (`std_plugin.lam:363`): one registry should feed both. | M | Medium |
| `@lam/s3` 2.3 additions (no new repository) | `upload_buffer.lam:84-343` `UploadBuffer` (reassembles out-of-order chunks into multipart parts, SHA-256 state that survives a resume); `sftpgw.lam:61-160` `mftPartPipeline` (bounded concurrent part uploads), `mftBlockCache` (ranged-GET read-ahead) | `S3MultipartWriter(client, key, partSize, maxInflight)` with `writeAt`, `hashState`, `close`; `S3ReadAhead(client, key, blockSize, ahead).readAt` | `@lam/s3` | Medium-high for anyone streaming large files to object storage | M | Medium |
| `@lam/jwt` additions (no new repository) | `workload.lam:149-166` `generateIssuerKey` | `Jwt.generateEcKey(kid) -> Result[JwtKeyPair]` (PKCS#8 PEM plus public JWKS) and `JwtKeySet.toJwks()`; `@lam/jwt` can load JWKS but not generate or export them | Go stdlib | Medium | S | Low-Medium |

These belong in the stdlib rather than a package (small, broadly needed):

- `lamjson`: dotted, null-safe lookups like `jsonpath.lam:35-122`
  (`JsonPath.raw/str/int/bool/len/json/has`, more than 200 call sites in MFT).
- `lamdb`: typed row getters to replace `util.lam:67-136` `Rows.int/str/bool`
  (needed because rows return `int64`, `time.Time`, `[]byte`), a stdlib `Sql`
  interface that both `Db` and `Tx` satisfy (MFT's `db.lam:10-13`), and a
  `isUniqueViolation`-style error kind. `one` and `inTx` already overlap
  `Db.first` and `Db.transaction`.
- `lampath`: a configurable `Path.match(pattern, path)` (`*`, `?`, `**`)
  replacing the three glob copies, and a traversal-safe virtual-path
  normalizer from `paths.lam:14-132` (`Paths.normalize` rejects `..`, control
  characters, invalid UTF-8, too-long names and too-deep paths; plus
  `resolve`, `toClient`, `isWithin`) with the limits as parameters.
- `lammigrate`: a way for libraries to ship their own migrations (today
  `Migrator(db, dir)` takes one directory). This blocks the DB-backed
  candidates below. `db.lam:35-41` `runMigrations` is a thin wrapper and
  needs nothing extracted.
- `Ids`, `Digest` and `Clock` in `util.lam` duplicate `lamuuid`, `lamhash`,
  `lamcrypto` and `lamtime`; replace them, do not extract them.

DB-backed patterns, generic but PostgreSQL-specific today (`ON CONFLICT`,
`make_interval`, `FOR UPDATE SKIP LOCKED`, app-owned tables); later and only
with library-shipped migrations:

| Proposed package | Source | API sketch | Effort | Priority |
|---|---|---|---|---|
| `@lam/jobs` (leases, scheduling, health) | `worker.lam:125-136` `acquire` (`job_leases`), `:294-307` `due`, `:317-342` `runJob`/`heartbeat` (`ops_jobs`), `status.lam:15-46` `Heartbeat` | `Leases(db).acquire(name, owner, ttlSeconds)`, `Scheduler(db).every(name, intervalSeconds, fn)`, `JobHealth.record(job, failure)`, `InstanceHeartbeat(db, component, version).run()` | M | Medium |
| `@lam/lockout` (failure throttling shared by replicas) | `team.lam:98-119` `Throttle.check/fail/clear` (`auth_throttle`) | `Lockout(db).check(key)`, `fail(key, limit, windowSeconds)`, `clear(key)`. `lamratelimit.TokenBucket` is in-memory only. | S | Low-Medium |
| `@lam/outbox` (transactional outbox and retried delivery) | `events.lam:39-47` `Outbox.emit`, `worker.lam:138-176` `dispatchOutbox`, `mailer.lam:15-63` `Mailer.queue/deliver` (8 attempts, 1, 2, 4... minute backoff) | `Outbox.enqueue(conn, topic, payload)`, `Outbox.claim(db, limit)`, `markDone`, `markFailed(error, backoff)`; mail and webhooks become consumers | L | Low |

Too app-specific to extract (and why):

- `resumable.lam` (`ResumableUploads.create/status/putChunk/abort`) is tied to
  tenants, namespaces, quotas and `FileSystem`, and
  `@lam/vulture/tus-plugin` already covers the protocol. The reusable core is
  `UploadBuffer` (the `@lam/s3` row above).
- `sftpgw.lam` (1264 lines) wires `@lam/sftp` callbacks to the namespace,
  policy, metering and storage layers. Only its S3 streaming helpers are
  reusable; at most it could become a "S3-backed SFTP backend" example in
  `lam-sftp`.
- `billing.lam` (catalog sync, `stripe_objects`/`stripe_events`, plan
  mapping, grace periods), `pricing.lam` `Catalog`, `units.lam` metric
  definitions and Stripe meter names, `metering.lam` `Accrual`/`UsageMeter`:
  business rules. Only `Charge.costMicros` (graduated/volume tiers with an
  allowance, `units.lam:199-231`) and `moneyMicros` (exact decimals,
  `pricing.lam:21-43`) are generic; a small `@lam/money` would only be worth it
  with a second user.
- `policy.lam` (`Policy.parse` DSL: `default allow|deny`, `allow|deny
  <actions> <pattern> [user:|group:]`, `decide` with line numbers) is close to
  generic, but its actions are fixed in `PolicyActions`. Wait for a second user
  before making a `@lam/pathpolicy`.
- The SQL in `opsgauges.lam`, plus `status.lam` `StatusPage`,
  `encryption.lam` `NamespaceKeys`, `team.lam` `Sessions`/`Mfa`/`Team`,
  `events.lam` `Audit`, `lifecycle.lam`, `retention.lam`, `namespace.lam`,
  `tenants.lam`, `config.lam` and the `api_*.lam` routes are all product
  logic.

### Suggested phasing

1. **Prerequisites.** Package the installed-boundary test runner as a
   template; decide the license; retarget
   `tests/tests/test_github_lams3_install.py`.
2. **Empty `third_party/`.** `@lam/otel` 1.0.0, then `@lam/stripe` 1.0.0
   (typed Results, `StripeWebhook`, meter events), switch MFT, then delete
   `third_party/` along with the docs, website, rules and changelog updates.
3. **Small security packages from MFT.** `@lam/netguard`, then
   `@lam/keyring`, `@lam/totp` and `@lam/webhooks`. MFT switches after each
   release and deletes its duplicate code (the three dialers, `SecretBox`,
   `Totp`, `Signatures`).
4. **Stdlib gaps.** `lamjson` path lookups, `lamdb` typed rows and the `Sql`
   interface, `lampath` glob and virtual paths, `lammigrate` migrations
   shipped by libraries.
5. **Larger integrations.** `@lam/oidc` (with the JWKS cache), the `@lam/jwt`
   key-generation and JWKS-export additions, `@lam/prometheus` (aligned with
   the std-plugin `metrics`), and the `@lam/s3` streaming writer and
   read-ahead.
6. **DB-backed patterns**, once phase 4 can ship migrations: `@lam/jobs`,
   `@lam/lockout`, `@lam/outbox`. Consider `@lam/pathpolicy` and `@lam/money`
   only when a second application needs them.

Every release in these phases updates `CHANGELOG.md`, `docs/libraries.md`
(mirrored to `website/content/libraries.md`), `docs/third_party_libraries.md`
where it lists packages, and `REPOSITORIES` in `mirror-to-github.py`.
