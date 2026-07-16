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

## Naming

| Lam identifier            | Go identifier            | Notes                                   |
|---------------------------|--------------------------|-----------------------------------------|
| `my_func` (top-level)     | `My_func`                | Title-cased first letter; rest verbatim.|
| `_helper`                 | `_helper`                | Leading underscore kept, no rename.     |
| `__init__`                | `Init` / `NewX`          | Constructors become `NewClass`.         |
| `__str__`, `__repr__`     | `String`                 | Satisfies `fmt.Stringer`.               |
| Other dunder ops          | `Add`, `Sub`, `Eq`, ...  | See `DUNDER_OPS` in `constants.py`.     |
| `self`                    | `s`                      | Fixed receiver name in all methods.     |
| `Class` / `Class.method`  | `Class` / `Method`       | Methods are public unless prefixed `_`. |
| `private func foo()`      | `foo`                    | Go unexported (lowercase initial).      |

Classes always become pointer receivers: a constructor returns `*Class`, and
methods are defined on `(s *Class)`.

Go-only keywords may be used as Lam declaration names when this mapping produces
a non-keyword Go name. For example, `func select()` lowers to `func Select()`,
`class Query { func select(self) { ... } }` lowers to a `Select` method, and a
static method named `switch` lowers to a namespaced function such as
`Query_switch`. Names emitted verbatim, such as locals, parameters, import
aliases, and private instance methods, still cannot use Go-only keywords.

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
| `T \| None`      | `T` (pointer if applicable) |
| `Callable[[A], B]` / `func(A) -> B` | `func(A) B` |

`list`, `dict` and `set` without type arguments default to
`[]interface{}`, `map[string]interface{}` and `map[interface{}]bool`
respectively. See `TYPE_MAP` in `compiler/constants.py`.

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

`Result` is not a compiler generic. The stdlib class has two
`any` fields (`value`, `error`), so both `Result.Ok(v)` and
`Result.Err(e)` lower to ordinary static-method calls and preserve
the payload as `interface{}`. The `?` propagation path can cast into
an annotated target; direct `.value`, `.error`, `unwrap()`, and
`unwrapOr(...)` reads stay `interface{}` unless the user asserts in
`go!`:

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

### `for` / `while`

`for x in iterable` becomes `for _, x := range iterable`.
`for i, x in enumerate(xs)` becomes `for i, x := range xs`.
`for i in range(n)` becomes `for i := 0; i < n; i += 1`.
`while cond` becomes `for cond`.
Both loop kinds support an `else` suite, which is emitted using a small
`broke` boolean guard — see `_for_else_break` / `_while_else_break` in the
transpiler.

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

The `.Value.(int)` cast is injected automatically when the enclosing
annassign supplies a target Go type — `_visit_annassign` publishes the
type via `_propagate_cast_hint` on the scoped-context stack and the
`propagate` branch in `_expr_to_go` reads it when emitting the
substitution. In untyped contexts (e.g. `return Result.Ok(parseInt(s)?)`)
the substitution is plain `__qN.Value` of type `interface{}`.

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
don't leak between consecutive `do` blocks. The user must
`from lamerrors import Result` for the emitted `*Result` /
`Result_Ok` references to resolve; we don't auto-inject the import.

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
- Overloads dispatch on **arity *and* parameter type**. The
  transpiler walks the per-name `_overload_variants` list after
  the pre-scan and assigns each variant a stable Go-name suffix:
  `Name_<argcount>` when arities differ, `Name_<argcount>_<sigSuffix>`
  when two variants share an arity (`Describe_1_int` vs
  `Describe_1_str` vs `Describe_1_sliceOfInt`). Call sites build
  the same signature from each argument's inferred Go type
  (`_infer_call_arg_sig`) and pick the matching suffix; typed
  locals participate via `_var_go_types` so `nums: list[int]`
  routes to the `list[int]` overload. Variadic functions
  (`*args`) still can't be overloaded.

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
stripped so source paths can't false-positive); names that appear
exactly once — i.e. only in their own declaration line — get a trailing
`_ = name` before the scope closes.

Limitations:

- **Compiler-internal names** (anything starting with `_` or
  `__`) are excluded from the diff so we don't churn output for
  things the user can't see (`__qN` from `?` propagation,
  `__lamPanicked` etc.).

Pair this with the **semantic checker's unused warnings**
(unused imports, unused parameters, unused locals — see
[`SYNTAX.md`](#/docs/syntax?h=compiler-diagnostics)) to get the full
warn-don't-error stack: Lam tells the user, the transpiler keeps
Go happy, the build proceeds.

## Classes

```lam
class Point {
    static originCalls: int = 0

    func __init__(self, x: int, y: int) {
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
| `a ** b`                           | `math.Pow(float64(a), float64(b))`                                        |
| `a in xs`                          | `a == xs` (placeholder — see *Limitations*; container-aware lowering pending) |
| `not x`                            | `!x`                                                                      |
| `x and y`, `x or y`                | `x && y`, `x \|\| y`                                                      |
| `a if c else b`                    | Go ternary via IIFE                                                       |
| `name := expr`                     | IIFE binding `name` locally and returning it (binding does *not* escape)  |
| f-strings `f"x={x}"`               | `fmt.Sprintf("x=%v", x)` with method-call translation                     |
| `[expr for x in xs if c]`          | IIFE building a `[]T` slice                                                |
| `{k: v for x in xs}`               | IIFE building a `map[K]V`                                                  |
| `{expr for x in xs}`               | IIFE building a `map[interface{}]bool` (set)                               |
| `(expr for x in xs)`               | Same lowering as the list comprehension form                              |
| `{a, b, c}` (set literal)          | IIFE that inserts each element into a `map[interface{}]bool` (deduplicates) |
| `len(x)`                           | `len(x)`                                                                  |
| `str(x)` / `repr(x)`               | `fmt.Sprintf("%v", x)` / `fmt.Sprintf("%#v", x)`                           |
| `int(x)` / `float(x)`              | `int(x)` / `float64(x)`                                                   |
| `print(...)`                       | `fmt.Println(...)`                                                        |
| `input(prompt)`                    | `bufio.NewScanner(os.Stdin)` one-liner                                    |
| `File.open(path, "w")`             | `File_open(path, "w")`, returning a stdlib `File` instance                |
| `xs.length()`                      | `len(xs)`                                                                  |
| `xs.append(v)`                     | `xs = append(xs, v)`                                                      |
| `xs.map(fn)`                       | IIFE that builds a new slice; picks `.(T)` only if `fn` isn't a user func |
| `xs.filter(fn)`                    | IIFE that keeps elements where `fn(_v)` is true                           |
| `xs.reduce(fn[, init])`            | IIFE that accumulates                                                     |
| `xs.any(fn)` / `xs.all(fn)`        | IIFE with early return                                                    |
| `xs.foreach(fn)`                   | IIFE with no result                                                       |
| `xs.sort([compare][, inplace])`    | `sort.Slice` copy by default; when `inplace=true`, sorts and returns `xs` |
| `sorted(xs)`                       | `sort.Slice` copy                                                         |
| `enumerate(xs)`                    | passed through with a `/* enumerate */` marker                            |
| `isinstance(x, T)`                 | `true /* isinstance */` (see “limitations” below)                         |

The `xs.map`, `xs.filter`, `xs.any`, `xs.all` lowerings detect whether the
argument refers to a user function (in any of its renamed forms: raw,
`_go_public_name`, `_go_private_name`) and skip the `.(bool)` /
`.(elem_type)` interface assertion when it does.

## Modules and imports

```lam
from lammath import Math
from lamos import Os
```

- Every module under `lib/<name>.lam` is transpiled to its own Go source
  file alongside the user program. Classes, static methods, and top-level
  defaults are exposed under `<ModuleName>_symbol` names. For the most
  common case (`from mod import Class`), the transpiler rewrites
  `Class.method(...)` to the flat `Class_method(...)` function.
- A module may opt into a Go import by writing `go! { import "pkg" }`.
  Imports from go blocks are consolidated into a single Go import group.
- Defaults, arities, variadic markers and static-method metadata from
  library modules are merged into the main transpiler before it walks the
  main file, so call sites in the user's program can fill in library
  defaults exactly like their in-file counterparts.

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
[`SYNTAX.md`](#/docs/syntax) → "Scoping inside `go!` blocks" and
[`server_plugins.md`](#/docs/server_plugins) → "Identifier scoping inside
`go!`".

## Assertions, deletions, and other statement forms

| Lam                   | Go                                                                         |
|-----------------------|----------------------------------------------------------------------------|
| `assert cond, "msg"`  | `if !(cond) { panic("msg") }`                                              |
| `del x`               | `_ = x` (no-op; freed by GC)                                               |
| `pass`                | emits nothing; the empty block is legal Go                                 |
| `with f as x: body`   | Go defer pattern (`defer x.Close()`) + inline body                         |
| `yield expr`          | `chan <- expr`; generators compile to a goroutine + channel pair           |

## Concurrency

- `async func` is implemented by wrapping the body in a goroutine that
  writes its return value to a buffered channel. `await call` unwraps the
  channel with `<-`.
- Raw `go!` blocks can use `go func() { ... }()` and channels directly,
  which is what `lamnet`, `lamhttp`, and the `lamheap` backing store do
  internally.

## External Go modules

The compiler runs `go mod tidy` after writing the generated source, so
any `go!` block that imports a third-party package will trigger an
automatic `go get`. The toolchain caches downloads under `$GOPATH/pkg`,
so repeat builds incur the network cost only once.

This is how `lamarray` pulls in **gonum** (`gonum.org/v1/gonum/mat`,
`floats`, `stat`) for BLAS-accelerated dense linear algebra without
requiring users to manage Go modules manually. The relevant flow:

1. The library `.lam` file declares its imports inside a `go! { import (...) }`
   block. Those import paths land verbatim in the emitted `.go` file.
2. `lamc` writes the generated Go to a temporary directory, invokes
   `go mod init`, then `go mod tidy`. `tidy` resolves the new imports
   against the public proxy and downloads them.
3. `go build` runs against the populated module — same toolchain, same
   build cache as any hand-written Go program.

If you want a stdlib-only build (no network at compile time), pin every
dependency by vendoring or by writing equivalent kernels in pure Go via
inline `go!` blocks. There are no Lam-side hooks for offline builds yet.

### Stdlib Go-module pins

Third-party Go modules that the Lam stdlib itself depends on are
listed with frozen versions in
`compiler/stdlib_go_deps.py::STDLIB_GO_PINS`. Examples: `lamdb`
blank-imports `modernc.org/sqlite`, `lamserver_ws` imports
`github.com/gorilla/websocket`, `lamenv` reaches into
`github.com/BurntSushi/toml` and `gopkg.in/yaml.v3`, and so on.
Each entry is a `<go_module_path> = "<v…>"` pair sorted by
module path for diff stability.

`_collect_go_pins` in `compiler/transpiler.py` seeds the
project's pin map with `STDLIB_GO_PINS` first and then layers
project-side pins (from `lamlib.toml` `[go-deps]` and
`lamlib.lock.toml` `[go_pins.*]`) on top. Project pins win over
stdlib pins via Go's MVS rule (the higher version is selected),
so users can still upgrade an individual stdlib transitive
without forking the compiler. Every other stdlib module stays
reproducible across builds — no silent upgrade between when the
stdlib was last tested and when `go mod tidy` runs in your build.

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
| `lamserver.Server`            | `*http.Server` (+ `[]any` route table) | `RawSrv`   |
| `lamcache.LruCache`           | `*container/list.List` + map        | `Lst`/`Idx`|
| `lamcache.TtlCache`           | `map[any]*ttlEntry`                 | `Idx`      |
| `lamconcurrency.Channel`      | `chan interface{}`                  | `Ch`       |
| `lamconcurrency.WaitGroup`    | `*sync.WaitGroup`                   | `Wg`       |
| `lamheap.Heap`                | `*container/heap` adapter           | `Items`    |

When debugging a stdlib module, that field is a good first place to
inspect — the Lam method bodies usually do little more than typecheck
inputs and forward to the underlying Go API.

## Limitations and pitfalls

- **Nested function definitions:** a nested `func` declaration is
  rewritten to a Go closure assignment (``name := func(...) { ... }``),
  so it cannot carry its own generic type-parameter list. The semantic
  checker rejects nested generic functions with a Lam diagnostic; lift
  the generic helper to module scope if you need type parameters.
- **Multi-line function calls:** calls may break after `(` and after
  comma-separated arguments, with an optional trailing comma before
  `)`. Positional and keyword arguments use the same semantics as
  single-line calls.
- **Implicit conversions:** integer ↔ float conversions are explicit.
  `Conv.toFloat`, `int()`, `float()` exist for readability.
- **Reserved words:** identifiers colliding with Go keywords (e.g.
  `range`) get an underscore suffix; prefer `range_` in API designs.
- **Multiple try/catch in one function:** the current lowering shares a
  single `defer recover()` per function, so two consecutive try/catch
  blocks won't both fire. Wrap each `try` in its own helper if you
  need independent recovery sites.
- **Chained user-method calls lose return-type info:** the transpiler
  doesn't yet thread class-method return types through chains, so
  `iter.filter(p).map(f).take(3)` fails to type-resolve the inner
  receivers. Bind each step to a typed intermediate (`a: Iter =
  iter.filter(p); b: Iter = a.map(f); ...`). The functional combinators
  on plain lists (`xs.filter(...).map(...)`) are exempt because the
  transpiler unwinds them into IIFEs of known element type.
- **Nested typed list literals don't infer:** `rows: list[list[float]]
  = [[1.0, 2.0], [3.0, 4.0]]` keeps the inner literals as
  `[]interface{}`. Build them via a typed intermediate or a small
  helper that returns `list[float]`. Top-level typed literals
  (`xs: list[float] = [1.0, 2.0]`) work fine.
- **F-string interpolations are textual:** the f-string lowerer runs
  string-level regex transforms on each `{expr}`. It doesn't accept
  escaped quotes (`f"x={d[\"key\"]}"`) and may not rewrite every
  identifier inside nested method-call args. When in doubt, lift the
  expression into a `:= …` line above the `print(...)` and interpolate
  the simple variable.
- **Lam `int(s)` is a Go type-conversion, not a parser:** to coerce a
  string into an integer at runtime, call `strconv.Atoi(s)` from a
  `go!` block (or use `Conv.toInt` for a silent default).
- **`in` is a placeholder lowering:** `a in xs` currently emits a raw
  `==` comparison rather than the container-aware membership test
  used in Python. For lists use a `for` loop or `xs.contains(...)`,
  for dicts/sets use `_, ok := m[k]` from a `go!` block until the
  proper lowering lands.
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
- **`nonlocal` is a no-op declaration:** nested functions and lambdas
  already capture surrounding locals directly. The visitor emits only
  a comment, so `nonlocal` does not change binding, assignment, or
  closure behavior. Module-level state should be touched via
  `global` instead.

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
