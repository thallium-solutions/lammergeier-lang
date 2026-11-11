"""Semantic checks of ``any`` values and of the inline cast ``expr as T``.

An ``any`` value (Go ``interface{}``) converts at run time wherever a
concrete type is expected (an annotated variable, a typed parameter, ...),
but an operation that needs a concrete type — a member, an arithmetic or
ordering operator, indexing, slicing, iteration, ``len()``, a call, a
condition — cannot be lowered on it: Go rejected the generated code with
``interface{}`` errors. This mixin reports those operations as Lam errors
(kind ``any``) that name the expression and the fixes: the inline cast
``expr as T``, an annotated variable, a conversion between kinds and an
``isinstance`` / ``match`` narrowing.

Operations that are well defined on ``any`` stay allowed: ``==`` / ``!=``,
``is None``, ``??``, ``str()`` / ``print`` / f-strings, ``isinstance``,
``match``, passing it where an ``any`` or a typed value is expected,
``int()`` / ``float()`` / ``bool()`` (checked at run time) and the
membership test of an ``any`` needle.

``expr as T`` has the static type ``T``. From ``any`` it is the run-time
conversion of an annotated assignment; from a concrete type it is the
identity, a widening (``int`` as ``float``), an upcast, a downcast checked
at run time (base class or interface to a class, as an ``isinstance``
narrowing does) or, when no value of the source type can be a ``T``, a Lam
error pointing to the conversion functions.
"""

from __future__ import annotations

from lark import Token, Tree

from compiler.constants import LIST_METHODS, STR_METHODS, any_operand_message, lam_simple_expr_text
from compiler.typesys import (
    DictType, GenericType, ListType, NamedType, Type, UnionType,
    is_assignable, parse_type, render_type,
)

# Expressions whose ``any`` type comes from a declaration (an ``any``
# variable, parameter or field, an element of a ``list[any]`` /
# ``dict[K, any]``, a ``-> any`` result, ``?`` on a ``Result[any]``, a cast
# to ``any``): an operation on them is reported. Other nodes typed ``any``
# by inference (a mixed literal, a ternary) are left alone.
_ANY_OPERAND_NODES = frozenset({
    "var", "getitem", "getattr", "funccall", "propagate", "cast_expr",
})

# Builtins whose argument must have a concrete type.
_CONCRETE_ARG_BUILTINS = {
    "len": "list[any]", "reversed": "list[any]", "sorted": "list[any]",
    "enumerate": "list[any]", "zip": "list[any]", "list": "list[any]",
    "set": "list[any]", "dict": "dict[str, any]", "tuple": "list[any]",
    "range": "int", "abs": "float", "min": "float", "max": "float",
}

_SCALARS = frozenset({"int", "float", "str", "bool", "bytes"})
_BINARY_OP_NODES = {"or_expr": "|", "and_expr": "&", "xor_expr": "^"}


class AnyCastSemanticMixin:
    # ── Inline casts ───────────────────────────────────────────────────

    @staticmethod
    def _cast_target_type(node: Tree) -> Type:
        return parse_type(node.children[1])

    def _check_cast_expr(self, node: Tree) -> None:
        """``expr as T``: statically impossible casts are errors."""
        value = node.children[0]
        target = self._cast_target_type(node)
        if self._is_any_type(target):
            return
        source = self._expr_type_for_expected(value, target) if isinstance(value, Tree) else None
        if source is None:
            source = self._expr_simple_type(value)
        if source is None or self._is_any_type(source) or self._cast_involves_open_type(source, target):
            return
        if self._cast_statically_allowed(source, target):
            return
        self._error(node, "type", self._cast_error_message(source, target))

    def _cast_involves_open_type(self, source: Type, target: Type) -> bool:
        """A type parameter or a type the checker does not know on either
        side: the cast is checked at run time."""
        params = self._current_type_params()
        for type_ in (source, target):
            if isinstance(type_, NamedType) and (
                type_.name in params or self._is_unknown_open_assignment_type(type_)
            ):
                return True
            if self._type_mentions_generic(type_, tuple(params)) if params else False:
                return True
        return False

    def _cast_statically_allowed(self, source: Type, target: Type) -> bool:
        if is_assignable(target, source) or self._nominal_type_assignable(target, source):
            return True
        if self._type_mentions_any(source) and isinstance(target, (ListType, DictType)):
            # ``xs as list[str]`` with ``xs: list[any]``: converted item by
            # item at run time, like an annotated assignment.
            return True
        if self._is_named_type(source, "json"):
            # ``doc.name as str``: the run-time extraction of an annotated
            # ``json`` assignment.
            return isinstance(target, NamedType) and target.name in _SCALARS - {"bytes"} | {"json"}
        source_inner = self._cast_none_union_inner(source)
        target_inner = self._cast_none_union_inner(target) or target
        if source_inner is not None and self._cast_downcast_allowed(source_inner, target_inner):
            return True
        return self._cast_downcast_allowed(source, target_inner)

    @staticmethod
    def _cast_none_union_inner(type_: Type) -> Type | None:
        """``T`` of a union ``T | None``."""
        if isinstance(type_, UnionType):
            others = [t for t in type_.options if not (isinstance(t, NamedType) and t.name == "None")]
            if len(others) == 1 and len(others) != len(type_.options):
                return others[0]
        return None

    def _cast_downcast_allowed(self, source: Type, target: Type) -> bool:
        """A base class, or an interface, as one of its subclasses /
        implementations (or another interface): checked at run time."""
        if not (isinstance(source, NamedType) and isinstance(target, NamedType)):
            return False
        source_class = source.name in self._class_method_shapes
        source_iface = source.name in self._interface_methods
        target_class = target.name in self._class_method_shapes
        target_iface = target.name in self._interface_methods
        if target_class and self._class_type_params.get(target.name):
            return False
        if source_class and target_class:
            return self._subclass_hook(source, target)
        if source_iface and (target_class or target_iface):
            return True
        return False

    def _cast_error_message(self, source: Type, target: Type) -> str:
        head = f"cannot cast `{render_type(source)}` to `{render_type(target)}` with `as`"
        inner = self._cast_none_union_inner(source)
        if inner is not None and (is_assignable(target, inner) or self._nominal_type_assignable(target, inner)):
            return f"{head}: the value may be `None`; test `x != None` first (it narrows `x`) or use `x ?? fallback`"
        if isinstance(target, NamedType) and target.name in _SCALARS:
            name = target.name
            if name == "str":
                return f"{head}; use `str(...)` to format a value"
            if self._is_named_type(source, "str") and name in {"int", "float", "bool"}:
                return (
                    f"{head}: `as` checks the type, it does not convert between kinds; "
                    f"use `{name}(...)` to parse or convert"
                )
            return f"{head}; use `{name}(...)` to convert"
        if (
            isinstance(source, NamedType) and isinstance(target, NamedType)
            and source.name in self._class_method_shapes and target.name in self._class_method_shapes
        ):
            return f"{head}: the classes are unrelated"
        return head

    # ── Operations on `any` ────────────────────────────────────────────

    def _is_any_operand(self, node) -> bool:
        if not isinstance(node, Tree) or node.data not in _ANY_OPERAND_NODES:
            return False
        if self._is_lambda_any_context_param(node):
            return False
        type_ = self._expr_simple_type(node)
        return type_ is not None and self._is_any_type(type_)

    def _is_lambda_any_context_param(self, node: Tree) -> bool:
        """A lambda parameter typed ``any`` by its callback type is reported
        by ``_check_untyped_lambda_params`` (with the callback's name)."""
        if node.data != "var" or not node.children:
            return False
        name = self._name_text(node.children[0])
        for scope in reversed(self._scopes):
            if name in scope.var_types or name in scope.names:
                return scope.kind == "function" and scope.__dict__.get("lambda_any_params", set()) >= {name}
        return False

    def _report_any_operand(self, operand, action: str, target: str) -> None:
        self._error(operand, "any", any_operand_message(lam_simple_expr_text(operand), action, target))

    def _concrete_hint(self, other) -> str:
        """The type a cast should give an ``any`` operand next to ``other``
        (``int`` when ``other`` gives no clue)."""
        type_ = self._expr_simple_type(other) if isinstance(other, Tree) else None
        if isinstance(type_, NamedType) and (type_.name in _SCALARS or type_.name in self._class_method_shapes):
            return type_.name
        if isinstance(type_, (ListType, DictType)):
            return render_type(type_)
        return "int"

    def _member_hint(self, attr: str) -> str:
        """The cast suggested for ``v.attr``: ``str`` / ``list[any]`` for
        their methods, the class declaring ``attr`` when only one does."""
        if attr in STR_METHODS:
            return "str"
        if attr in LIST_METHODS:
            return "list[any]"
        owners = sorted(
            name for name, members in self._class_members.items()
            if attr in members and name not in {"Result", "Error"}
        )
        return owners[0] if len(owners) == 1 else "T"

    def _check_any_member(self, node: Tree, obj, attr: str) -> bool:
        if not self._is_any_operand(obj) or self._result_error_member_class(obj):
            # (``r.error.message``: the members of lamerrors' ``Error``.)
            return False
        is_call = id(node) in self.__dict__.get("_callee_getattr_ids", set())
        action = f"method `{attr}()`" if is_call else f"member `{attr}`"
        self._report_any_operand(obj, action, self._member_hint(attr))
        return True

    def _check_any_binary(self, node: Tree) -> bool:
        """``row["n"] + 1``, ``v << 2``, ``a | v``: an ``any`` operand of an
        arithmetic or bitwise operator (unless the other side is a class
        whose operator method takes it)."""
        operands = [c for c in node.children if isinstance(c, Tree)]
        if len(operands) < 2:
            return False
        fixed = _BINARY_OP_NODES.get(node.data)
        ops = [fixed] * (len(operands) - 1) if fixed else [str(c) for c in node.children if isinstance(c, Token)]
        for index, operand in enumerate(operands):
            if not self._is_any_operand(operand):
                continue
            op = ops[index - 1] if index > 0 else ops[0]
            other = operands[index + 1] if index == 0 else operands[index - 1]
            if index > 0 and self._receiver_class_from_type(self._expr_simple_type(other)):
                # ``money + v``: the class's ``__add__`` decides.
                continue
            self._report_any_operand(operand, f"operator `{op}`", self._concrete_hint(other))
            return True
        return False

    def _check_any_power(self, node: Tree) -> bool:
        operands = [c for c in node.children if isinstance(c, Tree)]
        if len(operands) != 2:
            return False
        for index, operand in enumerate(operands):
            if self._is_any_operand(operand):
                self._report_any_operand(operand, "operator `**`", self._concrete_hint(operands[1 - index]))
                return True
        return False

    def _check_any_unary(self, node: Tree) -> bool:
        if len(node.children) != 2 or not self._is_any_operand(node.children[1]):
            return False
        op = str(node.children[0])
        self._report_any_operand(node.children[1], f"operator `{op}`", "int")
        return True

    def _check_any_comparison(self, operands: list, ops: list) -> bool:
        for index, op in enumerate(ops):
            left, right = operands[index], operands[index + 1]
            if op in {"<", ">", "<=", ">="}:
                for side, other in ((left, right), (right, left)):
                    if self._is_any_operand(side) and not self._receiver_class_from_type(
                        self._expr_simple_type(other)
                    ):
                        self._report_any_operand(side, f"operator `{op}`", self._concrete_hint(other))
                        return True
            elif op in {"in", "not in"} and self._is_any_operand(right):
                self._report_any_operand(right, f"the right operand of `{op}`", "list[any]")
                return True
        return False

    def _check_any_getitem(self, node: Tree, receiver, index) -> bool:
        if self._is_any_operand(receiver):
            if isinstance(index, Tree) and index.data == "slice":
                self._report_any_operand(receiver, "slicing", "list[any]")
                return True
            index_type = self._expr_simple_type(index)
            hint = "dict[str, any]" if self._is_named_type(index_type, "str") else "list[any]"
            self._report_any_operand(receiver, "indexing", hint)
            return True
        if isinstance(index, Tree) and index.data != "slice" and self._is_any_operand(index):
            receiver_type = self._expr_simple_type(receiver)
            if receiver_type is None or self._is_named_type(receiver_type, "json"):
                return False
            expected = self._getitem_index_type(receiver_type)
            if expected is None or self._is_any_type(expected):
                return False
            self._report_any_operand(index, "an index", render_type(expected))
            return True
        return False

    def _check_any_iterable(self, iterable) -> bool:
        if not self._is_any_operand(iterable):
            return False
        self._report_any_operand(iterable, "iteration", "list[any]")
        return True

    def _check_any_condition(self, cond, action: str) -> bool:
        if not self._is_any_operand(cond):
            return False
        self._report_any_operand(cond, action, "bool")
        return True

    def _check_any_call(self, call: Tree) -> bool:
        """``v(1)`` on an ``any`` callee, ``len(v)`` and the other builtins
        that need a concrete argument."""
        callee = call.children[0] if call.children else None
        if not isinstance(callee, Tree):
            return False
        if callee.data == "var" and callee.children:
            name = self._name_text(callee.children[0])
            hint = _CONCRETE_ARG_BUILTINS.get(name)
            if hint and name not in self._func_return_types and not self._var_type(name):
                args = [self._unwrap_argvalue(a) for a in self._call_positional_args(call)]
                for arg in args:
                    if self._is_any_operand(arg):
                        if name in {"min", "max"}:
                            others = [a for a in args if a is not arg]
                            hint = self._concrete_hint(others[0]) if others else "list[any]"
                        self._report_any_operand(arg, f"`{name}()`", hint)
                        return True
                return False
        if callee.data in {"var", "getitem", "getattr", "funccall", "cast_expr"} and self._is_any_operand(callee):
            if callee.data == "getattr" and self._getattr_is_method_reference(callee):
                return False
            self._report_any_operand(callee, "a call", "func(...) -> T")
            return True
        return False

    def _getattr_is_method_reference(self, node: Tree) -> bool:
        """``obj.method`` where ``method`` is a method, not an ``any`` field."""
        receiver_type = self._expr_simple_type(node.children[0])
        class_name = self._receiver_class_from_type(receiver_type)
        if not class_name:
            return True
        attr = self._name_text(node.children[1])
        return bool(self._class_method_shape(class_name, attr))

    def _check_any_augassign(self, node: Tree, op: str, value) -> bool:
        target = node.children[0]
        if self._is_any_operand(target):
            hint = self._concrete_hint(value) if isinstance(value, Tree) else "int"
            if isinstance(value, Token):
                hint = "str" if value.type in {"STRING", "LONG_STRING"} else "int"
            self._report_any_operand(target, f"operator `{op}=`", hint)
            return True
        if isinstance(value, Tree) and self._is_any_operand(value):
            if self._receiver_class_from_type(self._expr_simple_type(target)):
                return False
            self._report_any_operand(value, f"operator `{op}=`", self._concrete_hint(target))
            return True
        return False

    def _check_any_unpacking(self, value) -> bool:
        if not self._is_any_operand(value):
            return False
        self._report_any_operand(value, "unpacking", "list[any]")
        return True
