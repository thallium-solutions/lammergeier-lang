#!/usr/bin/env python3
"""Expression visitor methods for the transpiler."""

from __future__ import annotations
from lark import Tree, Token
from typing import List, Optional
import re
from compiler.constants import (
    PYTHON_EXCEPTIONS, LOWER_PKGS, GO_BUILTINS,
    OP_TO_DUNDER, CMP_TO_DUNDER,
)


class ExpressionVisitorMixin:
    """Handles expression-to-Go conversion, function calls, f-strings, comprehensions."""

    def _parent_alias_base(self, name: str) -> str:
        if not name or not self.current_class or not self._self_replacement:
            return ""
        if name in self.declared_vars:
            return ""
        return self.class_base_aliases.get(self.current_class, {}).get(name, "")

    def _parent_alias_expr(self, name: str) -> str:
        base = self._parent_alias_base(name)
        if not base:
            return ""
        return f"{self._self_replacement}.{self._go_public_name(base)}"

    def _parent_alias_call_info(self, func) -> tuple[str, str, str]:
        if not isinstance(func, Tree) or func.data != "getattr" or len(func.children) < 2:
            return "", "", ""
        obj = func.children[0]
        if not isinstance(obj, Tree) or obj.data != "var" or not obj.children:
            return "", "", ""
        alias = self._get_name(obj.children[0])
        base = self._parent_alias_base(alias)
        if not base:
            return "", "", ""
        receiver = f"{self._self_replacement}.{self._go_public_name(base)}"
        return alias, base, receiver

    # ─── Typed value conversion ────────────────────────────────

    def _typed_value_to_go(self, node, go_type: str) -> str:
        """Convert a value expression to Go, using the declared type for collection literals."""
        if isinstance(node, Tree):
            if node.data == "list" and go_type.startswith("[]"):
                elems = [self._expr_to_go(c) for c in node.children if c is not None]
                return f"{go_type}{{{', '.join(elems)}}}"
            if node.data == "list_comprehension" and go_type.startswith("[]"):
                return self._list_comp_to_go(node, elem_type=go_type)
            if node.data == "dict_comprehension" and go_type.startswith("map["):
                return self._dict_comp_to_go(node, map_type=go_type)
            if node.data == "dict" and go_type.startswith("map["):
                entries = []
                for child in node.children:
                    if isinstance(child, Tree) and child.data == "key_value":
                        k = self._expr_to_go(child.children[0])
                        v = self._expr_to_go(child.children[1])
                        entries.append(f"{k}: {v}")
                return f"{go_type}{{{', '.join(entries)}}}"
        raw = self._expr_to_go(node)
        # ── Class-pointer coercion ─────────────────────────────
        # When annassign targets a Lam class (``go_type`` is
        # ``*<ClassName>``) and the value is either:
        #
        #   * an attribute access on another Lam object
        #     (``r.error`` / ``r.value`` — the attribute's Go type is
        #     ``interface{}``), or
        #   * a bare variable reference whose tracked Go type is
        #     ``interface{}`` (e.g. an ``err`` bound by a ``catch``
        #     clause — we now preserve the live panic value rather
        #     than stringifying it, so typed unboxing of the caught
        #     object is the natural way to reach its fields),
        #
        # wrap the rvalue in a type assertion so the declaration
        # typechecks:
        #
        #     err: Error = r.error
        #   → var err *Error = r.Error.(*Error)
        #
        #     ve:  ValidationError = err   # err bound by catch
        #   → var ve *ValidationError = err.(*ValidationError)
        #
        # ``getattr`` / ``var`` are the only shapes we coerce —
        # function calls and constructors already return typed
        # ``*Class`` values and double-casting would be a silent
        # correctness hazard if the return type ever drifts.
        if go_type.startswith("*") and not raw.endswith(f".({go_type})"):
            class_name = go_type[1:]
            if class_name in self._class_names and isinstance(node, Tree):
                if node.data == "getattr":
                    return f"{raw}.({go_type})"
                if node.data == "var":
                    var_name = self._get_name(node.children[0])
                    if self._var_go_types.get(var_name) == "interface{}":
                        return f"{raw}.({go_type})"
        return raw

    # ─── Main expression dispatcher ───────────────────────────

    def _expr_to_go(self, node) -> str:
        if node is None:
            return ""
        if isinstance(node, Token):
            return str(node)
        if not isinstance(node, Tree):
            return str(node)

        d = node.data

        if d == "var":
            name = self._get_name(node.children[0])
            if name == "self" and self._self_replacement:
                return self._self_replacement
            parent_expr = self._parent_alias_expr(name)
            if parent_expr:
                return parent_expr
            # Bare references to user-defined top-level functions need to
            # resolve to their Go public name so `Http.serve(port, handler)`
            # passes the actual Go function value rather than an undefined
            # lowercase identifier. Guarded so this only fires when we are
            # not in a call position (the call path extracts the raw name
            # directly and wraps it in _funccall_to_go).
            if (
                name
                and name not in self.declared_vars
                and name in self._user_functions
                and not self._in_funccall_head
                and name != "main"
            ):
                return self._go_public_name(name)
            return name

        if d == "name":
            return self._get_name(node)

        if d == "number":
            return self._number_to_go(node)

        if d == "string":
            raw = str(node.children[0])
            if raw.startswith('"""') or raw.startswith("'''"):
                inner = raw[3:-3]
                inner = inner.replace('`', '` + "`" + `')
                return f"`{inner}`"
            # Normalise Python-style single-quoted strings to Go double-quoted
            # strings so that 'foo' does not produce a rune literal.
            if raw.startswith("'") and raw.endswith("'"):
                inner = raw[1:-1]
                # Unescape single quote, then escape any unescaped double quote.
                inner = inner.replace(r"\'", "'")
                inner = re.sub(r'(?<!\\)"', r'\\"', inner)
                return f'"{inner}"'
            return raw

        if d == "fstring":
            return self._fstring_to_go(node)

        if d == "string_concat":
            parts = [self._expr_to_go(c) for c in node.children if c is not None]
            return " + ".join(parts) if len(parts) > 1 else parts[0] if parts else '""'

        if d == "const_none":
            return "nil"
        if d == "const_true":
            return "true"
        if d == "const_false":
            return "false"
        if d == "ellipsis":
            return "nil"

        if d == "funccall":
            return self._funccall_to_go(node)

        if d == "getattr":
            attr = self._get_name(node.children[1])
            obj_node = node.children[0]
            if isinstance(obj_node, Tree) and obj_node.data == "var":
                raw_obj = self._get_name(obj_node.children[0])
                if attr in self._static_vars.get(raw_obj, {}):
                    return self._static_var_go_name(raw_obj, attr)
            obj = self._expr_to_go(obj_node)
            if obj in LOWER_PKGS:
                return f"{obj}.{attr}"
            if self._is_private_method_name(attr):
                return f"{obj}.{self._go_private_name(attr)}"
            return f"{obj}.{self._go_public_name(attr)}"

        if d == "getattr_safe":
            # ``obj?.field`` — guard the access with a nil-check and
            # return ``nil`` when the receiver is ``nil``. The result
            # type is ``interface{}`` because we can't statically
            # guarantee the field's concrete type in every context; a
            # caller that needs a typed value can assign to an
            # annotated variable (the assignment site will unbox).
            obj = self._expr_to_go(node.children[0])
            attr = self._get_name(node.children[1])
            if self._is_private_method_name(attr):
                field = self._go_private_name(attr)
            else:
                field = self._go_public_name(attr)
            return (
                f"func() interface{{}} {{ if {obj} != nil {{ "
                f"return {obj}.{field} }}; return nil }}()"
            )

        if d == "propagate":
            # ``expr?`` — Result-propagation operator. Lowers to:
            #
            #     __qN := <expr>
            #     if !__qN.Ok() { return __qN }
            #     __qN.Value [.(GO_TYPE)]
            #
            # The typed assertion is added when the enclosing
            # annassign published its target Go type via
            # ``_propagate_cast_hint``; otherwise the result is left
            # as ``interface{}`` and either flows through an untyped
            # context (function arg of type ``any``, ``return``) or
            # the user lifts to a typed local for the cast.
            #
            # When the enclosing function *doesn't* return a
            # ``Result`` (and we aren't inside a ``do { } catch``
            # IIFE) there's no valid ``return`` target for the
            # propagated ``*Result``. The semantic checker already
            # warned the author, but we still need to emit *something*
            # that compiles. Falling back to ``panic(__qN.Error)``
            # turns an ``Err`` into an exception, which is recoverable
            # via a ``try { } catch { }`` upstream and surfaces the
            # underlying error message instead of Go's opaque
            # "too many return values" build failure.
            inner_str = self._expr_to_go(node.children[0])
            self._q_temp_counter += 1
            tmp = f"__q{self._q_temp_counter}"
            self._emit(f"{tmp} := {inner_str}")
            if self._q_propagate_ok:
                self._emit(f"if !{tmp}.Ok() {{ return {tmp} }}")
            else:
                self._emit(
                    f"if !{tmp}.Ok() {{ "
                    f"panic({tmp}.Error) "
                    f"}}"
                )
            self._declare_var(tmp)
            cast = self._propagate_cast_hint
            if cast and cast != "interface{}":
                return f"{tmp}.Value.({cast})"
            return f"{tmp}.Value"

        if d == "null_coalesce":
            # ``a ?? b ?? c`` — return the first non-nil operand. We
            # fold right-to-left, each layer wrapping the previous
            # result, so evaluation still stops at the first non-nil
            # value (Go's early ``return`` inside the IIFE).
            #
            # The left operand is boxed as ``interface{}`` before the
            # comparison so Go doesn't complain about untyped-nil or
            # primitive-to-nil comparisons (``"s" != nil`` and
            # ``nil != nil`` are both compile errors otherwise).
            parts = [self._expr_to_go(c) for c in node.children if c is not None]
            if not parts:
                return "nil"
            if len(parts) == 1:
                return parts[0]
            expr = parts[-1]
            for left in reversed(parts[:-1]):
                expr = (
                    f"func() interface{{}} {{ _v := interface{{}}({left}); "
                    f"if _v != nil {{ return _v }}; return {expr} }}()"
                )
            return expr

        if d == "getitem":
            inner = node.children[1]
            if isinstance(inner, Tree) and inner.data == "slice":
                obj_go = self._expr_to_go(node.children[0])
                return self._slice_to_go(obj_go, inner, node.children[0])
            obj = self._expr_to_go(node.children[0])
            idx = self._expr_to_go(inner)
            return f"{obj}[{idx}]"

        if d == "slice":
            # Bare ``slice`` (no enclosing ``getitem``) shouldn't appear
            # in valid programs, but emit something parseable rather
            # than crashing.
            parts = []
            for c in node.children:
                if c is None:
                    parts.append("")
                elif isinstance(c, Tree) and c.data == "sliceop":
                    step_val = ""
                    for sc in c.children:
                        if sc is not None:
                            step_val = self._expr_to_go(sc)
                    parts.append(step_val)
                else:
                    parts.append(self._expr_to_go(c))
            while len(parts) > 2 and parts[-1] == "":
                parts.pop()
            return ":".join(parts)

        if d == "arith_expr" or d == "term":
            overloaded = self._try_operator_overload(node)
            if overloaded:
                return overloaded
            return self._binop_to_go(node)

        if d == "and_expr":
            parts = [self._expr_to_go(c) for c in node.children if c is not None]
            wrapped = [f"({p})" if any(op in p for op in "+-*/%") else p for p in parts]
            inner = " & ".join(wrapped)
            return f"({inner})" if len(parts) > 1 else inner

        if d == "or_expr":
            parts = [self._expr_to_go(c) for c in node.children if c is not None]
            wrapped = [f"({p})" if any(op in p for op in "+-*/%") else p for p in parts]
            inner = " | ".join(wrapped)
            return f"({inner})" if len(parts) > 1 else inner

        if d == "xor_expr":
            parts = [self._expr_to_go(c) for c in node.children if c is not None]
            wrapped = [f"({p})" if any(op in p for op in "+-*/%") else p for p in parts]
            inner = " ^ ".join(wrapped)
            return f"({inner})" if len(parts) > 1 else inner

        if d == "shift_expr":
            return self._binop_to_go(node)

        if d == "factor":
            if len(node.children) == 2:
                op = str(node.children[0])
                operand_node = node.children[1]
                if op == "-":
                    cls = self._infer_expr_class(operand_node)
                    method = self._get_dunder_method(cls, "__neg__") if cls else ""
                    if method:
                        return f"{self._expr_to_go(operand_node)}.{method}()"
                if op == "~":
                    cls = self._infer_expr_class(operand_node)
                    method = self._get_dunder_method(cls, "__invert__") if cls else ""
                    if method:
                        return f"{self._expr_to_go(operand_node)}.{method}()"
                    op = "^"
                operand = self._expr_to_go(operand_node)
                return f"({op}{operand})"
            return self._expr_to_go(node.children[0])

        if d == "power":
            base_node = node.children[0]
            if len(node.children) > 1 and node.children[1] is not None:
                cls = self._infer_expr_class(base_node)
                method = self._get_dunder_method(cls, "__pow__") if cls else ""
                if method:
                    left = self._expr_to_go(base_node)
                    right = self._expr_to_go(node.children[1])
                    return f"{left}.{method}({right})"
                base = self._expr_to_go(base_node)
                exp = self._expr_to_go(node.children[1])
                base_is_int = isinstance(node.children[0], Tree) and node.children[0].data == "number" and "." not in str(node.children[0].children[0])
                exp_is_int = isinstance(node.children[1], Tree) and node.children[1].data == "number" and "." not in str(node.children[1].children[0])
                if base_is_int and exp_is_int:
                    try:
                        exp_val = int(exp)
                        if exp_val > 63:
                            self._need_import("math/big")
                            return f'new(big.Int).Exp(big.NewInt({base}), big.NewInt({exp}), nil)'
                    except ValueError:
                        pass
                self._need_import("math")
                return f"math.Pow(float64({base}), float64({exp}))"
            return base

        if d == "comparison":
            overloaded = self._try_comparison_overload(node)
            if overloaded:
                return overloaded
            return self._comparison_to_go(node)

        if d == "not_test":
            return f"!({self._expr_to_go(node.children[0])})"

        if d in ("or_test", "and_test"):
            op = "&&" if d == "and_test" else "||"
            parts = [self._expr_to_go(c) for c in node.children if isinstance(c, Tree)]
            return f" {op} ".join(parts)

        if d == "tuple":
            elems = [self._expr_to_go(c) for c in node.children if c is not None]
            return ", ".join(elems)

        if d == "list":
            elems = [self._expr_to_go(c) for c in node.children if c is not None]
            return f"[]interface{{}}{{{', '.join(elems)}}}"

        if d == "list_comprehension":
            return self._list_comp_to_go(node)

        if d == "dict":
            return self._dict_to_go(node)

        if d == "dict_comprehension":
            return self._dict_comp_to_go(node)

        if d == "set":
            # Build the set inside an IIFE so duplicate literals
            # don't trip Go's "duplicate key in map literal" check
            # — sets are de-duplicating by definition.
            elems = [self._expr_to_go(c) for c in node.children if c is not None]
            if not elems:
                return "map[interface{}]bool{}"
            lines = ["func() map[interface{}]bool {",
                     "\t_s := map[interface{}]bool{}"]
            for e in elems:
                lines.append(f"\t_s[{e}] = true")
            lines.append("\treturn _s")
            lines.append("}()")
            return self._pin_multiline_to(node, "\n".join(lines))

        if d == "set_comprehension":
            return self._set_comp_to_go(node)

        if d == "tuple_comprehension":
            return self._list_comp_to_go(node)

        if d == "star_expr":
            return self._expr_to_go(node.children[0]) + "..."

        if d == "kwargs":
            return self._expr_to_go(node.children[0])

        if d == "testlist_tuple":
            return ", ".join(self._expr_to_go(c) for c in node.children if c is not None)

        if d == "test":
            if len(node.children) == 3:
                true_val = self._expr_to_go(node.children[0])
                cond = self._expr_to_go(node.children[1])
                false_val = self._expr_to_go(node.children[2])
                return f"func() interface{{}} {{ if {cond} {{ return {true_val} }}; return {false_val} }}()"
            return self._expr_to_go(node.children[0])

        if d == "assign_expr":
            name = self._get_name(node.children[0])
            val = self._expr_to_go(node.children[1])
            return f"func() interface{{}} {{ {name} := {val}; return {name} }}()"

        if d == "lambdef":
            return self._lambda_to_go(node)

        if d == "yield_expr" or d == "yield_from":
            if self._in_generator and self._generator_chan:
                if node.children and node.children[0] is not None:
                    val = self._expr_to_go(node.children[0])
                    return f"{self._generator_chan} <- {val}"
                return f"{self._generator_chan} <- nil"
            return "/* yield not supported */"

        if d == "await_call":
            return f"<-{self._expr_to_go(node.children[0])}"

        if d == "type_expr":
            return self._type_expr_to_go(node)

        # Fallback
        parts = [self._expr_to_go(c) for c in node.children if c is not None]
        return " ".join(parts)

    # ─── Function calls ───────────────────────────────────────

    def _args_to_go(self, args: List[str]) -> str:
        return ", ".join(args)

    def _is_private_method_name(self, method_name: str) -> bool:
        return any(method_name in methods for methods in self._private_methods.values())

    def _go_user_function_name(self, func_name: str) -> str:
        if func_name in self._private_functions:
            return self._go_private_name(func_name)
        if func_name != "main":
            return self._go_public_name(func_name)
        return func_name

    def _is_user_function_reference(self, func_expr: str) -> bool:
        if func_expr in self._user_functions:
            return True
        return any(
            self._go_public_name(name) == func_expr
            or self._go_private_name(name) == func_expr
            for name in self._user_functions
        )

    def _user_function_call_to_go(
        self, func_name: str, args: List[str], kwargs, args_node,
        type_args: Optional[List[str]] = None,
    ) -> str:
        go_name = self._go_user_function_name(func_name)
        call_args = self._apply_call_kwargs(func_name, args, kwargs)
        call_args = self._fill_default_args(func_name, call_args)
        if (len(self._overloaded_functions.get(func_name, set())) > 1
                or len(self._overload_variants.get(func_name, [])) > 1):
            call_sig = self._infer_call_arg_sig(args_node)
            go_name = f"{go_name}{self._overload_suffix_for_sig(func_name, call_sig)}"
        if type_args is not None:
            return f"{go_name}[{self._args_to_go(type_args)}]({self._args_to_go(call_args)})"
        return f"{go_name}({self._args_to_go(call_args)})"

    def _nested_generic_call_to_go(
        self, func_name: str, args: List[str],
        explicit_type_args: Optional[List[str]] = None,
    ) -> str:
        info = self._nested_generic_functions[func_name]
        hidden = [name for name, _ in info.get("captures", [])]
        type_args = list(info.get("outer_type_names", []))
        if explicit_type_args:
            type_args.extend(explicit_type_args)
        call_args = hidden + args
        if type_args:
            return f"{info['go_name']}[{self._args_to_go(type_args)}]({self._args_to_go(call_args)})"
        return f"{info['go_name']}({self._args_to_go(call_args)})"

    def _generic_getitem_call_to_go(
        self, func, args: List[str], kwargs, args_node,
    ) -> Optional[str]:
        if not (
            isinstance(func, Tree)
            and func.data == "getitem"
            and isinstance(func.children[0], Tree)
            and func.children[0].data == "var"
        ):
            return None
        base_name = self._get_name(func.children[0].children[0])
        if base_name in self._nested_generic_functions:
            type_args = self._type_arg_list(func.children[1])
            return self._nested_generic_call_to_go(base_name, args, type_args)
        if base_name in self._class_names and base_name in self._generic_classes:
            type_args = self._type_arg_list(func.children[1])
            go_cls = self._go_public_name(base_name)
            init_key = f"{base_name}.init"
            call_args = self._apply_call_kwargs(init_key, args, kwargs)
            call_args = self._fill_default_args(init_key, call_args)
            return f"New{go_cls}[{self._args_to_go(type_args)}]({self._args_to_go(call_args)})"
        if base_name in self._user_functions:
            type_args = self._type_arg_list(func.children[1])
            return self._user_function_call_to_go(
                base_name, args, kwargs, args_node, type_args=type_args,
            )
        return None

    def _funccall_to_go(self, node: Tree) -> str:
        func = node.children[0]
        args_node = node.children[1] if len(node.children) > 1 else None
        args, kwargs = self._collect_call_args(args_node)

        # ``Pair[int, str](...)`` — generic-constructor call on a user
        # class. The parser produces ``funccall(getitem(Pair, <types>),
        # args)`` and the naive lowering would dump the subscript into
        # the callee expression, losing commas and producing invalid Go.
        # Intercept that shape here and emit ``NewPair[int, string](...)``
        # directly.
        generic_call = self._generic_getitem_call_to_go(func, args, kwargs, args_node)
        if generic_call is not None:
            return generic_call

        # ``obj?.method(args)`` — safe-navigation followed by a call.
        # We don't want the ``getattr_safe`` IIFE (which returns
        # ``interface{}``) as a callee, so rewrite the whole thing as
        # a nil-guarded call at this layer.
        if isinstance(func, Tree) and func.data == "getattr_safe":
            obj = self._expr_to_go(func.children[0])
            attr = self._get_name(func.children[1])
            if self._is_private_method_name(attr):
                method = self._go_private_name(attr)
            else:
                method = self._go_public_name(attr)
            args_str = ", ".join(args)
            return (
                f"func() interface{{}} {{ if {obj} != nil {{ "
                f"return {obj}.{method}({args_str}) }}; return nil }}()"
            )

        raw_method = None
        raw_obj = None
        if isinstance(func, Tree) and func.data == "getattr":
            raw_obj = self._expr_to_go(func.children[0])
            raw_method = self._get_name(func.children[1])
            _alias, parent_base, parent_receiver = self._parent_alias_call_info(func)
            if parent_base and raw_method:
                method_key = f"{parent_base}.init" if raw_method in {"init", "__init__"} else f"{parent_base}.{raw_method}"
                args = self._apply_call_kwargs(method_key, args, kwargs)
                args = self._fill_default_args(method_key, args)
                if raw_method in {"init", "__init__"}:
                    go_base = self._go_public_name(parent_base)
                    return f"{parent_receiver} = New{go_base}({', '.join(args)})"

        # Resolve the callee expression without letting bare identifiers
        # be rewritten to their Go public name (that's handled here).
        with self._scoped(_in_funccall_head=True):
            func_str = self._expr_to_go(func)

        # ── Built-in mappings ──
        if func_str == "print":
            self._need_import("fmt")
            return f'fmt.Println({", ".join(args)})'

        if func_str == "len":
            return f"len({args[0]})" if args else "0"

        if func_str == "str":
            self._need_import("fmt")
            return f'fmt.Sprintf("%v", {args[0]})' if args else '""'

        if func_str == "repr":
            self._need_import("fmt")
            return f'fmt.Sprintf("%#v", {args[0]})' if args else '""'

        if func_str == "int":
            if args:
                return f"int({args[0]})"
            return "0"

        if func_str == "float":
            return f"float64({args[0]})" if args else "0.0"

        if func_str == "bool":
            return f"({args[0]} != 0)" if args else "false"

        if func_str in ("panic", "recover", "make", "new", "cap", "copy", "delete", "close"):
            return f"{func_str}({', '.join(args)})"

        if func_str in ("string", "byte", "rune", "int8", "int16", "int32", "int64",
                         "uint", "uint8", "uint16", "uint32", "uint64",
                         "float32", "float64"):
            return f"{func_str}({', '.join(args)})" if args else f"{func_str}(0)"

        if func_str == "abs":
            self._need_import("math")
            return f"math.Abs(float64({args[0]}))" if args else "0"

        if func_str == "max":
            self._need_import("math")
            if len(args) == 2:
                return f"math.Max(float64({args[0]}), float64({args[1]}))"

        if func_str == "min":
            self._need_import("math")
            if len(args) == 2:
                return f"math.Min(float64({args[0]}), float64({args[1]}))"

        if func_str == "sorted":
            self._need_import("sort")
            if args:
                return f"func() []interface{{}} {{ tmp := append([]interface{{}}{{}}, {args[0]}...); sort.Slice(tmp, func(i, j int) bool {{ return fmt.Sprintf(\"%v\", tmp[i]) < fmt.Sprintf(\"%v\", tmp[j]) }}); return tmp }}()"

        if func_str == "enumerate":
            # In a for-head ``for i, v in enumerate(xs)`` the for-stmt
            # visitor rewrites this directly into a Go ``range`` loop.
            # A bare call needs to materialise the ``(i, v)`` pairs, so
            # we reflect over the iterable at runtime and emit a slice
            # where each element is a two-element ``[]interface{}``.
            if not args:
                return "[]interface{}{}"
            self._need_import("reflect")
            src = args[0]
            return (
                "func() []interface{} { "
                "v := reflect.ValueOf(" + src + "); "
                "if !v.IsValid() { return []interface{}{} }; "
                "out := make([]interface{}, v.Len()); "
                "for i := 0; i < v.Len(); i++ { "
                "out[i] = []interface{}{i, v.Index(i).Interface()} "
                "}; return out }()"
            )

        if func_str == "isinstance":
            raw = self._get_raw_call_args(args_node)
            if len(raw) >= 2:
                obj_go = self._expr_to_go(raw[0])
                return self._isinstance_test_go(obj_go, raw[1])
            return "false"

        if func_str == "type":
            self._need_import("fmt")
            return f'fmt.Sprintf("%T", {args[0]})' if args else '"unknown"'

        if func_str == "input":
            self._need_import("fmt")
            self._need_import("bufio")
            self._need_import("os")
            if args:
                return f'func() string {{ fmt.Print({args[0]}); scanner := bufio.NewScanner(os.Stdin); scanner.Scan(); return scanner.Text() }}()'
            return f'func() string {{ scanner := bufio.NewScanner(os.Stdin); scanner.Scan(); return scanner.Text() }}()'

        if func_str == "exit":
            self._need_import("os")
            return f"os.Exit({args[0] if args else '0'})"

        if func_str == "append":
            if len(args) >= 2:
                return f"append({args[0]}, {', '.join(args[1:])})"

        if func_str == "format":
            self._need_import("fmt")
            return f'fmt.Sprintf({", ".join(args)})'

        # ── math module ──
        if func_str.startswith("math."):
            self._need_import("math")
            method = func_str[5:]
            go_method = method[0].upper() + method[1:]
            return f"math.{go_method}({', '.join(args)})"

        if func_str.startswith("os."):
            self._need_import("os")
            return f"{func_str}({', '.join(args)})"

        if func_str.startswith("strings."):
            self._need_import("strings")
            return f"{func_str}({', '.join(args)})"

        # ── String methods ──
        #
        # Every Lam-side string method (``"hello".toUpper()``,
        # ``s.split(",")``, …) lowers to a call into the
        # ``lamstrings`` standard library rather than inlining a
        # ``strings.X`` Go call here. The dispatch key matches the
        # Lam-facing static-method name on ``lamstrings.Strings``
        # exactly (``toUpper``, ``trim``, ``startsWith``,
        # ``index``, …) so the language surface reads like the
        # stdlib: ``"hi".toUpper()`` and ``Strings.toUpper("hi")``
        # are spelled the same way and run the same code. The
        # Go-mangled emission is ``Strings_<methodName>``.
        # See ``lib/lamstrings.lam`` for the reference
        # implementations; the compiler driver auto-injects
        # ``lamstrings`` into the import worklist whenever any of
        # these dispatch names appears in user source, so callers
        # don't need ``from lamstrings import Strings`` for the
        # syntactic sugar to "just work".
        #
        # The whitespace cutset for argument-less ``.trimLeft()``
        # / ``.trimRight()`` matches what the Go-level
        # ``strings.TrimLeft`` / ``TrimRight`` defaults used to be
        # — a small, ASCII-only set, not the full Unicode
        # whitespace class that ``strings.TrimSpace`` would use.
        # We keep that exact behaviour so existing user code
        # doesn't change semantics under the refactor.
        _ws_arg = '" \\t\\n"'

        def _strings_call(method, *args):
            return f"Strings_{method}({', '.join(args)})"

        string_methods = {
            "repeat":     lambda o, a: _strings_call("repeat", o, a[0]),
            "contains":   lambda o, a: _strings_call("contains", o, a[0]),
            "hasPrefix":  lambda o, a: _strings_call("hasPrefix", o, a[0]),
            "hasSuffix":  lambda o, a: _strings_call("hasSuffix", o, a[0]),
            "toUpper":    lambda o, a: _strings_call("toUpper", o),
            "toLower":    lambda o, a: _strings_call("toLower", o),
            "trim":       lambda o, a: _strings_call("trim", o),
            "length":     lambda o, a: _strings_call("length", o),
            # ``Strings.trimLeft`` / ``trimRight`` need an explicit
            # cutset; default to ASCII whitespace when the Lam call
            # was bare (``s.trimLeft()``).
            "trimLeft":   lambda o, a: _strings_call("trimLeft", o, a[0] if a else _ws_arg),
            "trimRight":  lambda o, a: _strings_call("trimRight", o, a[0] if a else _ws_arg),
            "replace":    lambda o, a: _strings_call("replace", o, *a),
            "split":      lambda o, a: _strings_call("split", o, a[0]),
            # ``"sep".join(parts)`` — the receiver is the
            # separator, the argument is the sequence, but
            # ``Strings.join(parts, sep)`` takes them the other
            # way around. The dispatcher swaps them so callers
            # keep the familiar receiver-as-separator ordering.
            "join":       lambda o, a: _strings_call("join", a[0], o) if a else o,
            "startsWith": lambda o, a: _strings_call("startsWith", o, a[0]),
            "endsWith":   lambda o, a: _strings_call("endsWith", o, a[0]),
            # ``Strings.index`` returns -1 when the substring is
            # absent — same shape as Go's ``strings.Index``.
            "index":      lambda o, a: _strings_call("index", o, a[0]),
            "lastIndex":  lambda o, a: _strings_call("lastIndex", o, a[0]),
            "count":      lambda o, a: _strings_call("count", o, a[0]),
            "title":      lambda o, a: _strings_call("title", o),
            "equalFold":  lambda o, a: _strings_call("equalFold", o, a[0]),
            "fields":     lambda o, a: _strings_call("fields", o),
            "capitalize": lambda o, a: _strings_call("capitalize", o),
            "isAlpha":    lambda o, a: _strings_call("isAlpha", o),
            "isDigit":    lambda o, a: _strings_call("isDigit", o),
            "isAlnum":    lambda o, a: _strings_call("isAlnum", o),
            "isSpace":    lambda o, a: _strings_call("isSpace", o),
            "reverse":    lambda o, a: _strings_call("reverse", o),
            "center":     lambda o, a: _strings_call("center", o, a[0], a[1] if len(a) > 1 else '" "'),
            "zfill":      lambda o, a: _strings_call("zfill", o, a[0]),
            "padLeft":    lambda o, a: _strings_call("padLeft", o, a[0], a[1] if len(a) > 1 else '" "'),
            "padRight":   lambda o, a: _strings_call("padRight", o, a[0], a[1] if len(a) > 1 else '" "'),
            "splitLines": lambda o, a: _strings_call("splitLines", o),
            "splitN":     lambda o, a: _strings_call("splitN", o, a[0], a[1]),
            "replaceFirst": lambda o, a: _strings_call("replaceFirst", o, *a),
            "containsAny":  lambda o, a: _strings_call("containsAny", o, a[0]),
            "isEmpty":    lambda o, a: _strings_call("isEmpty", o),
            "isBlank":    lambda o, a: _strings_call("isBlank", o),
            "indent":     lambda o, a: _strings_call("indent", o, a[0]),
            "dedent":     lambda o, a: _strings_call("dedent", o),
            # ``Strings.format`` is variadic so the ``args`` are
            # forwarded as-is. Empty ``a`` is fine — Sprintf with
            # no extras just returns the template untouched.
            "format":     lambda o, a: _strings_call("format", o, *a),
        }

        if raw_method and raw_obj is not None:
            # Static method call
            if raw_obj in self._class_names and raw_obj in self._static_methods:
                if raw_method in self._static_methods[raw_obj]:
                    go_cls = self._go_public_name(raw_obj)
                    go_name = f"{go_cls}_{raw_method}"
                    method_key = f"{raw_obj}.{raw_method}"
                    args = self._apply_call_kwargs(method_key, args, kwargs)
                    args = self._fill_default_args(method_key, args)
                    return f"{go_name}({', '.join(args)})"

            # ``str``-builtins like ``replace``/``count``/``split``/``find``
            # take at least one mandatory argument. If a Lam method-call
            # uses one of those names with zero args (e.g.
            # ``qb.count()`` on a custom class), intercepting it as
            # ``strings.Count(o, " ")`` would silently emit nonsense.
            # Fall through to user-method dispatch in that case.
            string_method_min_args = {
                "repeat": 1,
                "contains": 1,
                "hasPrefix": 1,
                "hasSuffix": 1,
                "replace": 2,
                "split": 1,
                "join": 1,
                "startsWith": 1,
                "endsWith": 1,
                "index": 1,
                "lastIndex": 1,
                "count": 1,
                "equalFold": 1,
                "center": 1,
                "zfill": 1,
                "padLeft": 1,
                "padRight": 1,
                "splitN": 2,
                "replaceFirst": 2,
                "containsAny": 1,
                "indent": 1,
            }

            if (
                raw_method in string_methods
                and len(args) >= string_method_min_args.get(raw_method, 0)
            ):
                # Only treat this as a string-builtin call when we are sure
                # the receiver is actually a string. If the receiver is a
                # known class instance (tracked via a type annotation) we
                # delegate to the regular method-call lowering instead.
                obj_cls = self._var_types.get(raw_obj, "")
                obj_go_type = self._var_go_types.get(raw_obj, "")
                is_user_instance = bool(obj_cls) and obj_cls in self._class_names
                is_known_non_string = obj_go_type and obj_go_type != "string"
                if not is_user_instance and not is_known_non_string:
                    return string_methods[raw_method](raw_obj, args)

            # List methods — only applied when the receiver is (or may be)
            # a list. If we know the receiver is a user class instance,
            # dispatch to the user-defined method instead so `obj.pop()`
            # calls the class's `pop` rather than slicing the struct.
            _obj_cls = self._var_types.get(raw_obj, "")
            _obj_go = self._var_go_types.get(raw_obj, "")
            _is_user_inst = bool(_obj_cls) and _obj_cls in self._class_names
            # Inside a class method body, ``self`` (which lowers to the
            # configured replacement, normally ``s``) refers to the
            # current class instance. Treat it as a user instance so
            # ``self.pop()`` dispatches to the method rather than the
            # built-in list-pop transform.
            if (
                not _is_user_inst
                and self.current_class
                and self._self_replacement
                and raw_obj == self._self_replacement
            ):
                _obj_cls = self.current_class
                _is_user_inst = True
            _is_non_list = _obj_go and not _obj_go.startswith("[]")
            if not _is_user_inst and not _is_non_list:
                if raw_method == "length":
                    return f"len({raw_obj})"
                if raw_method == "append" and args:
                    return f"{raw_obj} = append({raw_obj}, {', '.join(args)})"
                if raw_method == "pop":
                    return f"func() interface{{}} {{ last := {raw_obj}[len({raw_obj})-1]; {raw_obj} = {raw_obj}[:len({raw_obj})-1]; return last }}()"
                if raw_method == "extend" and args:
                    return f"{raw_obj} = append({raw_obj}, {args[0]}...)"
                if raw_method == "insert" and len(args) >= 2:
                    return f"{raw_obj} = append({raw_obj}[:int({args[0]})], append([]interface{{}}{{{args[1]}}}, {raw_obj}[int({args[0]}):]...)...)"

            # Functional methods — same gate as the list methods above.
            # If the receiver is known to be a user class, fall through
            # to the generic method-call dispatcher so that classes can
            # define their own map/filter/reduce/… without colliding
            # with the built-in list combinators.
            _obj_go_type = self._var_go_types.get(raw_obj, "")
            _elem_type = ""
            if _obj_go_type.startswith("[]"):
                _elem_type = _obj_go_type[2:]
            if not _elem_type:
                # Chained receivers like ``nums.filter(...).map(...)``
                # aren't tracked in _var_go_types, so walk the AST to
                # see if we can infer the element type from an earlier
                # link in the chain.
                _elem_type = self._infer_receiver_elem_type(
                    func.children[0] if isinstance(func, Tree) else None
                )
            if not _elem_type:
                _elem_type = "interface{}"
            _slice_type = f"[]{_elem_type}"
            _apply_list_combinators = not _is_user_inst and not _is_non_list

            def _resolve_func_ref(fn_expr):
                if fn_expr in self._user_functions:
                    return self._go_user_function_name(fn_expr)
                return fn_expr

            def _is_user_func_arg(arg_expr):
                return self._is_user_function_reference(arg_expr)

            # Raw AST nodes for the functional-method arguments — we
            # inspect the first positional arg to detect inline lambdas
            # and transpile them with the receiver's element type as a
            # hint, so the emitted callback has typed params (and often
            # a concrete return type) rather than ``interface{}``.
            raw_args_nodes = self._get_raw_call_args(args_node)
            inline_lambda = (
                raw_args_nodes and self._is_lambda_node(raw_args_nodes[0])
            )

            raw_kwargs_nodes = {}
            if isinstance(args_node, Tree) and args_node.data == "arguments":
                for child in args_node.children:
                    if not (
                        isinstance(child, Tree)
                        and child.data == "argvalue"
                        and len(child.children) == 2
                    ):
                        continue
                    name_node = child.children[0]
                    if (
                        isinstance(name_node, Tree)
                        and name_node.data == "var"
                        and name_node.children
                    ):
                        raw_kwargs_nodes[
                            self._get_name(name_node.children[0])
                        ] = child.children[1]

            if _apply_list_combinators and raw_method == "sort":
                self._need_import("sort")
                sort_args = list(args)
                compare_expr = kwargs.get("compare")
                inplace_expr = kwargs.get("inplace")
                compare_raw = raw_kwargs_nodes.get("compare")
                if sort_args:
                    first = sort_args[0]
                    if first in ("true", "false") and len(sort_args) == 1:
                        inplace_expr = first
                    else:
                        compare_expr = first
                        compare_raw = raw_args_nodes[0] if raw_args_nodes else None
                        if len(sort_args) >= 2:
                            inplace_expr = sort_args[1]
                inplace = inplace_expr == "true"
                target_expr = raw_obj if inplace else f"append({_slice_type}{{}}, {raw_obj}...)"
                if compare_expr:
                    if compare_raw is not None and self._is_lambda_node(compare_raw):
                        fn = self._lambda_to_go(
                            compare_raw,
                            param_types=[_elem_type, _elem_type],
                            return_type="bool",
                        )
                        less_expr = f"({fn})(_r[i], _r[j])"
                    else:
                        fn = _resolve_func_ref(compare_expr)
                        less_expr = f"{fn}(_r[i], _r[j])"
                elif _elem_type in (
                    "int", "int8", "int16", "int32", "int64",
                    "uint", "uint8", "uint16", "uint32", "uint64",
                    "float32", "float64", "string",
                ):
                    less_expr = "_r[i] < _r[j]"
                elif _elem_type == "bool":
                    less_expr = "!_r[i] && _r[j]"
                else:
                    self._need_import("fmt")
                    less_expr = (
                        'fmt.Sprintf("%v", _r[i]) < '
                        'fmt.Sprintf("%v", _r[j])'
                    )
                return (
                    f"func() {_slice_type} {{ _r := {target_expr}; "
                    f"sort.Slice(_r, func(i, j int) bool {{ "
                    f"return {less_expr} }}); return _r }}()"
                )

            if _apply_list_combinators and raw_method == "map" and args:
                if inline_lambda:
                    fn = self._lambda_to_go(
                        raw_args_nodes[0], param_types=[_elem_type]
                    )
                    return (
                        f"func() {_slice_type} {{ _r := make({_slice_type}, len({raw_obj})); "
                        f"for _i, _v := range {raw_obj} {{ _r[_i] = ({fn})(_v) }}; return _r }}()"
                    )
                fn = _resolve_func_ref(args[0])
                is_user_func = _is_user_func_arg(args[0])
                if is_user_func or _elem_type == "interface{}":
                    return f"func() {_slice_type} {{ _r := make({_slice_type}, len({raw_obj})); for _i, _v := range {raw_obj} {{ _r[_i] = {fn}(_v) }}; return _r }}()"
                else:
                    return f"func() {_slice_type} {{ _r := make({_slice_type}, len({raw_obj})); for _i, _v := range {raw_obj} {{ _r[_i] = {fn}(_v).({_elem_type}) }}; return _r }}()"
            if _apply_list_combinators and raw_method == "filter" and args:
                if inline_lambda:
                    fn = self._lambda_to_go(
                        raw_args_nodes[0], param_types=[_elem_type], return_type="bool"
                    )
                    return (
                        f"func() {_slice_type} {{ _r := {_slice_type}{{}}; "
                        f"for _, _v := range {raw_obj} {{ if ({fn})(_v) {{ _r = append(_r, _v) }} }}; return _r }}()"
                    )
                fn = _resolve_func_ref(args[0])
                is_user_func = _is_user_func_arg(args[0])
                if is_user_func:
                    return f"func() {_slice_type} {{ _r := {_slice_type}{{}}; for _, _v := range {raw_obj} {{ if {fn}(_v) {{ _r = append(_r, _v) }} }}; return _r }}()"
                else:
                    return f"func() {_slice_type} {{ _r := {_slice_type}{{}}; for _, _v := range {raw_obj} {{ if {fn}(_v).(bool) {{ _r = append(_r, _v) }} }}; return _r }}()"
            if _apply_list_combinators and raw_method == "reduce" and len(args) >= 1:
                if inline_lambda:
                    fn = self._lambda_to_go(
                        raw_args_nodes[0],
                        param_types=[_elem_type, _elem_type],
                        return_type=_elem_type,
                    )
                    if len(args) >= 2:
                        init = args[1]
                        return (
                            f"func() {_elem_type} {{ _acc := {_elem_type}({init}); "
                            f"for _, _v := range {raw_obj} {{ _acc = ({fn})(_acc, _v) }}; return _acc }}()"
                        )
                    return (
                        f"func() {_elem_type} {{ _acc := {raw_obj}[0]; "
                        f"for _, _v := range {raw_obj}[1:] {{ _acc = ({fn})(_acc, _v) }}; return _acc }}()"
                    )
                fn = _resolve_func_ref(args[0])
                if len(args) >= 2:
                    init = args[1]
                    return f"func() {_elem_type} {{ _acc := {_elem_type}({init}); for _, _v := range {raw_obj} {{ _acc = {fn}(_acc, _v) }}; return _acc }}()"
                else:
                    return f"func() {_elem_type} {{ _acc := {raw_obj}[0]; for _, _v := range {raw_obj}[1:] {{ _acc = {fn}(_acc, _v) }}; return _acc }}()"
            if _apply_list_combinators and raw_method == "any" and args:
                if inline_lambda:
                    fn = self._lambda_to_go(
                        raw_args_nodes[0], param_types=[_elem_type], return_type="bool"
                    )
                    return (
                        f"func() bool {{ for _, _v := range {raw_obj} {{ if ({fn})(_v) {{ return true }} }}; return false }}()"
                    )
                fn = _resolve_func_ref(args[0])
                is_user_func = _is_user_func_arg(args[0])
                if is_user_func:
                    return f"func() bool {{ for _, _v := range {raw_obj} {{ if {fn}(_v) {{ return true }} }}; return false }}()"
                else:
                    return f"func() bool {{ for _, _v := range {raw_obj} {{ if {fn}(_v).(bool) {{ return true }} }}; return false }}()"
            if _apply_list_combinators and raw_method == "all" and args:
                if inline_lambda:
                    fn = self._lambda_to_go(
                        raw_args_nodes[0], param_types=[_elem_type], return_type="bool"
                    )
                    return (
                        f"func() bool {{ for _, _v := range {raw_obj} {{ if !({fn})(_v) {{ return false }} }}; return true }}()"
                    )
                fn = _resolve_func_ref(args[0])
                is_user_func = _is_user_func_arg(args[0])
                if is_user_func:
                    return f"func() bool {{ for _, _v := range {raw_obj} {{ if !{fn}(_v) {{ return false }} }}; return true }}()"
                else:
                    return f"func() bool {{ for _, _v := range {raw_obj} {{ if !{fn}(_v).(bool) {{ return false }} }}; return true }}()"
            if _apply_list_combinators and raw_method == "foreach" and args:
                if inline_lambda:
                    fn = self._lambda_to_go(
                        raw_args_nodes[0], param_types=[_elem_type]
                    )
                    return (
                        f"func() {{ for _, _v := range {raw_obj} {{ ({fn})(_v) }} }}()"
                    )
                fn = _resolve_func_ref(args[0])
                return f"func() {{ for _, _v := range {raw_obj} {{ {fn}(_v) }} }}()"

            # General method call — try to fill defaults. First look
            # up the receiver as a typed variable; if that fails (e.g.
            # the receiver is itself a chained call like
            # ``db.table(name)``) fall back to walking the AST to
            # infer its class through ``_method_return_types``.
            if raw_obj and raw_method:
                obj_class = self._var_types.get(raw_obj, "")
                if not obj_class and isinstance(func, Tree) and func.data == "getattr":
                    obj_class = self._infer_receiver_class(func.children[0])
                if obj_class:
                    method_key = f"{obj_class}.{raw_method}"
                    args = self._apply_call_kwargs(method_key, args, kwargs)
                    args = self._fill_default_args(method_key, args)
            return f"{func_str}({', '.join(args)})"
        elif "." in func_str:
            return f"{func_str}({', '.join(args)})"

        # ── Python exception constructors ──
        if func_str in PYTHON_EXCEPTIONS:
            self._need_import("fmt")
            if args:
                return f'fmt.Sprintf("{func_str}: %v", {", ".join(args)})'
            return f'"{func_str}"'

        # ── go! block marker ──
        if func_str == "__go_block__" and args:
            block_id = args[0].strip('"').strip("'")
            if block_id in self.go_blocks:
                raw = self.go_blocks[block_id]
                # Resolve ``LAMMERGEIER.<userFunc/Class[.staticMethod]>``
                # references to the Go-mangled symbol. Compiler-emitted
                # aliases were already rewritten before parsing; this
                # pass handles user-defined names that need the AST
                # to resolve. Done before the return-rewrite so the
                # zero-value rewriter sees the final shape.
                raw = self._resolve_user_lammergeier(raw)
                # Rewrite bare ``return`` statements inside this block
                # to include the zero-value of the surrounding Lam
                # function's declared return type. This lets Lam
                # authors write idiomatic Go control-flow inside
                # ``go!`` without having to know Go's
                # ``not enough return values`` rule.
                raw = self._rewrite_go_block_returns(raw)
                # When the surrounding scope is a method body, rewrite
                # any literal ``self.<Field>`` reference inside the
                # ``go!`` block to use the configured Go receiver
                # name (typically ``s``). Lam authors otherwise have
                # to remember to write ``s.X`` manually inside go!
                # blocks even though the surrounding Lam code uses
                # ``self.X`` — this aligns the two. Field names are
                # also title-cased to match Go's exported convention
                # used by the struct emitter.
                if self._self_replacement:
                    import re as _re_self
                    repl = self._self_replacement
                    def _self_to_recv(m):
                        attr = m.group(1)
                        return f"{repl}.{self._go_public_name(attr)}"
                    raw = _re_self.sub(r'\bself\.(\w+)', _self_to_recv, raw)
                lines_raw = raw.strip().split("\n")
                in_import_group = False
                for line in lines_raw:
                    stripped = line.strip()
                    if stripped == "import (":
                        in_import_group = True
                        continue
                    if in_import_group:
                        if stripped == ")":
                            in_import_group = False
                            continue
                        if '"' in stripped and not stripped.startswith('"'):
                            self.user_go_imports.add(stripped)
                        else:
                            imp = stripped.strip('"').strip()
                            if imp:
                                self.user_go_imports.add(imp)
                        continue
                    if stripped.startswith("import "):
                        imp = stripped.replace("import ", "").strip().strip('"')
                        self.user_go_imports.add(imp)
                    else:
                        self._emit(stripped)
                return ""
            return f"/* go block {block_id} not found */"

        # ── Inline go! expression ──
        if func_str == "__go_inline__" and args:
            block_id = args[0].strip().strip('"').strip("'")
            if block_id in self.go_blocks:
                inline_raw = self.go_blocks[block_id].strip()
                # Same user-name resolution as the statement form so
                # ``go! { LAMMERGEIER.foo() }`` and inline
                # ``go! LAMMERGEIER.foo()`` both work.
                return self._resolve_user_lammergeier(inline_raw)
            return f"/* go inline {block_id} not found */"

        # ── User-defined function ──
        if func_str in self._nested_generic_functions:
            return self._nested_generic_call_to_go(func_str, args)

        if func_str in self._user_functions:
            return self._user_function_call_to_go(func_str, args, kwargs, args_node)

        # ── Local variable (lambda, etc.) ──
        if func_str in self.declared_vars:
            return f"{func_str}({', '.join(args)})"

        # ── Constructor ──
        if func_str in self._class_names:
            go_cls = self._go_public_name(func_str)
            init_key = f"{func_str}.init"
            args = self._apply_call_kwargs(init_key, args, kwargs)
            args = self._fill_default_args(init_key, args)
            return f"New{go_cls}({', '.join(args)})"
        if func_str and func_str[0].isupper() and "." not in func_str:
            return f"New{func_str}({', '.join(args)})"

        # ── Default ──
        go_name = self._go_public_name(func_str)
        return f"{go_name}({', '.join(args)})"

    # ─── f-string ──────────────────────────────────────────────

    def _lower_fstring_expr_via_ast(self, expr: str) -> "Optional[str]":
        """Parse a single f-string interpolation slot and lower it
        through the normal expression pipeline.

        Wraps the slot in a tiny synthetic statement (``_x = (<expr>);``)
        so the existing parser can handle it without needing a second
        start rule. The wrapper unwraps to find the RHS and returns
        ``self._expr_to_go(rhs)``. Returns ``None`` on any parse
        failure so the caller can fall back to the legacy regex path
        (and on rare expressions the regex is still correct, e.g.
        bare identifiers that were already valid Go).
        """
        # Cache parses to avoid re-running Lark for repeated slots
        # (a typical log line has the same identifier multiple times).
        cache = self.__dict__.setdefault("_fstring_expr_cache", {})
        if expr in cache:
            cached = cache[expr]
            if cached is None:
                return None
            return self._expr_to_go(cached)
        try:
            from compiler.lammergeier import create_parser as _cp
        except Exception:
            return None
        try:
            parser = _cp()
            wrapped = f"_lamFstrTmp = ({expr});\n"
            tree = parser.parse(wrapped)
        except Exception:
            cache[expr] = None
            return None
        # Find the assign tree's RHS — the parser produces a tiny
        # ``file_input > expr_stmt > assign_stmt > assign`` chain, but
        # the exact shape depends on whether the RHS was a tuple,
        # ternary, etc. Walk for the first ``assign`` and grab the
        # last child (which is the RHS by convention).
        rhs = None
        for sub in tree.iter_subtrees():
            if sub.data == "assign":
                if len(sub.children) >= 2:
                    rhs = sub.children[-1]
                    break
        if rhs is None:
            cache[expr] = None
            return None
        # The RHS came in as ``( ... )`` so the outer node is a
        # ``test`` wrapper around the actual expression. Unwrap when
        # the parens collapse to a single child.
        from lark import Tree as _Tree
        while isinstance(rhs, _Tree) and rhs.data == "test" and len(rhs.children) == 1:
            rhs = rhs.children[0]
        cache[expr] = rhs
        try:
            return self._expr_to_go(rhs)
        except Exception:
            cache[expr] = None
            return None

    def _transform_fstring_expr(self, expr: str) -> str:
        expr = expr.strip()
        if not expr:
            return expr

        # ── Full-AST path ─────────────────────────────────────────
        # Try parsing the interpolation slot as a Lam expression and
        # lowering it through the regular ``_expr_to_go`` pipeline.
        # This is the *only* way to handle composite expressions like
        # ``users[0].profile?.email ?? "none"`` correctly — the regex
        # path below can only see the outermost shape and silently
        # drops the rest. Parse failures fall through to the legacy
        # regex path so simple expressions never regress.
        ast_lowered = self._lower_fstring_expr_via_ast(expr)
        if ast_lowered is not None:
            return ast_lowered

        if self._self_replacement:
            def _replace_self_attr(m):
                attr = m.group(1)
                return f"{self._self_replacement}.{self._go_public_name(attr)}"
            expr = re.sub(r'\bself\.(\w+)', _replace_self_attr, expr)

        sm_match = re.match(r'^(.+?)\.(\w+)\((.*)\)$', expr)
        if sm_match:
            obj_part = sm_match.group(1)
            method_name = sm_match.group(2)
            call_args = sm_match.group(3).strip()

            if method_name == "join":
                self._need_import("strings")
                obj_go = self._transform_fstring_expr(obj_part)
                return f"strings.Join({call_args}, {obj_go})"
            else:
                obj_go = self._transform_fstring_expr(obj_part)
                # Recursively rewrite the call args so nested static
                # calls (``Uuid.isValid(Uuid.v7())``) are translated
                # all the way down rather than being left in
                # dotted-method form for the outer transform to fix.
                inner_args_go = (
                    self._transform_fstring_expr(call_args)
                    if call_args else ""
                )
                if obj_part in self._class_names and obj_part in self._static_methods:
                    if method_name in self._static_methods[obj_part]:
                        go_cls = self._go_public_name(obj_part)
                        go_name = f"{go_cls}_{method_name}"
                        return f"{go_name}({inner_args_go})"
                go_method = self._go_public_name(method_name)
                return f"{obj_go}.{go_method}({inner_args_go})"

        dot_match = re.match(r'^(\w+)\.(\w+)$', expr)
        if dot_match:
            obj_name = dot_match.group(1)
            attr_name = dot_match.group(2)
            if obj_name in LOWER_PKGS:
                return f"{obj_name}.{attr_name}"
            if attr_name in self._static_vars.get(obj_name, {}):
                return self._static_var_go_name(obj_name, attr_name)
            return f"{obj_name}.{self._go_public_name(attr_name)}"

        bare_call_match = re.match(r'^(\w+)\((.*)\)$', expr, re.DOTALL)
        if bare_call_match:
            func_name = bare_call_match.group(1)
            call_args = bare_call_match.group(2)
            if func_name in GO_BUILTINS:
                return expr
            go_name = self._go_public_name(func_name)
            if func_name in self._func_defaults:
                arg_list = [a.strip() for a in call_args.split(",") if a.strip()] if call_args.strip() else []
                arg_list = self._fill_default_args(func_name, arg_list)
                call_args = ", ".join(arg_list)
            if len(self._overloaded_functions.get(func_name, set())) > 1:
                arg_count = len([a.strip() for a in call_args.split(",") if a.strip()]) if call_args.strip() else 0
                go_name = f"{go_name}_{arg_count}"
            return f"{go_name}({call_args})"

        return expr

    def _fstring_to_go(self, node: Tree) -> str:
        self._need_import("fmt")
        raw = str(node.children[0])
        # Triple-quoted ``f"""..."""`` must be tested *before* the
        # single-quoted forms — every triple-quoted literal also
        # starts with ``f"``/``f'`` so the shorter prefix would win
        # and leave two quote characters dangling on each end, which
        # then leak into the emitted Go format string.
        if raw.startswith('f"""') or raw.startswith("f'''"):
            raw = raw[4:-3]
        elif raw.startswith("f'") or raw.startswith('f"'):
            raw = raw[2:-1]

        fmt_str = ""
        go_args = []
        i = 0
        while i < len(raw):
            if raw[i] == '{':
                if i + 1 < len(raw) and raw[i+1] == '{':
                    fmt_str += "{"
                    i += 2
                    continue
                depth = 0
                j = i
                while j < len(raw):
                    if raw[j] == '{':
                        depth += 1
                    elif raw[j] == '}':
                        depth -= 1
                        if depth == 0:
                            break
                    j += 1
                expr_str = raw[i+1:j]
                if ':' in expr_str:
                    last_colon = expr_str.rfind(':')
                    spec_part = expr_str[last_colon+1:]
                    if spec_part and (spec_part[-1] in 'fdseEgGoxXbcn%' or spec_part.startswith('.')):
                        expr_part = expr_str[:last_colon]
                        spec = spec_part
                        fmt_str += f"%{spec}"
                        go_args.append(self._transform_fstring_expr(expr_part))
                    else:
                        fmt_str += "%v"
                        go_args.append(self._transform_fstring_expr(expr_str))
                else:
                    fmt_str += "%v"
                    go_args.append(self._transform_fstring_expr(expr_str))
                i = j + 1
            elif raw[i] == '}':
                if i + 1 < len(raw) and raw[i+1] == '}':
                    fmt_str += "}"
                    i += 2
                    continue
                fmt_str += raw[i]
                i += 1
            else:
                if raw[i] == '%':
                    fmt_str += "%%"
                else:
                    fmt_str += raw[i]
                i += 1

        if go_args:
            args_str = ", ".join(go_args)
            return f'fmt.Sprintf("{fmt_str}", {args_str})'
        else:
            return f'"{fmt_str}"'

    # ─── Lambda ────────────────────────────────────────────────

    def _lambda_to_go(self, node: Tree, param_types=None, return_type=None) -> str:
        """Transpile a lambda literal to a Go function value.

        ``param_types`` — optional list of Go types used to fill in any
        params that lack an annotation. Supplied by callers that know
        the lambda's context (e.g. ``.map(lambda x: x * 2)`` on a
        ``list[int]`` receiver).

        ``return_type`` — optional Go return type. When absent we use
        ``interface{}`` unless the body and param types let us infer a
        concrete type.

        The grammar admits four shapes — bare-untyped (``lambda x: x``),
        paren-typed (``lambda (x: int, y: float): x + y``), explicit
        return-type (``lambda (x) -> int: x``), and brace-delimited
        block body (``lambda (x: int) { return x * 2 }``). All four
        funnel through here.
        """
        params_node = None
        return_anno = None
        body_node = None
        for child in node.children:
            if isinstance(child, Tree):
                if child.data in ("paren_lambda_params", "inline_lambda_params"):
                    params_node = child
                elif child.data == "lambda_return_anno":
                    return_anno = child
                else:
                    # Anything else is the body. ``suite`` for block
                    # bodies; any expression node otherwise.
                    body_node = child
            elif child is not None:
                body_node = child

        # Collect (name, declared_go_type_or_None) pairs so we can
        # backfill from hints without losing user-supplied annotations.
        raw_params = self._collect_lambda_params(params_node)

        hints = list(param_types) if param_types else []
        params = []
        for i, (name, declared) in enumerate(raw_params):
            if declared:
                go_type = declared
            elif i < len(hints) and hints[i]:
                go_type = hints[i]
            else:
                go_type = "interface{}"
            params.append(f"{name} {go_type}")

        # An explicit ``-> Type`` annotation on the lambda always wins
        # over the contextual hint and inference.
        explicit_return = None
        if return_anno is not None and return_anno.children:
            explicit_return = self._type_expr_to_go(return_anno.children[0])

        # Teach the body visitor about the lambda's parameter types so
        # uses of those names compile to the right Go type (no more
        # `x.(int) * 2` shims for pure arithmetic).
        saved_vars = dict(self._var_go_types)
        saved_declared = set(self.declared_vars)
        for (name, _), p in zip(raw_params, params):
            go_ty = p.split(" ", 1)[1]
            self._var_go_types[name] = go_ty
            self.declared_vars.add(name)

        is_block_body = isinstance(body_node, Tree) and body_node.data == "suite"

        try:
            if is_block_body:
                body_src = self._lambda_block_body_to_go(body_node)
            else:
                body_src = self._expr_to_go(body_node)
        finally:
            self._var_go_types = saved_vars
            self.declared_vars = saved_declared

        # The default return type stays ``interface{}`` for
        # compatibility: existing callers rely on the lambda result
        # being type-asserted to a concrete Go type (e.g. ``.map`` does
        # ``fn(_v).(int)``). Return-type inference only kicks in when
        # an explicit hint has been provided *or* when the caller has
        # also supplied ``param_types``, which signals an inference-
        # friendly context like ``.map``/``.filter``.
        if explicit_return is not None:
            resolved_return = explicit_return
        elif return_type is not None:
            resolved_return = return_type
        elif is_block_body:
            # Block bodies always need an explicit return type from the
            # caller or the user. We default to interface{} so callers
            # can still receive a value via type assertion.
            resolved_return = "interface{}"
        elif param_types:
            resolved_return = self._infer_lambda_return_type(body_node, params)
        else:
            resolved_return = "interface{}"

        params_str = ", ".join(params)
        if is_block_body:
            return f"func({params_str}) {resolved_return} {{\n{body_src}\n}}"
        return f"func({params_str}) {resolved_return} {{ return {body_src} }}"

    def _collect_lambda_params(self, params_node):
        """Extract ``(name, go_type_or_None)`` pairs from a lambda's
        parameter node. Handles both the paren-wrapped (typed) form
        and the bare comma-list (untyped) form."""
        out = []
        if params_node is None:
            return out
        if not isinstance(params_node, Tree):
            return out
        for child in params_node.children:
            if child is None:
                continue
            if isinstance(child, Tree):
                if child.data == "typed_lambda_param":
                    name = self._get_name(child.children[0])
                    declared = None
                    if len(child.children) > 1:
                        declared = self._type_expr_to_go(child.children[1])
                    out.append((name, declared))
                elif child.data == "inline_lambda_param":
                    name = self._get_name(child.children[0])
                    out.append((name, None))
                elif child.data == "name":
                    out.append((self._get_name(child), None))
            elif isinstance(child, Token):
                # Bare ``NAME`` token, possible when the inline-param
                # rule's single ``name`` child collapsed under ``?``.
                out.append((str(child), None))
        return out

    def _lambda_block_body_to_go(self, suite_node) -> str:
        """Render a multi-line lambda body (a ``suite`` of statements)
        as Go source. Reuses the statement visitor so every Lam
        construct that works in a function body — ``if``/``for``/
        ``return``/...— works here too."""
        # Snapshot + isolate the line buffer so we can capture only
        # the suite's emitted lines. The buffer-swap pattern is the
        # same one used by inline ``go!`` blocks.
        saved_lines = self.output_lines
        saved_indent = self.indent
        self.output_lines = []
        self.indent = 1
        try:
            self._visit_suite(suite_node)
            body = "\n".join(self.output_lines)
        finally:
            self.output_lines = saved_lines
            self.indent = saved_indent
        return body

    # Heuristic inference for a lambda's return type when the caller
    # can't supply one. We walk the body for simple patterns: comparisons
    # return bool, arithmetic over homogenous numeric params keeps their
    # type, string concatenation yields string.
    def _infer_lambda_return_type(self, body_node, params) -> str:
        if not isinstance(body_node, Tree):
            return "interface{}"
        param_types = {}
        for p in params:
            name, ty = p.split(" ", 1)
            param_types[name] = ty
        # Comparison / boolean-producing nodes → bool.
        if body_node.data in (
            "comparison", "and_test", "or_test", "not_test",
            "const_true", "const_false",
        ):
            return "bool"
        # Arithmetic: if every ``var`` leaf resolves to the same numeric
        # type we return that type.
        if body_node.data in ("arith_expr", "term", "factor"):
            numeric = {"int", "int8", "int16", "int32", "int64",
                       "uint", "uint8", "uint16", "uint32", "uint64",
                       "float32", "float64"}
            seen = set()
            def _walk(n):
                if isinstance(n, Tree):
                    if n.data == "var":
                        name = self._get_name(n.children[0])
                        t = param_types.get(name)
                        if t and t in numeric:
                            seen.add(t)
                    elif n.data == "number":
                        seen.add("int")
                    elif n.data == "float_number":
                        seen.add("float64")
                    for c in n.children:
                        _walk(c)
            _walk(body_node)
            if len(seen) == 1:
                return next(iter(seen))
            if seen and seen.issubset({"int", "float64"}):
                return "float64"
        return "interface{}"

    def _is_lambda_node(self, node) -> bool:
        return isinstance(node, Tree) and node.data == "lambdef"

    def _infer_receiver_class(self, obj_node) -> str:
        """Return the Lam class name of the value flowing into a
        method call's receiver expression, or ``""`` if it can't be
        determined.

        Cases handled:
          * ``var`` — direct lookup in ``_var_types``.
          * ``funccall(getattr(<inner>, method))`` — recurse on
            ``inner`` to learn its class, then return
            ``_method_return_types[InnerClass.method]``.
          * ``funccall(var(Class))`` — constructor or static call;
            for constructors return ``Class``, for static methods
            consult ``_method_return_types[Class.method]``.

        This makes fluent builders work: ``db.table(name).where(...)``
        resolves ``where`` against ``QueryBuilder`` (the declared
        return type of ``Db.table``) without needing the
        intermediate value to be assigned to a typed variable.
        """
        if not isinstance(obj_node, Tree):
            return ""
        if obj_node.data == "var":
            name = self._get_name(obj_node.children[0])
            return self._var_types.get(name, "")
        if obj_node.data == "funccall":
            inner_func = obj_node.children[0]
            if isinstance(inner_func, Tree):
                if inner_func.data == "getattr":
                    inner_obj = inner_func.children[0]
                    inner_method = self._get_name(inner_func.children[1])
                    # Static call ``Class.method(...)``?
                    if isinstance(inner_obj, Tree) and inner_obj.data == "var":
                        receiver_name = self._get_name(inner_obj.children[0])
                        if receiver_name in self._class_names:
                            return self._method_return_types.get(
                                f"{receiver_name}.{inner_method}", ""
                            )
                    # Instance call — recurse for the inner receiver's
                    # class, then look up the method's return type on it.
                    inner_class = self._infer_receiver_class(inner_obj)
                    if inner_class:
                        return self._method_return_types.get(
                            f"{inner_class}.{inner_method}", ""
                        )
                elif inner_func.data == "var":
                    # Bare ``Class()`` constructor.
                    receiver_name = self._get_name(inner_func.children[0])
                    if receiver_name in self._class_names:
                        return receiver_name
        return ""

    def _infer_receiver_elem_type(self, obj_node) -> str:
        """Walk a chained method-call receiver AST to figure out the
        element type of the list flowing into the current method call.

        Used when ``_var_go_types`` doesn't know about the immediate
        receiver (e.g. because it's a chained fluent call that
        returns a fresh slice value)."""
        if not isinstance(obj_node, Tree):
            return ""
        if obj_node.data == "var":
            name = self._get_name(obj_node.children[0])
            go_ty = self._var_go_types.get(name, "")
            if go_ty.startswith("[]"):
                return go_ty[2:]
            return ""
        if obj_node.data == "list":
            # Bare list literal — no type info, caller will default.
            return ""
        if obj_node.data == "funccall":
            inner_func = obj_node.children[0]
            if isinstance(inner_func, Tree) and inner_func.data == "getattr":
                inner_obj = inner_func.children[0]
                inner_method = self._get_name(inner_func.children[1])
                inner_elem = self._infer_receiver_elem_type(inner_obj)
                if inner_method in ("filter", "map", "sort"):
                    # These preserve the element type (for map that is
                    # only approximate, but matches the common case).
                    return inner_elem
        return ""

    # ─── Python-style slicing ──────────────────────────────────

    @staticmethod
    def _slice_bound_is_native(node) -> bool:
        """Return True when ``node`` (a slice start/end expression)
        is safe to drop straight into Go's native ``[a:b]`` form.

        The cheap path is invalid when the expression syntactically
        contains a unary-minus (i.e. a negative index in Python's
        sense). Anything else — literal ints, identifiers,
        ``len(x) - 1``, function calls — is a non-negative computation
        from Go's POV, so it lowers cleanly. We walk the subtree
        looking for a ``factor`` node whose first child is the ``-``
        token; that's the AST shape of ``-x``.
        """
        if node is None:
            return True
        def _has_neg(n) -> bool:
            if not isinstance(n, Tree):
                return False
            if n.data == "factor" and n.children:
                first = n.children[0]
                if isinstance(first, Token) and str(first) == "-":
                    return True
            for c in n.children:
                if _has_neg(c):
                    return True
            return False
        return not _has_neg(node)

    def _slice_to_go(self, obj_go: str, slice_node: Tree, obj_node) -> str:
        """Lower a ``getitem(obj, slice(...))`` to Go.

        Cheap path: when start / stop are non-negative integer literals
        (or absent) and there is no step, emit the native ``obj[a:b]``
        form so Go can keep the slice header trick.

        Full Python semantics path: emit a runtime IIFE that resolves
        negative indices against ``len(obj)`` and supports a stride
        (including a negative one for reversed iteration). The IIFE
        uses ``reflect`` so it handles slices, arrays, and strings
        uniformly. Strings round-trip back to a ``string`` rather
        than a ``[]interface{}`` — slicing a string in Lam should
        feel like Python's slicing, not Go's.
        """
        # Parse the slice node into start / end / step subtrees.
        start_node = None
        end_node = None
        step_node = None
        non_op = [c for c in slice_node.children
                  if not (isinstance(c, Tree) and c.data == "sliceop")]
        if len(non_op) >= 1:
            start_node = non_op[0]
        if len(non_op) >= 2:
            end_node = non_op[1]
        for c in slice_node.children:
            if isinstance(c, Tree) and c.data == "sliceop":
                for sc in c.children:
                    if sc is not None:
                        step_node = sc

        cheap = (
            step_node is None
            and self._slice_bound_is_native(start_node)
            and self._slice_bound_is_native(end_node)
        )
        if cheap:
            start_go = self._expr_to_go(start_node) if start_node is not None else ""
            end_go = self._expr_to_go(end_node) if end_node is not None else ""
            return f"{obj_go}[{start_go}:{end_go}]"

        self._need_import("reflect")
        self._need_import("strings")
        # The IIFE keeps Python's slice semantics: negative indices
        # resolve against the length, a missing ``end`` defaults to
        # ``len(obj)`` for a forward step or ``-1`` for a reverse step,
        # and a non-1 step (positive or negative) drives the iteration.
        # The bound expressions are assumed to be ``int``-typed; if the
        # user passes an ``any`` value, they should ``int(x)`` it.
        start_go = self._expr_to_go(start_node) if start_node is not None else "0"
        end_go = self._expr_to_go(end_node) if end_node is not None else "0"
        step_go = self._expr_to_go(step_node) if step_node is not None else "1"
        start_provided = "true" if start_node is not None else "false"
        end_provided = "true" if end_node is not None else "false"

        lines = [
            "func() interface{} {",
            f"\t_o := interface{{}}({obj_go})",
            "\t_v := reflect.ValueOf(_o)",
            "\t_isStr := _v.Kind() == reflect.String",
            "\t_n := 0",
            "\tif _isStr { _n = len(_o.(string)) } else if _v.IsValid() && (_v.Kind() == reflect.Slice || _v.Kind() == reflect.Array) { _n = _v.Len() }",
            f"\t_step := int({step_go})",
            "\tif _step == 0 { _step = 1 }",
            f"\t_startProvided := {start_provided}",
            f"\t_start := int({start_go})",
            f"\t_endProvided := {end_provided}",
            f"\t_end := int({end_go})",
            "\tif _startProvided && _start < 0 { _start += _n }",
            "\tif _endProvided && _end < 0 { _end += _n }",
            "\tif _step > 0 {",
            "\t\tif !_startProvided { _start = 0 }",
            "\t\tif _start < 0 { _start = 0 }",
            "\t\tif _start > _n { _start = _n }",
            "\t\tif !_endProvided { _end = _n }",
            "\t\tif _end < 0 { _end = 0 }",
            "\t\tif _end > _n { _end = _n }",
            "\t} else {",
            "\t\tif !_startProvided { _start = _n - 1 }",
            "\t\tif _start >= _n { _start = _n - 1 }",
            "\t\tif _start < -1 { _start = -1 }",
            "\t\tif !_endProvided { _end = -1 }",
            "\t\tif _end >= _n { _end = _n - 1 }",
            "\t\tif _end < -1 { _end = -1 }",
            "\t}",
            "\tif _isStr {",
            "\t\t_s := _o.(string)",
            "\t\tvar _sb strings.Builder",
            "\t\tif _step > 0 {",
            "\t\t\tfor _i := _start; _i < _end; _i += _step { _sb.WriteByte(_s[_i]) }",
            "\t\t} else {",
            "\t\t\tfor _i := _start; _i > _end; _i += _step { _sb.WriteByte(_s[_i]) }",
            "\t\t}",
            "\t\treturn _sb.String()",
            "\t}",
            "\t_r := []interface{}{}",
            "\tif _step > 0 {",
            "\t\tfor _i := _start; _i < _end; _i += _step { _r = append(_r, _v.Index(_i).Interface()) }",
            "\t} else {",
            "\t\tfor _i := _start; _i > _end; _i += _step { _r = append(_r, _v.Index(_i).Interface()) }",
            "\t}",
            "\treturn _r",
            "}()",
        ]
        return self._pin_multiline_to(slice_node, "\n".join(lines))

    # ─── List comprehension ────────────────────────────────────

    def _list_comp_to_go(self, node: Tree, elem_type: str = None) -> str:
        self._need_import("fmt")
        comp = node.children[0]
        slice_type = elem_type if elem_type else "[]interface{}"
        if not isinstance(comp, Tree) or comp.data != "comprehension":
            return f"{slice_type}{{}}"

        result_expr = comp.children[0]
        comp_fors = comp.children[1]
        comp_if = comp.children[2] if len(comp.children) > 2 else None

        for_clauses = []
        if isinstance(comp_fors, Tree) and comp_fors.data == "comp_fors":
            for child in comp_fors.children:
                if isinstance(child, Tree) and child.data == "comp_for":
                    for_clauses.append(child)

        if not for_clauses:
            return f"{slice_type}{{}}"

        result_str = self._expr_to_go(result_expr)

        lines = [f"func() {slice_type} {{"]
        lines.append(f"\t_result := {slice_type}{{}}")

        depth = 0
        for fc in for_clauses:
            var_expr = fc.children[0]
            iter_expr = fc.children[1]
            var_str = self._expr_to_go(var_expr)
            range_info = self._check_range_call(iter_expr) if isinstance(iter_expr, Tree) and iter_expr.data == "funccall" else None
            tabs = "\t" * (depth + 1)
            if range_info:
                start, end, step = range_info
                lines.append(f"{tabs}for {var_str} := {start}; {var_str} < {end}; {var_str} += {step} {{")
            else:
                iter_str = self._expr_to_go(iter_expr)
                lines.append(f"{tabs}for _, {var_str} := range {iter_str} {{")
            depth += 1

        inner_tabs = "\t" * (depth + 1)
        if comp_if and isinstance(comp_if, Tree):
            cond_str = self._expr_to_go(comp_if)
            lines.append(f"{inner_tabs}if {cond_str} {{")
            lines.append(f"{inner_tabs}\t_result = append(_result, {result_str})")
            lines.append(f"{inner_tabs}}}")
        else:
            lines.append(f"{inner_tabs}_result = append(_result, {result_str})")

        for i in range(depth):
            tabs = "\t" * (depth - i)
            lines.append(f"{tabs}}}")

        lines.append(f"\treturn _result")
        lines.append(f"}}()")

        return self._pin_multiline_to(node, "\n".join(lines))

    # ─── Set comprehension ─────────────────────────────────────

    def _set_comp_to_go(self, node: Tree) -> str:
        # ``{f(x) for x in xs if cond}`` lowers to a `map[interface{}]bool`
        # built incrementally — the same shape ``set`` literals use.
        comp = node.children[0]
        set_type = "map[interface{}]bool"
        if not isinstance(comp, Tree) or comp.data != "comprehension":
            return f"{set_type}{{}}"

        result_expr = comp.children[0]
        comp_fors = comp.children[1]
        comp_if = comp.children[2] if len(comp.children) > 2 else None

        for_clauses = []
        if isinstance(comp_fors, Tree) and comp_fors.data == "comp_fors":
            for child in comp_fors.children:
                if isinstance(child, Tree) and child.data == "comp_for":
                    for_clauses.append(child)

        if not for_clauses:
            return f"{set_type}{{}}"

        result_str = self._expr_to_go(result_expr)

        lines = [f"func() {set_type} {{"]
        lines.append(f"\t_result := {set_type}{{}}")

        depth = 0
        for fc in for_clauses:
            var_expr = fc.children[0]
            iter_expr = fc.children[1]
            var_str = self._expr_to_go(var_expr)
            range_info = self._check_range_call(iter_expr) if isinstance(iter_expr, Tree) and iter_expr.data == "funccall" else None
            tabs = "\t" * (depth + 1)
            if range_info:
                start, end, step = range_info
                lines.append(f"{tabs}for {var_str} := {start}; {var_str} < {end}; {var_str} += {step} {{")
            else:
                iter_str = self._expr_to_go(iter_expr)
                lines.append(f"{tabs}for _, {var_str} := range {iter_str} {{")
            depth += 1

        inner_tabs = "\t" * (depth + 1)
        if comp_if and isinstance(comp_if, Tree):
            cond_str = self._expr_to_go(comp_if)
            lines.append(f"{inner_tabs}if {cond_str} {{")
            lines.append(f"{inner_tabs}\t_result[{result_str}] = true")
            lines.append(f"{inner_tabs}}}")
        else:
            lines.append(f"{inner_tabs}_result[{result_str}] = true")

        for i in range(depth):
            tabs = "\t" * (depth - i)
            lines.append(f"{tabs}}}")

        lines.append(f"\treturn _result")
        lines.append(f"}}()")

        return self._pin_multiline_to(node, "\n".join(lines))

    # ─── Dict comprehension ───────────────────────────────────

    def _dict_comp_to_go(self, node: Tree, map_type: str = None) -> str:
        comp = node.children[0]
        mtype = map_type if map_type else "map[interface{}]interface{}"
        if not isinstance(comp, Tree) or comp.data != "comprehension":
            return f"{mtype}{{}}"

        kv = comp.children[0]
        comp_fors = comp.children[1]
        comp_if = comp.children[2] if len(comp.children) > 2 else None

        if isinstance(kv, Tree) and kv.data == "key_value":
            key_expr = self._expr_to_go(kv.children[0])
            val_expr = self._expr_to_go(kv.children[1])
        else:
            return f"{mtype}{{}}"

        for_clause = comp_fors.children[0]
        var_str = self._expr_to_go(for_clause.children[0])
        iter_expr = for_clause.children[1]

        range_info = self._check_range_call(iter_expr) if isinstance(iter_expr, Tree) and iter_expr.data == "funccall" else None

        if range_info:
            start, end, step = range_info
            for_line = f"for {var_str} := {start}; {var_str} < {end}; {var_str} += {step}"
        else:
            iter_str = self._expr_to_go(iter_expr)
            for_line = f"for _, {var_str} := range {iter_str}"

        if comp_if and isinstance(comp_if, Tree):
            cond_str = self._expr_to_go(comp_if)
            return (f"func() {mtype} {{ "
                    f"_r := {mtype}{{}}; "
                    f"{for_line} {{ "
                    f"if {cond_str} {{ _r[{key_expr}] = {val_expr} }} }}; "
                    f"return _r }}()")

        return (f"func() {mtype} {{ "
                f"_r := {mtype}{{}}; "
                f"{for_line} {{ "
                f"_r[{key_expr}] = {val_expr} }}; "
                f"return _r }}()")

    # ─── Binary ops / operator overloading ─────────────────────

    def _try_operator_overload(self, node: Tree) -> str:
        operands = []
        operators = []
        for child in node.children:
            if isinstance(child, Token):
                operators.append(str(child))
            elif isinstance(child, Tree):
                operands.append(child)

        if not operators or not operands:
            return ""

        cls = self._infer_expr_class(operands[0])
        if not cls:
            return ""

        result = self._expr_to_go(operands[0])
        all_overloaded = True
        for i, op in enumerate(operators):
            py_op = "//" if op == "//" else op
            dunder = OP_TO_DUNDER.get(py_op, "")
            method = self._get_dunder_method(cls, dunder) if dunder else ""
            if method and i + 1 < len(operands):
                right = self._expr_to_go(operands[i + 1])
                result = f"{result}.{method}({right})"
            else:
                all_overloaded = False
                break

        return result if all_overloaded else ""

    def _try_comparison_overload(self, node: Tree) -> str:
        exprs = []
        comp_ops = []
        for child in node.children:
            if isinstance(child, Tree) and child.data == "comp_op":
                ops = [str(t) for t in child.children]
                comp_ops.append(" ".join(ops))
            elif isinstance(child, Tree):
                exprs.append(child)

        if len(exprs) < 2 or not comp_ops:
            return ""

        cls = self._infer_expr_class(exprs[0])
        if not cls:
            return ""

        if len(comp_ops) == 1 and len(exprs) == 2:
            dunder = CMP_TO_DUNDER.get(comp_ops[0], "")
            method = self._get_dunder_method(cls, dunder) if dunder else ""
            if method:
                left = self._expr_to_go(exprs[0])
                right = self._expr_to_go(exprs[1])
                return f"{left}.{method}({right})"

        return ""

    def _binop_to_go(self, node: Tree) -> str:
        parts = []
        for child in node.children:
            if isinstance(child, Token):
                op = str(child)
                parts.append("/" if op == "//" else op)
            elif isinstance(child, Tree):
                s = self._expr_to_go(child)
                if child.data in ("arith_expr", "term") and node.data != child.data:
                    s = f"({s})"
                parts.append(s)
        return " ".join(parts)

    def _comparison_to_go(self, node: Tree) -> str:
        parts = []
        for child in node.children:
            if isinstance(child, Tree) and child.data == "comp_op":
                ops = [str(t) for t in child.children]
                op_str = " ".join(ops)
                if op_str == "not in":
                    parts.append("/* not in */ !=")
                elif op_str == "is not":
                    parts.append("!=")
                elif op_str == "is":
                    parts.append("==")
                elif op_str == "in":
                    parts.append("/* in */ ==")
                else:
                    parts.append(op_str)
            elif isinstance(child, Tree):
                parts.append(self._expr_to_go(child))
        return " ".join(parts)

    def _dict_to_go(self, node: Tree) -> str:
        entries = []
        has_non_string_key = False
        for child in node.children:
            if isinstance(child, Tree) and child.data == "key_value":
                k = self._expr_to_go(child.children[0])
                v = self._expr_to_go(child.children[1])
                if not (k.startswith('"') or k.startswith("'")):
                    has_non_string_key = True
                entries.append(f"{k}: {v}")
        if has_non_string_key:
            return f"map[interface{{}}]interface{{}}{{{', '.join(entries)}}}"
        return f"map[string]interface{{}}{{{', '.join(entries)}}}"

    # ─── isinstance ────────────────────────────────────────────
    #
    # ``isinstance(obj, type)`` compiles to a small Go IIFE containing a
    # two-value type assertion, ``obj == nil`` check, or a ``reflect``
    # kind comparison — whichever matches the supplied type expression.
    # ``isinstance(obj, (T1, T2, ...))`` OR-combines the per-type tests.

    _ISINSTANCE_PRIMITIVES = {
        "int": "int", "int8": "int8", "int16": "int16",
        "int32": "int32", "int64": "int64",
        "uint": "uint", "uint8": "uint8", "uint16": "uint16",
        "uint32": "uint32", "uint64": "uint64",
        "float": "float64", "float32": "float32", "float64": "float64",
        "str": "string", "string": "string",
        "bool": "bool", "byte": "byte", "rune": "rune",
        "bytes": "[]byte",
    }

    def _isinstance_test_go(self, obj_go: str, type_node) -> str:
        """Build the Go boolean expression for `isinstance(obj, type_node)`."""
        # Tuple of types: (int, str, SomeClass)
        if isinstance(type_node, Tree) and type_node.data == "tuple":
            parts = []
            for c in type_node.children:
                if c is None:
                    continue
                parts.append(self._isinstance_test_go(obj_go, c))
            if not parts:
                return "false"
            return "(" + " || ".join(parts) + ")"

        # Parenthesised single type.
        if isinstance(type_node, Tree) and type_node.data == "argvalue":
            inner = type_node.children[0] if type_node.children else None
            if inner is not None:
                return self._isinstance_test_go(obj_go, inner)

        # Parametric type: list[int], dict[str, int] — treat as the
        # outer container kind (Go does not carry parametric runtime
        # type info for generic collections).
        if isinstance(type_node, Tree) and type_node.data == "getitem":
            return self._isinstance_test_go(obj_go, type_node.children[0])

        name = self._isinstance_name(type_node)

        if name is None:
            # Fallback: transpile as a type and use a direct assertion.
            go_ty = self._expr_to_go(type_node)
            return (
                f"func() bool {{ _, ok := interface{{}}({obj_go}).({go_ty}); return ok }}()"
            )

        if name == "None":
            return f"({obj_go} == nil)"
        if name in ("any", "object"):
            return "true"

        prim = self._ISINSTANCE_PRIMITIVES.get(name)
        if prim is not None:
            if prim == "[]byte":
                return (
                    "func() bool { _, ok := interface{}("
                    + obj_go + ").([]byte); return ok }()"
                )
            return (
                f"func() bool {{ _, ok := interface{{}}({obj_go}).({prim}); "
                f"return ok }}()"
            )

        if name == "list":
            self._need_import("reflect")
            return (
                "func() bool { v := reflect.ValueOf(" + obj_go + "); "
                "return v.IsValid() && (v.Kind() == reflect.Slice || v.Kind() == reflect.Array) }()"
            )
        if name in ("dict", "set"):
            self._need_import("reflect")
            return (
                "func() bool { v := reflect.ValueOf(" + obj_go + "); "
                "return v.IsValid() && v.Kind() == reflect.Map }()"
            )
        if name == "tuple":
            self._need_import("reflect")
            return (
                "func() bool { v := reflect.ValueOf(" + obj_go + "); "
                "return v.IsValid() && (v.Kind() == reflect.Slice || v.Kind() == reflect.Array) }()"
            )
        if name == "error":
            return (
                f"func() bool {{ _, ok := interface{{}}({obj_go}).(error); return ok }}()"
            )
        if name in PYTHON_EXCEPTIONS:
            # Exceptions are represented as panics; runtime type is a
            # string, so we can't reliably identify them from a value.
            return "false"
        if name in self._interfaces:
            go_name = self._go_public_name(name)
            return (
                f"func() bool {{ _, ok := interface{{}}({obj_go}).({go_name}); return ok }}()"
            )

        # User class — represented as a pointer to a struct in Go.
        go_name = self._go_public_name(name)
        return (
            f"func() bool {{ _, ok := interface{{}}({obj_go}).(*{go_name}); return ok }}()"
        )

    def _isinstance_name(self, node) -> Optional[str]:
        """Best-effort: return the dotted type name from an AST node."""
        if node is None:
            return None
        if isinstance(node, Token):
            return str(node)
        if isinstance(node, Tree):
            if node.data == "const_none":
                return "None"
            if node.data in ("var", "type_name"):
                return self._dotted_name_to_str(node.children[0])
            if node.data == "name":
                return self._get_name(node)
            if node.data == "dotted_name":
                return self._dotted_name_to_str(node)
        return None
# benign comment for cache-invalidation test
