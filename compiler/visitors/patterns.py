#!/usr/bin/env python3
"""``match`` statement lowering.

Every ``case`` pattern is compiled into straight-line Go statements over a
*source expression* with a known Lam type:

* ``test`` mode: the statements of a ``func() bool { ... }()`` condition;
  every check is ``if !(cond) { return false }`` and the pattern's names
  are bound (``x := ...``) so a guard can read them;
* ``bind`` mode: the same extraction without checks, emitted at the top of
  the case body (the case only runs after its condition succeeded);
* ``assign`` mode: like ``test`` but binds with ``=`` into variables
  declared up front (or-patterns whose alternatives bind names).

A ``match`` becomes ``{ __lamMatchN := subject; if c1 { ... } else if c2
{ ... } else { ... } }``. An if/else chain keeps Go's terminating-statement
analysis (a match whose cases all return is a terminating statement) and,
unlike a ``switch``, lets ``break`` / ``continue`` in a case body target the
enclosing loop. Pure literal matches over scalar subjects without guards,
bindings or loop exits still lower to a Go ``switch``.

Dynamic subjects (``any``, unknown, the raw value inside ``json``) use small
helper closures declared at the top of the match block (only the ones the
patterns need): numeric-aware equality, sequence/mapping views (reflection
for typed slices/maps hidden in ``any``) and integer narrowing.
"""

from __future__ import annotations

import re
from typing import List, Optional, Tuple

from lark import Token, Tree

from compiler.constants import TYPE_MAP
from compiler.typesys import (
    DictType,
    GenericType,
    ListType,
    NamedType,
    Type,
    UnionType,
    parse_type,
)

_SCALAR_GO = {"int", "int8", "int16", "int32", "int64", "uint", "float64",
              "float32", "string", "bool", "byte", "rune"}
_BUILTIN_CLASS_PATTERNS = {"int", "float", "str", "bool", "list", "dict", "json"}
_ANY = NamedType("any")
_JSON = NamedType("json")

# Helper closures, in dependency order. ``__lamMatchRaw`` unwraps a ``json``
# value hidden in ``any`` (``LamJSON`` implements ``LamMatchRaw``).
_HELPERS = {
    "raw": (
        "__lamMatchRaw := func(v interface{}) interface{} { "
        "if j, ok := v.(interface{ LamMatchRaw() interface{} }); ok { return j.LamMatchRaw() }; "
        "return v }"
    ),
    "num": (
        "__lamMatchNum := func(v interface{}) (float64, bool) { "
        "switch n := __lamMatchRaw(v).(type) { "
        "case int: return float64(n), true; case int8: return float64(n), true; "
        "case int16: return float64(n), true; case int32: return float64(n), true; "
        "case int64: return float64(n), true; case uint: return float64(n), true; "
        "case uint8: return float64(n), true; case uint16: return float64(n), true; "
        "case uint32: return float64(n), true; case uint64: return float64(n), true; "
        "case float32: return float64(n), true; case float64: return n, true }; "
        "return 0, false }"
    ),
    "int": (
        "__lamMatchInt := func(v interface{}) (int, bool) { "
        "switch n := __lamMatchRaw(v).(type) { "
        "case int: return n, true; case int8: return int(n), true; case int16: return int(n), true; "
        "case int32: return int(n), true; case int64: return int(n), true; case uint: return int(n), true; "
        "case uint8: return int(n), true; case uint16: return int(n), true; case uint32: return int(n), true; "
        "case uint64: return int(n), true }; return 0, false }"
    ),
    "float": (
        "__lamMatchFloat := func(v interface{}) (float64, bool) { "
        "switch n := __lamMatchRaw(v).(type) { case float64: return n, true; "
        "case float32: return float64(n), true }; return 0, false }"
    ),
    "eq": (
        "__lamMatchEq := func(v interface{}, w interface{}) bool { "
        "v = __lamMatchRaw(v); "
        "if a, ok := __lamMatchNum(v); ok { if b, ok := __lamMatchNum(w); ok { return a == b }; return false }; "
        "if _, ok := __lamMatchNum(w); ok { return false }; "
        "defer func() { recover() }(); return v == w }"
    ),
    "seq": (
        "__lamMatchSeq := func(v interface{}) ([]interface{}, bool) { "
        "v = __lamMatchRaw(v); "
        "switch s := v.(type) { case []interface{}: return s, true; case string, []byte, nil: return nil, false }; "
        "rv := reflect.ValueOf(v); "
        "if rv.Kind() != reflect.Slice && rv.Kind() != reflect.Array { return nil, false }; "
        "out := make([]interface{}, rv.Len()); "
        "for i := range out { out[i] = rv.Index(i).Interface() }; return out, true }"
    ),
    "map": (
        "__lamMatchMap := func(v interface{}) (map[string]interface{}, bool) { "
        "v = __lamMatchRaw(v); "
        "if m, ok := v.(map[string]interface{}); ok { return m, true }; "
        "rv := reflect.ValueOf(v); "
        "if rv.Kind() != reflect.Map || rv.Type().Key().Kind() != reflect.String { return nil, false }; "
        "out := make(map[string]interface{}, rv.Len()); iter := rv.MapRange(); "
        "for iter.Next() { out[iter.Key().String()] = iter.Value().Interface() }; return out, true }"
    ),
}
_HELPER_DEPS = {
    "num": ("raw",), "int": ("raw",), "float": ("raw",), "eq": ("raw", "num"),
    "seq": ("raw",), "map": ("raw",),
}
_HELPER_ORDER = ("raw", "num", "int", "float", "eq", "seq", "map")


class _Src:
    """A pattern source: Go expression, Lam type, raw-json storage flag."""

    __slots__ = ("expr", "type", "raw")

    def __init__(self, expr: str, type_: Optional[Type], raw: bool = False):
        self.expr = expr
        self.type = type_
        self.raw = raw


class PatternMatchMixin:
    """Lowering of ``match`` statements (see the module docstring)."""

    # ─── Pre-pass ──────────────────────────────────────────────

    def _normalize_match_patterns(self, node) -> None:
        """Turn value/class-name pattern nodes into expression trees.

        ``Color.RED`` and ``Point(...)`` parse as ``value`` nodes holding
        raw NAME tokens. Rewriting them to ``value_pattern(var/getattr)``
        before the namespacing pass lets imported module members and
        renamed declarations resolve like any other expression.
        """
        if not isinstance(node, Tree):
            return
        if node.data == "case" and node.children:
            node.children[0] = self._normalize_pattern_tree(node.children[0])
        for child in node.children:
            if isinstance(child, Tree):
                self._normalize_match_patterns(child)

    def _normalize_pattern_tree(self, pat):
        if not isinstance(pat, Tree):
            return pat
        if pat.data == "value":
            names = [str(c) for c in pat.children]
            expr = Tree("var", [Tree("name", [Token("NAME", names[0])])])
            for attr in names[1:]:
                expr = Tree("getattr", [expr, Tree("name", [Token("NAME", attr)])])
            return Tree("value_pattern", [expr])
        if pat.data in {"guard"}:
            return pat
        pat.children = [self._normalize_pattern_tree(c) for c in pat.children]
        return pat

    # ─── Types ─────────────────────────────────────────────────

    def _pm_go_type(self, t: Optional[Type]) -> str:
        """Go spelling of a Lam type (``""`` when unknown)."""
        if t is None:
            return ""
        if isinstance(t, NamedType):
            name = t.name
            if name in TYPE_MAP:
                return TYPE_MAP[name]
            if name in self._generic_names:
                return name
            if name in self._interfaces:
                return self._go_class_name(name)
            if name in self._class_names:
                return "*" + self._go_class_name(name)
            return "interface{}"
        if isinstance(t, ListType):
            return "[]" + (self._pm_go_type(t.item) or "interface{}")
        if isinstance(t, DictType):
            return f"map[{self._pm_go_type(t.key) or 'string'}]{self._pm_go_type(t.value) or 'interface{}'}"
        if isinstance(t, GenericType):
            args = [self._pm_go_type(a) or "interface{}" for a in t.args]
            if t.base == "set":
                return f"map[{args[0]}]bool" if args else "map[interface{}]bool"
            if t.base == "tuple":
                return "[]interface{}"
            if t.base in {"optional", "Option"}:
                if not args:
                    return "interface{}"
                return args[0] if args[0].startswith("*") or args[0] == "interface{}" else "*" + args[0]
            if t.base in self._generic_classes:
                return f"*{self._go_class_name(t.base)}[{', '.join(args)}]"
            return "interface{}"
        return "interface{}"

    def _pm_type_from_go(self, go: str) -> Optional[Type]:
        if not go:
            return None
        rev = {"int": "int", "int8": "int8", "int16": "int16", "int32": "int32",
               "int64": "int64", "uint": "uint", "float64": "float", "float32": "float32",
               "string": "str", "bool": "bool", "byte": "byte", "rune": "rune",
               "LamJSON": "json", "interface{}": "any", "[]byte": "bytes"}
        if go in rev:
            return NamedType(rev[go])
        if go.startswith("[]"):
            return ListType(self._pm_type_from_go(go[2:]) or _ANY)
        if go.startswith("map["):
            key, value = self._split_go_map_type(go)
            if value == "bool" and key and key != "string":
                return GenericType("set", (self._pm_type_from_go(key) or _ANY,))
            return DictType(self._pm_type_from_go(key) or NamedType("str"), self._pm_type_from_go(value) or _ANY)
        if go.startswith("*"):
            base = go[1:].split("[", 1)[0]
            for name in self._class_names:
                if self._go_class_name(name) == base:
                    return NamedType(name)
            return None
        for name in self._interfaces:
            if self._go_class_name(name) == go:
                return NamedType(name)
        if go in self._generic_names:
            return NamedType(go)
        return None

    def _pm_subject_type(self, node) -> Optional[Type]:
        go = self._expr_go_type_hint(node) or ""
        if isinstance(node, Tree) and node.data == "var" and node.children:
            name = self._get_name(node.children[0])
            lam = getattr(self, "_var_lam_types", {}).get(name)
            if lam is not None and (not go or self._pm_go_type(lam) == go):
                return lam
            class_name = self._var_types.get(name, "")
            if not go and class_name:
                return NamedType(class_name)
        if not go:
            inferred = self._infer_type_from_value(node)
            if inferred and inferred != "interface{}":
                go = inferred
        if not go and isinstance(node, Tree):
            class_name = self._infer_receiver_class(node) if node.data in {"funccall", "getattr", "var"} else ""
            if class_name and class_name in self._class_names:
                return NamedType(class_name)
        return self._pm_type_from_go(go)

    @staticmethod
    def _pm_is_dynamic(src: _Src) -> bool:
        t = src.type
        if src.raw or t is None:
            return True
        if isinstance(t, NamedType) and t.name in {"any", "object"}:
            return True
        return isinstance(t, UnionType)

    def _pm_is_json(self, src: _Src) -> bool:
        return not src.raw and isinstance(src.type, NamedType) and src.type.name == "json"

    def _pm_class_of(self, t: Optional[Type]) -> str:
        if isinstance(t, NamedType) and t.name in self._class_names:
            return t.name
        if isinstance(t, GenericType) and t.base in self._class_names:
            return t.base
        if isinstance(t, GenericType) and t.base in {"optional", "Option"} and t.args:
            return self._pm_class_of(t.args[0])
        return ""

    def _pm_is_interface(self, t: Optional[Type]) -> bool:
        return isinstance(t, NamedType) and t.name in self._interfaces

    def _pm_ancestors(self, class_name: str) -> List[str]:
        out: List[str] = []
        todo = list(self.class_bases.get(class_name, []))
        while todo:
            base = todo.pop(0)
            if base not in out:
                out.append(base)
                todo.extend(self.class_bases.get(base, []))
        return out

    def _pm_class_fields(self, class_name: str, type_args: Tuple[Type, ...] = ()) -> List[Tuple[str, Optional[Type]]]:
        """``(field, Lam type)`` in declaration order, ancestors first."""
        chain = list(reversed(self._pm_ancestors(class_name))) + [class_name]
        seen = set()
        out: List[Tuple[str, Optional[Type]]] = []
        subst = {}
        clause = self._generic_classes.get(class_name, "")
        if clause and type_args:
            names = [p.strip().split()[0] for p in clause.strip("[]").split(",") if p.strip()]
            subst = dict(zip(names, type_args))
        for cls in chain:
            fields = list(self.class_fields.get(cls, []))
            if not fields:
                fields = list(self._external_field_types.get(cls, {}).items())
            for name, go in fields:
                if name in seen:
                    continue
                seen.add(name)
                if go in subst:
                    out.append((name, subst[go]))
                else:
                    out.append((name, self._pm_type_from_go(go)))
        return out

    # ─── Pattern helpers ───────────────────────────────────────

    def _pm_tmp(self) -> str:
        self._pm_counter = getattr(self, "_pm_counter", 0) + 1
        return f"__lamP{self._pm_counter}"

    def _pm_need(self, helper: str) -> None:
        stack = [helper]
        while stack:
            h = stack.pop()
            if h not in self._pm_helpers:
                self._pm_helpers.add(h)
                stack.extend(_HELPER_DEPS.get(h, ()))
        if helper in {"seq", "map"}:
            self._need_import("reflect")

    def _pm_let(self, out: List[str], mode: str, expr: str, has_ok: bool) -> str:
        var = self._pm_tmp()
        if has_ok:
            if mode == "bind":
                out.append(f"{var}, _ := {expr}")
            else:
                out.append(f"{var}, __lamOK := {expr}")
                out.append("if !__lamOK { return false }")
        else:
            out.append(f"{var} := {expr}")
        out.append(f"_ = {var}")
        return var

    @staticmethod
    def _pm_check(out: List[str], mode: str, cond: str) -> None:
        if mode != "bind":
            out.append(f"if !({cond}) {{ return false }}")

    def _pm_bind(self, out: List[str], mode: str, binds: list, name: str, src: _Src) -> None:
        if name == "_":
            return
        go_name = self._go_binding_name(name)
        expr, type_ = src.expr, src.type
        if src.raw:
            expr, type_ = f"lamJSONWrapRaw({expr})", _JSON
        if mode == "assign":
            out.append(f"{go_name} = {expr}")
        else:
            out.append(f"{go_name} := {expr}")
            out.append(f"_ = {go_name}")
        binds.append((name, go_name, type_))

    @staticmethod
    def _pm_has_bindings(pat) -> bool:
        if not isinstance(pat, Tree):
            return False
        if pat.data in {"capture_pattern", "star_pattern"}:
            return str(pat.children[0]) != "_"
        if pat.data == "as_pattern":
            return True
        if pat.data == "mapping_star_pattern":
            return True
        if pat.data == "value_pattern":
            return False
        return any(PatternMatchMixin._pm_has_bindings(c) for c in pat.children)

    def _pm_literal_go(self, pat) -> Tuple[str, str]:
        """``(go_expr, kind)`` of a literal pattern; kind in num/str/bool/none."""
        if isinstance(pat, Tree) and pat.data == "literal_pattern":
            pat = pat.children[0]
        d = pat.data
        if d == "number":
            return self._number_to_go(pat), "num"
        if d == "negative_number":
            return "-" + self._number_to_go(pat.children[0]), "num"
        if d == "string":
            return self._expr_to_go(pat), "str"
        if d == "const_true":
            return "true", "bool"
        if d == "const_false":
            return "false", "bool"
        return "nil", "none"

    # ─── Pattern compiler ──────────────────────────────────────

    def _pm_gen(self, pat, src: _Src, mode: str, out: List[str], binds: list) -> None:
        if mode == "bind" and not self._pm_has_bindings(pat):
            return
        d = pat.data if isinstance(pat, Tree) else ""
        if d == "any_pattern":
            return
        if d == "capture_pattern":
            self._pm_bind(out, mode, binds, str(pat.children[0]), src)
            return
        if d == "as_pattern":
            inner, name = pat.children[0], str(pat.children[-1])
            narrowed = self._pm_gen_narrowing(inner, src, mode, out, binds)
            self._pm_bind(out, mode, binds, name, narrowed)
            return
        if d == "or_pattern":
            self._pm_gen_or(pat, src, mode, out, binds)
            return
        if self._pm_is_json(src) and d not in {"value_pattern"}:
            if d == "literal_pattern" and self._pm_literal_go(pat)[1] == "none":
                self._pm_check(out, mode, f"lamJSONIsNull({src.expr})")
                return
            src = _Src(f"lamJSONRaw({src.expr})", _JSON, raw=True)
        if d == "literal_pattern":
            lit, kind = self._pm_literal_go(pat)
            if self._pm_is_dynamic(src):
                self._pm_need("eq")
                self._pm_check(out, mode, f"__lamMatchEq({src.expr}, {lit})")
            else:
                self._pm_check(out, mode, f"{src.expr} == {lit}")
            return
        if d == "value_pattern":
            value = self._expr_to_go(pat.children[0])
            if self._pm_is_dynamic(src) or self._pm_is_json(src):
                self._pm_need("eq")
                expr = f"lamJSONRaw({src.expr})" if self._pm_is_json(src) else src.expr
                self._pm_check(out, mode, f"__lamMatchEq({expr}, {value})")
            else:
                self._pm_check(out, mode, f"{src.expr} == {value}")
            return
        if d == "sequence_pattern":
            self._pm_gen_sequence(pat, src, mode, out, binds)
            return
        if d in {"mapping_pattern", "mapping_star_pattern"}:
            self._pm_gen_mapping(pat, src, mode, out, binds)
            return
        if d == "class_pattern":
            self._pm_gen_class(pat, src, mode, out, binds)
            return
        # Unknown pattern shape: never matches (the semantic checker
        # reports unsupported forms before emission).
        self._pm_check(out, mode, "false")

    def _pm_gen_narrowing(self, pat, src: _Src, mode, out, binds) -> _Src:
        """Compile ``pat`` and return the source an ``as`` name binds.

        Class patterns narrow (``case Point() as p`` binds the ``Point``);
        every other pattern binds the matched value with its own type.
        """
        if isinstance(pat, Tree) and pat.data == "class_pattern":
            return self._pm_gen_class(pat, src, mode, out, binds)
        self._pm_gen(pat, src, mode, out, binds)
        return src

    def _pm_gen_or(self, pat, src: _Src, mode, out, binds) -> None:
        alts = [c for c in pat.children if isinstance(c, Tree)]
        if not self._pm_has_bindings(pat):
            if mode == "bind":
                return
            conds = [self._pm_condition(alt, src, [], None) for alt in alts]
            self._pm_check(out, mode, " || ".join(f"({c})" for c in conds))
            return
        # Alternatives bind the same names: declare them once, then try
        # each alternative in ``assign`` mode.
        probe: list = []
        self._pm_gen(alts[0], src, "bind", [], probe)
        names = [(name, go_name, type_) for name, go_name, type_ in probe]
        alt_types = []
        for alt in alts[1:]:
            other: list = []
            self._pm_gen(alt, src, "bind", [], other)
            alt_types.append({n: t for n, _, t in other})
        decls = []
        for name, go_name, type_ in names:
            same = all(self._pm_go_type(types.get(name)) == self._pm_go_type(type_) for types in alt_types)
            final_type = type_ if same else _ANY
            go_type = self._pm_go_type(final_type) or "interface{}"
            if mode != "assign":
                out.append(f"var {go_name} {go_type}")
                out.append(f"_ = {go_name}")
            decls.append((name, go_name, final_type, same))
        attempts = []
        for alt in alts:
            body: list = []
            self._pm_gen(alt, src, "assign", body, [])
            attempts.append("func() bool { " + "; ".join(body + ["return true"]) + " }()")
        cond = " || ".join(attempts)
        if mode == "bind":
            out.append(f"_ = {cond}")
        else:
            self._pm_check(out, mode, cond)
        for name, go_name, final_type, _same in decls:
            binds.append((name, go_name, final_type))

    def _pm_seq_parts(self, pat) -> Tuple[list, Optional[str], list]:
        before, star, after = [], None, []
        for child in pat.children:
            if not isinstance(child, Tree):
                continue
            if child.data == "star_pattern":
                star = str(child.children[0])
            elif star is None:
                before.append(child)
            else:
                after.append(child)
        return before, star, after

    def _pm_gen_sequence(self, pat, src: _Src, mode, out, binds) -> None:
        before, star, after = self._pm_seq_parts(pat)
        t = src.type
        n_fixed = len(before) + len(after)
        elem_types: List[Optional[Type]]
        if self._pm_is_dynamic(src):
            self._pm_need("seq")
            seq = self._pm_let(out, mode, f"__lamMatchSeq({src.expr})", True)
            elem = _ANY if not src.raw else _JSON
            elem_go = "interface{}"
            elem_types = [elem] * n_fixed
            assert_types = [None] * n_fixed
            rest_type: Type = ListType(_ANY)
            raw = src.raw
        elif isinstance(t, ListType) or (isinstance(t, GenericType) and t.base == "tuple"):
            seq = src.expr
            raw = False
            if isinstance(t, ListType):
                elem_types = [t.item] * n_fixed
                assert_types = [None] * n_fixed
                elem_go = self._pm_go_type(t.item) or "interface{}"
                rest_type = t
            else:
                args = list(t.args)
                fixed = args[:len(before)] + (args[len(args) - len(after):] if after else [])
                if star is None and len(args) != n_fixed:
                    self._pm_check(out, mode, "false")
                    return
                elem_types = [a if self._pm_go_type(a) not in {"", "interface{}"} else _ANY for a in fixed]
                assert_types = [self._pm_go_type(a) if self._pm_go_type(a) not in {"", "interface{}"} else None for a in fixed]
                elem_go = "interface{}"
                rest_type = ListType(_ANY)
        else:
            self._pm_check(out, mode, "false")
            return
        if star is None:
            self._pm_check(out, mode, f"len({seq}) == {n_fixed}")
        elif n_fixed:
            self._pm_check(out, mode, f"len({seq}) >= {n_fixed}")
        positions = [f"{seq}[{i}]" for i in range(len(before))]
        positions += [f"{seq}[len({seq})-{len(after) - j}]" for j in range(len(after))]
        for idx, (sub, pos) in enumerate(zip(before + after, positions)):
            elem_src = pos
            if assert_types[idx]:
                elem_src = f"{pos}.({assert_types[idx]})"
            if isinstance(sub, Tree) and sub.data in {"capture_pattern", "any_pattern"}:
                self._pm_gen(sub, _Src(elem_src, elem_types[idx], raw), mode, out, binds)
                continue
            if mode == "bind" and not self._pm_has_bindings(sub):
                continue
            var = self._pm_let(out, mode, elem_src, False)
            self._pm_gen(sub, _Src(var, elem_types[idx], raw), mode, out, binds)
        if star is not None and star != "_":
            hi = f"len({seq})-{len(after)}" if after else f"len({seq})"
            rest_go = "[]interface{}" if self._pm_is_dynamic(src) else (self._pm_go_type(rest_type) or "[]interface{}")
            expr = f"append({rest_go}(nil), {seq}[{len(before)}:{hi}]...)"
            if src.raw:
                self._pm_bind(out, mode, binds, star, _Src(expr, _JSON, raw=True))
            else:
                self._pm_bind(out, mode, binds, star, _Src(expr, rest_type if not self._pm_is_dynamic(src) else ListType(_ANY)))

    def _pm_gen_mapping(self, pat, src: _Src, mode, out, binds) -> None:
        items = [c for c in pat.children if isinstance(c, Tree) and c.data == "mapping_item_pattern"]
        rest = None
        if pat.data == "mapping_star_pattern":
            tail = [c for c in pat.children if isinstance(c, Token)]
            rest = str(tail[-1]) if tail else None
        t = src.type
        if self._pm_is_dynamic(src):
            self._pm_need("map")
            mp = self._pm_let(out, mode, f"__lamMatchMap({src.expr})", True)
            value_type: Type = _JSON if src.raw else _ANY
            map_go = "map[string]interface{}"
            rest_type: Type = DictType(NamedType("str"), _ANY)
            raw = src.raw
        elif isinstance(t, DictType):
            mp = src.expr
            value_type = t.value
            map_go = self._pm_go_type(t)
            rest_type = t
            raw = False
            if mode != "bind":
                self._pm_check(out, mode, f"{mp} != nil")
        else:
            self._pm_check(out, mode, "false")
            return
        keys = []
        for item in items:
            key_node, sub = item.children[0], item.children[1]
            if isinstance(key_node, Tree) and key_node.data == "value_pattern":
                key = self._expr_to_go(key_node.children[0])
            else:
                key, _kind = self._pm_literal_go(key_node)
            keys.append(key)
            if mode == "bind" and not self._pm_has_bindings(sub):
                continue
            var = self._pm_let(out, mode, f"{mp}[{key}]", True)
            self._pm_gen(sub, _Src(var, value_type, raw), mode, out, binds)
        if rest and rest != "_":
            excluded = " || ".join(f"k == {k}" for k in keys) or "false"
            expr = (
                f"func() {map_go} {{ out := {map_go}{{}}; for k, v := range {mp} {{ "
                f"if {excluded} {{ continue }}; out[k] = v }}; return out }}()"
            )
            self._pm_bind(out, mode, binds, rest, _Src(expr, _JSON if raw else rest_type, raw))

    def _pm_class_pattern_parts(self, pat) -> Tuple[object, list, list]:
        callee = pat.children[0]
        positional, keywords = [], []
        args = pat.children[1] if len(pat.children) > 1 else None
        if isinstance(args, Tree):
            for arg in args.children:
                if not isinstance(arg, Tree):
                    continue
                if arg.data == "keyw_arg_pattern":
                    keywords.append((str(arg.children[0]), arg.children[1]))
                else:
                    positional.append(arg)
        return callee, positional, keywords

    def _pm_pattern_class_name(self, callee) -> str:
        expr = callee.children[0] if isinstance(callee, Tree) and callee.data == "value_pattern" else callee
        if isinstance(expr, Tree) and expr.data == "var":
            return self._get_name(expr.children[0])
        if isinstance(expr, Tree) and expr.data in {"getattr", "getattr_safe"}:
            return self._get_name(expr.children[1])
        return ""

    def _pm_gen_class(self, pat, src: _Src, mode, out, binds) -> _Src:
        """Compile a class pattern; return the narrowed source."""
        callee, positional, keywords = self._pm_class_pattern_parts(pat)
        name = self._pm_pattern_class_name(callee)
        if name in _BUILTIN_CLASS_PATTERNS and name not in self._class_names:
            narrowed = self._pm_gen_builtin_class(name, src, mode, out)
            if positional:
                sub = positional[0]
                self._pm_gen(sub, narrowed, mode, out, binds)
            return narrowed
        if self._pm_is_json(src):
            src = _Src(f"lamJSONRaw({src.expr})", _JSON, raw=True)
        go_class = self._go_class_name(name)
        static_class = self._pm_class_of(src.type)
        type_args: Tuple[Type, ...] = ()
        if name in self._interfaces:
            if self._pm_is_interface(src.type) and src.type.name == name:
                self._pm_check(out, mode, f"{src.expr} != nil")
                return _Src(src.expr, NamedType(name))
            obj = self._pm_let(out, mode, f"interface{{}}({src.expr}).({go_class})", True)
            return _Src(obj, NamedType(name))
        if static_class and not self._pm_is_dynamic(src):
            if isinstance(src.type, GenericType) and src.type.base == static_class:
                type_args = tuple(src.type.args)
            if static_class == name:
                obj = src.expr
                result_type: Type = src.type
                if isinstance(src.type, GenericType) and src.type.base in {"optional", "Option"}:
                    result_type = NamedType(name)
            elif name in self._pm_ancestors(static_class):
                obj = f"{src.expr}.{go_class}"
                result_type = NamedType(name)
            elif self._hier_dynamic_target(name) and self._hier_is_subclass(name, static_class):
                # Downcast: the base-typed subject holds a ``name`` (or a
                # subclass of it); bind the narrowed ``*name`` view.
                obj = self._pm_let(out, mode, self._hier_downcast_go(src.expr, static_class, name), True)
                result_type = NamedType(name)
                static_class = ""
            else:
                self._pm_check(out, mode, "false")
                return _Src(src.expr, NamedType(name))
            if static_class:
                self._pm_check(out, mode, f"{src.expr} != nil")
        elif self._hier_dynamic_target(name):
            # Dynamic subject: match the most-derived object, so a base-typed
            # view stored in ``any`` and every subclass (any module) match.
            obj = self._pm_let(out, mode, self._hier_downcast_go(src.expr, "", name), True)
            result_type = NamedType(name)
        else:
            descendants = [
                cls for cls in self._class_and_local_descendants(name)
                if cls != name and not self._generic_classes.get(cls)
            ]
            ptr = f"*{go_class}"
            cases = [f"case {ptr}: return t, t != nil"]
            for cls in descendants:
                cases.append(f"case *{self._go_class_name(cls)}: if t != nil {{ return t.{go_class}, true }}")
            helper = (
                f"func(v interface{{}}) ({ptr}, bool) {{ switch t := v.(type) {{ "
                + "; ".join(cases) + " }; return nil, false }"
                + f"(interface{{}}({src.expr}))"
            )
            obj = self._pm_let(out, mode, helper, True)
            result_type = NamedType(name)
        fields = self._pm_class_fields(name, type_args)
        field_types = dict(fields)
        subs = []
        for i, sub in enumerate(positional):
            if i < len(fields):
                subs.append((fields[i][0], sub))
        subs.extend(keywords)
        for field, sub in subs:
            go_field = self._go_field_name(name, field)
            for cls in self._pm_ancestors(name):
                if (cls, field) in self._field_go_names:
                    go_field = self._field_go_names[(cls, field)]
            field_src = _Src(f"{obj}.{go_field}", field_types.get(field))
            if isinstance(sub, Tree) and sub.data in {"capture_pattern", "any_pattern"}:
                self._pm_gen(sub, field_src, mode, out, binds)
                continue
            if mode == "bind" and not self._pm_has_bindings(sub):
                continue
            var = self._pm_let(out, mode, field_src.expr, False)
            self._pm_gen(sub, _Src(var, field_src.type), mode, out, binds)
        return _Src(obj, result_type)

    def _pm_gen_builtin_class(self, name: str, src: _Src, mode, out) -> _Src:
        """``int(n)`` / ``str(s)`` / ``list(xs)`` / ... type patterns."""
        t = src.type
        if self._pm_is_json(src):
            if name == "json":
                return src
            src = _Src(f"lamJSONRaw({src.expr})", _JSON, raw=True)
        wanted = {"int": NamedType("int"), "float": NamedType("float"), "str": NamedType("str"),
                  "bool": NamedType("bool"), "json": _JSON}
        if not self._pm_is_dynamic(src):
            go = self._pm_go_type(t)
            ok = {
                "int": go in {"int", "int8", "int16", "int32", "int64", "uint"},
                "float": go in {"float64", "float32"},
                "str": go == "string",
                "bool": go == "bool",
                "list": isinstance(t, ListType) or (isinstance(t, GenericType) and t.base == "tuple"),
                "dict": isinstance(t, DictType),
                "json": go == "LamJSON",
            }[name]
            if not ok:
                self._pm_check(out, mode, "false")
            return src
        if name == "int":
            self._pm_need("int")
            return _Src(self._pm_let(out, mode, f"__lamMatchInt({src.expr})", True), wanted[name])
        if name == "float":
            self._pm_need("float")
            return _Src(self._pm_let(out, mode, f"__lamMatchFloat({src.expr})", True), wanted[name])
        if name in {"str", "bool"}:
            self._pm_need("raw")
            go = "string" if name == "str" else "bool"
            return _Src(self._pm_let(out, mode, f"__lamMatchRaw({src.expr}).({go})", True), wanted[name])
        if name == "json":
            if src.raw:
                return src
            return _Src(self._pm_let(out, mode, f"interface{{}}({src.expr}).(LamJSON)", True), _JSON)
        if name == "list":
            self._pm_need("seq")
            var = self._pm_let(out, mode, f"__lamMatchSeq({src.expr})", True)
            return _Src(var, _JSON if src.raw else ListType(_ANY), src.raw)
        self._pm_need("map")
        var = self._pm_let(out, mode, f"__lamMatchMap({src.expr})", True)
        return _Src(var, _JSON if src.raw else DictType(NamedType("str"), _ANY), src.raw)

    # ─── Conditions and statement emission ─────────────────────

    def _pm_condition(self, pat, src: _Src, binds: list, guard, guard_q_var: str = "") -> str:
        """Go boolean expression testing ``pat`` (and ``guard``)."""
        if isinstance(pat, Tree) and pat.data == "any_pattern" and guard is None:
            return "true"
        stmts: List[str] = []
        self._pm_gen(pat, src, "test", stmts, binds)
        guard_go = ""
        if guard is not None:
            saved = self._pm_register_bindings(binds)
            # A ``?`` in the guard emits its prologue (``__qN := f()``,
            # error check) as statements: capture them into the condition
            # closure so they run only when this case is tried. A
            # propagated error records itself in ``guard_q_var`` and
            # selects the case, whose body re-raises it (see
            # ``_visit_match_stmt``).
            outer_lines = self.output_lines
            self.output_lines = []
            try:
                with self._scoped(_guard_q_var=guard_q_var):
                    guard_go = self._expr_to_go(guard)
                prologue = [line.strip() for line in self.output_lines if line.strip()]
            finally:
                self.output_lines = outer_lines
                self._pm_restore_bindings(saved)
            if prologue:
                stmts.extend(prologue)
                self._pm_guard_q_used = True
        # Plain checks without intermediates collapse to an ``&&`` chain.
        simple = all(s.startswith("if !(") and s.endswith(") { return false }") for s in stmts)
        if simple and not binds:
            conds = [s[len("if !("):-len(") { return false }")] for s in stmts]
            if guard_go:
                conds.append(f"({guard_go})")
            return " && ".join(conds) if conds else "true"
        tail = f"return {guard_go}" if guard_go else "return true"
        return "func() bool { " + "; ".join(stmts + [tail]) + " }()"

    def _pm_register_bindings(self, binds: list):
        saved = []
        for name, go_name, type_ in binds:
            saved.append((go_name, self._var_go_types.get(go_name), self._var_types.get(go_name),
                          getattr(self, "_var_lam_types", {}).get(go_name),
                          go_name in self.declared_vars))
            go_type = self._pm_go_type(type_)
            if go_type:
                self._var_go_types[go_name] = go_type
            else:
                self._var_go_types.pop(go_name, None)
            class_name = self._pm_class_of(type_)
            if class_name:
                self._var_types[go_name] = class_name
            else:
                self._var_types.pop(go_name, None)
            if not hasattr(self, "_var_lam_types"):
                self._var_lam_types = {}
            if type_ is not None:
                self._var_lam_types[go_name] = type_
            else:
                self._var_lam_types.pop(go_name, None)
            self._declare_var(go_name)
        return saved

    def _pm_restore_bindings(self, saved) -> None:
        for go_name, go_type, class_name, lam_type, declared in reversed(saved):
            for registry, value in ((self._var_go_types, go_type), (self._var_types, class_name),
                                    (self._var_lam_types, lam_type)):
                if value is None:
                    registry.pop(go_name, None)
                else:
                    registry[go_name] = value
            if not declared:
                self.declared_vars.discard(go_name)

    @staticmethod
    def _pm_case_parts(case: Tree):
        pattern = case.children[0]
        guard = case.children[1] if len(case.children) > 2 else None
        suite = case.children[-1]
        return pattern, guard, suite

    @staticmethod
    def _pm_irrefutable(pat) -> bool:
        if not isinstance(pat, Tree):
            return False
        if pat.data in {"any_pattern", "capture_pattern"}:
            return True
        if pat.data == "as_pattern":
            return PatternMatchMixin._pm_irrefutable(pat.children[0])
        if pat.data == "or_pattern":
            return any(PatternMatchMixin._pm_irrefutable(c) for c in pat.children)
        return False

    @staticmethod
    def _pm_suite_breaks_loop(node) -> bool:
        """``break`` in a case body that targets an enclosing loop."""
        if not isinstance(node, Tree):
            return False
        if node.data == "break_stmt":
            return True
        if node.data in {"for_stmt", "while_stmt", "funcdef", "lambdef", "classdef"}:
            return False
        return any(PatternMatchMixin._pm_suite_breaks_loop(c) for c in node.children)

    def _pm_switch_label(self, pat, subject_go: str) -> Optional[List[str]]:
        """Go ``case`` labels for a switchable pattern, else ``None``."""
        if not isinstance(pat, Tree):
            return None
        if pat.data == "literal_pattern":
            lit, kind = self._pm_literal_go(pat)
            if kind == "none":
                return None
            return [lit]
        if pat.data == "value_pattern":
            return [self._expr_to_go(pat.children[0])]
        if pat.data == "or_pattern":
            labels: List[str] = []
            for alt in pat.children:
                sub = self._pm_switch_label(alt, subject_go)
                if sub is None:
                    return None
                labels.extend(sub)
            return labels
        return None

    def _pm_switch_plan(self, cases, subject_type: Optional[Type]):
        go = self._pm_go_type(subject_type) if subject_type is not None else ""
        if go not in _SCALAR_GO:
            return None
        plan = []
        seen = set()
        for i, case in enumerate(cases):
            pattern, guard, suite = self._pm_case_parts(case)
            if guard is not None or self._pm_suite_breaks_loop(suite):
                return None
            if isinstance(pattern, Tree) and pattern.data == "any_pattern":
                if i != len(cases) - 1:
                    return None
                plan.append((None, suite))
                continue
            labels = self._pm_switch_label(pattern, "")
            if labels is None:
                return None
            for label in labels:
                literal = isinstance(pattern, Tree) and pattern.data != "value_pattern"
                if literal:
                    if go == "string" and not label.startswith('"'):
                        return None
                    if go != "string" and label.startswith(('"', '`')):
                        return None
                    if label in seen:
                        return None
                    seen.add(label)
            plan.append((labels, suite))
        return plan

    def _pm_subject_to_go(self, node, subject_type: Optional[Type]) -> str:
        """Lower the subject; collection literals get their element type."""
        if isinstance(node, Tree) and node.data == "tuple":
            items = [self._expr_to_go(c) for c in node.children if isinstance(c, Tree)]
            return "[]interface{}{" + ", ".join(items) + "}"
        if isinstance(node, Tree) and node.data in {"list", "dict", "set"}:
            go = self._pm_go_type(subject_type)
            if go and go != "interface{}":
                return self._typed_value_to_go(node, go)
        return self._expr_to_go(node)

    def _visit_match_stmt(self, node: Tree):
        cases = [c for c in node.children[1:] if isinstance(c, Tree) and c.data == "case"]
        if not hasattr(self, "_pm_helpers"):
            self._pm_helpers = set()
        irrefutable_index = next(
            (i for i, case in enumerate(cases)
             if self._pm_case_parts(case)[1] is None and self._pm_irrefutable(self._pm_case_parts(case)[0])),
            None,
        )
        if irrefutable_index is not None:
            cases = cases[:irrefutable_index + 1]
            self._hoist_flow_locals([self._pm_case_parts(c)[2] for c in cases])
        subject_node = node.children[0]
        subject_type = self._pm_subject_type(subject_node)
        subject = self._pm_subject_to_go(subject_node, subject_type)

        plan = self._pm_switch_plan(cases, subject_type)
        if plan is not None:
            self._emit(f"switch {subject} {{")
            for labels, suite in plan:
                self._emit("default:" if labels is None else f"case {', '.join(labels)}:")
                self.indent += 1
                self._push_scope()
                body_start, vars_at_start = self._scope_unused_tracking_start()
                self._visit_suite(suite)
                self._pop_scope_with_unused_local_silencers(body_start, vars_at_start)
                self.indent -= 1
            self._emit("}")
            return

        self._match_counter = getattr(self, "_match_counter", 0) + 1
        subj = f"__lamMatch{self._match_counter}"
        outer_helpers = self._pm_helpers
        self._pm_helpers = set()
        src = _Src(subj, subject_type)

        compiled = []
        guard_q_vars: List[str] = []
        for case in cases:
            pattern, guard, suite = self._pm_case_parts(case)
            self._pm_guard_q_used = False
            guard_q_var = f"__lamGuardQ{self._match_counter}_{len(compiled)}"
            cond = self._pm_condition(pattern, src, [], guard, guard_q_var)
            if guard is None and self._pm_irrefutable(pattern):
                cond = "true"
            bind_stmts: List[str] = []
            bind_binds: list = []
            if self._pm_guard_q_used:
                guard_q_vars.append(guard_q_var)
                bind_stmts.append(f"if {guard_q_var} != nil {{ {self._q_propagate_stmt(guard_q_var)} }}")
            self._pm_gen(pattern, src, "bind", bind_stmts, bind_binds)
            compiled.append((cond, suite, bind_stmts, bind_binds))
        needed = self._pm_helpers
        self._pm_helpers = outer_helpers

        self._emit("{")
        self.indent += 1
        self._emit(f"{subj} := {subject}")
        self._emit(f"_ = {subj}")
        for guard_q_var in guard_q_vars:
            self._emit(f"var {guard_q_var} *Result")
        for helper in _HELPER_ORDER:
            if helper in needed:
                self._emit(_HELPERS[helper])
                self._emit(f"_ = __lamMatch{helper.capitalize()}")
        first = True
        for cond, suite, bind_stmts, bind_binds in compiled:
            last_irrefutable = cond == "true"
            if first:
                self._emit("{" if last_irrefutable else f"if {cond} {{")
            else:
                self._emit("} else {" if last_irrefutable else f"}} else if {cond} {{")
            first = False
            self.indent += 1
            self._push_scope()
            body_start, vars_at_start = self._scope_unused_tracking_start()
            for stmt in bind_stmts:
                self._emit(stmt)
            saved = self._pm_register_bindings(bind_binds)
            try:
                self._visit_suite(suite)
                self._pop_scope_with_unused_local_silencers(body_start, vars_at_start)
            finally:
                self._pm_restore_bindings(saved)
            self.indent -= 1
            if last_irrefutable:
                break
        self._emit("}")
        self.indent -= 1
        self._emit("}")
