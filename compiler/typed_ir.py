from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from lark import Token, Tree

from compiler.ast_builder import build_module
from compiler.ast_nodes import ClassDecl, FuncDecl, ImportDecl, InterfaceDecl, Module, VarDecl
from compiler.diagnostics import SourceSpan
from compiler.source_map import SourceMap
from compiler.typesys import DictType, FuncType, GenericType, ListType, NamedType, Type, parse_type


@dataclass(frozen=True)
class TypedParam:
    name: str
    type: Type
    span: SourceSpan
    has_default: bool = False
    variadic: str | None = None


@dataclass(frozen=True)
class TypedExpr:
    kind: str
    type: Type | None
    span: SourceSpan
    name: str | None = None
    args: tuple["TypedExpr", ...] = ()
    expected_type: Type | None = None


@dataclass(frozen=True)
class TypedVariable:
    name: str
    type: Type
    span: SourceSpan
    is_const: bool = False
    initializer: TypedExpr | None = None


@dataclass(frozen=True)
class TypedFunction:
    name: str
    signature: FuncType
    params: tuple[TypedParam, ...]
    return_type: Type
    locals: tuple[TypedVariable, ...]
    span: SourceSpan
    parent: str | None = None
    is_static: bool = False
    is_private: bool = False
    is_async: bool = False


@dataclass(frozen=True)
class TypedClass:
    name: str
    fields: tuple[TypedVariable, ...] = ()
    methods: tuple[TypedFunction, ...] = ()
    span: SourceSpan | None = None


@dataclass(frozen=True)
class TypedInterface:
    name: str
    methods: tuple[TypedFunction, ...] = ()
    span: SourceSpan | None = None


@dataclass(frozen=True)
class TypedImport:
    module: str
    names: tuple[str, ...]
    span: SourceSpan
    is_from: bool = False


TypedDecl = TypedFunction | TypedClass | TypedInterface | TypedVariable | TypedImport


@dataclass(frozen=True)
class TypedModule:
    body: tuple[TypedDecl, ...]
    path: Path | None = None
    function_signatures: dict[str, FuncType] = field(default_factory=dict)


def build_typed_module(
    tree: Tree,
    *,
    path: Path | None = None,
    source_map: SourceMap | None = None,
    ast_module: Module | None = None,
) -> TypedModule:
    module = ast_module or build_module(tree, path=path, source_map=source_map)
    func_nodes = _collect_function_nodes(tree)
    signatures = _collect_signatures(module)
    body: list[TypedDecl] = []
    for decl in module.body:
        typed = _typed_decl(decl, func_nodes=func_nodes, signatures=signatures)
        if typed is not None:
            body.append(typed)
    return TypedModule(body=tuple(body), path=module.path or path, function_signatures=signatures)


def _typed_decl(
    decl,
    *,
    func_nodes: dict[str, list[Tree]],
    signatures: dict[str, FuncType],
) -> TypedDecl | None:
    if isinstance(decl, FuncDecl):
        node = _pop_func_node(func_nodes, decl.name)
        return _typed_function(decl, node, signatures=signatures)
    if isinstance(decl, ClassDecl):
        methods = tuple(
            _typed_function(method, _pop_func_node(func_nodes, f"{decl.name}.{method.name}"), signatures=signatures)
            for method in decl.methods
        )
        fields = tuple(_typed_variable(field) for field in decl.fields)
        return TypedClass(decl.name, fields=fields, methods=methods, span=decl.span)
    if isinstance(decl, InterfaceDecl):
        methods = tuple(_typed_function(method, None, signatures=signatures) for method in decl.methods)
        return TypedInterface(decl.name, methods=methods, span=decl.span)
    if isinstance(decl, VarDecl):
        return _typed_variable(decl)
    if isinstance(decl, ImportDecl):
        return TypedImport(
            decl.module,
            tuple(binding.name for binding in decl.bindings if binding.name),
            decl.span,
            decl.is_from,
        )
    return None


def _typed_function(
    decl: FuncDecl,
    node: Tree | None,
    *,
    signatures: dict[str, FuncType],
) -> TypedFunction:
    params = tuple(_typed_param(param) for param in decl.params)
    return_type = _type_from_ref(decl.return_type, default=NamedType("None"))
    signature = FuncType(tuple(param.type for param in params), return_type)
    env = {param.name: param.type for param in params}
    locals_ = _function_locals(node, env=env, signatures=signatures) if node is not None else ()
    return TypedFunction(
        decl.name,
        signature,
        params,
        return_type,
        locals_,
        decl.span,
        parent=decl.parent,
        is_static=decl.is_static,
        is_private=decl.is_private,
        is_async=decl.is_async,
    )


def _typed_param(param) -> TypedParam:
    return TypedParam(
        param.name,
        _type_from_ref(param.type_ref, default=NamedType("any")),
        param.span,
        has_default=param.has_default,
        variadic=param.variadic,
    )


def _typed_variable(decl: VarDecl) -> TypedVariable:
    return TypedVariable(
        decl.name,
        _type_from_ref(decl.type_ref, default=NamedType("any")),
        decl.span,
        is_const=decl.is_const,
    )


def _collect_signatures(module: Module) -> dict[str, FuncType]:
    out: dict[str, FuncType] = {}
    for decl in module.body:
        if isinstance(decl, FuncDecl):
            out[decl.name] = _signature_from_decl(decl)
        elif isinstance(decl, ClassDecl):
            for method in decl.methods:
                out[f"{decl.name}.{method.name}"] = _signature_from_decl(method)
        elif isinstance(decl, InterfaceDecl):
            for method in decl.methods:
                out[f"{decl.name}.{method.name}"] = _signature_from_decl(method)
    return out


def _signature_from_decl(decl: FuncDecl) -> FuncType:
    params = tuple(_type_from_ref(param.type_ref, default=NamedType("any")) for param in decl.params)
    return FuncType(params, _type_from_ref(decl.return_type, default=NamedType("None")))


def _type_from_ref(ref, *, default: Type) -> Type:
    if ref is None or not getattr(ref, "name", ""):
        return default
    return parse_type(ref.name)


def _function_locals(
    node: Tree,
    *,
    env: dict[str, Type],
    signatures: dict[str, FuncType],
) -> tuple[TypedVariable, ...]:
    suite = _suite_node(node)
    if suite is None:
        return ()
    locals_: list[TypedVariable] = []
    seen: set[str] = set()
    local_env = dict(env)
    for stmt in _walk_local_statements(suite):
        for var in _variables_from_statement(stmt, env=local_env, signatures=signatures):
            local_env[var.name] = var.type
            if var.name in seen:
                continue
            seen.add(var.name)
            locals_.append(var)
    return tuple(locals_)


def _variables_from_statement(
    node: Tree,
    *,
    env: dict[str, Type],
    signatures: dict[str, FuncType],
) -> tuple[TypedVariable, ...]:
    if node.data == "annassign":
        name_node = _first_name_like(node)
        type_node = _first_child(node, "type_expr")
        value_node = _value_after_type(node)
        if name_node is None or type_node is None:
            return ()
        declared = parse_type(type_node)
        return (TypedVariable(
            _name_text(name_node),
            declared,
            _span(name_node),
            initializer=_typed_expr(
                value_node,
                env=env,
                signatures=signatures,
                expected_type=declared,
            ),
        ),)
    if node.data == "const_stmt":
        name_node = _first_child(node, "name")
        type_node = _first_child(node, "type_expr")
        value_node = _const_value_node(node)
        if name_node is None:
            return ()
        declared = parse_type(type_node) if type_node is not None else None
        initializer = _typed_expr(
            value_node,
            env=env,
            signatures=signatures,
            expected_type=declared,
        )
        if declared is not None:
            type_ = declared
        elif initializer is not None and initializer.type is not None:
            type_ = initializer.type
        else:
            type_ = NamedType("any")
        return (
            TypedVariable(
                _name_text(name_node),
                type_,
                _span(name_node),
                is_const=True,
                initializer=initializer,
            ),
        )
    if node.data == "assign":
        target = node.children[0] if node.children else None
        value = node.children[-1] if len(node.children) > 1 else None
        name_node = _first_name_like(target) if isinstance(target, Tree) else None
        target_type = env.get(_name_text(name_node)) if name_node is not None else None
        initializer = _typed_expr(
            value,
            env=env,
            signatures=signatures,
            expected_type=target_type,
        )
        if name_node is None or initializer is None or initializer.type is None:
            return ()
        return (
            TypedVariable(
                _name_text(name_node),
                initializer.type,
                _span(name_node),
                initializer=initializer,
            ),
        )
    return ()


def _typed_expr(
    node,
    *,
    env: dict[str, Type],
    signatures: dict[str, FuncType],
    expected_type: Type | None = None,
) -> TypedExpr | None:
    if not isinstance(node, Tree):
        return None
    literal_type = _literal_type(node)
    if literal_type is not None:
        return TypedExpr(node.data, literal_type, _span(node), expected_type=expected_type)
    if node.data == "var":
        name = _name_text(node)
        return TypedExpr("name", env.get(name), _span(node), name=name, expected_type=expected_type)
    if node.data == "list":
        items = tuple(
            expr for expr in (_typed_expr(child, env=env, signatures=signatures) for child in node.children)
            if expr is not None
        )
        item_type = _common_type(tuple(expr.type for expr in items if expr.type is not None)) or NamedType("any")
        return TypedExpr("list", ListType(item_type), _span(node), args=items, expected_type=expected_type)
    if node.data == "dict":
        pairs = [child for child in node.children if isinstance(child, Tree) and child.data == "key_value"]
        keys: list[Type] = []
        values: list[Type] = []
        args: list[TypedExpr] = []
        for pair in pairs:
            if len(pair.children) < 2:
                continue
            key = _typed_expr(pair.children[0], env=env, signatures=signatures)
            value = _typed_expr(pair.children[1], env=env, signatures=signatures)
            if key is not None:
                args.append(key)
                if key.type is not None:
                    keys.append(key.type)
            if value is not None:
                args.append(value)
                if value.type is not None:
                    values.append(value.type)
        key_type = _common_type(tuple(keys)) or NamedType("str")
        value_type = _common_type(tuple(values)) or NamedType("any")
        return TypedExpr(
            "dict",
            DictType(key_type, value_type),
            _span(node),
            args=tuple(args),
            expected_type=expected_type,
        )
    if node.data == "tuple":
        items = tuple(
            expr for expr in (_typed_expr(child, env=env, signatures=signatures) for child in node.children)
            if expr is not None
        )
        return TypedExpr(
            "tuple",
            GenericType("tuple", tuple(expr.type or NamedType("any") for expr in items)),
            _span(node),
            args=items,
            expected_type=expected_type,
        )
    if node.data == "funccall" and node.children:
        callee = node.children[0]
        name = _call_name(callee)
        sig = signatures.get(name or "")
        typed_args: list[TypedExpr] = []
        for idx, child in enumerate(_call_arg_nodes(node)):
            arg_expected = (
                sig.params[idx]
                if sig is not None and idx < len(sig.params)
                else None
            )
            expr = _typed_expr(
                child,
                env=env,
                signatures=signatures,
                expected_type=arg_expected,
            )
            if expr is not None:
                typed_args.append(expr)
        return TypedExpr(
            "call",
            sig.ret if sig is not None else None,
            _span(node),
            name=name,
            args=tuple(typed_args),
            expected_type=expected_type,
        )
    for child in node.children:
        expr = _typed_expr(child, env=env, signatures=signatures)
        if expr is not None:
            return expr
    return None


def _literal_type(node: Tree) -> Type | None:
    if node.data in {"string", "fstring", "string_concat"}:
        return NamedType("str")
    if node.data in {"const_true", "const_false"}:
        return NamedType("bool")
    if node.data == "const_none":
        return NamedType("None")
    if node.data == "number":
        for child in node.children:
            if isinstance(child, Token) and child.type in {"FLOAT_NUMBER", "IMAG_NUMBER"}:
                return NamedType("float")
        return NamedType("int")
    return None


def _common_type(types: tuple[Type, ...]) -> Type | None:
    if not types:
        return None
    first = types[0]
    if all(type_ == first for type_ in types):
        return first
    return NamedType("any")


def _collect_function_nodes(tree: Tree) -> dict[str, list[Tree]]:
    out: dict[str, list[Tree]] = {}
    for node in _top_level_nodes(tree):
        target = _decorated_target(node)
        if target.data == "funcdef":
            out.setdefault(_func_name(target), []).append(target)
        elif target.data == "classdef":
            class_name = _class_name(target)
            suite = _suite_node(target)
            if suite is None:
                continue
            for stmt in _top_level_nodes(suite):
                method = _decorated_target(stmt)
                if method.data == "funcdef":
                    out.setdefault(f"{class_name}.{_func_name(method)}", []).append(method)
    return out


def _walk_local_statements(node: Tree):
    if node.data in {"funcdef", "classdef", "interfacedef", "lambdef"}:
        return
    if node.data == "simple_stmt":
        for child in node.children:
            if isinstance(child, Tree):
                yield from _walk_local_statements(child)
        return
    if node.data == "assign_stmt" and node.children and isinstance(node.children[0], Tree):
        yield from _walk_local_statements(node.children[0])
        return
    if node.data in {"annassign", "assign", "const_stmt"}:
        yield node
        return
    for child in node.children:
        if isinstance(child, Tree):
            yield from _walk_local_statements(child)


def _top_level_nodes(tree: Tree):
    for child in getattr(tree, "children", []):
        if isinstance(child, Tree):
            yield child


def _decorated_target(node: Tree) -> Tree:
    if node.data != "decorated":
        return node
    for child in node.children:
        if isinstance(child, Tree) and child.data in {"funcdef", "classdef", "interfacedef"}:
            return child
    return node


def _suite_node(node: Tree) -> Tree | None:
    for child in node.children:
        if isinstance(child, Tree) and child.data == "suite":
            return child
    return None


def _func_name(node: Tree) -> str:
    name_node = _first_child(node, "name")
    return _name_text(name_node) if name_node is not None else ""


def _class_name(node: Tree) -> str:
    name_node = _first_child(node, "name")
    return _name_text(name_node) if name_node is not None else ""


def _pop_func_node(nodes: dict[str, list[Tree]], key: str) -> Tree | None:
    values = nodes.get(key)
    if not values:
        return None
    return values.pop(0)


def _first_child(node: Tree, data: str) -> Tree | None:
    for child in node.children:
        if isinstance(child, Tree) and child.data == data:
            return child
    return None


def _first_name_like(node) -> Tree | None:
    if not isinstance(node, Tree):
        return None
    if node.data == "name":
        return node
    if node.data == "var":
        return _first_child(node, "name")
    for child in node.children:
        found = _first_name_like(child)
        if found is not None:
            return found
    return None


def _value_after_type(node: Tree):
    seen_type = False
    for child in node.children:
        if not isinstance(child, Tree):
            continue
        if child.data == "type_expr":
            seen_type = True
            continue
        if seen_type:
            return child
    return None


def _const_value_node(node: Tree):
    seen_type = False
    for child in node.children[1:]:
        if not isinstance(child, Tree):
            continue
        if child.data == "type_expr":
            seen_type = True
            continue
        if seen_type or _first_child(node, "type_expr") is None:
            return child
    return None


def _call_arg_nodes(call: Tree) -> tuple[Tree, ...]:
    if len(call.children) < 2:
        return ()
    args = call.children[1]
    if not isinstance(args, Tree) or args.data != "arguments":
        return ()
    out: list[Tree] = []
    for child in args.children:
        if not isinstance(child, Tree):
            continue
        if child.data == "argvalue" and len(child.children) == 2:
            value = child.children[1]
            if isinstance(value, Tree):
                out.append(value)
            continue
        if child.data in {"stararg", "starargs", "kwargs"}:
            continue
        out.append(child)
    return tuple(out)


def _call_name(node) -> str | None:
    if not isinstance(node, Tree):
        return None
    if node.data == "var":
        return _name_text(node)
    if node.data == "getattr" and len(node.children) >= 2:
        left = _call_name(node.children[0])
        right = _name_text(node.children[1])
        return f"{left}.{right}" if left and right else right or left
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


def _span(node) -> SourceSpan:
    meta = getattr(node, "meta", None)
    line = int(getattr(meta, "line", 1) or 1)
    col = int(getattr(meta, "column", 1) or 1)
    end_line = int(getattr(meta, "end_line", line) or line)
    end_col = int(getattr(meta, "end_column", col) or col)
    return SourceSpan(None, line, col, end_line, end_col)
