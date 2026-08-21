"""Build the small canonical AST from the existing Lark parse tree."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from lark import Token, Tree

from compiler.ast_nodes import (
    ClassDecl,
    Decl,
    FuncDecl,
    ImportBinding,
    ImportDecl,
    InterfaceDecl,
    Module,
    Param,
    TypeRef,
    VarDecl,
)
from compiler.diagnostics import SourceSpan
from compiler.source_map import SourceMap


def build_module(
    tree: Tree,
    *,
    path: Path | None = None,
    source_map: SourceMap | None = None,
) -> Module:
    body: list[Decl] = []
    for node in _top_level_nodes(tree):
        decl = _decl_from_node(node, path=path, source_map=source_map)
        if decl is None:
            continue
        if isinstance(decl, list):
            body.extend(decl)
        else:
            body.append(decl)
    return Module(body=body, path=path)


def _decl_from_node(
    node: Tree,
    *,
    path: Path | None,
    source_map: SourceMap | None,
) -> Decl | list[Decl] | None:
    if node.data == "funcdef":
        return _func_decl(node, path=path, source_map=source_map)
    if node.data == "classdef":
        return _class_decl(node, path=path, source_map=source_map)
    if node.data == "interfacedef":
        return _interface_decl(node, path=path, source_map=source_map)
    if node.data in {"const_stmt", "annassign", "assign_stmt", "assign"}:
        return _var_decls(node, path=path, source_map=source_map)
    if node.data in {"import_from", "import_name"}:
        return _import_decl(node, path=path, source_map=source_map)
    if node.data == "decorated":
        for child in node.children:
            if isinstance(child, Tree):
                decl = _decl_from_node(child, path=path, source_map=source_map)
                if decl is not None:
                    return decl
    if node.data in {"simple_stmt", "import_stmt"}:
        decls: list[Decl] = []
        for child in node.children:
            if isinstance(child, Tree):
                decl = _decl_from_node(child, path=path, source_map=source_map)
                if isinstance(decl, list):
                    decls.extend(decl)
                elif decl is not None:
                    decls.append(decl)
        return decls
    return None


def _top_level_nodes(tree: Tree) -> Iterable[Tree]:
    for child in getattr(tree, "children", []):
        if isinstance(child, Tree):
            yield child


def _func_decl(
    node: Tree,
    *,
    path: Path | None,
    source_map: SourceMap | None,
    parent: str | None = None,
) -> FuncDecl:
    name = ""
    params_node: Tree | None = None
    return_node: Tree | None = None
    is_static = False
    is_private = False
    is_async = False

    for child in node.children:
        if isinstance(child, Token):
            tok = str(child)
            is_static = is_static or tok == "static"
            is_private = is_private or tok == "private"
            is_async = is_async or tok == "async"
            continue
        if not isinstance(child, Tree):
            continue
        if child.data == "name" and not name:
            name = _name_text(child)
        elif child.data == "typed_parameters":
            params_node = child
        elif child.data in {"single_return_type", "multi_return_type", "return_type"}:
            return_node = child

    return FuncDecl(
        name=name,
        params=_params(params_node, path=path, source_map=source_map),
        return_type=_return_type(return_node, path=path, source_map=source_map),
        span=_span(node, path=path, source_map=source_map),
        is_static=is_static,
        is_private=is_private,
        is_async=is_async,
        parent=parent,
    )


def _class_decl(
    node: Tree,
    *,
    path: Path | None,
    source_map: SourceMap | None,
) -> ClassDecl:
    name = ""
    suite: Tree | None = None
    for child in node.children:
        if isinstance(child, Tree):
            if child.data == "name" and not name:
                name = _name_text(child)
            elif child.data == "suite":
                suite = child

    methods: list[FuncDecl] = []
    fields: list[VarDecl] = []
    if suite is not None:
        for stmt in _top_level_nodes(suite):
            target = _decorated_target(stmt)
            if target.data == "funcdef":
                methods.append(_func_decl(target, path=path, source_map=source_map, parent=name))
            elif target.data in {"const_stmt", "annassign", "assign_stmt", "assign", "simple_stmt"}:
                fields.extend(_var_decls(target, path=path, source_map=source_map))

    return ClassDecl(
        name=name,
        methods=methods,
        fields=fields,
        span=_span(node, path=path, source_map=source_map),
    )


def _interface_decl(
    node: Tree,
    *,
    path: Path | None,
    source_map: SourceMap | None,
) -> InterfaceDecl:
    name = ""
    for child in node.children:
        if isinstance(child, Tree):
            if child.data == "name" and not name:
                name = _name_text(child)

    methods: list[FuncDecl] = []
    for child in node.children:
        if isinstance(child, Tree) and child.data == "interface_method":
            methods.append(_func_decl(child, path=path, source_map=source_map, parent=name))
    return InterfaceDecl(name=name, methods=methods, span=_span(node, path=path, source_map=source_map))


def _decorated_target(node: Tree) -> Tree:
    if node.data != "decorated":
        return node
    for child in node.children:
        if isinstance(child, Tree) and child.data in {"funcdef", "classdef", "interfacedef"}:
            return child
    return node


def _params(
    params_node: Tree | None,
    *,
    path: Path | None,
    source_map: SourceMap | None,
) -> list[Param]:
    if params_node is None:
        return []
    out: list[Param] = []
    for child in params_node.children:
        if not isinstance(child, Tree):
            continue
        out.extend(_params_from_node(child, path=path, source_map=source_map))
    return out


def _params_from_node(
    node: Tree,
    *,
    path: Path | None,
    source_map: SourceMap | None,
) -> list[Param]:
    if node.data == "typed_paramvalue":
        inner = next((c for c in node.children if isinstance(c, Tree)), None)
        if inner is None:
            return []
        param = _param_from_typed_node(inner, path=path, source_map=source_map)
        if param is None:
            return []
        has_default = any(c is not None and c is not inner for c in node.children)
        return [Param(
            name=param.name,
            type_ref=param.type_ref,
            span=param.span,
            has_default=has_default,
            variadic=param.variadic,
        )]
    if node.data in {"typed_starparams", "typed_kwparams"}:
        variadic = "*" if node.data == "typed_starparams" else "**"
        params: list[Param] = []
        for child in node.children:
            if isinstance(child, Tree):
                for param in _params_from_node(child, path=path, source_map=source_map):
                    params.append(Param(
                        name=param.name,
                        type_ref=param.type_ref,
                        span=param.span,
                        has_default=param.has_default,
                        variadic=variadic,
                    ))
        return params
    param = _param_from_typed_node(node, path=path, source_map=source_map)
    return [param] if param is not None else []


def _param_from_typed_node(
    node: Tree,
    *,
    path: Path | None,
    source_map: SourceMap | None,
) -> Param | None:
    if node.data not in {"typed_param", "typed_default_param", "param", "name"}:
        return None
    name_node = next((c for c in node.children if isinstance(c, Tree) and c.data == "name"), node)
    name = _name_text(name_node)
    type_node = next((c for c in node.children if isinstance(c, Tree) and c.data == "type_expr"), None)
    return Param(
        name=name,
        type_ref=_type_ref(type_node, path=path, source_map=source_map),
        span=_span(name_node if isinstance(name_node, Tree) else node, path=path, source_map=source_map),
        has_default=node.data == "typed_default_param",
    )


def _return_type(
    node: Tree | None,
    *,
    path: Path | None,
    source_map: SourceMap | None,
) -> TypeRef | None:
    if node is None:
        return None
    type_node = next((c for c in node.children if isinstance(c, Tree)), None)
    return _type_ref(type_node, path=path, source_map=source_map)


def _type_ref(
    node: Tree | None,
    *,
    path: Path | None,
    source_map: SourceMap | None,
) -> TypeRef | None:
    if node is None:
        return None
    return TypeRef(_type_to_str(node), span=_span(node, path=path, source_map=source_map))


def _var_decls(
    node: Tree,
    *,
    path: Path | None,
    source_map: SourceMap | None,
) -> list[VarDecl]:
    if node.data == "simple_stmt":
        out: list[VarDecl] = []
        for child in node.children:
            if isinstance(child, Tree):
                out.extend(_var_decls(child, path=path, source_map=source_map))
        return out
    if node.data == "assign_stmt" and node.children and isinstance(node.children[0], Tree):
        return _var_decls(node.children[0], path=path, source_map=source_map)
    if node.data == "const_stmt":
        name_node = next((c for c in node.children if isinstance(c, Tree) and c.data == "name"), None)
        type_node = next((c for c in node.children if isinstance(c, Tree) and c.data == "type_expr"), None)
        if name_node is None:
            return []
        return [VarDecl(
            name=_name_text(name_node),
            type_ref=_type_ref(type_node, path=path, source_map=source_map),
            span=_span(name_node, path=path, source_map=source_map),
            is_const=True,
        )]
    if node.data == "annassign":
        name_node = _first_name_like(node)
        type_node = next((c for c in node.children if isinstance(c, Tree) and c.data == "type_expr"), None)
        if name_node is None:
            return []
        return [VarDecl(
            name=_name_text(name_node),
            type_ref=_type_ref(type_node, path=path, source_map=source_map),
            span=_span(name_node, path=path, source_map=source_map),
        )]
    if node.data == "name":
        return [VarDecl(
            name=_name_text(node),
            type_ref=None,
            span=_span(node, path=path, source_map=source_map),
        )]
    return []


def _import_decl(
    node: Tree,
    *,
    path: Path | None,
    source_map: SourceMap | None,
) -> ImportDecl | None:
    if node.data == "import_from":
        module = ""
        bindings: list[ImportBinding] = []
        for child in node.children:
            if isinstance(child, Tree) and child.data in {"dotted_name", "scoped_name"} and not module:
                module = _module_name(child)
            elif isinstance(child, Tree) and child.data == "import_as_names":
                for item in child.children:
                    if not isinstance(item, Tree) or item.data != "import_as_name":
                        continue
                    names = [c for c in item.children if isinstance(c, Tree) and c.data == "name"]
                    if not names:
                        continue
                    imported = _name_text(names[0])
                    alias = _name_text(names[1]) if len(names) > 1 else None
                    bindings.append(ImportBinding(
                        name=alias or imported,
                        imported=imported,
                        alias=alias,
                        span=_span(names[-1] if alias else names[0], path=path, source_map=source_map),
                    ))
        if not module:
            return None
        return ImportDecl(
            module=module,
            bindings=bindings,
            span=_span(node, path=path, source_map=source_map),
            is_from=True,
        )

    if node.data == "import_name":
        bindings: list[ImportBinding] = []
        module = ""
        for child in node.children:
            if not isinstance(child, Tree) or child.data != "dotted_as_names":
                continue
            for item in child.children:
                if not isinstance(item, Tree) or item.data != "dotted_as_name":
                    continue
                dotted = next(
                    (c for c in item.children if isinstance(c, Tree) and c.data == "dotted_name"),
                    None,
                )
                if dotted is None:
                    continue
                mod_name = _module_name(dotted)
                alias_node = next(
                    (c for c in item.children[1:] if isinstance(c, Tree) and c.data == "name"),
                    None,
                )
                alias = _name_text(alias_node) if alias_node is not None else None
                local = alias or mod_name.rsplit(".", 1)[-1].rsplit("/", 1)[-1]
                if not module:
                    module = mod_name
                bindings.append(ImportBinding(
                    name=local,
                    imported=None,
                    alias=alias,
                    span=_span(alias_node or dotted, path=path, source_map=source_map),
                ))
        if not bindings:
            return None
        return ImportDecl(
            module=module,
            bindings=bindings,
            span=_span(node, path=path, source_map=source_map),
            is_from=False,
        )
    return None


def _first_name_like(node: Tree) -> Tree | None:
    if node.data == "name":
        return node
    if node.data == "var":
        for child in node.children:
            if isinstance(child, Tree) and child.data == "name":
                return child
    for child in node.children:
        if isinstance(child, Tree):
            found = _first_name_like(child)
            if found is not None:
                return found
    return None


def _name_text(node) -> str:
    if isinstance(node, Token):
        return str(node)
    if isinstance(node, Tree):
        if node.data == "name" and node.children:
            return str(node.children[0])
        for child in node.children:
            text = _name_text(child)
            if text:
                return text
    return ""


def _module_name(node: Tree) -> str:
    if node.data == "scoped_name" and node.children:
        return str(node.children[0])
    if node.data == "dotted_name":
        return ".".join(_name_text(child) for child in node.children if _name_text(child))
    return _name_text(node)


def _type_to_str(node) -> str:
    if node is None:
        return ""
    if isinstance(node, Token):
        return str(node)
    if isinstance(node, Tree):
        if node.data == "name":
            return str(node.children[0]) if node.children else ""
        if node.data == "dotted_name":
            return ".".join(filter(None, (_type_to_str(c) for c in node.children)))
        if node.data == "type_expr":
            return _type_to_str(node.children[0]) if node.children else ""
        if node.data == "type_union":
            parts = [text for text in (_type_to_str(c) for c in node.children) if text]
            return " | ".join(parts)
        if node.data == "type_name":
            return _type_to_str(node.children[0]) if node.children else ""
        if node.data == "type_generic" and node.children:
            base = _type_to_str(node.children[0])
            args = ", ".join(
                filter(None, (_type_to_str(c) for c in node.children[1:]))
            )
            return f"{base}[{args}]" if args else base
        if node.data == "type_subscript" and node.children:
            base = _type_to_str(node.children[0])
            inner = ", ".join(filter(None, (_type_to_str(c) for c in node.children[1:])))
            return f"{base}[{inner}]" if inner else base
        if node.data == "type_func":
            params = ", ".join(_type_to_str(c) for c in node.children[:-1])
            ret = _type_to_str(node.children[-1]) if node.children else ""
            return f"func({params}) -> {ret}" if ret else f"func({params})"
        for child in node.children:
            text = _type_to_str(child)
            if text:
                return text
    return ""


def _span(
    node,
    *,
    path: Path | None,
    source_map: SourceMap | None,
) -> SourceSpan:
    meta = getattr(node, "meta", None)
    line = int(getattr(meta, "line", 1) or 1)
    col = int(getattr(meta, "column", 1) or 1)
    end_line = int(getattr(meta, "end_line", line) or line)
    end_col = int(getattr(meta, "end_column", col) or col)
    if source_map is not None:
        start = source_map.generated_to_original(line, col)
        end = source_map.generated_to_original(end_line, end_col)
        line, col, end_line, end_col = start.line, start.col, end.line, end.col
    return SourceSpan(path, line, col, end_line, end_col)
