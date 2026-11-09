#!/usr/bin/env python3
"""Generator bodies compiled to resumable state machines.

A generator body is lowered in continuation-passing style: the body is a Go
closure ``func(__lamF *lamGenFrame[T]) int`` (a *continuation*) and every
``yield`` stores its value in the frame, stores the continuation running
the rest of the body in ``__lamF.k`` and returns ``lamGenYield``; the next
``Next`` runs ``k``. The body's locals are ordinary Go locals of the
closures, captured by the continuations created after them, so they live
in heap cells referenced by the continuation the generator holds — not by
a goroutine stack: a suspended generator is plain data, and a cycle
``self -> generator -> continuation -> self`` is collected like any other.
The runtime (``GENERATOR_RUNTIME_GO``) runs continuations in a trampoline
(``lamGenRun``): a continuation returning ``lamGenJump`` has set the next
one (loop back-edges always bounce through it, so the Go stack stays
bounded).

Structured control flow becomes continuations:

* a statement holding a ``yield`` (``if``, loops, ``try``, ``with``,
  ``match``, ``do``) gets the rest of its block as a named continuation
  (``__lamKN``), created before it, which every path through it calls;
* a loop is a closure (``__lamLoopN``) running a native Go ``for`` until a
  ``yield`` suspends it; the continuation after that ``yield`` re-enters
  the loop closure; iteration state (an index into a snapshot of the
  iterable, an inner generator handle) lives outside the closure so the
  loop resumes where it was; ``break`` calls the loop's continuation;
* ``try`` regions holding a ``yield`` cannot use ``defer``/``recover``
  across suspensions: the frame holds the handler of the region running
  (``__lamF.h``), which the trampoline calls with a recovered exception;
  a ``catch`` handler selects its clause's continuation, a ``finally``
  handler records the exception and runs the ``finally`` continuation,
  which re-raises it at its end. Leaving a ``finally`` region another
  way (falling through, ``return``, ``break`` / ``continue``) records
  where to go next in the region's ``__lamPendN`` and runs the ``finally``
  continuation, which goes there (``lamGenResume``). A ``with`` is a
  ``finally`` region closing its resource; a loop over a generator call
  is one stopping the inner generator.

The *close path* of the state a generator is suspended in (``return`` from
the ``yield``: its pending ``finally`` blocks and ``with`` closes, Python's
``GeneratorExit``) is the continuation ``__lamCloseN`` of the innermost
``finally`` region, stored in ``__lamF.c`` at each ``yield``; ``Stop`` /
``Close`` and the cleanup of a collected handle run it. It references only
what those blocks use, so the cleanup does not keep a collected generator
alive unless the ``finally`` code itself uses an object holding the
generator. A method generator whose close path reads ``self``
(``close_path_reads_self``) reads its receiver from the frame in every
continuation (``lamNewMethodGenerator``, ``_sm_emit_recv_shadow``), so the
cleanup reaches the receiver only through ``lamGenRecv``, which the runtime
makes weak while the generator is stored in a field of the receiver's
object.

Bodies the state machine does not lower (``defer`` in the body, ``yield``
used as a value, a ``do`` body holding a ``yield`` and a ``?`` inside a
nested ``try`` / ``with``) keep the coroutine lowering
(``_generator_sm_unsupported``).
"""

from __future__ import annotations

import re
from typing import List, Optional

from lark import Token, Tree

from compiler import narrowing as _narrowing
from compiler.constants import generator_go_elem
from compiler.safe_nav import rewrite_result_safe_nav


class _K:
    """Where control goes next: the end of the generator (``done``), a
    continuation variable (``name``), the back-edge of a loop through the
    trampoline (``back``), the end of a native loop body (``native``:
    falling off the Go ``for`` body runs the next iteration), a ``finally``
    region to run first (``fin``: ``(pend, fin, target)``) or the end of a
    ``finally`` block (``resume``: the ``lamGenResume`` call)."""

    __slots__ = ("kind", "name")

    def __init__(self, kind: str, name: str = ""):
        self.kind = kind
        self.name = name

    def in_closure(self) -> "_K":
        """The same target seen from inside a new continuation closure."""
        return _K("back", self.name) if self.kind == "native" else self


class _Region:
    """A ``try`` region holding a ``yield``: ``fin`` (a ``finally``, a
    ``with``, a loop owning a generator) or ``catch``."""

    __slots__ = ("kind", "handler", "close", "pend", "fin")

    def __init__(self, kind, handler, close, pend="", fin=""):
        self.kind = kind
        self.handler = handler
        self.close = close
        self.pend = pend
        self.fin = fin


class _Loop:
    """A loop holding a ``yield``: its closure ``head``, where ``break``
    goes, the region depth outside it, and the continuations of its body
    created once with the loop instead of once per iteration
    (``deferred``: they use no local of the body), with the locals and
    types in scope outside the body."""

    __slots__ = ("head", "after", "depth", "deferred", "declared", "types", "do_depth")

    def __init__(self, head: str, after: _K, depth: int, declared, types, do_depth: int):
        self.head = head
        self.after = after
        self.depth = depth
        self.deferred: list = []
        self.declared = declared
        self.types = types
        self.do_depth = do_depth


class _SMContext:
    def __init__(self, elem: str, has_regions: bool):
        self.elem = elem
        self.frame = f"*lamGenFrame[{elem}]"
        self.has_regions = has_regions
        self.regions: List[_Region] = []
        self.loops: List[_Loop] = []
        # The loop whose Go ``for`` body is being emitted directly (a
        # ``continue`` there is Go's); ``None`` inside a continuation.
        self.native_loop: Optional[_Loop] = None
        # ``(go_name, go_type)`` of the receiver every continuation reads
        # from the frame (``lamNewMethodGenerator``), ``None`` otherwise.
        self.recv: Optional[tuple] = None
        # ``(err_var, handler, depth)`` of the innermost ``do`` holding a
        # ``yield``: a ``?`` failing there runs its handler.
        self.do_targets: list = []

    def handler(self) -> str:
        return self.regions[-1].handler if self.regions else "nil"

    def close(self) -> str:
        for region in reversed(self.regions):
            if region.kind == "fin":
                return region.close
        return ""


_NESTED_SCOPES = {"funcdef", "classdef", "lambdef"}


def _own_nodes(node):
    """``node`` and its descendants outside nested functions / lambdas."""
    stack = [node]
    while stack:
        cur = stack.pop()
        if not isinstance(cur, Tree):
            continue
        yield cur
        if cur.data in _NESTED_SCOPES and cur is not node:
            continue
        stack.extend(c for c in cur.children if isinstance(c, Tree) and c.data not in _NESTED_SCOPES)


def _has_own_yield(node) -> bool:
    if isinstance(node, Tree) and node.data in _NESTED_SCOPES | {"decorated"}:
        return False
    return any(n.data in ("yield_expr", "yield_from") for n in _own_nodes(node))


def coroutine_construct(suite_node):
    """``(node, description)`` of the construct keeping the generator body
    ``suite_node`` on the coroutine lowering (``None`` when the state
    machine lowers it): a ``defer`` (it runs when the body ends), a
    ``yield`` used as a value, a ``?`` inside a ``try`` / ``with`` / ``do``
    nested in a ``do`` body holding a ``yield``."""
    for node in _own_nodes(suite_node):
        if node.data == "defer_stmt":
            return node, "`defer`"
        if node.data != "yield_stmt":
            for child in node.children:
                if isinstance(child, Tree) and child.data in ("yield_expr", "yield_from"):
                    return node, "`yield` as a value"
        if node.data == "do_stmt" and _has_own_yield(node.children[0]):
            for inner in _own_nodes(node.children[0]):
                if inner.data in ("try_stmt", "try_finally", "with_stmt", "do_stmt") and inner is not node and any(
                    n.data == "propagate" for n in _own_nodes(inner)
                ):
                    return inner, "`?` in a `try`, `with` or `do` inside a `do` body holding a `yield`"
    return None


_IDENT_RE = re.compile(r"[^\W\d]\w*")


def _fstring_slot_names(raw: str) -> list:
    """Identifiers of the slots of an f-string token (its whole text when
    the slots cannot be split)."""
    from compiler.fstrings import Slot, fstring_body, split_fstring
    found = fstring_body(raw)
    parts = split_fstring(found[0], found[1]) if found else None
    if parts is None:
        return _IDENT_RE.findall(raw)
    out: list = []
    for part in parts:
        if isinstance(part, Slot):
            out.extend(_IDENT_RE.findall(part.expr))
            out.extend(_IDENT_RE.findall(part.spec))
    return out


def _uses_name(node, name: str) -> bool:
    """Whether ``node`` reads the variable ``name`` (nested functions and
    lambdas included: they capture it)."""
    if not isinstance(node, Tree):
        return False
    word = re.compile(rf"\b{re.escape(name)}\b")
    for sub in node.iter_subtrees():
        for child in sub.children:
            # ``f"{self.n}"``: the slots of an f-string are still text.
            if isinstance(child, Token) and "FSTRING" in child.type.upper() and word.search(str(child)):
                return True
        if sub.data == "var" and sub.children and isinstance(sub.children[0], Tree):
            inner = sub.children[0]
            if inner.children and str(inner.children[0]) == name:
                return True
        elif sub.data == "var" and sub.children and str(sub.children[0]) == name:
            return True
    return False


def _finally_suite(node):
    """The ``finally`` block of a ``try`` statement (``None`` without one)."""
    if node.data == "try_stmt":
        for child in node.children[1:]:
            if isinstance(child, Tree) and child.data == "finally":
                return child.children[0] if child.children else None
        return None
    if node.data == "try_finally" and len(node.children) > 1:
        fin = node.children[1]
        if isinstance(fin, Tree) and fin.data == "finally":
            fin = fin.children[0] if fin.children else None
        return fin if isinstance(fin, Tree) else None
    return None


def _close_regions(suite_node) -> list:
    """``(node, finally_suite)`` of the ``try`` / ``with`` statements of a
    generator body holding a ``yield`` (the ``finally`` regions its close
    path runs; ``finally_suite`` is ``None`` for a ``with``)."""
    out = []
    for node in _own_nodes(suite_node):
        if node.data in ("try_stmt", "try_finally"):
            fin = _finally_suite(node)
            if fin is not None and _has_own_yield(node.children[0]):
                out.append((node, fin))
            elif fin is not None and node.data == "try_stmt" and any(
                isinstance(c, Tree) and c.data in ("catch_clauses", "suite") and _has_own_yield(c)
                for c in node.children[1:]
            ):
                out.append((node, fin))
        elif node.data == "with_stmt" and len(node.children) > 1 and _has_own_yield(node.children[1]):
            out.append((node, None))
    return out


def close_path_self_use(suite_node, self_name: str = "self"):
    """What of the close path of the generator body ``suite_node`` uses
    ``self``: ``"finally"`` (a ``finally`` block of a ``try`` holding a
    ``yield``), ``"with"`` (the resource of a ``with`` holding a
    ``yield``), ``""`` when none does."""
    regions = _close_regions(suite_node)
    for _node, fin in regions:
        if fin is not None and _uses_name(fin, self_name):
            return "finally"
    for node, fin in regions:
        if fin is None and _uses_name(node.children[0], self_name):
            return "with"
    return ""


def close_path_reads_self(suite_node, self_name: str = "self") -> bool:
    """Whether the close path of the generator body ``suite_node`` (the
    code ``Stop`` runs) may read ``self``: a ``finally`` block of a region
    using it, or — when an exception or a jump out of a ``finally`` can
    resume the body (a ``catch`` / ``do`` around a region, a ``break`` /
    ``continue`` in a ``finally``) — any use of ``self`` in the body."""
    regions = _close_regions(suite_node)
    if not regions:
        return False
    if any(fin is not None and _uses_name(fin, self_name) for _n, fin in regions):
        return True
    resumable = False
    region_ids = {id(node) for node, _f in regions}
    for node in _own_nodes(suite_node):
        if node.data == "try_stmt" and any(
            isinstance(c, Tree) and c.data == "catch_clauses" and c.children for c in node.children[1:]
        ) or node.data == "do_stmt":
            body = node.children[0]
            if any(id(n) in region_ids for n in _own_nodes(body)):
                resumable = True
                break
    if not resumable:
        for _n, fin in regions:
            if fin is not None and any(
                n.data in ("break_stmt", "continue_stmt") for n in _own_nodes(fin)
            ):
                resumable = True
                break
    return resumable and _uses_name(suite_node, self_name)


class GeneratorStateMachineMixin:
    """Lowering of generator bodies to state machines (see module doc)."""

    # ─── Entry ────────────────────────────────────────────────

    def _generator_sm_unsupported(self, suite_node) -> str:
        """The construct of a generator body that keeps it on the coroutine
        lowering (``coroutine_construct``), ``""`` when the state machine
        lowers it."""
        found = coroutine_construct(suite_node)
        return found[1] if found else ""

    def _emit_generator_handle(self, elem_type: str, suite_node, params_node, *,
                               skip_self: bool = False, recv: str = "", overrides=None,
                               value_type=None, recv_type: str = "", name: str = "") -> None:
        """``return <generator handle>`` on the body ``suite_node`` (at the
        current indent, inside the function returning it): a generator of
        Go ``elem_type`` values, each ``yield`` shaping its value as
        ``value_type`` (the declared element type, ``""`` when unknown).

        A method generator (receiver ``recv`` of Go type ``*recv_type``)
        whose close path reads ``self`` is a ``lamNewMethodGenerator``
        named ``name``: every continuation reads the receiver from the
        frame instead of capturing it, so the cleanup of a collected handle
        reaches it only through ``lamGenRecv`` (``GENERATOR_RUNTIME_GO``)."""
        from compiler.constants import GENERATOR_YIELD_NAME
        overrides = dict(overrides or {})
        unsupported = self._generator_sm_unsupported(suite_node)
        scoped = {
            **overrides,
            "_in_generator": True,
            "_generator_chan": GENERATOR_YIELD_NAME,
            "_generator_elem_type": value_type if value_type is not None else elem_type,
        }
        if unsupported:
            ctor = "lamNewIsolatedGenerator" if self._generator_needs_isolation(suite_node) else "lamNewGenerator"
            self._emit(f"return {ctor}[{elem_type}](func({GENERATOR_YIELD_NAME} func({elem_type}) bool) {{")
            scoped["_sm"] = None
        else:
            sm = _SMContext(elem_type, self._sm_body_has_regions(suite_node))
            if recv and recv_type and close_path_reads_self(suite_node):
                sm.recv = (recv, recv_type)
                self._emit(
                    f"return lamNewMethodGenerator[{elem_type}, {recv_type}]({recv}, \"{name}\", "
                    f"func(__lamF *lamGenFrame[{elem_type}]) int {{"
                )
            else:
                self._emit(f"return lamNewStateGenerator[{elem_type}](func(__lamF *lamGenFrame[{elem_type}]) int {{")
            scoped["_sm"] = sm
            scoped["_sm_leaf_loop"] = False
            scoped["_sm_case_k"] = None
        self.indent += 1
        self._push_scope()
        shadow = self._sm_emit_recv_shadow(scoped["_sm"]) if not unsupported else -1
        self._emit_params_prologue(params_node, skip_self=skip_self)
        if recv:
            self._declare_var(recv)
        body_start = len(self.output_lines)
        params_at_start = set(self.declared_vars)
        with self._scoped(**scoped):
            if unsupported:
                self._visit_suite(suite_node)
            else:
                self._sm_stmts(self._sm_suite_stmts(suite_node), _K("done"))
        self._emit_unused_local_silencers(body_start, params_at_start)
        self._pop_scope()
        if not unsupported:
            self._sm_prune_recv_shadow(scoped["_sm"], shadow)
        self.indent -= 1
        self._emit("})")

    def _sm_emit_recv_shadow(self, sm) -> int:
        """The receiver read from the frame at the start of a continuation
        of a ``lamNewMethodGenerator`` (shadowing the method's receiver);
        the index of that line (``-1`` when there is none)."""
        if sm is None or sm.recv is None:
            return -1
        recv, recv_type = sm.recv
        self._emit(f"{recv}, _ := __lamF.r.(*{recv_type})")
        self._emit(f"_ = {recv}")
        return len(self.output_lines) - 2

    def _sm_prune_recv_shadow(self, sm, index: int) -> None:
        """Drop the receiver line ``index`` of a continuation that does not
        use the receiver (the continuation's lines follow it)."""
        if index < 0 or sm is None or sm.recv is None:
            return
        recv, recv_type = sm.recv
        shadow = f"{recv}, _ := __lamF.r.(*{recv_type})"
        pattern = re.compile(rf"\b{re.escape(recv)}\b")
        nested_indent = None
        for line in self.output_lines[index + 2:]:
            if line.startswith("//line "):
                continue
            indent = len(line) - len(line.lstrip("\t"))
            if nested_indent is not None:
                if indent >= nested_indent:
                    continue
                nested_indent = None
            if line.strip() == shadow:
                # A nested continuation reads the receiver itself.
                nested_indent = indent
                continue
            if pattern.search(line):
                return
        del self.output_lines[index:index + 2]

    def _sm_body_has_regions(self, suite_node) -> bool:
        """Whether the body has a ``try`` / ``with`` / loop over a call
        holding a ``yield`` (exception handlers and close paths)."""
        for node in _own_nodes(suite_node):
            if node.data in ("try_stmt", "try_finally", "with_stmt") and _has_own_yield(node):
                return True
            if node.data == "for_stmt" and _has_own_yield(node.children[2]) and isinstance(
                node.children[1], Tree
            ) and node.children[1].data == "funccall":
                return True
            if node.data == "yield_from":
                return True
        return False

    # ─── Helpers ──────────────────────────────────────────────

    @staticmethod
    def _sm_suite_stmts(node) -> list:
        if not isinstance(node, Tree):
            return []
        if node.data != "suite":
            children = [node]
        else:
            children = [c for c in node.children if isinstance(c, Tree)]
        out = []
        for child in children:
            if child.data == "simple_stmt":
                out.extend(c for c in child.children if isinstance(c, Tree))
            else:
                out.append(child)
        return out

    def _sm_new(self, prefix: str) -> str:
        self._sm_counter = getattr(self, "_sm_counter", 0) + 1
        return f"__lamG{prefix}{self._sm_counter}"

    def _sm_goto(self, k: _K) -> None:
        if k.kind == "done":
            self._emit("return lamGenDone")
        elif k.kind == "name":
            self._emit(f"return {k.name}(__lamF)")
        elif k.kind == "back":
            self._emit(f"__lamF.k = {k.name}")
            self._emit("return lamGenJump")
        elif k.kind == "fin":
            pend, fin, target = k.name
            self._emit(f"{pend} = {target}")
            self._emit(f"return {fin}(__lamF)")
        elif k.kind == "resume":
            self._emit(f"return {k.name}")
        # ``native``: fall off the end of the loop body.

    def _sm_value(self, k: _K) -> str:
        sm = self._sm
        if k.kind == "done":
            return f"lamGenEnd[{sm.elem}]"
        if k.kind in ("name", "back", "native"):
            return k.name
        if k.kind == "fin":
            pend, fin, target = k.name
            return f"func(__lamF {sm.frame}) int {{ {pend} = {target}; return {fin}(__lamF) }}"
        return f"func(__lamF {sm.frame}) int {{ return {k.name} }}"

    def _sm_set_handler(self) -> None:
        if self._sm.has_regions:
            self._emit(f"__lamF.h = {self._sm.handler()}")

    def _sm_open_closure(self, decl: str) -> tuple:
        """Open ``decl func(__lamF *F) int {`` (a continuation literal); the
        returned token closes it with ``_sm_close_closure``."""
        sm = self._sm
        self._emit(f"{decl}func(__lamF {sm.frame}) int {{")
        self.indent += 1
        self._push_scope()
        saved_native = sm.native_loop
        sm.native_loop = None
        shadow = self._sm_emit_recv_shadow(sm)
        self._sm_set_handler()
        return saved_native, self._scope_unused_tracking_start(), shadow

    def _sm_close_closure(self, token, suffix: str = "") -> None:
        saved_native, (body_start, vars_at_start), shadow = token
        self._pop_scope_with_unused_local_silencers(body_start, vars_at_start)
        self._sm_prune_recv_shadow(self._sm, shadow)
        self.indent -= 1
        self._emit("}" + suffix)
        self._sm.native_loop = saved_native

    def _sm_closure(self, name: str, emit_body, declare: bool = True) -> None:
        """``name := func(__lamF *F) int { <emit_body()> }``."""
        token = self._sm_open_closure(f"{name} := " if declare else f"{name} = ")
        emit_body()
        self._sm_close_closure(token)
        self._emit(f"_ = {name}")

    # ─── Jumps (hooks of the statement visitors) ───────────────

    def _sm_jump_to(self, target: str, depth: int) -> None:
        """Go to the continuation value ``target`` from here, through the
        ``finally`` regions entered since region depth ``depth``."""
        sm = self._sm
        fins = [r for r in reversed(sm.regions[depth:]) if r.kind == "fin"]
        if not fins:
            self._emit(f"__lamF.k = {target}")
            self._emit("return lamGenJump")
            return
        value = target
        for region in reversed(fins[1:]):
            value = f"func(__lamF {sm.frame}) int {{ {region.pend} = {value}; return {region.fin}(__lamF) }}"
        self._emit(f"{fins[0].pend} = {value}")
        self._emit(f"return {fins[0].fin}(__lamF)")

    def _sm_emit_loop_jump(self, kind: str) -> bool:
        """``break`` / ``continue`` of a loop holding a ``yield``; ``False``
        when the jump is Go's (not in a state machine loop)."""
        sm = self._sm
        if sm is None or self._sm_leaf_loop or not sm.loops:
            return False
        loop = sm.loops[-1]
        crosses = any(r.kind == "fin" for r in sm.regions[loop.depth:])
        if kind == "continue":
            if sm.native_loop is loop and not crosses:
                self._emit("continue")
            else:
                self._sm_jump_to(loop.head, loop.depth)
            return True
        if not crosses:
            self._sm_goto(loop.after)
        else:
            self._sm_jump_to(self._sm_value(loop.after), loop.depth)
        return True

    def _sm_emit_return(self) -> bool:
        """``return`` in a state machine body: the close path of the
        innermost ``finally`` region, else the end of the generator."""
        sm = self._sm
        if sm is None:
            return False
        close = sm.close()
        if close:
            self._emit(f"return {close}(__lamF)")
        else:
            self._emit("return lamGenDone")
        return True

    def _sm_emit_q_guard(self, tmp: str) -> bool:
        """``?`` failing inside a ``do`` body holding a ``yield``: store the
        error and run the ``do`` handler."""
        sm = self._sm
        if sm is None or not sm.do_targets or not self._sm_q_target:
            return False
        err_var, handler, depth = sm.do_targets[-1]
        self._emit(f"if !{tmp}.Ok() {{")
        self.indent += 1
        self._emit(f"{err_var} = {tmp}")
        self._sm_jump_to(handler, depth)
        self.indent -= 1
        self._emit("}")
        return True

    # ─── Statement lists ──────────────────────────────────────

    def _sm_stmts(self, stmts: list, k: _K) -> None:
        """Run ``stmts`` then continue with ``k``."""
        for index, stmt in enumerate(stmts):
            rest = stmts[index + 1:]
            if not _has_own_yield(stmt):
                self._visit(stmt)
                if stmt.data == "if_stmt" and rest:
                    plan = self._nr_plan(_narrowing.guard_facts(stmt), rest)
                    if plan:
                        self._emit("{")
                        self.indent += 1
                        self._push_scope()
                        body_start, vars_at_start = self._scope_unused_tracking_start()
                        self._nr_emit_binds(plan)
                        with self._nr_registered(plan):
                            self._sm_stmts(rest, k)
                        self._pop_scope_with_unused_local_silencers(body_start, vars_at_start)
                        self.indent -= 1
                        self._emit("}")
                        return
                continue
            self._emit_line_directive(stmt)
            rewrite_result_safe_nav(stmt, lambda recv: self._expr_go_type_hint(recv) == "*Result")
            if stmt.data == "yield_stmt":
                self._sm_yield(stmt, rest, k)
                return
            self._sm_prehoist(stmt)
            plan = self._nr_plan(_narrowing.guard_facts(stmt), rest) if rest and stmt.data == "if_stmt" else []
            deferred = self._sm_defer_in_loop(rest, k) if rest and not plan else ""
            if deferred:
                after = _K("name", deferred)
            elif rest:
                name = self._sm_new("K")
                kk = k.in_closure()

                def emit_rest(rest=rest, plan=plan, kk=kk):
                    self._nr_emit_binds(plan)
                    with self._nr_registered(plan):
                        self._sm_stmts(rest, kk)

                self._sm_closure(name, emit_rest)
                after = _K("name", name)
            else:
                after = k
            self._emit("{")
            self.indent += 1
            self._push_scope()
            body_start, vars_at_start = self._scope_unused_tracking_start()
            self._sm_compound(stmt, after)
            self._pop_scope_with_unused_local_silencers(body_start, vars_at_start)
            self.indent -= 1
            self._emit("}")
            return
        self._sm_goto(k)

    def _sm_prehoist(self, stmt) -> None:
        """Locals assigned on every path through ``stmt`` and visible after
        it (as the structured lowerings declare them before the statement):
        declared before the continuation running the rest."""
        d = stmt.data
        if d == "if_stmt":
            else_suite = stmt.children[3] if len(stmt.children) > 3 else None
            if isinstance(else_suite, Tree):
                suites = [stmt.children[1]]
                elifs = stmt.children[2] if len(stmt.children) > 2 else None
                if isinstance(elifs, Tree):
                    suites.extend(
                        c.children[1] for c in elifs.children
                        if isinstance(c, Tree) and len(c.children) > 1 and isinstance(c.children[1], Tree)
                    )
                suites.append(else_suite)
                self._hoist_flow_locals(suites)
        elif d == "try_stmt":
            catch_suites = []
            for child in stmt.children[1:]:
                if isinstance(child, Tree) and child.data == "catch_clauses":
                    for clause in child.children:
                        if isinstance(clause, Tree) and clause.data == "catch_clause":
                            catch_suites.extend(s for s in clause.children if isinstance(s, Tree) and s.data == "suite")
            self._hoist_flow_locals([stmt.children[0]] + catch_suites)
        elif d == "try_finally":
            self._hoist_flow_locals([stmt.children[0]])
        elif d == "for_stmt":
            if self._flow_iterable_statically_nonempty(stmt.children[1]) and not self._flow_contains_loop_exit(stmt.children[2]):
                self._hoist_flow_locals([stmt.children[2]])
        elif d == "match_stmt":
            cases = [c for c in stmt.children[1:] if isinstance(c, Tree) and c.data == "case"]
            irrefutable = next(
                (i for i, case in enumerate(cases)
                 if self._pm_case_parts(case)[1] is None and self._pm_irrefutable(self._pm_case_parts(case)[0])),
                None,
            )
            if irrefutable is not None:
                self._hoist_flow_locals([self._pm_case_parts(c)[2] for c in cases[:irrefutable + 1]])

    def _sm_compound(self, stmt, k: _K) -> None:
        d = stmt.data
        if d == "if_stmt":
            self._sm_if(stmt, k)
        elif d == "while_stmt":
            self._sm_while(stmt, k)
        elif d == "for_stmt":
            self._sm_for(stmt, k)
        elif d == "try_stmt":
            clauses_node = else_suite = finally_node = None
            for child in stmt.children[1:]:
                if isinstance(child, Tree):
                    if child.data == "catch_clauses":
                        clauses_node = child
                    elif child.data == "finally":
                        finally_node = child
                    elif child.data == "suite":
                        else_suite = child
            clauses = [
                c for c in (clauses_node.children if clauses_node is not None else [])
                if isinstance(c, Tree) and c.data == "catch_clause"
            ]
            finally_suite = finally_node.children[0] if finally_node is not None else None
            self._sm_try(
                lambda kk: self._sm_stmts(self._sm_suite_stmts(stmt.children[0]), kk),
                clauses, else_suite,
                (lambda kk: self._sm_stmts(self._sm_suite_stmts(finally_suite), kk)) if finally_suite is not None else None,
                k,
            )
        elif d == "try_finally":
            fin = stmt.children[1] if len(stmt.children) > 1 else None
            if isinstance(fin, Tree) and fin.data == "finally":
                fin = fin.children[0]
            self._sm_try(
                lambda kk: self._sm_stmts(self._sm_suite_stmts(stmt.children[0]), kk),
                [], None,
                (lambda kk: self._sm_stmts(self._sm_suite_stmts(fin), kk)) if isinstance(fin, Tree) else None,
                k,
            )
        elif d == "with_stmt":
            items = [
                item for item in (stmt.children[0].children if isinstance(stmt.children[0], Tree) else [])
                if isinstance(item, Tree) and item.data == "with_item"
            ]
            self._sm_with(items, stmt.children[1], k)
        elif d == "match_stmt":
            with self._scoped(_sm_case_k=k):
                self._visit_match_stmt(stmt)
            self._sm_goto(k)
        elif d == "do_stmt":
            self._sm_do(stmt, k)
        elif d in ("suite", "block"):
            self._sm_stmts(self._sm_suite_stmts(stmt), k)
        else:
            self._sm_unreachable(stmt)

    def _sm_unreachable(self, stmt) -> None:
        raise NotImplementedError(f"generator state machine: unsupported statement `{stmt.data}` holding a yield")

    def _visit_case_suite(self, suite) -> None:
        """A ``match`` case body (``_visit_match_stmt``): in a state machine
        ``match`` holding a ``yield``, the body continues with the
        statement's continuation."""
        k = self._sm_case_k
        if k is None:
            self._visit_suite(suite)
            return
        with self._scoped(_sm_case_k=None):
            self._sm_stmts(self._sm_suite_stmts(suite), k)

    # ─── yield ────────────────────────────────────────────────

    def _sm_yield(self, stmt, rest: list, k: _K) -> None:
        expr = stmt.children[0]
        if isinstance(expr, Tree) and expr.data == "yield_from":
            item = self._sm_new("YieldFrom")
            item_var = Tree("var", [Tree("name", [Token("NAME", item)])])
            loop = Tree("for_stmt", [
                Tree("untyped_for_target", [item_var]),
                expr.children[0],
                Tree("suite", [Tree("yield_stmt", [Tree("yield_expr", [item_var])])]),
                None,
            ])
            self._sm_stmts([loop] + rest, k)
            return
        sm = self._sm
        self._emit(f"__lamF.v = {self._yield_value_go(expr)}")
        if sm.has_regions:
            self._emit("if __lamF.closing {")
            self.indent += 1
            self._sm_emit_return()
            self.indent -= 1
            self._emit("}")
            self._emit(f"__lamF.c = {sm.close() or 'nil'}")
        deferred = self._sm_defer_in_loop(rest, k) if rest else ""
        if deferred:
            self._emit(f"__lamF.k = {deferred}")
        elif rest:
            token = self._sm_open_closure("__lamF.k = ")
            self._sm_stmts(rest, k.in_closure())
            self._sm_close_closure(token)
        else:
            self._emit(f"__lamF.k = {self._sm_value(k)}")
        self._emit("return lamGenYield")

    # ─── if ───────────────────────────────────────────────────

    def _sm_if(self, node, k: _K) -> None:
        branch_facts, else_facts = _narrowing.if_branch_facts(node)
        cond = self._expr_to_go(node.children[0])
        self._emit(f"if {cond} {{")
        self._sm_branch(branch_facts[0] if branch_facts else [], node.children[1], k)
        nested = 0
        elifs = node.children[2] if len(node.children) > 2 else None
        branch_index = 0
        if isinstance(elifs, Tree) and elifs.data == "elifs":
            for elif_node in elifs.children:
                if not (isinstance(elif_node, Tree) and elif_node.data == "elif_"):
                    continue
                branch_index += 1
                if self._contains_propagate(elif_node.children[0]):
                    self._emit("} else {")
                    self.indent += 1
                    nested += 1
                    econd = self._expr_to_go(elif_node.children[0])
                    self._emit(f"if {econd} {{")
                else:
                    econd = self._expr_to_go(elif_node.children[0])
                    self._emit(f"}} else if {econd} {{")
                facts = branch_facts[branch_index] if branch_index < len(branch_facts) else []
                self._sm_branch(facts, elif_node.children[1], k)
        else_suite = node.children[3] if len(node.children) > 3 else None
        self._emit("} else {")
        self._sm_branch(else_facts if isinstance(else_suite, Tree) else [], else_suite, k)
        self._emit("}")
        for _ in range(nested):
            self.indent -= 1
            self._emit("}")

    def _sm_branch(self, facts, suite, k: _K) -> None:
        self.indent += 1
        self._push_scope()
        body_start, vars_at_start = self._scope_unused_tracking_start()
        stmts = self._sm_suite_stmts(suite) if isinstance(suite, Tree) else []
        self._nr_emit_region(facts, [suite] if isinstance(suite, Tree) else [],
                             lambda: self._sm_stmts(stmts, k))
        self._pop_scope_with_unused_local_silencers(body_start, vars_at_start)
        self.indent -= 1

    # ─── Loops ────────────────────────────────────────────────

    def _sm_loop_closure(self, head: str, k: _K, emit_for, else_suite) -> None:
        """``head = func(__lamF) int { <for loop>; <else>; <k> }`` and its
        first entry. ``emit_for(loop)`` emits the Go ``for`` running the body
        natively."""
        sm = self._sm
        after = k.in_closure()
        loop = _Loop(head, after, len(sm.regions), set(self.declared_vars),
                     dict(self._var_go_types), len(sm.do_targets))
        self._emit(f"var {head} func(__lamF {sm.frame}) int")
        decl_at = len(self.output_lines)
        token = self._sm_open_closure(f"{head} = ")
        sm.loops.append(loop)
        try:
            emit_for(loop)
        finally:
            sm.loops.pop()
        if else_suite is not None:
            self._push_scope()
            body_start, vars_at_start = self._scope_unused_tracking_start()
            self._sm_stmts(self._sm_suite_stmts(else_suite), after)
            self._pop_scope_with_unused_local_silencers(body_start, vars_at_start)
        else:
            self._sm_goto(after)
        self._sm_close_closure(token)
        if loop.deferred:
            self.output_lines[decl_at:decl_at] = [
                f"{self._indent_str()}var {name} func(__lamF {sm.frame}) int" for name, _r in loop.deferred
            ]
            sm.loops.append(loop)
            try:
                for name, rest in loop.deferred:
                    # A ``yield``'s continuation only runs from the
                    # trampoline: it may enter the loop closure directly.
                    back = _K("name" if name.startswith("__lamGResume") else "back", head)
                    self._sm_closure(name, lambda rest=rest, back=back: self._sm_stmts(rest, back), declare=False)
            finally:
                sm.loops.pop()
        self._emit(f"return {head}(__lamF)")

    def _sm_defer_in_loop(self, rest: list, k: _K) -> str:
        """The name of a continuation running ``rest`` then the next
        iteration of the loop whose Go ``for`` body is being emitted,
        created once with the loop (``_Loop.deferred``) when ``rest`` uses
        no local declared or narrowed in the loop body; ``""`` otherwise."""
        sm = self._sm
        loop = sm.native_loop
        if loop is None or k.kind != "native" or k.name != loop.head:
            return ""
        if len(sm.do_targets) != loop.do_depth or len(sm.regions) != loop.depth:
            return ""
        for stmt in rest:
            for go_name in self._sm_go_names_read(stmt):
                if go_name not in self.declared_vars:
                    continue
                if go_name not in loop.declared or self._var_go_types.get(go_name) != loop.types.get(go_name):
                    return ""
        name = self._sm_new("Resume")
        loop.deferred.append((name, rest))
        return name

    def _sm_go_names_read(self, node) -> set:
        """Go names of the variables ``node`` may read: its ``var`` nodes,
        the identifiers of its f-string slots (text until the f-string is
        lowered) and of its ``go!`` blocks (Go code naming Go locals) —
        over-approximated, which only keeps a continuation per iteration."""
        names: set = set()
        if not isinstance(node, Tree):
            return names
        for sub in node.iter_subtrees():
            if sub.data == "var" and sub.children:
                name = self._get_name(sub.children[0])
                names.add(self._go_binding_name(name))
            for child in sub.children:
                if isinstance(child, Token) and "FSTRING" in child.type.upper():
                    names.update(self._go_binding_name(n) for n in _fstring_slot_names(str(child)))
            if sub.data == "funccall" and sub.children and isinstance(sub.children[0], Tree) \
                    and sub.children[0].data == "var" and sub.children[0].children \
                    and self._get_name(sub.children[0].children[0]) in ("__go_block__", "__go_inline__"):
                for token in sub.scan_values(lambda v: isinstance(v, Token)):
                    text = str(token)
                    if len(text) >= 2 and text[0] in "\"'" and text[-1] == text[0]:
                        names.update(_IDENT_RE.findall(self.go_blocks.get(text[1:-1], "")))
        return names

    def _sm_loop_body(self, loop: _Loop, body_suite, facts=None, region=None) -> None:
        """The statements of a loop body inside its Go ``for`` (at the
        current indent; the loop variables are already bound)."""
        sm = self._sm
        saved_native = sm.native_loop
        sm.native_loop = loop
        stmts = self._sm_suite_stmts(body_suite)
        try:
            with self._scoped(_loop_break_flag="", _loop_jump=None, _sm_leaf_loop=False):
                if facts:
                    self._nr_emit_region(facts, region or [body_suite],
                                         lambda: self._sm_stmts(stmts, _K("native", loop.head)))
                else:
                    self._sm_stmts(stmts, _K("native", loop.head))
        finally:
            sm.native_loop = saved_native

    def _sm_while(self, node, k: _K) -> None:
        head = self._sm_new("Loop")
        else_suite = node.children[2] if len(node.children) > 2 and node.children[2] is not None else None

        def emit_for(loop):
            self._emit("for {")
            self.indent += 1
            self._push_scope()
            body_start, vars_at_start = self._scope_unused_tracking_start()
            cond = self._expr_to_go(node.children[0])
            self._emit(f"if !({cond}) {{ break }}")
            self._sm_loop_body(
                loop, node.children[1],
                _narrowing.condition_facts(node.children[0], True),
                [node.children[1], node.children[0]],
            )
            self._pop_scope_with_unused_local_silencers(body_start, vars_at_start)
            self.indent -= 1
            self._emit("}")

        self._sm_loop_closure(head, k, emit_for, else_suite)

    def _sm_for(self, node, k: _K) -> None:
        """A ``for`` loop holding a ``yield``: the iterable is evaluated
        once into a snapshot (a slice, a map's keys, a string's runes, a
        generator handle, a channel) indexed by a counter outside the loop
        closure, so the loop resumes at the next element."""
        fresh = [
            go_name for go_name in (
                self._go_binding_name(name) for name in _narrowing.target_names(node.children[0])
            )
            if go_name != "_" and go_name not in self.declared_vars
        ]
        saved = {
            name: (
                self._var_go_types.pop(name, self._NO_TYPE),
                self._var_types.pop(name, self._NO_TYPE),
                self._var_result_payloads.pop(name, self._NO_TYPE),
            )
            for name in fresh
        }
        try:
            self._sm_for_inner(node, k)
        finally:
            for name, (go_type, class_name, payload) in saved.items():
                for table, value in (
                    (self._var_go_types, go_type), (self._var_types, class_name),
                    (self._var_result_payloads, payload),
                ):
                    if value is self._NO_TYPE:
                        table.pop(name, None)
                    else:
                        table[name] = value

    def _sm_for_inner(self, node, k: _K) -> None:
        target_node, iterable_node, suite_node = node.children[0], node.children[1], node.children[2]
        else_suite = node.children[3] if len(node.children) > 3 and node.children[3] is not None else None
        if isinstance(target_node, Tree) and target_node.data in ("typed_for_target", "untyped_for_target"):
            var_name = self._expr_to_go(target_node.children[0])
        elif isinstance(target_node, Tree):
            var_name = self._expr_to_go(target_node)
        else:
            var_name = str(target_node)

        hint = self._expr_go_type_hint(iterable_node) if isinstance(iterable_node, Tree) and iterable_node.data == "var" else ""
        iterable_type = hint if hint and hint != "interface{}" else self._infer_type_from_value(iterable_node)
        if iterable_type in {"", "interface{}"} and isinstance(iterable_node, Tree) and iterable_node.data in {"funccall", "getattr"}:
            iterable_type = self._expr_go_type_hint(iterable_node) or iterable_type
        if isinstance(iterable_node, Tree) and iterable_node.data == "list":
            iterable_type = self._list_literal_elem_hint_type(iterable_node, iterable_type)
        range_info = self._check_range_call(iterable_node)
        if range_info:
            self._var_go_types[var_name] = "int"
        elif iterable_type.startswith("[]"):
            self._var_go_types[var_name] = iterable_type[2:]
            if iterable_type == "[]*Result":
                payload = self._iterable_result_payload_go_type(iterable_node)
                if payload and payload != "interface{}":
                    self._var_result_payloads[var_name] = payload

        n = self._sm_new("")[6:]
        it, idx = f"__lamGIt{n}", f"__lamGIdx{n}"
        binds: list = []        # Go statements binding the targets (``{idx}`` advanced first)
        declared: list = []     # names the binds declare
        cond = f"{idx} < len({it})"
        advance = f"{idx}++"
        owned = False
        head = f"__lamGLoop{n}"

        def bind(name: str, value: str) -> None:
            if name == "_":
                return
            if name in self.declared_vars and name not in declared:
                binds.append(f"{name} = {value}")
            else:
                binds.append(f"{name} := {value}")
                declared.append(name)

        enumerate_src = self._check_enumerate_call(iterable_node)
        if enumerate_src:
            idx_name, val_name = self._split_tuple_target(target_node)
            start_go = self._enumerate_start_go(
                iterable_node, lambda name, value: self._emit(f"{name} := {value}"),
            ) if idx_name and idx_name != "_" else ""
            raw_src = self._get_raw_call_args(iterable_node.children[1] if len(iterable_node.children) > 1 else None)
            src_node = raw_src[0] if raw_src else None
            src_type = (self._expr_go_type_hint(src_node) or self._infer_type_from_value(src_node) or "") if src_node is not None else ""
            iter_str = enumerate_src
            if isinstance(src_node, Tree) and src_node.data == "list" and src_type.startswith("[]") and src_type != "[]interface{}":
                iter_str = self._typed_value_to_go(src_node, src_type)
            if src_type == "string":
                self._emit(f"{it} := []rune({iter_str})")
            else:
                self._emit(f"{it} := {iter_str}")
            self._emit(f"{idx} := 0")
            index_value = f"{idx} - 1" + (f" + {start_go}" if start_go else "")
            if idx_name and val_name:
                bind(idx_name, index_value)
                if src_type == "string":
                    bind(val_name, f"string({it}[{idx} - 1])")
                    if val_name != "_":
                        self._var_go_types[val_name] = "string"
                else:
                    bind(val_name, f"{it}[{idx} - 1]")
                    if val_name != "_" and src_type.startswith("[]"):
                        self._var_go_types[val_name] = src_type[2:]
                if idx_name != "_":
                    self._var_go_types[idx_name] = "int"
            else:
                bind(var_name, f"{idx} - 1")
        elif range_info:
            start, end, step = range_info
            negative = step.lstrip().startswith(("-", "(−", "(-"))
            self._emit(f"{idx} := {start}")
            self._emit(f"{it} := {end}")
            if step.strip() != "1":
                step_var = f"__lamGStep{n}"
                self._emit(f"{step_var} := {step}")
                advance = f"{idx} += {step_var}"
            cond = f"{idx} {'>' if negative else '<'} {it}"
            # The target is bound before the counter advances.
            bind(var_name, idx)
        else:
            dict_literal_type = self._uniform_dict_literal_type(iterable_node)
            if isinstance(iterable_node, Tree) and iterable_node.data == "list" and iterable_type.startswith("[]") and iterable_type != "[]interface{}":
                iter_str = self._typed_value_to_go(iterable_node, iterable_type)
            elif dict_literal_type:
                iter_str = self._typed_dict_literal_to_go(iterable_node, dict_literal_type)
            elif (
                isinstance(iterable_node, Tree) and iterable_node.data == "propagate"
                and (iterable_type.startswith("[]") or iterable_type.startswith("map["))
                and iterable_type not in ("[]interface{}", "map[string]interface{}")
            ):
                with self._scoped(_propagate_cast_hint=iterable_type):
                    iter_str = self._expr_to_go(iterable_node)
            else:
                iter_str = self._expr_to_go(iterable_node)
            is_chan = self._is_generator_call(iterable_node) or self._is_chan_iterable(iterable_node)
            key_name, val_name = (None, None) if is_chan else self._split_tuple_target(target_node)
            destructure = self._for_tuple_destructure_types(target_node, iterable_node, is_chan, dict_literal_type)
            item = f"__lamGItem{n}"
            if is_chan:
                owned = self._is_generator_call(iterable_node)
                is_gen = owned or generator_go_elem(self._expr_go_type_hint(iterable_node) or "") is not None
                self._emit(f"{it} := {iter_str}.own()" if owned else f"{it} := {iter_str}")
                cond = ""
                advance = ""
                if is_gen:
                    binds.append(f"{item}, __lamGMore{n} := {it}.Next()")
                else:
                    binds.append(f"{item}, __lamGMore{n} := <-{it}")
                binds.append(f"if !__lamGMore{n} {{ break }}")
                if destructure is not None:
                    targets, tuple_types = destructure
                    for index, target in enumerate(targets):
                        elem_type = tuple_types[index] if index < len(tuple_types) else ""
                        cast = f".({elem_type})" if elem_type else ""
                        bind(target, f"{item}[{index}]{cast}")
                        if elem_type and target != "_":
                            self._var_go_types[target] = elem_type
                else:
                    self._bind_generator_loop_target(var_name, iterable_node)
                    bind(var_name, item)
            else:
                if destructure is not None:
                    targets, tuple_types = destructure
                    if isinstance(iterable_node, Tree) and iterable_node.data == "list" and not iterable_type.startswith("[][]"):
                        iter_str = self._typed_value_to_go(iterable_node, "[][]interface{}")
                    self._emit(f"{it} := {iter_str}")
                    binds.append(f"{item} := {it}[{idx} - 1]")
                    for index, target in enumerate(targets):
                        elem_type = tuple_types[index] if index < len(tuple_types) else ""
                        cast = f".({elem_type})" if elem_type else ""
                        bind(target, f"{item}[{index}]{cast}")
                        if elem_type and target != "_":
                            self._var_go_types[target] = elem_type
                elif (key_name and val_name) or self._range_single_target_uses_key(iterable_node):
                    if key_name and val_name:
                        map_type = dict_literal_type or (iterable_type or "").strip()
                    else:
                        map_type = (self._expr_go_type_hint(iterable_node) or iterable_type or "").strip()
                    key_type, value_type = self._split_go_map_type(map_type)
                    keys = f"__lamGKeys{n}"
                    self._emit(f"{it} := {iter_str}")
                    self._emit(f"{keys} := lamGenMapKeys({it})")
                    cond = f"{idx} < len({keys})"
                    if key_name and val_name:
                        for name, go_type in ((key_name, key_type), (val_name, value_type)):
                            if name != "_" and name not in self.declared_vars and go_type and go_type != "interface{}":
                                self._var_go_types[name] = go_type
                        binds.append(f"{item} := {keys}[{idx} - 1]")
                        binds.append(f"{item}V, __lamGHas{n} := {it}[{item}]")
                        binds.append(f"if !__lamGHas{n} {{ continue }}")
                        bind(key_name, item)
                        bind(val_name, f"{item}V")
                    else:
                        if key_type and key_type != "interface{}":
                            self._var_go_types[var_name] = key_type
                        binds.append(f"{item} := {keys}[{idx} - 1]")
                        binds.append(f"if _, __lamGHas{n} := {it}[{item}]; !__lamGHas{n} {{ continue }}")
                        bind(var_name, item)
                elif self._expr_go_type_hint(iterable_node) == "LamJSON":
                    self._emit(f"{it} := lamJSONToList({iter_str})")
                    bind(var_name, f"lamJSONWrapRaw({it}[{idx} - 1])")
                    self._var_go_types[var_name] = "LamJSON"
                elif self._iterates_string(iterable_node):
                    self._emit(f"{it} := []rune({iter_str})")
                    bind(var_name, f"string({it}[{idx} - 1])")
                    self._var_go_types[var_name] = "string"
                else:
                    self._emit(f"{it} := {iter_str}")
                    bind(var_name, f"{it}[{idx} - 1]")
            if cond:
                self._emit(f"{idx} := 0")
        self._emit(f"_ = {it}")

        def emit_for(loop):
            if cond:
                self._emit(f"for {cond} {{")
            else:
                self._emit("for {")
            self.indent += 1
            self._push_scope()
            body_start, vars_at_start = self._scope_unused_tracking_start()
            if range_info:
                for line in binds:
                    self._emit(line)
                self._emit(advance)
            else:
                if advance:
                    self._emit(advance)
                for line in binds:
                    self._emit(line)
            for name in declared:
                self._declare_var(name)
            self._sm_loop_body(loop, suite_node)
            self._pop_scope_with_unused_local_silencers(body_start, vars_at_start)
            self.indent -= 1
            self._emit("}")

        if owned:
            # A loop over a generator call owns it: leaving the loop in any
            # way (and the outer generator ending) stops it.
            self._sm_try(
                lambda kk: self._sm_loop_closure(head, kk, emit_for, else_suite),
                [], None,
                lambda kk: (self._emit(f"{it}.Stop()"), self._sm_goto(kk)),
                k,
            )
        else:
            self._sm_loop_closure(head, k, emit_for, else_suite)

    # ─── try / with ───────────────────────────────────────────

    def _sm_try(self, emit_body, clauses: list, else_suite, emit_finally, k: _K) -> None:
        """A ``try`` region: ``emit_body(k)`` / ``emit_finally(k)`` emit the
        body and the ``finally`` code continuing with ``k``."""
        sm = self._sm
        if not clauses and emit_finally is None:
            emit_body(k)
            return
        frame = sm.frame
        outer_h = sm.handler()
        after = k.in_closure()
        fin_region = None
        if emit_finally is not None:
            pend, exc = self._sm_new("Pend"), self._sm_new("Exc")
            fin, hfin, close = self._sm_new("Fin"), self._sm_new("HFin"), self._sm_new("Close")
            self._emit(f"var {pend} func(__lamF {frame}) int")
            self._emit(f"var {exc} any")
            self._emit(f"_, _ = {pend}, {exc}")
            self._sm_closure(fin, lambda: emit_finally(_K("resume", f"lamGenResume(__lamF, &{pend}, &{exc})")))
            self._emit(f"{hfin} := func(__lamF {frame}, r any) {{ {exc} = r; {pend} = nil; __lamF.k = {fin} }}")
            outer_close = sm.close()
            self._emit(
                f"{close} := func(__lamF {frame}) int {{ {pend} = {outer_close or f'lamGenEnd[{sm.elem}]'}; return {fin}(__lamF) }}"
            )
            self._emit(f"_, _ = {hfin}, {close}")
            fin_region = _Region("fin", hfin, close, pend, fin)
            after = _K("fin", (pend, fin, self._sm_value(after)))
        h_after = fin_region.handler if fin_region else outer_h
        if fin_region:
            sm.regions.append(fin_region)
        try:
            body_k = after
            if else_suite is not None:
                else_name = self._sm_new("Else")
                self._sm_closure(else_name, lambda: self._sm_stmts(self._sm_suite_stmts(else_suite), after))
                body_k = _K("name", else_name)
            body_h = h_after
            if clauses:
                body_h = self._sm_catch_handler(clauses, h_after, after)
                sm.regions.append(_Region("catch", body_h, sm.close()))
            try:
                body = self._sm_new("Try")
                self._sm_closure(body, lambda: emit_body(body_k))
            finally:
                if clauses:
                    sm.regions.pop()
        finally:
            if fin_region:
                sm.regions.pop()
        self._emit(f"__lamF.h = {body_h}")
        self._emit(f"__lamF.k = {body}")
        self._emit("return lamGenJump")

    def _sm_catch_handler(self, clauses: list, h_after: str, after: _K) -> str:
        """The handler of a ``try`` body with ``catch`` clauses: each clause
        is a continuation; an exception no clause matches goes to
        ``h_after`` (the ``finally``, or the enclosing region)."""
        sm = self._sm
        exc = self._sm_new("Caught")
        self._emit(f"var {exc} any")
        parsed = []
        for clause in clauses:
            exception_type, is_go_expr, as_name, suite = self._parse_catch_clause(clause)
            cond = self._catch_condition(exception_type, is_go_expr)
            class_type = exception_type if (cond is not None and not is_go_expr) else None
            name = self._sm_new("Catch")

            def emit_clause(as_name=as_name, suite=suite, class_type=class_type):
                self._emit(f"r := {exc}")
                self._emit(f"{exc} = nil")
                self._emit("_ = r")
                with self._scoped(_in_recover_block=True):
                    self._emit_catch_clause(
                        as_name, suite, class_type,
                        visit_body=lambda s: self._sm_stmts(self._sm_suite_stmts(s), after),
                    )
                if suite is None:
                    self._sm_goto(after)

            self._sm_closure(name, emit_clause)
            parsed.append((cond, name))
            if cond is None:
                break
        handler = self._sm_new("HCatch")
        self._emit(f"{handler} := func(__lamF {sm.frame}, r any) {{")
        self.indent += 1
        self._emit(f"__lamF.h = {h_after}")
        if any(cond and "__lamExcKind" in cond for cond, _ in parsed):
            self._emit_exception_kind()
        for cond, name in parsed:
            if cond is None:
                self._emit(f"{exc} = r")
                self._emit(f"__lamF.k = {name}")
                self._emit("return")
                break
            self._emit(f"if {cond} {{")
            self._emit(f"\t{exc} = r")
            self._emit(f"\t__lamF.k = {name}")
            self._emit("\treturn")
            self._emit("}")
        else:
            if h_after != "nil":
                self._emit(f"{h_after}(__lamF, r)")
            else:
                self._emit("panic(r)")
        self.indent -= 1
        self._emit("}")
        return handler

    def _sm_with(self, items: list, body, k: _K) -> None:
        """``with a as x, b { body }``: each resource opened in turn, then a
        ``finally`` region closing it."""
        if not items:
            self._sm_stmts(self._sm_suite_stmts(body), k)
            return
        item, rest = items[0], items[1:]
        expr = item.children[0]
        as_name = item.children[1] if len(item.children) > 1 and item.children[1] is not None else None
        expr_str = self._expr_to_go(expr)
        if as_name is not None:
            name_str = self._go_binding_name(self._get_name(as_name))
            resource_class = self._infer_receiver_class(expr)
            if resource_class:
                self._var_types[name_str] = resource_class
            else:
                self._var_types.pop(name_str, None)
            resource_type = self._expr_go_type_hint(expr)
            if resource_type:
                self._var_go_types[name_str] = resource_type
            else:
                self._var_go_types.pop(name_str, None)
        else:
            name_str = self._sm_new("With")
        self._emit(f"{name_str} := {expr_str}")
        self._emit(f"_ = {name_str}")
        self._declare_var(name_str)
        self._sm_try(
            lambda kk: self._sm_with(rest, body, kk),
            [], None,
            lambda kk: (self._emit(f"{name_str}.Close()"), self._sm_goto(kk)),
            k,
        )

    # ─── do / catch ───────────────────────────────────────────

    def _sm_do(self, node, k: _K) -> None:
        """``do { body } catch err { handler }`` holding a ``yield``: a ``?``
        failing in the body stores its ``*Result`` and runs the handler
        continuation (through the ``finally`` regions it leaves)."""
        sm = self._sm
        body_suite, err_name_node, handler_suite = node.children[0], node.children[1], node.children[2]
        err_name = self._go_binding_name(self._get_name(err_name_node))
        err_var = self._sm_new("DoErr")
        handler = self._sm_new("DoCatch")
        after = k.in_closure()
        self._emit(f"var {err_var} *Result")
        self._emit(f"_ = {err_var}")

        def emit_handler():
            self._emit_do_error_binding(err_name, err_var)
            self._sm_stmts(self._sm_suite_stmts(handler_suite), after)

        self._sm_closure(handler, emit_handler)
        sm.do_targets.append((err_var, handler, len(sm.regions)))
        try:
            with self._scoped(_q_propagate_ok=True, _sm_q_target=True):
                self._sm_stmts(self._sm_suite_stmts(body_suite), k)
        finally:
            sm.do_targets.pop()
