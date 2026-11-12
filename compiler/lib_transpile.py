"""Transpile of one library module of a build (cache lookup, transpile,
cache store), runnable in a worker process (``lamc build --workers N``).

:class:`LibStage` holds what every library transpile of a build reads: the
program-wide metadata harvested from every module (injected into each
library's transpiler), the namespace plans, the class hierarchy, the cache
discriminators and the base tables of the result deltas
(``compiler/lib_results.py``). It is built once, after the harvest, and
handed to the worker processes once (free under ``fork``).
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from compiler import lib_results as _lib_results

# Attributes of a module's metadata collector the namespace metadata view
# (``namespacing.apply_metadata_view``) and the main transpile read.
META_SOURCE_ATTRS = (
    "_result_payload_go_types",
    "_func_defaults",
    "_func_param_counts",
    "_func_param_names",
    "_func_param_go_types",
    "_func_param_lam_types",
    "_func_return_go_types",
    "_func_return_lam_types",
    "_func_generic_params",
    "_generator_functions",
    "_external_user_functions",
    "_external_private_functions",
    "_variadic_functions",
    "_user_functions",
    "_private_functions",
    "_overload_variants",
)


def meta_source_view(collector) -> SimpleNamespace:
    """The metadata of a module collector the later passes read (the same
    objects, not copies)."""
    return SimpleNamespace(**{
        attr: getattr(collector, attr) for attr in META_SOURCE_ATTRS if hasattr(collector, attr)
    })


@dataclass
class LibStage:
    stdlib_path: str | None
    go_module: str
    use_cache: bool
    pre: SimpleNamespace  # the program-wide ``lib_pre_*`` metadata
    ns_plans: dict
    ns_meta_sources: dict
    program_class_bases: dict
    program_method_sigkeys: dict
    program_own_init: set
    program_field_default_classes: set
    program_class_type_params: dict
    lib_module_classes: dict
    cross_lib_extra: bytes
    base: Any = None
    cross_lib_digest: bytes = b""
    _cache_salt: bytes = field(default=b"", repr=False)

    def finish(self) -> "LibStage":
        """Compute the base tables and the cache discriminators (once, in
        the main process)."""
        from compiler.transpiler import GoTranspiler

        base_transpiler = GoTranspiler(
            go_blocks={}, stdlib_path=self.stdlib_path, go_module_name=self.go_module,
        )
        inject_program_metadata(self, base_transpiler)
        self.base = _lib_results.Base.from_transpiler(base_transpiler, lower_defaults(base_transpiler))
        self.cross_lib_digest = hashlib.sha256(self.cross_lib_extra).hexdigest().encode("ascii")
        self._cache_salt = (
            b"lib-v" + str(_lib_results.ENTRY_VERSION).encode("ascii")
            + b"|program:" + self.cross_lib_digest
            + b"|base:" + self.base.digest.encode("ascii")
            + b"|go-module:" + self.go_module.encode("utf-8")
            + b"|stdlib:" + str(self.stdlib_path or "").encode("utf-8")
        )
        # The digest replaces the full text in every key.
        self.cross_lib_extra = b""
        return self

    def cache_extra(self, mod_name: str, mod_file: Path) -> bytes:
        from compiler.lammergeier import _module_init_name

        extra = self._cache_salt
        ns_plan = self.ns_plans.get(mod_file)
        if ns_plan is not None and not ns_plan.is_empty():
            extra += b"|ns:" + ns_plan.cache_key().encode("utf-8")
        extra += b"|init:" + _module_init_name(mod_name).encode("utf-8")
        extra += hierarchy_cache_key(self, mod_file)
        return extra


def inject_program_hierarchy(stage: LibStage, target) -> None:
    target._program_class_bases = stage.program_class_bases
    target._program_method_sigkeys = stage.program_method_sigkeys
    target._program_own_init = stage.program_own_init
    target._program_field_default_classes = stage.program_field_default_classes
    target._program_class_type_params = stage.program_class_type_params


def hierarchy_cache_key(stage: LibStage, mod_file: Path) -> bytes:
    """The parts of the hierarchy a library's output depends on beyond
    its own source: which of its classes other modules (the main file
    included) subclass, and with which methods."""
    own = stage.lib_module_classes.get(mod_file, set())
    if not own:
        return b""
    parents: dict[str, list[str]] = stage.program_class_bases
    entries = []
    for cls in sorted(parents):
        if cls in own:
            continue
        todo, seen = list(parents.get(cls, [])), set()
        while todo:
            base = todo.pop()
            if base in seen:
                continue
            seen.add(base)
            todo.extend(parents.get(base, []))
        if seen & own:
            sig = ",".join(f"{m}={k}" for m, k in sorted(stage.program_method_sigkeys.get(cls, {}).items(), key=lambda kv: kv[0]))
            entries.append(f"{cls}<{','.join(parents.get(cls, []))}#{sig}#{int(cls in stage.program_own_init)}")
    return ("|hier:" + ";".join(entries)).encode("utf-8") if entries else b""


def apply_namespace_plan(target, plan, sources) -> None:
    from compiler import namespacing as _namespacing

    if plan is None or plan.is_empty():
        return
    target._link_function_go_names.update(plan.function_go_names)
    target._link_function_aliases.update(plan.function_aliases)
    target._link_const_go_names.update(plan.const_go_names)
    target._namespace_rewrites.update(plan.rewrites)
    target._namespace_module_access.update(plan.module_access)
    target._namespace_declarations.update(plan.declaration_renames)
    target._ctor_go_names.update(plan.constructor_go_names)
    for value_name, value_type in plan.imported_value_types.items():
        target._var_go_types.setdefault(value_name, value_type)
        # The defining module declares it ``var Name T``.
        target._typed_decls.setdefault(value_name, value_type)
    _namespacing.apply_metadata_view(target, plan, sources)


def inject_program_metadata(stage: LibStage, lib_transpiler) -> None:
    """Inject the union of class names, static members, signatures ... seen
    across *all* libraries so a library that imports another library (e.g.
    user code importing ``Strings`` from ``lamstrings``) can lower calls into
    it correctly even though the called class is defined in another file."""
    P = stage.pre
    lib_transpiler._class_names.update(P.class_names)
    lib_transpiler._interfaces.update(P.interfaces)
    for cls, meths in P.static_methods.items():
        lib_transpiler._static_methods.setdefault(cls, set()).update(meths)
    for cls, fields in P.static_vars.items():
        lib_transpiler._static_vars.setdefault(cls, {}).update(fields)
    lib_transpiler._result_payload_go_types.update(P.result_payload_go_types)
    for key, defaults in P.func_defaults.items():
        lib_transpiler._func_defaults.setdefault(key, defaults)
    for key, count in P.func_param_counts.items():
        lib_transpiler._func_param_counts.setdefault(key, count)
    for key, names in P.func_param_names.items():
        lib_transpiler._func_param_names.setdefault(key, names)
    for key, types in P.func_param_go_types.items():
        lib_transpiler._func_param_go_types.setdefault(key, types)
    for key, type_ in P.func_return_go_types.items():
        lib_transpiler._func_return_go_types.setdefault(key, type_)
    for key, ret in P.method_return_types.items():
        lib_transpiler._method_return_types.setdefault(key, ret)
    for key, names in P.func_generic_params.items():
        lib_transpiler._func_generic_params.setdefault(key, set()).update(names)
    for key, order in P.func_generic_order.items():
        lib_transpiler._func_generic_order.setdefault(key, list(order))
    for key, types in P.func_param_lam_types.items():
        lib_transpiler._func_param_lam_types.setdefault(key, types)
    for key, type_ in P.func_return_lam_types.items():
        lib_transpiler._func_return_lam_types.setdefault(key, type_)
    for key, order in P.method_generic_order.items():
        lib_transpiler._method_generic_order.setdefault(key, list(order))
    for key, names in P.generic_instance_methods.items():
        lib_transpiler._generic_instance_methods.setdefault(key, list(names))
    for key, ret in P.generic_method_returns.items():
        lib_transpiler._generic_method_returns.setdefault(key, ret)
    lib_transpiler._generator_functions.update(P.generator_functions)
    for cls, clause in P.generic_classes.items():
        lib_transpiler._generic_classes.setdefault(cls, clause)
    for key, variants in P.overload_variants.items():
        lib_transpiler._overload_variants.setdefault(key, []).extend(variants)
        lib_transpiler._overloaded_functions.setdefault(key, set()).update(
            int(variant.get("arity", 0)) for variant in variants
        )
    lib_transpiler._external_user_functions.update(P.user_functions)
    lib_transpiler._external_private_functions.update(P.private_functions)
    for cls, methods in P.instance_methods.items():
        lib_transpiler._external_instance_methods.setdefault(cls, set()).update(methods)
    for cls, fields in P.field_names.items():
        lib_transpiler._external_field_names.setdefault(cls, set()).update(fields)
    for cls, fields in P.field_types.items():
        lib_transpiler._external_field_types.setdefault(cls, {}).update(fields)
    lib_transpiler._external_field_lam_types.update(P.field_lam_types)


def lower_defaults(lib_transpiler) -> dict:
    """``_func_defaults`` with every default lowered to its Go source (the
    form the library cache stores): ``Tree`` values are not JSON. A function
    whose default cannot be lowered ahead of call-site context (e.g. it
    references a caller-scoped binding) is left out."""
    lowered_defaults: dict = {}
    for fn, pairs in lib_transpiler._func_defaults.items():
        lowered_pairs = []
        for idx, node in pairs:
            if isinstance(node, str):
                lowered_pairs.append((idx, node))
            else:
                try:
                    target_type = lib_transpiler._param_go_type_at(
                        lib_transpiler._func_param_go_types.get(fn, []),
                        idx,
                    )
                    lowered_pairs.append((
                        idx,
                        lib_transpiler._typed_value_to_go(node, target_type),
                    ))
                except Exception:
                    lowered_pairs = None
                    break
        if lowered_pairs is not None:
            lowered_defaults[fn] = lowered_pairs
    return lowered_defaults


def _strip_main(lib_go: str) -> str:
    """Remove any ``func main()`` block left in the library (the transpiler
    omits it) and the ``//line`` directive right above it."""
    lines = lib_go.split("\n")
    filtered = []
    skip_main = False
    for line in lines:
        if line.strip().startswith("func main()"):
            # Drop any //line pragma that was sitting right above
            # ``func main()``; it's now attached to nothing useful.
            if filtered and filtered[-1].lstrip().startswith("//line "):
                filtered.pop()
            skip_main = True
            continue
        if skip_main:
            # ``main``'s body ends at the first column-0 ``}``; keep
            # what follows (the module's top-level statement function).
            if line == "}":
                skip_main = False
            continue
        filtered.append(line)
    return "\n".join(filtered)


def lookup_cached(stage: LibStage, mod_name: str, mod_file: Path):
    """``(mod_name, go_src, delta)`` from the cache, else ``None``."""
    from compiler import cache as _lamcache
    from compiler.lammergeier import _retarget_line_directives

    if not stage.use_cache:
        return None
    lib_source_bytes = mod_file.read_bytes()
    entry = _lamcache.load(lib_source_bytes, extra=stage.cache_extra(mod_name, mod_file))
    if entry is None or entry.get("version") != _lib_results.ENTRY_VERSION:
        return None
    go_src = entry.get("go_src")
    payload = entry.get("tables")
    if not isinstance(go_src, str) or not isinstance(payload, dict):
        return None
    delta = _lib_results.delta_from_entry(payload)
    if delta is None:
        return None
    # The cache is keyed on content, so the entry may come from an
    # identical copy of this library at another path. Re-point its
    # ``//line`` directives at the file actually being compiled.
    return mod_name, _retarget_line_directives(go_src, str(mod_file)), delta


def transpile_library(stage: LibStage, frontend, mod_name: str, mod_file: Path):
    """Transpile one library module (not in the cache): ``(mod_name, go_src,
    delta)``. Raises ``SyntaxDiagnosticError`` / ``TranspileDiagnosticError``
    for errors in the module."""
    from compiler import cache as _lamcache
    from compiler import namespacing as _namespacing
    from compiler.lammergeier import (
        UnexpectedInput, _module_init_name, apply_lammergeier_aliases, render_syntax_error,
    )
    from compiler.syntax_errors import SyntaxDiagnosticError
    from compiler.transpiler import GoTranspiler

    lib_source_bytes = mod_file.read_bytes()
    lib_source = lib_source_bytes.decode("utf-8")
    ns_plan = stage.ns_plans.get(mod_file)

    # Apply the same parse preprocessing pipeline used for main
    # files, including LAMMERGEIER.* aliases and go! extraction.
    lib_meta, lib_tree = frontend.module(mod_file, lib_source)
    lib_parse_input = lib_meta.parse_input
    lib_source = apply_lammergeier_aliases(lib_source)
    lib_go_blocks = lib_parse_input.go_blocks
    if ns_plan is not None and ns_plan.go_renames:
        lib_go_blocks = {
            key: _namespacing.rename_go_identifiers(code, ns_plan.go_renames)
            for key, code in lib_go_blocks.items()
        }
    if lib_tree is None:
        try:
            frontend.raise_parse_error(lib_parse_input)
        except UnexpectedInput as e:
            rendered = render_syntax_error(e, lib_source, mod_file)
            raise SyntaxDiagnosticError(rendered) from None
    lib_transpiler = GoTranspiler(
        go_blocks=lib_go_blocks,
        stdlib_path=stage.stdlib_path,
        go_module_name=stage.go_module,
        source_file=str(mod_file),
    )
    inject_program_metadata(stage, lib_transpiler)
    apply_namespace_plan(lib_transpiler, ns_plan, stage.ns_meta_sources)
    inject_program_hierarchy(stage, lib_transpiler)
    lib_transpiler._module_init_name = _module_init_name(mod_name)
    # The module's ``func main()`` is not emitted, nor the imports only
    # it uses (``print`` / ``fmt``, ``go!`` blocks).
    lib_transpiler._omit_main = True
    go_src = _strip_main(lib_transpiler.transpile(lib_tree))
    # The tables are final here except ``func_defaults`` (the own defaults
    # are qualified below).
    # ``go! { ... }`` blocks are inlined at emit time, so ``go_src`` is
    # self-contained. Default-parameter values are ``Tree`` nodes
    # internally: the cache entry stores them lowered to Go source (the
    # reload side unwraps strings directly in ``_fill_default_args``).
    lowered_defaults = lower_defaults(lib_transpiler) if stage.use_cache else None
    other_fields = tuple(name for name in _lib_results.FIELDS if name != "func_defaults")
    delta = _lib_results.delta_from_raw(_lib_results.raw_tables(lib_transpiler), stage.base, other_fields)

    # ── Cache store ──────────────────────────────────────
    if lowered_defaults is not None:
        stored = dict(delta)
        stored.update(_lib_results.delta_from_raw(
            {"func_defaults": lowered_defaults}, stage.base, ("func_defaults",),
        ))
        _lamcache.save(
            lib_source_bytes,
            {
                "version": _lib_results.ENTRY_VERSION,
                "go_src": go_src,
                "tables": {name: _lib_results.ser_delta(name, stored[name]) for name in _lib_results.FIELDS},
            },
            extra=stage.cache_extra(mod_name, mod_file),
        )

    # The module's own defaults are lowered at call sites in other
    # modules: name its (possibly renamed) values by Go name, as the
    # cached form (lowered above, in this module) does.
    own_defaults = {
        key: pairs for key, pairs in lib_transpiler._func_defaults.items()
        if str(key).split("#", 1)[0] in lib_transpiler._module_function_names
        or str(key).split("#", 1)[0].split(".", 1)[0] in lib_transpiler.class_bases
    }
    _namespacing.qualify_default_values(own_defaults, ns_plan)
    lib_transpiler._func_defaults.update(own_defaults)
    delta.update(_lib_results.delta_from_raw(
        {"func_defaults": lib_transpiler._func_defaults}, stage.base, ("func_defaults",),
    ))

    return mod_name, go_src, delta


# ── Worker side ──────────────────────────────────────────────────────

_WORKER_STAGE: LibStage | None = None


def _init_worker(stage: LibStage) -> None:
    global _WORKER_STAGE
    _WORKER_STAGE = stage


def _worker_task(task: tuple[str, str, str, Any]):
    """``(mod_name, path, text, front-end blobs or None)`` -> ``(result, error)``
    where ``error`` is a picklable stand-in for a Lam error of the module."""
    import sys
    import time

    from compiler import frontend as _frontend
    from compiler.diagnostics import TranspileDiagnosticError
    from compiler.syntax_errors import SyntaxDiagnosticError

    sys.setrecursionlimit(max(sys.getrecursionlimit(), 10000))
    mod_name, path, text, blobs = task
    stage = _WORKER_STAGE
    assert stage is not None
    frontend = _frontend.FrontEnd(use_disk=stage.use_cache, workers=1)
    if blobs is not None:
        frontend.seed(Path(path), text, blobs)
    start = time.process_time()
    try:
        result = transpile_library(stage, frontend, mod_name, Path(path))
    except SyntaxDiagnosticError as exc:
        return None, ("syntax", str(exc)), time.process_time() - start
    except TranspileDiagnosticError as exc:
        return None, ("transpile", (list(exc.errors), exc.source_file)), time.process_time() - start
    return result, None, time.process_time() - start
