"""Module namespacing for linked Lam modules.

Every Lam module is emitted into one Go package, so two modules that both
define a top-level ``tag`` would otherwise produce two Go ``Tag`` symbols.
This module computes, per module, how its names map onto Go:

* top-level functions and consts defined in more than one linked module are
  *colliding*; outside the user's main module they get a module-qualified
  Go name (``Tag__alpha``), applied to the module's own definitions,
  references and raw ``go!`` code;
* ``from m import x as y`` and ``import m as a`` / ``a.x`` are rewritten in
  the AST to the canonical name when that name is free in the module;
  otherwise the alias keeps its own Lam name and is mapped to the right Go
  symbol;
* imported-function metadata for colliding names comes from the defining
  module (``apply_metadata_view``), so same-name functions in different
  modules never merge their signatures or overload sets.

* classes and interfaces defined in more than one linked module are renamed at
  the Lam level (``Config`` in ``settings`` -> ``Config__settings``) before any
  metadata is collected, so the Go type, ``New<Class>`` constructor, static
  members and every compiler table follow automatically;
* raw ``go!`` code follows the renames of its own module and of names it
  imports without an alias.

Definitions in the main module and the classes compiler-generated code refers
to (``lamerrors.Result`` / ``Error``, ``lamstrings.Strings``) are never
renamed; a clash between two of those is still reported by the link-time
clash diagnostic.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from lark import Token, Tree

from compiler.link_symbols import mask_go_literals

FUNCTION_KINDS = {"func", "private func"}
# Classes referenced by name from compiler-generated Go code.
PINNED_CLASSES = {("lamerrors", "Result"), ("lamerrors", "Error"), ("lamstrings", "Strings")}
VALUE_KINDS = {"const", "var"}
TYPE_KINDS = {"class", "interface"}


class NamespaceError(Exception):
    """A namespacing request the compiler cannot honour (reported to the user)."""


@dataclass(frozen=True)
class ImportBinding:
    module: str
    imported: Optional[str]  # None: ``import module [as alias]``
    local: str
    explicit_alias: bool
    line: int = 0


@dataclass
class NamespacePlan:
    # Lam reference renames applied to the AST (alias -> canonical name).
    rewrites: dict[str, str] = field(default_factory=dict)
    # ``alias.member`` accesses -> Lam name used after the rewrite.
    module_access: dict[str, dict[str, str]] = field(default_factory=dict)
    # Lam function / const name -> Go identifier.
    function_go_names: dict[str, str] = field(default_factory=dict)
    const_go_names: dict[str, str] = field(default_factory=dict)
    # Lam name -> (defining module path, defining name) whose metadata applies.
    metadata_from: dict[str, tuple[Path, str]] = field(default_factory=dict)
    # Colliding names not visible in this module: drop their merged metadata.
    hidden: set[str] = field(default_factory=set)
    # Go identifier renames for this module's raw ``go!`` code; keys ending
    # in ``_`` rename a prefix (``Config_`` -> ``Config__one_`` for statics).
    go_renames: dict[str, str] = field(default_factory=dict)
    # This module's own class/interface declarations renamed at the Lam level.
    declaration_renames: dict[str, str] = field(default_factory=dict)
    # Function aliases kept under their own Lam name (the canonical name is
    # also bound here) -> canonical name: the alias uses the canonical
    # function's final Go name, including any de-duplication suffix.
    function_aliases: dict[str, str] = field(default_factory=dict)

    def cache_key(self) -> str:
        parts = [
            "rw:" + ",".join(f"{k}>{v}" for k, v in sorted(self.rewrites.items())),
            "ma:" + ",".join(
                f"{alias}.{m}>{n}"
                for alias, members in sorted(self.module_access.items())
                for m, n in sorted(members.items())
            ),
            "fn:" + ",".join(f"{k}>{v}" for k, v in sorted(self.function_go_names.items())),
            "cv:" + ",".join(f"{k}>{v}" for k, v in sorted(self.const_go_names.items())),
            "md:" + ",".join(f"{k}>{p}:{n}" for k, (p, n) in sorted(self.metadata_from.items())),
            "hd:" + ",".join(sorted(self.hidden)),
            "go:" + ",".join(f"{k}>{v}" for k, v in sorted(self.go_renames.items())),
            "dc:" + ",".join(f"{k}>{v}" for k, v in sorted(self.declaration_renames.items())),
            "fa:" + ",".join(f"{k}>{v}" for k, v in sorted(self.function_aliases.items())),
        ]
        return "|".join(parts)

    def is_empty(self) -> bool:
        return not (
            self.rewrites or self.module_access or self.function_go_names
            or self.const_go_names or self.metadata_from or self.hidden or self.go_renames
            or self.declaration_renames or self.function_aliases
        )


# ── AST helpers ──────────────────────────────────────────────────────


def _name_text(node) -> str:
    if isinstance(node, Token):
        return str(node)
    if isinstance(node, Tree) and node.children:
        return _name_text(node.children[0])
    return ""


def _dotted_text(node) -> str:
    if isinstance(node, Tree) and node.data == "dotted_name":
        return ".".join(_name_text(c) for c in node.children)
    if isinstance(node, Tree) and node.data == "scoped_name":
        return _name_text(node)
    return ""


def import_name_entries(node: Tree) -> list[tuple[str, Optional[str]]]:
    """``import a.b [as x], @scope/pkg [as y]`` -> [(module, alias|None)]."""
    out: list[tuple[str, Optional[str]]] = []
    if not isinstance(node, Tree) or node.data != "import_name":
        return out
    for child in node.children:
        if not isinstance(child, Tree) or child.data != "dotted_as_names":
            continue
        for item in child.children:
            if not isinstance(item, Tree) or item.data != "dotted_as_name" or not item.children:
                continue
            module = _dotted_text(item.children[0])
            alias = None
            for part in item.children[1:]:
                if isinstance(part, Tree) and part.data == "name":
                    alias = _name_text(part)
            if module:
                out.append((module, alias))
    return out


def default_module_local(module: str) -> str:
    return module.rsplit(".", 1)[-1].rsplit("/", 1)[-1]


def _top_level_statements(tree: Tree):
    for child in tree.children if isinstance(tree, Tree) else []:
        if not isinstance(child, Tree):
            continue
        if child.data in {"simple_stmt", "decorated"}:
            for sub in child.children:
                if isinstance(sub, Tree):
                    yield sub
        else:
            yield child


def _funcdef_name(node: Tree) -> str:
    for child in node.children:
        if isinstance(child, Tree) and child.data == "name":
            return _name_text(child)
    return ""


def _funcdef_is_private(node: Tree) -> bool:
    return any(isinstance(c, Token) and str(c) == "private" for c in node.children)


def top_level_symbols(tree: Tree) -> dict[str, str]:
    """Top-level functions, classes, interfaces, consts and globals of a module."""
    out: dict[str, str] = {}
    for stmt in _top_level_statements(tree):
        data = stmt.data
        if data == "funcdef":
            name = _funcdef_name(stmt)
            if name and name != "main":
                out.setdefault(name, "private func" if _funcdef_is_private(stmt) else "func")
        elif data in {"classdef", "interfacedef"}:
            name = _funcdef_name(stmt)
            if name:
                out[name] = "class" if data == "classdef" else "interface"
        elif data == "const_stmt" and stmt.children:
            name = _name_text(stmt.children[0])
            if name:
                out[name] = "const"
        elif data == "assign_stmt":
            for sub in stmt.children:
                if isinstance(sub, Tree) and sub.data == "annassign":
                    target = next(
                        (c for c in sub.children if isinstance(c, Tree) and c.data == "var"),
                        None,
                    )
                    if target is not None:
                        out.setdefault(_name_text(target), "var")
    return out


def import_bindings(tree: Tree) -> list[ImportBinding]:
    out: list[ImportBinding] = []

    def visit(node):
        if not isinstance(node, Tree):
            return
        line = getattr(getattr(node, "meta", None), "line", 0) or 0
        if node.data == "import_name":
            for module, alias in import_name_entries(node):
                out.append(ImportBinding(module, None, alias or default_module_local(module), alias is not None, line))
            return
        if node.data == "import_from":
            module = ""
            prefix = ""
            for child in node.children:
                if isinstance(child, Tree) and child.data == "dots":
                    prefix = "".join(str(t) for t in child.children)
                elif isinstance(child, Tree) and child.data in {"dotted_name", "scoped_name"}:
                    module = prefix + _dotted_text(child)
            if not module and prefix:
                module = prefix
            for child in node.children:
                if isinstance(child, Tree) and child.data == "import_as_names":
                    for item in child.children:
                        if not isinstance(item, Tree) or item.data != "import_as_name":
                            continue
                        names = [_name_text(c) for c in item.children if isinstance(c, Tree)]
                        if not names:
                            continue
                        alias = names[1] if len(names) > 1 else None
                        out.append(ImportBinding(module, names[0], alias or names[0], alias is not None, line))
            return
        for child in node.children:
            visit(child)

    for stmt in _top_level_statements(tree):
        visit(stmt)
    return out


def module_member_accesses(tree: Tree, alias: str) -> set[str]:
    """Members read as ``alias.member`` (expressions and type annotations)."""
    found: set[str] = set()

    def visit(node):
        if not isinstance(node, Tree):
            return
        if node.data in {"getattr", "getattr_safe"} and len(node.children) >= 2:
            obj = node.children[0]
            if isinstance(obj, Tree) and obj.data == "var" and _name_text(obj) == alias:
                found.add(_name_text(node.children[1]))
        if node.data == "dotted_name" and len(node.children) >= 2 and _name_text(node.children[0]) == alias:
            found.add(_name_text(node.children[1]))
        if node.data == "fstring" and node.children:
            found.update(re.findall(r"(?<![\w.])" + re.escape(alias) + r"\.([A-Za-z_]\w*)", _name_text(node)))
        for child in node.children:
            visit(child)

    visit(tree)
    return found


def _binders(node: Tree) -> set[str]:
    """Names bound by a function/lambda parameter list or a catch clause."""
    names: set[str] = set()
    if node.data in {"funcdef", "lambdef"}:
        for child in node.children:
            if isinstance(child, Tree) and child.data in {"typed_parameters", "parameters", "lambda_params"}:
                for param in child.iter_subtrees():
                    if param.data in {"typed_param", "typed_lambda_param", "inline_lambda_param"} and param.children:
                        names.add(_name_text(param.children[0]))
    elif node.data in {"catch_clause", "do_stmt"}:
        for child in node.children:
            if isinstance(child, Tree) and child.data == "name":
                names.add(_name_text(child))
    return names


def rewrite_tree(
    tree: Tree,
    rewrites: dict[str, str],
    module_access: dict[str, dict[str, str]],
    declarations: Optional[dict[str, str]] = None,
) -> None:
    """Apply reference renames and ``alias.member`` rewrites in place.

    Only reference positions change: ``var`` nodes and type-annotation dotted
    names. Keyword-argument names, attribute names, declarations and import
    statements are left alone, and a subtree whose parameters or catch name
    rebind a name stops that name's rewrite inside it.
    """
    declarations = declarations or {}
    if not rewrites and not module_access and not declarations:
        return

    def make_var(name: str, like: Tree) -> None:
        like.data = "var"
        like.children = [Tree("name", [Token("NAME", name)])]

    def visit(node, active_rewrites: dict[str, str], active_modules: dict[str, dict[str, str]]):
        if not isinstance(node, Tree):
            return
        if node.data in {"import_stmt", "import_name", "import_from"}:
            return
        if node.data in {"classdef", "interfacedef"} and declarations:
            for child in node.children:
                if isinstance(child, Tree) and child.data == "name":
                    old = _name_text(child)
                    if old in declarations:
                        child.children = [Token("NAME", declarations[old])]
                    break
        bound = _binders(node)
        if bound:
            active_rewrites = {k: v for k, v in active_rewrites.items() if k not in bound}
            active_modules = {k: v for k, v in active_modules.items() if k not in bound}
            if not active_rewrites and not active_modules:
                return
        if node.data in {"getattr", "getattr_safe"} and len(node.children) >= 2:
            obj = node.children[0]
            if isinstance(obj, Tree) and obj.data == "var":
                members = active_modules.get(_name_text(obj))
                member = _name_text(node.children[1])
                if members is not None and member in members:
                    make_var(members[member], node)
                    return
        if node.data == "fstring" and node.children and isinstance(node.children[0], Token):
            token = node.children[0]
            text = rewrite_fstring(str(token), active_rewrites, active_modules)
            if text != str(token):
                node.children[0] = Token(token.type, text, token.start_pos, token.line, token.column)
            return
        if node.data == "var":
            name = _name_text(node)
            if name in active_rewrites:
                node.children = [Tree("name", [Token("NAME", active_rewrites[name])])]
            return
        if node.data == "dotted_name" and node.children:
            first = _name_text(node.children[0])
            if len(node.children) >= 2 and first in active_modules:
                member = _name_text(node.children[1])
                target = active_modules[first].get(member)
                if target is not None:
                    node.children = [Tree("name", [Token("NAME", target)])] + list(node.children[2:])
                    return
            if len(node.children) == 1 and first in active_rewrites:
                node.children = [Tree("name", [Token("NAME", active_rewrites[first])])]
            return
        for index, child in enumerate(node.children):
            if (
                node.data == "argvalue"
                and index == 0
                and len(node.children) == 2
                and isinstance(child, Tree)
                and child.data == "var"
            ):
                continue  # keyword-argument name
            visit(child, active_rewrites, active_modules)

    visit(tree, dict(rewrites), {k: dict(v) for k, v in module_access.items()})


def _rewrite_expression_text(expr: str, rewrites: dict[str, str], modules: dict[str, dict[str, str]]) -> str:
    """Rewrite names in one f-string placeholder expression (outside quotes)."""
    out: list[str] = []
    index = 0
    while index < len(expr):
        ch = expr[index]
        if ch in "\"'":
            end = expr.find(ch, index + 1)
            end = len(expr) if end < 0 else end + 1
            out.append(expr[index:end])
            index = end
            continue
        match = re.match(r"[A-Za-z_]\w*", expr[index:])
        if match and (index == 0 or not (expr[index - 1].isalnum() or expr[index - 1] in "_.")):
            word = match.group(0)
            after = index + len(word)
            member = re.match(r"\.([A-Za-z_]\w*)", expr[after:])
            if word in modules and member and member.group(1) in modules[word]:
                out.append(modules[word][member.group(1)])
                index = after + len(member.group(0))
                continue
            keyword_arg = re.match(r"\s*=(?!=)", expr[after:])
            out.append(rewrites[word] if word in rewrites and not keyword_arg else word)
            index = after
            continue
        if match:
            out.append(match.group(0))
            index += len(match.group(0))
            continue
        out.append(ch)
        index += 1
    return "".join(out)


def rewrite_fstring(text: str, rewrites: dict[str, str], modules: dict[str, dict[str, str]]) -> str:
    """Apply namespace rewrites inside ``{...}`` placeholders of an f-string literal."""
    if not rewrites and not modules:
        return text
    out: list[str] = []
    index = 0
    while index < len(text):
        ch = text[index]
        if ch == "{" and text.startswith("{{", index):
            out.append("{{")
            index += 2
            continue
        if ch != "{":
            out.append(ch)
            index += 1
            continue
        depth = 1
        end = index + 1
        quote = ""
        while end < len(text) and depth:
            c = text[end]
            escaped = (len(text[:end]) - len(text[:end].rstrip("\\"))) % 2 == 1
            if quote:
                if c == quote and not escaped:
                    quote = ""
            elif c in "\"'" and not escaped:
                quote = c
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
            end += 1
        inner = text[index + 1:end - 1]
        out.append("{" + _rewrite_expression_text(inner, rewrites, modules) + "}")
        index = end
    return "".join(out)


def rename_go_identifiers(go_source: str, renames: dict[str, str]) -> str:
    """Rename Go identifiers outside strings/comments and not after a ``.``.

    Overload-mangled names (``Tag_1_str``) follow their base name.
    """
    if not renames or not go_source:
        return go_source
    masked = mask_go_literals(go_source)
    exact = [name for name in renames if not name.endswith("_")]
    prefixes = [name for name in renames if name.endswith("_")]
    alternatives = []
    if exact:
        alternatives.append(
            "(?P<exact>" + "|".join(re.escape(n) for n in sorted(exact, key=len, reverse=True)) + r")(?=(?:_\d\w*)?\b)"
        )
    if prefixes:
        alternatives.append(
            "(?P<prefix>" + "|".join(re.escape(n) for n in sorted(prefixes, key=len, reverse=True)) + r")(?=[A-Za-z])"
        )
    pattern = re.compile(r"(?<![\w.])(?:" + "|".join(alternatives) + ")")
    out: list[str] = []
    last = 0
    for match in pattern.finditer(masked):
        key = match.group("exact") if exact and match.group("exact") else match.group("prefix")
        out.append(go_source[last:match.start()])
        out.append(renames[key])
        last = match.end()
    out.append(go_source[last:])
    return "".join(out)


def module_tag(module_name: str) -> str:
    tag = re.sub(r"[^0-9A-Za-z]+", "_", module_name).strip("_")
    return tag or "mod"


# ── Plans ────────────────────────────────────────────────────────────


@dataclass
class ModuleInfo:
    path: Path
    name: str
    tree: Tree
    symbols: dict[str, str] = field(default_factory=dict)
    bindings: list[ImportBinding] = field(default_factory=list)


def build_plans(
    modules: dict[Path, ModuleInfo],
    main_path: Path,
    resolve: Callable[[str, Path], Optional[Path]],
    function_go_name: Callable[[str, bool], str],
) -> dict[Path, NamespacePlan]:
    owners: dict[str, list[Path]] = {}
    for path, info in modules.items():
        for name, kind in info.symbols.items():
            owners.setdefault(name, []).append(path)
    colliding = {name for name, paths in owners.items() if len(paths) > 1}

    def default_go(name: str, kind: str) -> str:
        if kind in FUNCTION_KINDS:
            return function_go_name(name, kind == "private func")
        return name

    def pinned(path: Path, name: str) -> bool:
        return path == main_path or (modules[path].name, name) in PINNED_CLASSES

    # (path, name) -> new Go name (functions, consts, vars) or new Lam name
    # (classes, interfaces).
    renamed: dict[tuple[Path, str], str] = {}
    for name in colliding:
        for path in owners[name]:
            if pinned(path, name):
                continue
            kind = modules[path].symbols[name]
            tag = module_tag(modules[path].name)
            renamed[(path, name)] = f"{name}__{tag}" if kind in TYPE_KINDS else f"{default_go(name, kind)}__{tag}"

    def go_renames_for(name: str, kind: str, new: str) -> dict[str, str]:
        if kind in TYPE_KINDS:
            return {name: new, f"New{name}": f"New{new}", f"{name}_": f"{new}_"}
        return {default_go(name, kind): new}

    def resolve_symbol(path: Path, name: str, depth: int = 0) -> Optional[tuple[Path, str, str]]:
        info = modules.get(path)
        if info is None or depth > 16:
            return None
        kind = info.symbols.get(name)
        if kind is not None:
            return path, name, kind
        for binding in info.bindings:
            if binding.local == name and binding.imported is not None:
                target = resolve(binding.module, path)
                if target is not None:
                    return resolve_symbol(target, binding.imported, depth + 1)
        return None

    plans: dict[Path, NamespacePlan] = {}
    for path, info in modules.items():
        plan = NamespacePlan()
        bound = set(info.symbols) | {b.local for b in info.bindings}
        claimed: dict[str, tuple[Path, str, str]] = {}

        for name, kind in info.symbols.items():
            claimed[name] = (path, name, kind)
            new = renamed.get((path, name))
            if new is None:
                continue
            plan.go_renames.update(go_renames_for(name, kind, new))
            if kind in TYPE_KINDS:
                plan.rewrites[name] = new
                plan.declaration_renames[name] = new
                continue
            target = plan.function_go_names if kind in FUNCTION_KINDS else plan.const_go_names
            target[name] = new
            plan.metadata_from[name] = (path, name)

        def register(lam_name: str, symbol: tuple[Path, str, str], where: str) -> None:
            def_path, def_name, kind = symbol
            previous = claimed.get(lam_name)
            if previous is not None and previous != symbol:
                raise NamespaceError(
                    f"{info.path}: `{lam_name}` would refer to two different definitions; "
                    f"import one of them with `as` ({where})"
                )
            claimed[lam_name] = symbol
            new = renamed.get((def_path, def_name))
            if lam_name == def_name and new is not None and def_name not in info.symbols:
                # Raw ``go!`` code in this module may name the import directly.
                plan.go_renames.update(go_renames_for(def_name, kind, new))
            if kind in TYPE_KINDS:
                target = new or def_name
                if lam_name != target:
                    if lam_name != def_name and new is None and def_name in bound:
                        raise NamespaceError(
                            f"{info.path}: class/interface `{def_name}` cannot be used as `{lam_name}` "
                            f"because `{def_name}` is already bound in this module and the class "
                            f"cannot be renamed ({where})"
                        )
                    plan.rewrites[lam_name] = target
                return
            go = new or default_go(def_name, kind)
            if lam_name != def_name or new is not None:
                target_map = plan.function_go_names if kind in FUNCTION_KINDS else plan.const_go_names
                target_map[lam_name] = go
                if kind in FUNCTION_KINDS and new is None and lam_name != def_name:
                    plan.function_aliases[lam_name] = def_name
            if def_name in colliding or lam_name != def_name:
                plan.metadata_from[lam_name] = (def_path, def_name)

        for binding in info.bindings:
            target_path = resolve(binding.module, path)
            if target_path is None or target_path not in modules:
                continue
            if binding.imported is None:
                members = module_member_accesses(info.tree, binding.local)
                access: dict[str, str] = {}
                for member in sorted(members):
                    symbol = resolve_symbol(target_path, member)
                    if symbol is None:
                        continue
                    lam_name = member
                    if (member in bound and claimed.get(member) != symbol) or (
                        member in claimed and claimed[member] != symbol
                    ):
                        lam_name = f"{binding.local}__{member}"
                    if symbol[2] in TYPE_KINDS:
                        lam_name = renamed.get((symbol[0], symbol[1]), symbol[1])
                        if lam_name in claimed and claimed[lam_name] != symbol:
                            raise NamespaceError(
                                f"{info.path}: `{binding.local}.{member}` refers to a class that "
                                f"cannot be renamed and `{member}` is already bound in this module"
                            )
                        claimed[lam_name] = symbol
                        access[member] = lam_name
                        continue
                    access[member] = lam_name
                    register(lam_name, symbol, f"`{binding.local}.{member}`")
                if access:
                    plan.module_access[binding.local] = access
                continue
            symbol = resolve_symbol(target_path, binding.imported)
            if symbol is None:
                continue
            lam_name = binding.local
            if binding.local != binding.imported:
                canonical = binding.imported
                if canonical not in bound and claimed.get(canonical) in (None, symbol):
                    plan.rewrites[binding.local] = canonical
                    lam_name = canonical
            register(lam_name, symbol, f"`from {binding.module} import {binding.imported}`")

        # alias -> canonical -> renamed class: resolve chains in one lookup.
        for key in list(plan.rewrites):
            seen = {key}
            value = plan.rewrites[key]
            while value in plan.rewrites and value not in seen and plan.rewrites[value] != value:
                seen.add(value)
                value = plan.rewrites[value]
            plan.rewrites[key] = value

        for name in colliding:
            if name not in claimed:
                plan.hidden.add(name)
        plans[path] = plan
    return plans


# ── Transpiler metadata view ─────────────────────────────────────────

_FUNCTION_META_DICTS = (
    "_result_payload_go_types",
    "_func_defaults",
    "_func_param_counts",
    "_func_param_names",
    "_func_param_go_types",
    "_func_return_go_types",
    "_func_generic_params",
)
_FUNCTION_META_SETS = (
    "_generator_functions",
    "_external_user_functions",
    "_external_private_functions",
    "_variadic_functions",
)


def _meta_keys(mapping: dict, name: str) -> list:
    prefix = f"{name}#"
    return [key for key in mapping if key == name or (isinstance(key, str) and key.startswith(prefix))]


def apply_metadata_view(transpiler, plan: NamespacePlan, sources: dict[Path, object]) -> None:
    """Make imported-function metadata reflect this module's namespace.

    ``sources`` maps a module path to an object exposing that module's own
    metadata attributes (a collector or transpiler for that module).
    """
    names = set(plan.hidden) | set(plan.metadata_from)
    for name in names:
        for attr in _FUNCTION_META_DICTS:
            mapping = getattr(transpiler, attr, None)
            if isinstance(mapping, dict):
                for key in _meta_keys(mapping, name):
                    del mapping[key]
        for attr in _FUNCTION_META_SETS:
            values = getattr(transpiler, attr, None)
            if isinstance(values, set):
                values.discard(name)
        transpiler._overload_variants.pop(name, None)
        transpiler._overloaded_functions.pop(name, None)
    for local, (def_path, def_name) in plan.metadata_from.items():
        source = sources.get(def_path)
        if source is None:
            continue
        for attr in _FUNCTION_META_DICTS:
            src_map = getattr(source, attr, None)
            dst_map = getattr(transpiler, attr, None)
            if not isinstance(src_map, dict) or not isinstance(dst_map, dict):
                continue
            for key in _meta_keys(src_map, def_name):
                new_key = local + key[len(def_name):]
                dst_map[new_key] = copy.deepcopy(src_map[key])
        if def_name in getattr(source, "_user_functions", set()) or def_name in getattr(source, "_external_user_functions", set()):
            transpiler._external_user_functions.add(local)
        for attr, source_attr in (
            ("_external_private_functions", "_private_functions"),
            ("_generator_functions", "_generator_functions"),
            ("_variadic_functions", "_variadic_functions"),
        ):
            if def_name in getattr(source, source_attr, set()):
                getattr(transpiler, attr).add(local)
        variants = getattr(source, "_overload_variants", {}).get(def_name)
        if variants:
            renamed_variants = []
            for variant in variants:
                item = copy.deepcopy(variant)
                meta_key = str(item.get("meta_key") or "")
                if meta_key.startswith(def_name):
                    item["meta_key"] = local + meta_key[len(def_name):]
                renamed_variants.append(item)
            transpiler._overload_variants[local] = renamed_variants
            transpiler._overloaded_functions[local] = {
                int(variant.get("arity", 0)) for variant in renamed_variants
            }
