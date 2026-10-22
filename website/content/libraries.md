# Official Lammergeier Libraries

Lammergeier keeps general-purpose building blocks in the standard library and
ships service clients, fast-moving protocols, and vendor integrations as
independently versioned packages. The official packages below are tested with
`lamc` 3.0; each uses a scoped `@lam/...` import and carries its own tests,
release notes, and pinned Go dependencies. The libraries that existed before
3.0 declare `>=1.18, <4`; the five added in October 2026 declare `>=3.0, <4`.
Current releases:

| Release | Notes |
|---|---|
| `@lam/jwt@3.1.2` | lamc 3.x compatible; ES256/ES384/ES512, EC keys in `JwtKeySet` and JWKS, `JwtKeySet.decode` for any RS*/PS*/ES* token (root `tag()` removed in 3.0.0) |
| `@lam/ssh@3.0.3` | lamc 3.x compatible; `golang.org/x/crypto` v0.58.0 (SSH channel denial-of-service fixes; Go 1.26) |
| `@lam/protobuf@3.0.2` | lamc 3.x compatible; root `tag()` removed |
| `@lam/vulture/tus-plugin@3.0.3` | lamc 3.x compatible; pins `@lam/vulture` 2.0.3; root `tag()` removed (3.0.0) |
| `@lam/sftp@3.6.0` | host key rotation (`announceHostKey`, OpenSSH hostkeys-00/prove), OpenSSH user certificates (`withUserCertificates`), `fsync@openssh.com` (`onFsync`), `SftpKey.fingerprint`/`publicKey`; PROXY protocol v1/v2 from trusted load balancers (`withProxyProtocol`); lamc 3.x compatible; pins `@lam/ssh` 3.0.3; per-address authentication limits (`withAddressAuthRateLimit`, `withAddressLockout`, `SftpAddressLimiter`); adaptive ciphers (`withAdaptiveCiphers`), `onOpenWrite`, live session listing and targeted disconnect (`activeSessions`, `disconnectWhere`) |
| `@lam/s3@2.5.0` | `blockPrivateNetworks` (SSRF-safe clients for user-supplied endpoints, checked after DNS resolution); S3 Object Lock (`setRetention`, `setLegalHold`, `objectLockConfig`, `createBucketWithObjectLock`) and versioned buckets (`versions`, `deleteVersion`); lamc 3.x compatible; multipart parts streamed from files and readers (`uploadPartFile`, `uploadPartReader`); SSE-C customer keys (`withCustomerKey`), cross-key and large server-side copies (`copyFrom`, `copyLarge`); multipart uploads, ranged/streaming reads, bucket helpers |
| `@lam/redis@2.0.2`, `@lam/memcached@2.0.2` | lamc 3.x compatible |
| `@lam/vulture/ws-plugin@2.0.3` | lamc 3.x compatible; pins `@lam/vulture` 2.0.3 |
| `@lam/vulture@2.0.3` | lamc 3.x compatible (integer divisions use `//`), no API change |
| `@lam/vulture/std-plugin@2.0.4` | lamc 3.x compatible; pins `@lam/vulture` 2.0.3 |
| `@lam/stripe@1.0.1` | moved from `third_party/lamstripe` 0.2.0: typed Results, retries with idempotency keys, pagination, billing meters and meter events, subscriptions, portal, webhook verification (`StripeWebhook`) |
| `@lam/otel@1.1.0` | moved from `third_party/lamotel` 0.2.1: OpenTelemetry traces, metrics and logs over OTLP/HTTP JSON, W3C propagation, sampling, batching, in-memory exporters |
| `@lam/keyring@1.0.1` | sealing secrets at rest: key rings with rotation and resealing, HKDF/PBKDF2 derivation, associated data, purpose subkeys, envelope keys |
| `@lam/totp@1.0.1` | RFC 4226/6238 one-time passwords, otpauth URIs, dependency-free QR codes (SVG / data URI), recovery codes |
| `@lam/jobs@1.0.1` | PostgreSQL leases, periodic and cron jobs across replicas, job health, instance heartbeats, durable queues with retries and dead letters, worker pools |

Git release tags are the plain version (`3.0.1`, not `v3.0.1`).

Use the **Filter libraries** field on the website to narrow this table by
package name, capability, or keyword.

| Library | Purpose | Main exports | Repository |
|---|---|---|---|
| `@lam/jwt` | Sign and verify JWTs with HS256, HS512, RS256 and ES256/384/512; manage `kid` and JWKS (RSA and EC) key rotation. | `Jwt`, `JwtKeySet` | [thallium-solutions/lam-jwt](https://github.com/thallium-solutions/lam-jwt) |
| `@lam/memcached` | Binary-protocol Memcached client with SASL authentication, TTLs, counters, mutation helpers, and statistics. | `Memcached` | [thallium-solutions/lam-memcached](https://github.com/thallium-solutions/lam-memcached) |
| `@lam/protobuf` | Protocol Buffers binary, canonical JSON, text, equality, size, and clone helpers for generated Go messages. | `Pb` | [thallium-solutions/lam-protobuf](https://github.com/thallium-solutions/lam-protobuf) |
| `@lam/redis` | Redis strings, native JSON, keys, lists, hashes, sets, sorted sets, pub/sub, scans, and pipelines. | `PubSub`, `Redis`, `RedisCommand`, `RedisScanPage`, `RedisZMember` | [thallium-solutions/lam-redis](https://github.com/thallium-solutions/lam-redis) |
| `@lam/s3` | AWS S3 and compatible object storage with text, buffers, streams, files, metadata, bulk operations, and presigned URLs. | `S3`, `S3Config`, `S3Client`, `S3Object` | [thallium-solutions/lam-s3](https://github.com/thallium-solutions/lam-s3) |
| `@lam/sftp` | Callback-driven, storage-agnostic SFTP server with authentication, streaming backends, policy hooks, and metrics. | `SftpServer`, `SftpConfig`, `SftpHandlers`, `SftpRequest` | [thallium-solutions/lam-sftp](https://github.com/thallium-solutions/lam-sftp) |
| `@lam/ssh` | Strictly verified SSH clients, commands, reusable sessions, OpenSSH keys, cancellation, and direct TCP channels. | `SshClient`, `SshCommandResult`, `SshConfig`, `SshKeyPair`, `SshSession` | [thallium-solutions/lam-ssh](https://github.com/thallium-solutions/lam-ssh) |
| `@lam/vulture` | Fastify-style HTTP server with typed routes, hooks, native JSON/schema validation, contexts, streaming, OpenAPI, and in-process injection. | `Server`, `Request`, `Response`, `RouteOptions`, `InjectResponse`, `RequestBodyReader`, `SseEmitter`, `HttpError` | [thallium-solutions/lam-vulture](https://github.com/thallium-solutions/lam-vulture) |
| `@lam/vulture/std-plugin` | Common Vulture plugins for logging, security, compression, metrics, tracing, authentication, sessions, CSRF, and resilience. | `CircuitBreaker`, `compress`, `helmet`, `metrics`, `rateLimit`, `requestLog` (plus other direct installers and `*Plugin` factories) | [thallium-solutions/lam-vulture-std-plugin](https://github.com/thallium-solutions/lam-vulture-std-plugin) |
| `@lam/vulture/tus-plugin` | Result-returning tus.io resumable-upload route registration and garbage collection for Vulture. | `tusUploads`, `tusGc` | [thallium-solutions/lam-vulture-tus-plugin](https://github.com/thallium-solutions/lam-vulture-tus-plugin) |
| `@lam/vulture/ws-plugin` | Result-returning WebSocket upgrades, frames, subprotocols, compression, and origin policy for Vulture. | `WebSocket`, `wsRoute`, `wsUpgrade` | [thallium-solutions/lam-vulture-ws-plugin](https://github.com/thallium-solutions/lam-vulture-ws-plugin) |
| `@lam/stripe` | Stripe API client: customers, products, prices, checkout and portal sessions, subscriptions, invoices, billing meters and meter events, retries with idempotency keys, pagination, webhook signature verification. | `StripeClient`, `StripeConfig`, `StripeWebhook`, `StripeEvent`, `StripeError` | [thallium-solutions/lam-stripe](https://github.com/thallium-solutions/lam-stripe) |
| `@lam/otel` | OpenTelemetry SDK: traces, metrics and logs exported as OTLP/HTTP JSON, W3C trace context and baggage, samplers, batch processors, in-memory and console exporters. | `Otel`, `OtelConfig`, `OtelTracer`, `OtelSpan`, `OtelMeter`, `OtelLogger` | [thallium-solutions/lam-otel](https://github.com/thallium-solutions/lam-otel) |
| `@lam/keyring` | Secrets at rest: AES-256-GCM key rings with key ids and rotation, resealing in batches, HKDF/PBKDF2 derivation, associated data, purpose subkeys, envelope (wrapped) keys. | `KeyRing`, `KeyDerivation`, `Resealer` | [thallium-solutions/lam-keyring](https://github.com/thallium-solutions/lam-keyring) |
| `@lam/totp` | One-time passwords (HOTP/TOTP, SHA-1/256/512), base32 secrets, otpauth URIs, QR codes rendered to SVG with no dependencies, recovery codes. | `Totp`, `Hotp`, `OtpAuth`, `QrCode`, `RecoveryCodes` | [thallium-solutions/lam-totp](https://github.com/thallium-solutions/lam-totp) |
| `@lam/jobs` | PostgreSQL-backed background work: leases, periodic and cron schedules running once across replicas, job health, instance heartbeats, durable queues with retries, dead letters and worker pools. | `Jobs`, `JobScheduler`, `JobQueue`, `JobWorkers`, `JobLeases`, `JobHealth`, `InstanceHeartbeat` | [thallium-solutions/lam-jobs](https://github.com/thallium-solutions/lam-jobs) |

## Installation

Install a tagged release directly from Git (release tags are plain versions,
without a `v` prefix):

```bash
lamc install https://github.com/thallium-solutions/lam-redis.git@2.0.2
```

From a configured registry, use the scoped package and a SemVer constraint:

```bash
lamc install '@lam/redis@^2.0'
```

For sibling checkouts during development, install by path:

```bash
lamc install ../lam-redis
```

All forms install to `extlibs/@lam/<name>` and use the same import:

```lammergeier
from @lam/redis import PubSub, Redis, RedisCommand, RedisScanPage, RedisZMember
from lamerrors import Result

func cacheGreeting() -> Result[str] {
    redis: Redis = Redis.connect("127.0.0.1:6379")?
    _ = redis.set("greeting", "hello", 60)?
    value: str = redis.get("greeting")?
    _ = redis.close()?
    return Result.Ok(value)
}
```

Commit `lamlib.toml` and `lamlib.lock.toml`. Do not commit credentials or place
passwords, private keys, access keys, or tokens in package configuration files.
See the [package-manager guide](#/docs/package_manager) for frozen installs,
lockfile verification, local replacements, and transitive Go-module pins.

## Version 2 Result contracts

Version 2 package APIs use unsuffixed `Result[T]` operations for expected
configuration, transport, service, filesystem, and codec failures. Propagate
with `?`, inspect `ok()` / `error`, or deliberately `unwrap()` at a process or
test boundary. The Redis example above shows the current `connect`, command,
and `close` contract; the other package families follow the same policy.

### Memcached

```lammergeier
from @lam/memcached import Memcached
from lamerrors import Result

func cachedGreeting() -> Result[str] {
    cache: Memcached = Memcached.connect("127.0.0.1:11211")?
    _ = cache.set("greeting", "hello", ttlSec = 60)?
    value: str = cache.get("greeting")?
    _ = cache.close()?
    return Result.Ok(value)
}
```

A missing key is `Result.Err(MemcachedNotFound)` rather than `""`;
conditional mutations such as `add`, `replace`, and `delete` use `Ok(false)`
for their normal negative outcomes.

### JWT and Protocol Buffers

```lammergeier
from @lam/jwt import Jwt
from lamerrors import Result

func issueAndDecode() -> Result[dict[str, any]] {
    claims: dict[str, any] = {"sub": "alice", "role": "admin"}
    token: str = Jwt.signHS256(claims, "replace-with-a-strong-secret", 3600)?
    return Jwt.decodeHS256(token, "replace-with-a-strong-secret")
}
```

The v2 JWT Result surface covers `Jwt.signHS256`, `Jwt.decodeHS256`,
`Jwt.decodeRS256`, `JwtKeySet.decodeRS256`, `JwtKeySet.addRSA`,
`JwtKeySet.addJwk`, and `JwtKeySet.loadJwks`. Boolean verification helpers and
legacy algorithm-specific operations that cannot report details retain their
documented direct return types.

```lammergeier
from @lam/protobuf import Pb
from lamerrors import Result

go! {
    import "google.golang.org/protobuf/types/known/structpb"
}

func protobufRoundTrip() -> Result[bool] {
    source: any = None
    target: any = None
    go! {
        source, _ = structpb.NewStruct(map[string]interface{}{"name": "Ada"})
        target = &structpb.Struct{}
    }
    wire: str = Pb.marshal(source)?
    return Pb.unmarshal(wire, target)
}
```

`Pb.marshal -> Result[str]` and `Pb.unmarshal -> Result[bool]`; JSON/text,
equality, size, and clone helpers retain the direct contracts documented by the
package.

### S3

```lammergeier
from @lam/s3 import S3Client, S3Object
from lamerrors import Result

func archiveReport() -> Result[bool] {
    client: S3Client = S3Client.fromEnv()?
    _ = client.put("reports/daily.txt", "ready", "text/plain")?
    body: str = client.get("reports/daily.txt")?
    objects: list[S3Object] = client.list("reports/")?
    _ = client.copy("reports/daily.txt", "archive/daily.txt")?
    print(f"{len(body)} bytes across {len(objects)} report objects")
    return client.delete("reports/daily.txt")
}
```

Use `S3Client.connect(...) -> Result[S3Client]` or `fromEnv`; v2 consolidates
string, `list[int]`, and native stream uploads under overloaded `put`. `get`,
`uploadFile`, `downloadFile`, `list`, `keys`, `stat`, `copy`, `move`, deletes,
and presigning all return typed Results.
Large objects use `createMultipartUpload` / `uploadPart` / `listParts` /
`completeMultipartUpload` / `abortMultipartUpload`, and reads can fetch byte
ranges (`getRange`, `getRangeBytes`) or stream in chunks through
`open(key, offset, length) -> Result[S3ObjectReader]`. An explicit `http://`
endpoint is kept, so local MinIO/SeaweedFS gateways work; bare hosts default
to HTTPS.

### SFTP and SSH

```lammergeier
from @lam/sftp import SftpConfig, SftpServer
from lamerrors import Result

func serveSftp(server: SftpServer) -> Result[bool] {
    return server.listen()
}

func loadSftpConfig(path: str) -> Result[SftpConfig] {
    return SftpConfig.fromKeyFile(path)
}
```

Configure SFTP authentication and typed storage callbacks before calling
`listen()` or `listenBackground()`; both lifecycle methods return
`Result[bool]`. `SftpRequest.context()`, `cancelled()`, and `deadlineUnix()` are
also Result-returning. High-throughput backends should register
`onReadAtBytes`/`onWriteAtBytes` (or install a `SftpByteStreamingStorage`),
which receive packet payloads as `bytes` without the per-byte `list[int]`
conversion of `onReadAt`/`onWriteAt`.

```lammergeier
from @lam/ssh import SshClient, SshCommandResult, SshConfig, SshKeyPair, SshSession
from lamerrors import Result

func inspectHost() -> Result[SshCommandResult] {
    config: SshConfig = SshConfig("ssh.example.com", "deploy", 22)
    config.withPassword("read-this-from-a-secret-store")
    config.withHostKeyFingerprint("SHA256:REPLACE_WITH_VERIFIED_FINGERPRINT")

    client: SshClient = SshClient.connect(config)?
    result: SshCommandResult = client.run("uname -a")?
    _ = client.close()?
    return Result.Ok(result)
}
```

SSH v2 uses Results for `connect`, `close`, command/context runs, session open,
direct dialing, and TCP channel send/receive/close. A nonzero remote exit is a
successful transport Result whose `SshCommandResult.success` is `false`.

## Billing, observability, secrets, MFA and background jobs

The five packages added in October 2026 grew out of a production SaaS
application (a managed SFTP service) and follow the same conventions:

- Stripe billing (subscriptions with metered prices and meter events,
  checkout and portal sessions, verified webhooks):
  `from @lam/stripe import StripeClient, StripeConfig, StripeWebhook`.
- OpenTelemetry (`Otel.setup(OtelConfig.fromEnv())` once, then spans, metrics
  and logs exported to any OTLP/HTTP collector):
  `from @lam/otel import Otel, OtelConfig, OtelTracer`.
- Secrets at rest (sealing credentials and tokens in a database column,
  rotating keys and resealing in batches):
  `from @lam/keyring import KeyRing, KeyDerivation, Resealer`.
- Two-factor authentication (enrolment QR code as SVG or data URI,
  verification with replay protection, recovery codes):
  `from @lam/totp import Totp, Hotp, OtpAuth, QrCode, RecoveryCodes`.
- Background work on PostgreSQL (periodic and cron jobs that run once across
  replicas, durable queues with retries and dead letters):
  `from @lam/jobs import Jobs, JobScheduler, JobQueue, JobWorkers`.

Each README has complete examples, the configuration (environment variables)
and the security or operations notes.

## Upgrading to the 3.0 releases

`@lam/jwt`, `@lam/protobuf`, `@lam/sftp`, `@lam/ssh` and
`@lam/vulture/tus-plugin` 3.0.0 remove the root `tag()` helper (the package
name and version live in `lamlib.toml`). Nothing else breaks: replace any
`tag()` call with a constant of your own. `@lam/sftp` 3.0.0 also adds
zero-conversion byte streaming (`onReadAtBytes` / `onWriteAtBytes`), and
`@lam/s3` 2.1.0 adds multipart uploads, ranged and streaming reads and bucket
helpers.

## Migrating legacy integrations to v2

Applications upgrading compiler-era imports should install the matching scoped
2.0.0 package and update imports:

| Legacy import | Current package import |
|---|---|
| `from lamjwt import Jwt, JwtKeySet` | `from @lam/jwt import Jwt, JwtKeySet` |
| `from lamemcached import Memcached` | `from @lam/memcached import Memcached` |
| `from lamprotobuf import Pb` | `from @lam/protobuf import Pb` |
| `from lamredis import Redis, PubSub` | `from @lam/redis import Redis, PubSub` |
| `from lamstripe import StripeClient, StripeConfig` | `from @lam/stripe import StripeClient, StripeConfig` (1.0.0: typed Results; see its CHANGELOG) |
| `from lamotel import OtelTracer, OtelConfig` | `from @lam/otel import OtelTracer, OtelConfig` (1.0.0; see its CHANGELOG) |

Imports are not the only change in v2:

- Redis and Memcached connection, command, maintenance, Pub/Sub, and close
  operations return typed Results; missing values are errors rather than empty
  strings.
- JWT's HS256 signing plus HS256/RS256 decode paths use Results where failure
  details matter.
- Protobuf binary `marshal` and `unmarshal` return `Result[str]` and
  `Result[bool]`.
- Constructors use Lam 1.18 `init`; package tests and service fixtures live in
  the sibling package repositories.

## Migrating legacy Vulture applications to v2

Vulture, its standard plugin collection, WebSocket support, and tus support are
independent scoped packages. Replace legacy compiler-era imports with:

| Legacy import | Current package import |
|---|---|
| `from lamserver import Server, Request, Response` | `from @lam/vulture import Server, Request, Response, RouteOptions, InjectResponse` |
| `from lamserver_plugins import requestLog, metrics` | `from @lam/vulture/std-plugin import requestLogPlugin, metricsPlugin` |
| `from lamserver_tus import tusUploads, tusGc` | `from @lam/vulture/tus-plugin import tusUploads, tusGc` |
| `from lamserver_ws import WebSocket, wsRoute` | `from @lam/vulture/ws-plugin import WebSocket, wsRoute` |

```bash
lamc install @lam/vulture@2.0.3
lamc install @lam/vulture/std-plugin@2.0.3
# Install only when used:
lamc install @lam/vulture/ws-plugin@2.0.2
lamc install @lam/vulture/tus-plugin@3.0.2
```

The standard plugin package's canonical direct-installer imports are:

```lammergeier
from @lam/vulture import Server
from @lam/vulture/std-plugin import CircuitBreaker, compress, helmet, metrics, rateLimit, requestLog

func installHttpDefaults(srv: Server) {
    requestLog(srv)
    rateLimit(srv)
    compress(srv)
    helmet(srv)
    metrics(srv)
}
```

`CircuitBreaker` protects downstream calls; the direct installers above also
have configurable `*Plugin(...)` factory forms where configuration is needed.

Then migrate call sites to the v2 contracts:

- Construct typed `RouteOptions` and pass them to the actual `route`, `get`,
  `post`, `put`, `del`, `patch`, `head`, `options`, or `all` overload.
- Treat `Server.inject` as `InjectResponse`; use `.status`, `.body`, `.headers`,
  and case-insensitive `.header(name)`.
- Handle Results from request JSON/content parsing, cookie reads/signature
  checks, body-reader read/copy/close operations, and listener startup.
- Use `func(Server)` plugin values. The standard plugin package exposes
  configurable `*Plugin(...)` factories for `register`; `helmet`, `etag`, and
  `serverTiming` can be registered directly.
- Use the configured `wsRoute(..., RouteOptions)` overload and handle its
  registration Result. WebSocket `send`, `receive`, `sendBytes`,
  `receiveBytes`, and `ping` are Result-based.
- Handle `tusUploads -> Result[bool]` and `tusGc -> Result[int]`.

Each plugin manifest resolves `@lam/vulture` v2 transitively.

## Package relationships

`@lam/sftp` depends on `@lam/ssh` for host-key generation and dependency
ownership. `@lam/ssh` uses the Lam `lamcrypto` and `lamcontext` standard
libraries for key material and cancellation. `@lam/redis` also uses
`lamcontext` internally for command and pub/sub cancellation.

`@lam/vulture/std-plugin`, `@lam/vulture/tus-plugin`, and
`@lam/vulture/ws-plugin` all depend on `@lam/vulture`. The core Vulture package
links back to the Lammergeier language/compiler repository; every plugin links
to both projects and keeps its own protocol-specific tests and Go dependencies.

Each package README is the authoritative API and security guide. The
[third-party library specification](#/docs/third_party) explains how to
author, test, publish, and resolve packages with the same layout.
