# Lammergeier Lang — Syntax Reference

A typed programming language that compiles to Go. Combines Python-like readability with Go's performance and type safety. Uses C-style `{}` blocks and the `func` keyword. File extension: `.lam`.

---

## Start Here: Guided Tour

Lammergeier is easiest to learn if you think in three layers:

1. **Lam syntax** for day-to-day code: Python-like expressions,
   explicit typed APIs, braces for blocks, and Go-backed runtime
   semantics.
2. **Stdlib classes** for batteries-included work: `Math`, `Strings`,
   `Json`, `Db`, `DataFrame`, `Result`, and the rest of `lib/`. The Vulture
   `Server` lives in the separately installed `@lam/vulture` package.
3. **`go!` interop** for the rare cases where you need exact Go
   syntax, Go packages, type assertions, or low-level concurrency.

The smallest useful file is a `main` function:

```lammergeier
func main() {
    name: str = "Lammergeier"
    count: int = 3
    for i in range(count) {
        print(f"{i}: hello, {name}")
    }
}
```

Compile and run it with:

```bash
lamc app.lam --run
```

### Program Shape

Source files contain imports, raw Go blocks, constants, functions,
classes, interfaces, and top-level statements. Application entry is
`func main()`. Library modules usually expose classes or functions and
keep `main` empty with `pass`.

```lammergeier
from lamstrings import Strings
from lamconv import Conv
from lamerrors import Result, Error

const AppName: str = "orders"

func normalizeSku(s: str) -> str {
    return Strings.toUpper(Strings.trim(s))
}

func parseQty(raw: str) -> Result[int] {
    if raw == "" {
        return Result.Err(Error("ValidationError", "quantity is required"))
    }
    parsed: Result[int] = Conv.parse(raw)
    return parsed
}

func main() {
    print(f"{AppName}: {normalizeSku(' ab-10 ')}")
}
```

### Choosing Types

Use concrete types at module boundaries and public functions. Use the native
`json` type for decoded JSON; reserve `any` for values whose shape is genuinely
unrestricted, such as plugin state or Go interop payloads.

```lammergeier
typeName: str = "invoice"
ids: list[int] = [10, 11, 12]
labels: dict[str, str] = {"new": "New", "paid": "Paid"}
seen: set[str] = {"a", "b", "c"}
payload: json = {"id": 10, "status": "new"}

func total(values: list[float]) -> float {
    acc: float = 0.0
    for v in values {
        acc += v
    }
    return acc
}
```

When a function accepts a callback, use a function type instead of
`any`:

```lammergeier
func applyTwice(f: func(int) -> int, x: int) -> int {
    return f(f(x))
}

func inc(x: int) -> int { return x + 1 }

func main() {
    print(applyTwice(inc, 5))       # 7
}
```

### Conversions: `int()`, `float()`, `bool()`

| Argument | Result |
|---|---|
| `int`, `float` and other numerics | converted (`int(3.7)` truncates to `3`; `bool(n)` is `n != 0`) |
| `bool` | `1` / `0` (`1.0` / `0.0`) |
| `str` | parsed; malformed text throws `ParseError` (`int("42")`, `float("2.5")`, `bool("true")`) |
| `json` | JSON numbers and booleans convert, JSON strings are parsed (`ParseError`), arrays/objects/null throw `TypeError` |
| class instance | calls `__int__` / `__float__` / `__bool__` (inherited methods included); compile error when the class does not define it |
| `any` | compile-time **warning**; checked at runtime: `int` accepts every integer kind (`int64` database values, `uint16`, …), `float` every floating kind (`float32`, `float64`), `bool` a bool; anything else throws `TypeError` |
| anything else (`list`, `dict`, `bytes`, …) | compile error: ``cast `int` is not defined for `list[int]` `` |

Use `Conv.parse(s)` when malformed input is an expected outcome and you want a
`Result` instead of an exception. Conversion methods take only `self` and return
the target type:

```lammergeier
class Money {
    func init(self, cents: int) {
        self.cents: int = cents
    }

    func __int__(self) -> int { return self.cents }
    func __float__(self) -> float { return float(self.cents) / 100.0 }
    func __bool__(self) -> bool { return self.cents != 0 }
}

func main() {
    price: Money = Money(1250)
    print(int(price), float(price), bool(price))   # 1250 12.5 true
    print(int("42") + int(true))                   # 43
}
```

`str(x)` formats any value and never fails, including `any`.

### Data Flow With Collections

Lists, dicts, sets, tuples, destructuring, comprehensions, and slices
cover the common data-manipulation shapes:

```lammergeier
func main() {
    nums: list[int] = [1, 2, 3, 4, 5]
    evens: list[int] = [n for n in nums if n % 2 == 0]
    squares: dict[int, int] = {n: n * n for n in nums}

    first: int = nums[0]
    middle: list[int] = nums[1:4]
    nums[1:3] = [20, 30]

    user: dict[str, any] = {"id": 1, "email": "ada@example.com"}
    cfg: dict[str, dict[str, any]] = {
        "service": {
            "host": "localhost",
            "port": 8080
        }
    }
    { email } = user
    print(email)
}
```

Use `list[T]`, `dict[K, V]`, and `set[T]` annotations when the
literal is empty or when Go needs an exact element type. Nested
dictionary literals work in typed contexts and lower recursively; a
comma is required between entries, but the final entry in a dictionary
does not need a trailing comma.

```lammergeier
names: list[str] = []
counts: dict[str, int] = {}
flags: set[str] = {}
```

### Native JSON

`json` is a native recursive value type for REST payloads, configuration files,
database JSON/JSONB columns, and caches. It accepts JSON null (`None`), booleans,
strings, finite integer/float numbers, arrays, string-keyed objects, and nested
`json` values. Classes opt in with `func __json__(self) -> json` and are converted
explicitly with `json(instance)`. Numeric object keys, sets/tuples, arbitrary
`any`, and direct class values are rejected before Go emission.

```lammergeier
from lamjson import Json
from lamerrors import Result

class Image {
    func init(self, width: int, height: int) {
        self.width: int = width
        self.height: int = height
    }
    func __json__(self) -> json {
        return {"width": self.width, "height": self.height}
    }
}

payload: json = [{
    "hello": 100,
    "nested": {"y": [1, 2, 3]},
    "image": json(Image(1024, 1024))
}]

second: int = payload[0].nested.y[1]
payload[0].hello = 101
encoded: str = Json.encode(payload)?
```

Dot access is available only when the key is a Lam identifier (`value.name`,
`value.camelCase`, `value._private`). Every JSON string remains a valid key;
numeric strings, punctuation, empty strings, and whitespace use brackets:
`value["123"]`, `value["first-name"]`, `value[""]`, and `value[" "]`.
`value.123` is invalid Lam syntax because `123` is not an identifier.

Object bracket/property access and integer array indexing return `json`; an
annotated primitive assignment performs the runtime extraction
(`name: str = payload.name`). `len` works on objects, arrays, and strings; `del
value["key"]` removes an object member. JSON values compare
structurally with `==` / `!=`, compare to `None` as JSON null, and support
`missing ?? fallback` in typed contexts.

The builtin `json(value)` performs statically checked conversion. A string stays
a JSON string; use `Json.decode(text)` to parse encoded JSON. A custom class must
implement `__json__` and still be passed explicitly to `json(instance)`.
`Json.fromDict`, `Json.fromList`, and `Json.fromValue` validate dynamic Lam
values and convert them to `json`. `Json.toDict` / `Json.toList` convert an
object/array back for algorithms where ordinary collections are more useful.
`Json.decode` and `Json.encode` use unsuffixed typed `Result` contracts. Native
`json` automatically brings in its runtime support, so literals and annotations
do not require an explicit import.

The semantic checker reports statically knowable JSON mistakes during Lam
compilation: non-string object keys; tuple/set arrays; incompatible nested
values; direct `any`; missing or malformed `__json__` declarations; implicit
class conversion; invalid `json(value)` arguments; invalid boolean/class/etc.
indices (only `str` or `int` are allowed); incompatible assignments/returns/call
arguments; and malformed literal strings passed to `Json.decode`,
`Json.decodeInto`, or `Schema.validate`.
Dynamic `Json.fromValue(any)` data, decoded wire shapes, object-vs-array access,
missing keys, bounds, cycles/depth, and non-finite runtime floats remain runtime
checks because their values are not knowable from source.

### Control Flow

`if`, `elif`, `else`, `while`, `for`, `break`, `continue`, `defer`,
`try/catch/finally`, `do/catch`, and `match/case` are statement forms.
Boolean negation is `not`; `!` belongs inside `go!` blocks.

```lammergeier
func classify(n: int) -> str {
    if n < 0 {
        return "negative"
    } elif n == 0 {
        return "zero"
    }
    return "positive"
}

func findFirstEven(nums: list[int]) -> int {
    for n in nums {
        if n % 2 != 0 {
            continue
        }
        return n
    } else {
        return -1
    }
}

func describe(value: any) -> str {
    match value {
        case 0 { return "zero" }
        case [x, y] { return f"pair {x}/{y}" }
        case {"kind": k, **rest} { return f"kind={k}" }
        case _ { return "other" }
    }
}
```

### Functions, Defaults, Lambdas, and Nested Helpers

Functions support default arguments, variadic parameters, multiple
returns, nested helper functions, lambdas, and generic type
parameters.

```lammergeier
func greet(name: str, suffix: str = "!", times: int = 1) {
    for i in range(times) {
        print(f"hello, {name}{suffix}")
    }
}

func sumAll(*nums: int) -> int {
    total: int = 0
    for n in nums { total += n }
    return total
}

func divmod_(a: int, b: int) -> (int, int) {
    return a / b, a % b
}

func outer(base: int) -> func(int) -> int {
    func addBase(x: int) -> int {
        return base + x
    }
    return addBase
}

func identity[T](x: T) -> T {
    return x
}

func chooseFirst[T](a: T, b: T) -> T {
    func pick[U](x: U, _fallback: U) -> U {
        return x
    }
    return pick[T](a, b)
}
```

Nested helper functions may also be generic. They use the same
`func name[T, U: constraint](...)` syntax as top-level functions, can
capture typed locals from the enclosing function, and can be called with
inferred type arguments (`pick(a, b)`) or explicit type arguments
(`pick[int](a, b)`).

### Classes and Interfaces

Classes compile to Go structs with pointer receivers. **Prefer `init` for
constructors.** It is an alias of `__init__`, and both spellings are accepted.
Annotate fields where they are first assigned, and keep public method
signatures explicit.

```lammergeier
interface Stringable {
    func toString() -> str;
}

class Money {
    func init(self, cents: int, currency: str = "EUR") {
        self.cents: int = cents
        self.currency: str = currency
    }

    func add(self, other: Money) -> Money {
        if self.currency != other.currency {
            throw ValueError("currency mismatch")
        }
        return Money(self.cents + other.cents, self.currency)
    }

    func toString(self) -> str {
        return f"{self.currency} {self.cents}"
    }
}
```

Use operator overloads only when the operator is the clearest API.
Named methods are better for domain operations with side effects or
surprising constraints.

```lammergeier
class Vec2 {
    func init(self, x: float, y: float) {
        self.x: float = x
        self.y: float = y
    }

    func __add__(self, other: Vec2) -> Vec2 {
        return Vec2(self.x + other.x, self.y + other.y)
    }

    func __eq__(self, other: Vec2) -> bool {
        return self.x == other.x and self.y == other.y
    }
}
```

### Error Handling Strategy

Use exceptions (`throw`, `try/catch/finally`) for programmer bugs,
invariants, and failures that should unwind the stack. Use
`Result` for expected failures callers should handle locally.

```lammergeier
from lamerrors import Result, Error

func readConfig(path: str) -> Result {
    if path == "" {
        return Result.Err(Error("ConfigError", "path is empty"))
    }
    return Result.Ok(path)
}

func boot(path: str) -> Result {
    cfg: str = readConfig(path)?
    return Result.Ok(f"loaded {cfg}")
}

func main() {
    do {
        msg: str = boot("settings.toml")?
        print(msg)
    } catch err {
        print(f"boot failed: {err}")
    }
}
```

### When to Use `go!`

Prefer Lam syntax for application logic. Use `go!` when you need:

- a Go import or package not wrapped by the stdlib;
- a type assertion from `any`;
- a goroutine, select statement, mutex, or channel pattern not covered
  by `lamconcurrency`;
- exact access to generated Go names via `LAMMERGEIER.*`;
- a small low-level performance escape hatch.

```lammergeier
go! {
    import "strings"
}

func shout(v: any) -> str {
    out: str = ""
    _ = v  # referenced inside go!
    go! {
        if s, ok := v.(string); ok {
            out = strings.ToUpper(s)
        }
    }
    return out
}
```

Keep `go!` blocks small and typed at the boundary: convert dynamic
values into Lam locals before returning to normal Lam code.

---

## Table of Contents

1. [Basic Types](#basic-types)
2. [Variables & Annotations](#variables--annotations)
3. [Constants](#constants)
4. [Function Types](#function-types)
5. [Functions](#functions)
6. [Ternary Operators](#ternary-operators)
7. [Variadic Arguments](#variadic-arguments)
8. [Classes](#classes)
9. [Constructors](#constructors)
10. [Inheritance](#inheritance)
11. [Operator Overloading](#operator-overloading)
12. [Static Members](#static-members)
13. [Interfaces](#interfaces)
14. [Private Members](#private-members)
15. [Async / Await](#async--await)
16. [Control Flow](#control-flow)
17. [For-Else / While-Else](#for-else--while-else)
18. [Match / Case](#match--case)
19. [List Comprehensions](#list-comprehensions)
20. [Multidimensional Comprehensions](#multidimensional-comprehensions)
21. [Dictionary Comprehensions](#dictionary-comprehensions)
22. [Dictionaries with Non-String Keys](#dictionaries-with-non-string-keys)
23. [Enumerate](#enumerate)
24. [F-Strings](#f-strings)
25. [Multi-line Expressions](#multi-line-expressions)
26. [Multiline Strings](#multiline-strings)
27. [Comments](#comments)
28. [File Management](#file-management)
29. [Try / Catch / Finally](#try--catch--finally)
30. [Errors & Results](#errors--results)
31. [With Statement](#with-statement)
32. [Del Statement](#del-statement)
33. [Slice Assignment](#slice-assignment)
34. [Generators / Yield](#generators--yield)
35. [Functional Programming](#functional-programming)
36. [Multiple Return Values](#multiple-return-values)
37. [Importing Custom Libraries](#importing-custom-libraries)
38. [Standard Library](#standard-library)
39. [Raw Go Injection (go!)](#raw-go-injection-go)
40. [Default Parameter Values](#default-parameter-values)
41. [Function Overloading](#function-overloading)
42. [Compilation](#compilation)

---

## Basic Types

| Type         | Go Type        |
|--------------|----------------|
| `int`        | `int`          |
| `float`      | `float64`      |
| `str`        | `string`       |
| `bool`       | `bool`         |
| `list[T]`    | `[]T`          |
| `dict[K, V]` | `map[K]V`      |
| `any`        | `interface{}`  |
| `None`       | (empty return) |

---

## Variables & Annotations

Variables are declared with type annotations:

```lammergeier
x: int = 10
name: str = "hello"
pi: float = 3.14
active: bool = true
items: list[int] = [1, 2, 3]
scores: dict[str, int] = {"alice": 100, "bob": 95}
```

---

## Constants

`const NAME [: TYPE] = EXPR` declares an immutable binding. The type
annotation is optional — the compiler infers it from the literal or
expression on the right. The semantic checker rejects any later
assignment, augmented assignment, or `const`-redeclaration of the same
name. Constants can be top-level or function-local:

```lammergeier
const PI: float = 3.14
const COUNT = 10        # type inferred as int

func main() {
    const greeting = "hello"
    const limit: int = COUNT * 2
    print(PI, greeting, limit)
}
```

A constant protects only the name, not the value: mutating an
attribute or element of a const-bound object is allowed, just as Go
allows mutation through a `var` of pointer/slice type:

```lammergeier
const arr: list[int] = [1, 2, 3]
arr[0] = 99             # OK — `arr` itself isn't reassigned
# arr = [4, 5, 6]       # error: cannot reassign constant `arr`
```

### Discarding a value with `_`

`_` is the **blank target**, identical to Go's `_`. Use it to call a
function purely for its side effects when the return value isn't
needed, or to suppress an unused-variable diagnostic:

```lammergeier
func touch() -> int { return 1 }

func main() {
    _ = touch()         # call ignored — return value discarded
}
```

The transpiler lowers `_ = expr` to a plain Go `_ = expr` (Go's
blank identifier is pre-declared, so `:=` would be an error here —
the compiler picks the right form automatically). Multiple `_`
targets in tuple-unpacking work the same way: `_, ok := mp[k]`.

---

## Function Types

A function signature can itself be used as a type. Variables, parameters,
return values, and collection elements may be typed as
`func(T1, T2, ...) -> R`. Omit the `-> R` clause for void functions
(`func()`):

```lammergeier
func add(a: int, b: int) -> int { return a + b }
func mul(a: int, b: int) -> int { return a * b }
func greet() { print("hello") }

func apply(op: func(int, int) -> int, x: int, y: int) -> int {
    return op(x, y)
}

func main() {
    f: func(int, int) -> int = add        # function-typed variable
    print(f(2, 3))                         # 5

    ops: list[func(int, int) -> int] = [add, mul]
    print(apply(ops[1], 4, 5))             # 20

    cb: func() = greet                     # void function type
    cb()                                   # hello
}
```

The compiler lowers `func(T, U) -> V` to Go's `func(T, U) V`, so
function-typed values are first-class Go values you can store, pass,
and return.

---

## Functions

Functions use the `func` keyword with type annotations and return type. Blocks use `{}`:

```lammergeier
func add(a: int, b: int) -> int {
    return a + b
}

func greet(name: str) -> str {
    return f"Hello, {name}!"
}

func main() {
    print(add(3, 4))        # 7
    print(greet("World"))   # Hello, World!
}
```

---

## Ternary Operators

Inline conditional expressions:

```lammergeier
label: str = "big" if x > 5 else "small"

# In return statements
func classify(age: int) -> str {
    return "adult" if age >= 18 else "minor"
}

# In function arguments
print("even" if x % 2 == 0 else "odd")
```

---

## Variadic Arguments

Accept a variable number of arguments with `*`:

```lammergeier
func sum_all(*nums: int) -> int {
    total: int = 0
    for n in nums {
        total += n
    }
    return total
}

func main() {
    print(sum_all(1, 2, 3, 4))  # 10
}
```

---

## Classes

Classes with typed fields and methods:

```lammergeier
from lammath import Math

class Point {
    func init(self, x: int, y: int) {
        self.x: int = x
        self.y: int = y
    }

    func distance(self) -> float {
        return Math.sqrt(float(self.x * self.x + self.y * self.y))
    }
}

func main() {
    p: Point = Point(3, 4)
    print(p.distance())  # 5
}
```

Fields can also be declared in the class body. An initializer there is a
per-instance default: every constructor assigns it before running the
`init` body (ancestors' defaults first), and each instance gets its own
fresh value, so `items: list[T] = []` is not shared between instances:

```lammergeier
class Counter {
    name: str = "anon"
    count: int = 0

    func init(self, n: int) {
        self.count = n       # overrides the default
    }
}

print(Counter(3).name)       # anon
```

---

## Constructors

**`init` is the preferred constructor spelling.** It is an alias of
`__init__`, and the compiler accepts both spellings with identical behavior.
New code, standard-library APIs, and examples should use `init`:

```lammergeier
class Circle {
    func init(self, r: int) {
        self.r: int = r
    }

    func area(self) -> float {
        return 3.14159 * float(self.r * self.r)
    }
}

func main() {
    c: Circle = Circle(5)
    print(c.area())
}
```

The legacy spelling remains valid for compatibility:

```lammergeier
class LegacyCircle {
    func __init__(self, r: int) {
        self.r: int = r
    }
}
```

---

## Inheritance

Classes can inherit from one or more parent classes. With one unnamed parent,
the compiler provides a `base` alias inside the child body:

```lammergeier
class Animal {
    func init(self, name: str, species: str) {
        self.name: str = name
        self.species: str = species
    }

    func speak(self) -> str {
        return f"{self.name} the {self.species}"
    }
}

class Dog(Animal) {
    func init(self, name: str, breed: str) {
        base.init(name, "dog")
        self.breed: str = breed
    }

    func speak(self) -> str {
        return f"{base.speak()} barks"
    }
}
```

`init` and `__init__` are equivalent constructor spellings. Calling
`base.init(...)` or `base.__init__(...)` initializes the embedded parent part
of the current child instance.

Methods are virtual: when a parent method calls `self.method()`, the override
of the object's actual class runs, as in Python or Java. Explicit parent calls
such as `base.speak()` always run the parent implementation:

```lammergeier
class Shape {
    func area(self) -> int { return 0 }
    func describe(self) -> str { return f"area {self.area()}" }
}

class Square(Shape) {
    func area(self) -> int { return 9 }
}

print(Square().describe())   # area 9
```

When a method or field is not declared on the child, ordinary access through
`self` or an instance can use the inherited member:

```lammergeier
dog: Dog = Dog("Rex", "labrador")
print(dog.name)
print(dog.speak())
```

### Subtyping

A subclass instance is usable wherever its base class is expected: function
and method parameters, variables and re-assignments, returns, fields,
`optional[Base]`, elements of `list[Base]` / `dict[str, Base]` (literals,
`append`, `insert`, `extend([...])`, `d[k] = v`), `??`, ternaries and `==` /
`!=` comparisons (with `None` or another value). Calls on a base-typed value
run the override of the object's actual class, field access reads the
shared base fields, and `isinstance` tests the actual class:

```lammergeier
class Circle(Shape) {
    func init(self, r: int) { self.r: int = r }
    func area(self) -> int { return 3 * self.r * self.r }
}

func total(shapes: list[Shape]) -> int {
    n: int = 0
    for s in shapes {
        n += s.area()              # Square.area / Circle.area / Shape.area
    }
    return n
}

shapes: list[Shape] = [Square(), Circle(2), Shape()]
print(total(shapes))               # 21
s: Shape = shapes[0]
print(isinstance(s, Square))       # true
```

This also holds across modules: a library function taking a `Shape`
dispatches to the overrides of subclasses declared by the program that
imports it.

The other direction is explicit. `sq: Square = shape` is a compile error;
narrow a base-typed value with a class pattern, which binds the subclass
view (`as`) and its fields:

```lammergeier
func describe(s: Shape) -> str {
    match s {
        case Square() as sq { return f"square {sq.area()}" }
        case Circle(r=r) { return f"circle of radius {r}" }
        case _ { return "some shape" }
    }
}
```

Collections are invariant: an existing `list[Square]` cannot be assigned to
a `list[Shape]` (the alias could then receive a `Circle`); build the
`list[Shape]` from the elements instead.

### Inherited constructors

A class without its own `init` inherits the `init` of its nearest ancestor
that has one, like Python: the same parameters, defaults and named
arguments. With several levels the nearest one wins; with several bases the
first base (depth-first, left to right) whose chain declares an `init`. The
subclass's class-body field defaults still apply:

```lammergeier
class Animal {
    legs: int = 4
    func init(self, name: str, age: int = 1) {
        self.name: str = name
        self.age: int = age
    }
}

class Dog(Animal) {
    func sound(self) -> str { return "woof" }
}

class Puppy(Dog) {
    tricks: int = 0
}

d: Dog = Dog("rex", 3)
p: Puppy = Puppy(name="bolt")      # age defaults to 1, tricks to 0
```

Arguments are checked against the inherited signature
(``too many positional arguments in call to `Dog`: expected at most 2, got
3``, ``missing required argument `name` in call to `Dog` ``, unknown
keywords, argument types). A class that declares its own `init` only uses
that one. A class-body default for a field the parent declares
(`sides: int = 4` in a subclass of a class with `sides: int = 0`) also
survives an explicit `base.init(...)`, unless the parent's `init` assigns
the field itself.

Printing an instance of a class without `__str__` shows its fields, the
parent part as a nested `{...}`: `print(Square(3))` with a `Shape` parent
holding `name = "sq"` prints `&{{sq} 3}`. A base-typed value prints its actual
object, and `__str__` (own or inherited) still wins.

For multiple inheritance, name the parent aliases when you need explicit parent
dispatch:

```lammergeier
class Account {
    func init(self, owner: str, tenant: str) {
        self.owner: str = owner
        self.tenant: str = tenant
    }
}

class Flags {
    func init(self, enabled: bool) {
        self.enabled: bool = enabled
    }
}

class ServiceAccount(account: Account, flags: Flags) {
    func init(self, owner: str) {
        account.init(owner, "service")
        flags.init(true)
    }
}
```

If more than one parent provides the same inherited member, unqualified access
such as `self.label()` or `obj.label()` is ambiguous. Override the member on
the child or call through a named parent alias such as `left.label()`.
`base` is only available for the single unnamed-parent case; in classes with
multiple parents, use explicit aliases.

---

## Operator Overloading

Define arithmetic and comparison operators for classes using dunder methods:

```lammergeier
class Vec {
    func init(self, x: int, y: int) {
        self.x: int = x
        self.y: int = y
    }

    func __add__(self, other: Vec) -> Vec {
        return Vec(self.x + other.x, self.y + other.y)
    }

    func __eq__(self, other: Vec) -> bool {
        return self.x == other.x and self.y == other.y
    }
}
```

Supported operators: `__add__`, `__sub__`, `__mul__`, `__truediv__`, `__floordiv__`, `__mod__`, `__pow__`, `__eq__`, `__ne__`, `__lt__`, `__le__`, `__gt__`, `__ge__`, `__and__`, `__or__`, `__xor__`, `__contains__`, `__getitem__`, `__setitem__`, `__inc__`, `__dec__`.

Operator methods are instance methods. Binary/comparison methods take exactly
one operand after `self`; `__neg__`, `__invert__`, `__inc__`, and `__dec__`
take none; `__setitem__` takes key and value. Comparison and containment
methods return `bool`. The semantic checker uses those signatures at operator
sites, so `vec + 1` is rejected when `__add__` expects another `Vec`, while the
declared return type flows into assignments and chained expressions. Unknown
or `any` operands retain the dynamic fallback.

### Postfix `++` / `--`

Plain integers use the Go-native postfix operator — `i++` emits
`i++` in Go directly. Classes may opt in to the shape by defining
`__inc__` / `__dec__`; the methods must return the new value because
the transpiler rewrites `c++` to `c = c.Inc()`:

```lammergeier
class Counter {
    func init(self, n: int) { self.n: int = n }
    func __inc__(self) -> Counter { return Counter(self.n + 1) }
    func __dec__(self) -> Counter { return Counter(self.n - 1) }
}

func main() {
    c: Counter = Counter(0)
    c++       # -> c = c.Inc()
    c++
    c--
}
```

`++` / `--` are *statements*, never expressions — you can't embed
them in a larger expression (matching Go's rules).

---

## Static Members

Use the `static` keyword for package-level functions and shared
class-level variables associated with a class:

```lammergeier
class MathHelper {
    static calls: int = 0
    private static prefix: str = "math"

    static func square(x: int) -> int {
        MathHelper.calls += 1
        return x * x
    }

    static func label() -> str {
        return MathHelper.prefix + "-" + str(MathHelper.calls)
    }
}

func main() {
    print(MathHelper.square(5))  # 25
    print(MathHelper.calls)      # 1
    print(MathHelper.label())    # math-1
}
```

Static variables are accessed as `ClassName.field`. Public static
variables can be read and assigned from normal code; `private static`
variables use the same spelling inside Lam code but emit as unexported
Go package variables. A static variable without an initializer uses the
Go zero value for its type, which makes singleton holders straightforward:

```lammergeier
class Config {
    private static instance: Config

    static func get() -> Config {
        if Config.instance == None {
            Config.instance = Config()
        }
        return Config.instance
    }
}
```

---

## Interfaces

Define interfaces with required method signatures:

```lammergeier
interface Drawable {
    func draw(self) -> str
}

class Circle {
    func init(self, r: int) {
        self.r: int = r
    }

    func draw(self) -> str {
        return f"Circle({self.r})"
    }
}
```

Conformance is structural: a class with the required method set can be passed to
an interface-typed parameter without an explicit declaration. Interface methods
may spell the receiver (`func draw(self) -> str`) or omit it (`func draw() ->
str`); either way `self` is not counted as a parameter. An interface may
be empty (`interface Marker { }`); every value conforms to it. Imported
interfaces work the same way. The importing module carries the interface's
method metadata across the package boundary, so callers may import only the
concrete class and a function whose signature uses that interface. Interfaces
may also declare parameter- and return-type overload sets; implementations must
provide every signature, and contextual return dispatch works through an
interface receiver.

---

## Private Members

Use the `private` keyword for unexported (Go lowercase) names:

```lammergeier
private func helper(x: int) -> int {
    return x * 2
}

class MyClass {
    private func internal(self) -> str {
        return "internal"
    }
}
```

---

## Async / Await

Async functions run as goroutines and return one-slot buffered handles. The
same lowering applies to top-level functions, instance methods (including
methods on generic receivers), and static or private class methods:

```lammergeier
class Loader[T] {
    func init(self, fallback: T) {
        self.fallback: T = fallback
    }

    async func fetch(self, value: T) -> T {
        return value
    }

    static async func doubled(value: int) -> int {
        return value * 2
    }
}

func main() {
    loader: Loader[str] = Loader[str]("missing")
    pending = loader.fetch("ready")  # starts immediately
    doubled: int = await Loader.doubled(21)
    result: str = await pending       # awaiting later is supported
    print(f"{result}: {doubled}")
}
```

Calling an async callable starts its goroutine immediately; `await` may be used
at the call site or later on the returned handle. The declared payload type is
preserved, including `Result[T]`. Each invocation creates an independent handle
with capacity one, so a completed task can publish its result before its caller
is ready to await it. Async functions without a return type can be awaited too;
`await` then simply waits for the body to finish.

An exception raised inside an async function (directly or by anything it
calls) does not crash the program: it is captured and **re-raised at the
`await`** that collects the handle, so an ordinary `try`/`catch` around the
`await` handles it, typed clauses included:

```lammergeier
async func load(id: int) -> str {
    if id < 0 {
        raise ValueError(f"bad id {id}")
    }
    return f"item {id}"
}

func main() {
    pending = load(-1)
    try {
        print(await pending)
    } catch ValueError as e {
        print(f"failed: {e}")    # failed: ValueError: bad id -1
    }
}
```

An exception in a task that is never awaited is discarded with the task.

`lamconcurrency.Channel` and `lamactor.Mailbox` use the same operation names:
`send` / `receive` block, `asyncSend` / `asyncReceive` return awaitable handles,
and `offer` / `poll` decide immediately. All six operations preserve explicit
`Result` outcomes rather than masking closed, empty, or full states.

---

## Control Flow

### If / Elif / Else

```lammergeier
if x > 0 {
    print("positive")
} elif x == 0 {
    print("zero")
} else {
    print("negative")
}
```

Conditions of `if`, `elif` and `while` must be `bool`: Lam has no
truthiness, so `if xs { }` on a list is a compile error (write
`if len(xs) > 0`), as are numbers (`n != 0`), optionals (`opt != None`)
and `json` values (`if bool(payload.ok)`).

### Single-Statement Blocks

For concise one-liners, braces can be omitted:

```lammergeier
if x > 0 print("positive")
for i in range(3) print(i)
while running doWork()
if a > b print("a")
elif a == b print("equal")
else print("b")
```

These are automatically expanded to braced blocks by the preprocessor.

### For Loop

```lammergeier
for i in range(10) {
    print(i)
}

for item in items {
    print(item)
}

for i in range(0, 10, 2) {
    print(i)  # 0, 2, 4, 6, 8
}
```

A single target over a dict or set receives keys:

```lammergeier
counts: dict[str, int] = {"a": 1, "b": 2}
for key in counts {
    print(counts[key])
}

ids: set[int] = {5, 8, 13}
for id in ids {
    print(id)
}
```

Use a tuple target when you want both dict keys and values:

```lammergeier
for key, value in counts {
    print(key + "=" + str(value))
}
```

Over a list of tuples (or of nested lists), a tuple target unpacks each
element, like Python; `_` skips a position:

```lammergeier
pairs: list[tuple[str, int]] = [("a", 1), ("b", 2)]
for name, score in pairs {
    print(f"{name}: {score}")
}
```

### While Loop

```lammergeier
while x > 0 {
    x -= 1
}
```

---

## For-Else / While-Else

The `else` block runs only if the loop completes without `break`:

```lammergeier
# For-else: check if number is prime
for i in range(2, n) {
    if n % i == 0 {
        print("not prime")
        break
    }
} else {
    print("prime")
}

# While-else
while i < 10 {
    if items[i] == target {
        print("found")
        break
    }
    i += 1
} else {
    print("not found")
}
```

---

## Match / Case

Pattern matching on values, sequences, mappings and class instances:

```lammergeier
match command {
    case "start" {
        print("starting")
    }
    case "stop" {
        print("stopping")
    }
    case _ {
        print("unknown")
    }
}
```

A case may carry a guard: `case <pattern> if <condition>` matches only when
the pattern matches **and** the condition is true; otherwise the next case is
tried. A capture pattern is bound before its guard is evaluated:

```lammergeier
match n {
    case 0 { print("zero") }
    case x if x < 10 { print(f"small {x}") }
    case _ { print("large") }
}
```

Cases are tried top to bottom and the first match runs; the subject is
evaluated once. Supported patterns:

| Pattern | Example | Matches |
|---------|---------|---------|
| literal | `case 0`, `case -2`, `case 1.5`, `case "a"`, `case 'a'`, `case True`, `case None` | an equal value (single-quoted strings are strings; numbers compare numerically on `any`/`json`, so `case 2` matches `2.0`) |
| wildcard | `case _` | anything, binds nothing |
| capture | `case other` | anything, binds it to `other` |
| value | `case Color.RED`, `case config.LIMIT` | equality with a dotted name (a static field, module constant, …); a bare name is always a capture |
| or | `case 1 \| 2 \| 3`, `case [x, 0] \| [0, x]` | any alternative; every alternative must bind the same names |
| `as` | `case "b" as v`, `case [x, y] as pair`, `case (1 \| 2) as small` | the inner pattern, binding the matched value too |
| sequence | `case []`, `case [x, y]`, `case (a, b)`, `case [first, *rest]`, `case [*init, last]` | a list or tuple (also inside `any` / `json`) of that length; `*name` (at most one) collects the remaining elements into a list, `*_` ignores them. Strings are not sequences |
| mapping | `case {"kind": k}`, `case {"op": "add", **rest}` | a dict (or `json` object / string-keyed dict in `any`) containing the keys; extra keys are allowed and `**rest` collects them |
| class | `case Point(x=0, y=y)`, `case Point(px, py)`, `case Circle()` | an instance of the class or of a subclass (declared in any module); on a base-typed subject a subclass pattern downcasts, binding the subclass view; keyword sub-patterns match fields, positional ones match fields in declaration order (class-body fields, then the `self.x` assignments of `init`) |
| interface | `case Drawable() as d` | a value implementing the interface |
| builtin type | `case int(n)`, `case str(s)`, `case float(f)`, `case bool(b)`, `case list(xs)`, `case dict(d)`, `case json(j)` | a value of that type; the optional sub-pattern sees the narrowed value (`int(n)` binds an `int` even when the subject is `any` or `json`) |

Patterns nest freely and every kind combines with a guard. Names bound by a
pattern are visible in its guard and its body only (not in other cases, not
after the `match`) and take the type of what they matched: elements of a
`list[int]` are `int`, a `*rest` of a `list[str]` is a `list[str]`, fields
take their declared types, sub-values of `json` stay `json`, and parts of an
`any` value are `any` (narrow them with `int(n)`, `str(s)`, … or a class
pattern). A class pattern narrows an `as` name to the class
(`case Point() as p` gives a `Point`).

```lammergeier
class Point {
    func init(self, x: int, y: int) {
        self.x: int = x
        self.y: int = y
    }
}

func describe(value: any) -> str {
    match value {
        case 0 | None { return "nothing" }
        case Point(x=0, y=0) { return "origin" }
        case Point(x, y) if x == y { return f"diagonal {x}" }
        case [int(a), int(b)] { return f"pair sum {a + b}" }
        case [first, *rest] { return f"list starting {first}, {len(rest)} more" }
        case {"kind": str(kind), **rest} { return f"kind={kind} with {len(rest)} fields" }
        case str(s) as text { return f"text of {len(s)}: {text}" }
        case other { return "other" }
    }
}

func command(payload: json) -> str {
    match payload {
        case {"op": "add", "items": [first, *more]} { return f"add {first} and {len(more)} more" }
        case {"op": "delete", "id": int(id)} if id > 0 { return f"delete {id}" }
        case _ { return "unknown" }
    }
}
```

`case _` and an unguarded capture (`case other`) match everything: a
`match` ending with one returns on every path when all its cases do, and
later cases are reported as unreachable. `break` and `continue` inside a
case body act on the enclosing loop. The semantic checker rejects patterns
that can never match the subject's static type (`case "a"` on an `int`, a
sequence pattern on a `str`, a class pattern on an unrelated class), a name
bound twice in one pattern, or-pattern alternatives binding different
names, more than one `*name`, unknown classes or fields, non-string mapping
keys on `any` / `json` subjects, and class patterns on generic classes
unless the subject is typed `Box[...]`.

---

## List Comprehensions

Create lists with inline expressions and optional filters:

```lammergeier
# Basic
squares: list[int] = [x * x for x in range(10)]

# With filter
evens: list[int] = [x for x in range(20) if x % 2 == 0]

# Tuple and dictionary iteration expose element/key-value target shapes.
labels = [label for number, label in pairs]
entries = [key for key, value in table]
```

The semantic checker validates comprehension target arity and propagates
iterable element types into the result/filter expressions. A scalar iterable
cannot bind `a, b`; a `tuple[A, B]` element cannot bind three names. Tuple
iteration targets lower consistently in list, set, and dictionary comprehensions.

---

## Multidimensional Comprehensions

Multiple `for` clauses for cartesian products:

```lammergeier
pairs: list[int] = [x * y for x in range(3) for y in range(3)]

# With filter
filtered: list[int] = [x + y for x in range(5) for y in range(5) if x != y]
```

---

## Dictionary Comprehensions

Create dictionaries inline:

```lammergeier
squares: dict[int, int] = {i: i * i for i in range(6)}
even_sq: dict[int, int] = {i: i * i for i in range(10) if i % 2 == 0}
```

---

## Set Literals & Comprehensions

A `{ ... }` literal whose entries are bare values (not `key: value`
pairs) builds a set. Sets compile to Go's `map[K]bool` form, so
they are unordered, deduplicate on insertion, and iterate cheaply:

```lammergeier
flags = {"new", "active", "active", "stale"}
print(len(flags))               # 3 — duplicates collapsed
for f in flags { print(f) }     # iteration order unspecified

# Set comprehension — same braces, but with a `for` clause.
unique_squares = {x * x for x in range(-2, 3)}   # {0, 1, 4}
print(len(unique_squares))      # 3
```

An unannotated `{}` is an empty dict. Where a `set[T]` is expected — an
annotated variable or field (`flags: set[str] = {}`), a reassignment of a
`set[T]` variable, a `-> set[T]` return or a `set[T]` parameter — `{}` is
an empty set. A `{key: value}` literal with entries is always a dict.

Set literals and comprehensions also use surrounding type context. A direct
call such as `takeIds({1, 2})`, where `takeIds` expects `set[int]`, lowers to
Go's `map[int]bool` representation rather than the untyped fallback
`map[interface{}]bool`; annotated declarations, reassignments, returns, and lambda
returns behave the same way.

Membership uses the set key type, so `x in someset` and `x not in someset`
compile to a Go map-key presence check. Sets are not indexable from Lam code;
the `map[K]bool` representation is a transpilation detail. The same `in`
syntax also works for lists, dict keys, and strings.

Sets have the usual operations. `set()` is an empty set (typed by the
annotation or parameter it initialises; `set(xs)` builds one from a list or
another set); the operators return a new set and leave their operands
unchanged, their augmented forms update the variable:

```lammergeier
tags: set[str] = set()
tags.add("go")              # insert (no-op when present)
tags.add("lam")
tags.discard("rust")        # remove when present
tags.remove("go")           # remove; raises KeyError when missing
print(tags.contains("lam"), len(tags))   # true 1

a: set[int] = {1, 2, 3}
b: set[int] = {3, 4}
print(len(a | b), len(a & b), len(a - b))  # union 4, intersection 1, difference 2
a |= b                      # also &=, -=
```

Both operands of `|`, `&` and `-` must be sets of the same element type
(``operator `|` is not defined for `set[int]` and `set[str]` ``), and the
argument of `add` / `remove` / `discard` / `contains` must have the element
type.

---

## Tuple / Generator Comprehensions

Wrapping a comprehension in `(...)` produces an iterable that
behaves like a list comprehension, except the parentheses signal
"intended for one-shot iteration" rather than "intended to be
indexed". The transpiler lowers it to a Go slice, so it composes
cleanly with the functional helpers introduced later:

```lammergeier
for n in (x * x for x in range(5)) {
    print(n)                    # 0, 1, 4, 9, 16
}
```

The `[...]` form remains the right choice when you want to index
or slice the result; `(...)` signals that consumers will iterate
once.

> **Allocation note.** `(...)` is **not** a lazy generator — it
> lowers to the same "build a `[]T`, then iterate it once" IIFE
> as `[...]`. A `(x for x in range(10_000_000))` will allocate
> 10 million entries before the loop body runs. For truly lazy
> iteration (stream one element at a time, short-circuit via
> `take` / `takeWhile`) reach for the [`lamiter`](#/docs/stdlib?h=lamiter--itert)
> stdlib module or a hand-rolled `yield`-generator function
> ([see Generators](#generators--yield)).

---

## Dictionaries with Non-String Keys

Dictionaries support integer, boolean, and variable keys:

```lammergeier
scores: dict[int, int] = {1: 10, 2: 20, 3: 30}
flags: dict[bool, str] = {true: "yes", false: "no"}
```

---

## Enumerate

Iterate with index and value:

```lammergeier
fruits: list[str] = ["apple", "banana", "cherry"]
for i, fruit in enumerate(fruits) {
    print(f"{i}: {fruit}")
}
labels: list[str] = [f"{i}:{f}" for i, f in enumerate(fruits)]
```

`enumerate` works in `for` loops and comprehensions over lists (also list
literals, whose elements keep their type) and strings (one-character `str`
values with their character position). Iterating a `str` directly
(`for ch in text`) also yields one-character strs. Comprehensions iterate
generators too (`[x for x in gen()]`).

`sorted(xs)` returns a sorted copy (stable); `sorted(xs, key=fn)` orders by
`fn(x)` (a lambda or function reference) and `reverse=true` sorts in
descending order:

```lammergeier
words: list[str] = ["ccc", "a", "bb"]
byLength: list[str] = sorted(words, key=lambda w: len(w))   # [a bb ccc]
desc: list[str] = sorted(words, reverse=true)               # [ccc bb a]
```

---

## F-Strings

Formatted string literals embed any Lam expression inside `{...}`.
The interpolation slot is fed back through the regular expression
parser, so chained method calls, indexing, safe navigation, null
coalescing, and arithmetic all compose:

```lammergeier
name: str = "Alice"
age: int = 30
print(f"Name: {name}, Age: {age}")
print(f"Sum: {3 + 4}")
print(f"Pi: {3.14159:.2f}")            # Go fmt verb after the colon
print(f"{name.toUpper()}")

# Composite expressions
items: list[int] = [10, 20, 30]
print(f"second={items[1]}")             # 20
print(f"hyp²={3*3 + 4*4}")              # 25

# Safe nav + null-coalesce
print(f"email={user?.profile?.email ?? 'none'}")
```

The `:fmt` suffix maps directly to a Go `fmt` verb — `{x:.2f}`
becomes `%.2f`, `{n:5d}` becomes `%5d`, etc. Without a suffix the
slot uses `%v` so any value type works.

> **Quoting.** When a slot needs an inner string literal, use single
> quotes inside double-quoted f-strings (or vice versa) —
> ``f"x={a ?? 'fallback'}"`` — or escape the delimiter
> (``f"v={d[\"k\"]}"``). An unescaped delimiter quote inside a slot
> (``f"{d["k"]}"``) ends the string. Names read in a slot are checked like
> any other name (an undefined one is reported at its line and column).

---

## Multi-line Expressions

Function calls, collection literals, and parenthesised expressions can
be broken across as many lines as you like. A `(`, `[`, or `{` that is
clearly opening a literal (after `=`, `,`, `(`, `[`, `:`, or an
operator) suspends automatic statement termination until its matching
closer, and trailing commas are tolerated:

```lammergeier
total: int = add(
    1,
    2,
    3,
)

d: dict[str, int] = {
    "a": 1,
    "b": 2,
}

big: int = (1
            + 2
            + 3
            + 4)
```

No special continuation character is required — newlines inside a
balanced `(...)`, `[...]`, or literal `{...}` are treated as whitespace.

---

## Defer Statements

`defer expr` mirrors Go: the expression is scheduled to run when the
enclosing function returns, and multiple `defer`s run in LIFO order.

```lammergeier
from lamerrors import Result
from lamos import File

func load_config(path: str) -> Result[str] {
    f: File = File.open(path, "r")?
    defer f.close()
    return f.read()
}
```

Bare values are wrapped in an anonymous closure; function calls are
emitted as Go's native `defer call()`.

---

## isinstance

`isinstance(obj, T)` is lowered to a real Go runtime check — not a
placeholder. The compiler picks the right mechanism per target type:

- primitive types (`int`, `float`, `str`, `bool`, `bytes`, …) use a
  two-value type assertion `_, ok := x.(T)`;
- `None` becomes `x == nil`;
- container families (`list`, `dict`, `set`, `tuple`) use
  `reflect.ValueOf(x).Kind()` so they accept any slice/map variant;
- user classes become `_, ok := x.(*MyClass)` because classes compile
  to pointer-to-struct values;
- interfaces become `_, ok := x.(MyInterface)`;
- a tuple of types — `isinstance(x, (int, str, MyClass))` — OR-combines
  the individual tests.

```lammergeier
v: any = 42
print(isinstance(v, int))              # true
print(isinstance(v, (int, str)))       # true
print(isinstance([1, 2, 3], list))     # true
print(isinstance(None, None))          # true
```

---

## String Repetition

`*` repeats a string (`str * int` or `int * str`); a negative count gives
`""`. `s *= n` repeats in place. Other operand types are a semantic error.

```lammergeier
rule: str = "-" * 40
title: str = 3 * "ab"        # "ababab"
s: str = "ha"
s *= 2                       # "haha"
```

F-string interpolations may contain the escaped delimiter quote:
`f"{table[\"key\"]}"`.

## Multiline Strings

Triple-quoted strings (compiled to Go backtick strings):

```lammergeier
msg: str = """Hello
World
Multiple lines"""
print(msg)
```

---

## Primitive-Type Methods

Built-in method calls on strings and lists (e.g.
`"hello".toUpper()`, `xs.append(v)`, `xs.map(fn)`) don't come
from a class declaration —
Go's `string` and `[]T` types don't have methods of their own.
Instead the transpiler pattern-matches the `.<method>()` call site
at compile time:

- **String methods** lower to a call into the `lamstrings`
  standard library — `"hi".toUpper()` becomes
  `Strings_toUpper("hi")`, `s.split(",")` becomes
  `Strings_split(s, ",")`, and so on. The dispatch *names match
  the lamstrings static-method names exactly* (`toUpper`, `trim`,
  `startsWith`, `index`, …) — there's no Python-style alias
  layer, so the language reads the same way whether you call
  `"hi".toUpper()` or `Strings.toUpper("hi")`. The Go-side
  behaviour lives in `lib/lamstrings.lam`, one source of truth
  for every dispatched method, so overriding or extending a
  built-in is just an edit in that file. The
  compiler driver auto-injects `from lamstrings import Strings`
  into the import worklist whenever any of the dispatched names
  appears in your source — direct or transitive — so callers
  never need to write the import explicitly.
- **List methods** (`length` / `append` / `map` / `filter` /
  `any` / `all` / `foreach` / `reduce` / `sort` / …) lower to
  either a Go primitive (`len(xs)`, `append(xs, 4)`) or an inline
  IIFE that builds the result. Lists don't go through a stdlib
  library — there's no `Lists` class to host them — but the dispatch
  convention is the same: the transpiler picks the lowering only when
  the receiver is genuinely a `list[T]`.

The rewrite fires only when the receiver is known to be of the
right primitive type (either because its declared annotation is
`str` / `list[T]`, or because we inferred the type from the
expression shape). If the receiver is an instance of a user class
that happens to define a method with the same name — say a
`Counter` with its own `.count()` — the built-in rewrite steps out
of the way and the user method wins.

```lammergeier
# Strings — lower to lamstrings.Strings static methods.
s: str = "  hello, WORLD  "
print(s.toUpper())                 # Strings_toUpper(s)
print(s.split(","))                # Strings_split(s, ",")
print("hi".startsWith("h"))        # Strings_startsWith("hi", "h")
print("banana".index("ana"))       # Strings_index("banana", "ana")
print("name=%s age=%d".format("alice", 42))
                                   # Strings_format("name=…", "alice", 42)

# Lists.
xs: list[int] = [1, 2, 3]
xs.append(4)                       # xs = append(xs, 4)
print(xs.length())                 # len(xs)
doubled: list[int] = xs.map(lambda x: x * 2)
                                   # IIFE that builds a new []int
print(xs.any(lambda x: x > 2))     # IIFE with early return -> bool
sorted: list[int] = xs.sort()      # sorted copy; xs is unchanged
xs = xs.sort(inplace=true)         # sort in-place and return xs
desc: list[int] = xs.sort(compare=lambda a, b: a > b)
byLen: list[str] = words.sort(key=lambda w: len(w))
                                   # stable sort by a key (also positional:
                                   # a one-parameter callback is a key)
```

The full set of dispatched string methods is `toUpper`,
`toLower`, `trim`, `length`, `trimLeft`, `trimRight`, `replace`, `split`,
`join`, `startsWith`, `endsWith`, `index`, `lastIndex`, `count`, `contains`,
`hasPrefix`, `hasSuffix`, `repeat`, `title`, `equalFold`, `fields`,
`capitalize`, `isAlpha`, `isDigit`, `isAlnum`, `isSpace`, `reverse`,
`center`, `zfill`, `padLeft`, `padRight`, `splitLines`, `splitN`,
`replaceFirst`, `containsAny`, `isEmpty`, `isBlank`, `indent`, `dedent`,
`format` — the same identifiers `lamstrings.Strings` exposes as static
methods. The list methods are `length`, `append`, `pop` (`pop()` removes the
last item, `pop(i)` the item at `i`, negative from the end), `extend`,
`insert`, `sort`, `map`, `filter`, `reduce`, `any`, `all` and `foreach`.
Calling any other method on a `str` or `list[T]` is a compile error with a
hint for the usual Python spelling (`s.find(x)` → `s.index(x)`,
`s.rfind(x)` → `s.lastIndex(x)`, both returning -1 when absent;
`s.upper()` → `s.toUpper()`; `xs.index(x)` / `xs.contains(x)` → `x in xs`
or a loop). See [TRANSPILATION.md](#/docs/transpilation)
for the emission details, including which method names require
at least one argument (`replace` / `split` / `index` / `count` /
`contains` / `format` — a zero-arg call like `qb.count()` falls
through to user-method dispatch so the built-in doesn't silently
emit `Strings_count(o, " ")`).

---

## Comments

Single-line comments with `#`:

```lammergeier
# This is a comment
x: int = 42  # inline comment
```

Multiline documentation comments use the exact delimiters `#-` and `-#`:

```lammergeier
#-
This is a multiline comment.
It can span multiple lines.
-#
```

Whitespace is not allowed inside the closing delimiter: forms such as `-  #`
are syntax errors. `lamc fmt` can repair a spaced closing delimiter to `-#`.

---

## File Management

`File.open(path, mode="r")` returns `Result[File]`. The opened `lamos.File`
owns the underlying handle; `read`, `write`, `readline`, `readlines`, and
`close` also use unsuffixed `Result` contracts. Modes `"r"`, `"w"`, and `"a"`
map to read, truncate/write, and append/create respectively.

```lammergeier
from lamos import File

# Write to file
f: File = File.open("output.txt", "w").unwrap()
f.write("hello world").unwrap()
f.close().unwrap()

# Read from file
reader: File = File.open("input.txt").unwrap()
content: str = reader.read().unwrap()
reader.close().unwrap()

# With statement (auto-close) with an as binding
managed: File = File.open("data.txt").unwrap()
with managed as f {
    content: str = f.read().unwrap()
    print(content)
}

# Line-oriented helpers
lineFile: File = File.open("data.txt").unwrap()
with lineFile as f {
    first: str = f.readline().unwrap()
    lines: list[str] = f.readlines().unwrap()
}
```

---

## Try / Catch / Finally

Exception handling (compiled to Go panic/recover). Uses `catch` (not `except`):

```lammergeier
divisor: int = 0
try {
    result: int = 10 / divisor
} catch e {
    print(f"Error: {e}")      # Error: ZeroDivisionError: ...
} finally {
    print("cleanup")
}
```

Dividing by the constant `0` (`10 / 0`, `n % 0`) is a compile-time error.

Inside the `try` block (or anywhere else in your code) you can raise
an exception explicitly. Both `raise` and `throw` are accepted —
`throw` is purely an alias for users coming from Java/JS/C#:

```lammergeier
func validate(n: int) {
    if n < 0 {
        throw ValueError("n must be non-negative")
    }
}
```

A bare `raise` (or bare `throw`) inside a `catch` re-raises the
currently-active exception, identical to Python.

### Typed catch clauses

A clause can name the exception type it handles. Clauses are tried in
order and **only the first matching clause runs**; `catch e` (or a bare
`catch`) matches anything. An exception that no clause matches keeps
propagating to the enclosing `try` (the `finally` block still runs first):

```lammergeier
try {
    lookup(key)
} catch KeyError as e {
    print(f"missing: {e}")
} catch ValueError as e {
    print(f"bad value: {e}")
} catch e {
    print(f"something else: {e}")
}
```

Matching rules:

- Builtin names follow Python's hierarchy: `Exception` catches everything,
  `LookupError` catches `KeyError` and `IndexError`, `ArithmeticError`
  catches `ZeroDivisionError` and `OverflowError`, `OSError` / `IOError`
  catch `FileNotFoundError`, `RuntimeError` catches `NotImplementedError`,
  and `ValueError` also catches the `ParseError` raised by failed
  `int(...)` / `float(...)` conversions.
- Go runtime faults are classified too: integer division by zero is a
  `ZeroDivisionError`, an out-of-range index or slice is an `IndexError`,
  a nil dereference is an `AttributeError` and a failed type assertion is
  a `TypeError`.
- A class name matches instances of that class and of its subclasses
  (declared in any module); an interface name matches any value
  implementing it.

The binding (`as e`) is the raised value itself, as described below.

### Rich exception objects — throw any class instance

`throw` accepts any value, not just strings. A class instance
carries its own fields through `panic`/`recover` intact, so a
handler can either print it via the class's `__str__` or rebind
it to a typed local to reach the metadata directly:

```lammergeier
class ValidationError {
    func init(self, field: str, reason: str) {
        self.field: str = field
        self.reason: str = reason
    }
    func __str__(self) -> str {
        return f"{self.field}: {self.reason}"
    }
}

func parseBody(raw: str) {
    throw ValidationError("email", "missing @")
}

func main() {
    try {
        parseBody("{}")
    } catch err {
        # Default stringer — ``__str__`` fires transparently.
        print(err)                   # email: missing @

        # Rebind to the concrete class to reach fields.
        ve: ValidationError = err
        print(ve.field)              # email
        print(ve.reason)             # missing @
    }
}
```

Under the hood a catch-all binding (`catch err`) is the live panic
value (Go `interface{}`). Field access requires naming the class — Lam
inserts the type assertion for you when you write
`ve: ValidationError = err`. The old eager-stringification bind
would have left `err` as a plain `str`, hiding the fields; the
current binding preserves them.

A typed clause binds the class directly: in `catch ValidationError as ve`
the name `ve` is a `ValidationError`, so `ve.field` works without the
rebinding. A clause naming a base class also catches its subclasses, and
`ve` is then the base-typed view of the subclass instance (methods run the
subclass overrides).

### Exceptions vs. `Result` errors — two complementary models

Lam gives you **two** orthogonal tools for the "this operation
might fail" problem. Choose based on how the failure should flow
through your code:

| | **Exceptions** (`raise` / `try`/`catch`) | **`Result` errors** (`Result.Ok` / `Result.Err` / `?`) |
|---|---|---|
| How the failure travels | **Propagates** up the call stack until a matching `catch` handles it, or the program aborts. Intermediate frames need to know nothing about it. | **Must be handled immediately** at every hop — either by propagating with `?`, by pattern-matching, or by converting to a default. A dropped `Result` is a warning. |
| When to reach for it | Truly exceptional conditions: programmer bugs, unrecoverable I/O failures, "this should never happen" invariants. | Expected failure modes you want to force callers to acknowledge: validation errors, missing keys, parse failures, network timeouts. |
| Runtime cost | A panic + `recover` (Go's native mechanism — free on the happy path, but an unwind on the failure path). | A plain tagged struct. No stack unwinding. The ``?`` operator lowers to an `if` check. |
| Interop with Go | Raised values become Go `panic` values; caught by the IIFE wrapping each `try`. | Plain Go structs, usable directly from `go!` blocks. |

The two can be mixed: a function can return a `Result[T]` *and*
raise exceptions for programming-bug class failures. Callers
handle `Result` explicitly and are protected from the panic case
by an outer `try`.

Because exceptions unwind through any number of intermediate
frames without those frames needing to participate, you can catch
an error raised three calls deep with a single `try` at the
top-level:

```lammergeier
func _inner() {
    raise "something broke"
}

func _middle() {
    _inner()               # middle neither catches nor re-throws
}

func main() {
    try {
        _middle()
    } catch e {
        print(f"caught at top: {e}")   # caught at top: something broke
    }
}
```

`Result` errors do *not* auto-propagate: the intermediate
function would have to either call `?` on the inner result or
manually inspect it. That difference is why `Result` is the tool
of choice for flows where every caller should be *forced* to
acknowledge the failure explicitly.

### Recovery is *block*-scoped — execution continues after the catch

When a catch clause successfully handles a raised exception, control
flow resumes with the **next statement after the `try`/`catch`** — just
like Python, JS, Java, etc. There is no hidden function-level skip:

```lammergeier
func describe(n: int) -> str {
    label: str = "unknown"
    try {
        if n < 0 {
            raise ValueError("negative")
        }
        label = "positive"
    } catch ValueError as e {
        label = "fixed"
    }
    # Always runs, regardless of whether the try raised.
    return label + "!"
}

print(describe(-1))   # fixed!
print(describe(7))    # positive!
```

Internally the `try`-with-`catch` block is wrapped in an immediately-
invoked Go closure with a `defer recover()` of its own, so recovery is
local to the block. A `return` inside the `try`/`catch` still
propagates out of the enclosing function — only execution that *would
have fallen off the end of the catch* continues with the post-block
statements.

> **Exceptions vs. errors.** `raise`/`throw` are for *exceptional*
> situations that should unwind the call stack (programmer mistakes,
> impossible states, OS-level failures). For *expected* failure modes
> that the caller is meant to handle inline, prefer the `Result`-based
> error system covered next — see [Errors & Results](#errors--results).

---

## Errors & Results

Lam ships a structured error-handling system in the standard library
that complements `try`/`catch`. Where exceptions unwind the stack on
unexpected failures, `Result`-based errors are *values* the caller
inspects, with built-in syntax for ergonomic propagation.

The system has three pieces:

- The `Result` and `Error` classes from the `lamerrors` module.
- The `?` operator that short-circuits a function on a `Result.Err`.
- The `do { } catch err { }` block that catches propagated errors
  locally instead of bubbling them to the enclosing function.

### `Result` and `Error`

Import them from `lamerrors`:

```lammergeier
from lamerrors import Result, Error

func parseInt(s: str) -> Result[int] {
    if s == "1" {
        return Result.Ok(1)
    }
    return Result.Err(Error("ParseError", f"cannot parse: {s}"))
}
```

Construction goes through the static factories so the
`value`-or-`error` invariant is explicit at every call site:

| Form                              | Result                                                  |
|-----------------------------------|---------------------------------------------------------|
| `Result.Ok(v)`                    | success result; `.value = v`, `.error = None`           |
| `Result.Err(e)`                   | error result; `.value = None`, `.error = e`; `e` is `any` |
| `Error(kind, message)`            | structured error with a kind tag and human message      |
| `Error(kind, message, cause)`     | error chain — `cause` is the wrapped lower-level error  |

Inspection is by methods or fields, whichever reads better:

| Form              | Meaning                                                 |
|-------------------|---------------------------------------------------------|
| `r.ok()`          | `True` iff this is a success result                     |
| `r.value`         | the contained value (`any`)                             |
| `r.error`         | the error or `None` (`any`)                             |
| `r.unwrap()`      | the value, or panic with the error                      |
| `r.unwrapOr(x)`   | the value, or `x` if this is an error                   |

`Result` remains non-generic at runtime — `value` and `error` are
`any` — but signatures may carry one payload annotation such as
`Result[int]`. The compiler uses that annotation for semantic inference,
including unannotated assignments such as `document = loadDocument()?`,
and erases it to the same Go `*Result` representation as bare `Result`.
The generated unwrap includes the payload's Go assertion, so inferred
class receivers remain concrete rather than leaking `interface{}`. Public
payload-class metadata is loaded from the function's module without binding
that class in user scope, so importing `loadDocument` does not require a
second, otherwise-unused `Document` import.
Direct `.value` and `.error` reads remain `any`. `unwrap()` and
`unwrapOr(...)` keep the payload type wherever it is statically known — on a
local or parameter annotated `Result[T]`, or on a call to a function or method
declared `-> Result[T]` (overloads resolve to the called variant) — so inline
uses such as `if r.ok() and r.unwrap()`, `n.unwrap() + 1`,
`len(names().unwrap())` or `for x in load().unwrap()` type-check. When the
payload is unknown or `any`, the value stays dynamic (`any`), and an annotated
assignment, enclosing return, typed call argument, or typed collection element
still supplies context for the Go assertion. `Result.Ok(...)` likewise shapes
collection literals, comprehensions and ternaries from the declared payload, so
`return Result.Ok(["a", "b"])` in a `-> Result[list[str]]` function stores a
`list[str]`. The `?` operator likewise has
a typed assignment path (`n: int = parseInt(s)?`) and emits the matching Go
type assertion for the unwrapped value. `Result[T, E]` is rejected because errors remain
open `any` values rather than a second type parameter.

`Result.Err(e)` is equally broad: `.error` is also `any`. Use a
structured `Error` when callers need a kind/message/cause contract,
but a string, dict, sentinel object, or raw value is valid when that
fits the domain better.

```lammergeier
from lamerrors import Result, Error

func configError(name: str) -> Result {
    return Result.Err({"kind": "MissingConfig", "name": name})
}

func parseError(s: str) -> Result {
    return Result.Err(Error("ParseError", f"bad integer: {s}"))
}

func simpleError() -> Result {
    return Result.Err("permission denied")
}

func main() {
    r: Result = configError("DATABASE_URL")
    if not r.ok() {
        print(r.error)
    }
}
```

A payload annotation plus typed assignment gives `unwrap()` the concrete
result type without a raw-Go assertion:

```lammergeier
from lamerrors import Result

func main() {
    r: Result[int] = Result.Ok(42)
    n: int = r.unwrap()
    print(n)
}
```

### The `?` propagation operator

A postfix `?` after a `Result`-typed expression returns early from the
*enclosing function* if the result is an error, otherwise it unwraps
to the value. The two snippets below are equivalent:

```lammergeier
from lamerrors import Result

# With ?
func double(s: str) -> Result[int] {
    n: int = parseInt(s)?
    return Result.Ok(n * 2)
}

# What the compiler desugars it to (sketch)
func doubleDesugared(s: str) -> Result[int] {
    r: Result[int] = parseInt(s)
    if not r.ok() {
        return r
    }
    n: int = r.unwrap()
    return Result.Ok(n * 2)
}
```

The compiler injects the matching type assertion when the receiving
binding is annotated, so `n: int = parseInt(s)?` produces typed Go
without any manual value-casting shim. Multiple `?` in one function are
fine — each gets its own internal temporary.

`?` only accepts a known `Result` value and only propagates normally from
a function whose return type is bare `Result` or `Result[T]`. A known
`int`, `str`, `Option[T]`, or other non-`Result` operand is a Lam
`error[type]`; unknown and `any` values keep the dynamic escape hatch.
The semantic checker warns when `?` is used outside a `Result`-returning
function or `do/catch` body.

Because `?` lowers to `Result` helpers, the file must directly import the
helper class with `from lamerrors import Result`; otherwise the compiler
emits a Lam-source `error[import]` before Go runs. A scoped import before
the syntax is valid because imports are compile-time dependencies. A
transitive import from another module does not count, so refactoring that
module cannot silently remove syntax support. `Result as R` remains
unsupported for helper-backed syntax because emitted helpers use the
canonical name.

### The `do { } catch err { }` block

Sometimes you want `?` to short-circuit *locally* rather than back to
the caller. `do/catch` is the structured-error counterpart of `try/catch`:
inside the body, `?` returns from the **block**, and the bound
variable in `catch` receives whatever error was propagated:

```lammergeier
from lamerrors import Result

func main() {
    do {
        a: int = parseInt("1")?
        b: int = parseInt("2")?
        print(a + b)              # 3 — both succeeded
    } catch err {
        print(f"failed: {err}")   # only runs if a ? short-circuited
    }
}
```

`do/catch` emits a small `Result`-returning helper closure even when the
body does not contain `?`, so it also requires a direct
`from lamerrors import Result` at file or preceding scoped-import level.

Two consecutive `do` blocks are independent; locals declared inside
one block don't leak into the next. The catch variable is mandatory
and lives only inside the handler.

The catch variable is an `Error` (from `lamerrors`): `err.kind`,
`err.message` and `err.cause` work directly, and an `Error` subclass keeps
its class (`match err { case NotFound() as nf { ... } }`). A value that is
not an `Error` (`Result.Err("timeout")`, a number) is wrapped as an `Error`
with an empty `kind`, its text as `message` and the original value as
`cause`; it prints exactly as the value did.

A `return` inside the `do` body (or inside the handler) returns from the
enclosing function, exactly as it would outside the block:

```lammergeier
from lamerrors import Result

func firstValid(inputs: list[str]) -> int {
    for s in inputs {
        do {
            return parseInt(s)?    # leaves firstValid on success
        } catch err {
            print(f"skipping {s}")
        }
    }
    return -1
}
```

### When to choose what

| Situation                                              | Use                       |
|--------------------------------------------------------|---------------------------|
| Want to jump the stack or there's a handler somewhere above | `raise` / `throw`    |
| Want to manage the problem immiediately or force the caller to check | `Result`    |
| Programmer mistake / impossible state / OS panic       | `raise` / `throw`         |
| Recoverable failure the caller will handle             | return `Result`           |
| Threading several fallible calls together              | `?` in a Result-returning function |
| Containing several fallible calls *without* propagating | `do { ... } catch err { ... }` |

---

## With Statement

Context managers with auto-cleanup:

```lammergeier
from lamos import File

file: File = File.open("file.txt").unwrap()
with file as f {
    data: str = f.read().unwrap()
    print(data)
}
```

---

## Del Statement

Delete map entries:

```lammergeier
scores: dict[str, int] = {"alice": 100, "bob": 95}
del scores["bob"]
```

---

## Augmented Assignment

The compound-assignment operators rewrite `target op= value` into
`target = target op value` after type-checking the LHS, so they
work on every annotated location an ordinary assignment would.

```lammergeier
n: int = 0
n += 5            # 5
n -= 1            # 4
n *= 3            # 12
n //= 5           # 2  (integer division)

s: str = "ab"
s += "cd"         # "abcd"

flags: int = 0
flags |= 1        # bit set
flags &= ~2       # bit clear
flags ^= 4        # bit toggle
flags <<= 1
flags >>= 1
```

Supported operators: `+= -= *= /= //= %= **= &= |= ^= <<= >>=`.

---

## Assert Statement

`assert <cond>` panics with `"assertion failed"` if the condition
is falsy, and `assert <cond>, <msg>` panics with the supplied
message instead. Useful for invariants and quick test harnesses;
production code should prefer explicit `raise` so the error type
is part of the API:

```lammergeier
func sqrt(n: float) -> float {
    assert n >= 0.0, "sqrt: negative input"
    ...
}
```

Asserts are not stripped from release builds — they execute
unconditionally, like Go's own `panic`. If you need a removable
sanity check, gate it behind a `const debug: bool = ...` flag.

---

## Global Declarations

`global name` declares that an assignment inside a function should
target the module-level binding rather than create a new local.
The keyword serves primarily as a scope-resolution hint for the
semantic checker; the transpiler reaches the existing top-level
variable directly.

```lammergeier
counter: int = 0

func tick() {
    global counter
    counter += 1
}

func main() {
    tick(); tick(); tick()
    print(counter)         # 3
}
```

`nonlocal name` explicitly targets a binding in an enclosing function.
The compiler verifies that the binding exists, rejects ambiguous bindings
redeclared in multiple enclosing functions, and prevents writes to an outer
`const`. Go closures already capture by reference, so valid nonlocal writes
need no runtime wrapper.

---

## Top-Level Statements

Statements may appear at file top level, next to declarations. They run
once, at program start, **before `main()`**, in file order:

```lammergeier
names: list[str] = []

func register(name: str) {
    names.append(name)
}

register("core")              # runs before main()
print(f"{len(names)} plugin")
for extra in ["a", "b"] {
    register(extra)
}
total: int = len(names)       # declared after a statement: sees 3

func main() {
    print(total)              # 3
}
```

Rules:

- **Declarations** — `func`, `class`, `interface`, imports, `const`,
  `go!` blocks, annotated globals (`x: T = v`) and the first plain
  assignment of a new name (`count = 0`, an inferred global) — are
  module-level declarations as before. **Everything else** at top level
  (calls, `items.append(3)`, assignments to existing globals, `+=`, field
  and index assignment, `if` / `for` / `while` / `match` / `try` / `do`,
  `raise`, ...) is a top-level statement. A bare string literal (a module
  docstring) is ignored.
- A global declared **before** the file's first top-level statement keeps
  its initializer exactly as today: Go evaluates it during package
  initialization (in dependency order, so `a: int = b + 1` may use a `b`
  declared later), before any top-level statement runs. A global declared
  **after** a top-level statement is zero-valued until its declaration is
  reached: its initializer runs in file order with the statements, so it
  sees their effects. Reading a global in a top-level statement *before*
  its declaration is a compile error.
- Names first assigned inside a top-level block (`if c { mode = "a" }`,
  match cases, loop and `try` bodies, `x, y = 1, 2`) are module globals,
  visible to every function. Loop targets (`for i in ...`) stay local to
  the loop.
- **Imported modules** run their top-level statements when the module is
  initialized: modules initialize in dependency order — a module's imports
  (transitively, following imports in source order) before the module —
  each module exactly once, the main file last, all before `main()`.
- An exception raised by a top-level statement (and not caught there)
  aborts the program before `main()` starts. `return`, `break` and
  `continue` are not valid at top level.

---

## Nested Functions

A `func` may be declared inside another `func` body. Lam compiles
the inner declaration to a Go closure assigned to a local variable
of the inner function's name, so the inner function can read and
mutate the surrounding locals exactly like a `lambda`.

The two forms — nested `func` and `lambda` — compile to the same
Go closure. Capturing an active `for` target emits `warning[capture]` because
all closures may observe the loop variable's later value; copy it into a
per-iteration local when each closure needs a snapshot. Parameters that shadow
the loop target do not warn. The forms otherwise differ only in what the reader
sees:

- A **nested `func`** has a name. The name shows up in IDE
  outlines, `grep`, stack traces from a `throw` inside the
  helper, and code review — so it's easier to answer *"what does
  this helper do?"* at a glance. The full `func` shape also lets
  you attach typed parameters, a return type, and a docstring:

  ```lammergeier
  func settle(orders: list[Order]) -> int {
      # The helper gets a name (``total``) and an explicit
      # signature, so a reader scanning the outer function sees
      # both the call site and the definition without leaving
      # the scope.
      func total(subset: list[Order]) -> int {
          sum: int = 0
          for o in subset { sum += o.amount }
          return sum
      }
      pending: list[Order] = orders.filter(lambda o: not o.paid)
      return total(pending)
  }
  ```

- A **`lambda`** is anonymous and expression-shaped. Reach for it
  when the closure is a one-off passed directly into another
  call — naming it would only add noise:

  ```lammergeier
  # The lambda is never referred to again; inlining it keeps
  # the reader's eyes on the data flow, not on a helper name.
  doubled: list[int] = nums.map(lambda x: x * 2)
  ```

Rule of thumb: if the helper gets referenced more than once, or
needs a type signature or docstring, write it as a nested `func`;
otherwise use `lambda`.

```lammergeier
func adder(base: int) -> int {
    func add(x: int) -> int {
        return base + x        # closes over `base`
    }
    return add(5)
}

func tracePlugin(srv: Server) {
    func start(req: Request, res: Response) {
        req.ctx["traceId"] = randomTraceId()
    }
    func finish(req: Request, res: Response) {
        res.setHeader("X-Trace-Id", str(req.ctx["traceId"]))
    }
    srv.onRequest(start)
    srv.onResponse(finish)
}
```

A nested `func` is only visible inside the enclosing body; it is
not callable from anywhere else. To expose a helper widely, lift
it to module scope. Nested funcs may carry their own generic type
parameters (see "Functions, Defaults, Lambdas, and Nested Helpers")
and can be declared inside `if` / `else` blocks too.

---

## Slicing

Lam supports the full Python slicing surface — negative indices,
omitted bounds, and an optional stride — for both lists and
strings:

```lammergeier
xs: list[int] = [10, 20, 30, 40, 50]

# Plain bounds
print(xs[1:3])     # [20 30]
print(xs[:3])      # [10 20 30]
print(xs[2:])      # [30 40 50]
print(xs[:])       # full copy

# Negative indices count from the end
print(xs[-2:])     # [40 50]
print(xs[:-1])     # [10 20 30 40]
print(xs[-3:-1])   # [30 40]

# Stride (positive or negative)
print(xs[::2])     # [10 30 50]
print(xs[1::2])    # [20 40]
print(xs[::-1])    # [50 40 30 20 10]

# Strings work the same way and the result is still a string.
s: str = "hello world"
print(s[6:])       # world
print(s[-5:])      # world
print(s[::-1])     # dlrow olleh
```

A single negative index written as a constant (`xs[-1]`, `s[-2]`) counts
from the end like Python (`xs[len(xs) - 1]`) and also works as an assignment
target (`xs[-1] = 0`); an index still out of range raises `IndexError`. A
negative index held in a variable is not adjusted: `xs[i]` with `i = -1`
raises `IndexError` (use `xs[len(xs) + i]`).

Indexing a string with a single integer follows Go's native behavior and
returns the byte at that position. Use `string(s[i])` when you want a
one-character string, or `int(s[i])` when you want the code value.

The cheap `[a:b]` form (with non-negative bounds and no step) is
lowered to Go's native slice syntax. Negative indices or a stride
trigger a small runtime IIFE that uses `reflect` to support
arrays, slices, and strings uniformly. Strings round-trip back to
`string`; sliced lists come back as `list[any]` — annotate the
target if you need a tighter element type.

## Slice Assignment

Replace a slice of a list with new values:

```lammergeier
a: list[int] = [1, 2, 3, 4, 5]
a[1:3] = [10, 20]
# a is now [1, 10, 20, 4, 5]

b: list[int] = [1, 2, 3, 4, 5]
b[2:5] = [3]
# b is now [1, 2, 3]
```

Slice assignment uses the cheap (non-negative, no-step) form; for
Python-style negative-index removal, build the new slice
explicitly with the read-side `[a:b]` and assign it back.
When the list target has a known element type, anonymous replacement
literals are lowered to that type recursively, so nested replacements such as
`rows[0:1] = [[6, 8]]` preserve `list[list[int]]` instead of falling back to
`list[any]`.

---

## Generators / Yield

Generator functions using `yield` (compiled to goroutine + channel):

```lammergeier
func fibonacci(n: int) -> int {
    a: int = 0
    b: int = 1
    for i in range(n) {
        yield a
        a, b = b, a + b
    }
}

func main() {
    for val in fibonacci(10) {
        print(val)
    }
}
```

Generators may also be generic static methods, including methods imported from
another module. The compiler infers concrete element types for inline list
literals and ranges directly over the returned channel:

```lammergeier
from lamsort import Sort

for value in Sort.iterSorted([3, 1, 2]) {
    print(value)
}
```

`Sort.iterSorted` uses natural ordering; `Sort.iterSortedWith` accepts a typed
three-way comparator for arbitrary classes. Both copy the input and select each
next value incrementally.

---

## Functional Programming

Lists support `.map()`, `.filter()`, `.reduce()`, `.any()`, `.all()`, and `.foreach()`.

**All of these are eager.** Each call produces a fresh `[]T` slice
(or, for `.reduce` / `.any` / `.all` / `.foreach`, a scalar)
immediately — the callback runs once per element as part of the
method's own IIFE. `.any` / `.all` additionally short-circuit on
the first definitive result.

If you want pipeline-style laziness — one element flowing through
the chain at a time, `take` / `takeWhile` bailing out before the
rest of the input is touched — reach for the
[`lamiter` stdlib module](#/docs/stdlib?h=lamiter--itert) (`Iter.fromList(xs)
.map(f).filter(g).take(10).toList()`). That's the only place in
the language where the combinator chain is deferred to the
terminal call.

```lammergeier
func double(x: int) -> int {
    return x * 2
}

func is_even(x: int) -> bool {
    return x % 2 == 0
}

func add(a: int, b: int) -> int {
    return a + b
}

func main() {
    nums: list[int] = [1, 2, 3, 4, 5]

    mapped: list[int] = nums.map(double)       # [2 4 6 8 10]
    filtered: list[int] = nums.filter(is_even) # [2 4]
    total: int = nums.reduce(add)              # 15
    total2: int = nums.reduce(add, 10)         # 25 (with initial value)

    has_even: bool = nums.any(is_even)         # true
    all_even: bool = nums.all(is_even)         # false
}
```

These methods also work with lambdas. Two forms are supported:

**Inferred params** — written with bare names; the compiler propagates
the receiver's element type into each lambda parameter (and infers the
return type for predicates and arithmetic bodies):

```lammergeier
doubled: list[int] = nums.map(lambda x: x * 2)
evens: list[int]  = nums.filter(lambda x: x % 2 == 0)
total: int        = nums.reduce(lambda a, b: a + b, 0)
```

**Annotated params** — wrap the whole parameter list in one paren
group; type annotations are optional per-param. Use this form for
stand-alone lambdas or when the caller context can't supply a hint:

```lammergeier
doubler = lambda (x: int): x * 2
adder   = lambda (a: int, b: int): a + b
result: list[int] = nums.map(doubler)
```

Function references work the same way, builtins included:
`words.map(len)` is a `list[int]`, `nums.map(str)` a `list[str]`. A
`map` changes the element type for the rest of a chain
(`words.map(lambda w: len(w)).filter(lambda n: n > 1)` filters ints),
`reduce(fn, initial)` accumulates in the initial value's type
(`words.reduce(lambda acc, w: acc + len(w), 0)` is an `int`), and a
predicate (`filter` / `any` / `all`) whose body is not a `bool`, or a
lambda with the wrong number of parameters, is a compile error.

**Explicit return type** — append `-> Type` between the params and
the body when inference can't reach it (typically multi-line bodies
or generic combinators):

```lammergeier
toFloat = lambda (x: int) -> float: float(x)
```

**Multi-line body** — replace the `:` body separator with a
brace-delimited suite. Multi-line lambdas need an explicit
`return` to yield a value; without one, the lambda's return type
stays `any` and callers receive a zero value:

```lammergeier
classify = lambda (n: int) -> str {
    if n < 0 {
        return "negative"
    }
    if n == 0 {
        return "zero"
    }
    return "positive"
}
print(classify(-3))   # "negative"
```

The inline (untyped) form does not accept the brace syntax — use
the paren-typed form when you want a multi-line body.

**Contextual typing** — a lambda written where a `func(...)` value is
expected (a parameter, an annotated variable, a `return` from a function
returning a `func` type, a `func`-typed field) takes its parameter and
return types from that type, so bare parameter names are enough:

```lammergeier
func apply(f: func(int) -> bool, x: int) -> bool { return f(x) }
func run(f: func()) { f() }
func maker() -> func() -> Shape { return lambda: Square(1) }

ok: bool = apply(lambda x: x > 0, 3)   # x: int, returns bool
check: func(int) -> bool = lambda a: a > 1
run(lambda: print("tick"))             # void body for func()
```

A lambda whose body is a call that produces no value (`print(...)`, a
function without a return type) is a void `func` even where the expected
type is `any` (`Cron.schedule(expr, lambda: print("tick"))`).

---

## Multiple Return Values

Functions can return multiple values:

```lammergeier
func swap(a: int, b: int) -> (int, int) {
    return b, a
}

func main() {
    x, y = swap(1, 2)
    print(x)  # 2
    print(y)  # 1
}
```

---

## Safe Navigation & Null Coalescing

Two operators make working with nullable values less noisy:

- `obj?.field` — if `obj` is `nil`, the whole expression evaluates to
  `nil` without dereferencing; otherwise it returns `obj.field`.
  Method calls use the same syntax (`obj?.method(args)`).
- `a ?? b` — returns `a` if it is non-`nil`, otherwise `b`. Chains
  fold left-to-right, e.g. `a ?? b ?? c`.

```lammergeier
class User {
    func init(self, email: str) {
        self.email: str = email
    }
}

func main() {
    active: User = User("ada@example.com")
    missing: User = None

    print(active?.email)                    # ada@example.com
    print(missing?.email)                   # <nil>
    print(missing?.email ?? "default")      # default
    print(None ?? 42)                       # 42
    print(None ?? None ?? "fallback")       # fallback
}
```

On a class or `optional[...]` receiver `obj?.field` is typed: it is an
`optional` of the field's type (`optional[str]` for a `str` field, the
field's own type when it already holds `None`), so `?.` chains
(`user?.profile?.email`), and `??` with a fallback of the payload type
yields that type (`e: str = user?.email ?? "x"`). `print` and f-strings
show an optional value's payload, or `<nil>`. `d[key] ?? fallback` on a
dict selects the fallback when the key is missing. In other contexts
(`None ?? 42`, receivers of unknown type, `?.method()` calls) both
operators return `interface{}`; assign to an annotated variable when a
concrete type is required downstream. `optional[User]` and
`Option[User]` annotations retain `User` as the safe-navigation receiver
for member suggestions and call-shape checks, and lower to one `*User`
pointer rather than a pointer-to-pointer. `optional[any]` stays dynamic.

An `optional[T]` (or `Option[T]`) variable, field, parameter or return
accepts `None` and any `T`, compares with `None` (`n == None`,
`n != None`), and an optional class reference can be assigned back to the
class type (class-typed variables already hold `None`). A value payload
(`optional[int]` is a Go `*int`) is boxed automatically when an `int` is
assigned or returned, and `opt ?? fallback` unwraps it to an `int`; it does
not convert to a plain `int` implicitly.

```lammergeier
class Node {
    func init(self, v: int) {
        self.v: int = v
        self.next: optional[Node] = None
    }
}

func find(v: int) -> optional[Node] {
    if v > 0 {
        return Node(v)
    }
    return None
}

func count(s: str) -> optional[int] {
    if s == "" {
        return None
    }
    return len(s)
}

func main() {
    head: optional[Node] = None
    print(head == None)                 # true
    a: Node = Node(1)
    a.next = Node(2)
    head = a
    m: Option[Node] = find(3)
    print(m?.v)                         # 3
    n: int = count("abc") ?? 0          # 3
    print(n)
}
```

---

## Dictionary Destructuring

JS-style `{key1, key2, key3} = mapping` pulls one or more keys out
of a dict-shaped value into local variables in a single statement.
A `{key: localName}` form lets you rename on the fly:

```lammergeier
user = {"name": "alice", "age": 30, "role": "admin"}

# Bind ``name`` and ``age`` directly.
{name, age} = user
print(name)      # alice
print(age)       # 30

# Rename: bind ``user["role"]`` to a different local.
{role: userRole} = user
print(userRole)  # admin

# Mix and match — the order doesn't matter.
{age: aliceAge, name: aliceName, role} = user
```

The right-hand side may be any expression that evaluates to a
dict; it is evaluated **once** into a synthetic temporary, so
`{a, b} = computeUser()` only calls `computeUser` once. The
introduced names are plain locals — annotate them on a follow-up
line if you need a specific Lam type:

```lammergeier
{name, age} = user
n: str = name        # tighten the inferred ``any`` type
a: int = age
```

Defaults use `key = expression`, and combine with aliases as
`key: localName = expression`:

```lammergeier
{name, role = "reader", age: years = 0} = payload
```

The destructuring rewrite happens at the preprocessor layer, so the pattern
must sit at statement start. When the right side is a dictionary literal, a
missing key without a default is a shared compiler/LSP `error[assign]`.
Dynamic dictionaries retain runtime lookup semantics because their keys are
not statically knowable.

Tuple/list assignment destructuring (`a, b = pair()` or `a, b = [1, 2]`)
uses selected overload signatures, imported and Go-backed Lam wrapper
annotations, and literal element types to report arity/type errors. New targets
emit Go `:=`; reassignments emit `=`; list/tuple literals are expanded into
individual right-hand values.

---

## Tuple Destructuring in Parameters

A function parameter can be a parenthesised list of names paired with a
`tuple[...]` annotation. The compiler emits a single synthetic slice
argument and binds the inner names (with typed assertions) at the top
of the body:

```lammergeier
func midpoint((x, y): tuple[int, int]) -> int {
    return (x + y) / 2
}

func greet((first, last): tuple[str, str]) -> str {
    return f"Hello, {first} {last}"
}

func main() {
    print(midpoint((4, 10)))          # 7
    print(greet(("Ada", "Lovelace"))) # Hello, Ada Lovelace
}
```

At the call site the tuple literal `(x, y)` is wrapped as a Go
`[]interface{}{x, y}` so it travels as one value.

---

## Generics / Type Parameters

Both functions and classes accept an optional type-parameter list
immediately after their name. The grammar is `[T, U: constraint, ...]`;
an omitted constraint defaults to Go's `any`.

```lammergeier
func identity[T](x: T) -> T {
    return x
}

func pair_first[T, U](a: T, b: U) -> T {
    return a
}

func max_of[T: ordered](a: T, b: T) -> T {
    if a > b { return a }
    return b
}
```

Type arguments may be explicit (`identity[int](1)`) or inferred from bound
arguments (`identity(1)`). Inference unifies repeated parameters recursively,
so `same[T](left: T, right: T)` rejects `same(1, "two")`, and
`sameList[T](list[T], list[T])` rejects mixed element types. The inferred
mapping also specializes return types, constructors, imported generic
functions/classes, and member inference. A function passed by name binds
the type parameters of a callback parameter (`mapList(names, len2)` with
`f: func(T) -> U` and `len2: func(str) -> int` gives `list[int]`), and a
`.map(lambda p: p.name)` over a `list[Person]` types `p` as `Person`. If an
argument is `any` or otherwise unknown, the checker keeps the conservative
fallback instead of guessing.

Lambdas passed to a generic function or method take their parameter types
from the type arguments known at the call, and their result binds the
rest, like Go's own inference: the receiver's type arguments first
(`it: Iter[int]` gives `T = int`), then typed arguments, then untyped
literals, and last lambdas and function references (builtins included:
`mapList(words, len)`). No annotation or conversion is needed, and a
callback of the wrong type (an annotated parameter, the body's result or
the parameter count) is a Lam error:

```lammergeier
func mapList[T, U](xs: list[T], fn: func(T) -> U) -> list[U] { ... }
func fold[T, A](xs: list[T], initial: A, fn: func(A, T) -> A) -> A { ... }

strs: list[str] = mapList([1, 2], lambda n: f"{n}!")       # n: int, U = str
total: int = fold([1, 2, 3], 0, lambda acc, x: acc + x)    # A = int
lens: list[int] = mapList(["a", "bb"], len)
```

Built-in constraints:

| Lam name     | Go constraint                                |
|--------------|----------------------------------------------|
| `any`        | `any`                                        |
| `comparable` | `comparable`                                 |
| `ordered`    | `~int \| ~int64 \| ~float64 \| ~string`      |
| `number`     | `float64 \| int`                             |
| `int`, `str` | `int`, `string` — exact-type fast paths      |

Inside a generic body, values of a type parameter support the operators its
Go constraint allows (`total + x` with `T: number`), and an untyped numeric
constant initialises or is returned as a type parameter whose constraint
only holds numbers (`number`, `int`, `float`): `t: T = 0` in
`class Stats[T: number]`. An unconstrained `T` (`any`) does not accept `0`.

Any unrecognised name passes through verbatim, so you can use an
imported interface directly:

```lammergeier
func write_json[T: fmt.Stringer](value: T) -> str {
    return value.String()
}
```

Classes take the same clause, and every method automatically
receives the class's type parameters on its receiver:

```lammergeier
class Stack[T] {
    items: list[T] = []
    func push(self, v: T) { self.items.append(v) }
    func pop(self) -> T { return self.items.pop() }
}

func main() {
    s: Stack[int] = Stack[int]()   # NewStack[int]()
    s.push(42)
    print(s.pop())
}
```

The constructor call site uses `ClassName[ArgTypes](...)`; the
transpiler rewrites that to `NewClassName[ArgTypes](...)` — which is
the zero-value constructor Lam already emits for every class, now with
type arguments threaded through. Static methods of a generic class
inherit the class's type parameters automatically:

```lammergeier
class Box[T] {
    static func wrap(x: T) -> T { return x }
}
# -> func Box_wrap[T any](x T) T
```

A method may declare its own type parameters next to the class's
(`map[U]` changing the element type of a container). Go methods cannot
have type parameters, so such a method lowers to a package-level generic
function taking the receiver first, and its calls are rewritten
(`b.map(f)` -> `Box_Map(b, f)`); the call is written like any method
call and chains (`b.map(f).map(g)`). These methods are not virtual: a
subclass cannot override one (it is a compile error; give the subclass
method another name).

```lammergeier
class Box[T] {
    func init(self, value: T) { self.value: T = value }
    func map[U](self, fn: func(T) -> U) -> Box[U] {
        return Box[U](fn(self.value))
    }
}
# -> func Box_Map[T any, U any](s *Box[T], fn func(T) U) *Box[U]

b: Box[int] = Box[int](3)
label: Box[str] = b.map(lambda x: f"n={x * 2}")   # x: int, U = str
```

A local annotated with a generic class but no type arguments takes them
from its initializer (`it: Iter = Iter.range_(0, 3)` is an `Iter[int]`);
everywhere else (parameters, fields, returns, locals without a value) a
generic class needs its type arguments (`it: Iter[int]`).

A class may inherit from a generic instantiation. The Go struct embeds
`*Box[int]`, the parent is built with `NewBox[int](...)`, and inherited
fields and methods use the bound types (`b.get()` is an `int` below);
virtual dispatch and field defaults work as for any parent. A generic
class can pass its own parameter on (`class Named[T](Box[T])`).

```lammergeier
class Box[T] {
    func init(self, value: T) {
        self.value: T = value
    }
    func get(self) -> T {
        return self.value
    }
}

class IntBox(Box[int]) {
    func init(self, value: int) {
        base.init(value)
    }
    func doubled(self) -> int {
        return self.value * 2
    }
}

func main() {
    b: IntBox = IntBox(21)
    n: int = b.get()
    print(n, b.doubled())             # 21 42
}
```

---

## Importing Custom Libraries

Create `.lam` library files and import them:

**`myhelper.lam`:**

```lammergeier
func double(x: int) -> int {
    return x * 2
}

func main() {
    pass
}
```

**`main.lam`:**

```lammergeier
from myhelper import double

func main() {
    print(double(5))  # 10
}
```

### Aliasing imports with `as`

Functions, constants, classes and interfaces are imported by name with
`from X import A` and can be renamed locally with `from X import A as B`:

```lammergeier
from lamerrors import Result as R, Error as E

func bad() -> R {
    return R.Err(E("parse", "bad input"))
}
```

Aliasing is a local-binding convenience: the library still exports its
canonical names, so two files can alias the same symbol differently without
conflicting. Unused aliases trigger the same `unused import` warning as
unused plain imports (the alias name is cited in the warning, not the
original).

### Module imports and namespaces

`import X` (or `import X as Y`) binds the module itself; its members are then
reached with `X.name` / `Y.name` — functions, constants, classes, static
methods and type annotations alike. Scoped packages (`@scope/pkg`) cannot be
written as qualifiers, so they must be imported with an alias:

```lammergeier
import helpers
import @lam/jwt as jwt

func main() {
    print(helpers.double(4))
    token: str = jwt.Jwt.signHS256({"sub": "u1"}, "secret").unwrap()
    print(len(token) > 0)
}
```

`import @lam/jwt` without `as` is a compile error that suggests the alias.

Every module has its own namespace for top-level **functions, constants,
module-level variables, classes and interfaces**: two libraries may both
define `tag()`, `Version`, `counter` or `class Config`, and each keeps using
its own. A name only has to be unique where it is used:

```lammergeier
from alpha import tag              # alpha's tag()
import beta as b                   # b.tag() is beta's tag()
from gamma import tag as gammaTag  # aliased because `tag` is already bound

func main() {
    print(tag(), b.tag(), gammaTag())
}
```

Defining a top-level `tag` and also importing another module's `tag` without
`as` is an error that suggests an alias (`from alpha import tag as alphaTag`).
The same works for classes (`from settings import Config as SettingsConfig`
next to your own `class Config`).

Two exceptions keep their names and are still reported by the link-time clash
diagnostic if something else in the program defines the same name in the
**main** file: definitions in the main file itself, and the classes that
compiler-generated code uses (`Result` and `Error` from `lamerrors`, `Strings`
from `lamstrings`). A *library* may still define its own `Error`.

Raw `go!` code follows these renames for its own module's definitions and for
names it imports without an alias (`from one import Config` → `NewConfig()` in
`go!` keeps working). It cannot follow an aliased import or a module reached
only indirectly: there, use the generated Go name (`NewConfig__one`, `Tag__alpha`;
see [Transpilation](#/docs/transpilation)).

Library files are resolved in three layers, in this order:

1. **Stdlib** — `<compiler>/lib/`. Always wins, so users can't
   accidentally shadow a core module.
2. **Extlibs** — third-party libraries, in priority order:
   a. `--extlibs DIR` CLI flag (repeatable).
   b. `LAMC_EXTLIBS` env var (colon-separated list).
   c. `<source-dir>/extlibs/` — per-project vendored deps.
   d. `~/.lammergeier/extlibs/` — user-global install path.
3. **Project** — the source file's own directory, then its
   `lib/` subdirectory.

The file extension is `.lam`; a library may also be a directory
containing `__init__.lam`. Package submodules use dotted import paths:

```text
lamwebp/
├── __init__.lam
├── codec.lam
└── io/
    └── files.lam
```

```lammergeier
from lamwebp.codec import Decoder
from lamwebp.io.files import readWebp
```

Module paths preserve the author's casing. `snake_case`, `camelCase`,
`PascalCase`, and `SCREAMING_SNAKE_CASE` are all valid, including scoped
packages such as `from @AliceTeam/LamWebP import Decoder`. Scoped package
families may add segments, as in `from @lam/vulture/ws-plugin import WebSocket`.
Imports are case-sensitive and must match the `.lam` filename, package
directory, and `library.name` spelling exactly. Plain module segments remain
Lam identifiers, so use `_` rather than `-`; scoped path segments may use
hyphens after their first character.

Each import bundles that module plus its transitive imports. A package
`__init__.lam` may re-export submodule names with a normal `from` import,
so `from lamwebp import Decoder` can remain the concise public API. See
[`third_party_libraries.md`](#/docs/third_party)
for the distribution / install workflow that layers on top of
the resolver.

---

## Standard Library

All standard library modules use OOP-style static classes with camelCase naming:

| Module           | Class                              | Description |
|------------------|------------------------------------|-------------|
| `lamerrors`      | `Result`, `Error`                  | Structured errors and `Result` (Ok/Err/unwrap/unwrapOr) for the `?` and `do/catch` system. See [Errors & Results](#errors--results) |
| `lammath`        | `Math`                             | Math functions (sqrt, sin, cos, pow, log, etc.) |
| `lamstrings`     | `Strings`                          | String utilities (repeat, contains, trim, split, join, etc.) |
| `lamtemplate`    | `Template`, `HtmlTemplate`         | Result-based Go text and autoescaped HTML templates with file, named-template, and helper support |
| `lamtime`        | `Time`                             | Duration units/arithmetic plus Unix time, sleeps, parsing, formatting, and measurement |
| `lamcron`        | `Cron`                             | Cron scheduling with standard/seconds-aware expressions, descriptors, and lifecycle controls |
| `lamconv`        | `Conv`                             | Return-type-overloaded `parse` for decimal int/float/bool plus explicitly named hex/octal/binary parsers |
| `lamos`          | `Os`, `File`, `FileLineGenerator`  | Result-based OS/filesystem helpers plus streaming file lines and batches |
| `lamjson`        | `Json`                             | Native `json` runtime, Result-based encode/decode/pretty, validation, and dict/list conversion |
| `lamyaml`        | `Yaml`                             | Result-returning YAML encoding, decoding, and multi-document decoding |
| `lamxml`         | `Xml`, `XmlNode`                   | Result-based XML encoding/decoding plus DOM-style node traversal |
| `lamrandom`      | `Random`                           | Pseudo-random values and secure bytes/tokens; UUID generation belongs to `lamuuid` |
| `lamsort`        | `Sort`                             | Generic natural/comparator sorting, asc/desc checks, named algorithms, reverse, and lazy sorted generators |
| `lamre`          | `Re`                               | Result-returning regex match, find, findAll, replaceAll, split, and groups |
| `lamhttp`        | `Http`, `HttpClient`, `HttpResponse`, `HttpCookie`, `HttpServer` | Overloaded native-JSON requests, response headers/cookies, and a blocking server (`Http.serve`) |
| `lamsmtp`        | `Smtp`, `Mail`                     | Result-based SMTP delivery and MIME mail construction for text, HTML, and attachments |
| `lamdb`          | `Db`                               | Database connectivity (connect, close, exec, queryRow, queryAll, queryRows) |
| `lammigrate`     | `Migrator`, `SchemaBuilder`        | SQL migration batches and portable programmatic schema helpers |
| `lamhash`        | `Hash`                             | Hashing (sha256, sha1, sha512, md5, crc32) plus HMAC (hmacSha1/256/512), `constantTimeEquals`, `verifyHmacSha256` |
| `lamcrypto`      | `Crypto`, `CryptoKeyPair`, `CryptoCertificate` | Secure random data, AES-GCM envelopes, PBKDF2 password hashes, PEM keys/signatures, and X.509 metadata |
| `lambase64`      | `Base16`, `Base32`, `Base64`, `Hex` | Standard/raw Base16/Base32/Base64 codecs and compatibility aliases |
| `lampath`        | `Path`                             | Variadic path joining plus base/dir/ext/abs/glob/existence/splitting helpers |
| `lamcsv`         | `Csv`, `CsvReader`                 | Result-based CSV parsing/formatting and lazy row/batch streaming |
| `lamfmt`         | `Fmt`                              | Formatting (sprintf, printf, println, hex, binary, octal, padLeft, padRight, center, formatFloat, repr) |
| `lamurl`         | `Url`                              | URL parsing (parse, host, path, scheme, encode, decode) |
| `lamlog`         | `Log`                              | Logging (info/warn/error/debug/fatal plus infof/warnf/errorf/debugf/fatalf) |
| `lambytes`       | `Bytes`                            | Byte/hex utilities (toBytes, fromBytes, hexEncode, hexDecode) |
| `lamexec`        | `Exec`, `ExecCommand`, `ExecResult` | Typed execution with env/cwd/stdin, timeout, cancellation, and captured output |
| `lamunicode`     | `Unicode`                          | Unicode/UTF-8 (runeCount, isValid, isLetter, isDigit, etc.) |
| `lamenv`         | `Env`                              | String, typed, and Result-based environment access plus set/unset/all |
| `lamdatetime`    | `DateTime`                         | Date/time formatting and parsing |
| `lamdatastructures` | `Queue`, `Deque`, `Stack`, `Set`, `Heap`, `PriorityHeap` | Canonical collection implementations with Result-returning empty-state operations |
| `lamstack`       | `Stack`                            | Compatibility re-export from `lamdatastructures` |
| `lamqueue`       | `Queue`, `Deque`                   | Compatibility re-exports from `lamdatastructures` |
| `lamdeque`       | `Deque`                            | Compatibility re-export from `lamdatastructures` |
| `lamheap`        | `Heap`, `PriorityHeap`             | Compatibility re-exports from `lamdatastructures` |
| `lamset`         | `Set`                              | Compatibility re-export from `lamdatastructures` |
| `lamstats`       | `Stats`                            | Statistics (sum, product, mean, median, mode, variance, sampleVariance, stddev, minVal, maxVal, range_, percentile) |
| `lamcompress`    | `Compress`                         | Compression (gzip/gunzip and zlibDeflate/zlibInflate; outputs base64 strings) |
| `lamnet`         | `Net`, `TcpConn`, `TcpListener`, `UdpConn`, `UdpPacket` | Result-based DNS and TCP/TLS/UDP sockets; reads use `receive` / `receiveFrom` |
| `lamcli`         | `Cli`                              | CLI args/positionals and overloaded string/integer `getFlag` |
| `lamtest`        | `Test`                             | Assertion/test framework (assertTrue, assertFalse, assertEqual, assertNotEqual, assertNil, assertNotNil, assertLen, assertContains, assertAlmostEqual, describe, pass, fail, summary) |
| `lamconcurrency` | `Channel`, `WaitGroup`, `Mutex`, `RWMutex`, `Atomic` | Blocking `send` / `receive`, awaitable `asyncSend` / `asyncReceive`, immediate `offer` / `poll`, and synchronization primitives |
| `lamcontext`     | `Context`                          | Cancellation trees, unit-aware expiration, inherited values, bounded waits, and native Go context interop |
| `lamactor`       | `Mailbox`, `ActorRef`, `ActorSystem` | Actor messaging; mailboxes share Channel's blocking, awaitable, and immediate Result APIs |
| `lamretry`       | `Retry`                            | Result-aware retries and predicate polling with capped exponential backoff and optional jitter |
| `lamratelimit`   | `TokenBucket`                      | Goroutine-safe token-bucket rate limiting with blocking and immediate acquisition |
| `lamarray`       | `Array`, `Matrix`                  | NumPy-style numerics backed by **gonum** — `fromList`, `zeros`, `ones`, `linspace`, `arange`, element-wise add/sub/mul/div, BLAS `matmul`, `mulVec`, `transpose`, `inverse`, `det`, `trace`, `solve`, `dot`, `norm`, reductions (sum/mean/std/min/max/argMin/argMax) |
| `lamdata`        | `DataFrame`, `Series`, `DataFrameGroups` | Pandas-style dataframes backed by **go-gota** — overloaded text/native-JSON `readJSON`, generic typed `mutate`/`Series.from`, native `json(frame)`, selection/filtering/sorting/joins/grouping, CSV/JSON output, and Series statistics |
| `lamschema`      | `Schema`                           | JSON Schema validation (draft-07) via `xeipuuv/gojsonschema` with compile-once caching; declarative validation reusable as OpenAPI specs |
| `lamiter`        | `Iter[T]`                          | Typed lazy iterator combinators: `fromList`, `range_`, `count`, `repeat`; `map`, `filter`, `take`, `drop`, `takeWhile`, `dropWhile`, `enumerate`, `chain`; terminals `toList`, `forEach`, `reduce`, `count_`, `first`, `anyMatch`, `allMatch` |
| `lamcache`       | `LruCache`, `TtlCache`             | In-memory caches — fixed-capacity LRU (eviction on overflow) and per-entry TTL (lazy expiry on read), both goroutine-safe |
| `lamuuid`        | `Uuid`                             | RFC 4122 UUID generation: `v4` (cryptographically-random), `v7` (time-ordered, k-sortable), plus `nil_`, `isValid`, `parse` |

The HTTP server is distributed separately as `@lam/vulture`; its standard,
WebSocket, and tus integrations are `@lam/vulture/std-plugin`,
`@lam/vulture/ws-plugin`, and `@lam/vulture/tus-plugin`. Their current releases
(2.0.1, 2.0.2, 2.0.1 and 3.0.1) are tested with `lamc` 1.19 and support 1.18
and newer. See the [official package catalog](#/docs/libraries)
for installation and imports.

**Example:**

```lammergeier
from lammath import Math
from lamconv import Conv
from lamerrors import Result
from lamfmt import Fmt

func main() {
    print(Math.sqrt(25.0))       # 5
    print(Math.pi())             # 3.141592653589793
    print(Fmt.hex(42))           # 2a
    result: Result[int] = Conv.parse("123")
    parsed: int = result.unwrap()
    print(parsed)                # 123
}
```

---

## Raw Go Injection (go!)

Embed raw Go code directly using `go! { ... }`:

```lammergeier
func main() {
    go! {
        import "fmt"
        fmt.Println("Hello from raw Go!")
    }
}
```

### Inline Go Expressions

Use `go!(expr)` for single Go expressions inline:

```lammergeier
go! {
    import "time"
    import "strings"
}

func main() {
    year: int = go!(time.Now().Year())
    upper: str = go!(strings.ToUpper("hello"))
    print(year)
    print(upper)
}
```

### Scoping inside `go!` blocks

A `go!` block is a verbatim paste into the generated Go source — Lam
does **not** rewrite identifiers inside it, with one exception: in a
**class method** the bare receiver is renamed and `self.<Field>` is
rewritten to use it. Knowing exactly what each shape gives you prevents
the most common stdlib-extension pitfall: relying on a name that simply
isn't bound in the surrounding Go scope.

#### Inside class methods — `s` and `self.<Field>` are bound

The transpiler emits class methods as Go methods with receiver `s` and
rewrites every `self.X` reference inside the body — including those
that appear inside a `go!` block — to `s.<TitleCased X>`:

```lammergeier
# GOOD — inside a method, self/s and self.<Field> are both usable.
class Counter {
    func init(self, start: int) {
        self.value: int = start
    }

    func bump(self) {
        go! {
            // ``self.value`` is rewritten to ``s.Value`` automatically.
            self.value += 1
            // You may also use the receiver directly.
            s.Value += 1
        }
    }
}
```

Both `self.value` and `s.Value` mean the same thing here. The receiver
name (`s`) is the same one the struct emitter uses; field names are
title-cased to match Go's exported convention. When the method (or
constructor) itself uses the name `s` — a parameter `func init(self, s:
int)`, a local, a global it reads — the receiver is named `_lamS` instead so
the two don't collide; write `self.<field>` in its `go!` blocks.

#### Inside top-level functions — neither `s` nor `srv` is implicitly bound

A top-level function gets no implicit receiver. References to `s` or
`srv` inside a top-level `go!` block resolve through normal Go scoping,
which means: only what your Lam function actually declared as a
parameter (or what a surrounding top-level `go!` block declared as a
package-level Go var) is visible. There is **no compiler magic** that
binds `s` or `srv` for you.

```lammergeier
# GOOD — `srv` is a Lam parameter, so it's a Go local in the emitted
# function and can be referenced inside `go!` directly.
func myPlugin(srv: Server) {
    go! {
        srv.OnRequestHooks = append(srv.OnRequestHooks, func(req *Request, res *Response) {
            // ...
        })
    }
}
```

```lammergeier
# BAD — `s` is not a parameter and there's no class method context.
# The compiler rejects it: `s` is only the receiver alias inside class methods.
func brokenPlugin() {
    go! {
        s.OnRequestHooks = append(s.OnRequestHooks, ...)   # error
    }
}
```

#### Sharing state across handlers — package-level Go vars

HTTP handlers are typically lambdas, closures, or top-level functions —
none of which have a `self`/`s` to hang state on. The canonical pattern
is to declare a Go-level package var (or a `sync.Mutex`-protected
struct) in a **top-level** `go!` block and capture it from inside the
handler's own `go!` block:

```lammergeier
# GOOD — a package-level Go var declared at module top, shared safely
# across all handlers via `sync/atomic`.
go! {
    import "sync/atomic"
    var lamRequestHits int64
}

func incrementHits(req: Request, res: Response) {
    go! {
        atomic.AddInt64(&lamRequestHits, 1)
    }
}

func readHits() -> int {
    n: int = 0
    go! { n = int(atomic.LoadInt64(&lamRequestHits)) }
    return n
}
```

```lammergeier
# BAD — declaring the counter inside the handler resets it on every
# request, and `srv` doesn't carry it for you either.
func incrementHits(req: Request, res: Response) {
    go! {
        var hits int64       # NEW VARIABLE EVERY REQUEST
        atomic.AddInt64(&hits, 1)
    }
}
```

If you need *per-server* (rather than process-global) state, store it
on the `Server` instance via `srv.decorate("name", value)` and read it
back with `req.dec("name")` from inside handlers.

---

## Default Parameter Values

Functions and methods support default values:

```lammergeier
func greet(name: str, greeting: str = "Hello") -> str {
    return f"{greeting}, {name}!"
}

print(greet("Alice"))        # Hello, Alice!
print(greet("Bob", "Hi"))    # Hi, Bob!
```

Defaults work in constructors and static/instance methods too.

> **Note:** Variadic functions (`*args`) are excluded from default filling.

---

## Named (Keyword) Arguments

Any function, method, or constructor parameter can be filled by
name at the call site. Named arguments may appear in any order
and may be mixed with positional arguments — positionals come
first, then keywords:

```lammergeier
func greet(name: str = "world", times: int = 1, suffix: str = ".") {
    for i in range(times) { print(f"hello, {name}{suffix}") }
}

greet()                                   # hello, world.
greet(name="alice")                       # hello, alice.
greet(times=3, suffix="!")                # 3× hello, world!
greet("bob", suffix="?", times=2)         # 2× hello, bob?
```

Named arguments work with constructors and methods exactly the
same way:

```lammergeier
class User {
    func init(self, name: str = "anon", role: str = "guest") {
        self.name: str = name
        self.role: str = role
    }
}

u = User(role="admin", name="bob")        # order doesn't matter
```

Rules:

- A keyword that doesn't match a parameter is rejected at compile
  time with an `unknown keyword argument` error listing the valid
  parameter names.
- Supplying the same parameter both positionally and by keyword is
  rejected with a `given both positionally and as a keyword` error.
- Keywords cannot fill the variadic slot of a `*args` function;
  pass those positionally.

---

## Function Overloading

Functions with the same name can differ by arity, parameter types, or return
type. Lam records each complete signature and picks a matching variant at every
call site.

### By arity

```lammergeier
func describe(name: str) -> str {
    return f"Name: {name}"
}

func describe(name: str, age: int) -> str {
    return f"Name: {name}, Age: {age}"
}

print(describe("Alice"))      # Name: Alice
print(describe("Bob", 30))    # Name: Bob, Age: 30
```

### By parameter type (same arity)

```lammergeier
func describe(value: int) -> str {
    return f"int: {value}"
}

func describe(value: str) -> str {
    return f"str: {value}"
}

func describe(value: list[int]) -> str {
    return f"list[int]: {value}"
}

print(describe(42))            # int: 42
print(describe("hi"))          # str: hi

nums: list[int] = [1, 2, 3]
print(describe(nums))          # list[int]: [1 2 3]
```

The compiler picks a variant by inferring the Go type of every
positional argument: typed locals (`nums: list[int]`) participate
through the variable-type table, literals through their
expression shape (`42` → `int`, `"hi"` → `string`, `[1, 2, 3]` →
`[]int`). If argument or return context cannot distinguish otherwise viable
variants, dispatch falls back deterministically to the first matching
source-order definition.

### By return type

Overloads may have identical parameters and different return types. The compiler
uses the expected type from an annotated assignment, the enclosing function's
return type, or the receiving function parameter:

```lammergeier
class Parser {
    static func parse(value: str) -> int { return len(value) }
    static func parse(value: str) -> str { return f"parsed:{value}" }
}

size: int = Parser.parse("lam")
label: str = Parser.parse("lam")

func show(value: str) { print(value) }
show(Parser.parse("lam"))

func parsedLabel() -> str {
    return Parser.parse("lam")
}
```

Generic payload annotations participate in this selection. Although
`Result[T]` erases to one runtime Go `*Result`, the compiler retains payloads in
its overload keys, so identical `(str)` parameters may distinguish
`Result[int]`, `Result[float]`, and `Result[bool]` returns. A payload-typed local
is the clearest context when the call is nested inside propagation, collection,
or other erased-runtime expressions.

If no expected type can distinguish otherwise viable variants, Lam chooses the
first matching definition in source order. Do not rely on that fallback when
variants have different behavior; add a specific annotation instead.

### Class method overloads

Instance and static methods follow the same signature rules. Instance signatures
exclude `self`; static signatures include every parameter. A class cannot mix a
static and instance overload under one name. Return-type context applies equally
to methods and top-level functions.

```lammergeier
class Formatter {
    static func render(value: int) -> str { return f"int:{value}" }
    static func render(value: str) -> str { return f"str:{value}" }

    func describe(self, value: int) -> str { return f"int:{value}" }
    func describe(self, value: str) -> str { return f"str:{value}" }
}
```

Overload metadata crosses imports and inheritance boundaries. Interfaces may
declare overload sets; an implementing class must provide every signature.

### Mangling rules

Each variant is emitted with a stable Go-name suffix:

- **Different arities** → `Name_<argcount>` (the historical form).
- **Same arity, different parameter types** → `Name_<argcount>_<sigSuffix>`.
- **Same parameters, different return types** →
  `Name_<argcount>_<sigSuffix>_ret_<returnSuffix>`.
  Suffixes are built from Go types — `int`, `str`, `float64`, `sliceOfInt`,
  `mapOfStrToInt`, `ptrFoo`, … — so symbols remain stable and unique. Example:

  ```text
  describe(int)          → Describe_1_int
  describe(str)          → Describe_1_str
  describe(list[int])    → Describe_1_sliceOfInt
  describe(int, int)     → Describe_2_int_int
  describe(str, int)     → Describe_2_str_int
  parse(str) -> int      → Parse_1_str_ret_int
  parse(str) -> str      → Parse_1_str_ret_str
  ```

The duplicate-definition checker keys on
`(name, arity, parameter-type-signature, return-type-signature)`, so only
complete signature duplicates are rejected.

> **Note:** Variadic functions (`*args`) cannot be overloaded.

---

## Compilation

```bash
# Compile to binary
./lamc source.lam

# Compile and run
./lamc source.lam --run

# Emit generated Go source
./lamc source.lam --emit-go

# Emit AST
./lamc source.lam --emit-ast

# Transpile only (generate .go file without compiling)
./lamc source.lam --transpile-only

# Specify output path
./lamc source.lam -o mybinary

# Keep generated .go file
./lamc source.lam --keep-go

# Verbose output
./lamc source.lam -v

# Skip the disk caches (libraries + serialised parser)
./lamc source.lam --no-cache

# Wipe every cached artefact
./lamc source.lam --clear-cache

# Skip the pre-emission semantic checker (rare; prefer `--verbose`)
./lamc source.lam --no-semantic-check
```

The build pipeline caches two artefacts on disk under
`$XDG_CACHE_HOME/lammergeier/` (or `~/.cache/lammergeier/`):

1. **Library transpile results** — keyed on
   `sha256(compiler-source || lib-content)`, so any edit to a `.lam`
   library or to the compiler/grammar invalidates entries
   automatically. Cold builds populate the cache; subsequent builds
   reuse the emitted Go source and the metadata sets the main
   transpiler needs (class names, method return types, default
   args, …).
2. **Lark parser** — pickled via `Lark.save()` and keyed on
   `sha256(lark_version || python_minor || grammar)`. A typical
   parser build is ~150–300 ms; loading the cached blob is &lt;10 ms.

Both caches are best-effort: a corrupt or unreadable file is treated
as a miss and silently overwritten by the next build. `--clear-cache`
removes both.

---

## `LAMMERGEIER.*` — stable namespace for Lam-visible identifiers

`LAMMERGEIER.<name>` is the supported, stable way to reach a
Lam-side identifier from raw Go or to request explicit compiler dispatch in
ordinary Lam. Calls such as `LAMMERGEIER.userFunc()`,
`LAMMERGEIER.UserClass(...)`, `LAMMERGEIER.UserClass.staticMethod()`, and
static-variable reads work directly; existing `go!` behavior remains
compatible. Two layers cooperate behind the scenes:

1. **Compiler-emitted literal aliases.** A tiny fixed table in
   `compiler/preprocessor.py::LAMMERGEIER_ALIASES` rewrites a
   handful of values that *don't* correspond to a Lam-level
   symbol: `LAMMERGEIER.None` and `LAMMERGEIER.nil` both lower
   to a bare Go `nil`. These are textual substitutions performed
   before parsing.
2. **Dynamic symbol dispatch.** Everything else — top-level
   `func`, `class`, `ClassName.staticMethod`,
   `ClassName.staticVar` — is resolved at go-block emission time
   from the symbol table built by the AST walk. The dispatcher
   sees every user-defined identifier *plus* every class /
   static method reachable through the transitive `from X
   import …` graph, so stdlib classes like `lamerrors.Result`
   and `lamerrors.Error` resolve through the same machinery as
   anything you defined yourself. There's no special-casing —
   if `Result` is in scope, `LAMMERGEIER.Result.Ok(v)` works.

| Alias form                                | Lowers to (Go)            | Notes                                                                       |
|-------------------------------------------|---------------------------|-----------------------------------------------------------------------------|
| `LAMMERGEIER.None` / `LAMMERGEIER.nil`    | `nil`                     | Literal alias (no Lam-level symbol)                                          |
| `LAMMERGEIER.Result.Ok(v)`                | `Result_Ok(v)`            | Dynamic — `Result` is the stdlib `lamerrors.Result` class                    |
| `LAMMERGEIER.Result.Err(e)`               | `Result_Err(e)`           | Dynamic — `Err` is a `Result` static method                                  |
| `LAMMERGEIER.Error(k,m,c)`                | `NewError(k,m,c)`         | Dynamic — `Error` resolves to the stdlib class constructor                   |
| `LAMMERGEIER.<userFunc>`                  | `UserFunc` (or unchanged for `private` funcs) | Top-level user function                                  |
| `LAMMERGEIER.<UserClass>(args)`           | `NewUserClass(args)`      | Class instantiation (call form)                                              |
| `LAMMERGEIER.<UserClass>`                 | `UserClass`               | Class type (no trailing call)                                                |
| `LAMMERGEIER.<UserClass>.<staticMethod>(…)` | `UserClass_staticMethod(…)` | Static method                                                              |
| `LAMMERGEIER.<UserClass>.<staticVar>`     | `UserClass_staticVar`     | Static variable                                                              |

The displayed Go names are preferred spellings. If two Lam declarations would
collide after casing/operator/static/overload lowering, the compiler assigns a
readable `__lamN` suffix and routes both ordinary and `LAMMERGEIER` calls
through that same internal mapping.

Stdlib classes participate in the dispatcher only when they're
imported (directly or transitively): `from lamerrors import Result`
brings `Result` and `Result.Ok` / `Result.Err` into scope; if you
never import `lamerrors`, `LAMMERGEIER.Result` is reported as
unknown. The same rule applies to your own classes — declare the
class (or import it) and it appears in the dispatcher. Private symbols are
available to raw Go in their declaring module but are rejected through a
transitive/imported `LAMMERGEIER` reference with a visibility diagnostic.

Use it from a `go!` block to call back into Lam-defined logic
without hand-mangling Go names:

```lammergeier
func formatGreeting(name: str) -> str {
    return "hello, " + name
}

class Counter {
    func init(self, start: int = 0) {
        self.value: int = start
    }
    static func zero() -> int { return 0 }
}

func main() {
    out: str = ""
    go! {
        // Lam function — compiler emits `FormatGreeting("alice")`.
        out = LAMMERGEIER.formatGreeting("alice")

        // Class instantiation — compiler emits `NewCounter(7)`.
        c := LAMMERGEIER.Counter(7)

        // Static method — compiler emits `Counter_zero()`.
        z := LAMMERGEIER.Counter.zero()
        _ = c; _ = z
    }
}
```

The same alias table inside an unsuffixed Result-returning stdlib helper still works:

```lammergeier
static func decode(xmlStr: str) -> Result {
    go! {
        v, err := decoder.DecodeOne()
        if err != nil {
            return LAMMERGEIER.Result.Err(LAMMERGEIER.Error(
                "XmlError", err.Error(), nil,
            ))
        }
        return LAMMERGEIER.Result.Ok(v)
    }
}
```

The compiler emits a typo-guard diagnostic with a did-you-mean
suggestion when a reference doesn't resolve. The suggestion pool
unifies both layers — literal aliases plus every Lam-level symbol
in scope (user-defined and transitively-imported), so a typo on a
stdlib name (`LAMMERGEIER.Result.OK`) suggests the right casing
just like a typo on a user function does:

```text
error: unknown LAMMERGEIER.* reference in app.lam
  line 8: `LAMMERGEIER.realFunctionTypo` does not resolve to a known symbol — did you mean `LAMMERGEIER.realFunction`?
           7 |     go! {
   >>>     8 |         out = LAMMERGEIER.realFunctionTypo()
           9 |     }

  LAMMERGEIER.* resolves to either of:
    • a compiler-emitted literal alias (for values with no Lam-level symbol):
        - LAMMERGEIER.None
        - LAMMERGEIER.nil
    • a Lam-level symbol in scope (function / class / static member):
        - LAMMERGEIER.Error
        - LAMMERGEIER.Result
        - LAMMERGEIER.Result.Err
        - LAMMERGEIER.Result.Ok
        - LAMMERGEIER.realFunction
        - …
```

The typo guard runs *after* the library import worklist has
finished, so transitively-imported classes (`Result`, `Error`,
…) are already in `extra_valid` by the time the diagnostic
fires — false positives on stdlib names are no longer possible.

**Limitations.** The alias only resolves single-segment user names
(top-level `func`/`class`) and `Class.staticMember`. It does *not*
lift Lam literals, expressions, or local variables — only declared
identifiers. Instance methods need a receiver value at call site
and aren't supported via the alias; static methods and variables are.

---

## Syntax flexibility (JS-style formatting)

Lammergeier deliberately avoids the indentation-sensitivity that
Python enforces. You can format a file however you like as long as
the brace structure parses. The preprocessor handles every common
arrangement:

| Style                 | Example                                   |
|-----------------------|-------------------------------------------|
| K&R                   | `func f() {`                              |
| Allman                | `func f()\n{`                             |
| Single-line           | `func f() { return 42 }`                  |
| Single-statement body | `if x > 0 print(x)`                       |
| Trailing semicolons   | `a = 1;` (also `a = 1;;;`)                |
| Compact if/elif/else  | `if x > 0 a = 1 elif x < 0 a = -1 else a = 0` |
| Allman *and* compact  | `func f()\n{ return 42 }`                 |
| Statement before close| `print(value)}` inside an open block      |
| Backslash continuation| `a + \\\n  b`                               |
| Operator continuation | `ok = a and\n  b` / `x = a +\n  b`         |
| Parenthesised lines   | `v = (\n  "big" if x > 3\n  else "small"\n)` |
| Inline trailing comma | `f(\n    x,\n    y,\n)`                   |
| Empty body            | `class Empty { }` / `func nop() { }`      |
| Comment-only body     | `func todo() {\n    # later\n}`            |
| Trailing comment      | `if ok { f() }  # note`                   |

Internally these are normalised by:

- `_inline_block_semicolons` — inserts `;` before `}` when a compact
  block has a statement immediately before its close (for example,
  `func f() { return 1 }`, or `print(value)}` inside a block).
- `_fill_empty_blocks` — replaces `{ }` (or a body holding only
  comments) with `{ pass }` so a class or function with no body still
  parses against the grammar's `suite: "{" stmt+ "}"` rule. Interface
  bodies are left empty: `interface Marker { }` declares no methods.
- `_collapse_runaway_semicolons` — collapses `;;;` to `;`.
- The auto-semicolon pass treats a leading `{` on the next line as
  a continuation of the current header (Allman braces) and skips
  inserting `;` after lines that close blocks.

`lamc fmt <file-or-dir>` canonicalises these shapes toward the project style:
compact block bodies are expanded, redundant blank lines are collapsed, and a
directory argument recursively formats every `.lam` file below it. `--stdout`
is intentionally file-only; use `--check` on files or directories in CI.

---

## Accepted but unused grammar forms

The Lark grammar accepts a few compatibility or reserved forms that
do not currently participate in code generation. Treat these as
parse-compatible placeholders, not supported language features:

| Accepted form | Current compiler behavior | Supported spelling to use |
|---------------|---------------------------|---------------------------|
| `@decorator func f() { ... }` / `@decorator class C { ... }` | The decorator node is parsed, then rejected by the semantic checker because decorator semantics are not implemented yet. | Use explicit Lam keywords or ordinary wrapper/helper functions. |
| `@private func f() { ... }` | Rejected by the semantic checker so it cannot silently emit a public `F`. | `private func f() { ... }` |
| `nonlocal name` | Resolves the nearest unambiguous enclosing-function binding and validates outer writes before Go emission. | Use it when a nested function intentionally reassigns an outer local. |

Decorators are only parse-compatible on the same logical line as the
definition today, because the semicolon insertion pass may terminate a
standalone `@decorator` line before Lark sees the decorated form.

---

## Compiler diagnostics

Diagnostics come in two **severities** — borrowed straight from
Go's vocabulary:

- **Errors** abort the build. Anything that would later produce a
  Go compile error or a misleading runtime mistake belongs here.
- **Warnings** print to stderr but let transpilation proceed. They
  flag *advisory* issues you'd want to clean up, not correctness
  bugs.

The pipeline runs in three layers:

1. **Preprocessor** — the literal aliases `LAMMERGEIER.None` and
   `LAMMERGEIER.nil` are rewritten textually before parsing
   (they don't correspond to any Lam-level symbol). Every other
   `LAMMERGEIER.*` reference passes through untouched and is
   handled later by the dynamic dispatcher; see the
   [`LAMMERGEIER.*`](#lammergeier--stable-namespace-for-lam-visible-identifiers)
   section for the full lowering table.
2. **Parser** — Lark errors are line-tagged and printed with a
   three-line source snippet pointing at the offending token.
3. **Semantic checker** (runs on a successful parse) — emits both
   errors and warnings:
   - **Errors:** undefined names, duplicate class members,
     misplaced `return` / `break` / `continue`, `const` reassignment,
     missing/extra/unknown call arguments, typed collection element/key/value
     mismatches in declarations, reassignments, returns, call arguments,
     container writes and list helpers, and `LAMMERGEIER.<name>` typo
     detection across the user-defined namespace. Also: undefined names
     inside f-string slots (at the slot's line and column), unknown types in
     annotations and unknown base classes, non-`bool` conditions, constant
     division by zero, re-annotating an outer local with another type in an
     inner block, and untyped lambda parameters that nothing types (a
     stand-alone `sq = lambda x: x * x`, or `any` parameters of a
     `func(any) -> ...` callback) when the body needs a concrete type.
   - **Warnings:** unused top-level imports
     (`from lamX import Y` where `Y` is never referenced),
     unused function parameters
     (`func f(name, age)` where `age` is never used), unused locals
     declared in functions or block bodies, unused `catch` variables of
     `try`/`catch` and `do`/`catch` handlers, and project-level unused
     manifest dependencies (a
     `[dependencies]` entry in `lamlib.toml` that no `.lam` in
     the project tree imports).

   A function with a return type whose every path raises needs no `return`.
   Definite-assignment tracking joins continuing `if`/`match`/`try`
   branches (a local assigned in the `try` body and in every continuing
   `catch` body, even with an annotation in each, is assigned afterwards),
   ignores paths that return or raise, keeps `with ... as`
   resources block-local, and checks nested-function/lambda captures at
   their definition point. Statically non-empty literal/range loops preserve
   body assignments only when no `break` or `continue` can skip them; empty,
   unknown, or early-exit loops remain conservative. The Go emitter hoists
   valid joined locals so accepted Lam flow also compiles with Go block scope.

   Call shapes come from the resolved Lam declaration, including stdlib
   and package methods whose implementation body uses `go!`. Direct,
   aliased, module-qualified, constructor, static-method, and inferred
   instance-method calls therefore share the same checks. Truly raw Go or
   dynamic `any` values remain conservative fallbacks.

   Declared return types are checked for method calls, ternaries,
   comprehensions, inferred generic/operator results, and propagated `?`
   payloads. Dropping a known non-void local/imported/static/inferred-instance
   call emits a warning; `Result`/`Option` wording asks the caller to handle it
   or assign to `_`. A `?` outside a compatible Result-returning target remains
   a warning because the documented dynamic fallback converts `Err` to panic.

   Skip with `--no-semantic-check` when chasing a transpiler bug,
   but it's on by default because it's cheap (&lt;50 ms) and catches
   whole classes of "compiles but doesn't link" failures upfront.

The Pythonic **leading-underscore** opt-out applies to every
"unused" warning: rename a parameter `age` to `_age` (or just `_`)
to mark it as deliberately unused, and the warning disappears. The
same convention works for `for _ in xs`, `with file as _f`,
`catch SomeError as _e`, and `do { ... } catch _err { ... }`.

Unused **locals** follow the same warn-don't-error contract. The
semantic checker emits ``warning: unused local `name` `` for locals
declared in a function or block body and never read. Go normally
rejects those locals with a hard `declared and not used` error
pointing at generated code, so the transpiler also emits defensive
`_ = name` lines inside the affected scope. That keeps Go happy
while Lam still tells the author what to clean up. Prefix the name
with `_` to mark the unused local as intentional. `catch` variables
behave the same way (``warning: unused catch variable `err` ``);
mentioning a name inside a string literal does not count as using it.

The user-name resolver runs at go-block emit time, after the AST
walk has populated the symbol table, so `LAMMERGEIER.<userName>`
participates in did-you-mean suggestions alongside the static
aliases. See the
[`LAMMERGEIER.*`](#lammergeier--stable-namespace-for-lam-visible-identifiers)
section for the full lowering table.

The **language server** (`python -m compiler.lsp`) emits both parse
errors and semantic-checker findings as
`textDocument/publishDiagnostics` messages, so the same checks
(errors *and* warnings, mapped to LSP severities 1 and 2) appear
live in the editor's gutter while you type.

---

## Build performance

`lamc` aims to keep cold builds in the &lt;1 s range for typical
single-file programs:

- Library transpilation runs in parallel via a thread pool.
- The Lark parser is built once per process *and* persisted to disk
  (`~/.cache/lammergeier/parsers/<digest>.bin`) so the next process
  loads it in tens of milliseconds instead of rebuilding the LALR
  tables.
- Library outputs are content-addressed and reused across builds.
- `go mod tidy` runs only inside an isolated tmpdir, so it never
  touches the user's checkout.

Profile a build with `--verbose` to see which stage dominates.
