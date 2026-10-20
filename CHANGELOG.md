# Changelog

All notable changes to Lammergeier Lang are documented here.

Releases are tagged with plain versions (`2.0.0`); older entries are grouped
by commit date.

## 3.0.0 — 2026-10-20

### Breaking changes

- **Division follows Python.** `/` is always a float division
  (`7 / 2 == 3.5`, `4 / 2 == 2.0`, `int / int` is a `float`), `//` is floor
  division (`-7 // 2 == -4`, `-7.5 // 2 == -4.0`) and `%` takes the sign of
  the divisor (`-7 % 2 == 1`, `7 % -2 == -1`, floats included: `-7.5 % 2 ==
  0.5`). Before, `/` and `//` of two ints both truncated toward zero, `%`
  kept the dividend's sign, `%` on floats failed in `go build`, and
  `y: float = n / 2` with `n: int` passed `lamc` and failed in `go build`.
  `/=` on an `int` variable is now a type error (use `//=`).
- **Every zero divisor raises `ZeroDivisionError`:** int `//` / `%`, float
  `/` / `//` / `%` and `/` of ints (a float division by zero used to give
  `+Inf`). The message follows Python (`ZeroDivisionError: division by
  zero`, `ZeroDivisionError: integer division or modulo by zero`,
  `ZeroDivisionError: float modulo`); an int division by zero used to print
  `runtime error: integer divide by zero`.
- **Numeric fields are typed in expressions** (`self.cents`, `order.total`
  with a declared `int` / `float` field), so `return self.cents / 100` in an
  `-> int` method is a compile-time error with the `//` hint instead of a
  Go build error.
- Standard library: `Duration.inMilliseconds()` / `inSeconds()` /
  `inMinutes()` / `inHours()` / `inDays()` and `DateTime.unix()` /
  `unixMilli()` floor negative values (they truncated toward zero).
- Official packages: `@lam/sftp` 3.4.0 and `@lam/vulture` 2.0.2 (and the
  plugins pinning it) used `/` for integer division and do not build with
  3.0; use `@lam/sftp` 3.5.0, `@lam/vulture` 2.0.3 and the plugin releases
  pinning it (see "Changed"), which declare `lamc >=1.18, <4`.

**Migrating:** replace every `/` whose result must stay an `int` with `//`
(indexes, counters, `range(...)` bounds, pagination, unit conversions,
`len(xs) // 2`). `//` already compiles on 2.x, where it truncated like `/`,
so code using it for non-negative operands builds and behaves the same with
2.1 and 3.0. Keep `/` where a fraction is wanted and give the target a
`float` type. `lamc` reports the leftover cases: `x: int = a / b`, returning
or passing `a / b` where an `int` is expected and indexing with it fail with
``cannot assign `float` to `int` variable `x` (`/` always gives a `float`;
use `//` for integer division)``, also inside imported modules and in
`Result.Ok(a / b)` for a `Result[int]`. Review `//` and `%` on values that
may be negative: they now floor (`-7 // 2 == -4`) instead of truncating.

### Changed

- Official libraries: `third_party/lamstripe` and `third_party/lamotel` moved
  to their own repositories as `@lam/stripe` 1.0.0 and `@lam/otel` 1.0.0
  (`from @lam/stripe import ...`, `from @lam/otel import ...`; typed Results,
  see each CHANGELOG), and `third_party/` is gone: the compiler repository
  vendors no package. New official libraries `@lam/keyring`, `@lam/totp` and
  `@lam/jobs` 1.0.0. Catalog: every pre-3.0 library has a release that
  declares `lamc >=1.18, <4` (`@lam/jwt` 3.1.2, `@lam/memcached` 2.0.2,
  `@lam/protobuf` 3.0.2, `@lam/redis` 2.0.2, `@lam/s3` 2.3.1, `@lam/ssh` 3.0.3,
  `@lam/sftp` 3.5.0 with PROXY protocol support, `@lam/vulture` 2.0.3 and its
  plugins `std-plugin` 2.0.4, `tus-plugin` 3.0.3, `ws-plugin` 2.0.3).

- An exception raised in an async task that is never awaited is printed to
  stderr when `main` returns (`Task exception was never retrieved:
  ValueError: ...`, like Python's asyncio) instead of disappearing; the exit
  status is unchanged and an awaited exception is still re-raised at the
  `await` only.
- `x()?.y()` on a `Result` means `(x()?).y()` (propagation, then member
  access); it did not compile before. `?.` on an optional value keeps its
  safe-navigation meaning.
- `abs(n)`, `min(...)` and `max(...)` nested in each other stay ints for
  int arguments, an int widens to a float wherever a float is expected, and
  inline list comprehensions are typed by their element: code relying on
  the previous `[]interface{}` shape in `go!` blocks may need an update.
- `lamc fmt` leaves raw Go in `go! { }` blocks byte for byte as written; it
  used to run gofmt on them.

### Fixed

- `lamc fmt` corrupted code: gofmt's doc-comment rewriting turned
  double-backtick spans in `//` comments of `go!` blocks into typographic
  quotes, the elements of multi-line list / dict literals and call
  arguments lost their indentation, slices became `s[: 3]` / `x[0: 7]`,
  `take({...})` became `take( {...})`, and `func match(...)` gained one
  space before `(` per run. Formatting is idempotent and keeps the generated
  Go unchanged, checked over the test corpus, the standard library and the
  official libraries.
- `await` on a value typed `any` (an element of a `list[any]`) failed in
  `go build` (`cannot receive from non-channel interface{}`); it is a Lam
  error pointing at the untyped handle.
- **Wrong results: a parenthesized right operand lost its parentheses**
  (since at least 1.19.9). `a - (b + c)` compiled to Go `a - b + c`, so
  with `a = 10, b = 2, c = 3` it printed `11` instead of `5`; the same
  happened to `x - (y - z)` on floats, to a unary operator on a group
  (`-(b + c)` gave `-b + c`, also `a - -(b + c)`, `~(b * c)`), to a shift on
  the right of `*`, `<<`, `>>` or `&` (`a * (b >> c)`, `a << (b << c)`, and
  `a & b << c`, which Python groups as `a & (b << c)`), to `a * (b // c)` /
  `a * (b % c)` where Go's own operator is used (non-negative operands), and,
  in 2.x, to `a // (b * c)` (`15` for `10 // (2 * 3)`) and `100 % (7 * 3)`
  (`6`). Programs built with an earlier `lamc` computed
  wrong values for such expressions without any error; rebuild them. The
  standard library had no affected expression. Every operator now keeps the
  grouping of the source, checked against Python's evaluation of thousands
  of generated expressions over every pair of operators.
- `a + b ** c`, `c < a ** b` and `n < x / 2` (an `int` mixed with the
  `float` of `**` or `/`) and `x / (2 << n)` passed `lamc` and failed in
  `go build` (`mismatched types int and float64`, `shifted operand 2 (type
  float64) must be integer`): the `int` operand is converted.
- A `try` / `catch` inside a `do { } catch err { }` body failed in
  `go build` (`not enough return values`), and a `?` inside a `try` body
  or `catch` handler (in a `do` body or a `-> Result` function) failed with
  `too many return values`. The `?` error now leaves the `try` and is
  propagated to the `do` handler or returned by the function (a `catch`
  clause never sees it).
- `a: str = (m.make()?).key` (a member, method call, index or call argument
  around a `?`) failed in `go build` (`__q1.Value.(string).Key undefined`):
  the `?` payload was asserted to the declared type of the target instead
  of the `Result`'s payload type.
- `f()?.method()` / `load()?.field` on a `Result` was read as safe
  navigation on the `Result` and failed in `go build` (`Next undefined
  (type *Result has no field or method Next)`). On a `Result` receiver it
  is now propagation followed by the member access (`(f()?).method()`), so
  `b: Box = load(1)?.next()?.next()?` chains fallible calls; `?.` on an
  optional value is still safe navigation. `r?` on a local declared
  `Result[T]` is also typed by `T` now.
- A `?` inside a list, dict or set comprehension (`[half(n)? for n in xs]`,
  also in the filter) was hoisted out of the loop and failed in `go build`
  (`undefined: n`). It runs once per element inside the loop now, and an
  `Err` stops the comprehension and propagates like any `?`.
- `==` / `!=` on lists, dicts and sets (`xs == ["a", "b"]`, `d["k"] ==
  ["a"]`, two `list[str]` variables, `counts == {"a": 1}`) passed `lamc`
  and failed in `go build` (`mismatched types []string and []interface{}`,
  `slice can only be compared to nil`). They compare element by element,
  typed by the element type (an element class's `__eq__` is used).
- A lambda declared `-> Result[list[Item]]` returning `Result.Ok([Item("a")])`
  built a `[]interface{}` payload and the caller's `?` / `unwrap()` panicked
  (`interface {} is []interface {}, not []*main.Item`): the payload takes
  the declared type, like in a top-level function. `?`, `unwrap()` and
  `unwrapOr()` with a `list[T]` / `dict[str, T]` payload also convert an
  untyped list / dict built elsewhere (an unannotated lambda, a `Result`
  built in an `any` context), element by element.
- `m: int = max(abs(a), 2)` with `a: int` failed in `go build` (`value of
  type float64`): a nested `abs` / `min` / `max` call was not typed, so the
  outer call compared floats. Ints stay ints at any nesting depth.
- A local named like a Go package was treated as the package:
  `http: HttpRequestOptions = ...; http.header("A", "b")` and `log: Logger =
  Logger(); log.info("x")` failed in `go build` (`http.header undefined`),
  and `strings: Box` (or `fmt`, `sort`, `math`, ...) hid the package from the
  generated code of the same function. Locals shadow packages now; locals
  spelled like a package the generated code relies on are renamed in Go
  (`strings__lam`), also inside `go!` blocks of their scope.
- Symbols imported through a package `__init__.lam` re-export (`from
  .errs import DemoError`, also aliased or chained through nested packages,
  and with `import pkg`) lost their type information in the semantic
  checker: `case DemoError() as d` failed with "`DemoError` is not a class",
  `b: json = mk()` and `json(Point(3))` (a `__json__` class) failed with
  "value of type `unknown` is not JSON-compatible", and `s: str =
  p.count()` passed `lamc` and failed in `go build`. Re-exported functions,
  classes (fields, methods, constructors, bases) and interfaces now carry
  the same types as direct imports, and the classes of the defining module
  are known as with a direct import. A `range(...)` / `enumerate(...)` loop
  variable is an `int` for the checker (it was `unknown`, so `{"i": i}`
  passed to a `json` parameter of such a method was reported as not
  JSON-compatible).
- An inline list comprehension used as an operand lost its element type:
  `",".join([w.toUpper() for w in words][1:])` and `[v for k, v in
  d].reduce(...)` failed in `go build` (`[]interface{}` / `need type
  assertion`). Outside a typed context a comprehension over a typed list,
  string, dict, `range(...)` or `enumerate(...)` is typed by its element.
- A `str` method called with the wrong number of arguments
  (`"a".replace("x", "", 1)`, `s.split(",", 2)`) passed `lamc` and failed in
  `go build` (`too many arguments in call to Strings_replace`). It is a Lam
  error (``error[call]: `str.replace()` takes 2 arguments, got 3``, with the
  Lam alternative: `replaceFirst`, `splitN`, `fields`), also in imported
  modules.
- A class-typed field read through a loop variable (`for it in items {
  use(it.res) }`, `d["k"] = it.res`, also over a method's result `for it in
  bag.all()`) failed in `go build` (`it.Res (variable of type *Res) is not
  an interface`): the loop variable's class was not known, so the field was
  asserted as if it were `any`.
- Assigning `{}`, `[]` or another literal to a field declared by a base
  class (`self.points = {}` in a subclass method with `dict[str, int]` in
  the base) failed in `go build` (`cannot use map[string]interface{}{} as
  map[string]int value`): inherited fields are typed like the class's own.
- Overload resolution fell back to the first variant of the right arity when
  no variant matched the argument types exactly: `t.fail(2.5, {"a": 1})`
  with `fail(message: str, attrs: dict[str, any])` and `fail(code: float,
  attrs: dict[str, any])` bound the `str` variant and failed in `go build`,
  and so did an int passed to the `float` variant. The variant every
  argument fits (a collection literal to `any` elements, an int to a
  `float`, anything to `any`) is chosen, the closest match first; a float
  literal now selects a `float` variant over an `int` one (`pq.push(3.0,
  ...)` in `lamheap`).
- An `any` local assigned a class instance and later something else
  (`last = b` with a `Box`, then `last = r.error` in another branch of a
  loop) panicked (`interface {} is string, not *main.Box`): the first
  assignment retyped the variable as `Box` and the second was asserted to
  `*Box`. A variable declared `any` stays `any`.
- A nested `func` inside a class method or constructor was emitted as a Go
  method declaration inside the function body (`syntax error: unexpected
  name int`), and the checker warned that the "method" used `self` without
  declaring it. It is a local closure, like a nested `func` in a top-level
  function, with access to the method's `self` and locals; passing it to
  `xs.map(f)` keeps its return type.
- A lambda passed to a generic class constructor (`Queue[str](lambda items:
  len(items))` with a `func(list[T]) -> int` parameter) failed in `go build`
  (`undefined: T`): the parameter types now take the explicit type
  arguments.
- Reading an integral JSON number as a `float` (`x: float = doc.sum` with
  `"sum": 30`, also as a `float` argument or return value) panicked
  (`interface {} is int, not float64`); the number widens to a float.
- An `int` passed where a `float` is expected (`half(n)` with `v: float`,
  `return n + 1` in a `-> float` function, `[1, n]` in a `list[float]`,
  `f: float = n`, `self.x = n + 1`, `total += n`, a `func(float)` value, an
  interface method, a lambda or generator declared `float`) passed `lamc`
  and failed in `go build` (`cannot use n (variable of type int) as float64
  value`). The int widens implicitly, as in Python.
- `+` on two lists (`out + firsts`, `xs + [1, 2]`, in expressions, returns,
  loops, comprehensions and f-strings) passed `lamc` and failed in `go build`
  (`operator + not defined on out`): it builds a new list typed by the
  element type and leaves both operands unchanged (mixed element types give
  a `list[any]`), and `xs += ys` appends the items of `ys`.
- An inline list / dict / set literal passed to a function value
  (`size(["a"])` with `size: func(list[str]) -> int`, a `func(...)`
  parameter, a typed lambda in a local) failed in `go build` (`cannot use
  []interface{}{…} as []string value`): the literal takes the declared
  parameter type.
- Calling an interface method without an argument that has a default value
  (`n.name()` with `interface Named { func name(suffix: str = "") -> str }`)
  failed in `go build` (`not enough arguments in call to n.Name`), and a
  keyword argument (`n.name(suffix="!")`) crashed `lamc` with a Python
  traceback. Calls through the interface fill its declared defaults and
  reorder keyword arguments, through parameters, annotated locals, fields,
  loop / comprehension variables and lambda parameters, also when the
  interface and the implementing class live in other modules.

## 2.1.1 — 2026-10-19

### Changed

- Catalog: `@lam/s3` 2.3.0 (multipart parts streamed from files and readers).

### Fixed

- A method a `str` does not have (`label.substring(0, 2)`) passed `lamc`
  and failed in `go build` (`label.Substring undefined`) when it was written
  in an imported module, on a `str` field (`self.name.substring(...)`) or on
  a typed lambda parameter: `lamc build` runs the semantic checker on the
  compiled file only. The transpiler now reports the same Lam diagnostic for
  every module, and the message names the Lam spelling with the call's own
  operands: slicing for `substring` / `substr` / `slice`
  (``use slicing `label[0:2]` ``), indexing for `charAt` / `at`, `+` / `==`
  for `concat` / `equals`, and `index` / `toUpper` / `contains` / ... for
  JavaScript / Java names such as `indexOf`, `toUpperCase`, `includes`.
- `Result.Ok(x)` returned from a function (or lambda) declaring `Result[T]`,
  or assigned to a variable annotated `Result[T]`, accepted any `x`:
  `return Result.Ok(Json.encode(v))` in a `-> Result[str]` panicked when the
  caller unwrapped it (`interface {} is *main.Result, not string`). An `x`
  that does not fit `T` is a semantic error; for a fitting inner `Result` it
  suggests ``Json.encode(v)?`` or returning it directly (also reported in
  imported modules). `Result[any]`, a bare `Result`, `Result[Result[T]]` and
  generic payloads are unchanged. Typed lambda parameters now give the lambda
  body their declared type.
- Raw Go in `go!(...)` expressions and `go! { }` blocks calling a module
  function by its Lam spelling (`go!(helper(options("a"), Origin))`) failed
  with `undefined: options`: the call now lowers to the function's Go name
  (`Options`, or its module-qualified name), also for functions imported
  without `as`. A Lam local or a Go declaration of the same name in the same
  function or in a package-level `go!` block of the module keeps its Go
  meaning.
- The type of a loop, comprehension or local variable leaked into a
  same-named variable of another function or method of the module (and
  past a comprehension or loop in its own function): `c` in
  `for c in "x, y".split(",")` kept the `list[str]` type of `c` in
  `[c for c in Pairs]` in another method, and `c.trim()` failed in
  `go build` (`c.Trim undefined`). Those types now end with their function,
  comprehension or loop.
- `[[c for c in row] for row in rows]` was rejected with "undefined name
  `row`", and `[x for _ in xs]` failed in `go build` (`no new variables on
  left side of :=`).
- `unwrap()` on `Result.Ok(1)` returned from a `-> Result[float]` function
  panicked (`interface {} is int, not float64`), and on `Result.Ok(Circle())`
  in a `Result[Shape]` (`*main.Circle, not *main.Shape`). The payload is
  converted where the Ok is built for a declared `Result[T]` (int to float,
  subclass to the base-class view, with overrides still dispatching), and
  `unwrap()`, `unwrapOr(...)` and `?` accept such payloads from a `Result`
  built elsewhere (a call argument, a lambda, a list element). A method
  called on an inline `r.unwrap()` of a `Result[Base]` now dispatches to
  the override.

## 2.1.0 — 2026-10-19

### Added

- isinstance narrowing: where `isinstance(x, T)` is known to hold, a local
  or parameter `x` is a `T` for the semantic checker and the generated Go
  (fields, subclass-only methods, `T` parameters), with overridden methods
  still dispatching to the object's class. The test holds in `if` / `elif`
  branches, the `else` of `if not isinstance(...)`, the rest of a block
  after an early-exit guard (`if not isinstance(x, T) { return }`, also
  `raise` / `continue` / `break`), the right operand of `and` (and of `or`
  after `not isinstance`), ternary branches, `while` bodies and filtered
  comprehension results. A base class narrows to a subclass; `any` to
  `int` / `float` / `str` / `bool`, `list[any]`, `dict[str, any]`, a class or
  an interface; an interface to a class; `optional[T]` to `T` (after
  `not isinstance(x, None)` or `isinstance(x, T)`); `json` to a scalar; a
  tuple of classes to their nearest common class. Variables assigned in the
  narrowed region, fields, module globals, `nonlocal` targets, collections
  changed in place, names read by `go!` blocks, operands using `?` and
  generic classes are not narrowed, and the unknown-member error says why.
  The Go code binds a shadow of the variable at the start of the region.
  See `docs/SYNTAX.md` → "isinstance" → "Narrowing" and
  `docs/TRANSPILATION.md` → "isinstance narrowing".
- f-string slots may contain strings in the f-string's own quote and
  anything inside them — commas, `#`, braces, colons — plus nested
  brackets, dict literals and nested f-strings (`f"{",".join(xs)}"`,
  `f"{d["k"]}"`, `f"{d["n"]:.2f}"`); an unescaped delimiter quote used to end
  the string. Slots are now checked like any other expression: unknown
  members and methods, type errors and undefined names inside them are Lam
  diagnostics instead of Go build errors (`f"{seen.join(" ")}"` on a list
  reports `use " ".join(seen)`). `lamc fmt` keeps such f-strings intact.
- `enumerate(xs, start)` and `enumerate(xs, start=k)` number from `start`
  (in `for` loops, comprehensions, over strings and as a value); the start
  was ignored. A non-constant start is evaluated once.
- `del xs[i]` on a list removes the item (a negative index counts from the
  end, out of range raises `IndexError`), and `del xs[a:b]` / `del xs[a:b:k]`
  remove a slice with Python's bounds; they emitted a map `delete` (a Go
  build error).
- `r.error.kind`, `r.error.message` and `r.error.cause` on a `Result` read
  the error as an `Error`, like the `do/catch` binding (another error value
  is viewed as `Error("", str(v), v)`); member access on `r.error` used to
  pass the checker and fail in the Go build, and an unknown member is now a
  Lam error.
- Runtime benchmark `language/bench_list_index` (list reads and writes in a
  hot loop with an index the compiler cannot prove non-negative), with a Go
  baseline.

### Changed

- Catalog: `@lam/sftp` 3.4.0 (per-address authentication limits).

- **Runtime negative indexes follow Python**: `xs[i]` with `i < 0` counts
  from the end on lists, strings and tuples (`xs[len(xs) + i]`), for reads
  and assignment targets (`xs[i] = v`, `xs[i] += v`, `grid[r][c] = v`); out
  of range either way still raises `IndexError`. It used to raise
  `IndexError` for any negative runtime index (only constants such as
  `xs[-1]` counted from the end). Dictionaries are unaffected. The
  adjustment is a branch-free `i + len(xs)&(i>>63)`, skipped when the index
  is provably non-negative (constants, `len(...)`, `xs[len(xs) - 1]`,
  `range` / `enumerate` loop variables, locals only ever assigned
  non-negative values, sums, products, `//`, `%`, `min` / `max` / `abs` of
  those, `i - 1` with `i` from `range(1, ...)`). See
  `docs/SYNTAX.md` → "Slicing" and `docs/TRANSPILATION.md` → "Runtime
  negative indexes".

### Fixed

- `isinstance` on a `json` value tests the JSON value it holds
  (`isinstance(payload.n, int)`, `dict` for an object, `None` for `null`);
  it was always `false`. `isinstance(x, int)` on an `optional[int]` is true
  when the value is set (it was always `false`).
- The variable of `for row in rows` takes the element type of the local
  `rows`; it took the type of a same-named constructor parameter of another
  class (`init(self, rows: list[list[int]])`), mistyping `row` (a
  dictionary looked like a list).
- `xs.join(sep)` on a `list[str]` suggests the call spelled with its own
  operands (``use `" ".join(seen)` ``).
- A local named like a top-level function of another module — not imported
  by the file (`one: Server = ...` while `db` defines `func one()`), or
  imported and shadowed in one function — was declared and used under
  different Go names (`undefined: one`): its declaration, method calls,
  arguments, closures and comprehension variables now all use the local.
- A parenthesized str ternary joined with `+` (`(Paths.base(p) if p != "/"
  else "files") + ".zip"`) lowered to an `interface{}` closure when a branch
  type was not tracked — notably a call to another library's class from a
  library — and failed in Go (`mismatched types interface{} and string`): it
  is a `string`, a branch typed `any` is asserted to `str`, and libraries now
  see the method return types of the other libraries.
- Checking f-string slots surfaced two checker false positives, now fixed
  everywhere: a class satisfies an interface through an inherited method,
  and a tuple may be indexed with a runtime `int` (the item type when it is
  a constant, else the common or `any` type).
- A member that only the tested class has, read on a field tested with
  `isinstance` (`if isinstance(h.shape, Square) { h.shape.side }`), is a
  semantic error explaining that fields are not narrowed, instead of a Go
  build error.

## 2.0.0 — 2026-10-18

### Breaking changes

- **`catch` clauses are typed.** `catch ValueError as e` only catches
  `ValueError` (and its family); the clauses form an `if / else if` chain and
  only the first match runs. Before, every clause ran in sequence and caught
  everything. An exception no clause matches is re-raised.
- **Subclasses without `init` inherit their parent's constructor**
  (`AutoReport("auto")` instead of `AutoReport()` when the parent declares
  `init(self, title)`).
- **`self.method()` and calls on base-typed values dispatch to subclass
  overrides** (virtual dispatch), and subclass instances are accepted where
  the base class is expected.
- **Top-level statements run before `main()`** (they used to be a Go build
  error); the first plain `x = v` of a new name at top level declares a
  global.
- **Printing instances of classes in a hierarchy** shows only their own
  fields with the parent part nested (`&{{sq} 3}`).
- **Typed stdlib callbacks:** `lamiter.Iter` is generic (`Iter[T]`, `first()`
  returns `Result[T]`), `QueryBuilder.whereGroup`/`orWhereGroup` take
  `func(QueryBuilder) -> QueryBuilder`, `Http.serve` takes
  `func(str, str, str) -> str`; a bare `Iter` in a parameter, field or
  return annotation is an error.
- **New compile-time errors** for code that only failed in the Go build
  before (unknown `str`/`list` methods, undefined names in f-strings, unknown
  types and base classes, non-`bool` conditions, constant division by zero,
  untyped stand-alone lambdas, callbacks of the wrong type, ...).
- **Constant negative indexes follow Python** (`xs[-1]` is the last element).
- Libraries declaring `compatibility.lamc = "^1.18"` print a compatibility
  warning; their new patch releases declare `">=1.18, <3"`.

### Added

- `lamc build --timings` (or `LAMC_TIMINGS=1`): one `LAMC_TIMINGS {...}`
  JSON line on stderr with milliseconds per compile phase (preprocess,
  parse, semantic, transpile, go mod, go build, total).
- Benchmark suites in `benchmarks/`: `compiler/` (lamc speed per phase on
  generated programs of three sizes, a corpus and optional projects) and
  `runtime/` (each program times its own workload; hand-written Go
  baselines give a `lam/go` ratio). `sh scripts/bench.sh compiler|runtime`.
- ROADMAP: "Possible new libraries" (moving `lamstripe`/`lamotel` out of
  the repo, libraries that could be extracted from a large application).

- Typed iterator pipelines: `lamiter.Iter` is generic (`Iter[T]`) and its
  callbacks are typed from the elements, so lambdas need no annotations or
  conversions and keep static types through the chain:
  `evens: list[int] = Iter.range_(0, 100).filter(lambda x: x % 2 == 0).map(lambda x: x * x).toList()`,
  `Iter.fromList(users).map(lambda u: u.name)` (an `Iter[str]`),
  `reduce(0, lambda acc, x: acc + x)` (the initial value's type),
  `map(len)`. A callback of the wrong type is a compile error. See
  `docs/stdlib.md` → "`lamiter`".
- Methods with their own type parameters (`func map[U](self, fn: func(T) ->
  U) -> Box[U]`, on generic classes or not) lower to package-level generic
  functions (`Box_Map[T, U](s, fn)`) with rewritten call sites, also for
  imported and inherited methods (they emitted Go methods with type
  parameters, which Go rejects). See `docs/TRANSPILATION.md` → "Methods with
  their own type parameters".
- Call-site type-argument inference for lambdas: a lambda passed to a
  generic function or method takes its parameter types from the type
  arguments bound by the receiver, the other arguments and literals, and its
  body's type binds the rest (`mapList([1, 2], lambda n: f"{n}!")` is a
  `list[str]`, `Sort.sortWith(jobs, lambda a, b: a.priority - b.priority)`
  types `a`/`b` as `Job`). The semantic checker checks such lambdas against
  the callback type (annotated parameters, result, parameter count) and
  checks their bodies with the inferred parameter types; builtin list
  method lambdas get the same checks.
- Builtins as function values where a callback is expected (`xs.map(len)`,
  `nums.map(str)`, `it.map(len)`), `xs.sort(key=f)` / `xs.sort(f)` with a
  one-parameter key (stable), `reduce(fn, initial)` accumulating in the
  initial value's type, and `map` results keeping their element type in a
  chain (`words.map(lambda w: len(w)).filter(lambda n: n > 1)`).
- A local annotated with a bare generic class takes its initializer's type
  arguments (`it: Iter = Iter.range_(0, 3)` is an `Iter[int]`).

- Every documented `match` pattern works end to end: sequence patterns
  (`[x, y]`, `(a, b)`, `[first, *rest]`, `[*init, last]`), mapping patterns
  (`{"kind": k}`, `{"op": "add", **rest}`), class patterns
  (`Point(x=0, y=y)`, positional `Point(x, y)` in field declaration order,
  subclasses, interfaces), builtin type patterns (`int(n)`, `str(s)`,
  `float(f)`, `bool(b)`, `list(xs)`, `dict(d)`, `json(j)`), or-patterns
  (`1 | 2`, also binding the same names), captures (`case other`, exhaustive
  like `_`), value patterns (`Color.RED`, module constants) and `as`
  bindings, nested freely and combined with guards, over typed values,
  `any` and `json` subjects. Bindings are typed from the subject and scoped
  to their case. The semantic checker reports patterns that can never match
  the subject type, duplicate bindings, or-alternatives binding different
  names, several `*rest`, unknown classes/fields and too many positional
  sub-patterns. See `docs/SYNTAX.md` → "Match / Case".

- Top-level statements: statements at file top level (`print(x)`,
  `items.append(3)`, assignments to existing globals, `+=`, loops, `if`,
  `match`, `try`, ...) run once at program start, before `main()`, in file
  order; imported modules run theirs when initialized, in dependency
  (import) order, each module once. Globals declared before a file's first
  top-level statement keep their Go initializers; globals declared after
  one are initialized in file order with the statements; names first
  assigned inside top-level blocks are module globals; a first plain
  `count = 0` at top level declares an inferred global (it emitted invalid
  Go before). Reading a global in a top-level statement before its
  declaration is a compile error. See `docs/SYNTAX.md` → "Top-Level
  Statements".

- Subtyping: a subclass instance is usable wherever its base class is
  expected — parameters, assignments and re-assignments, returns, fields,
  `optional[Base]`, `list[Base]` / `dict[str, Base]` elements (literals,
  `append`, `insert`, `extend([...])`, `d[k] = v`), `??`, ternaries and
  `==` / `!=` (with `None` or another value), also across modules. Calls on a
  base-typed value run the override of the object's class (from outside the
  class too, and from library code that only knows the base), field access
  reads the shared base fields, `isinstance(x, Square)` tests the actual
  class, and class patterns downcast a base-typed subject (`case Square() as
  sq`, binding the subclass view and its fields). Collections stay invariant
  (`list[Square]` is not a `list[Shape]`) and a downcast without a pattern
  (`sq: Square = shape`) is rejected. A base-typed value stored as an
  interface or `any` keeps its most-derived object. See `docs/SYNTAX.md` →
  "Subtyping" and `docs/TRANSPILATION.md` → "Virtual dispatch and
  subtyping".
- Inherited constructors: a class without its own `init` inherits the
  nearest ancestor's `init` (parameters, defaults, named arguments, generic
  type arguments substituted; with several bases the first one whose chain
  declares an `init`), like Python. The subclass's class-body field defaults
  still apply, and arguments are checked against the inherited signature.
  See `docs/SYNTAX.md` → "Inherited constructors".
- `catch Cls as e` binds `e` typed as `Cls` (fields and methods reachable
  directly; a subclass instance binds its `Cls` view and keeps its
  overrides); class clauses catch subclasses declared in any module.
- Native sets support `add`, `remove` (raises `KeyError` when missing),
  `discard`, `contains`, `set()` / `set(iterable)` and the `|` (union), `&`
  (intersection) and `-` (difference) operators with their augmented forms
  (`|=`, `&=`, `-=`); the operators return new sets. Mismatched element
  types are Lam errors (`a | b` on `set[int]` and `set[str]`, `ids.add("x")`
  on a `set[int]`); `a | b` / `a & b` / `a.add(1)` / `set()` used to fail in
  the Go build and `a - b` was rejected.

### Changed

- Catalog: every official package has a release declaring
  `compatibility.lamc = ">=1.18, <3"`, tested with lamc 2.0.0:
  `@lam/jwt` 3.1.1, `@lam/memcached` 2.0.1, `@lam/protobuf` 3.0.1,
  `@lam/redis` 2.0.1, `@lam/s3` 2.2.1, `@lam/sftp` 3.3.2 (pins `@lam/ssh`
  3.0.2), `@lam/ssh` 3.0.2, `@lam/vulture` 2.0.2, `@lam/vulture/std-plugin`
  2.0.3, `@lam/vulture/tus-plugin` 3.0.2 and `@lam/vulture/ws-plugin` 2.0.2
  (pinning `@lam/vulture` 2.0.2). `third_party/lamstripe` and
  `third_party/lamotel` declare the same range.

- `lamiter.Iter` is `Iter[T]`: `toList()` returns `list[T]` (was
  `list[any]`), callbacks are `func(T) -> ...` (an `Iter[any]` keeps
  `func(any) -> ...`), `reduce[A](initial: A, fn: func(A, T) -> A)` returns
  `A`, `enumerate()` yields `tuple[int, T]` pairs (a 2-element list before;
  they print the same, `[0 a]`), and `first()` returns a `Result[T]` with an
  `EmptyIteratorError` for an empty iterator (it returned `None`).
- `Http.serve(port, handler)` types `handler` as `func(str, str, str) ->
  str`; an `any` value holding a handler is still accepted (checked
  conversion).
- A method with its own type parameters cannot be overridden (compile
  error), and a generic class written without type arguments in a
  parameter, field or return type is a compile error (it failed in the Go
  build).

- The Rosetta Code programs moved into the core cases
  (`tests/tests/cases/rosetta/`); the separate `rosetta` suite is gone
  (`sh scripts/test.sh core rosetta`). Core filters match the path below
  `cases/`.
- `tests/benchmarks/` was replaced by the two suites above.
- Methods are now virtual: `self.method()` inside a parent-class method calls
  the override of the object's actual class (previously the parent's own
  method always ran, silently ignoring the override), and so does any call on
  a base-typed value. The compiler knows the whole program's class hierarchy:
  only classes that have a subclass somewhere get a small dispatch interface,
  a `_lamSelf` field and inlinable forwarding wrappers, and only for the
  methods a descendant overrides; a call goes through a wrapper only when a
  descendant of the receiver's static class overrides the method, so classes
  that are never subclassed (even when they call methods through `self`) are
  emitted exactly as before. Constructors bind the object into every embedded
  ancestor (also after `base.init(...)` and across modules). Explicit
  `base.method()` / parent-alias calls stay static. See
  `docs/TRANSPILATION.md` → "Virtual dispatch and subtyping".
- A class without `init` now inherits its parent's constructor instead of
  getting a no-argument one: `AutoReport()` for `class AutoReport(Report)`
  with `Report.init(self, title)` must pass the title (`AutoReport("auto")`).
- Empty interfaces (`interface Marker { }`) are valid, so the syntax
  diagnostic rejecting them is gone; a field or statement inside an
  interface body now reports ``expected: `func` or `}` `` with an "Interface
  bodies only declare method signatures" hint.

### Fixed

- A lambda passed to a generic function lowered with the type parameters
  in its Go signature (`func(x T) U`), which did not compile.
- `Retry.runFn(fn)` / `Retry.until(predicate)` accept any function without
  parameters: a `func() -> Result` (the documented example), `-> str` or
  `-> bool` function failed with "fn must be func() -> any" or counted as
  `false`.
- An untyped lambda handler for `Http.serve` was lowered with `any`
  parameters and answered every request with an empty body.
- `xs.map(len)` failed in the Go build, a `map` result chained into
  `filter` kept the receiver's element type, and `reduce` with an initial
  value of another type than the elements did not compile.
- Nested generic type arguments in a constructor call
  (`Iter[tuple[int, T]]()`) and `Box[str](...)` used as a receiver of a
  method call lowered to invalid Go.

- `str` method results (`split`, `startsWith`, `index`, ...) keep their types
  in the semantic checker, also on call receivers (`link().split("=")[1]`
  inside a `json` literal was `unknown`).

- The `catch err` binding of `do { } catch err { }` is an `Error`:
  `err.message` / `err.kind` / `err.cause` work (they failed in the Go build
  on the dynamic value), an `Error` subclass keeps its class, and a
  non-`Error` value (`Result.Err("x")`) is wrapped with an empty kind and
  prints as before (`Error.__str__` prints only the message when `kind` is
  empty).
- A `?` inside a `match` guard (`case 4 if half(n)? == 2`) is evaluated only
  when its case is tried; its prologue used to run before the whole `match`
  (propagating an error even when another case would have matched).
- A constructor or method parameter, local or global named `s` (`func
  init(self, s: int)`) no longer collides with the generated receiver
  (`s := &Sq{}` redeclared the parameter): such methods use the receiver
  name `_lamS`.
- Printing an instance (`print`, `str()`, f-strings) of a class in an
  inheritance hierarchy no longer shows the internal virtual-dispatch field
  (`&{3 <nil>}`) or a parent pointer address (`&{0xc000010030 3}`): it shows
  the user fields, the parent part as a nested `{...}` (`&{{sq} 3}`), a
  base-typed value prints its actual object, and `__str__` still wins.
  Classes outside any hierarchy print as before.
- A subclass's class-body default for a field its parent declares
  (`sides: int = 4` over the parent's `sides: int = 0`) is no longer lost when
  `base.init(...)` rebuilds the parent part (unless the parent's `init`
  assigns that field itself).
- A subclass declared in another module than its parent no longer gets a
  duplicate copy of the parent's fields (which hid the parent's values from
  inherited methods), and its constructor allocates the imported
  grandparents and binds the object into them.
- A method overridden by an intermediate class (`Puppy(Dog(Animal))` with
  `Dog` overriding `sound`) is no longer reported as an ambiguous inherited
  member on the grandchild.
- `isinstance(sq, Shape)` with `sq` a subclass instance is `true` (it was
  `false`).
- A method call on a list element or a loop variable of a class type
  (`shapes[0].area()`, `for s in shapes { s.area() }`) knows the receiver's
  class (default arguments, overloads and dispatch); explicit type arguments
  naming a class (`Registry[Shape]()`) lower to the class pointer, and the
  arguments of a method called on a generic instance (`reg.add(x)` with
  `reg: Registry[Shape]`) are typed with the instance's type arguments.
  Generic classes are known before any signature is collected, so a
  parameter typed `Box[int]` has the `*Box[int]` type in the call metadata
  (it was `interface{}` when the function came before the class or was
  collected first, losing the argument's contextual typing).
- Typed `catch` clauses dispatch on the raised type: only the first matching
  clause runs (`catch ValueError` no longer catches everything, and the
  following clauses no longer all run in sequence). Builtin families are
  honoured (`LookupError` catches `KeyError`/`IndexError`, `ArithmeticError`
  catches `ZeroDivisionError` and Go divide-by-zero faults), class clauses
  also catch subclasses declared in the same module, and an exception no
  clause matches propagates to the enclosing `try` after `finally` runs.
  Repeating the same binding name across clauses now compiles.
- `match` guards (`case 5 if n > 100`) are honoured: a case only matches
  when its guard holds, otherwise the next case is tried. The subject is
  evaluated once, and a capture pattern is bound before its guard runs.
- Nested `for`/`while` loops with `else` clauses use one break flag per
  loop: an inner loop no longer shadows the outer flag (which made the
  outer `else` run after a `break`), a `break` out of an inner loop without
  `else` no longer cancels the outer `else`, and several loop-`else`
  statements in one function no longer fail with `no new variables on left
  side of :=`.
- `for k, v in pairs` over a list of tuples (or of nested lists, a list
  literal of tuples, or a generator) unpacks each element instead of binding
  the index and the whole tuple; three or more targets and `_` placeholders
  work too. Dicts keep binding key/value.
- Iterating an unannotated set literal (`flags = {"new", "active"}`, then
  `for f in flags`) yields the elements instead of `true`; uniform set
  literals keep their element type (`set[str]`).
- Class-body field initializers (`class A { secret: int = 42 }`,
  `items: list[T] = []`) are no longer dropped: every constructor assigns
  them (ancestors first) before the `init` body, including classes without
  `init` and inherited fields.
- Awaiting an async function without a return type no longer dies with
  `all goroutines are asleep - deadlock!`.
- An exception raised inside an async function no longer kills the process:
  it is re-raised at the `await` that collects the handle, where
  `try`/`catch` (typed clauses included) can handle it. Async handles now
  carry result thunks (`chan func() T`; `await h` lowers to `(<-h)()`). A
  `return` inside a `try` block of an async function no longer continues
  past the `try`, and `return` in nested functions/lambdas inside async,
  `try` or `do` bodies targets the nested function.
- A `return` (bare or with a value) inside a `do { } catch` body returns
  from the enclosing function instead of failing with Go's `not enough
  return values` / `cannot use ... as *Result`.
- The preprocessor/parser accept valid forms they used to reject: inline-brace
  `if`/`for`/`while` blocks followed by a trailing `# comment`; comment-only
  bodies (`func f() {\n    # todo\n}`, classes, `if`, loops); compact one-line
  `if c a = 1 elif d a = 2 else a = 3` / `if c a = 1 else a = 2`; typed
  lambdas inside an `if`/`while` condition
  (`if check(xs, lambda (a: int) -> bool: a > 0) {`); mapping patterns
  (`case {"kind": k}` / `case {"kind": k, **rest}`, parse only for now) and
  negative literal patterns (`case -2`); multi-line parenthesised conditional
  expressions with `else` on its own line; `and` / `or` at the end (or start)
  of a continued line; empty interfaces (`interface Marker { }`).
- `case 'a'` (a single-quoted string pattern) matches the string instead of
  emitting a Go rune literal, and `break` inside a `match` case in a loop
  leaves the loop (it only left the generated Go `switch`). Non-literal
  matches lower to an if/else chain; literal matches over scalars keep the
  Go `switch`.
- An unused local in a block ending with `return` (an `if`/`else` branch,
  a `match` case, a function body) no longer breaks Go's terminating
  statement analysis (`missing return`): the `_ = name` silencer is placed
  before the `return`.
- Class patterns accept positional and keyword sub-patterns together
  (`Point(x, y=0)` failed to parse).
- Valid code the semantic checker rejected: interface methods written with
  `self` (`func draw(self) -> str`, the documented form) no longer count it
  as a parameter; a typed function whose every path raises needs no
  `return`; the payload of a typed `Result` local or field is known in JSON
  contexts (`body: json = r.unwrap()`, `{"bytes": c.unwrap()}`) and
  `r.ok()` is a `bool`; a function passed by name binds the type
  parameters of a `func(T) -> U` parameter (`mapList(xs, len2)`), and so
  does a lambda with annotated parameters (`lambda (n: int): n * n`);
  `.map(lambda p: p.name)` over a list of class instances is typed from
  the element (also in the generated Go, which used `interface{}`); an
  untyped numeric constant initialises or is returned as a number-constrained
  type parameter (`t: T = 0` in `class Stats[T: number]`) and such values
  support arithmetic; a local assigned in the `try` body and in every
  `catch` body (also with an annotation in each) is definitely assigned
  after the statement and is hoisted in Go; `{}` where a `set[T]` is
  expected (annotation, reassignment, return, parameter, field) is an empty
  set (the docs said `{}` was always a dict).
- A top-level function named like a Go keyword (`fallthrough`, `select`,
  `goto`, `struct`, `chan`) is called by its emitted Go name (calls used
  `Fallthrough__lam`, undefined, directly and inside f-strings).
- A class can inherit from a generic instantiation (`class IntBox(Box[int])`,
  `class Entry(Pair[str, int])`, `class Named[T](Box[T])`): the struct embeds
  `*Box[int]`, constructors use `NewBox[int]`, and inherited methods return
  the bound type in the semantic checker.
- `return` inside a `try` nested below the top level of a typed function or
  method (in a loop, branch or `match` case) and inside a `try` within a
  `do { }` body returns from the function (Go reported `not enough return
  values` / `too many return values`); methods containing a `try` now get the
  named `retval` result like functions.
- The functions a library module defines after its `func main()` are no
  longer dropped when the module is imported (the stripping of the
  library's `main` removed everything after it).
- `catch _e` (an underscore-prefixed binding, as the unused-variable warning
  suggests) binds the exception instead of being treated as a class name.
- `optional[T]` / `Option[T]` are usable as documented: they accept `None`
  and `T` values (variables, fields such as `a.next = b`, parameters and
  returns), compare with `None`, and an optional class reference flows back
  into the class type. `optional[int]`-style value payloads are boxed when an
  `int` is assigned or returned, and `opt ?? fallback` yields the payload (a
  nil class pointer now selects the fallback instead of being returned as a
  non-nil `interface{}`).
- Lambdas are contextually typed: a lambda assigned, passed or returned
  where a `func(...)` value is expected takes its parameter and return types
  from it (`apply(lambda x: x > 0, 3)`, `g: func(int) -> bool = lambda a:
  a > 1`, `run(lambda: print("x"))` into `func()`, `nums.foreach(lambda n:
  print(n))`, returning `lambda: Square(1)` from `-> func() -> Shape`,
  `adder(2)(10)`); a lambda whose body is a call without a value (`print`, a
  function without a return type) is a void `func`, so `Cron.schedule(expr,
  lambda: print("tick"))` compiles. `words.map(lambda w: len(w))` yields
  `list[int]`. All of these emitted invalid Go before. `whereGroup` /
  `orWhereGroup` take a `func(QueryBuilder) -> QueryBuilder`, so
  `.whereGroup(lambda q: q.whereEq(...))` types `q`.
- Calling the function value a call returns (`maker()()`, `f()` with `f:
  func() -> Shape`) dispatches methods on the result's class.
- `?.` chains on `optional[...]` values are typed: `user?.profile?.email`
  chains, `e: str = user?.email ?? "x"` compiles, a value-typed field read
  through `?.` is an `optional` of that type, and `print` / f-strings show
  an `optional[str]` / `optional[int]` value (or `<nil>`) instead of a
  pointer address. `d[key] ?? fallback` on a typed map selects the fallback
  for a missing key (dictionary destructuring defaults use it).
- Unannotated locals keep the type the semantic checker infers (`s = "ab"`,
  `r = g()` with `g() -> Result[int]`, `handles = [slow(1), slow(2)]`), so
  `for ch in s` yields one-character strs, `r.unwrap() > 1`,
  `r.unwrap() * 3`, `z += r.unwrapOr(5)` and `await h` over a list of async
  handles compile (they failed in the Go build).
- Comprehensions with tuple targets whose names are unused (`[k for k, v in
  table]`, `[label for number, label in pairs]`), over `enumerate(...)`
  (lists, list literals and strings) and over generators (also yielding
  tuples, `yield ("a", 1)`), and `for i, k in enumerate(["p", "q"])` with a
  typed `k` emitted invalid Go; an unused `enumerate` index in a `for` loop
  is silenced inside the loop.
- `sorted(xs, key=fn, reverse=true)` honours `key` (a lambda or function
  reference) and `reverse` (both were ignored) and sorts stably; a
  comprehension argument takes the expected element type.
- A function reference passed to a call whose result is a receiver
  (`build(inc).get()`, `route("/a", echo).unwrap()`) is mangled like any
  other function reference, and a user function named like a Go builtin
  (`make`) is called instead of the builtin.
- A static method of a generic class that does not use the class's type
  parameters (`Box.two()` on `class Box[T]`) is no longer generic in Go
  (`cannot infer T`).
- `int(3.7)` truncates to 3 (Go rejected the constant conversion), `x **= 2`
  on an `int` stays an `int`, slices with negative or stepped bounds of a
  `list[T]` / `str` keep their type (`b: list[int] = a[-2:]`), and a dynamic
  negative slice bound (`a[k:]` with `k = -2`) counts from the end instead
  of clamping to 0.
- String repetition of a call result (`word() * 2`, `f(path) * n` with a
  `func`-typed `f`), `"a,b".split(",").length()` (dispatched the string
  `length`), `with file as f` then `f.read().unwrap()` into a `str`,
  dictionary destructuring from an inferred dict into annotated targets,
  `isinstance(None, None)` / `isinstance(3, None)`, and a local only
  assigned inside a `go!` block (`declared and not used`) all emitted
  invalid Go.
- The semantic checker reports, with a Lam diagnostic instead of a Go build
  error: undefined names inside f-string slots (at the slot's line and
  column, with a did-you-mean), unknown types in annotations
  (`list[Order]`) even without a close match, unknown base classes
  (`class Child(Parent)`), non-`bool` `if` / `elif` / `while` conditions
  (`if xs`, `while n`, `if payload.ok` on `json`), constant division by
  zero (`10 / 0`, `n % 0`), re-annotating an outer local with another type
  in an inner block, and untyped lambda parameters that nothing types when
  the body needs a concrete type (`sq = lambda x: x * x`; `lambda v: v > 0`
  passed to a `func(any) -> bool` parameter). Chained calls
  (`Iter.range_(0, 9).filter(...)`) now get the same call checks as calls on
  typed variables.
- Constant negative indexes follow Python (`xs[-1]` is the last element,
  also as an assignment target; out of range raises `IndexError`). Inferred
  int/float arithmetic (`f = 1.5 * a` with `a = 1`) converts the int
  operand, an unannotated tuple (`t = (1, "a")`) is one value and `n, s = t`
  destructures a tuple variable (also annotated ones), `for v in
  payload.tags` iterates a `json` array (each item is `json`), and a
  `list[any]` value (`Iter...toList()`) assigned to a `list[int]` target is
  converted element by element. `lamiter` callbacks are typed
  (`func(any) -> bool`, `func(any) -> any`, ...), so lambdas passed to them
  get the right Go signature.
- Calling a method a `str` or `list[T]` does not have (`name.rfind(".")`,
  `text.find("x")`, `xs.index(2)`, `names.add(x)`) is a semantic error with a
  hint (`lastIndex` / `index`, `toUpper`, `x in xs`, `append`, ...) instead
  of a Go build error. `xs.pop(i)` removes and returns the item at `i`
  (negative from the end); it ignored the index and popped the last item.
- `lamtest`'s `assertEqual` / `assertNotEqual` / `assertContains` compare
  values: numbers by value across Go types and lists / dicts element-wise,
  so `Test.assertEqual(f(), [80, 90, 100])` with `f() -> list[int]` passes
  (it failed with "expected [80 90 100] got [80 90 100]").
- A generic class imported from another module accepts type arguments
  (`Bag[int]()`, `b: Bag[str]`, `-> Bag[int]`, `Pair[str, int](...)`); it was
  rejected as "not generic" and emitted invalid Go.
- Diagnostics point at the right line after preprocessor rewrites:
  brace-less blocks (`if x print(...)`, `else ...`, `for`/`while`),
  multi-line triple-quoted strings and dictionary destructuring are now
  rewritten without adding or removing lines (they shifted later errors by
  several lines and syntax-error carets landed on blank lines).
- `lamc fmt` keeps `i++` / `xs[0]--` (it produced `i + +`, which no longer
  compiled), formats files with dictionary destructuring (`{name} = user`)
  and the `__inc__` example, keeps the text of multi-line triple-quoted
  strings (it re-indented their lines, changing the value), keeps
  `match [a, b]` / `case [x, *rest]` / `case {"k": v}` patterns valid, and
  spaces unary signs after operators and keywords (`i - -1`, `return -1`).
  Every `.lam` test case and stdlib module now formats to a file that parses
  and behaves the same.
- A function ending in a loop whose `else` block returns is accepted when the
  loop body never `break`s out of it (every exit runs the `else`); the
  `else` block is emitted as a plain Go block so Go sees the function
  terminate (`missing return` before). A typed lambda stored in a global
  (`classify = lambda (n: int) -> str { ... }`) keeps its `func` type (it
  was `interface{}` and could not be called).
- Documentation: the examples using `?` or `do/catch` import
  `from lamerrors import Result` (stdlib.md, SYNTAX.md, TRANSPILATION.md,
  third_party_libraries.md); `Log` documents its real API (`setPrefix`,
  `*f` variants, single-argument `error`); `Stats.percentile` examples pass
  `95.0` (p is 0-100); the `groups.aggregate` example passes one strategy
  per column; the decorator example converts `any` values before building
  `json`; the streaming plugin snippet uses Lam (`or`, `None`, Lam field
  names); the function-type example no longer recurses into `main`; the
  `Point.distance` example imports `Math` and prints `5`; nested functions
  may be generic; escaped quotes in f-string slots are documented as
  working; the generic `Stack` example declares `self`; the `?` desugaring
  sketch compiles; `10 / 0` examples use a runtime divisor.
- Whole classes and nested blocks on one line (`class Shape { func
  area(self) -> int { return 0 } }`) and a one-line `} else { ... }` after
  another statement on the same line parse (a `;` was inserted after the
  inner block / the `else` block was taken for a literal).
- Comprehensions type their loop variable from a typed list (`[o.customer.
  profile?.email ?? "none" for o in orders]` into `list[str]`), unpack
  nested targets over `enumerate` (`for i, (name, n) in enumerate(pairs)`),
  and iterate `-> generator[T]` functions with typed items (`yield [1, 2]`
  in a `generator[list[int]]` sends a `list[int]`). A list comprehension
  whose element parts the checker cannot type is no longer rejected as
  `list[tuple[any, any]]` against an annotated target.

## 2026-10-25

### Changed

- Bumped the compiler to 1.19.9 (the fixes below).
- Catalog: `@lam/s3` 2.2.0 (SSE-C customer keys, cross-key and large
  server-side copies).

### Fixed

- A local or parameter no longer inherits the class type of a same-named
  local in another function: `out: Box` in one method made `out: str` in
  another dispatch `out.toUpper()` as a method of `Box` (Go build error).
- Arithmetic expressions have a type: `put(key, chunk + "end")` picks the
  `str` overload instead of `any`, and `min(partSize, size - offset)` stays
  an `int` instead of widening to float.
- An element of a `list[any]` / `dict[str, any]` can be assigned to a typed
  variable (`name: str = row[0]`): a checked conversion.
- `lamc install` no longer refuses a correctly labelled minor release as
  "mis-labelled": the API differ counted struct-literal lines in `go!`
  blocks (`CopySource: x,`) and method locals as class fields. Fields are now
  class-level declarations and `self.x: T` assignments.

## 2026-10-24

### Changed

- Bumped the compiler to 1.19.8 (the fixes below).

### Fixed

- A string literal ending in an escaped backslash (`"\\"`, as in
  `path.replace("\\", "/")`) no longer swallows the rest of the line: the
  preprocessor counted `\"` as an escaped quote. The f-string scanner of the
  namespacing pass had the same problem and ignored escaped closing quotes.
- Slices with out-of-range bounds clamp like Python (`s[i:i + 2]` on the last
  character, `xs[5:]` on a short list) instead of panicking: the fast
  `[a:b]` path used Go's native slicing.
- `for x in self.items()?` inside a method ranges over the typed payload
  (it ranged over `interface{}`), and `return Result.Ok([...])` in a method
  shapes the literal as the declared payload (methods did not receive the
  declared return type, so the payload assertion panicked).

## 2026-10-23

### Added

- String repetition: `"ab" * 3`, `3 * "ab"` and `s *= 3` (a negative count
  gives `""`); other operand types are a semantic error.
- `Smtp.setTimeout(seconds)` (default 30): one delivery may not take longer.

### Changed

- Bumped the compiler to 1.19.7 (the fixes below).
- `lamsmtp` delivers through its own client: implicit TLS on port 465,
  STARTTLS when offered, PLAIN auth only when the server supports AUTH.

### Fixed

- `lamsmtp` had no timeouts (`smtp.SendMail`): a server that accepted the
  connection and never answered blocked the caller forever. Addresses with
  line breaks are refused (command injection).
- An escaped delimiter quote inside an f-string interpolation
  (`f"{table[\"key\"]}"`) reached the Go source and broke the build.
- A static method passed as a callback (`apply(K.add)`) is checked against
  the expected function type by the semantic pass (it failed in Go).
- Calling a method on a `dict` (`d.keys()`, `d.get()`, `d.pop()`) is a
  semantic error pointing to `for k, v in d`, `k in d`, `d[k]`, `del d[k]`
  and `len(d)` instead of a Go build error.
- Augmented assignments are type-checked: `n += "x"` and `i += 0.5` (int
  target) are semantic errors instead of Go build errors.

## 2026-10-22

### Changed

- Bumped the compiler to 1.19.6 (the fixes below).
- Catalog: `@lam/ssh` 3.0.1 and `@lam/sftp` 3.3.1 (`golang.org/x/crypto`
  v0.58.0: SSH channel denial-of-service fixes; Go 1.26).

### Fixed

- **Boolean precedence:** `a and (b or c)` was emitted as `a && b || c`,
  which Go reads as `(a && b) || c`: the parentheses around an `or` (or a
  conditional/coalescing expression) inside an `and` were dropped. A nil
  check such as `x != None and (... x.f ...)` no longer guarded its
  right-hand side, and `enabled and (force or due)` ran while disabled.
  Recompile code built with earlier versions.
- A list comprehension over a list literal (`[i for i in [1, 2, 3] if i > 1]`)
  types its variable from the literal (comparisons on it did not compile).
- A value declared `any` can be assigned to a `str`, `bool`, list or dict
  variable (`items: list[any] = raw` after `isinstance(raw, list)`): a
  checked conversion instead of a Go compile error.
- `xs.sort()` used as a statement warns: it returns a sorted copy and leaves
  `xs` unchanged (use `xs = xs.sort()` or `xs.sort(inplace=true)`).
- `for` loop variables get the element type of the iterable (list literals,
  typed lists; key and value types of dicts), so `{"url": target}` and
  typed calls on them are checked instead of "unknown".
- `Json.fromValue(x).unwrap()` / `.unwrapOr(...)` are JSON-typed where JSON
  is required (the payload of the consumed `Result`).

## 2026-10-21

### Changed

- Bumped the compiler to 1.19.5 (the fixes below).
- Catalog: `@lam/jwt` 3.1.0 (ECDSA ES256/384/512, EC JWKS keys).

### Fixed

- `if f()? {`, `if not f()? {`, `elif f()? {` and `while f()? {` parse: a
  single `?` before a brace is error propagation, so the brace opens the
  block (`x ?? {...}` still coalesces to a dict literal).
- `while f()? {` evaluates the condition (and its `?`) on every iteration;
  it was evaluated once before the loop. `elif f()? {` runs its `?` inside
  its own branch instead of referencing an undefined temporary.
- A conditional expression can end with a dict literal:
  `x: json = {"a": 1} if c else {"b": 2}` (the brace after an expression
  `else` was read as a block).
- A variable declared `json` keeps its type when reassigned in a branch
  (`payload = {...}` inside an `if` made it "unknown").
- `value ?? {"fallback": true}` is accepted where JSON is expected.
- A static method can be used as a function value (`apply(K.twice)`, stored
  in variables, lists and dicts), and a function stored in a list or dict
  can be called directly (`handlers[0](x)`, `table["inc"](x)`).

## 2026-10-20

### Changed

- Bumped the compiler to 1.19.4 (the fixes below). Catalog: `@lam/sftp`
  3.3.0 (adaptive ciphers).

### Fixed

- `for k, v in {"a": "x", ...}` over a dict literal whose values share one
  type binds typed variables (`len(v)`, string methods); the literal was a
  `map[string]interface{}`.
- A function passed by name to a function-typed parameter is checked
  against the callback's shape (arity, parameter and return types):
  `onPasswordAuth(accept)` with `accept(user: str, password: str)` where
  `func(SftpAuth) -> bool` is expected used to fail only in Go.

## 2026-10-19

### Changed

- Bumped the compiler to 1.19.3 (the fixes below).
- Catalog: `@lam/sftp` 3.2.0 (`onOpenWrite`).

### Fixed

- `min`, `max` and `abs` have typed overloads: whole numbers give `int`
  (Go's builtin `min`/`max`), any `float` gives `float`, strings compare
  lexicographically, any number of arguments or one list
  (`slices.Min`/`Max`); mixing numbers and strings is a semantic error.
  They used to return `float` always (`math.Min`), so `lo: int = min(a, b)`
  did not compile, and three arguments failed.
- Iterating a string yields one-character strings (`for ch in text`, `[c for
  c in text]`): the loop variable was a Go rune, so `ch >= "a"` did not
  compile and `print(ch)` showed the character code.
- `x += f()?` gives the `?` payload the target's type, also for method calls
  and fields (`sent += self.one(i)?` added an `interface{}` to an int).
- Re-declaring a name with a different type in the same block
  (`again: str = ...` then `again: int = ...`) is a semantic error; it used to
  reuse the first Go variable and fail in Go.

## 2026-10-17

### Changed

- Bumped the compiler to 1.19.2 (the fixes below, listed under 2026-10-16:
  `for` over list literals, fields of call results, cross-module return
  types, plain + aliased imports of one function, `_` range variables) and
  the unit gate now runs the package-manager and import suites.

## 2026-10-16

### Changed

- The unit gate (`scripts/test.sh unit`) now runs the package-manager and
  import suites that existed but were never executed: manifest, install CLI,
  tidy/verify, replace workspaces, init introspection, extlibs resolution,
  dependency crash, cache integration and scoped imports (all passing).

### Fixed

- `_` as the variable of a `range` loop or comprehension
  (`["?" for _ in range(n)]`, `for _ in range(3)`) compiles; it lowered to an
  invalid Go `for _ := 0; ...` counter.
- `for x in [-1, 0, 1]` binds `x` as an `int`: the loop ranges over the typed
  slice the variable was typed from (the literal was lowered as
  `[]interface{}`), and signed number literals are inferred as numbers.
- A field of a function's class-typed result (`current().box`,
  `apiState().db`, also for imported functions) keeps its static type when
  passed to a typed parameter instead of being type-asserted as
  `interface{}` (a Go compile error). Function return types are now part of
  the cross-module metadata.
- Importing a function both plainly and under an alias
  (`from m import f` and `from m import f as g`) makes `g` call the same Go
  symbol as `f`; when `f`'s Go name was de-duplicated (e.g. it clashes with a
  class `F`), `g` used to lower to the class name.

### Changed

- Bumped the compiler to 1.19.1: the fixes listed under 2026-10-15 that
  landed after the 1.19.0 tag (nested `?` evaluation, comparison grouping,
  scalar method-return dispatch, runtime conversion fallback, `len()` in json
  literals, git branch refresh in `lamc install`).
- Official package catalog: `@lam/sftp` 3.1.0 (`activeSessions`,
  `disconnectWhere`), and 3.0.1, `@lam/vulture/std-plugin`
  2.0.2, `@lam/vulture/tus-plugin` 3.0.1 and `@lam/vulture/ws-plugin` 2.0.1
  pin their dependencies to published tags (`@lam/ssh` 3.0.0,
  `@lam/vulture` 2.0.1); release tags are plain versions without a `v`.

## 2026-10-15

### Added

- Added a link-time check for top-level Go symbol clashes between modules.
  Because every module links into one Go package, duplicate classes,
  functions, constants, private helpers or raw `go!` declarations across
  modules now fail with a Lam diagnostic that lists each module, location
  and source snippet instead of Go's `redeclared in this block` cascade.

- Inline `unwrap()` / `unwrapOr(...)` keep the `Result[T]` payload type when it
  is statically known (annotated locals and parameters, including lambda
  parameters, and calls to functions/methods declared `-> Result[T]`), so
  expressions such as `r.ok() and r.unwrap()` or `len(load().unwrap())` compile;
  unknown and `any` payloads keep the dynamic fallback.

### Changed

- Bumped the compiler feature version to 1.19.0. Official packages were
  audited against it: every package source now passes the semantic check
  without errors or warnings (`@lam/s3` returned `Result[bool]` guards from
  differently typed functions, `lamotel` converted `dict[str, any]` to `json`
  implicitly), and all package suites pass on both 1.18 and 1.19, so their
  `^1.18` compatibility stays. Package releases: `@lam/jwt`, `@lam/protobuf`,
  `@lam/sftp`, `@lam/ssh`, `@lam/vulture/tus-plugin` 3.0.0; `@lam/s3` 2.1.0;
  `@lam/redis`, `@lam/memcached` 2.0.0; `@lam/vulture`,
  `@lam/vulture/std-plugin` 2.0.1; `lamotel` 0.2.1.
- Unused-import warnings are no longer reported for package entry modules
  (`__init__.lam`) or modules made only of imports: their imports are
  re-exports.
- Module namespacing: same-name top-level functions (public and private) and
  constants in different linked modules no longer clash; each module keeps its
  own (module-qualified Go names, metadata taken from the defining module, raw
  `go!` references renamed). `from X import A as B` now works for functions,
  constants, classes and static methods (it was documented but not
  implemented), `import X` / `import X as Y` give `Y.name` access to functions,
  constants, classes, static methods and type annotations, and scoped packages
  are imported with a required alias (`import @lam/jwt as jwt`; a missing
  alias is a compile error). A local definition clashing with a plain import
  suggests an `as` alias. One class imported under several names is one
  nominal type. Module-level variables, classes and interfaces are namespaced
  too (classes are renamed at the Lam level, so constructors, static members
  and metadata follow), f-string placeholders see aliases and `module.member`,
  and raw `go!` code follows the renames of its own module and of unaliased
  imports. Definitions in the main module and the compiler-referenced classes
  (`lamerrors.Result`/`Error`, `lamstrings.Strings`) keep their names; clashes
  between those, raw `go!` declarations and constructor-vs-function collisions
  are still reported by the link-time clash diagnostic.
- `int()`, `float()` and `bool()` follow explicit conversion rules: numerics and
  `bool` convert, `str` is parsed and throws `ParseError` on malformed text,
  `json` converts numbers/booleans and parses strings (otherwise `TypeError`),
  classes convert through new `__int__` / `__float__` / `__bool__` methods
  (validated as `(self) -> <type>`), and other types are compile errors.
  `int(any)` / `float(any)` / `bool(any)` emit a warning and are checked at
  runtime (`int` accepts every integer kind such as `int64` database values,
  `float` every floating kind, `bool` a bool; anything else throws
  `TypeError`); they previously produced invalid Go (`int`, `float`) or
  compared with `0` (`bool`). `int(str)` / `bool(str)` were compile errors before.
- **Breaking:** renamed `lammigrate.Schema` to `SchemaBuilder`
  (`from lammigrate import Migrator, SchemaBuilder`). The old name clashed with
  `lamschema.Schema`, so `lammigrate` could not be linked together with
  `lamschema` or anything that uses it, such as `@lam/vulture`.
- `lamc init --lib` no longer scaffolds a `tag()` function. The library name
  and version already live in `lamlib.toml`, and the generic helper made any
  two scaffolded libraries impossible to link into one program.

### Fixed

- `len(...)` is typed `int` by the semantic checker, so `{"n": len(items)}`
  is accepted where a `json` value is expected.
- `outer(inner()?)?` evaluates `inner()` once and casts each `?` to its own
  payload type. The arguments of a user function call containing `?` were
  lowered twice (running the inner call twice), and the inner `?` inherited
  the enclosing statement's cast (e.g. a `json` payload asserted as a map at
  run time).
- Builtin methods on the result of a method returning a scalar
  (`Rows.str(row, key).contains(...)`, `g.greet().endsWith(...)`) dispatch on
  the scalar type; the return type was taken for a class name, emitting
  `.Contains` on a Go string.
- `int(x)` / `float(x)` / `bool(x)` whose argument type the transpiler cannot
  see (e.g. `int(addr.split(":")[1])`) apply the conversion rules at run time
  (parse strings, convert numbers, booleans to 0/1) instead of emitting a Go
  conversion that does not compile for strings.
- A parenthesized comparison or boolean expression used as a comparison
  operand keeps its parentheses in Go: `(a == "") != (b == "")` lowered to the
  invalid `a == "" != b == ""`.
- `lamc install` refreshes cached git repositories with explicit branch and
  tag refspecs. The cache is a bare clone without a fetch refspec, so branch
  refs were never updated: `ref = "main"` installed the commit cached on first
  use, and a branch pushed after the first clone (e.g. to an initially empty
  repository) failed with "pathspec did not match".
- The install-time SemVer gate compared an empty API surface for installed
  packages: its skip list (`tests`, `extlibs`, …) was matched against the
  absolute path, and installed packages live under `extlibs/`. Reinstalling
  any package reported bogus feature changes (requiring `--allow-breaking`)
  and real breaking changes went undetected. `tests/tests/test_apidiff.py` is
  now part of the unit suite.
- `test_stdlib_ratelimit` no longer fails under heavy parallel load: it
  required an 80 ms wait after draining the bucket, but the bucket could
  refill between draining and starting the clock. It now checks that `take()`
  waits for the deficit present when it starts.
- Passing a class to an imported function or method whose parameter is an
  interface the caller did not import (`describe(n: Named)`) is accepted; the
  interface was unknown to the importer's semantic check ("has type `Config`,
  expected `Named`").
- Builds using `lamarray` no longer fail intermittently: gonum was the only
  unpinned stdlib Go dependency, so every build ran `go mod tidy` against the
  module proxy to find its latest version and occasionally failed under
  parallel load. It is now pinned (`gonum.org/v1/gonum v0.17.0`, the version
  builds already resolved), a test enforces a pin for every third-party stdlib
  import, and the core test runner's failure summary skips warning blocks so
  the actual error is no longer cut off.
- A ternary without a target type takes its branches' common type (`int` with
  `float` widens to `float`); it lowered to `interface{}`, so
  `"x: " + (a if c else b)`, arithmetic on a ternary or `len(...)` of one
  failed to compile in Go.
- Functions overloaded only by their `Result[T]` return type (for example
  `Conv.parse`) select the variant from the target type when consumed through
  `?`, `unwrap()` or `unwrapOr()`; the first variant was always chosen, so
  `f: float = Conv.parse("2.5")?` parsed an int and `b: bool = ...?` was
  rejected.
- Unused `catch` variables (`try`/`catch` and `do`/`catch`) now produce a Lam
  warning (``unused catch variable `name` ``) and compile; the unused-local
  silencer no longer treats a name inside a string literal, rune or comment as
  a use, which left such variables unused in Go ("declared and not used").
- `sep.join([...])` accepts an inline list literal or comprehension; the
  argument was lowered as `[]interface{}` and rejected by Go.
- `tests/benchmarks/cases/stdlib/bench_json_encode.lam` builds a native `json`
  payload; it no longer compiled after `Json.encode` started accepting only
  `json` values.
- `expr?` on an overloaded function or static method whose variants return
  different `Result` payloads now asserts the payload of the variant actually
  called. `_ = Schema.validate(schema, doc)?` with `json` arguments
  previously asserted the `str` variant's payload and panicked with
  `interface {} is main.LamJSON, not string`.
- `Result.Ok(<literal>)` is shaped by the declared `Result[T]` payload; a
  `return Result.Ok(["a", "b"])` in a `-> Result[list[str]]` function stored
  `[]interface{}`, so `xs: list[str] = names()?` panicked at runtime.
- `from lamerrors import Result` is no longer reported as unused when `?` or
  `do/catch` (which lower to `Result` helpers) are its only uses.
- `f()?` used as a bare statement no longer emits an unused `__qN.Value`
  expression that Go rejected with "is not used"; the error still propagates.
- Importing several names from the same module on separate `from` lines no
  longer duplicates the module's method signatures, which made single-variant
  methods called with `any` arguments fail as "ambiguous call".
- Library cache hits now rewrite `//line` directives to the library copy
  being compiled; identical copies at different paths previously reported
  Go errors against the path of whichever copy populated the cache.

## 2026-09-25

### Added

- Added top-level, static, and instance overloads distinguished only by return
  type, selected from annotated assignments, enclosing returns, and typed call
  parameters, with deterministic source-order fallback when context is
  insufficient.
- Added stable return-aware Go symbol mangling and cross-library dispatch
  metadata for return-type overloads.
- Added `HttpRequestOptions`, Result-returning `Http.request` / `HttpClient.request`,
  and fluent `HttpRequest` builders with per-request headers, query parameters,
  timeouts, text bodies, and native JSON bodies.
- Added typed Result failure reporting for filesystem, parsing, networking,
  database, crypto, collection, context, actor, and scheduling edge cases.
- Added Base16/Base32 and raw Base64 variants, lazy CSV row/batch generators,
  timezone-aware `DateTime`, duration-unit `Time`, and nested DB predicate groups.
- Added typed cancellable process commands, TCP/TLS/UDP networking, streaming
  file lines/batches, variadic path joining, and canonical `lamdatastructures`
  imports with compatibility re-exports.
- Added a cross-feature integration and stress matrix covering return-type
  overload composition, Result pipelines, stdlib orchestration,
  numeric/data/time workflows, and resilient network/database services.
- Added bound-listener tracking and `HttpServer.port() -> Result[int]` so
  `Http.serve(0, ...)` supports discoverable ephemeral ports.
- Added a website/documentation consistency gate covering deterministic,
  idempotent content generation, SPA routes and heading links, complete stdlib
  and official-package inventories, removed-API guidance, and homepage snippets.

### Changed

- Bumped the compiler feature version to 1.18.0.
- Applied declared return-type context while transpiling static method bodies,
  including typed collection literals.
- Extended contextual overload selection through ternary branches, typed
  collection literals, keyword arguments, generic receivers, and payload-aware
  `Result[T]` dispatch keys.
- Extended generic call-site inference from flat to nested collection literals.
- Extended contextual lowering for native `json(...)` overload arguments and
  typed payloads returned by `Result.unwrap()`, without leaking inferred types
  through blank-identifier assignments.
- Standardized fallible stdlib entry points on unsuffixed `Result[T]` contracts,
  including HTTP/DB, OS/path/exec/net, data formats, config, crypto, templates,
  migrations, collections, concurrency/context/actors, and ID/mail helpers.
- Standardized public TCP and UDP reads on `receive` and `receiveFrom` naming.
- Extended `async func` lowering to instance, static, private, and generic class
  methods; `Channel` and actor `Mailbox` now expose blocking `send` / `receive`,
  awaitable `asyncSend` / `asyncReceive`, and immediate `offer` / `poll` APIs.
- Refactored actor mailboxes, registry locking, replies, stop state, and completion
  to compose `lamconcurrency.Channel`, `Mutex`, `Atomic`, and `WaitGroup` instead
  of declaring a second raw Go concurrency implementation.
- Replaced type-suffixed Env, CLI, and DataFrame/Series entry points with
  overloads; generalized Stats and gonum Array/Matrix inputs across numeric
  types, and overloaded Heap priorities for homogeneous `int` or `float` use.
- Preferred `init` while preserving `__init__` as a compatibility alias, and
  moved cache/cron synchronization to `lamconcurrency.Mutex` to minimize raw Go.
- Consolidated decimal integer, float, and boolean parsing under three
  return-type-overloaded `Conv.parse(str) -> Result[int|float|bool]` signatures;
  compiler payload keys distinguish them despite runtime Result erasure. Radix
  parsers remain explicit because their same `Result[int]` type cannot express
  the semantic difference between hexadecimal, octal, and binary input.
- Refreshed the website from its authoritative documentation, including current
  official-package exports/imports, the complete compiler-bundled stdlib module
  inventory, compatibility-shim labels, deploy-safe third-party links, and a
  concise Vulture hello-world homepage example.

### Fixed

- Made documentation comments require the exact `#- ... -#` delimiters during
  compilation and LSP folding, while allowing `lamc fmt` to repair spaced
  terminators; added workspace-wide lexical and parse coverage.
- Fixed actor panic-stop handling so a dispatch goroutine no longer waits on
  itself and deadlocks a pending `ask` before delivering its failure sentinel.
- Fixed async Result propagation, async ternary returns, reserved-handle name
  collisions, and channel send/close races detected by Go's race detector.
- Fixed SPA heading IDs and cross-document routes by decoding Marked's HTML
  entities before slugging and preserving the adjacent dashes used by Markdown
  heading links, rather than leaking an `amp` encoding artifact into URLs.

### Removed

- Removed obsolete `try*` Result aliases and failure-masking defaults, empty
  values, dropped actor messages, and panic-only fallible variants.
- Removed type-suffixed Data/Env/CLI compatibility methods, type-specific
  `Conv.toInt` / `Conv.toFloat` / `Conv.toBool` parsing names, formatting methods
  from `Conv`, and UUID generation from `Random`; use overloaded primary APIs,
  `Conv.parse`, `Fmt`, and `lamuuid.Uuid` respectively.

## 2026-09-18

### Added

- Added nested scoped package paths such as `@lam/vulture/ws-plugin` across the
  parser, manifest validation, resolver, formatter, installer, and editor grammar.
- Added generic natural and comparator-based sorting, asc/desc validation, five
  named algorithms, generic reverse, and static sorted generators.
- Added native-JSON HTTP overloads, repeated response headers, JSON response
  decoding, and complete `Set-Cookie` attribute parsing through `HttpCookie`.
- Added generic typed DataFrame/Series APIs, safe JSON collection conversions,
  typed environment accessors, and Result-based base-number parsing.

### Changed

- Moved the HTTP server and its standard, WebSocket, and tus plugins into the
  independently versioned `@lam/vulture` package family.
- Replaced additional raw-Go standard-library implementation logic with native
  Lam control flow where the language provides equivalent behavior.
- Extended generic static generator lowering, concrete inline-list inference,
  and overloaded instance-method dispatch inside imported libraries.
- Updated Lamotel payload construction and export to use native JSON end to end.

### Removed

- Removed the compiler-bundled `lamserver`, `lamserver_plugins`, `lamserver_ws`,
  and `lamserver_tus` modules; applications now install their Vulture packages.
- Removed type-specific `Sort.ints` / `strings` / `floats` and `*JsonValue`
  HTTP method names in favor of generic and overloaded primary APIs.

## 2026-09-16

### Added

- Added the `json(value)` builtin and `__json__() -> json` class conversion
  protocol, with explicit conversion diagnostics and LSP support.
- Extended signature-based overloading to instance methods, static methods,
  inheritance, interfaces, imported classes, and defaulted variants.
- Added Result-based JSON, YAML, and XML/XSD validation through overloaded
  `Schema.validate` methods.
- Added optional request schemas and schema formats to the simple `lamserver`
  route helpers.

### Changed

- Bumped the compiler feature version to 1.17.0 and the editor extension to 1.4.0.
- Backed `Request.ctx` with `lamcontext.Context` while preserving `req.ctx[key]`
  syntax through indexing and containment dunders.
- Unified `lamserver` request/response schema validation with the `lamschema`
  compilation cache and validation core.

### Removed

- Removed the legacy `toJson` conversion protocol and the registry/boolean/error
  method family from `lamschema`; schema failures now use `Result` and `Error`.

## 2026-09-13

### Added

- Added `lamcrypto` with secure random values, versioned AES-GCM envelopes,
  PBKDF2-HMAC-SHA256 password storage, Ed25519/RSA PEM key generation and
  signatures, constant-time comparison, and typed X.509 certificate metadata.
- Added the Lam-first `lamcontext.Context` API for cancellation trees,
  unit-aware expiration, inherited values, bounded waits, reasons, and native
  Go context interop.
- Added a searchable official-libraries catalog to the documentation website.

### Changed

- Bumped the compiler feature version to 1.16.0.
- Simplified `lamcontext` naming around `simple`, `cancellable`, `expirable`,
  `valued`, `expired`, and unit-aware `expiresIn` operations.
- Moved JWT, Protocol Buffers, Redis, and Memcached to independently versioned
  `@lam/jwt`, `@lam/protobuf`, `@lam/redis`, and `@lam/memcached` packages,
  preserving their public class APIs under scoped imports.
- Moved each integration's Go-module pins and regression tests into its package.
  Redis and Memcached now own their Docker service fixtures.
- Aligned `@lam/s3` and `@lam/sftp` with Lam 1.16. `@lam/sftp` now consumes
  `@lam/ssh` for host keys and exposes request cancellation through
  `lamcontext`; `@lam/ssh` composes `lamcrypto` and `lamcontext`.

### Removed

- Removed compiler-bundled `lamjwt`, `lamprotobuf`, `lamredis`, and
  `lamemcached`, including their stdlib Go dependency pins and duplicate tests.

### Documentation

- Documented the new crypto/context APIs, scoped-package migration, package
  relationships, installation workflows, and standalone repositories across
  the README, stdlib, package-manager, library-authoring, and website guides.

## 2026-08-27

### Added

- Added a native `json` type backed by canonical recursive JSON values. It
  validates string object keys and JSON-compatible members before Go emission,
  supports chained property/index access and mutation, and accepts classes only
  through `toJson() -> json`.
- Expanded `lamjson.Json` with native decode/encode types, `Result[json]`,
  `fromValue` / `fromDict` / `fromList`, `toDict` / `toList`, and `kind`.
- Added native JSON integration for server request/response bodies, typed HTTP
  client helpers, SQL/JSONB parameters and result columns, Redis values,
  DataFrames, and JSON Schema validation.
- Added semantic, LSP, runtime, HTTP, SQLite, PostgreSQL JSONB, and Redis tests.
- Added compile-time diagnostics for invalid JSON index types, malformed/unused
  `toJson` declarations, invalid literal `Json.decode` input, and malformed
  literal schemas passed to `Schema.register` or `Server.addSchema`.

### Changed

- Fixed multiline nested dictionary literals so the final entry in each nested
  dictionary no longer needs a trailing comma; commas remain required between
  different entries.
- Bumped the compiler version to 1.15.0 and editor extension to 1.2.0.

### Documentation

- Documented nested dictionary literals and final-entry comma behavior.
- Added native JSON syntax, lowering, conversion, HTTP/server, database,
  Redis, DataFrame, schema, and migration guidance across the README, syntax,
  transpilation, stdlib, extension, and website references.

## 2026-08-25

### Added

- Added case-preserving library and module naming across manifests, dependency
  and replacement keys, scoped imports, registries, installation directories,
  lockfiles, LSP module discovery, generated library filenames, and `lamc init`.
  Authors may choose snake_case, camelCase, PascalCase, or
  SCREAMING_SNAKE_CASE and must import the exact spelling.

### Changed

- `lamc init` now preserves directory/requested casing instead of forcing the
  inferred module name to lowercase.
- Bumped the compiler version to 1.14.0.

### Documentation

- Documented case-preserving, case-sensitive library/file naming and audited
  recent compiler changes against their README, docs, extension, changelog, and
  website updates.

## 2026-08-22

### Changed

- Changed the homepage “Tour the language” cards to a responsive three-column,
  two-column, then one-column layout across desktop, tablet, and mobile widths.
- Updated homepage examples to describe eager comprehensions accurately and use
  typed `Result[T]` signatures and tuple-target collection comprehensions.

### Documentation

- Refreshed the project README with current semantic diagnostics, editor
  features, `nonlocal`, typed comprehension, and adversarial stress-test
  coverage.
- Corrected the transpilation guide for validated `nonlocal`, compile-time
  `Result[T]` payload metadata, exhaustive-`match` hoisting, and tuple-target
  comprehension lowering.

## 2026-08-21

### Added

- Added dictionary-destructuring defaults (`key = value` and
  `key: alias = value`) plus shared compiler/LSP diagnostics for missing keys in
  literal dictionaries.
- Added comprehension target arity and propagated element-type diagnostics.
- Added loop-variable capture warnings for lambdas and nested functions.
- Added validated `nonlocal` writes, including missing/ambiguous binding and
  outer-constant diagnostics.
- Added LSP diagnostics for unresolved `LAMMERGEIER.*` paths and preprocess-time
  dictionary-pattern errors.
- Added adversarial compile-and-run and semantic stress fixtures that combine
  nested control flow, comprehensions, generics, overloads, `Result`, `nonlocal`,
  destructuring, contextual names, Go-name mangling, and raw-Go boundaries.

### Changed

- Fixed contextual `match` parsing after ordinary statements while preserving
  `match` as a legal identifier outside statement-keyword context.
- Fixed Go lowering for locals assigned in every exhaustive `match` arm,
  tuple-target list/set/dict comprehensions, nested typed tuple literals, and
  calls to private static methods.
- Changed editor diagnostics to show errors in `# expect-error` fixtures by
  default; matching-fixture suppression is now an explicit LSP setting.
- Updated the VS Code TextMate grammar to scope dynamic `LAMMERGEIER.*` paths in
  Lam and embedded Go, and corrected contextual keyword/type highlighting.
- Bumped the compiler version to 1.13.2 and extension version to 1.1.1.

### Documentation

- Documented dictionary defaults, comprehension target inference, loop captures,
  active `nonlocal` semantics, and compiler/LSP/editor parity.

## 2026-08-20

### Added

- Added package submodule imports such as `from lamwebp.codec import Decoder`
  and deeply nested package modules backed by either `.lam` files or
  `__init__.lam`.
- Added nested-module discovery and export completion to the language server.
- Added import-resolution diagnostics that list every nested and legacy path
  searched for a missing dotted module.
- Added a shared metadata-driven support-import validator for helper-backed
  syntax, including scoped-import coverage and direct-dependency enforcement.
- Added Lam-side `?` operand diagnostics for known non-`Result` values and
  erased `Result[T]` payload annotations for typed propagation signatures.
- Added comprehensive call-shape fixtures for Go-backed stdlib and package
  constructors, static methods, aliases, module-qualified calls, and inferred
  instance methods.
- Added payload-aware member inference for local/imported `Result[T]`
  propagation and `optional[T]`/`Option[T]` safe-navigation receivers, including
  branch joins, direct expressions, fields, methods, and call shapes. Public
  payload-class metadata is resolved without requiring an unused class import.
- Added signature-driven diagnostics and result inference for overloaded binary,
  unary, and comparison operators, including dunder declaration validation.
- Added recursive omitted-type-argument inference for local/imported generic
  functions and constructors, nested containers, repeated type parameters, and
  specialized generic return types.
- Added capture-sensitive and resource-lifetime flow fixtures plus precise
  definite assignment for statically non-empty literal/range loops without
  `break`/`continue`.
- Added return-type coverage for inferred instance methods, ternaries,
  comprehensions, and propagated payloads, plus dropped-value warnings for
  inferred local/imported instance calls.
- Added deterministic `__lamN` emitted-name mangling for colliding top-level
  functions/classes/interfaces, fields/methods, dunder methods, static
  namespaces, and overload-generated wrappers.
- Added ordinary Lam dispatch through `LAMMERGEIER.userFunc()`, class
  constructors, static methods, and static variables while preserving raw-Go
  compatibility.
- Added overload-selected return-shape metadata for local/imported/Go-backed
  destructuring plus list-literal arity and element checks.
- Added `LAMMERGEIER` visibility diagnostics for transitive private functions,
  classes, static methods, and static fields.
- Added cold/warm-cache regressions for overload-selected destructuring metadata
  and real Redis-linked dependency auto-resolution without service probing.
- Added readable mangling for Go-only lexical bindings, private names, and
  compiler-prefix-looking locals across parameters, lambdas, destructuring,
  loops/comprehensions, catch/with bindings, and generated temporaries.

### Changed

- Canonical nested paths now take precedence over legacy module files whose
  filenames contain literal dots; the legacy layout remains a compatibility
  fallback.
- Dotted module names now produce readable, filesystem-safe generated Go
  filenames such as `lib_lamwebp__codec_lam.go`.
- Fixed inferred instance-method handling so bound receivers no longer consume
  the first user argument or bypass arity, keyword, and type checks, and the
  transpiler now fills imported method defaults while preserving user methods
  whose names overlap built-in string dispatch.
- Preserved erased `Result[T]` payload Go types through the versioned library
  cache and emitted concrete assertions for unannotated propagated assignments.
- Prevented `optional[Class]` from lowering to a double Go pointer.
- Hoisted typed locals assigned on every continuing branch or guaranteed loop
  iteration so semantic branch joins no longer emit block-local Go variables.
- Kept `with ... as` resource bindings confined to their lexical lifetime.
- Replaced top-level/member emitted-name collision errors with readable,
  deterministic mangling while keeping preferred Go names unchanged.
- Fixed new destructuring bindings to emit `:=` and list/tuple literal
  destructuring to emit individual Go values.
- Kept optional service availability out of ordinary compilation while Go
  module requirements remain fully compiler-managed.
- Made dictionary-destructure temporary allocation source-name aware.
- Bumped the compiler version to 1.12.0.

### Documentation

- Documented direct submodule imports, package-root re-exports, transitive-only
  bundling, nested path precedence, and generated Go filenames in the syntax,
  transpilation, and third-party library guides.
- Documented direct/scoped support-import rules, non-`Result` propagation
  diagnostics, and the erased `Result[T]` payload marker.
- Documented that Lam wrapper signatures remain authoritative for Go-backed
  stdlib and package implementations, including inferred bound methods.
- Documented Result payload receiver inference, cached Go assertions, optional
  class pointer/member behavior, overloaded operator contracts, and omitted
  generic type-argument inference, branch/loop/resource/capture flow rules, and
  complex return/dropped-value diagnostics, emitted-name mangling, and direct
  `LAMMERGEIER` dispatch, and overload-aware destructuring.

## 2026-08-14

### Added

- Added recursive directory support to `lamc fmt <directory>`, including
  `--check` coverage for every `.lam` file found below the target directory.
- Added formatter regressions for compact block bodies, redundant blank lines,
  semicolon-separated statements, and directory formatting.

### Fixed

- Fixed parsing of compact block statements where a normal statement is
  immediately followed by the closing block brace, such as
  `print(value)}` inside a function body.
- Improved `lamc fmt` canonicalization so compact block bodies are expanded,
  semicolon-separated statements are split, redundant blank lines are
  collapsed, and dict/set literals inside compact blocks are preserved.

### Documentation

- Documented recursive `lamc fmt <directory>` behavior and the syntax
  flexibility around compact closing braces in the docs and website content.

## 2026-08-12

### Fixed

- Fixed compilation of imported Lam modules named `test` or ending in
  `_test`. Bundled library Go files now use a `_lam.go` suffix so Go does not
  treat them as test-only files and exclude them from `go build`.

### Added

- Added compiler regressions for both `from test import ...` layouts:
  sibling `test.lam` files and package-style `test/__init__.lam` modules.

### Documentation

- Documented why bundled Lam library files avoid Go's `*_test.go` filename
  convention in the transpilation guide.

## 2026-08-02

### Added

- Added runtime regression coverage for anonymous collection literals passed
  directly to typed functions, methods, constructors, static methods, keyword
  arguments, defaults, nested list/dict shapes, and typed variadic splats.
- Expanded typed anonymous collection coverage to returns, reassignments,
  indexed writes, list mutations, comprehensions, and lambda expression bodies.
- Added semantic regression coverage for typed collection mismatches that
  should fail in Lammergeier before the generated Go build runs.
- Added deterministic and seeded fuzz-style regression coverage for typed
  collection lowering around `sorted`, `pop`, slice replacement, `map`, and
  typed set literals/comprehensions.
- Added 40 deterministic random-program fuzz cases: 20 emit/build/run programs
  and 20 deeper compile-only programs covering nested anonymous list/dict/set
  values, classes, defaults, lambdas, comprehensions, membership checks,
  chained comparisons, typed ternaries, and typed numeric expressions.
- Added 20 emitted-Go random-program fuzz cases that write `lamc --emit-go`
  output into a fresh Go module, run `gofmt`, and compile it with
  `go test ./...`.
- Added line-shrinking failure output to the deterministic random-program
  fuzz helpers so unexpected emit/build failures include a reduced numbered
  Lam source when the same diagnostic can be preserved.
- Added generated-Go contract tests for typed anonymous call arguments,
  contextual `?` unboxing, and class-pointer argument passing.
- Added static lowering-contract tests that guard typed contexts against
  bypassing `_typed_value_to_go` during future transpiler refactors.
- Added source-mapped Go build error regressions for invalid `go!` blocks in
  both the main file and imported Lam modules.
- Added deterministic driver coverage for `go mod tidy` failures so module
  resolution errors stop before `go build` and keep the underlying diagnostic.
- Added cross-module runtime coverage for typed anonymous lists, dicts, sets,
  defaults, constructors, static methods, and nested typed returns.
- Added semantic regression coverage for non-indexable subscripts, including
  set indexing and slicing non-sliceable containers.
- Added `scripts/test-services.sh` to start the Postgres, Redis, and memcached
  Docker containers required by the full integration test suite.

### Fixed

- Fixed call-site lowering for anonymous list and dict literals so they use the
  declared parameter type instead of defaulting to `[]interface{}` /
  `map[string]interface{}`. Calls such as `sum([4, 8, 15])` now compile when
  `sum` expects `list[int]`, including nested collection literals and imported
  library metadata restored from cache.
- Hardened typed collection lowering outside call sites, including function
  returns, plain reassignments, indexed container assignments, list
  `append`/`extend`/`insert`, typed comprehensions, and explicit lambda returns.
- Added Lam-source semantic diagnostics for incompatible typed collection
  reassignments, indexed writes, list mutations, and explicit lambda returns;
  the existing LSP diagnostic pipeline publishes these without VS Code
  extension changes.
- Fixed additional Go-side type leaks in `sorted`, typed list `pop`, nested
  slice assignment replacement, and `list.map` result typing, including list
  literals used directly as method receivers.
- Fixed typed `set[T]` lowering so declarations, reassignments, call
  arguments, and set comprehensions emit `map[T]bool` instead of leaking
  `interface{}` into generated Go.
- Fixed membership and chained-comparison lowering so valid expressions like
  `2 in nums`, `"k" in table`, `8 in ids`, `"ell" in text`, and `1 < x < 10`
  emit valid Go instead of falling through to raw comparison syntax.
- Fixed single-target iteration over dict/set-shaped maps so loops and
  comprehensions bind keys instead of Go map values; this fixes set
  comprehensions such as `{value for value in ids}`.
- Fixed typed integer/float32 contexts containing `**` so `math.Pow` results
  are cast to the expected Go type instead of failing assignments or returns.
- Fixed annotated ternary assignments so branch expressions are lowered with
  the declared target type, covering anonymous collections and non-`float64`
  `**` expressions inside ternary branches.
- Added Lam-source semantic diagnostics for non-indexable `x[y]` and
  non-sliceable `x[a:b]` expressions so representation leaks such as
  `setValue[item]` no longer fall through to generated-Go errors.
- Fixed semantic typing for string indexing so `s[i]` is treated as a byte-like
  numeric value while `s[a:b]` remains a string; Rosetta-style
  `int(s[i])` and `string(s[i])` casts now pass Lam-side checks.
- Replaced a remaining missing-required-argument transpiler fallback with a
  Lammergeier compiler error, including when semantic checking is disabled.
- Fixed contextual `?` lowering inside typed function-call arguments so
  `Result.Ok(parseInt(s)?)` emits one unboxed `__qN.Value` instead of
  evaluating the propagated expression twice or casting the success value to
  the enclosing `Result` return type.
- Fixed class-pointer argument lowering so typed fields like
  `self.retryPolicy: RetryPolicy` pass directly to constructors, while
  class values read from `any`/unknown fields such as `result.error` still get
  the required Go type assertion.
- Fixed unused-import diagnostics for type-only imports in local annotations,
  such as `from lamerrors import Error` followed by `e: Error = result.error`.
- Report `go mod tidy` failures as Lammergeier module-resolution errors before
  `go build`, preserving the underlying Go diagnostic when stdlib or user
  `go!` dependencies cannot be resolved.
- Aligned the Redis stdlib integration fixture with the documented local auth
  password (`Password` on `localhost:6379`).
- Fixed declaration-AST type rendering for nested generic annotations such as
  `list[dict[str, list[int]]]`, allowing typed IR call-argument contexts to
  preserve the full parameter type instead of collapsing to `list`.

### Changed

- Refactored contextual expression lowering around typed list, dict, and set
  literals into smaller helpers while keeping `_typed_value_to_go` as the
  common type-aware lowering entry point.
- Made function-call lowering lazy for user functions, constructors, and class
  methods so arguments with emit-time side effects, including `?`, are only
  lowered on the final typed path.
- Centralized known-call argument planning for user functions, constructors,
  static methods, and instance methods so contextual typing, keyword
  reordering, and default filling share one path.
- Extended the incremental typed IR so expression nodes can record the
  expected type supplied by annotated, const, and reassignment contexts.
- Added the collection-context and random-program fuzz tests to the standard
  `scripts/test.sh unit` gate.
- Added the new lowering contracts, generated-Go contracts, Go-error mapping,
  and Go dependency diagnostic tests to the standard `scripts/test.sh unit`
  gate.

### Documentation

- Updated the transpilation and syntax docs to describe contextual lowering
  for anonymous list, dict, and set literals/comprehensions, including the
  Lam-side diagnostics that catch mismatches before Go build errors.
- Documented that Lam sets use `value in ids` for membership and cannot be
  indexed even though they lower to Go's `map[K]bool` representation.
- Documented string indexing versus slicing behavior in the syntax and
  transpilation guides.
- Documented module-resolution error reporting, contextual `?` unboxing, and
  class-pointer assertions in the transpilation guide.
- Documented typed-IR expected-type boundaries, generated-Go contract tests,
  source-mapped Go errors, and shrinking fuzz behavior in the transpilation
  guide.
- Documented the local Postgres, Redis, and memcached containers needed for the
  full core integration suite, including the separate memcached auth fixture on
  `11212`.
- Updated the README test workflow to call out that the full gate now includes
  Python fuzz checks.
- Updated the README test workflow to point at `scripts/test-services.sh` for
  disposable Docker service setup.

## 2026-07-31

### Fixed

- Fixed unused-import diagnostics for type-only imports used inside generic
  return annotations and interface method return annotations, such as
  `list[MonsterProjection]` and `-> Rect`.

## 2026-07-29

### Fixed

- Fixed unused-binding diagnostics for mutation targets so assigning through a
  receiver or container, such as `monster.alive = false` or `xs[i] = value`,
  counts as using the receiver/container in both compiler and VS Code LSP
  warnings.

## 2026-07-11

### Added

- Expanded the Lammergeier LSP with richer IntelliSense for instance members,
  inherited fields/methods, `self`, `base`, named inheritance aliases, and
  annotated local variables. Go-to-definition now resolves those members too,
  and semantic/syntax test fixtures with `# expect-error` or
  `# expect-warning` suppress matching expected diagnostics while surfacing
  expectation mismatches clearly.
- Added read-only LSP import suggestions for stdlib and workspace exports,
  module-name completion after `from`, workspace symbol search, and richer
  signature help for methods and constructors. Suggested imports do not edit
  source automatically, method signatures hide implicit `self`, and
  constructors are displayed with `init(...)` sugar.
- Fixed VS Code folding by removing brace marker folding that could collapse
  ranges from the wrong line and visually truncate collapsed blocks.
- Fixed LSP semantic diagnostics for imports used only in type annotations or
  class bases, and for inherited fields on imported parent classes such as
  `self.stats` in `Player(Entity)`.
- Added `lamc version` / `lamc --version` to print the compiler version used
  when evaluating library `compatibility.lamc` ranges.
- Added `lamc lib run <script>` for running commands declared in a project's
  `lamlib.toml` `[scripts]` table, with `--list`, `--cwd`, `--dry-run`, and
  `--quiet` support.
- Added git dependency write-back: explicit git installs now update
  `lamlib.toml` with `{ git = "...", ref = "..." }`, bare `lamc install`
  consumes that form, and lockfiles retain both the requested ref and the
  resolved commit.
- Added a guarded GitHub integration test for
  `https://github.com/thallium-solutions/lams3.git` that verifies git install
  write-back, frozen/offline replay from cache, and external lams3 import
  transpilation when `LAMC_LIVE_GITHUB_LAMS3=1` is set.
- Added richer `lamc doctor` diagnostics, including `--json` output,
  `--strict` CI gating, project-manifest discovery, cache stats, PATH details,
  selected Go environment values, Python requirement checks, and editor
  extension inspection.
- Added Lam-source semantic errors when `?` or `do/catch` are used without the
  required `from lamerrors import Result` support import, avoiding generated-Go
  `undefined: Result` failures.
- Added semantic call-shape/type diagnostics for directly imported functions
  from resolved Lam modules, including `from helper import f` and
  `import helper; helper.f(...)`, so missing arguments, bad keywords, and
  simple argument type mismatches are reported before Go emission.
- Added semantic metadata for directly and module-qualified imported Lam
  classes, so constructor calls, static method calls, instance-method misuse,
  and simple constructor type mismatches are validated at the Lam source
  location.
- Added conservative inferred receiver types for plain assignments from known
  constructors, static factory methods, and imported functions, so misspelled
  members on unannotated imported Lam values are reported before Go emission.
- Added semantic expression diagnostics for common binary operator mismatches,
  list/string/dict index type mistakes, and non-integer slice bounds.
- Added dropped-return warnings for non-void imported functions and imported
  static methods, including module-qualified calls.
- Added semantic name diagnostics for top-level functions, classes, and
  interfaces that lower to the same Go symbol after export-name casing.
- Added `go!` boundary diagnostics for `self` and the receiver alias `s` used
  outside instance methods.
- Added branch-sensitive definite-assignment diagnostics for local reads before
  assignment, `if`/`else` joins, self-referential first assignments, and
  conservative loop boundaries.
- Added branch-sensitive `match` capture scopes so pattern bindings are only
  visible in their own case arm and do not leak after the match.
- Added conservative `try`/`catch`/`else` definite-assignment joins so values
  assigned only on some continuing exception paths are reported before Go
  emission.
- Added destructuring arity diagnostics for tuple literals and local functions
  annotated with `tuple[...]` return types.
- Added alias-specific diagnostics for `from lamerrors import Result as ...`
  used with `?` or `do/catch`, which require the unaliased `Result` helper
  import today.
- Added comparison and membership type diagnostics for incompatible ordered
  comparisons, equality checks, `in` item types, and invalid `in` containers.
- Added unary and boolean operator type diagnostics, including `not`, `and`,
  `or`, unary `+`/`-`, and bitwise `~` operands.
- Added ternary expression type diagnostics for non-boolean conditions,
  incompatible branch values, and annotated assignment/return inference.
- Added comprehension filter and annotated container literal diagnostics,
  including list elements, dict keys/values, set elements, and const literals.
- Added built-in cast diagnostics for invalid cast arity, keyword arguments,
  and non-numeric arguments to numeric/bool casts.
- Added explicit generic-call diagnostics for type-argument arity and concrete
  type-parameter substitutions in function and class constructor calls.
- Allowed Go-only keywords such as `select` in Lam declarations that lower to
  safe Go names, including public functions, classes, class fields, static
  methods, and public instance methods.
- Added semantic diagnostics for parsed-but-unsupported decorators, including a
  specific `@private` hint to use the supported `private func` spelling.
- Added inheritance diagnostics for incompatible method overrides,
  instance/static override mismatches, duplicate direct bases, field/method
  conflicts, and ambiguous direct promoted members from multiple inheritance.
- Added interface conformance diagnostics for method parameter type mismatches.
- Extended class/interface diagnostics to compare nested generic method
  parameter and return types, including imported interfaces/classes.
- Extended inheritance diagnostics through transitive and imported base
  hierarchies.
- Added a non-fatal warning for classes with multiple distinct base classes,
  since multiple inheritance is parsed but not fully supported yet.
- Added module-boundary visibility diagnostics for importing private functions,
  calling module-qualified private functions, and calling private methods on
  imported classes.
- Added tuple/destructuring diagnostics for annotated element type mismatches,
  annotation arity mismatches, duplicate destructuring targets, `-> (T, U)`
  multi-return arity, and imported tuple-returning functions/static/instance
  methods.
- Improved destructuring type inference so each tuple target receives its own
  element type instead of the whole tuple type.
- Added specialized dropped `Result`/`Option` warnings that tell callers to
  handle the value or assign it to `_`.
- Added conservative branch-join inference for local variable types across
  `if`/`else`, `match`, and `try`/`catch`, so misspelled members after branches
  are reported when every continuing path agrees on the receiver class.
- Extended Go-reserved identifier diagnostics to import aliases, function and
  lambda parameters, loop/comprehension targets, catch bindings, and
  destructuring targets.
- Added warnings for user-declared names with compiler-reserved prefixes such
  as `__lam*` and `__q*`.
- Added standalone `third_party/lams3/CHANGELOG.md`, `LICENSE`, and `NOTICE`
  files so lams3 can be distributed from its external repository.
- Added explicit inheritance parent aliases: single unnamed parents expose
  `base`, named bases use `class Child(alias: Parent)`, and
  `base.init(...)` / `alias.__init__(...)` initialize the embedded parent via
  the parent constructor.

### Changed

- Updated lams3 package metadata to advertise Apache-2.0 licensing.
- Compiles now warn, without blocking, when an installed extlib's
  `compatibility.lamc` range does not include the running compiler version.
- Refactored `lamc fmt` / LSP document formatting to preserve inline `go!(...)`,
  format `go! { ... }` blocks with `gofmt` when available, and keep Lam source
  in the repository's K&R/four-space style more reliably.
- Updated roadmap and diagnostics documentation to reflect that unused local
  warnings and their Go silencing are implemented for function and block
  scopes.
- Corrected inheritance transpilation documentation to remove unsupported
  `super()` lowering and describe current parent embedding, constructor, and
  inherited-method behavior.
- Multiple inheritance is now supported through Go embedding with named parent
  aliases, ambiguity diagnostics for unqualified inherited members, and
  inheritance-cycle errors.

## 2026-07-10

### Added

- Expanded `third_party/lams3` with Result-returning S3 operations, including
  text/bytes object I/O, buffer upload/download, stream upload, file
  upload/download, object stat/head metadata, copy, move, key-only listing,
  bulk delete, config validation, and presigned GET/PUT URL helpers.
- Added lams3 offline coverage for `Result`/`do-catch` config handling and
  direct configuration without environment variables, plus presigned URL
  generation and a live R2/S3 roundtrip that uses a dedicated
  `lams3-tests/live-roundtrip/` prefix.

### Changed

- Updated lams3 AWS SDK for Go v2 pins, including
  `github.com/aws/aws-sdk-go-v2/service/s3 v1.105.0`.
- Simplified lams3 environment configuration to use `S3_BUCKET` instead of
  separate public/private bucket variables.
- Updated lams3 documentation to cover direct constructors, Result-first APIs,
  file/buffer/stream workflows, stat metadata, copy/move, bulk delete, and
  presigned URL usage.

## 2026-05-09

### Added

- Expanded `third_party/lams3` offline coverage for environment configuration,
  readiness checks, static and client-level public URL generation, URL encoding
  variants, and `S3Object` defaults.
- Added a more complete `third_party/lams3` README covering configuration,
  usage, API reference, offline tests, and optional live S3-compatible
  round-trip testing.

### Changed

- Updated the `lams3` package test runner to discover every `offline_*.lam`
  regression case automatically.

## 2026-05-07

### Added

- Lam-facing syntax diagnostics that render parser failures with source
  snippets, expected Lam constructs, and targeted repair hints.
- Import-resolution diagnostics for direct missing modules, missing imported
  symbols, typo suggestions, searched paths, and package `__init__.lam`
  resolution.
- Semantic diagnostics for local call-shape errors, including missing required
  arguments, too many positional arguments, unknown or duplicate keyword
  arguments, and positional/keyword duplicates.
- Semantic return-flow diagnostics for functions that omit required return
  values or return values from `-> None` functions.
- Conservative known-class member diagnostics for unknown `self.member`,
  `Class.member`, static field, and static method references, with suggestions
  when a close member exists.
- Import binding diagnostics for duplicate aliases, aliases that shadow
  builtins, and imports that conflict with existing functions, classes,
  interfaces, or constants.
- Structural interface conformance diagnostics for known local classes at
  typed assignments and function-call boundaries, covering missing methods,
  obvious method arity mismatches, and return annotation mismatches.
- Constructor call-shape diagnostics for known local classes, validating
  `init` / `__init__` arguments and zero-argument classes before Go emission.
- Syntax repair hints for C-style logical operators, pointing `!`, `&&`, and
  `||` users toward Lammergeier's `not`, `and`, and `or` forms.
- Syntax repair hints for common wrong function declaration keywords such as
  `function` and `fn`, pointing users to Lammergeier's `func`.
- Semantic diagnostics for misplaced `self`, class-qualified instance method
  calls, and static methods called through known instances.
- Deterministic undefined-name suggestions that prefer Lam keywords, builtins,
  and stdlib modules before falling back to in-scope variable/function names.
- Semantic warnings for likely receiver declaration mistakes, including methods
  that use `self` without declaring it, constructors missing `self`, and static
  methods that still accept `self`.
- Semantic diagnostics for unknown members on known typed instances, close
  type-annotation typos, and obvious literal return-type mismatches.
- Clearer semantic diagnostic rendering with explicit `error[kind]` /
  `warning[kind]` tags and modest ANSI color in interactive terminals.
- Conservative non-void return-path diagnostics for functions with missing
  paths through simple `if` / `match` / `try` control flow.
- Import diagnostics now warn on unused top-level imports and suggest close
  exported symbol names when a direct `from module import Name` typo can be
  resolved from the imported Lam module.
- Match diagnostics now warn on duplicate unguarded literal `case` patterns
  and cases made unreachable by an earlier wildcard `_`.
- LSP diagnostics now publish semantic warnings as editor warnings instead of
  suppressing advisory diagnostics.
- LSP preprocessing now mirrors the compiler's dict-destructuring rewrite, so
  valid `{key, other: alias} = expr` syntax no longer appears as a parse error
  in editors.

### Tests

- Added focused syntax, semantic, and import-resolution diagnostic test suites
  covering negative and positive/fallback cases for the new Lam-side error
  messages.
- Extended semantic diagnostic tests with warning expectations so advisory
  messages are verified separately from hard errors.
- Added coverage for missing return paths, unused import warnings, and import
  export typo suggestions, plus full normal runtime regression validation.
- Added coverage for duplicate `match` cases, wildcard-before-case warnings,
  and guarded-case fallbacks.
- Added LSP coverage for semantic warning severities and verified the VS Code
  extension TypeScript build.
- Added LSP regression coverage for valid dict destructuring syntax.
- Added standalone `lams3` third-party library tests for offline URL helpers
  and optional live S3-compatible upload/read/list/delete round-trips.

## 2026-05-05

### Changed

- Renamed primitive string method aliases to match `lamstrings.Strings` static
  method names exactly. For example, `"hi".toUpper()` now mirrors
  `Strings.toUpper("hi")`; Python-style aliases such as `.upper()`, `.strip()`,
  `.lstrip()`, and `.find()` were replaced by names such as `.toUpper()`,
  `.trim()`, `.trimLeft()`, and `.index()`.
- Refactored built-in string method dispatch to call the stdlib
  `lamstrings.lam` implementation instead of emitting inline Go `strings.*`
  calls.
- Refined `LAMMERGEIER.*` resolution to use the dynamic dispatcher for
  user-defined symbols while retaining only `None` and `nil` as literal aliases.
- Removed legacy `.tpy` file support.

### Added

- Auto-injection of `lamstrings` when built-in string methods are detected, so
  simple string method calls no longer require an explicit import.
- `Strings.format`, backed by a variadic `fmt.Sprintf` wrapper and used by
  `.format()`.
- Import aliasing with `as`.
- Ignored-return-value handling through assignment to `_`.
- Postfix `++` and `--` operators, including overload support.
- Multiline f-strings.
- Same-arity function overloading by parameter type.
- Compiler warning when the `?` operator is used outside a `Result`-returning
  function.
- Frozen stdlib Go-module pins documentation and a pinned
  `modernc.org/sqlite v1.50.0` dependency.

### Documentation

- Updated `docs/SYNTAX.md` and `docs/TRANSPILATION.md` to describe the
  string-method naming model, `lamstrings` auto-injection, and the current
  Lam-to-Go lowering behavior.

## 2026-05-03

### Added

- Static variables in classes.
- Apache License 2.0 licensing metadata and NOTICE file.

### Fixed

- Documentation references and miscellaneous fixes.

## 2026-05-02

### Added

- Third-party package manager support through `lamc install`, `lamc uninstall`,
  `lamc publish`, `lamc tidy`, `lamc verify`, and project scaffolding helpers.
- SemVer and API-diff enforcement for published Lammergeier libraries.
- Transitive dependency resolution for both Lammergeier libraries and Go
  modules.
- Registry server implementation under `tools/registry/`.
- Vendor-style installs with deterministic lockfiles.
- Content-addressed cache support for tarballs and git repositories.
- Lockfile v1 metadata including `requested_by` arrays and `tree_sha256` for
  git/path sources.
- `--frozen` and `--offline` package-manager modes for reproducible CI and
  container builds.
- `--global` installs for one-off user-global library installs.
- `[replace]` directives for local overrides of transitive dependencies.

### Documentation

- Reorganized the README quick start and package-manager sections.
- Added `docs/installation.md` with installer flags, prerequisites, editor
  wiring, upgrade flow, uninstall steps, and troubleshooting.
- Added package authoring and registry documentation in
  `docs/package_manager.md` and `docs/third_party_libraries.md`.
- Documented accepted-but-unused grammar forms in `docs/SYNTAX.md` and
  `docs/TRANSPILATION.md`.

### Fixed

- Website fixes and documentation cleanup.
- Additional test coverage for package-manager and compiler behavior.

## 2026-05-01

### Added

- LSP server and editor extension refactors for VS Code, Cursor, and Windsurf.
- LSP symbol collection for top-level variable assignments, scope-aware local
  bindings, and type annotation details.
- Blank identifier `_` for discarding values.
- Multiline lambda bodies with explicit return types.
- `lamdata` DataFrame, Series, and DataFrameGroups stdlib module with
  pandas-style operations.

### Changed

- Moved `SYNTAX.md` and `TRANSPILATION.md` into the `docs/` directory and
  updated references across the repository.
- Improved cross-library class/static-method dispatch by extending compiler
  cache keys and harvesting pre-class/static-method metadata.
- Documented default output-path behavior: without `-o`, binaries are emitted
  beside the source file with the `.lam` suffix removed.
- Clarified block-scoped `try`/`catch` recovery behavior.
- Updated `.gitignore` to exclude common compiled binaries generated from
  tests and examples.

## 2026-04-30

### Added

- Three-layer library resolution: bundled stdlib, external libraries, then the
  project layer.
- `--extlibs` CLI flag and `LAMC_EXTLIBS` environment variable.
- `lamc migrate` subcommand while preserving legacy `lamc <source>` behavior.
- `lamcron` scheduler and `lamsmtp` email client documentation.
- Set literals and comprehensions.
- Tuple and generator comprehensions.
- Expanded f-string expression support, including safe navigation and
  null-coalescing.
- Augmented assignment operators.
- `assert` statements.
- `global` and `nonlocal` declarations.
- Nested function closures.
- Python-style slicing with negative indices and strides.
- Dictionary destructuring.
- Named and keyword arguments with validation.

### Documentation

- Rewrote `CONTRIBUTING.md` with repository layout, compiler pipeline,
  workflow, testing discipline, stdlib authoring guidance, parser/transpiler
  change notes, documentation expectations, and pull request checklist.
- Added compiler documentation covering build caching, the `LAMMERGEIER.*`
  namespace, syntax flexibility, diagnostics, parser caching, and semantic
  checker integration with the LSP.

## 2026-04-29

### Added

- `HttpClient` with base URL, headers, timeout, and GET/POST/PUT/PATCH/DELETE
  helpers.
- `HttpResponse` wrapper.
- `JwtKeySet` with JWKS loading, `kid`-based verification routing, and
  `kid`-tagged signing methods.
- Inline-block semicolon insertion, empty-block pass filling, and runaway
  semicolon collapsing.
- `lamserver` lifecycle hooks, decorators, content-type parsers, versioned
  routes, pagination helpers, and OpenAPI generation.
- Cross-library default-argument filling and chained-call class inference using
  enhanced library cache metadata.
- Multiline expression continuation with lookahead for leading operators.
- `go!` block return-statement rewriting and `self` reference translation.

## 2026-04-28

### Documentation

- Added the comprehensive standard library reference in `docs/stdlib.md`.
- Added the `lamserver` plugin authoring guide in `docs/server_plugins.md`.
- Expanded the syntax reference module inventory.

## 2026-04-27

### Added

- Advanced `lamserver` features: query and cookie helpers, HTML responses,
  redirects, cookie helpers, error responses, `onError` hooks, static file
  serving, route prefixes, CORS support, 405 responses with `Allow` headers,
  and graceful shutdown.
- Server tests and README documentation for LSP extension setup.

## 2026-04-26

### Added

- Language Server Protocol server with diagnostics, hover, completion,
  goto-definition, and document symbols.
- `Array` and `Matrix` numerics backed by Gonum.
- `Server` web framework.
- `Iter` combinators.
- `LruCache` and `TtlCache`.
- RFC 4122 UUID v4/v7 generation.
- Constants.
- Function types.
- `Result`-based error handling with the `?` propagation operator.
- `do { } catch { }` blocks.

## 2026-04-25

### Added

- Semantic checker for undefined names, duplicate class members, and misplaced
  flow statements.
- `--no-semantic-check` compiler flag.
- Semantic checker test suite.

## 2026-04-24

### Added

- Comprehensive README and project documentation.
- `docs/TRANSPILATION.md` with Lammergeier-to-Go lowering rules.
- Documentation for multiline expressions, `defer`, `isinstance`, safe
  navigation operators, tuple destructuring, and generics.
- Library cache system with source maps.
- Transpilation-output regression test suite.
- Project icon and initial build-artifact ignore rules.

## 2026-04-23

### Added

- Initial Lammergeier Lang repository structure.
- Compiler, grammar, standard library, examples, tests, website, and editor
  support directories.
