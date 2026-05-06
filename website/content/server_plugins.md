# `lamserver` Plugin Authoring Guide

This guide walks through the conventions used by every plugin shipped
in [`lib/lamserver_plugins.lam`](https://github.com/thallium-solutions/lammergeier-lang/blob/main/lib/lamserver_plugins.lam) and
shows how to build, test, and ship your own.

If you've used Fastify before, the model is recognisable: a plugin
attaches hooks to the request lifecycle and (optionally) registers
routes. The big difference is that Lammergeier plugins are plain
top-level functions — there's no factory layer, no closure
type-erasure, and no async setup. They run synchronously and they
type-check at compile time.

---

## 1. The lifecycle hooks

`lamserver` exposes the main Fastify-style hook ladder. They fire **in this order**
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

A plugin is just:

```lammergeier
func myPlugin(srv: Server, ...config) {
    # Install hooks / routes / state on `srv`.
}
```

That's the entire interface. Call it from `main()`:

```lammergeier
from lamserver import Server
from my_plugins import myPlugin

func main() {
    srv: Server = Server()
    myPlugin(srv, "config-value")
    srv.listen(8080)
}
```

If you'd rather use the Fastify-flavoured `srv.register(...)` form,
write a thin wrapper:

```lammergeier
func myPluginWrapper(srv: Server) {
    myPlugin(srv, "default-config")
}

# Now srv.register(myPluginWrapper) works.
```

The wrapper exists because Lam's `register()` requires a
`func(*Server)`-typed value, which top-level named functions satisfy
but closures do not.

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
    srv.listen(8080)
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
from lamserver import Server, Request, Response

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
and capture a pointer to it from the hook closure. See
[`lib/lamserver_plugins.lam`](https://github.com/thallium-solutions/lammergeier-lang/blob/main/lib/lamserver_plugins.lam)'s
`rateLimit` for the canonical pattern.

### Production auth plugin pattern

A production auth plugin usually does three things:

1. Reads the credential from a header/cookie.
2. Verifies it with a caller-provided function.
3. Stores the authenticated principal on `req.ctx` for downstream
   handlers.

```lammergeier
from lamserver import Server, Request, Response

func tokenAuth(srv: Server, verify: any, header: str = "Authorization") {
    func guard(req: Request, res: Response) {
        raw: str = req.header(header)
        if raw == "" {
            res.code(401).json({"error": "missing token"})
            return
        }

        claims: any = None
        _ = verify  # used inside go!
        go! {
            if fn, ok := verify.(func(string) interface{}); ok {
                claims = fn(raw)
            }
        }
        if claims == None {
            res.code(401).json({"error": "invalid token"})
            return
        }
        req.ctx["user"] = claims
    }
    srv.preHandler(guard)
}
```

Handlers can then read `req.ctx["user"]`. If you want this auth to
apply only to one mounted feature, register the feature with
encapsulation and install the auth plugin inside it:

```lammergeier
func privateApi(srv: Server) {
    tokenAuth(srv, verifyToken)
    srv.get("/me", meHandler)
}

srv.register(privateApi, "/api", true)
```

---

## 4. Sharing data between hooks: `req.ctx`

Every `Request` has a `ctx: dict[str, any]` you can stash anything
on. Use it to thread state from `onRequest` to the handler to
`onResponse`:

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
only" — `lamserver_ws`, for instance, uses `req.ctx["__wsHandler"]`
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
    srv.listen(8080)
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

The final routes are `/api/v1/users/` and `/api/v1/users/:id`.

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

`Response.streamFile` and `Response.sse` set internal `streamHandler`
/ `sseHandler` slots. The dispatcher honours them after running
`onResponse` hooks, so plugins can still inspect streaming responses
to (for example) skip compression — the bundled `compress` plugin
does exactly that:

```lammergeier
if res.StreamHandler != nil || res.SseHandler != nil || res.Hijacked {
    return  # Don't try to gzip a stream.
}
```

WebSocket plugins are essentially `lamserver_ws`'s `wsRoute` —
register a normal GET route, attach a `preHandler` that puts your
WebSocket callback on `req.ctx`, and let the route handler call
`websocket.Upgrade` from inside a `go!` block. Read
[`lib/lamserver_ws.lam`](https://github.com/thallium-solutions/lammergeier-lang/blob/main/lib/lamserver_ws.lam) for the full
implementation.

### Custom content-type parser plugin

Plugins can teach the server about vendor media types:

```lammergeier
from lamstrings import Strings

func lineProtocol(srv: Server) {
    func parse(body: str) -> any {
        return {"lines": Strings.splitLines(body)}
    }
    srv.addContentTypeParser("text/x-lines", parse)
}

func handler(req: Request, res: Response) {
    res.json(req.parsedBody())
}

lineProtocol(srv)
srv.post("/ingest", handler)
```

Use this for signed webhook formats, log-forwarding protocols, or
legacy services that are not JSON/form/multipart. Parser functions
should return quickly and leave expensive validation to schemas or
`preValidation` hooks.

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

For large responses, streaming responses, SSE, WebSocket hijacks, and
file downloads, inspect the Go fields as the bundled `compress`
plugin does before mutating the body.

---

## 8. Testing your plugin

Use `Server.inject` — it runs the full hook + route pipeline against
a synthetic request without binding a port, so tests stay
deterministic in CI:

```lammergeier
from lamserver import Server, Request, Response

func main() {
    srv: Server = Server()
    bearerAuth(srv, "s3cret")
    srv.get("/protected", lambda req, res: res.text("ok"))

    # Without auth header → 401
    r1: dict[str, any] = srv.inject("GET", "/protected")
    assert(r1["status"] == 401)

    # With correct header → 200
    h: dict[str, str] = {}
    h["Authorization"] = "Bearer s3cret"
    r2: dict[str, any] = srv.inject("GET", "/protected", "", h)
    assert(r2["status"] == 200)
}
```

`inject` returns `{status: int, body: str, headers: dict[str, str]}`.
Streaming responses fall through `streamFallback` so `res.body` is
populated with the bytes that *would* have been streamed, making the
test path identical to a real request.

For real network tests (TLS, WebSocket, SSE), see the suite under
[`tests/tests/cases/stdlib/test_stdlib_server_*`](https://github.com/thallium-solutions/lammergeier-lang/blob/main/tests/tests/cases/stdlib).

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

inside: dict[str, any] = srv.inject("GET", "/p/inside")
outside: dict[str, any] = srv.inject("GET", "/outside")
insideHeader: str = ""
outsideHeader: str = ""
go! {
    if h, ok := inside["headers"].(map[string]string); ok {
        insideHeader = h["X-Private"]
    }
    if h, ok := outside["headers"].(map[string]string); ok {
        outsideHeader = h["X-Private"]
    }
}
assert(insideHeader == "1")
assert(outsideHeader == "")
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

For options that should only apply to one endpoint, prefer the
`*Opts` family over a global plugin:

```lammergeier
optsCreateUser: dict[str, any] = {}
optsCreateUser["bodyLimit"]    = 8192
optsCreateUser["preHandler"]   = validateUserSchema
optsCreateUser["onResponse"]   = auditUserCreate
optsCreateUser["summary"]      = "Create a user"
optsCreateUser["tags"]         = ["users", "write"]

srv.postOpts("/users", createUserHandler, optsCreateUser)
```

Recognised keys:

- **`bodyLimit`** — int. Returns 413 if the request body exceeds it
  (overrides `Server.bodyLimit` for this route).
- **`preHandler`** — `func(Request, Response)`. Runs *after* every
  global preHandler hook but before the route handler. Can
  short-circuit by populating `res`.
- **`onResponse`** — `func(Request, Response)`. Runs *before* every
  global onResponse hook. Useful for route-specific metrics or
  audit logging.
- **`summary`** / **`tags`** — `str` / `list[str]`. Surfaced via
  `Server.listRoutes()` for OpenAPI generation. Don't affect
  request handling.

---

## Validating request bodies with `lamschema`

Pair `routeOpts.preHandler` with `lamschema` for declarative,
spec-compliant request validation:

```lammergeier
from lamserver import Server, Request, Response
from lamschema import Schema

userCreateSchema: str = """
{
  "type": "object",
  "required": ["name", "email"],
  "properties": {
    "name":  {"type": "string", "minLength": 1, "maxLength": 80},
    "email": {"type": "string", "format": "email"}
  }
}
"""

# Pre-compile once on startup.
Schema.register("user.create", userCreateSchema)

func validateUserCreate(req: Request, res: Response) {
    errs: list[str] = Schema.errorsByKey("user.create", req.body)
    if len(errs) > 0 {
        res.setStatus(400)
        body: dict[str, any] = {}
        body["errors"] = errs
        res.json(body)
    }
}

opts: dict[str, any] = {}
opts["preHandler"] = validateUserCreate
srv.postOpts("/users", createUserHandler, opts)
```

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

`rateLimit` from `lamserver_plugins` is global. To rate-limit a
single endpoint, call it inside a registered prefix:

```lammergeier
func limitedAuth(srv: Server) {
    rateLimit(srv, 5, 60000, "auth too noisy")
    srv.post("/login", loginHandler)
}

srv.register(limitedAuth, "/auth")
```

### Add a request id

A `requestId` plugin is already bundled in `lamserver_plugins`; use this
recipe as a template if you need a custom variant (different header
name, custom ID format, etc.). Pick a unique plugin name to avoid
colliding with the built-in:

```lammergeier
from lamuuid import Uuid

func uuidRequestId(srv: Server) {
    func emit(req: Request, res: Response) {
        id: str = Uuid.v4()
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

## Bundled plugin reference

The current `lamserver_plugins` catalogue, grouped by concern:

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
- `cacheControl(srv, directive="no-store", ifMissing=True)` —
  default `Cache-Control` for every response.

### Operations
- `healthcheck(srv, livePath="/healthz", readyPath="/readyz")`
  plus `markReady(srv)` / `markNotReady(srv)` for
  Kubernetes-style probes.

## See also

- [`lib/lamserver.lam`](https://github.com/thallium-solutions/lammergeier-lang/blob/main/lib/lamserver.lam) — Server / Request / Response classes
- [`lib/lamserver_plugins.lam`](https://github.com/thallium-solutions/lammergeier-lang/blob/main/lib/lamserver_plugins.lam) — bundled plugins
- [`lib/lamserver_ws.lam`](https://github.com/thallium-solutions/lammergeier-lang/blob/main/lib/lamserver_ws.lam) — WebSocket plugin example
- [`lib/lamserver_tus.lam`](https://github.com/thallium-solutions/lammergeier-lang/blob/main/lib/lamserver_tus.lam) — TUS resumable upload plugin
- [`docs/stdlib.md`](#/docs/stdlib) — full standard-library reference
