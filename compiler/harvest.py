"""Cached metadata harvest of the library modules of a build.

Before the libraries are transpiled, the build collects every module's
declarations (classes, interfaces, signatures, defaults, overloads ...) from
its namespaced tree with a fresh collector, in two passes: the declared type
names of every module (the program's interfaces and generic classes), then
each module's metadata with those known. Both results are pure functions of
the module's source, its namespace plan, the compiler and (pass 2) the
program's interfaces and generic classes, so they are cached on disk by
those (``<cache_dir>/harvest/``); a module whose results are cached is
neither loaded nor rewritten.
"""
from __future__ import annotations

import hashlib
import pickle
from types import SimpleNamespace

# What the build reads from a module's metadata collector (the harvest
# merge and the namespace metadata view).
COLLECTOR_ATTRS = (
    "_class_names", "_interfaces", "_static_methods", "_static_vars",
    "_result_payload_go_types", "_func_defaults", "_func_param_counts",
    "_func_param_names", "_func_param_go_types", "_func_return_go_types",
    "_method_return_types", "_func_generic_params", "_func_generic_order",
    "_func_param_lam_types", "_func_return_lam_types", "_method_generic_order",
    "_generic_instance_methods", "_generic_method_returns", "_generator_functions",
    "_overload_variants", "_user_functions", "_private_functions",
    "_instance_methods", "_private_methods", "class_fields", "_field_lam_types",
    "class_bases", "_class_method_sigkeys", "_class_own_init", "class_field_defaults",
    "_class_type_param_names", "_generic_classes", "_external_user_functions",
    "_external_private_functions", "_variadic_functions",
)
FORMAT = 1


def snapshot(collector) -> SimpleNamespace:
    return SimpleNamespace(**{
        attr: getattr(collector, attr) for attr in COLLECTOR_ATTRS if hasattr(collector, attr)
    })


class HarvestCache:
    def __init__(self, use_disk: bool, stdlib_path, go_module: str) -> None:
        from compiler import cache as _cache

        self.use_disk = use_disk
        self._salt = (
            f"lam-harvest-v{FORMAT}\0{_cache.compiler_version()}\0"
            f"{stdlib_path or ''}\0{go_module}\0"
        ).encode("utf-8")

    def key(self, kind: str, text: str, plan, extra: bytes = b"") -> str:
        h = hashlib.sha256(self._salt)
        h.update(kind.encode("ascii") + b"\0")
        h.update((plan.cache_key() if plan is not None else "<no plan>").encode("utf-8") + b"\0")
        h.update(extra + b"\0")
        h.update(text.encode("utf-8", "surrogatepass"))
        return h.hexdigest()

    def load(self, key: str):
        if not self.use_disk:
            return None
        from compiler import cache as _cache
        from compiler.frontend import _loads

        data = _cache.load_blob("harvest", key)
        if data is None:
            return None
        try:
            return _loads(data)
        except Exception:  # noqa: BLE001 - a damaged entry is a miss
            return None

    def save(self, key: str, value) -> None:
        if not self.use_disk:
            return
        from compiler import cache as _cache

        try:
            data = pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL)
        except Exception:  # noqa: BLE001 - not picklable: not cached
            return
        _cache.save_blob("harvest", key, data)


def program_types_digest(interfaces: set, generic_classes: dict) -> bytes:
    h = hashlib.sha256()
    h.update(repr(sorted(interfaces)).encode("utf-8"))
    h.update(b"\0")
    h.update(repr(list(generic_classes.items())).encode("utf-8"))
    return h.hexdigest().encode("ascii")
