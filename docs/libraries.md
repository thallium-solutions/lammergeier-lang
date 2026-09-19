# Official Lammergeier Libraries

Lammergeier keeps general-purpose building blocks in the standard library and
ships service clients, fast-moving protocols, and vendor integrations as
independently versioned packages. Every official package below targets
`lamc` 1.17, uses a scoped `@lam/...` import, and carries its own tests,
release notes, and pinned Go dependencies.

Use the **Filter libraries** field on the website to narrow this table by
package name, capability, or keyword.

| Library | Purpose | Main exports | Repository |
|---|---|---|---|
| `@lam/jwt` | Sign and verify JWTs with HS256, HS512, and RS256; manage `kid` and JWKS key rotation. | `Jwt`, `JwtKeySet` | [thallium-solutions/lam-jwt](https://github.com/thallium-solutions/lam-jwt) |
| `@lam/memcached` | Binary-protocol Memcached client with SASL authentication, TTLs, counters, mutation helpers, and statistics. | `Memcached` | [thallium-solutions/lam-memcached](https://github.com/thallium-solutions/lam-memcached) |
| `@lam/protobuf` | Protocol Buffers binary, canonical JSON, text, equality, size, and clone helpers for generated Go messages. | `Pb` | [thallium-solutions/lam-protobuf](https://github.com/thallium-solutions/lam-protobuf) |
| `@lam/redis` | Redis strings, native JSON, keys, lists, hashes, sets, sorted sets, pub/sub, scans, and pipelines. | `Redis`, `PubSub` | [thallium-solutions/lam-redis](https://github.com/thallium-solutions/lam-redis) |
| `@lam/s3` | AWS S3 and compatible object storage with text, buffers, streams, files, metadata, bulk operations, and presigned URLs. | `S3`, `S3Config`, `S3Client`, `S3Object` | [thallium-solutions/lam-s3](https://github.com/thallium-solutions/lam-s3) |
| `@lam/sftp` | Callback-driven, storage-agnostic SFTP server with authentication, streaming backends, policy hooks, and metrics. | `SftpServer`, `SftpConfig`, `SftpHandlers`, `SftpRequest` | [thallium-solutions/lam-sftp](https://github.com/thallium-solutions/lam-sftp) |
| `@lam/ssh` | Strictly verified SSH clients, commands, reusable sessions, OpenSSH keys, cancellation, and direct TCP channels. | `SshClient`, `SshConfig`, `SshSession`, `SshKeyPair` | [thallium-solutions/lam-ssh](https://github.com/thallium-solutions/lam-ssh) |
| `@lam/vulture` | Fastify-style HTTP server with routing, hooks, native JSON/schema validation, contexts, streaming, OpenAPI, and in-process injection. | `Server`, `Request`, `Response`, `SseEmitter`, `HttpError` | [thallium-solutions/lam-vulture](https://github.com/thallium-solutions/lam-vulture) |
| `@lam/vulture/std-plugin` | Common Vulture plugins for logging, security, compression, metrics, tracing, authentication, sessions, CSRF, and resilience. | `requestLog`, `rateLimit`, `compress`, `helmet`, `metrics`, `tracing`, `CircuitBreaker`, … | [thallium-solutions/lam-vulture-std-plugin](https://github.com/thallium-solutions/lam-vulture-std-plugin) |
| `@lam/vulture/tus-plugin` | tus.io resumable-upload routes and garbage collection for Vulture. | `tusUploads`, `tusGc` | [thallium-solutions/lam-vulture-tus-plugin](https://github.com/thallium-solutions/lam-vulture-tus-plugin) |
| `@lam/vulture/ws-plugin` | WebSocket upgrades, frames, deadlines, subprotocols, compression, and origin policy for Vulture. | `WebSocket`, `wsRoute`, `wsRouteOpts` | [thallium-solutions/lam-vulture-ws-plugin](https://github.com/thallium-solutions/lam-vulture-ws-plugin) |

## Installation

Install from Git before the first registry release:

```bash
lamc install https://github.com/thallium-solutions/lam-redis.git@v1.0.0
```

After a registry release, use the scoped package and a SemVer constraint:

```bash
lamc install '@lam/redis@^1.0'
```

For sibling checkouts during development, install by path:

```bash
lamc install ../lam-redis
```

All forms install to `extlibs/@lam/<name>` and use the same import:

```lammergeier
from @lam/redis import Redis

func main() {
    redis: Redis = Redis.connect("127.0.0.1:6379")
    redis.set("greeting", "hello", 60)
    print(redis.get("greeting"))
    redis.close()
}
```

Commit `lamlib.toml` and `lamlib.lock.toml`. Do not commit credentials or place
passwords, private keys, access keys, or tokens in package configuration files.
See the [package-manager guide](package_manager.md) for frozen installs,
lockfile verification, local replacements, and transitive Go-module pins.

## Migrating from Lam 1.15

Four integrations moved out of the compiler-bundled standard library in Lam
1.16. Their public class APIs remain available from scoped packages:

| Lam 1.15 import | Lam 1.16 package import |
|---|---|
| `from lamjwt import Jwt, JwtKeySet` | `from @lam/jwt import Jwt, JwtKeySet` |
| `from lamemcached import Memcached` | `from @lam/memcached import Memcached` |
| `from lamprotobuf import Pb` | `from @lam/protobuf import Pb` |
| `from lamredis import Redis, PubSub` | `from @lam/redis import Redis, PubSub` |

Install the matching package, update the import, and keep the rest of the call
sites unchanged. Redis and Memcached integration fixtures now live in their
package repositories, so the language repository's service script only starts
services needed by the remaining stdlib tests.

## Migrating the HTTP server from Lam 1.17

The Vulture server family moved from compiler-bundled modules to scoped packages.
The API remains Vulture's Fastify-style server surface; update imports and install
the packages used by the application:

| Bundled import | External package import |
|---|---|
| `from lamserver import Server, Request, Response` | `from @lam/vulture import Server, Request, Response` |
| `from lamserver_plugins import requestLog, metrics` | `from @lam/vulture/std-plugin import requestLog, metrics` |
| `from lamserver_tus import tusUploads, tusGc` | `from @lam/vulture/tus-plugin import tusUploads, tusGc` |
| `from lamserver_ws import WebSocket, wsRoute` | `from @lam/vulture/ws-plugin import WebSocket, wsRoute` |

```bash
lamc install https://github.com/thallium-solutions/lam-vulture.git@v1.0.0
lamc install https://github.com/thallium-solutions/lam-vulture-std-plugin.git@v1.0.0
```

Each plugin manifest resolves `@lam/vulture` transitively. Install tus/WebSocket
plugins only when the application uses those protocols.

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
[third-party library specification](third_party_libraries.md) explains how to
author, test, publish, and resolve packages with the same layout.
