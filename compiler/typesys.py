"""Canonical Lammergeier type model and lowering helpers."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable, Sequence

from lark import Token, Tree

from compiler.constants import TYPE_MAP


class Type:
    """Base class for semantic Lammergeier types."""


@dataclass(frozen=True)
class NamedType(Type):
    name: str


@dataclass(frozen=True)
class ListType(Type):
    item: Type


@dataclass(frozen=True)
class DictType(Type):
    key: Type
    value: Type


@dataclass(frozen=True)
class FuncType(Type):
    params: tuple[Type, ...]
    ret: Type


@dataclass(frozen=True)
class UnionType(Type):
    options: tuple[Type, ...]


@dataclass(frozen=True)
class GenericType(Type):
    base: str
    args: tuple[Type, ...]


class TypeParseError(ValueError):
    pass


def parse_type(value: Type | Tree | Token | str | None) -> Type:
    """Parse a Lammergeier type expression into the canonical type model."""

    if isinstance(value, Type):
        return value
    if value is None:
        return NamedType("None")
    if isinstance(value, Token):
        return NamedType(str(value))
    if isinstance(value, Tree):
        return _parse_lark_type(value)
    if isinstance(value, str):
        parser = _TextTypeParser(value)
        parsed = parser.parse()
        return parsed
    raise TypeParseError(f"unsupported type value: {value!r}")


def render_type(type_: Type) -> str:
    """Render a canonical type back to Lammergeier annotation syntax."""

    if isinstance(type_, NamedType):
        return type_.name
    if isinstance(type_, ListType):
        return f"list[{render_type(type_.item)}]"
    if isinstance(type_, DictType):
        return f"dict[{render_type(type_.key)}, {render_type(type_.value)}]"
    if isinstance(type_, FuncType):
        params = ", ".join(render_type(param) for param in type_.params)
        ret = render_type(type_.ret)
        return f"func({params})" if ret == "None" else f"func({params}) -> {ret}"
    if isinstance(type_, UnionType):
        return " | ".join(render_type(option) for option in type_.options)
    if isinstance(type_, GenericType):
        args = ", ".join(render_type(arg) for arg in type_.args)
        return f"{type_.base}[{args}]"
    raise TypeParseError(f"unsupported type object: {type_!r}")


def type_to_lam_name(type_: Type) -> str:
    return render_type(type_)


def is_assignable(expected: Type, actual: Type) -> bool:
    """Return True when a value of ``actual`` can be assigned to ``expected``."""

    if isinstance(expected, NamedType) and expected.name in {"any", "object"}:
        return True
    if isinstance(actual, NamedType) and actual.name in {"any", "object"}:
        return True
    if isinstance(actual, NamedType) and actual.name == "None":
        if isinstance(expected, NamedType):
            return expected.name in {"None", "any", "object"}
        if isinstance(expected, UnionType):
            return any(is_assignable(option, actual) for option in expected.options)
        return False
    if isinstance(expected, UnionType):
        return any(is_assignable(option, actual) for option in expected.options)
    if isinstance(actual, UnionType):
        return all(is_assignable(expected, option) for option in actual.options)
    if isinstance(expected, ListType) and isinstance(actual, NamedType) and actual.name == "list":
        return True
    if isinstance(expected, DictType) and isinstance(actual, NamedType) and actual.name == "dict":
        return True
    if isinstance(expected, GenericType) and isinstance(actual, NamedType) and actual.name == expected.base:
        return expected.base in {"set", "tuple"}
    if isinstance(expected, ListType) and isinstance(actual, ListType):
        return is_assignable(expected.item, actual.item)
    if isinstance(expected, DictType) and isinstance(actual, DictType):
        return (
            is_assignable(expected.key, actual.key)
            and is_assignable(expected.value, actual.value)
        )
    if isinstance(expected, FuncType) and isinstance(actual, FuncType):
        return expected == actual
    if isinstance(expected, GenericType) and isinstance(actual, GenericType):
        return expected == actual
    if isinstance(expected, NamedType) and isinstance(actual, NamedType):
        return _named_type_assignable(expected.name, actual.name)
    return False


def type_to_go(
    type_: Type,
    *,
    generic_names: Iterable[str] | None = None,
    interfaces: Iterable[str] | None = None,
    generic_classes: Iterable[str] | None = None,
) -> str:
    """Lower a canonical Lammergeier type to the Go spelling used today."""

    generic_name_set = set(generic_names or ())
    interface_set = set(interfaces or ())
    generic_class_set = set(generic_classes or ())
    return _type_to_go(
        type_,
        generic_names=generic_name_set,
        interfaces=interface_set,
        generic_classes=generic_class_set,
    )


def _type_to_go(
    type_: Type,
    *,
    generic_names: set[str],
    interfaces: set[str],
    generic_classes: set[str],
) -> str:
    if isinstance(type_, NamedType):
        name = type_.name
        if name in TYPE_MAP:
            return TYPE_MAP[name]
        if name in generic_names:
            return name
        go_name = _go_public_name(name)
        if name in interfaces:
            return go_name
        return "*" + go_name
    if isinstance(type_, ListType):
        return f"[]{_type_to_go(type_.item, generic_names=generic_names, interfaces=interfaces, generic_classes=generic_classes) or 'interface{}'}"
    if isinstance(type_, DictType):
        key = _type_to_go(type_.key, generic_names=generic_names, interfaces=interfaces, generic_classes=generic_classes) or "string"
        value = _type_to_go(type_.value, generic_names=generic_names, interfaces=interfaces, generic_classes=generic_classes) or "interface{}"
        return f"map[{key}]{value}"
    if isinstance(type_, FuncType):
        params = [
            _type_to_go(param, generic_names=generic_names, interfaces=interfaces, generic_classes=generic_classes) or "interface{}"
            for param in type_.params
        ]
        ret = _type_to_go(type_.ret, generic_names=generic_names, interfaces=interfaces, generic_classes=generic_classes)
        return f"func({', '.join(params)}) {ret}" if ret else f"func({', '.join(params)})"
    if isinstance(type_, UnionType):
        options = [_type_to_go(option, generic_names=generic_names, interfaces=interfaces, generic_classes=generic_classes) for option in type_.options]
        options = [option for option in options if option]
        if len(options) == len(type_.options):
            return options[0] if len(options) == 1 else "interface{}"
        return ""
    if isinstance(type_, GenericType):
        args = [
            _type_to_go(arg, generic_names=generic_names, interfaces=interfaces, generic_classes=generic_classes) or "interface{}"
            for arg in type_.args
        ]
        if type_.base == "tuple":
            return "[]interface{}"
        if type_.base == "optional":
            return f"*{args[0]}" if args else "interface{}"
        if type_.base == "chan":
            return f"chan {args[0]}" if args else "chan interface{}"
        if type_.base in generic_classes:
            return f"*{_go_public_name(type_.base)}[{', '.join(args)}]"
        return "interface{}"
    raise TypeParseError(f"unsupported type object: {type_!r}")


def _parse_lark_type(node: Tree | Token | None) -> Type:
    if node is None:
        return NamedType("None")
    if isinstance(node, Token):
        return NamedType(str(node))

    data = node.data
    if data in {"type_expr", "single_return_type", "return_type"}:
        return _first_child_type(node)
    if data == "type_union":
        options = [_parse_lark_type(child) for child in node.children if isinstance(child, (Tree, Token))]
        return _union(options)
    if data == "type_none":
        return NamedType("None")
    if data == "type_name":
        return NamedType(_dotted_name_to_text(node.children[0]) if node.children else "")
    if data == "type_generic":
        base = _dotted_name_to_text(node.children[0]) if node.children else ""
        args = tuple(_parse_lark_type(child) for child in node.children[1:] if isinstance(child, (Tree, Token)))
        return _generic_type(base, args)
    if data == "type_func":
        params: list[Type] = []
        ret: Type = NamedType("None")
        for child in node.children:
            if not isinstance(child, Tree):
                continue
            if child.data == "type_func_params":
                params = [
                    _parse_lark_type(param)
                    for param in child.children
                    if isinstance(param, (Tree, Token))
                ]
            else:
                ret = _parse_lark_type(child)
        return FuncType(tuple(params), ret)
    if data == "type_func_params":
        return _union(_parse_lark_type(child) for child in node.children if isinstance(child, (Tree, Token)))
    if data == "dotted_name":
        return NamedType(_dotted_name_to_text(node))
    if data == "name":
        return NamedType(_name_text(node))
    if data == "type_subscript" and node.children:
        base = render_type(_parse_lark_type(node.children[0]))
        args = tuple(_parse_lark_type(child) for child in node.children[1:] if isinstance(child, (Tree, Token)))
        return _generic_type(base, args)
    return _first_child_type(node)


def _first_child_type(node: Tree) -> Type:
    for child in node.children:
        if isinstance(child, (Tree, Token)):
            return _parse_lark_type(child)
    return NamedType("None")


def _generic_type(base: str, args: Sequence[Type]) -> Type:
    if base == "list":
        return ListType(args[0] if args else NamedType("any"))
    if base == "dict":
        key = args[0] if len(args) >= 1 else NamedType("str")
        value = args[1] if len(args) >= 2 else NamedType("any")
        return DictType(key, value)
    return GenericType(base, tuple(args))


def _union(options: Iterable[Type]) -> Type:
    flat: list[Type] = []
    for option in options:
        if isinstance(option, UnionType):
            flat.extend(option.options)
        else:
            flat.append(option)
    if not flat:
        return NamedType("None")
    if len(flat) == 1:
        return flat[0]
    return UnionType(tuple(flat))


def _dotted_name_to_text(node: Tree | Token) -> str:
    if isinstance(node, Token):
        return str(node)
    if node.data == "dotted_name":
        return ".".join(_name_text(child) for child in node.children if _name_text(child))
    return _name_text(node)


def _name_text(node: Tree | Token) -> str:
    if isinstance(node, Token):
        return str(node)
    if node.data == "name" and node.children:
        return str(node.children[0])
    for child in node.children:
        if isinstance(child, (Tree, Token)):
            text = _name_text(child)
            if text:
                return text
    return ""


def _go_public_name(name: str) -> str:
    if not name:
        return name
    if name.startswith("__") and name.endswith("__"):
        clean = name.strip("_")
        return clean[0].upper() + clean[1:] if clean else name
    if name.startswith("_"):
        return name
    return name[0].upper() + name[1:]


def _named_type_assignable(expected: str, actual: str) -> bool:
    expected = "str" if expected == "string" else expected
    actual = "str" if actual == "string" else actual
    if expected == actual:
        return True
    if expected in {"any", "object"}:
        return True
    if expected in {"int", "int8", "int16", "int32", "int64",
                    "uint", "uint8", "uint16", "uint32", "uint64",
                    "byte", "rune"}:
        return actual == "int"
    if expected in {"float", "float32", "float64"}:
        return actual in {"int", "float"}
    if expected == "bool":
        return actual == "bool"
    if expected == "None":
        return actual == "None"
    return False


_TOKEN_RE = re.compile(r"\s*(->|[()[\],|]|\w+(?:\.\w+)*)")


class _TextTypeParser:
    def __init__(self, source: str):
        self.source = source
        self.tokens = self._tokenize(source)
        self.pos = 0

    def parse(self) -> Type:
        type_ = self._parse_union()
        if self._peek() is not None:
            raise TypeParseError(f"unexpected token {self._peek()!r} in type {self.source!r}")
        return type_

    @staticmethod
    def _tokenize(source: str) -> list[str]:
        tokens: list[str] = []
        pos = 0
        while pos < len(source):
            match = _TOKEN_RE.match(source, pos)
            if match is None:
                if source[pos:].strip() == "":
                    break
                raise TypeParseError(f"invalid type syntax near {source[pos:]!r}")
            tokens.append(match.group(1))
            pos = match.end()
        return tokens

    def _parse_union(self) -> Type:
        options = [self._parse_primary()]
        while self._accept("|"):
            options.append(self._parse_primary())
        return _union(options)

    def _parse_primary(self) -> Type:
        token = self._peek()
        if token is None:
            raise TypeParseError(f"unexpected end of type {self.source!r}")
        if token == "func":
            return self._parse_func()
        if not _is_name_token(token):
            raise TypeParseError(f"expected type name, got {token!r}")
        name = self._advance()
        if self._accept("["):
            args: list[Type] = []
            if not self._accept("]"):
                while True:
                    args.append(self._parse_union())
                    if self._accept("]"):
                        break
                    self._expect(",")
            return _generic_type(name, tuple(args))
        return NamedType(name)

    def _parse_func(self) -> Type:
        self._expect("func")
        self._expect("(")
        params: list[Type] = []
        if not self._accept(")"):
            while True:
                params.append(self._parse_union())
                if self._accept(")"):
                    break
                self._expect(",")
        ret: Type = NamedType("None")
        if self._accept("->"):
            ret = self._parse_union()
        return FuncType(tuple(params), ret)

    def _peek(self) -> str | None:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def _advance(self) -> str:
        token = self._peek()
        if token is None:
            raise TypeParseError(f"unexpected end of type {self.source!r}")
        self.pos += 1
        return token

    def _accept(self, token: str) -> bool:
        if self._peek() == token:
            self.pos += 1
            return True
        return False

    def _expect(self, token: str) -> None:
        found = self._advance()
        if found != token:
            raise TypeParseError(f"expected {token!r}, got {found!r}")


def _is_name_token(token: str) -> bool:
    return bool(re.fullmatch(r"\w+(?:\.\w+)*", token))
