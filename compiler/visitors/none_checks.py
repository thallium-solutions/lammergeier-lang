#!/usr/bin/env python3
"""``None`` receivers: Go lowering of ``AttributeError``.

A class reference or an interface value can be ``None`` (a Go ``nil``
pointer / interface). A member of ``None`` raises ``AttributeError: None has
no attribute 'name'`` (``compiler/semantic_none.py``). Go already faults on a
field read through a ``nil`` pointer, but without the member's name, and a
method call on a ``nil`` receiver runs until the method touches a field (a
method that never uses ``self`` silently returns). So the receiver of a field
access or a method call is wrapped in ``lamNN(x, "name")`` (a class pointer)
or ``lamNNI(x, "name")`` (an interface value), small generic helpers Go
inlines (``_NONE_RUNTIME_GO`` in ``compiler/transpiler.py``), except where it
cannot be ``None``:

* ``self`` (a method's receiver) and ``base``;
* a constructor call (``User("a").name``);
* a local of the function that is only ever assigned constructor calls (or
  ``self``): every read follows an assignment (the checker's definite
  assignment), so it holds an object (``c: Counter = Counter()`` then
  ``c.add(1)`` in a loop);
* a local narrowed by a ``None`` test (``if u != None { u.name }``, the
  statements after ``if u == None { return }``, the right operand of ``u !=
  None and u.ok``), see ``visitors/narrowing.py``.
"""

from __future__ import annotations

from lark import Token, Tree

# Assignment-like nodes whose targets are re-bound by something that is not
# a plain ``name = value`` / ``name: T = value``.
_REBINDING_NODES = frozenset({
    "for_stmt", "with_item", "catch_clause", "do_stmt", "augassign",
    "comp_for", "global_stmt", "nonlocal_stmt", "del_stmt",
})


class NoneCheckMixin:
    """Lowering side of ``None`` receivers (see the module docstring)."""

    # ─── Receivers that cannot be ``None`` ─────────────────────

    def _nn_is_constructor_call(self, node) -> bool:
        """``User(...)`` / ``Box[int](...)``: a fresh object, never ``None``."""
        if not (isinstance(node, Tree) and node.data == "funccall" and node.children):
            return False
        callee = node.children[0]
        if isinstance(callee, Tree) and callee.data == "getitem" and callee.children:
            callee = callee.children[0]
        if not (isinstance(callee, Tree) and callee.data == "var" and callee.children):
            return False
        name = self._get_name(callee.children[0])
        return (
            name in self._class_names
            and name not in self._interfaces
            and self._go_binding_name(name) not in self.declared_vars
        )

    def _nn_fresh_locals(self, funcdef: Tree) -> frozenset:
        """Locals of ``funcdef`` only ever assigned constructor calls (cached
        on the node)."""
        cached = getattr(funcdef, "_lam_fresh_locals", None)
        if cached is not None:
            return cached
        assigned: dict = {}
        rebound: set = set()
        params = next((c for c in funcdef.children if isinstance(c, Tree) and c.data in {"parameters", "typedargslist", "params"}), None)
        if params is not None:
            for sub in params.iter_subtrees():
                for child in sub.children:
                    if isinstance(child, Token) and child.type == "NAME":
                        rebound.add(str(child))
        seen_order: list = []
        for sub in funcdef.iter_subtrees_topdown():
            if sub.data in {"annassign", "assign"} and sub.children:
                parts = [c for c in sub.children if isinstance(c, Tree) and c.data != "type_expr"]
                target = parts[0] if parts else None
                value = parts[-1] if len(parts) == 2 else None
                if target is None:
                    continue
                if isinstance(target, Tree) and target.data in {"getattr", "getitem"}:
                    continue  # ``node.value = v`` / ``xs[i] = v`` rebinds no name
                if not (isinstance(target, Tree) and target.data == "var" and target.children):
                    for name_node in target.iter_subtrees() if isinstance(target, Tree) else ():
                        if name_node.data == "var" and name_node.children:
                            rebound.add(self._get_name(name_node.children[0]))
                    continue
                name = self._get_name(target.children[0])
                fresh = self._nn_is_constructor_call(value) or (
                    isinstance(value, Tree) and value.data == "var" and value.children
                    and self._get_name(value.children[0]) == "self"
                )
                if value is None or not fresh:
                    rebound.add(name)
                    continue
                if name not in assigned:
                    seen_order.append(name)
                assigned[name] = True
            elif sub.data in _REBINDING_NODES:
                rebound |= self._nn_rebound_names(sub)
        # (the checker's definite-assignment rule: every read follows an
        # assignment, so a local only ever assigned objects is never ``None``)
        result = frozenset(n for n in seen_order if n not in rebound)
        try:
            funcdef._lam_fresh_locals = result
        except AttributeError:  # pragma: no cover - lark trees accept attributes
            pass
        return result

    def _nn_rebound_names(self, node: Tree) -> set:
        """Names a loop / ``with`` / ``catch`` / ``+=`` / ``global`` node
        binds (its target, not its body)."""
        names: set = set()

        def collect(tree) -> None:
            if not isinstance(tree, Tree):
                return
            for sub in tree.iter_subtrees():
                if sub.data in {"var", "name"} and sub.children:
                    child = sub.children[0]
                    names.add(self._get_name(child) if isinstance(child, Tree) else str(child))

        if node.data in {"for_stmt", "comp_for", "augassign"}:
            target = next((c for c in node.children if isinstance(c, Tree)), None)
            if not (isinstance(target, Tree) and target.data in {"getattr", "getitem"}):
                collect(target)
        elif node.data in {"global_stmt", "nonlocal_stmt", "del_stmt"}:
            collect(node)
        else:
            for child in node.children:
                if isinstance(child, Tree) and child.data in {"var", "name"}:
                    collect(child)
                elif isinstance(child, Token) and child.type == "NAME":
                    names.add(str(child))
        return names

    def _nn_receiver_known_set(self, node) -> bool:
        """``node`` (a receiver) is never ``None`` here."""
        if not isinstance(node, Tree):
            return False
        if node.data == "var" and node.children:
            name = self._get_name(node.children[0])
            if name in {"self", "base"}:
                return True
            go_name = self._go_binding_name(name)
            if go_name in self._nonnil_names:
                return True
            return name in getattr(self, "_fresh_locals", ())
        if node.data == "funccall":
            return self._nn_is_constructor_call(node)
        return False

    # ─── The check ─────────────────────────────────────────────

    def _nn_receiver_go(self, obj_go: str, obj_node, member: str) -> str:
        """``obj_go`` (the lowering of the receiver ``obj_node`` of
        ``.member``) wrapped in the ``None`` check of its kind, or as is when
        it cannot be ``None`` (or is not a reference)."""
        if self._nn_receiver_known_set(obj_node):
            return obj_go
        go_type = (self._expr_go_type_hint(obj_node) or "").strip()
        if not go_type:
            return obj_go
        quoted = '"' + member.replace("\\", "\\\\").replace('"', '\\"') + '"'
        if go_type.startswith("*") and go_type != "*Result" and self._nn_is_class_pointer(go_type):
            return f"lamNN({obj_go}, {quoted})"
        if go_type in self._interface_go_names():
            return f"lamNNI({obj_go}, {quoted})"
        return obj_go

    def _nn_is_class_pointer(self, go_type: str) -> bool:
        base = go_type[1:].split("[", 1)[0]
        if base in self._interface_go_names():
            return False
        cache = self.__dict__.get("_nn_class_go_names")
        if cache is None or cache[0] != len(self._class_names):
            cache = (len(self._class_names), frozenset(self._go_class_name(c) for c in self._class_names))
            self._nn_class_go_names = cache
        return base in cache[1]
