# Lammergeier → Go transpilation rules

This document describes, for each Lammergeier (`.lam`) construct, the Go code
that the compiler emits. It is the authoritative reference for anyone who
wants to:

- Understand what a snippet compiles to without running the pipeline.
- Write stdlib modules that call through to specific Go APIs.
- Debug a build that produced invalid Go.

The compiler lives under `compiler/`, with the transpiler entry-point in
`compiler/transpiler.py` and the visitor mixins in
`compiler/visitors/{helpers,statements,expressions,definitions}.py`.

## Pipeline overview

1. **Preprocessor** (`compiler/preprocessor.py`) pulls `go! { ... }`, `go! :`
   and inline `go!(expr)` blocks out of the source. Each block is replaced
   with a placeholder call (`__go_block__("N")` / `__go_inline__("N")`),
   and the raw Go source is stored in a side table keyed by `N`.
2. **Parser** – `lammergeier.lark` feeds Lark to produce a parse tree over
   the preprocessed source.
3. **Transpiler** walks the tree and emits Go. Go blocks are re-inserted
   verbatim in place of their placeholders.
4. **Formatter + compiler** (`go fmt` + `go build`) produce the final
   binary.

## Source maps

Every statement the transpiler visits emits a Go `//line` pragma pointing
back at the owning `.lam` source file and line. The effect:

- `go build` errors are printed with the `.lam:N` position, which the
  driver (`compiler/lammergeier.py`) unwraps into the familiar
  `line N: <message>` report with surrounding source context.
- Runtime panics include the `.lam:N` position in the goroutine trace
  (e.g. `main.main() /path/to/foo.lam:3 +0x17`).
- Multi-line IIFE expansions (list comprehensions) stay pinned: each
  internal Go line carries the same `//line` directive so errors deep
  inside a comprehension still report the comprehension's own source
  line, not a drifted line further down the file.

Library modules are mapped with their own paths, so an error triggered
inside `helper.lam` is printed as `helper.lam:2: ...` instead of being
attributed to the importing main file.

Multi-line parenthesised expressions currently pin to the *first* line
of the expression (where Lark records the statement position). A future
pass may tighten this to the exact argument line.

## Library cache

Transpiling each `.lam` library (parse, AST walk, emit Go, strip `main`,
collect metadata) is the most expensive phase of a cold build. The
compiler caches that work on disk in a content-addressed store so
warm builds skip the whole pipeline for every unchanged library.

- **Key** is `sha256(compiler_version || lib_content)`. The
  `compiler_version` component is itself a digest over every `.py`
  under `compiler/` plus `lammergeier.lark`, so any compiler or
  grammar change transparently busts every cached entry.
- **Value** is a JSON blob with the emitted Go source plus the
  metadata sets (`_class_names`, `_static_methods`, `_static_vars`,
  `_func_defaults`, `_func_param_counts`, `_variadic_functions`) that the main-file
  transpiler re-injects before walking the user's code.
- **Location** is `$XDG_CACHE_HOME/lammergeier/libs/` by default,
  overridable with `LAMC_CACHE_DIR`.

Default parameter values are lowered to their Go source strings before
caching — the in-memory representation uses Lark `Tree` nodes which
aren't JSON-serialisable. `_fill_default_args` accepts either shape so
the on-the-fly and cached paths converge.

Raw `go! { ... }` blocks were originally assumed uncacheable because
their placeholder ids are build-local, but the transpiler inlines each
block's raw Go source at emit time — the cached `go_src` is therefore
self-contained and safe to reuse.

Because the key is content-only, two identical copies of a library at
different paths (for example a vendored `extlibs/` copy and its source
checkout) share one entry. On a cache hit the compiler rewrites the
entry's `//line` directives to the file actually being compiled, so Go
errors and diagnostics always point at the current copy.

CLI flags:

- `--no-cache` bypasses the cache for a single build (useful when
  debugging a library the compiler just miscompiled into the cache).
- `--clear-cache` deletes every cached entry and exits.

## Generics

Lam type parameters lower straight to Go 1.18 generics:

| Lam                                                         | Go                                                                   |
|-------------------------------------------------------------|----------------------------------------------------------------------|
| `func foo[T](x: T) -> T`                                    | `func Foo[T any](x T) T`                                             |
| `func foo[T: ordered](a: T, b: T)`                          | `func Foo[T ~int \| ~int64 \| ~float64 \| ~string](a T, b T)`        |
| `class Box[T] { ... }`                                      | `type Box[T any] struct { ... }`                                     |
| method of `Box[T]`                                          | `func (s *Box[T]) Method(...)`                                       |
| auto-constructor for `Box[T]`                               | `func NewBox[T any]() *Box[T] { return &Box[T]{} }`                  |
| `Box[int](x)` at call site                                  | `NewBox[int](x)`                                                     |
| static method of `Box[T]`                                   | `func Box_method[T any](...)` — class's T is threaded through        |
| nested `func id[T](x: T) -> T`                              | hoisted helper `func __lam_nested_N_id[T any](x T) T`                |

The transpiler tracks generic classes in `_generic_classes` (class → Go
clause) and the set of type-parameter names currently in lexical scope
in `_generic_names`. The field-collection pass registers class-level
clauses early so init-parameter and field types can reference `T`
before the class body is visited; the visit pass saves/restores
`_generic_names` per function and per class so sibling generics don't
collide. `_type_expr_to_go` treats an in-scope `T` as a literal
identifier rather than an unknown user class.

Generic-constructor call sites (`Pair[int, str](a, b)`) parse as
`funccall(getitem(Pair, <types>), args)`. The call-site visitor
intercepts that shape and emits `NewPair[int, string](a, b)` directly
rather than letting the naive expression lowering drop commas.

Generic nested functions cannot lower to Go function literals because
Go does not permit type parameters on anonymous functions. The
transpiler therefore hoists each nested generic helper to a generated
top-level Go function named `__lam_nested_<n>_<name>`. Any typed locals
captured from the enclosing Lam function become hidden leading
parameters at the Go call site, while Lam code keeps using normal nested
syntax:

```lam
func main() {
    offset: int = 3
    func addOffset[T: int](x: T) -> int {
        return int(x) + offset
    }
    print(addOffset[int](7))
}
```

```go
func __lam_nested_1_addOffset[T int](offset int, x T) int {
    return int(x) + offset
}
```

Calls with explicit type arguments (`addOffset[int](7)`) and calls where
Go can infer type arguments (`id(7)`) are both supported.

## Go dependency pins

Project manifests can declare Go module pins in `[go-deps]`, and
installed libraries can contribute resolved pins through
`lamlib.lock.toml`. The compiler also knows about Go modules used by the
standard library, but it only seeds pins for the stdlib modules actually
reached by the Lam import graph. Importing a lightweight core module
such as `lamstrings` therefore does not pre-populate `go.mod` with
unrelated data, database, cache, or protobuf dependencies.

Manifest and lockfile pins still take precedence over stdlib defaults.
After the compiler writes the initial `go.mod`, Go's normal module
selection and `go mod tidy` decide the final transitive set.
If `go mod tidy` cannot resolve those modules, Lammergeier stops at
module resolution and prints the Go diagnostic as a compiler error instead of
continuing to a later, misleading `go build` failure.

This failure path is tested without relying on live network state by
monkeypatching the driver-level `go mod tidy` subprocess. The contract is that
`go build` is not attempted after a module-resolution failure, and the user
still sees the underlying Go diagnostic under Lammergeier's
`Go module resolution failed` heading.

## Naming

| Lam identifier            | Go identifier            | Notes                                   |
|---------------------------|--------------------------|-----------------------------------------|
| `my_func` (top-level)     | `My_func`                | Title-cased first letter; rest verbatim.|
| `_helper`                 | `_helper`                | Leading underscore kept, no rename.     |
| `init` / `__init__`       | `Init` / `NewX`          | `init` is preferred; both constructors become `NewClass`. |
| `__str__`, `__repr__`     | `String`                 | Satisfies `fmt.Stringer`.               |
| Other dunder ops          | `Add`, `Sub`, `Eq`, ...  | See `DUNDER_OPS` in `constants.py`.     |
| `self`                    | `s`                      | Fixed receiver name in all methods.     |
| `Class` / `Class.method`  | `Class` / `Method`       | Methods are public unless prefixed `_`. |
| `private func foo()`      | `foo`                    | Go unexported (lowercase initial).      |

Classes always become pointer receivers: a constructor returns `*Class`, and
methods are defined on `(s *Class)`.

Preferred Go names stay unchanged. If casing, a class/function boundary,
operator lowering, a field/method pair, a static namespace, or an overload
wrapper would reuse an emitted symbol, the later deterministic entry receives
a readable `__lamN` suffix. For example, `userID` / `UserID` become `UserID` /
`UserID__lam2`, while colliding `value` / `Value` fields become `Value__lam2` /
`Value`. Calls, constructors, inheritance, operator dispatch, and
`LAMMERGEIER.*` all consult the same mapping.

When a Lam class value is recovered from an `any` field, such as
`e: Error = result.error`, the transpiler emits a Go type assertion
(`result.Error.(*Error)`). The assertion is skipped for fields already known to
have the exact pointer type, so passing `self.retryPolicy: RetryPolicy` into a
constructor remains a direct `s.RetryPolicy` value rather than an invalid
double assertion.

Go-only keywords may be used as Lam names. Declarations that naturally become
non-keywords keep their preferred mapping (`func select()` → `Select`, static
`switch` → `Query_switch`); raw lexical bindings receive a readable `__lam`
suffix (`chan` → `chan__lam`, private `select` → `select__lam`). Parameters,
locals, destructuring/loop/comprehension targets, catch/with bindings, and
compiler-prefix-looking names all route through this binding map.

## Types

| Lam              | Go                          |
|------------------|-----------------------------|
| `int`, `int8`…   | `int`, `int8`…              |
| `uint`, `uint8`… | `uint`, `uint8`…            |
| `float`          | `float64`                   |
| `float32`        | `float32`                   |
| `str`, `string`  | `string`                    |
| `bool`           | `bool`                      |
| `byte`, `rune`   | `byte`, `rune`              |
| `bytes`          | `[]byte`                    |
| `None`           | (empty; implies void/`nil`) |
| `any`, `object`  | `interface{}`               |
| `error`          | `error`                     |
| `list[T]`        | `[]T`                       |
| `dict[K, V]`     | `map[K]V`                   |
| `set[T]`         | `map[T]bool`                |
| `json`           | `LamJSON` (canonical recursive JSON wrapper) |
| `optional[T]`, `Option[T]` | `*T` for value types; an existing class pointer is not doubled |
| `Result[T]`      | `*Result` (payload metadata is compile-time only) |
| `T \| None`      | `T` (pointer if applicable) |
| `Callable[[A], B]` / `func(A) -> B` | `func(A) B` |

`list`, `dict` and `set` without type arguments default to
`[]interface{}`, `map[string]interface{}` and `map[interface{}]bool`
respectively. See `TYPE_MAP` in `compiler/constants.py`.

### Typed collection contexts

Anonymous collection literals and comprehensions have a broad fallback when no
expected type is available: lists become `[]interface{}`, dicts become
`map[K]interface{}` or `map[string]interface{}`, and sets become
`map[interface{}]bool`. When an enclosing Lam construct supplies a type, the
transpiler lowers the literal recursively to that exact Go shape instead.

Typed contexts include annotated declarations, returns, reassignments, indexed
container writes, slice replacement, default arguments, function/method/static
method/constructor call arguments, variadic splats, explicit lambda returns,
and list helpers such as `map`, `pop`, and `sorted`.

```lam
func sum(values: list[int]) -> int { ... }
func size(ids: set[int]) -> int { return len(ids) }

sum([4, 8, 15])           # []int{4, 8, 15}
size({1, 2, 2})           # map[int]bool built by an IIFE
rows[0:1] = [[6, 8]]      # replacement is lowered as [][]int
```

Set values remain Lam sets even though they lower to Go maps. User code should
test `value in ids`; `ids[value]` is rejected by semantic analysis instead of
leaking a Go map-indexing detail.

The semantic checker reports incompatible element/key/value types in those
contexts before Go is invoked; the transpiler still applies the contextual
lowering when semantic checking is disabled so valid Lam source does not fall
through into invalid Go.

The incremental typed IR records those contexts as `TypedExpr.expected_type`
for annotated initializers, const initializers, and typed reassignments. The
current Go emitter still returns strings, but the tested contract is that any
site with a known target type reaches `_typed_value_to_go` rather than lowering
the value directly through `_expr_to_go`. The unit gate has a static
`test_lowering_contracts.py` check for these choke points and generated-Go
contract tests for the cases most likely to regress: anonymous collection call
arguments, contextual `?` unboxing, and class-pointer argument passing.

### Native JSON lowering

A `json` annotation triggers the compiler-managed `lamjson` support module and
lowers to `LamJSON`, a wrapper around canonical Go JSON values
(`map[string]interface{}`, `[]interface{}`, primitives, or nil). Contextual
literal lowering emits string-keyed maps and interface slices, then
`lamJSONMust(...)` recursively validates and normalizes the value. The builtin
`json(value)` uses the same normalizer. A Lam `__json__()` method lowers to the
stable internal Go protocol `LamJSON() LamJSON`; custom classes require the
explicit `json(instance)` conversion.

```lam
payload: json = {"user": {"name": "Ada"}, "scores": [8, 13]}
name: str = payload.user.name
payload["active"] = true
```

Conceptually lowers to:

```go
var payload LamJSON = lamJSONMust(map[string]interface{}{...})
var name string = lamJSONRaw(lamJSONGet(lamJSONGet(payload, "user"), "name")).(string)
lamJSONSet(payload, "active", lamJSONMust(true))
```

`LamJSON` implements `json.Marshaler`, `json.Unmarshaler`, `fmt.Stringer`,
`driver.Valuer`, and `sql.Scanner`. This lets `encoding/json`, HTTP responses,
Redis helpers, and JSON/JSONB database parameters consume the same native value
without exposing ordinary dictionaries. `Json.toDict` / `toList` deliberately
unwrap it when collection algorithms are desired.

## Variable declarations

```lam
x: int = 10
name: str = "Alice"
items: list[int] = [1, 2, 3]
```

→

```go
var x int = 10
var name string = "Alice"
var items []int = []int{1, 2, 3}
```

Without a type annotation the compiler falls back to `:=` inference.
Re-assignment (`x = 5`) always emits a plain `=`.

### Discard target (`_`)

`_` is the Go blank identifier — it's already declared by the
runtime, so the transpiler never decorates it with `:=`. The
assignment-target visitor (`_visit_assign` in
`compiler/visitors/statements.py`) detects a single bare `_` LHS
and emits `_ = expr` instead of the usual `:= / =` choice:

```lam
_ = side_effect()
```

→

```go
_ = side_effect()
```

This keeps "call for side effects" idiomatic without needing a
dummy variable, and avoids the `no new variables on left side of :=`
build error that would otherwise fire.

## Constants (`const`)

```lam
const PI: float = 3.14
const COUNT = 10        # type inferred from the literal

func main() {
    const greeting = "hello"
}
```

→

```go
var PI float64 = 3.14
var COUNT int = 10

func main() {
    var greeting string = "hello"
}
```

The transpiler emits a Go `var`, not a Go `const` — Go's `const`
requires a compile-time evaluable RHS, while Lam allows any
expression. Immutability is enforced earlier, by the semantic checker
(see `compiler/semantic.py`): every scope tracks a `const_names` set
and any later `assign_stmt`, `augassign`, `annassign`, or
`const_stmt` targeting one of those names is rejected with
`cannot reassign constant `…`` or `redeclaration of constant `…``.
The protection is on the *binding*, not the value: `arr[0] = 1` and
`obj.field = 2` are still allowed when `arr` and `obj` are const.

## Function types

```lam
op: func(int, int) -> int = add
ops: list[func(int, int) -> int] = [add, mul]
cb: func()                = noop
```

→

```go
var op func(int, int) int = Add
var ops []func(int, int) int = []func(int, int) int{Add, Mul}
var cb func() = Noop
```

The grammar rule is `type_func: "func" "(" [type_func_params] ")" ["->" type_expr]`,
so the parameter list is wrapped in its own AST node and the return is
optional. `_type_expr_to_go` walks the children explicitly, mapping
each parameter type and joining them as `func(P1, P2, ...) R` (or
`func(P1, P2, ...)` when the return is absent). Function types may
appear anywhere a regular type does — parameters, return clauses,
variable annotations, generics, container element types.

`int(x)` / `float(x)` / `bool(x)` lower by the static type of `x`
(`_conversion_to_go` in `compiler/visitors/expressions.py`): numerics use Go
conversions (`bool(n)` → `n != 0`); `bool` → an inline `1`/`0` function; `str`
→ an inline `strconv.Atoi` / `ParseFloat` / `ParseBool` call that panics with
`ParseError: cannot parse ...` (no stdlib import is forced); `json` →
`lamJSONToInt` / `lamJSONToFloat` / `lamJSONToBool` from `lamjson`; a class
defining `__int__` / `__float__` / `__bool__` → that method; a declared `any` →
an inline type switch accepting every integer kind (`int`) / floating kind
(`float`) / `bool`, panicking with `TypeError: int(...) expects an integer, got
<type>` otherwise.

`Result[T]` is a compile-time payload annotation rather than a runtime Go
generic. The stdlib class still has two `any` fields (`value`, `error`), so
`Result.Ok(v)` and `Result.Err(e)` lower to ordinary static-method calls and
store payloads as `interface{}`. The compiler carries `T` through local,
imported, overloaded, and propagated calls for Lam-side member/type checks;
contextual lowering inserts a Go assertion when an annotated assignment,
return, call argument, or collection element needs one, including values read
through `unwrap()` or `unwrapOr(...)`. Inline `unwrap()` / `unwrapOr(...)` on
a receiver whose payload is statically known — a local or parameter annotated
`Result[T]` (tracked in `_var_result_payloads`), or a call whose declared
payload is in `_result_payload_go_types` (per overload variant) — lowers to
`x.Unwrap().(T)`; as a bare statement the assertion is omitted. With a target
of `Result[T]`, `Result.Ok(<collection literal | comprehension | ternary>)`
lowers its argument through `_typed_value_to_go` with `T`, so the stored value
has the concrete Go type later assertions expect. Direct `.value`, `.error`,
and `unwrap()` / `unwrapOr(...)` on payload-unknown or `any` results retain
the dynamic fallback:

```lam
err: Result = Result.Err({"code": 400})
raw: any = err.error
```

## Control flow

### `if` / `elif` / `else`

```lam
if x > 0 {
    print("pos")
} elif x == 0 {
    print("zero")
} else {
    print("neg")
}
```

→

```go
if x > 0 {
    fmt.Println("pos")
} else if x == 0 {
    fmt.Println("zero")
} else {
    fmt.Println("neg")
}
```

When a local is assigned on every continuing branch, Lam treats it as
available after the control-flow join. Go does not leak block-local
declarations, so the emitter hoists one typed `var` before an `if` or exhaustive
unguarded-wildcard `match` and lowers branch writes to assignments. Branches
that return or raise do not participate in the join.

### `for` / `while`

`for x in iterable` becomes `for _, x := range iterable` for slices/channels
and `for x := range iterable` for dict/set-shaped maps, so a single loop
target receives keys. Use `for k, v in mapping` when both map keys and values
are needed.
`for i, x in enumerate(xs)` becomes `for i, x := range xs`.
`for i in range(n)` becomes `for i := 0; i < n; i += 1`.
`while cond` becomes `for cond`.
Both loop kinds support an `else` suite, which is emitted using a small
`broke` boolean guard — see `_for_else_break` / `_while_else_break` in the
transpiler. A statically non-empty literal or integer `range` can promote
body assignments to the continuing outer flow when the loop contains no
`break`/`continue`; the emitter hoists matching typed storage. Empty, unknown,
or early-exit loops keep conservative assignment state.

### `try` / `catch`

`try`/`catch`/`finally` blocks compile to a Go closure with `defer
recover()` (the keyword is `catch`, not `except`):

```lam
try {
    do_thing()
} catch ValueError as e {
    print(e)
} finally {
    cleanup()
}
```

→

```go
func() {
    defer func() {
        if r := recover(); r != nil {
            e := fmt.Sprintf("%v", r)
            _ = e
            fmt.Println(e)
        }
        cleanup()
    }()
    do_thing()
}()
```

`raise` and `throw` (its alias) both become `panic(...)`; Python
exception types (`ValueError`, `TypeError`, …) are formatted as
`fmt.Sprintf("Name: %v", arg)`. The two keywords share a single
`raise_stmt` AST node, so downstream passes don't distinguish them.

### `?` (Result propagation)

The postfix `?` operator desugars to a temp + early-return inside the
enclosing function. Each `?` allocates a fresh `__qN` from the
transpiler-global counter:

```lam
func double(s: str) -> Result {
    n: int = parseInt(s)?
    return Result.Ok(n * 2)
}
```

→

```go
func Double(s string) *Result {
    __q1 := ParseInt(s)
    if !__q1.Ok() { return __q1 }
    var n int = __q1.Value.(int)
    return Result_Ok(n * 2)
}
```

The `.Value.(int)` cast is injected automatically when the immediate typed
context supplies a target Go type. Typed assignments, returns, call arguments,
and collection elements all route through `_typed_value_to_go`, which publishes
the expected type for the `propagate` branch in `_expr_to_go`. In `any`
contexts, such as `return Result.Ok(parseInt(s)?)`, the immediate argument type
wins over the enclosing `-> Result` return type and the substitution stays
plain `__qN.Value`.

A signature-level payload marker such as `Result[int]` lowers to the same
`*Result` shown above. The marker improves Lam-side inference but is erased
because the runtime `Result.Value` and `Result.Error` fields remain
`interface{}`. At most one payload marker is accepted. For an unannotated
assignment, the compiler carries the payload through local/imported function
and static-method metadata, emits the matching assertion (for example
`__q1.Value.(*Document)`), and records the concrete receiver for later member
and default-argument dispatch. Public payload-class metadata is harvested from
the resolved module without creating a user-scope binding, so callers import
only the function they use. Overloads may declare different payloads
(`parse(text: str) -> Result[str]` next to `parse(doc: json) -> Result[json]`),
so payload metadata is recorded per overload variant and the `?` site resolves
the variant from the argument types exactly like call dispatch does; an
overloaded call whose variant cannot be resolved keeps the plain
`__qN.Value` instead of asserting another variant's type. This metadata is part of the versioned library
cache so warm builds behave like cold builds. Before emission, the semantic
pass also rejects known non-`Result` operands, while unknown/`any` operands
retain the dynamic fallback. The same expression model validates propagated,
ternary, comprehension, operator/generic, and instance-method return values.
Known non-void function/static/inferred-instance calls used as bare statements
produce Lam warnings; dropped `Result`/`Option` calls use handle-or-discard
wording instead of surfacing as generated-Go noise.

Multiple `?` in one statement just allocate consecutive temps
(`__q2`, `__q3`, …); the prelude lines are emitted in source order so
each guard runs before later operands evaluate.

### `do { } catch err { }`

A `do`/`catch` block lowers to a Go IIFE with return type `*Result`,
followed by a check on the IIFE's `.Error` field:

```lam
do {
    n: int = parseInt(s)?
    print(n)
} catch err {
    print(f"failed: {err}")
}
```

→

```go
__rdo1 := func() *Result {
    __q1 := ParseInt(s)
    if !__q1.Ok() { return __q1 }
    var n int = __q1.Value.(int)
    fmt.Println(n)
    return Result_Ok(nil)            // implicit success-tail
}()
if __rdo1.Error != nil {
    err := __rdo1.Error
    fmt.Println(fmt.Sprintf("failed: %v", err))
}
```

Inside the IIFE, the same `?` lowering applies — but the `return __qN`
exits the closure, not the surrounding function, so the error pops
out as the IIFE's value. `_visit_do_stmt` pushes a fresh
`declared_vars` scope around both the body and the handler so locals
don't leak between consecutive `do` blocks. The source must directly
`from lamerrors import Result` for the emitted `*Result` /
`Result_Ok` references to resolve; scoped imports are accepted, while a
transitive import is deliberately insufficient.

### `match`

`match x { case 1 { ... } case _ { ... } }` compiles to a Go `switch`
with each case body inlined.

## Functions

```lam
func add(a: int, b: int = 2) -> int {
    return a + b
}
```

→

```go
func Add(a int, b int) int {
    return a + b
}
```

- Default arguments are not part of the Go signature. The compiler records
  the defaults in `_func_defaults` and injects them at every **call site**
  that omits the trailing arguments.
- Variadics (`func f(xs: ...int)`) become Go variadics (`xs ...int`). The
  compiler remembers variadic functions in `_variadic_functions`.
- `private func` or a leading underscore produces an unexported name.
- Overloads dispatch on **arity, parameter type, and contextual return type**.
  The transpiler walks the per-name `_overload_variants` list after the pre-scan
  and assigns each variant a stable Go-name suffix: `Name_<argcount>` when
  arities differ, `Name_<argcount>_<sigSuffix>` when parameter signatures differ,
  and `Name_<argcount>_<sigSuffix>_ret_<returnSuffix>` when only return types
  differ. Call sites infer parameter signatures through `_infer_call_arg_sig`.
  Annotated assignments, enclosing returns, and typed receiving parameters
  publish `_expected_expr_go_type`, which selects a matching return variant.
  `Result[T]` still lowers to runtime `*Result`, but `_overload_return_key`
  retains the payload as a compiler-only key such as `*Result[int]`; identical
  parameter lists can therefore dispatch to distinct Result payload variants.
  Unresolved ambiguity selects the first matching source-order candidate.
  Instance, static, inherited, interface, defaulted, and imported-library
  overloads share the same metadata and dispatch path; interface signatures and
  overload sets are serialized with imported module metadata. Variadic functions (`*args`) still can't
  be overloaded.

Bare references to user functions (e.g. passing `handler` as a value)
resolve to the Go public name so they can be used as first-class values.

### Unused-local silencer

Go treats unused locals as a hard build error
(`declared and not used`). Lam aims for warn-don't-error semantics
on advisory diagnostics, so the transpiler defuses the conflict by
emitting defensive `_ = name` lines in function and block scopes for
locals that were declared but never referenced. Take this Lam:

```lam
func compute() {
    x = 42
    y = 99
    print(y)
}
```

The transpiler emits:

```go
func Compute() {
    x := 42
    y := 99
    fmt.Println(y)
    _ = x
}
```

The implementation lives in `_emit_unused_local_silencers`
(`compiler/visitors/helpers.py`) and the block-scope wrappers in
`compiler/visitors/statements.py`. Each scope snapshots
`declared_vars`, lets its suite walk run, then diffs the post-walk set
against the snapshot to find scope-local declarations. Each remaining
name is searched for word-boundary references in the slice of
`output_lines` emitted during that scope (with `//line` directives
stripped and Go string, rune and comment text masked, so source paths or a
`"err: ..."` message can't false-positive); names that appear
exactly once — i.e. only in their own declaration line — get a trailing
`_ = name` before the scope closes.

Limitations:

- **Compiler-internal names** (anything starting with `_` or
  `__`) are excluded from the diff so we don't churn output for
  things the user can't see (`__qN` from `?` propagation,
  `__lamPanicked` etc.).

Pair this with the **semantic checker's unused warnings**
(unused imports, unused parameters, unused locals — see
[`SYNTAX.md`](SYNTAX.md#compiler-diagnostics)) to get the full
warn-don't-error stack: Lam tells the user, the transpiler keeps
Go happy, the build proceeds.

## Classes

```lam
class Point {
    static originCalls: int = 0

    func init(self, x: int, y: int) {
        self.x = x
        self.y = y
    }

    func distance(self, other: Point) -> float {
        dx: int = self.x - other.x
        dy: int = self.y - other.y
        return float((dx * dx) + (dy * dy))
    }

    static func origin() -> Point {
        Point.originCalls += 1
        return Point(0, 0)
    }
}
```

→

```go
type Point struct {
    X int
    Y int
}

var Point_originCalls int = 0

func NewPoint(x int, y int) *Point {
    return &Point{X: x, Y: y}
}

func (s *Point) Distance(other *Point) float64 {
    var dx int = s.X - other.X
    var dy int = s.Y - other.Y
    return float64((dx * dx) + (dy * dy))
}

func Point_origin() *Point {
    Point_originCalls += 1
    return NewPoint(0, 0)
}
```

- Fields are inferred from the constructor body (`self.foo = ...`) and
  from top-level annotations inside the class.
- `self` in method bodies is rewritten to `s`.
- Static methods are emitted as plain functions named
  `<Class>_<method>`. Callers who write `Class.method(...)` or
  `Class.method()` via the transpiler are mapped onto that.
- Static variables are emitted as package-level `var` declarations named
  `<Class>_<field>`. `private static` variables lower to an unexported
  package variable by lowercasing the first character of that generated
  name. Reads, assignments, and augmented assignments written as
  `Class.field` are mapped onto the package variable.
- Dunder operator methods are rewritten per `DUNDER_OPS` (`__add__` →
  `Add`, `__eq__` → `Eq`, etc.).

### Inheritance

`class Child(Parent)` compiles to Go struct embedding: `Child` gets an
anonymous `*Parent` field, so Go field and method promotion makes parent
members reachable from child values. Lam code calls inherited methods normally,
for example `self.greet()` inside a child method or `dog.greet()` at a call
site.

Inside a child with exactly one unnamed parent, `base` is a compile-time alias
for the embedded parent field. A parent initializer call:

```lam
class Child(Parent) {
    func init(self, name: str) {
        base.init(name, "internal")
    }
}
```

lowers to assigning the embedded parent pointer from the parent constructor:

```go
s.Parent = NewParent(name, "internal")
```

`base.__init__(...)` lowers the same way as `base.init(...)`.

There is no special `super()` support today. `super` is treated like an ordinary
name and is reported as undefined unless user code defines it. If a child
overrides a parent method, `self.method()` calls the child override. To call the
parent implementation explicitly, use the parent alias, such as
`base.method(...)` or a named alias.

Constructors are independent. `Child(...)` is checked against `Child.init` /
`Child.__init__`, not against the parent initializer, and the child constructor
signature does not need to match the parent signature. Parent parameters do not
need defaults merely because the child constructor has different parameters.

When a child constructor is emitted, the compiler first allocates zero-value
embedded parent structs, for example `s.Parent = &Parent{}`, before running the
child constructor body. Explicit parent initializer calls replace that
zero-value parent with the result of `NewParent(...)`. A child class without its
own initializer now emits a fallback constructor that also initializes embedded
parent pointers.

Multiple inheritance embeds each parent pointer:

```lam
class ServiceAccount(account: Account, flags: Flags) {
    func init(self, owner: str) {
        account.init(owner, "service")
        flags.init(true)
    }
}
```

The aliases are compile-time names for those embedded parent fields, so
`account.label()` lowers to `s.Account.Label()`. If multiple parents provide
the same inherited member, unqualified access through `self.member` or
`obj.member` is rejected as ambiguous unless the child overrides that member.
Using `base` in a multiple-parent class is also rejected; name the parent
aliases instead.

### Interfaces

```lam
interface Greeter {
    func greet(self) -> str
}
```

→

```go
type Greeter interface {
    Greet() string
}
```

Interfaces are never wrapped in pointers.

## Expressions

| Lam                                | Go                                                                        |
|------------------------------------|---------------------------------------------------------------------------|
| `a + b`                            | `a + b`                                                                   |
| `a // b`                           | integer `a / b`                                                           |
| `a ** b`                           | `math.Pow(float64(a), float64(b))`; typed non-`float64` numeric contexts cast the result |
| `a in xs`, `a not in xs`           | Container-aware membership test for strings, lists, dicts, and sets          |
| `not x`                            | `!x`                                                                      |
| `x and y`, `x or y`                | `x && y`, `x \|\| y`                                                      |
| `a if c else b`                    | Go ternary via IIFE                                                       |
| `name := expr`                     | IIFE binding `name` locally and returning it (binding does *not* escape)  |
| f-strings `f"x={x}"`               | `fmt.Sprintf("x=%v", x)` with method-call translation                     |
| `s[i]` / `s[a:b]`                  | Go string byte indexing / string slicing                                 |
| `[expr for x in xs if c]`          | IIFE building a `[]T` slice                                                |
| `{k: v for x in xs}`               | IIFE building a `map[K]V`                                                  |
| `{expr for x in xs}`               | IIFE building a `map[K]bool` in a typed set context, otherwise `map[interface{}]bool` |
| `(expr for x in xs)`               | Same eager slice lowering as the list comprehension form                   |
| `[expr for a, b in pairs]`         | IIFE loop with typed positional unpacking; map targets lower directly to `for a, b := range` |
| `{a, b, c}` (set literal)          | IIFE that inserts each element into a `map[K]bool` in typed contexts (deduplicates) |
| `len(x)`                           | `len(x)`                                                                  |
| `str(x)` / `repr(x)`               | `fmt.Sprintf("%v", x)` / `fmt.Sprintf("%#v", x)`                           |
| `int(x)` / `float(x)`              | `int(x)` / `float64(x)`                                                   |
| `print(...)`                       | `fmt.Println(...)`                                                        |
| `input(prompt)`                    | `bufio.NewScanner(os.Stdin)` one-liner                                    |
| `File.open(path, "w")`             | `File_open(path, "w")`, returning `*Result` with `File` payload metadata   |
| `xs.length()`                      | `len(xs)`                                                                  |
| `xs.append(v)`                     | `xs = append(xs, v)`                                                      |
| `xs.pop()`                         | IIFE that returns the removed element and shrinks `xs`                    |
| `xs.map(fn)`                       | IIFE that builds a typed result slice from callback/context information   |
| `xs.filter(fn)`                    | IIFE that keeps elements where `fn(_v)` is true                           |
| `xs.reduce(fn[, init])`            | IIFE that accumulates                                                     |
| `xs.any(fn)` / `xs.all(fn)`        | IIFE with early return                                                    |
| `xs.foreach(fn)`                   | IIFE with no result                                                       |
| `xs.sort([compare][, inplace])`    | `sort.Slice` copy by default; when `inplace=true`, sorts and returns `xs` |
| `sorted(xs)`                       | `sort.Slice` copy preserving the receiver/context element type            |
| `enumerate(xs)`                    | IIFE materializing index/value pairs as `[]interface{}` tuples            |
| `isinstance(x, T)`                 | Type-specific runtime check: assertion, nil test, or `reflect.Kind`        |

For class operands, arithmetic/unary/comparison expressions dispatch through
the corresponding dunder method. Before emission, semantic analysis validates
operator-method arity, required boolean comparison returns, right-operand
compatibility, and the declared result type; unknown/`any` operands remain
dynamic. Generic call signatures use the same recursive unifier when type
arguments are omitted, including nested containers, imported functions/classes,
and return-type specialization.

The `xs.map`, `xs.filter`, `xs.any`, `xs.all` lowerings detect whether the
argument refers to a user function (in any of its renamed forms: raw,
`_go_public_name`, `_go_private_name`) and skip unnecessary interface
assertions when the callback already has a concrete return type. For lambdas,
an explicit `lambda (...) -> T` return annotation or an enclosing assignment
such as `names: list[str] = nums.map(...)` shapes the emitted result slice.

Membership lowering follows the container shape: strings use
`strings.Contains`, maps and sets emit a key-presence lookup, and lists emit a
small loop using `reflect.DeepEqual` so nested collection elements can be
tested without triggering Go's non-comparable-slice errors. Chained comparisons
such as `1 < x < 10` are lowered pairwise with `&&`.

### Destructuring assignments

New tuple targets use `:=`; fully existing targets use `=`. Literal tuple/list
right sides are emitted as individual Go values. Function calls retain Go
multi-return form, while semantic analysis chooses the matching overload and
uses its linked return annotation for arity/element checks. The same metadata
survives imported-library cache entries and covers Go-backed wrappers declared
with `-> (T, U)`.

## Modules and imports

```lam
from lammath import Math
from lamos import Os
```

- Every imported module is transpiled to its own Go source file alongside
  the user program. Dotted modules map to nested Lam paths, so
  `from lamwebp.io.files import readWebp` resolves
  `lamwebp/io/files.lam` or `lamwebp/io/files/__init__.lam`. Only that
  module and its transitive imports are bundled; sibling package files do
  not become implicit dependencies.
- Classes, static methods, and top-level defaults are exposed under
  `<ModuleName>_symbol` names. For the common case
  (`from mod import Class`), the transpiler rewrites `Class.method(...)`
  to the flat `Class_method(...)` function.
- Bundled Lam libraries are written with a neutral `_lam.go` suffix, for
  example `test` becomes `lib_test_lam.go` and `lamwebp.codec` becomes
  `lib_lamwebp__codec_lam.go`. This avoids Go's special `*_test.go`
  convention, where files are excluded from normal `go build`. A user
  module named `test` or `foo_test` therefore still links into the binary.
  Module casing is preserved (`PascalCaseLib` becomes
  `lib_PascalCaseLib_lam.go`) rather than normalized during emission.
- A module may opt into a Go import by writing `go! { import "pkg" }`.
  Imports from go blocks are consolidated into a single Go import group.
- Defaults, arities, parameter names, Go parameter types, variadic markers and
  static-method metadata from library modules are merged into the main
  transpiler before it walks the main file, so call sites in the user's program
  can fill in library defaults and contextually lower anonymous collection
  arguments exactly like their in-file counterparts. A `go!` implementation
  body does not hide its surrounding Lam signature: stdlib/package constructors,
  static methods, and inferred instance methods receive the same Lam-side call
  diagnostics as ordinary methods. Raw Go values and dynamic `any` receivers
  intentionally keep the conservative fallback.
- All modules of a program are emitted into **one Go package**
  (`package main`). Module namespacing (`compiler/namespacing.py`) keeps
  same-name top-level definitions apart. When a name is defined by more than
  one linked module, every definition outside the main module (and outside the
  compiler-referenced classes `lamerrors.Result` / `Error` and
  `lamstrings.Strings`) is renamed:
  - functions, constants and module-level variables get a module-qualified Go
    name (`tag` in `alpha` → `Tag__alpha`, `Version` in `@acme/jwt.core` →
    `Version__acme_jwt_core`), applied to definitions, references and importers;
    imported-function metadata (signatures, defaults, overload variants, Result
    payloads) of a colliding name is taken from the defining module, so overload
    sets never merge;
  - classes and interfaces are renamed at the Lam level before any metadata is
    collected (`Config` in `one` → `Config__one`), so the Go type, the
    `NewConfig__one` constructor, static members (`Config__one_make`) and every
    compiler table follow.
- Imports are resolved before transpilation: `from m import x as y` and
  `import m as a` / `a.x` (including inside f-string placeholders) are
  rewritten in the AST to `x` (or its renamed form) when that name is free in
  the module; when it is taken (a local `x`, or another module's `x`) the alias
  keeps its own Lam name and maps to the right Go symbol.
- Raw `go!` code is rewritten for its own module's renamed definitions and for
  names imported without an alias (identifiers outside strings and comments;
  overload suffixes and `Class_static` prefixes included). Aliased imports and
  modules reached only indirectly are not followed: such raw code must use the
  generated Go name.
- What still has to be unique is reported before `go build` as a Lam
  diagnostic listing each module, its source location and a three-line
  snippet, instead of Go's `redeclared in this block` cascade: a clash between
  two definitions that cannot be renamed (the main module and a
  compiler-referenced class, or two of those), raw `go!` declarations, and
  cross-kind collisions such as a class `Thing` (constructor `NewThing`) next
  to a function `newThing`.

  ```text
  error: top-level symbol clash while linking main.lam
    Go `type Error` is declared by modules `main` and `lamerrors`
      - `main` at /app/main.lam:3
          >>>    3 | class Error {
      - `lamerrors` at <lamc>/lib/lamerrors.lam:24
          >>>   24 | class Error {
    help: rename one of the declarations. ...
  ```

  Methods, `func init()` and `_` never clash. Prefix raw `go!` helpers with a
  module-specific stem (`lamXml…`, `lamHttp…`).

## `go!` escape hatches

```lam
go! {
    import "hash/crc32"
}

func checksum(s: str) -> int {
    go! {
        return int(crc32.ChecksumIEEE([]byte(s)))
    }
}

inline: int = go!(42) # inside an expression
```

- **Top-level `go!`** blocks are emitted at file scope. `import` lines
  inside are merged with the compiler-generated imports so duplicates are
  not a problem. This is also where you declare **package-level Go
  variables** that need to be shared across handlers / lambdas / top-level
  functions (see below).
- **Inside a function body** a `go!` block is pasted verbatim where it
  appeared; the preceding and following Lam statements translate as
  normal.
- **Inline `go!(expr)`** substitutes the raw expression in place — useful
  for anonymous structs, type assertions, and other constructs the Lam
  grammar does not yet model directly.

### `self` / `s` rewrite — class methods only

Within a method body, inside a `go!` block, `self` is available as the
plain Go receiver `s` and `self.<Field>` is rewritten to `s.<TitleCased
Field>` (for example `s.Inner`, `s.Raw`). This is the **only**
identifier-rewrite the transpiler performs inside `go!` blocks.

Outside class methods (top-level functions, plugin functions like
`func plugin(srv: Server)`, `lambda` handlers) **no `s` is bound** and
the literal text `s` resolves through normal Go lexical scoping. In
particular:

- A `srv: Server` parameter on a top-level function shows up as a Go
  function parameter and is reachable inside `go!` because Go scoping
  makes it so — the transpiler does *not* rewrite `srv`.
- For state that must outlive a single function call (counters,
  caches, mutexes), declare a package-level Go var inside a top-level
  `go!` block and reference it from handler-level `go!` blocks.

User-facing examples and a good/bad cheatsheet live in
[`SYNTAX.md`](SYNTAX.md) → "Scoping inside `go!` blocks" and
[`server_plugins.md`](server_plugins.md) → "Identifier scoping inside
`go!`".

## Assertions, deletions, and other statement forms

| Lam                   | Go                                                                         |
|-----------------------|----------------------------------------------------------------------------|
| `assert cond, "msg"`  | `if !(cond) { panic("msg") }`                                              |
| `del x`               | `_ = x` (no-op; freed by GC)                                               |
| `pass`                | emits nothing; the empty block is legal Go                                 |
| `with f as x: body`   | Go defer pattern (`defer x.Close()`) + inline body                         |
| `yield expr`          | `chan <- expr`; generators compile to a goroutine + channel pair           |

Top-level, instance, and static generator functions share this lowering.
Generic static generators preserve their type parameter and return a typed
channel:

```lammergeier
class Streams {
    static func values[T](items: list[T]) -> T {
        for item: T in items { yield item }
    }
}

for value in Streams.values([3, 1, 2]) { print(value) }
```

The generated signature is `func Streams_values[T any]([]T) chan T`, and the
loop becomes `for value := range Streams_values([]int{3, 1, 2})`. Imported
generator metadata follows the module graph, so the same lowering applies when
`Streams` comes from an external library. Generic callable arguments also
contextually lower non-empty inline list literals to their concrete slice type
instead of leaking the callee's `T` into caller Go.

## Concurrency

- Every `async func` invocation starts a goroutine and returns a channel handle
  buffered to exactly one result; `await call` or `await handle` receives it
  with `<-`. The same emitter is used for top-level functions, instance methods,
  and static methods, so receiver type arguments, private/static Go naming, and
  `Result[T]` payloads are preserved.
- User-facing channel operations belong in `lamconcurrency.Channel`; actor queues
  compose it through `lamactor.Mailbox`. Both expose blocking `send` / `receive`,
  immediate `offer` / `poll`, and awaitable `asyncSend` / `asyncReceive` methods
  with explicit `Result` outcomes. Their raw Go channels remain runtime
  boundaries rather than leaking into callers' `go!` blocks.

## External Go modules

The compiler synthesizes a temporary `go.mod`, injects the applicable pinned
versions, and runs `go mod tidy` after writing generated source. Pins come from
the reached stdlib modules, the project's `lamlib.toml` `[go-deps]`, and
resolved package pins in `lamlib.lock.toml`; project and lockfile pins take
precedence over compiler defaults.

This is how `lamarray` pulls in **gonum** (`gonum.org/v1/gonum/mat`,
`floats`, `stat`) for BLAS-accelerated dense linear algebra without requiring
users to maintain a `go.mod`. The relevant flow:

1. The library `.lam` file declares imports inside a `go! { import (...) }`
   block. Stdlib dependencies are pinned by the compiler; third-party package
   authors declare external modules under `[go-deps]` in `lamlib.toml`.
2. `lamc` writes generated Go to a temporary directory, invokes `go mod init`,
   injects the selected `require` directives, then runs `go mod tidy`. Missing
   modules may be downloaded through the configured Go proxy and cached in the
   normal Go module cache.
3. `go build` runs against the populated module with the same module and build
   caches as hand-written Go code.

For network-free builds, materialize Lam dependencies with `lamc install
--frozen --offline` and pre-populate the Go module cache (or configure the Go
toolchain for offline use). The package-manager flag governs Lam package
fetches; it does not turn an empty Go module cache into a vendored build.

Raw Go remains an escape hatch, so some invalid programs can still fail in
the Go toolchain. Lammergeier preserves `//line` provenance for generated Go
and reports those failures with Lam source snippets for both the main file and
imported Lam modules. The regression tests intentionally inject a bad `go!`
declaration in each location and assert the final diagnostic points at the
original `.lam` line.

### Fuzz and generated-Go contracts

The local unit gate includes deterministic random-program fuzz tests for
broad Lam-to-Go lowering. They generate nested anonymous list/dict/set values,
classes, defaults, lambdas, comprehensions, membership checks, chained
comparisons, ternaries, numeric expressions, and imported typed contexts. The
suite has three layers:

1. compile and run generated Lam programs;
2. compile deeper programs that stress nested typed literals;
3. write `lamc --emit-go` output to a fresh Go module, run `gofmt`, and
   compile it with `go test ./...`.

When a generated Lam program unexpectedly fails to emit or build, the fuzz
helper keeps the original source and also attempts a simple line-based shrink
that preserves the same diagnostic signal. The failure message prints the
reduced numbered source so the next regression can be promoted into a focused
`.lam` case quickly.

### Stdlib Go-module pins

Third-party Go modules that the Lam stdlib itself depends on are
listed with frozen versions in
`compiler/stdlib_go_deps.py::STDLIB_GO_PINS`. Examples: `lamdb`
blank-imports `modernc.org/sqlite`, while `lamenv` reaches into
`github.com/BurntSushi/toml` and `gopkg.in/yaml.v3`. External packages own
their pins: `@lam/vulture/ws-plugin`, for example, declares
`github.com/gorilla/websocket` in its `lamlib.toml`.
Each entry is a `<go_module_path> = "<v…>"` pair sorted by
module path for diff stability.

`_collect_go_pins` in `compiler/lammergeier.py` selects defaults for the
stdlib modules reached by the import graph through `STDLIB_GO_PIN_MODULES`, then
layers project-side pins (from `lamlib.toml` `[go-deps]` and
`lamlib.lock.toml` `[go_pins.*]`) on top. Project pins win over stdlib pins via
Go's MVS rule (the higher version is selected), so users can upgrade an
individual stdlib transitive without forking the compiler. Reached stdlib
modules remain reproducible without adding unrelated requirements to a small
program's `go.mod`.

Updating a pin: bump the entry in `STDLIB_GO_PINS`, update the
stdlib library's `go!` block if the new module API changed, and
run the regression suite. The file's docstring carries the
checklist.

## Built-in string methods → `lamstrings`

`"hello".toUpper()`, `s.split(",")`, `parts.join("/")` and
friends don't lower to inline `strings.X` Go calls. The
dispatcher in `compiler/visitors/expressions.py::_funccall_to_go`
(search for `string_methods`) emits a call into the `lamstrings`
standard library instead. The Lam-side method names match the
`lamstrings.Strings` static-method names verbatim —
`"hi".toUpper()` and `Strings.toUpper("hi")` are spelled the
same way and dispatch to the same Go function:

| Lam call                  | Go emission                            |
|---------------------------|----------------------------------------|
| `"…".toUpper()`           | `Strings_toUpper("…")`                 |
| `"…".toLower()`           | `Strings_toLower("…")`                 |
| `s.trim()`                | `Strings_trim(s)`                      |
| `s.trimLeft()` / `(set)`  | `Strings_trimLeft(s, " \t\n")` / `(set)` |
| `s.trimRight()` / `(set)` | `Strings_trimRight(s, " \t\n")` / `(set)` |
| `s.replace(o, n)`         | `Strings_replace(s, o, n)`             |
| `s.split(sep)`            | `Strings_split(s, sep)`                |
| `sep.join(parts)`         | `Strings_join(parts, sep)`             |
| `s.startsWith(p)`         | `Strings_startsWith(s, p)`             |
| `s.endsWith(p)`           | `Strings_endsWith(s, p)`               |
| `s.index(sub)`            | `Strings_index(s, sub)`                |
| `s.count(sub)`            | `Strings_count(s, sub)`                |
| `s.contains(sub)`         | `Strings_contains(s, sub)`             |
| `s.title()`               | `Strings_title(s)`                     |
| `tpl.format(*args)`       | `Strings_format(tpl, *args)`           |

Two consequences worth understanding:

1. **`lamstrings` is the single source of truth.** Tweaking how
   `.toUpper()` behaves (Unicode handling, normalisation rules)
   means editing `Strings.toUpper` in `lib/lamstrings.lam`. The
   compiler doesn't carry an inline copy of the lowering.
2. **The compiler driver auto-injects `lamstrings`.** A textual
   scan over the preprocessed source (in
   `compile_lam` and inside the library worklist) appends
   `lamstrings` to `_lam_imports` whenever any of the dispatched
   names appears in a `.<method>(` position. The injection runs
   for both the user file *and* every transitively-imported
   stdlib library, so `prefix_parser.lam` calling `source.split(" ")`
   gets `Strings_split` resolved without having to write
   `from lamstrings import Strings` itself. False positives —
   say a user class with a `.contains()` method — are harmless:
   the dispatcher falls back to user-method lowering whenever
   the receiver is a known class instance, and the unused
   `Strings_*` symbols get dropped by Go's linker.

The "needs at least one argument" guard still applies: a bare
`qb.count()` on an unknown receiver shape skips the
`Strings_count(o, " ")` lowering and falls through to user-method
dispatch so the build doesn't silently emit nonsense.

## Lam-side abstractions over Go primitives

Several stdlib classes are thin Lam wrappers around Go runtime types.
The pattern is consistent: a single `any`-typed field holds the raw Go
value, and every method casts to the concrete type before calling
through:

| Lam class                     | Go backing                          | Field name |
|-------------------------------|-------------------------------------|------------|
| `lamarray.Array`              | `[]float64`                         | `Data`     |
| `lamarray.Matrix`             | `*gonum.org/v1/gonum/mat.Dense`     | `Dense`    |
| `@lam/vulture.Server`         | `*http.Server` (+ `[]any` route table) | `RawSrv`   |
| `lamcache.LruCache`           | `*container/list.List` + map        | `Lst`/`Idx`|
| `lamcache.TtlCache`           | `map[any]*ttlEntry`                 | `Idx`      |
| `lamconcurrency.Channel`      | `*lamChannel` (data + close signal) | `Ch`       |
| `lamconcurrency.WaitGroup`    | `*sync.WaitGroup`                   | `Wg`       |
| `lamdatastructures.Heap`      | `*container/heap` adapter           | `Inner`    |

When debugging a stdlib module, that field is a good first place to
inspect — the Lam method bodies usually do little more than typecheck
inputs and forward to the underlying Go API.

## Limitations and pitfalls

- **Multi-line function calls:** calls may break after `(` and after
  comma-separated arguments, with an optional trailing comma before
  `)`. Positional and keyword arguments use the same semantics as
  single-line calls.
- **Implicit conversions:** integer ↔ float conversions are explicit through
  `int()` and `float()`. String parsing uses a payload-typed `Conv.parse`
  Result overload rather than an implicit numeric conversion.
- **Reserved words:** identifiers colliding with Go keywords (e.g.
  `range`) get an underscore suffix; prefer `range_` in API designs.
- **Multiple try/catch in one function:** the current lowering shares a
  single `defer recover()` per function, so two consecutive try/catch
  blocks won't both fire. Wrap each `try` in its own helper if you
  need independent recovery sites.
- **F-string interpolations are textual:** the f-string lowerer runs
  string-level regex transforms on each `{expr}`. It doesn't accept
  escaped quotes (`f"x={d[\"key\"]}"`) and may not rewrite every
  identifier inside nested method-call args. When in doubt, lift the
  expression into a `:= …` line above the `print(...)` and interpolate
  the simple variable.
- **Lam `int(s)` is a Go type-conversion, not a parser:** parse a whole string
  with an explicitly typed Result, for example `parsed: Result[int] =
  Conv.parse(s)`, or call `strconv.Atoi(s)` from a `go!` block. `Conv.parse`
  reports malformed input rather than silently defaulting. `int(s[i])` is
  different: string indexing yields a byte-like numeric value.
- **Walrus (`name := expr`) binds locally:** the lowering wraps the
  binding in an IIFE, so the introduced name is *not* visible in the
  surrounding scope. Use `:=` only when the value of the expression
  is what you need; for a binding that survives, use a regular
  assignment statement.
- **Decorators are parsed but unsupported:** the grammar has
  `@decorator` / `@decorator(args)` forms before functions and
  classes, but the semantic checker rejects them before emission.
  `@private func f() { ... }` is rejected with a hint to use
  `private func f() { ... }`.
- **`nonlocal` is validated before emission:** it resolves one unambiguous
  binding in an enclosing function and rejects missing bindings, declarations
  outside a nested function, ambiguous redeclarations, and writes to outer
  `const` values. Valid declarations emit no Go statement because Go closures
  already capture the resolved local by reference. Module-level state should be
  touched via `global` instead.

## Where to look when something breaks

1. `compiler/preprocessor.py` – go! extraction or inline go! expressions.
2. `compiler/visitors/definitions.py` – funcdef / classdef / interfacedef
   signatures, constructor emission.
3. `compiler/visitors/statements.py` – control flow, assignments,
   try/except, with statements, for-else / while-else.
4. `compiler/visitors/expressions.py` – method-call rewriting, map/filter
   lowerings, builtins, f-string transformation, default-argument
   filling.
5. `compiler/visitors/helpers.py` – name mangling, type mapping, param
   parsing.
6. `compiler/lsp.py` – LSP server (parsing reuses the same Lark grammar
   and preprocessor pipeline, so most parse-side fixes show up there
   automatically; symbol extraction has its own AST walker plus a
   regex fallback for partial documents).
