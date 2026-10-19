#!/usr/bin/env python3
"""Constants used across the transpiler modules."""

from typing import Dict, Set

# ─── Type mapping ──────────────────────────────────────────────
TYPE_MAP: Dict[str, str] = {
    "int": "int",
    "int8": "int8",
    "int16": "int16",
    "int32": "int32",
    "int64": "int64",
    "uint": "uint",
    "float": "float64",
    "float32": "float32",
    "float64": "float64",
    "string": "string",
    "str": "string",
    "bool": "bool",
    "byte": "byte",
    "rune": "rune",
    "None": "",
    "list": "[]interface{}",
    "dict": "map[string]interface{}",
    "set": "map[interface{}]bool",
    "json": "LamJSON",
    "any": "interface{}",
    "object": "interface{}",
    "error": "error",
    "bytes": "[]byte",
}

# Python exceptions → panic strings
PYTHON_EXCEPTIONS: Set[str] = {
    "ValueError", "TypeError", "KeyError", "IndexError",
    "RuntimeError", "AttributeError", "IOError", "OSError",
    "FileNotFoundError", "ZeroDivisionError", "OverflowError",
    "NotImplementedError", "StopIteration", "Exception",
    "ArithmeticError", "LookupError", "NameError",
}

# Statement-level AST node names (used for source-line mapping)
STMT_NODES = frozenset({
    'funcdef', 'classdef', 'if_stmt', 'for_stmt', 'while_stmt',
    'assign_stmt', 'annassign', 'const_stmt', 'expr_stmt',
    'return_stmt', 'try_stmt', 'do_stmt', 'with_stmt',
    'assert_stmt', 'raise_stmt', 'del_stmt', 'break_stmt',
    'continue_stmt', 'pass_stmt', 'match_stmt',
    'import_from', 'import_name', 'defer_stmt',
})

# Map Python operator tokens to dunder method names
OP_TO_DUNDER = {
    "+": "__add__", "-": "__sub__", "*": "__mul__",
    "/": "__truediv__", "//": "__floordiv__", "%": "__mod__",
    "**": "__pow__",
    "&": "__and__", "|": "__or__", "^": "__xor__",
}

CMP_TO_DUNDER = {
    "==": "__eq__", "!=": "__ne__",
    "<": "__lt__", "<=": "__le__",
    ">": "__gt__", ">=": "__ge__",
}

# Operator overloading dunder-to-Go mappings (used in funcdef visitor)
DUNDER_OPS = {
    "__add__": "Add", "__sub__": "Sub", "__mul__": "Mul",
    "__truediv__": "Div", "__floordiv__": "FloorDiv",
    "__mod__": "Mod", "__pow__": "Pow",
    "__eq__": "Eq", "__ne__": "Ne",
    "__lt__": "Lt", "__le__": "Le",
    "__gt__": "Gt", "__ge__": "Ge",
    "__neg__": "Neg", "__abs__": "Abs",
    "__and__": "And", "__or__": "Or", "__xor__": "Xor",
    "__invert__": "Invert",
    "__contains__": "Contains",
    "__getitem__": "GetItem", "__setitem__": "SetItem",
    # Postfix ``x++`` / ``x--``. The methods must return the
    # updated value because the transpiler rewrites the statement
    # to ``x = x.__inc__()`` / ``x = x.__dec__()`` — reusing the
    # same value-returning shape as the other overloads keeps
    # method emission uniform (no special void-returning path).
    "__inc__": "Inc", "__dec__": "Dec",
}

# Go stdlib packages that keep lowercase naming
LOWER_PKGS = frozenset({
    "fmt", "math", "os", "strconv", "errors", "strings",
    "io", "bufio", "sort", "time", "sync", "log",
    "path", "filepath", "net", "http", "json",
    "encoding", "bytes", "reflect", "runtime",
})

# Go builtins that should not be CamelCased
GO_BINDING_KEYWORDS = frozenset({
    "break", "default", "func", "interface", "select", "case", "defer",
    "go", "map", "struct", "chan", "else", "goto", "package", "switch",
    "const", "fallthrough", "if", "range", "type", "continue", "for",
    "import", "return", "var", "iota", "any",
})

GO_BUILTINS = frozenset({
    "len", "cap", "make", "new", "append", "copy", "delete",
    "close", "panic", "recover", "print", "println",
    "int", "float64", "string", "bool", "byte", "rune",
    "fmt", "math", "os", "strconv", "errors", "strings",
})


# Methods callable on a ``str`` value (lowered to ``lamstrings``) and on a
# ``list[T]`` value (lowered inline). Anything else is a semantic error.
STR_METHODS: Set[str] = {
    "repeat", "contains", "hasPrefix", "hasSuffix", "toUpper", "toLower", "trim",
    "length", "trimLeft", "trimRight", "replace", "split", "join", "startsWith",
    "endsWith", "index", "lastIndex", "count", "title", "equalFold", "fields",
    "capitalize", "isAlpha", "isDigit", "isAlnum", "isSpace", "reverse", "center",
    "zfill", "padLeft", "padRight", "splitLines", "splitN", "replaceFirst",
    "containsAny", "isEmpty", "isBlank", "indent", "dedent", "format",
}
# Result types of the ``str`` methods (everything else returns ``str``).
STR_METHOD_RESULT_TYPES: Dict[str, str] = {
    "split": "list[str]", "fields": "list[str]", "splitLines": "list[str]", "splitN": "list[str]",
    "contains": "bool", "hasPrefix": "bool", "hasSuffix": "bool", "startsWith": "bool",
    "endsWith": "bool", "equalFold": "bool", "isAlpha": "bool", "isDigit": "bool",
    "isAlnum": "bool", "isSpace": "bool", "containsAny": "bool", "isEmpty": "bool",
    "isBlank": "bool", "length": "int", "index": "int", "lastIndex": "int", "count": "int",
}
LIST_METHODS: Set[str] = {
    "length", "append", "pop", "extend", "insert", "sort", "map", "filter",
    "reduce", "any", "all", "foreach",
}
# Python spellings with the Lam method (or idiom) they correspond to.
STR_METHOD_HINTS: Dict[str, str] = {
    "find": "index", "rfind": "lastIndex", "rindex": "lastIndex", "upper": "toUpper",
    "lower": "toLower", "strip": "trim", "lstrip": "trimLeft", "rstrip": "trimRight",
    "startswith": "startsWith", "endswith": "endsWith", "splitlines": "splitLines",
    "isalpha": "isAlpha", "isdigit": "isDigit", "isalnum": "isAlnum", "isspace": "isSpace",
    "casefold": "toLower", "len": "length", "size": "length",
    # JavaScript / Java spellings.
    "indexOf": "index", "lastIndexOf": "lastIndex", "toUpperCase": "toUpper",
    "toLowerCase": "toLower", "includes": "contains", "trimStart": "trimLeft",
    "trimEnd": "trimRight", "replaceAll": "replace", "padStart": "padLeft",
    "padEnd": "padRight",
}
# Methods other languages have on strings that Lam spells as an operator,
# slicing or indexing. ``{s}`` is the receiver, ``{a}``/``{b}`` the first
# two arguments (rendered by ``str_method_idiom``).
STR_METHOD_IDIOMS: Dict[str, str] = {
    "substring": "slicing `{s}[{a}:{b}]`",
    "substr": "slicing `{s}[{a}:{a} + {b}]` (`substr` takes a length)",
    "slice": "slicing `{s}[{a}:{b}]`",
    "charAt": "indexing `{s}[{a}]`",
    "at": "indexing `{s}[{a}]`",
    "concat": "`{s} + {a}`",
    "equals": "`{s} == {a}`",
    "toString": "the value directly (it is already a `str`)",
    "chars": "`for c in {s}`",
}
_STR_IDIOM_PLACEHOLDERS: Dict[str, tuple] = {
    "substring": ("start", "end"), "slice": ("start", "end"), "substr": ("start", "length"),
    "charAt": ("i", ""), "at": ("i", ""), "concat": ("other", ""), "equals": ("other", ""),
}


def lam_simple_expr_text(node) -> str:
    """Lam source text of a short argument/receiver node (a name, a dotted
    name, a number or a short string literal), or ``""`` when the node is
    not that simple. Works on Lark trees without importing Lark."""
    data = getattr(node, "data", None)
    children = list(getattr(node, "children", []) or [])
    if data is None:
        return str(node) if node is not None else ""
    if data in {"var", "name", "number"} and children:
        return lam_simple_expr_text(children[0])
    if data == "argvalue" and len(children) == 1:
        return lam_simple_expr_text(children[0])
    if data in {"getattr", "getattr_safe"} and len(children) >= 2:
        prefix = lam_simple_expr_text(children[0])
        attr = lam_simple_expr_text(children[1])
        return f"{prefix}.{attr}" if prefix and attr else ""
    if data == "string" and children:
        text = str(children[0])
        return text if len(text) <= 24 and "\n" not in text else ""
    if data == "factor" and len(children) == 2 and getattr(children[0], "data", None) is None:
        inner = lam_simple_expr_text(children[1])
        return f"{children[0]}{inner}" if inner else ""
    if data == "funccall" and children:
        # ``Json.encode(v)``: a call whose callee and arguments are simple.
        callee = lam_simple_expr_text(children[0])
        args_node = children[1] if len(children) > 1 else None
        if args_node is None:
            arg_nodes = []
        elif getattr(args_node, "data", None) == "arguments":
            arg_nodes = [arg for arg in args_node.children if arg is not None]
        else:
            arg_nodes = [args_node]
        arg_texts = [lam_simple_expr_text(arg) for arg in arg_nodes]
        if not callee or not all(arg_texts):
            return ""
        return f"{callee}({', '.join(arg_texts)})"
    return ""


def str_method_idiom(method: str, receiver: str = "", args=None) -> str:
    """The Lam spelling of a string method other languages have
    (``label.substring(0, 2)`` -> slicing ``label[0:2]``), or ``""``."""
    template = STR_METHOD_IDIOMS.get(method)
    if not template:
        return ""
    args = list(args or [])
    first, second = _STR_IDIOM_PLACEHOLDERS.get(method, ("x", "y"))
    a = args[0] if args and args[0] else first
    b = args[1] if len(args) > 1 and args[1] else second
    s = receiver or "s"
    if method in {"substring", "slice", "substr"} and len(args) == 1 and args[0]:
        return f"slicing `{s}[{a}:]`"
    if method == "substr" and a == "0":
        return f"slicing `{s}[0:{b}]`"
    return template.format(s=s, a=a, b=b)


def unknown_str_method_message(method: str, receiver: str = "", args=None) -> str:
    """Diagnostic for a method a ``str`` does not have, with the Lam idiom
    or the closest Lam method as a suggestion. Shared by the semantic
    checker and the transpiler (which reports it for imported modules)."""
    from difflib import get_close_matches

    idiom = str_method_idiom(method, receiver, args)
    if idiom:
        suffix = f"; use {idiom}"
    else:
        hint = STR_METHOD_HINTS.get(method)
        if not hint:
            matches = get_close_matches(method, sorted(STR_METHODS), n=1, cutoff=0.75)
            hint = matches[0] if matches else ""
        suffix = f" — did you mean `{hint}`?" if hint else ""
        if method in {"find", "rfind"}:
            suffix = f" — did you mean `{hint}` (returns -1 when absent)?"
    return f"`str` has no method `{method}`{suffix}"


LIST_METHOD_HINTS: Dict[str, str] = {
    "add": "append", "push": "append", "size": "length", "len": "length",
    "forEach": "foreach", "each": "foreach", "some": "any", "every": "all",
    "sorted": "sort",
}
LIST_METHOD_IDIOMS: Dict[str, str] = {
    "contains": "`x in xs`", "index": "a loop (or `x in xs`)", "count": "a loop or `len(xs.filter(...))`",
    "remove": "`xs = xs.filter(lambda v: v != x)`", "reverse": "`xs[::-1]`", "copy": "`xs[:]`",
    "clear": "`xs = []`", "join": "`\", \".join(xs)` on a `list[str]`",
    "first": "`xs[0]`", "last": "`xs[-1]`", "isEmpty": "`len(xs) == 0`",
}
