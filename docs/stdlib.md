# Lammergeier Standard Library Reference

The standard library lives in [`lib/`](../lib) and is imported exactly
like any user module:

```lammergeier
from lammath import Math
from lamstrings import Strings
```

Every module is listed below, organised by topic. Method signatures
are extracted directly from the source — when in doubt, open the
`.lam` file (each is heavily commented) for the authoritative
behaviour.

## Table of contents

- [Stdlib tiers](#stdlib-tiers)
- [Strings & text](#strings--text)
- [Numerics, math & data](#numerics-math--data)
- [Collections & iterators](#collections--iterators)
- [Errors, testing & logging](#errors-testing--logging)
- [Concurrency](#concurrency)
- [Time](#time)
- [OS, filesystem, env, CLI](#os-filesystem-env-cli)
- [Network & web](#network--web)
- [Validation, auth & APIs](#validation-auth--apis)
- [Serialization](#serialization)
- [Crypto, encoding, IDs](#crypto-encoding-ids)
- [Database](#database)
- [Cookbook](#cookbook)
- [Public API index](#public-api-index)

---

## Stdlib tiers

Stdlib tiers are documentation boundaries only. They describe expected
weight and domain ownership; they do not change imports, module names,
or resolution. Import a module with its normal `from lam... import ...`
form regardless of tier.

| Tier | Purpose | Modules |
| --- | --- | --- |
| core | Small, general-purpose modules expected to stay lightweight and broadly useful. | `lamstrings`, `lammath`, `lampath`, `lamjson`, `lamerrors`, `lamenv`, `lamtime`, `lamdatetime`, `lamconv`, `lamfmt`, `lamlog`, `lamtest` |
| data | File formats, tabular/numeric data, and data-shaping helpers. | `lamcsv`, `lamyaml`, `lamxml`, `lamdata`, `lamarray`, `lamstats`, `lamrandom` |
| net | Network protocols, web servers/clients, auth tokens, and wire formats. | `lamhttp`, `lamserver`, `lamserver_plugins`, `lamserver_ws`, `lamserver_tus`, `lamsmtp`, `lamnet`, `lamurl`, `lamjwt`, `lamprotobuf` |
| infra | External services and operational infrastructure. | `lamdb`, `lamredis`, `lamemcached`, `lammigrate`, `lamcron`, `lamexec`, `lamcache` |
| concurrency | Concurrency primitives, async coordination, and resilience helpers. | `lamactor`, `lamconcurrency`, `lamretry`, `lamratelimit`, `lamqueue`, `lamdeque`, `lamheap`, `lamstack`, `lamset`, `lamiter`, `lamsort` |
| encoding & crypto | Encoding, hashing, compression, bytes, UUIDs, templates, and text validation helpers. | `lambase64`, `lambytes`, `lamcompress`, `lamhash`, `lamuuid`, `lamtemplate`, `lamunicode`, `lamre`, `lamschema`, `lamsecurity` |
| CLI & OS | Program entrypoint, filesystem, environment, and host-process helpers. | `lamcli`, `lamos` |

The tiers are intentionally coarse. A module can compose across tiers
at the Lam source level; Phase 8 dependency work tracks whether that
composition accidentally spreads heavy Go dependencies.

---

## Cookbook

This section shows how the stdlib pieces compose in real programs.
The reference sections below list the individual methods; these
recipes show the intended workflow.

### Parse, validate, and propagate with `Result`

Use `try*` APIs when bad input is expected and should stay in normal
control flow. The `?` operator unwraps success values and returns the
same `Result.Err(...)` from the current function on failure.

```lammergeier
from lamconv import Conv
from lamerrors import Result, Error

func parsePort(raw: str) -> Result {
    if raw == "" {
        return Result.Err(Error("ConfigError", "PORT is required"))
    }
    port: int = Conv.tryInt(raw)?
    if port <= 0 or port > 65535 {
        return Result.Err({"kind": "ConfigError", "field": "PORT", "value": raw})
    }
    return Result.Ok(port)
}

func main() {
    do {
        port: int = parsePort("8080")?
        print(f"listening on {port}")
    } catch err {
        print(f"bad config: {err}")
    }
}
```

Use direct `unwrap()` mainly at program edges or tests, where a panic
is an acceptable failure signal. In reusable code, prefer `?`,
`do/catch`, or explicit `r.ok()` checks.

### Build a CLI that reads JSON and writes a report

`Cli` handles flags, `Os` handles files, `Json` handles parsing, and
`Result` keeps IO/parse errors explicit.

```lammergeier
from lamcli import Cli
from lamos import Os
from lamjson import Json
from lamerrors import Result

func loadJson(path: str) -> Result[json] {
    text: str = Os.tryReadFile(path)?
    return Json.tryDecode(text)
}

func main() {
    input: str = Cli.getFlag("input", "data.json")
    pretty: bool = Cli.hasFlag("pretty")

    do {
        value: json = loadJson(input)?
        if pretty {
            print(Json.encodePretty(value))
        } else {
            print(Json.encode(value))
        }
    } catch err {
        print(f"failed: {err}")
    }
}
```

Invocation:

```bash
lamc tools/report.lam --run -- --input=orders.json --pretty
```

### Build a production-style HTTP API

`lamserver` gives you Fastify-like routing, lifecycle hooks,
validation, decorators, schemas, OpenAPI, test injection, TLS,
timeouts, and plugins.

```lammergeier
from lamserver import Server, Request, Response, HttpError
from lamserver_plugins import requestId, requestLog, helmet, metrics

func requireJson(req: Request, res: Response) {
    if not req.is_("json") {
        throw HttpError.badRequest("application/json required")
    }
}

func listUsers(req: Request, res: Response) {
    res.json({
        "data": [{"id": "u_1", "name": "Ada"}],
        "requestId": req.id(),
    })
}

func createUser(req: Request, res: Response) {
    body: json = req.jsonBody()
    name: str = body.name
    res.code(201).json({"id": "u_2", "name": name})
}

func main() {
    srv: Server = Server()
    srv.setRequestTimeout(3000)
    requestId(srv)
    requestLog(srv, "[api]")
    helmet(srv)
    metrics(srv, "/metrics")

    userBody: dict[str, any] = {
        "type": "object",
        "required": ["name"],
        "properties": {"name": {"type": "string", "minLength": 1}},
    }

    srv.getOpts("/users", listUsers, {
        "summary": "List users",
        "tags": ["users"],
    })
    srv.postOpts("/users", createUser, {
        "preHandler": requireJson,
        "schema": {"body": userBody},
        "summary": "Create user",
        "tags": ["users"],
    })

    print(srv.printRoutes())
    print(srv.openapi({"title": "Users API", "version": "1.0.0"}))
    srv.listen(8080)
}
```

Test the same API without binding a port:

```lammergeier
headers: dict[str, str] = {"Content-Type": "application/json"}
out: dict[str, any] = srv.inject("POST", "/users", "{\"name\":\"Grace\"}", headers)
print(out["status"])     # 201
print(out["headers"])    # includes request/security/plugin headers
print(out["body"])
```

### Analyze CSV data with `DataFrame` and `Stats`

Use `DataFrame` for tabular IO, filtering, joins, and group-by; use
`Series` and `Stats` for numeric reductions.

```lammergeier
from lamdata import DataFrame, DataFrameGroups, Series
from lamstats import Stats

func main() {
    csv: str = "city,temp,wind\nRome,20.5,8.0\nRome,22.0,6.0\nMilan,18.0,9.5\n"
    df: DataFrame = DataFrame.readCSV(csv)

    cols: list[str] = ["temp", "wind"]
    rome: DataFrame = df.filterEq("city", "Rome").selectCols(cols)
    temps: Series = rome.col("temp")
    print(temps.mean())

    groupCols: list[str] = ["city"]
    aggTypes: list[str] = ["mean"]
    aggCols: list[str] = ["temp", "wind"]
    groups: DataFrameGroups = df.groupBy(groupCols)
    summary: DataFrame = groups.aggregate(aggTypes, aggCols)
    print(summary.toString())

    values: list[float] = temps.toFloatList()
    print(Stats.percentile(values, 0.95))
}
```

`DataFrame` methods return new values and preserve the receiver,
matching `gota`'s value-oriented design. Check `.error()` after
operations that depend on external data or column names.

### Use SQLite/Postgres/MySQL through `Db`

`lamdb` provides raw SQL, transactions, savepoints, retries, and a
chainable query builder. Placeholder dialects are normalized, so write
`?` in Lam code and let the driver adapter translate where needed.

```lammergeier
from lamdb import Db, QueryBuilder, Tx

func bumpAda(tx: Tx) {
    tx.table("users").whereEq("name", "Ada").increment("age", 1)
}

func main() {
    db: Db = Db.connect("sqlite", "file:app.db?cache=shared")
    defer db.close()

    db.exec("CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, name TEXT, age INTEGER)")
    db.table("users").insert({"name": "Ada", "age": 36})
    db.table("users").insert({"name": "Grace", "age": 30})

    adults: list[dict[str, any]] = (
        db.table("users")
          .select(["id", "name", "age"])
          .where("age", ">=", 18)
          .orderBy("name", "asc")
          .get()
    )
    print(adults)

    ok: bool = db.transaction(bumpAda)
    print(ok)
}
```

Use `tryConnect` / `tryExec` when setup failures should be reported as
`Result`; use throwing methods when failure should abort the current
operation and be caught by `try/catch` or the server error pipeline.

### Cache and retry downstream calls

`LruCache`, `TtlCache`, `Retry`, and `CircuitBreaker` cover the most
common resilience patterns.

```lammergeier
from lamcache import TtlCache
from lamretry import Retry
from lamserver_plugins import CircuitBreaker
from lamerrors import Result

func expensiveFetch() -> Result {
    return Result.Ok("fresh")
}

func downstreamSideEffect() {
    _ = expensiveFetch()
}

func main() {
    cache: TtlCache = TtlCache()
    cached: any = cache.get("user:1")
    if cached == None {
        r: Result = Retry.runFn(expensiveFetch, 3, 50, 500, true)
        if r.ok() {
            cache.put("user:1", r.value, 60)
        }
    }

    breaker: CircuitBreaker = CircuitBreaker(5, 2000)
    guarded: any = breaker.callFn(downstreamSideEffect)  # holds a Result
    print(guarded)
    print(f"breaker state: {breaker.state()}")
}
```

Use TTL caches for remote data, LRU caches for bounded in-memory
memoization, retry for transient errors, and circuit breakers when a
dependency can become unhealthy for a sustained period.

### Test stdlib-heavy code

The `lamtest` module is intentionally small and works well with the
regular `tests/tests/run_tests.py` runner.

```lammergeier
from lamtest import Test
from lamstrings import Strings

func main() {
    Test.describe("slug normalization")
    got: str = Strings.toLower(Strings.replace("Hello World", " ", "-"))
    Test.assertEqual(got, "hello-world", "lowercase hyphen slug")
    Test.assertTrue(Strings.hasSuffix(got, "world"), "suffix")
    Test.summary()
}
```

Embed expected output at the top of the `.lam` file when you want it
covered by the repository runner:

```lammergeier
# expect: PASS lowercase hyphen slug
# expect: PASS suffix
```

---

## Strings & text

### `lamstrings` — `Strings`

UTF-8-safe string utilities. Like Python's `str` methods plus a few
Go-flavoured helpers.

```lammergeier
from lamstrings import Strings
```

| Method | Returns |
|--------|---------|
| `Strings.repeat(s, n)` | `str` |
| `Strings.contains(s, sub)` / `hasPrefix` / `hasSuffix` | `bool` |
| `Strings.toUpper(s)` / `toLower(s)` / `title(s)` / `capitalize(s)` | `str` |
| `Strings.trim(s)` / `trimLeft(s, cutset)` / `trimRight(s, cutset)` | `str` |
| `Strings.replace(s, old, new)` / `replaceFirst(s, old, new)` | `str` |
| `Strings.split(s, sep)` / `splitN(s, sep, n)` / `splitLines(s)` / `fields(s)` | `list[str]` |
| `Strings.join(parts, sep)` | `str` |
| `Strings.count(s, sub)` / `index(s, sub)` / `lastIndex(s, sub)` | `int` |
| `Strings.equalFold(a, b)` | `bool` |
| `Strings.center(s, width, fill=" ")` / `padLeft` / `padRight` / `zfill` | `str` |
| `Strings.reverse(s)` | `str` |
| `Strings.isAlpha(s)` / `isDigit(s)` / `isAlnum(s)` / `isSpace(s)` / `isEmpty(s)` / `isBlank(s)` | `bool` |
| `Strings.startsWith(s, p)` / `endsWith(s, p)` / `containsAny(s, chars)` | `bool` |
| `Strings.indent(s, prefix)` / `dedent(s)` | `str` |
| `Strings.format(template, *args)` — `fmt.Sprintf`-style formatter (`"%s"` / `"%d"` / `"%v"` verbs) | `str` |

`Strings` is also the backing for built-in string-method
dispatch. Every static method in the table above can be used as
a receiver method too:

```lammergeier
func main() {
    slug: str = " Hello Lam ".trim().toLower().replace(" ", "-")
    print(slug)                         # hello-lam

    csv: list[str] = "a,b,c".split(",")
    print("/".join(csv))                 # a/b/c

    print("Lam".equalFold("lam"))        # true
    print("42".padLeft(5, "0"))          # 00042
    print("a\nb".indent("> "))
}
```

The Lam-side method names match the static-method names exactly:
there is no Python-style alias layer mapping `.upper()` to
`.toUpper()`. The compiler rewrites `"hi".toUpper()` into the
same `Strings_toUpper("hi")` call that backs
`Strings.toUpper("hi")`, then auto-injects `lamstrings` into the
import graph so the sugar links without an explicit
`from lamstrings import Strings`.

Most receiver calls put the receiver in the first argument slot:
`s.contains("x")` becomes `Strings.contains(s, "x")`.
`join` is the one intentionally inverted helper because the
natural receiver is the separator: `",".join(parts)` becomes
`Strings.join(parts, ",")`. `trimLeft()` and `trimRight()` also
accept a bare no-argument form; when you omit `cutset`, the
compiler supplies ASCII whitespace (`" \t\n"`). Formatting uses
Go `fmt.Sprintf` verbs, so prefer `"%s"`, `"%d"`, `"%v"`,
`"%.2f"`, and similar Go-style placeholders.

### `lamunicode` — `Unicode`

Rune-level operations.

```lammergeier
from lamunicode import Unicode

n: int = Unicode.runeCount("héllo")    # 5, not 6
runes: list[int] = Unicode.toRunes("ab")
```

| Method | Returns |
|--------|---------|
| `Unicode.runeCount(s)` | `int` |
| `Unicode.isValid(s)` | `bool` |
| `Unicode.isLetter(ch)` / `isDigit(ch)` / `isUpper(ch)` / `isLower(ch)` / `isSpace(ch)` | `bool` |
| `Unicode.toRunes(s)` | `list[int]` |
| `Unicode.fromRunes(runes)` | `str` |

### `lamre` — `Re`

Go's `regexp` package. Every method has a `try*` sibling that returns
a `Result` for explicit error handling.

```lammergeier
from lamre import Re

ok: bool = Re.match("^\\d+$", "12345")
words: list[str] = Re.findAll("\\w+", "two words")
```

| Method | Returns |
|--------|---------|
| `Re.match(pattern, s)` / `tryMatch(...)` | `bool` / `Result` |
| `Re.find(pattern, s)` / `tryFind(...)` | `str` / `Result` |
| `Re.findAll(pattern, s)` / `tryFindAll(...)` | `list[str]` / `Result` |
| `Re.replaceAll(pattern, s, repl)` / `tryReplaceAll(...)` | `str` / `Result` |
| `Re.split(pattern, s)` / `trySplit(...)` | `list[str]` / `Result` |

### `lamfmt` — `Fmt`

Sprintf-style formatting (wraps `fmt`).

```lammergeier
from lamfmt import Fmt

s: str = Fmt.sprintf("%d items @ $%.2f", 5, 9.99)
```

11 helpers covering `sprintf`, `printf`, `println`, `printError`,
hex/binary/octal formatting and pretty-printers. See the source for
the full list.

### `lamcsv` — `Csv`

CSV parser/formatter using Go's `encoding/csv`.

```lammergeier
from lamcsv import Csv

rows: list[list[str]] = Csv.parseAll("a,b\n1,2\n3,4")
out: str = Csv.formatAll([["x", "y"], ["1", "2"]])
```

| Method | Returns |
|--------|---------|
| `Csv.parseAll(content)` | `list[list[str]]` |
| `Csv.formatRow(fields)` | `str` |
| `Csv.formatAll(rows)` | `str` |

### Native `json` and `lamjson` — `Json`

The native `json` type is backed by `lamjson` and only contains real JSON
values: null, booleans, strings, finite numbers, arrays, string-keyed objects,
nested values, or classes implementing `toJson() -> json`. `Json` handles wire
encoding/decoding and explicit conversion to/from ordinary Lam collections.

```lammergeier
from lamjson import Json

obj: json = Json.decode('{"name":"alice","roles":["admin"]}')
name: str = obj.name
obj["active"] = true
out: str = Json.encode(obj)
pretty: str = Json.encodePretty(obj)

asDict: dict[str, any] = Json.toDict(obj)
back: json = Json.fromDict(asDict)
```

| Method | Returns |
|--------|---------|
| `Json.encode(data)` / `Json.tryEncode(data)` | `str` / `Result[str]` |
| `Json.encodePretty(data)` / `Json.tryEncodePretty(data)` | `str` / `Result[str]` |
| `Json.decode(s)` / `Json.tryDecode(s)` | `json` / `Result[json]` |
| `Json.fromValue(value)` / `fromDict(value)` / `fromList(value)` | validated `json` |
| `Json.toDict(data)` / `Json.toList(data)` | `dict[str, any]` / `list[any]` |
| `Json.kind(data)` | `str`: `null`, `boolean`, `string`, `number`, `array`, or `object` |
| `Json.decodeInto(s, target)` / `Json.tryDecodeInto(s, target)` | `None` / `Result` (Go/class compatibility) |
| `Json.isValid(s)` | `bool` |

`Json.fromValue` is the runtime boundary for dynamic `any`; it rejects numeric
map keys, non-finite floats, cycles/excessive nesting, sets, functions, and
classes without `toJson`. Native values implement Go JSON marshaling and
`database/sql` value/scanner interfaces, so they can be passed directly as
JSON/JSONB query parameters.

### `lamurl` — `Url`

URL parsing and percent-encoding.

```lammergeier
from lamurl import Url

host: str = Url.host("https://example.com:8080/path?q=1")
encoded: str = Url.encode("hello world")
```

| Method | Returns |
|--------|---------|
| `Url.parse(rawUrl)` | `str` |
| `Url.scheme(rawUrl)` / `host(rawUrl)` / `path(rawUrl)` | `str` |
| `Url.queryParam(rawUrl, key)` | `str` |
| `Url.encode(s)` / `decode(s)` | `str` |

### `lambase64` — `Base64`

```lammergeier
from lambase64 import Base64

enc: str = Base64.encode("data")
dec: str = Base64.decode(enc)
url: str = Base64.encodeURL("data")  # URL-safe alphabet
```

### `lambytes` — `Bytes`

```lammergeier
from lambytes import Bytes

b: list[int] = Bytes.toBytes("hello")
s: str = Bytes.fromBytes(b)
hx: str = Bytes.hexEncode("abc")
```

---

## Numerics, math & data

### `lammath` — `Math`

39 helpers: trigonometry, exponentials, rounding, GCD/LCM, primality,
combinatorics. Constants are exposed as static functions:
`Math.pi()` and `Math.e()`.

```lammergeier
from lammath import Math

x: float = Math.sin(Math.pi() / 4.0)
y: int = Math.gcd(48, 18)
```

### `lamstats` — `Stats`

Descriptive statistics over `list[float]`.

```lammergeier
from lamstats import Stats

values: list[float] = [1.0, 2.0, 3.0, 4.0]
mean: float = Stats.mean(values)
sd:   float = Stats.stddev(values)
p95:  float = Stats.percentile(values, 0.95)
```

12 helpers: `sum`, `product`, `mean`, `median`, `mode`, `variance`,
`sampleVariance`, `stddev`, `minVal`, `maxVal`, `range_`, `percentile`.

### `lamrandom` — `Random`

PRNG + cryptographic randomness.

```lammergeier
from lamrandom import Random

n: int = Random.randInt(1, 100)
hex_: str = Random.secureToken(16)  # crypto-rand
uuid: str = Random.secureUuid()
```

15 helpers covering `randInt`, `randFloat`, `randIntInclusive`,
`shuffle`, `choice`, `sample`, `randomString`, `randomHex`,
`secureBytes`, `secureToken`, `secureInt`, `secureUuid`, plus seed
utilities.

### `lamarray` — `Array`, `Matrix`

NumPy-style numerics backed by **gonum**.

```lammergeier
from lamarray import Array, Matrix

x: Array = Array.linspace(0.0, 1.0, 100)
y: Array = x.mulScalar(2.0).addScalar(1.0)

A: Matrix = Matrix.fromRows([[1.0, 2.0], [3.0, 4.0]])
b: Array = Array.fromList([5.0, 11.0])
solution: Array = A.solve(b)   # Ax = b via LU
```

`Array` (1-D float64): `zeros`, `ones`, `full`, `arange`, `linspace`,
`fromList`, `random` constructors; element-wise `add`/`sub`/`mul`/`div`,
scalar versions, `neg`/`sqrt`/`exp`/`log`/`absVal`/`pow`; reductions
`sum`/`mean`/`std`/`variance`/`min`/`max`/`argMin`/`argMax`; `dot`,
`norm`, `slice`, `reverse`, `sortAsc`, `equals`, `allClose`,
`toString`, `toList`.

`Matrix` (dense 2-D): `zerosM`, `eye`, `fromList`, `fromRows`
constructors; `transpose`, `addM`/`subM`, `scale`, `matmul`,
`mulVec`, `sumM`/`meanM`, `trace`, `det`, `inverse`, `solve`,
`equals`, `allClose`, `toRows`, `toList`, `toString`.

### `lamdata` — `DataFrame`, `Series`, `DataFrameGroups`

Pandas-style dataframes backed by
[`github.com/go-gota/gota`](https://github.com/go-gota/gota). `DataFrame`
is a typed 2-D table, `Series` is a single typed column, and
`DataFrameGroups` is the lazy handle returned by `DataFrame.groupBy`.

```lammergeier
from lamdata import DataFrame, Series, DataFrameGroups

df: DataFrame = DataFrame.fromRecords([
    ["Country", "Year", "Pop"],
    ["IT",      "2020", "60.0"],
    ["IT",      "2021", "59.5"],
    ["FR",      "2020", "67.4"],
    ["FR",      "2021", "67.6"],
])

# Chainable ops (each returns a new DataFrame — receiver is left
# intact, mirroring gota's value-type semantics).
selCols: list[str] = ["Year", "Pop"]
italy: DataFrame = (
    df.filterEq("Country", "IT")
      .selectCols(selCols)
      .sort("Year", false)
)

# Column-level stats via Series.
pop: Series = df.col("Pop")
total: float = pop.sum()        # 254.5
avg:   float = pop.mean()       # 63.625

# Inner-join plus group-by aggregation.
gdp: DataFrame = DataFrame.fromRecords([
    ["Country", "Gdp"],
    ["IT",      "2.1"],
    ["FR",      "2.9"],
])
joinKey: list[str] = ["Country"]
joined: DataFrame = df.innerJoin(gdp, joinKey)

groupCols: list[str] = ["Country"]
byCountry: DataFrameGroups = df.groupBy(groupCols)
aggTypes: list[str] = ["mean"]
aggCols:  list[str] = ["Pop"]
means: DataFrame = byCountry.aggregate(aggTypes, aggCols)
```

**DataFrame surface.** Constructors `fromRecords` (header + rows),
`fromMaps`, `readCSV`, `readJSON`, and native `readJsonValue(json)`.
Introspection `nrow`, `ncol`,
`dims`, `names`, `types`, `describe`, `toString`, `error`. Access
`col`, `elem`, `records`, `maps`, `copy`. Subsetting `head`, `tail`,
`slice`, `subset`, `selectCols`, `dropCols`. Mutation `rename`,
`setNames`, `mutate` / `mutateInts` / `mutateFloats` /
`mutateStrings` / `mutateBools` (typed variants because Lam emits
`list[int]` as `[]int` — use the matching mutate function when you
already have a typed list). Filtering `filter(col, op, value)` plus
sugar `filterEq` / `filterNeq` / `filterGt` / `filterGte` /
`filterLt` / `filterLte` / `filterIn`. Sorting `sort` and multi-key
`sortBy`. Joins `innerJoin`, `leftJoin`, `rightJoin`, `outerJoin`,
`crossJoin`. Combine `rbind`, `cbind`, `concat`. I/O `writeCSV`,
`writeJSON`, and `jsonValue() -> json`. Group-by via
`groupBy(cols) -> DataFrameGroups`.

**Series surface.** `fromStrings` / `fromInts` / `fromFloats` /
`fromBools` constructors. Introspection `name`, `typeName`,
`length`, `records`, `isNaN`, `error`, `toString`. Access `at(i)`,
`elemStr(i)`. Aggregations `sum`, `mean`, `median`, `stddev`,
`minVal`, `maxVal`, `quantile(p)`. Conversion `toStringList`,
`toIntList`, `toFloatList`, `toBoolList`. Derived `copy`, `slice`,
`order(reverse)`.

**DataFrameGroups surface.** `aggregate(types, cols)` (strategies
`"max"`, `"min"`, `"mean"`, `"median"`, `"std"`, `"sum"`,
`"count"`), `getGroups()` (map of group-key → partition),
`error()`.

**Error handling.** Every operation that can fail stores its error
on the returned DataFrame / Series / Groups rather than panicking;
use `.error()` to read it (empty string means success). This
matches the upstream `gota` convention.

**Typed-list convention.** Lam emits `list[str]` as `[]string` and
`list[int]` as `[]int`. When a method takes `list[str]`, pass a
pre-declared typed variable (inline literals compile to
`[]interface{}`). See the example above for the canonical shape.

### `lamconv` — `Conv`

String ↔ number conversions with `try*` siblings for recoverable
parsing.

```lammergeier
from lamconv import Conv

n: int = Conv.toInt("42")
hx: str = Conv.toHex(255)        # "ff"
ok: bool = Conv.toBool("true")
```

16 helpers: `toInt`, `tryInt`, `toFloat`, `tryFloat`, `toString`,
`floatToString`, `intToBase`, `toBool`, `tryBool`, `boolToString`,
`toHex`/`toOctal`/`toBin`, `fromHex`/`fromOctal`/`fromBin`.

---

## Collections & iterators

### `lamstack` — `Stack`

LIFO. `push`, `pop`, `peek`, `size`, `isEmpty`, `clear`, `toList`.

### `lamqueue` — `Queue`

FIFO. `enqueue`, `dequeue`, `peek`, `size`, `isEmpty`, `clear`, `toList`.

### `lamdeque` — `Deque`

Double-ended. `pushBack`/`pushFront`, `popBack`/`popFront`,
`peekBack`/`peekFront`, `size`, `isEmpty`, `clear`, `toList`.

### `lamset` — `Set`

Hashed set with stable insertion order.

```lammergeier
from lamset import Set

s: Set = Set()
s.add("alice")
s.addAll(["bob", "carol"])
ok: bool = s.contains("alice")     # true
```

`add`, `addAll`, `remove`, `contains`, `size`, `isEmpty`, `clear`,
`toList`, `union`, `intersect`, `difference`, `isSubsetOf`, `equals`.

### `lamheap` — `Heap`, `PriorityHeap`

Binary heaps backed by `container/heap`.

```lammergeier
from lamheap import Heap, PriorityHeap

h: Heap = Heap()                   # min-heap by default
h.push(3); h.push(1); h.push(2)
smallest: float = h.pop()           # 1.0

pq: PriorityHeap = PriorityHeap(true)   # max-heap
pq.push("urgent", 9.0)
pq.push("later",  1.0)
top: str = pq.pop()                # "urgent"
```

`push`, `pop`, `peek`, `size`, `isEmpty`. Switch min/max via the
`maxHeap` boolean to the constructor.

### `lamcache` — `LruCache`, `TtlCache`

Goroutine-safe in-memory caches.

```lammergeier
from lamcache import LruCache, TtlCache

lru: LruCache = LruCache(100)        # capacity
lru.put("k", "v")
v: any = lru.get("k")

ttl: TtlCache = TtlCache()
ttl.put("session", token, 3600)      # expires in 1 hour
ttl.putForever("config", cfg)
```

`LruCache`: `get`, `put`, `contains`, `remove`, `size`, `clear`.
`TtlCache`: `put`, `putForever`, `get`, `contains`, `remove`, `size`,
`purgeExpired`.

### `lamiter` — `Iter`

Lazy iterator combinators.

```lammergeier
from lamiter import Iter

evens: list[int] = (Iter.range_(0, 100)
    .filter(lambda x: x % 2 == 0)
    .take(5)
    .toList())
```

Constructors: `fromList`, `range_`, `count`, `repeat`.
Combinators: `map`, `filter`, `take`, `drop`, `takeWhile`, `dropWhile`,
`enumerate`, `chain`.
Terminals: `toList`, `forEach`, `reduce`, `count_`, `first`,
`anyMatch`, `allMatch`.

### `lamsort` — `Sort`

In-place sort utilities.

```lammergeier
from lamsort import Sort

asc: list[int] = Sort.ints([3, 1, 2])      # [1, 2, 3]
rev: list[str] = Sort.reverseStrings(["a", "b"])
```

`ints`, `floats`, `strings`, `reverseInts`, `reverseFloats`,
`reverseStrings`, `reverse` (any), `isSorted`.

---

## Errors, testing & logging

### `lamerrors` — `Error`, `Result`

Tagged errors plus a `Result` monad.

```lammergeier
from lamerrors import Result, Error

func divide(a: int, b: int) -> Result {
    if b == 0 {
        return Result.Err(Error("ZeroDivisionError", "b == 0", None))
    }
    return Result.Ok(a / b)
}

fallback: any = divide(10, 0).unwrapOr(0) # 0
value: any = divide(10, 2).unwrap()       # 5
```

`Error(kind, message, cause=None)` stores `.kind`, `.message`, and
`.cause`; `str(error)` renders `Kind: message` and appends the cause
when present.

`Result` is non-generic at runtime. Both `.value` and `.error` are typed
`any`, while signatures may use one erased payload marker such as
`Result[int]` to improve Lam-side propagation inference. The implemented
runtime API is:

| Signature | Meaning |
|---|---|
| `Result.Ok(v: any) -> Result` | Success. Stores `v` in `.value` and `None` in `.error`. |
| `Result.Err(e: any) -> Result` | Failure. Stores `None` in `.value` and `e` in `.error`. |
| `r.ok() -> bool` | `true` when `.error == None`. |
| `r.unwrap() -> any` | Returns `.value`; throws `RuntimeError` if this is an error. |
| `r.unwrapOr(fallback: any) -> any` | Returns `.value`, or `fallback` on error. |

`Result.Err` accepts any value because `.error` is `any`: structured
`Error` objects, plain strings, dicts, sentinel objects, Go errors
captured through `go!`, or domain payloads.

```lammergeier
from lamerrors import Result, Error

bad1: Result = Result.Err(Error("ParseError", "bad integer"))
bad2: Result = Result.Err("missing API key")
bad3: Result = Result.Err({"field": "email", "reason": "invalid"})

if not bad3.ok() {
    print(bad3.error)
}
```

Direct `.value`, `.error`, `unwrap()`, and `unwrapOr(...)` reads are
`any`. Keep them as `any` when forwarding/logging, use `?` into an
annotated local when composing `Result`-returning functions, or use a
small `go!` type assertion when you intentionally need a concrete
Go type from a direct unwrap. The postfix `?` operator and
`do { } catch err { }` block are documented in
[`SYNTAX.md`](SYNTAX.md#errors--results).

### `lamtest` — `Test`

Assertion library used by `tests/tests/run_tests.py`.

```lammergeier
from lamtest import Test

func main() {
    Test.describe("math operations")
    Test.assertEqual(2 + 2, 4)
    Test.assertAlmostEqual(0.1 + 0.2, 0.3)
    Test.summary()
}
```

`assertTrue`, `assertFalse`, `assertEqual`, `assertNotEqual`,
`assertNil`, `assertNotNil`, `assertLen`, `assertContains`,
`assertAlmostEqual`, `pass`, `fail`, `describe`, `passed()`,
`failed()`, `reset()`, `summary()`.

### `lamlog` — `Log`

Levelled logging on top of `log`.

```lammergeier
from lamlog import Log

Log.setLevel("info")
Log.info("server starting")
Log.error("could not connect", "db unreachable")
```

11 helpers: `setLevel`, `getLevel`, `debug`, `info`, `warn`, `error`,
`fatal`, `withField`, `withFields`, `setOutput`, `setPrefix`.

---

## Concurrency

### `lamconcurrency` — `Channel`, `WaitGroup`, `Mutex`, `RWMutex`, `Atomic`

Goroutine-safe primitives wrapping Go's `sync` package.

```lammergeier
from lamconcurrency import Channel, WaitGroup

ch: Channel = Channel(10)
wg: WaitGroup = WaitGroup()

func worker(id: int) {
    ch.send(f"worker {id} done")
    wg.done()
}

wg.add(3)
go! { go Worker(1); go Worker(2); go Worker(3) }
wg.wait()
```

`Channel`: `send`, `recv`, `tryRecv`, `close`.
`WaitGroup`: `add`, `done`, `wait`.
`Mutex` / `RWMutex`: `lock`, `unlock`, plus `rLock`/`rUnlock` on RW.
`Atomic`: `get`, `set`, `increment`, `decrement`, `add`,
`compareAndSwap` over `int64`.

### `lamactor` — `Mailbox`, `ActorRef`, `ActorSystem`

Higher-level message-passing on top of `lamconcurrency`. Each actor
owns a goroutine, a private mailbox, and any state it captures. The
dispatch loop processes one message at a time so handlers never need
their own locks. Inspired by [`go-actor`](https://github.com/vladopajic/go-actor),
reshaped around an Erlang/Akka-style `onMessage` convention.

```lammergeier
from lamactor import ActorRef, ActorSystem

class Counter {
    func __init__(self) {
        self.n: int = 0
    }

    func onMessage(self, msg: any) -> any {
        if msg == "inc" { self.n = self.n + 1; return self.n }
        if msg == "get" { return self.n }
        return None
    }
}

sys: ActorSystem = ActorSystem()
ref: ActorRef = sys.spawn(Counter())
ref.tell("inc")
ref.tell("inc")
total: any = ref.ask("get", 1000)        # blocks up to 1s for reply
print(total)                             # 2
sys.shutdown(1000)
```

An actor only needs `onMessage(self, msg)`; the dispatcher also calls
`onStart(self)` and `onStop(self)` if defined, plus `onError(self,
err) -> bool` to opt-in to panic recovery (return `True` to stay
alive). All hooks are looked up reflectively, so adding one is a
purely additive change.

`Mailbox(capacity=0)` is the standalone primitive: `send`, `trySend`,
`recv`, `tryRecv`, `close`, `isClosed`, `size`. `capacity=0` selects
an unbounded queue (a forwarder goroutine grows a slice on demand);
any positive capacity uses a bounded buffered channel.

`ActorRef`: `tell` (fire-and-forget), `ask(msg, timeoutMs)` (returns
`None` on timeout or stopped actor), `stop`, `isAlive`. Refs are
plain data — passing them between actors is how multi-stage
pipelines compose.

`ActorSystem`: `spawn(actor)`, `spawnNamed(name, actor)`, `find(name)`
(returns an inert ref when the name is unknown — check `isAlive()`
before sending), `numActors()`, `shutdown(timeoutMs)`.

---

## Time

### `lamtime` — `Time`

Unix epoch focused. Best for measuring durations.

```lammergeier
from lamtime import Time

start: int = Time.nowUnixMilli()
work()
elapsed: int = Time.elapsedSince(start)   # ms

formatted: str = Time.format(Time.nowUnix(), "2006-01-02 15:04:05")
```

18 helpers: `nowUnix`, `nowUnixMilli`, `nowString`, `nowRfc3339`,
`nowYear`/`nowMonth`/`nowDay`/`nowHour`/`nowMinute`/`nowSecond`/
`nowWeekday`, `sleepMs`, `sleepSec`, `format`, `parse`, `addSeconds`,
`elapsedSince`, `measureMs(fn)`.

### `lamdatetime` — `DateTime`

Calendar-aware helpers.

```lammergeier
from lamdatetime import DateTime

today: str = DateTime.today()              # "2024-12-15"
weekday: str = DateTime.weekday()          # "Sunday"
leap: bool = DateTime.isLeapYear(2024)
```

10 helpers: `now`, `today`, `timestamp`, `fromTimestamp`,
`diffSeconds`, `year`, `month`, `day`, `weekday`, `isLeapYear`.

### `lamcron` — `Cron`

Cron-style scheduler wrapping
[`github.com/robfig/cron/v3`](https://pkg.go.dev/github.com/robfig/cron/v3).
Use `Cron.new()` for standard 5-field expressions, `Cron.newWithSeconds()`
when you need sub-minute resolution.

```lammergeier
from lamcron import Cron

c: Cron = Cron.new()
id: int = c.schedule("*/5 * * * *", lambda: print("tick"))
c.start()            # non-blocking; scheduler runs in its own goroutine
# …work…
c.stop()             # synchronous — waits for in-flight jobs
```

Accepted expressions: standard 5-field (`min hour dom mon dow`),
6-field (`sec min hour dom mon dow`) via `newWithSeconds()`, and
`@daily` / `@hourly` / `@every 30s` descriptors. Malformed
expressions surface as a panic from `schedule` so ``try/catch``
can isolate the failure.

| Method | Returns | Notes |
|--------|---------|-------|
| `Cron.new()` / `Cron.newWithSeconds()` | `Cron` | Parser differs; runtime is identical. |
| `c.schedule(expr, fn)` | `int` | Entry ID; pass to `remove`. ``fn`` is any zero-arg Lam callable. |
| `c.start()` / `c.stop()` | — | Idempotent. `stop` blocks until running jobs finish. |
| `c.remove(id)` | — | Silent no-op for unknown IDs. |
| `c.count()` / `c.entryIds()` | `int` / `list[int]` | Snapshot view. |

Jobs that panic are recovered automatically and logged to stderr,
so one misbehaving callback can't take the scheduler down.

---

## OS, filesystem, env, CLI

### `lamos` — `Os`

File and process I/O. The `try*` siblings return `Result` so callers
can decide whether to swallow errors or propagate.

```lammergeier
from lamos import Os

content: str = Os.readFile("/etc/hostname")
Os.writeFile("/tmp/log", "hello\n")
files: list[str] = Os.walk("/tmp")
```

33 helpers: env (`getenv`/`setenv`), filesystem (`readFile`,
`writeFile`, `appendFile`, `tryReadFile`, `tryWriteFile`, `readLines`,
`writeLines`, `tryReadLines`, `tryWriteLines`, `copyFile`,
`fileExists`, `isFile`, `isDir`, `fileSize`, `mkdir`, `remove`,
`removeAll`, `rename`, `listDir`, `listFiles`, `listDirs`, `walk`,
`tempDir`, `tempFile`), process (`exit`, `args`, `getcwd`,
`hostname`, `user`, `readStdin`, `readStdinLine`).

### `lampath` — `Path`

Filepath helpers.

```lammergeier
from lampath import Path

p: str = Path.join("a", "b", "c.txt")           # a/b/c.txt
ext: str = Path.ext("file.tar.gz")              # ".gz"
parts: list[str] = Path.split("/a/b/c")
```

13 helpers: `join`, `dir`, `base`, `ext`, `split`, `clean`,
`isAbs`, `abs`, `tryAbs`, `relative`, `tryRelative`, `withExt`,
`stripExt`.

### `lamenv` — `Env`, `Dotenv`, `Config`

Environment variables, dotenv parsing, and unified config-file
loading in one module.

```lammergeier
from lamenv import Env, Dotenv, Config

# Plain env access
port: str = Env.getOr("PORT", "8080")
if Env.has("DEBUG") { print("debug mode") }

# Dotenv — supports export, quoted values, escapes, and ${VAR}
#          interpolation (both file-local and process env).
entries: dict[str, any] = Dotenv.parseFile(".env")
applied: int = Dotenv.load(".env")          # don't override live env
applied_: int = Dotenv.loadOverride(".env") # file wins

# Config picks a decoder from the extension (.env, .json, .yaml,
# .yml, .toml) and returns a dict.
cfg: dict[str, any] = Config.load("config.toml")
merged: dict[str, any] = Config.merge([
    Config.load("config/defaults.yaml"),
    Config.load("config/prod.yaml"),
    Dotenv.parseFile(".env"),
])
```

- **Env**: `get`, `getOr`, `set`, `unset`, `has`, `all`.
- **Dotenv**: `parse`, `parseFile`, `load`, `loadOverride`,
  `applyToEnv`. The parser handles `export KEY=val`, single- and
  double-quoted values (with `\n` / `\t` / `\"` / `\$` escapes in
  double quotes), triple-quoted multi-line values, inline
  `# comments`, and `${VAR}` / `$VAR` interpolation resolved
  against already-parsed entries then the live environment.
- **Config**: `tryLoad` (returns a `Result`), `load` (returns an
  empty dict on failure), and `merge` for stacking sources. TOML
  parsing is backed by `github.com/BurntSushi/toml` and YAML by
  `gopkg.in/yaml.v3`.

### `lamcli` — `Cli`

Command-line flag parsing.

```lammergeier
from lamcli import Cli

verbose: bool = Cli.hasFlag("verbose")            # --verbose
out: str = Cli.getFlag("out", "default.txt")     # --out=...
n: int = Cli.getInt("n", 10)
files: list[str] = Cli.positional()              # everything else
```

8 helpers: `args`, `program`, `argCount`, `arg(idx)`, `hasFlag`,
`getFlag`, `getInt`, `positional`.

### `lamexec` — `Exec`

Run external processes.

```lammergeier
from lamexec import Exec

out: str = Exec.run("git status --short")
status: int = Exec.runSilent("git status --short")
files: str = Exec.output("ls", ["-la", "/tmp"])
```

3 helpers: `run(command)`, `runSilent(command)`, `output(name, args)`.

---

## Network & web

### `lamhttp` — `Http`, `HttpServer`

HTTP **client** plus a tiny blocking server for one-off scripts.
Use [`lamserver`](#lamserver--server-request-response-sseemitter)
for real applications.

```lammergeier
from lamhttp import Http

body: str = Http.get("https://api.example.com/users")
status: int = Http.statusCode("https://example.com")
out: str = Http.postJson("https://api.example.com/echo", '{"x":1}')
nativeOut: str = Http.postJsonValue("https://api.example.com/echo", {"x": 1})
```

`postJson` / `tryPostJson` retain the already-encoded string form;
`postJsonValue` / `tryPostJsonValue` accept native `json` and encode it.
`Http`: `get`, `tryGet`, `post`, `tryPost`, `postJson`, `tryPostJson`,
`postJsonValue`, `tryPostJsonValue`, `getWithHeaders`, `getHeader`,
`statusCode`, `serve`.
`HttpServer.close()` stops the throwaway server.

### `lamserver` — `Server`, `Request`, `Response`, `SseEmitter`, `HttpError`

Fastify-style HTTP server with the full lifecycle ladder, schema-
based validation, decorators, plugin encapsulation, content-type
parsers, a trie router, app-lifecycle hooks, OpenAPI generation,
streaming, SSE, signed cookies, an in-process test harness, and TLS.

```lammergeier
from lamserver import Server, Request, Response, HttpError

func helloHandler(req: Request, res: Response) {
    name: str = req.queryGet("name", "world")
    res.text(f"hello, {name}")
}

func main() {
    srv: Server = Server()
    srv.useCors()
    srv.get("/hello", helloHandler)
    srv.listen(8080)
}
```

Routes can be as small as a handler function, or they can carry
Fastify-style options for validation, route-local hooks, metadata,
timeouts, streaming, and response serialization. `Server.inject`
runs the same dispatcher in-process, which makes server examples and
tests deterministic:

```lammergeier
from lamserver import Server, Request, Response

func auth(req: Request, res: Response) {
    if req.header("Authorization") != "Bearer dev" {
        res.code(401).json({"error": "unauthorized"})
    }
}

func createUser(req: Request, res: Response) {
    body: json = req.jsonBody()
    name: str = body.name
    res.code(201).json({
        "id": "u_1",
        "name": name,
        "internal": "not in response",
    })
}

func main() {
    srv: Server = Server()
    srv.addSchema({
        "$id": "userName",
        "type": "string",
        "minLength": 1,
    })

    srv.postOpts("/users", createUser, {
        "preHandler": auth,
        "summary": "Create a user",
        "tags": ["users"],
        "schema": {
            "body": {
                "type": "object",
                "required": ["name"],
                "properties": {"name": {"$ref": "userName"}},
            },
            "response": {
                "201": {
                    "type": "object",
                    "additionalProperties": false,
                    "properties": {
                        "id": {"type": "string"},
                        "name": {"type": "string"},
                    },
                },
            },
        },
    })

    headers: dict[str, str] = {
        "Authorization": "Bearer dev",
        "Content-Type": "application/json",
    }
    out: dict[str, any] = srv.inject("POST", "/users", "{\"name\":\"Ada\"}", headers)
    print(out["status"])   # 201
    print(out["body"])     # response schema strips "internal"
}
```

### `lamserver` mental model

`lamserver` follows the same shape as Fastify:

- A `Server` owns routes, hooks, decorators, schemas, parsers, and
  app-lifecycle callbacks.
- A route is a method + path + handler, optionally with route-local
  options.
- Hooks form a request pipeline. A hook can short-circuit by writing a
  response.
- Plugins are ordinary functions that receive a `Server` and register
  routes/hooks/decorators.
- `Server.inject(...)` runs the same dispatcher in memory, so tests
  exercise the real pipeline without opening a socket.

The typical startup flow is:

```lammergeier
func main() {
    srv: Server = Server()

    # Server-wide behavior.
    srv.trustProxy(["X-Forwarded-For"], 1)
    srv.setRequestTimeout(5000)
    srv.useCors()
    srv.useSecurityHeaders()

    # Plugins, then schemas, then routes.
    requestId(srv)
    metrics(srv, "/metrics")
    srv.addSchema({"$id": "Id", "type": "string", "minLength": 1})

    srv.get("/health", healthHandler)
    srv.postOpts("/orders", createOrderHandler, createOrderOpts())

    # Startup visibility.
    print(srv.printRoutes())
    srv.listen(8080)
}
```

### Routing guide

Use method helpers for normal routes and `route` / `routeOpts` when
the method is dynamic:

```lammergeier
srv.get("/users", listUsers)
srv.post("/users", createUser)
srv.put("/users/:id", replaceUser)
srv.patch("/users/:id", patchUser)
srv.del("/users/:id", deleteUser)
srv.all("/debug/*", debugCatchAll)
srv.route("OPTIONS", "/custom", customOptions)
```

Path parameters are stored with their marker included:

```lammergeier
func getUser(req: Request, res: Response) {
    id: str = req.params[":id"]
    res.json({"id": id})
}

func getFile(req: Request, res: Response) {
    rest: str = req.params["*"]
    res.text(rest)
}

srv.get("/users/:id", getUser)
srv.get("/files/*", getFile)
```

`HEAD` requests use the normal route pipeline. If a `HEAD` route is
registered it wins; otherwise server behavior follows the explicit
route table rather than inventing a hidden `GET` fallback.

### Request and response workflow

Request helpers normalize common HTTP access:

```lammergeier
func inspect(req: Request, res: Response) {
    ct: str = req.header("Content-Type")
    page: str = req.queryGet("page", "1")
    sid: str = req.cookie("sid")
    signed: str = req.signedCookie("sid", "secret")
    ip: str = req.realIP()
    scheme: str = req.realScheme("http")

    res.header("X-Request-Id", req.id())
    res.json({
        "contentType": ct,
        "page": page,
        "hasSession": sid != "",
        "signedSession": signed,
        "ip": ip,
        "scheme": scheme,
    })
}
```

Response methods are chainable, so handlers can read like Fastify
reply code:

```lammergeier
func created(req: Request, res: Response) {
    res.code(201)
       .type_("application/json")
       .header("Location", "/items/123")
       .json({"id": "123"})
}

func download(req: Request, res: Response) {
    res.streamFile("/var/reports/daily.csv", "text/csv")
}

func redirectOld(req: Request, res: Response) {
    res.redirect("/new-path", 301)
}
```

### Route options in practice

Use `*Opts` routes when behavior belongs to one endpoint rather than
the whole server.

```lammergeier
func requireAuth(req: Request, res: Response) {
    if req.header("Authorization") == "" {
        res.code(401).json({"error": "missing token"})
    }
}

func afterAudit(req: Request, res: Response) {
    print(f"{req.routerMethod()} {req.routerPath()} -> {res.status}")
}

func upload(req: Request, res: Response) {
    reader: RequestBodyReader = req.bodyReader()
    n: int = reader.copyTo("/tmp/upload.bin")
    res.json({"bytes": n})
}

srv.routeOpts("POST", "/upload", upload, {
    "preHandler": requireAuth,
    "onResponse": afterAudit,
    "bodyLimit": 20_000_000,
    "streamBody": true,
    "timeoutMs": 10000,
    "summary": "Upload a binary object",
    "tags": ["uploads"],
})
```

Route-local hooks run in the same phase as global hooks. They are the
right place for endpoint-specific authentication, audit logging,
serialization tweaks, and validation that should not affect sibling
routes.

### Schemas and OpenAPI

Schemas are JSON Schema draft-07. Put route input under `body`,
`querystring`, `params`, or `headers`; put output under `response`.
Response schemas are status-keyed. Exact status wins, then status
family, then `default`.

Internally, lamserver still marshals dictionary-defined schemas to JSON text and
unmarshals schema documents while resolving `$ref`, projecting responses, and
building OpenAPI. Compiled validation is delegated to `gojsonschema`; request
JSON reaches it as decoded native/raw JSON data. This marshal/unmarshal boundary
is intentional because JSON Schema and OpenAPI are wire specifications, while
application payloads use Lam's native `json`. Malformed literal schema strings
passed directly to `Schema.register` or `Server.addSchema` are diagnosed by the
Lam compiler; dynamically constructed schemas remain runtime-validated by the
schema compiler (an invalid shared registration is ignored and unresolved route
validation surfaces through the normal validation error path).

```lammergeier
func createOrderOpts() -> dict[str, any] {
    return {
        "operationId": "createOrder",
        "summary": "Create an order",
        "description": "Validates the body and returns the public order view.",
        "tags": ["orders"],
        "schema": {
            "body": {
                "type": "object",
                "required": ["sku", "quantity"],
                "properties": {
                    "sku": {"type": "string", "minLength": 1},
                    "quantity": {"type": "integer", "minimum": 1},
                },
            },
            "response": {
                "201": {
                    "type": "object",
                    "additionalProperties": false,
                    "properties": {
                        "id": {"type": "string"},
                        "sku": {"type": "string"},
                        "quantity": {"type": "integer"},
                    },
                },
                "400": {
                    "type": "object",
                    "properties": {"error": {"type": "string"}},
                },
            },
        },
    }
}
```

Register shared schemas once and reference them with `$ref`:

```lammergeier
srv.addSchema({
    "$id": "PaginationQuery",
    "type": "object",
    "properties": {
        "page": {"type": "integer", "minimum": 1},
        "perPage": {"type": "integer", "minimum": 1, "maximum": 100},
    },
})

srv.getOpts("/orders", listOrders, {
    "schema": {"querystring": {"$ref": "PaginationQuery"}},
    "summary": "List orders",
    "tags": ["orders"],
})
```

Export an OpenAPI document from the route table:

```lammergeier
spec: str = srv.openapi({
    "title": "Orders API",
    "version": "1.0.0",
    "description": "Internal order-management API",
})
```

### Custom parsers

`addContentTypeParser(mediaType, fn)` lets a server accept custom
request bodies. The parser receives the raw body string and returns
any value; handlers read the result through `req.parsedBody()`.

```lammergeier
from lamstrings import Strings

func parseLineBody(body: str) -> any {
    return {"lines": Strings.splitLines(body)}
}

func ingest(req: Request, res: Response) {
    parsed: any = req.parsedBody()
    res.json(parsed)
}

srv.addContentTypeParser("text/x-lines", parseLineBody)
srv.post("/ingest", ingest)
```

Use custom parsers for line protocols, signed webhooks, or vendor
media types. Keep parsers small; expensive validation belongs in
`preValidation` or route schemas.

### Decorators and shared state

Decorators are per-server storage for values a plugin or route needs
later. They are split across server, request, and response scopes:

```lammergeier
srv.decorate("serviceName", "orders")
srv.decorateRequest("user", None)
srv.decorateResponse("view", "public")

func whoami(req: Request, res: Response) {
    service: any = srv.dec("serviceName")
    user: any = req.dec("user")
    view: any = res.dec("view")
    res.json({"service": service, "user": user, "view": view})
}
```

Plugins usually prefer `req.ctx` for per-request values and
decorators for server-wide values or default request/reply slots.
Use a unique prefix such as `__myPluginName` for internal keys.

### Error handling

Throw `HttpError` when a hook or handler needs to abort with an HTTP
status. Plain panics are converted to a 500 envelope; `HttpError`
keeps the intended status and optional data.

```lammergeier
func requireAdmin(req: Request, res: Response) {
    if req.header("X-Admin") != "1" {
        throw HttpError.forbidden("admin required", {"route": req.routerPath()})
    }
}

func errorHandler(err: any, req: Request, res: Response) {
    res.code(500).json({
        "error": "internal",
        "requestId": req.id(),
    })
}

srv.preHandler(requireAdmin)
srv.setErrorHandler(errorHandler)
```

Use `onError` hooks for observation and `setErrorHandler` when you
want to own the response body. Keep error handlers defensive: if they
panic, the built-in fallback takes over.

### Testing and inspection

`inject` returns a dict with `status`, `body`, and `headers`.
Headers can be passed as `dict[str, str]`.

```lammergeier
func testCreateOrder() {
    srv: Server = Server()
    srv.postOpts("/orders", createOrder, createOrderOpts())

    h: dict[str, str] = {"Content-Type": "application/json"}
    out: dict[str, any] = srv.inject("POST", "/orders", "{\"sku\":\"A\",\"quantity\":2}", h)
    assert(out["status"] == 201)
    assert(out["body"] != "")
}
```

Use `printRoutes()` for human-readable startup output and
`listRoutes()` for tooling:

```lammergeier
print(srv.printRoutes())
routes: list[any] = srv.listRoutes()
```

### Production checklist

- Set `trustProxy(...)` only when the process is actually behind a
  trusted reverse proxy.
- Install `requestId`, `requestLog`, `helmet`, `metrics`, and
  `serverTiming` early.
- Use route schemas for public APIs and `setSchemaErrorFormatter` for
  stable validation envelopes.
- Prefer `register(plugin, prefix, encapsulate=true)` for feature
  modules that own routes and private hooks.
- Set `setRequestTimeout(...)` globally, override with `timeoutMs` on
  slow routes, and use `onTimeout` to tag timeout responses.
- Use `listenTLS` only when the Lam process terminates TLS directly;
  behind a proxy, terminate TLS at the proxy and use `listen`.

**`Request`** — `.method`, `.path`, `.body`, `.headers`, `.query`,
`.params`, `.remoteAddr`, `.ctx` (plugin-shared dict). Methods:
`header(name)`, `queryGet(name, fallback)`, `cookie(name)`,
`signedCookie(name, secret)`, `jsonBody() -> json`, `parsedBody()` (returns native
`json` for `application/json` and dynamic form/text values otherwise),
`formField(name, fallback)`, `formFile(name)`, `formFiles(name)`,
`realIP()`, `realScheme(fallback="http")`, `dec(name)`,
`hasDec(name)`, `id()`, `setId(value)`.

Fastify-parity sugar:
`protocol()` (alias over `realScheme`), `hostname()` (Host header
minus port), `ips()` (full X-Forwarded-For / Forwarded chain when
`trustProxy` is enabled, else `[remoteAddr]`),
`routeOptions()` (matched route's metadata dict — `method`,
`path`, `summary`, `tags`, `meta`, …), `startTime()` (Unix
milliseconds captured when the dispatcher first saw this
request), `routerPath()` / `routerMethod()` (matched route
pattern + normalised verb — `""` when the request 404'd),
`is_(type)` (case-insensitive `Content-Type` substring match,
accepts either `"application/json"` or the short form `"json"`),
`url()` (reconstructs `scheme://host/path?query`, honouring
`X-Forwarded-Proto` when present).

**`Response`** — `.status`, `.headers`, `.body`. Methods:
`setStatus(code)` / `code(n)`, `setHeader(k, v)` / `header(k, v)`,
`type_(ct)`, `text(body)`, `json(obj)`, `html(body)`, `send(body)`,
`redirect(url, code=302)`, `paginate(total, page, perPage, basePath="")` (sets `X-Total-Count` + `X-Pagination-*` + RFC 5988 `Link`), `cookie(name, val, path="/", maxAge=0)`,
`signedCookie(name, val, secret, ...)`, `error_(code, msg="")`,
`streamFile(path, ct="")`, `sse(emitterFn)`,
`push(target, contentType="")` (HTTP/2 server push, best-effort),
`dec(name)`, `hasDec(name)`.

Fastify-parity sugar:
`getHeader(name)`, `getHeaders()` (snapshot copy),
`hasHeader(name)`, `removeHeader(name)`, `setHeaders({...})`
(batch setter — Fastify's `reply.headers({...})`), `hijack()`
(skip remaining lifecycle + writer — handler owns `rawWriter`
from here), `elapsedTime()` / `getResponseTime()` (alias;
milliseconds since the dispatcher first saw the associated
request), `callNotFound()` (delegate to the registered
`notFoundHandler` — Fastify's `reply.callNotFound()`).

**`SseEmitter`** — `send(data)`, `sendEvent(event, data)`,
`comment(text)`, `close()`.

**`HttpError`** — typed HTTP error. `panic(HttpError(409, "dup",
{"key": x}))` from a hook or handler short-circuits with a structured
JSON response. Static shortcuts: `HttpError.badRequest`,
`unauthorized`, `forbidden`, `notFound`, `conflict`, `unprocessable`,
`tooManyRequests`, `internal`, `badGateway`, `serviceUnavailable`.

**`Server`** — Routes: `route`, `get`, `post`, `put`, `del`, `patch`,
`head`, `options`, `all` plus matching `*Opts` siblings for per-route
options. `notFound(handler)` / `setNotFoundHandler(handler)`.
`static_(urlPrefix, dir)` for static files.
`versionedRoute(method, path, handlers, fallback="", header="Accept-Version")`
registers one route that dispatches to the handler in `handlers` matching
the request's version header (returns 406 if unknown and no `fallback`).
Plugins: `register(plugin, prefix="", encapsulate=false)`,
`useCors(origin, methods, headers)`,
`useSecurityHeaders(opts={})` (Helmet-equivalent — ships CSP,
HSTS, X-Content-Type-Options, X-Frame-Options, Referrer-Policy,
COOP / CORP, X-DNS-Prefetch-Control, X-Download-Options,
X-Permitted-Cross-Domain-Policies, Origin-Agent-Cluster, and
optional Permissions-Policy with sane defaults; every header is
overridable via `opts` and respects handler-set values).
Introspection: `hasRoute(method, path)` (exact-pattern match; the
`:id` placeholder is literal). Trust proxy:
`trustProxy(headers=["X-Forwarded-For"], hops=1)`. Runtime: `listen`,
`listenBackground`, `listenTLS`, `listenTLSBackground`, `close`,
`shutdown(timeoutMs=5000)`. Decorators: `decorate(name, value)` /
`dec(name)` / `hasDecorator(name)`, `decorateRequest(name, value)`,
`decorateResponse(name, value)` (alias `decorateReply`). Content-type
parsers: `addContentTypeParser(mediaType, fn)`. Shared schemas:
`addSchema({"$id": "...", ...})` / `removeSchema(id)` — per-route
schemas then reference them via `{"$ref": "<id>"}`. Errors:
`setErrorHandler(fn)`, `setSchemaErrorFormatter(fn)` (custom
shape for 400 validation bodies — signature
`func(where, errs, req) -> dict | str`).  Per-request timeout:
`setRequestTimeout(ms)` (server-wide default; `0` disables) +
`onTimeout(fn)` hook (signature `func(req: Request, res:
Response)`); per-route override via `routeOpts({"timeoutMs": ...})`
or its alias `"timeout"`. When a handler exceeds the deadline
the dispatcher snapshots a fresh response, runs every `onTimeout`
hook against it, and writes a Fastify-style 408 envelope
(`{"statusCode": 408, "error": "Request Timeout", "message":
"...", "timeoutMs": <n>}`) — the still-running handler keeps its
own (orphaned) response object so it can't race the writer.
Introspection / test: `listRoutes()`, `printRoutes()` (sorted
pretty-printed table with `[stream]` / `[deprecated]` / `[hidden]`
/ `[timeout=...]` flags — good for startup banners),
`openapi(info=None)`,
`inject(method, path, body="", headers=None)`. Response-side
serialisation: `res.serialize(payload=None)` applies the matched
route's response schema (exact status → family → `default`) and
returns the on-the-wire string without mutating the response —
mirrors Fastify's `reply.serialize()`; strings pass through
verbatim.
Config: `.bodyLimit`, `.debug`, `.ctx`, `.boundAddr` (populated
after listen succeeds), `.requestTimeoutMs`.

**Request lifecycle hooks** (Fastify-compatible ladder; each phase
has both a global hook list and a route-level slot):

| Phase | Server method | Route-opts key | Notes |
|---|---|---|---|
| `onRequest` | `onRequest(fn)` | `onRequest` | First touch; body still raw. |
| `preParsing` | `preParsing(fn)` | `preParsing` | Before content-type parser. |
| `preValidation` | `preValidation(fn)` | `preValidation` | After parsing, before schema validation. |
| `preHandler` | `preHandler(fn)` | `preHandler` | After validation, before the handler. |
| `preSerialization` | `preSerialization(fn)` | `preSerialization` | After handler, before response serializer. |
| `onSend` | `onSend(fn)` | `onSend` | Last chance to mutate body / headers. |
| `onResponse` | `onResponse(fn)` | `onResponse` | Observational; status/body locked in. |
| `onError` | `onError(fn)` | `onError` | Fires on panic in any of the above. |
| `onRequestAbort` | `onRequestAbort(fn)` | — | Fires when the HTTP client disconnects before the handler finishes. Signature: `func(req: Request)`. Runs on a side goroutine; use to cancel background work keyed off the request. Inject path never triggers it. |
| `onTimeout` | `onTimeout(fn)` | — | Fires when the per-request handler timeout (`setRequestTimeout` or per-route `timeoutMs`) elapses before the handler returns. Signature: `func(req: Request, res: Response)`. The hook can mutate `res` (status, headers, body) before the dispatcher writes the 408 envelope. Inject path never triggers it. |

**App-lifecycle hooks** — fire once each at the corresponding
transition. Signature: `func(srv: Server)`.

| Hook | Fired |
|---|---|
| `onReady` | Synchronously before the listener binds. |
| `onListen` | After bind succeeds (`srv.boundAddr` is populated). |
| `onClose` | During `close()` / `shutdown()`, in reverse registration order. |

**Per-route options** — pass a `dict[str, any]` to any `*Opts` method:

| Key | Type | Effect |
|---|---|---|
| `bodyLimit` | `int` | Reject this route's body over N bytes with 413. |
| `timeoutMs` (alias `timeout`) | `int` | Per-request handler timeout for this route only; overrides `srv.setRequestTimeout(...)`. `0` means inherit. |
| `streamBody` (alias `stream`) | `bool` | Skip the buffered body read. `req.body` stays `""`; pull chunks via `req.bodyReader()`. |
| `preParsing` / `preValidation` / `preHandler` / `preSerialization` / `onSend` / `onResponse` / `onError` | `func` | Route-scoped lifecycle hook. |
| `schema` | `dict` | `body` / `querystring` / `params` / `headers` / `response` (status-keyed) JSON Schemas. |
| `summary` / `description` / `operationId` | `str` | OpenAPI metadata. |
| `tags` | `list[str]` | Categorisation. |
| `deprecated` / `hidden` | `bool` | Mark deprecated; hide from `listRoutes()` and OpenAPI. |
| `consumes` / `produces` | `list[str]` | OpenAPI media types. |

**Streaming request bodies** — set `streamBody: true` in a route's
opts to tell the dispatcher not to `io.ReadAll` the body.
`req.body` then stays `""` and you read chunks off the wire
yourself via `req.bodyReader()` (returns a `RequestBodyReader`).
The reader is also available on buffered routes — it just wraps
`req.body` in a `strings.Reader` so a single handler can work
with both modes.
Methods: `.read(maxBytes=8192) -> str` returns `""` on EOF;
`.readAll() -> str` drains; `.copyTo(path) -> int` streams to disk
and returns the bytes written; `.close()` releases the underlying
connection reader; `.isEOF() -> bool` surfaces the read-state.
`bodyLimit` still caps streamed reads — the reader raises a 413
`HttpError` via the server's error pipeline when the limit is
crossed. Content-type parsers / schema validation don't run on
streamed routes (they need the whole body buffered).

**Schema validation** — schemas are JSON Schema (draft-07), passed
either as a raw string or a Lam dict (auto-marshalled). Validation
failures return 400 with a structured
`{statusCode, error, message, validation}` body. Response schemas
project the body to schema-allowed properties when
`additionalProperties: false` is set (Fastify's whitelist behaviour).

**Shared schemas + `$ref`** — register reusable schemas with
`srv.addSchema({"$id": "timestamp", "type": "string", "minLength": 10})`,
then reference them from per-route schemas as
`{"$ref": "timestamp"}`. Resolution substitutes the shared schema
text inline before compilation, so nested refs and re-registered
`$id`s just work. `removeSchema(id)` forgets a registered schema
(routes that still reference it stop enforcing that constraint).

**Custom validation errors** — replace the default envelope with
`srv.setSchemaErrorFormatter(fn)`; the formatter
(`func(where: str, errs: list[str], req: Request) -> any`) can
return a dict (serialised as JSON) or a string (sent verbatim with
`text/plain`). Panics inside the formatter fall back to the
built-in envelope so a buggy formatter can't crash the server.

**Plugin encapsulation** — `srv.register(plugin, prefix="",
encapsulate=true)` snapshots every request-lifecycle hook list before
the plugin runs; after it returns, hooks added during its run are
attached as route-level chains on the plugin's routes only. Decorators
and app-lifecycle hooks remain global.

**Routing** — `:name` for path parameters (read via
`req.params[":name"]`), trailing `*` for wildcard suffix capture
(`req.params["*"]`). Per-method radix trie; falls back to a linear
scan for routes added by direct `srv.Routes` mutation.

**OpenAPI** — `srv.openapi({"title":..., "version":...,
"description":...})` returns a JSON-serialised OpenAPI 3.0.3 doc with
paths, parameters from path patterns + query schemas, request bodies
from body schemas, and responses keyed by status code.

**Multipart** — `Request.formField(name, fallback)`,
`Request.formFile(name)` (returns dict with
`name`/`filename`/`contentType`/`size`/`content`),
`Request.formFiles(name)` for multi-uploads. Lazy-parsed once and
cached on the request.

See [`docs/server_plugins.md`](server_plugins.md) for the plugin
authoring guide.

### `lamserver_plugins`

Ready-made plugins. Each is a top-level
`func(srv: Server, ...config)` you call after constructing the Server:

| Plugin | What it does |
|---|---|
| `requestLog(srv, label="[req]")` | Per-request structured log to stderr. |
| `requestId(srv, header="X-Request-Id")` | Generate / preserve a 20-hex-char ID per request; echo it back as `X-Request-Id`. |
| `rateLimit(srv, max=100, windowMs=60000, message=...)` | Sliding-window per-IP limit; returns 429. |
| `compress(srv, minBytes=1024)` | Gzip when client sends `Accept-Encoding: gzip`. |
| `helmet(srv)` | Security-header defaults (`X-Frame-Options`, CSP, HSTS over HTTPS, etc.). |
| `etag(srv)` | Weak SHA-256 ETag + `If-None-Match` → 304. |
| `healthcheck(srv, livePath="/healthz", readyPath="/readyz")` | K8s-style probes; pair with `markReady` / `markNotReady`. |
| `metrics(srv, path="/metrics")` | OpenMetrics-format scrape endpoint with counters + histogram. |
| `serverTiming(srv)` | RFC 8290 `Server-Timing` header with `total;dur=...`; handlers can append marks via `req.ctx["__lamSrvTimingMarks"]`. |
| `idempotency(srv, header="Idempotency-Key", ttlSec=300)` | Stripe-style request-replay cache. Caches `POST` / `PUT` / `PATCH` responses keyed by `Idempotency-Key`; replays mark `X-Idempotency-Replay: 1`. |
| `tracing(srv, echoHeader=true)` | W3C Trace Context (`traceparent` / `tracestate`) propagation. Reads inbound context, mints a fresh `spanId` for the current service, and stashes `__lamTraceId` / `__lamSpanId` on `req.ctx`. Optionally echoes back the outgoing traceparent. |
| `basicAuth(srv, users, realm="Restricted")` | HTTP Basic authentication; sets `req.ctx["user"]` on success. |
| `bearerAuth(srv, verify, header="Authorization")` | Bearer-token authentication; `verify(token)` returns truthy claims on success. |
| `ipFilter(srv, allow=[], deny=[])` | CIDR allow/deny filtering, using the trust-proxy-aware client IP. |
| `cacheControl(srv, directive="no-store", ifMissing=true)` | Adds a default `Cache-Control` value without clobbering handler-set headers. |
| `session(srv, secret, cookieName="sid", maxAge=86400, secure=false, sameSite="Lax")` | In-memory signed-cookie sessions exposed through `req.ctx["session"]`. |
| `csrf(srv, secret, cookieName="csrf", headerName="X-CSRF-Token", safeMethods=[...])` | Double-submit-cookie CSRF protection for unsafe methods. |

The module also exports a `CircuitBreaker` class for downstream-call
protection:

```lammergeier
from lamserver_plugins import CircuitBreaker

cb = CircuitBreaker(5, 2000)        # threshold=5 failures, cooldown=2000ms
out: any = cb.callFn(callRemoteAPI)  # a Result stored as any
print(f"breaker={cb.state()} result={out}")
```

State transitions: `closed` → `open` (after `threshold` consecutive
failures) → `half-open` (after `cooldownMs` elapses) → `closed` (on
the first probe success) or back to `open` (on probe failure).
Inspect via `cb.state()`.

See [`docs/server_plugins.md`](server_plugins.md) for the plugin
authoring guide and recipes.

### `lamserver_ws` — `WebSocket`

WebSocket helper using `github.com/gorilla/websocket` (auto-fetched
on build). Supports subprotocols + permessage-deflate compression
(RFC 7692) via `wsRouteOpts`.

```lammergeier
from lamserver_ws import WebSocket, wsRoute, wsRouteOpts

func chatHandler(ws: WebSocket, req: Request) {
    while not ws.isClosed() {
        msg: str = ws.recv()
        if msg == "" { break }
        ws.send(f"echo: {msg}")
    }
}

# Plain (permissive Origin, no compression).
wsRoute(srv, "/chat", chatHandler)

# Production: subprotocols + deflate + Origin allow-list.
opts: dict[str, any] = {}
opts["subprotocols"]    = ["jsonrpc-v2"]
opts["compression"]     = true
opts["originAllowList"] = ["https://app.example.com"]
wsRouteOpts(srv, "/rpc", rpcHandler, opts)
```

`WebSocket` fields: `.subprotocol` (negotiated). Methods: `send(text)`,
`sendBytes(data)`, `recv()`, `recvBytes()`, `ping()`, `close()`,
`isClosed()`, `rawConn()`.

### `lamserver_tus` — tus.io resumable uploads

Implements the [tus 1.0.0](https://tus.io) protocol — Creation +
Patch + Head + Termination — on top of `lamserver`. Compatible
with every official tus client (uppy, tus-js-client, Go/Python/iOS
SDKs).

```lammergeier
from lamserver_tus import tusUploads, tusGc

tusUploads(srv, "/uploads", "/var/lib/uploads", 200_000_000)
# Optional: garbage-collect stale uploads from a periodic task.
n: int = tusGc("/var/lib/uploads", 24)  # purge >24h-old uploads
```

Storage is a flat directory: each upload gets `<id>` (the bytes) +
`<id>.json` (metadata). Hook completion via
`srv.ctx["__tusComplete"] = lambda info: …`.

### `lamnet` — `Net`, `TcpConn`, `TcpListener`

Raw TCP/UDP sockets and DNS.

```lammergeier
from lamnet import Net, TcpConn, TcpListener

l: TcpListener = Net.listenTcp(9000)
conn: TcpConn = l.accept()
conn.send("welcome\n")
chunk: str = conn.recv(4096)
conn.close()
```

`Net`: `lookupHost(host)`, `lookupAddr(ip)`, `hostname()`,
`isReachable(host, port, timeoutMs=1000)`, `dialTcp(host, port,
timeoutMs=5000)`, `listenTcp(port)`.
`TcpConn`: `send(data)`, `recv(maxBytes=4096)`, `close()`,
`remoteAddr()`.
`TcpListener`: `accept()`, `close()`, `port()`.

### `lamcompress` — `Compress`

Stream-free gzip/zlib/deflate. Output is base64-encoded so it round-
trips through string-typed channels.

```lammergeier
from lamcompress import Compress

enc: str = Compress.gzip("hello hello hello")
dec: str = Compress.gunzip(enc)
```

`gzip`, `gunzip`, `zlibDeflate`, `zlibInflate`.

### `lamsmtp` — `Smtp`, `Mail`

Outbound SMTP built on Go's `net/smtp` + MIME multipart assembly.
Sends plain-text, HTML, and attachment-bearing mail to any SMTP
server (MailHog / Mailpit for dev, SES / SendGrid / a bare-metal
relay for prod).

```lammergeier
from lamsmtp import Smtp, Mail

# One-shot plaintext.
Smtp.sendMail(
    host="localhost:1025",
    sender="alice@example.com",
    to=["bob@example.com"],
    subject="Hello",
    text="Hi Bob!",
)

# HTML + plaintext alternative + attachment via the builder.
m: Mail = Mail()
m.setSender("alice@example.com")
m.addTo("bob@example.com")
m.addCc("carol@example.com")
m.setSubject("Invoice")
m.setText("PDF attached.")
m.setHtml("<h1>Invoice</h1><p>PDF attached.</p>")
m.attach("invoice.pdf", "application/pdf", pdfBytes)
Smtp.send(host="smtp.example.com:587",
          username="alice", password="app-password",
          mail=m)
```

`Smtp.sendMail`: short-form for plaintext. `Smtp.send`: accepts a
pre-built `Mail` with multipart / attachment / custom-header
support. `username` / `password` opt into PLAIN auth (stdlib's
client refuses plain-auth over an untrusted cleartext channel
unless the server is `localhost`, matching Go's built-in policy).

`Mail` builder methods: `setSender`, `addTo`, `addCc`, `addBcc`,
`setReplyTo`, `setSubject`, `setText`, `setHtml`, `attach`,
`setHeader`. Bcc recipients are routed on the envelope without
appearing in any rendered header. When both `setText` and
`setHtml` are populated the message is emitted as
`multipart/alternative`; attachments force `multipart/mixed`
with a nested alternative part.

---

## Validation, auth & APIs

### `lamschema` — `Schema`

Declarative JSON Schema validation backed by
`xeipuuv/gojsonschema` (draft-07 compliant). Schemas are compiled
lazily on first use and cached, so registering the same schema in a
hot path is just a map lookup. Full coverage of types, ranges,
patterns, formats, and combinators (`oneOf`/`anyOf`/`allOf`/`not`).

```lammergeier
from lamschema import Schema

userSchema: str = """
{
  "type": "object",
  "required": ["name", "age"],
  "properties": {
    "name":  {"type": "string", "minLength": 1},
    "age":   {"type": "integer", "minimum": 0, "maximum": 150},
    "email": {"type": "string", "format": "email"}
  }
}
"""

ok: bool = Schema.validateJson(userSchema, '{"name":"alice","age":30}')

# Pre-compile once for hot-path validation:
Schema.register("user.create", userSchema)
ok2: bool = Schema.validateByKey("user.create", body)
errors: list[str] = Schema.errorsByKey("user.create", body)
```

Why JSON Schema rather than struct-decode? Struct-decode catches
type errors and required fields but misses ranges, patterns, enums,
formats (`email`, `uri`, `uuid`), and combinatory schemas — about
90% of what real REST APIs need to enforce. JSON Schema is also the
format OpenAPI uses, so the schema you write here doubles as your
spec.

| Method | Returns |
|--------|---------|
| `Schema.validateJson(schemaText, jsonDoc)` | `bool` |
| `Schema.validateValue(schemaText, value: json)` | `bool` (skip JSON re-parse) |
| `Schema.errors(schemaText, jsonDoc)` | `list[str]` (empty = valid) |
| `Schema.register(key, schemaText)` | `str` (`""` ok, else error) |
| `Schema.validateByKey(key, jsonDoc)` / `validateValueByKey(key, value)` | `bool` |
| `Schema.errorsByKey(key, jsonDoc)` | `list[str]` |
| `Schema.isJson(s)` | `bool` (cheap pre-check) |
| `Schema.unregister(key)` | `bool` |

### `lamjwt` — `Jwt`

JSON Web Tokens — sign + verify with HS256/HS512/RS256, automatic
`iat` + `exp`, configurable verify-side leeway (default 30 s per
RFC 7519). Built on `github.com/golang-jwt/jwt/v5`.

```lammergeier
from lamjwt import Jwt

claims: dict[str, any] = {}
claims["sub"]  = "alice"
claims["role"] = "admin"

token: str = Jwt.signHS256(claims, "supersecret", 3600)
ok: bool = Jwt.verifyHS256(token, "supersecret")
payload: dict[str, any] = Jwt.decodeHS256(token, "supersecret")

Jwt.setLeeway(60)   # seconds — RFC 7519 recommends ≤300
```

Sign/verify pairs for HS256, HS512, RS256 plus `try*` siblings that
return `Result`. `parseUnverified(token)` exposes the payload
without checking the signature — diagnostic only.

---

## Serialization

### `lamprotobuf` — `Pb`

Protocol Buffers wire-format codec on top of
`google.golang.org/protobuf`. Lam doesn't compile `.proto` files
itself; the workflow is:

1. Run `protoc --go_out=. *.proto` to generate Go types (one-off).
2. Import the generated package inside a `go!` block.
3. Use `Pb.marshal(msg)` / `Pb.unmarshal(data, msg)` from Lam.

```lammergeier
from lamprotobuf import Pb

go! {
    import myproto "github.com/me/app/proto/v1"
}

func main() {
    msg: any = None
    go! { msg = &myproto.User{Name: "alice", Age: 30} }
    wire: str = Pb.marshal(msg)

    decoded: any = None
    go! { decoded = &myproto.User{} }
    Pb.unmarshal(wire, decoded)
}
```

| Method | Returns |
|--------|---------|
| `Pb.marshal(msg)` / `tryMarshal(msg)` | `str` / `Result` |
| `Pb.unmarshal(data, msg)` / `tryUnmarshal(...)` | `bool` / `Result` |
| `Pb.toJson(msg)` (canonical proto-JSON) / `fromJson(...)` | `str` / `bool` |
| `Pb.toText(msg)` (text-format) / `fromText(...)` | `str` / `bool` |
| `Pb.equal(a, b)` | `bool` (descriptor-aware) |
| `Pb.size(msg)` | `int` (without serialising) |
| `Pb.clone(msg)` | `any` (deep copy) |

---

## Crypto, encoding, IDs

### `lamhash` — `Hash`

Hashing + HMAC + constant-time comparison.

```lammergeier
from lamhash import Hash

sig: str = Hash.hmacSha256("secret", "payload")
ok: bool = Hash.constantTimeEquals(a, b)
fp: str = Hash.sha256("file contents")
```

10 helpers: `sha256`, `sha1`, `sha512`, `md5`, `crc32`, `hmacSha1`,
`hmacSha256`, `hmacSha512`, `constantTimeEquals`, `verifyHmacSha256`.

### `lambase64` — `Base64`

(Listed under [Strings & text](#lambase64--base64).)

### `lamuuid` — `Uuid`

RFC 4122 UUIDs.

```lammergeier
from lamuuid import Uuid

id: str = Uuid.v4()                # cryptographic random
sortable: str = Uuid.v7()          # time-ordered, k-sortable
```

`v4`, `v7`, `nil_()`, `isValid`, `parse`.

---

## Database

### `lamdb` — `Db`, `Tx`, `QueryBuilder`

Driver-agnostic SQL access. Drivers ship with the runtime — SQLite
(`modernc.org/sqlite`, pure-Go so no C toolchain needed), MySQL
(`go-sql-driver/mysql`), and PostgreSQL (`lib/pq`). Placeholder
dialects are auto-normalised: write `?` everywhere, and the builder
rewrites to `$1`, `$2`, … on Postgres.

```lammergeier
from lamdb import Db, Tx, QueryBuilder
from lamjson import Json

db: Db = Db.connect("sqlite", ":memory:")
db.exec("CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, age INTEGER)")

# Raw SQL — positional params, driver-agnostic ``?``.
n: int = db.exec("INSERT INTO users (name, age) VALUES (?, ?)", ["alice", 30])

# Knex-style query builder.
rows: list[dict[str, any]] = db.table("users")
    .select(["id", "name"])
    .whereBetween("age", 18, 40)
    .whereLike("name", "a%")
    .orderBy("name")
    .limit(10)
    .get()

# Aggregates return the scalar directly.
total: any = db.table("users").sum("age")

# Pagination returns ``{data, total, page, perPage, lastPage}``.
page: dict[str, any] = db.table("users").orderBy("id").paginate(1, 15)

# Upsert (ON CONFLICT for sqlite/postgres, ON DUPLICATE KEY UPDATE for mysql).
db.table("users").upsert({"name": "alice", "age": 31}, ["name"])

# Native json implements driver.Valuer, so JSON/JSONB parameters need no manual encode.
db.exec("CREATE TABLE configs (id INTEGER, payload JSON)")
config: json = {"theme": "dark", "features": ["api", "jobs"]}
db.exec("INSERT INTO configs (id, payload) VALUES (?, ?)", [1, config])
jsonRows: list[dict[str, any]] = db.queryJson(
    "SELECT payload FROM configs WHERE id = ?", ["payload"], [1]
)
restored: json = Json.fromValue(jsonRows[0]["payload"])
```

`Db`: `connect(driver, dsn)` / `tryConnect` (returns `Result`),
`setPool(maxOpen, maxIdle, idleSec, lifetimeSec)`,
`setRetries(maxRetries, baseMs)` (transient-error retry with
exponential backoff), `ping`, `close`, `driverName`, `raw` (escape
hatch for the underlying `*sql.DB`), `exec` / `tryExec`, `query` /
`first` / `scalar`, JSON-aware `queryJson(query, columns, params)` /
`firstJson` / `scalarJson`, `table(name)` → `QueryBuilder`, `transaction(fn)`
(auto-commit / auto-rollback on panic), `begin()` → explicit `Tx`.

`QueryBuilder` predicates (chainable, AND-joined by default):

- **where family**: `where(col, op, val)`, `whereEq`, `orWhere`,
  `orWhereEq`, `whereIn`, `whereNotIn`, `whereNull`, `whereNotNull`,
  `whereBetween`, `whereNotBetween`, `whereLike`, `whereNotLike`,
  `whereRaw(fragment, params)`, `orWhereRaw`.
- **structure**: `select(cols)`, `selectRaw(expr)`, `distinct()`,
  `join`, `leftJoin`, `rightJoin`, `groupBy(cols)`, `having`,
  `orderBy(col, dir)`, `limit`, `offset`, `forUpdate`, `forShare`.

`QueryBuilder` terminals:

- **read**: `get()`, `first()`, `count()`, `exists()`, `pluck(col)`,
  `sum` / `avg` / `min` / `max`, `paginate(page, perPage)`,
  `toSql()` (preview SQL + params without executing).
- **write**: `insert(record)`, `insertMany(records)`, `update(record)`,
  `delete()`, `truncate()`, `increment(col, n=1)`,
  `decrement(col, n=1)`, `upsert(record, conflictCols, updateCols=[])`,
  `firstOrCreate(lookup, extra={})`,
  `updateOrCreate(lookup, values)`, `insertReturning(record)`
  (Postgres only; pair with `returning(cols)` to pick which columns
  come back).

Transactions give you two patterns:

```lammergeier
# Scoped — auto-commits on success, auto-rolls-back on panic.
func work(tx: Tx) {
    tx.exec("INSERT INTO users (name, age) VALUES (?, ?)", ["bob", 20])
    tx.table("users").whereEq("name", "alice").update({"age": 31})
}
ok: bool = db.transaction(work)

# Explicit — you own commit()/rollback().
tx: Tx = db.begin()
tx.exec("INSERT INTO users (name, age) VALUES (?, ?)", ["carol", 21])
tx.commit()
```

`Tx` also provides `savepoint(name)`, `rollbackTo(name)`, and
`releaseSavepoint(name)` for nested control, plus `tx.table(name)`
for a builder bound to the transaction so chained writes stay
atomic with the surrounding `tx.exec` calls.

Automatic retries on transient errors (closed connection, server
gone away, bad conn…) are bounded by `setRetries`. Non-transient
errors bubble up as panics, or as `Result.Err` via the `try*`
siblings for callers that want them surfaced.

### `lammigrate` — `Migrator`, `Schema`

Knex-style schema migrations on top of `lamdb`. Each migration is
a `.sql` file with two sections delimited by marker comments
(`-- +lam up` / `-- +lam down`). Files are sorted lexicographically
by their `YYYYMMDDHHMMSS_name.sql` filename, applied in order, and
recorded in a `lam_migrations` bookkeeping table grouped by "batch"
so a single `down(1)` rewinds the entire batch atomically.

```lammergeier
from lamdb import Db
from lammigrate import Migrator, Schema

db: Db = Db.connect("sqlite", "/var/data/app.db")
mig: Migrator = Migrator(db, "./migrations")
mig.ensureSchema()
applied: list[str] = mig.up()                   # apply every pending file
rolled: list[str] = mig.down(1)                 # roll back the last batch
info: dict[str, any] = mig.status()             # summary for humans
```

`Migrator`: `ensureSchema()` (idempotent — creates `lam_migrations`
if missing), `setTableName(name)`, `list()` → `{ran, pending}`,
`status()` → `{total, ran, pending, lastBatch, ranNames,
pendingNames}`, `up(steps=0)` (0 means all pending; non-zero caps
the count), `down(steps=1)` (1 means "the last batch", values >1
roll back exactly that many files).

A migration file looks like:

```sql
-- +lam up
CREATE TABLE users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT UNIQUE NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_users_email ON users (email);

-- +lam down
DROP INDEX idx_users_email;
DROP TABLE users;
```

Inside each section, statements separated by `;` are dispatched
one-by-one through `db.exec` so transient-error retry still
applies. The splitter respects quoted strings and `--`/`/* … */`
comments.

`Schema` is an optional helper that emits portable DDL for the
three supported dialects so common columns don't need driver-
specific casts:

```lammergeier
schema: Schema = Schema(db.driverName())
db.exec(schema.createTableIfNotExists("widgets", [
    {"name": "id", "type": "id"},                          # auto-incrementing PK
    {"name": "label", "type": "string", "length": 64, "unique": true},
    {"name": "created_at", "type": "timestamp", "default": schema.timestampNow()},
]))
db.exec(schema.addIndex("widgets", ["label"]))
```

Recognised column `type` values: `id`, `string` (with optional
`length`), `text`, `integer`, `bigint`, `float`, `decimal` (with
`precision`/`scale`), `boolean`, `timestamp`, `date`, `json`,
`uuid`, plus `raw:<sql>` to inline driver-specific syntax. Column
modifiers: `nullable`, `unique`, `default` (raw SQL expression),
`references` / `on` (foreign-key target).

#### `lamc migrate` CLI

`migrate` is a first-class `lamc` subcommand — `lamc --help` lists
it alongside `build`, and `lamc <src.lam>` keeps working as a
shortcut for `lamc build <src.lam>`. The compiler ships a CLI
front-end that wraps `Migrator`:

```bash
# Scaffold a new file under ./migrations/
lamc migrate make "create users" --dir ./migrations

# Apply pending files (all, or capped via --steps N)
lamc migrate up    --dir ./migrations --driver sqlite --dsn ./app.db

# Roll back. --steps 1 == last batch (default); >1 == that many files.
lamc migrate down  --dir ./migrations --driver sqlite --dsn ./app.db

# Read-only summary.
lamc migrate status --dir ./migrations --driver sqlite --dsn ./app.db
```

`--driver` accepts `sqlite`, `mysql`, or `postgres`. `--dsn` is the
driver-specific connection string (file path for SQLite, a `host=…`
URL for Postgres, a `user:pass@tcp(host:port)/db` form for MySQL).
The CLI compiles a tiny one-shot Lam program for each invocation so
the runtime path is exactly the same as production code.

---

### `lamredis` — Redis client

A thin wrapper around `github.com/redis/go-redis/v9`. The Go client
manages the connection pool, transparent reconnects, ACL auth, and
pipelining for us, so this module focuses on giving Lam code an
idiomatic surface: GET/SET with TTL, list / hash / set / sorted-set
helpers, pub/sub with a blocking iterator, pipelines, and a generic
`exec` escape hatch for commands the wrapper doesn't expose.

```lammergeier
from lamredis import Redis, PubSub

# No-auth, default DB.
r: Redis = Redis.connect("localhost:6379")

# Auth + non-default DB. ``db`` selects a logical database; Redis
# 6+ ACLs also accept a username.
r2: Redis = Redis.connect(
    "localhost:6380",
    password="secret",
    db=1,
    username="alice",
)

# Or parse a redis://… URL straight out of an env var.
r3: Redis = Redis.fromUrl("rediss://user:pw@host:6380/2")

# Strings + TTL.
r.set("greeting", "hello", ttlSec=60)
hello: str = r.get("greeting")          # "hello"
missing: str = r.get("does-not-exist")  # "" (use ``r.exists(["k"])``
                                        #     to disambiguate)

# Native JSON is encoded/decoded automatically and can carry a TTL.
payload: json = {"name": "Ada", "roles": ["admin", "writer"]}
r.setJson("user:json:1", payload, ttlSec=60)
restored: json = r.getJson("user:json:1")
name: str = restored.name

# Atomic counters.
n: int = r.incr("hits")
r.incrBy("hits", 9)

# Lists.
r.rpush("jobs", ["a", "b", "c"])        # tail-push three values
job: str = r.lpop("jobs")               # "a"

# Hashes.
r.hsetMap("user:1", {"name": "alice", "tier": "gold"})
profile: dict[str, str] = r.hgetAll("user:1")

# Sets / sorted sets.
r.sadd("tags", ["go", "rust"])
r.zadd("scores", 100.0, "alice")
top: list[dict[str, any]] = r.zrangeWithScores("scores", -1, -1)

# Pub/sub — ``subscribe`` returns a blocking iterator.
ps: PubSub = r.subscribe(["events"])
ps.setTimeout(3000)                     # ms; 0 == wait forever
msg: str = ps.next()
ps.close()

# Pipelines — one round-trip for many commands.
results: list[any] = r.pipeline([
    {"cmd": "GET",  "args": ["key"]},
    {"cmd": "INCR", "args": ["counter"]},
])

r.close()
```

Missing-key semantics: `get`, `hget`, `lpop`, … all return `""` for
absent keys (mirroring how Lam treats empty values). Use `exists`
when you need to distinguish "missing" from "empty string".

### `lamemcached` — Memcached client

Wraps `github.com/memcachier/mc/v3` — picked over the more popular
`bradfitz/gomemcache` because it speaks the *binary* protocol, which
is the only memcached dialect that supports SASL PLAIN auth. The
same client therefore talks to an unauthenticated dev container and
a SASL-protected production cluster with no API surface change.

```lammergeier
from lamemcached import Memcached

# No-auth.
m: Memcached = Memcached.connect("localhost:11211")
m.set("greeting", "hello", ttlSec=60)
hello: str = m.get("greeting")           # "hello"

# SASL auth (requires a memcached built with ``-S`` and a SASL DB).
m2: Memcached = Memcached.connect(
    "localhost:11211",
    username="memuser",
    password="mempass123",
)

# ADD / REPLACE return ``false`` on the no-op cases instead of
# raising — handy for "set if absent" / "set if present" idioms.
created: bool = m.add("only-once", "v1")     # true the first time
again:   bool = m.add("only-once", "v2")     # false thereafter
swapped: bool = m.replace("only-once", "v3") # true while it's there

# Atomic counters with seed-on-miss.
n0: int = m.incr("hits", 1, 0)               # first call → 0
n1: int = m.incr("hits")                     # → 1, then 2, …
n2: int = m.decr("hits")

# APPEND / PREPEND for log-style accumulation.
m.set("buf", "head")
m.append("buf", "|tail")
m.prepend("buf", "front|")

# Maintenance.
versions: dict[str, str] = m.version()
stats:    dict[str, dict[str, str]] = m.stats()
m.flush()                                    # wipe every key
m.close()
```

The eager `noop` performed by `connect` surfaces bad credentials at
connect time rather than the first command, so the call site doesn't
have to inspect every result.

---

## Quick reference

| Want to… | Reach for |
|----------|-----------|
| Manipulate strings | `lamstrings`, `lamunicode`, `lamre` |
| Parse / build JSON | `lamjson` |
| Parse / build YAML (Kubernetes manifests, CI configs, …) | `lamyaml` |
| Parse / build XML (RSS feeds, SOAP, OpenSearch) | `lamxml` |
| Build a web service | `lamserver` + `lamserver_plugins` |
| Validate request bodies | `lamschema` (JSON Schema) |
| Issue / verify JWTs (with kid + JWKS rotation) | `lamjwt` (`Jwt`, `JwtKeySet`) |
| Encode / decode protobuf | `lamprotobuf` |
| Add WebSockets | `lamserver_ws` (subprotocols + permessage-deflate via `wsRouteOpts`) |
| Receive resumable uploads (concat + checksum) | `lamserver_tus` (tus.io 1.0.0) |
| Receive multipart form uploads | `Request.formField` / `formFile` (in `lamserver`) |
| Make HTTP calls (one-shot) | `lamhttp.Http.get` / `.post` / `.postJson` |
| Make HTTP calls (reusable client w/ baseUrl, headers, timeout) | `lamhttp.HttpClient` (`get`/`head`/`delete`/`post(Json)`/`put(Json)`/`patch(Json)`) |
| Talk to a SQL database | `lamdb` (raw SQL, native JSON/JSONB values, `QueryBuilder`, transactions + savepoints) |
| Run schema migrations | `lammigrate` + `lamc migrate make/up/down/status` |
| Talk to Redis (strings / native JSON / lists / hashes / sets / zsets / pub-sub / pipelines) | `lamredis` (`Redis`, `PubSub`) |
| Talk to memcached over the binary protocol (incl. SASL PLAIN auth) | `lamemcached` (`Memcached`) |
| Read / write files | `lamos`, `lampath` |
| Hash or sign data | `lamhash`, `lambase64`, `lamuuid` |
| Compute statistics or matrix algebra | `lamstats`, `lamarray` |
| Schedule work concurrently | `lamconcurrency`, `lamactor` (message-passing) |
| Cache lookup results | `lamcache` |
| Iterate lazily | `lamiter` |
| Render text / HTML templates | `lamtemplate` (`Template`, `HtmlTemplate`) |
| Retry with exponential backoff + jitter | `lamretry.Retry` |
| Throttle outbound calls (token bucket) | `lamratelimit.TokenBucket` |
| Trip a circuit on downstream failure | `lamserver_plugins.CircuitBreaker` |
| Test code | `lamtest` |
| Parse CLI flags | `lamcli` |
| Run behind a reverse proxy | `Server.trustProxy(...)` + `Request.realIP()` |
| Expose Prometheus metrics | `metrics(srv)` plugin |
| Add K8s liveness / readiness probes | `healthcheck(srv)` plugin |
| Enable security headers | `helmet(srv)` plugin |
| Conditional GET / 304 caching | `etag(srv)` plugin |
| Rate-limit incoming requests | `serverTiming(srv)` + `idempotency(srv)` plugins |
| Distributed tracing (W3C Trace Context) | `tracing(srv)` plugin |

---

## New modules (April 2026)

### `lamxml` — XML encode / decode + DOM walk

```lammergeier
from lamxml import Xml, XmlNode

# Shape-shifted decode (mirrors Json.decode):
doc: any = Xml.decode("<book id='42'><title>Refactoring</title></book>")

# Proper DOM tree:
root: any = Xml.parse(rssFeed)
go! {
    if rn, ok := root.(*XmlNode); ok {
        for _, item := range rn.FindAll("item") {
            if it, ok := item.(*XmlNode); ok {
                title := it.Find("title")  // returns *XmlNode or nil
                _ = title
            }
        }
    }
}

# Encode:
xmlStr: str = Xml.encode({"name": "alice", "age": 30}, "person")
# → "<person><name>alice</name><age>30</age></person>"
```

`Xml.tryDecode` / `Xml.tryParse` return `Result` for error
surfacing. Mapping nodes lower to `dict[str, any]`, sequence-like
collisions collapse into `list[any]`, and attributes are folded into
the dict under `@name` keys.

### `lamyaml` — YAML 1.2 encode / decode

```lammergeier
from lamyaml import Yaml

doc: dict[str, any] = Yaml.decode("name: alice\nage: 30")
yamlStr: str = Yaml.encode(doc)

# Multi-document streams (--- separators):
docs: list[any] = Yaml.decodeAll("---\nname: a\n---\nname: b\n")
```

Backed by `gopkg.in/yaml.v3`. Non-string YAML keys are stringified
during normalisation so the result still slots into Lam's `dict[str,
any]`.

### `lamhttp.HttpClient` — reusable HTTP client

```lammergeier
from lamhttp import HttpClient, HttpResponse

api: HttpClient = HttpClient("https://api.example.com", 5000)
api.setHeader("Authorization", "Bearer xyz")

res: HttpResponse = api.get("/users/42")
if res.ok() {
    print(res.body)
}

api.postJson("/users", "{\"name\":\"alice\"}")       # encoded-string compatibility
api.postJsonValue("/users", {"name": "alice"})
api.putJsonValue("/users/42", {"name": "alice2"})
api.patchJsonValue("/users/42", {"age": 31})
api.delete("/users/42")
```

Absolute URLs (`http://`/`https://`) bypass the configured base URL
so the same client can call out to a third-party API by full URL
when needed. `HttpResponse.ok()` returns `true` only for 2xx
responses with no transport error.

### `lamtemplate` — text and HTML templating

Wraps Go's `text/template` + `html/template`:

```lammergeier
from lamtemplate import Template, HtmlTemplate

t: Template = Template()
t.parse("Hello, {{.Name}}! {{len .Hobbies}} hobbies.")
out: str = t.render({"Name": "Alice", "Hobbies": ["a", "b"]})

# Browser output autoescapes:
ht: HtmlTemplate = HtmlTemplate()
ht.parse("<p>{{.Body}}</p>")
print(ht.render({"Body": "<b>bold</b>"}))   # <p>&lt;b&gt;bold&lt;/b&gt;</p>
```

Shared helper map: `upper` / `lower` / `title` / `trim` / `replace`
/ `join` / `default`.

### `lamretry.Retry` — exponential backoff with jitter

```lammergeier
from lamretry import Retry

# Fn that returns Result; Err triggers retry. Panics caught and
# treated as transient.
r: Result = Retry.runFn(myFn, 5, 100, 5000, true)

# Predicate polling:
ok: bool = Retry.until(predicate, 10, 50, 1000, true)
```

### `lamratelimit.TokenBucket` — client-side throttling

```lammergeier
from lamratelimit import TokenBucket

bucket: TokenBucket = TokenBucket(10, 5)   # capacity=10, refill=5/s
if bucket.tryTake(1) {
    callApi()
}
bucket.wait(3)   # blocks until 3 tokens available
```

Thread-safe via `sync.Mutex` so a single bucket can shape many
concurrent callers.

### `lamactor` — actor-model concurrency

`Mailbox`, `ActorRef`, `ActorSystem`. See the
[Concurrency](#concurrency) section for the full reference. TL;DR:
write a class with `onMessage(self, msg)`, `sys.spawn(it)`, then use
`ref.tell(msg)` for fire-and-forget or `ref.ask(msg, timeoutMs)` to
wait for a reply. Unhandled panics in handlers are recovered (and
optionally observed via `onError`) so a buggy actor can't bring down
its neighbours.

### `lamjwt.JwtKeySet` — JWKS-style key rotation

```lammergeier
from lamjwt import Jwt, JwtKeySet

ks: JwtKeySet = JwtKeySet()
ks.addHS("k1", "secret1")
ks.addHS("k2", "secret2")

# Later — rotate:
ks.remove("k1")

# Or load a JSON Web Key Set:
ks.loadJwks(jwksJson)

# Verify — iterates kid-first then falls back to every other key:
claims: any = ks.verifyHS256(token)
```

`Jwt.signRS256Kid` / `signHS256Kid` stamp the `kid` header.
`Jwt.extractKid` reads the kid without verifying.

For deeper dives, every module file in [`lib/`](../lib) opens with a
docstring and example block.

---

## Public API index

Use this as a module map when you know the problem but not the import.
The longer guide sections above explain behavior and examples; this
index lists the public classes and top-level functions you can import.

### Signature conventions

- Static utility classes are imported as classes and called with
  `Class.method(...)`: `Math.sqrt(9.0)`, `Json.encode(value)`.
- Data-structure classes are constructed: `Stack()`, `Queue()`,
  `Set()`, `LruCache(100)`.
- Server plugins are top-level functions: `requestId(srv)`,
  `rateLimit(srv, 100, 60000)`.
- `Result`-returning methods use `Result.Ok(value)` /
  `Result.Err(error)`. The error payload is `any`.
- Methods returning `any` usually wrap dynamic Go values, decoded YAML/XML,
  or plugin state. Decoded JSON uses native `json`. Keep other values as `any`, pass them to
  another dynamic API, or assert with a small `go!` block.

| Module | Import | Public surface |
|---|---|---|
| `lamactor` | `from lamactor import Mailbox, ActorRef, ActorSystem` | Actor mailboxes, references, spawn/find/shutdown. |
| `lamarray` | `from lamarray import Array, Matrix` | 1-D arrays, dense matrices, linear algebra, reductions. |
| `lambase64` | `from lambase64 import Base64` | `encode`, `decode`, URL-safe encode/decode. |
| `lambytes` | `from lambytes import Bytes` | String/byte conversion, hex encode/decode, byte search/count. |
| `lamcache` | `from lamcache import LruCache, TtlCache` | Bounded LRU cache and expiry-based TTL cache. |
| `lamcli` | `from lamcli import Cli` | Args, flags, positional arguments, typed integer flags. |
| `lamcompress` | `from lamcompress import Compress` | gzip/gunzip and zlib deflate/inflate as base64 strings. |
| `lamconcurrency` | `from lamconcurrency import Channel, WaitGroup, Mutex, RWMutex, Atomic` | Go-style concurrency primitives. |
| `lamconv` | `from lamconv import Conv` | String/int/float/bool/base conversions plus `try*` Result forms. |
| `lamcron` | `from lamcron import Cron` | Cron scheduler, seconds-aware mode, start/stop/remove. |
| `lamcsv` | `from lamcsv import Csv` | Parse/format CSV rows and whole documents. |
| `lamdata` | `from lamdata import DataFrame, DataFrameGroups, Series` | DataFrames, Series, joins, filters, group-by aggregation. |
| `lamdatetime` | `from lamdatetime import DateTime` | Calendar-style date/time helpers. |
| `lamdb` | `from lamdb import Db, Tx, QueryBuilder` | SQL connections, transactions, raw queries, query builder. |
| `lamdeque` | `from lamdeque import Deque` | Double-ended queue helpers. |
| `lamemcached` | `from lamemcached import Memcached` | Memcached client: get/set/add/replace/delete/touch/counters/stats. |
| `lamenv` | `from lamenv import Env, Dotenv, Config` | Environment variables, dotenv parsing/loading, layered config. |
| `lamerrors` | `from lamerrors import Error, Result` | Structured errors and success/error containers. |
| `lamexec` | `from lamexec import Exec` | Run shell-like commands or command + arg lists. |
| `lamfmt` | `from lamfmt import Fmt` | `sprintf`, printing, padding, integer bases, float formatting. |
| `lamhash` | `from lamhash import Hash` | SHA/MD5/CRC/HMAC and constant-time comparison. |
| `lamheap` | `from lamheap import Heap, PriorityHeap` | Numeric heap and priority queue. |
| `lamhttp` | `from lamhttp import Http, HttpClient, HttpResponse, HttpServer` | One-shot HTTP helpers, reusable client, tiny blocking server. |
| `lamiter` | `from lamiter import Iter` | Lazy iterator pipelines: map/filter/take/drop/reduce/enumerate. |
| `lamjson` | `from lamjson import Json` | Native `json`, wire encoding/decoding, collection conversion, kind inspection, and `try*` forms. |
| `lamjwt` | `from lamjwt import Jwt, JwtKeySet` | JWT sign/verify, HMAC/RSA, kid/JWKS key rotation. |
| `lamlog` | `from lamlog import Log` | Leveled logging and formatted log helpers. |
| `lammath` | `from lammath import Math` | Constants, trig, logs, rounding, combinatorics, number theory. |
| `lammigrate` | `from lammigrate import Migrator, Schema` | SQL migrations and schema helpers. |
| `lamnet` | `from lamnet import Net, TcpConn, TcpListener` | DNS, reachability, TCP dial/listen/send/recv. |
| `lamos` | `from lamos import Os` | Filesystem, process, path, directory, temp-file helpers. |
| `lampath` | `from lampath import Path` | Path joining/splitting/ext/base/dir/existence helpers. |
| `lamprotobuf` | `from lamprotobuf import Pb` | Protobuf marshal/unmarshal/JSON/descriptor helpers. |
| `lamqueue` | `from lamqueue import Queue` | FIFO queue. |
| `lamrandom` | `from lamrandom import Random` | Pseudo-random, secure random, tokens, UUID-like helpers. |
| `lamratelimit` | `from lamratelimit import TokenBucket` | Thread-safe token bucket throttling. |
| `lamre` | `from lamre import Re` | Regex match/find/findAll/replace/split/groups plus `try*`. |
| `lamredis` | `from lamredis import Redis, PubSub` | Redis strings, hashes, lists, sets, sorted sets, pub/sub, pipelines. |
| `lamretry` | `from lamretry import Retry` | Retry `Result`-returning calls and poll predicates. |
| `lamschema` | `from lamschema import Schema` | JSON Schema validation and registry. |
| `lamserver` | `from lamserver import Server, Request, Response, HttpError, SseEmitter, RequestBodyReader` | Fastify-style HTTP framework. |
| `lamserver_plugins` | `from lamserver_plugins import requestId, rateLimit, compress, helmet, etag, healthcheck, metrics, CircuitBreaker, ...` | Ready-made server plugins and a circuit breaker. |
| `lamserver_tus` | `from lamserver_tus import tusUploads, tusGc` | tus.io resumable upload routes and garbage collection. |
| `lamserver_ws` | `from lamserver_ws import WebSocket, wsRoute, wsRouteOpts` | WebSocket route helper and socket wrapper. |
| `lamset` | `from lamset import Set` | Set operations: add/remove/contains/union/intersect/difference/subset. |
| `lamsmtp` | `from lamsmtp import Smtp, Mail` | SMTP send helpers and MIME mail builder. |
| `lamsort` | `from lamsort import Sort` | Typed sort/reverse helpers. |
| `lamstack` | `from lamstack import Stack` | LIFO stack. |
| `lamstats` | `from lamstats import Stats` | Descriptive statistics and percentiles. |
| `lamstrings` | `from lamstrings import Strings` | UTF-8 string manipulation and formatting helpers. |
| `lamtemplate` | `from lamtemplate import Template, HtmlTemplate` | Text and HTML templates. |
| `lamtest` | `from lamtest import Test` | Assertions, counters, summaries. |
| `lamtime` | `from lamtime import Time` | Unix time, sleeps, formatting, parsing, measurement. |
| `lamunicode` | `from lamunicode import Unicode` | Rune counts, validity, categories, rune conversion. |
| `lamurl` | `from lamurl import Url` | URL parse parts and percent encode/decode. |
| `lamuuid` | `from lamuuid import Uuid` | UUID v4/v7/nil/parse/validate. |
| `lamxml` | `from lamxml import Xml, XmlNode` | XML encode/decode/parse and node traversal. |
| `lamyaml` | `from lamyaml import Yaml` | YAML encode/decode/decodeAll plus `try*` forms. |

### High-value signatures

These are the signatures most often needed while writing application
code. They duplicate the guide sections intentionally so you can skim
without jumping around.

```lammergeier
# lamerrors
Error(kind: str, message: str, cause: any = None)
Result.Ok(v: any) -> Result
Result.Err(e: any) -> Result
r.ok() -> bool
r.unwrap() -> any
r.unwrapOr(fallback: any) -> any

# lamserver
srv.route(method: str, path: str, handler: any)
srv.routeOpts(method: str, path: str, handler: any, opts: any)
srv.get/post/put/del/patch/head/options/all(path: str, handler: any)
srv.getOpts/postOpts/putOpts/delOpts/patchOpts/headOpts/optionsOpts(path, handler, opts)
srv.onRequest/preParsing/preValidation/preHandler/preSerialization/onSend/onResponse/onError(fn)
srv.register(plugin: any, prefix: str = "", encapsulate: bool = false)
srv.addSchema(schema: any)
srv.setSchemaErrorFormatter(fn: any)
srv.inject(method: str, path: str, body: str = "", headers: any = None) -> dict[str, any]
req.header(name: str) -> str
req.queryGet(name: str, fallback: str = "") -> str
req.jsonBody() -> json
req.bodyReader() -> RequestBodyReader
res.code(n: int) -> Response
res.header(name: str, value: str) -> Response
res.json(obj: json) -> Response
res.text(body: str) -> Response
res.streamFile(path: str, contentType: str = "") -> Response

# lamdb
Db.connect(driver: str, dsn: str) -> Db
Db.tryConnect(driver: str, dsn: str) -> Result
db.exec(query: str, params: list[any] = []) -> int
db.query(query: str, params: list[any] = []) -> list[dict[str, any]]
db.transaction(fn: any) -> bool
db.table(name: str) -> QueryBuilder
qb.select(cols: list[any]) -> QueryBuilder
qb.where(col: str, op: str, val: any) -> QueryBuilder
qb.whereEq(col: str, val: any) -> QueryBuilder
qb.orderBy(col: str, dir_: str = "asc") -> QueryBuilder
qb.get() -> list[dict[str, any]]
qb.first() -> dict[str, any]
qb.insert(record: dict[str, any]) -> int
qb.update(record: dict[str, any]) -> int
qb.delete() -> int

# lamdata
DataFrame.readCSV(content: str) -> DataFrame
DataFrame.readJSON(content: str) -> DataFrame
df.selectCols(cols: list[str]) -> DataFrame
df.filter(col: str, op: str, value: any) -> DataFrame
df.groupBy(cols: list[str]) -> DataFrameGroups
groups.aggregate(types: list[str], cols: list[str]) -> DataFrame
series.mean() -> float
series.toFloatList() -> list[float]
```
