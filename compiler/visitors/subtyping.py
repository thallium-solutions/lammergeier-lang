#!/usr/bin/env python3
"""Subclass-to-base subtyping: hierarchy queries and the Go support code.

A Lam subclass embeds a pointer to each parent (``type Square struct {
*Shape; ... }``). A base-typed value stays a plain ``*Shape`` — field
access and calls on classes that are never subclassed are exactly as fast
as before — and a subclass instance used where its base is expected is
*upcast* to the embedded parent (``sq._lamUp_Shape()``). Every class that
has a subclass anywhere in the program keeps the most-derived object in
``_lamSelf`` (bound by the subclass constructors), so:

* a method that some descendant overrides is called through the
  ``_lamVirt_<M>`` wrapper of the class that defines it (``x._lamVirt_Area()``
  when the static type of ``x`` has a descendant overriding ``area``),
  whatever the receiver (``self`` or any base-typed value);
* ``_lamDyn()`` returns the most-derived object, and ``_lamIs_<C>()``
  (promoted to every descendant) recovers the ``*C`` view of it, which
  powers ``isinstance``, class-pattern downcasts and typed ``catch``
  across modules;
* ``Format`` prints the user fields only (parent parts as nested
  ``{...}``), delegating to the most-derived object, so the internal
  field never shows up in ``print`` / ``str`` / f-strings.

The hierarchy is program-wide: the driver injects every module's class
bases and method signatures (``_program_class_bases`` /
``_program_method_sigkeys``) so a library knows which of its classes the
program subclasses and which methods get overridden.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Set

from lark import Tree


_SIMPLE_RECEIVER_RE = re.compile(r"[A-Za-z_][\w.]*(\[[^\]]*\])?(\(.*\))?")


class SubtypingMixin:
    """Hierarchy queries and support-code emission (see module docstring)."""

    # ─── Collection ────────────────────────────────────────────

    def _record_method_sigkey(self, class_name: str, method_name: str, funcdef: Tree,
                              class_type_param_names) -> None:
        """Remember the dispatch signature of an instance method.

        ``None`` marks a method that never dispatches virtually: dunders,
        generators, methods with their own type parameters and overloaded
        names (several definitions).
        """
        (_p, _is_static, is_async, _n, params_node, return_type_node,
         suite, method_type_params) = self._parse_funcdef(funcdef)
        bucket = self._class_method_sigkeys.setdefault(class_name, {})
        if method_name in bucket:
            bucket[method_name] = None
            return
        if (
            method_name.startswith("__")
            or method_type_params is not None
            or self._tree_contains(suite, "yield_expr")
        ):
            bucket[method_name] = None
            return
        with self._scoped(_generic_names=self._generic_names | set(class_type_param_names or ())):
            sig = self._param_types_sig(params_node, skip_self=True) if params_node else ()
            ret = self._resolve_return_type(return_type_node)
        bucket[method_name] = f"{'async ' if is_async else ''}({','.join(str(t) for t in sig)}){ret}"

    # ─── Program-wide hierarchy ────────────────────────────────

    def _hier(self) -> dict:
        cache = self.__dict__.setdefault("_hier_cache", {})
        if "bases" in cache:
            return cache
        bases: Dict[str, List[str]] = {
            name: [b for b in values if b] for name, values in self._program_class_bases.items()
        }
        for name, values in self.class_bases.items():
            bases[name] = [b for b in values if b]
        sigs: Dict[str, Dict[str, Optional[str]]] = {
            name: dict(values) for name, values in self._program_method_sigkeys.items()
        }
        for name, values in self._class_method_sigkeys.items():
            sigs[name] = dict(values)
        classes = set(bases) | set(sigs) | set(self._class_names)
        children: Dict[str, List[str]] = {}
        for name, values in bases.items():
            for base in values:
                if base in classes:
                    children.setdefault(base, []).append(name)
        cache.update(bases=bases, sigs=sigs, classes=classes, children=children,
                     ancestors={}, descendants={}, virtual={})
        return cache

    def _hier_bases(self, class_name: str) -> List[str]:
        data = self._hier()
        return [b for b in data["bases"].get(class_name, []) if b in data["classes"]]

    def _hier_ancestors(self, class_name: str) -> List[str]:
        data = self._hier()
        memo = data["ancestors"]
        if class_name not in memo:
            out: List[str] = []
            todo = list(self._hier_bases(class_name))
            while todo:
                base = todo.pop(0)
                if base in out or base == class_name:
                    continue
                out.append(base)
                todo.extend(self._hier_bases(base))
            memo[class_name] = out
        return memo[class_name]

    def _hier_descendants(self, class_name: str) -> List[str]:
        data = self._hier()
        memo = data["descendants"]
        if class_name not in memo:
            out: List[str] = []
            todo = list(data["children"].get(class_name, []))
            while todo:
                child = todo.pop(0)
                if child in out or child == class_name:
                    continue
                out.append(child)
                todo.extend(data["children"].get(child, []))
            memo[class_name] = out
        return memo[class_name]

    def _hier_subclassed(self, class_name: str) -> bool:
        return bool(self._hier()["children"].get(class_name))

    def _hier_member(self, class_name: str) -> bool:
        """A class in an inheritance relation (has a base or a subclass)."""
        return bool(self._hier_bases(class_name)) or self._hier_subclassed(class_name)

    def _hier_is_subclass(self, child: str, base: str) -> bool:
        return base in self._hier_ancestors(child)

    def _hier_defines(self, class_name: str, method: str) -> bool:
        return method in self._hier()["sigs"].get(class_name, {})

    def _hier_def_class(self, class_name: str, method: str) -> Optional[str]:
        for cls in [class_name] + self._hier_ancestors(class_name):
            if self._hier_defines(cls, method):
                return cls
        return None

    def _hier_virtual_methods(self, class_name: str) -> Set[str]:
        """Methods ``class_name`` defines that a descendant overrides with
        the same signature: they get a ``_lamVirt_<M>`` wrapper."""
        data = self._hier()
        memo = data["virtual"]
        if class_name not in memo:
            out: Set[str] = set()
            own = data["sigs"].get(class_name, {})
            descendants = self._hier_descendants(class_name)
            for method, key in own.items():
                if key is None:
                    continue
                overrides = [
                    data["sigs"][cls][method]
                    for cls in descendants
                    if method in data["sigs"].get(cls, {})
                ]
                if overrides and all(o == key for o in overrides):
                    out.add(method)
            memo[class_name] = out
        return memo[class_name]

    def _hier_needs_virtual(self, static_class: str, method: str) -> bool:
        """``x.method()`` with ``x`` statically a ``static_class`` must
        dispatch dynamically: a strict descendant overrides it."""
        if not static_class or not self._hier_subclassed(static_class):
            return False
        def_class = self._hier_def_class(static_class, method)
        if def_class is None or method not in self._hier_virtual_methods(def_class):
            return False
        return any(self._hier_defines(cls, method) for cls in self._hier_descendants(static_class))

    def _hier_inherited_field_names(self, class_name: str) -> Set[str]:
        """Fields declared by any ancestor (local or imported)."""
        out: Set[str] = set()
        for ancestor in self._hier_ancestors(class_name):
            out.update(name for name, _ in self.class_fields.get(ancestor, []))
            out.update(self._external_field_names.get(ancestor, set()))
        for base in self.class_bases.get(class_name, []):
            out.update(name for name, _ in self.class_fields.get(base, []))
        return out

    def _class_type_param_names_of(self, class_name: str) -> List[str]:
        return self._class_type_param_names.get(class_name) or self._program_class_type_params.get(class_name) or []

    def _hier_class_has_str(self, class_name: str) -> bool:
        sigs = self._hier()["sigs"]
        for cls in [class_name] + self._hier_ancestors(class_name):
            methods = sigs.get(cls, {})
            if "__str__" in methods or "__repr__" in methods:
                return True
        return False

    def _hier_user_format_method(self, class_name: str) -> bool:
        """A user method lowering to Go ``Format`` (``func format(self)``)
        on the class or an ancestor: the generated formatter would clash."""
        sigs = self._hier()["sigs"]
        for cls in [class_name] + self._hier_ancestors(class_name):
            for method in sigs.get(cls, {}):
                if self._go_method_name(cls, method) == "Format":
                    return True
        return False

    def _hier_class_from_go_type(self, go_type: str) -> str:
        """``*Square`` / ``*Box[int]`` -> the Lam class name (or ``""``)."""
        go_type = (go_type or "").strip()
        if not go_type.startswith("*"):
            return ""
        base = go_type[1:].split("[", 1)[0]
        data = self._hier()
        by_go = data.get("by_go")
        if by_go is None:
            by_go = {}
            for name in sorted(data["classes"]):
                by_go.setdefault(self._go_class_name(name), name)
            data["by_go"] = by_go
        return by_go.get(base, "")

    def _generic_receiver_subst(self, class_name: str, func) -> Dict[str, str]:
        """``{T: *Shape}`` for a method called on ``reg: Registry[Shape]``."""
        params = self._class_type_param_names_of(class_name) if class_name else []
        if not params or not isinstance(func, Tree) or func.data not in {"getattr", "getattr_safe"}:
            return {}
        go_type = self._expr_go_type_hint(func.children[0]) or ""
        if "[" not in go_type or not go_type.endswith("]"):
            return {}
        inner = go_type[go_type.index("[") + 1:-1]
        args: List[str] = []
        depth, start = 0, 0
        for i, ch in enumerate(inner):
            if ch in "[({":
                depth += 1
            elif ch in "])}":
                depth -= 1
            elif ch == "," and depth == 0:
                args.append(inner[start:i].strip())
                start = i + 1
        args.append(inner[start:].strip())
        if len(args) != len(params) or any(a == p for a, p in zip(args, params)):
            return {}
        return dict(zip(params, args))

    # ─── Upcasts ───────────────────────────────────────────────

    def _hier_expr_go_type(self, node) -> str:
        """Go type of ``node`` for upcasts: the usual hint, plus class
        constructor calls (``Square(3)``) and methods returning a class."""
        hint = self._expr_go_type_hint(node) or ""
        if hint or not isinstance(node, Tree) or node.data != "funccall" or not node.children:
            return hint
        callee = node.children[0]
        if isinstance(callee, Tree) and callee.data == "var" and callee.children:
            name = self._get_name(callee.children[0])
            if name in self._class_names and not self._generic_classes.get(name) and name not in self._interfaces:
                return f"*{self._go_class_name(name)}"
            return ""
        if isinstance(callee, Tree) and callee.data in {"getattr", "getattr_safe"} and len(callee.children) >= 2:
            receiver_class = self._infer_receiver_class(callee.children[0])
            method = self._get_name(callee.children[1])
            if receiver_class:
                raw_return = self._method_return_types.get(f"{receiver_class}.{method}", "")
                if raw_return in self._class_names and raw_return not in self._interfaces:
                    return f"*{self._go_class_name(raw_return)}"
        return ""

    @staticmethod
    def _hier_receiver(raw: str) -> str:
        return raw if _SIMPLE_RECEIVER_RE.fullmatch(raw) and " " not in raw else f"({raw})"

    def _hier_upcast_to_go(self, raw: str, actual_go: str, expected_go: str) -> str:
        """``raw`` (of Go type ``actual_go``) converted to ``expected_go``
        when the first is a subclass of the second, else ``""``."""
        if not expected_go.startswith("*") or not actual_go.startswith("*"):
            return ""
        if actual_go == expected_go:
            return ""
        expected_cls = self._hier_class_from_go_type(expected_go)
        actual_cls = self._hier_class_from_go_type(actual_go)
        if not expected_cls or not actual_cls or expected_cls == actual_cls:
            return ""
        if not self._hier_is_subclass(actual_cls, expected_cls):
            return ""
        return f"{self._hier_receiver(raw)}._lamUp_{self._go_class_name(expected_cls)}()"

    def _hier_dynamic_to_go(self, raw: str, actual_go: str, expected_go: str) -> str:
        """A value of a subclassed class converted to an interface type or
        ``interface{}``: the most-derived object (``x._lamDyn()``)."""
        actual_cls = self._hier_class_from_go_type(actual_go)
        if not actual_cls or not self._hier_subclassed(actual_cls) or self._generic_classes.get(actual_cls):
            return ""
        receiver = self._hier_receiver(raw)
        if expected_go == "interface{}":
            return f"{receiver}._lamDyn()"
        iface = next(
            (name for name in self._interfaces if self._go_class_name(name) == expected_go),
            None,
        )
        if iface is None:
            return ""
        return (
            f"func(v interface{{}}) {expected_go} {{ if v == nil {{ return nil }}; "
            f"return v.({expected_go}) }}({receiver}._lamDyn())"
        )

    def _hier_upcast_paths(self, class_name: str) -> List[tuple]:
        """``[(ancestor, go_path_from_s, go_type)]`` for each ancestor the
        class can be viewed as (the first path wins in diamond shapes).
        A generic grand-ancestor whose type arguments depend on an
        intermediate class's parameters is skipped."""
        out: List[tuple] = []
        seen: Set[str] = set()
        todo = [
            (base, f"s.{self._go_class_name(base)}", self._base_type_args(class_name, base))
            for base in self._hier_bases(class_name)
        ]
        while todo:
            base, path, type_args = todo.pop(0)
            if base in seen:
                continue
            seen.add(base)
            out.append((base, path, f"*{self._go_class_name(base)}{type_args}"))
            for grand in self._hier_bases(base):
                grand_args = self._base_type_args(base, grand)
                if grand_args and type_args:
                    continue
                todo.append((grand, f"{path}.{self._go_class_name(grand)}", grand_args))
        return out

    # ─── Support code ──────────────────────────────────────────

    def _emit_hierarchy_support(self, class_name: str) -> None:
        """``_lamDyn`` / ``_lamIs_<C>`` / ``_lamUp_<A>`` / ``Format``."""
        if not self._hier_member(class_name):
            return
        go_cls = self._go_class_name(class_name)
        args = self._class_type_args(class_name)
        recv = f"*{go_cls}{args}"
        subclassed = self._hier_subclassed(class_name)
        self._emit(f"func (s {recv}) _lamDyn() interface{{}} {{")
        self._emit("\tif s == nil { return nil }")
        if subclassed:
            self._emit("\tif s._lamSelf != nil { return s._lamSelf }")
        self._emit("\treturn s")
        self._emit("}")
        self._emit("")
        self._emit(f"func (s {recv}) _lamIs_{go_cls}() {recv} {{ return s }}")
        self._emit("")
        for ancestor, path, go_type in self._hier_upcast_paths(class_name):
            self._emit(
                f"func (s {recv}) _lamUp_{self._go_class_name(ancestor)}() {go_type} "
                f"{{ if s == nil {{ return nil }}; return {path} }}"
            )
            self._emit("")
        self._emit_hierarchy_format(class_name, recv, subclassed)

    def _emit_hierarchy_format(self, class_name: str, recv: str, subclassed: bool) -> None:
        if self._hier_user_format_method(class_name):
            return
        self._need_import("fmt")
        has_str = self._hier_class_has_str(class_name)
        self._emit(f"func (s {recv}) _lamFmtValue() interface{{}} {{")
        self.indent += 1
        self._emit("if s == nil { return nil }")
        if has_str:
            self._emit("return s.String()")
        else:
            fields: List[str] = []
            values: List[str] = []
            for base in self.class_bases.get(class_name, []):
                go_base = self._go_class_name(base)
                fields.append(f"{go_base} interface{{}}")
                if base in self._hier()["classes"] and not self._hier_user_format_method(base):
                    values.append(f"s.{go_base}._lamFmtValue()")
                else:
                    values.append(f"s.{go_base}")
            parent_fields = self._hier_inherited_field_names(class_name)
            for field_name, go_type in self.class_fields.get(class_name, []):
                if field_name in parent_fields:
                    continue
                go_field = self._go_field_name(class_name, field_name)
                fields.append(f"{go_field} {go_type}")
                values.append(f"s.{go_field}")
            self._emit(f"return struct{{ {'; '.join(fields)} }}{{{', '.join(values)}}}")
        self.indent -= 1
        self._emit("}")
        self._emit("")
        self._emit(f"func (s {recv}) Format(f fmt.State, verb rune) {{")
        self.indent += 1
        self._emit('if s == nil { fmt.Fprint(f, "<nil>"); return }')
        if subclassed:
            self._emit("if s._lamSelf != nil { fmt.Fprintf(f, fmt.FormatString(f, verb), s._lamSelf); return }")
        if has_str:
            self._emit("fmt.Fprintf(f, fmt.FormatString(f, verb), s.String())")
        else:
            self._emit('fmt.Fprint(f, "&")')
            self._emit("fmt.Fprintf(f, fmt.FormatString(f, verb), s._lamFmtValue())")
        self.indent -= 1
        self._emit("}")
        self._emit("")

    # ─── Dynamic class tests ───────────────────────────────────

    def _hier_dynamic_target(self, class_name: str) -> bool:
        """Class tests on ``class_name`` go through ``_lamIs_<C>``."""
        return (
            class_name in self._hier()["classes"]
            and self._hier_member(class_name)
            and not self._generic_classes.get(class_name)
        )

    def _hier_is_iface(self, class_name: str) -> str:
        go_cls = self._go_class_name(class_name)
        return f"interface{{ _lamIs_{go_cls}() *{go_cls} }}"

    def _hier_dyn_value_go(self, obj_go: str) -> str:
        """Most-derived object behind ``obj_go`` (any Go value)."""
        return (
            f"func(v interface{{}}) interface{{}} {{ if d, ok := v.(interface{{ _lamDyn() interface{{}} }}); ok "
            f"{{ return d._lamDyn() }}; return v }}(interface{{}}({obj_go}))"
        )

    def _hier_isinstance_go(self, obj_go: str, static_class: str, target: str) -> str:
        """``isinstance(obj, target)`` for a hierarchy class ``target``
        (``""`` when the generic lowering applies)."""
        if not self._hier_dynamic_target(target):
            if static_class and (target == static_class or self._hier_is_subclass(static_class, target)):
                return f"({obj_go} != nil)"
            return ""
        if static_class and (target == static_class or self._hier_is_subclass(static_class, target)):
            return f"({obj_go} != nil)"
        if static_class and self._hier_member(static_class) and not self._generic_classes.get(static_class):
            dyn = f"{self._hier_receiver(obj_go)}._lamDyn()"
        else:
            dyn = self._hier_dyn_value_go(obj_go)
        return f"func() bool {{ _, ok := {dyn}.({self._hier_is_iface(target)}); return ok }}()"

    def _hier_downcast_go(self, obj_go: str, static_class: str, target: str) -> str:
        """``(*Target, bool)`` expression narrowing ``obj_go`` to ``target``."""
        go_cls = self._go_class_name(target)
        if static_class and self._hier_member(static_class) and not self._generic_classes.get(static_class):
            dyn = f"{self._hier_receiver(obj_go)}._lamDyn()"
        else:
            dyn = self._hier_dyn_value_go(obj_go)
        return (
            f"func(v interface{{}}) (*{go_cls}, bool) {{ if t, ok := v.({self._hier_is_iface(target)}); ok "
            f"{{ return t._lamIs_{go_cls}(), true }}; return nil, false }}({dyn})"
        )

    # ─── Inherited constructors ────────────────────────────────

    def _inherited_init_source(self, class_name: str, seen: Optional[Set[str]] = None) -> Optional[str]:
        """Base whose constructor ``class_name`` inherits (it has no own
        ``init``): the first base, depth-first, whose chain declares one."""
        seen = seen if seen is not None else set()
        if class_name in seen:
            return None
        seen.add(class_name)
        for base in self.class_bases.get(class_name, []) or self._hier_bases(class_name):
            if self._class_declares_init(base) or self._inherited_init_source(base, seen):
                return base
        return None

    def _class_declares_init(self, class_name: str) -> bool:
        return (
            class_name in self._class_own_init
            or class_name in self._program_own_init
        )

    def _resolve_local_inherited_inits(self) -> None:
        """Give every local class without ``init`` the constructor metadata
        (parameter count, names, Go types, defaults) of the ancestor whose
        ``init`` it inherits, so call sites fill defaults and named args."""
        own = set(self._class_own_init) | set(self._program_own_init)
        targets = [name for name, bases in self.class_bases.items() if bases and name not in own]
        if not targets:
            return
        resolve_inherited_init_metadata(
            self._hier()["bases"], own, self._hier()["classes"], targets,
            self._func_param_counts, self._func_defaults, self._func_param_names,
            self._func_param_go_types, self._variadic_functions,
            overwrite=True, type_subst=self._subst_inherited_init_types,
        )

    def _subst_inherited_init_types(self, class_name: str, base: str, go_types):
        """Rewrite a generic base's ``init`` parameter types with the type
        arguments ``class_name`` instantiates it with (``T`` -> ``int``)."""
        if not go_types:
            return go_types
        params = self._class_type_param_names.get(base) or self._program_class_type_params.get(base) or []
        arg_nodes = self._class_base_type_nodes.get(class_name, {}).get(base) or []
        if not params or not arg_nodes:
            return go_types
        with self._scoped(_generic_names=self._generic_names | set(self._class_type_param_names.get(class_name, []))):
            args = [self._type_expr_to_go(node) or "any" for node in arg_nodes]
        mapping = dict(zip(params, args))
        pattern = re.compile(r"\b(" + "|".join(re.escape(p) for p in mapping) + r")\b")
        return [pattern.sub(lambda m: mapping[m.group(1)], t) for t in go_types]

    def _init_assigns_field(self, class_name: str, field: str, seen: Optional[Set[str]] = None) -> bool:
        """Whether constructing ``class_name`` (its ``init`` or the one it
        inherits, ancestors included) assigns ``self.<field>``."""
        seen = seen if seen is not None else set()
        if class_name in seen:
            return False
        seen.add(class_name)
        funcdef = self._local_class_init_defs.get(class_name)
        if funcdef is not None and self._tree_assigns_self_field(funcdef, field):
            return True
        return any(self._init_assigns_field(base, field, seen) for base in self._hier_bases(class_name))

    def _tree_assigns_self_field(self, node, field: str) -> bool:
        if not isinstance(node, Tree):
            return False
        if node.data in {"assign", "annassign", "augassign"} and node.children:
            target = next((c for c in node.children if isinstance(c, Tree)), None)
            if (
                isinstance(target, Tree) and target.data == "getattr" and len(target.children) >= 2
                and isinstance(target.children[0], Tree) and target.children[0].data == "var"
                and self._get_name(target.children[0].children[0]) == "self"
                and self._get_name(target.children[1]) == field
            ):
                return True
        return any(self._tree_assigns_self_field(child, field) for child in node.children)

    def _hier_parent_field_default_stmts(self, class_name: str, parent: str, recv: str) -> List[str]:
        """Re-apply ``class_name``'s class-body defaults for fields a parent
        declares after the parent was rebuilt by ``NewParent(...)``
        (``base.init(...)`` or an inherited ``init``), unless the parent's
        ``init`` assigns the field itself (``sides: int = 4`` in ``Square``
        overrides ``Shape``'s ``sides: int = 0``)."""
        defaults = self.class_field_defaults.get(class_name, [])
        if not defaults:
            return []
        parent_fields = self._hier_inherited_field_names(class_name)
        out: List[str] = []
        for field_name, go_type, value_node in defaults:
            if field_name not in parent_fields or self._init_assigns_field(parent, field_name):
                continue
            with self._scoped(_self_replacement=recv, _propagate_cast_hint=go_type):
                value_str = self._typed_value_to_go(value_node, go_type)
            out.append(f"{recv}.{self._go_field_name(class_name, field_name)} = {value_str}")
        return out

    def _emit_inherited_constructor(self, class_name: str, tp_clause: str) -> bool:
        """``NewC(<params of the inherited init>)`` for a class without its
        own ``init``: allocate, apply field defaults, build the parent the
        ``init`` comes from with ``NewParent(...)`` and bind the object
        into it. ``False`` when nothing is inherited (or the parameter list
        has a shape this path does not forward: ``*args``, tuples)."""
        source = self._inherited_init_source(class_name)
        if source is None:
            return False
        key = f"{class_name}.init"
        names = self._func_param_names.get(key)
        types = self._func_param_go_types.get(key)
        if (
            names is None or types is None or len(names) != len(types)
            or any(not n for n in names) or key in self._variadic_functions
        ):
            return False
        go_names = [self._go_binding_name(n) for n in names]
        recv = "_lamS" if "s" in go_names else "s"
        go_cls = self._go_class_name(class_name)
        class_args = self._class_type_args(class_name)
        params = ", ".join(f"{n} {t}" for n, t in zip(go_names, types))
        self._emit(f"func New{go_cls}{tp_clause}({params}) *{go_cls}{class_args} {{")
        self.indent += 1
        self._emit(f"{recv} := &{go_cls}{class_args}{{}}")
        self._emit_ancestor_init(class_name, receiver=recv)
        self._emit_field_defaults(class_name, receiver=recv)
        self._emit_virtual_binds(class_name, receiver=recv)
        go_source = self._go_class_name(source)
        path = f"{recv}.{go_source}"
        self._emit(f"{path} = New{go_source}{self._base_type_args(class_name, source)}({', '.join(go_names)})")
        for stmt in self._virtual_bind_stmts(source, path, recv):
            self._emit(stmt)
        for stmt in self._hier_parent_field_default_stmts(class_name, source, recv):
            self._emit(stmt)
        self._emit(f"return {recv}")
        self.indent -= 1
        self._emit("}")
        self._emit("")
        return True


def resolve_inherited_init_metadata(
    bases, own_init, classes, targets,
    counts, defaults, names, go_types, variadic,
    *, overwrite: bool = False, type_subst=None,
) -> None:
    """Copy constructor metadata (``"<Class>.init"`` keys) from the nearest
    ancestor declaring ``init`` to each class in ``targets`` lacking one
    (bases searched depth-first, left to right)."""
    memo: Dict[str, Optional[tuple]] = {}

    def resolve(cls: str, seen: Set[str]) -> Optional[tuple]:
        if cls in own_init:
            key = f"{cls}.init"
            if key not in counts:
                return None
            return (counts.get(key, 0), list(defaults.get(key, [])), names.get(key),
                    go_types.get(key), key in variadic)
        if cls in memo:
            return memo[cls]
        if cls in seen:
            return None
        seen.add(cls)
        found = None
        for base in bases.get(cls, []):
            if base not in classes:
                continue
            inherited = resolve(base, seen)
            if inherited is not None:
                if type_subst is not None:
                    inherited = (inherited[0], inherited[1], inherited[2],
                                 type_subst(cls, base, inherited[3]), inherited[4])
                found = inherited
                break
        memo[cls] = found
        return found

    for cls in targets:
        found = resolve(cls, set())
        if found is None:
            continue
        key = f"{cls}.init"
        if key in counts and not overwrite:
            continue
        count, defs, param_names, param_types, is_variadic = found
        counts[key] = count
        if defs:
            defaults[key] = defs
        else:
            defaults.pop(key, None)
        if param_names is not None:
            names[key] = list(param_names)
        if param_types is not None:
            go_types[key] = list(param_types)
        if is_variadic:
            variadic.add(key)
