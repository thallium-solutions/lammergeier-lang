#!/usr/bin/env python3
"""Helper / utility methods mixin for the transpiler."""

from __future__ import annotations
import re as _re
from lark import Tree, Token
from typing import Dict, List, Optional, Tuple
from compiler.constants import TYPE_MAP


class HelpersMixin:
    """Naming, type conversion, and utility helpers."""

    # ─── Static helpers ────────────────────────────────────────

    @staticmethod
    def _parse_funcdef(node: Tree):
        """Parse a funcdef node and return its components.
        Returns: (is_private, is_static, is_async, name_node, params_node,
                  return_type_node, suite_node, type_params_node)

        ``type_params_node`` is the optional ``[T, U: comparable]`` tree
        for generic functions, or ``None`` when the function isn't
        parameterised.
        """
        is_private = False
        is_static = False
        is_async = False
        name_node = None
        params_node = None
        return_type_node = None
        suite_node = None
        type_params_node = None

        for child in node.children:
            if child is None:
                continue
            if isinstance(child, Token):
                if str(child) == "private":
                    is_private = True
                elif str(child) == "static":
                    is_static = True
                elif str(child) == "async":
                    is_async = True
                continue
            if isinstance(child, Tree):
                if child.data == "name":
                    name_node = child
                elif child.data == "type_params":
                    type_params_node = child
                elif child.data == "typed_parameters":
                    params_node = child
                elif child.data in ("single_return_type", "multi_return_type"):
                    return_type_node = child
                elif child.data == "suite":
                    suite_node = child

        return (is_private, is_static, is_async, name_node, params_node,
                return_type_node, suite_node, type_params_node)

    # Go constraints we accept out of the box. Anything else is passed
    # through verbatim so users can reference imported interfaces by
    # dotted name (e.g. ``T: fmt.Stringer``).
    _BUILTIN_CONSTRAINTS = {
        "any": "any",
        "comparable": "comparable",
        "int": "int",
        "int64": "int64",
        "float": "float64",
        "float64": "float64",
        "str": "string",
        "string": "string",
        "bool": "bool",
        "number": "float64 | int",
        "ordered": "~int | ~int64 | ~float64 | ~string",
    }

    def _type_params_to_go(self, node):
        """Lower a ``type_params`` tree to its Go clause and record names.

        Returns a tuple ``(clause, names, pairs)`` where:

        - ``clause`` is the Go source including brackets
          (e.g. ``"[T any, U comparable]"``) or ``""`` when the node is
          ``None``.
        - ``names`` is a list of the bare type-parameter identifiers.
        - ``pairs`` is a list of ``(name, constraint)`` tuples, mostly
          useful for callers that need the constraint text.
        """
        if node is None or not isinstance(node, Tree):
            return "", [], []
        pairs = []
        for child in node.children:
            if not isinstance(child, Tree) or child.data != "type_param":
                continue
            tp_name = self._get_name(child.children[0])
            constraint = "any"
            if len(child.children) > 1:
                cnode = child.children[1]
                if isinstance(cnode, Tree) and cnode.data == "type_constraint":
                    cname = self._get_name(cnode.children[0])
                    constraint = self._BUILTIN_CONSTRAINTS.get(cname, cname)
            pairs.append((tp_name, constraint))
        if not pairs:
            return "", [], []
        names = [n for n, _ in pairs]
        clause = "[" + ", ".join(f"{n} {c}" for n, c in pairs) + "]"
        return clause, names, pairs

    def _type_arg_list(self, node) -> list:
        """Lower a subscript used as a generic-type argument list.

        ``Pair[int, str]`` produces a ``subscript_tuple`` node at
        ``getitem.children[1]``, ``Box[int]`` produces a single
        subscript. Both cases need to come back as a list of Go type
        names (``["int", "string"]``), not a joined string, so the
        caller can splice them into ``NewPair[int, string]``.
        """
        if node is None:
            return []
        if isinstance(node, Tree) and node.data == "subscript_tuple":
            return [self._type_expr_to_go(c) for c in node.children if c is not None]
        return [self._type_expr_to_go(node)]

    @staticmethod
    def _merge_generic_clauses(outer: str, inner: str) -> str:
        """Concatenate two ``[T any]``-style clauses, deduplicating names.

        Used when a static method of a generic class also has method-
        level type parameters: the emitted free function has to declare
        the union of both lists.
        """
        if not outer:
            return inner or ""
        if not inner:
            return outer or ""
        outer_body = outer.strip("[]")
        inner_body = inner.strip("[]")
        seen = {p.strip().split()[0] for p in outer_body.split(",") if p.strip()}
        extras = [
            p.strip()
            for p in inner_body.split(",")
            if p.strip() and p.strip().split()[0] not in seen
        ]
        if not extras:
            return outer
        return "[" + outer_body + ", " + ", ".join(extras) + "]"

    @staticmethod
    def _suite_stmts(suite_node):
        """Get statement children from a suite node, flattening simple_stmt."""
        if suite_node is None:
            return []
        if not isinstance(suite_node, Tree):
            return []
        if suite_node.data == "suite":
            stmts = []
            for c in suite_node.children:
                if isinstance(c, Tree):
                    if c.data == "simple_stmt":
                        # Flatten: simple_stmt contains multiple small_stmts
                        for sub in c.children:
                            if isinstance(sub, Tree):
                                stmts.append(sub)
                    else:
                        stmts.append(c)
            return stmts
        return [suite_node] if isinstance(suite_node, Tree) else []

    # ─── Indentation / emit ────────────────────────────────────

    def _indent_str(self) -> str:
        return "\t" * self.indent

    def _emit(self, line: str = ""):
        self.output_lines.append(f"{self._indent_str()}{line}")

    def _emit_raw(self, line: str):
        self.output_lines.append(line)

    def _push_scope(self):
        self.scope_stack.append(set(self.declared_vars))

    def _pop_scope(self):
        if self.scope_stack:
            self.declared_vars = self.scope_stack.pop()

    def _declare_var(self, name: str):
        self.declared_vars.add(name)

    def _need_import(self, pkg: str):
        self.needed_imports.add(pkg)

    # ─── Unused-local silencers ────────────────────────────────

    def _emit_unused_local_silencers(
        self, body_start_line: int, params_at_start: set,
    ) -> None:
        """Emit ``_ = name`` for every function-scope local that the
        body declared but never referenced.

        Lam follows Go's "warn don't error" stance for advisory
        diagnostics: the semantic checker surfaces unused parameters
        / imports as warnings instead of fatal errors. Go itself, by
        contrast, treats *unused locals* as a hard build error
        (``declared and not used``). Without this epilogue, an
        otherwise-correct Lam program with one stray local would
        compile cleanly through Lam but fail at the Go layer with a
        diagnostic that points at generated code rather than the
        user's source.

        We compute the diff between the snapshot of ``declared_vars``
        captured before the body walk and the post-walk set, then
        scan the slice of ``output_lines`` that was emitted during
        the walk to count word-boundary occurrences of each name.
        Anything that appears exactly once (i.e. only in its own
        declaration line) gets a trailing ``_ = name`` so Go
        accepts it.

        Block-scope locals (declared inside an ``if`` / ``for`` /
        ``with`` body) are *not* covered: the transpiler pops their
        scope before this runs, so they no longer live in
        ``declared_vars`` and a function-level silencer would itself
        be an undefined-identifier error. Those cases still surface
        as Go errors today; the user can fix them by deleting the
        local or adding their own ``_ = name``. Adding per-block
        silencers requires emitting a hook at every ``_pop_scope``
        site and is left as a future iteration.
        """
        new_locals = self.declared_vars - params_at_start
        if not new_locals:
            return
        body_lines = self.output_lines[body_start_line:]
        # Strip ``//line`` directives so source paths can't be misread
        # as references (a path like ``/tmp/x.lam`` would otherwise
        # match ``\bx\b`` for a local named ``x``).
        scrubbed = "\n".join(
            ln for ln in body_lines if not ln.lstrip().startswith("//line")
        )
        for name in sorted(new_locals):
            # Compiler-internal temporaries (``__qN``, ``__lamFoo``,
            # ``_chN``…) are bookkeeping, not user code; skip them so
            # we don't churn the output for things the user can't see.
            if not name or name.startswith("_") or name.startswith("__"):
                continue
            pat = _re.compile(rf"\b{_re.escape(name)}\b")
            if len(pat.findall(scrubbed)) <= 1:
                self._emit(f"_ = {name}")

    # ─── Source-line mapping ───────────────────────────────────

    def _emit_line_directive(self, node):
        """Emit a Go //line directive if source_file is set and node has position."""
        if self._source_file and hasattr(node, 'meta') and hasattr(node.meta, 'line') and node.meta.line:
            line = node.meta.line
            if line != self._last_line_directive:
                self._last_line_directive = line
                self._emit_raw(f"//line {self._source_file}:{line}")

    def _node_lam_line(self, node):
        """Return the Lam source line for ``node`` or ``None`` if unknown."""
        if node is None or not hasattr(node, "meta"):
            return None
        meta = node.meta
        if getattr(meta, "line", None):
            return meta.line
        return None

    def _pin_multiline_to(self, node, body: str) -> str:
        """Prepend a ``//line`` pragma to each line of a multi-line Go
        expression so every internal line reports back to the same Lam
        source line.

        Used by IIFE-style lowerings (comprehensions, etc.) whose body
        spans multiple Go lines but corresponds to a single Lam
        expression — without this rewrite, Go's line counter drifts
        forward across the IIFE's internal lines and errors inside the
        IIFE get mis-attributed to later statements. Not needed when
        ``body`` is already a one-liner.
        """
        if not self._source_file:
            return body
        if "\n" not in body:
            return body
        lam_line = self._node_lam_line(node)
        if lam_line is None:
            return body
        pragma = f"//line {self._source_file}:{lam_line}"
        # Splice the pragma after every newline. Lines stay on their
        # own — Go requires //line to be the only content on its line.
        return body.replace("\n", f"\n{pragma}\n")

    # ─── Name utilities ────────────────────────────────────────

    def _get_name(self, node) -> str:
        if isinstance(node, Token):
            return str(node)
        if isinstance(node, Tree):
            if node.data == "name":
                return str(node.children[0])
            if node.data == "dotted_name":
                return self._dotted_name_to_str(node)
            if node.data == "var":
                return self._get_name(node.children[0])
        return ""

    def _dotted_name_to_str(self, node) -> str:
        if not isinstance(node, Tree):
            return str(node)
        parts = []
        for child in node.children:
            if isinstance(child, Tree) and child.data == "name":
                parts.append(str(child.children[0]))
            elif isinstance(child, Token):
                parts.append(str(child))
        return ".".join(parts)

    @staticmethod
    def _go_zero_value(go_type: str) -> str:
        """Return the Go zero-value literal for ``go_type``.

        Used to rewrite bare ``return`` statements inside ``go!`` blocks
        of typed Lam functions so the developer never has to know the
        Go signature constraints. If we don't recognise the type, we
        fall back to ``*new(T)`` which works for any Go type at the
        cost of one heap allocation.
        """
        if not go_type:
            return ""
        t = go_type.strip()
        # Multi-return: ``(int, string)`` → ``0, ""``.
        if t.startswith("(") and t.endswith(")"):
            inner = t[1:-1]
            depth = 0
            parts: list[str] = []
            current = ""
            for ch in inner:
                if ch in "([{":
                    depth += 1
                elif ch in ")]}":
                    depth -= 1
                if ch == "," and depth == 0:
                    parts.append(current.strip())
                    current = ""
                else:
                    current += ch
            if current.strip():
                parts.append(current.strip())
            return ", ".join(HelpersMixin._go_zero_value(p) for p in parts)
        # Pointers, slices, maps, channels, interfaces, funcs.
        if (t.startswith("*") or t.startswith("[]") or t.startswith("[")
                or t.startswith("map[") or t.startswith("chan ")
                or t.startswith("<-chan ") or t.startswith("chan<- ")
                or t.startswith("func") or t == "interface{}"
                or t == "any" or t.startswith("interface{")):
            return "nil"
        # Booleans.
        if t == "bool":
            return "false"
        # Strings.
        if t == "string" or t == "rune" or t == "byte":
            return '""' if t == "string" else "0"
        # Numerics.
        if t in (
            "int", "int8", "int16", "int32", "int64",
            "uint", "uint8", "uint16", "uint32", "uint64",
            "uintptr", "float32", "float64", "complex64", "complex128",
        ):
            return "0"
        # Generic / unknown — fall back to ``*new(T)`` which works for
        # every concrete Go type and lets the user write their custom
        # struct as a return type without us having to enumerate it.
        return f"*new({t})"

    def _rewrite_go_block_returns(self, go_src: str) -> str:
        """Rewrite bare ``return`` statements inside a raw ``go!`` block.

        When the surrounding Lam function declares a non-void return
        type, every Go ``return`` statement that doesn't already supply
        a value is rewritten to ``return <zero-value>``. This means
        Lam authors can write::

            func tryGc(dir: str) -> int {
                go! {
                    if entries, err := os.ReadDir(dir); err == nil {
                        ...
                        return  // walks out of the surrounding func
                    }
                }
                return n
            }

        and have it compile, without having to know that Go would
        otherwise refuse with ``not enough return values``.
        """
        ret_type = getattr(self, "_current_return_type", "") or ""
        if not ret_type:
            return go_src
        zero = self._go_zero_value(ret_type)
        if not zero:
            return go_src
        # Match a line whose only content is ``return`` (optionally
        # followed by a comment). We deliberately don't touch lines
        # that already supply a value — even ``return nil`` is left
        # alone so explicit author intent always wins.
        pattern = _re.compile(
            r'^(\s*)return\s*(//.*)?$',
            _re.MULTILINE,
        )
        return pattern.sub(
            lambda m: f"{m.group(1)}return {zero}{(' ' + m.group(2)) if m.group(2) else ''}",
            go_src,
        )

    def _go_public_name(self, name: str) -> str:
        if not name:
            return name
        if name.startswith("__") and name.endswith("__"):
            clean = name.strip("_")
            return clean[0].upper() + clean[1:] if clean else name
        if name.startswith("_"):
            return name
        return name[0].upper() + name[1:]

    def _go_private_name(self, name: str) -> str:
        """Convert to Go unexported (private) name — lowercase first letter."""
        if not name:
            return name
        return name[0].lower() + name[1:]

    # ─── LAMMERGEIER.<userName> resolver ─────────────────────────
    #
    # Compiler-emitted aliases (``LAMMERGEIER.Result.Ok`` etc.) get
    # rewritten textually before parsing — see
    # ``compiler/preprocessor.py::apply_lammergeier_aliases``.
    # User-side names (functions, classes, static methods) need
    # the AST first because the rewrite depends on the Go name
    # mangling rules: ``foo`` (public) → ``Foo``, ``foo`` (private)
    # → unchanged, ``MyCls.staticMethod`` → ``MyCls_staticMethod``.
    # This helper is invoked on raw go-block content right before
    # the dispatcher emits it into the surrounding function — by
    # then ``_user_functions`` / ``_class_names`` / ``_static_methods`` /
    # ``_static_vars``
    # are fully populated.
    # Capture the optional trailing ``(`` so the substitution can do
    # context-aware rewriting (``Counter(...)`` → ``NewCounter(...)``
    # when the class is being instantiated, plain ``Counter`` when
    # used as a type).
    _LAM_USER_RE = _re.compile(
        r"\bLAMMERGEIER\.([A-Za-z_][A-Za-z_0-9]*(?:\.[A-Za-z_][A-Za-z_0-9]*)?)(\s*\()?"
    )

    def _resolve_user_lammergeier(self, raw: str) -> str:
        """Rewrite ``LAMMERGEIER.<userName>`` references in ``raw`` to
        the Go-side identifier the surrounding code expects.

        Resolution order:

          * ``LAMMERGEIER.<funcName>`` — top-level Lam function. Becomes
            the public ``FuncName`` (or the original lowercase name
            for ``private`` functions).
          * ``LAMMERGEIER.<ClassName>(...)`` — class instantiation.
            Becomes ``NewClassName(...)`` (the auto-generated
            constructor Lam emits for every class).
          * ``LAMMERGEIER.<ClassName>`` (no trailing call) — the class
            *type* itself. Becomes ``ClassName`` so it can be used
            in type assertions / declarations.
          * ``LAMMERGEIER.<ClassName>.<staticMethod>`` — static
            method. Becomes ``ClassName_staticMethod``.
          * ``LAMMERGEIER.<ClassName>.<staticVar>`` — static
            variable. Becomes the package-level variable name Lam emits
            for ``ClassName.staticVar``.

        Anything not in those buckets is left as-is. The deferred
        typo guard in ``compiler/lammergeier.py`` rejects unknown
        references *before* the dispatcher runs, so by the time we
        get here the residual references are guaranteed to resolve
        cleanly. The fall-through is a defensive no-op.
        """
        if "LAMMERGEIER." not in raw:
            return raw

        def _sub(m):
            tail = m.group(1)
            paren = m.group(2) or ""
            parts = tail.split(".")
            if len(parts) == 1:
                head = parts[0]
                if head in self._user_functions:
                    name = (
                        self._go_private_name(head)
                        if head in self._private_functions
                        else self._go_public_name(head)
                    )
                    return name + paren
                if head in self._class_names:
                    go_cls = self._go_public_name(head)
                    # Call form ``LAMMERGEIER.MyClass(args)`` instantiates;
                    # bare form is the type itself.
                    if paren:
                        return f"New{go_cls}{paren}"
                    return go_cls
                return m.group(0)
            if len(parts) == 2:
                cls, method = parts
                if (
                    cls in self._class_names
                    and cls in getattr(self, "_static_methods", {})
                    and method in self._static_methods[cls]
                ):
                    return f"{self._go_public_name(cls)}_{method}{paren}"
                if (
                    cls in self._class_names
                    and cls in getattr(self, "_static_vars", {})
                    and method in self._static_vars[cls]
                ):
                    return f"{self._static_var_go_name(cls, method)}{paren}"
                return m.group(0)
            return m.group(0)

        return self._LAM_USER_RE.sub(_sub, raw)

    def _number_to_go(self, node: Tree) -> str:
        return str(node.children[0]).replace("_", "")

    # ─── Type resolution ──────────────────────────────────────

    def _type_expr_to_go(self, node) -> str:
        if node is None:
            return ""
        if isinstance(node, Token):
            return TYPE_MAP.get(str(node), str(node))
        if not isinstance(node, Tree):
            return str(node)

        d = node.data

        if d == "type_expr":
            return self._type_expr_to_go(node.children[0])

        if d == "type_union":
            for c in node.children:
                if isinstance(c, Tree) and c.data == "type_none":
                    return ""
            types = [self._type_expr_to_go(c) for c in node.children if c is not None]
            types = [t for t in types if t]
            if not types:
                return ""
            if len(types) == 1:
                return types[0]
            return "interface{}"

        if d == "type_name":
            name = self._dotted_name_to_str(node.children[0])
            if name in TYPE_MAP:
                return TYPE_MAP[name]
            # Type-parameter names (e.g. ``T``) stay literal. They're
            # neither pointers nor user classes — Go will resolve them
            # against the surrounding ``[T any]`` clause.
            if name in self._generic_names:
                return name
            go_name = self._go_public_name(name)
            # Interfaces are not pointers
            if name in self._interfaces:
                return go_name
            return "*" + go_name

        if d == "type_none":
            return ""

        if d == "type_generic":
            base = self._dotted_name_to_str(node.children[0])
            type_args = [self._type_expr_to_go(c) for c in node.children[1:] if c is not None]
            if base == "list":
                return f"[]{type_args[0]}" if type_args else "[]interface{}"
            if base == "dict":
                if len(type_args) >= 2:
                    return f"map[{type_args[0]}]{type_args[1]}"
                return "map[string]interface{}"
            if base == "tuple":
                # Tuples compile to ``[]interface{}``. A fixed-size Go
                # array would be more precise but it would diverge
                # from the slice representation used for call-site
                # tuple literals and ``enumerate`` pair values, and
                # the element types aren't preserved at runtime either
                # way. Keeping a single representation simplifies
                # interop across compiler features.
                return "[]interface{}"
            if base == "optional":
                return f"*{type_args[0]}" if type_args else "interface{}"
            if base == "chan":
                return f"chan {type_args[0]}" if type_args else "chan interface{}"
            # Instantiation of a user-defined generic class:
            # ``Box[int]`` -> ``*Box[int]``. Non-generic classes fall
            # through to the ``interface{}`` branch below — treating a
            # stray ``Foo[X]`` annotation on a plain class as a
            # catch-all avoids emitting invalid Go.
            if base in self._generic_classes:
                go_name = self._go_public_name(base)
                return f"*{go_name}[{', '.join(type_args)}]"
            return "interface{}"

        if d == "type_func":
            # AST shape: ``type_func FUNC [type_func_params|None] [type_expr|None]``
            # The grammar keeps the literal ``FUNC`` keyword token in
            # the rule body so the parser stays unambiguous against
            # ``type_name``; we have to skip it here, otherwise the
            # token gets treated as the return-type subtree and Lam
            # ends up emitting ``func() func`` for a void ``func()``.
            params: list = []
            ret = ""
            for c in node.children:
                if c is None:
                    continue
                if isinstance(c, Token):
                    continue
                if isinstance(c, Tree) and c.data == "type_func_params":
                    params = [
                        self._type_expr_to_go(p)
                        for p in c.children
                        if p is not None
                    ]
                else:
                    ret = self._type_expr_to_go(c)
            if ret:
                return f"func({', '.join(params)}) {ret}"
            return f"func({', '.join(params)})"

        parts = [self._type_expr_to_go(c) for c in node.children if c is not None]
        return " ".join(parts)

    def _resolve_return_type(self, node) -> str:
        if node is None:
            return ""
        if isinstance(node, Tree):
            if node.data == "single_return_type":
                return self._type_expr_to_go(node.children[0])
            if node.data == "multi_return_type":
                types = [self._type_expr_to_go(c) for c in node.children]
                return f"({', '.join(types)})"
        return ""

    def _resolve_multi_return_types(self, node) -> List[str]:
        if isinstance(node, Tree) and node.data == "multi_return_type":
            return [self._type_expr_to_go(c) for c in node.children]
        return []

    def _get_raw_type_name(self, type_node) -> str:
        """Extract the raw type name string from a type_expr node (e.g. 'Vec', 'Point').

        Also accepts ``single_return_type`` / ``multi_return_type``
        wrappers so callers can pass a method's return-type node
        directly without unwrapping it themselves. ``multi_return_type``
        only resolves when it carries a single child (otherwise the
        return is genuinely multi-valued and has no single raw name).
        """
        if not isinstance(type_node, Tree):
            return ""
        d = type_node.data
        if d == "type_expr":
            return self._get_raw_type_name(type_node.children[0])
        if d == "type_union":
            if len(type_node.children) == 1:
                return self._get_raw_type_name(type_node.children[0])
            return ""
        if d == "type_name":
            return self._dotted_name_to_str(type_node.children[0])
        if d == "single_return_type":
            return self._get_raw_type_name(type_node.children[0])
        if d == "multi_return_type":
            if len(type_node.children) == 1:
                return self._get_raw_type_name(type_node.children[0])
            return ""
        return ""

    # ─── Parameters ────────────────────────────────────────────

    # Internal name used for synthesised tuple parameters. Kept as a
    # class-level attribute so the declaration, prologue, and
    # destructuring code agree on the same convention.
    _TUPLE_PARAM_PREFIX = "_tup"

    def _typed_params_to_go(self, node, skip_self=False, func_name=None) -> str:
        if not isinstance(node, Tree) or node.data != "typed_parameters":
            return ""
        params = []
        param_idx = 0
        tuple_param_idx = 0
        for child in node.children:
            if child is None:
                continue
            if isinstance(child, Tree) and child.data == "typed_paramvalue":
                param_node = child.children[0]
                if isinstance(param_node, Tree) and param_node.data == "typed_param":
                    name = self._get_name(param_node.children[0])
                    if skip_self and name == "self":
                        continue
                    if len(param_node.children) > 1:
                        type_go = self._type_expr_to_go(param_node.children[1])
                    else:
                        type_go = "interface{}"
                    params.append(f"{name} {type_go}")
                    # Track default value if present
                    if func_name and len(child.children) > 1:
                        default_node = child.children[1]
                        self._func_defaults.setdefault(func_name, []).append((param_idx, default_node))
                    param_idx += 1
                elif isinstance(param_node, Tree) and param_node.data == "tuple_typed_param":
                    # Tuple-destructured param: the callee sees a single
                    # synthesised argument with the tuple's Go type, and
                    # the body later unpacks it into the original names
                    # via ``_emit_tuple_param_prologue``.
                    type_node = param_node.children[-1]
                    type_go = self._type_expr_to_go(type_node)
                    synth = f"{self._TUPLE_PARAM_PREFIX}{tuple_param_idx}"
                    params.append(f"{synth} {type_go}")
                    tuple_param_idx += 1
                    param_idx += 1
            elif isinstance(child, Tree) and child.data == "typed_starparams":
                # Variadic: *args: type -> args ...type
                star_param = child.children[0]
                if isinstance(star_param, Tree) and star_param.data == "typed_param":
                    name = self._get_name(star_param.children[0])
                    if len(star_param.children) > 1:
                        type_go = self._type_expr_to_go(star_param.children[1])
                    else:
                        type_go = "interface{}"
                    params.append(f"{name} ...{type_go}")
        return ", ".join(params)

    def _emit_tuple_param_prologue(self, params_node, skip_self=False):
        """For every ``tuple_typed_param`` in the signature, emit the
        Go statements that bind the individual names to the elements
        of the synthesised tuple parameter (``_tup0``, ``_tup1``, …).

        Supports two tuple lowerings:

        - ``tuple[T1, T2, ...]`` which compiles to a fixed-size Go
          array ``[N]interface{}`` — we index it positionally and
          type-assert to each declared type;
        - a fallback for callers that hand us a ``[]interface{}`` —
          same mechanism since indexing syntax is identical.
        """
        if not isinstance(params_node, Tree) or params_node.data != "typed_parameters":
            return
        tuple_idx = 0
        for child in params_node.children:
            if child is None:
                continue
            if not (isinstance(child, Tree) and child.data == "typed_paramvalue"):
                continue
            param_node = child.children[0]
            if not (isinstance(param_node, Tree) and param_node.data == "tuple_typed_param"):
                continue

            # Split children into name tokens and the trailing type_expr.
            name_tokens = param_node.children[:-1]
            type_node = param_node.children[-1]

            # Extract the positional Go types from the tuple annotation
            # when available — gives a typed unpack. Fall back to
            # ``interface{}`` otherwise.
            elem_types = self._tuple_element_types(type_node)

            synth = f"{self._TUPLE_PARAM_PREFIX}{tuple_idx}"
            tuple_idx += 1
            for i, name_node in enumerate(name_tokens):
                var = self._get_name(name_node)
                if not var:
                    continue
                elem_ty = elem_types[i] if i < len(elem_types) else "interface{}"
                if elem_ty and elem_ty != "interface{}":
                    self._emit(f"{var} := {synth}[{i}].({elem_ty})")
                    self._var_go_types[var] = elem_ty
                else:
                    self._emit(f"{var} := {synth}[{i}]")
                self._declare_var(var)
            # Mark the synthesised name as declared so later references
            # (in case the body uses it directly) don't redeclare it.
            self._declare_var(synth)

    def _tuple_element_types(self, type_node):
        """Extract Go element types from a ``tuple[...]`` annotation.
        Returns an empty list if the type isn't a recognisable tuple."""
        if not isinstance(type_node, Tree):
            return []
        if type_node.data == "type_expr":
            return self._tuple_element_types(type_node.children[0])
        if type_node.data == "type_union":
            if len(type_node.children) == 1:
                return self._tuple_element_types(type_node.children[0])
            return []
        if type_node.data == "type_generic":
            base = self._dotted_name_to_str(type_node.children[0])
            if base == "tuple":
                return [
                    self._type_expr_to_go(c)
                    for c in type_node.children[1:]
                    if c is not None
                ]
        return []

    # ─── Tree inspection ───────────────────────────────────────

    def _suite_contains_try(self, node) -> bool:
        if not isinstance(node, Tree):
            return False
        if node.data in ("try_stmt", "try_finally"):
            return True
        if node.data == "suite":
            for child in node.children:
                if isinstance(child, Tree) and child.data in ("try_stmt", "try_finally"):
                    return True
        return False

    def _tree_contains(self, node, data_name: str) -> bool:
        """Recursively check if any node in the tree has the given data name."""
        if not isinstance(node, Tree):
            return False
        if node.data == data_name:
            return True
        for child in node.children:
            if isinstance(child, Tree) and self._tree_contains(child, data_name):
                return True
        return False

    # ─── Class inference ───────────────────────────────────────

    def _infer_expr_class(self, node) -> str:
        """Try to determine the class name of an expression node, for operator overloading."""
        if not isinstance(node, Tree):
            return ""
        if node.data == "var":
            var_name = self._get_name(node.children[0])
            return self._var_types.get(var_name, "")
        if node.data == "funccall":
            func = node.children[0]
            if isinstance(func, Tree) and func.data == "var":
                name = self._get_name(func.children[0])
                if name in self._class_names:
                    return name
        return ""

    def _get_dunder_method(self, class_name: str, dunder: str) -> str:
        """Return the Go method name for a dunder on a class, or empty string."""
        methods = self._class_dunder_methods.get(class_name, {})
        return methods.get(dunder, "")

    def _infer_type_from_value(self, node) -> str:
        from compiler.constants import PYTHON_EXCEPTIONS
        if isinstance(node, Token):
            return "interface{}"
        if not isinstance(node, Tree):
            return "interface{}"
        d = node.data
        if d == "number":
            tok = str(node.children[0])
            return "float64" if "." in tok else "int"
        if d == "string" or d == "fstring":
            return "string"
        if d == "const_true" or d == "const_false":
            return "bool"
        if d == "list":
            return "[]interface{}"
        if d == "dict":
            return "map[string]interface{}"
        if d == "var":
            var_name = self._get_name(node.children[0])
            if var_name in self._init_param_types:
                return self._init_param_types[var_name]
            return "interface{}"
        if d == "funccall":
            func = node.children[0]
            if isinstance(func, Tree) and func.data == "var":
                name = self._get_name(func.children[0])
                if name and name[0].isupper() and name not in PYTHON_EXCEPTIONS:
                    return "*" + self._go_public_name(name)
        return "interface{}"

    # ─── Call argument helpers ─────────────────────────────────

    def _get_call_args(self, args_node) -> List[str]:
        # Backwards-compatible: callers that don't care about keyword
        # arguments still receive a positional-only list. Keyword args
        # are silently dropped here — pair this call with
        # ``_collect_call_args`` whenever you also need the kwargs.
        positional, _ = self._collect_call_args(args_node)
        return positional

    def _collect_call_args(self, args_node) -> Tuple[List[str], Dict[str, str]]:
        """Return ``(positional, kwargs)`` for an ``arguments`` node.

        The grammar's ``?argvalue`` collapse means a plain positional
        argument is the bare expression tree, while a keyword argument
        ``name=value`` shows up as an ``argvalue`` tree with two
        children. Star/double-star args (``*xs`` / ``**kw``) stay
        positional — those are passed through unchanged so the
        existing variadic lowering keeps working.
        """
        positional: List[str] = []
        kwargs: Dict[str, str] = {}
        if not isinstance(args_node, Tree) or args_node.data != "arguments":
            return positional, kwargs
        for child in args_node.children:
            if not isinstance(child, Tree):
                continue
            if child.data == "argvalue" and len(child.children) == 2:
                name_node = child.children[0]
                kw_name = ""
                if isinstance(name_node, Tree) and name_node.data == "var" and name_node.children:
                    kw_name = self._get_name(name_node.children[0]) or ""
                if kw_name:
                    kwargs[kw_name] = self._expr_to_go(child.children[1])
                else:
                    # Fall back to positional if the LHS isn't a bare name
                    # (defensive: the parser shouldn't produce this shape).
                    positional.append(self._transpile_argvalue(child))
            else:
                positional.append(self._transpile_argvalue(child))
        return positional, kwargs

    def _apply_call_kwargs(
        self, func_key: str, positional: List[str], kwargs: Dict[str, str],
    ) -> List[str]:
        """Reorder keyword args into a positional argument list.

        Uses ``_func_param_names[func_key]`` for the parameter order.
        Any keyword that doesn't match a parameter triggers a
        ``RuntimeError`` so the user gets a clear "unknown keyword
        argument" message rather than a confusing Go-side error.
        Returns ``positional`` unchanged when no kwargs were supplied
        — default-arg filling is left to ``_fill_default_args``.
        """
        if not kwargs:
            return positional
        names = self._func_param_names.get(func_key, [])
        if not names:
            kw_list = ", ".join(f"`{k}`" for k in sorted(kwargs))
            raise RuntimeError(
                f"keyword arguments {kw_list} cannot be applied to "
                f"`{func_key}` — its parameter names are not visible "
                f"in this compilation unit"
            )
        unknown = [k for k in kwargs if k not in names]
        if unknown:
            kw_list = ", ".join(f"`{k}`" for k in sorted(unknown))
            raise RuntimeError(
                f"unknown keyword argument(s) {kw_list} for `{func_key}`; "
                f"valid parameters are {', '.join(f'`{n}`' for n in names if n)}"
            )
        # Validate no overlap between positional and keyword args.
        for i, _ in enumerate(positional):
            if i < len(names) and names[i] in kwargs:
                raise RuntimeError(
                    f"argument `{names[i]}` for `{func_key}` was given "
                    f"both positionally and as a keyword"
                )
        # Build the final positional list, slot by slot.
        result = list(positional)
        defaults = self._func_defaults.get(func_key, [])
        default_map = {idx: node for idx, node in defaults}
        for i in range(len(positional), len(names)):
            pname = names[i]
            if pname in kwargs:
                result.append(kwargs[pname])
            elif i in default_map:
                entry = default_map[i]
                if isinstance(entry, str):
                    result.append(entry)
                else:
                    result.append(self._expr_to_go(entry))
            else:
                # No positional, no kwarg, no default — required arg
                # left unfilled. Let Go's diagnostic surface it; the
                # call site is pinned via ``//line`` already.
                break
        return result

    def _transpile_argvalue(self, node) -> str:
        """Transpile a single call argument.

        The interesting edge case is a tuple literal used as a single
        positional argument — ``fn((1, "hi"))``. Without special
        handling, the tuple's own ``_expr_to_go`` would flatten to
        ``1, "hi"``, silently turning the call into ``fn(1, "hi")``
        (two arguments). We detect the tuple shape and wrap it as a
        Go slice literal so it survives as a single value and matches
        functions that declare a tuple-destructured parameter.

        The grammar inlines ``argvalue`` (via Lark's ``?argvalue``), so
        the node we receive for a plain positional arg may be the bare
        expression *or* an ``argvalue`` wrapper when a default value is
        supplied — we handle both cases.
        """
        inner = node
        if isinstance(node, Tree) and node.data == "argvalue" and node.children:
            inner = node.children[0]
        if isinstance(inner, Tree) and inner.data == "tuple":
            elems = [
                self._expr_to_go(c)
                for c in inner.children
                if c is not None
            ]
            if elems:
                return f"[]interface{{}}{{{', '.join(elems)}}}"
        return self._expr_to_go(node)

    def _get_raw_call_args(self, args_node) -> List[Tree]:
        """Return raw AST nodes for each positional argument of a call.

        Useful when the transpilation of an argument depends on its
        *shape* (e.g. ``isinstance`` needs to see the type expression
        without compiling it like a value).
        """
        if not isinstance(args_node, Tree) or args_node.data != "arguments":
            return []
        result: List[Tree] = []
        for child in args_node.children:
            if isinstance(child, Tree):
                # argvalue wraps a single `test` node — unwrap it so the
                # caller receives the actual expression tree.
                if child.data == "argvalue" and child.children:
                    inner = child.children[0]
                    if isinstance(inner, Tree):
                        result.append(inner)
                        continue
                result.append(child)
        return result

    def _fill_default_args(self, func_key: str, args: list) -> list:
        """Fill in default argument values for calls with fewer args than params.

        Each default is stored as either a Lark ``Tree`` (fresh from the
        current transpile pass) or a pre-compiled Go expression string
        (loaded from the on-disk cache — Trees are not JSON-serialisable
        so the cache pipeline lowers them to their Go source before
        persisting).
        """
        if func_key not in self._func_defaults:
            return args
        # Don't fill defaults for variadic functions
        if func_key in self._variadic_functions:
            return args
        total = self._func_param_counts.get(func_key, 0)
        if len(args) >= total:
            return args
        defaults = self._func_defaults[func_key]
        default_map = {idx: node for idx, node in defaults}
        result = list(args)
        for i in range(len(args), total):
            if i in default_map:
                entry = default_map[i]
                if isinstance(entry, str):
                    result.append(entry)
                else:
                    result.append(self._expr_to_go(entry))
            else:
                break
        return result

    def _declare_params(self, params_node):
        if not isinstance(params_node, Tree) or params_node.data != "typed_parameters":
            return
        tuple_idx = 0
        for child in params_node.children:
            if isinstance(child, Tree) and child.data == "typed_paramvalue":
                param = child.children[0]
                if isinstance(param, Tree) and param.data == "typed_param":
                    name = self._get_name(param.children[0])
                    if name and name != "self":
                        self._declare_var(name)
                        # Track the declared type so method-dispatch logic
                        # knows whether this parameter is a user class
                        # instance (needed for correctly resolving calls
                        # like other.contains(...) that would otherwise be
                        # mis-dispatched to string built-ins).
                        if len(param.children) > 1:
                            raw_type = self._get_raw_type_name(param.children[1])
                            if raw_type and raw_type in getattr(self, "_class_names", set()):
                                self._var_types[name] = raw_type
                            go_type = self._type_expr_to_go(param.children[1])
                            if go_type:
                                self._var_go_types[name] = go_type
                elif isinstance(param, Tree) and param.data == "tuple_typed_param":
                    # The synthesised ``_tupN`` parameter is registered
                    # so downstream name lookups know it exists; the
                    # per-element names are declared later by the
                    # function-body prologue.
                    synth = f"{self._TUPLE_PARAM_PREFIX}{tuple_idx}"
                    self._declare_var(synth)
                    type_go = self._type_expr_to_go(param.children[-1])
                    if type_go:
                        self._var_go_types[synth] = type_go
                    tuple_idx += 1
