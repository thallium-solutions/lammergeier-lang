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

The parse preprocessor keeps every statement on its source line: brace-less
blocks (`if x print(...)`) are braced on the same line, dictionary
destructuring expands to `;`-separated assignments on its line, and a
multi-line triple-quoted string collapsed to one line is followed by the
newlines it swallowed. Parser, semantic and Go positions therefore all use
the user's line numbers.

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
| `func map[U](self, fn: func(T) -> U) -> Box[U]` on `Box[T]` | `func Box_Map[T any, U any](s *Box[T], fn func(T) U) *Box[U]`        |
| `b.map(lambda x: x * 2)` with `b: Box[int]`                 | `Box_Map(b, func(x int) int { return x * 2 })`                       |
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

### Methods with their own type parameters

Go methods cannot declare type parameters, so a Lam method that does
(`func map[U](self, ...)`, on a generic class or not) lowers to a
package-level generic function named `<Class>_<Method>` whose clause is
the class's parameters followed by the method's, with the receiver as
the first parameter (named `s`, like a method receiver, so `go!` blocks
and `self` rewrites are unchanged). `_generic_instance_methods` records
these methods (also for imported classes, through the library metadata),
and a call `recv.map(f)` becomes `Box_Map(recv, f)`; a receiver whose
class inherits the method passes its embedded parent
(`Box_Map(ib.Box, f)` for `class IntBox(Box[int])`). Such methods are not
part of the virtual-dispatch interfaces; the semantic checker rejects an
override.

### Call-site type-argument inference

A lambda argument lowers to a Go function literal, which must spell its
parameter and result types; Go infers type arguments *from* such a
literal but never types the literal itself. `GenericCallMixin`
(`compiler/visitors/generics.py`) therefore binds a generic callee's type
parameters at each call, on the Go spelling of the declared parameter
types and in Go's order: the receiver's type arguments (`*Iter[int]` gives
`T = int`; the in-scope `T` for `self`; the bound arguments of a generic
base for a subclass), typed arguments, untyped literals, then lambdas and
function references, typed with what is already bound — the lambda body's
type binds the rest (`func(T) U` with `T = int` and `x * x` gives
`U = int`). The bindings substitute the parameter types the arguments are
lowered with (`_call_type_subst`), so lambdas come out concrete
(`func(x int) int`); an unresolved parameter falls back to `interface{}`
(what Go would then infer). The same bindings give the call's result type
(`_generic_method_returns` keeps each generic method's Go result spelled
with its parameters), so chains such as
`Iter.range_(0, 9).filter(f).map(g).toList()` keep their types for the next
link, `Result[T]` payloads (`it.first().unwrap()`) and inferred locals.
The Go call itself still relies on Go's inference, except for a static
method whose parameter no argument determines (`Iter.repeat(None, 2)`
emits `Iter_repeat[interface{}](nil, 2)`).

Builtins passed as function values (`it.map(len)`, `xs.map(str)`) are
not Go values; where a `func(P) R` is expected they become
`func(_lamArg P) R { return len(_lamArg) }`.

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
leaking a Go map-indexing detail. Set methods are map operations
(`ids.add(x)` → `ids[x] = true`, `discard` → `delete(ids, x)`, `contains` →
`ids[x]`, `remove` panics with a `KeyError` when the key is missing), `set()`
is the expected map type's empty literal, and `a | b`, `a & b`, `a - b` call
a small closure building a fresh map (`func(l, r map[int]bool) map[int]bool
{ out := ...; for k := range l { ... }; return out }(a, b)`); `a |= b` assigns
its result back.

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
Re-assignment (`x = 5`) always emits a plain `=`. The semantic checker hands
the type it inferred for a first plain assignment to the transpiler (the
`_lam_inferred_type` attribute on the `assign` node): a concrete type is
recorded for the name (so `for ch in s`, `r.unwrap() > 1`, method dispatch
and `?.` chains see it) and, when the value's own Go shape differs, the
declaration is lowered like `name: T = value` (`var name T = ...`). Types
mentioning `any`, unknown names or type parameters keep plain `:=`; values
holding un-awaited async handles stay untyped (their element type is the
handle, `chan func() T`).

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

A lambda lowered where a `func(...)` Go type is expected (`_typed_value_to_go`:
arguments, annotated targets, returns, `func`-typed fields, `foreach`) takes
that type's parameters and result (`_split_go_func_type`); a `func(...)` with
no result emits a void Go closure whose expression body is a statement
(`func(n int) { fmt.Println(n) }`). Without a context, a lambda whose body
is a call producing no value (`print`, a user function without a return
type) is void too, and a lambda with all parameters typed has the Go type
`func(P...) R` (its `-> R` annotation or the inferred body type).

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

### isinstance narrowing

Where `isinstance(x, T)` holds (see `docs/SYNTAX.md` → "isinstance" →
"Narrowing"), the region starts by binding a *shadow* of the local in its
own Go block scope, so the outer variable keeps its Go type and the region
is lowered with `x` typed as `T` (`compiler/visitors/narrowing.py`; the
analysis of where a test holds, `compiler/narrowing.py`, is shared with the
semantic checker so both narrow the same names):

```lam
func side(s: Shape) -> int {
    if not isinstance(s, Square) {
        return 0
    }
    return s.side
}

if isinstance(x, Square) {
    print(x.side, x.area())
}
```

→

```go
func Side(s *Shape) int {
	if !(func() bool { _, ok := s._lamDyn().(interface{ _lamIs_Square() *Square }); return ok }()) {
		return 0
	}
	{
		s, _ := func(v interface{}) (*Square, bool) { if t, ok := v.(interface{ _lamIs_Square() *Square }); ok { return t._lamIs_Square(), true }; return nil, false }(s._lamDyn())
		_ = s
		return s.Side
	}
}

if func() bool { _, ok := x._lamDyn().(interface{ _lamIs_Square() *Square }); return ok }() {
	x, _ := func(v interface{}) (*Square, bool) { ... }(x._lamDyn())
	_ = x
	fmt.Println(x.Side, x._lamVirt_Area())
}
```

| Narrowing | Shadow |
|---|---|
| hierarchy class to a subclass (also from `any` / an interface) | the `*T` view of the most-derived object (`_lamDyn()` / `_lamIs_T()`), so virtual calls keep reaching the overrides |
| `any` / interface to a class outside any hierarchy | `x, _ := interface{}(x).(*T)` |
| to an interface | `x, _ := interface{}(x).(I)` |
| `any` to `int` / `float` / `str` / `bool` | `x, _ := interface{}(x).(int)` (`float64`, `string`, `bool`) |
| `json` to a scalar | `x, _ := lamJSONRaw(x).(int)` |
| `any` to `list` / `dict` | a `[]interface{}` / `map[string]interface{}` view (the value itself when it already has that Go type, else a reflection copy) |
| `optional[int]` (a `*int`) to `int` | `x := *x` |
| `optional[C]` to `C` | nothing (same Go pointer); only the Lam type changes |

- `if` / `elif` / `else` branches and `while` bodies bind the shadow at the
  top of their Go block; the statements after an early-exit guard are
  wrapped in `{ ... }` holding it.
- The right operand of `and` / `or`, the branches of a ternary and a
  comprehension result bind it inside a closure:
  `cond && func() bool { x, _ := ...; _ = x; return x.Side > 2 }()`,
  `func() int { if cond { x, _ := ...; return x.Side }; return 0 }()`. An
  operand using `?` is not narrowed: its propagation is a statement of the
  enclosing function, emitted before the condition.
- Because the shadow is a separate Go variable, a local assigned in the
  region is never narrowed there (the assignment would only reach the
  shadow), nor is a global, a field, or a name a `go!` block reads.

### `for` / `while`

`for x in iterable` becomes `for _, x := range iterable` for slices/channels
and `for x := range iterable` for dict/set-shaped maps, so a single loop
target receives keys. Use `for k, v in mapping` when both map keys and values
are needed.
`for i, x in enumerate(xs)` becomes `for i, x := range xs`; with a start
(`enumerate(xs, 1)`, `start=k`) the Go index is `i__lamRaw` and the loop
binds `i := i__lamRaw + 1` (a non-constant start is evaluated once into
`__lamEnumStartN` before the loop).
`for i in range(n)` becomes `for i := 0; i < n; i += 1`.
`while cond` becomes `for cond`.
Both loop kinds support an `else` suite, which is emitted using a small
per-loop boolean guard (`_lamLoopBreakN := false`, set by `break` in that
loop's own body and tested after it), so nested and sequential loop-`else`
statements keep independent flags. A statically non-empty literal or integer `range` can promote
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

Because the body runs inside that closure, a `return X` in the `try` body
or in a handler stores `X` in the enclosing function's named result
`retval` (functions and methods whose body contains a `try` anywhere —
also inside a loop, a branch or a `match` case — get `(retval T)`), sets
`__lamShouldReturn` and leaves the closure; `if __lamShouldReturn { return }`
after it leaves the function. Inside a `do { }` body the value goes to the
`do` block's return slot instead and the check becomes
`if __lamShouldReturn { return nil }` (leaving the `do` closure, after which
the function returns the recorded value).

`raise` and `throw` (its alias) both become `panic(...)`; Python
exception types (`ValueError`, `TypeError`, …) are formatted as
`fmt.Sprintf("Name: %v", arg)`. The two keywords share a single
`raise_stmt` AST node, so downstream passes don't distinguish them.

A clause naming a user class (`catch AppError as e`) tests the most-derived
object behind the recovered value (`_lamIs_AppError()`, see "Virtual dispatch
and subtyping"), so it also catches subclasses declared in other modules and
base-typed values that hold one; the binding is typed as the class
(`e, _ := ...` of type `*AppError`, the subclass's `AppError` view), so
`e.message` and `e.code()` (dispatching to overrides) work directly. A
catch-all binding (`catch e`) stays the raw `interface{}` value.

### `?` (Result propagation)

The postfix `?` operator desugars to a temp + early-return inside the
enclosing function. Each `?` allocates a fresh `__qN` from the
transpiler-global counter:

```lam
from lamerrors import Result

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
from lamerrors import Result

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
    err := func(v interface{}) *Error {
        if e, ok := v.(*Error); ok { return e }
        if e, ok := v.(interface{ _lamIs_Error() *Error }); ok { return e._lamIs_Error() }
        return NewError("", fmt.Sprint(v), v)
    }(__rdo1.Error)
    fmt.Println(fmt.Sprintf("failed: %v", err))
}
```

The handler's `err` is typed `*Error`: an `Error` (or the `Error` view of a
subclass instance) is used as is, any other propagated value is wrapped with
an empty kind (`Error.__str__` prints just the message then, so the output is
unchanged). A module declaring its own `Error` class keeps the raw value.
A member of `Error` read on a `Result`'s `error` field (`r.error.kind`) goes
through the same view (with `nil` kept for a success result):
`func(v interface{}) *Error { if v == nil { return nil }; ... }(r.Error).Kind`.

Inside the IIFE, the same `?` lowering applies — but the `return __qN`
exits the closure, not the surrounding function, so the error pops
out as the IIFE's value. A Lam `return` inside the body still leaves the
enclosing function: it stores the value in `__lamDoRetVN`, sets the flag
`__lamDoRetN` and returns `nil` from the closure; right after the closure
`if __lamDoRetN { return __lamDoRetVN }` performs the real return (routed
through the enclosing `try` or async lowering when there is one). `_visit_do_stmt` pushes a fresh
`declared_vars` scope around both the body and the handler so locals
don't leak between consecutive `do` blocks. The source must directly
`from lamerrors import Result` for the emitted `*Result` /
`Result_Ok` references to resolve; scoped imports are accepted, while a
transitive import is deliberately insufficient.

### `match`

A match of literal / value / or-patterns over a scalar subject (`int`,
`float`, `str`, `bool`) without guards, bindings or a `break` in a case
body compiles to a Go `switch` with each case body inlined
(`case 1 | 2` → `case 1, 2:`, `case _` → `default:`).

Every other match lowers to an if/else chain over the subject, evaluated
once into `__lamMatchN`:

```go
{
	__lamMatch1 := subject
	if <case 1 condition> {
		<case 1 bindings>
		<case 1 body>
	} else if <case 2 condition> {
		...
	} else {            // `case _` / an unguarded capture
		...
	}
}
```

An if/else chain ending in `else` is a Go terminating statement when every
branch returns (a function may end with such a match), and a `break` /
`continue` inside a case body targets the enclosing loop — inside a Go
`switch`, `break` would only leave the switch.

Each pattern is compiled twice from the same description: as the
condition (plain checks become an `&&` chain; anything that binds names
or needs intermediate values becomes `func() bool { ...; return guard }()`,
returning `false` at the first failed check, with the names bound so the
guard can read them) and as the binding prologue of the case body (the
same extraction without the checks). A `?` in the guard keeps its
`__qN := ...` prologue inside that condition closure, so it runs only when
the case is tried; an error is stored in a per-case `__lamGuardQN_M`
variable and makes the condition true, and the case body starts with
`if __lamGuardQN_M != nil { return __lamGuardQN_M }`. Patterns are typed by
the subject:

- typed lists index the slice (`len(s) == 2`, `s[0]`); `*rest` copies
  `s[i:len(s)-k]` into a new slice of the same element type; tuples assert
  each element to its declared type;
- typed dicts look keys up with `v, ok := m[k]`; `**rest` copies the
  remaining entries into a map of the same type;
- class patterns on a class-typed subject read the (promoted) fields; a
  subclass pattern on a base-typed subject, and any class pattern on an
  `any` / interface subject, tests the most-derived object
  (`_lamIs_<C>()`, see "Virtual dispatch and subtyping") and binds the
  narrowed view, for subclasses declared in any module; classes outside any
  hierarchy use a direct type assertion;
- `any` subjects use small helper closures declared at the top of the
  match block, only when needed: `__lamMatchEq` (numeric-aware equality),
  `__lamMatchSeq` / `__lamMatchMap` (a `[]interface{}` / `map[string]interface{}`
  view, through `reflect` for typed slices and string-keyed maps),
  `__lamMatchInt` / `__lamMatchFloat` (builtin type patterns) and
  `__lamMatchRaw` (sees through a `json` value stored in `any`);
- `json` subjects match on `lamJSONRaw(value)` and re-wrap bound
  sub-values with `lamJSONWrapRaw` so they stay `json`.

### Top-level statements

Top-level declarations stay Go package-level declarations. The module's
top-level statements are emitted, in file order, into one function per
module, and a single `func init()` in the main file calls those functions
in dependency order (a depth-first walk of the import graph from the main
file, imports in source order, each module once, the main file last):

```go
var items []int = []int{}   // declared before the first statement
var total int               // declared after one: zero value here

func lamModuleInit_main() {
	items = append(items, 3)
	total = len(items)      // assigned in file order
}

// Top-level statements of every module, in import order.
func init() {
	lamModuleInit_registry_1a2b3c4d()
	lamModuleInit_main()
}
```

Library modules name their function `lamModuleInit_<module>_<hash>`; a
module without top-level statements emits none and gets no call. Names
first assigned inside top-level blocks are declared as package-level
`var`s before the init function assigns them.

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
  from top-level annotations inside the class. A class-body initializer
  (`count: int = 0`) becomes an assignment `s.Count = 0` in every
  constructor (including the generated no-`init` one), emitted after the
  embedded parents are allocated and before the `init` body; inherited
  defaults are assigned through the parent path (`s.Base.Count = 0`).
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
name and is reported as undefined unless user code defines it. To call the
parent implementation explicitly, use the parent alias, such as
`base.method(...)` or a named alias; those calls are always static.

#### Virtual dispatch and subtyping

Methods are virtual, like Python and Java, and a subclass instance is usable
wherever its base class is expected. Go embedding alone gives neither: the
receiver of an inherited method is the embedded `*Parent` (so `self.area()`
inside it runs the parent's own method), and a `*Square` is not a `*Shape`.
The compiler knows the whole program's class hierarchy (the driver collects
every module's classes, so a library knows which of its classes the program
subclasses) and adds a little machinery to the classes that need it:

```lam
class Shape {
    func area(self) -> int { return 0 }
    func describe(self) -> str { return f"area {self.area()}" }
}

class Square(Shape) {
    func area(self) -> int { return 9 }
}

func total(shapes: list[Shape]) -> int { ... s.area() ... }

s: Shape = Square()
```

→

```go
type Shape struct {
    _lamSelf lamVirtualShape // most-derived object; nil for a plain Shape
}

type lamVirtualShape interface {
    Area() int
}

func (s *Shape) _lamBindSelf(v interface{}) {
    if iv, ok := v.(lamVirtualShape); ok { s._lamSelf = iv }
}

func (_lamRecv *Shape) _lamVirt_Area() int {
    if _lamRecv._lamSelf != nil { return _lamRecv._lamSelf.Area() }
    return _lamRecv.Area()
}

func (s *Shape) _lamDyn() interface{} {   // the most-derived object
    if s == nil { return nil }
    if s._lamSelf != nil { return s._lamSelf }
    return s
}
func (s *Shape) _lamIs_Shape() *Shape { return s }
func (s *Shape) Format(f fmt.State, verb rune) { ... }  // see below

func (s *Shape) Describe() string {
    return fmt.Sprintf("area %v", s._lamVirt_Area())
}

func (s *Square) _lamUp_Shape() *Shape { if s == nil { return nil }; return s.Shape }

func NewSquare() *Square {
    s := &Square{}
    s.Shape = &Shape{}
    s.Shape._lamBindSelf(s)
    return s
}

var s *Shape = NewSquare()._lamUp_Shape()
... n += s._lamVirt_Area() ...
```

- **Representation.** A base-typed value stays a plain `*Shape`: field access
  is a direct (promoted) field read and needs no accessor. Where a `Square`
  flows into a `Shape` (argument, assignment, re-assignment, return, field,
  `optional[Shape]`, list/dict literal element, `append` / `insert` /
  `extend([...])`, `d[k] = v`, `??`, ternary, `==` with a base-typed value)
  it is converted to its embedded parent with the nil-safe, inlinable
  `_lamUp_Shape()` (one method per ancestor; grandparents included). Lists
  stay invariant: an existing `list[Square]` is not a `list[Shape]` (the
  semantic checker reports it), only element-wise uses convert.
- **Dynamic dispatch.** Every class that has a subclass anywhere in the
  program gets `_lamSelf` (bound by each subclass constructor in every
  embedded ancestor, after the parents are allocated and again after an
  explicit `base.init(...)` / alias `init` call or an inherited
  constructor, which replace the parent pointer), `_lamBindSelf` and an
  interface listing the methods it defines that a descendant overrides.
  Each such method gets a wrapper `_lamVirt_<M>` that Go inlines. A call
  `x.m(...)` — `self.m()` inside the class or any receiver from outside —
  lowers to `x._lamVirt_M(...)` only when a strict descendant of the static
  class of `x` overrides `m`; every other call (classes never subclassed,
  methods nobody overrides, a subclass receiver whose own method is final)
  is a direct Go call exactly as before, so hot loops over plain classes
  are unaffected. A plain `Shape` pays one nil check per virtual call, a
  subclass instance an interface call.
- **Dynamic class.** `_lamDyn()` returns the most-derived object and
  `_lamIs_C()` (defined on every class in a hierarchy and promoted to all
  its descendants) recovers its `*C` view, so `isinstance(x, Square)` on a
  `Shape` is `_, ok := x._lamDyn().(interface{ _lamIs_Square() *Square })`,
  a class pattern `case Square(side=n) as sq` on a `Shape` subject binds
  the narrowed `*Square`, and the same test powers class patterns on `any`
  / interface subjects and typed `catch` clauses, for subclasses declared in
  any module. `isinstance(x, Shape)` with `x` statically a `Shape` or a
  subclass is `x != nil`. A base-typed value converted to an interface type
  or `any` is stored as its most-derived object (`x._lamDyn()`), so interface
  calls reach the overrides too.
- **Printing.** Classes in a hierarchy implement `fmt.Formatter`: the
  object prints as `&{...}` with its own fields, the parent part as a
  nested `{...}` (`&{{sq} 3}` for a `Square` with `side = 3` whose `Shape`
  part has `name = "sq"`), the internal `_lamSelf` never shows, a base-typed
  value prints its most-derived object, and `__str__` (own or inherited)
  still wins. Classes outside any hierarchy keep Go's default formatting.
  The unexported field is also skipped by `encoding/json`.
- `self.m()` called from inside a parent's *constructor* while that parent is
  being built by `base.init(...)` still runs the parent's own method: the
  binding happens when the parent constructor returns.
- Dispatch stays static for dunder methods (printing dispatches through
  `Format`), generator methods, methods with their own type parameters,
  overloaded method names, method values (`self.m` without a call) and
  overrides whose signature spells a generic base's type parameter
  differently (`get(self) -> int` overriding `Box[T].get(self) -> T`).
- Downcasts are explicit: `sq: Square = shape` is a compile error; narrow
  with a class pattern (`match shape { case Square() as sq { ... } }`) or
  test with `isinstance`, which binds the downcast view where the test
  holds (see "isinstance narrowing").
- In a diamond (`D(B, C)` with `B(A)` and `C(A)`) each path embeds its own
  `A`; the upcast to `A` uses the first base's path.

#### Constructors

A class without its own `init` inherits the constructor of its nearest
ancestor that has one (bases searched depth-first, left to right, like
Python's MRO for non-diamond hierarchies): same parameters, defaults and
named arguments, with generic base type arguments substituted
(`class IntBox(Box[int])` gets `NewIntBox(value int)`). The constructor
allocates the parents, applies the class-body field defaults, builds the
parent the `init` comes from with its constructor and binds the object into
it:

```go
func NewDog(name string, age int) *Dog {
    s := &Dog{}
    s.Animal = &Animal{}
    s.Animal._lamBindSelf(s)
    s.Animal = NewAnimal(name, age)
    s.Animal._lamBindSelf(s)
    return s
}
```

A class that declares `init` is checked against its own `init` only; the
child constructor signature does not need to match the parent signature, and
parent parameters do not need defaults merely because the child constructor
has different parameters. A class with no `init` anywhere in its ancestry gets
a no-argument constructor.

When a child constructor is emitted, the compiler first allocates zero-value
embedded parent structs, for example `s.Parent = &Parent{}` (grandparents and
imported parents included), before running the child constructor body.
Explicit parent initializer calls replace that zero-value parent with the
result of `NewParent(...)`; the child's class-body defaults for fields the
parent declares (`sides: int = 4` in `Square` over `Shape`'s `sides: int = 0`)
are applied again afterwards unless the parent's `init` assigns the field
itself.

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
| `a if c else b`                    | Go ternary via IIFE typed by its branches (`func() string { ... }()`); joined with `+` to a `str`, it is a `string` closure even when one branch's type is not tracked |
| `name := expr`                     | IIFE binding `name` locally and returning it (binding does *not* escape)  |
| f-strings `f"x={x}"`               | `fmt.Sprintf("x=%v", x)` with method-call translation                     |
| `s[i]` / `s[a:b]`                  | Go string byte indexing / string slicing                                 |
| `xs[-1]` (constant negative index) | `xs[len(xs)-1]` (an IIFE for a non-trivial receiver); still an `IndexError` when out of range |
| `xs[i]` on a list / string / tuple, `i` not provably non-negative | `xs[i+len(xs)&(i>>63)]` (see "Runtime negative indexes") |
| `xs[a:b:c]` with negative / stepped bounds | typed IIFE keeping the receiver's Go type (`[]T` / `string`), reflection only for unknown receivers |
| `obj?.field` on a pointer receiver | `func() *T { if o := obj; o != nil { v := o.F; return &v }; return nil }()` for a value field (`T` itself when nilable) |
| `d[k] ?? fallback` on a map        | comma-ok lookup: the fallback for a missing key                            |
| `1.5 * n` with `n: int`            | `1.5 * float64(n)` (int operands of a mixed int/float expression)          |
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
| `xs.pop()` / `xs.pop(i)`           | IIFE that returns the removed (last / `i`-th) element and shrinks `xs`    |
| `xs.map(fn)`                       | IIFE that builds a typed result slice from callback/context information   |
| `xs.filter(fn)`                    | IIFE that keeps elements where `fn(_v)` is true                           |
| `xs.reduce(fn[, init])`            | IIFE that accumulates (in `init`'s type when given, else the element's)   |
| `xs.any(fn)` / `xs.all(fn)`        | IIFE with early return                                                    |
| `xs.foreach(fn)`                   | IIFE with no result                                                       |
| `xs.sort([compare][, inplace])`    | `sort.Slice` copy by default; when `inplace=true`, sorts and returns `xs` |
| `xs.sort(key=f)` / `xs.sort(f)` with a one-parameter `f` | `sort.SliceStable` comparing `f(x)` (ordered keys natively, others by `%v`) |
| `sorted(xs, key=f, reverse=b)`     | `sort.SliceStable` copy preserving the receiver/context element type; `key` compares `f(x)` |
| `enumerate(xs)`                    | IIFE materializing index/value pairs as `[]interface{}` tuples            |
| `isinstance(x, T)`                 | Type-specific runtime check: assertion, nil test, or `reflect.Kind` (`lamJSONRaw(x)` first on `json`, `x != nil` on an optional value type) |

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
such as `names: list[str] = nums.map(...)` shapes the emitted result slice;
otherwise the body's type does (`words.map(lambda w: len(w))` is a `[]int`),
and the expression type of a `map` / `filter` / `sort` / `reduce` call feeds
the next link of a chain. A builtin passed by name (`words.map(len)`) is
wrapped in a function literal.

Membership lowering follows the container shape: strings use
`strings.Contains`, maps and sets emit a key-presence lookup, and lists emit a
small loop using `reflect.DeepEqual` so nested collection elements can be
tested without triggering Go's non-comparable-slice errors. Chained comparisons
such as `1 < x < 10` are lowered pairwise with `&&`.

### Runtime negative indexes

`xs[i]` on a list, string or tuple counts from the end when `i < 0`
(Python semantics). Unless the index is provably non-negative, the index
gets a branch-free adjustment: `i>>63` is `-1` (all bits set) for a
negative `int` and `0` otherwise, so `i + len(xs)&(i>>63)` adds the length
only to a negative index. It stays inlined, costs no branch misprediction
on mixed signs, and an index still out of range panics with Go's runtime
index error, which `catch IndexError` (and `LookupError`) recognises.

| Lam | Go |
|---|---|
| `xs[i]`, `xs[i] = v`, `xs[i] += v` | `xs[i+len(xs)&(i>>63)]` |
| `xs[f(k)]` (index evaluated once) | `xs[func(i, n int) int { return i + n&(i>>63) }(f(k), len(xs))]` |
| `make()[i]` (receiver evaluated once) | `func(s []int, i int) int { return s[i+len(s)&(i>>63)] }(Make(), i)` |
| `rows(k)[i] = v` (target with such a receiver) | `(*func(s []int, i int) *int { return &s[i+len(s)&(i>>63)] }(Rows(k), i)) = v` |
| `xs[k]` with `for k in range(len(xs))` | `xs[k]` |

A receiver is reused as written when evaluating it twice has no effect
(names, fields, `grid[r]` with a name or constant index); an index of
another integer type is converted with `int(...)`. Maps (`d[k]`), `json`
values and classes with `__getitem__` are unaffected.

`compiler/index_bounds.py` decides which indexes skip the adjustment: per
top-level function or method it computes a lower bound for each local
whose every assignment is bounded (the greatest fixpoint of
`bound(x) = min(bounds of the values assigned to x)` over `x = e`,
`x += e`, `x -= <constant>`, `x++`, `for x in range(...)`,
`for x, _ in enumerate(...)`, widened to unknown when a bound keeps
decreasing; parameters, destructuring and other bindings are unknown;
module globals are never trusted), stores them on the `funcdef`, and the
getitem lowering asks whether the index expression has a non-negative
lower bound given them (constants, `len(...)`, `abs`, `min` / `max`,
`a + b`, `a - <constant>`, `* // %` of non-negative values, ternaries),
including in f-string slots. `xs[len(xs) - 1]` on a side-effect-free
receiver is also left as is: its index is `-1` only for an empty `xs`,
out of range either way.

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
- A local (variable, parameter, loop or comprehension variable) named like a
  function of the program is a plain Go local: its declaration uses its own
  name, and a function the module does not import has its metadata hidden in
  that module, so the declaration and every use (`one.start()`, `f(one)`,
  closures) agree; a local named like an imported function shadows it in its
  function, like Python.
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
| `del d[k]`            | `delete(d, k)`                                                             |
| `del xs[i]` (list)    | `xs = func(s []T, i int) []T { if i < 0 { i += len(s) }; _ = s[i]; return append(append(make([]T, 0, len(s)-1), s[:i]...), s[i+1:]...) }(xs, i)` (the bounds check raises `IndexError`; the kept items go to a new slice, so a view such as `ys = xs[:]` is not shifted) |
| `del xs[a:b]` / `del xs[a:b:k]` | a closure clamping the bounds like Python and copying the kept items to a new slice (a step marks the removed positions) |
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
  buffered to exactly one result. The handle carries a *thunk*: an
  `async func f() -> T` returns `chan func() T` (`chan func()` when it returns
  nothing), and `await call` / `await handle` lowers to `(<-handle)()`. A
  `return v` publishes `func() T { return v }` (a void body publishes
  `func() {}` when it finishes, so awaiting it never deadlocks). A deferred
  `recover()` in the goroutine publishes `func() T { panic(r) }` instead, so an
  exception raised in the async body is re-raised by the `await` and can be
  caught there. The same emitter is used for top-level functions, instance
  methods, and static methods, so receiver type arguments, private/static Go
  naming, and `Result[T]` payloads are preserved.

```lam
async func double(n: int) -> int {
    return n * 2
}
x: int = await double(21)
```

→

```go
func Double(n int) chan func() int {
    __lam_async_ch := make(chan func() int, 1)
    go func() {
        defer func() {
            if r := recover(); r != nil {
                select { case __lam_async_ch <- func() int { panic(r) }: default: }
            }
        }()
        {
            var __lamAsyncV int = n * 2
            __lam_async_ch <- func() int { return __lamAsyncV }
        }
        return
    }()
    return __lam_async_ch
}

var x int = (<-Double(21))()
```
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
- **F-string slots are parsed on their own:** the lexer sees an f-string
  as one token. Before parsing, `compiler/fstrings.py` replaces each
  delimiter quote *inside a slot* (`f"{d["k"]}"`) with a private-use
  character (same length, so lines and columns do not move) so the lexer
  and the line-based passes see one literal; the slot splitter restores
  it, finds each slot's end and format spec outside nested strings and
  brackets, and every slot expression is parsed and lowered (and checked)
  like ordinary code. The formatter applies the same protection.
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
