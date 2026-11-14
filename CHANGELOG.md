# Changelog

All notable changes to Lammergeier Lang are documented here.

Releases are tagged with plain versions (`2.0.0`); older entries are grouped
by commit date.

## 4.13.5 — 2026-11-14

### Fixed

Found by the fuzzer, each with a regression test:

- **Evaluated once, in Python's order:** the middle operand of a chained
  comparison (`a < f() <= b`) was evaluated twice. The left operand of
  `in` / `not in` was evaluated once per element of the container, or never
  for an empty one.
- `isinstance(e, ValueError)` of a caught built-in exception was always
  `false`. It now tests the exception's class with the same families as
  `catch` (`LookupError` covers `IndexError`...).

### New diagnostics

Each of these programs passed the checker and then failed in `go build`:

- a `with` resource without a `close()` callable with no argument;
- a built-in exception class used as a value (`[ValueError]`);
- `await` of a call that is not `async`;
- a NUL byte in the source;
- calling a constant (`false(20, 10)`).

A value alone on a line (a name, a literal, `(-5)`, `n + 1`) is a warning
("has no effect"); the program builds.

### Known limitations

- A dict literal right after `else`, or as the first element of a set,
  reads as a block: parenthesise it (docs/SYNTAX.md, "Ternary Operators").
- A constant-folded `-0.0` prints `0`, because Go constants have no negative
  zero.

### Safety checks

- **Fuzzer:**
  - its Python twin evaluates an async call's argument at the call;
  - an uncaught exception of the program's own class must end the Lam
    program with `panic: <its __str__>`.

## 4.13.4 — 2026-11-14

### Changed

- **`int ** int` is an exact `int`** (it was always a `float` computed with
  `math.Pow`, so `print(41 ** 4)` showed `2.825761e+06`, and the checker
  gave `**` no type: `s: str = 2 ** b` passed it and failed in `go build`).
  Two ints give an int that wraps on overflow like every int operation; a
  float operand still gives a float. **Breaking:** an int power with a
  negative exponent raises `ValueError` (use a float base: `2.0 ** -1`);
  constant powers that overflow 64 bits or have a negative exponent are
  compile-time errors.

### Fixed

Found by the fuzzer, the documentation examples test or the int power
change, each with a regression test:

- a `for` loop over a slice of a comprehension or over a list
  concatenation left its variable untyped, so a dict literal or lambda
  using it did not build;
- `n -= x ** 4` on an `int` kept the float power;
- a comprehension over `range()` inside a `json` literal was reported as
  an "unknown" value;
- an unused local named like a local of a generated helper (`out`, `src`,
  `v`) lost the `_ = name` that keeps Go from rejecting it;
- an f-string float format spec of an `int` (`f"{n:.1f}"`) printed Go's
  `%!f(int=23)` instead of `23.0`.

### Safety checks

- **Documentation examples are tests** (`tests/tests/test_doc_examples.py`,
  in the unit suite): every Lam block of `docs/` and the README is built,
  and the 34 with an `# Output:` section are run and compared. Blocks marked
  `fragment` are skipped, and `error` blocks must be rejected with their
  `# error:` messages (CONTRIBUTING.md). Several examples were fixed (a
  `lamdb` example querying columns its table lacked, missing imports, `#`
  comments in `go!` blocks).
- **Canary builds** (`sh scripts/test.sh canaries`): Luxyt,
  managed-file-transfer and every `lam-*` package are built with the
  working-tree compiler, leaving those repositories untouched.
- **Race detector** (`sh scripts/race.sh`, `run_tests.py --race`): runs core
  tests built with `-race`; the whole core suite runs clean.
- **Fuzzer:** an uncaught exception of the Python twin the safety contract
  maps (`ZeroDivisionError`, `IndexError`, ...) must be raised by the Lam
  program too, after the same output (`panic: <Exception>`, status 2). A twin
  that doesn't finish is discarded, and shrinking keeps the original error.

## 4.13.3 — 2026-11-13

### Safety (docs/SAFETY.md)

- **The contract:** a program the checker accepts builds, the compiler never
  crashes on input, and run-time faults are Lam exceptions. The new
  `docs/SAFETY.md` describes the guarantees, the run-time exceptions, the
  deliberate semantics (64-bit wrapping ints, a missing typed-dict key read as
  the zero value, floats as IEEE 754) and the limits (`go!` blocks, stack
  overflow, concurrent dict writes, out of memory).
- **Internal compiler errors instead of tracebacks** (`compiler/ice.py`): any
  crash inside `lamc` is reported as `internal compiler error` with the Lam
  location and a link to report it (exit status 70; `LAMC_TRACEBACK=1` adds
  the Python traceback). Invalid UTF-8 and nesting beyond the compiler's depth
  are Lam errors. A Go build failure of generated code is an internal compiler
  error at the Lam line the Go error maps to; a Go error inside a `go!` block
  stays the user's Go build error.
- **Run-time faults are Lam exceptions with the Lam line:** index out of
  range → `IndexError`, integer division by zero → `ZeroDivisionError`,
  `int()` of NaN → `ValueError`, of an infinite or out-of-range float →
  `OverflowError`, a failed conversion → `TypeError` naming Lam types,
  writing to a `None` dict → `AttributeError`, a negative run-time shift
  count → `ValueError`; they reach `await` and generator loops. An uncaught
  exception prints `panic: <Exception>: <message> (at file.lam:LINE)` and the
  Lam frames (exit status 2); `LAM_TRACEBACK=go` keeps Go's report.
  **Possibly breaking:** code that compared Go's panic texts sees the Lam
  messages.
- **Compile-time errors for what Go would reject:**
  - constant expressions that overflow `int`, `float` or `byte`, including
    integer literals of any length;
  - negative constant shift counts;
  - duplicate constant keys in a dict literal;
  - a program without `func main()`;
  - nested list, dict or set literals of the wrong kind (`x: list[int] =
    [[[]]]`).
- **Fixed:**
  - a dict comprehension used without an annotated variable (indexed,
    iterated, passed to a function) was a `map[interface{}]interface{}`, so
    `{f"k{x}": x for x in xs}["k1"]` into an `int` did not build;
  - iterating a dict comprehension inside a list comprehension gave its
    values instead of its keys;
  - a list comprehension over a generator used directly (`[g for g in
    gen(3)][1]`, in arithmetic, with `.filter()`) was a `[]interface{}`;
  - the counter of a comprehension over `range()` was untyped, so
    `[max(7, i) for i in range(4)]` and dict literals holding it did not
    build.
- **Fuzzing** (`tests/fuzz/lamfuzz.py`, `sh scripts/fuzz.sh`): well-typed
  generated programs checked against a Python twin, mutations of them and
  hostile inputs; failures are shrunk to regression tests.
  `tests/tests/test_compiler_crash_guard.py` and
  `tests/tests/test_runtime_panics.py` run in the gate.

## 4.13.2 — 2026-11-13

### Fixed

- A static member lowers to the Go function / variable `<Class>_<member>`,
  so a class `Site_make` next to the static method `Site.make`, a function
  `site_list()` (Go `Site_list`) next to `Site.list`, a const or variable
  `Site_count` next to the static field `Site.count`, or two statics such
  as `A_b.c` and `A.b_c` failed in the Go build (`Site_make redeclared in
  this block`, or the link-time clash diagnostic across modules). As for
  constructors (4.13.1) the other definition keeps its name and the static
  becomes `Site_make__static` (the first static in module / class / member
  order keeps a contested name); a const or variable gives way instead
  (module-qualified Go name). The decision is program-wide (part of every
  module's namespace plan and cache key); Lam code, method values,
  `LAMMERGEIER.Site.make()` and raw `go!` code of a module that does not
  see the other definition (`Site_make()`) keep working.
- Classes of one module whose names differ only in the case of their first
  letter (`site`, `Site`) next to a class `NewSite` both got the
  constructor `NewSite__ctor` (`NewSite__ctor redeclared in this block`):
  the 4.13.1 constructor renaming did not know that `site` is the Go type
  `Site__lam2` (constructor `NewSite__lam2`). Only the constructor of
  `Site` is renamed now, and a renamed constructor never takes a name
  another one took.
- A class with a field and a method of one name (`self.entries` in `init`
  or a class-body `entries` field, and `func entries(self)`) failed in the
  Go build (`*Catalog does not implement Listing (Entries is a field, not
  a method)`, `cannot call c.Entries`): it is a semantic error at the later
  declaration naming both lines, ``duplicate class member `entries`: class
  `Catalog` has a field `entries` (line 3) and a method `entries` (line 6);
  a field and a method cannot share a name — rename one of them``. The
  inherited conflicts (a subclass method named like a base field and the
  reverse) are reported at the subclass's declaration with the same hint,
  and a missing interface method that is a field (own or inherited) says
  so. A static method may still share its name with an instance field.
- An instance method or field of a subclass spelt like its direct base
  class (`func shape(self)` in `class Square(Shape)`, Go `Shape`, the
  embedded parent) failed in the Go build; it is a semantic error now.
- A private function, const, variable, local or parameter named like one
  of the compiler's runtime helpers (`lamNN`, `lamAs`, `lamGenRun`, ...)
  redeclared it in the Go package; it is a semantic error
  (``error[name]: `lamNN` is the Go name of a compiler runtime helper``).
- A parameterless method `error(self) -> str` / `string(self) -> str`
  (Go `Error()` / `String()`) made the class a Go `error` / `fmt.Stringer`:
  `print(obj)`, f-strings, lists and the message of a thrown object used
  that method, and `error()` even won over `__str__`. Such a class gets the
  `Format` method hierarchy classes already had, so only `__str__` /
  `__repr__` define its text (the stdlib's `DataFrame.error()` included).
- A static field read through its class (`Money.currency`) in a json
  literal or `json(...)` was ``value of type `unknown` is not
  JSON-compatible``; it has its declared type.
- An overloaded operator whose left operand is a call result
  (`make(1) + make(2)`, `Money.of(1) + Money.of(2)`, `a.plus() + b`), a list
  element or a `?` payload failed in the Go build (`operator + not defined
  on Money_of(1)`); it dispatches to `__add__` & co. like a variable.
- `min([4, 1, 9])` / `max([...])` of a list literal and `set([3, 1, 2])`
  emitted the literal as a `[]interface{}` and failed in the Go build;
  `sorted(set(xs))` / `len(set(xs))` did not know `set(...)` is a set.
- `catch Notifiable as n` with an interface bound `n` as an untyped value,
  so `n.notice()` failed in the Go build; `n` is the interface.
- `list(...)`, `dict(...)` and `tuple(...)` (Python conversions Lam does
  not have) failed in the Go build with `undefined: List`; they are
  semantic errors naming the Lam spelling. `print(..., end="")` /
  `sep=` dropped the keyword silently; `print` keyword arguments are a
  semantic error.

### Tests

- Regression tests for each fix above (core, semantic, transpilation and
  `tests/tests/test_go_names.py` unit tests of the namespace planning), and
  new coverage tests pinning current behaviour: the collection builtins,
  `for ... else` on every loop shape, typed catch families and interfaces,
  generator loops over every source kind, match class-pattern diagnostics,
  f-string format verbs, statics in service shapes, lamdatetime / lamenv,
  fire-and-forget async tasks, the hot-loop Go lowerings (snapshot) and a
  cross-feature shop service fixture (classes, statics, interfaces,
  generics, Result / `?`, do/catch, try/finally, generators, `with`, async,
  f-strings, html, lamjson, lamdatetime, lamenv, lamdb over sqlite).

## 4.13.1 — 2026-11-12

### Fixed

- A member chain through a value typed by an interface that is a field
  (`self.db: DbConn`), an element of a list (`self.conns[0]`, `conns[0]`) or
  a loop / comprehension variable over a `list[DbConn]` keeps the
  interface's declared return types: `self.db.table("t").whereRaw("x")`
  fills `whereRaw`'s default argument (it failed to build with `not enough
  arguments in call to ...WhereRaw`), `.count().unwrap()` is an `int`, and
  `for row in self.db.query(q, [])? { ... }` / `rows = self.db.query(q)?` /
  `self.db.first(q)?` / `self.db.scalar(q)?` have the payload types of the
  interface methods (it failed with `cannot range over ... (variable of type
  interface{})`). The same for user-defined interfaces (a method with a
  default argument, a method returning `Result[list[T]]` iterated after
  `?`, a method returning the interface itself).
- A class `Site` and a class (or interface) `NewSite` — or a function
  `newSite()` — in one module or in different modules of a program no
  longer clash in the Go build (`NewSite redeclared in this block`, or the
  link-time `Go func NewSite is declared by modules ...` error): the Go
  constructor of `Site`, normally `NewSite`, is `NewSite__ctor` when
  `NewSite` names another definition; a const or variable named like a
  constructor gets a module-qualified Go name instead. Lam code is
  unchanged (constructors, statics, subclasses, generics, `isinstance`,
  imports); raw `go!` code of a module that does not see the other
  `NewSite` keeps calling `NewSite(...)`, and `LAMMERGEIER.Site(...)` is the
  constructor everywhere. A user class `NewError` or `NewResult` next to
  lamerrors' `Error` / `Result` works the same way (the stdlib's `go!` code
  follows). The import-resolution test of the former link-clash
  diagnostic for a constructor next to a same-named function of another
  module now checks that the program builds and runs.

## 4.13.0 — 2026-11-11

### Changed

- **Renamed:** `Row.optionalStr` / `optionalInt` / `optionalFloat` /
  `optionalBool` (`lamdb`) are `Row.nullableStr` / `nullableInt` /
  `nullableFloat` / `nullableBool`: the language has no optional type since
  4.12, and the functions return an `any` (the value, or `None` for SQL
  NULL). The old names are an error suggesting the new one (``unknown member
  `Row.optionalStr` on class `Row` — did you mean `Row.nullableStr`?``).
  `Row.str(row, key, fallback)` & co. read a NULL as their fallback, and
  `Row.isNull` tells NULL apart.

## 4.12.0 — 2026-11-11

### Removed

- **Potentially breaking:** the optional type and safe navigation are
  removed from the language. `?` is for errors; `None` is an ordinary
  value that references can hold, and checking for it is the program's
  duty. Writing `optional[T]` / `Option[T]` anywhere (annotations,
  generic arguments, casts, `Result[optional[T]]`, `list[optional[T]]`...)
  is a compile error with the migration:

  ```
  error[type]: `optional[Shop]` was removed: write `Shop` (a class
  reference can be `None`; test `x != None` before using it)
  error[type]: `optional[str]` was removed: a `str` cannot be `None`;
  return a `Result[str]` (an error when there is no value), use a default
  value, or `any`
  ```

  For a class, an interface, `html`, `xml`, `json` or `any` payload write
  the type itself (`optional[Shop]` -> `Shop`, `Result[optional[html]]` ->
  `Result[html]`); for a value payload (`str`, numbers, `bool`, `bytes`,
  lists, dicts, sets, tuples) return a `Result[T]`, use a default or `any`
  (a markup attribute `role: optional[str] = "alert" if failed else None`
  becomes `role = "alert" if failed else None`). `obj?.field` /
  `obj?.method()` on anything but a `Result` is a compile error
  (``?.` was removed: test `obj != None` first (`None.field` throws), or
  use `?` on a `Result```); `f()?.m()` on a `Result` is still propagation
  then a member. Removed with them: the boxing of value payloads (`*string`,
  `*int`), the `optional[T]` bridging of generic code with class type
  arguments and its override adapters, the `Box[optional[T]]` error,
  optional equality, optional narrowing of value types, `str()` / `print`
  of optionals and of `list[optional[str]]`.
- `Row.optionalStr` / `optionalInt` / `optionalFloat` / `optionalBool`
  (`lamdb`) return `any` (the value, or `None` for NULL: `Row.optionalStr(row,
  "x") ?? ""`), `Row.time` a `DateTime` (`None` for NULL).

### Changed

- What can be `None`: class references (also of generic classes),
  interface values, `html`, `xml` (now: `h: html = None`, `return None` in
  a `-> html`, `h == None`, `h ?? fallback`; a `None` fragment renders
  nothing), `json` (null), `any` and `Result` payloads. Value types never
  hold `None`: a literal `None` assigned, passed, returned, as a default,
  in a ternary branch or in `Result.Ok(None)` for a `Result[str]` is a
  compile error; a dynamic `None` converted to a value type (an `any`, a
  `Result` payload built from one, a `dict[str, any]` lookup, a JSON null,
  `return None` in a `-> T` with `T = int`) throws a catchable `TypeError:
  None is not a str` instead of Go's `interface conversion` panic.
- A member of `None` throws `AttributeError: None has no attribute 'name'`:
  a field read or write, a method call (also of a method that never uses
  `self`, which used to run on the nil receiver), an interface method. It
  is an exception like the `TypeError` of a `None` conversion: `try` /
  `catch AttributeError` catches it; a `do` / `catch` handler, which handles
  the errors `?` propagates from a `Result`, does not (the exception goes on
  to the enclosing `try`); uncaught it ends the program with exit status 2.
  The check is an inlined nil test, skipped for `self`, constructor calls,
  locals only assigned constructor calls and locals narrowed by `!= None`
  (or `isinstance`): the runtime benchmarks are unchanged within noise, and
  the new `none_checks` one (a linked list walked through a reassigned
  local, three checked members per node) takes 1.1 ns per node instead of
  0.9 ns.
- `x if c else None` with `x` a value type is an `any` (it was an
  `optional[str]`, an error in a new unannotated variable); a `None`
  markup attribute value still omits the attribute.
- `??`: `load()? ?? "none"` with `load() -> Result[str]` is a `str` (the
  payload stays dynamic until `??` tests it); `??` on a value type, never
  `None`, is a warning (``warning[type]: `name` is a `str`, never `None`:
  `?? "anon"` has no effect``).

## 4.11.0 — 2026-11-10

### Changed

- **Potentially breaking:** the checker types safe navigation as optional.
  `obj?.field` is an `optional[F]` and `obj?.method(args)` an
  `optional[R]` (`F` / `R` the declared field type / result; an
  `optional[R]` result does not nest, `any` and `json` stay as they are, a
  void method is a statement), `None` when `obj` is; a chain
  `a?.b?.c()` is optional as a whole. It used to be typed as the plain
  member, so `takes(s?.title())` with `takes(x: str)` passed the check and
  handed `None` to `x`, `x = s?.title(); x.toUpper()` passed, and
  `c = s?.title(); if c == None` was rejected (`cannot compare str and
  None`). Now the optional rules apply: passing, assigning or returning it
  where its payload type is expected is an error with a hint, and so is
  its payload's methods and operators without a `None` test or a default.
  Code passing `?.` results to non-optional parameters needs a fix:

  ```
  error[type]: argument `x` to `takes` has type `optional[str]`, expected
  `str`; `s?.title()` is `None` when `s` is `None`: give a default
  (`s?.title() ?? ...`) or test `s != None` and use `s.title()`
  ```

  `takes(s?.title() ?? "")`, or `t = s?.title()` then `if t != None {
  takes(t) }` (a local assigned from `?.` narrows like any optional).
  `load()?.title()` on a `Result` is unaffected (propagation, then a plain
  call).
- `obj?.method(args)` on a receiver of known type (a class, an interface,
  an optional class, a generic class) lowers to that optional instead of
  an `interface{}`: `func() *string { if __lamR := obj; __lamR != nil {
  __lamV := __lamR.Title(); return &__lamV }; return nil }()` (the class
  pointer for a class result, `*[]T` for a list, `*T` for a type
  parameter), so `x = obj?.title()` is a `*string` local like `x:
  optional[str] = ...`, `??`, narrowing and further `?.` stay typed, and the
  call goes through the plain method-call lowering (typed arguments,
  inherited methods). Output is unchanged (`print` / f-strings / `str()`
  show the value or `<nil>`).

### Fixed

- A void method called through `?.` (`job?.cancel()`) no longer fails to
  build (`__lamR.Cancel() (no value) used as value`).
- `x == "abc"` / `x == y` with `x: optional[str]` (or another optional of
  a comparable value) compares the values (`false` when `x` is `None`, true
  for two `None`); it failed to build (`mismatched types *string and
  untyped string`) or compared the pointers.
- `str(x)` of an `optional[str]` (or `optional[int]`, ...) is the value or
  `<nil>`, like `print`, instead of the pointer's address (`0xc000014070`).
- `x.toUpper()` on an `optional[str]` local, parameter or field (and other
  `str`, `bytes`, list, dict and set methods on an optional of them) is a
  Lam error asking for a `None` test, `??` or `?.`; it failed only in the
  Go build (`x.ToUpper undefined (type *string has no field or method
  ToUpper)`). `x?.toUpper()` on an `optional[str]` works (it failed the same
  way).
- `x if x != None else "?"` where a `str` is expected (an argument, an
  annotated variable, `list.append`) narrows `x` in the branch as the
  untyped ternary already did, instead of reporting an `optional[str]`.
- The type hint of a call of an inherited method (`p.label()` with
  `label` declared in a base class) is the base's declared result.
- `optional[T]` of a generic class or function with `T` a class or an
  interface builds and behaves like any optional: `Shelf[User].at(i) ->
  optional[T]` failed with `cannot use s.At(0) (value of type **User) as
  *User value`, because generic code holds `optional[T]` as a Go `*T`
  (`**User`) while an `optional[User]` is the class pointer itself (and an
  `optional[Named]` the interface value). Results, parameters, fields
  (read, assigned, `None`), `list[optional[T]]` / `dict[K, optional[T]]`
  values and fields (appended to, indexed, popped), `Result[optional[T]]`
  through `?`, generic functions (`first(xs)`, `orElse(x, d)`, which also
  bound `T` to the class instead of its reference), a subclass of
  `Shelf[User]` using the inherited field, and an override `-> optional[User]`
  (which became a second overload, so calls through a `Shelf[User]` ran the
  base) now convert at the boundary. A list or dict of such optionals read
  outside generic code is a converted copy. `-> Box[optional[T]]` with `T` a
  class or interface (two generic class types Go cannot convert) is now an
  `error[type]` at the call instead of a Go build error.
- A class instance (or an `optional` of it) fits an `optional[Named]`
  variable, field, parameter or `optional[T]` with `T = Named` (`cannot
  assign P to optional[Named]`).
- `print`, f-strings and `str()` of a `list[optional[str]]` (or of another
  optional value) show the values or `<nil>` instead of the pointers.

## 4.10.0 — 2026-11-09

### Added

- `g.close()` on any generator value (`generator[T]` / `iterator[T]`: a
  variable, a field, a parameter, a call result), like Python's
  `generator.close()`: a body suspended in a `try` / `with` runs its pending
  `finally` blocks and `with` closes, an exception raised there reaches the
  caller of `close()`, the generator then yields nothing more and is not
  closed again when collected; closing an exhausted, unstarted or closed
  generator does nothing. Also for generators kept on the coroutine
  lowering. `close()` arriving while the body computes a value (from the
  body itself — a `finally` closing its object, which closes the generator —
  or from another task) ends the generator once that value is produced
  instead of deadlocking. Any other method of a generator is a compile error
  (`` `generator[int]` has no method `stop` — did you mean `close`? ``).
- Warning at `self.items = self.produce()` / `o.items = o.produce()` when
  the method generator's `finally` (or a `with` resource) uses `self`:
  dropping the object before the generator ends loses that code; call
  `self.items.close()` first (no warning when a method of the class closes
  that field, typically the object's own `close()`).

### Changed

- An f-string whose slots are all `str`, `int` or `bool` values (and
  `html` / `xml` / `json`, as in 4.9.0) without format specs is a string
  concatenation instead of `fmt.Sprintf`, which boxed every value into an
  `interface{}` and copied the text through fmt's buffer:
  `f"{entry}_{size}.avif"` with `entry: str, size: str` is
  `entry + "_" + size + ".avif"`, one allocation of the exact size. A slot
  whose type is declared (a parameter, an annotated local or global, a
  const, a field, a static field, a call of a function or method declared
  `-> str`, `str(x)`, `str` methods, slices, `+`, ternaries) is used as is;
  an `int` / `bool` one goes through `strconv`; a slot that is a `str` only
  by inference goes through `lamSlotText`, a generic helper that returns a
  string without allocating and formats anything else as `%v` did. Other
  slots (floats, optionals, `?.` calls, generics, `any`) keep
  `fmt.Sprintf`. The text is the same (a differential test compares both
  lowerings over awkward values). `str(x)` of a declared `str` / `int` /
  `bool` follows the same rule. A str-only f-string benchmark: 499 to
  146 ns, 400 to 256 bytes per iteration (Go's concatenation: 150-165 ns).
  Luxyt: the generated Go has 404 `fmt.Sprintf` calls instead of 928, a
  JSON API products request allocates 772 kB instead of 799 kB (a feed
  fragment 511 kB instead of 522 kB), with no `fmt.Sprintf` left on that
  path.

### Fixed

- A generator whose loop body uses a variable of that loop (or a local of
  its body) only in an f-string slot or a `go!` block after a `yield` no
  longer fails to build with `undefined: n` (4.9.0): the continuation after
  that `yield` was created once per loop, outside the scope of the loop
  variable, because the check deciding it skipped f-string slots and `go!`
  blocks. And a continuation ending with a loop no longer fails with
  `missing return` when a local before that loop is unused (its silencer
  went after the loop instead of before it). A differential fuzz test
  (`tests/tests/test_generator_lowering_fuzz.py`) compares random generator
  bodies with their coroutine lowering.
- A method generator stored in a field of its own object, suspended in a
  `try` whose `finally` uses `self` (`self.items = self.produce()` with
  `finally { self.release() }`), is no longer leaked with its object when the
  object is dropped: such generators read their receiver from the frame
  (`lamNewMethodGenerator`), and while a field of the receiver's object holds
  the generator (`lamGenStore`, at every store of a generator into a field)
  its collection cleanup holds the object only weakly. Both are collected;
  the `finally`, which needs the object, cannot run and is reported on
  stderr (`Exception ignored in generator Feed.produce: its finally uses the
  object holding it, collected with it, so it did not run; call close()
  before dropping the object`). Collected while its object is alive (the
  field reassigned) such a generator still runs its `finally`.

- `x = obj?.method()` declared `x` with the method's declared result type
  (the type hint ignored that `?.` calls give an `interface{}`): a `str`
  result was then asserted (`x.(string)`), which panicked when `obj` was
  `None`, and `x ?? "default"` returned `x` (`<nil>`) instead of the
  fallback. `x` is now an `any` holding the result or `None`, `??` falls
  back, and `obj` is evaluated once (`make()?.name()` called `make` twice).
- An f-string without slots doubled its `%` signs (`f"100%"` printed
  `100%%`).

## 4.9.0 — 2026-11-09

### Changed

- json values are encoded directly: `Json.encode`, `str(doc)`, f-strings,
  markup slots, `MarshalJSON` (so `encoding/json`, HTTP frameworks) and
  database parameters write the `encoding/json` bytes (sorted keys, `<` `>`
  `&` and U+2028/U+2029 escaped, invalid UTF-8 as `\ufffd`, its number
  format) into one pooled buffer, then allocate the text once, instead of
  `encoding/json` reflecting over the tree, encoding a json value twice
  (`MarshalJSON` and its caller) and copying the result. Values it would
  treat specially go through `encoding/json` unchanged, so bytes and errors
  never differ (a differential test checks 2,100 random nested values,
  class instances and invalid ones against the 4.8.0 code). Encoding a
  50-product API response: about 4.5x faster, 4.4x less allocated.
- json values are shared, not copied, like Python objects (and like
  `doc.key = value` already did): a json value put into a json literal,
  returned by `__json__` and converted with `json(x)`, or converted again is
  the same value, so nesting documents (`{"data": items}`, a class's
  `__json__` using its parts' `__json__`) costs nothing per level; it used
  to deep-copy the whole subtree at every level. Lam collections are still
  converted (copied). New `Json.copy(doc)` makes an independent copy. A json
  literal's own object or array is normalized in place (`lamJSONLiteral`),
  not copied.
- `str(x)` of an `html`, `xml` or `json` value is the value's own text
  (`lamValueText`: its `String()`, rendered or encoded once) instead of
  `fmt.Sprintf("%v", x)`, which copied the page again; an f-string whose
  slots are all `str` / `html` / `xml` / `json` without format specs, one of
  them not a `str`, is a string concatenation (`f"<!doctype html>{page}"`).
  The text is the same.
- `x in xs` on a list of strings, numbers or booleans compares with `==`
  instead of `reflect.DeepEqual` (which boxed both sides on every
  comparison): `HtmlPolicy.allowTags` and every `name in names` loop stop
  allocating.
- `Html.sanitize` compiles a policy once: the `HtmlPolicy` keeps its
  compiled form, and a process-wide cache (64 entries) serves a policy built
  again with the same content, so `Html.sanitize(text, HtmlPolicy.rich())`
  per call no longer rebuilds the lookup tables (62 kB to 6 kB per call for
  a rich policy). A policy that is no longer changed can be shared between
  goroutines; changing its fields after a call takes effect on the next.
  Sanitizing itself allocates less (no per-element maps or intermediate
  node lists, URLs without control characters are not copied).
- `Re` compiles each pattern once (a cache shared by goroutines, bounded to
  512 patterns): `Re.match(pattern, text)` no longer compiles the pattern on
  every call (4.8 kB to under 256 bytes per match).
- `Db.first`, `scalar`, `firstJson` and `scalarJson` (and the `Tx` ones)
  stop reading at the first row and close the rows, instead of reading and
  keeping the whole result set (a 200,000-row query: 86 MB to under 200 kB
  for `first`). `firstJson` decodes only that row. `Db.textArray` no longer
  builds a `strings.Replacer` per element; rows are scanned through one
  reused buffer into pre-sized maps; placeholders are rewritten with one
  allocation (none without `?`) and parameters are passed without a copy.
- `Schema` validation looks its compiled schema up before parsing the
  schema text, which it parsed again on every validation.
- Markup slots holding an `int` are written with `strconv` instead of
  `fmt.Sprintf`.

### Fixed

- A generator kept in a field of the object it uses still leaked: an
  object holding a partially iterated generator of its own method
  (`self.items = self.produce()`), or a generator closure capturing the
  object holding it, was never collected once dropped — the suspended
  generator's goroutine referenced the object, which referenced the
  generator — nor were its `finally` blocks run (1,000 such objects: 1,000
  goroutines and 1,000 objects kept forever). Generator bodies are now
  compiled to state machines: a suspended generator is data held by its
  handle, not a goroutine, so the cycle is collected and the pending
  `finally` blocks and `with` closes of the state it was suspended in run
  when it is. Every construct may hold a `yield` (nested loops with
  `break` / `continue` / `else`, `if`, `match`, `try` / `catch` / `else` /
  `finally`, `with`, `do` / `catch`, `yield from`); a `yield` in a `do`
  body no longer fails to build. A body using `defer`, a `yield` as a
  value, or a `?` in a `try` nested in a `do` holding a `yield` keeps the
  previous lowering, with a warning. Resuming a generator value is about
  5.7x faster (`bench_generator_resume`, new: 94 to 16 ns per value), the
  generator benchmark about 2.3x (`bench_generator`: 33 to 14 ns per
  value); starting and stopping one costs a little more
  (`bench_generator_break`: 186 to 238 ns per loop of 10 values).

- `Db.scalar` (and `tx.scalar`) returned a random column of the first row
  when the query selected several (it took the first member of the row's
  map); it is the first column in select order.

## 4.8.0 — 2026-11-09

### Added

- `Os.modTime(path) -> Result[int]`: a file's last modification time in
  Unix nanoseconds, to tell a changed file without reading it.

### Fixed

- Generators leaked: a loop leaving a generator early (`break`, `return`,
  `?`, an exception), a generator never iterated or one dropped after a
  partial loop left its goroutine blocked forever with everything it
  captured (1,000 broken loops: 1,000 goroutines), its `finally` blocks
  never ran, and an exception raised in a generator crashed the program
  instead of reaching the consumer's `try`. Generators now follow Python:
  the body runs lazily, in step with the consumer; a loop over a generator
  call ends it when the loop is left (its `finally` blocks and `with`
  closes run before the code after the loop); a generator in a variable is
  resumable and is ended when collected; its exceptions are raised in the
  consumer. `generator[T]` lowers to a generator handle (`*lamGenerator[T]`)
  instead of `chan T`; a loop over a generator call runs the body in place
  (no goroutine: the generator benchmark is about 3.5x faster, a broken loop
  over an endless generator about 15x). `yield from src` (in the grammar,
  but rejected or mis-lowered) yields each value of `src`.
- `with` closed its resources when the enclosing function returned (a Go
  `defer`), not when its block ended: in a loop every resource stayed open
  until the function ended, closing in reverse after all the later code. It
  now closes at the end of the block on every path (`return` with its
  value, `break` / `continue`, `?`, exceptions), several items in reverse
  order; two items without `as` in one block no longer fail to build, and a
  `with` block returning on every path ends the function (no "not all
  paths return a value" error).
- Async task failures accumulated: the exception of a task never awaited
  stayed registered until `main` returned (never, in a server), and one
  raised after the task had published its result was registered and never
  removed. A failure is now reported (`Task exception was never retrieved`)
  when its task handle is collected, or at the end of `main` for the
  handles still alive, and a late one is reported at once.
- `Csv.rows` / `Csv.batches` / `streamRows` / `streamBatches` close the file
  when the consumer leaves its loop early.

### Changed

- Go 1.24 or newer is required (range-over-func loops, `iter.Pull`,
  `runtime.AddCleanup`); `lamc doctor` checks it.

- Composing `html` / `xml` is much cheaper: inserting a value (a slot,
  `append`, `setInner`, `replaceWith`, `html(x)`, `clone()`) shares its
  elements copy-on-write instead of copying the whole tree, so a page built
  from nested components no longer copies every card once per level of
  layout; the parts of a literal without slots are built once and shared;
  rendering sizes its result once and writes it in one allocation; the
  compiled-literal cache no longer builds a key string per call. Behaviour
  is unchanged: output byte for byte, value semantics in both directions
  (changing the inserted value or the value it was inserted in never shows
  in the other), live views, queries over composed values, and values
  shared between concurrent tasks (rendering takes a read lock on shared
  parts, a change swaps them for a copy first). A 50-card page in four
  layout levels (`bench_markup_compose`): 914 µs and 1.66 MB allocated per
  page before, about 160 µs and 197 kB after; a real server's product page
  went from 2.1 MB to 0.57 MB allocated per request.

### Tests

- `markup/test_markup_embed_isolation.lam`, `test_markup_composed_queries.lam`,
  `test_markup_concurrent_sharing.lam` and the allocation regression check
  `test_markup_embed_alloc.lam`; `tests/tests/test_markup_race.py` runs the
  composition cases under the Go race detector. The runtime benchmarks accept
  an optional bytes-per-operation field (`alloc/op` column).

## 4.7.0 — 2026-11-09

### Added

- `lamc build --workers N` (also `lamc <file>`, `--run`, `lamc dev
  --workers N`, `LAMC_WORKERS`): the per-module work of a build — parse
  preprocessing and parsing, the semantic check of the imported modules,
  the library transpiles — runs in `N` worker processes. Default: the CPUs
  the process may use, at most 16; `--workers 1` keeps everything in the
  `lamc` process. Results are used in module order, so the generated Go,
  the warnings and the errors (the first syntax error in module order) are
  the same for every `N`, under `fork` and `spawn`. Pools start only for
  enough work (a small program stays in-process). `--timings` adds
  `cache_load`, `workers` (waiting for them) and `workers_ms` (their time per
  phase).
- The compiler cache keeps every module's front end (preprocessed source,
  parse tree, import facts) and metadata harvest by content, next to the
  library transpiles and clean checks; a tree is loaded only when a pass
  needs it. A rebuild after editing one module parses only that module,
  re-checks it and its importers and transpiles it (every library when its
  signatures changed). `--no-cache` skips all of it.
- The compiler cache has a size cap: `LAMC_CACHE_MAX_SIZE` (`500MB`,
  `5GB`, `2GiB`; `0` = unlimited; default `2GB`). After a build, at most
  once every 10 minutes per cache directory, the entries used least
  recently (every cache hit marks an entry used) are removed down to 80% of
  the cap: library entries, parse trees, module metadata, check markers,
  the parser and the Go work directories. Only the compiler's own entries
  under the cache directory are removed; entries a build is writing and Go
  work directories in use are skipped. Pruning 50,000 entries takes under a
  second; a build that does not prune is not slower. The 4.6.0 cache was
  never pruned (169 GB on one machine).
- `lamc cache info` (size per kind of entry), `lamc cache prune
  [--max-size SIZE]` and `lamc cache clear` (as `--clear-cache`); `lamc
  doctor` shows the cap.
- Each program builds in a Go work directory of its own under the cache
  (`gobuild/`, locked while a build uses it), so `go build` reuses its build
  cache (an unchanged program links from Go's cache), and `go mod init/tidy`
  run again only when the Go imports, pins or Go environment changed.

### Changed

- Faster builds. A 230-module application (170 project modules, the stdlib
  and 7 installed packages; 32 CPUs) builds in 18 s from an empty cache
  (4.6.0: 96 s), 3.6 s unchanged (34 s), 7.4 s after editing one module's
  body (36 s; 4 s of it `go build`) and 11 s after changing its signatures
  (133 s). The large generated benchmark program's preprocessing drops
  from 18 s to under 1 s.
- A library's cache entry stores its metadata tables as a delta against the
  program metadata injected into every library (every entry used to repeat
  the whole program's, megabytes each, decoded and merged once per library
  at every build), merged in the same order and with the same values.

### Fixed

- The parse preprocessing classified every `{` against the whole text
  before it (quadratic: 18 s for a 10,000-line file); braces are classified
  only for empty blocks, with the same result.
- The library cache keys depended on whether the Lark parser had just been
  built or was loaded from its cache (tree labels print differently), so the
  build after a cold one transpiled every library again and the entries of
  cold builds were never reused.
- Module lookups and path resolution are memoized for a build (they ran
  hundreds of thousands of `realpath` calls), the namespace metadata view
  scans each table once instead of once per name, and the content digests
  of the semantic-check keys are computed once per module.
- The persisted Lark parser is written under a unique temporary name, so
  concurrent `lamc` processes cannot clobber each other's write.

## 4.6.0 — 2026-11-08

### Added

- `lamc dev` caches its good builds in `$LAMC_CACHE/dev/<project>/<entry>/`
  (default `~/.lammergeier/cache/dev`) and, at start, runs the last good
  build of unchanged inputs instead of building again: `[lamc dev] starting
  serve (cached build of 2026-11-08 09:12:40)`. A container whose home is a
  persistent volume no longer waits for a full build at every `docker
  compose up`. The inputs are the `.lam` and `lamlib*.toml` files under the
  watched roots, every module the build read elsewhere (stdlib, `extlibs/`,
  `LAMC_EXTLIBS`, recorded by the compiler with the search paths that must
  stay missing, so a new file shadowing a module builds again), the compiler
  (version, location, sources), `go version`, the build flags and the Go
  build environment (`GOFLAGS`, `GOOS`, `GOARCH`, `CGO_ENABLED`, ...). The
  last three builds per entry point are kept; entries are written atomically
  and checked (size, SHA-256) before running, so a damaged one is rebuilt;
  two `lamc dev` processes of the same entry point take turns at start
  (the second starts the first one's build).
- `lamc dev --rebuild` builds at start even when a cached build matches
  (`--no-cache` implies it).
- `lamdb.DbConn`: the query surface a `Db` (the pool or a `withContext`
  view) and a `Tx` share — `exec`, `query`, `first`, `scalar`, `queryJson`,
  `firstJson`, `scalarJson`, `table`, `transaction` and `pool` — so a service
  taking a `DbConn` runs inside or outside a transaction unchanged.
- `Db.atomic(conn, fn: func(Tx) -> Result[T]) -> Result[T]`: typed
  transactions returning the callback's own `Result`. On a `Db` it commits on
  `Ok`, rolls back on `Err` (returned unchanged) or a throw (re-thrown), and
  returns a begin or commit failure as a `DbTransactionError`; on a `Tx` it
  runs the callback in a savepoint (`SAVEPOINT lam_sp_<depth>`, released on
  `Ok`, rolled back to on `Err` or a throw), so units nest and compose — on
  PostgreSQL a failing statement inside one no longer aborts the outer
  transaction. Inside a nested level `tx.commit()` / `tx.rollback()` end that
  level only; `tx.depth()` counts the open levels. The savepoint statements
  are observed like the others, with the transaction's context.
- `tx.transaction(fn)`: `Db.transaction` nested in a savepoint (`Ok(false)`
  when the callback rolled its level back).
- `pool()` on `Db` and `Tx`: the owning pool of a `withContext` view (itself
  for a pool) and of the `Db` that began a transaction, to hand a `Db` to
  libraries that need one (`@lam/jobs`).
- `Db.transaction(fn: any) -> Result[bool]` keeps its signature and
  behaviour (`Ok(false)` still means the callback rolled back itself, and a
  callback without a result is still accepted); `Db.atomic` is the typed
  form.
- Inline casts `expr as T`: the run-time conversion of an annotated
  assignment `value: T = expr` (same helper, same `TypeError` on a
  mismatch: int to float widening, a subclass instance as its base, `None`
  as a class, typed lists / dicts from `list[any]` / `dict[str, any]`,
  `optional[T]`, `any`), usable in place with the static type `T`:
  `row["count"] as int + 1`, `(lookup("user") as User).name`,
  `for x in data["items"] as list[str] { ... }`, `take(v as float)`. `as`
  binds tighter than every binary operator and looser than unary operators,
  `**`, calls, indexing and member access (`a + b as int` is
  `a + (b as int)`, `-x as float` is `(-x) as float`); a union target needs
  parentheses (`v as (int | str)`). From a concrete type a cast is the
  identity, a widening or an upcast, a downcast of a base-class or interface
  value checked at run time like an `isinstance` narrowing (`TypeError` for
  another class, `None` stays `None`), and otherwise a compile error that
  points to the conversions (``cannot cast `str` to `int` with `as`: ... use
  `int(...)` ``). `import x as y`, `from m import a as b`, `catch E as e`,
  `with r as f` and `case p as name` keep their meaning.
- Operations that need a concrete type on an `any` value are Lam errors
  (kind `any`) at the operand, instead of `interface{}` errors from
  `go build`: members and method calls (`row["name"].toUpper()`,
  `lookup("user").name`), arithmetic, bitwise, unary and ordering operators,
  augmented assignments, indexing and slicing (and an `any` index into a
  typed list or dict), iteration, unpacking, item and member writes, calls,
  the right operand of `in`, conditions (`if`, `while`, ternary, `not`,
  `and` / `or`) and `len`, `reversed`, `sorted`, `enumerate`, `zip`,
  `list`, `set`, `dict`, `tuple`, `range`, `abs`, `min`, `max`. The message
  names the expression and the fixes: an inline cast with a suggested type
  (`row["count"] as int`), an annotation, a conversion (`int(x)`) or an
  `isinstance` / `match` narrowing. `==` / `!=`, `is None`, `??`, `str()`,
  `print`, f-strings, `isinstance`, `match`, an `any` needle of `in`, and
  passing / assigning it where an `any` or a typed value is expected stay
  allowed; `json` values and `r.error` of a `Result` are unchanged.
- Syntax diagnostics for a cast without a type (`v as`, `v as 3`), `lamc fmt`
  spacing around `as`, language-server hover and completion for members of
  `(expr as T).` and of locals assigned `expr as T`, and VS Code / Linguist
  highlighting of `as` with its target type (extension 1.5.0).

### Changed

- `lamc dev` says why the start takes time: `[lamc dev] building serve.lam
  (no cached build; the first build can take a minute or two)`.
- Constructs that used to compile on an `any` value by an implicit
  assumption are now `any` errors with the cast fix-it: `reversed(v)` (a
  reflection copy; `reversed` was documented for lists and strs only),
  `min(v, 2)` / `max(v, ...)` (a run-time `float` conversion of the
  operand), `x in v` with an `any` container (a reflection lookup), and an
  operator on the payload of `?` on a `Result[any]` in a typed context
  (`x: int = anyVal()? * 2` asserted the payload to the target's type, so
  `x: float = anyVal()? * 2` failed on an int): write
  `anyVal()? as int * 2`.

### Fixed

- A call through an interface-typed receiver lost its declared result type
  (`unknown`): `conn.scalarJson(q).unwrap()` or `src.doc(k)?` assigned to a
  `json` local was rejected ("value of type `unknown` is not JSON-compatible")
  and a wrong annotation (`s: str = src.count(k).unwrap()` with `count`
  returning `Result[int]`) went unnoticed. Interface methods now give their
  declared result, also for imported interfaces.
- A type parameter that appears only inside `Result[T]` (Go cannot infer
  it) was not bound from a nested function passed by name or from an
  unannotated lambda (`Db.atomic(db, work)` with a nested `work`,
  `Runner.run(ctx, lambda c: add(c, 5))`): the build failed with `cannot
  infer T`. Nested functions now carry their Lam signature, and an
  expression lambda takes the declared result of the function, static
  method or receiver method its body calls.
- `value = load()?` on a `Result[any]` and `v = same(a, b)` with a type
  parameter bound to `any`, followed by `t: str = value`, did not build
  (`cannot use value (variable of type interface{}) as string`): such locals
  are now `any` locals converted like the others.
- A list or dict literal with an empty `[]` / `{}` item next to typed ones
  (`for m in [{"k": 1}, {}]`) is typed by its other items in the semantic
  checker too (`list[dict[str, int]]`), as the lowering already built it.

## 4.5.2 — 2026-11-08

### Fixed

- A module of an import cycle could not be built as the program entry
  (`lamc pkg/scope.lam` with `pkg/conn.lam` importing `.scope`): the entry
  was bundled again as a library (`Scope redeclared in this block`), and the
  modules importing it back did not know its classes and statics
  (`Scope.Of undefined`). The entry is now emitted once and its declarations
  are harvested for the modules importing it back.
- The result of a call through a function value — a function-typed
  parameter, local or field (also of an imported class), a collection
  element or a returned function — lost its declared type when the value
  returns `Result[T]`: `len(fn(x).unwrap())`, `fn(x)?[0]`, `for v in
  fn(x)?`, `fn(x).unwrap().name` did not build (`interface{}`). The payload
  is now asserted like a declared function's, and calls through fields and
  collection elements have the value's declared result type.
- `lamc fmt` wrote `n ?? -1` as `n ?? - 1` and `xs ?? []` as `xs ??[]`
  (also `n ??(a)`): the `?` ending `??` was not seen as an operator. A sign
  after an operator or keyword now sticks to its operand (a spaced `x = - 1`
  becomes `x = -1`), a literal or `(` after `??` keeps its space, and the
  postfix `?` (`r? - 1`, `f()?[0]`) is unchanged.
- In an import cycle (`pkg/b.lam` importing `.c`, which imports `.b`), a
  module collected its own classes a second time through the other module,
  doubling their method signatures: a single-variant call with an `any`
  argument (`Scope.kind(err)` with `err: any`, also instance methods) was
  reported as an `ambiguous call ... for dynamic argument types`. A module's
  own classes are now only collected from the module itself; a real
  overload called with an `any` argument stays ambiguous.
- A static method (generic or not, of a plain or generic class) with a
  result and a `try` in its body did not build (`not enough return
  values`): unlike functions and instance methods it lacked the named
  `retval` result(s) the `try` closure stores a `return` in, so a `throw`
  in the `catch`, a `return` inside the `try` and multi-value returns
  failed. Static methods now lower their `try` like functions.
- A `private static func` used as a function value (`Hooks.run(Hooks.double)`)
  did not build (`undefined: Hooks_double`): the value named the exported
  Go function while the definition is unexported (`hooks_double`). The
  value now uses the name its calls use.
- A static or instance method call whose declared result is a value type
  was not boxed where an `optional` of it is expected (`return
  Hooks.page()` in a `-> optional[html]`: `cannot use ... LamHTML as
  *LamHTML`): `html` / `xml` results of static and instance methods and
  every value result of a method called on `self` (`return self.name()` in
  a `-> optional[str]`) are now boxed like module function calls.
  `optional[json]` did not work at all (a `json` value or JSON literal
  returned or assigned there panicked or did not build, and `opt ?? {...}`
  produced a Go map): it is now a boxed `*LamJSON` like other value types.
- A static method used as a function value had only the root of each
  parameter type (`apply(Handlers.count)` with `count(where: str, items:
  list[Item])` was a `func(str, list) -> int`), so it was rejected where a
  `func(str, list[Item]) -> int` (or a `dict[str, Item]`, `set[int]`,
  `optional[Item]` parameter) is expected. It now has the declared types in
  full. Calling a function value read from a collection element
  (`fns[0]("a", [Item(1)])`) also types its literal arguments by the
  value's parameter types (it passed a `[]interface{}`).
- `lamtest` assertions called as statements (`Test.assertTrue(...)`,
  `Test.assertEqual(...)`, ..., `Test.summary()`) were warned about as
  dropped return values, so test suites wrote `_ = Test.assertTrue(...)`.
  They are statements by design: their result (whether the check passed,
  the failure count) stays available but no longer warns when unused,
  however `Test` is imported; a program's own `Test` class still warns.
- `lamc dev` rebuilt and restarted the program when a watched file was
  touched or rewritten with the same content (`lamc install --frozen`
  re-copying `extlibs/`, an editor saving an unchanged buffer): changes were
  detected by modification time and size only. A file whose modification
  time or size changed is now hashed and compared with its previous
  content; after the first scan only files whose stat changed are read.
- `lamlib.lock.toml` recorded a dependency declared in `lamlib.toml` and
  also required by another dependency with only one of its requesters
  (`requested_by = ["@lam/vulture/dev-plugin@1.0.0"]` without `"root"`, or
  `["root"]` alone, depending on the declaration order). `requested_by` now
  lists every requester, re-installs keep them, and requesters no longer
  pinned are dropped (also by `lamc uninstall`). `lamc tree` renders such a
  shared pin under each requester instead of marking it `(cycle)`, and
  `lamc why` follows scoped requesters (`@lam/vulture@1.0.0`) instead of
  printing `(no requester recorded)`.
- `lamc uninstall` rewrote `lamlib.lock.toml` without its `[go_pins]`
  table, dropping every Go-module pin of the project. It now keeps them,
  pruning only the Go pins left without a requester. `[go_pins.*]`
  `requested_by` also listed only the library whose version won; it now
  lists every requester (libraries declaring the module and `"root"` for
  the project's `[go-deps]`), sorted and deduplicated.
- `install.sh --prefix DIR` with a missing `DIR` (and `--user` without
  `~/.local`) fell back to `sudo mkdir`, failing without a terminal: only
  the immediate parent of the bin directory was checked for write access.
  Missing directories are now created without sudo when the nearest
  existing ancestor is writable.
- A type parameter only the result of a generic call mentions
  (`make[T]() -> Result[T]`, `none[T]() -> list[T]`) was bound only by an
  annotated assignment (`r: Result[int] = make()`); elsewhere the checker
  asked for explicit type arguments. It is now also bound by the declared
  result of the function, method, nested function or lambda the call is
  returned from (`return make()`, `return make()?` in a `-> int`, an
  expression lambda's body), by the declared type of the parameter it is
  passed to (functions, methods, constructors, static methods, keyword
  arguments, `?` operands, imported callables) and by the declared type of
  the field it is assigned to (`self.items = none()`, `ledger.status =
  fail("x")` on an imported class); the Go call spells the type arguments
  (`Make[int]()`), where Go reported `cannot infer T`.
- A generic class constructor whose type parameter only an empty literal
  (or no argument) mentions takes the type arguments of the expected type:
  `c: Box[int] = Box([])`, `d: Bag[str] = Bag({})`, `return Box([])` in a
  `-> Box[int]` function, `take(Box([]))`, `r: Registry[str, int] =
  Registry()` (`NewBox([]int{})`, `NewRegistry[string, int]()`). These
  were errors asking for `Box[int]([])`; a context whose type is itself
  generic (`boxed[U](b: Box[U])`) still is.
- A `try { } catch ... { }` inside a block lambda or a nested function
  that returns a value did not build (`too many return values`, `missing
  return`): its `return` and the guard of a `?` returned from the closure
  the `try` lowers to. Such a lambda gets a named result as a function
  does, so `return` there returns from the lambda and a `?` in the `try`
  body or a handler propagates the `Err` out of a `-> Result` lambda (to
  the `catch` of a `do` block around it, also for a `try` inside the `do`
  body and a `do` inside the `try`).
- Importing a module that is also a runnable program (a non-empty `func
  main()`) failed in `go build` with `"fmt" imported and not used` (and
  `"os"` / `"strings"` for packages a top-level `go! { import ... }` added
  for `go!` blocks in that `main`): the module's `main` was cut out of the
  generated Go after its imports were computed. It is now left out before,
  with the imports only it needs. Library modules need no `func main()`
  at all (docs/SYNTAX.md said to keep an empty one).
- `break` / `continue` inside a `try` body, a `catch` clause or a
  `finally` block, or inside a `do` body, failed in `go build` (`continue is
  not in a loop`): those parts are Go closures. They now leave or continue
  the enclosing loop (also through nested `try` / `do` statements, in
  methods, lambdas and module-level loops, with loop `else` blocks), after
  `finally` has run. A `break` / `continue` in a lambda or nested function
  whose loop is outside it is now a compile error instead of a Go error.
- A catch-less `try { } finally { }` ran its `finally` when the enclosing
  function returned (once per loop iteration, all at the end) instead of
  when the statement ended, and a `try { return x } finally { }` body was
  reported as not returning. `finally` now runs at the end of the
  statement and such a body ends the function.
- `return a, b` inside a `try` / `catch` (functions, methods, nested
  functions) did not build: the named result the `try` lowering stores into
  had a single value. A multi-value result now gets one named result per
  value. An `else` block of a `try` also ran when the body returned.
- A list or set literal (or the values of a dict literal) mixing a subclass
  and its base, or sibling subclasses (`for x in [B(1), A(2)]`), was a
  `list[any]` / `[]interface{}`: method calls and field reads failed in
  `go build`. Its item type is now the least common base class (calls run
  the overrides), also for `[B(7), A(8)][0].show()` and for a ternary of two
  such classes (`s = B(0) if c else A(1)` declared `s` with the first
  branch's class). Without a common base the items stay `any` and using a
  member of one is a compile error naming the classes and the fixes
  (annotate the collection, or narrow with `isinstance` / `match`).
- Calling a generic function declared inside another function with a list
  literal (`pick(["a"], 0)`) emitted `[]T{...}` (`undefined: T`): the
  arguments of a nested generic function are now lowered with its type
  parameters bound from them (or from explicit type arguments), like a
  top-level generic's.
- The expected type that binds a type parameter only the result or an
  empty literal mentions now also comes from typed collections: elements of
  a list / set literal and values of a dict literal with a declared type
  (annotations, returns, arguments, fields, nested literals), the argument
  of `append` / `insert` / `extend` / set `add` and `d[k] = ...` /
  `xs[i] = ...` (`xs: list[Box[int]] = [Box([])]`, `xs.append(Box([]))`,
  `rs: list[Result[int]] = [fail("x")]`), where the checker asked for
  explicit type arguments or Go rejected `*Box[interface{}]`.
- An interface written as a class base (`class Item(Priced)`) failed in `go
  build` (`embedded field type cannot be a pointer to an interface`). It is
  now a compile error explaining that interfaces are satisfied structurally
  and which of the interface's methods the class still lacks.
- A `try`'s `else` block ran after `finally` (Python runs it before) and was
  skipped when a `try` nested in the body had caught its own exception. The
  order is now
  body → `else` (only when the body completed: no exception, `return`,
  `break` or `continue`) → `finally`; an exception raised in `else` is not
  handled by the statement's own `catch` clauses and propagates after
  `finally`, and a `return` in `else` runs `finally` first.
- A `return` / `break` / `continue` in `finally` did not cancel an
  exception propagating through the `try` (the program still crashed), a
  `break` / `continue` there did not discard a pending `return` of the body,
  and a `try` inside `finally` lost such a pending `return`. They now follow
  Python: the exception is cancelled, a `return` in `finally` replaces the
  body's return value and a jump discards it. A `finally` that always
  returns now ends the function for the "not all paths return" check. Such
  jumps are warned about (`` `return` in `finally` cancels an exception ``),
  as Python linters and PEP 765 do.
- A list / set literal (or the values of a dict literal) with an item of
  type `any` or of a type the compiler could not determine took its item
  type from the other items: `[anyv(1), 2]` lowered to `[]int{Anyv(1), 2}`
  (rejected by Go) and `[f(1), 2]` with an untyped lambda `f` was a
  `list[int]` that refused `append("y")`. Such literals now have `any`
  items unless an annotation says otherwise.
- The result of a function, method, static method or lambda declared `->
  any` (also imported, nested, or a generic result bound to `any`) did not
  build where a concrete type is expected (`n: int = anyv(3)`, `return
  anyv(x)`, `take(anyv(x))`, `[anyv(1)]` in a `list[int]`): Go reported
  `need type assertion`. Neither did an `any` field (`self.raw`,
  `r.value`) or an element of a `dict[int, any]`. These now convert like an
  element of a `list[any]` / `dict[str, any]` or an `any` local did. All
  sources also share one conversion now, the one `unwrap()` of an untyped
  `Result` already used: an int held in `any` is accepted where a `float`
  is expected and a subclass instance where its base class is expected
  (both used to panic for `list[any]` / `dict[str, any]` elements and `any`
  locals), and `None` where a class is expected; a mismatch still throws a
  `TypeError`.
- `tests/tests/test_doc_comments.py` aborted the unit suite with
  `PermissionError` when the repository sits next to a directory the user
  cannot read (a worktree in `/tmp`); unreadable siblings are now skipped.

## 4.5.1 — 2026-11-07

### Fixed

- `lamc install` with the git cache on another filesystem than the project
  (the `RUN --mount=type=cache,target=/root/.lammergeier/cache` Docker
  recipe, a tmpfs) failed with `Invalid cross-device link`: the cache was
  cloned with hard links only. It now falls back to copying the objects.
- `lamc install` no longer reports a scoped sub-package installed inside its
  parent (`extlibs/@lam/vulture/dev-plugin`, `/std-plugin`) as part of the
  parent's API: upgrading `@lam/vulture` with a plugin installed failed as a
  "mis-labelled release" (`class LiveReload removed`). Directories with their
  own `lamlib.toml` are left out of the API surface.

## 4.5.0 — 2026-11-07

### Added

- `lamc dev <file.lam> [-- ARGS]`: builds and runs a program, then rebuilds
  and restarts it when a `.lam` file or `lamlib*.toml` under the project
  changes, and restarts it without rebuilding on `--watch PATH` / `--ext`
  files. A failed build keeps the previous build running; a program that
  exits is restarted by the next change (or `rs` + Enter). The program runs
  with `LAMC_DEV=1` in its own process group (`--signal`, `--kill-timeout`).
  Polling makes it work on container bind mounts.
- `lamc lib run <script> -- ARGS` appends the shell-quoted arguments to the
  script command, and `lamc build <file.lam> --run -- ARGS` passes them to
  the program (as `docs/stdlib.md` already showed).
- Entry points below the project root resolve project modules from it: the
  directory of the nearest `lamlib.toml` (within six levels), its `lib/` and
  its `extlibs/` are searched after the source's own directory, as the
  language server already did. `lamc cmd/serve.lam` and test programs in
  `tests/` import the project's packages and installed libraries without
  `--extlibs` flags.
- New stdlib module `lami18n` (`Translations`): translated texts loaded from
  `<dir>/<lang>/*.json` (nested objects as dotted keys, numbers and booleans
  as text) with `TranslationError` results naming the file, line and key for
  duplicate keys (across files or inside one object), malformed JSON, arrays
  and `null`; a fallback language; `{name}` parameters inserted verbatim
  (`text`), HTML-escaped into trusted markup (`markup -> html`) or as its
  text content (`plain`); `add` / `merge`, `has`, `count`, `keys`,
  `missing(lang, other)` for completeness checks, and `language` /
  `negotiate` for `Accept-Language` headers.
- `Tx.first`, `Tx.scalar`, `Tx.queryJson`, `Tx.firstJson` and
  `Tx.scalarJson`, with the semantics of the `Db` methods, run through the
  transaction's query path (its connection, context and `DbObserver`), so
  code taking a `Db` or a `Tx` no longer wraps `query`.
- `Env.flag(key, fallback)` reads a lenient switch (`1` / `true` / `t` /
  `yes` / `y` / `on` and `0` / `false` / `f` / `no` / `n` / `off`, any case,
  else the fallback) and `Env.list(key, separator = ",")` a list of trimmed,
  non-empty parts.

### Changed

- `lamc lib run` starts the script in its own process group and forwards
  `SIGINT`, `SIGTERM` and `SIGHUP` to it, so `docker stop` or Ctrl+C stops a
  long-running script together with `lamc`; a script killed by a signal
  exits with `128 + signal`.

### Fixed

- `Context.onSignals`: a signal arriving right after the context was cancelled,
  before its watcher stopped listening, was swallowed instead of getting the
  default handling (ending the process); it is now delivered again.
- `sorted(s)` of a `set[T]` returns a new sorted `list[T]` (a typed Go
  `[]T`), with `key=` / `reverse=`, for set variables, literals,
  comprehensions, fields, function results and set operations; it was typed
  as the set (`cannot assign set[str] to list[str]`) and generated Go that
  did not build (`",".join(sorted(s))`). `sorted(d)` of a `dict[K, V]`
  likewise returns its sorted keys as a `list[K]`, as in Python.
- A type parameter used as a set element or a dict key (`func count[T](s:
  set[T])`, `dict[K, V]` parameters, fields and locals of generic functions,
  classes and methods) is emitted with Go's `comparable` constraint, also
  when it is passed on to another generic definition that needs it
  (`Bag[T]`, `distinct(xs)`); such code failed in `go build` with `invalid
  map key type T (missing comparable constraint)`. `sorted()` of a generic
  set or dict works like `sorted()` of a generic list.
- Methods on a `byte` (indexing a `str` with one integer, `w[0].toUpper()`)
  and on `int`, `float` and `bool` values are Lam errors instead of `go
  build` failures (`cannot use w[0] (value of type byte) as string`); the
  `byte` / `str` mismatches of a `str` index (`first: str = w[0]`,
  `w[0] == "h"`, `str` arguments, `+`, returns) name the `string(w[0])`
  conversion.
- A lambda testing `in` / `not in` passed to a list method
  (`xs.all(lambda s: "z" not in s)`, `.any`, `.filter`, `.map`, `.reduce`,
  `.sort` over `str`, `dict`, `set` and `list` elements) no longer fails in
  `go build` with `"reflect" imported and not used` / `"strings" imported
  and not used`: the Go import block only lists the packages the emitted
  code uses.
- Dict methods on a dict field (`self.counts.get(x, 0)`,
  `obj.inner.totals.keys()`, a generic `dict[K, V]` field, a static field)
  are reported like on a dict local ("dicts have no `get()` method")
  instead of failing in `go build`; methods a set does not have
  (`ids.index(1)`, `self.names.append(x)`) are reported with the Lam
  spelling (`` `set[str]` has no method `append` — did you mean `add`? ``,
  ``use `len(s)` ``).
- `lamc fmt` writes keyword arguments and keyword sub-patterns without
  spaces around `=` (`sorted(words, reverse=true)`, `case Point(x=0)`, also
  on the argument lines of a multi-line call) and type parameters as
  `func top[T: ordered]` / `class Registry[K: comparable, V]`, as in the
  docs; it wrote `reverse = true` and `[T:ordered]`.
- `lamc fmt` keeps a `{}` / `{...}` operand of a conditional expression on
  its line (`slots: json = schema if ok else {}`, also in a lambda body); it
  laid it out as a block (`else {` / `}`). A keyword before `(` gets one
  space (`if (a and b) or (c and d) {` was written `if  (a and b) ...`, also
  `elif` / `while` / `match`; `not(x)` / `and(x)` become `not (x)` /
  `and (x)`), a parenthesised lambda head is written `lambda (x: int)` as
  in the docs (it was tightened to `lambda(x: int)`), and a keyword
  argument's dict value follows `=` directly (`attrs={...}`, not
  `attrs= {...}`).
- A class without its own `init` runs an inherited parameterless `init`
  (`class Sub(Registry)` with `func init(self)` in `Registry`); `Sub()`
  only allocated the parent, so its fields kept Go zero values
  (`assignment to entry in nil map`).
- Methods inherited from a generic base that a generic subclass binds in
  part (`class Sub[V](Registry[str, V])`) check their arguments and type
  their results with the receiver's type arguments (`s: Sub[int]`,
  `s.put("q", 2)`); they were rejected with ``has type `int`, expected
  `V` ``.
- A generic function or method whose type parameter only appears inside a
  tuple type (`func toDict[K, V](pairs: list[tuple[K, V]])`) builds: the
  call spells the type arguments bound from the arguments
  (`ToDict[string, int](pairs)`) where Go reported `cannot infer K`.
- `?` inside a lambda declared `-> Result` / `-> Result[T]` returns the
  `Err` from the lambda, as in a function (block and expression bodies,
  nested lambdas, lambdas in methods, `?.` chains, lambdas passed to generic
  functions, inside `do/catch`); it panicked at run time and the checker
  warned "`?` operator used outside a `-> Result` function". A lambda that
  does not return `Result` keeps the warning (and the panic). `f()?.m()` in
  an expression-bodied lambda is propagation then member access, as in a
  statement (it was safe navigation, which did not build).
- `f()?.method()` on a `Result` evaluated `f()` (and its `?` guard) twice;
  it runs once.
- `catch _ { ... }` in `do { } catch _` and `try { } catch _` (and
  `catch ValueError as _`) handles the error without binding it; it
  generated `_ := ...`, which failed `go build` (`no new variables on left
  side of :=`).
- A type parameter that only appears inside a `Result[T]` (the Go
  `*Result` is not generic) is inferred from the Lam types of function-typed
  arguments: `inTx(c, lambda (tx: Conn) -> Result[int]: count(tx))` with
  `func inTx[T](conn: Conn, fn: func(Conn) -> Result[T]) -> Result[T]`
  emits `InTx[int](...)` where Go reported `cannot infer T`. Lambda
  parameter and result annotations and functions or static methods passed
  by name bind type parameters through `Result[T]`, `optional[T]`,
  `list[T]`, `dict[K, V]` and function types, for top-level functions,
  static and instance generic methods, also imported from another module.
  A type parameter only the result mentions is bound by the annotated
  variable the call initializes (`r: Result[int] = make()`,
  `xs: list[int] = make()`, `n: int = make()?`) instead of asking for type
  arguments.
- `max(a, b)` / `min(a, b)` of values of a type parameter (`T: ordered`)
  use Go's builtin `max` / `min`; they converted the operands to `float64`,
  which failed `go build` for `string` (`cannot convert string (in T) to
  type float64`).
- An unannotated local initialized with an explicit instantiation
  (`b = Box[int](5)`) keeps its type arguments: `b.map(lambda x: x * 2)`
  typed the lambda as `func(interface{}) interface{}` and `[b]` was a
  `[]*Box`, which failed `go build`; assigning it to a `Box[str]` is now a
  Lam error.

## 4.4.0 — 2026-11-07

### Added

- `lamdb` request-scoped database contexts: `Db.withContext(ctx)` creates a
  handle sharing the connection pool while using that `Context` for raw SQL,
  query-builder execution, and transactions; `Tx` inherits the context.
  Keep the owning `Db` open for the lifetime of the pool, and use a separate
  scoped handle per request rather than a mutable global current context.
- Optional `DbObserver` (`start(ctx, statement) -> any`,
  `finish(ctx, token, failure)`) installed with `Db.setObserver(observer)`
  observes raw and builder operations plus transaction begin, commit, and
  rollback (including savepoints). The statement is SQL with placeholders,
  never the separately bound parameter values; `failure` is empty on success
  or the stable `Error.kind` on failure, not the driver's message or cause.
  Do not interpolate secrets into SQL. Cancellation and deadlines interrupt
  retry backoff, and negative retry settings are clamped to zero.

### Fixed

- The documentation-comment corpus treats intentionally invalid syntax fixtures
  in sibling repositories the same way as this repository's fixtures.

## 4.3.0 — 2026-11-05

### Changed

- **Every module of the program gets the full semantic check**, not only the
  compiled file: project modules, `extlibs` packages and the stdlib, each
  once, in its own scope and with the signatures, overloads, classes,
  generic classes, interfaces, constants and module-level variables of what
  it imports (also re-exported by a package `__init__.lam`, reached through
  `import .m as m` or a relative import). Imported modules used to get name
  resolution only, so their type errors failed in `go build` with Go
  messages (`{}` passed to a `list[FieldError]`, a generator's element type,
  a misused `json` value, a wrong argument or return type). They are Lam
  errors at the imported file's line, with the compiled file's wording.
  Warnings (unused names, dropped results, `?` outside a `Result` function)
  stay those of the compiled file; an imported module only adds its import
  advisories. A module that checks clean is cached by the compiler, its
  content and the content of every module it imports (`--no-cache` checks
  all), and the checks of one build share parsed trees and imported class
  tables.

  **Migration**: code that built before may now be rejected where an
  imported module was wrong all along. The usual cases, each with the
  compiled file's message:
  - a module importing itself (`from .paging import Page` inside
    `paging.lam`): remove the import;
  - `?` / `do`-`catch` in a module that does not import `Result` (it only
    built because another module imported `lamerrors`): add `from lamerrors
    import Result`;
  - an `any` value passed where `json` is expected (`Json.encode(raw)` with
    `raw: any`): convert it with `Json.fromValue(raw)?`;
  - `json(x)` of an `optional[Class]` (it panics on `None`): test for
    `None` first or pass the class value;
  - an `html` value in a markup attribute (`title="${label}"`): insert a
    `str`;
  - a `Result[A]` returned as a `Result[B]` (an error result re-wrapped as
    is): `return Result.Err(r.error)`.
- A field's declared type now types every expression it is in (it was
  known only for `int`, `float`, `json` and `optional` fields, other fields
  were unknown): `self.code + "x"` is a `str`, `self.totals["a"]` an `int`,
  `for t in self.tiers` binds the element type, `p.items` of a
  `Page[Product]` is a `list[Product]`. Mistakes such as `return
  self.raw[i]` (a `byte`) from a `-> str` method are reported instead of
  failing in `go build`.
- A generic call no argument gives a type parameter to is a Lam error
  asking for explicit type arguments: a parameter default (`firstOf()` with
  `xs: list[T] = []`), `None` (`ident(None)`), or an empty `[]` / `{}`
  where the result must be a given instantiation (`b: Box[int] = Box([])`).
  These failed in `go build` (`undefined: T`, `cannot use *Box[interface
  {}] as *Box[int]`). An empty literal whose result may be anything
  (`count([])`, `b = Box([])`) still makes the parameter `any`.

### Added

- `reversed(xs)` returns a reversed copy of a list (same list type, `xs`
  unchanged); `reversed(s)` of a `str` gives its characters last first as a
  `list[str]`, matching Python's iterator of characters (`"".join(reversed(
  s))`, `for ch in reversed(s)`). It was accepted by the checker and failed
  in `go build` ("undefined: Reversed"); any other argument is a compile
  error.
- `iterator[T]` is another name of `generator[T]` (a Go `chan T`) for
  return types, parameters, variables and fields: the code generator
  accepted it while the checker reported "unknown type `iterator`".
- Stdlib: `HtmlPolicy.allowFrames(hosts)` keeps trusted embeds (admin-authored
  ads with videos) in `Html.sanitize`, which otherwise always removes frames:
  an `iframe` survives only when its `src` is an absolute `https://` URL of a
  listed host or of a subdomain of one (`host:port` for another port), read
  strictly (entities decoded; white space, control characters, backslashes,
  non-ASCII / IDN look-alikes, userinfo, percent-encoded or IP hosts,
  `http:`, protocol-relative, relative, `data:` and `javascript:` sources
  refuse it). The frame is rebuilt with `src` (scheme and host lowercased,
  `:443` dropped), `width`, `height`, `title`, `allow` (seven player
  features), `allowfullscreen`, `loading` and `referrerpolicy` (default
  `strict-origin-when-cross-origin`), always gets `sandbox="allow-scripts
  allow-same-origin allow-presentation allow-popups"` and loses its content;
  `srcdoc`, `name`, event handlers and every other attribute are dropped.
  `HtmlPolicy.videoHosts()` lists YouTube (also `youtube-nocookie`) and
  Vimeo's embed hosts. `frame`, `object`, `embed` and frames in `svg` /
  `math` stay removed. The sanitizer's fuzz test covers frames.
- Covariant return types: an override may return a subclass of the base
  method's return class, as builders do (`withError(...) -> AppProblem`
  overriding `-> Problem` in a subclass of Vulture's `Problem`, also through
  further subclasses); calls through the subclass are typed with it and calls
  through the base type still dispatch to the override (Go has no covariant
  methods: the dispatch interface uses adapters that upcast the result, see
  docs/TRANSPILATION.md). Methods of generic classes, returns of a generic
  class and `async` methods keep requiring the base's return type, with a
  message saying so.
- Static methods do not override: a subclass may declare a static method
  with a base static's name and another signature (`static func
  validation(code: str) -> AppProblem` next to `Problem.validation(where,
  issues, status)`); the checker compared them as overrides and rejected it.

### Fixed

- False positives of the checker, found by checking every module of the
  stdlib, the official packages, two applications and the examples (they
  also affected the compiled file):
  - a union value passed to a parameter of the same or a wider union
    (`handler: func(R, S) | (func(R, S) -> html)` passed on): each
    alternative is checked;
  - an interface value where another interface is expected when it has
    every method of it with the same signature (`SqlTx` where `Sql` is
    expected), as in Go;
  - one class reached under two names (`Column` and `ui.Column` through
    `import .ui as ui`), also inside `list[...]` / `dict[...]`;
  - imported constants and top-level variables had no type (`Prefix + "a"`
    was "not JSON-compatible"), also through a module alias (`c.Limit`);
  - a dict literal appended to a `list[json]` ("list append expects
    `json`, got `dict[str, any]`") and a list comprehension of dict
    literals passed as `json`: a literal is JSON when its values are;
  - `{}` assigned to a `dict[int, Part]` field or variable;
  - `"x" if c else None` where an optional fits (an `optional` / `any`
    variable, argument or result, a list item, JSON, a markup attribute, an
    f-string slot); a new unannotated variable or a `str` target still
    rejects it;
  - `None` for a function value (`self.onDone: func(int) = None`);
  - a `Result[any]` returned as a `Result[str]`;
  - `json(t)` / field reads of `t` in `for t in self.tiers`;
  - a `go!` block whose Go code declares an identifier `self` (a parameter
    `func(n *node, self bool)`, a receiver, `self := ...`), which made the
    stdlib's `lammarkup` fail;
  - `base.init()` in a subclass whose base declares no `init` (only field
    defaults: the implicit constructor);
  - a subclass of a generic class imported through a module alias
    (`class Ints(sh.Box[int])`) overriding a method returning the generic
    class (`-> sh.Box[int]`) was told the base returns `Box[int]`: the
    alias renames generic class names in imported signatures too.
- Errors inside the slots of a multi-line markup literal (and of a literal
  nested in a slot), and in code after such a literal on its last line, were
  reported at the literal's first line (`return <>`), and errors in the
  slots of a multi-line f-string at the string's first line. They are
  reported at the slot's own line and column: the literal's marker call now
  spans the literal's lines with each slot argument at the place of its
  `${`, and a collapsed multi-line f-string marks its line breaks. A slot of
  a multi-line f-string may also span lines now (it was "not a valid
  expression").
- A top-level function is capitalised in Go (`t` is `T`); inside a generic
  function or class with a type parameter of that name, Go resolved a
  reference to the function as the type parameter: `t("x")` was a
  conversion, `t` passed as a handler "is not an expression". Such
  references go through a package-level alias of the function (calls,
  values, defaults, lambdas, list callbacks and f-string slots, for
  functions of the module and imported ones); a generic function cannot be
  aliased and is a Lam error naming the clash.
- An overloaded function used as a value (`apply(fmtValue, x)`) failed in
  `go build` ("undefined: FmtValue", there is one Go function per variant);
  it is a Lam error suggesting a lambda.
- A type parameter that no parameter of a generic function mentions (only
  its result, or nothing: `make[R]() -> list[R]`) is a Lam error asking for
  the type arguments; Go reported "cannot infer R".
- A method a subclass inherits (from a base of the same module or of an
  imported one) did not type the literal arguments of calls on a subclass
  value: `sub.need(["a"])` passed a `[]interface{}` to a `[]string`
  parameter.
- The loop variable of `for r in results()` with `-> generator[Result[int]]`
  (reported on 4.2.1) was typed by the checker but lost its payload in the
  generated Go: `r.unwrap() * 2` failed in `go build` ("mismatched types
  interface{} and int"). `Result` elements keep their payload over
  generators, iterators and lists returned by functions and methods,
  annotated locals, parameters and fields, and in comprehensions.
- Two classes of the same name in different modules were merged by the
  checker of a module that knew both: a module importing a package's
  `Problem` as `HttpProblem` and defining its own `Problem` (luxyt's
  `api/problems.lam`), used by a module that also imports from the package,
  got "unknown member `Problem.notFound`" and the package's constructor
  ("argument `status` to `Problem` has type `str`, expected `int`"). The
  other classes of an imported module (known so its signatures can be
  checked) were collected under their bare names, overriding or merging with
  the importer's own bindings, and the class names written in a re-exported
  class's module were read with the importer's aliases. Every class is now
  tracked by its defining module: an alias binds only the alias, a class the
  importer does not name keeps its name unless the importer binds that name
  to another class (it is then `kit.Problem` in messages), and members,
  constructors, statics, method signatures, bases and type parameters stay
  per class. A local whose inferred type is such a class is declared by Go
  inference.
- Members of a `Result[T]` value were not checked (`r.isOk()` failed in `go
  build`): they are those of lamerrors' `Result` (`ok`, `unwrap`,
  `unwrapOr`, `value`, `error`), with a did-you-mean suggestion.
- A module function passed by name where the parameter's function type has
  fewer parameters (the others have defaults: `apply(suffix, "a")` with `f:
  func(str) -> str`) or no result (`onEach(show)` with a `func(str, int)`
  callback and `show` returning an `int`) was accepted by the checker, as
  documented since 1.19.4, but failed in `go build`: the argument is now a
  closure calling the function with its defaults filled. A parameter typed
  by a union of callback types receives the function value as is, so a
  function relying on its defaults there is a Lam error suggesting a lambda.
- Explicit type arguments type the defaults of a generic function:
  `firstOf[str]()` with `xs: list[T] = []` passed a Go `[]T{}` ("undefined:
  T"); a default typed by the other arguments' type parameters too
  (`merge(xs)` with `extra: list[T] = []`).
- Stdlib: `DateTime.convert` returned the `Result[DateTime]` of a failed
  parse as its `Result[str]`; it re-wraps the error.

## 4.2.2 — 2026-11-04

### Fixed

- A collection literal that no value of the declared type can be passed
  the checks in an imported module and failed in `go build`:
  `shopForm(c, action, shop, None, {}, false)` with `errors:
  list[FieldError]` ("cannot use map[string]interface{}{} ... as
  []*FieldError value"). Only the compiled file gets the full semantic
  check; imported modules got name resolution only. A `{}` / `[]` / set
  literal or comprehension where its kind cannot fit (a list where a dict,
  set, class, interface, scalar or function is expected, a dict where a
  list is, ...) is now a Lam error in every module of the build, for call
  arguments (positional and keyword, functions, constructors, instance and
  static methods, also across modules and for generic `list[T]`
  parameters), parameter defaults, annotated variables and fields, and
  returns, with the semantic checker's wording. `{}` stays an empty set
  for a `set[T]` and an empty object for `json`, `[]` an empty array for
  `json`.
- In the compiled file, an empty `[]` was accepted for any type (`counts:
  dict[str, int] = []`, `label([])` with `name: str`, `return []` from a
  `-> Item`, `byName["a"] = []` into a `dict[str, Item]`, a `set[str]`
  argument), a `{}` passed to a generic `list[T]` parameter or constructor
  (`count({})`, `Box({})`) was skipped, and parameter defaults were not
  checked at all (`limit: int = "10"`, `errors: list[Item] = {}`,
  `title: str = None`): all failed in `go build`. They are Lam errors;
  `None` stays a valid default for lists, dicts, sets, classes and
  optionals. An annotated field's error names it (`self.last`, was
  `<target>`).
- A generator declared `-> generator[T]` compiled to a `chan interface{}`:
  reading a field of a yielded class (`for c in circles() { print(c.Title)
  }`) failed in `go build` ("c.Title undefined (type interface{} has no
  field or method Title)"), as did arithmetic on `generator[int]` values,
  a `generator[T]` parameter, variable or field ("cannot range over g
  (variable of type interface{})") and a comprehension over a typed
  generator method. `generator[T]` is a `chan T`: class, generic class,
  `optional`, list, dict and tuple element types work for local, imported,
  static, instance and generic generators, `for` loops (also destructuring
  a `generator[tuple[...]]`) and comprehensions.
- A generator declared inside a function made the enclosing function a
  generator (`main` became `func main() chan interface{}`) and the nested
  one had no body ("missing return"); it is a closure returning its
  channel, capturing the enclosing locals (also in methods and in other
  generators).
- A `yield` value was not checked against the generator's element type
  (`yield int(w)` in a `-> generator[Result[int]]`, `yield "x"` in a
  `-> int` generator) and failed in `go build`; it is a Lam error.
- `min` / `max` with `key=` ignored the key (`max(xs, key=lambda x: -x)`
  returned the largest value) and failed in `go build` for lists of
  classes, generic classes, lists and dicts ("*Circle does not satisfy
  cmp.Ordered"). They return the first value with the smallest / largest
  key (compared like `sorted`'s keys), keep the element type
  (`max(ps, key=f).name`), take several arguments (`max(a, b, key=f)`) and
  throw `ValueError` on an empty list.
- The field of a generic class element read in a lambda or a comprehension
  (`sorted(boxes, key=lambda b: b.v)`, `[b.v for b in boxes]`,
  `boxes.map(lambda b: b.v + 1)` with `boxes: list[Box[int]]`) leaked the
  class's type parameter into the caller ("undefined: T"); it has the
  element's type argument.

## 4.2.1 — 2026-11-04

### Fixed

- A lambda with a block body in an f-string slot
  (`f"{xs.all(lambda (x: int) -> bool { return x > 0 })}"`, also
  `lambda(x: int)`, several statements, strings holding braces) failed in
  `go build` ("syntax error: unexpected :"): the slot was parsed without the
  statement preprocessing (the `;` before the body's `}`), so it was neither
  checked nor lowered and reached Go as written. Slots that do not parse as
  written now get that preprocessing, in the semantic pass and in the
  lowering; undefined names and type errors inside such a lambda are
  reported at their place.
- F-string format specs follow Python's alignment forms where Go has a
  verb: `{x:>5}` (`%5v`), `{s:<6}` (`%-6v`), `{x:>8.2f}`, `{x:+d}`, `{x:.2}`
  and `{x:}` reached Go as an expression (`x:>5`) and failed in `go build`.
  Specs with no Go verb (centering `{s:^7}`, a fill such as `{n:*>6}`, `=`,
  grouping `{n:,}`, `z`, `n`, `%`), the conversions `!r` / `!s` / `!a`,
  `{x=}`, an empty slot `{}` and a slot that is not an expression are
  compile errors naming the alternative (`s.center(7)`, `Fmt.grouped(n)`,
  `repr(x)`, `x={x}`, `{{}}`) instead of invalid Go. Dict literals, slices
  (`xs[1:]`, `xs[::-1]`), ternaries and nested f-strings in a slot keep
  working with a spec after them.
- `sorted()`, `xs.sort()` (also with a `key` returning such values) and
  `min(xs)` / `max(xs)` of a list whose elements are not of an ordered type
  compared the values' text, so `[10, 9, 100]` of a `list[any]` sorted as
  `[10 100 9]` and a `list[optional[int]]` by pointer address, and
  `min` / `max` of a `list[any]` / `list[json]` failed in `go build`
  ("does not satisfy cmp.Ordered"). Elements of `list[any]` (from a literal,
  `Json.toList`, a function result, a comprehension), `list[json]`,
  `list[T]` in a generic function and `list[optional[...]]` compare by kind
  at run time: numbers numerically (ints and floats mixed), strings by
  bytes, `false < true`, and different kinds as None < bool < number <
  str < other values (by their text); `min` / `max` of an empty list throw a
  `ValueError`. `for x in sorted(opt)` keeps the list's element type.
- `lamc fmt`: a dict literal alone on a continuation line (an argument of a
  call spread over lines, an item of a list of dicts) was laid out as a
  block (`{` / `"k": v` / `}` / `,` on separate lines); the `}` of a lambda
  body inside a call was split from what follows it (`}` and `)` on two
  lines, `}, 0)` indented one level too deep); a method chain continued on
  the next lines (`.map(f)`) was moved back to the statement's level. The
  stray space before a dict / list / markup argument (`res.json( {...})`),
  written by formatters before 3.0.0, is removed (fixed in 3.0.0; the
  formatter tests now also cover lists, markup and nested calls).

## 4.2.0 — 2026-11-04

### Added

- `lamjson` typed member reads: `Json.getStr(doc, key, fallback="")`,
  `getInt`, `getFloat`, `getBool` return the member when it has the
  requested kind and `fallback` otherwise (missing, null, another kind, a
  `doc` that is not an object); `getInt` takes integers and integral floats
  (`3.0`, not `3.5`), strings are never converted. `Json.has(doc, key)`
  tells a null member from a missing one, `Json.getList` / `getObject` give
  the document's own array / object (`[]` / a new `{}` otherwise),
  `Json.at(doc, ["orders", -1, "id"])` follows a path, and
  `Json.mapKeys(doc, rename, deep=true)` copies a document with renamed
  object keys.
- Key-case conversion: `Strings.camelCase`, `pascalCase`, `snakeCase`,
  `kebabCase`, also as `str` methods (`"slots_total".camelCase()` is
  `"slotsTotal"`, `"HTTPServer".snakeCase()` is `"http_server"`); words
  split on non-alphanumerics, case changes and acronym ends, digits stay
  with the word before them.
- `lamurl`: `Url.encodeComponent` (JavaScript's `encodeURIComponent`) /
  `decodeComponent`, `Url.encodePath` (`/` kept), `encodePathSegment`,
  `encodeUserInfo`; `Url.query(params)` builds a form-encoded query string
  from a `dict[str, list[str]]` / `dict[str, str]` (keys sorted) or
  `[key, value]` pairs (order and repeated keys kept), and
  `Url.parseQuery(s) -> Result[dict[str, list[str]]]` reads one.
- `Conv.parseFinite(s) -> Result[float]`: like `Conv.parse` but `NaN`,
  infinities, overflow and `_` separators are a `ParseError`.
- `Fmt.grouped(value, separator=",", decimals, decimalSeparator=".")` for
  `int` and `float`: `1,234,567.5`, `Fmt.grouped(1360.0, ".", 2, ",")` is
  `1.360,00`.
- `DateTime.parseAny(value, layouts, location="UTC") -> Result[DateTime]`:
  the first matching Go layout wins, zoned layouts keep the text's offset.
  `DateTime.parseCommon` also reads PostgreSQL `timestamptz` /
  `timestamp` texts (`2026-11-03 19:05:00.123456+01`, `+05:30`, fractional
  seconds optional).
- `lamdb`: driver failures keep the driver's error as `Error.cause`, and
  `Db.sqlState(err)` reads its SQLSTATE from the driver's error code
  (PostgreSQL's own; SQLite extended result codes and MySQL error numbers
  mapped to `23505` / `23503` / `23502` / `23514` / `23000`), with
  `Db.isUniqueViolation(err)` / `Db.isForeignKeyViolation(err)` for a
  failed `Result`, its `Error` or a driver error. `Where` builds WHERE
  clauses for hand-written SQL: `add(sql, params)`, `addIf(cond, ...)`,
  `addIn(column, values)`, `ilikeAny(columns, text)` (PostgreSQL) /
  `likeAny` with the user's `%` / `_` escaped, `sql(prefix="WHERE")`,
  `params()`, `paramsWith(extra)`, and `Where.likePattern(text, match,
  escape)`. `Row.timeText(row, key, layout, location, fallback)` writes a
  timestamp column with a Go layout (RFC 3339 in whole seconds by default).
- `lamcsv`: a `delimiter` on every reader and writer (`Csv.formatAll(rows,
  ";")`, `Csv.parseAll(text, ";")`, `parseFile`, `rows`, `batches`,
  `CsvReader.open`); an invalid delimiter throws a `CsvDelimiterError`. The
  RFC 4180 quoting rules are documented.
- `lamhttp.Multipart`: `multipart/form-data` bodies with `field(name,
  value)`, `file(name, filename, contentType, data: bytes)`, `body() ->
  bytes`, `contentType()`, `setBoundary` for stable test bodies, sent with
  `HttpRequestOptions.multipart(form)` / `HttpRequest.multipart(form)`;
  names are quoted as browsers quote them.
- `Html.sanitize(markup: str | html, policy=HtmlPolicy.basic()) -> html`
  with `HtmlPolicy` (`HtmlPolicy()`, `.basic()`, `.rich()`, `allowTags`,
  `denyTags`, `allowAttributes`, `allowSchemes`, `allowRelativeUrls`,
  `allowDataImages`, `allowStyles`): default-deny on the parsed tree;
  scripts, plugins, frames, foreign content, comments, event handlers,
  `srcdoc` and `javascript:` / `vbscript:` / `data:` URLs (read as browsers
  read them) are removed, `style` keeps only allowed properties with plain
  values, links opening a window get `rel="noopener noreferrer"`, and the
  output is serialized from the new tree. Covered by 32 XSS payloads and a
  fuzz test that checks 1,500 random inputs with Python's HTML tokenizer.

### Changed

- A fluent builder step used as a statement no longer gets the "dropped
  return value" warning: a call of an instance method whose declared return
  type is the receiver's class or one of its bases (type arguments aside, not
  `optional` / `Result`), such as `w.addIf(...)`, `form.file(...)`,
  `b.add("x").add("y")` or an inherited / generic builder (`Bag[T].put ->
  Bag[T]`), local, imported or stdlib. Static factories (`Builder.make()`),
  `Result`s and other results still warn.

### Fixed

- An argument that is a call of a function or static method picks the
  overload of its declared result: `Html.sanitize(Html.parse(s), p)` and
  `show(make(s))` (with `show(str)` / `show(html)` variants) chose the first
  variant and failed in `go build` ("cannot use ... (value of struct type
  LamHTML) as string value").
- A float literal without a `.` (`1e21`, `2.5E-5`) was typed `int` when
  choosing an overload, so `grouped(1e21)` called the `int` variant.
- Keyword arguments are matched to each overload's parameter names:
  `Fmt.grouped(1234.5, decimals=1)` or `f(2.5, sep=";", decimals=2)` in any
  order picked the first variant (`int`) and failed in `go build`.
- `str(Error(...))` no longer repeats a cause whose text is the message.
- `json(p)` with `p` typed as a base class (`p: Problem = MyProblem(...)`, a
  `Problem` parameter, list element, `self`, a `catch Problem as e` binding,
  a `Result[Problem]` payload) ran the base's `__json__` instead of the
  subclass's override; so did `html(p)`, `xml(p)`, markup slots
  (`<>${p}</>`, a `list[Problem]` in a slot, `str` in attributes) and
  explicit `p.__json__()` / `__html__()` / `__xml__()` / `__str__()` calls.
  The most-derived object's method now runs, in the module declaring the base
  too. `json(self)` in a method and `json(e)` of a typed `catch` binding were
  rejected ("value of type `unknown`").
- Nested functions with default parameters did not compile: calls leaving out
  a defaulted argument failed in `go build` ("not enough arguments in call to
  pad") and keyword arguments were dropped. Calls of a nested function fill
  defaults and order keywords like calls of top-level functions (also nested
  in methods, in other nested functions and in imported modules), and the
  semantic checker reports missing, unknown and extra arguments. A recursive
  nested function failed with "undefined: fact".
- A const or variable whose name differs from a function, class or interface
  of the same module only in its first letter (`const Languages` and `func
  languages()`, `Point` and `class point`, `helper` and `private func
  Helper`) failed with a Go redeclaration; 4.1.1 only separated such names
  across modules. The value gets a module-qualified Go name (main file and
  imported modules). An imported const or global (`from m import Codes`)
  was untyped in the importing module, so `Codes + ["fr"]` failed in `go
  build` ("mismatched types []string and []interface{}").
- `private func f()` right after a simple statement (`x: int = 1`, a call, a
  class field) was a syntax error ("Unexpected `func`").
- Fields a class inherits (from a base in the same module, another module or
  a package) were `unknown` inside the subclass, so `return {"title":
  self.title}` in a subclass's `__json__` (or any method returning `json`)
  was rejected and other checks (`self.count + "x"`) missed them; a generic
  base's field takes the subclass's type argument (`item: T` of `Box[int]`).
  `{"extra": self.extra ?? "none"}` and `{"titles": [p.title for p in ps]}`
  were rejected in JSON literals for the same reason (an `optional` before
  `??`, untyped comprehension targets).
- An f-string slot holding a comprehension, a generator argument or a lambda
  skipped the semantic check, so `f"{all(x > 0 for x in xs)}"` reached Go as
  `All(x > 0 x xs)`. Such slots are checked like other expressions; `all`,
  `any` and `sum`, which Lam spells `xs.all(lambda x: ...)`, `xs.any(...)`
  and `xs.reduce(lambda a, b: a + b, 0)`, are Lam errors that suggest those
  methods. A generator expression as the only argument (`max(len(n) for n
  in names)`, `", ".join(s.upper() for s in xs)`, `total(x * 2 for x in
  xs)`), accepted by the grammar, is the list comprehension `[...]`, in and
  out of f-strings. `sorted([-x for x in xs])` compared the numbers as text.
- Values yielded by a generator had no element type for the consumer when it
  declares a `Result` or a class: `for batch in Csv.batches(path)` gave
  `batch.unwrap()` the Go type `interface{}` ("cannot use batch.Unwrap()
  ... as [][]string value"). The loop target (or comprehension target) now
  has the declared element type (`Result[T]` with its payload, classes,
  `generator[T]`'s `T`) for stdlib, local and imported generators, in the
  compiler and the semantic checker (`n: int = batch.unwrap()` is a Lam
  error). Instance-method generators did not compile ("missing return") and
  a bare `return` in a generator was rejected; both work. `lamcsv` no longer
  needs its internal payload helper.

## 4.1.3 — 2026-11-04

### Fixed

- A generic class's type arguments are inferred from its constructor's
  arguments, as the documentation describes for generic functions:
  `a: Box[int] = Box([1, 2])` was rejected ("cannot assign `Box` to
  `Box[int]`"), and a list comprehension passed to the constructor
  (`Box([Row(x) for x in xs])`, in one module or with the class imported)
  was emitted as `[]T` ("undefined: T"). An argument the checker cannot
  type takes the target's type arguments (`c: Box[float] = Box([x / 2.0
  for x in xs])`); a conflicting one is still reported (`labels: Box[str]
  = Box([1, 2])`: "cannot assign `Box[int]` to `Box[str]`").

## 4.1.2 — 2026-11-04

### Added

- Relative module imports: `import .kit as ui` binds the sibling module
  `kit.lam` (or `kit/__init__.lam`) and `import ..shared.util as util`
  goes up one package first; members are reached through the alias
  (`ui.page(...)`, `ui.Listing.read(...)`, `ui.PageSize`, annotations
  `l: ui.Listing`), as for `import pkg.mod as m`. Like `from .kit import
  x`, the path is resolved from the importing file only. The alias is
  required, like for scoped packages (`import .kit` alone is a Lam error
  suggesting `import .kit as kit`); `import .` with no module name is a
  syntax error listing the form. Works in the main file, in package modules
  and in a package `__init__.lam`.

### Fixed

- A `?` result used as an operand took the type of the enclosing context
  instead of the callee's payload: `return Result.Ok(parse(s)? * 2)` (also
  in a `do` block) failed in `go build` with "__q1.Value * 2 (mismatched
  types interface{} and int)", because `Result.Ok(...)` publishes
  `interface{}`. A known payload type now always wins (the context only
  fills in an unknown or `any` payload, as before), and the payload type is
  the operand's type for the surrounding expression. This covers arithmetic,
  unary minus, comparisons, membership, `and` / `or` / `not`, string
  concatenation and repetition, int/float mixes (`ratio()? * 4.0 +
  parse(s)?`, `x: float = parse(s)? * 2`), f-string slots, method calls and
  indexing on the payload (`rows()?[0] * 2`, `rows()?[-1]`), function and
  builtin arguments (`max(parse(s)? * 2, 3)`), list / dict literals and
  comprehensions (`[word(w)?.toUpper() for w in words]` is a `list[str]`),
  ternaries, nested `?` (`half(parse(s)? * 2)? * 3`), `return`,
  unannotated assignments, augmented assignments and `if` / `while` /
  `for` / `match` heads, for functions, generic functions, instance /
  static / `self` methods, interface methods, `optional` payloads (`maybe()?
  ?? 0`) and all of these imported from another module.
- `f"{word(s)?.toUpper()}"`: `?.` on a `Result` inside an f-string slot was
  lowered as safe navigation on the `Result` ("Word(s).ToUpper undefined");
  it is propagation then member access, as outside f-strings.
- Calling a method on a dict reached through a call, `?` or an index
  (`table()?.keys()`, `load().values()`, `rows[0].items()`) passed `lamc`
  and failed in `go build`; it is the same Lam `error[type]` ("dicts have no
  `keys()` method") as on a dict local.
- `??` results were untyped: `w = find(4) ?? 0` with `find() ->
  optional[int]` left `w` unknown, so `1.5 * (100 - w)` failed in `go build`
  ("1.5 (untyped float constant) truncated to int"), and chains, generic
  calls, optional lists / dicts and mixed int / float operands fell back to
  `interface{}`. `opt ?? fallback` now has the payload type for the compiler
  and the semantic checker (unannotated and annotated targets): value
  payloads, classes (a subclass fallback is upcast), optional fields,
  `optional[list[T]]` / `optional[dict[K, V]]` (an empty `[]` / `{}`
  fallback takes the payload type), generic functions returning
  `optional[T]` (`first(xs) ?? 0`, also with `T` a class), `load()? ?? 0`
  and functions of other modules; an int payload with a float fallback (or
  the reverse) is a `float`, `a ?? b ?? c` is `a ?? (b ?? c)` and two
  optionals of one type stay optional. The result composes with arithmetic,
  concatenation, f-strings, comparisons, arguments, `Result.Ok`, `return`,
  ternaries, lambdas, comprehensions and method calls
  (`(name() ?? "x").toUpper()`, `(lookup() ?? Admin("r")).greet()` runs the
  override), and member / assignment checks apply to it (`q: str = find(1)
  ?? 0` is a Lam error). `x: float = doc.n ?? 0` on an integral JSON number
  no longer panics.
- `optional[list[T]]` and `optional[dict[K, V]]` did not compile: returning
  or assigning a list / dict failed in `go build` ("cannot use xs as *[]int
  value"). They are boxed like value payloads, print their payload and
  unwrap with `??`.
- A generic function call's result type now carries the call's type
  arguments (`last([1, 2]) * 1.5` widens the `int`).
- A list or dict of a concrete type did not convert to `list[any]` /
  `dict[str, any]`, although the checker accepted it: `xs: list[any] = ys`
  with `ys: list[int]` (also `xs: list[any] = rows()?`) failed in `go build`
  ("cannot use ys (variable of type []int) as []interface{} value"). It
  widens implicitly, as in Python, to a converted **copy** (element writes
  and appends through it do not reach the original) at annotated
  assignments and reassignments, arguments (functions, methods, imported),
  returns, `Result.Ok(...)` payloads (`unwrap()` panicked), fields,
  `optional[list[any]]` targets, `extend` / `+=`, list / dict literal
  elements and nested lists (`list[list[int]]` to `list[list[any]]`), and
  `list[int]` where a `list[float]` is expected. Keys must still match and
  class element types stay invariant (Lam errors, as before).
- A generic class of one module in the `Result` return of another module's
  function or method (`func load(conn: Sql, n: int) -> Result[Box[Row]]`
  and `Table.fetch` in `users.lam`, `class Box[T]` in `box.lam`) failed in
  `go build` when called from a third library module ("undefined: Load",
  "t.Fetch undefined"): the import pre-scan did not know `Box` was generic,
  recorded the payload as `interface{}`, and the module's own transpile
  then saw two "overloads" and mangled the names. Every module's generic
  classes are now known before any signature is collected (also for the
  main file's method signatures).
- A list comprehension passed straight to an imported generic constructor
  (`Box[Row]([Row(n) for n in names], 3)` with `class Box[T]` in another
  module, in a method or a function) was emitted as `[]T`; the class's
  type parameters are bound to the explicit type arguments for imported
  classes too.
- Overriding a method of a generic base was rejected ("incompatible
  parameters: got (User), expected (T)") and, once accepted, did not
  dispatch: in `class Users(Registry[User])`, `row(self, item: User)`,
  `first(self) -> User`, `keep(self, xs: list[User])` and `find(self, k:
  str) -> optional[User]` override `row(item: T)`, `-> T`, `list[T]`,
  `optional[T]`. Override checks bind the base's type parameters to the
  subclass's type arguments (another type is still an `error[inheritance]`,
  naming the bound type), the override replaces the base variant instead
  of becoming an overload, and the base's own methods and calls through a
  `Registry[User]` value run it, also when the base is declared in another
  module. A generic intermediate class (`class Named[U](Registry[U])`,
  `class Tags(Named[str])`) no longer emits `&Registry[*U]{}` ("undefined:
  U") for the grandparent.
- Class-body field defaults of an ancestor declared in another module were
  skipped when the subclass has its own `init` (`notFound: str = "x"` in
  `Base`, `class Child(Base) { func init(self) {...} }` in another module
  gave `""`). Every ancestor's defaults are applied, ancestors first and
  before the `init` body, with or without `base.init()`, through a generic
  base too; the defining module evaluates them (in its namespace) through a
  `_lamFieldDefaults()` helper.
- A default parameter naming a module constant (`func pick(n: int = Size)`)
  failed in `go build` ("undefined: Size") when another module, or the
  caller, declares a constant of the same name, and an imported constant
  under an alias (`from cfg import Size as S`, `n: int = S`) was not
  found. Defaults resolve in the defining module: functions, constructors,
  instance and static methods, expressions over the constant, keyword
  calls, callers in the main file and in other library modules.
- `x == None` / `x != None` / `x is None` / `x is not None` (`None` on
  either side) now narrow an optional local or parameter like
  `isinstance(x, None)`: returning an `optional[html]` (or any optional)
  as its payload after a `None` check was refused, in functions and
  methods alike; a narrowed value returned or passed where an optional is
  expected is boxed again, a loop variable over a `list[optional[T]]`
  narrows too, and `n ?? -1` on a narrowed `n` is `n` (it emitted an
  `interface{}` that `value: int = ...` rejected). An optional *field* (`return self.cached` after `if
  self.cached == None`) passed the checker and compiled to broken Go
  ("cannot use s.Cached (variable of type *LamHTML) as LamHTML value");
  fields are not narrowed, so it is now the same Lam error as for a local
  (also for assignments and arguments), with a help suggesting a local
  copy or `??`.
- An imported module's relative import of a module that does not exist
  (`from .missing import Thing`, `from ..gone.deep import X`, `import
  .nokit as k`) was skipped and surfaced as a Go "undefined: Thing"; it is
  an `error[import]` ("module `.missing` could not be found") at the import
  line with the paths searched. So is an absolute import of a missing
  module in an imported module (`from lamsyncx import Mutex`), also skipped
  silently so far, even when another module of the build happens to provide
  its names: such an import binds nothing.
- Calling a function value with a subclass instance (`rate(book)` with
  `rate: func(Item) -> float` and `book: Featured`, `class Featured(Item)`)
  failed in `go build` ("cannot use book (variable of type *Featured) as
  *Item value"); the argument is upcast as for a declared function, for
  function-typed locals, parameters and fields (`self.rate(it)`), and the
  callee still runs the overrides.

## 4.1.1 — 2026-11-04

### Changed

- Tests: the PostgreSQL core test no longer connects to `localhost:5432`;
  it reads `LAM_TEST_POSTGRES_DSN` (a disposable database) and is skipped
  when the variable is unset (`# requires-env:` in the core runner).

### Fixed

- A function whose declared result is an interface of another module
  (`func requestDb() -> Sql`) could not be called from a third module: the
  defining module and its callers chose different Go names for it ("undefined:
  RequestDb_1_str_ret_ptrSql"). `optional[Sql]` of an interface is the
  interface value itself (not a pointer to it).
- `for row in conn.query(q)?` with `conn` typed by an interface (a parameter,
  a local or the result of a call) knows the rows' type, as for a class:
  it failed in `go build` ("cannot range over __q1.Value"); interface method
  results keep their `Result` payload and container type too
  (`conn.query(q).unwrap()[0]["id"]`, comprehensions over `conn.query(q)?`).
- Top-level names of two modules that differ only in the case of their first
  letter (`userColumns()` in one module, `UserColumns` in another; also
  classes, interfaces, constants, variables and private functions) map to the
  same Go identifier; they are now namespaced per module like same-name
  definitions instead of failing with a top-level symbol clash or a Go
  redeclaration.
- `import m as p` together with `from m import load` in one file, calling
  both `p.load()` and `load()` (or a `from m import load as ld` alias next to
  `load`), gave the function a second Go name that `m` never declared
  ("undefined: Load__lam2").
- Indexing an unannotated list literal directly (`[obj][0].name`,
  `[a, b][-1].label()`, `[1, 2][0] + 1`) typed the literal as `[]interface{}`
  and failed in `go build`.
- Async *method* calls in a list literal (`tasks = [a.run(1), b.run(2)]`,
  static, inherited, generic and imported methods) are handles, as async
  function calls already were; the list was typed by the methods' results and
  `go build` failed.
- A list literal of lists holding an empty list (`for n in [["a"], []]`,
  `[[], ["b"]][1]`) gives the empty list its siblings' type: passing `n` to a
  `list[str]` parameter panicked at run time ("interface {} is []interface {},
  not []string") and `len(n)` / `for s in n` failed in `go build`. Empty
  `{}` next to dict literals works the same way.
- Indexing a JSON value that is neither an object nor an array (null, a
  string, a number, a boolean) yields JSON null, like a missing object key,
  instead of panicking ("cannot index json null"): `doc.user.address.city ??
  "unknown"` works when `user` is null. Out-of-range array indexes and
  wrongly typed indexes on objects / arrays still fail at run time.
- `do { ... return a } catch err { ... return b }` whose body and handler both
  return (or raise) ends the function: it was reported as "not all paths in
  `f` return a value", and the generated Go had no final return.
- The key / value variables of `for k, v in d` over a typed dict (a
  `dict[str, str] = {}` parameter, a local, a field, or a comprehension over a
  dict literal) keep their types in expressions such as `len(v if c else k)`
  and `(k if c else v).toUpper()`, which failed in `go build`.
- A function that changes the length of a `list` parameter it does not return
  (`xs.append(v)`, `extend`, `insert`, `pop`, `xs += ys`) now gets a
  compile-time warning (`warning[list-param]`): Lam lists are Go slices passed
  by value, so the caller does not see the new length (element writes are
  shared). Return the list and reassign it at the call site, or keep it in a
  class field. Giving lists Python's reference semantics is planned for 4.2.

## 4.1.0 — 2026-11-03

### Added

- `bytes(...)` converts: `bytes(text)` is the UTF-8 encoding, `bytes(codes)`
  a `list[int]`, `bytes(data)` a copy, `bytes()` an empty value (an `any`
  holding one of these converts at run time). It passed the checker and
  failed in `go build` ("undefined: Bytes").
- `bytes` values index (`data[0]` is a `byte`, negative indexes count from
  the end), slice with Python semantics, join with `+` / `+=`, compare with
  `==` / `!=`, and test membership of a byte value or a sub-sequence with
  `in` (`104 in data`, `bytes("ll") in data`); all of them were rejected by
  the checker. `x in data` with a `str` needle, `data + "x"`, a non-`int`
  index and `bytes(2.5)` are compile errors.
- List literals and comprehensions of ints build `bytes` where `bytes` is
  expected (`data: bytes = [b + 1 for b in raw]`, `[len(payload)] + payload`,
  `bytes([104, 105])`); they failed in `go build` ("cannot use x (variable of
  type int) as byte value"). A constant outside 0..255 is a compile error and
  a computed one throws `ValueError` instead of wrapping around.
- `Fmt.formatFloat(f, decimals = -1)`: a negative `decimals` (now the default)
  gives the shortest text that reads back as the same float, without an
  exponent (Go's `strconv.FormatFloat(f, 'f', -1, 64)`: `1234567.5`, `2`,
  `0.00001`, `1000000000000000000000`). Non-negative `decimals` are unchanged.
- `Os.waitForSignal(names = ["SIGINT", "SIGTERM"]) -> str` blocks until one of
  the signals arrives and returns its name; `Context.onSignals(names = ["SIGINT",
  "SIGTERM"]) -> Context` is a root context cancelled by the first of them,
  with the signal name as `reason()`, for graceful shutdowns. `SIGINT`,
  `SIGTERM`, `SIGHUP` and `SIGQUIT` are supported (prefix and case optional;
  unknown names throw `ValueError`); after the first signal the default
  handling is back, so a second Ctrl-C ends the process.
- `lamdb.Row`: typed reads of database rows — `Row.int`, `Row.float`,
  `Row.str`, `Row.bool` (with a `fallback`), `Row.unix` and `Row.time`
  (`optional[DateTime]`) for timestamps and timestamp texts,
  `Row.optionalStr` / `optionalInt` / `optionalFloat` / `optionalBool` for
  nullable columns, `Row.isNull`, and `Row.strList` / `Row.intList` for
  PostgreSQL arrays (`{a,"b c",NULL}`). They read every value the drivers
  return (`int64`, `float64`, `bool`, text including PostgreSQL `NUMERIC`,
  `uuid`, arrays and `bytea`, `time.Time`, NULL), plus `[]byte` and
  `math/big` numbers. `Db.textArray(values)` writes a `text[]` parameter
  (`?::text[]`) and `Db.parseTextArray(text) -> Result[list[str]]` reads one.
- `Crypto.bcryptHash(password, cost = 10) -> Result[str]`,
  `Crypto.bcryptVerify(password, encoded) -> Result[bool]` (a wrong password
  is `Ok(false)`, a malformed hash an error, like `verifyPassword`) and
  `Crypto.bcryptCost(encoded) -> Result[int]` for `$2a$` / `$2b$` / `$2y$`
  hashes stored by other systems. `lamcrypto` now depends on
  `golang.org/x/crypto`, pinned at v0.48.0 (the newest release that builds
  with Go 1.24); projects can raise it in `lamlib.toml`.

### Changed

- `str(data)`, `print(data)` and f-strings show a `bytes` value as its UTF-8
  text (`hello`), like Python's `bytes.decode()`, instead of Go's list form
  (`[104 101 108 108 111]`); `repr(data)` keeps the Go form.
- Programs that import `lamdatetime` or `lamcron` (or `lamdb`, whose
  `Row.time` returns a `DateTime`) embed Go's zone database (`time/tzdata`,
  about 450 KB), so `"Europe/Rome"` and the other IANA zones (`DateTime`,
  `CRON_TZ=...`) resolve in minimal containers without system tzdata; a
  `go! { import _ "time/tzdata" }` is no longer needed. A system zone
  database or `ZONEINFO` still takes precedence.
- Catalog: the official packages are tested with lamc 4.1 (their test suites
  pass unchanged).

### Fixed

- `print(...)` and f-strings showed the Go pointer (`0xc000014070`) of a
  static or instance method returning `optional[str]` / `optional[int]` /
  `optional[float]` / `optional[bool]`, and `?? fallback` on such a call kept
  the pointer; the value (or `<nil>`) is shown now.
- `value == None` / `!= None` on an `optional[Cls]` whose class defines
  `__eq__` called `__eq__` and crashed on the nil value; it is a nil test.

## 4.0.6 — 2026-11-03

### Fixed

- A local declared inside a loop or a branch and returned from there
  (`c: json = items[k]` in a `while`, then `return c`) was typed `unknown`:
  a `json` return was rejected as not JSON-compatible and other mismatches
  were left to `go build`. Return values are now checked with the types the
  `return` sees.
- Indexing, `len()`, attribute access and assignment on a `json` class field
  reached through an element, a call or `?` (`docs[0].body[i]`,
  `shelf.items[0].body`, `load()?.body`) failed in `go build` ("cannot index
  ... struct type LamJSON"), and `v: json = c.body[i]` was typed `unknown`.
  A `json` field now behaves like a `json` local, so `"k" in doc.body` (an
  always-false reflection test) is reported like it is for a local, and
  slicing a `json` value (`value[1:]`, invalid Go before) is a Lam error.
- Typed locals in a constructor (`base: int = n * 2` in `init`) were hoisted
  as fields whose initializer ran at the top of the Go constructor, before
  the locals it reads ("undefined: base" in `go build`); they also leaked
  into the struct. They are plain locals now.
- An imported module could use a function, constant, class or interface of
  another module without importing it, and import a name its target module
  does not declare (`from .x import lxNumber`): every module compiles into
  one Go package, which resolved them, and only the main file was checked.
  Imported project and `extlibs` modules are now checked for undefined names,
  unknown annotation types and unresolvable imports, with the import to add
  (`from app.web.forms import queryValues`); in the
  main file a class of a module imported for another name is no longer
  accepted as an annotation. Raw `go!` code is not checked.
- Module export facts ignored every declaration after a brace inside a
  string literal or a comment (`text.startsWith("{")`), so importing them
  was reported as `does not export`.

## 4.0.5 — 2026-11-03

### Fixed

- `?` did not short-circuit: in `flag and check()?`, `flag or check()?` and
  `f()? if c else x` the call ran (and could return early) even when the left
  side or the condition decided the result. The call and its early return now
  run only when that operand is evaluated.

## 4.0.4 — 2026-11-03

### Fixed

- 4.0.3 regression: a dict literal holding `?` appended to a `list[json]`
  (`rows.append({"doc": Json.decode(text)?})`) was no longer converted to
  `json` and failed in `go build`.

## 4.0.3 — 2026-11-03

### Fixed

- `xs.append(f()?)` (also `extend` / `insert`) evaluated `f()` twice: its side
  effects ran twice and an error was returned only after the second call.
- `card.label() + " >"` where a method returns `html` / `xml` was lowered as a
  Go string `+` and failed in `go build`; markup joins now use the checker's
  types.
- `value: json = doc["b"] ?? None` and indexing the json a function returns
  (`doc()["a"]`) failed in `go build`.

### Changed

- Catalog: `@lam/vulture` 3.1.0 (`HEAD` answered by GET routes,
  `req.queryAll` for repeated query parameters).

## 4.0.2 — 2026-11-03

### Fixed

- `user: optional[User] = find()?` panicked when `find` returned
  `Result.Ok(None)` (the nil payload was type-asserted to `*User`), and an
  `optional[int]` payload holding a plain `Result.Ok(0)` was asserted to
  `*int`. Optional payloads now unwrap to `None` / a pointer to the value.

## 4.0.1 — 2026-11-03

### Fixed

- `lamc migrate up` / `down` / `status` failed before touching the database:
  the program the CLI compiles still used the lamdb/lammigrate API from
  before `Result` (`db: Db = Db.connect(...)` is "cannot assign `Result[Db]`
  to `Db`"). It now propagates errors and exits with code 1 and the message
  (an unreachable database, a failing statement) instead of panicking.
  Covered end to end on SQLite by `tests/tests/test_migrate_cli.py`.

## 4.0.0 — 2026-11-03

### Added

- Native `html` and `xml` types, siblings of `json`. Markup is written
  directly between `<>` and `</>` with `${expr}` slots: text is escaped
  wherever it lands (content or attribute value), `html`/`xml` values and
  lists of them are inserted as markup, `true`/`false`/`None` as a whole
  attribute value write or drop a bare attribute, and `<script>`/`<style>`
  content stays JavaScript/CSS (a `json` value there is written as JSON, and
  nothing inserted can close the element). `\$` writes a dollar sign. A
  literal's kind comes from its target (annotation, parameter, return type,
  list element type, `+` operand, DOM method receiver, enclosing literal);
  without one it is `html`. HTML is read leniently (void and implicitly
  closed elements, stray end tags ignored, nothing moved); xml literals must
  be well formed (checked by the compiler).
- `html(value)` / `xml(value)` and the `__html__` / `__xml__` protocol (like
  `__json__`); `+`, `+=`, `==`, `str()`, f-strings and JSON encoding.
- DOM-style methods on markup values: `select` / `first` with CSS
  selectors, `text`, `tag`, `attr`, `attrs`, `hasAttr`, `setAttr`,
  `removeAttr`, class helpers, `setText`, `setInner`, `append`, `prepend`,
  `remove`, `replaceWith`, `children`, `nodes`, `parent`, `clone`,
  `isEmpty`.
- Compile-time diagnostics for markup: slots in tag/attribute names,
  comments or doctypes, markup values in attributes or scripts, `html`
  inside `xml` (and the reverse), classes without `__html__`/`__xml__`,
  malformed XML literals, unknown DOM methods and wrong arities, an
  unterminated `<>`. `lamc fmt` keeps literals exactly as written; the VS
  Code grammar highlights them as HTML with Lam slots.
- Stdlib: `lammarkup` (runtime, imported automatically), `lamhtml`
  (`Html.parse`, `Html.escape`), `HttpRequestOptions.html/xml` and
  `HttpRequest.html/xml` (bodies with their Content-Type),
  `HttpResponse.html()` / `.xml()`, `Xml.escape`.
- Parenthesized types: `(func(R) -> html) | (func(R) -> str)` is a union of
  function shapes (without parentheses the result type takes the rest of
  the union, as before). Diagnostics render such unions with parentheses.

### Breaking changes

- `Xml.parse(text)` returns `Result[xml]` (a native value with the DOM
  methods); `XmlNode` is removed. `Xml.encode(value, root)` returns `xml`
  (use `str(...)` for the text). `Xml.decode(text)` is unchanged.
- `HtmlTemplate.render` / `renderNamed` return `Result[html]`.
- `Mail.setHtml` takes `html` (`m.setHtml(<><p>Hi</p></>)`).
- `html` and `xml` are type names: a variable or function named `html` /
  `xml` shadows the conversion builtins.

### Fixed

- A function passed where a union of function types is expected
  (`handler: func(R) | (func(R) -> str)`) had to fit none of them: it is now
  checked, and a shape without a result only takes functions that return
  nothing.

### Changed

- Catalog: `@lam/stripe` 1.0.1, `@lam/otel` 1.1.0 (split into modules),
  `@lam/keyring` 1.0.1, `@lam/totp` 1.0.1 and `@lam/jobs` 1.0.1 drop their
  lamc 2.1 workarounds and require lamc 3.0.
- Catalog: `@lam/sftp` 3.6.0 (host key rotation, user certificates,
  `fsync@openssh.com`).

## 3.0.1 — 2026-10-20

### Changed

- Catalog: `@lam/s3` 2.4.0 (S3 Object Lock: retention, legal holds, Object
  Lock buckets; version listing and version deletes) and 2.5.0
  (`blockPrivateNetworks` for endpoints supplied by users).

### Fixed

- A `for` loop over a list literal of call results (`for q in
  [Queue.make("a"), base.twin("b")]`, also unwrapped Results) typed the loop
  variable `interface{}`, so method calls on it failed in `go build`. Static
  and instance methods returning a class now give their type to expression
  hints, and the loop ranges over a typed slice when every element shares
  one type. A literal mixing known types (`[Queue.make("z"), "text"]`) is
  iterated as `interface{}`; it used to be built as `[]string` and failed in
  `go build`.
- Module constants had no type inside json literals and json arguments
  (`{"schemas": [Schema]}` was "value of type `unknown` is not
  JSON-compatible"), also when declared below the function using them; a
  constant now has its declared type, or the type of its value.
- Chained member reads (`env.cfg.endpoint`) had no type in json literals;
  the checker now follows the chain through typed fields (also across
  modules).

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
