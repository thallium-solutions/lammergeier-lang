"""Canonical Lammergeier AST node definitions.

This AST starts intentionally small. Phase 4 uses it for declaration
facts first, while expression checks and Go emission continue to consume
the existing Lark tree until their own migrations are scoped.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from compiler.diagnostics import SourceSpan


@dataclass(frozen=True)
class TypeRef:
    name: str
    span: SourceSpan | None = None


@dataclass(frozen=True)
class Param:
    name: str
    type_ref: TypeRef | None
    span: SourceSpan
    has_default: bool = False
    variadic: str | None = None


@dataclass(frozen=True)
class FuncDecl:
    name: str
    params: list[Param]
    return_type: TypeRef | None
    span: SourceSpan
    is_static: bool = False
    is_private: bool = False
    is_async: bool = False
    parent: str | None = None


@dataclass(frozen=True)
class VarDecl:
    name: str
    type_ref: TypeRef | None
    span: SourceSpan
    is_const: bool = False


@dataclass(frozen=True)
class ImportBinding:
    name: str
    imported: str | None
    alias: str | None
    span: SourceSpan


@dataclass(frozen=True)
class ImportDecl:
    module: str
    bindings: list[ImportBinding]
    span: SourceSpan
    is_from: bool = False


@dataclass(frozen=True)
class ClassDecl:
    name: str
    methods: list[FuncDecl] = field(default_factory=list)
    fields: list[VarDecl] = field(default_factory=list)
    span: SourceSpan | None = None


@dataclass(frozen=True)
class InterfaceDecl:
    name: str
    methods: list[FuncDecl] = field(default_factory=list)
    span: SourceSpan | None = None


Decl = FuncDecl | ClassDecl | InterfaceDecl | VarDecl | ImportDecl


@dataclass(frozen=True)
class Module:
    body: list[Decl]
    path: Path | None = None
