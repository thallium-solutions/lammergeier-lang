"""The metadata a library transpile hands back to the build, as a delta.

A library module is transpiled with the metadata of the whole program
injected (every library's classes, signatures, defaults, overloads ...), and
its result has always been the transpiler's metadata tables *after* the
walk: the injected program-wide tables plus what the module adds or changes.
The build merges those results in module order. Stored and merged whole,
that is quadratic: every library's cache entry repeats the whole program's
metadata (megabytes each), which every warm build decoded and merged once
per library.

So a result is kept as a delta against :class:`Base`, the injected tables
common to every library of the build (a fresh transpiler with the program
metadata injected): per table, the entries the module set or changed, the
entries it removed, and the key order when it is not the natural one (base
order, then new keys). :class:`Merger` folds the deltas exactly like the
merge of the full tables did (``dict.update`` in module order, set unions,
list concatenation), in time proportional to the base plus the deltas, and
:func:`materialize` rebuilds a full table where one is still needed. The
base enters the cache key (``Base.digest``), so a stored delta is always
applied to the base it was computed against.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

# The tables of a library result (``_transpile_lib``), by fold:
# ``acc.update(table)`` in module order.
UPDATE_DICTS = (
    "func_defaults",
    "func_param_counts",
    "method_return_types",
    "result_payload_go_types",
    "func_param_names",
    "func_param_go_types",
)
# ``acc.setdefault(key, set()).update(values)``.
UNION_DICT_SETS = ("static_methods",)
# ``acc.setdefault(key, []).extend(values)``.
EXTEND_DICT_LISTS = ("overload_variants",)
# ``acc |= table``.
UNION_SETS = ("class_names", "variadic_functions", "user_functions", "private_functions")
# Kept whole (small). ``func_defaults`` is a delta too: against the base's
# defaults lowered to Go source (the form a cache entry stores), so a fresh
# transpile's (``Tree`` values) differs on every key, a stored one on few.
FULL = ("static_vars",)

DICT_FIELDS = UPDATE_DICTS + UNION_DICT_SETS + EXTEND_DICT_LISTS
FIELDS = DICT_FIELDS + UNION_SETS + FULL

# Transpiler attribute of each table.
ATTRS = {
    "class_names": "_class_names",
    "static_methods": "_static_methods",
    "static_vars": "_static_vars",
    "func_defaults": "_func_defaults",
    "func_param_counts": "_func_param_counts",
    "variadic_functions": "_variadic_functions",
    "user_functions": "_user_functions",
    "private_functions": "_private_functions",
    "method_return_types": "_method_return_types",
    "result_payload_go_types": "_result_payload_go_types",
    "overload_variants": "_overload_variants",
    "func_param_names": "_func_param_names",
    "func_param_go_types": "_func_param_go_types",
}

ENTRY_VERSION = 10


# ── JSON form of a table value (the library cache stores JSON) ─────────


def _ser_value(field: str, value: Any) -> Any:
    if field == "func_defaults":
        return [[idx, val] for idx, val in value]
    if field == "static_methods":
        return sorted(value)
    if field == "overload_variants":
        return [dict(variant, sig=list(variant.get("sig", ()))) for variant in value]
    if field in ("func_param_names", "func_param_go_types"):
        return list(value)
    return value


def _deser_value(field: str, value: Any) -> Any:
    if field == "func_defaults":
        return [(int(idx), val) for idx, val in value]
    if field == "static_methods":
        return set(value)
    if field == "overload_variants":
        return [dict(variant, sig=tuple(variant.get("sig", ()))) for variant in value]
    if field in ("func_param_names", "func_param_go_types"):
        return list(value)
    return value


def ser_table(field: str, table: Any) -> Any:
    if field in UNION_SETS:
        return sorted(table)
    if field == "static_vars":
        return {
            cls: {name: bool(is_private) for name, is_private in fields.items()}
            for cls, fields in (table or {}).items()
        }
    return {k: _ser_value(field, v) for k, v in table.items()}


def deser_table(field: str, table: Any) -> Any:
    if field in UNION_SETS:
        return set(table)
    if field == "static_vars":
        return {k: {str(name): bool(is_private) for name, is_private in v.items()} for k, v in table.items()}
    return {k: _deser_value(field, v) for k, v in table.items()}


# ── Deltas ────────────────────────────────────────────────────────────


def _dict_delta(final: dict, base: dict) -> dict:
    """``set``: entries new or changed; ``del``: base keys removed; ``tail``:
    the keys that follow the base order (when not just the new keys in
    ``set`` order): the table's key order is the base's (minus ``del`` and
    ``tail``) followed by ``tail``. Keys the module removed and put back
    (the namespace metadata view does) move to the tail."""
    missing = object()
    changed = {k: v for k, v in final.items() if base.get(k, missing) != v}
    removed = [k for k in base if k not in final]
    # Longest prefix of the final order that follows the base order.
    position = {k: i for i, k in enumerate(base)}
    keys = list(final)
    cursor = 0
    split = len(keys)
    for i, k in enumerate(keys):
        at = position.get(k)
        if at is None or at < cursor:
            split = i
            break
        cursor = at + 1
    tail = keys[split:]
    natural_tail = [k for k in changed if k not in base]
    return {"set": changed, "del": removed, "tail": None if tail == natural_tail else tail}


def table_delta(field: str, final: Any, base: Any) -> Any:
    """Delta of ``final`` against ``base`` (both in the same form: Python
    objects or the JSON form); the value itself for a :data:`FULL` table."""
    if field in FULL:
        return final
    if field in UNION_SETS:
        final_set, base_set = set(final), set(base)
        return {"add": sorted(final_set - base_set), "del": sorted(base_set - final_set)}
    return _dict_delta(final, base)


def ser_delta(field: str, delta: Any) -> Any:
    if field in FULL:
        return ser_table(field, delta)
    if field in UNION_SETS:
        return delta
    return {
        "set": {k: _ser_value(field, v) for k, v in delta["set"].items()},
        "del": list(delta["del"]),
        "tail": delta["tail"],
    }


def deser_delta(field: str, delta: Any) -> Any:
    if field in FULL:
        return deser_table(field, delta)
    if field in UNION_SETS:
        return {"add": list(delta["add"]), "del": list(delta["del"])}
    tail = delta.get("tail")
    return {
        "set": {k: _deser_value(field, v) for k, v in delta["set"].items()},
        "del": list(delta["del"]),
        "tail": list(tail) if tail is not None else None,
    }


class Base:
    """The tables common to every library transpile of a build, in Python
    form (``tables``, as deserialised from ``ser``: the form a cache hit
    returns) and JSON form (``ser``)."""

    def __init__(self, raw_tables: dict[str, Any]) -> None:
        self.ser = {field: ser_table(field, raw_tables[field]) for field in FIELDS if field not in FULL}
        self.tables = {field: deser_table(field, value) for field, value in self.ser.items()}
        self.order_index = {
            field: {k: i for i, k in enumerate(self.tables[field])} for field in DICT_FIELDS
        }
        payload = json.dumps(self.ser, sort_keys=False, separators=(",", ":"), default=repr)
        self.digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @classmethod
    def from_transpiler(cls, transpiler, lowered_defaults: dict) -> "Base":
        tables = {field: getattr(transpiler, ATTRS[field]) for field in FIELDS}
        tables["func_defaults"] = lowered_defaults
        return cls(tables)


def raw_tables(transpiler) -> dict[str, Any]:
    return {field: getattr(transpiler, ATTRS[field]) for field in FIELDS}


def delta_from_raw(raw: dict[str, Any], base: Base, fields: tuple[str, ...] = FIELDS) -> dict[str, Any]:
    """The delta of a freshly transpiled library's tables (Python form)."""
    return {
        field: (raw[field] if field in FULL else table_delta(field, raw[field], base.tables[field]))
        for field in fields
    }


def entry_payload(ser_tables: dict[str, Any], base: Base) -> dict[str, Any]:
    """JSON-form deltas of a library's tables (already in JSON form) for the
    cache entry."""
    out = {}
    for field in FIELDS:
        if field in FULL:
            out[field] = ser_tables[field]
        else:
            delta = table_delta(field, ser_tables[field], base.ser[field])
            out[field] = delta
    return out


def delta_from_entry(payload: dict[str, Any]) -> dict[str, Any] | None:
    try:
        return {field: deser_delta(field, payload[field]) for field in FIELDS}
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


# ── Full tables ───────────────────────────────────────────────────────


def _tail(delta: dict, base_table: dict) -> list:
    tail = delta["tail"]
    if tail is None:
        return [k for k in delta["set"] if k not in base_table]
    return tail


def materialize(field: str, delta: Any, base: Base) -> Any:
    """The full table a delta stands for."""
    if field in FULL:
        return delta
    if field in UNION_SETS:
        removed = set(delta["del"])
        return {value for value in base.tables[field] if value not in removed} | set(delta["add"])
    table = base.tables[field]
    changed = delta["set"]
    tail = _tail(delta, table)
    skip = set(delta["del"])
    skip.update(tail)
    full = {k: (changed[k] if k in changed else v) for k, v in table.items() if k not in skip}
    for k in tail:
        full[k] = changed[k] if k in changed else table[k]
    return full


class LibView:
    """Attribute view of one library's full tables (``_func_defaults`` ...),
    built on first access (the namespace metadata view reads a few)."""

    def __init__(self, delta: dict[str, Any], base: Base, fields: tuple[str, ...] = FIELDS,
                 extra: dict[str, Any] | None = None) -> None:
        self._delta = delta
        self._base = base
        self._cache: dict[str, Any] = {}
        self._extra = dict(extra or {})
        self._by_attr = {ATTRS[field]: field for field in fields}

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)
        extra = self.__dict__.get("_extra", {})
        if name in extra:
            return extra[name]
        field = self.__dict__.get("_by_attr", {}).get(name)
        if field is None:
            raise AttributeError(name)
        cache = self.__dict__["_cache"]
        if field not in cache:
            cache[field] = materialize(field, self._delta[field], self._base)
        return cache[field]


# ── Merge ─────────────────────────────────────────────────────────────


class _UpdateFold:
    """``acc.update(full_i)`` for every library ``i``, from deltas.

    ``dirty``: base keys whose value in ``acc`` may differ from the base's
    (set by a delta since); ``missing``: base keys not in ``acc`` yet. A
    library then costs the size of its delta (plus ``dirty``), not the size
    of the base: ``full_i`` is the base (minus ``del`` and ``tail``, in base
    order, changed entries in place) followed by ``tail``, and updating
    ``acc`` with it inserts the keys it lacks in that order."""

    def __init__(self, field: str, base: Base) -> None:
        self.field = field
        self.base_obj = base
        self.base = base.tables[field]
        self.order_index = base.order_index[field]
        self.acc: dict = {}
        self.dirty: set = set()
        self.missing: set | None = None

    def add(self, delta: dict) -> None:
        acc, base = self.acc, self.base
        removed = set(delta["del"])
        changed = delta["set"]
        if self.missing is None:
            for k, v in materialize(self.field, delta, self.base_obj).items():
                acc[k] = v
            self.dirty = {k for k in changed if k in base}
            self.missing = {k for k in base if k not in acc}
            return
        tail = _tail(delta, base)
        tail_set = set(tail)
        if self.missing:
            for k in sorted(self.missing - removed - tail_set, key=self.order_index.__getitem__):
                acc[k] = base[k]
            self.missing &= removed | tail_set
        for k in list(self.dirty):
            if k not in removed:
                acc[k] = base[k]
                self.dirty.discard(k)
        for k in tail:
            acc[k] = changed[k] if k in changed else base[k]
            self.missing.discard(k)
        for k, v in changed.items():
            acc[k] = v
            if k in base:
                self.dirty.add(k)


class _UnionDictFold:
    """``acc.setdefault(key, set()).update(values)`` for every library.

    ``pending``: base keys whose base value is not in ``acc`` yet."""

    def __init__(self, field: str, base: Base) -> None:
        self.field = field
        self.base_obj = base
        self.base = base.tables[field]
        self.order_index = base.order_index[field]
        self.acc: dict = {}
        self.pending: set | None = None

    def add(self, delta: dict) -> None:
        acc, base = self.acc, self.base
        removed = set(delta["del"])
        changed = delta["set"]
        if self.pending is None:
            for k, v in materialize(self.field, delta, self.base_obj).items():
                acc.setdefault(k, set()).update(v)
            self.pending = {k for k in base if k in removed or k in changed}
            return
        tail = _tail(delta, base)
        tail_set = set(tail)
        if self.pending:
            for k in sorted(self.pending - removed - tail_set, key=self.order_index.__getitem__):
                if k in changed:
                    acc.setdefault(k, set()).update(changed[k])
                else:
                    acc.setdefault(k, set()).update(base[k])
                    self.pending.discard(k)
        for k in tail:
            if k in changed:
                acc.setdefault(k, set()).update(changed[k])
            else:
                acc.setdefault(k, set()).update(base[k])
                self.pending.discard(k)
        for k, v in changed.items():
            acc.setdefault(k, set()).update(v)


class _SetFold:
    """``acc |= table`` for every library. ``missing``: base values not in
    ``acc`` yet."""

    def __init__(self, field: str, base: Base) -> None:
        self.base = base.tables[field]
        self.acc: set = set()
        self.missing: set | None = None

    def add(self, delta: dict) -> None:
        removed = set(delta["del"])
        if self.missing is None:
            self.acc.update(v for v in self.base if v not in removed)
            self.missing = set(removed)
        elif self.missing:
            self.acc.update(self.missing - removed)
            self.missing &= removed
        self.acc.update(delta["add"])
        if self.missing:
            self.missing.difference_update(delta["add"])


class Merger:
    """Folds library deltas, in module order, into the program tables the
    main transpile receives (``tables()``), exactly as merging the full
    tables did (``dict.update``, set unions, ``list.extend``)."""

    def __init__(self, base: Base) -> None:
        self.base = base
        self._update = {f: _UpdateFold(f, base) for f in UPDATE_DICTS}
        self._union_dict = {f: _UnionDictFold(f, base) for f in UNION_DICT_SETS}
        self._sets = {f: _SetFold(f, base) for f in UNION_SETS}
        self._extend: dict[str, dict] = {f: {} for f in EXTEND_DICT_LISTS}
        self.static_vars: dict = {}

    def add(self, delta: dict[str, Any]) -> None:
        for field, fold in self._update.items():
            fold.add(delta[field])
        for field, fold in self._union_dict.items():
            fold.add(delta[field])
        for field, fold in self._sets.items():
            fold.add(delta[field])
        for field, acc in self._extend.items():
            for k, v in materialize(field, delta[field], self.base).items():
                acc.setdefault(k, []).extend(v)
        for cls_name, fields in delta["static_vars"].items():
            self.static_vars.setdefault(cls_name, {}).update(fields)

    def tables(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for field, fold in self._update.items():
            out[field] = fold.acc
        for field, fold in self._union_dict.items():
            out[field] = fold.acc
        for field, fold in self._sets.items():
            out[field] = fold.acc
        out.update(self._extend)
        out["static_vars"] = self.static_vars
        return out
