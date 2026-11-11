"""``x()?.y`` on a ``Result``: propagation, then member access.

The lexer reads ``f()?.method()`` as ``f()`` followed by the token ``?.``
(``getattr_safe``). Lam has no safe navigation (removed after 4.11): on a
``Result`` the author means ``(f()?).method()`` (propagate the error, then use
the payload). The semantic checker calls ``rewrite_result_safe_nav`` on each
statement before analysing it, with its type oracle, and reports every
``?.`` left (a receiver that is not a ``Result``); the transpiler, which only
sees checked code, rewrites every ``?.`` (``rewrite_safe_nav_as_propagation``),
so the rest of the pipeline only sees the ``propagate`` + ``getattr`` shape.
"""

from __future__ import annotations

from typing import Callable

from lark import Tree

# Nodes whose subtrees are statements or callables of their own: they are
# rewritten when they are visited, with their own scope.
_STOP = frozenset({"suite", "funcdef", "classdef", "lambdef", "go_block", "go_inline"})


def rewrite_result_safe_nav(node, is_result: Callable[[Tree], bool]) -> None:
    """Rewrite ``getattr_safe(recv, name)`` into
    ``getattr(propagate(recv), name)`` below ``node`` where ``is_result(recv)``
    holds. Receivers are rewritten first, so a chain
    ``a()?.b()?.c()`` is typed link by link."""
    if not isinstance(node, Tree):
        return
    stack = [(node, False)]
    while stack:
        cur, done = stack.pop()
        if not done:
            stack.append((cur, True))
            for child in reversed(cur.children):
                if isinstance(child, Tree) and child.data not in _STOP:
                    stack.append((child, False))
            continue
        if cur.data != "getattr_safe" or len(cur.children) < 2:
            continue
        receiver = cur.children[0]
        if isinstance(receiver, Tree) and is_result(receiver):
            cur.data = "getattr"
            cur.children[0] = Tree("propagate", [receiver], meta=getattr(receiver, "_meta", None))


def rewrite_safe_nav_as_propagation(node) -> None:
    """Every ``getattr_safe`` below ``node`` as ``getattr(propagate(recv),
    name)`` (the checker rejected the others)."""
    rewrite_result_safe_nav(node, lambda _recv: True)
