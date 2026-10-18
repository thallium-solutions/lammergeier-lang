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
}
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
