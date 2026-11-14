#!/usr/bin/env python3
"""Type-aware program fuzzer for the Lam compiler.

``lamfuzz`` generates well-typed Lam programs from a seed, together with a
Python *twin* of every program: the same statements rendered as Python with a
small runtime (``PRELUDE``) that reproduces the documented Lam semantics where
they differ from Python (Go value formatting, 64-bit ints, a missing dict key
read as the zero value, ...).  For each program:

* ``lamc build`` must succeed (the generator only writes valid programs), so
  a semantic diagnostic is a generator bug or a checker false positive, and a
  Go build failure or a compiler traceback is a compiler bug;
* the binary must exit with status 0 and print exactly what the Python twin
  prints (the oracle).

Modes (``--mode``):

``gen``     generate, build, run and compare (the default).
``mutate``  derive near-valid programs from generated ones (token and line
            edits): ``lamc`` must accept them (and then Go must build) or
            reject them with a Lam diagnostic, never a Go build error or a
            Python traceback.
``bytes``   hostile inputs (random bytes, truncations, deep nesting, huge
            literals, BOM, CRLF, unicode): a clean diagnostic or a build,
            never a traceback.

Every case is deterministic by ``(seed, index)``.  Failures are shrunk (a
generated program by dropping whole units, other inputs line by line) and
written to ``--out`` (default ``fuzz-failures/``).

Run ``python3 tests/fuzz/lamfuzz.py --help`` or ``sh scripts/fuzz.sh``.

Constructs whose Lam semantics differ from Python and how the twin models
them (docs/SAFETY.md, "Fuzzing"): printing (Go ``%v`` formatting: ``true``,
``[1 2]``, ``map[a:1]``, ``1e+06``), ints (64-bit: a program whose twin
leaves the int64 range is discarded and regenerated), float constants (Go
folds constant expressions exactly, so float literals are never combined
with each other), dict and set iteration order (unspecified: only consumed
by order-insensitive folds), a missing typed-dict key (the zero value),
strings (none: indexing, slicing and ``len`` count characters as in Python,
on ASCII and non-ASCII text; only case conversion stays ASCII), list
aliasing (a Go slice: lists are never mutated
after being shared), closures over loop variables (per-iteration in Go:
not generated), async bodies (run concurrently: pure, no output), exception
messages of runtime faults (only the exception kind is printed).
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
COMPILER = ROOT / "compiler" / "lammergeier.py"
PYTHON = os.environ.get("LAMFUZZ_PYTHON", sys.executable)

# ---------------------------------------------------------------------------
# The oracle runtime: Lam semantics expressed in Python.
# ---------------------------------------------------------------------------

PRELUDE = r'''
import math as _math
import sys as _sys
import json as _json
from decimal import Decimal as _Decimal
from functools import reduce as _freduce

class _Discard(BaseException):
    """The twin left the domain the generator models (int64 overflow)."""

class _Prop(BaseException):
    """``?`` on an error Result: unwinds to the function or ``do`` block."""
    def __init__(self, err):
        self.err = err

_I64 = 1 << 63

def _ck(v):
    if type(v) is int and not (-_I64 <= v < _I64):
        raise _Discard("int64 overflow")
    return v

def _gofloat(x):
    if x != x:
        return "NaN"
    if x in (float("inf"), float("-inf")):
        return "+Inf" if x > 0 else "-Inf"
    if x == 0:
        return "-0" if _math.copysign(1.0, x) < 0 else "0"
    sign, digits, exponent = _Decimal(repr(x)).as_tuple()
    ds = "".join(map(str, digits)).lstrip("0")
    stripped = ds.rstrip("0")
    exponent += len(ds) - len(stripped)
    ds = stripped or "0"
    nd = len(ds)
    dp = nd + exponent
    exp = dp - 1
    neg = "-" if sign else ""
    if exp < -4 or exp >= 6:
        mant = ds[0] + ("." + ds[1:] if nd > 1 else "")
        e = "e" + ("-" if exp < 0 else "+") + ("%02d" % abs(exp))
        return neg + mant + e
    if dp <= 0:
        return neg + "0." + "0" * (-dp) + ds
    if dp >= nd:
        return neg + ds + "0" * (dp - nd)
    return neg + ds[:dp] + "." + ds[dp:]

def _key(k):
    return k

def _show(v):
    if v is None:
        return "<nil>"
    if v is True:
        return "true"
    if v is False:
        return "false"
    if type(v) is int:
        return str(v)
    if type(v) is float:
        return _gofloat(v)
    if type(v) is str:
        return v
    if type(v) in (list, tuple):
        return "[" + " ".join(_show(e) for e in v) + "]"
    if type(v) is dict:
        return "map[" + " ".join(_show(k) + ":" + _show(v[k]) for k in sorted(v)) + "]"
    if type(v) in (set, frozenset):
        return "map[" + " ".join(_show(k) + ":true" for k in sorted(v)) + "]"
    return str(v)

def _p(*args):
    print(" ".join(_show(a) for a in args))

def _div(a, b):
    return a / b

def _int(x):
    if type(x) is float:
        if x != x:
            raise ValueError("cannot convert float NaN to integer")
        if x in (float("inf"), float("-inf")):
            raise OverflowError("cannot convert float infinity to integer")
        r = int(x)
        if not (-_I64 <= r < _I64):
            raise OverflowError("float out of range")
        return r
    return int(x)

def _srep(s, n):
    return s * n

def _reduce(f, xs, init):
    return _freduce(f, xs, init)

def _esct(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def _esca(s):
    return _esct(s).replace('"', "&quot;")

def _jenc(v):
    out = _json.dumps(v, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return out.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")

class Error:
    def __init__(self, kind, message):
        self.kind = kind
        self.message = message

class Result:
    def __init__(self, ok, val, err):
        self._ok = ok
        self._val = val
        self._err = err
    @staticmethod
    def Ok(v):
        return Result(True, v, None)
    @staticmethod
    def Err(e):
        return Result(False, None, e)
    def ok(self):
        return self._ok
    def unwrapOr(self, d):
        return self._val if self._ok else d

def _q(r):
    if not r._ok:
        raise _Prop(r._err)
    return r._val

class _Task:
    def __init__(self, fn):
        try:
            self._v = fn()
            self._e = None
        except Exception as e:
            self._v = None
            self._e = e
    def get(self):
        if self._e is not None:
            raise self._e
        return self._v

class AppErr(Exception):
    def __init__(self, code):
        Exception.__init__(self, code)
        self.code = code
    def __str__(self):
        return "AppErr(" + _show(self.code) + ")"
'''

LAM_APPERR = """\
class AppErr {
    func init(self, code: int) {
        self.code: int = code
    }

    func __str__(self) -> str {
        return f"AppErr({self.code})"
    }
}
"""

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

INT, FLOAT, STR, BOOL = "int", "float", "str", "bool"
LINT, LSTR, LFLOAT = "list[int]", "list[str]", "list[float]"
DSI, DII = "dict[str, int]", "dict[int, int]"
SINT, SSTR = "set[int]", "set[str]"
SCALARS = (INT, FLOAT, STR, BOOL)
COLLECTIONS = (LINT, LSTR, LFLOAT, DSI, DII, SINT, SSTR)


def is_list(t: str) -> bool:
    return t.startswith("list[")


def elem(t: str) -> str:
    return t[t.index("[") + 1:-1]


ZERO = {INT: "0", FLOAT: "0.0", STR: '""', BOOL: "False"}

ASCII_WORDS = ["a", "b", "ab", "xy", "Lam", "go", "zz", "q1", "k", "", " pad ", "A-B", "x,y", "1", "42"]
UNI_WORDS = ["é", "日本", "🦅", "naïve", "ü"]


@dataclass
class Var:
    name: str
    ty: str
    mutable: bool = True
    owned: bool = False     # a list only this variable references
    ascii: bool = True      # a str known to be ASCII
    loop: bool = False      # a loop variable (never captured)


@dataclass
class Func:
    name: str
    params: list            # [(name, ty)]
    ret: str | None
    kind: str = "plain"     # plain | gen | result | async


@dataclass
class ClassInfo:
    name: str
    fields: list            # [(name, ty)]
    methods: list           # [(name, params, ret)]
    base: str | None = None
    ctor: list = field(default_factory=list)   # [(name, ty)]


class Scope:
    def __init__(self, parent: "Scope | None" = None):
        self.parent = parent
        self.vars: dict[str, Var] = {}

    def all(self) -> list[Var]:
        out = dict(self.parent.all_map()) if self.parent else {}
        out.update(self.vars)
        return list(out.values())

    def all_map(self) -> dict[str, Var]:
        out = dict(self.parent.all_map()) if self.parent else {}
        out.update(self.vars)
        return out

    def lookup(self, name: str) -> Var | None:
        if name in self.vars:
            return self.vars[name]
        return self.parent.lookup(name) if self.parent else None


class Out:
    """Two parallel renderings (Lam with braces, Python with indentation)."""

    def __init__(self):
        self.lam: list[str] = []
        self.py: list[str] = []
        self.li = 0
        self.pi = 0
        self._py_has_body: list[bool] = []

    def emit(self, lam: str | None, py: str | None) -> None:
        if lam is not None:
            self.lam.append("    " * self.li + lam)
        if py is not None:
            self.py.append("    " * self.pi + py)
            if self._py_has_body:
                self._py_has_body[-1] = True

    def open(self, lam: str, py: str) -> None:
        self.lam.append("    " * self.li + lam + " {")
        self.py.append("    " * self.pi + py + ":")
        if self._py_has_body:
            self._py_has_body[-1] = True
        self.li += 1
        self.pi += 1
        self._py_has_body.append(False)

    def mid(self, lam: str, py: str) -> None:
        self._fill()
        self.li -= 1
        self.pi -= 1
        self.lam.append("    " * self.li + "} " + lam + " {")
        self.py.append("    " * self.pi + py + ":")
        self.li += 1
        self.pi += 1
        self._py_has_body.append(False)

    def py_open(self, py: str) -> None:
        self.py.append("    " * self.pi + py + ":")
        if self._py_has_body:
            self._py_has_body[-1] = True
        self.pi += 1
        self._py_has_body.append(False)

    def py_close(self) -> None:
        self._fill()
        self.pi -= 1

    def _fill(self) -> None:
        if self._py_has_body and not self._py_has_body.pop():
            self.py.append("    " * self.pi + "pass")

    def close(self) -> None:
        self._fill()
        self.li -= 1
        self.pi -= 1
        self.lam.append("    " * self.li + "}")


@dataclass
class Unit:
    """A removable piece of a program (a declaration, a main statement)."""
    lam: list
    py: list
    kind: str = "stmt"


@dataclass
class Program:
    seed: int
    header: list
    defs: list            # [Unit]
    main: list            # [Unit]
    features: set

    def lam_source(self, defs=None, main=None) -> str:
        defs = self.defs if defs is None else defs
        main = self.main if main is None else main
        lines = list(self.header)
        for u in defs:
            lines.extend(u.lam)
            lines.append("")
        lines.append("func main() {")
        for u in main:
            lines.extend("    " + line for line in u.lam)
        lines.append("}")
        return "\n".join(lines) + "\n"

    def py_source(self, defs=None, main=None) -> str:
        defs = self.defs if defs is None else defs
        main = self.main if main is None else main
        lines = [PRELUDE]
        for u in defs:
            lines.extend(u.py)
            lines.append("")
        lines.append("def main():")
        lines.append("    pass")
        for u in main:
            lines.extend("    " + line for line in u.py)
        lines.append("")
        lines.append("try:")
        lines.append("    main()")
        lines.append("except _Discard:")
        lines.append("    _sys.stdout.flush()")
        lines.append("    _sys.exit(99)")
        return "\n".join(lines) + "\n"


FEATURES = (
    "arith", "float", "str", "unicode", "list", "dict", "set", "compr", "fstr",
    "lambda", "closure", "class", "inherit", "iface", "generic", "static",
    "result", "try", "gen", "with", "async", "match", "markup", "json", "loops",
)


class Gen:
    def __init__(self, seed: int, features: set | None = None):
        self.seed = seed
        self.rng = random.Random(seed)
        self.features = set(features or FEATURES)
        self.n = 0
        self.funcs: list[Func] = []
        self.classes: dict[str, ClassInfo] = {}
        self.iface: tuple | None = None   # (name, method, ret)
        self.used: set[str] = set()
        self.depth = 0
        self._typed_ctx = False

    # -- helpers -----------------------------------------------------------
    def fresh(self, prefix: str = "v") -> str:
        self.n += 1
        return f"{prefix}{self.n}"

    def chance(self, p: float) -> bool:
        return self.rng.random() < p

    def has(self, feat: str) -> bool:
        return feat in self.features

    def pick(self, seq):
        return self.rng.choice(list(seq))

    def vars_of(self, scope: Scope, ty: str, **flt) -> list[Var]:
        out = []
        for v in scope.all():
            if v.ty != ty:
                continue
            if flt.get("ascii") and not v.ascii:
                continue
            if flt.get("owned") and not (v.owned and v.mutable):
                continue
            if flt.get("mutable") and not v.mutable:
                continue
            out.append(v)
        return out

    def freeze(self, lam_expr: str, scope: Scope) -> None:
        """A list variable used as a value may now be shared: never mutate it."""
        for v in scope.all():
            if is_list(v.ty) and re.search(rf"\b{v.name}\b", lam_expr):
                v.owned = False

    # -- literals ----------------------------------------------------------
    def int_lit(self) -> tuple[str, str]:
        r = self.rng.random()
        if r < 0.7:
            n = self.rng.randint(0, 12)
        elif r < 0.85:
            n = -self.rng.randint(1, 9)
        elif r < 0.95:
            n = self.rng.randint(100, 100000)
        else:
            n = self.rng.choice([2 ** 31, 2 ** 40 + 7, 9007199254740993])
        if n < 0:
            return f"({n})", f"({n})"
        return str(n), str(n)

    def str_lit(self, ascii: bool = True) -> tuple[str, str]:
        if not ascii and self.has("unicode") and self.chance(0.5):
            s = self.pick(UNI_WORDS)
        else:
            s = self.pick(ASCII_WORDS)
        return json.dumps(s, ensure_ascii=False), repr(s)

    # -- expressions -------------------------------------------------------
    def expr(self, ty: str, scope: Scope, d: int = 0, risky: bool = False, ascii: bool = False) -> tuple[str, str]:
        self.depth += 1
        try:
            if ty == INT:
                return self.int_expr(scope, d, risky)
            if ty == FLOAT:
                return self.float_expr(scope, d, risky)
            if ty == STR:
                return self.str_expr(scope, d, risky, ascii)
            if ty == BOOL:
                return self.bool_expr(scope, d, risky)
            if is_list(ty):
                return self.list_expr(ty, scope, d, risky)
            if ty in (DSI, DII):
                return self.dict_expr(ty, scope, d, risky)
            if ty in (SINT, SSTR):
                return self.set_expr(ty, scope, d, risky)
            if ty in self.classes:
                return self.obj_expr(ty, scope, d, risky)
            raise ValueError(ty)
        finally:
            self.depth -= 1

    def leaf(self, ty: str, scope: Scope, ascii: bool = False) -> tuple[str, str] | None:
        cands = self.vars_of(scope, ty, ascii=ascii and ty == STR)
        if cands and self.chance(0.75):
            v = self.pick(cands)
            if is_list(v.ty):
                v.owned = False
            return v.name, v.name
        return None

    def int_expr(self, scope: Scope, d: int, risky: bool) -> tuple[str, str]:
        leaf = self.leaf(INT, scope)
        if d >= 3 or (d >= 1 and self.chance(0.35)):
            return leaf or self.int_lit()
        choices = ["lit", "var", "bin", "bin", "neg", "len", "minmax", "abs", "tern", "call"]
        if risky:
            choices += ["div", "mod", "index", "pow"]
            if self.has("float"):
                choices.append("int_of_float")
        if self.has("dict"):
            choices.append("dget")
        if self.has("class") and self.classes:
            choices += ["field", "method"]
        if self.has("static") and "Reg" in self.classes:
            choices.append("static")
        if self.has("generic") and "Box" in self.classes:
            choices.append("box")
        if self.has("lambda"):
            choices.append("lambda_call")
        c = self.pick(choices)
        if c == "lit":
            return self.int_lit()
        if c == "var":
            return leaf or self.int_lit()
        if c == "bin":
            op = self.pick(["+", "-", "*"])
            a = self.int_expr(scope, d + 1, risky)
            b = self.int_expr(scope, d + 1, risky)
            return f"({a[0]} {op} {b[0]})", f"_ck({a[1]} {op} {b[1]})"
        if c == "neg":
            a = self.int_expr(scope, d + 1, risky)
            return f"(-{a[0]})", f"_ck(-{a[1]})"
        if c in ("div", "mod"):
            op = "//" if c == "div" else "%"
            a = self.int_expr(scope, d + 1, risky)
            b = leaf or self.int_expr(scope, d + 1, risky)
            if re.fullmatch(r"\(?-?\d+\)?", b[0]):
                # A constant divisor: a constant 0 is a compile error.
                b = (f"({b[0]} + 1)", f"_ck({b[1]} + 1)") if b[0].strip("()") in ("0", "-1") else b
            return f"({a[0]} {op} {b[0]})", f"_ck({a[1]} {op} {b[1]})"
        if c == "pow":
            # ``**`` is computed in floating point (math.Pow): keep the
            # result exact (below 2**53) so it prints like an int.
            a = self.int_expr(scope, d + 2, False)
            e = self.rng.randint(0, 4)
            return f"((({a[0]}) % 50) ** {e})", f"_ck(_ck(({a[1]}) % 50) ** {e})"
        if c == "len":
            t = self.pick([LINT, LSTR, STR, DSI, SINT] if self.has("set") else [LINT, LSTR, STR, DSI])
            a = self.expr(t, scope, d + 1, risky)
            return f"len({a[0]})", f"len({a[1]})"
        if c == "minmax":
            fn = self.pick(["min", "max"])
            a = self.int_expr(scope, d + 1, risky)
            b = self.int_expr(scope, d + 1, risky)
            return f"{fn}({a[0]}, {b[0]})", f"{fn}({a[1]}, {b[1]})"
        if c == "abs":
            a = self.int_expr(scope, d + 1, risky)
            return f"abs({a[0]})", f"_ck(abs({a[1]}))"
        if c == "tern":
            cnd = self.bool_expr(scope, d + 1, risky)
            a = self.int_expr(scope, d + 1, risky)
            b = self.int_expr(scope, d + 1, risky)
            return f"({a[0]} if {cnd[0]} else {b[0]})", f"({a[1]} if {cnd[1]} else {b[1]})"
        if c == "int_of_float":
            a = self.float_expr(scope, d + 1, risky)
            return f"int({a[0]})", f"_int({a[1]})"
        if c == "index":
            xs = self.list_expr(LINT, scope, d + 1, risky)
            i = self.int_expr(scope, d + 2, risky)
            return f"{xs[0]}[{i[0]}]", f"{xs[1]}[{i[1]}]"
        if c == "dget":
            dd = self.dict_expr(DSI, scope, d + 1, risky)
            k = self.str_expr(scope, d + 2, risky, False)
            # A dict literal right after `else` or as a set's first element
            # reads as a block (a known grammar limitation, docs/SYNTAX.md):
            # the literal is parenthesised, as a program would write it.
            lam_dict = f"({dd[0]})" if dd[0].startswith("{") else dd[0]
            return f"{lam_dict}[{k[0]}]", f"{dd[1]}.get({k[1]}, 0)"
        if c == "field":
            obj = self.pick_obj(scope, d, risky)
            if obj:
                name, (lo, po) = obj
                fields = [f for f in self.all_fields(name) if f[1] == INT]
                if fields:
                    f = self.pick(fields)[0]
                    return f"{lo}.{f}", f"{po}.{f}"
            return self.int_lit()
        if c == "method":
            obj = self.pick_obj(scope, d, risky)
            if obj:
                name, (lo, po) = obj
                ms = [m for m in self.all_methods(name) if m[2] == INT]
                if ms:
                    mname, params, _ = self.pick(ms)
                    args = [self.expr(t, scope, d + 2, risky) for _, t in params]
                    return (f"{lo}.{mname}({', '.join(a[0] for a in args)})",
                            f"{po}.{mname}({', '.join(a[1] for a in args)})")
            return self.int_lit()
        if c == "static":
            a = self.int_expr(scope, d + 2, risky)
            return f"Reg.bump({a[0]})", f"Reg.bump({a[1]})"
        if c == "box":
            a = self.int_expr(scope, d + 1, risky)
            return f"Box[int]({a[0]}).get()", f"Box({a[1]}).get()"
        if c == "lambda_call":
            k = self.int_expr(scope, d + 2, False)
            x = self.int_expr(scope, d + 2, risky)
            return (f"applyTwice(lambda n: n + {k[0]}, {x[0]})",
                    f"applyTwice(lambda n: _ck(n + {k[1]}), {x[1]})")
        if c == "call":
            fs = [f for f in self.funcs if f.ret == INT and f.kind == "plain"]
            if fs:
                f = self.pick(fs)
                args = [self.expr(t, scope, d + 2, risky) for _, t in f.params]
                for a in args:
                    self.freeze(a[0], scope)
                return f"{f.name}({', '.join(a[0] for a in args)})", f"{f.name}({', '.join(a[1] for a in args)})"
            return leaf or self.int_lit()
        return self.int_lit()

    def float_expr(self, scope: Scope, d: int, risky: bool) -> tuple[str, str]:
        # Every float expression has a non-literal leaf: Go folds constant
        # float expressions exactly, Python rounds each operation.
        leaf = self.leaf(FLOAT, scope)
        if leaf and (d >= 2 or self.chance(0.3)):
            return leaf
        c = self.pick(["of_int", "bin", "bin", "div", "neg", "tern"] + (["var"] if leaf else []))
        if c == "var" and leaf:
            return leaf
        if c == "of_int" or d >= 3:
            v = self.vars_of(scope, INT)
            if v:
                n = self.pick(v).name
                return f"float({n})", f"float({n})"
            w = self.pick(["ab", "xyz", ""])
            return f"float(len(\"{w}\"))", f"float({len(w)})"
        if c == "bin":
            op = self.pick(["+", "-", "*"])
            a = self.float_expr(scope, d + 1, risky)
            if self.chance(0.3):
                lit = self.rng.choice(["0.5", "2.25", "1.5", "0.1", "3.0"])
                return f"({a[0]} {op} {lit})", f"({a[1]} {op} {lit})"
            b = self.float_expr(scope, d + 1, risky)
            return f"({a[0]} {op} {b[0]})", f"({a[1]} {op} {b[1]})"
        if c == "div":
            if not risky:
                a = self.float_expr(scope, d + 1, risky)
                return f"({a[0]} / 4.0)", f"({a[1]} / 4.0)"
            a = self.float_expr(scope, d + 1, risky)
            b = self.int_expr(scope, d + 1, risky)
            if re.fullmatch(r"\(?-?\d+\)?", b[0]):
                b = (f"len(\"ab\")", "2")
            return f"({a[0]} / {b[0]})", f"({a[1]} / {b[1]})"
        if c == "neg":
            a = self.float_expr(scope, d + 1, risky)
            return f"(-{a[0]})", f"(-{a[1]})"
        cnd = self.bool_expr(scope, d + 1, risky)
        a = self.float_expr(scope, d + 1, risky)
        b = self.float_expr(scope, d + 1, risky)
        return f"({a[0]} if {cnd[0]} else {b[0]})", f"({a[1]} if {cnd[1]} else {b[1]})"

    def str_expr(self, scope: Scope, d: int, risky: bool, ascii: bool) -> tuple[str, str]:
        leaf = self.leaf(STR, scope, ascii)
        if d >= 3 or (d >= 1 and self.chance(0.35)):
            return leaf or self.str_lit(ascii)
        choices = ["lit", "var", "cat", "cat", "of_int", "upper", "tern", "rep"]
        if self.has("fstr"):
            choices += ["fstr", "fstr"]
        if self.has("float"):
            choices.append("of_float")
        choices += ["slice", "slice", "index", "join"]
        if self.has("class") and self.classes:
            choices += ["describe"]
        if self.has("generic") and "Box" in self.classes:
            choices.append("box")
        c = self.pick(choices)
        if c == "lit":
            return self.str_lit(ascii)
        if c == "var":
            return leaf or self.str_lit(ascii)
        if c == "cat":
            a = self.str_expr(scope, d + 1, risky, ascii)
            b = self.str_expr(scope, d + 1, risky, ascii)
            return f"({a[0]} + {b[0]})", f"({a[1]} + {b[1]})"
        if c == "of_int":
            a = self.int_expr(scope, d + 1, risky)
            return f"str({a[0]})", f"_show({a[1]})"
        if c == "of_float":
            a = self.float_expr(scope, d + 1, risky)
            return f"str({a[0]})", f"_show({a[1]})"
        if c == "upper":
            m, pm = self.pick([("toUpper", "upper"), ("toLower", "lower"), ("trim", "strip")])
            a = self.str_expr(scope, d + 1, risky, True)
            return f"{a[0]}.{m}()", f"{a[1]}.{pm}()"
        if c == "tern":
            cnd = self.bool_expr(scope, d + 1, risky)
            a = self.str_expr(scope, d + 1, risky, ascii)
            b = self.str_expr(scope, d + 1, risky, ascii)
            return f"({a[0]} if {cnd[0]} else {b[0]})", f"({a[1]} if {cnd[1]} else {b[1]})"
        if c == "rep":
            a = self.str_expr(scope, d + 2, risky, ascii)
            n = self.rng.randint(-1, 3)
            return f"({a[0]} * {n})" if n >= 0 else f"({a[0]} * ({n}))", f"_srep({a[1]}, {n})"
        if c == "fstr":
            parts_l, parts_p = [], []
            for _ in range(self.rng.randint(1, 3)):
                t = self.pick([INT, STR, BOOL] + ([FLOAT] if self.has("float") else []))
                a = self.expr(t, scope, d + 2, risky, ascii=ascii)
                if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*|\d+", a[0]):
                    # f-string slots hold names and simple calls: keep the
                    # slot expression free of string literals and braces.
                    if '"' in a[0] or "{" in a[0] or "'" in a[0]:
                        a = (str(self.rng.randint(0, 9)),) * 2 if t == INT else self.leaf(t, scope) or ((str(self.rng.randint(0, 9)),) * 2)
                        t = INT if a[0].isdigit() else t
                txt = self.pick(["", "-", " x=", ":"])
                parts_l.append(txt + "{" + a[0] + "}")
                parts_p.append(txt + "{_show(" + a[1] + ")}")
            return 'f"' + "".join(parts_l) + '"', 'f"' + "".join(parts_p) + '"'
        if c == "slice":
            # By character, like Python (non-ASCII text too).
            a = self.str_expr(scope, d + 1, risky, ascii)
            lo = self.rng.randint(-3, 3)
            hi = self.rng.randint(-2, 5)
            form = self.pick(["lo", "hi", "both", "step"])
            if form == "lo":
                return f"{a[0]}[{lo}:]", f"{a[1]}[{lo}:]"
            if form == "hi":
                return f"{a[0]}[:{hi}]", f"{a[1]}[:{hi}]"
            if form == "step":
                st = self.pick([-2, -1, 2, 3])
                b = self.pick(["", str(lo)])
                e = self.pick(["", str(hi)])
                return f"{a[0]}[{b}:{e}:{st}]", f"{a[1]}[{b}:{e}:{st}]"
            return f"{a[0]}[{lo}:{hi}]", f"{a[1]}[{lo}:{hi}]"
        if c == "index":
            # ``s[i]`` is a one-character str; the receiver has at least
            # two characters, so the index is in range.
            a = self.str_expr(scope, d + 1, risky, ascii)
            pad = self.pick(["xy", "éz", "日本", "🦅!"] if not ascii else ["xy", "ab"])
            k = self.rng.randint(-2, 1)
            return f"({a[0]} + \"{pad}\")[{k}]", f"({a[1]} + \"{pad}\")[{k}]"
        if c == "join":
            xs = self.list_expr(LSTR, scope, d + 1, risky, ascii=ascii)
            sep = self.pick([",", "", " ", "-"])
            return f'"{sep}".join({xs[0]})', f'"{sep}".join({xs[1]})'
        if c == "describe":
            obj = self.pick_obj(scope, d, risky)
            if obj:
                name, (lo, po) = obj
                return f"{lo}.describe()", f"{po}.describe()"
            return self.str_lit(ascii)
        if c == "box":
            a = self.str_expr(scope, d + 1, risky, ascii)
            return f"Box[str]({a[0]}).get()", f"Box({a[1]}).get()"
        return self.str_lit(ascii)

    def bool_expr(self, scope: Scope, d: int, risky: bool) -> tuple[str, str]:
        leaf = self.leaf(BOOL, scope)
        if d >= 3:
            return leaf or self.pick([("true", "True"), ("false", "False")])
        c = self.pick(["cmp", "cmp", "cmp", "not", "andor", "in", "lit", "chain", "strpred", "listpred"])
        if c == "lit":
            return leaf or self.pick([("true", "True"), ("false", "False")])
        if c == "cmp":
            t = self.pick([INT, INT, STR] + ([FLOAT] if self.has("float") else []))
            op = self.pick(["<", "<=", ">", ">=", "==", "!="])
            a = self.expr(t, scope, d + 1, risky, ascii=True)
            b = self.expr(t, scope, d + 1, risky, ascii=True)
            return f"({a[0]} {op} {b[0]})", f"({a[1]} {op} {b[1]})"
        if c == "chain":
            a = self.int_expr(scope, d + 1, risky)
            b = self.int_expr(scope, d + 1, risky)
            e = self.int_expr(scope, d + 1, risky)
            return f"({a[0]} < {b[0]} <= {e[0]})", f"({a[1]} < {b[1]} <= {e[1]})"
        if c == "not":
            a = self.bool_expr(scope, d + 1, risky)
            return f"(not {a[0]})", f"(not {a[1]})"
        if c == "andor":
            op = self.pick(["and", "or"])
            a = self.bool_expr(scope, d + 1, risky)
            b = self.bool_expr(scope, d + 1, risky)
            return f"({a[0]} {op} {b[0]})", f"({a[1]} {op} {b[1]})"
        if c == "in":
            t = self.pick([LINT, DSI] + ([SINT] if self.has("set") else []) + [STR])
            if t == STR:
                a = self.str_expr(scope, d + 1, risky, True)
                b = self.str_expr(scope, d + 1, risky, True)
                return f"({a[0]} in {b[0]})", f"({a[1]} in {b[1]})"
            coll = self.expr(t, scope, d + 1, risky)
            k = self.expr(STR if t == DSI else INT, scope, d + 2, risky)
            return f"({k[0]} in {coll[0]})", f"({k[1]} in {coll[1]})"
        if c == "strpred":
            m, pm = self.pick([("startsWith", "startswith"), ("endsWith", "endswith")])
            a = self.str_expr(scope, d + 1, risky, True)
            b = self.str_expr(scope, d + 2, risky, True)
            return f"{a[0]}.{m}({b[0]})", f"{a[1]}.{pm}({b[1]})"
        if c == "listpred" and self.has("lambda"):
            m = self.pick(["any", "all"])
            xs = self.list_expr(LINT, scope, d + 1, risky)
            k = self.int_lit()
            op = self.pick([">", "<", "=="])
            return (f"{xs[0]}.{m}(lambda x: x {op} {k[0]})",
                    f"{m}((x {op} {k[1]}) for x in {xs[1]})")
        return leaf or ("true", "True")

    def list_expr(self, ty: str, scope: Scope, d: int, risky: bool, ascii: bool = False) -> tuple[str, str]:
        et = elem(ty)
        leaf = self.leaf(ty, scope)
        if d >= 3 or (d >= 1 and self.chance(0.4)):
            if leaf:
                return leaf
            return self.list_lit(ty, scope, d, risky, ascii)
        choices = ["lit", "var", "cat"]
        if self.has("compr"):
            choices += ["compr", "compr_range"]
        if self.has("lambda") and et != STR:
            choices += ["map", "filter"]
        if et in (INT, STR):
            choices += ["sort", "slice"]
        if et == STR:
            choices.append("split")
        if self.has("gen") and et == INT and any(f.kind == "gen" for f in self.funcs):
            choices.append("from_gen")
        c = self.pick(choices)
        if c == "var" and leaf:
            return leaf
        if c == "cat":
            a = self.list_expr(ty, scope, d + 1, risky, ascii)
            b = self.list_expr(ty, scope, d + 1, risky, ascii)
            return f"({a[0]} + {b[0]})", f"({a[1]} + {b[1]})"
        if c == "compr":
            src = self.list_expr(ty if et != FLOAT else LINT, scope, d + 1, risky, ascii)
            x = self.fresh("x")
            inner = Scope(scope)
            inner.vars[x] = Var(x, elem(ty) if et != FLOAT else INT, mutable=False, loop=True, ascii=ascii)
            body = self.expr(et, inner, d + 2, risky, ascii=ascii)
            if self.chance(0.5):
                cnd = self.bool_expr(inner, d + 2, risky)
                return f"[{body[0]} for {x} in {src[0]} if {cnd[0]}]", f"[{body[1]} for {x} in {src[1]} if {cnd[1]}]"
            return f"[{body[0]} for {x} in {src[0]}]", f"[{body[1]} for {x} in {src[1]}]"
        if c == "compr_range":
            x = self.fresh("i")
            n = self.rng.randint(0, 6)
            inner = Scope(scope)
            inner.vars[x] = Var(x, INT, mutable=False, loop=True)
            body = self.expr(et, inner, d + 2, risky, ascii=ascii)
            return f"[{body[0]} for {x} in range({n})]", f"[{body[1]} for {x} in range({n})]"
        if c == "map":
            src = self.list_expr(ty, scope, d + 1, risky)
            x = self.fresh("m")
            inner = Scope(scope)
            inner.vars[x] = Var(x, et, mutable=False, loop=True)
            body = self.expr(et, inner, d + 2, risky)
            return f"{src[0]}.map(lambda {x}: {body[0]})", f"list(map(lambda {x}: {body[1]}, {src[1]}))"
        if c == "filter":
            src = self.list_expr(ty, scope, d + 1, risky)
            x = self.fresh("f")
            inner = Scope(scope)
            inner.vars[x] = Var(x, et, mutable=False, loop=True)
            body = self.bool_expr(inner, d + 2, risky)
            return f"{src[0]}.filter(lambda {x}: {body[0]})", f"list(filter(lambda {x}: {body[1]}, {src[1]}))"
        if c == "sort":
            src = self.list_expr(ty, scope, d + 1, risky, ascii=True)
            if self.chance(0.3) and et == INT:
                return f"{src[0]}.sort(compare=lambda a, b: a > b)", f"sorted({src[1]}, reverse=True)"
            return f"{src[0]}.sort()", f"sorted({src[1]})"
        if c == "slice":
            src = self.list_expr(ty, scope, d + 1, risky, ascii)
            lo = self.rng.randint(-3, 3)
            hi = self.rng.randint(-2, 5)
            return f"{src[0]}[{lo}:{hi}]", f"{src[1]}[{lo}:{hi}]"
        if c == "split":
            s = self.str_expr(scope, d + 1, risky, ascii)
            sep = self.pick([",", " ", "a"])
            return f'{s[0]}.split("{sep}")', f'{s[1]}.split("{sep}")'
        if c == "from_gen":
            g = self.pick([f for f in self.funcs if f.kind == "gen"])
            args = [self.expr(t, scope, d + 2, risky) for _, t in g.params]
            x = self.fresh("g")
            return (f"[{x} for {x} in {g.name}({', '.join(a[0] for a in args)})]",
                    f"[{x} for {x} in {g.name}({', '.join(a[1] for a in args)})]")
        return self.list_lit(ty, scope, d, risky, ascii)

    def list_lit(self, ty: str, scope: Scope, d: int, risky: bool, ascii: bool = False) -> tuple[str, str]:
        et = elem(ty)
        # An empty literal has no element type outside an annotated
        # declaration (a Lam error, not what this fuzzer is after).
        n = self.rng.randint(0 if d == 0 and self._typed_ctx else 1, 4)
        items = [self.expr(et, scope, d + 2, risky, ascii=ascii) for _ in range(n)]
        return "[" + ", ".join(i[0] for i in items) + "]", "[" + ", ".join(i[1] for i in items) + "]"

    def dict_expr(self, ty: str, scope: Scope, d: int, risky: bool) -> tuple[str, str]:
        leaf = self.leaf(ty, scope)
        kt = STR if ty == DSI else INT
        if leaf and (d >= 1 or self.chance(0.5)):
            return leaf
        if self.has("compr") and self.chance(0.3) and d < 2:
            x = self.fresh("x")
            src = self.list_expr(LINT, scope, d + 1, risky)
            inner = Scope(scope)
            inner.vars[x] = Var(x, INT, mutable=False, loop=True)
            v = self.int_expr(inner, d + 2, risky)
            key_l, key_p = (f'f"k{{{x}}}"', f'f"k{{_show({x})}}"') if kt == STR else (x, x)
            return f"{{{key_l}: {v[0]} for {x} in {src[0]}}}", f"{{{key_p}: {v[1]} for {x} in {src[1]}}}"
        n = self.rng.randint(0 if d == 0 and self._typed_ctx else 1, 3)
        items = []
        seen = set()
        for _ in range(n):
            k = self.str_lit(True) if kt == STR else self.int_lit()
            if k[0] in seen:
                continue
            seen.add(k[0])
            v = self.int_expr(scope, d + 2, risky)
            items.append((k, v))
        return ("{" + ", ".join(f"{k[0]}: {v[0]}" for k, v in items) + "}",
                "{" + ", ".join(f"{k[1]}: {v[1]}" for k, v in items) + "}")

    def set_expr(self, ty: str, scope: Scope, d: int, risky: bool) -> tuple[str, str]:
        leaf = self.leaf(ty, scope)
        et = INT if ty == SINT else STR
        if leaf and (d >= 1 or self.chance(0.5)):
            return leaf
        if self.has("compr") and self.chance(0.4) and d < 2:
            x = self.fresh("x")
            src = self.list_expr(f"list[{et}]", scope, d + 1, risky, ascii=True)
            inner = Scope(scope)
            inner.vars[x] = Var(x, et, mutable=False, loop=True)
            v = self.expr(et, inner, d + 2, risky, ascii=True)
            return f"{{{v[0]} for {x} in {src[0]}}}", f"{{{v[1]} for {x} in {src[1]}}}"
        n = self.rng.randint(1, 4)
        items = [self.expr(et, scope, 3, False, ascii=True) for _ in range(n)]
        return "{" + ", ".join(i[0] for i in items) + "}", "{" + ", ".join(i[1] for i in items) + "}"

    # -- classes -----------------------------------------------------------
    def all_fields(self, cls: str) -> list:
        info = self.classes[cls]
        out = list(info.fields)
        if info.base:
            out = self.all_fields(info.base) + out
        return out

    def all_methods(self, cls: str) -> list:
        info = self.classes[cls]
        names = {m[0] for m in info.methods}
        out = list(info.methods)
        if info.base:
            out += [m for m in self.all_methods(info.base) if m[0] not in names]
        return out

    def pick_obj(self, scope: Scope, d: int, risky: bool):
        names = [c for c in self.classes if c.startswith("Shape")]
        if not names:
            return None
        objs = [v for v in scope.all() if v.ty in names]
        if objs and self.chance(0.8):
            v = self.pick(objs)
            return v.ty, (v.name, v.name)
        if d >= 2:
            return None
        cls = self.pick(names)
        return cls, self.obj_expr(cls, scope, d + 1, risky)

    def obj_expr(self, cls: str, scope: Scope, d: int, risky: bool) -> tuple[str, str]:
        objs = [v for v in scope.all() if v.ty == cls]
        if objs and self.chance(0.5):
            v = self.pick(objs)
            return v.name, v.name
        info = self.classes[cls]
        args = [self.expr(t, scope, d + 2, risky) for _, t in info.ctor]
        for a in args:
            self.freeze(a[0], scope)
        return f"{cls}({', '.join(a[0] for a in args)})", f"{cls}({', '.join(a[1] for a in args)})"

    def gen_classes(self, defs: list) -> None:
        if not self.has("class"):
            return
        # Base shape.
        out = Out()
        cls = "Shape"
        self.classes[cls] = ClassInfo(cls, [("w", INT), ("label", STR)], [], ctor=[("w", INT), ("label", STR)])
        out.open(f"class {cls}", f"class {cls}")
        out.open("func init(self, w: int, label: str)", "def __init__(self, w, label)")
        out.emit("self.w: int = w", "self.w = w")
        out.emit("self.label: str = label", "self.label = label")
        out.close()
        out.emit("", "")
        sc = Scope()
        sc.vars["self.w"] = Var("self.w", INT, mutable=False)
        body = self.int_expr(sc, 2, False)
        out.open("func area(self) -> int", "def area(self)")
        out.emit(f"return {body[0]}", f"return {body[1]}")
        out.close()
        self.classes[cls].methods.append(("area", [], INT))
        k = self.rng.randint(1, 5)
        out.open("func scale(self, k: int) -> int", "def scale(self, k)")
        out.emit(f"return self.area() * k + {k}", f"return _ck(_ck(self.area() * k) + {k})")
        out.close()
        self.classes[cls].methods.append(("scale", [("k", INT)], INT))
        out.open("func describe(self) -> str", "def describe(self)")
        out.emit('return f"{self.label}:{self.area()}"', 'return f"{self.label}:{_show(self.area())}"')
        out.close()
        self.classes[cls].methods.append(("describe", [], STR))
        out.open("func __str__(self) -> str", "def __str__(self)")
        out.emit('return f"Shape({self.w})"', 'return f"Shape({_show(self.w)})"')
        out.close()
        out.close()
        defs.append(Unit(out.lam, out.py, "class"))

        if self.has("inherit"):
            out = Out()
            sub = "ShapeSq"
            self.classes[sub] = ClassInfo(sub, [("h", INT)], [], base=cls, ctor=[("w", INT), ("h", INT)])
            out.open(f"class {sub}({cls})", f"class {sub}({cls})")
            out.open("func init(self, w: int, h: int)", "def __init__(self, w, h)")
            out.emit('base.init(w, "sq")', f'{cls}.__init__(self, w, "sq")')
            out.emit("self.h: int = h", "self.h = h")
            out.close()
            out.emit("", "")
            bonus = self.rng.randint(0, 9)
            out.open("func area(self) -> int", "def area(self)")
            if self.chance(0.5):
                out.emit(f"return self.w * self.h + {bonus}", f"return _ck(_ck(self.w * self.h) + {bonus})")
            else:
                out.emit(f"return base.area() + self.h * {bonus}", f"return _ck({cls}.area(self) + _ck(self.h * {bonus}))")
            out.close()
            self.classes[sub].methods.append(("area", [], INT))
            out.close()
            defs.append(Unit(out.lam, out.py, "class"))

        if self.has("iface"):
            out = Out()
            out.open("interface HasArea", "class HasArea")
            out.emit("func area(self) -> int", None)
            out.close()
            defs.append(Unit(out.lam, out.py, "class"))
            out = Out()
            out.open("func areaOf(a: HasArea, k: int) -> int", "def areaOf(a, k)")
            out.emit("return a.area() + k", "return _ck(a.area() + k)")
            out.close()
            defs.append(Unit(out.lam, out.py, "func"))
            self.iface = ("HasArea", "area", INT)
            self.funcs.append(Func("areaOf", [("a", cls), ("k", INT)], INT, "iface"))

        if self.has("generic"):
            out = Out()
            self.classes["Box"] = ClassInfo("Box", [], [])
            out.open("class Box[T]", "class Box")
            out.open("func init(self, v: T)", "def __init__(self, v)")
            out.emit("self.v: T = v", "self.v = v")
            out.close()
            out.emit("", "")
            out.open("func get(self) -> T", "def get(self)")
            out.emit("return self.v", "return self.v")
            out.close()
            out.close()
            defs.append(Unit(out.lam, out.py, "class"))
            out = Out()
            out.open("func pick[T](a: T, b: T, first: bool) -> T", "def pick(a, b, first)")
            out.emit("return a if first else b", "return a if first else b")
            out.close()
            defs.append(Unit(out.lam, out.py, "func"))

        if self.has("static"):
            out = Out()
            self.classes["Reg"] = ClassInfo("Reg", [], [])
            out.open("class Reg", "class Reg")
            out.emit("static count: int = 0", "count = 0")
            out.emit("", "")
            out.open("static func bump(n: int) -> int", "@staticmethod\n" + "    " * out.pi + "def bump(n)")
            out.emit("Reg.count += n", "Reg.count = _ck(Reg.count + n)")
            out.emit("return Reg.count", "return Reg.count")
            out.close()
            out.close()
            defs.append(Unit(out.lam, out.py, "class"))

    # -- functions ---------------------------------------------------------
    def gen_helpers(self, defs: list) -> None:
        if self.has("lambda"):
            out = Out()
            out.open("func applyTwice(f: func(int) -> int, x: int) -> int", "def applyTwice(f, x)")
            out.emit("return f(f(x))", "return f(f(x))")
            out.close()
            defs.append(Unit(out.lam, out.py, "func"))
        if self.has("closure"):
            out = Out()
            k = self.rng.randint(1, 9)
            out.open("func makeAdder(base: int) -> func(int) -> int", "def makeAdder(base)")
            out.open("func add(x: int) -> int", "def add(x)")
            out.emit(f"return x + base * {k}", f"return _ck(x + _ck(base * {k}))")
            out.close()
            out.emit("return add", "return add")
            out.close()
            defs.append(Unit(out.lam, out.py, "func"))
        # ``AppErr`` is caught by every guarded main statement.
        defs.append(Unit(LAM_APPERR.rstrip("\n").splitlines(), [], "class"))

    def gen_function(self, defs: list) -> None:
        name = self.fresh("fn")
        nparams = self.rng.randint(0, 3)
        ptypes = [INT, INT, STR, BOOL, LINT] + ([FLOAT] if self.has("float") else []) + ([DSI] if self.has("dict") else [])
        params = [(f"p{i}", self.pick(ptypes)) for i in range(nparams)]
        ret = self.pick([INT, INT, STR, BOOL] + ([FLOAT] if self.has("float") else []) + ([LINT] if self.has("list") else []))
        sc = Scope()
        for p, t in params:
            sc.vars[p] = Var(p, t, mutable=False, owned=False, ascii=False)
        out = Out()
        sig = ", ".join(f"{p}: {t}" for p, t in params)
        out.open(f"func {name}({sig}) -> {ret}", f"def {name}({', '.join(p for p, _ in params)})")
        self.block(out, sc, ret_ty=ret, budget=self.rng.randint(1, 4), risky=True, depth=1)
        r = self.expr(ret, sc, 0, True)
        out.emit(f"return {r[0]}", f"return {r[1]}")
        out.close()
        defs.append(Unit(out.lam, out.py, "func"))
        self.funcs.append(Func(name, params, ret, "plain"))

    def gen_generator(self, defs: list) -> None:
        name = self.fresh("gen")
        out = Out()
        k = self.rng.randint(1, 4)
        out.open(f"func {name}(n: int, k: int) -> generator[int]", f"def {name}(n, k)")
        out.open("for i in range(n)", "for i in range(n)")
        cond = self.pick(["i % 2 == k % 2", "i > k", "true", "i != k"])
        pcond = cond.replace("true", "True")
        out.open(f"if {cond}", f"if {pcond}")
        out.emit(f"yield i * k + {k}", f"yield _ck(_ck(i * k) + {k})")
        out.close()
        if self.chance(0.3):
            out.open("if i == 3", "if i == 3")
            out.emit("return", "return")
            out.close()
        out.close()
        last = self.rng.randint(0, 99)
        out.emit(f"yield {last}", f"yield {last}")
        out.close()
        defs.append(Unit(out.lam, out.py, "func"))
        self.funcs.append(Func(name, [("n", INT), ("k", INT)], INT, "gen"))

    def gen_result_funcs(self, defs: list) -> None:
        out = Out()
        m = self.rng.randint(2, 5)
        out.open("func parseNum(x: int) -> Result[int]", "def parseNum(x)")
        out.open(f"if x % {m} == 0", f"if x % {m} == 0")
        out.emit('return Result.Err(Error("BadNum", f"bad {x}"))', 'return Result.Err(Error("BadNum", f"bad {_show(x)}"))')
        out.close()
        out.emit("return Result.Ok(x * 2 + 1)", "return Result.Ok(_ck(_ck(x * 2) + 1))")
        out.close()
        defs.append(Unit(out.lam, out.py, "func"))
        out = Out()
        out.open("func chain(x: int) -> Result[int]", "def chain(x)")
        out.py_open("try")
        out.emit("a: int = parseNum(x)?", "a = _q(parseNum(x))")
        out.emit("b: int = parseNum(a + 1)?", "b = _q(parseNum(_ck(a + 1)))")
        out.emit("return Result.Ok(a + b)", "return Result.Ok(_ck(a + b))")
        out.py_close()
        out.py_open("except _Prop as _e")
        out.emit(None, "return Result.Err(_e.err)")
        out.py_close()
        out.close()
        defs.append(Unit(out.lam, out.py, "func"))
        self.funcs.append(Func("parseNum", [("x", INT)], "Result[int]", "result"))
        self.funcs.append(Func("chain", [("x", INT)], "Result[int]", "result"))

    def gen_async(self, defs: list) -> None:
        out = Out()
        k = self.rng.randint(1, 7)
        out.open("async func work(x: int) -> int", "def work(x)")
        out.open(f"if x % {k + 2} == 1", f"if x % {k + 2} == 1")
        out.emit('raise ValueError(f"work {x}")', 'raise ValueError(f"work {_show(x)}")')
        out.close()
        out.emit(f"return x * {k} + 1", f"return _ck(_ck(x * {k}) + 1)")
        out.close()
        defs.append(Unit(out.lam, out.py, "func"))
        self.funcs.append(Func("work", [("x", INT)], INT, "async"))

    def gen_resource(self, defs: list) -> None:
        out = Out()
        out.open("class Res", "class Res")
        out.open("func init(self, name: str)", "def __init__(self, name)")
        out.emit("self.name: str = name", "self.name = name")
        out.close()
        out.emit("", "")
        out.open("func close(self)", "def close(self)")
        out.emit('print(f"close {self.name}")', '_p(f"close {self.name}")')
        out.close()
        out.close()
        defs.append(Unit(out.lam, out.py, "class"))

    # -- statements --------------------------------------------------------
    def block(self, out: Out, scope: Scope, ret_ty: str | None, budget: int, risky: bool, depth: int, in_loop: bool = False) -> None:
        for _ in range(budget):
            self.stmt(out, scope, ret_ty, risky, depth, in_loop)

    def stmt(self, out: Out, scope: Scope, ret_ty, risky: bool, depth: int, in_loop: bool) -> None:
        kinds = ["decl", "decl", "assign", "print", "print", "if"]
        if self.has("loops") and depth < 3:
            kinds += ["for", "while"]
        if self.has("list"):
            kinds.append("append")
        if self.has("dict"):
            kinds.append("dset")
        if in_loop:
            kinds += ["brk"]
        if self.has("match") and depth < 3:
            kinds.append("match")
        if self.has("try") and depth < 3 and risky:
            kinds.append("throw")
        k = self.pick(kinds)
        if k == "decl":
            ty = self.pick([INT, INT, STR, BOOL] + ([FLOAT] if self.has("float") else [])
                           + ([LINT, LSTR] if self.has("list") else []) + ([DSI] if self.has("dict") else [])
                           + ([SINT] if self.has("set") else []))
            name = self.fresh("v")
            ascii = self.chance(0.6)
            self._typed_ctx = True
            try:
                e = self.expr(ty, scope, 0, risky, ascii=ascii)
            finally:
                self._typed_ctx = False
            self.freeze(e[0], scope)
            fresh_list = is_list(ty) and not re.fullmatch(r"[A-Za-z_]\w*", e[0]) and "[" in e[0][:1] + e[0][-1:]
            out.emit(f"{name}: {ty} = {e[0]}", f"{name} = {e[1]}")
            scope.vars[name] = Var(name, ty, mutable=True, owned=bool(fresh_list) and e[0].startswith("["),
                                   ascii=ascii)
            return
        if k == "assign":
            cands = [v for v in scope.vars.values() if v.mutable and v.ty in (INT, STR, FLOAT)]
            if not cands:
                return self.stmt(out, scope, ret_ty, risky, depth, in_loop) if self.chance(0.5) else None
            v = self.pick(cands)
            e = self.expr(v.ty, scope, 1, risky, ascii=v.ascii)
            if v.ty == INT and self.chance(0.5):
                op = self.pick(["+=", "-=", "*="])
                out.emit(f"{v.name} {op} {e[0]}", f"{v.name} = _ck({v.name} {op[0]} {e[1]})")
            elif v.ty == STR and self.chance(0.5):
                out.emit(f"{v.name} += {e[0]}", f"{v.name} = {v.name} + {e[1]}")
            else:
                out.emit(f"{v.name} = {e[0]}", f"{v.name} = {e[1]}")
            return
        if k == "print":
            ty = self.pick([INT, STR, BOOL] + ([FLOAT] if self.has("float") else [])
                           + ([LINT, LSTR] if self.has("list") else []) + ([DSI] if self.has("dict") else [])
                           + ([SINT] if self.has("set") else []))
            e = self.expr(ty, scope, 0, risky, ascii=False)
            if self.chance(0.2):
                e2 = self.expr(INT, scope, 1, risky)
                out.emit(f"print({e[0]}, {e2[0]})", f"_p({e[1]}, {e2[1]})")
            else:
                out.emit(f"print({e[0]})", f"_p({e[1]})")
            return
        if k == "append":
            cands = [v for v in scope.all() if is_list(v.ty) and v.owned and v.mutable and v.name in scope.vars]
            if not cands:
                return
            v = self.pick(cands)
            e = self.expr(elem(v.ty), scope, 1, risky, ascii=False)
            if self.chance(0.6):
                out.emit(f"{v.name}.append({e[0]})", f"{v.name}.append({e[1]})")
            elif risky and self.chance(0.5):
                i = self.int_expr(scope, 2, risky)
                out.emit(f"{v.name}[{i[0]}] = {e[0]}", f"{v.name}[{i[1]}] = {e[1]}")
            else:
                more = self.list_expr(v.ty, scope, 1, risky)
                out.emit(f"{v.name} += {more[0]}", f"{v.name} += {more[1]}")
            return
        if k == "dset":
            cands = [v for v in scope.vars.values() if v.ty == DSI]
            if not cands:
                return
            v = self.pick(cands)
            key = self.str_expr(scope, 1, risky, False)
            val = self.int_expr(scope, 1, risky)
            out.emit(f"{v.name}[{key[0]}] = {val[0]}", f"{v.name}[{key[1]}] = {val[1]}")
            return
        if k == "if":
            c = self.bool_expr(scope, 0, risky)
            out.open(f"if {c[0]}", f"if {c[1]}")
            self.block(out, Scope(scope), ret_ty, self.rng.randint(1, 2), risky, depth + 1, in_loop)
            if self.chance(0.3):
                c2 = self.bool_expr(scope, 1, risky)
                out.mid(f"elif {c2[0]}", f"elif {c2[1]}")
                self.block(out, Scope(scope), ret_ty, 1, risky, depth + 1, in_loop)
            if self.chance(0.5):
                out.mid("else", "else")
                self.block(out, Scope(scope), ret_ty, self.rng.randint(1, 2), risky, depth + 1, in_loop)
            if ret_ty and self.chance(0.3):
                pass
            out.close()
            if ret_ty and self.chance(0.2):
                c = self.bool_expr(scope, 1, risky)
                r = self.expr(ret_ty, scope, 1, risky)
                out.open(f"if {c[0]}", f"if {c[1]}")
                out.emit(f"return {r[0]}", f"return {r[1]}")
                out.close()
            return
        if k == "for":
            form = self.pick(["range", "list", "dict", "set", "gen"])
            x = self.fresh("it")
            inner = Scope(scope)
            acc = self.fresh("acc")
            out.emit(f"{acc}: int = 0", f"{acc} = 0")
            if form == "range":
                a = self.rng.randint(-2, 3)
                b = self.rng.randint(0, 6)
                out.open(f"for {x} in range({a}, {b})", f"for {x} in range({a}, {b})")
                inner.vars[x] = Var(x, INT, mutable=False, loop=True)
            elif form == "dict" and self.has("dict"):
                dd = self.dict_expr(DSI, scope, 1, risky)
                kk = self.fresh("key")
                out.open(f"for {kk}, {x} in {dd[0]}", f"for {kk}, {x} in ({dd[1]}).items()")
                inner.vars[x] = Var(x, INT, mutable=False, loop=True)
                out.emit(f"{acc} += len({kk})", f"{acc} = _ck({acc} + len({kk}))")
                out.emit(f"{acc} += {x}", f"{acc} = _ck({acc} + {x})")
                out.close()
                out.emit(f"print({acc})", f"_p({acc})")
                return
            elif form == "set" and self.has("set"):
                ss = self.set_expr(SINT, scope, 1, risky)
                out.open(f"for {x} in {ss[0]}", f"for {x} in {ss[1]}")
                out.emit(f"{acc} += {x} * {x}", f"{acc} = _ck({acc} + _ck({x} * {x}))")
                out.close()
                out.emit(f"print({acc})", f"_p({acc})")
                return
            elif form == "gen" and self.has("gen") and any(f.kind == "gen" for f in self.funcs):
                g = self.pick([f for f in self.funcs if f.kind == "gen"])
                n = self.rng.randint(0, 6)
                kk = self.int_expr(scope, 2, False)
                out.open(f"for {x} in {g.name}({n}, {kk[0]})", f"for {x} in {g.name}({n}, {kk[1]})")
                inner.vars[x] = Var(x, INT, mutable=False, loop=True)
            else:
                xs = self.list_expr(LINT, scope, 1, risky)
                out.open(f"for {x} in {xs[0]}", f"for {x} in {xs[1]}")
                inner.vars[x] = Var(x, INT, mutable=False, loop=True)
            out.emit(f"{acc} += {x}", f"{acc} = _ck({acc} + {x})")
            self.block(out, inner, ret_ty, self.rng.randint(0, 2), risky, depth + 1, True)
            if self.chance(0.25):
                out.mid("else", "else")
                out.emit(f"{acc} += 1000", f"{acc} = _ck({acc} + 1000)")
            out.close()
            out.emit(f"print({acc})", f"_p({acc})")
            return
        if k == "while":
            c = self.fresh("w")
            n = self.rng.randint(0, 5)
            out.emit(f"{c}: int = 0", f"{c} = 0")
            out.open(f"while {c} < {n}", f"while {c} < {n}")
            out.emit(f"{c} += 1", f"{c} += 1")
            self.block(out, Scope(scope), ret_ty, self.rng.randint(0, 2), risky, depth + 1, True)
            out.close()
            out.emit(f"print({c})", f"_p({c})")
            return
        if k == "brk":
            c = self.bool_expr(scope, 1, risky)
            kw = self.pick(["break", "continue"])
            out.open(f"if {c[0]}", f"if {c[1]}")
            out.emit(kw, kw)
            out.close()
            return
        if k == "match":
            if self.chance(0.6):
                subj = self.int_expr(scope, 1, risky)
                m = self.rng.randint(2, 4)
                out.open(f"match ({subj[0]}) % {m}", f"match _ck(({subj[1]}) % {m})")
                for case in range(m - 1):
                    out.open(f"case {case}", f"case {case}")
                    out.emit(f'print("case {case}")', f'_p("case {case}")')
                    out.close()
                cap = self.fresh("c")
                out.open(f"case {cap} if {cap} > 0", f"case {cap} if {cap} > 0")
                out.emit(f'print(f"big {{{cap}}}")', f'_p(f"big {{_show({cap})}}")')
                out.close()
                out.open("case _", "case _")
                out.emit('print("other")', '_p("other")')
                out.close()
                out.close()
            else:
                subj = self.str_expr(scope, 1, risky, True)
                words = self.rng.sample(ASCII_WORDS[:9], 2)
                out.open(f"match {subj[0]}", f"match {subj[1]}")
                for w in words:
                    out.open(f'case "{w}"', f'case "{w}"')
                    out.emit(f'print("word {w}")', f'_p("word {w}")')
                    out.close()
                out.open("case _", "case _")
                out.emit('print("no word")', '_p("no word")')
                out.close()
                out.close()
            return
        if k == "throw":
            c = self.bool_expr(scope, 1, risky)
            out.open(f"if {c[0]}", f"if {c[1]}")
            if self.chance(0.5):
                code = self.int_expr(scope, 2, False)
                out.emit(f"throw AppErr({code[0]})", f"raise AppErr({code[1]})")
            else:
                out.emit('throw ValueError("user")', 'raise ValueError("user")')
            out.close()
            return

    # -- main --------------------------------------------------------------
    def guarded(self, scope: Scope, body) -> Unit:
        """A main statement inside try/catch printing the exception kind."""
        out = Out()
        out.open("try", "try")
        body(out, Scope(scope))
        out.mid("catch AppErr as e", "except AppErr as e")
        out.emit('print(f"AppErr {e.code}")', '_p(f"AppErr {_show(e.code)}")')
        out.mid("catch ZeroDivisionError", "except ZeroDivisionError")
        out.emit('print("ZeroDivisionError")', '_p("ZeroDivisionError")')
        out.mid("catch IndexError", "except IndexError")
        out.emit('print("IndexError")', '_p("IndexError")')
        out.mid("catch OverflowError", "except OverflowError")
        out.emit('print("OverflowError")', '_p("OverflowError")')
        out.mid("catch ValueError", "except ValueError")
        out.emit('print("ValueError")', '_p("ValueError")')
        out.mid("catch e", "except Exception")
        out.emit('print("Exception")', '_p("Exception")')
        if self.chance(0.2):
            out.mid("finally", "finally")
            out.emit('print("finally")', '_p("finally")')
        out.close()
        return Unit(out.lam, out.py)

    def main_items(self, scope: Scope) -> list[Unit]:
        items: list[Unit] = []
        # Seed variables (safe initialisers).
        for ty in [INT, INT, STR, BOOL] + ([FLOAT] if self.has("float") else []) + ([LINT] if self.has("list") else []):
            out = Out()
            name = self.fresh("s")
            if ty == FLOAT:
                lit = self.rng.choice(["0.5", "2.25", "-1.5", "0.1", "3.0", "1e-05", "123456.75", "1e+21"])
                out.emit(f"{name}: float = {lit}", f"{name} = {lit}")
            elif ty == INT:
                lit = self.int_lit()
                out.emit(f"{name}: int = {lit[0]}", f"{name} = {lit[1]}")
            elif ty == STR:
                lit = self.str_lit(ascii=False)
                out.emit(f"{name}: str = {lit[0]}", f"{name} = {lit[1]}")
            elif ty == BOOL:
                out.emit(f"{name}: bool = true", f"{name} = True")
            else:
                lit = self.list_lit(LINT, scope, 3, False)
                out.emit(f"{name}: list[int] = {lit[0]}", f"{name} = {lit[1]}")
            items.append(Unit(out.lam, out.py))
            scope.vars[name] = Var(name, ty, mutable=True, owned=(ty == LINT), ascii=(ty != STR or '"' in out.lam[0] and all(ord(ch) < 128 for ch in out.lam[0])))
        n = self.rng.randint(6, 14)
        for _ in range(n):
            items.append(self.main_item(scope))
        return items

    def main_item(self, scope: Scope) -> Unit:
        kinds = ["safe_decl", "guard", "guard", "guard"]
        if self.has("result"):
            kinds.append("do")
        if self.has("async"):
            kinds.append("async")
        if self.has("with"):
            kinds.append("with")
        if self.has("class"):
            kinds.append("obj")
        if self.has("closure"):
            kinds.append("closure")
        if self.has("markup"):
            kinds.append("markup")
        if self.has("json"):
            kinds.append("json")
        if "Box" in self.classes:
            kinds.append("generic")
        k = self.pick(kinds)
        if k == "safe_decl":
            out = Out()
            self.stmt(out, scope, None, False, 0, False)
            return Unit(out.lam, out.py)
        if k == "guard":
            def body(out, inner):
                self.block(out, inner, None, self.rng.randint(1, 3), True, 1)
            return self.guarded(scope, body)
        if k == "do":
            out = Out()
            x = self.int_expr(scope, 1, False)
            out.open("do", "try")
            out.emit(f"r1: int = chain({x[0]})?", f"r1 = _q(chain({x[1]}))")
            out.emit('print(f"chain {r1}")', '_p(f"chain {_show(r1)}")')
            out.mid("catch err", "except _Prop as _perr")
            out.emit(None, "err = _perr.err")
            out.emit('print(f"err {err.kind} {err.message}")', '_p(f"err {err.kind} {err.message}")')
            out.close()
            y = self.int_expr(scope, 1, False)
            r = self.fresh("res")
            out.emit(f"{r}: Result[int] = parseNum({y[0]})", f"{r} = parseNum({y[1]})")
            out.emit(f"print({r}.ok(), {r}.unwrapOr(-1))", f"_p({r}.ok(), {r}.unwrapOr(-1))")
            return Unit(out.lam, out.py)
        if k == "async":
            def body(out, inner):
                h = self.fresh("h")
                x = self.int_expr(inner, 1, False)
                y = self.int_expr(inner, 1, False)
                h2 = self.fresh("h")
                # The argument is evaluated at the call, in the caller (an
                # exception there is not the task's): a lambda default.
                out.emit(f"{h} = work({x[0]})", f"{h} = _Task(lambda _a=({x[1]}): work(_a))")
                out.emit(f"{h2} = work({y[0]})", f"{h2} = _Task(lambda _a=({y[1]}): work(_a))")
                out.emit(f"print(await {h2})", f"_p({h2}.get())")
                out.emit(f"print(await {h})", f"_p({h}.get())")
            return self.guarded(scope, body)
        if k == "with":
            def body(out, inner):
                nm = self.str_lit(True)
                r = self.fresh("r")
                out.emit(None, f"{r} = Res({nm[1]})")
                out.open(f"with Res({nm[0]}) as {r}", "try")
                out.emit(f'print("in " + {r}.name)', f'_p("in " + {r}.name)')
                self.block(out, Scope(inner), None, self.rng.randint(0, 2), True, 2)
                out.py_close()
                out.py_open("finally")
                out.emit(None, f"{r}.close()")
                out.close()
            return self.guarded(scope, body)
        if k == "obj":
            out = Out()
            cls = self.pick([c for c in self.classes if c.startswith("Shape")])
            name = self.fresh("o")
            e = self.obj_expr(cls, scope, 1, False)
            out.emit(f"{name}: {cls} = {e[0]}", f"{name} = {e[1]}")
            out.emit(f"print({name})", f"_p({name})")
            out.emit(f"print({name}.describe(), {name}.scale(2))", f"_p({name}.describe(), {name}.scale(2))")
            if self.iface:
                out.emit(f"print(areaOf({name}, 1))", f"_p(areaOf({name}, 1))")
            if "ShapeSq" in self.classes:
                out.emit(f"print(isinstance({name}, ShapeSq))", f"_p(isinstance({name}, ShapeSq))")
                lst = self.fresh("shapes")
                out.emit(f"{lst}: list[Shape] = [{name}, ShapeSq(2, 3)]", f"{lst} = [{name}, ShapeSq(2, 3)]")
                out.emit(f"print({lst}.map(lambda s: s.area()))" if self.has("lambda") else f"print(len({lst}))",
                         f"_p(list(map(lambda s: s.area(), {lst})))" if self.has("lambda") else f"_p(len({lst}))")
            scope.vars[name] = Var(name, cls, mutable=False)
            return Unit(out.lam, out.py)
        if k == "closure":
            out = Out()
            f = self.fresh("add")
            b = self.int_expr(scope, 2, False)
            x = self.int_expr(scope, 2, False)
            out.emit(f"{f}: func(int) -> int = makeAdder({b[0]})", f"{f} = makeAdder({b[1]})")
            out.emit(f"print({f}({x[0]}), applyTwice({f}, 1))" if self.has("lambda") else f"print({f}({x[0]}))",
                     f"_p({f}({x[1]}), applyTwice({f}, 1))" if self.has("lambda") else f"_p({f}({x[1]}))")
            return Unit(out.lam, out.py)
        if k == "markup":
            out = Out()
            t = self.str_expr(scope, 1, False, False)
            a = self.str_expr(scope, 2, False, False)
            n = self.int_expr(scope, 2, False)
            h = self.fresh("h")
            if '"' in t[0] or '"' in a[0] or "{" in t[0] + a[0]:
                vt, va = self.fresh("t"), self.fresh("a")
                out.emit(f"{vt}: str = {t[0]}", f"{vt} = {t[1]}")
                out.emit(f"{va}: str = {a[0]}", f"{va} = {a[1]}")
                t, a = (vt, vt), (va, va)
            out.emit(f'{h}: html = <><p class="${{{a[0]}}}">${{{t[0]}}} #${{{n[0]}}}</p></>',
                     f"{h} = '<p class=\"' + _esca({a[1]}) + '\">' + _esct({t[1]}) + ' #' + _show({n[1]}) + '</p>'")
            out.emit(f"print({h})", f"_p({h})")
            return Unit(out.lam, out.py)
        if k == "json":
            out = Out()
            n = self.int_expr(scope, 2, False)
            s = self.str_expr(scope, 2, False, False)
            xs = self.list_expr(LINT, scope, 2, False)
            if '"' in s[0] or "{" in s[0]:
                vs = self.fresh("t")
                out.emit(f"{vs}: str = {s[0]}", f"{vs} = {s[1]}")
                s = (vs, vs)
            j = self.fresh("j")
            out.emit(f'{j}: json = {{"n": {n[0]}, "s": {s[0]}, "xs": {xs[0]}, "ok": true}}',
                     f'{j} = {{"n": {n[1]}, "s": {s[1]}, "xs": {xs[1]}, "ok": True}}')
            out.emit(f"print(Json.encode({j}).unwrap())", f"_p(_jenc({j}))")
            got = self.fresh("g")
            out.emit(f"{got}: int = {j}.n", f"{got} = {j}['n']")
            out.emit(f"print({got} + 1)", f"_p(_ck({got} + 1))")
            return Unit(out.lam, out.py)
        if k == "generic":
            out = Out()
            a = self.int_expr(scope, 1, False)
            b = self.int_expr(scope, 1, False)
            c = self.bool_expr(scope, 1, False)
            s1 = self.str_expr(scope, 2, False, False)
            out.emit(f"print(pick({a[0]}, {b[0]}, {c[0]}))", f"_p(pick({a[1]}, {b[1]}, {c[1]}))")
            out.emit(f"print(pick({s1[0]}, \"other\", {c[0]}))", f"_p(pick({s1[1]}, \"other\", {c[1]}))")
            return Unit(out.lam, out.py)
        out = Out()
        return Unit(out.lam, out.py)

    # -- program -----------------------------------------------------------
    def program(self) -> Program:
        header = []
        defs: list[Unit] = []
        if self.has("result"):
            header.append("from lamerrors import Result, Error")
        if self.has("json"):
            header.append("from lamjson import Json")
        if header:
            header.append("")
        self.gen_classes(defs)
        self.gen_helpers(defs)
        if self.has("gen"):
            self.gen_generator(defs)
        if self.has("result"):
            self.gen_result_funcs(defs)
        if self.has("async"):
            self.gen_async(defs)
        if self.has("with"):
            self.gen_resource(defs)
        for _ in range(self.rng.randint(1, 4)):
            self.gen_function(defs)
        scope = Scope()
        main = self.main_items(scope)
        return Program(self.seed, header, defs, main, set(self.features))


def generate(seed: int, features: set | None = None) -> Program:
    if features is None:
        rng = random.Random(seed ^ 0x5EED)
        # Most programs use everything; some use a random subset so a
        # failure is easier to attribute.
        if rng.random() < 0.6:
            features = set(FEATURES)
        else:
            features = {f for f in FEATURES if rng.random() < 0.5}
    return Gen(seed, features).program()


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------

def _env() -> dict:
    env = dict(os.environ)
    env.setdefault("LAMC_WORKERS", "1")
    return env


def lamc(src_path: Path, out_bin: Path, timeout: int = 120, extra=()) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PYTHON, str(COMPILER), "build", str(src_path), "-o", str(out_bin), *extra],
        capture_output=True, text=True, timeout=timeout, env=_env(), cwd=str(src_path.parent),
    )


def classify_build(proc: subprocess.CompletedProcess) -> str:
    """``ok``, ``diag`` (a Lam diagnostic), ``gofail`` or ``crash``."""
    out = proc.stderr + proc.stdout
    if "the generated Go did not build" in out or "Go build failed" in out:
        return "gofail"
    if "Traceback (most recent call last)" in out or "internal compiler error" in out:
        return "crash"
    if proc.returncode == 0:
        return "ok"
    if proc.returncode < 0:
        return "crash"
    if "error" not in out:
        return "crash"
    return "diag"


# Uncaught Python exceptions whose Lam counterpart the program must raise
# too (docs/SAFETY.md "Run-time exceptions"); any other twin failure is a
# fuzzer bug.
UNCAUGHT_RE = re.compile(r"^(ZeroDivisionError|IndexError|ValueError|OverflowError|TypeError|AttributeError)\b")


# An uncaught exception of a class the program declares (``AppErr``): the
# Lam program ends with ``panic: <its __str__>``.
USER_UNCAUGHT_RE = re.compile(r"^__main__\.(\w+): (.*)$")


def run_python(src: str, timeout: int = 30) -> tuple[int, str, str]:
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as f:
        f.write(src)
        path = f.name
    try:
        p = subprocess.run([sys.executable, path], capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return -9, "", "timeout"
    finally:
        os.unlink(path)


@dataclass
class CaseResult:
    seed: int
    status: str             # pass | discard | fail
    reason: str = ""
    detail: str = ""
    lam: str = ""
    py: str = ""
    secs: float = 0.0


def check_program(prog: Program, work: Path, defs=None, main=None, run_oracle=True) -> tuple[str, str, str]:
    """Build and run one program: (status, reason, detail)."""
    lam_src = prog.lam_source(defs, main)
    py_src = prog.py_source(defs, main)
    expected_exc = ""
    if run_oracle:
        rc, expected, perr = run_python(py_src)
        if rc == 99:
            return "discard", "int64", ""
        if rc == -9:
            # The twin does not finish in time (a generated loop that runs
            # too long): no oracle, regenerate.
            return "discard", "timeout", ""
        if rc != 0:
            # An uncaught exception the safety contract maps to a Lam
            # exception (docs/SAFETY.md): the Lam program must print the
            # same output, then end with ``panic: <Exception>`` (status 2).
            last = perr.strip().splitlines()[-1] if perr.strip() else ""
            m = UNCAUGHT_RE.match(last)
            user = USER_UNCAUGHT_RE.match(last)
            if m:
                expected_exc = m.group(1)
            elif user:
                # A program's own exception class: Lam reports its
                # ``__str__`` (``panic: AppErr(11)``).
                expected_exc = user.group(2)
            else:
                return "fail", "oracle", perr[-3000:]
    work.mkdir(parents=True, exist_ok=True)
    src = work / "prog.lam"
    src.write_text(lam_src, encoding="utf-8")
    binary = work / "prog.bin"
    try:
        proc = lamc(src, binary)
    except subprocess.TimeoutExpired:
        return "fail", "lamc-timeout", ""
    kind = classify_build(proc)
    if kind != "ok":
        return "fail", f"build-{kind}", (proc.stderr + proc.stdout)[-4000:]
    try:
        run = subprocess.run([str(binary)], capture_output=True, text=True, timeout=30, cwd=str(work))
    except subprocess.TimeoutExpired:
        return "fail", "run-timeout", ""
    finally:
        try:
            binary.unlink()
        except OSError:
            pass
    if expected_exc:
        if run.returncode != 2 or not run.stderr.startswith(f"panic: {expected_exc}"):
            return "fail", "uncaught", (
                f"expected an uncaught {expected_exc} (exit 2), got exit {run.returncode}: {run.stderr[:500]!r}"
            )
        if run.stdout != expected:
            return "fail", "output", _first_diff(expected, run.stdout)
        return "pass", "", ""
    if run.returncode != 0:
        return "fail", f"run-exit-{run.returncode}", run.stderr[-3000:]
    if not run_oracle:
        return "pass", "", ""
    if run.stdout != expected:
        diff = _first_diff(expected, run.stdout)
        return "fail", "output", diff
    return "pass", "", ""


def _first_diff(expected: str, actual: str) -> str:
    e, a = expected.splitlines(), actual.splitlines()
    for i in range(max(len(e), len(a))):
        el = e[i] if i < len(e) else "<missing>"
        al = a[i] if i < len(a) else "<missing>"
        if el != al:
            return f"line {i + 1}: expected {el!r}, got {al!r}"
    return "trailing newline difference"


def shrink_program(prog: Program, work: Path, reason: str, detail: str = "") -> tuple[list, list]:
    """Drop whole units while the failure keeps its reason and its first
    error (``signature``): dropping a definition must not turn the bug into
    an unrelated "undefined name"."""
    defs, main = list(prog.defs), list(prog.main)
    wanted = signature(detail) if reason.startswith(("build-", "gofail")) and detail else ""

    def still(d, m) -> bool:
        status, r, det = check_program(prog, work / "shrink", d, m)
        if status != "fail" or r != reason:
            return False
        return not wanted or signature(det) == wanted

    changed = True
    rounds = 0
    while changed and rounds < 6:
        changed = False
        rounds += 1
        for seq_name in ("main", "defs"):
            i = 0
            while i < len(main if seq_name == "main" else defs):
                if seq_name == "main":
                    cand_d, cand_m = defs, main[:i] + main[i + 1:]
                else:
                    cand_d, cand_m = defs[:i] + defs[i + 1:], main
                if still(cand_d, cand_m):
                    defs, main = cand_d, cand_m
                    changed = True
                else:
                    i += 1
    return defs, main


def gen_case(seed: int, out_dir: str | None, shrink: bool = True) -> CaseResult:
    t0 = time.time()
    prog = None
    for attempt in range(5):
        prog = generate(seed * 8 + attempt)
        with tempfile.TemporaryDirectory(prefix="lamfuzz_") as tmp:
            status, reason, detail = check_program(prog, Path(tmp))
            if status == "discard":
                continue
            res = CaseResult(seed, status, reason, detail, secs=time.time() - t0)
            if status == "fail":
                defs, main = (shrink_program(prog, Path(tmp), reason, detail) if shrink and reason != "lamc-timeout"
                              else (prog.defs, prog.main))
                res.lam = prog.lam_source(defs, main)
                res.py = prog.py_source(defs, main)
                _, _, res.detail = check_program(prog, Path(tmp) / "final", defs, main)
                res.detail = res.detail or detail
                _save_failure(out_dir, f"gen-{seed}", res)
            return res
    return CaseResult(seed, "discard", "int64", secs=time.time() - t0)


# ---------------------------------------------------------------------------
# Mutation mode
# ---------------------------------------------------------------------------

MUT_TOKENS = ["?", "await", "yield", "None", "self", "return", "break", ".", "[", "]", "}", "{", "(", ")",
              ",", ":", "->", "lambda", "int", "str", "list[int]", "dict[str, int]", "go!", "\"", "f\"{", "<>",
              "</>", "${", "catch", "finally", "base", "static", "async", "0", "-1", "9223372036854775807",
              "1e400", "x", "_", "**", "//", "%", "not", "and", "in", "is", "as", "@", "#-", "'"]
TYPE_NAMES = ["int", "str", "float", "bool", "list[int]", "list[str]", "dict[str, int]", "set[int]", "any",
              "json", "html", "Shape", "Result[int]", "func(int) -> int", "None", "byte", "tuple[int, str]"]


def mutate(src: str, rng: random.Random) -> str:
    lines = src.splitlines()
    for _ in range(rng.randint(1, 3)):
        if not lines:
            break
        op = rng.choice(["del_line", "dup_line", "swap_lines", "type", "ident", "token_ins", "token_del",
                         "literal", "op", "truncate", "indent_brace"])
        i = rng.randrange(len(lines))
        if op == "del_line":
            del lines[i]
        elif op == "dup_line":
            lines.insert(i, lines[i])
        elif op == "swap_lines":
            j = rng.randrange(len(lines))
            lines[i], lines[j] = lines[j], lines[i]
        elif op == "type":
            line = lines[i]
            hits = [m for m in re.finditer(r"\b(int|str|float|bool|list\[int\]|list\[str\]|dict\[str, int\])\b", line)]
            if hits:
                m = rng.choice(hits)
                lines[i] = line[:m.start()] + rng.choice(TYPE_NAMES) + line[m.end():]
        elif op == "ident":
            names = re.findall(r"\b[a-zA-Z_]\w*\b", src)
            line = lines[i]
            hits = list(re.finditer(r"\b[a-zA-Z_]\w*\b", line))
            if hits and names:
                m = rng.choice(hits)
                lines[i] = line[:m.start()] + rng.choice(names) + line[m.end():]
        elif op == "token_ins":
            line = lines[i]
            pos = rng.randint(0, len(line))
            lines[i] = line[:pos] + rng.choice(MUT_TOKENS) + line[pos:]
        elif op == "token_del":
            line = lines[i]
            toks = list(re.finditer(r"\w+|[^\w\s]", line))
            if toks:
                m = rng.choice(toks)
                lines[i] = line[:m.start()] + line[m.end():]
        elif op == "literal":
            line = lines[i]
            hits = list(re.finditer(r"\b\d+\b|\"[^\"]*\"", line))
            if hits:
                m = rng.choice(hits)
                lines[i] = line[:m.start()] + rng.choice(["0", "\"s\"", "1.5", "true", "None", "[]", "{}",
                                                          "99999999999999999999", "-0", "1 << 70", "(1, 2)"]) + line[m.end():]
        elif op == "op":
            line = lines[i]
            hits = list(re.finditer(r" (\+|-|\*|//|%|<|>|==|!=|and|or) ", line))
            if hits:
                m = rng.choice(hits)
                lines[i] = line[:m.start()] + " " + rng.choice(["+", "-", "*", "/", "//", "%", "**", "<<", ">>",
                                                              "==", "<", "and", "or", "in", "??", "&", "|"]) + " " + line[m.end():]
        elif op == "truncate":
            text = "\n".join(lines)
            cut = rng.randint(0, len(text))
            lines = text[:cut].splitlines()
        elif op == "indent_brace":
            line = lines[i]
            if line.rstrip().endswith("{"):
                lines[i] = line.rstrip()[:-1]
            elif line.strip() == "}":
                del lines[i]
    return "\n".join(lines) + "\n"


_CORPUS: list | None = None


def corpus() -> list:
    """The core test programs: valid Lam to mutate besides generated ones."""
    global _CORPUS
    if _CORPUS is None:
        _CORPUS = sorted(str(p) for p in (ROOT / "tests" / "tests" / "cases").rglob("*.lam"))
    return _CORPUS


def base_source(seed: int, rng: random.Random) -> str:
    files = corpus()
    if files and rng.random() < 0.5:
        return Path(rng.choice(files)).read_text(encoding="utf-8")
    return generate(seed * 8 + 1).lam_source()


def mutate_case(seed: int, out_dir: str | None, shrink: bool = True) -> CaseResult:
    t0 = time.time()
    rng = random.Random(seed ^ 0xA11CE)
    src = mutate(base_source(seed, rng), rng)
    return _check_hostile(seed, "mut", src, out_dir, t0, shrink)


def _check_hostile(seed: int, tag: str, src: str | bytes, out_dir: str | None, t0: float,
                   shrink: bool = True) -> CaseResult:
    with tempfile.TemporaryDirectory(prefix="lamfuzz_") as tmp:
        work = Path(tmp)
        path = work / "prog.lam"

        def probe(text) -> tuple[str, str]:
            if isinstance(text, bytes):
                path.write_bytes(text)
            else:
                path.write_text(text, encoding="utf-8", newline="")
            try:
                proc = lamc(path, work / "prog.bin", timeout=180)
            except subprocess.TimeoutExpired:
                return "timeout", ""
            return classify_build(proc), (proc.stderr + proc.stdout)[-4000:]

        kind, detail = probe(src)
        res = CaseResult(seed, "pass" if kind in ("ok", "diag") else "fail", kind, secs=time.time() - t0)
        if res.status == "fail":
            if shrink and isinstance(src, str) and kind != "timeout":
                wanted = signature(detail)

                def same_failure(s: str) -> bool:
                    k, d = probe(s)
                    return k == kind and signature(d) == wanted

                src = _shrink_lines(src, same_failure)
                kind, detail = probe(src)
            res.detail = detail
            res.lam = src if isinstance(src, str) else src.decode("utf-8", "replace")
            _save_failure(out_dir, f"{tag}-{seed}", res, raw=src if isinstance(src, bytes) else None)
        return res


def _shrink_lines(src: str, failing) -> str:
    lines = src.splitlines()
    chunk = max(1, len(lines) // 4)
    while chunk >= 1:
        i = 0
        while i < len(lines):
            cand = lines[:i] + lines[i + chunk:]
            if cand and failing("\n".join(cand) + "\n"):
                lines = cand
            else:
                i += chunk
        chunk //= 2
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Hostile inputs
# ---------------------------------------------------------------------------

def hostile_input(seed: int) -> str | bytes:
    rng = random.Random(seed ^ 0xB17E5)
    base = base_source(seed, rng)
    kind = rng.choice(["bytes", "truncate", "deep_paren", "deep_block", "deep_list", "huge_int", "huge_float",
                       "bom", "crlf", "unicode_ident", "nul", "long_line", "deep_unary", "huge_str",
                       "many_funcs", "lone_surrogate", "cr_only", "tabs"])
    if kind == "bytes":
        return bytes(rng.randrange(256) for _ in range(rng.randint(1, 400)))
    if kind == "truncate":
        return base[:rng.randint(0, len(base))]
    if kind == "deep_paren":
        n = rng.choice([50, 300, 1000, 3000])
        return "func main() {\n    x: int = " + "(" * n + "1" + ")" * n + "\n    print(x)\n}\n"
    if kind == "deep_unary":
        n = rng.choice([50, 500, 3000])
        return "func main() {\n    x: int = " + "-" * n + "1\n    print(x)\n}\n"
    if kind == "deep_block":
        n = rng.choice([20, 100, 400])
        s = "func main() {\n    x: int = 0\n"
        for i in range(n):
            s += "    " * (i + 1) + f"if x < {i + 10} {{\n"
        s += "x += 1\n" + "}\n" * n + "    print(x)\n}\n"
        return s
    if kind == "deep_list":
        n = rng.choice([30, 300, 1000])
        return "func main() {\n    x: list[int] = " + "[" * n + "]" * n + "\n}\n"
    if kind == "huge_int":
        return "func main() {\n    x: int = " + "9" * rng.choice([19, 20, 50, 5000]) + "\n    print(x)\n}\n"
    if kind == "huge_float":
        return "func main() {\n    x: float = 1e" + str(rng.choice([308, 309, 400, 100000])) + "\n    print(x)\n}\n"
    if kind == "huge_str":
        return 'func main() {\n    s: str = "' + "a" * rng.choice([1000, 200000]) + '"\n    print(len(s))\n}\n'
    if kind == "bom":
        return b"\xef\xbb\xbf" + base.encode("utf-8")
    if kind == "crlf":
        return base.replace("\n", "\r\n")
    if kind == "cr_only":
        return base.replace("\n", "\r")
    if kind == "tabs":
        return base.replace("    ", "\t")
    if kind == "unicode_ident":
        return "func main() {\n    héllo: int = 1\n    日本: str = \"x\"\n    print(héllo, 日本)\n}\n"
    if kind == "nul":
        pos = rng.randint(0, len(base))
        return base[:pos] + "\x00" + base[pos:]
    if kind == "long_line":
        return "func main() {\n    x: int = " + " + ".join(["1"] * rng.choice([100, 5000])) + "\n    print(x)\n}\n"
    if kind == "many_funcs":
        n = rng.choice([50, 500])
        return "".join(f"func f{i}(x: int) -> int {{ return x + {i} }}\n" for i in range(n)) + "func main() {\n    print(f1(2))\n}\n"
    if kind == "lone_surrogate":
        return b'func main() {\n    s: str = "\xed\xa0\x80"\n    print(s)\n}\n'
    return base


def bytes_case(seed: int, out_dir: str | None, shrink: bool = True) -> CaseResult:
    t0 = time.time()
    return _check_hostile(seed, "bytes", hostile_input(seed), out_dir, t0, shrink)


# ---------------------------------------------------------------------------
# Reporting / CLI
# ---------------------------------------------------------------------------

def _save_failure(out_dir: str | None, name: str, res: CaseResult, raw: bytes | None = None) -> None:
    if not out_dir:
        return
    d = Path(out_dir)
    d.mkdir(parents=True, exist_ok=True)
    if raw is not None:
        (d / f"{name}.lam").write_bytes(raw)
    else:
        (d / f"{name}.lam").write_text(res.lam, encoding="utf-8")
    if res.py:
        (d / f"{name}.py").write_text(res.py, encoding="utf-8")
    (d / f"{name}.txt").write_text(f"reason: {res.reason}\n\n{res.detail}\n", encoding="utf-8")


MODES = {"gen": gen_case, "mutate": mutate_case, "bytes": bytes_case}


def run_campaign(mode: str, seed: int, count: int, jobs: int, out_dir: str | None,
                 shrink: bool = True, quiet: bool = False) -> list[CaseResult]:
    fn = MODES[mode]
    results: list[CaseResult] = []
    t0 = time.time()
    seeds = [seed + i for i in range(count)]
    if jobs <= 1:
        for s in seeds:
            results.append(fn(s, out_dir, shrink))
            _progress(results, count, t0, quiet)
    else:
        with ProcessPoolExecutor(max_workers=jobs) as ex:
            futs = [ex.submit(fn, s, out_dir, shrink) for s in seeds]
            for fut in as_completed(futs):
                results.append(fut.result())
                _progress(results, count, t0, quiet)
    results.sort(key=lambda r: r.seed)
    return results


def _progress(results, count, t0, quiet) -> None:
    if quiet:
        return
    r = results[-1]
    if r.status == "fail" and not quiet:
        print(f"  FAIL seed={r.seed} {r.reason}: {signature(r.detail)}", flush=True)
    n = len(results)
    if n % 50 == 0 or n == count:
        fails = sum(1 for x in results if x.status == "fail")
        print(f"  [{n}/{count}] {fails} failing, {time.time() - t0:.0f}s", flush=True)


def signature(detail: str) -> str:
    """A normalised first error line, to group failures by root cause."""
    for line in detail.splitlines():
        text = line.strip()
        m = re.match(r"line \d+: (.*)", text)
        if m and "warning" not in text:
            text = m.group(1)
        elif not (text.startswith("error") or "Traceback" in text or "internal compiler error" in text
                  or text.startswith("panic:") or text.startswith("line ")):
            continue
        if ("warning" in text or text.startswith("error: semantic check failed")
                or text.startswith("error: Go build failed") or "the generated Go did not build" in text):
            continue
        text = re.sub(r"\b([a-z]+)\d+\b", r"\1N", text)
        text = re.sub(r"\b\d+\b", "N", text)
        text = re.sub(r'"[^"]*"', '"S"', text)
        return text[:160]
    return detail.strip().splitlines()[-1][:160] if detail.strip() else ""


def summarize(mode: str, results: list[CaseResult]) -> dict:
    counts: dict[str, int] = {}
    for r in results:
        key = r.status if r.status != "fail" else f"fail:{r.reason}"
        counts[key] = counts.get(key, 0) + 1
    sigs: dict[str, list[int]] = {}
    for r in results:
        if r.status == "fail":
            sigs.setdefault(f"{r.reason}: {signature(r.detail)}", []).append(r.seed)
    return {"mode": mode, "total": len(results), "counts": counts,
            "failing_seeds": [r.seed for r in results if r.status == "fail"], "signatures": sigs}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Type-aware Lam program fuzzer (see docs/SAFETY.md).")
    ap.add_argument("--mode", choices=sorted(MODES) + ["all"], default="gen")
    ap.add_argument("--seed", type=int, default=1, help="first seed (cases are seed, seed+1, ...)")
    ap.add_argument("--count", type=int, default=100)
    ap.add_argument("--jobs", "-j", type=int, default=max(1, (os.cpu_count() or 2) // 2))
    ap.add_argument("--out", default="fuzz-failures", help="directory for failing cases ('' to disable)")
    ap.add_argument("--no-shrink", action="store_true")
    ap.add_argument("--show", type=int, metavar="SEED", help="print the Lam program (and its Python twin) of a gen seed")
    ap.add_argument("--json", action="store_true", help="print the summary as JSON")
    args = ap.parse_args(argv)
    if args.show is not None:
        prog = generate(args.show * 8)
        print(prog.lam_source())
        print("# ---- python twin ----")
        print(prog.py_source().replace(PRELUDE, "# <prelude>\n"))
        return 0
    modes = sorted(MODES) if args.mode == "all" else [args.mode]
    failed = False
    summaries = []
    for mode in modes:
        print(f"lamfuzz: mode={mode} seed={args.seed} count={args.count} jobs={args.jobs}", flush=True)
        results = run_campaign(mode, args.seed, args.count, args.jobs, args.out or None, not args.no_shrink)
        s = summarize(mode, results)
        summaries.append(s)
        failed = failed or bool(s["failing_seeds"])
        print(f"lamfuzz: {mode}: {s['counts']}", flush=True)
        for sig, seeds in sorted(s["signatures"].items(), key=lambda kv: -len(kv[1])):
            print(f"  {len(seeds):4d}x {sig}  (seeds {', '.join(map(str, seeds[:6]))})", flush=True)
    if args.json:
        print(json.dumps(summaries))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
