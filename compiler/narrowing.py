#!/usr/bin/env python3
"""isinstance narrowing: where a test is known to hold (shared analysis).

The semantic checker and the transpiler both narrow a local tested with
``isinstance(x, T)``; this module holds the purely syntactic part they
share, so both agree on *where* a fact holds:

* :func:`condition_facts` -- the ``(name, type)`` facts a condition
  establishes when it is true (or false): ``isinstance(x, T)``, ``not``,
  ``and`` (true: every operand), ``or`` (false: every operand), and
  ``isinstance(x, None)`` whose negation is the :data:`NOT_NONE` fact;
* :func:`operand_facts` -- facts holding while an operand of ``and`` /
  ``or`` is evaluated (``isinstance(x, T) and x.f``);
* :func:`if_branch_facts` / :func:`guard_facts` -- facts of each branch of
  an ``if`` / ``elif`` / ``else`` chain and of the statements after an
  ``if`` whose branches all exit (``return`` / ``raise`` / ``continue`` /
  ``break``: an early-exit guard);
* :func:`assigned_names` / :func:`mutated_names` / :func:`go_block_names`
  -- what a region does to a name, so a variable assigned in the region
  (or read by a ``go!`` block) is not narrowed there.

Only plain names are subjects (``isinstance(self.shape, T)`` narrows
nothing: an attribute can change behind the test). How a fact turns into
a type (and into Go) is up to each side.
"""

from __future__ import annotations

import re
from typing import Iterable, List, Optional, Sequence, Set, Tuple

from lark import Token, Tree

#: Fact target for "not ``None``" (the false side of ``isinstance(x, None)``).
NOT_NONE = "<not None>"

Fact = Tuple[str, object]

_EXIT_STMTS = {"return_stmt", "raise_stmt", "break_stmt", "continue_stmt"}
_SCOPE_NODES = {"funcdef", "lambdef", "classdef"}


def _name(node) -> str:
    if isinstance(node, Token):
        return str(node)
    if isinstance(node, Tree):
        if node.data in {"name", "var"} and node.children:
            return _name(node.children[0])
        if node.data == "dotted_name" and node.children:
            return ".".join(_name(c) for c in node.children)
    return ""


def _call_args(node: Tree) -> list:
    if len(node.children) < 2 or not isinstance(node.children[1], Tree):
        return []
    args = node.children[1]
    if args.data != "arguments":
        return [args]
    return [c for c in args.children if c is not None]


def is_none_type(node) -> bool:
    if isinstance(node, Tree) and node.data == "const_none":
        return True
    return _name(node) == "None"


def isinstance_call(node) -> Optional[Tuple[object, object]]:
    """``(subject, type_node)`` of an ``isinstance(subject, T)`` call."""
    if not (isinstance(node, Tree) and node.data == "funccall" and node.children):
        return None
    callee = node.children[0]
    if not (isinstance(callee, Tree) and callee.data == "var" and _name(callee) == "isinstance"):
        return None
    args = _call_args(node)
    if len(args) != 2 or any(isinstance(a, Tree) and a.data == "argvalue" and len(a.children) == 2 for a in args):
        return None
    return args[0], args[1]


def subject_name(subject) -> str:
    """The narrowable name of an isinstance subject (plain locals only)."""
    if isinstance(subject, Tree) and subject.data == "var" and subject.children:
        name = _name(subject)
        if name and name not in {"self", "base", "true", "false", "nil"}:
            return name
    return ""


def condition_facts(cond, truth: bool = True) -> List[Fact]:
    """Facts that hold when ``cond`` evaluates to ``truth``."""
    if not isinstance(cond, Tree):
        return []
    d = cond.data
    if d == "not_test" and cond.children:
        return condition_facts(cond.children[0], not truth)
    if d == "and_test":
        if not truth:
            return []
        out: List[Fact] = []
        for child in cond.children:
            out.extend(condition_facts(child, True))
        return out
    if d == "or_test":
        if truth:
            return []
        out = []
        for child in cond.children:
            out.extend(condition_facts(child, False))
        return out
    call = isinstance_call(cond)
    if call is None:
        return []
    subject, type_node = call
    name = subject_name(subject)
    if not name:
        return []
    if is_none_type(type_node):
        return [] if truth else [(name, NOT_NONE)]
    return [(name, type_node)] if truth else []


def _dotted(node) -> str:
    if isinstance(node, Tree) and node.data == "var":
        return _name(node)
    if isinstance(node, Tree) and node.data in {"getattr", "getattr_safe"} and len(node.children) >= 2:
        left = _dotted(node.children[0])
        return f"{left}.{_name(node.children[1])}" if left else ""
    return ""


def attribute_subjects(cond, truth: bool = True) -> List[str]:
    """Dotted texts of field subjects (``self.shape``) tested positively
    when ``cond`` is ``truth``: they are not narrowed (for diagnostics)."""
    if not isinstance(cond, Tree):
        return []
    d = cond.data
    if d == "not_test" and cond.children:
        return attribute_subjects(cond.children[0], not truth)
    if d in {"and_test", "or_test"}:
        if truth != (d == "and_test"):
            return []
        out: List[str] = []
        for child in cond.children:
            out.extend(attribute_subjects(child, truth))
        return out
    call = isinstance_call(cond)
    if call is None or not truth or is_none_type(call[1]):
        return []
    subject = call[0]
    if isinstance(subject, Tree) and subject.data in {"getattr", "getattr_safe"}:
        dotted = _dotted(subject)
        return [dotted] if dotted else []
    return []


def contains_propagate(node) -> bool:
    """A ``?`` in ``node`` (outside nested lambdas / functions)."""
    if not isinstance(node, Tree):
        return False
    if node.data == "propagate":
        return True
    if node.data in _SCOPE_NODES:
        return False
    return any(contains_propagate(c) for c in node.children)


def operand_facts(node: Tree, index: int) -> List[Fact]:
    """Facts holding while operand ``index`` of an ``and`` / ``or`` runs.

    An operand holding a ``?`` is not narrowed: its propagation runs as
    statements of the enclosing function, outside the operand."""
    operands = [c for c in node.children if isinstance(c, Tree)]
    if index < len(operands) and contains_propagate(operands[index]):
        return []
    truth = node.data == "and_test"
    out: List[Fact] = []
    for child in operands[:index]:
        out.extend(condition_facts(child, truth))
    return out


def ternary_facts(node: Tree) -> Tuple[List[Fact], List[Fact]]:
    """Facts of the ``a`` and ``b`` branches of ``a if cond else b``."""
    cond = node.children[1]
    true_facts = [] if contains_propagate(node.children[0]) else condition_facts(cond, True)
    false_facts = [] if contains_propagate(node.children[2]) else condition_facts(cond, False)
    return true_facts, false_facts


def if_parts(node: Tree) -> Tuple[List[Tuple[object, Tree]], Optional[Tree]]:
    """``([(cond, suite), ...], else_suite)`` of an ``if_stmt``."""
    branches: List[Tuple[object, Tree]] = []
    children = list(node.children)
    if len(children) >= 2:
        branches.append((children[0], children[1]))
    elifs = children[2] if len(children) > 2 else None
    if isinstance(elifs, Tree) and elifs.data == "elifs":
        for elif_node in elifs.children:
            if isinstance(elif_node, Tree) and elif_node.data == "elif_" and len(elif_node.children) >= 2:
                branches.append((elif_node.children[0], elif_node.children[1]))
    else_suite = children[3] if len(children) > 3 and isinstance(children[3], Tree) else None
    return branches, else_suite


def if_branch_facts(node: Tree) -> Tuple[List[List[Fact]], List[Fact]]:
    """Facts of each ``if`` / ``elif`` branch and of the ``else`` branch."""
    branches, _else = if_parts(node)
    out: List[List[Fact]] = []
    previous: List[Fact] = []
    for cond, _suite in branches:
        out.append(previous + condition_facts(cond, True))
        previous = previous + condition_facts(cond, False)
    return out, previous


def suite_exits(suite) -> bool:
    """The block always leaves its enclosing block (``return``, ``raise``,
    ``continue``, ``break``, or an ``if`` / ``else`` whose branches all do)."""
    for stmt in suite_statements(suite):
        if stmt.data in _EXIT_STMTS:
            return True
        if stmt.data == "if_stmt":
            branches, else_suite = if_parts(stmt)
            if else_suite is not None and suite_exits(else_suite) and all(
                suite_exits(s) for _c, s in branches
            ):
                return True
    return False


def guard_facts(node: Tree) -> List[Fact]:
    """Facts holding after an ``if`` statement: when every ``if`` / ``elif``
    branch exits, only the (possibly implicit) ``else`` path continues."""
    branches, else_suite = if_parts(node)
    if not branches or not all(suite_exits(suite) for _cond, suite in branches):
        return []
    if else_suite is not None and suite_exits(else_suite):
        return []
    return if_branch_facts(node)[1]


def suite_statements(suite) -> List[Tree]:
    """Statements of a block, ``simple_stmt`` groups flattened."""
    if not isinstance(suite, Tree):
        return []
    items = suite.children if suite.data == "suite" else [suite]
    out: List[Tree] = []
    for item in items:
        if not isinstance(item, Tree):
            continue
        if item.data in {"simple_stmt", "assign_stmt"}:
            out.extend(c for c in item.children if isinstance(c, Tree))
        else:
            out.append(item)
    return out


# ─── What a region does to a name ─────────────────────────────


def _target_names(node, out: Set[str]) -> None:
    """Names bound by an assignment target (``a``, ``a, b``, ``*rest``)."""
    if isinstance(node, Token):
        if node.type == "NAME":
            out.add(str(node))
        return
    if not isinstance(node, Tree):
        return
    if node.data in {"var", "name"}:
        name = _name(node)
        if name:
            out.add(name)
        return
    if node.data in {"tuple", "list", "exprlist", "testlist_tuple", "star_expr",
                     "untyped_for_target", "typed_for_target", "testlist_star_expr"}:
        for child in node.children:
            if isinstance(child, Tree) and child.data == "type_expr":
                continue
            _target_names(child, out)


def target_names(node) -> Set[str]:
    """Names an assignment / loop target binds."""
    out: Set[str] = set()
    _target_names(node, out)
    return out


#: Node kinds :func:`direct_bindings` may return names for.
BINDING_NODES = frozenset({
    "assign", "augassign", "annassign", "inc_stmt", "dec_stmt", "del_stmt", "global_stmt",
    "nonlocal_stmt", "for_stmt", "comp_for", "assign_expr", "with_item", "catch_clause",
    "capture_pattern", "star_pattern", "as_pattern", "mapping_star_pattern", "typed_param",
    "typed_lambda_param", "inline_lambda_param", "lambda_param", "funcdef",
})


def direct_bindings(node) -> Set[str]:
    """Names ``node`` itself binds (not its children): assignment and loop
    targets, ``with ... as``, ``catch ... as``, pattern captures,
    parameters, ``global`` / ``nonlocal``, a nested function's name."""
    out: Set[str] = set()
    if not isinstance(node, Tree):
        return out
    d = node.data
    if d in {"assign", "augassign", "annassign"}:
        children = [c for c in node.children if c is not None]
        if d == "assign":
            targets = children[:-1]
        elif d == "augassign":
            targets = children[:1]
        else:
            targets = [next((c for c in children if isinstance(c, Tree)), None)]
        for target in targets:
            _target_names(target, out)
    elif d in {"inc_stmt", "dec_stmt", "del_stmt", "global_stmt", "nonlocal_stmt"}:
        for child in node.children:
            _target_names(child, out)
    elif d in {"for_stmt", "comp_for", "assign_expr"} and node.children:
        _target_names(node.children[0], out)
    elif d == "with_item" and len(node.children) > 1:
        _target_names(node.children[1], out)
    elif d == "catch_clause":
        for child in node.children:
            if isinstance(child, Tree) and child.data == "name":
                _target_names(child, out)
        first = next((c for c in node.children if c is not None), None)
        if isinstance(first, Tree) and first.data == "var" and _name(first)[:1].islower():
            out.add(_name(first))  # ``catch e`` binds ``e``
    elif d in {"capture_pattern", "star_pattern", "as_pattern", "mapping_star_pattern"}:
        for child in node.children:
            if isinstance(child, Token) and child.type == "NAME":
                out.add(str(child))
    elif d in {"typed_param", "typed_lambda_param", "inline_lambda_param", "lambda_param"}:
        if node.children:
            _target_names(node.children[0], out)
    elif d == "funcdef":
        for child in node.children:
            if isinstance(child, Tree) and child.data == "name":
                out.add(_name(child))
                break
    return out


def assigned_names(nodes: Iterable, nested: bool = True) -> Set[str]:
    """Every name (re)bound anywhere in ``nodes``, nested functions and
    lambdas included (a closure assigning a captured local counts) unless
    ``nested`` is false."""
    out: Set[str] = set()
    stack = [n for n in nodes if isinstance(n, Tree)]
    while stack:
        node = stack.pop()
        if not nested and node.data in _SCOPE_NODES:
            continue
        if node.data in BINDING_NODES:
            out |= direct_bindings(node)
        stack.extend([c for c in node.children if isinstance(c, Tree)])
    return out


_MUTATING_METHODS = {
    "append", "extend", "insert", "pop", "remove", "clear", "sort", "reverse",
    "update", "setdefault", "popitem", "add", "discard",
}


def mutated_names(nodes: Iterable) -> Set[str]:
    """Names whose contents ``nodes`` change in place (``x[k] = v``,
    ``x.append(v)``, ...): a converted collection view would miss them."""
    out: Set[str] = set()
    stack = [n for n in nodes if isinstance(n, Tree)]
    while stack:
        node = stack.pop()
        d = node.data
        if d in {"assign", "augassign", "annassign"} and node.children:
            target = node.children[0]
            while isinstance(target, Tree) and target.data in {"getitem", "getattr"}:
                target = target.children[0]
                if isinstance(target, Tree) and target.data == "var":
                    out.add(_name(target))
        elif d == "funccall" and node.children:
            callee = node.children[0]
            if (
                isinstance(callee, Tree) and callee.data in {"getattr", "getattr_safe"}
                and len(callee.children) >= 2 and _name(callee.children[1]) in _MUTATING_METHODS
                and isinstance(callee.children[0], Tree) and callee.children[0].data == "var"
            ):
                out.add(_name(callee.children[0]))
        stack.extend(c for c in node.children if isinstance(c, Tree))
    return out


_GO_IDENT_RE = re.compile(r"[A-Za-z_]\w*")


def go_block_names(nodes: Iterable, go_blocks: Optional[dict]) -> Set[str]:
    """Identifiers appearing in the raw Go of ``go!`` blocks in ``nodes``."""
    out: Set[str] = set()
    if not go_blocks:
        return out
    stack = [n for n in nodes if isinstance(n, Tree)]
    while stack:
        node = stack.pop()
        if node.data == "funccall" and node.children and _name(node.children[0]) in {"__go_block__", "__go_inline__"}:
            for arg in _call_args(node):
                if isinstance(arg, Tree) and arg.data == "string" and arg.children:
                    key = str(arg.children[0]).strip("\"'")
                    out.update(_GO_IDENT_RE.findall(go_blocks.get(key, "")))
            continue
        stack.extend(c for c in node.children if isinstance(c, Tree))
    return out


def blocked_names(nodes: Sequence, go_blocks: Optional[dict]) -> Set[str]:
    """Names that cannot be narrowed over ``nodes``: assigned there or read
    by a ``go!`` block (raw Go expects the declared Go type)."""
    return assigned_names(nodes) | go_block_names(nodes, go_blocks)


_DECL_NODES = {"funcdef", "classdef", "interfacedef", "decorated", "import_stmt",
               "import_name", "import_from"}


def module_variable_names(tree) -> Set[str]:
    """Names bound by module-level statements (globals, constants, names
    first assigned in top-level blocks): isinstance never narrows them."""
    out: Set[str] = set()
    if not isinstance(tree, Tree):
        return out
    for stmt in tree.children:
        if not isinstance(stmt, Tree):
            continue
        parts = stmt.children if stmt.data == "simple_stmt" else [stmt]
        for part in parts:
            if not isinstance(part, Tree) or part.data in _DECL_NODES:
                continue
            if part.data == "const_stmt" and part.children:
                _target_names(part.children[0], out)
                continue
            out |= assigned_names([part], nested=False)
    return out


def nonlocal_names(tree) -> Set[str]:
    """Names some nested function rebinds (``nonlocal x``): a call inside a
    narrowed region could change them, so they are never narrowed."""
    out: Set[str] = set()
    stack = [tree] if isinstance(tree, Tree) else []
    while stack:
        node = stack.pop()
        if node.data == "nonlocal_stmt":
            out |= direct_bindings(node)
        stack.extend(c for c in node.children if isinstance(c, Tree))
    return out


def type_alternatives(type_node) -> List[object]:
    """``(A, B)`` -> ``[A, B]``; a single type -> ``[type_node]``."""
    if isinstance(type_node, Tree) and type_node.data == "tuple":
        return [c for c in type_node.children if c is not None]
    return [type_node]


def type_target_name(type_node) -> str:
    """``Square`` / ``int`` / ``mod.Cls`` (``list[int]`` -> ``list``)."""
    if isinstance(type_node, Tree) and type_node.data == "getitem" and type_node.children:
        return type_target_name(type_node.children[0])
    if isinstance(type_node, Tree) and type_node.data == "getattr" and len(type_node.children) >= 2:
        return _name(type_node.children[1])
    if isinstance(type_node, Tree) and type_node.data == "const_none":
        return "None"
    return _name(type_node)
