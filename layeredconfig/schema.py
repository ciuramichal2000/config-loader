"""Schema: declared types, required keys, defaults, and value coercion.

Environment variables and command-line overrides arrive as strings, so the
schema is where "5432" becomes 5432 and "true" becomes True. Conversions are
deliberately strict (no "1_000", no "nan", no int-as-bool) because a config
value that silently means something else is worse than a startup error.
"""

from __future__ import annotations

import copy
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .errors import CoercionError, Problem, SchemaError, ValidationError
from .layers import set_path
from .masking import is_secret_path


_INT_TEXT = re.compile(r"[+-]?[0-9]+")
_FLOAT_TEXT = re.compile(r"[+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
_TRUE_WORDS = frozenset({"true", "1", "yes", "on"})
_FALSE_WORDS = frozenset({"false", "0", "no", "off"})


def _show(raw: Any, secret: bool) -> str:
    """Describe an offending value for an error message without leaking secrets."""
    if secret:
        return "<masked>"
    if isinstance(raw, (list, dict)):
        return type(raw).__name__
    text = repr(raw)
    return text if len(text) <= 40 else text[:37] + "..."


def _to_str(raw: Any, secret: bool) -> str:
    if isinstance(raw, str):
        return raw
    raise CoercionError(f"expected str, got {_show(raw, secret)}")


def _to_int(raw: Any, secret: bool) -> int:
    if isinstance(raw, int) and not isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        text = raw.strip()
        if _INT_TEXT.fullmatch(text):
            try:
                return int(text)
            except ValueError:
                pass  # digit strings beyond Python's int conversion limit
    raise CoercionError(f"expected int, got {_show(raw, secret)}")


def _to_float(raw: Any, secret: bool) -> float:
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        candidate: Any = raw
    elif isinstance(raw, str) and _FLOAT_TEXT.fullmatch(raw.strip()):
        candidate = raw.strip()
    else:
        raise CoercionError(f"expected float, got {_show(raw, secret)}")
    try:
        number = float(candidate)
    except (OverflowError, ValueError):
        raise CoercionError(f"expected a finite float, got {_show(raw, secret)}") from None
    if not math.isfinite(number):
        raise CoercionError(f"expected a finite float, got {_show(raw, secret)}")
    return number


def _to_bool(raw: Any, secret: bool) -> bool:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        word = raw.strip().lower()
        if word in _TRUE_WORDS:
            return True
        if word in _FALSE_WORDS:
            return False
    raise CoercionError(
        f"expected bool (true/false, yes/no, on/off, 1/0), got {_show(raw, secret)}"
    )


_SCALAR_COERCERS = {str: _to_str, int: _to_int, float: _to_float, bool: _to_bool}


def _to_list(raw: Any, item_kind: type, secret: bool) -> list[Any]:
    if isinstance(raw, str):
        if not raw.strip():
            return []
        items: list[Any] = [part.strip() for part in raw.split(",")]
        if "" in items:
            raise CoercionError("empty item in comma-separated list")
    elif isinstance(raw, list):
        items = raw
    else:
        raise CoercionError(f"expected list, got {_show(raw, secret)}")
    converted = []
    for index, item in enumerate(items):
        try:
            converted.append(_SCALAR_COERCERS[item_kind](item, secret))
        except CoercionError as exc:
            raise CoercionError(f"item {index}: {exc}") from None
    return converted


@dataclass(frozen=True)
class Field:
    """Declaration of one configuration key.

    ``kind`` is str, int, float, bool or list; lists hold ``item_kind`` items
    (a scalar kind). ``default=None`` means "no default": None is not a valid
    value for any kind, so it needs no separate marker. ``secret=True`` masks
    the key even if its name does not look secret. A required field cannot
    also have a default.
    """

    kind: type = str
    default: Any = None
    required: bool = False
    secret: bool = False
    item_kind: type = str

    def __post_init__(self) -> None:
        if self.kind is not list and self.kind not in _SCALAR_COERCERS:
            raise SchemaError(f"unsupported field type {self.kind!r}")
        if self.item_kind not in _SCALAR_COERCERS:
            raise SchemaError(f"unsupported list item type {self.item_kind!r}")
        if self.required and self.default is not None:
            raise SchemaError("a required field cannot also declare a default")
        if self.default is not None:
            try:
                # Always masked: a default may well be a credential.
                coerce(self.default, self, secret=True)
            except CoercionError as exc:
                raise SchemaError(f"invalid default: {exc}") from exc


def coerce(raw: Any, field: Field, *, secret: bool = False) -> Any:
    """Convert ``raw`` to the type ``field`` declares, or raise CoercionError."""
    if field.kind is list:
        return _to_list(raw, field.item_kind, secret)
    return _SCALAR_COERCERS[field.kind](raw, secret)


def _ancestors(path: str) -> list[str]:
    """Proper dotted prefixes of ``path``: "a.b.c" -> ["a", "a.b"]."""
    parts = path.split(".")
    return [".".join(parts[:end]) for end in range(1, len(parts))]


class Schema:
    """A set of Fields addressed by dotted path.

    ``fields`` may be nested (``{"db": {"port": Field(int)}}``) or use dotted
    keys (``{"db.port": Field(int)}``); both describe the key ``db.port``. With
    ``reject_unknown=True`` any key in the merged configuration that the schema
    does not declare is an error, which catches typos such as ``APP_DB__PROT``.
    """

    def __init__(self, fields: Mapping[str, Any], *, reject_unknown: bool = False) -> None:
        self.reject_unknown = reject_unknown
        self.fields: dict[str, Field] = {}
        self._collect(fields, "")
        for path in self.fields:
            for ancestor in _ancestors(path):
                if ancestor in self.fields:
                    raise SchemaError(f"'{ancestor}' is declared as a value and as a section")

    def _collect(self, spec: Mapping[str, Any], prefix: str) -> None:
        for key, entry in spec.items():
            path = prefix + key if isinstance(key, str) else ""
            if "" in path.split("."):
                raise SchemaError(f"invalid schema key {key!r}")
            if isinstance(entry, Field):
                if path in self.fields:
                    raise SchemaError(f"'{path}' is declared twice")
                self.fields[path] = entry
            elif isinstance(entry, Mapping):
                self._collect(entry, path + ".")
            else:
                raise SchemaError(f"'{path}' must be a Field or a mapping of Fields")

    def defaults(self) -> dict[str, Any]:
        """Nested dict of the declared defaults, used as the lowest layer."""
        tree: dict[str, Any] = {}
        for path, field in self.fields.items():
            if field.default is not None:
                set_path(tree, path.split("."), copy.deepcopy(field.default), "schema")
        return tree

    def secret_paths(self) -> frozenset[str]:
        """Paths of fields explicitly flagged ``secret=True``."""
        return frozenset(path for path, field in self.fields.items() if field.secret)

    def validate(self, flat: Mapping[str, Any]) -> dict[str, Any]:
        """Check and coerce a flat ``{dotted path: raw value}`` mapping.

        Defaults are expected to be in ``flat`` already (they are the lowest
        layer). Returns the coerced flat mapping, or raises ValidationError
        listing every problem found.
        """
        problems: list[Problem] = []
        result: dict[str, Any] = {}
        blocked: set[str] = set()
        extra_secrets = self.secret_paths()

        for path, value in flat.items():
            if path in self.fields:
                continue
            below = [field for field in self.fields if field.startswith(path + ".")]
            owner = next((a for a in _ancestors(path) if a in self.fields), None)
            if below:
                if value == {}:
                    result[path] = value  # an empty section is compatible with a section
                else:
                    problems.append(
                        Problem(path, "type", "expected a section here, found a plain value")
                    )
                    blocked.update(below)
            elif owner is not None:
                problems.append(
                    Problem(path, "type", f"'{owner}' is a plain value, not a section")
                )
                blocked.add(owner)
            elif self.reject_unknown:
                problems.append(Problem(path, "unknown", "unknown key"))
            else:
                result[path] = value

        for path, field in self.fields.items():
            if path in blocked:
                continue
            if path in flat:
                try:
                    secret = is_secret_path(path, extra_secrets)
                    result[path] = coerce(flat[path], field, secret=secret)
                except CoercionError as exc:
                    problems.append(Problem(path, "type", str(exc)))
            elif field.required:
                problems.append(Problem(path, "missing", "required key is missing"))

        if problems:
            raise ValidationError(problems)
        return result
