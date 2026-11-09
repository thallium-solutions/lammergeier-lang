# Lammergeier Standard Library Reference

The standard library lives in [`lib/`](../lib) and is imported exactly
like any user module:

```lammergeier
from lammath import Math
from lamstrings import Strings
```

Every standard-library module is listed below, organised by topic. Clearly
marked companion sections also show how independently versioned official
packages compose with the standard library; those packages are not part of
`lib/`. Method signatures are extracted directly from source — when in doubt,
open the relevant `.lam` file for authoritative behaviour.

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
| data | File formats, markup, translations, tabular/numeric data, and data-shaping helpers. | `lamcsv`, `lamyaml`, `lamxml`, `lamhtml`, `lammarkup`, `lami18n`, `lamdata`, `lamarray`, `lamstats`, `lamrandom` |
| net | Network protocols and clients. | `lamhttp`, `lamsmtp`, `lamnet`, `lamurl` |
| infra | Database and operational infrastructure. | `lamdb`, `lammigrate`, `lamcron`, `lamexec`, `lamcache` |
| concurrency | Concurrency primitives, cancellation, async coordination, resilience, and collections. | `lamactor`, `lamconcurrency`, `lamcontext`, `lamretry`, `lamratelimit`, `lamdatastructures` (canonical), `lamqueue`, `lamdeque`, `lamheap`, `lamstack`, and `lamset` (compatibility re-exports), `lamiter`, `lamsort` |
| encoding & crypto | Encryption, keys, encoding, hashing, compression, bytes, UUIDs, templates, and text validation helpers. | `lamcrypto`, `lambase64`, `lambytes`, `lamcompress`, `lamhash`, `lamuuid`, `lamtemplate`, `lamunicode`, `lamre`, `lamschema` |
| CLI & OS | Program entrypoint, filesystem, environment, and host-process helpers. | `lamcli`, `lamos` |

The tiers are intentionally coarse. A module can compose across tiers
at the Lam source level; Phase 8 dependency work tracks whether that
composition accidentally spreads heavy Go dependencies.

---

## Cookbook

This section shows how standard-library modules and clearly marked official
packages compose in real programs. The reference sections below list the
individual methods; these recipes show the intended workflow.

### Parse, validate, and propagate with `Result`

Fallible stdlib APIs use their unsuffixed names and return typed
`Result[T]` values. The `?` operator unwraps success values and returns the
same `Result.Err(...)` from the current function on failure.

```lammergeier
from lamconv import Conv
from lamerrors import Result, Error

func parsePort(raw: str) -> Result[int] {
    if raw == "" {
        return Result.Err(Error("ConfigError", "PORT is required"))
    }
    parsed: Result[int] = Conv.parse(raw)
    port: int = parsed?
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
    text: str = Os.readFile(path)?
    return Json.decode(text)
}

func main() {
    input: str = Cli.getFlag("input", "data.json")
    pretty: bool = Cli.hasFlag("pretty")

    do {
        value: json = loadJson(input)?
        if pretty {
            print(Json.encodePretty(value)?)
        } else {
            print(Json.encode(value)?)
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

`@lam/vulture` gives you Fastify-like routing, lifecycle hooks,
validation, decorators, schemas, OpenAPI, test injection, TLS,
timeouts, and plugins. It is an independently versioned official package, not
part of the standard library. Install the v2 packages first:

```bash
lamc install @lam/vulture@2.0.2
lamc install @lam/vulture/std-plugin@2.0.3
```

```lammergeier
from @lam/vulture import InjectResponse, Request, Response, RouteOptions, Server, HttpError
from @lam/vulture/std-plugin import helmet, metricsPlugin, requestIdPlugin, requestLogPlugin
from lamerrors import Result

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
    bodyResult: Result[json] = req.jsonBody()
    if not bodyResult.ok() {
        res.code(400).text(str(bodyResult.error))
        return
    }
    body: json = bodyResult.unwrap()
    name: str = body.name
    res.code(201).json({"id": "u_2", "name": name})
}

func main() {
    srv: Server = Server()
    srv.setRequestTimeout(3000)
    srv.register(requestIdPlugin())
    srv.register(requestLogPlugin("[api]"))
    srv.register(helmet)
    srv.register(metricsPlugin("/metrics"))

    userBody: dict[str, any] = {
        "type": "object",
        "required": ["name"],
        "properties": {"name": {"type": "string", "minLength": 1}},
    }

    listOptions: RouteOptions = RouteOptions()
    listOptions.summary = "List users"
    listOptions.tags = ["users"]
    srv.get("/users", listUsers, listOptions)

    createOptions: RouteOptions = RouteOptions()
    _ = createOptions.preHandler(requireJson)
    _ = createOptions.withSchema({"body": userBody})
    createOptions.summary = "Create user"
    createOptions.tags = ["users"]
    srv.post("/users", createUser, createOptions)

    print(srv.printRoutes())
    print(srv.openapi({"title": "Users API", "version": "1.0.0"}))
    _ = srv.listen(8080).unwrap()
}
```

Test the same API without binding a port. `inject` returns a typed
`InjectResponse`, not a dictionary:

```lammergeier
headers: dict[str, str] = {"Content-Type": "application/json"}
out: InjectResponse = srv.inject("POST", "/users", "{\"name\":\"Grace\"}", headers)
print(out.status)          # 201
print(out.headers)         # includes request/security/plugin headers
print(out.body)
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
    aggTypes: list[str] = ["mean", "mean"]   # one strategy per column
    aggCols: list[str] = ["temp", "wind"]
    groups: DataFrameGroups = df.groupBy(groupCols)
    summary: DataFrame = groups.aggregate(aggTypes, aggCols)
    print(summary.toString())

    values: list[float] = temps.toFloatList()
    print(Stats.percentile(values, 95.0))   # p is 0-100
}
```

`DataFrame` methods return new values and preserve the receiver,
matching `gota`'s value-oriented design. Check `.error()` after
operations that depend on external data or column names.

### Use SQLite/Postgres/MySQL through `Db`

`lamdb` provides raw SQL, transactions, savepoints, retries, and a
chainable query builder. Placeholder dialects are normalized, so write
`?` in Lam code and let the driver adapter translate where needed.
Functions taking a `DbConn` run on a `Db` or inside a `Tx`.

```lammergeier
from lamdb import Db, DbConn, QueryBuilder, Tx
from lamerrors import Result

func bump(conn: DbConn, name: str) -> Result[int] {
    return conn.table("users").whereEq("name", name).increment("age", 1)
}

func main() {
    do {
        db: Db = Db.connect("sqlite", "file:app.db?cache=shared")?
        defer db.close()

        _ = db.exec("CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, name TEXT, age INTEGER)")?
        _ = db.table("users").insert({"name": "Ada", "age": 36})?
        _ = db.table("users").insert({"name": "Grace", "age": 30})?

        adults: list[dict[str, any]] = (
            db.table("users")
              .select(["id", "name", "age"])
              .where("age", ">=", 18)
              .orderBy("name", "asc")
              .get()?
        )
        print(adults)

        bumped: int = Db.atomic(db, lambda (tx: Tx) -> Result[int]: bump(tx, "Ada"))?
        print(bumped)
    } catch err {
        print(f"database error: {err}")
    }
}
```

The primary `connect`, execution, query, transaction, and query-builder
terminal methods all return typed `Result[T]` values; propagate them with `?`
or inspect `ok()` and `error` explicitly. For concurrent requests, derive a
separate `Db.withContext(ctx)` handle from the shared pool; see
[request-scoped database observation](#request-scoped-database-observation).

### Cache and retry downstream calls

`LruCache`, `TtlCache`, `Retry`, and `CircuitBreaker` cover the most
common resilience patterns.

```lammergeier
from lamcache import TtlCache
from lamretry import Retry
from @lam/vulture/std-plugin import CircuitBreaker
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
    guarded: Result = breaker.call(downstreamSideEffect)
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
| `Strings.camelCase(s)` / `pascalCase(s)` / `snakeCase(s)` / `kebabCase(s)` | `str` |
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

Case conversion splits the text into words, then joins them in the
target style, lower-cased (the first letter of each word but the first
upper-cased for `camelCase`, of every word for `pascalCase`):

```lammergeier
print("slots_total".camelCase())               # slotsTotal
print("purchased-products-limit".camelCase())  # purchasedProductsLimit
print("userID".snakeCase())                    # user_id
print("HTTPServer".kebabCase())                # http-server
print("user name".pascalCase())                # UserName
```

Any rune that is not a letter or a digit separates words (`_`, `-`,
spaces, `.`); a word also ends before an upper-case letter that follows a
lower-case letter or a digit (`userName`), and before the last capital of
a run followed by a lower-case letter (`HTTPServer` -> `HTTP` `Server`).
Digits stay with the word before them, so `line2Text` is `line2_text`
(and `version_2` becomes `version2`). Leading and trailing separators
disappear (`__private__` -> `private`). `Json.mapKeys(doc,
Strings.camelCase)` renames the keys of a whole document.

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

Go's `regexp` package. Pattern-compiling operations use unsuffixed names and
return `Result[T]` so malformed expressions remain distinguishable from valid
no-match results.

```lammergeier
from lamerrors import Result
from lamre import Re

ok: Result[bool] = Re.match("^\\d+$", "12345")
words: Result[list[str]] = Re.findAll("\\w+", "two words")
```

| Method | Returns |
|--------|---------|
| `Re.match(pattern, s)` | `Result[bool]` |
| `Re.find(pattern, s)` | `Result[str]` (`Ok("")` means no match) |
| `Re.findAll(pattern, s)` | `Result[list[str]]` |
| `Re.replaceAll(pattern, s, repl)` | `Result[str]` |
| `Re.split(pattern, s)` | `Result[list[str]]` |
| `Re.matchGroups(pattern, s)` | `Result[list[str]]` |

A pattern is compiled once and reused (a cache shared by goroutines, starting
over after 512 distinct patterns; invalid patterns are not cached), so
`Re.match(pattern, text)` in a request handler costs no compilation.

### `lamfmt` — `Fmt`

Sprintf-style formatting (wraps `fmt`).

```lammergeier
from lamfmt import Fmt

s: str = Fmt.sprintf("%d items @ $%.2f", 5, 9.99)
```

`Fmt` owns presentation: `sprintf`, `printf`, `println`, padding/centering,
`hex`, `octal`, `binary`, `formatFloat`, and `repr`. `Conv` is intentionally
limited to string parsers; use `Fmt` (or the `str(...)` builtin) when converting
values to display text.

`Fmt.formatFloat(f, decimals = -1)` rounds to `decimals` digits after the point
(`formatFloat(2.5, 2)` is `"2.50"`). A negative `decimals` (the default) gives
the shortest text that reads back as the same float, without an exponent —
Go's `strconv.FormatFloat(f, 'f', -1, 64)`: `formatFloat(1234567.5)` is
`"1234567.5"`, `formatFloat(2.0)` is `"2"`, `formatFloat(1e21)` is
`"1000000000000000000000"` and `formatFloat(0.00001)` is `"0.00001"` (where
`str(f)` would write an exponent).

`Fmt.grouped(value, separator = ",", decimals, decimalSeparator = ".")` writes
the integer digits in groups of three, for an `int` or a `float`:

```lammergeier
Fmt.grouped(1234567)                    # "1,234,567"
Fmt.grouped(1234567.5)                  # "1,234,567.5"
Fmt.grouped(1360.0, ".", 2, ",")        # "1.360,00"   (Italian, German...)
Fmt.grouped(136000 / 100.0, ",", 2)     # "1,360.00"   (English)
Fmt.grouped(1234567, " ")               # "1 234 567"
Fmt.grouped(5, decimals=2)              # "5.00"
```

For a `float`, `decimals` digits are written after the point, rounded like
`formatFloat`; a negative `decimals` (the default) writes the shortest text
that reads back as the same float, never with an exponent. For an `int`,
`decimals` (default `0`) adds that many zeros. The sign comes first
(`"-1.234"`); NaN and infinities are `"NaN"`, `"+Inf"`, `"-Inf"`.

### `lamcsv` — `Csv`, `CsvReader`

CSV parsing, formatting, and lazy file streaming using Go's `encoding/csv`.

```lammergeier
from lamcsv import Csv
from lamerrors import Result

rows: list[list[str]] = Csv.parseAll("a,b\n1,2\n3,4")?
out: str = Csv.formatAll([["x", "y"], ["1", "2"]])

for rowResult in Csv.rows("large.csv") {
    row: list[str] = rowResult?
    print(row)
}
```

| Method | Returns |
|--------|---------|
| `Csv.parseAll(content, delimiter = ",")` / `Csv.parseFile(path, delimiter = ",")` | `Result[list[list[str]]]` |
| `Csv.rows(path, delimiter = ",")` / `Csv.streamRows(...)` | lazy generator yielding `Result[list[str]]` |
| `Csv.batches(path, batchSize = 100, delimiter = ",")` / `streamBatches(...)` | lazy generator yielding `Result[list[list[str]]]` |
| `Csv.formatRow(fields, delimiter = ",")` | `str` (no trailing newline) |
| `Csv.formatAll(rows, delimiter = ",")` | `str` (every record ends with `\n`) |

The file stays open only while a loop reads it: leaving a loop over `Csv.rows`
/ `Csv.batches` early (`break`, `return`, `?`, an exception) closes it at once.

Every reader and writer takes a `delimiter`: `";"` for the spreadsheets of
locales that write decimal commas, `"\t"` for TSV, any single character other
than `"`, `\r`, `\n` and the Unicode replacement character (another value
throws a `CsvDelimiterError`):

```lammergeier
out: str = Csv.formatAll([["name", "price"], ["Borsa; pelle", "1.200,50"]], ";")
# name;price
# "Borsa; pelle";1.200,50
rows: list[list[str]] = Csv.parseAll(out, ";")?
```

Quoting follows RFC 4180: a field is quoted when it contains the delimiter,
`"`, `\r` or `\n`, starts with white space or is exactly `\.`, and a `"`
inside it is doubled. Reading accepts quoted fields spanning lines and
`\r\n` record ends, and requires every record to have as many fields as the
first (a `CsvParseError` otherwise).

`CsvReader.open(path, delimiter = ",") -> Result[CsvReader]` provides explicit streaming control;
`readRow() -> Result[list[str]]` sets `reader.eof` and returns `Ok([])` at EOF,
and `close() -> Result[bool]` is idempotent. Generator open/read/parse/close
failures are yielded once as `Result.Err`, so callers can process large files
without retaining them in memory.

### Native `json` and `lamjson` — `Json`

The native `json` type is backed by `lamjson` and only contains real JSON
values: null, booleans, strings, finite numbers, arrays, string-keyed objects,
and nested values. Classes implement `__json__() -> json` and are converted with
`json(instance)`. `Json` handles wire encoding/decoding and explicit conversion
to/from ordinary Lam collections.

```lammergeier
from lamerrors import Result
from lamjson import Json

obj: json = Json.decode('{"name":"alice","roles":["admin"]}')?
name: str = obj.name
obj["active"] = true
out: str = Json.encode(obj)?
pretty: str = Json.encodePretty(obj)?

asDict: dict[str, any] = Json.toDict(obj)?
back: json = Json.fromValue(asDict)?
```

| Method | Returns |
|--------|---------|
| `Json.encode(data)` | `Result[str]` |
| `Json.encodePretty(data)` | `Result[str]` |
| `Json.decode(s)` | `Result[json]` |
| `Json.fromValue(value)` / `fromDict(value)` / `fromList(value)` | `Result[json]` after validation |
| `Json.toDict(data)` / `Json.toList(data)` | `Result[dict[str, any]]` / `Result[list[any]]` |
| `Json.kind(data)` | `str`: `null`, `boolean`, `string`, `number`, `array`, or `object` |
| `Json.decodeInto(s, target)` | `Result[any]` (Go/class compatibility) |
| `Json.isValid(s)` | `bool` |
| `Json.has(doc, key)` | `bool`: `doc` is an object with the member (even null) |
| `Json.getStr(doc, key, fallback = "")` | `str` |
| `Json.getInt(doc, key, fallback = 0)` | `int` |
| `Json.getFloat(doc, key, fallback = 0.0)` | `float` |
| `Json.getBool(doc, key, fallback = false)` | `bool` |
| `Json.getList(doc, key)` | `list[json]` (`[]` when not an array) |
| `Json.getObject(doc, key)` | `json` (a new `{}` when not an object) |
| `Json.at(doc, path)` | `json` at a path of keys / indexes, null when missing |
| `Json.mapKeys(doc, rename, deep = true)` | `json` copy with renamed object keys |
| `Json.copy(doc)` | an independent deep copy of `doc` |

Typed member reads are tolerant, for request bodies and stored documents:
the member comes back when it has the requested kind, `fallback` otherwise
— a missing member, null, another kind, or a `doc` that is not an object.

```lammergeier
body: json = Json.decode(req.body)?
name: str = Json.getStr(body, "name")                  # "" when absent
page: int = Json.getInt(body, "page", 1)
city: str = Json.getStr(Json.getObject(body, "address"), "city")
lastId: int = Json.getInt(Json.at(body, ["orders", -1]), "id")
api: json = Json.mapKeys(row, Strings.camelCase)       # slots_total -> slotsTotal
```

- `getInt` takes integers and floats with an integral value inside the
  `int` range (`3.0` is `3`; `3.5` and `1e300` are `fallback`); `getFloat`
  takes any number. Strings are never converted (`"12"` is `fallback`: use
  `Conv.parse`), and `getBool` takes only `true` / `false`.
- `getList` and `getObject` return the document's own containers, so a
  change made through them changes `doc`; the `{}` of a missing object is new.
- `Json.at(doc, ["user", "tags", 0])`: `str` steps read members, `int` steps
  array elements (negative from the end); a missing step gives null.
- `Json.mapKeys` copies the document; with `deep` (the default) nested
  objects, also inside arrays, are renamed too, with `deep=false` only the
  keys of `doc` itself. Keys are renamed in sorted order: when two keys get
  the same name, the later key's value wins.

json values are shared like Python objects, never copied behind your back: a
json value put into a json literal (`{"user": user, "items": items}`),
returned by `__json__` and converted with `json(x)`, or assigned to a member
(`doc.user = user`) is the same value in both places (a change through one is
seen through the other), so documents nest at no cost per level. Lam
collections (`dict`, `list`, `list[json]`) are converted, which copies them.
`Json.copy(doc)` gives an independent copy.

`Json.encode`, `str(doc)`, f-strings, markup slots and the Go marshaler
(`encoding/json`, HTTP responses, database parameters) write the same bytes as
`encoding/json` (sorted keys, `<` `>` `&` escaped as `\u003c` `\u003e`
`\u0026`, U+2028/U+2029 escaped, invalid UTF-8 as `\ufffd`, its number
format), directly into one pooled buffer: one allocation for the text.

`Json.fromValue` is the runtime boundary for dynamic `any`; `fromDict` and
`fromList` remain compatibility aliases that delegate to it. The conversion
rejects numeric map keys, non-finite floats, cycles/excessive nesting, sets, functions, and
classes without `__json__`. Native values implement Go JSON marshaling and
`database/sql` value/scanner interfaces, so they can be passed directly as
JSON/JSONB query parameters.

### `lamurl` — `Url`

URL parsing, percent-encoding and query strings.

```lammergeier
from lamurl import Url
from lamerrors import Result

host: str = Url.host("https://example.com:8080/path?q=1")?
encoded: str = Url.encode("hello world")
decoded: str = Url.decode(encoded)?
link: str = "/search?" + Url.query({"q": ["borsa pelle"], "tag": ["a", "b"]})
# /search?q=borsa+pelle&tag=a&tag=b
params: dict[str, list[str]] = Url.parseQuery("q=a+b&tag=x&tag=y")?
```

| Method | Returns |
|--------|---------|
| `Url.parse(rawUrl)` | `Result[str]` |
| `Url.scheme(rawUrl)` / `host(rawUrl)` / `path(rawUrl)` | `Result[str]` |
| `Url.queryParam(rawUrl, key)` | `Result[str]`; a missing key is `Ok("")` |
| `Url.encode(s)` | `str` — a query value, form encoded (space is `+`) |
| `Url.decode(s)` | `Result[str]` |
| `Url.encodeComponent(s)` | `str` — JavaScript's `encodeURIComponent` |
| `Url.decodeComponent(s)` | `Result[str]` (`+` stays a plus) |
| `Url.encodePath(s)` | `str` — a whole path, `/` kept |
| `Url.encodePathSegment(s)` | `str` — one segment, `/` escaped |
| `Url.encodeUserInfo(s)` | `str` — a user name or password |
| `Url.query(params)` | `str` — from `dict[str, list[str]]`, `dict[str, str]` or `[key, value]` pairs |
| `Url.parseQuery(s)` | `Result[dict[str, list[str]]]` |

Which escaping goes where:

- `encodeComponent` leaves only `A-Z a-z 0-9 - _ . ! ~ * ' ( )` unescaped and
  writes every other byte of the UTF-8 text as `%XX` (`"a b&c/é"` ->
  `"a%20b%26c%2F%C3%A9"`), exactly as JavaScript's `encodeURIComponent`: for
  values put into URLs that browsers or JavaScript code decode.
- `encodePath` escapes a path but keeps `/` and `$ & + , : ; = @`
  (`"/files/a b/ü.txt"` -> `"/files/a%20b/%C3%BC.txt"`); `?` and `#` are
  escaped. `encodePathSegment` also escapes `/` (`"a/b"` -> `"a%2Fb"`).
- `encodeUserInfo` escapes `@ / ? :` and the characters outside a URL, for
  `postgres://{user}:{password}@host/db`.
- `Url.query` form-encodes keys and values (`encode`) and joins them with
  `&`, without a leading `?`. A dict gives its keys in sorted order and each
  key's values in order (a key with no values is left out); a list of
  `[key, value]` pairs keeps their order and repeated keys (a pair with
  another number of items throws a `UrlQueryError`).
- `Url.parseQuery` ignores a leading `?`, reads `key=value` pairs separated
  by `&` (a key without `=` has the value `""`) and decodes `+` and `%XX`; a
  malformed escape or a `;` separator is a `UrlQueryError`.

### `lambase64` — `Base16`, `Base32`, `Base64`, `Hex`

```lammergeier
from lambase64 import Base16, Base32, Base64
from lamerrors import Result

hexText: str = Base16.encode("data")
plain: str = Base16.decode(hexText)?
base32: str = Base32.encodeRaw("data")       # no padding
base64url: str = Base64.encodeURLRaw("data") # URL alphabet, no padding
```

All encoders return `str`; all decoders return `Result[str]` for malformed
input. `Base64` provides standard/raw and URL/raw-URL pairs (`encode`,
`encodeRaw`, `encodeURL`, `encodeURLRaw` and matching decoders), with
`encodeRawURL` / `decodeRawURL` compatibility aliases. `Base32` provides the
same padded/raw operations for the standard and extended-hex alphabets, with
`encodeRawHex` / `decodeRawHex` aliases. `Base16` is hexadecimal; `Hex`
remains a compatibility alias for it.

### `lambytes` — `Bytes`

```lammergeier
from lambytes import Bytes
from lamerrors import Result

b: list[int] = Bytes.toBytes("hello")
s: str = Bytes.fromBytes(b)
hx: str = Bytes.hexEncode("abc")
raw: str = Bytes.hexDecode(hx)?
```

`toBytes`, `fromBytes`, and `hexEncode` are infallible. `hexDecode` returns
`Result[str]`; `contains` and `count` retain their boolean/integer contracts.

The native `bytes` type needs no import: `bytes(text)` is the UTF-8 encoding,
`str(data)` decodes it, `bytes(codes)` converts a `list[int]`, and indexing,
slicing, `+`, `==` and `in` work on the value (see "Bytes" in `SYNTAX.md`).

### `lami18n` — `Translations`

Translated texts per language, read from JSON files and looked up by dotted
key, with `{name}` parameters:

```text
i18n/
  en/common.json   {"App": {"name": "Luxyt", "welcome": "Hello <b>{name}</b>!"},
                    "Feed": {"title": "Feed", "empty": "No posts yet"}}
  en/mail.json     {"Mail.subject": "Welcome {name}", "Feed.perPage": 20}
  it/common.json   {"App": {"welcome": "Ciao <b>{name}</b>!"}, "Feed": {"title": "Bacheca"}}
```

```lammergeier
from lami18n import Translations
from lamerrors import Result

func setup() -> Result[Translations] {
    texts: Translations = Translations.load("i18n", ["en", "it"], "en")?
    lang: str = texts.negotiate("it-IT,it;q=0.9,en;q=0.8")    # "it"
    print(texts.raw(lang, "Feed.title"))                      # Bacheca
    print(texts.raw(lang, "Feed.empty"))                      # No posts yet (English fallback)
    print(texts.raw(lang, "Feed.nope"))                       # Feed.nope
    params: dict[str, str] = {"name": "Ada <3"}
    print(texts.text("en", "Mail.subject", params))           # Welcome Ada <3
    page: html = <><header>${texts.markup(lang, "App.welcome", params)}</header></>
    print(page)                                               # <header>Ciao <b>Ada &lt;3</b>!</header>
    print(texts.plain(lang, "App.welcome", params))           # Ciao Ada <3!
    print(texts.missing("en", "it"))                          # [App.name Feed.empty Feed.perPage Mail.subject]
    return Result.Ok(texts)
}
```

**Loading.** `Translations.load(dir, languages, fallback = "") ->
Result[Translations]` reads, for each language, every `<dir>/<lang>/*.json`
file in name order (other files, hidden files and subdirectories are
ignored). A file is a JSON object; nested objects become dotted keys at any
depth (`{"Feed": {"title": ...}}` and `{"Feed.title": ...}` are the same
key), numbers and booleans become their JSON text (`20`, `2.50`, `true`).
These are `TranslationError` results naming the file, and the line and key
when known, so a broken translation stops the program at start-up:

- a key defined twice for one language, in one file or in two (`en/b.json:
  key "Feed.title" is already defined in en/a.json`), including a duplicate
  member of one object, which JSON decoders usually drop silently;
- malformed JSON, an empty file or data after the object (`en/a.json:3:
  invalid JSON: ...`), a top level that is not an object;
- arrays and `null` values (one key per text: plural forms are separate
  keys such as `Feed.count.one` / `Feed.count.other`) and empty keys;
- a missing, unreadable or non-directory `dir`, a `<dir>/<lang>` that is a
  file, and
  invalid `languages` (none, an empty, padded or duplicate code, a
  `fallback` that is not listed).

A missing `<dir>/<lang>` directory is not an error: that language has no
texts yet and every lookup falls back, so a language can be listed before it
is translated. A leading UTF-8 byte order mark is accepted.

**Building in code.** `Translations(languages, fallback = "")` starts empty
(invalid arguments are a `ValueError`); `add(lang, key, text) ->
Result[bool]` adds one text and `merge(lang, texts: dict[str, str]) ->
Result[int]` adds a map, all or nothing. An unsupported language, an empty
key and a key already defined for that language are `TranslationError`s.

**Languages.** `languages` are the supported codes in order; the first is
the default. `fallback` is the language consulted when a key is missing in
the requested one; `""` means the first language (pass it explicitly, e.g.
`"en"`, when the default display language is not the most complete one).
Lookups use the codes exactly as given: normalise user input with
`language(candidate)` (the same code ignoring case and `_` versus `-`, else
the language equal to its primary subtag, `en-US` -> `en`, else the first
language sharing its primary subtag, `pt` -> `pt-BR`, else the first
language) or `negotiate(header)` for an `Accept-Language` value (ranges in
`q` order, header order breaking ties, each matched like `language`; `q=0`
refuses a language, `*` is the first language not refused, a malformed `q`
ignores its range, and with no match the result is the first language not
refused).

| Method | Result |
| --- | --- |
| `raw(lang, key) -> str` | the text in `lang`, else in the fallback language, else `key` itself: a missing text shows up but never fails |
| `text(lang, key, params = {}) -> str` | `raw` with each `{name}` whose name is a key of `params` replaced by its value verbatim; plain text (mail subjects, log lines, values passed to markup slots, which escape them) |
| `markup(lang, key, params = {}) -> html` | `raw` read as trusted markup (translations may contain tags and entities) with every parameter value HTML-escaped before it is inserted |
| `plain(lang, key, params = {}) -> str` | the text content of `markup`: tags removed, entities decoded (plain-text mail bodies) |
| `has(lang, key) -> bool` | `key` has a text in `lang` itself, without fallback |
| `count(lang = "") -> int` | the texts of `lang`; `""` counts all languages |
| `keys(lang) -> list[str]` | the keys of `lang`, sorted |
| `missing(lang, other) -> list[str]` | the keys of `lang` that `other` lacks, sorted (a CI completeness check: `missing("en", "it")`) |
| `languages() -> list[str]`, `fallback() -> str` | the supported languages, the fallback language |

Placeholders other than the given parameter names stay as written
(`{count}` without a `count` parameter, `{}`, `{ name }`), and an inserted
value is not scanned again, so a parameter containing `{name}` stays
literal. Unsupported languages behave as languages without texts. A loaded
`Translations` is only read and can be shared by concurrent requests; `add`
and `merge` are not synchronised and belong to set-up, before the value is
shared.

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

Generic descriptive statistics over homogeneous numeric or ordered lists.

```lammergeier
from lamstats import Stats

values: list[float] = [1.0, 2.0, 3.0, 4.0]
mean: float = Stats.mean(values)
sd:   float = Stats.stddev(values)
p95:  float = Stats.percentile(values, 95.0)   # p is in [0, 100]
```

All 12 helpers are generic. `sum`, `product`, and `range_` accept
`list[T: number]` and preserve `T`; `mode`, `minVal`, and `maxVal` accept
`list[T: ordered]` and preserve `T`. `mean`, `median`, `variance`,
`sampleVariance`, `stddev`, and `percentile` accept numeric lists and return
`float`.

### `lamrandom` — `Random`

PRNG + cryptographic randomness.

```lammergeier
from lamrandom import Random
from lamuuid import Uuid
from lamerrors import Result

n: int = Random.randInt(1, 100)?
hex_: str = Random.secureToken(16)?  # crypto-rand
uuid: str = Uuid.v4()?              # UUID ownership lives in lamuuid
```

`randFloat`, `randFloatRange`, `randBool`, `seed`, and `shuffle` are
infallible. `randInt`, `choice`, `sample`, `randomString`, `randomHex`,
`secureBytes`, `secureToken`, and `secureInt` return typed Results for invalid
ranges, sizes, empty populations, or entropy failures. UUID generation was
removed from `Random`; use `Uuid.v4()` or `Uuid.v7()`.

### `lamarray` — `Array`, `Matrix`

NumPy-style numerics backed by **gonum**.

```lammergeier
from lamarray import Array, Matrix
from lamerrors import Result

x: Array = Array.linspace(0.0, 1.0, 100)
y: Array = x.mulScalar(2.0).addScalar(1.0)

A: Matrix = Matrix.fromRows([[1.0, 2.0], [3.0, 4.0]])
b: Array = Array.fromList([5.0, 11.0])
solution: Array = A.solve(b)?   # Ax = b via LU
inverse: Matrix = A.inverse()?
```

`Matrix.solve(b) -> Result[Array]` and `Matrix.inverse() -> Result[Matrix]`
report shape and singularity errors instead of panicking. Other constructors,
element-wise operations, and reductions retain their direct return types.

`Array` stores 1-D `float64` values for gonum, but generic `full`, `arange`,
`linspace`, and `fromList` accept homogeneous `int` or `float` inputs and
convert once at the gonum boundary. Other constructors are `zeros`, `ones`,
and `random`; element-wise operations are `add`/`sub`/`mul`/`div`,
scalar versions, `neg`/`sqrt`/`exp`/`log`/`absVal`/`pow`; reductions
`sum`/`mean`/`std`/`sampleStd`/`variance`/`minVal`/`maxVal`/`argMin`/`argMax`; `dot`,
`norm`, `slice`, `reverse`, `sortAsc`, `equals`, `allClose`,
`toString`, `toList`.

`Matrix` (dense 2-D) similarly accepts generic numeric values in `fromList`
and `fromRows`; `zerosM` and `eye` are the other constructors. Operations are
`transpose`, `addM`/`subM`, `scale`, `matmul`,
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

# Native JSON and generic typed columns use the primary method names.
rows: json = [{"name": "Ada"}, {"name": "Lin"}]
nativeDf: DataFrame = DataFrame.readJSON(rows)
scores: list[int] = [10, 12]
enriched: DataFrame = nativeDf.mutate("score", scores)
scoreSeries: Series = Series.from(scores, "score")
wireValue: json = json(enriched)
```

**DataFrame surface.** Constructors `fromRecords` (header + rows),
`fromMaps`, `readCSV`, and overloaded `readJSON(str | json)`.
Introspection `nrow`, `ncol`,
`dims`, `names`, `types`, `describe`, `toString`, `error`. Access
`col`, `elem`, `records`, `maps`, `copy`. Subsetting `head`, `tail`,
`slice`, `subset`, `selectCols`, `dropCols`. Mutation `rename`,
`setNames`, and overloaded `mutate(name, values)` for dynamic, int, float,
string, and bool lists. Filtering `filter(col, op, value)` plus
sugar `filterEq` / `filterNeq` / `filterGt` / `filterGte` /
`filterLt` / `filterLte` / `filterIn`. Sorting `sort` and multi-key
`sortBy`. Joins `innerJoin`, `leftJoin`, `rightJoin`, `outerJoin`,
`crossJoin`. Combine `rbind`, `cbind`, `concat`. I/O `writeCSV`,
`writeJSON`, `jsonValue() -> json`, and native `json(frame)` through
`__json__`. `writeCSV() -> Result[str]` and `writeJSON() -> Result[str]`
preserve dataframe and encoder failures. `readJsonValue(json)` remains an
alias for the native `readJSON` overload. Group-by via
`groupBy(cols) -> DataFrameGroups`.

**Series surface.** Generic `Series.from(values, name)` supports int, float,
string, and bool lists. Introspection `name`, `typeName`, `length`, `records`,
`isNaN`, `error`, `toString`. Access `at(i)`,
`elemStr(i)`. Aggregations `sum`, `mean`, `median`, `stddev`,
`minVal`, `maxVal`, `quantile(p)`. Conversion `toStringList`,
`toFloatList`, plus Result-returning `toIntList` and `toBoolList` so invalid
coercions are explicit. Derived `copy`, `slice`, `order(reverse)`.

**DataFrameGroups surface.** `aggregate(types, cols)` (strategies
`"max"`, `"min"`, `"mean"`, `"median"`, `"std"`, `"sum"`,
`"count"`), `getGroups()` (map of group-key → partition),
`error()`.

**Error handling.** Every operation that can fail stores its error
on the returned DataFrame / Series / Groups rather than panicking;
use `.error()` to read it (empty string means success). This
matches the upstream `gota` convention.

**Typed-list convention.** Lam emits `list[str]` as `[]string` and
`list[int]` as `[]int`. Generic calls such as `Series.from([1, 2], "score")`
infer the literal element type. For non-generic APIs with nested or dynamic
collections, a pre-declared typed variable remains the clearest shape.

### `lamconv` — `Conv`

String parsers with structured failures.

```lammergeier
from lamconv import Conv
from lamerrors import Result

parsedInt: Result[int] = Conv.parse("42")
parsedFloat: Result[float] = Conv.parse("3.14")
parsedBool: Result[bool] = Conv.parse("true")
parsedHex: Result[int] = Conv.fromHex("ff")
```

`parse(s)` has three identical-parameter overloads selected by the contextual
payload type: `parse(s) -> Result[int]`, `parse(s) -> Result[float]`, and
`parse(s) -> Result[bool]`. Give the result a payload-typed annotation (or an
equally specific enclosing return context) whenever the surrounding expression
would otherwise be ambiguous. Formatting belongs to `Fmt` and the `str(...)`
builtin.

`Conv.parse` for a float follows Go's `strconv.ParseFloat`, so it accepts
`"NaN"`, `"Inf"` and `"-Infinity"`. `Conv.parseFinite(s) -> Result[float]`
accepts the same decimal, exponent and hexadecimal notations but only finite
values, without the `_` separators of Go literals: `"NaN"`, `"inf"`, `"1e400"`
(overflow) and `"1_000"` are a `ParseError`. Use it for numbers typed by people
or read from files; surrounding spaces are not accepted (`s.trim()` first).

```lammergeier
price: float = Conv.parseFinite(form.get("price").trim())?
```

The base parsers intentionally retain `fromHex` / `fromOctal` / `fromBin`.
All three have the same `(str) -> Result[int]` type but different radix
semantics, so no return context could choose among them. Their explicit names
state the intended base rather than depending on runtime or declaration-order
fallback.

---

## Collections & iterators

### `lamdatastructures` — canonical collection

`from lamdatastructures import Queue, Deque, Stack, Set, Heap, PriorityHeap`
is the canonical import. `lamqueue`, `lamdeque`, `lamstack`, `lamset`, and
`lamheap` are compatibility shims that re-export those same classes; they do
not contain duplicate implementations. `lamqueue` also re-exports `Deque`.

### `lamstack` — `Stack` compatibility import

LIFO. `push`, `size`, `isEmpty`, `clear`, and `toList` are direct;
`pop()` and `peek()` return `Result[any]` with `EmptyStackError` on emptiness.

### `lamqueue` — `Queue`, `Deque` compatibility import

FIFO. `enqueue`, `size`, `isEmpty`, `clear`, and `toList` are direct;
`dequeue()` and `peek()` return `Result[any]` with `EmptyQueueError`.

### `lamdeque` — `Deque` compatibility import

Double-ended. `pushBack`/`pushFront`, `size`, `isEmpty`, `clear`, and `toList`
are direct. `popBack`/`popFront` and `peekBack`/`peekFront` return
`Result[any]` with `EmptyDequeError`.

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
from lamdatastructures import Heap, PriorityHeap
from lamerrors import Result

h: Heap = Heap()                   # min-heap by default
h.push(3); h.push(1); h.push(2)
smallest: int = h.pop()?           # 1

pq: PriorityHeap = PriorityHeap(true)   # max-heap
pq.push(9.0, "urgent")
pq.push(1.0, "later")
top: str = pq.pop()?               # "urgent"
```

`Heap.push` is overloaded for homogeneous `int` or `float` values;
`PriorityHeap.push(priority, value)` overloads the numeric priority while the
value remains `any`. `size`, `isEmpty`, and `clear` are direct. Both `pop` and
`peek` return `Result[any]`, preserving concrete numeric heap values at runtime;
empty heaps produce `EmptyHeapError`. Switch min/max via the `maxHeap`
constructor flag.

### `lamcache` — `LruCache`, `TtlCache`

Goroutine-safe in-memory caches. Public synchronization uses
`lamconcurrency.Mutex`; raw Go remains only around the unavoidable
`container/list`, map, and clock operations.

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

### `lamiter` — `Iter[T]`

Typed lazy iterator combinators: values flow through the chain one at a
time when a terminal operation runs (`take` / `takeWhile` stop pulling).

```lammergeier
from lamiter import Iter

evens: list[int] = Iter.range_(0, 100).filter(lambda x: x % 2 == 0).map(lambda x: x * x).toList()
names: list[str] = Iter.fromList(users).map(lambda u: u.name).filter(lambda n: n.startsWith("a")).toList()
total: int = Iter.fromList([1, 2, 3]).reduce(0, lambda acc, x: acc + x)
lens: list[int] = Iter.fromList(words).map(len).toList()
Iter.fromList(["a", "b"]).enumerate().forEach(lambda pair: print(pair))   # [0 a], [1 b]
```

`Iter` is generic over its element type, and every callback is typed from
it, so lambda parameters need no annotation or conversion and a callback
of the wrong type is a compile error:

| API | Type |
|---|---|
| `Iter.fromList(values: list[T])`, `Iter.repeat(value: T, times)` | `Iter[T]` |
| `Iter.range_(start, stop)` (half-open), `Iter.count(start, step)` (infinite) | `Iter[int]` |
| `map[U](fn: func(T) -> U)` | `Iter[U]` |
| `filter` / `takeWhile` / `dropWhile` (`func(T) -> bool`), `take(n)`, `drop(n)`, `chain(other: Iter[T])` | `Iter[T]` |
| `enumerate()` | `Iter[tuple[int, T]]` (prints `[0 a]`; destructure with `i, v = pair` or `for i, v in it.enumerate().toList()`) |
| `toList()` | `list[T]` |
| `forEach(fn: func(T))` | — |
| `reduce[A](initial: A, fn: func(A, T) -> A)` | `A` (the initial value's type: `reduce(0.0, ...)` is a `float`) |
| `count_()` | `int` |
| `first()` | `Result[T]` (`EmptyIteratorError` when the iterator is empty) |
| `anyMatch` / `allMatch(predicate: func(T) -> bool)` | `bool` (short-circuit) |

An `Iter[any]` (from a `list[any]`) takes `func(any) -> ...` callbacks;
convert inside them (`int(x)`) as before. A local annotated with the bare
class takes its initializer's type (`it: Iter = Iter.range_(0, 5)` is an
`Iter[int]`); parameters and fields spell it (`it: Iter[int]`).

### `lamsort` — `Sort`

Generic, non-mutating sorting for every naturally ordered Lam type and for
arbitrary classes through a typed three-way comparator. `direction` is
`"asc"` by default and accepts only `"asc"` or `"desc"`.

```lammergeier
from lamsort import Sort

asc: list[int] = Sort.sort([3, 1, 2])
desc: list[str] = Sort.sort(["a", "c", "b"], "desc")

class Job {
    func init(self, priority: int) { self.priority: int = priority }
}

jobs: list[Job] = [Job(3), Job(1), Job(2)]
# The comparator's parameters are typed from the list (`Job`).
byPriority: list[Job] = Sort.sortWith(jobs, lambda left, right: left.priority - right.priority)

if Sort.isSorted(asc, "asc") { print("ordered") }
if Sort.isSortedWith(byPriority, lambda a, b: a.priority - b.priority) {
    print("custom order")
}

# Generator form computes and yields each next ordered value incrementally.
for value in Sort.iterSorted([3, 1, 2]) {
    print(value)
}
```

A comparator returns a negative number when `left` precedes `right`, zero when
they are equivalent, and a positive number when `right` precedes `left`.
Comparator-aware APIs use the `With` suffix.

| API | Purpose |
|---|---|
| `sort(values, direction="asc")` / `sortWith(values, compare, direction="asc")` | Default stable merge sort. |
| `isSorted(...)` / `isSortedWith(...)` | Check ordering in the requested direction. |
| `iterSorted(...)` / `iterSortedWith(...)` | Lazy selection generator; copies the input and yields one next value at a time. |
| `bubble` / `bubbleWith` | Bubble sort; useful for teaching or tiny nearly sorted inputs. |
| `insertion` / `insertionWith` | Insertion sort; stable and effective for small inputs. |
| `selection` / `selectionWith` | Selection sort with predictable swaps. |
| `merge` / `mergeWith` | Stable O(n log n) merge sort. |
| `quick` / `quickWith` | Pivot-based quick sort. |
| `reverse(values)` | Return a reversed generic copy without sorting. |

All eager algorithms and generators copy their input; none mutate the caller's
list. Invalid directions throw `ValueError`.

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
    return Result.Ok(a // b)
}

fallback: any = divide(10, 0).unwrapOr(0) # 0
value: any = divide(10, 2).unwrap()       # 5
```

`Error(kind, message, cause=None)` stores `.kind`, `.message`, and
`.cause`; `str(error)` renders `Kind: message` and appends
` (cause: ...)` when there is a cause whose text differs from the message
(an empty `kind` renders just the message: that is how the `err` of
`do { } catch err` wraps a non-`Error` value). `lamdb` keeps the driver's
error as the cause of its failures (`Db.sqlState` reads it); since its text
is the message, those errors print as before.

`Result` is non-generic at runtime. Both `.value` and `.error` are typed
`any`, and every `Result[T]` lowers to the same Go `*Result`. The compiler does
not erase `T` from its contextual overload key, however: `Result[int]`,
`Result[float]`, and `Result[bool]` use distinct compile-time payload keys even
though their runtime representation is identical. Those keys select
return-type overloads and improve propagation inference; payload-typed locals
also make otherwise ambiguous call sites explicit. The implemented runtime API
is:

| Signature | Meaning |
|---|---|
| `Result.Ok(v: any) -> Result` | Success. Stores `v` in `.value` and `None` in `.error`. |
| `Result.Err(e: any) -> Result` | Failure. Stores `None` in `.value` and `e` in `.error`. |
| `r.ok() -> bool` | `true` when `.error == None`. |
| `r.unwrap() -> any` | Returns `.value`; throws `RuntimeError` if this is an error. |
| `r.unwrapOr(fallback: any) -> any` | Returns `.value`, or `fallback` on error. |

`Result.Err` accepts any value because `.error` is `any`: structured
`Error` objects, plain strings, dicts, Go errors captured through `go!`, or
domain payloads.

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
`any`; `r.error.kind`, `.message` and `.cause` read the error as an `Error`
(another value is viewed as `Error("", str(v), v)`, like the `do/catch`
binding). Keep them as `any` when forwarding/logging, use `?` into an
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

Assertions are statements: each prints `PASS` / `FAIL` and updates the
totals. The `assert*` helpers also return whether the check passed (`if
Test.assertEqual(a, b) { ... }`) and `summary()` the failure count, but an
unused result is not a dropped value: `Test.assertTrue(...)` and
`Test.summary()` as statements raise no `unused` warning (no `_ =` needed).

`assertEqual`, `assertNotEqual` and `assertContains` compare values:
numbers by value whatever their type (`2.0` equals `2`), lists and dicts
element by element, so `Test.assertEqual(scores(), [80, 90, 100])` passes
for a `scores() -> list[int]` even though the literal argument is untyped.

### `lamlog` — `Log`

Levelled logging on top of `log`.

```lammergeier
from lamlog import Log

Log.setPrefix("[api] ")
Log.info("server starting")
Log.errorf("could not connect: %s", "db unreachable")
```

11 helpers, all static: `debug`, `info`, `warn`, `error`, `fatal` (one
message each), their `printf`-style variants `debugf`, `infof`, `warnf`,
`errorf`, `fatalf` (format string plus arguments), and `setPrefix`. `fatal`
/ `fatalf` exit the process after logging.

---

## Concurrency

### `lamconcurrency` — `Channel`, `WaitGroup`, `Mutex`, `RWMutex`, `Atomic`

Goroutine-safe primitives wrapping Go's `sync` package. Raw Go channels stay
inside `lamconcurrency`; callers coordinate through Lam methods and `await`.

```lammergeier
from lamconcurrency import Channel
from lamerrors import Result

ch: Channel = Channel(1)
sendHandle = ch.asyncSend("ready")
received: Result[any] = await ch.asyncReceive()
sent: Result[bool] = await sendHandle

if received.ok() and sent.ok() {
    print(received.value)
}
```

`Channel(size)` creates a bounded (`size > 0`) or rendezvous (`size == 0`)
channel; `Channel.unbounded()` creates a FIFO that always accepts while open.
`send(val) -> Result[bool]` blocks until delivery or buffer space for bounded
channels and enqueues immediately for unbounded channels. `receive() ->
Result[any]` blocks until a value arrives; a sent `None` is `Ok(None)`, while a
closed and drained channel is a `ChannelClosed` error. `receiveTimeout(timeoutMs)
-> Result[any]` adds a `ChannelTimeout` outcome without conflating it with
`Ok(None)`.

`poll() -> Result[any]` is immediate and separates `ChannelEmpty`,
`ChannelClosed`, and a successfully sent `None`. `offer(val) -> Result[bool]` is
the immediate send counterpart: `Ok(true)` sent, `Ok(false)` means no receiver
or bounded buffer slot was ready, and a closed channel is an error. `size()`
reports queued values and `isClosed()` reports close state. `close() ->
Result[bool]` reports repeated or concurrent closes while preserving queued
values for drain.

`asyncSend` and `asyncReceive` are Lam-defined async instance methods around the
blocking operations; they return one-slot handles that may be awaited directly
or later. Async methods do **not** replace `poll` or `offer`, which remain
necessary when the caller must decide immediately rather than wait.

`WaitGroup`: `add`, `done`, `wait`.
`Mutex` / `RWMutex`: `lock`, `unlock`, plus `rLock`/`rUnlock` on RW.
`Atomic`: `get`, `set`, `increment`, `decrement`, `add`,
`compareAndSwap` over `int64`.

### `lamcontext` — `Context`

`Context` carries cancellation, expiration, and inherited values through a tree
of operations. The API is intentionally Lam-first: derive a cancellable or
expirable child, attach request data, and ask direct questions such as
`expired()`, `expiresIn(unit)`, or `reason()` instead of managing Go channels.

```lammergeier
from lamcontext import Context
from lamerrors import Result

root: Context = Context.simple().valued("requestId", "req-42")
work: Context = root.expirable(2, "seconds")?
work["user"] = "Ada"

if not work.expired() and "requestId" in work {
    print(work["requestId"])
    print(work.expiresIn("milliseconds")?)
}

work.cancel("user left")
print(work.reason())
```

`expirable(unixMs, None)` creates an absolute expiry from a Unix-millisecond
timestamp. Supplying a unit instead creates a relative expiry; accepted units
are nanoseconds, microseconds, milliseconds, seconds, minutes, hours, and days,
including their common abbreviations. `expiresIn(None)` returns the absolute
Unix-millisecond expiry, while `expiresIn(unit)` returns the remaining duration
rounded up.

Parent cancellation reaches every descendant, while cancelling a child leaves
its parent and siblings alive. `valued` creates an immutable child layer; `set`
mutates only the current layer. Values, cancellation, and native Go
`context.Context` interop are goroutine-safe.

| Method | Purpose |
|--------|---------|
| `Context.simple()` | Live root context. |
| `Context.cancelled(reason)` | Already-cancelled context, useful in tests and shutdown paths. |
| `Context.fromNative(value)` / `native()` | `fromNative -> Result[Context]` validates the Go context; `native()` returns it. |
| `child()` / `cancellable()` | Independently cancellable descendant. |
| `expirable(expiration, unit=None)` | `Result[Context]`: absolute Unix-ms expiry without a unit, or relative expiry in the supplied unit. |
| `valued(key, value)` / `set(key, value)` | Immutable child value or mutable current-layer value. |
| `get(key, fallback)` / `value(...)` / `has(key)` | Read inherited values without magic reserved keys. |
| `ctx[key]` / `ctx[key] = value` / `key in ctx` | Sugar for `get`, `set`, and `has`. |
| `cancel(reason)` / `expired()` / `reason()` | Cancel or inspect completion. |
| `expiresIn(unit=None)` | `Result[int]`: absolute Unix-ms expiry or remaining duration (`Ok(-1)` means none, `Ok(0)` means elapsed). |
| `wait(timeoutMs)` | Bounded wait; `true` means the context completed first. |
| `Context.onSignals(names = ["SIGINT", "SIGTERM"])` | Root cancelled by the first of the signals, with the signal name as `reason()`. |

`Context.onSignals` is the graceful-shutdown scope of a server or worker pool:
derive the workers' contexts from it, and they all stop when the process gets
SIGINT or SIGTERM (or the names given: `SIGINT`, `SIGTERM`, `SIGHUP`, `SIGQUIT`;
the `SIG` prefix and case are optional, unknown names throw `ValueError`).
After the first signal, or an explicit `cancel`, the signals have their default
effect again, so a second Ctrl-C ends a slow shutdown.

```lammergeier
from lamcontext import Context

shutdown: Context = Context.onSignals()
while not shutdown.wait(1000) {
    runPendingJobs(shutdown.child())
}
print(f"stopping on {shutdown.reason()}")   # stopping on SIGTERM
```

### `lamactor` — `Mailbox`, `ActorRef`, `ActorSystem`

Higher-level message-passing on top of `lamconcurrency`. Each actor
owns a goroutine, a private mailbox, and any state it captures. The
dispatch loop processes one message at a time so handlers never need
their own locks. Mailboxes, ask replies, registry locking, stop state, and
completion compose `Channel`, `Mutex`, `Atomic`, and `WaitGroup`. Native
synchronization stays inside `lamconcurrency`; the actor bridge retains dynamic
handler invocation and goroutine scheduling.
Inspired by [`go-actor`](https://github.com/vladopajic/go-actor), reshaped around
an Erlang/Akka-style `onMessage` convention.

```lammergeier
from lamactor import ActorRef, ActorSystem
from lamerrors import Result

class Counter {
    func init(self) {
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
_ = ref.tell("inc")?
_ = ref.tell("inc")?
total: any = ref.ask("get", 1000)?       # blocks up to 1s for reply
print(total)                             # 2
sys.shutdown(1000)
```

An actor only needs `onMessage(self, msg)`; the dispatcher also calls
`onStart(self)` and `onStop(self)` if defined, plus `onError(self,
err) -> bool` to opt-in to panic recovery (return `True` to stay
alive). All hooks are looked up reflectively, so adding one is a
purely additive change.

`Mailbox(capacity=0)` is a Lam class that delegates to `Channel`.
`capacity=0` selects an unbounded FIFO; positive capacities retain bounded
backpressure. Its public surface is `send` / `receive`, immediate `offer` /
`poll`, awaitable `asyncSend` / `asyncReceive`, and `close` / `isClosed` /
`size`. Message operations use `Result`, so a successful `None` payload stays
distinct from empty, closed, and full outcomes.

`ActorRef.tell(msg) -> Result[bool]` reports stopped actors and full mailboxes.
`ask(msg, timeoutMs) -> Result[any]` distinguishes timeout/stopped failures
from a handler successfully returning `None`; `stop` and `isAlive` remain
direct. Refs are plain data — passing them between actors is how multi-stage
pipelines compose.

`ActorSystem`: `spawn(actor)`, `spawnNamed(name, actor)`, `find(name)`,
`numActors()`, `shutdown(timeoutMs)`. `find` still returns an inactive typed
reference on a miss; `tell` and `ask` on it return `ActorStoppedError`.

---

## Time

### `lamtime` — `Time`

`Time` is a duration value stored as nanoseconds, plus compatibility helpers for
Unix timestamps and measurement.

```lammergeier
from lamtime import Time

delay: Time = Time.Seconds(2).add(Time.Milliseconds(500))
Time.sleep(delay)
start: int = Time.nowUnixNano()
elapsed: Time = Time.elapsedSinceNano(start)
```

Unit factories are `Nanoseconds`, `Milliseconds`, `Seconds`, `Minutes`,
`Hours`, `Days`, `Weeks`, `Months`, and `Years`; months and years use fixed
30-day and 365-day durations. `parseDuration(value) -> Result[Time]` accepts
Go duration text. Instance conversion methods are `inNanoseconds`,
`inMilliseconds`, `inSeconds`, `inMinutes`, `inHours`, and `inDays`; arithmetic
is available through `add`, `subtract`, `multiply`, `negate`, `abs` and the
`+`, `-`, `*`, `==` operators.

`nowUnix`, `nowUnixMilli`, `nowUnixNano`, `sleep`, `sleepMs`, `sleepSec`,
calendar accessors, formatting, and `addSeconds` return directly.
`parse(layout, value) -> Result[int]` and `measureMs(fn) -> Result[int]` retain
explicit failures; `betweenUnixNano` and `elapsedSinceNano` return `Time`.

### `lamdatetime` — `DateTime`

Timezone-aware calendar instants that compose with `Time` durations.

```lammergeier
from lamdatetime import DateTime
from lamtime import Time
from lamerrors import Result

utc: DateTime = DateTime.parseRfc3339("2026-09-25T12:00:00Z")?
rome: DateTime = utc.inLocation("Europe/Rome")?
later: DateTime = rome + Time.Hours(2)
elapsed: Time = later - rome
print(rome.format("2006-01-02 15:04 MST"))
```

Construct with `current(location = "UTC")`, `fromUnix`, `fromUnixMilli`,
`fromUnixNano`, `parse(layout, value, location = "UTC")`, `parseRfc3339`, or
`parseCommon`, or `parseAny(value, layouts, location = "UTC")`; invalid
layouts, values, and IANA timezone names return `Result[DateTime]`. `convert`
reformats text in a named timezone.

`DateTime.parseAny` tries Go layouts in order and keeps the first that
matches: day-first before month-first, database texts before client texts.
A layout with an offset (`Z07:00`, `-07`) keeps the text's offset as the
location; the others read the text in `location`. No layout matching (or an
empty list) is a `ParseError` naming the last layout's failure, an unknown
zone a `LocationError`.

```lammergeier
const Layouts: list[str] = ["2006-01-02 15:04:05.999999999-07", "2006-01-02T15:04:05Z07:00", "02/01/2006"]
when: DateTime = DateTime.parseAny(text.trim(), Layouts, "Europe/Rome")?
```

`parseCommon` reads RFC 3339, RFC 1123 / 822, ISO `2006-01-02 15:04:05`,
`2006-01-02 15:04`, `2006-01-02`, US `01/02/2006` forms, and the timestamp
texts of PostgreSQL and SQLite: `timestamptz` (`2026-11-03
19:05:00.123456+01`, `+05:30` offsets) and `timestamp` / `datetime`
(`2026-11-03 19:05:00.5`, `2026-11-03T19:05:00`), fractional seconds optional.

Instances expose Unix units, `locationName`, custom/RFC3339/date formatting,
`inLocation`, `add` / `subtract`, `since` / `until`, calendar-aware `addDays` /
`addMonths` / `addYears`, comparisons, UTC offset seconds, and `+`, `-`, `==`,
`str` operators. The older static helpers (`now`, `today`, `timestamp`,
`fromTimestamp`, `diffSeconds`, `year`, `month`, `day`, `weekday`,
`isLeapYear`) remain available.

The zone database is compiled into every program that imports `lamdatetime`
(directly or through `lamdb`, or `lamcron` for `CRON_TZ=...` schedules): Go's `time/tzdata`, about 450 KB.
`"Europe/Rome"` and the other IANA names resolve in minimal containers
(`scratch`, distroless, Alpine without `tzdata`) with no `import _
"time/tzdata"` in a `go!` block; a system zone database or `ZONEINFO`, when
present, still takes precedence.

### `lamcron` — `Cron`

Cron-style scheduler wrapping
[`github.com/robfig/cron/v3`](https://pkg.go.dev/github.com/robfig/cron/v3).
Use `Cron.new()` for standard 5-field expressions, `Cron.newWithSeconds()`
when you need sub-minute resolution.

```lammergeier
from lamcron import Cron
from lamerrors import Result

c: Cron = Cron.new()
id: int = c.schedule("*/5 * * * *", lambda: print("tick"))?
c.start()            # non-blocking; scheduler runs in its own goroutine
# …work…
c.stop()             # synchronous — waits for in-flight jobs
```

Accepted expressions: standard 5-field (`min hour dom mon dow`),
6-field (`sec min hour dom mon dow`) via `newWithSeconds()`, and
`@daily` / `@hourly` / `@every 30s` descriptors. Malformed expressions and unsupported callback signatures are
returned as `CronError`.

| Method | Returns | Notes |
|--------|---------|-------|
| `Cron.new()` / `Cron.newWithSeconds()` | `Cron` | Parser differs; runtime is identical. |
| `c.schedule(expr, fn)` | `Result[int]` | Entry ID; pass to `remove`. ``fn`` is any zero-arg Lam callable. |
| `c.start()` / `c.stop()` | — | Idempotent. `stop` blocks until running jobs finish. |
| `c.remove(id)` | — | Silent no-op for unknown IDs. |
| `c.count()` / `c.entryIds()` | `int` / `list[int]` | Snapshot view. |

Jobs that panic are recovered automatically and logged to stderr,
so one misbehaving callback can't take the scheduler down. Lifecycle state is
protected by `lamconcurrency.Mutex`; raw Go is limited to the robfig scheduler
handle and callback adapter.

---

## OS, filesystem, env, CLI

### `lamos` — `Os`

File and process I/O uses unsuffixed typed Results for every operation that can
fail.

```lammergeier
from lamos import File, Os
from lamerrors import Result

content: str = Os.readFile("/etc/hostname")?
_ = Os.writeFile("/tmp/log", "hello\n")?
files: list[str] = Os.walk("/tmp")?
f: File = File.open("/tmp/log")?
line: str = f.readline()?
_ = f.close()?
```

`File.open -> Result[File]`; instance `read`, `write`, `readline`, `readlines`,
and `close` are Result-returning. `lines() -> FileLineGenerator` and
`lineBatches(batchSize = 100) -> FileLineGenerator` stream a file; call
`nextLine()` or `nextBatch()` for `Result` payloads and `done()` to detect EOF.
The generator closes the file at EOF and reports invalid batch sizes and read or
close failures rather than hiding them. `Os.getenv`, `setenv`, `getcwd`, filesystem
mutation and inspection, directory listing/walking, `tempFile`, stdin reads,
`user`, and `hostname` likewise return the payload-specific `Result[T]` shown
by their signatures. Only `tempDir`, `args`, and the terminating `exit` remain
direct among these helpers. `Os.fileSize(path) -> Result[int]` and
`Os.modTime(path) -> Result[int]` (the last modification time in Unix
nanoseconds, comparable with `Time.nowUnixNano()`) read a file's metadata
without reading the file: a watcher compares them before reading anything.

`Os.waitForSignal(names = ["SIGINT", "SIGTERM"]) -> str` blocks until one of
the signals arrives and returns its name (`"SIGTERM"`). Names are `SIGINT`,
`SIGTERM`, `SIGHUP` and `SIGQUIT` (the termination signals every Go platform
has; the `SIG` prefix and case are optional); an unknown name or an empty list
throws `ValueError`. The signals are caught only while waiting. For a
non-blocking shape — workers that poll or wait with a timeout — use
`Context.onSignals(names)` from `lamcontext`.

```lammergeier
from lamos import Os

server.startInBackground()
received: str = Os.waitForSignal()
print(f"{received}: draining")
server.shutdown()
```

### `lampath` — `Path`

Filepath helpers.

```lammergeier
from lampath import Path
from lamerrors import Result

p: str = Path.join("a", "b", "c.txt")            # a/b/c.txt
legacy: str = Path.join(["a", "b", "c.txt"])      # list form remains accepted
ext: str = Path.ext("file.tar.gz")                 # ".gz"
parts: list[str] = Path.split("/a/b/c")
absolute: str = Path.abs(p)?
relative: str = Path.rel("/a", "/a/b/c")?
```

Direct helpers are variadic `join(*parts)` (also accepting one `list[str]`),
`base`, `dir`, `ext`, `clean`, `isAbs`,
`splitExt`, `withExt`, and `split`. `abs`, `rel`, `glob`, and `exists` return
`Result[str]`, `Result[str]`, `Result[list[str]]`, and `Result[bool]`
respectively.

### `lamenv` — `Env`, `Dotenv`, `Config`

Environment variables, dotenv parsing, and unified config-file
loading in one module.

```lammergeier
from lamenv import Env, Dotenv, Config
from lamerrors import Result

# Plain and typed env access
portText: str = Env.getOr("PORT", "8080")
port: int = Env.get("PORT", 8080)?
debug: bool = Env.get("DEBUG", false)?
ratio: float = Env.get("SAMPLE_RATE", 1.0)?
verbose: bool = Env.flag("VERBOSE", false)       # yes/no, on/off, 1/0, true/false
origins: list[str] = Env.list("CORS_ORIGINS")   # "https://a.example, https://b.example"

# Dotenv — supports export, quoted values, escapes, and ${VAR}
#          interpolation (both file-local and process env).
entries: dict[str, any] = Dotenv.parseFile(".env")?
applied: int = Dotenv.load(".env")?          # don't override live env
applied_: int = Dotenv.loadOverride(".env")? # file wins

# Config picks a decoder from the extension (.env, .json, .yaml,
# .yml, .toml) and returns a dict.
cfg: dict[str, any] = Config.load("config.toml")?
merged: dict[str, any] = Config.merge([
    Config.load("config/defaults.yaml")?,
    Config.load("config/prod.yaml")?,
    Dotenv.parseFile(".env")?,
])
```

- **Env**: `get(key) -> Result[str]`; overloaded `get(key, fallback: int | float |
  bool)` returns the matching typed `Result` and uses the fallback only when the
  key is absent (malformed present values remain parse errors). `set` and
  `unset` return `Result[any]`. `getOr(key, fallback: str)`, `has`, and `all`
  are the explicit direct-value operations.
  `flag(key, fallback: bool) -> bool` is the lenient switch: the trimmed value
  is `true` for `1`, `true`, `t`, `yes`, `y`, `on` and `false` for `0`,
  `false`, `f`, `no`, `n`, `off` (any case); unset, empty or blank and any
  other value give `fallback` (use `get(key, false)` to reject a malformed
  value). `list(key, separator = ",") -> list[str]` splits the value, trims
  each part and drops empty ones (`" a, ,b "` -> `["a", "b"]`); unset or
  empty is `[]`, and an empty `separator` splits on runs of white space.
- **Dotenv**: `parse` is in-memory and direct; `parseFile`, `load`,
  `loadOverride`, and `applyToEnv` return typed Results. The parser handles
  `export KEY=val`, quoted and triple-quoted values, escapes, inline comments,
  and `${VAR}` / `$VAR` interpolation.
- **Config**: `load -> Result[dict[str, any]]`; `merge` combines successfully
  loaded sources. TOML parsing is backed by `github.com/BurntSushi/toml` and
  YAML by `gopkg.in/yaml.v3`.

### `lamcli` — `Cli`

Command-line flag parsing.

```lammergeier
from lamcli import Cli

verbose: bool = Cli.hasFlag("verbose")            # --verbose
out: str = Cli.getFlag("out", "default.txt")     # --out=...
n: int = Cli.getFlag("n", 10)
files: list[str] = Cli.positional()              # everything else
```

Seven names: `args`, `program`, `argCount`, `arg(idx)`, `hasFlag`, overloaded
`getFlag(name, fallback: str = "") -> str` / `getFlag(name, fallback: int = 0)
-> int`, and `positional`. Integer flag parsing returns the fallback when the
flag is absent or malformed.

### `lamexec` — `Exec`

Run external processes.

```lammergeier
from lamexec import Exec, ExecCommand, ExecResult
from lamerrors import Result

cmd: ExecCommand = Exec.command("git", ["status", "--short"])?
cmd.cwd = "/repo"
cmd.env = {"CI": "true"}
cmd.timeoutMs = 5000
result: ExecResult = cmd.execute()?
print(result.stdout)
print(result.stderr)
print(result.exitCode)
```

`ExecCommand(name, args = [], env = {}, cwd = "", stdin = "", timeoutMs = 0)`
captures a typed process description. `execute() -> Result[ExecResult]` supports
stdin, environment additions, working directories, timeout, and concurrent
`cancel() -> Result[any]`; a completed non-zero exit remains `Ok` and is tested
with `result.success()`. Startup, timeout, and cancellation failures are
separate errors.

`Exec.command(name, args = []) -> Result[ExecCommand]`, `runCommand(command)`
and its `execute(command)` alias expose the typed path. Convenience `run`,
`runSilent`, and `output` retain their typed Results; unlike `ExecCommand`,
those legacy helpers treat non-zero exits as errors.

---

## Network & web

### `lamhttp` — typed options, fluent requests, responses, and a tiny server

HTTP **client** plus a tiny blocking server for one-off scripts.
Use [`@lam/vulture`](#lamvulture--server-request-response-sseemitter-httperror)
for production HTTP servers.

```lammergeier
from lamerrors import Result
from lamhttp import Http, HttpRequestOptions, HttpResponse

options: HttpRequestOptions = HttpRequestOptions()
options.timeout(5000).header("Authorization", "Bearer token").query("page", "2")
result: Result[HttpResponse] = Http.request("GET", "https://api.example.com/users", options)
response: HttpResponse = result.unwrap()

postedResult: Result[HttpResponse] = Http.build("POST", "https://api.example.com/events").header("X-Trace", "abc").json({"ready": true}).send()
posted: HttpResponse = postedResult.unwrap()
```

`HttpRequestOptions` carries per-request headers, query parameters, timeout,
content type, and body. `Http.request` returns `Result[HttpResponse]`;
`Http.build` exposes the same request core through chainable `header`, `query`,
`timeout`, `text`, `json`, `html`, `xml`, and `send` methods (`html` / `xml`
bodies are sent as `text/html; charset=utf-8` / `application/xml`).
`HttpResponse.json()`, `.html()` (lenient) and `.xml()` (`Result[xml]`) read
the body as the native types. `HttpClient` adds reusable base
URL, default-header, and default-timeout behavior.

The convenience methods are also Result-based: `get`, `head`, `delete`,
overloaded `post` / `put` / `patch`, `statusCode`, `getHeader`, and
`getWithHeaders` return their documented `Result[T]` payloads.
`serve(port, handler)` takes a `func(method: str, path: str, body: str) -> str`
handler (a function, or a lambda typed from it:
`Http.serve(0, lambda method, path, body: f"{method} {path}")`) and returns
`Result[HttpServer]`; `HttpServer.port()` gives the bound port and
`HttpServer.close()` returns `Result[bool]`.

`Multipart` builds a `multipart/form-data` body (RFC 7578), as a browser
sends a form with file inputs, for `HttpRequestOptions.multipart(form)` /
`HttpRequest.multipart(form)` or for tests that need the raw body:

```lammergeier
from lamhttp import HttpClient, HttpRequestOptions, Multipart

form: Multipart = Multipart().field("title", "Jackie").field("tag", "a").field("tag", "b")
form.file("photo", "bag.jpg", "image/jpeg", photoBytes)
res = HttpClient("https://shop.example").request("POST", "/upload", HttpRequestOptions().multipart(form))?

body: bytes = form.body()          # the encoded body
header: str = form.contentType()   # multipart/form-data; boundary=lam-...
```

| Method | Returns |
|--------|---------|
| `Multipart()` | an empty form with a random boundary (`lam-` and 48 hex digits) |
| `field(name, value)` / `file(name, filename, contentType, data: bytes)` | the form (chainable) |
| `setBoundary(boundary)` | the form; a fixed boundary for stable test bodies |
| `body()` | `bytes` |
| `contentType()` / `boundaryText()` / `partCount()` | `str` / `str` / `int` |

Parts keep their order; a file with an empty content type is sent as
`application/octet-stream`; binary data is sent as it is. Names and file
names are quoted as browsers quote them (`"` -> `%22`, CR -> `%0D`, LF ->
`%0A`), so they cannot add headers. A content type with a line break, an
invalid boundary (1–70 letters, digits, `'()+_,-./:=?` or spaces, not ending
with a space) or part data that contains the boundary line throws a
`ValueError`.

### `@lam/vulture` — `Server`, `Request`, `Response`, `SseEmitter`, `HttpError`

Vulture is an independently versioned official package, not a compiler-bundled
standard-library module. Install it with `lamc install @lam/vulture@2.0.2`; its
[repository README](https://github.com/thallium-solutions/lam-vulture) is the
authoritative API and security reference.

Fastify-style HTTP server with the full lifecycle ladder, schema-
based validation, decorators, plugin encapsulation, content-type
parsers, a trie router, app-lifecycle hooks, OpenAPI generation,
streaming, SSE, signed cookies, an in-process test harness, and TLS.

```lammergeier
from @lam/vulture import Server, Request, Response, HttpError

func helloHandler(req: Request, res: Response) {
    name: str = req.queryGet("name", "world")
    res.text(f"hello, {name}")
}

func main() {
    srv: Server = Server()
    srv.useCors()
    srv.get("/hello", helloHandler)
    _ = srv.listen(8080).unwrap()
}
```

Routes can be as small as a handler function, or they can carry typed
`RouteOptions` for validation, route-local hooks, metadata, timeouts, streaming,
and response serialization. `Server.inject` runs the same dispatcher in-process
and returns an `InjectResponse`, which makes server examples and tests
deterministic:

```lammergeier
from @lam/vulture import InjectResponse, Request, Response, RouteOptions, Server
from lamerrors import Result

func auth(req: Request, res: Response) {
    if req.header("Authorization") != "Bearer dev" {
        res.code(401).json({"error": "unauthorized"})
    }
}

func createUser(req: Request, res: Response) {
    bodyResult: Result[json] = req.jsonBody()
    if not bodyResult.ok() {
        res.code(400).text(str(bodyResult.error))
        return
    }
    body: json = bodyResult.unwrap()
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

    options: RouteOptions = RouteOptions()
    _ = options.preHandler(auth)
    _ = options.withSchema({
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
    })
    options.summary = "Create a user"
    options.tags = ["users"]
    srv.post("/users", createUser, options)

    headers: dict[str, str] = {
        "Authorization": "Bearer dev",
        "Content-Type": "application/json",
    }
    out: InjectResponse = srv.inject("POST", "/users", "{\"name\":\"Ada\"}", headers)
    print(out.status)   # 201
    print(out.body)     # response schema strips "internal"
}
```

### Vulture mental model

`@lam/vulture` follows the same shape as Fastify:

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
    srv.post("/orders", createOrderHandler, createOrderOptions())

    # Startup visibility.
    print(srv.printRoutes())
    _ = srv.listen(8080).unwrap()
}
```

### Routing guide

Use the verb helpers for normal routes and the `route` overload when the method
is dynamic. Each helper has overloads for a plain handler, typed `RouteOptions`,
and a body schema plus explicit schema format:

```lammergeier
srv.get("/users", listUsers)
srv.post("/users", createUser)
srv.put("/users/:id", replaceUser)
srv.patch("/users/:id", patchUser)
srv.del("/users/:id", deleteUser)
srv.all("/debug/*", debugCatchAll)
srv.route("OPTIONS", "/custom", customOptions)
```

`Request.params` preserves the route marker in lookup keys: a `:id` segment is
read as `req.params[":id"]`, while a trailing wildcard is `req.params["*"]`.
Parameter JSON Schemas use the unmarked property name (`"id"`), because Vulture
strips the leading colon only for schema validation and OpenAPI output:

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
from lamerrors import Result

func inspect(req: Request, res: Response) {
    ct: str = req.header("Content-Type")
    page: str = req.queryGet("page", "1")
    sid: Result[str] = req.cookie("sid")
    signed: Result[str] = req.signedCookie("sid", "secret")
    ip: str = req.realIP()
    scheme: str = req.realScheme("http")

    res.header("X-Request-Id", req.id())
    res.json({
        "contentType": ct,
        "page": page,
        "hasSession": sid.ok(),
        "signedSession": signed.unwrapOr(""),
        "ip": ip,
        "scheme": scheme,
    })
}
```

Missing cookies, malformed `Cookie` headers, and invalid signatures are typed
errors; an empty cookie value remains a successful `Ok("")`.

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

Use a typed `RouteOptions` value when behavior belongs to one endpoint rather
than the whole server, then pass it to the ordinary `route`, `get`, `post`,
`put`, `del`, `patch`, `head`, `options`, or `all` overload.

```lammergeier
from @lam/vulture import RequestBodyReader, RouteOptions
from lamerrors import Result

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
    copied: Result[int] = reader.copyTo("/tmp/upload.bin")
    if not copied.ok() {
        res.code(500).text(str(copied.error))
        return
    }
    res.json({"bytes": copied.unwrap()})
}

uploadOptions: RouteOptions = RouteOptions(20_000_000, 10000, true)
_ = uploadOptions.preHandler(requireAuth)
_ = uploadOptions.onResponse(afterAudit)
uploadOptions.summary = "Upload a binary object"
uploadOptions.tags = ["uploads"]
srv.route("POST", "/upload", upload, uploadOptions)
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

`@lam/vulture` uses the same private compilation cache and validation core as
`lamschema`. Route helpers have a request-body schema overload with an explicit
format; invalid documents short-circuit with HTTP 400 before `preHandler` and
the handler. Select JSON, YAML, or XML/XSD as appropriate:

```lammergeier
srv.post("/users", createUser, userSchema, "json")
srv.post("/events", ingestYaml, yamlSchema, "yaml")
srv.post("/legacy", ingestXml, xsdSchema, "xml")
```

`RouteOptions.withSchema(...)` carries query, params, headers, and status-keyed
response schemas as well as body schemas. Its typed fields carry OpenAPI
metadata; request media types follow the selected schema format.

```lammergeier
func createOrderOptions() -> RouteOptions {
    options: RouteOptions = RouteOptions()
    options.operationId = "createOrder"
    options.summary = "Create an order"
    options.description = "Validates the body and returns the public order view."
    options.tags = ["orders"]
    _ = options.withSchema({
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
    })
    return options
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

listOptions: RouteOptions = RouteOptions()
_ = listOptions.withSchema({"querystring": {"$ref": "PaginationQuery"}})
listOptions.summary = "List orders"
listOptions.tags = ["orders"]
srv.get("/orders", listOrders, listOptions)
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

`addContentTypeParser(mediaType, fn)` lets a server accept custom request
bodies. The typed parser is `func(Request) -> any`, so it can inspect headers as
well as `req.body`. Handlers read the cached value through the Result-returning
`req.parsedBody()`.

```lammergeier
from lamerrors import Result
from lamjson import Json
from lamstrings import Strings

func parseLineBody(req: Request) -> any {
    return {"lines": Strings.splitLines(req.body)}
}

func ingest(req: Request, res: Response) {
    parsed: Result[any] = req.parsedBody()
    if not parsed.ok() {
        res.code(400).text(str(parsed.error))
        return
    }
    value: Result[json] = Json.fromValue(parsed.unwrap())
    if not value.ok() {
        res.code(400).text(str(value.error))
        return
    }
    res.json(value.unwrap())
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
    # Decorator values are `any`: convert them before building `json`.
    service: str = str(srv.dec("serviceName"))
    user: any = req.dec("user")
    view: str = str(res.dec("view"))
    res.json({"service": service, "authenticated": user != None, "view": view})
}
```

Plugins usually prefer `req.ctx` for per-request values and decorators for
server-wide values or default request/reply slots. `req.ctx` keeps dictionary
indexing but is backed by `lamcontext.Context`, so `has`, `in`, cancellation,
deadlines, reasons, and waiting are available on the same object. `res.ctx` and
`srv.ctx` remain ordinary plugin scratch dictionaries because they do not model
request cancellation. Use a unique prefix such as `__myPluginName` for internal
keys.

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

`inject` returns a typed `InjectResponse` with `status`, `body`, `headers`, and
a case-insensitive `header(name)` helper. Request headers are passed as
`dict[str, str]`.

```lammergeier
func testCreateOrder() {
    srv: Server = Server()
    srv.post("/orders", createOrder, createOrderOptions())

    h: dict[str, str] = {"Content-Type": "application/json"}
    out: InjectResponse = srv.inject("POST", "/orders", "{\"sku\":\"A\",\"quantity\":2}", h)
    assert(out.status == 201)
    assert(out.body != "")
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
`.params`, `.remoteAddr`, and cancellation-aware `.ctx`. Named parameter keys
retain their leading colon (`req.params[":id"]`); wildcard capture uses `"*"`.
Methods: `header(name)`, `queryGet(name, fallback)`, `cookie(name) ->
Result[str]`, `signedCookie(name, secret) -> Result[str]`, `jsonBody() ->
Result[json]`, and `parsedBody() -> Result[any]` (native `json` for
`application/json`, dynamic form values, or text otherwise). Missing/malformed
cookies and invalid JSON or signatures are errors rather than sentinels. Other
helpers are `formField(name, fallback)`, `formFile(name)`, `formFiles(name)`,
`realIP()`, `realScheme(fallback="http")`, `dec(name)`, `hasDec(name)`, `id()`,
and `setId(value)`.

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
`comment(text)`, `close()`, `wait(timeoutMs) -> bool` (true once the
client disconnected or the server began `shutdown()`/`close()`, so
long-lived streams loop on `while not emitter.wait(15000) { ... }` and do
not hold a graceful shutdown for its drain timeout).

**`HttpError`** — typed HTTP error. `panic(HttpError(409, "dup",
{"key": x}))` from a hook or handler short-circuits with a structured
JSON response. Static shortcuts: `HttpError.badRequest`,
`unauthorized`, `forbidden`, `notFound`, `conflict`, `unprocessable`,
`tooManyRequests`, `internal`, `badGateway`, `serviceUnavailable`.

**`Server`** — Routes: overloaded `route`, `get`, `post`, `put`, `del`,
`patch`, `head`, `options`, and `all`. Pass a typed `RouteOptions` value to the
same method; v2 does not use separate suffixed route helpers.
`notFound(handler)` / `setNotFoundHandler(handler)`.
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
`listenBackground`, `listenTLS`, and `listenTLSBackground` return
`Result[bool]`; bind, certificate, and synchronous serving failures remain
explicit. `close` is immediate and `shutdown(timeoutMs=5000)` drains gracefully.
Decorators: `decorate(name, value)` /
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
Response)`); per-route override via `RouteOptions(0, timeoutMs)`.
When a handler exceeds the deadline
the dispatcher snapshots a fresh response, runs every `onTimeout`
hook against it, and writes a Fastify-style 408 envelope
(`{"statusCode": 408, "error": "Request Timeout", "message":
"...", "timeoutMs": <n>}`) — the still-running handler keeps its
own (orphaned) response object so it can't race the writer.
Introspection / test: `listRoutes()`, `printRoutes()` (sorted
pretty-printed table with `[stream]` / `[deprecated]` / `[hidden]`
/ `[timeout=...]` flags — good for startup banners),
`openapi(info=None)`, and typed
`inject(method, path, body="", headers={}) -> InjectResponse`. Response-side
serialisation: `res.serialize(payload=None)` applies the matched
route's response schema (exact status → family → `default`) and
returns the on-the-wire string without mutating the response —
mirrors Fastify's `reply.serialize()`; strings pass through
verbatim.
Config: `.bodyLimit`, `.debug`, `.ctx`, `.boundAddr` (populated
after listen succeeds), `.requestTimeoutMs`.

**Request lifecycle hooks** (Fastify-compatible ladder; each phase
has both a global hook list and a route-level slot):

| Phase | Server method | `RouteOptions` method | Notes |
|---|---|---|---|
| `onRequest` | `onRequest(fn)` | — | First touch; body still raw. |
| `preParsing` | `preParsing(fn)` | `preParsing(fn)` | Before content-type parser. |
| `preValidation` | `preValidation(fn)` | `preValidation(fn)` | After parsing, before schema validation. |
| `preHandler` | `preHandler(fn)` | `preHandler(fn)` | After validation, before the handler. |
| `preSerialization` | `preSerialization(fn)` | `preSerialization(fn)` | After handler, before response serializer. |
| `onSend` | `onSend(fn)` | `onSend(fn)` | Last chance to mutate body / headers. |
| `onResponse` | `onResponse(fn)` | `onResponse(fn)` | Observational; status/body locked in. |
| `onError` | `onError(fn)` | `onError(fn)` | Typed signature `func(Request, Response, any)`; fires on panic. |
| `onRequestAbort` | `onRequestAbort(fn)` | — | Fires when the HTTP client disconnects before the handler finishes. Signature: `func(req: Request)`. Runs on a side goroutine; use to cancel background work keyed off the request. Inject path never triggers it. |
| `onTimeout` | `onTimeout(fn)` | — | Fires when the per-request handler timeout (`setRequestTimeout` or per-route `timeoutMs`) elapses before the handler returns. Signature: `func(req: Request, res: Response)`. The hook can mutate `res` (status, headers, body) before the dispatcher writes the 408 envelope. Inject path never triggers it. |

**App-lifecycle hooks** — fire once each at the corresponding
transition. Signature: `func(srv: Server)`.

| Hook | Fired |
|---|---|
| `onReady` | Synchronously before the listener binds. |
| `onListen` | After bind succeeds (`srv.boundAddr` is populated). |
| `onClose` | During `close()` / `shutdown()`, in reverse registration order. |

**Per-route options** — construct `RouteOptions(bodyLimit=0, timeoutMs=0,
streamBody=false)` and pass it to an ordinary route overload:

| Member | Type | Effect |
|---|---|---|
| `bodyLimit` | `int` | Reject this route's body over N bytes with 413. |
| `timeoutMs` | `int` | Handler timeout for this route; overrides `srv.setRequestTimeout(...)`. `0` means inherit. |
| `streamBody` | `bool` | Skip the buffered body read. `req.body` stays `""`; pull chunks via `req.bodyReader()`. |
| `preParsing(fn)` / `preValidation(fn)` / `preHandler(fn)` / `preSerialization(fn)` / `onSend(fn)` / `onResponse(fn)` / `onError(fn)` | typed methods | Install one route-scoped lifecycle hook for each phase. |
| `withSchema(schema, format="json")` | method | Attach `body` / `querystring` / `params` / `headers` / status-keyed `response` schemas. |
| `summary` / `description` / `operationId` | `str` fields | OpenAPI metadata. |
| `tags` | `list[str]` field | Categorisation. |
| `deprecated` / `hidden` | `bool` fields | Mark deprecated; hide from `listRoutes()` and OpenAPI. |
| `consumes` / `produces` | `list[str]` fields | OpenAPI media types. |
| `metadata(name, value)` | typed method | Store an opaque application value exposed as route `meta`. |

**Streaming request bodies** — construct `RouteOptions(...,
streamBody=true)` to tell the dispatcher not to `io.ReadAll` the body.
`req.body` then stays `""` and you read chunks off the wire yourself via
`req.bodyReader()` (returns a `RequestBodyReader`). The reader is also available
on buffered routes — it wraps `req.body` in a `strings.Reader` so one handler can
work with both modes.

The operational methods are Result-based: `.read(maxBytes=8192) -> Result[str]`
returns `Ok("")` on EOF; `.readAll() -> Result[str]` drains; `.copyTo(path) ->
Result[int]` streams to disk; and `.close() -> Result[bool]` releases the reader.
`.isEOF() -> bool` surfaces read state. `bodyLimit` still caps streamed reads,
but limit, transport, file, and close failures remain explicit errors. Content-
type parsers and schema validation do not run on streamed routes because they
need the whole body buffered.

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

### `@lam/vulture/std-plugin`

Install with `lamc install @lam/vulture/std-plugin@2.0.3`; see the
[package README](https://github.com/thallium-solutions/lam-vulture-std-plugin).
This official package is independent of the standard library.

Every ready-made plugin has a direct installer such as
`requestId(srv, header)`. Configurable installers also expose a typed
`*Plugin(...) -> func(Server)` factory for `srv.register(...)`; parameterless
`helmet`, `etag`, and `serverTiming` already match `func(Server)` directly:

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

Factories are `requestLogPlugin`, `rateLimitPlugin`, `compressPlugin`,
`healthcheckPlugin`, `metricsPlugin`, `tracingPlugin`, `idempotencyPlugin`,
`requestIdPlugin`, `basicAuthPlugin`, `bearerAuthPlugin`, `ipFilterPlugin`,
`cacheControlPlugin`, `sessionPlugin`, and `csrfPlugin`. For example:

```lammergeier
srv.register(requestIdPlugin())
srv.register(rateLimitPlugin(max=60, windowMs=60000))
srv.register(helmet)
```

The module also exports a `CircuitBreaker` class for downstream-call
protection:

```lammergeier
from @lam/vulture/std-plugin import CircuitBreaker
from lamerrors import Result

cb: CircuitBreaker = CircuitBreaker(5, 2000)  # threshold=5, cooldown=2000ms
out: Result = cb.call(callRemoteAPI)
print(f"breaker={cb.state()} result={out}")
```

State transitions: `closed` → `open` (after `threshold` consecutive
failures) → `half-open` (after `cooldownMs` elapses) → `closed` (on
the first probe success) or back to `open` (on probe failure).
Inspect via `cb.state()`.

See [`docs/server_plugins.md`](server_plugins.md) for the plugin
authoring guide and recipes.

### `@lam/vulture/ws-plugin` — `WebSocket`

Install the independent package with
`lamc install @lam/vulture/ws-plugin@2.0.2`; see the
[package README](https://github.com/thallium-solutions/lam-vulture-ws-plugin).

The plugin uses `github.com/gorilla/websocket` and supports subprotocols plus
RFC 7692 permessage-deflate compression. v2 uses overloaded `wsRoute` calls
for both default and configured routes.

```lammergeier
from @lam/vulture import Request, RouteOptions
from @lam/vulture/ws-plugin import WebSocket, wsRoute
from lamerrors import Result

func chatHandler(ws: WebSocket, req: Request) {
    while not ws.isClosed() {
        incoming: Result[str] = ws.receive()
        if not incoming.ok() {
            return
        }
        message: str = incoming.unwrap()  # Ok("") is a valid empty frame
        if not ws.send(f"echo: {message}").ok() {
            return
        }
    }
}

# Plain route: permissive Origin and no compression.
_registered: bool = wsRoute(srv, "/chat", chatHandler).unwrap()

# Production route: metadata configures the WebSocket upgrade.
options: RouteOptions = RouteOptions(0, 30000)
_ = options.metadata("subprotocols", ["jsonrpc-v2"])
_ = options.metadata("compression", true)
_ = options.metadata("originAllowList", ["https://app.example.com"])
_registeredRpc: bool = wsRoute(srv, "/rpc", rpcHandler, options).unwrap()
```

`wsRoute(server, path, handler)` and its four-argument `RouteOptions` overload
return `Result[bool]`. `WebSocket.send(text)`, `sendBytes(data)`, and `ping()`
return `Result[bool]`; `receive() -> Result[str]` and `receiveBytes() ->
Result[any]` distinguish valid empty frames from close and transport errors.
`close()` is idempotent; `isClosed()`, negotiated `.subprotocol`, and
`rawConn()` cover state and advanced native access.

### `@lam/vulture/tus-plugin` — tus.io resumable uploads

Install the independent package with
`lamc install @lam/vulture/tus-plugin@3.0.2`; see the
[package README](https://github.com/thallium-solutions/lam-vulture-tus-plugin).

It implements [tus 1.0.0](https://tus.io) Creation, Creation With Upload,
`HEAD`, `PATCH`, Termination, Concatenation, and Checksum on top of Vulture.

```lammergeier
from @lam/vulture/tus-plugin import tusUploads, tusGc

_mounted: bool = tusUploads(
    srv,
    "/uploads",
    "/var/lib/uploads",
    200_000_000,
).unwrap()

# Optional: garbage-collect uploads older than 24 hours from a periodic task.
removed: int = tusGc("/var/lib/uploads", 24).unwrap()
```

`tusUploads(...) -> Result[bool]` reports invalid mount/store configuration and
directory creation failures. `tusGc(...) -> Result[int]` reports directory,
metadata, and removal failures instead of returning a partial count. Storage is
a flat directory: each upload gets `<id>` (the bytes) plus `<id>.json`
(metadata). Hook completion via
`srv.ctx["__tusComplete"] = lambda info: …`.

### `lamnet` — TCP, TLS, UDP, and DNS

```lammergeier
from lamnet import Net, TcpConn, TcpListener, UdpConn, UdpPacket
from lamerrors import Result

listener: TcpListener = Net.listenTcp(9000)?
conn: TcpConn = listener.accept(5000)?
_ = conn.send("welcome\n", 1000)?
line: str = conn.readLine(1000)?

udp: UdpConn = Net.listenUdp(9001)?
packet: UdpPacket = udp.receiveFrom(65535, 1000)?
_ = udp.sendTo("ack", packet.address, 1000)?
```

All operations that can fail return typed Results. `Net` provides DNS
`lookupHost` / `lookupAddr`, `hostname`, reachability checks, TCP dial/listen,
TLS dial/listen (`dialTls` supports `insecureSkipVerify`; `listenTls` loads a
certificate/key pair), and UDP dial/listen, all with millisecond timeouts where
applicable.

`TcpConn` supports timeout-aware `send`, `receive`, `readLine`, `setDeadline`,
`close`, and local/remote addresses. `TcpListener.accept(timeoutMs = 0)`,
`close`, and `port` are Result-based. `UdpConn` supports connected `send` /
`receive`, packet `receiveFrom` / `sendTo`, deadlines, addresses, `port`, and `close`;
`UdpPacket` exposes `data` and `address`.

### `lamcompress` — `Compress`

Stream-free gzip/zlib/deflate. Output is base64-encoded so it round-
trips through string-typed channels.

```lammergeier
from lamcompress import Compress
from lamerrors import Result

enc: str = Compress.gzip("hello hello hello")
dec: str = Compress.gunzip(enc)?
```

`gzip` and `zlibDeflate` return encoded strings directly. `gunzip` and
`zlibInflate` return `Result[str]` for malformed base64 or compressed data.

### `lamsmtp` — `Smtp`, `Mail`

Outbound SMTP built on Go's `net/smtp` + MIME multipart assembly.
Sends plain-text, HTML, and attachment-bearing mail to any SMTP
server (MailHog / Mailpit for dev, SES / SendGrid / a bare-metal
relay for prod).

```lammergeier
from lamsmtp import Smtp, Mail
from lamerrors import Result

# One-shot plaintext.
sent: bool = Smtp.sendMail(
    host="localhost:1025",
    sender="alice@example.com",
    to=["bob@example.com"],
    subject="Hello",
    text="Hi Bob!",
)?

# HTML + plaintext alternative + attachment via the builder.
m: Mail = Mail()
m.setSender("alice@example.com")
m.addTo("bob@example.com")
m.addCc("carol@example.com")
m.setSubject("Invoice")
m.setText("PDF attached.")
m.setHtml(<><h1>Invoice</h1><p>PDF attached.</p></>)
m.attach("invoice.pdf", "application/pdf", pdfBytes)
sentRich: bool = Smtp.send(host="smtp.example.com:587",
                           username="alice", password="app-password",
                           mail=m)?
```

`Smtp.sendMail(...) -> Result[bool]` is the plaintext short form;
`Smtp.send(...) -> Result[bool]` accepts a pre-built `Mail` with multipart,
attachment, and custom-header support. Message-construction and transport
failures are preserved as Results. `username` / `password` opt into PLAIN auth
(stdlib's client refuses plain-auth over an untrusted cleartext channel unless
the server is `localhost`, matching Go's built-in policy).

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

`Schema.validate` validates JSON Schema draft-07, JSON Schema represented in
YAML, or XML against XSD. Schemas are compiled lazily and cached privately by
content; applications keep named schemas in ordinary `dict[str, json]` or
`dict[str, str]` values.

```lammergeier
from lamerrors import Result, Error
from lamschema import Schema

schemas: dict[str, json] = {
    "user.create": {
        "type": "object",
        "required": ["name", "age"],
        "properties": {
            "name": {"type": "string", "minLength": 1},
            "age": {"type": "integer", "minimum": 0, "maximum": 150},
        },
    },
}

result: Result[json] = Schema.validate(
    schemas["user.create"],
    {"name": "Ada", "age": 30},
)
if not result.ok() {
    validationError: Error = result.error
    print(validationError.kind)
}
```

The overload returns the original document on success. Failures use
`SchemaFormatError`, `SchemaParseError`, `SchemaCompileError`, or
`SchemaValidationError`; machine-readable issue details are stored in
`Error.cause`.

| Signature | Returns |
|---|---|
| `Schema.validate(schema: str, document: str, format: str = "json")` | `Result[str]` |
| `Schema.validate(schema: str, document: json)` | `Result[json]` |
| `Schema.validate(schema: json, document: str)` | `Result[str]` |
| `Schema.validate(schema: json, document: json)` | `Result[json]` |

The text format names are exactly `json`, `yaml`, and `xml`. YAML schemas are
JSON Schema written in YAML. XML uses the pinned pure-Go
`github.com/moolekkari/validatexml-go` XSD 1.0 validator and disables external
XSD imports/includes/redefines. Use `Json.isValid(text)` when only JSON
syntax—not schema conformance—needs checking.

> JSON Web Tokens and Protocol Buffers now release independently as
> [`@lam/jwt`](libraries.md) and
> [`@lam/protobuf`](libraries.md). They are not compiler-bundled
> stdlib modules; install them before using their scoped imports.

---

## Crypto, encoding, IDs

### `lamcrypto` — `Crypto`, `CryptoKeyPair`, `CryptoCertificate`

A practical cryptography layer built on Go's standard library (bcrypt comes
from `golang.org/x/crypto`, pinned with the stdlib). Binary
values use `list[int]`, encrypted payloads and signatures are portable strings,
and asymmetric keys stay in PEM format so applications do not need Go key
objects.

```lammergeier
from lamcrypto import Crypto
from lamerrors import Result

key: list[int] = Crypto.randomBytes(32)?
sealed: str = Crypto.encrypt("private message", key, "tenant:42")?
plain: str = Crypto.decrypt(sealed, key, "tenant:42")?

stored: str = Crypto.passwordHash("correct horse battery staple")?
print(Crypto.verifyPassword("correct horse battery staple", stored)?)

pair: CryptoKeyPair = Crypto.generateEd25519KeyPair()?
signature: str = Crypto.sign("release manifest", pair.privateKey)?
print(Crypto.verify("release manifest", signature, pair.publicKey)?)
```

AES-GCM envelopes carry a fresh nonce and a `lamcrypto:v1:` version prefix.
Wrong keys, altered ciphertext, and mismatched authenticated data fail closed.
Password hashes use self-describing PBKDF2-HMAC-SHA256 records with a random
salt. All fallible cryptographic operations use their unsuffixed names and
return typed Results.

| Capability | Methods |
|------------|---------|
| Secure random data | `randomBytes -> Result[list[int]]`; `randomToken` / `randomHex -> Result[str]` |
| Authenticated encryption | `encrypt` / `decrypt -> Result[str]`; `isAesKey -> bool` |
| Password storage | `passwordHash -> Result[str]`; `verifyPassword -> Result[bool]` |
| bcrypt passwords | `bcryptHash(password, cost = 10) -> Result[str]`; `bcryptVerify(password, encoded) -> Result[bool]`; `bcryptCost(encoded) -> Result[int]` |
| Keys and signatures | Ed25519/RSA generation returns `Result[CryptoKeyPair]`; `sign -> Result[str]`; `verify -> Result[bool]` |
| Certificates | `parseCertificate -> Result[CryptoCertificate]` with typed X.509 metadata |
| Secret comparison | `constantTimeEquals` returns `bool` (delegates to `lamhash.Hash`) |

bcrypt reads and writes the hashes other systems store (`$2a$`, `$2b$`, `$2y$`
— PHP, Node's `bcrypt`, Python's `bcrypt`, Spring). `bcryptVerify` takes the
password first, like `verifyPassword`: a wrong password is `Result.Ok(false)`
and a value that is not a bcrypt hash is a `CryptoPasswordHashError`.
`bcryptHash` rejects a cost outside 4..31 and passwords over bcrypt's 72-byte
limit (`CryptoValueError`) instead of truncating them. `bcryptCost` tells
whether a stored hash is cheaper than today's cost, to rehash after a
successful login:

```lammergeier
from lamcrypto import Crypto
from lamerrors import Result, Error

func login(password: str, stored: str) -> Result[optional[str]] {
    if not Crypto.bcryptVerify(password, stored)? {
        return Result.Err(Error("AuthError", "wrong password"))
    }
    if Crypto.bcryptCost(stored)? < 12 {
        return Result.Ok(Crypto.bcryptHash(password, 12)?)   # store the new hash
    }
    return Result.Ok(None)
}
```

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

### `lambase64` — `Base16`, `Base32`, `Base64`, `Hex`

(Listed under [Strings & text](#lambase64--base16-base32-base64-hex).)

### `lamuuid` — `Uuid`

RFC 4122 UUIDs.

```lammergeier
from lamuuid import Uuid
from lamerrors import Result

id: str = Uuid.v4()?               # cryptographic random
sortable: str = Uuid.v7()?         # time-ordered, k-sortable
raw: bytes = Uuid.parse(id)?
```

`v4()` and `v7()` return `Result[str]` for entropy failures; `parse(s)` returns
`Result[bytes]` for malformed IDs. `nil_() -> str` and `isValid(s) -> bool` are
direct.

---

## Database

### `lamdb` — `Db`, `DbConn`, `Tx`, `QueryBuilder`, `Row`, `Where`

Driver-agnostic SQL access. Drivers ship with the runtime — SQLite
(`modernc.org/sqlite`, pure-Go so no C toolchain needed), MySQL
(`go-sql-driver/mysql`), and PostgreSQL (`lib/pq`). Placeholder
dialects are auto-normalised: write `?` everywhere, and the builder
rewrites to `$1`, `$2`, … on Postgres.

```lammergeier
from lamdb import Db, Tx, QueryBuilder
from lamerrors import Result
from lamjson import Json

db: Db = Db.connect("sqlite", ":memory:")?
_ = db.exec("CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, age INTEGER)")?

# Raw SQL — positional params, driver-agnostic ``?``.
n: int = db.exec("INSERT INTO users (name, age) VALUES (?, ?)", ["alice", 30])?

# Knex-style query builder.
rows: list[dict[str, any]] = db.table("users")
    .select(["id", "name"])
    .whereBetween("age", 18, 40)
    .whereGroup(lambda q: q.whereEq("country", "IT").orWhereEq("country", "FR"))
    .whereLike("name", "a%")
    .orderBy("name")
    .limit(10)
    .get()?

# Aggregates preserve execution errors.
total: any = db.table("users").sum("age")?

# Pagination returns ``{data, total, page, perPage, lastPage}``.
page: dict[str, any] = db.table("users").orderBy("id").paginate(1, 15)?

# Upsert (ON CONFLICT for sqlite/postgres, ON DUPLICATE KEY UPDATE for mysql).
_ = db.table("users").upsert({"name": "alice", "age": 31}, ["name"])?

# Native json implements driver.Valuer, so JSON/JSONB parameters need no manual encode.
_ = db.exec("CREATE TABLE configs (id INTEGER, payload JSON)")?
config: json = {"theme": "dark", "features": ["api", "jobs"]}
_ = db.exec("INSERT INTO configs (id, payload) VALUES (?, ?)", [1, config])?
jsonRows: list[dict[str, any]] = db.queryJson(
    "SELECT payload FROM configs WHERE id = ?", ["payload"], [1]
)?
restored: json = Json.fromValue(jsonRows[0]["payload"])?
```

`Db.connect(driver, dsn) -> Result[Db]`. `setPool`, `ping`, `close`, `exec`,
`query`, `first`, `scalar`, JSON-aware `queryJson` / `firstJson` /
`scalarJson`, `transaction`, and `begin` all return typed Results.
`first`, `scalar`, `firstJson` and `scalarJson` stop reading at the first row
(the rest of the result set is neither read nor kept, no `LIMIT` needed;
`firstJson` decodes only that row) and `scalar` is the first column in select
order.
`setRetries(maxRetries, baseMs)`, `driverName`, `raw`, `table(name)` and
`pool()` are direct configuration/introspection helpers. Scoped transactions
commit after a successful callback, roll back and return callback
`Result.Err` values, and roll back then re-raise unexpected callback panics;
`Db.atomic(conn, fn)` does the same and returns the callback's own typed
`Result` (see [connections and typed
transactions](#connections-and-typed-transactions--dbconn-dbatomic-pool)).

`QueryBuilder` predicates (chainable, AND-joined by default):

- **where family**: `where(col, op, val)`, `whereEq`, `orWhere`,
  `orWhereEq`, `whereIn`, `whereNotIn`, `whereNull`, `whereNotNull`,
  `whereBetween`, `whereNotBetween`, `whereLike`, `whereNotLike`,
  `whereRaw(fragment, params)`, `orWhereRaw`.
- **nested groups**: `whereGroup(callback)` and `orWhereGroup(callback)` pass a
  child builder to the callback and preserve parentheses, placeholder order,
  and AND/OR logic, allowing `A = B AND (A = C OR A = D)` and deeper nesting.
- **structure**: `select(cols)`, `selectRaw(expr)`, `distinct()`,
  `join`, `leftJoin`, `rightJoin`, `groupBy(cols)`, `having`,
  `orderBy(col, dir)`, `limit`, `offset`, `forUpdate`, `forShare`.

`QueryBuilder` terminals use unsuffixed typed Results:

- **read**: `get`, `first`, `count`, `exists`, `pluck`, `sum` / `avg` / `min` /
  `max`, and `paginate` return their corresponding `Result[T]`; `toSql()` is a
  direct preview that does not execute.
- **write**: `insert`, `insertMany`, `update`, `delete`, `truncate`,
  `increment`, `decrement`, `upsert`, `firstOrCreate`, `updateOrCreate`, and
  `insertReturning` return typed Results. `insertReturning` is Postgres-only;
  pair it with `returning(cols)` to select columns.

Transactions give you three patterns:

```lammergeier
# Typed — Db.atomic returns the callback's own Result: commits on Ok, rolls
# back on Err or a throw (re-thrown); on a Tx it is a nested savepoint.
func work(tx: Tx) -> Result[int] {
    _ = tx.exec("INSERT INTO users (name, age) VALUES (?, ?)", ["bob", 20])?
    return tx.table("users").whereEq("name", "alice").update({"age": 31})
}
updated: int = Db.atomic(db, work)?

# Scoped (Result[bool]) — Ok(true) committed, Ok(false) when the callback
# called tx.rollback() itself.
func legacyWork(tx: Tx) -> Result[bool] {
    _ = tx.exec("INSERT INTO users (name, age) VALUES (?, ?)", ["dan", 22])?
    return Result.Ok(true)
}
ok: bool = db.transaction(legacyWork)?

# Explicit — you own commit()/rollback().
tx: Tx = db.begin()?
_ = tx.exec("INSERT INTO users (name, age) VALUES (?, ?)", ["carol", 21])?
_ = tx.commit()?
```

`Tx` also provides `savepoint(name)`, `rollbackTo(name)`, and
`releaseSavepoint(name)` for nested control, plus `tx.table(name)`
for a builder bound to the transaction so chained writes stay
atomic with the surrounding `tx.exec` calls.

`Tx` reads rows like `Db`: `tx.first(query, params)` (the first row, `{}`
when there is none), `tx.scalar` (the first column of the first row,
`Ok(None)` for no rows) and the JSON-aware `tx.queryJson` / `tx.firstJson` /
`tx.scalarJson` have the semantics of the `Db` methods of the same name,
but run through `tx.query` on the transaction's connection: they see its
uncommitted writes, use its context and are reported to its observer as one
statement each. Code written against `DbConn` (the interface `Db` and `Tx`
share, below) accepts a `Db` or a `Tx` unchanged:

```lammergeier
from lamdb import Db, DbConn, Row, Tx

func userName(conn: DbConn, id: int) -> Result[str] {
    row: dict[str, any] = conn.first("SELECT name FROM users WHERE id = ?", [id])?
    return Result.Ok(Row.str(row, "name", "unknown"))
}

name: str = userName(db, 7)?          # outside a transaction
tx: Tx = db.begin()?
_ = tx.exec("UPDATE users SET name = ? WHERE id = ?", ["Ada", 7])?
renamed: str = userName(tx, 7)?       # "Ada": the transaction's own write
_ = tx.rollback()?
```

#### Connections and typed transactions — `DbConn`, `Db.atomic`, `pool()`

`DbConn` is the type services should take. A `Db` (the pool), a
`db.withContext(ctx)` view and a `Tx` all satisfy it structurally:

```lammergeier
interface DbConn {
    func exec(query: str, params: list[any] = []) -> Result[int]
    func query(query: str, params: list[any] = []) -> Result[list[dict[str, any]]]
    func first(query: str, params: list[any] = []) -> Result[dict[str, any]]
    func scalar(query: str, params: list[any] = []) -> Result[any]
    func queryJson(query: str, jsonColumns: list[str], params: list[any] = []) -> Result[list[dict[str, any]]]
    func firstJson(query: str, jsonColumns: list[str], params: list[any] = []) -> Result[dict[str, any]]
    func scalarJson(query: str, params: list[any] = []) -> Result[json]
    func table(name: str) -> QueryBuilder
    func transaction(fn: any) -> Result[bool]
    func pool() -> Db
}
```

`Db.atomic(conn: DbConn, fn: func(Tx) -> Result[T]) -> Result[T]` runs `fn`
as one unit of work and returns **its** `Result`:

- on a `Db` (plain or scoped) it begins a transaction (`BEGIN`), runs `fn`
  with the new `Tx`, commits when `fn` returns `Ok` and rolls back when it
  returns an `Err` (returned unchanged, the same `Error`) or throws (the
  exception is re-thrown after the rollback). A begin failure is returned
  without running `fn`; a commit failure (a deferred constraint, a lost
  connection) is returned as a `DbTransactionError`, its driver cause kept
  (`Db.isForeignKeyViolation(result)` works);
- on a `Tx` it runs `fn` in a savepoint of that transaction (`SAVEPOINT
  lam_sp_<depth>`): `Ok` releases it (`RELEASE SAVEPOINT`), `Err` or a throw
  rolls back to it and releases it (`ROLLBACK TO SAVEPOINT`, `RELEASE
  SAVEPOINT`), and the outer transaction goes on. On PostgreSQL this is also
  how a failing statement (a unique violation you handle, a missing table)
  is recovered from: inside the savepoint it does not abort the outer
  transaction. A savepoint failure (the transaction already ended, a
  cancelled context) is returned without running `fn`.

Units therefore compose: a service that calls `Db.atomic(conn, ...)` is a
transaction when called with a `Db` and a nested savepoint of the caller's
transaction when called with a `Tx`. `T` is bound from the callback (a
function passed by name — top-level or nested —, an annotated lambda, or a
lambda whose body calls a typed function or method), and checked:
`n: str = Db.atomic(db, returnsInt).unwrap()` is a compile error.

Inside a nested level the callback receives the **same** `Tx` handle;
`tx.depth()` is the number of open levels (`0` in the transaction itself).
While a level is open, `tx.commit()` releases its savepoint and
`tx.rollback()` rolls back to it and releases it — never the outer
transaction — and a second `commit()` / `rollback()` of the same level is a
`DbTransactionError`; statements after them run in the enclosing level.
`Db.atomic` returns the callback's result even when the callback ended its
level itself. `begin()` stays on `Db` only: open nested levels with
`Db.atomic(tx, fn)` (or `tx.transaction(fn)`). A `Tx` is not safe for
concurrent use; give each goroutine its own `Db.atomic` on the pool or a
scoped view.

`conn.pool()` is the owning pool: the `Db` itself for a pool, the pool a
`withContext` view was derived from (also through nested views), and for a
`Tx` the pool of the `Db` that began it (`None` for a `Tx()` built by hand).
Use it to hand a `Db` to a library that needs one (`Jobs.migrate(conn.pool())`);
the pool runs outside the transaction and without the view's context (scope it
again with `conn.pool().withContext(ctx)` if needed).

`db.transaction(fn: any) -> Result[bool]` is kept as it was: `fn` takes the
`Tx` and returns a `Result` (or nothing); `Ok(true)` means committed and
`Ok(false)` that the callback called `tx.rollback()` itself; callback errors,
begin and commit failures are returned as errors. `tx.transaction(fn)` is the
same on a `Tx`, in a savepoint (`Ok(false)` when the callback rolled its level
back). Prefer `Db.atomic` for new code: it returns the callback's typed value
and works on any `DbConn`.

A service taking a `DbConn` and composing units:

```lammergeier
from lamdb import Db, DbConn, Row, Tx
from lamerrors import Result, Error

func debit(conn: DbConn, account: int, amount: int) -> Result[int] {
    row: dict[str, any] = conn.first("SELECT balance FROM accounts WHERE id = ?", [account])?
    if Row.int(row, "balance") < amount {
        return Result.Err(Error("InsufficientFunds", f"account {account}"))
    }
    return conn.exec("UPDATE accounts SET balance = balance - ? WHERE id = ?", [amount, account])
}

func credit(conn: DbConn, account: int, amount: int) -> Result[int] {
    return conn.exec("UPDATE accounts SET balance = balance + ? WHERE id = ?", [amount, account])
}

func transfer(conn: DbConn, source: int, target: int, amount: int) -> Result[int] {
    # A transaction on a Db, a savepoint inside the caller's Tx.
    func move(tx: Tx) -> Result[int] {
        _ = debit(tx, source, amount)?
        _ = credit(tx, target, amount)?
        return tx.exec("INSERT INTO ledger (source, target, amount) VALUES (?, ?, ?)", [source, target, amount])
    }
    return Db.atomic(conn, move)
}

func payroll(conn: DbConn, payer: int, payees: list[int]) -> Result[int] {
    # All or nothing; a payee that fails is skipped (its savepoint rolled back).
    func run(tx: Tx) -> Result[int] {
        paid: int = 0
        for payee in payees {
            if transfer(tx, payer, payee, 100).ok() {
                paid += 1
            }
        }
        return Result.Ok(paid)
    }
    return Db.atomic(conn, run)
}

paid: int = payroll(db.withContext(req.ctx), 1, [2, 3, 4])?
```

Automatic retries on transient errors (closed connection, server gone away,
bad conn…) are bounded by `setRetries` (negative settings become zero).
A scoped context's cancellation or deadline interrupts retry backoff.
Exhausted transient and non-transient errors are returned by the same
unsuffixed operation as `Result.Err`.

#### Request-scoped database observation

`Db.withContext(ctx) -> Db` creates a handle sharing the owning pool, with
`ctx` used for SQL execution and transactions (including cancellation and
deadlines). Give each request its own context and scoped handle (for example,
pass a Vulture handler's `req.ctx`); raw `exec` / `query`,
`db.table(...).get()` and other builder terminals use that context.
`begin()` and `transaction(fn)` create a `Tx` inheriting it, so `tx.exec`,
`tx.query` (and `tx.first`, `tx.scalar` and the JSON reads built on it) and
`tx.table(...)` use the same request context. Keep the owner
open for the pool's lifetime; `close()` on a scoped handle returns an error
instead of closing the pool. Configure `setRetries` before creating scoped
handles: each view snapshots the owner's retry count and base delay, while
`setPool` still configures the shared underlying pool. Do not store a mutable
"current context" on the shared `Db` or swap one context between concurrent
requests.

An optional `DbObserver` receives `start(ctx: Context, statement: str) -> any`
before an operation and `finish(ctx: Context, token: any, failure: str)`
afterwards. The `token` is exactly what `start` returned; `failure` is `""`
on success and the stable `Error.kind` (such as `DbQueryError`) for a failed
operation, never a driver message or bound value. Configure
`pool.setObserver(observer)` once before sharing the pool (omit it if no
observation is needed). Raw SQL and builder terminals both reach the observer;
transaction begin, commit and rollback are reported as `BEGIN`, `COMMIT` and
`ROLLBACK`, including those performed by `transaction(fn)`. Savepoint,
rollback-to-savepoint and release statements are also observed and respect the
context. The observer should be safe for concurrent requests. A transaction
captures the observer and context when it begins.

For example, with a `users` table containing `id`, `name` and `visits`:

```lammergeier
from lamdb import Db, DbObserver, Tx
from lamcontext import Context
from lamerrors import Result

class RequestObserver {
    func start(self, ctx: Context, statement: str) -> any {
        return ctx.get("requestId", "")
    }

    func finish(self, ctx: Context, token: any, failure: str) {
        if failure != "" {
            print(f"database operation failed for request {token}")
        }
    }
}

func handleRequest(pool: Db, ctx: Context, userId: int) -> Result[bool] {
    scoped: Db = pool.withContext(ctx)
    _ = scoped.query("SELECT id FROM users WHERE id = ?", [userId])?
    _ = scoped.table("users").whereEq("id", userId).get()?

    func updateUser(tx: Tx) -> Result[bool] {
        _ = tx.exec("UPDATE users SET visits = visits + ? WHERE id = ?", [1, userId])?
        _ = tx.table("users").whereEq("id", userId).update({"name": "Ada"})?
        return Result.Ok(true)
    }
    _ = scoped.transaction(updateUser)?

    tx: Tx = scoped.begin()?
    checked: Result[list[dict[str, any]]] = tx.query("SELECT id FROM users WHERE id = ?", [userId])
    if not checked.ok() {
        _ = tx.rollback()
        return Result.Err(checked.error)
    }
    _ = tx.commit()?
    return Result.Ok(true)
}

func main() {
    do {
        pool: Db = Db.connect("sqlite", "file:app.db?cache=shared")?
        defer pool.close()
        observer: DbObserver = RequestObserver()
        pool.setObserver(observer)
        requestCtx: Context = Context.simple().valued("requestId", "req-42")
        _ = handleRequest(pool, requestCtx, 7)?
    } catch err {
        print(f"database error: {err}")
    }
}
```

Remove `setObserver` when observation is not wanted; `withContext` still
propagates cancellation and deadlines. The callback transaction rolls back
on a returned `Result.Err`, while explicit transactions require you to call
`rollback()` on an error before returning. SQL `statement` is the SQL text
with placeholders, **not bound parameter values**; the observer has no
`params` argument. Always bind values through `?` and `params` (including
builder predicates), never interpolate private data into SQL text. A raw SQL
string may itself contain sensitive literals even though the observer sees
only an error kind, not a driver error message. Avoid putting secrets in
request IDs or observer tokens.

#### Constraint violations — `Db.sqlState`

A driver failure keeps the driver's error as the `Error.cause`, so its code
can be read without matching messages:

```lammergeier
inserted: Result[int] = db.exec("INSERT INTO users (email) VALUES (?)", [email])
if Db.isUniqueViolation(inserted) {
    return Result.Err(Error("SignupError", f"{email} is already registered"))
}
_ = inserted?
```

| Method | Returns |
|--------|---------|
| `Db.sqlState(err)` | `str`: the SQLSTATE, `""` when there is no driver error |
| `Db.isUniqueViolation(err)` | `bool`: SQLSTATE `23505` (unique or primary key) |
| `Db.isForeignKeyViolation(err)` | `bool`: SQLSTATE `23503` |

`err` is a failed `Result` (from `exec`, `query`, the builder, `transaction`
— a deferred constraint failing at commit included), its `Error`, or a driver
error. PostgreSQL reports its own SQLSTATE (`23505`, `23503`, `23502` not null,
`23514` check, `40001` serialization failure, `42P01` undefined table...).
SQLite's extended result codes and MySQL's error numbers are mapped to the
same class 23 codes (`23505` unique / primary key, `23503` foreign key —
SQLite checks them only with `PRAGMA foreign_keys = ON` —, `23502` not null,
`23514` check, `23000` other constraints); other MySQL errors give their own
SQLSTATE and other SQLite errors `""`. A success, an `Error` built by hand or a
value that is not an error gives `""` / `false`. Printing such an error is
unchanged: an `Error` whose cause reads as its message does not repeat it.

#### WHERE clauses for hand-written SQL — `Where`

`Where` collects conditions and their `?` parameters in order, for SQL that
the query builder does not express:

```lammergeier
from lamdb import Db, Row, Where

w: Where = Where().add("shop_id = ?", [shopId]).addIf(onlyActive, "disabled = ?", [false])
w.addIn("status", ["paid", "shipped"]).ilikeAny(["title", "brand", "id"], search)   # PostgreSQL
rows = db.query(f"SELECT * FROM products {w.sql()} ORDER BY id LIMIT ? OFFSET ?", w.paramsWith([size, offset]))?
# SELECT * FROM products WHERE (shop_id = ?) AND (status IN (?, ?)) AND
#   (CAST(title AS TEXT) ILIKE ? ESCAPE '!' OR ...) ORDER BY id LIMIT ? OFFSET ?
```

| Method | Returns |
|--------|---------|
| `Where()` | an empty clause |
| `add(clause, params = [])` / `addIf(condition, clause, params = [])` | the `Where` (chainable) |
| `addIn(column, values)` | `column IN (?, ...)`; an empty list matches no row (`1 = 0`) |
| `ilikeAny(columns, text, match = "contains")` | PostgreSQL `CAST(col AS TEXT) ILIKE ? ESCAPE '!'` over the columns, OR-ed |
| `likeAny(columns, text, match = "contains")` | `col LIKE ? ESCAPE '!'`, every driver |
| `sql(prefix = "WHERE")` | `str`: `""` with no condition |
| `params()` / `paramsWith(extra)` | `list[any]` (a copy; `extra` appended) |
| `isEmpty()` | `bool` |
| `Where.likePattern(text, match = "contains", escape = "\\")` | `str` |

Conditions are joined with `AND`, each in parentheses, so a condition holding
`OR` keeps its meaning, also when more SQL follows (`w.sql() + " AND
visible"`). `ilikeAny` / `likeAny` trim `text` and add nothing when it is
empty; the `%` and `_` the user typed match themselves. `match` is
`"contains"` (`%text%`), `"prefix"`, `"suffix"` or `"exact"`.
`Where.likePattern` escapes the escape character, `%` and `_` for a
hand-written `LIKE ?`: the default backslash is PostgreSQL's and MySQL's
default escape (SQLite needs `ESCAPE '\'`). Column names and SQL fragments
are inserted as written — never build them from user input; values always
go through parameters.

#### Typed row values — `Row`

Rows are `dict[str, any]` holding the drivers' values: `int64`, `float64`,
`bool`, text (`str`; PostgreSQL `NUMERIC`, `uuid`, arrays and `bytea` arrive as
text too), date/time columns as Go `time.Time`, and `None` for NULL. `Row`
reads them typed, so applications need no conversion helpers:

```lammergeier
from lamdb import Db, Row
from lamdatetime import DateTime

row: dict[str, any] = db.first("SELECT id, price, name, active, created_at, tags FROM products WHERE id = ?", [7])?
id: int = Row.int(row, "id")                       # bigint (int64)
price: float = Row.float(row, "price")             # NUMERIC text "123.45"
name: str = Row.str(row, "name")
active: bool = Row.bool(row, "active")
created: optional[DateTime] = Row.time(row, "created_at")   # UTC DateTime
createdUnix: int = Row.unix(row, "created_at")
tags: list[str] = Row.strList(row, "tags")         # text[] "{a,\"b c\",NULL}"
note: optional[str] = Row.optionalStr(row, "note") # None for NULL

_ = db.exec("UPDATE products SET tags = ?::text[] WHERE id = ?", [Db.textArray(["bio", "local"]), id])?
```

| Method | Returns | Reads |
|--------|---------|-------|
| `Row.int(row, key, fallback = 0)` | `int` | integers, floats (truncated), numeric text, `bool` (1/0), big numbers, a time as Unix seconds |
| `Row.float(row, key, fallback = 0.0)` | `float` | floats, integers, numeric text (`NUMERIC`), big numbers |
| `Row.str(row, key, fallback = "")` | `str` | text as it is; numbers in decimal (floats shortest, no exponent); `true` / `false`; times in RFC 3339 UTC with fractional seconds |
| `Row.bool(row, key, fallback = false)` | `bool` | `bool`, numbers (non-zero), `t` / `true` / `yes` / `on` / `1` and `f` / `false` / `no` / `off` / `0` (any case) |
| `Row.unix(row, key, fallback = 0)` | `int` | `time.Time`, Unix seconds, timestamp text (`2026-11-03 19:05:00+01`, RFC 3339, `2026-11-03`; no offset is UTC) |
| `Row.time(row, key)` | `optional[DateTime]` | as `unix`, in UTC (`inLocation` for another zone) |
| `Row.timeText(row, key, layout = "2006-01-02T15:04:05Z07:00", location = "UTC", fallback = "")` | `str` | as `unix`, written with a Go layout: by default RFC 3339 in whole seconds (`2026-11-03T19:05:00Z`, where `Row.str` keeps the fraction); an unknown location throws a `LocationError` |
| `Row.optionalStr` / `optionalInt` / `optionalFloat` / `optionalBool(row, key)` | `optional[T]` | as above; `None` for NULL, a missing column, or a value that does not convert |
| `Row.isNull(row, key)` | `bool` | NULL or a missing column |
| `Row.strList(row, key)` / `Row.intList(row, key)` | `list[str]` / `list[int]` | a PostgreSQL array literal; NULL elements are dropped, NULL is `[]` |
| `Db.textArray(values)` | `str` | writes `{"a","b c"}` (every element quoted, `"` and `\` escaped) for a `?::text[]` parameter |
| `Db.parseTextArray(text)` | `Result[list[str]]` | parses an array literal; text that is not one is a `DbArrayError` |

A missing column, NULL, or a value that does not convert gives `fallback`
(the `optional` variants give `None`); `Row.time` is `None` for instants outside
the years 1678–2261 (`DateTime` holds Unix nanoseconds), such as a `time`
column's `0000-01-01` date.

### `lammigrate` — `Migrator`, `SchemaBuilder`

Knex-style schema migrations on top of `lamdb`. Each migration is
a `.sql` file with two sections delimited by marker comments
(`-- +lam up` / `-- +lam down`). Files are sorted lexicographically
by their `YYYYMMDDHHMMSS_name.sql` filename, applied in order, and
recorded in a `lam_migrations` bookkeeping table grouped by "batch"
so a single `down(1)` rewinds the entire batch atomically.

```lammergeier
from lamdb import Db
from lammigrate import Migrator, SchemaBuilder
from lamerrors import Result

db: Db = Db.connect("sqlite", "/var/data/app.db")?
mig: Migrator = Migrator(db, "./migrations")
_ = mig.ensureSchema()?
applied: list[str] = mig.up()?                  # apply every pending file
rolled: list[str] = mig.down(1)?                # roll back the last batch
info: dict[str, any] = mig.status()?            # summary for humans
```

`Migrator.ensureSchema() -> Result[bool]`; `list` and `status` return
`Result[dict[str, any]]`; `up(steps=0)` and `down(steps=1)` return
`Result[list[str]]`. Directory, migration-file, and SQL errors propagate
through those primary methods. `setTableName(name)` remains direct. Zero up
steps means all pending; one down step means the last batch, while values above
one roll back exactly that many files.

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

`SchemaBuilder` is an optional helper that emits portable DDL for the
three supported dialects so common columns don't need driver-
specific casts. It was called `Schema` before 2026-10-15; the old name
clashed with lamschema's `Schema`, so the two modules could not be linked
into one program (for example together with Vulture):

```lammergeier
schema: SchemaBuilder = SchemaBuilder(db.driverName())
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

> Redis and Memcached clients now release independently as
> [`@lam/redis`](libraries.md) and
> [`@lam/memcached`](libraries.md). Their service fixtures and
> integration tests live with those packages rather than the compiler suite.

---

## Quick reference

| Want to… | Reach for |
|----------|-----------|
| Manipulate strings | `lamstrings`, `lamunicode`, `lamre` |
| Parse / build JSON | `lamjson` |
| Parse / build YAML (Kubernetes manifests, CI configs, …) | `lamyaml` |
| Build / query HTML pages and fragments | native `html` literals, `lamhtml` |
| Clean untrusted HTML (comments, rich text) against XSS | `lamhtml.Html.sanitize` + `HtmlPolicy` |
| Parse / build XML (RSS feeds, SOAP, OpenSearch, sitemaps) | native `xml` literals, `lamxml` |
| Build a web service | External `@lam/vulture` + `@lam/vulture/std-plugin` packages |
| Validate request bodies | `lamschema` (JSON Schema) |
| Issue / verify JWTs (with kid + JWKS rotation) | External [`@lam/jwt`](libraries.md) package |
| Encode / decode protobuf | External [`@lam/protobuf`](libraries.md) package |
| Add WebSockets | External `@lam/vulture/ws-plugin` package (`wsRoute` + typed `RouteOptions`) |
| Receive resumable uploads (concat + checksum) | External `@lam/vulture/tus-plugin` package (tus.io 1.0.0) |
| Receive multipart form uploads | `Request.formField` / `formFile` (in `@lam/vulture`) |
| Make HTTP calls (one-shot) | Result-returning `lamhttp.Http.get` / overloaded `.post` |
| Make HTTP calls (reusable client w/ baseUrl, headers, timeout) | `lamhttp.HttpClient` (`get`/`head`/`delete`/`post(Json)`/`put(Json)`/`patch(Json)`) |
| Talk to a SQL database | `lamdb` (raw SQL, native JSON/JSONB values, `QueryBuilder`, transactions + savepoints, `DbConn` services with typed nested `Db.atomic` units) |
| Read typed values from database rows (NUMERIC, timestamps, `text[]`) | `lamdb.Row` (`Row.int` / `str` / `float` / `bool` / `time` / `timeText` / `strList`), `Db.textArray` |
| Tell a duplicate key from other database failures | `Db.isUniqueViolation`, `Db.isForeignKeyViolation`, `Db.sqlState` |
| Build WHERE clauses for hand-written SQL (filters, ILIKE search) | `lamdb.Where` |
| Read typed members of JSON request bodies | `Json.getStr` / `getInt` / `getFloat` / `getBool` / `getList` / `getObject` / `has` / `at` |
| Rename JSON keys (`snake_case` <-> `camelCase`) | `Json.mapKeys` + `Strings.camelCase` / `snakeCase` / `kebabCase` |
| Build / parse query strings, escape URL parts | `Url.query`, `Url.parseQuery`, `Url.encodeComponent` / `encodePath` / `encodeUserInfo` |
| Send multipart form uploads | `lamhttp.Multipart` + `HttpRequestOptions.multipart` |
| Format numbers with thousands separators (`1.234,50`) | `Fmt.grouped` |
| Run schema migrations | `lammigrate` + `lamc migrate make/up/down/status` |
| Talk to Redis (strings / native JSON / lists / hashes / sets / zsets / pub-sub / pipelines) | External [`@lam/redis`](libraries.md) package |
| Talk to memcached over the binary protocol (incl. SASL PLAIN auth) | External [`@lam/memcached`](libraries.md) package |
| Read / write files | `lamos`, `lampath` |
| Read on/off switches and comma-separated lists from the environment | `Env.flag`, `Env.list` |
| Encrypt, hash, sign, or store passwords safely | `lamcrypto`, `lamhash`, `lambase64`, `lamuuid` |
| Verify existing bcrypt password hashes (`$2a$` / `$2b$` / `$2y$`) | `lamcrypto` (`Crypto.bcryptVerify`, `bcryptHash`, `bcryptCost`) |
| Compute statistics or matrix algebra | `lamstats`, `lamarray` |
| Propagate cancellation, deadlines, and request values | `lamcontext.Context` |
| Shut down gracefully on SIGINT / SIGTERM | `lamcontext.Context.onSignals()`, `lamos.Os.waitForSignal()` |
| Schedule work concurrently | `lamconcurrency`, `lamactor` (message-passing) |
| Cache lookup results | `lamcache` |
| Iterate lazily | `lamiter` |
| Render text / HTML templates | `lamtemplate` (`Template`, `HtmlTemplate`) |
| Translate pages and mails (JSON files, fallback language, `{name}` parameters, `Accept-Language`) | `lami18n.Translations` |
| Retry with exponential backoff + jitter | `lamretry.Retry` |
| Throttle outbound calls (token bucket) | `lamratelimit.TokenBucket` |
| Trip a circuit on downstream failure | `@lam/vulture/std-plugin.CircuitBreaker` |
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

### Native `html` / `xml`, `lamhtml` — `Html`, `lamxml` — `Xml`

`html` and `xml` are native types written as literals between `<>` and
`</>` (see [SYNTAX.md, Markup: html and xml](SYNTAX.md#markup-html-and-xml)).
Their runtime, `lammarkup`, is imported automatically. The modules hold
parsing, escaping and the dict/list XML encoding:

```lammergeier
from lamhtml import Html
from lamxml import Xml
from lamerrors import Result

func titles(feedText: str) -> Result[list[str]] {
    feed: xml = Xml.parse(feedText)?
    return Result.Ok([item.first("title").text() for item in feed.select("channel > item")])
}

func main() {
    snippet: html = Html.parse("<p class=note>Hi <b>there</b>")
    snippet.first("b").setText("you")
    print(snippet)                                   # <p class="note">Hi <b>you</b></p>
    print(Html.escape("<a & b>"))                    # &lt;a &amp; b&gt;
    person: xml = Xml.encode({"name": "alice", "age": 30}, "person")
    shape: any = Xml.decode(str(person)).unwrap()    # dict: name -> "alice", age -> "30"
    print(shape)
}
```

| API | Result |
| --- | --- |
| `Html.parse(text) -> html` | lenient HTML reading (never fails); only for trusted text |
| `Html.escape(text) -> str` / `Xml.escape(text) -> str` | text escaped for content and attribute values |
| `Xml.parse(text) -> Result[xml]` | well-formed XML or an `XmlError` |
| `Xml.encode(value, root="root") -> xml` | maps become one child per key, lists repeated `<item>`, scalars text |
| `Xml.decode(text) -> Result[any]` | the shape-shifted form: `dict[str, any]` (attributes under `@name`), repeated tags as `list[any]`, leaf text as `str` |

The DOM-style methods of the values (`select`, `first`, `text`, `attr`,
`setAttr`, `addClass`, `append`, `remove`, `replaceWith`, `clone`...) are
listed in SYNTAX.md. `XmlNode` was removed in 4.0.0: `Xml.parse` returns the
native `xml` value.

#### Sanitizing untrusted HTML — `Html.sanitize`, `HtmlPolicy`

`Html.sanitize(markup, policy = HtmlPolicy.basic()) -> html` turns markup
you do not trust (user comments, rich-text editor output, admin-authored
snippets, an `html` value built from them) into an `html` value that keeps
only what the policy allows:

```lammergeier
from lamhtml import Html, HtmlPolicy

clean: html = Html.sanitize(comment.body)          # basic formatting and links
article: html = Html.sanitize(editorHtml, HtmlPolicy.rich().allowStyles(["color", "text-align"]))
page: html = <><section>${clean}</section></>

policy: HtmlPolicy = HtmlPolicy().allowTags(["p", "a", "b"]).allowAttributes("a", ["href"]).allowSchemes(["tel"])
```

| `HtmlPolicy` | Allows |
| --- | --- |
| `HtmlPolicy()` | nothing: only the text survives |
| `HtmlPolicy.basic()` | `a` (`href`, `title`), `abbr`, `b`, `blockquote` / `q` (`cite`), `br`, `cite`, `code`, `del`, `em`, `i`, `ins`, `kbd`, `li`, `mark`, `ol`, `p`, `pre`, `s`, `small`, `span`, `strong`, `sub`, `sup`, `u`, `ul`; `http` / `https` / `mailto` and relative URLs |
| `HtmlPolicy.rich()` | `basic` plus `h1`–`h6`, `hr`, `div`, `section`, `article`, `aside`, `header`, `footer`, `img` (`src`, `alt`, `width`, `height`), `figure`, `figcaption`, tables (`colspan`, `rowspan`, `scope`), `dl` / `dt` / `dd`, `details` (`open`) / `summary`, `a` `target` / `rel`, and `class`, `title`, `lang`, `dir` everywhere |
| `allowTags(tags)` / `denyTags(tags)` | adds / removes elements |
| `allowAttributes(tag, attributes)` | attributes on `tag`, `"*"` for every allowed element |
| `allowSchemes(schemes)` | more URL schemes (`javascript`, `vbscript`, `data` are refused) |
| `allowRelativeUrls(allowed = true)` | relative URLs on or off |
| `allowDataImages(allowed = true)` | base64 PNG / GIF / JPEG / WebP / AVIF `data:` URLs in `img src` |
| `allowStyles(properties)` | the `style` attribute, keeping only these CSS properties |
| `allowFrames(hosts)` | `iframe` embeds of these `https` hosts and their subdomains (below) |
| `HtmlPolicy.videoHosts()` | `["www.youtube.com", "www.youtube-nocookie.com", "player.vimeo.com"]`, for `allowFrames` |

The policy methods return the policy, so they chain; its fields (`tags`,
`attributes`, `urlSchemes`, `relativeUrls`, `dataImages`, `styleProperties`,
`noopener`, `frameHosts`) can also be set directly. `Html.sanitize` compiles a
policy (its lookup tables) once and reuses it: the policy object keeps its
compiled form, and a small process-wide cache serves a policy built again with
the same content (`HtmlPolicy.basic()` in a handler). Build the policy once
(a constant or a field) and share it: a policy that is not changed any more
is safe to use from many goroutines at once, and sanitizing never changes it.
Changing a field after a `sanitize` call takes effect on the next call (the
fields are compared with what was compiled). The sanitizer works on the parsed tree
and builds a new one, default-deny — what it covers:

- **Elements**: an element the policy does not allow is removed and its
  content kept. `script`, `style`, `template`, `iframe`, `frame`,
  `frameset`, `object`, `embed`, `applet`, `noscript`, `noembed`,
  `noframes`, `xmp`, `plaintext`, `svg`, `math`, `base`, `link`, `meta`,
  `head`, `title`, `param` and `portal` are removed with their content even
  when a policy lists them (`iframe` is kept only as `allowFrames`
  describes). Comments (including IE conditional comments),
  doctypes and processing instructions are removed. Nesting deeper than 256
  elements is flattened.
- **Attributes**: only those allowed for the element (or `"*"`) are kept;
  event handlers (`on*`) and `srcdoc` never are; of a duplicated attribute
  the first is kept, as browsers do.
- **URLs** (`href`, `src`, `action`, `formaction`, `cite`, `poster`,
  `background`, `longdesc`, `xlink:href`, `srcset` candidates...): the
  value is kept only when its scheme is allowed or it is relative (when
  allowed). The scheme is read after removing control characters and white
  space, which browsers ignore there, and after entities are decoded, so
  `java&#x09;script:`, `&#106;avascript:`, `javascript&colon;` and
  ` javascript:` are all `javascript:`. `data:` URLs are refused except
  base64 raster images in `img src` with `allowDataImages` (never
  `image/svg+xml`).
- **Styles**: `style` attributes are removed unless `allowStyles` lists
  properties; then only those properties are kept, with values made of
  letters, digits, spaces and `# % . , ( ) + - ! /`, and without `url(`,
  `expression`, `javascript`, `image(`, `attr(`, `var(` or comments.
- **Links** with a `target` other than `_self` / `_parent` / `_top` get
  `rel="noopener noreferrer"` (unless `noopener` is turned off).
- **Output**: the result is serialized from the new tree, so text and
  attribute values are always escaped and the only markup is the allowed
  elements: the browser parses what the policy checked, which closes the
  parser-differential (mXSS) tricks that rely on re-parsing comments, raw
  text or foreign content. Sanitizing an output again gives it back
  unchanged.

##### Trusted embeds — `allowFrames`

Frames stay removed unless a policy opts in with `allowFrames(hosts)`, for
markup whose author you trust with embeds (admin-authored ads, editorial
pages) and only for the hosts listed:

```lammergeier
adPolicy: HtmlPolicy = HtmlPolicy.rich().allowFrames(HtmlPolicy.videoHosts())
ad: html = Html.sanitize(admin.body, adPolicy)
# <iframe src="https://www.youtube.com/embed/x" width="560" height="315" allowfullscreen
#   referrerpolicy="strict-origin-when-cross-origin"
#   sandbox="allow-scripts allow-same-origin allow-presentation allow-popups"></iframe>
```

- **Hosts**: an `iframe` is kept only when its `src` is an absolute
  `https://` URL whose host equals a listed host or is a subdomain of one
  (`"vimeo.com"` also allows `player.vimeo.com`; `www.youtube.com` does not
  allow `youtube.com` nor `www.youtube.com.evil.com`). Entries are plain
  ASCII host names, lowercased; `host:port` allows that port instead of the
  default 443 (another port is refused). Other entries are ignored.
- **URL reading**: entities are decoded first and control characters and
  spaces at the ends ignored, as for other URLs. Anything browsers would
  read differently from what was checked refuses the frame: white space or
  control characters inside the URL (`ht&#9;tps:`), a backslash, non-ASCII
  characters (IDN look-alikes such as a Cyrillic `о`; write
  internationalised hosts as their `xn--` form, compared as written),
  userinfo (`https://www.youtube.com@evil.com`), a percent-encoded host, an
  IP address, an empty host (`https:///evil.com`), a trailing dot. `http:`
  (downgrade), protocol-relative (`//host`), relative, `data:` and
  `javascript:` sources and frames without `src` are removed. The kept
  `src` has its scheme and host lowercased and `:443` dropped.
- **Attributes**: the frame is rebuilt with `src`, `width` / `height`
  (digits, optionally `%`), `title`, `allow` (only `accelerometer`,
  `autoplay`, `clipboard-write`, `encrypted-media`, `gyroscope`,
  `picture-in-picture`, `fullscreen`, without origin lists),
  `allowfullscreen`, `loading` (`lazy` / `eager`) and `referrerpolicy`
  (`no-referrer`, `origin`, `same-origin`, `strict-origin`, or the default
  `strict-origin-when-cross-origin`), in that order. `srcdoc`, `name`,
  event handlers, `style`, `class`, `id`, `csp` and everything else are
  dropped, and so is the frame's content (fallback text, nested frames).
- **Sandbox**: every kept frame gets `sandbox="allow-scripts
  allow-same-origin allow-presentation allow-popups"`, replacing the
  markup's: players need their scripts and their own origin (cookies,
  storage), full screen and "watch on YouTube" windows, but a frame cannot
  navigate your page (`allow-top-navigation`), submit forms, open modal
  dialogs or download. `allow-scripts` with `allow-same-origin` would let a
  frame of *your own* origin remove its sandbox, so never list the host
  that serves your pages.
- `frame`, `frameset`, `object`, `embed` and frames inside `svg` / `math`
  or another frame are always removed; policies without `allowFrames`
  behave as before.

`id` and `name` (DOM clobbering) and `class` are not allowed by `basic`;
allow them only when the page cannot be confused by them. The sanitizer does
not judge where a link leads (an allowed `https:` URL can still point to a
phishing site) and does not rewrite CSS.

### `lamyaml` — YAML 1.2 encode / decode

```lammergeier
from lamyaml import Yaml
from lamerrors import Result

doc: any = Yaml.decode("name: alice\nage: 30")?
yamlStr: str = Yaml.encode(doc)?

# Multi-document streams (--- separators):
docs: list[any] = Yaml.decodeAll("---\nname: a\n---\nname: b\n")?
```

`encode -> Result[str]`, `decode -> Result[any]`, and
`decodeAll -> Result[list[any]]`. Backed by `gopkg.in/yaml.v3`. Non-string YAML
keys are stringified during normalisation.

### `lamhttp.HttpClient` — reusable defaults, typed options, and fluent requests

```lammergeier
from lamerrors import Result
from lamhttp import HttpClient, HttpCookie, HttpRequestOptions, HttpResponse

api: HttpClient = HttpClient("https://api.example.com", 5000)
api.setHeader("Authorization", "Bearer xyz")

res: HttpResponse = api.get("/users/42")?
if res.ok() {
    body: json = res.json()?
    requestId: str = res.header("X-Request-Id", "missing")
    allTraceValues: list[str] = res.headerValues("X-Trace")
    session: HttpCookie = res.cookie("session")
    if session != None { print(session.value) }
}

_ = api.post("/users", {"name": "alice"})?
_ = api.put("/users/42", {"name": "alice2"})?
_ = api.patch("/users/42", {"age": 31})?
_ = api.post("/events", "text/plain", "ready")?
_ = api.delete("/users/42")?

options: HttpRequestOptions = HttpRequestOptions().header("X-Request-Id", "abc").query("expand", "team")
requestResult: Result[HttpResponse] = api.request("GET", "/users/42", options)

fluentResult: Result[HttpResponse] = api.build("PATCH", "/users/42").timeout(2000).json({"active": true}).send()
```

`request(method, path, options)` returns `Result[HttpResponse]`, reporting
request creation, transport, timeout, and response-body read failures without
hiding them.
`build(method, path)` creates a fluent `HttpRequest` backed by that same path.
Per-request headers override client defaults; query values are URL-encoded and
sorted by Go's `url.Values.Encode`.

`get`, `head`, `delete`, `post`, `put`, and `patch` return
`Result[HttpResponse]`. The three body methods preserve both a native-`json`
overload that supplies `application/json` and an explicit
`(contentType, body: str)` overload. Absolute URLs (`http://`/`https://`) bypass
the configured base URL.

`HttpResponse` exposes `status`, `body`, and
`headers: dict[str, list[str]]`. `ok()` accepts only 2xx responses;
`json() -> Result[json]` decodes the body. Header lookups are case-insensitive:
`header(name, fallback="")` returns the first value and `headerValues(name)`
preserves every value.

`cookies()` parses each repeated `Set-Cookie` value in wire order;
`cookie(name)` returns the first exact-name match or `None`. `HttpCookie.parse`
is also available directly. `HttpCookie` preserves `name`, `value`, `path`,
`domain`, `expires`, `maxAge`, `secure`, `httpOnly`, `sameSite`,
`partitioned`, the original `raw` header, and unknown or malformed extension
attributes in `unparsed`.

### `lamtemplate` — text and HTML templating

Wraps Go's `text/template` + `html/template`:

```lammergeier
from lamtemplate import Template, HtmlTemplate
from lamerrors import Result

t: Template = Template()
_ = t.parse("Hello, {{.Name}}! {{len .Hobbies}} hobbies.")?
out: str = t.render({"Name": "Alice", "Hobbies": ["a", "b"]})?

# Browser output autoescapes and renders a native html value:
ht: HtmlTemplate = HtmlTemplate()
_ = ht.parse("<p>{{.Body}}</p>")?
page: html = ht.render({"Body": "<b>bold</b>"})?
print(page)   # <p>&lt;b&gt;bold&lt;/b&gt;</p>
```

Both template classes expose `parse`, `parseFile`, `parseFiles`, `render`, and
`renderNamed` as typed Result-returning methods; `Template` renders `str`,
`HtmlTemplate` renders `html`. For markup built in code, literals
(`<>...</>`) are usually simpler than templates. Shared helper map: `upper` /
`lower` / `title` / `trim` / `replace` / `join` / `default`.

### `lamretry.Retry` — exponential backoff with jitter

```lammergeier
from lamretry import Retry

# Fn that returns Result; Err triggers retry. Panics caught and
# treated as transient.
r: Result = Retry.runFn(myFn, 5, 100, 5000, true)

# Predicate polling:
ok: bool = Retry.until(predicate, 10, 50, 1000, true)
```

`fn` / `predicate` are functions without parameters of any result type
(`myFn() -> Result`, `-> str`, a lambda `lambda: fetch(url)`); a non-`Result`
value counts as `Ok(value)`, and `until` stops at the first `true`.

### `lamratelimit.TokenBucket` — client-side throttling

```lammergeier
from lamratelimit import TokenBucket

bucket: TokenBucket = TokenBucket(10, 5)   # capacity=10, refill=5/s
if bucket.takeAvailable(1) {
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
Result-returning `ref.tell(msg)` for fire-and-forget or
`ref.ask(msg, timeoutMs)` to wait for a reply. Unhandled panics in handlers are recovered (and
optionally observed via `onError`) so a buggy actor can't bring down
its neighbours.

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
- Data-structure classes are canonically imported from `lamdatastructures` and
  constructed: `Stack()`, `Queue()`, `Set()`, `Heap()`. Legacy module imports
  remain compatibility re-exports. Cache classes use `LruCache(100)` or
  `TtlCache()`.
- External Vulture plugins provide direct installers such as `requestId(srv)`
  and typed factories such as `requestIdPlugin()` for `srv.register(...)`.
- `Result`-returning methods use `Result.Ok(value)` /
  `Result.Err(error)`. The error payload is `any`.
- Methods returning `any` usually wrap dynamic Go values, decoded YAML/XML,
  or plugin state. Decoded JSON uses native `json`. Keep other values as `any`, pass them to
  another dynamic API, or assert with a small `go!` block.

| Module | Import | Public surface |
|---|---|---|
| `lamactor` | `from lamactor import Mailbox, ActorRef, ActorSystem` | Actor mailboxes, references, spawn/find/shutdown. |
| `lamarray` | `from lamarray import Array, Matrix` | Generic numeric inputs, float64-backed arrays/matrices, linear algebra, and reductions. |
| `lambase64` | `from lambase64 import Base16, Base32, Base64, Hex` | Base16/Base32/Base64 standard, raw, URL-safe, and extended-hex codecs. |
| `lambytes` | `from lambytes import Bytes` | String/byte conversion, hex encode/decode, byte search/count. |
| `lamcache` | `from lamcache import LruCache, TtlCache` | Bounded LRU cache and expiry-based TTL cache. |
| `lamcli` | `from lamcli import Cli` | Args, flags, positional arguments, and overloaded string/integer flag values. |
| `lamcompress` | `from lamcompress import Compress` | gzip/gunzip and zlib deflate/inflate as base64 strings. |
| `lamconcurrency` | `from lamconcurrency import Channel, WaitGroup, Mutex, RWMutex, Atomic` | Go-style concurrency primitives. |
| `lamcontext` | `from lamcontext import Context` | Cancellation trees, unit-aware expiration, values, bounded waits, and Go context interop. |
| `lamconv` | `from lamconv import Conv` | Result-based int/float/bool, finite-float and explicitly named radix string parsers. |
| `lamcrypto` | `from lamcrypto import Crypto, CryptoKeyPair, CryptoCertificate` | Secure random data, AES-GCM, password hashes, PEM keys/signatures, and X.509 metadata. |
| `lamcron` | `from lamcron import Cron` | Cron scheduler, seconds-aware mode, start/stop/remove. |
| `lamcsv` | `from lamcsv import Csv, CsvReader` | Parse/format CSV with any delimiter plus lazy row/batch file streaming. |
| `lamdata` | `from lamdata import DataFrame, DataFrameGroups, Series` | DataFrames and Series with overloaded typed columns, native JSON, joins, filters, and group-by aggregation. |
| `lamdatastructures` | `from lamdatastructures import Queue, Deque, Stack, Set, Heap, PriorityHeap` | Canonical data-structure implementations. |
| `lamdatetime` | `from lamdatetime import DateTime` | Timezone-aware instants, parsing (one layout, the first of several, common and database forms)/formatting, calendar and duration arithmetic. |
| `lamdb` | `from lamdb import Db, DbConn, Tx, QueryBuilder, Row, Where` | SQL connections, the `DbConn` interface shared by `Db` and `Tx`, typed and nested transactions (`Db.atomic`), raw queries, query builder, typed row values, SQLSTATE checks, WHERE builder. |
| `lamdeque` | `from lamdeque import Deque` | Compatibility re-export of canonical `Deque`. |
| `lamenv` | `from lamenv import Env, Dotenv, Config` | Overloaded typed/Result environment access, lenient `flag` switches and `list` values, dotenv parsing/loading, and layered config. |
| `lamerrors` | `from lamerrors import Error, Result` | Structured errors and success/error containers. |
| `lamexec` | `from lamexec import Exec, ExecCommand, ExecResult` | Typed process execution with env/cwd/stdin, timeout, cancellation, and captured output. |
| `lamfmt` | `from lamfmt import Fmt` | `sprintf`, printing, padding, integer bases, float formatting, thousands grouping. |
| `lamhash` | `from lamhash import Hash` | SHA/MD5/CRC/HMAC and constant-time comparison. |
| `lamheap` | `from lamheap import Heap, PriorityHeap` | Compatibility re-export of numeric heaps from `lamdatastructures`. |
| `lamhtml` | `from lamhtml import Html, HtmlPolicy` | Native `html`: lenient parsing, escaping, allowlist sanitizing. |
| `lamhttp` | `from lamhttp import Http, HttpClient, HttpRequest, HttpRequestOptions, HttpResponse, HttpCookie, HttpServer, Multipart` | Typed request options, fluent Result-returning requests, multipart bodies, repeated response headers, cookies, reusable client, tiny blocking server. |
| `lami18n` | `from lami18n import Translations` | Translated texts from `<dir>/<lang>/*.json` (nested keys, duplicate/malformed-file errors), fallback language, `{name}` parameters as text or escaped markup, completeness checks, `Accept-Language` negotiation. |
| `lamiter` | `from lamiter import Iter` | Lazy iterator pipelines: map/filter/take/drop/reduce/enumerate. |
| `lamjson` | `from lamjson import Json` | Native `json`, wire encoding/decoding, safe Result-based collection conversion, kind inspection, typed member reads, key renaming. |
| `lamlog` | `from lamlog import Log` | Leveled logging and formatted log helpers. |
| `lammarkup` | `from lammarkup import Markup` | Runtime of the native `html` / `xml` types (imported automatically) and `Markup.escape`. |
| `lammath` | `from lammath import Math` | Constants, trig, logs, rounding, combinatorics, number theory. |
| `lammigrate` | `from lammigrate import Migrator, SchemaBuilder` | SQL migrations and schema helpers. |
| `lamnet` | `from lamnet import Net, TcpConn, TcpListener, UdpConn, UdpPacket` | DNS, reachability, TCP/TLS/UDP sockets, deadlines, and addresses. |
| `lamos` | `from lamos import Os, File, FileLineGenerator` | Result-based filesystem/process helpers and streaming file lines/batches. |
| `lampath` | `from lampath import Path` | Variadic path joining plus splitting/ext/base/dir/existence helpers. |
| `lamqueue` | `from lamqueue import Queue, Deque` | Compatibility re-export of FIFO and double-ended queues. |
| `lamrandom` | `from lamrandom import Random` | Pseudo-random values and secure random bytes/tokens; UUIDs live in `lamuuid`. |
| `lamratelimit` | `from lamratelimit import TokenBucket` | Thread-safe token bucket throttling. |
| `lamre` | `from lamre import Re` | Result-returning regex match/find/findAll/replace/split/groups. |
| `lamretry` | `from lamretry import Retry` | Retry `Result`-returning calls and poll predicates. |
| `lamschema` | `from lamschema import Schema` | JSON Schema validation and registry. |
| `lamset` | `from lamset import Set` | Compatibility re-export of canonical insertion-ordered `Set`. |
| `lamsmtp` | `from lamsmtp import Smtp, Mail` | SMTP send helpers and MIME mail builder. |
| `lamsort` | `from lamsort import Sort` | Generic natural/comparator sorting, direction checks, named algorithms, and lazy sorted generators. |
| `lamstack` | `from lamstack import Stack` | Compatibility re-export of canonical LIFO `Stack`. |
| `lamstats` | `from lamstats import Stats` | Generic numeric/ordered descriptive statistics and percentiles. |
| `lamstrings` | `from lamstrings import Strings` | UTF-8 string manipulation, case conversion and formatting helpers. |
| `lamtemplate` | `from lamtemplate import Template, HtmlTemplate` | Text and HTML templates. |
| `lamtest` | `from lamtest import Test` | Assertions, counters, summaries. |
| `lamtime` | `from lamtime import Time` | Nanosecond durations, unit factories/arithmetic, Unix time, sleeps, and measurement. |
| `lamunicode` | `from lamunicode import Unicode` | Rune counts, validity, categories, rune conversion. |
| `lamurl` | `from lamurl import Url` | URL parse parts, percent encode/decode per URL part, query string build/parse. |
| `lamuuid` | `from lamuuid import Uuid` | UUID v4/v7/nil/parse/validate. |
| `lamxml` | `from lamxml import Xml` | Native `xml` parsing (well-formed check), escaping, dict/list encoding and shape-shifting decode. |
| `lamyaml` | `from lamyaml import Yaml` | Result-returning YAML encode/decode/decodeAll. |

### Official external packages used in this guide

These independently versioned packages are **not** standard-library modules:

| Package | Import | Public surface |
|---|---|---|
| `@lam/vulture` | `from @lam/vulture import Server, Request, Response, RouteOptions, InjectResponse, HttpError, SseEmitter, RequestBodyReader` | Fastify-style HTTP framework. |
| `@lam/vulture/std-plugin` | `from @lam/vulture/std-plugin import requestId, requestIdPlugin, rateLimit, rateLimitPlugin, helmet, CircuitBreaker, ...` | Direct plugin installers, `func(Server)` factories, and a circuit breaker. |
| `@lam/vulture/tus-plugin` | `from @lam/vulture/tus-plugin import tusUploads, tusGc` | Result-returning tus.io route registration and garbage collection. |
| `@lam/vulture/ws-plugin` | `from @lam/vulture/ws-plugin import WebSocket, wsRoute, wsUpgrade` | Result-returning WebSocket routes, upgrades, and frame I/O. |
| `@lam/vulture/dev-plugin` | `from @lam/vulture/dev-plugin import LiveReload, liveReload, liveReloadPlugin` | Browser live reload for `lamc dev`. |

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

# @lam/vulture (official external package, not stdlib)
RouteOptions(bodyLimit: int = 0, timeoutMs: int = 0, streamBody: bool = false)
opts.withSchema(schema: any, format: str = "json") -> RouteOptions
opts.preHandler(fn: func(Request, Response)) -> RouteOptions
opts.metadata(name: str, value: any) -> RouteOptions
srv.route(method: str, path: str, handler: func(Request, Response))
srv.route(method: str, path: str, handler: func(Request, Response), opts: RouteOptions)
srv.route(method: str, path: str, handler: func(Request, Response), schema: str | json, schemaFormat: str)
srv.get/post/put/del/patch/head/options/all(path: str, handler: func(Request, Response))
srv.get/post/put/del/patch/head/options/all(path: str, handler: func(Request, Response), opts: RouteOptions)
srv.get/post/put/del/patch/head/options/all(path: str, handler: func(Request, Response), schema: str | json, schemaFormat: str)
srv.onRequest/preParsing/preValidation/preHandler/preSerialization/onSend/onResponse(fn: func(Request, Response))
srv.onError(fn: func(Request, Response, any))
srv.register(plugin: func(Server), prefix: str = "", encapsulate: bool = false)
srv.setSchemaErrorFormatter(fn: func(str, list[str], Request) -> any)
srv.inject(method: str, path: str, body: str = "", headers: dict[str, str] = {}) -> InjectResponse
srv.listen/listenBackground(port: int) -> Result[bool]
req.header(name: str) -> str
req.queryGet(name: str, fallback: str = "") -> str
req.cookie(name: str) / signedCookie(name: str, secret: str) -> Result[str]
req.jsonBody() -> Result[json]
req.parsedBody() -> Result[any]
req.ctx[key] / req.ctx.has(key) / key in req.ctx
req.ctx.cancel(reason) / expired() / reason() / wait(timeoutMs)
req.bodyReader() -> RequestBodyReader
reader.read(maxBytes: int = 8192) / readAll() -> Result[str]
reader.copyTo(path: str) -> Result[int]
reader.close() -> Result[bool]
res.code(n: int) -> Response
res.header(name: str, value: str) -> Response
res.json(obj: json) -> Response
res.text(body: str) -> Response
res.streamFile(path: str, contentType: str = "") -> Response

# lamdb
Db.connect(driver: str, dsn: str) -> Result[Db]
db.setObserver(observer: DbObserver)
db.withContext(ctx: Context) -> Db
DbObserver.start(ctx: Context, statement: str) -> any
DbObserver.finish(ctx: Context, token: any, failure: str)
db.exec(query: str, params: list[any] = []) -> Result[int]
db.query(query: str, params: list[any] = []) -> Result[list[dict[str, any]]]
db.first(query: str, params: list[any] = []) -> Result[dict[str, any]]
db.scalar(query: str, params: list[any] = []) -> Result[any]
db.transaction(fn: any) -> Result[bool]
db.begin() -> Result[Tx]
db.pool() -> Db
Db.atomic[T](conn: DbConn, fn: func(Tx) -> Result[T]) -> Result[T]
DbConn: exec / query / first / scalar / queryJson / firstJson / scalarJson / table / transaction / pool (Db and Tx)
tx.exec(query: str, params: list[any] = []) -> Result[int]
tx.query(query: str, params: list[any] = []) -> Result[list[dict[str, any]]]
tx.first(query: str, params: list[any] = []) -> Result[dict[str, any]]
tx.scalar(query: str, params: list[any] = []) -> Result[any]
tx.queryJson(query: str, jsonColumns: list[str], params: list[any] = []) -> Result[list[dict[str, any]]]
tx.firstJson(query: str, jsonColumns: list[str], params: list[any] = []) -> Result[dict[str, any]]
tx.scalarJson(query: str, params: list[any] = []) -> Result[json]
tx.table(name: str) -> QueryBuilder
tx.savepoint(name: str) / tx.rollbackTo(name: str) / tx.releaseSavepoint(name: str) -> Result[bool]
tx.commit() / tx.rollback() -> Result[bool]
tx.transaction(fn: any) -> Result[bool]
tx.depth() -> int
tx.pool() -> Db
db.table(name: str) -> QueryBuilder
qb.select(cols: list[any]) -> QueryBuilder
qb.where(col: str, op: str, val: any) -> QueryBuilder
qb.whereEq(col: str, val: any) -> QueryBuilder
qb.whereGroup(callback: func(QueryBuilder) -> QueryBuilder) -> QueryBuilder
qb.orWhereGroup(callback: func(QueryBuilder) -> QueryBuilder) -> QueryBuilder
qb.orderBy(col: str, dir_: str = "asc") -> QueryBuilder
qb.get() -> Result[list[dict[str, any]]]
qb.first() -> Result[dict[str, any]]
qb.insert(record: dict[str, any]) -> Result[int]
qb.update(record: dict[str, any]) -> Result[int]
qb.delete() -> Result[int]
Row.int(row: dict[str, any], key: str, fallback: int = 0) -> int
Row.float(row: dict[str, any], key: str, fallback: float = 0.0) -> float
Row.str(row: dict[str, any], key: str, fallback: str = "") -> str
Row.bool(row: dict[str, any], key: str, fallback: bool = false) -> bool
Row.unix(row: dict[str, any], key: str, fallback: int = 0) -> int
Row.time(row: dict[str, any], key: str) -> optional[DateTime]
Row.optionalStr/optionalInt/optionalFloat/optionalBool(row: dict[str, any], key: str) -> optional[T]
Row.isNull(row: dict[str, any], key: str) -> bool
Row.strList(row: dict[str, any], key: str) -> list[str]
Row.intList(row: dict[str, any], key: str) -> list[int]
Db.textArray(values: list[str]) -> str
Db.parseTextArray(text: str) -> Result[list[str]]
Row.timeText(row: dict[str, any], key: str, layout: str = "2006-01-02T15:04:05Z07:00", location: str = "UTC", fallback: str = "") -> str
Db.sqlState(err: any) -> str
Db.isUniqueViolation(err: any) -> bool
Db.isForeignKeyViolation(err: any) -> bool
Where() -> Where
w.add(clause: str, params: list[any] = []) -> Where
w.addIf(condition: bool, clause: str, params: list[any] = []) -> Where
w.addIn(column: str, values: list[any]) -> Where
w.ilikeAny(columns: list[str], text: str, match: str = "contains") -> Where
w.likeAny(columns: list[str], text: str, match: str = "contains") -> Where
w.sql(prefix: str = "WHERE") -> str
w.params() -> list[any]
w.paramsWith(extra: list[any]) -> list[any]
w.isEmpty() -> bool
Where.likePattern(text: str, match: str = "contains", escape: str = "\\") -> str

# lamjson (typed reads, key renaming)
Json.has(doc: json, key: str) -> bool
Json.getStr(doc: json, key: str, fallback: str = "") -> str
Json.getInt(doc: json, key: str, fallback: int = 0) -> int
Json.getFloat(doc: json, key: str, fallback: float = 0.0) -> float
Json.getBool(doc: json, key: str, fallback: bool = false) -> bool
Json.getList(doc: json, key: str) -> list[json]
Json.getObject(doc: json, key: str) -> json
Json.at(doc: json, path: list[any]) -> json
Json.mapKeys(doc: json, rename: func(str) -> str, deep: bool = true) -> json
Json.copy(doc: json) -> json

# lamstrings (also str methods: s.camelCase())
Strings.camelCase(s: str) -> str
Strings.pascalCase(s: str) -> str
Strings.snakeCase(s: str) -> str
Strings.kebabCase(s: str) -> str

# lamurl
Url.encodeComponent(s: str) -> str
Url.decodeComponent(s: str) -> Result[str]
Url.encodePath(s: str) -> str
Url.encodePathSegment(s: str) -> str
Url.encodeUserInfo(s: str) -> str
Url.query(params: dict[str, list[str]]) -> str
Url.query(params: dict[str, str]) -> str
Url.query(pairs: list[list[str]]) -> str
Url.parseQuery(s: str) -> Result[dict[str, list[str]]]

# lamconv / lamfmt / lamdatetime / lamcsv
Conv.parseFinite(s: str) -> Result[float]
Fmt.grouped(value: int, separator: str = ",", decimals: int = 0, decimalSeparator: str = ".") -> str
Fmt.grouped(value: float, separator: str = ",", decimals: int = -1, decimalSeparator: str = ".") -> str
DateTime.parseAny(value: str, layouts: list[str], location: str = "UTC") -> Result[DateTime]
Csv.parseAll(content: str, delimiter: str = ",") -> Result[list[list[str]]]
Csv.formatRow(fields: list[str], delimiter: str = ",") -> str
Csv.formatAll(rows: list[list[str]], delimiter: str = ",") -> str

# lamhttp (multipart)
Multipart() -> Multipart
form.field(name: str, value: str) -> Multipart
form.file(name: str, filename: str, contentType: str, data: bytes) -> Multipart
form.setBoundary(boundary: str) -> Multipart
form.body() -> bytes
form.contentType() -> str
options.multipart(form: Multipart) -> HttpRequestOptions
request.multipart(form: Multipart) -> HttpRequest

# lamhtml (sanitizing)
Html.sanitize(markup: str | html) -> html
Html.sanitize(markup: str | html, policy: HtmlPolicy) -> html
HtmlPolicy() / HtmlPolicy.basic() / HtmlPolicy.rich() -> HtmlPolicy
policy.allowTags(tags: list[str]) / denyTags(tags: list[str]) -> HtmlPolicy
policy.allowAttributes(tag: str, attributes: list[str]) -> HtmlPolicy
policy.allowSchemes(schemes: list[str]) -> HtmlPolicy
policy.allowRelativeUrls(allowed: bool = true) / allowDataImages(allowed: bool = true) -> HtmlPolicy
policy.allowStyles(properties: list[str]) -> HtmlPolicy
policy.allowFrames(hosts: list[str]) -> HtmlPolicy
HtmlPolicy.videoHosts() -> list[str]

# lamcrypto (bcrypt)
Crypto.bcryptHash(password: str, cost: int = 10) -> Result[str]
Crypto.bcryptVerify(password: str, encoded: str) -> Result[bool]
Crypto.bcryptCost(encoded: str) -> Result[int]

# lamenv
Env.flag(key: str, fallback: bool) -> bool
Env.list(key: str, separator: str = ",") -> list[str]

# lami18n
Translations(languages: list[str], fallback: str = "")
Translations.load(dir: str, languages: list[str], fallback: str = "") -> Result[Translations]
t.add(lang: str, key: str, text: str) -> Result[bool]
t.merge(lang: str, texts: dict[str, str]) -> Result[int]
t.raw(lang: str, key: str) -> str
t.text(lang: str, key: str, params: dict[str, str] = {}) -> str
t.markup(lang: str, key: str, params: dict[str, str] = {}) -> html
t.plain(lang: str, key: str, params: dict[str, str] = {}) -> str
t.has(lang: str, key: str) -> bool
t.count(lang: str = "") -> int
t.keys(lang: str) -> list[str]
t.missing(lang: str, other: str) -> list[str]
t.languages() -> list[str]
t.fallback() -> str
t.language(candidate: str) -> str
t.negotiate(header: str) -> str

# lamos / lamcontext (signals)
Os.waitForSignal(names: list[str] = ["SIGINT", "SIGTERM"]) -> str
Context.onSignals(names: list[str] = ["SIGINT", "SIGTERM"]) -> Context

# lamdata
DataFrame.readCSV(content: str) -> DataFrame
DataFrame.readJSON(content: str) -> DataFrame
DataFrame.readJSON(content: json) -> DataFrame
df.selectCols(cols: list[str]) -> DataFrame
df.filter(col: str, op: str, value: any) -> DataFrame
df.groupBy(cols: list[str]) -> DataFrameGroups
df.writeCSV() -> Result[str]
df.writeJSON() -> Result[str]
groups.aggregate(types: list[str], cols: list[str]) -> DataFrame
series.mean() -> float
series.toIntList() -> Result[list[int]]
series.toBoolList() -> Result[list[bool]]
series.toFloatList() -> list[float]
```
