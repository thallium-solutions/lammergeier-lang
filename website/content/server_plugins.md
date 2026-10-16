# `@lam/vulture` Plugin Authoring Guide

This guide walks through the conventions used by the official
[`@lam/vulture/std-plugin`](https://github.com/thallium-solutions/lam-vulture-std-plugin)
collection and shows how to build, test, and ship your own. The HTTP framework
lives in the independent
[`@lam/vulture`](https://github.com/thallium-solutions/lam-vulture) package;
WebSocket and resumable-upload support live in
[`@lam/vulture/ws-plugin`](https://github.com/thallium-solutions/lam-vulture-ws-plugin)
and
[`@lam/vulture/tus-plugin`](https://github.com/thallium-solutions/lam-vulture-tus-plugin).

If you've used Fastify before, the model is recognisable: a plugin attaches
hooks to the request lifecycle and optionally registers routes. A plugin passed
to `Server.register` has the compile-time-checked type `func(Server)` and runs
synchronously. A parameterless installer can be registered directly; a
configurable plugin can expose a typed factory that captures configuration and
returns `func(Server)`. The official collection provides both direct installers
and `*Plugin(...)` factories.

## Installation

Install only the packages an application uses:

```bash
lamc install @lam/vulture@2.0.1
lamc install @lam/vulture/std-plugin@2.0.2
lamc install @lam/vulture/ws-plugin@2.0.1
lamc install @lam/vulture/tus-plugin@3.0.1
```

Each plugin declares `@lam/vulture` as a dependency. These are independently
versioned packages, not standard-library modules. Core Vulture owns HTTP
routing, request/response types, lifecycle hooks, schemas, streaming, SSE,
OpenAPI, and test injection. The standard plugin collection owns common auth,
security, observability, caching, session, and resilience helpers; WebSocket
and tus protocol support remain separate opt-in packages.

---

## 1. The lifecycle hooks

`@lam/vulture` exposes the main Fastify-style hook ladder. They fire **in this order**
around every request:

| Hook | When | Can short-circuit? | Typical use |
|------|------|--------------------|-------------|
| `onRequest`  | Right after the request line is parsed and headers are populated. | Yes (set `res.text/json/...`). | Auth tokens, IP filters, request log start. |
| `preParsing` | Before content-type parsers run. | Yes. | Raw body transforms, parser selection. |
| `preValidation` | After parsing, before schema validation. | Yes. | Normalisation, validation prep. |
| `preHandler` | After validation, before the route handler. | Yes. | Permission checks, route guards. |
| `preSerialization` | After the handler, before response schema serialisation. | Yes. | Body shaping before serialisation. |
| `onSend` | After serialisation, before the response is written. | Yes. | Compression, cache headers, final body/header mutation. |
| `onResponse` | After the response is prepared. | No — observers always run. | Metrics, logging, cleanup. |
| `onError`    | Whenever a hook or handler panics. | Yes (mutate `res`). | Sentry-style reporting, fallback responses. |

A "short-circuit" simply means a hook called `res.text()` / `res.json()`
/ `res.send()` / `res.error_()` / `res.redirect()` / etc., flipping
`res.sent` to `true`. Later request-side hooks plus the route handler
then skip; `onSend` and `onResponse` still run so observers and final
header/body transforms fire.

Two network-only hooks sit outside the inject path:
`onRequestAbort(fn)` fires when a client disconnects before the handler
finishes, and `onTimeout(fn)` fires when `setRequestTimeout(...)` or a
route-level `timeoutMs` deadline wins the race.

---

## 2. The plugin contract

The value accepted by `register` is exactly `func(Server)`:

```lammergeier
func myPlugin(srv: Server) {
    # Install hooks, routes, schemas, decorators, or state on `srv`.
}
```

Register a parameterless plugin directly:

```lammergeier
from @lam/vulture import Server
from my_plugins import myPlugin

func main() {
    srv: Server = Server()
    srv.register(myPlugin)
    _ = srv.listen(8080).unwrap()
}
```

For configurable plugins, keep the direct installer if useful and expose a
typed factory. Lam 1.18 closures preserve the captured configuration while
satisfying `func(Server)`:

```lammergeier
func installMyPlugin(srv: Server, label: str) {
    # Install configured hooks or routes.
}

func myPlugin(label: str = "default") -> func(Server) {
    return lambda(srv: Server) -> None {
        installMyPlugin(srv, label)
    }
}

srv.register(myPlugin("api"))
```

This is the pattern used by the official `requestLogPlugin`,
`rateLimitPlugin`, `compressPlugin`, and other configurable factories.

### Plugin categories

Most plugins fall into one of four shapes:

| Shape | Registers | Examples | Best for |
|---|---|---|---|
| Hook-only | lifecycle hooks | `requestId`, `requestLog`, `helmet`, `compress` | Cross-cutting behavior around every request. |
| Route pack | routes, maybe hooks | `healthcheck`, `metrics`, feature routers | A feature module mounted under a prefix. |
| Decorator | decorators/default state | sessions, db clients, service metadata | Sharing values with handlers/plugins. |
| Parser/serializer | content-type parsers, response hooks | custom webhook parsers, compression | Protocol-specific body handling. |

Keep a plugin focused. A plugin named `auth` should not also install
metrics, static files, and CORS. Compose small plugins in `main()` or
inside a feature router:

```lammergeier
func apiPlugins(srv: Server) {
    requestId(srv)
    requestLog(srv, "[api]")
    helmet(srv)
    metrics(srv, "/metrics")
}

func main() {
    srv: Server = Server()
    apiPlugins(srv)
    srv.register(userRoutes, "/users", true)
    srv.register(orderRoutes, "/orders", true)
    _ = srv.listen(8080).unwrap()
}
```

### Plugin options

Use ordinary parameters for small plugins and a `dict[str, any]` for
large option sets. Prefer typed parameters for required values because
they are self-documenting and checked at compile time.

```lammergeier
func cacheHeaders(srv: Server, directive: str = "no-store", ifMissing: bool = true) {
    func apply(req: Request, res: Response) {
        if ifMissing and res.hasHeader("Cache-Control") {
            return
        }
        res.header("Cache-Control", directive)
    }
    srv.onSend(apply)
}
```

For larger options, normalize once at install time:

```lammergeier
func audit(srv: Server, opts: dict[str, any] = {}) {
    header: str = "X-Audit-Id"
    if "header" in opts {
        header = str(opts["header"])
    }

    func emit(req: Request, res: Response) {
        if req.header(header) != "" {
            res.header(header, req.header(header))
        }
    }
    srv.onResponse(emit)
}
```

Avoid reading `opts` on every request when a simple local variable can
hold the resolved setting. Hook closures capture the local value.

---

## 3. Worked example: a 1-minute auth plugin

The whole plugin in 14 lines:

```lammergeier
from @lam/vulture import Server, Request, Response

func bearerAuth(srv: Server, secret: str) {
    func guard(req: Request, res: Response) {
        if req.header("Authorization") != "Bearer " + secret {
            res.setStatus(401)
            res.text("unauthorized")
        }
    }
    srv.preHandler(guard)
}
```

Three things to notice:

1. **`guard` is a nested named function** — Lam compiles it to a
   Go closure assignment, so the helper closes over its enclosing
   scope while still reading like a regular `func` at the
   definition site. See [SYNTAX.md → Nested Functions](#/docs/syntax?h=nested-functions).
2. **State is captured by closure** through the function-local `secret`.
   Each call to `bearerAuth(srv, "...")` creates a fresh `guard`.
3. **Short-circuit happens implicitly** — calling `res.text()` flips
   `res.sent`, so the route handler never runs.

If your plugin needs *mutable* state across requests (a cache, a
counter), drop into a `go!` block to declare a Go-typed struct + mutex
and capture a pointer to it from the hook closure. See the
[`@lam/vulture/std-plugin` implementation](https://github.com/thallium-solutions/lam-vulture-std-plugin)
of `rateLimit` for the canonical pattern.

### Production auth plugin pattern

A production auth plugin usually does three things:

1. Reads the credential from a header/cookie.
2. Verifies it with a caller-provided function.
3. Stores the authenticated principal on `req.ctx` for downstream
   handlers.

```lammergeier
from @lam/vulture import Server, Request, Response

func tokenAuth(srv: Server, verify: func(str) -> any, header: str = "Authorization") {
    func guard(req: Request, res: Response) {
        raw: str = req.header(header)
        if raw == "" {
            res.code(401).json({"error": "missing token"})
            return
        }

        claims: any = verify(raw)
        if claims == None {
            res.code(401).json({"error": "invalid token"})
            return
        }
        req.ctx["user"] = claims
    }
    srv.preHandler(guard)
}

func tokenAuthPlugin(
    verify: func(str) -> any,
    header: str = "Authorization",
) -> func(Server) {
    return lambda(srv: Server) -> None {
        tokenAuth(srv, verify, header)
    }
}
```

Handlers can then read `req.ctx["user"]`. A cookie-based variant must handle
`req.cookie(name) -> Result[str]`; absence and parse failures are no longer an
empty-string sentinel. If you want this auth to apply only to one mounted
feature, register the feature with encapsulation and install the direct auth
installer inside it:

```lammergeier
func privateApi(srv: Server) {
    tokenAuth(srv, verifyToken)
    srv.get("/me", meHandler)
}

srv.register(privateApi, "/api", true)
```

---

## 4. Sharing data between hooks: `req.ctx`

Every `Request` has a context-backed `ctx` that preserves dictionary indexing.
`req.ctx[key]` delegates to `Context.get`/`set`; `req.ctx.has(key)` and
`key in req.ctx` distinguish a missing key from a stored `None`. Cancellation,
deadline, reason, and wait methods are available on the same object. Use it to
thread state from `onRequest` to the handler to `onResponse`:

```lammergeier
func tracePlugin(srv: Server) {
    func start(req: Request, res: Response) {
        req.ctx["traceId"] = randomTraceId()
    }
    func finish(req: Request, res: Response) {
        tid: any = req.ctx["traceId"]
        res.setHeader("X-Trace-Id", str(tid))
    }
    srv.onRequest(start)
    srv.onResponse(finish)
}
```

Keys with a `__` prefix are an informal convention for "internal use
only" — `@lam/vulture/ws-plugin`, for instance, uses `req.ctx["__wsHandler"]`
to pass the WebSocket callback to the upgrade handler.

### `req.ctx` vs decorators

Use `req.ctx` for values that belong to one request:

- authenticated user/claims;
- trace/span IDs;
- per-request timing marks;
- parsed webhook metadata;
- idempotency/cache keys.

Use decorators for defaults and server-wide values:

- shared database/client objects;
- service name/version;
- default request/reply fields;
- pointers to plugin-owned state.

```lammergeier
func serviceInfo(srv: Server, name: str, version: str) {
    srv.decorate("serviceName", name)
    srv.decorate("serviceVersion", version)

    func expose(req: Request, res: Response) {
        res.header("X-Service", str(srv.dec("serviceName")))
        res.header("X-Service-Version", str(srv.dec("serviceVersion")))
    }
    srv.onSend(expose)
}
```

For request decorators, install a default and then override it during
the request:

```lammergeier
func userSlot(srv: Server) {
    srv.decorateRequest("user", None)
    func load(req: Request, res: Response) {
        req.ctx["user"] = req.dec("user")
    }
    srv.onRequest(load)
}
```

---

## 5. Per-route plugins via `register(plugin, prefix=...)`

Mount a plugin's routes under a path prefix:

```lammergeier
func adminRoutes(srv: Server) {
    srv.get("/users", listUsers)
    srv.post("/users", createUser)
}

func main() {
    srv: Server = Server()
    srv.register(adminRoutes, "/admin/v1")  # → /admin/v1/users
    _ = srv.listen(8080).unwrap()
}
```

`register(plugin, prefix=...)` saves the current prefix, sets a new
one for the duration of the plugin, then restores. Nested
`register(...)` calls compose, so you can build mount trees naturally.

### Encapsulation

`register(plugin, prefix, encapsulate=true)` makes hooks installed by
the plugin private to the routes the plugin adds. This is the closest
match to Fastify's plugin encapsulation.

```lammergeier
func adminAuth(req: Request, res: Response) {
    if req.header("X-Admin") != "1" {
        res.code(403).text("admin only")
    }
}

func adminPlugin(srv: Server) {
    srv.preHandler(adminAuth)
    srv.get("/users", listAdminUsers)
}

func publicPlugin(srv: Server) {
    srv.get("/status", publicStatus)
}

srv.register(adminPlugin, "/admin", true)
srv.register(publicPlugin, "", true)
```

Requests to `/admin/users` run `adminAuth`; `/status` does not.
Decorators and app-lifecycle hooks remain global, because they are
server configuration rather than request-pipeline hooks.

Use encapsulation when a plugin owns a feature area. Avoid it for
global observability/security plugins where every route should be
affected.

### Prefix composition

Nested prefixes compose:

```lammergeier
func v1Users(srv: Server) {
    srv.get("/", listUsers)
    srv.get("/:id", getUser)
}

func v1(srv: Server) {
    srv.register(v1Users, "/users", true)
}

srv.register(v1, "/api/v1", true)
```

The final routes are `/api/v1/users/` and `/api/v1/users/:id`. At runtime the
named value is `req.params[":id"]`; Vulture preserves the colon in
`Request.params`. A `RouteOptions` params schema uses the unmarked property name
`"id"`, which Vulture normalizes only for validation and OpenAPI.

---

## 6. Mutable plugin state with `go!`

The `rateLimit` plugin is the canonical recipe for "plugin needs a
mutex and a map":

```lammergeier
go! {
    type counterState struct {
        mu  sync.Mutex
        hit map[string]int
    }
}

func hitCounter(srv: Server) {
    go! {
        st := &counterState{hit: map[string]int{}}

        guard := func(req *Request, res *Response) {
            st.mu.Lock()
            st.hit[req.Path]++
            st.mu.Unlock()
        }
        srv.OnRequestHooks = append(srv.OnRequestHooks, guard)
    }
}
```

Two gotchas to remember:

- **Inside a `go!` block, everything is Go.** The Lam-side
  `srv.preHandler(guard)` won't work because `guard` is a Go closure.
  Append directly to `srv.OnRequestHooks` (the Go field name uses
  PascalCase) instead.
- **State you want shared across requests must live outside the hook
  closure.** Declare it once in the plugin's `go!` block and capture
  it; if you put `st := ...` inside `guard`, you reset it on every
  request.

### Identifier scoping inside `go!` — `s` / `srv` rules

`go!` blocks are pasted verbatim into Go, so the identifiers that
resolve are the ones Go can see at the paste site. The transpiler only
ever rebinds **one** name: inside a class method, `self.X` is rewritten
to `s.X`. Plugins are top-level functions, not methods, so:

| Where you are | Receiver `s` | Lam param `srv` | Lam globals |
|---|---|---|---|
| Inside `class Server`'s methods | ✅ bound to `*Server` | ❌ | ✅ via package |
| Inside `func plugin(srv: Server)` | ❌ undefined | ✅ Go param | ✅ via package |
| Inside a `lambda req, res:` handler | ❌ | ❌ unless captured | ✅ via package |

The implication for plugin authors: **`srv` is just a Lam parameter**,
not a magical binding. It works inside `go!` because Go scoping makes
the parameter visible — there is no compiler rewrite. `s` is *only*
bound inside `Server`'s own methods.

If a plugin needs state that survives between requests **and** outlives
a single function call, declare a package-level Go var in a top-level
`go!` block:

```lammergeier
# GOOD — package-level Go var, atomic-protected, shared by every
# handler/test in the binary.
go! {
    import "sync/atomic"
    var lamPluginCalls int64
}

func myCounter(srv: Server) {
    go! {
        srv.OnRequestHooks = append(srv.OnRequestHooks, func(req *Request, res *Response) {
            atomic.AddInt64(&lamPluginCalls, 1)
        })
    }
}
```

```lammergeier
# BAD — Top-level handler tries to use ``s`` (the class receiver name)
# but no method context exists. Go reports "undefined: s".
func brokenHandler(req: Request, res: Response) {
    go! {
        s.Routes = append(s.Routes, ...)   # error: undefined: s
    }
}
```

```lammergeier
# BAD — counter is local to the handler closure, so it resets every
# call instead of accumulating across requests.
func brokenCounter(srv: Server) {
    go! {
        srv.OnRequestHooks = append(srv.OnRequestHooks, func(req *Request, res *Response) {
            var calls int64           # NEW VARIABLE EVERY REQUEST
            atomic.AddInt64(&calls, 1)
        })
    }
}
```

For *per-server* (rather than *process-global*) state, use the
`Server` instance directly:

```lammergeier
# GOOD — state lives on the Server, not in module-level memory; two
# instances of the same plugin won't share the counter.
func myCounter(srv: Server) {
    go! {
        var hits int64
        srv.Decorators["__myCounterHits"] = &hits
        srv.OnRequestHooks = append(srv.OnRequestHooks, func(req *Request, res *Response) {
            atomic.AddInt64(&hits, 1)
        })
    }
}
```

(See [SYNTAX.md → Scoping inside `go!` blocks](#/docs/syntax?h=scoping-inside-go-blocks)
for the canonical reference and language-level examples.)

### State design checklist

- **Per request:** use `req.ctx`.
- **Per server instance:** capture a local Go value in the plugin
  function or store a pointer in `srv.Decorators`.
- **Process global:** package-level Go vars in a top-level `go!`
  block, protected by `sync.Mutex`, `sync.Map`, or `sync/atomic`.
- **Cross-process:** use Redis, Memcached, a database, or another
  external store. In-memory plugin state is per binary only.

When the plugin will be used in tests, prefer per-server state. It
lets each test create a fresh `Server()` without state leaking from a
previous test case.

### Route-pack state without raw Go

If state does not need locks or Go-only types, pure Lam fields are
enough:

```lammergeier
func hitHeader(srv: Server) {
    hits: dict[str, int] = {}

    func count(req: Request, res: Response) {
        key: str = req.path
        if key in hits {
            hits[key] = hits[key] + 1
        } else {
            hits[key] = 1
        }
        res.header("X-Route-Hits", str(hits[key]))
    }

    srv.onResponse(count)
}
```

For true concurrent network traffic, use a Go mutex around mutable
maps as shown above. `Server.inject` is deterministic, but real HTTP
listeners run handlers concurrently.

---

## 7. Streaming responses & WebSocket plugins

`Response.streamFile` and `Response.sse` set internal `streamHandler` /
`sseHandler` slots. The dispatcher honours them after running `onResponse`
hooks, so plugins can still inspect streaming responses to (for example) skip
compression — the maintained `compress` plugin does exactly that:

```lammergeier
if res.StreamHandler != nil || res.SseHandler != nil || res.Hijacked {
    return  # Don't try to gzip a stream.
}
```

Use `@lam/vulture/ws-plugin` rather than implementing the native upgrade
boundary in an application plugin. Its `wsRoute` function has default and typed
`RouteOptions` overloads, both returning `Result[bool]`:

```lammergeier
from @lam/vulture import Request, RouteOptions
from @lam/vulture/ws-plugin import WebSocket, wsRoute
from lamerrors import Result

func echo(ws: WebSocket, req: Request) {
    incoming: Result[str] = ws.receive()
    if incoming.ok() {
        _sent: bool = ws.send(incoming.unwrap()).unwrap()
    }
}

options: RouteOptions = RouteOptions(0, 30000)
_ = options.metadata("originAllowList", ["https://app.example.com"])
_ = options.metadata("subprotocols", ["events-v2"])
_ = options.metadata("compression", true)
_registered: bool = wsRoute(srv, "/events", echo, options).unwrap()
```

`receive() -> Result[str]` and `receiveBytes() -> Result[any]` distinguish
valid empty frames from closure and transport failures; `send`, `sendBytes`,
and `ping` also return Results. Read the
[`@lam/vulture/ws-plugin` repository](https://github.com/thallium-solutions/lam-vulture-ws-plugin)
for upgrade and lifecycle details.

### Composing the tus plugin

`@lam/vulture/tus-plugin` owns the protocol routes and filesystem store. Both
public operations use v2 Result contracts:

```lammergeier
from @lam/vulture/tus-plugin import tusGc, tusUploads

_mounted: bool = tusUploads(
    srv,
    "/uploads",
    "/var/lib/uploads",
    200 * 1024 * 1024,
).unwrap()
removed: int = tusGc("/var/lib/uploads", 72).unwrap()
```

`tusUploads(...) -> Result[bool]` reports invalid route/storage configuration;
`tusGc(...) -> Result[int]` reports directory, metadata, and deletion failures.
Keep authentication/authorization in Vulture hooks registered before the tus
routes, and keep completion work behind `srv.ctx["__tusComplete"]` short.

### Custom content-type parser plugin

Plugins can teach the server about vendor media types:

```lammergeier
from lamerrors import Result
from lamstrings import Strings
from lamjson import Json

func lineProtocol(srv: Server) {
    func parse(req: Request) -> any {
        return {"lines": Strings.splitLines(req.body)}
    }
    srv.addContentTypeParser("text/x-lines", parse)
}

func handler(req: Request, res: Response) {
    parsed: Result[any] = req.parsedBody()
    if not parsed.ok() {
        res.code(400).text(str(parsed.error))
        return
    }
    converted: Result[json] = Json.fromValue(parsed.unwrap())
    if not converted.ok() {
        res.code(400).text(str(converted.error))
        return
    }
    res.json(converted.unwrap())
}

lineProtocol(srv)
srv.post("/ingest", handler)
```

Use this for signed webhook formats, log-forwarding protocols, or legacy
services that are not JSON/form/multipart. The parser type is
`func(Request) -> any`, not `func(str) -> any`, and `parsedBody` preserves parse
failures as `Result.Err`. Parser functions should return quickly and leave
expensive validation to schemas or `preValidation` hooks.

### Response-transform plugin

`onSend` is the right phase for response-body/header transforms.
This plugin wraps JSON responses in a standard envelope unless the
handler has already set an opt-out flag:

```lammergeier
from lamstrings import Strings

func envelope(srv: Server) {
    func wrap(req: Request, res: Response) {
        if req.ctx["__skipEnvelope"] != None {
            return
        }
        if Strings.contains(res.getHeader("Content-Type"), "application/json") {
            res.body = "{\"data\":" + res.body + "}"
        }
    }
    srv.onSend(wrap)
}
```

For large responses, streaming responses, SSE, WebSocket hijacks, and file
downloads, inspect the Go fields as the `compress` plugin shipped by
`@lam/vulture/std-plugin` does before mutating the body.

---

## 8. Testing your plugin

Use `Server.inject` — it runs the full hook + route pipeline against
a synthetic request without binding a port, so tests stay
deterministic in CI:

```lammergeier
from @lam/vulture import InjectResponse, Server, Request, Response

func main() {
    srv: Server = Server()
    bearerAuth(srv, "s3cret")
    srv.get("/protected", lambda req, res: res.text("ok"))

    # Without auth header → 401
    r1: InjectResponse = srv.inject("GET", "/protected")
    assert(r1.status == 401)

    # With correct header → 200
    h: dict[str, str] = {}
    h["Authorization"] = "Bearer s3cret"
    r2: InjectResponse = srv.inject("GET", "/protected", "", h)
    assert(r2.status == 200)
    assert(r2.header("content-type") != "")
}
```

`inject` returns a typed `InjectResponse` with `status`, `body`, `headers`, and
a case-insensitive `header(name)` helper. Streaming responses fall through
`streamFallback` so `body` contains the bytes that would have been streamed.

For the installed-package network and cross-feature coverage, see the `tests/`
directories in the sibling `lam-vulture`, `lam-vulture-std-plugin`, and
`lam-vulture-ws-plugin` repositories.

### What to test

For hook-only plugins:

- normal pass-through request;
- short-circuited request;
- headers/body/status mutation;
- hook order relative to other installed plugins;
- behavior when a handler panics.

For route-pack plugins:

- prefix registration;
- route existence via `hasRoute`;
- injected success and failure responses;
- OpenAPI visibility when metadata is expected;
- encapsulation if private hooks are installed.

For stateful plugins:

- state starts empty on a fresh `Server()`;
- state updates after requests;
- TTL/cooldown behavior if time is involved;
- no state bleed between two `Server()` instances.

### Encapsulation test pattern

```lammergeier
func privateHeader(req: Request, res: Response) {
    res.header("X-Private", "1")
}

func privateRoutes(srv: Server) {
    srv.onSend(privateHeader)
    srv.get("/inside", insideHandler)
}

srv: Server = Server()
srv.register(privateRoutes, "/p", true)
srv.get("/outside", outsideHandler)

inside: InjectResponse = srv.inject("GET", "/p/inside")
outside: InjectResponse = srv.inject("GET", "/outside")
assert(inside.header("X-Private") == "1")
assert(outside.header("X-Private") == "")
```

The important assertion is negative: a private hook must not mutate a
route registered outside the encapsulated plugin.

---

## 9. Distributing your plugin

Ship plugins like any Lammergeier third-party library: a `lamlib.toml`,
one or more `.lam` files, and a README that documents installation,
configuration, hook phases, and examples. If your plugin needs an
external Go module, import it inside `go!` and declare the Go pin in
the library manifest's `[go-deps]` section so users get reproducible
builds.

```lammergeier
go! {
    import "github.com/my-org/some-go-lib"
}
```

Plugin README checklist:

- import line and install command;
- one minimal example and one production example;
- every option with default values;
- hook phases used and whether the plugin can short-circuit;
- keys written to `req.ctx`, `srv.ctx`, decorators, or headers;
- external services or Go module dependencies;
- concurrency/state behavior;
- `Server.inject` test recipe.

---

## Per-route options

For behavior that should apply to one endpoint rather than the whole server,
pass a typed `RouteOptions` value to the ordinary route or verb overload:

```lammergeier
from @lam/vulture import RouteOptions

createUserOptions: RouteOptions = RouteOptions(bodyLimit = 8192)
_ = createUserOptions.preHandler(validateUserSchema)
_ = createUserOptions.onResponse(auditUserCreate)
createUserOptions.summary = "Create a user"
createUserOptions.tags = ["users", "write"]

srv.post("/users", createUserHandler, createUserOptions)
```

`RouteOptions(bodyLimit=0, timeoutMs=0, streamBody=false)` configures request
limits, handler timeout, and streaming. Typed chainable methods install
`preParsing`, `preValidation`, `preHandler`, `preSerialization`, `onSend`,
`onResponse`, and `onError` hooks. `withSchema(schema, format="json")` carries
body, query, parameter, header, and response schemas. Public fields
`summary`, `description`, `operationId`, `tags`, `deprecated`, `hidden`,
`consumes`, and `produces` carry OpenAPI metadata; `metadata(name, value)`
stores intentionally heterogeneous application or protocol data.

---

## Validating request bodies with `lamschema`

Route helpers accept a schema directly and validate the request body before
`preHandler` and the handler:

```lammergeier
from @lam/vulture import Server

userCreateSchema: json = {
    "type": "object",
    "required": ["name", "email"],
    "properties": {
        "name": {"type": "string", "minLength": 1, "maxLength": 80},
        "email": {"type": "string", "format": "email"},
    },
}

srv.post("/users", createUserHandler, userCreateSchema, "json")
```

The same overload accepts schema text. Its explicit fourth argument is
`"json"`, `"yaml"`, or `"xml"`. Use `Schema.validate(...)` directly when
validation is needed outside request routing.

Why JSON Schema rather than struct decode? The struct-decode
shortcut catches type errors and required fields but misses ranges,
patterns, regex, enum values, formats (`email`, `uri`, `uuid`),
and combinators (`oneOf` / `anyOf` / `allOf`) — the bulk of what
real APIs need to enforce. JSON Schema is also reusable for
OpenAPI export.

---

## Trust-proxy

If you run behind a reverse proxy, enable trust-proxy *once* at
startup so plugins keyed on client IP (rate-limit, audit logs)
follow the original-client logic:

```lammergeier
srv: Server = Server()
srv.trustProxy(["X-Forwarded-For"], 1)  # 1 hop = closest proxy

# Inside a handler:
func authPlugin(req: Request, res: Response) {
    ip: str = req.realIP()           # honours XFF
    scheme: str = req.realScheme()   # "https" if XFP present
}
```

`hops > 1` lets you skip your own LB / Cloudflare layer when there
are *multiple* trusted proxies between the client and you. **Don't
enable this on a public-facing server** — clients would otherwise
spoof any IP via the `X-Forwarded-For` header.

---

## Recipes

### Rate limit a single route

`rateLimit` from `@lam/vulture/std-plugin` is global. To rate-limit a
single endpoint, call it inside a registered prefix:

```lammergeier
func limitedAuth(srv: Server) {
    rateLimit(srv, 5, 60000, "auth too noisy")
    srv.post("/login", loginHandler)
}

srv.register(limitedAuth, "/auth")
```

### Add a request id

The `@lam/vulture/std-plugin` package already ships `requestId` and the
`requestIdPlugin(...)` factory. Use this recipe as a template only for a custom
variant (different header name, custom ID format, and so on), and choose a
unique plugin name:

```lammergeier
from lamerrors import Result
from lamuuid import Uuid

func uuidRequestId(srv: Server) {
    func emit(req: Request, res: Response) {
        generated: Result[str] = Uuid.v4()
        if not generated.ok() {
            res.code(500).text(str(generated.error))
            return
        }
        id: str = generated.unwrap()
        req.ctx["reqId"] = id
        res.setHeader("X-Request-Id", id)
    }
    srv.onRequest(emit)
}
```

### Mirror a header from request to response

```lammergeier
func mirrorHeader(srv: Server, name: str) {
    func mirror(req: Request, res: Response) {
        v: str = req.header(name)
        if v != "" {
            res.setHeader(name, v)
        }
    }
    srv.onResponse(mirror)
}
```

### Strip an untrusted forwarded-for header

`Request.header(name)` returns `""` for missing headers, so a
non-empty result is the cue to strip:

```lammergeier
func stripForwarded(srv: Server) {
    func strip(req: Request, res: Response) {
        if req.header("X-Forwarded-For") != "" {
            del req.headers["X-Forwarded-For"]
        }
    }
    srv.onRequest(strip)
}
```

---

## Standard plugin package reference

The current independent `@lam/vulture/std-plugin` catalogue, grouped by
concern:

### Request lifecycle
- `requestId(srv, header="X-Request-Id")` — stable per-request ID,
  preserving upstream values and echoing them back on the response.
- `requestLog(srv, label="[req]")` — structured stderr log line
  per request.
- `serverTiming(srv)` — emits an RFC 8290 `Server-Timing`
  header with the total elapsed time plus any application-level
  marks via `req.ctx["__lamSrvTimingMarks"]`.
- `tracing(srv, echoHeader=true)` — W3C Trace Context
  propagation (`traceparent` + `tracestate`).
- `metrics(srv, path="/metrics")` — Prometheus-style exposition
  with per-method / status counters and a latency histogram.

### Resilience
- `rateLimit(srv, max=100, windowMs=60000)` — sliding-window
  in-memory rate limit keyed on client IP.
- `idempotency(srv, header="Idempotency-Key", ttlSec=300)` —
  cache + replay for duplicate requests (Stripe-compatible).
- `CircuitBreaker` — Hystrix-style wrapper class for downstream
  calls; trips open on a failure threshold and recovers via
  half-open probes.

### Security
- `helmet(srv)` — opinionated default security headers
  (`X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`,
  conservative `Content-Security-Policy`, `Strict-Transport-Security`).
- `basicAuth(srv, users, realm="Restricted")` — HTTP Basic
  with constant-time password compare; stashes the username on
  `req.ctx["user"]`.
- `bearerAuth(srv, verify, header="Authorization")` — Bearer-token
  authentication; `verify` is a `func(token: str) -> any` returning
  a truthy claims value on success.
- `ipFilter(srv, allow=[], deny=[])` — allow/deny by CIDR,
  trust-proxy-aware (honours `Server.trustProxy`).
- `session(srv, secret, cookieName="sid", maxAge=86400,
  secure=false, sameSite="Lax")` — signed-cookie sessions backed
  by an in-memory store. Handlers mutate `req.ctx["session"]`.
- `csrf(srv, secret, cookieName="csrf",
  headerName="X-CSRF-Token", safeMethods=["GET", "HEAD", "OPTIONS"])`
  — double-submit-cookie CSRF protection, rotating on every
  successful response.

### Performance & caching
- `compress(srv, minBytes=1024)` — gzip responses when the
  client advertises `Accept-Encoding: gzip`, across JSON, text,
  and streamed responses.
- `etag(srv)` — weak ETag + `If-None-Match` short-circuiting.
- `cacheControl(srv, directive="no-store", ifMissing=true)` —
  default `Cache-Control` for every response.

### Operations
- `healthcheck(srv, livePath="/healthz", readyPath="/readyz")`
  plus `markReady(srv)` / `markNotReady(srv)` for
  Kubernetes-style probes.

### `func(Server)` factories

Configurable installers have matching factories for `Server.register`:
`requestLogPlugin`, `rateLimitPlugin`, `compressPlugin`, `healthcheckPlugin`,
`metricsPlugin`, `tracingPlugin`, `idempotencyPlugin`, `requestIdPlugin`,
`basicAuthPlugin`, `bearerAuthPlugin`, `ipFilterPlugin`, `cacheControlPlugin`,
`sessionPlugin`, and `csrfPlugin`. Parameterless `helmet`, `etag`, and
`serverTiming` can be registered directly.

```lammergeier
srv.register(requestLogPlugin("[api]"))
srv.register(rateLimitPlugin(max=100, windowMs=60000))
srv.register(bearerAuthPlugin(verifyToken))
srv.register(helmet)
```

## See also

- [`@lam/vulture`](https://github.com/thallium-solutions/lam-vulture) — Server / Request / Response classes
- [`@lam/vulture/std-plugin`](https://github.com/thallium-solutions/lam-vulture-std-plugin) — standard plugin collection
- [`@lam/vulture/ws-plugin`](https://github.com/thallium-solutions/lam-vulture-ws-plugin) — WebSocket plugin
- [`@lam/vulture/tus-plugin`](https://github.com/thallium-solutions/lam-vulture-tus-plugin) — tus resumable-upload plugin
- [`docs/stdlib.md`](#/docs/stdlib) — standard-library reference and clearly marked official-package companion sections
