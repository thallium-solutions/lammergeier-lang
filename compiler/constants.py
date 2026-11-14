#!/usr/bin/env python3
"""Constants used across the transpiler modules."""

from typing import Dict, Set

# ─── Type mapping ──────────────────────────────────────────────
TYPE_MAP: Dict[str, str] = {
    "int": "int",
    "int8": "int8",
    "int16": "int16",
    "int32": "int32",
    "int64": "int64",
    "uint": "uint",
    "float": "float64",
    "float32": "float32",
    "float64": "float64",
    "string": "string",
    "str": "string",
    "bool": "bool",
    "byte": "byte",
    "rune": "rune",
    "None": "",
    "list": "[]interface{}",
    "dict": "map[string]interface{}",
    "set": "map[interface{}]bool",
    "json": "LamJSON",
    "html": "LamHTML",
    "xml": "LamXML",
    "any": "interface{}",
    "object": "interface{}",
    "error": "error",
    "bytes": "[]byte",
}

# Python exceptions → panic strings
PYTHON_EXCEPTIONS: Set[str] = {
    "ValueError", "TypeError", "KeyError", "IndexError",
    "RuntimeError", "AttributeError", "IOError", "OSError",
    "FileNotFoundError", "ZeroDivisionError", "OverflowError",
    "NotImplementedError", "StopIteration", "Exception",
    "ArithmeticError", "LookupError", "NameError",
}

# ``generator[T]`` spellings: a generator of ``T`` (a Go ``*lamGenerator[T]``,
# see ``GENERATOR_RUNTIME_GO``).
GENERATOR_TYPE_NAMES = frozenset({"generator", "iterator", "Generator", "Iterator"})

_GENERATOR_GO_PREFIX = "*lamGenerator["

# The parameter of a generator body a ``yield`` hands its value to.
GENERATOR_YIELD_NAME = "__lamYield"


def generator_go_type(elem: str) -> str:
    """Go type of a generator of ``elem`` values."""
    return f"{_GENERATOR_GO_PREFIX}{elem or 'interface{}'}]"


def generator_go_elem(go_type: str) -> str | None:
    """``T`` of a generator's Go type ``*lamGenerator[T]`` (``None`` for
    another type)."""
    go_type = (go_type or "").strip()
    if go_type.startswith(_GENERATOR_GO_PREFIX) and go_type.endswith("]"):
        return go_type[len(_GENERATOR_GO_PREFIX):-1].strip()
    return None


# The generator runtime, emitted once in the main file when some module uses
# a generator. A generator is a handle on the body of a function using
# ``yield``, compiled to a state machine (``compiler/visitors/generators.py``):
# the body is a continuation ``k`` (a closure over the body's locals) that
# runs to the next ``yield``, stores the value and the continuation of the
# rest in the frame and returns. Nothing runs before the first value is
# asked for, and a suspended generator is plain data (no goroutine holds
# it), so a generator dropped in a cycle with what it uses is collected.
# ``Next`` runs the continuations in a trampoline (``lamGenRun``), under the
# handle's mutex (concurrent consumers share the values); an exception the
# body raises outside its ``try`` regions ends the generator and reaches
# the consumer, one inside a region goes to the region's handler (``h``).
# Ending a generator before it is exhausted (``Stop`` / ``Close``: a loop
# over a generator call left early, ``g.close()``; the cleanup of a
# collected handle) runs the close continuation ``c`` of the state it is
# suspended in: its pending ``finally`` blocks and ``with`` closes, like
# Python's ``GeneratorExit`` (a ``yield`` reached meanwhile ends the body).
# The cleanup holds only that continuation (``lamGenClose``), never the
# handle. A ``close()`` finding the generator busy (a step of the body
# running — the body itself may be calling it — or its close path) does
# not wait: whoever holds the mutex ends it when it unlocks (``closeReq``).
#
# A method generator whose close path uses its receiver
# (``lamNewMethodGenerator``) reads the receiver from the frame (``r``) in
# every continuation, so no continuation captures it: the cleanup reaches
# it only through ``lamGenRecv``, which holds it strongly, or weakly while
# the generator is stored in a field of that object (``lamGenStore``, at
# every store of a generator into a field). A dropped object and the
# generator it holds are then collected together, and the cleanup, which
# finds the receiver gone, reports the ``finally`` it cannot run.
#
# A body the state machine does not lower (see
# ``_generator_sm_unsupported``) keeps the coroutine lowering: a
# range-over-func body ``func(yield func(T) bool)`` run in place by a loop
# over the generator call (``Consume``) unless a ``catch`` (or a ``finally``
# that returns or jumps) around a ``yield`` could intercept an exception of
# the loop body (``lamNewIsolatedGenerator``), otherwise pulled from a
# coroutine (``iter.Pull``) started on the first ``Next`` and stopped when
# the generator ends or its handle is collected.
GENERATOR_RUNTIME_GO = """
const (
\tlamGenJump  = 0
\tlamGenYield = 1
\tlamGenDone  = 2
)

// lamGenFrame holds the registers of a state machine generator: the
// continuation to run next, the close continuation of the state it is
// suspended in, the handler of the try region running, the value yielded,
// the receiver of a method generator reading it from the frame.
type lamGenFrame[T any] struct {
\tk       func(*lamGenFrame[T]) int
\tc       func(*lamGenFrame[T]) int
\th       func(*lamGenFrame[T], any)
\tv       T
\tr       any
\tclosing bool
}

// lamGenClose is what the cleanup of a collected generator runs.
type lamGenClose[T any] struct {
\tc    func(*lamGenFrame[T]) int
\tself lamGenSelf
}

// lamGenSelf is the receiver of a method generator whose close path uses
// it, as the cleanup sees it (``lamGenRecv``).
type lamGenSelf interface {
\trecv() any
\thold(owner any, delta int)
\tname() string
}

// lamGenRecv holds the receiver strongly, or weakly while the generator is
// stored in fields of the receiver's object (``held`` of them).
type lamGenRecv[R any] struct {
\tmu   sync.Mutex
\tn    string
\tp    *R
\tw    weak.Pointer[R]
\theld int
}

func (r *lamGenRecv[R]) recv() any {
\tr.mu.Lock()
\tdefer r.mu.Unlock()
\tif r.p != nil {
\t\treturn r.p
\t}
\tif p := r.w.Value(); p != nil {
\t\treturn p
\t}
\treturn nil
}

func (r *lamGenRecv[R]) name() string { return r.n }

// hold counts a store (``delta`` 1) or a removal (-1) of the generator in
// a field of ``owner``; it only counts when ``owner`` is the receiver's object.
func (r *lamGenRecv[R]) hold(owner any, delta int) {
\tr.mu.Lock()
\tdefer r.mu.Unlock()
\tcur := r.p
\tif cur == nil {
\t\tcur = r.w.Value()
\t}
\tif cur == nil || (delta < 0 && r.held == 0) || lamGenDyn(cur) != lamGenDyn(owner) {
\t\treturn
\t}
\tr.held += delta
\tif r.held > 0 && r.p != nil {
\t\tr.w, r.p = weak.Make(r.p), nil
\t} else if r.held == 0 {
\t\tr.p, r.w = cur, weak.Pointer[R]{}
\t}
}

// lamGenDyn is the most-derived object of a class instance.
func lamGenDyn(v any) any {
\tif d, ok := v.(interface{ _lamDyn() interface{} }); ok {
\t\treturn d._lamDyn()
\t}
\treturn v
}

// lamGenStore stores a generator in a field (``*slot``) of ``owner``.
func lamGenStore[T any](slot **lamGenerator[T], v *lamGenerator[T], owner any) {
\told := *slot
\t*slot = v
\tif old == v {
\t\treturn
\t}
\tif old != nil && old.self != nil {
\t\told.self.hold(owner, -1)
\t}
\tif v != nil && v.self != nil {
\t\tv.self.hold(owner, 1)
\t}
}

type lamGenerator[T any] struct {
\tmu       sync.Mutex
\tf        lamGenFrame[T]
\tsm       bool
\tpending  *lamGenClose[T]
\tself     lamGenSelf
\tbody     func(yield func(T) bool)
\tisolated bool
\tnext     func() (T, bool)
\tstop     func()
\tcleanup  runtime.Cleanup
\t// closeReq: a close() found the generator busy (a step of its body, or
\t// its close path, maybe calling it): whoever holds mu ends it on unlock.
\tcloseReq atomic.Bool
\tdone     bool
\t// owned: a loop owns the generator and ends it however it is left (no
\t// cleanup needed); claimed: a loop over the call resumes it without the mutex.
\towned   bool
\tclaimed bool
}

func lamNewStateGenerator[T any](k func(*lamGenFrame[T]) int) *lamGenerator[T] {
\treturn &lamGenerator[T]{f: lamGenFrame[T]{k: k}, sm: true}
}

// lamNewMethodGenerator is a method generator whose close path uses its
// receiver: the continuations read it from the frame.
func lamNewMethodGenerator[T, R any](recv *R, name string, k func(*lamGenFrame[T]) int) *lamGenerator[T] {
\treturn &lamGenerator[T]{f: lamGenFrame[T]{k: k, r: recv}, sm: true, self: &lamGenRecv[R]{n: name, p: recv}}
}

func lamNewGenerator[T any](body func(yield func(T) bool)) *lamGenerator[T] {
\treturn &lamGenerator[T]{body: body}
}

func lamNewIsolatedGenerator[T any](body func(yield func(T) bool)) *lamGenerator[T] {
\treturn &lamGenerator[T]{body: body, isolated: true}
}

// lamGenEnd is the continuation ending the body.
func lamGenEnd[T any](*lamGenFrame[T]) int { return lamGenDone }

// lamGenResume ends a finally block: it continues where the code leaving
// the try region was going, or re-raises the exception that left it.
func lamGenResume[T any](f *lamGenFrame[T], pend *func(*lamGenFrame[T]) int, exc *any) int {
\tp, e := *pend, *exc
\t*pend, *exc = nil, nil
\tif p == nil {
\t\tpanic(e)
\t}
\tf.k = p
\treturn lamGenJump
}

// lamGenMapKeys snapshots the keys a loop holding a yield iterates over.
func lamGenMapKeys[K comparable, V any](m map[K]V) []K {
\tkeys := make([]K, 0, len(m))
\tfor k := range m {
\t\tkeys = append(keys, k)
\t}
\treturn keys
}

// lamGenRun runs continuations up to the next value or the end of the body.
func lamGenRun[T any](f *lamGenFrame[T]) int {
\tfor {
\t\tif f.h == nil {
\t\t\tif c := f.k(f); c != lamGenJump {
\t\t\t\treturn c
\t\t\t}
\t\t} else if c, ok := lamGenGuarded(f); ok && c != lamGenJump {
\t\t\treturn c
\t\t}
\t}
}

// lamGenGuarded runs a continuation of a try region: an exception goes to
// the region's handler, which selects the continuation handling it.
func lamGenGuarded[T any](f *lamGenFrame[T]) (c int, ok bool) {
\tdefer func() {
\t\tif !ok {
\t\t\tif r := recover(); r != nil {
\t\t\t\tr = lamExc(r)
\t\t\t\th := f.h
\t\t\t\tif h == nil {
\t\t\t\t\tpanic(r)
\t\t\t\t}
\t\t\t\th(f, r)
\t\t\t}
\t\t}
\t}()
\treturn f.k(f), true
}

// Next resumes the generator up to its next value.
func (g *lamGenerator[T]) Next() (v T, more bool) {
\tif g == nil {
\t\treturn v, false
\t}
\tg.mu.Lock()
\tif g.done || !g.sm || g.closeReq.Load() {
\t\treturn g.nextSlow()
\t}
\tstepped := false
\tdefer func() {
\t\tif !stepped {
\t\t\t// The body raised: the exception reaches the consumer.
\t\t\tg.finish()
\t\t}
\t\tg.unlock()
\t}()
\tv, more = g.step()
\tstepped = true
\treturn v, more
}

// nextSlow is Next of an ended generator, of a coroutine generator, or
// after a close() arrived while this consumer waited (with g.mu held).
func (g *lamGenerator[T]) nextSlow() (v T, more bool) {
\tdefer g.unlock()
\tif g.closeReq.Swap(false) {
\t\tg.end()
\t}
\tif g.done || g.sm {
\t\treturn v, false
\t}
\treturn g.pull()
}

// unlock releases g.mu, then ends the generator if a close() found it busy.
func (g *lamGenerator[T]) unlock() {
\tg.mu.Unlock()
\tif g.closeReq.Load() {
\t\tg.closeRequested()
\t}
}

func (g *lamGenerator[T]) closeRequested() {
\tif !g.mu.TryLock() {
\t\t// The goroutine now holding mu ends it when it unlocks.
\t\treturn
\t}
\tdefer g.unlock()
\tif g.closeReq.Swap(false) {
\t\tg.end()
\t}
}

// pull resumes a coroutine generator (with g.mu held).
func (g *lamGenerator[T]) pull() (T, bool) {
\tif g.next == nil {
\t\tg.next, g.stop = iter.Pull(g.body)
\t\tg.body = nil
\t\t// Dropped before it is exhausted: stopped once collected.
\t\tg.cleanup = runtime.AddCleanup(g, lamStopCollectedGenerator, g.stop)
\t}
\tmore := false
\tdefer func() {
\t\tif !more {
\t\t\tg.finish()
\t\t}
\t}()
\tv, more := g.next()
\treturn v, more
}

// step runs the state machine to its next value (with g.mu held, or by
// the loop owning the generator); the generator ends with its body. The
// caller ends it when the body raises.
func (g *lamGenerator[T]) step() (v T, more bool) {
\tif lamGenRun(&g.f) != lamGenYield {
\t\tg.finish()
\t\treturn v, false
\t}
\tv = g.f.v
\tif g.owned {
\t\treturn v, true
\t}
\tvar zero T
\tg.f.v = zero
\tif c := g.f.c; c != nil {
\t\tif g.pending == nil {
\t\t\t// Dropped while suspended in a try region: closed once collected.
\t\t\tg.pending = &lamGenClose[T]{self: g.self}
\t\t\tg.cleanup = runtime.AddCleanup(g, lamCloseCollectedGenerator[T], g.pending)
\t\t}
\t\tg.pending.c = c
\t} else if g.pending != nil {
\t\tg.pending.c = nil
\t}
\treturn v, true
}

// finish ends the generator (with g.mu held); a suspended coroutine body leaves its yield.
func (g *lamGenerator[T]) finish() {
\tg.done = true
\tg.body = nil
\tif g.sm {
\t\tg.f = lamGenFrame[T]{}
\t\tif g.pending != nil {
\t\t\tg.cleanup.Stop()
\t\t\tg.pending.c = nil
\t\t\tg.pending = nil
\t\t}
\t\treturn
\t}
\tif stop := g.stop; stop != nil {
\t\tg.cleanup.Stop()
\t\tg.next, g.stop = nil, nil
\t\tstop()
\t}
}

// Close ends the generator (``g.close()``): a suspended body runs its
// pending finally blocks and with closes, whose exception reaches the
// caller; an ended generator is left as is.
func (g *lamGenerator[T]) Close() { g.Stop() }

// Stop ends the generator: a suspended body runs its pending finally
// blocks and with closes. Called while the generator is busy — a step of
// its body runs (the body itself may be calling it) or its close path does
// — it does not wait: the step ends the generator once its value is
// produced (``unlock``), and a close already running finishes the job.
func (g *lamGenerator[T]) Stop() {
\tif g == nil {
\t\treturn
\t}
\tif !g.mu.TryLock() {
\t\tg.closeReq.Store(true)
\t\tif !g.mu.TryLock() {
\t\t\treturn
\t\t}
\t\tg.closeReq.Store(false)
\t}
\tdefer g.unlock()
\tg.end()
}

// end runs the close path of the state the generator is suspended in and
// ends it (with g.mu held).
func (g *lamGenerator[T]) end() {
\tif g.done {
\t\treturn
\t}
\tdefer g.finish()
\tif c := g.f.c; g.sm && c != nil {
\t\tg.f.k, g.f.c, g.f.h, g.f.closing = c, nil, nil, true
\t\tlamGenRun(&g.f)
\t}
}

// Resume ranges over a generator held in a variable: from where the last
// loop stopped, leaving it resumable.
func (g *lamGenerator[T]) Resume(yield func(T) bool) {
\tfor {
\t\tv, ok := g.Next()
\t\tif !ok || !yield(v) {
\t\t\treturn
\t\t}
\t}
}

// Consume ranges over a generator the loop owns (``for x in gen()``):
// leaving the loop in any way ends it.
func (g *lamGenerator[T]) Consume(yield func(T) bool) {
\tif g == nil {
\t\treturn
\t}
\tif g.sm {
\t\tg.consume(yield)
\t\treturn
\t}
\tg.mu.Lock()
\tbody := g.body
\tif body == nil || g.isolated || g.next != nil || g.done {
\t\tg.unlock()
\t\tdefer g.Stop()
\t\tg.Resume(yield)
\t\treturn
\t}
\t// Not started: the body runs in place, to its end.
\tg.body, g.done = nil, true
\tg.unlock()
\tstopped := false
\tbody(func(v T) bool {
\t\tif stopped {
\t\t\treturn false
\t\t}
\t\t// Stays stopped when the loop body breaks, returns or raises.
\t\tstopped = true
\t\tif yield(v) {
\t\t\tstopped = false
\t\t}
\t\treturn !stopped
\t})
}

// own marks a generator call's handle as owned by the loop of a state
// machine body iterating it, which stops it when the loop is left.
func (g *lamGenerator[T]) own() *lamGenerator[T] {
\tg.mu.Lock()
\tg.owned = true
\tg.unlock()
\treturn g
}

// consume runs a state machine generator the loop owns: a generator call's
// fresh handle, which nothing else can reach, resumed without its mutex.
func (g *lamGenerator[T]) consume(yield func(T) bool) {
\tg.mu.Lock()
\towned := !g.done && !g.claimed
\tg.claimed, g.owned = true, true
\tg.unlock()
\tif !owned {
\t\tdefer g.Stop()
\t\tg.Resume(yield)
\t\treturn
\t}
\tinBody := false
\tdefer func() {
\t\tif inBody {
\t\t\t// The body raised: the exception reaches the loop.
\t\t\tg.finish()
\t\t} else if !g.done {
\t\t\t// The loop was left early.
\t\t\tg.Stop()
\t\t}
\t}()
\tfor {
\t\tinBody = true
\t\tv, more := g.step()
\t\tinBody = false
\t\tif !more || !yield(v) {
\t\t\treturn
\t\t}
\t}
}

func lamStopCollectedGenerator(stop func()) {
\tdefer func() {
\t\tif r := recover(); r != nil {
\t\t\tfmt.Fprintf(os.Stderr, "Exception ignored in generator cleanup: %v\\n", r)
\t\t}
\t}()
\tstop()
}

// lamCloseCollectedGenerator ends a generator collected while suspended in
// a try region, on a fresh frame (the handle is gone). A method generator
// collected with the object it was stored in cannot run its close path,
// which needs that object: it is reported.
func lamCloseCollectedGenerator[T any](p *lamGenClose[T]) {
\tc := p.c
\tif c == nil {
\t\treturn
\t}
\tp.c = nil
\tvar r any
\tif p.self != nil {
\t\tif r = p.self.recv(); r == nil {
\t\t\tfmt.Fprintf(os.Stderr, "Exception ignored in generator %s: its finally uses the object holding it, "+
\t\t\t\t"collected with it, so it did not run; call close() before dropping the object\\n", p.self.name())
\t\t\treturn
\t\t}
\t}
\tdefer func() {
\t\tif r := recover(); r != nil {
\t\t\tfmt.Fprintf(os.Stderr, "Exception ignored in generator cleanup: %v\\n", r)
\t\t}
\t}()
\tlamGenRun(&lamGenFrame[T]{k: c, closing: true, r: r})
}
"""

# Statement-level AST node names (used for source-line mapping)
STMT_NODES = frozenset({
    'funcdef', 'classdef', 'if_stmt', 'for_stmt', 'while_stmt',
    'assign_stmt', 'annassign', 'const_stmt', 'expr_stmt',
    'return_stmt', 'try_stmt', 'do_stmt', 'with_stmt',
    'assert_stmt', 'raise_stmt', 'del_stmt', 'break_stmt',
    'continue_stmt', 'pass_stmt', 'match_stmt',
    'import_from', 'import_name', 'defer_stmt',
})

# Map Python operator tokens to dunder method names
OP_TO_DUNDER = {
    "+": "__add__", "-": "__sub__", "*": "__mul__",
    "/": "__truediv__", "//": "__floordiv__", "%": "__mod__",
    "**": "__pow__",
    "&": "__and__", "|": "__or__", "^": "__xor__",
}

CMP_TO_DUNDER = {
    "==": "__eq__", "!=": "__ne__",
    "<": "__lt__", "<=": "__le__",
    ">": "__gt__", ">=": "__ge__",
}

# Operator overloading dunder-to-Go mappings (used in funcdef visitor)
DUNDER_OPS = {
    "__add__": "Add", "__sub__": "Sub", "__mul__": "Mul",
    "__truediv__": "Div", "__floordiv__": "FloorDiv",
    "__mod__": "Mod", "__pow__": "Pow",
    "__eq__": "Eq", "__ne__": "Ne",
    "__lt__": "Lt", "__le__": "Le",
    "__gt__": "Gt", "__ge__": "Ge",
    "__neg__": "Neg", "__abs__": "Abs",
    "__and__": "And", "__or__": "Or", "__xor__": "Xor",
    "__invert__": "Invert",
    "__contains__": "Contains",
    "__getitem__": "GetItem", "__setitem__": "SetItem",
    # Postfix ``x++`` / ``x--``. The methods must return the
    # updated value because the transpiler rewrites the statement
    # to ``x = x.__inc__()`` / ``x = x.__dec__()`` — reusing the
    # same value-returning shape as the other overloads keeps
    # method emission uniform (no special void-returning path).
    "__inc__": "Inc", "__dec__": "Dec",
}

# Go stdlib packages that keep lowercase naming
LOWER_PKGS = frozenset({
    "fmt", "math", "os", "strconv", "errors", "strings",
    "io", "bufio", "sort", "time", "sync", "log",
    "path", "filepath", "net", "http", "json",
    "encoding", "bytes", "reflect", "runtime",
})

# Go package identifiers the compiler's own lowerings may import into a
# generated file (``_need_import``). A Lam local, parameter or module
# variable spelled like one of them gets a ``__lam`` suffix in Go so it
# cannot hide the package the lowering relies on.
COMPILER_GO_PACKAGES = frozenset({
    "fmt", "reflect", "strings", "math", "slices", "os", "strconv", "sort",
    "runtime", "maps", "bufio", "weak",
})

# Package-level Go functions, types and variables the compiler itself emits
# into generated programs (the runtime of generators, async tasks, ``None``
# checks, ``as`` casts and markup slots). A Lam name that keeps its spelling
# in Go (a private function, a const, a variable, a local or a parameter)
# must not be one of them (semantic error); ``tests/tests/test_go_names.py``
# checks the list against the compiler's sources. The prefixes are those of
# generated names (function-reference aliases, module initialisers, the
# virtual-dispatch interfaces).
COMPILER_RUNTIME_HELPERS = frozenset({
    # Run-time faults and the uncaught-exception report (docs/SAFETY.md).
    "lamExc", "lamExcKindOf", "lamFault", "lamFaultArticle", "lamFaultConversion", "lamFaultFrames",
    "lamFaultGoType", "lamFaultText", "lamFloatToInt", "lamIPow", "lamRangeStep", "lamStdlibDir",
    "lamUncaught",
    "lamAs", "lamAsNil", "lamAsyncFailure", "lamAsyncFailureIDs", "lamAsyncFailures",
    "lamAsyncReportFailure", "lamAsyncReportUnretrieved", "lamAsyncTaskFailed",
    "lamAsyncTaskRetrieved", "lamAsyncTaskUnpublished", "lamCloseCollectedGenerator",
    "lamGenClose", "lamGenDone", "lamGenDyn", "lamGenEnd", "lamGenerator", "lamGenFrame", "lamGenGuarded",
    "lamGenJump", "lamGenMapKeys", "lamGenRecv", "lamGenResume", "lamGenRun", "lamGenSelf", "lamGenStore",
    "lamGenYield", "lamGoTypeLamName", "lamNewGenerator", "lamNewIsolatedGenerator",
    "lamNewMethodGenerator", "lamNewStateGenerator", "lamNN", "lamNNI", "lamNoneError",
    "lamNoneIsNot", "lamNoneMember", "lamNoneOf", "lamSlotText", "lamStopCollectedGenerator",
})
COMPILER_RUNTIME_HELPER_PREFIXES: tuple[str, ...] = ("lamFuncRef_", "lamModuleInit_", "lamVirtual")

# Go builtins that should not be CamelCased
GO_BINDING_KEYWORDS = frozenset({
    "break", "default", "func", "interface", "select", "case", "defer",
    "go", "map", "struct", "chan", "else", "goto", "package", "switch",
    "const", "fallthrough", "if", "range", "type", "continue", "for",
    "import", "return", "var", "iota", "any",
})

GO_BUILTINS = frozenset({
    "len", "cap", "make", "new", "append", "copy", "delete",
    "close", "panic", "recover", "print", "println",
    "int", "float64", "string", "bool", "byte", "rune",
    "fmt", "math", "os", "strconv", "errors", "strings",
})


# Methods callable on a ``str`` value (lowered to ``lamstrings``) and on a
# ``list[T]`` value (lowered inline). Anything else is a semantic error.
STR_METHODS: Set[str] = {
    "repeat", "contains", "hasPrefix", "hasSuffix", "toUpper", "toLower", "trim",
    "length", "trimLeft", "trimRight", "replace", "split", "join", "startsWith",
    "endsWith", "index", "lastIndex", "count", "title", "equalFold", "fields",
    "capitalize", "isAlpha", "isDigit", "isAlnum", "isSpace", "reverse", "center",
    "zfill", "padLeft", "padRight", "splitLines", "splitN", "replaceFirst",
    "containsAny", "isEmpty", "isBlank", "indent", "dedent", "format",
    "camelCase", "pascalCase", "snakeCase", "kebabCase",
}
# Result types of the ``str`` methods (everything else returns ``str``).
STR_METHOD_RESULT_TYPES: Dict[str, str] = {
    "split": "list[str]", "fields": "list[str]", "splitLines": "list[str]", "splitN": "list[str]",
    "contains": "bool", "hasPrefix": "bool", "hasSuffix": "bool", "startsWith": "bool",
    "endsWith": "bool", "equalFold": "bool", "isAlpha": "bool", "isDigit": "bool",
    "isAlnum": "bool", "isSpace": "bool", "containsAny": "bool", "isEmpty": "bool",
    "isBlank": "bool", "length": "int", "index": "int", "lastIndex": "int", "count": "int",
}
LIST_METHODS: Set[str] = {
    "length", "append", "pop", "extend", "insert", "sort", "map", "filter",
    "reduce", "any", "all", "foreach",
}
# Python spellings with the Lam method (or idiom) they correspond to.
STR_METHOD_HINTS: Dict[str, str] = {
    "find": "index", "rfind": "lastIndex", "rindex": "lastIndex", "upper": "toUpper",
    "lower": "toLower", "strip": "trim", "lstrip": "trimLeft", "rstrip": "trimRight",
    "startswith": "startsWith", "endswith": "endsWith", "splitlines": "splitLines",
    "isalpha": "isAlpha", "isdigit": "isDigit", "isalnum": "isAlnum", "isspace": "isSpace",
    "casefold": "toLower", "len": "length", "size": "length",
    # JavaScript / Java spellings.
    "indexOf": "index", "lastIndexOf": "lastIndex", "toUpperCase": "toUpper",
    "toLowerCase": "toLower", "includes": "contains", "trimStart": "trimLeft",
    "trimEnd": "trimRight", "replaceAll": "replace", "padStart": "padLeft",
    "padEnd": "padRight",
    "toCamelCase": "camelCase", "toSnakeCase": "snakeCase", "toKebabCase": "kebabCase",
    "toPascalCase": "pascalCase",
}
# Methods other languages have on strings that Lam spells as an operator,
# slicing or indexing. ``{s}`` is the receiver, ``{a}``/``{b}`` the first
# two arguments (rendered by ``str_method_idiom``).
STR_METHOD_IDIOMS: Dict[str, str] = {
    "substring": "slicing `{s}[{a}:{b}]`",
    "substr": "slicing `{s}[{a}:{a} + {b}]` (`substr` takes a length)",
    "slice": "slicing `{s}[{a}:{b}]`",
    "charAt": "indexing `{s}[{a}]`",
    "at": "indexing `{s}[{a}]`",
    "concat": "`{s} + {a}`",
    "equals": "`{s} == {a}`",
    "toString": "the value directly (it is already a `str`)",
    "chars": "`for c in {s}`",
}
_STR_IDIOM_PLACEHOLDERS: Dict[str, tuple] = {
    "substring": ("start", "end"), "slice": ("start", "end"), "substr": ("start", "length"),
    "charAt": ("i", ""), "at": ("i", ""), "concat": ("other", ""), "equals": ("other", ""),
}


def lam_simple_expr_text(node) -> str:
    """Lam source text of a short argument/receiver node (a name, a dotted
    name, a number or a short string literal), or ``""`` when the node is
    not that simple. Works on Lark trees without importing Lark."""
    data = getattr(node, "data", None)
    children = list(getattr(node, "children", []) or [])
    if data is None:
        return str(node) if node is not None else ""
    if data in {"var", "name", "number"} and children:
        return lam_simple_expr_text(children[0])
    if data == "argvalue" and len(children) == 1:
        return lam_simple_expr_text(children[0])
    if data in {"getattr", "getattr_safe"} and len(children) >= 2:
        prefix = lam_simple_expr_text(children[0])
        attr = lam_simple_expr_text(children[1])
        sep = "?." if data == "getattr_safe" else "."
        return f"{prefix}{sep}{attr}" if prefix and attr else ""
    if data == "string" and children:
        text = str(children[0])
        return text if len(text) <= 24 and "\n" not in text else ""
    if data == "getitem" and len(children) == 2:
        receiver, index = lam_simple_expr_text(children[0]), lam_simple_expr_text(children[1])
        return f"{receiver}[{index}]" if receiver and index else ""
    if data == "propagate" and children:
        inner = lam_simple_expr_text(children[0])
        return f"{inner}?" if inner else ""
    if data == "factor" and len(children) == 2 and getattr(children[0], "data", None) is None:
        inner = lam_simple_expr_text(children[1])
        return f"{children[0]}{inner}" if inner else ""
    if data == "funccall" and children:
        # ``Json.encode(v)``: a call whose callee and arguments are simple.
        callee = lam_simple_expr_text(children[0])
        args_node = children[1] if len(children) > 1 else None
        if args_node is None:
            arg_nodes = []
        elif getattr(args_node, "data", None) == "arguments":
            arg_nodes = [arg for arg in args_node.children if arg is not None]
        else:
            arg_nodes = [args_node]
        arg_texts = [lam_simple_expr_text(arg) for arg in arg_nodes]
        if not callee or not all(arg_texts):
            return ""
        return f"{callee}({', '.join(arg_texts)})"
    return ""


def str_index_byte_hint(index_text: str, method: str = "", has_args: bool = False) -> str:
    """Suffix for a ``byte`` from indexing a ``str`` (``w[0]``) used as a
    ``str``: the documented ``string(w[0])`` conversion to a one-character
    ``str`` (with the method call for ``w[0].toUpper()``). Shared by the
    semantic checker and the transpiler (imported modules)."""
    converted = f"string({index_text or 's[i]'})"
    if method:
        converted += f".{method}({'...' if has_args else ''})"
    return f"; indexing a `str` gives a `byte`: use `{converted}` for a one-character `str`"


def str_method_idiom(method: str, receiver: str = "", args=None) -> str:
    """The Lam spelling of a string method other languages have
    (``label.substring(0, 2)`` -> slicing ``label[0:2]``), or ``""``."""
    template = STR_METHOD_IDIOMS.get(method)
    if not template:
        return ""
    args = list(args or [])
    first, second = _STR_IDIOM_PLACEHOLDERS.get(method, ("x", "y"))
    a = args[0] if args and args[0] else first
    b = args[1] if len(args) > 1 and args[1] else second
    s = receiver or "s"
    if method in {"substring", "slice", "substr"} and len(args) == 1 and args[0]:
        return f"slicing `{s}[{a}:]`"
    if method == "substr" and a == "0":
        return f"slicing `{s}[0:{b}]`"
    return template.format(s=s, a=a, b=b)


def unknown_str_method_message(method: str, receiver: str = "", args=None) -> str:
    """Diagnostic for a method a ``str`` does not have, with the Lam idiom
    or the closest Lam method as a suggestion. Shared by the semantic
    checker and the transpiler (which reports it for imported modules)."""
    from difflib import get_close_matches

    idiom = str_method_idiom(method, receiver, args)
    if idiom:
        suffix = f"; use {idiom}"
    else:
        hint = STR_METHOD_HINTS.get(method)
        if not hint:
            matches = get_close_matches(method, sorted(STR_METHODS), n=1, cutoff=0.75)
            hint = matches[0] if matches else ""
        suffix = f" — did you mean `{hint}`?" if hint else ""
        if method in {"find", "rfind"}:
            suffix = f" — did you mean `{hint}` (returns -1 when absent)?"
    return f"`str` has no method `{method}`{suffix}"


# ``(min, max)`` arguments of each ``str`` method at the call site (the
# receiver excluded; ``None`` = variadic), matching ``lib/lamstrings.lam``
# and the lowering defaults (``trimLeft()`` trims ASCII whitespace,
# ``center`` / ``padLeft`` / ``padRight`` fill with a space).
STR_METHOD_ARITY: Dict[str, tuple] = {
    "repeat": (1, 1), "contains": (1, 1), "hasPrefix": (1, 1), "hasSuffix": (1, 1),
    "toUpper": (0, 0), "toLower": (0, 0), "trim": (0, 0), "length": (0, 0),
    "trimLeft": (0, 1), "trimRight": (0, 1), "replace": (2, 2), "split": (1, 1),
    "join": (1, 1), "startsWith": (1, 1), "endsWith": (1, 1), "index": (1, 1),
    "lastIndex": (1, 1), "count": (1, 1), "title": (0, 0), "equalFold": (1, 1),
    "fields": (0, 0), "capitalize": (0, 0), "isAlpha": (0, 0), "isDigit": (0, 0),
    "isAlnum": (0, 0), "isSpace": (0, 0), "reverse": (0, 0), "center": (1, 2),
    "zfill": (1, 1), "padLeft": (1, 2), "padRight": (1, 2), "splitLines": (0, 0),
    "splitN": (2, 2), "replaceFirst": (2, 2), "containsAny": (1, 1), "isEmpty": (0, 0),
    "isBlank": (0, 0), "indent": (1, 1), "dedent": (0, 0), "format": (0, None),
    "camelCase": (0, 0), "pascalCase": (0, 0), "snakeCase": (0, 0), "kebabCase": (0, 0),
}


def await_any_message(operand: str) -> str:
    """Diagnostic for ``await`` on a value typed ``any`` (the handle's
    payload type is lost, so the generated Go cannot receive from it)."""
    subject = f"`{operand}`" if operand else "the operand"
    return (
        f"`await` needs the handle returned by an async call, but {subject} is `any`; "
        "keep the handles in an unannotated local or list (`tasks = [load(1), load(2)]`) "
        "so their type is known, or await the call directly"
    )


def any_operand_message(subject: str, action: str, target: str) -> str:
    """Diagnostic for an operation that needs a concrete type applied to a
    value whose static type is ``any`` (``row["n"] + 1``, ``v.name``,
    ``for x in v``): Go rejects it on ``interface{}``. ``target`` is the
    suggested type for the fix-its (``int``, ``list[any]``, a class)."""
    expr = subject or "value"
    shown = f"`{subject}`" if subject else "this value"
    return (
        f"{shown} has type `any`, so {action} needs a concrete type; "
        f"cast it inline (`{expr} as {target}`), annotate a variable (`value: {target} = {expr}`), "
        "convert between kinds (`int(x)`, `float(x)`, `str(x)`) "
        "or narrow it with `isinstance` / `match`"
    )


def str_method_arity_message(method: str, given: int) -> str:
    """Diagnostic for a ``str`` method called with the wrong number of
    arguments (``""`` when the count fits)."""
    bounds = STR_METHOD_ARITY.get(method)
    if bounds is None:
        return ""
    low, high = bounds
    if given >= low and (high is None or given <= high):
        return ""
    if high is None:
        expected = f"at least {low}"
    elif low == high:
        expected = str(low)
    else:
        expected = f"{low} to {high}"
    noun = "argument" if expected == "1" else "arguments"
    message = f"`str.{method}()` takes {expected} {noun}, got {given}"
    if method == "replace" and given == 3:
        message += " (it replaces every occurrence; use `replaceFirst(old, new)` for the first one only)"
    elif method == "split" and given == 2:
        message += " (use `splitN(sep, n)` for a bounded split)"
    elif method == "split" and given == 0:
        message += " (use `fields()` to split on whitespace)"
    return message


# Python builtins Lam spells differently: the idiom an undefined-name error
# suggests (``all(x > 0 for x in xs)``).
PYTHON_BUILTIN_IDIOMS: Dict[str, str] = {
    "all": "the list method `xs.all(lambda x: x > 0)` (`flags.all(lambda b: b)` on a `list[bool]`)",
    "any": "the list method `xs.any(lambda x: x > 0)` (`flags.any(lambda b: b)` on a `list[bool]`)",
    "sum": "`xs.reduce(lambda a, b: a + b, 0)`",
}
LIST_METHOD_HINTS: Dict[str, str] = {
    "add": "append", "push": "append", "size": "length", "len": "length",
    "forEach": "foreach", "each": "foreach", "some": "any", "every": "all",
    "sorted": "sort",
}
LIST_METHOD_IDIOMS: Dict[str, str] = {
    "contains": "`x in xs`", "index": "a loop (or `x in xs`)", "count": "a loop or `len(xs.filter(...))`",
    "remove": "`xs = xs.filter(lambda v: v != x)`", "reverse": "`xs[::-1]`", "copy": "`xs[:]`",
    "clear": "`xs = []`", "join": "`\", \".join(xs)` on a `list[str]`",
    "first": "`xs[0]`", "last": "`xs[-1]`", "isEmpty": "`len(xs) == 0`",
}
