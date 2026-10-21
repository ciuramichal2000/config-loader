"""Turning each configuration source into a nested dict, and merging them.

A "layer" is just a nested dict plus a label. Keeping every source in the same
shape means merging and provenance tracking are written once, not per source.
"""

from __future__ import annotations

import copy
import json
import os
import tomllib
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from .errors import SourceError

Layer = tuple[str, Mapping[str, Any]]


def set_path(
    tree: dict[str, Any], parts: Sequence[str], value: Any, source: str
) -> None:
    """Store ``value`` at the nested key ``parts``, creating sections on the way.

    Raises SourceError when the same key would be both a plain value and a
    section (``db=1`` together with ``db.port=2``) because one of them would
    silently vanish otherwise.
    """
    node = tree
    for depth, part in enumerate(parts[:-1], start=1):
        child = node.setdefault(part, {})
        if not isinstance(child, dict):
            clash = ".".join(parts[:depth])
            raise SourceError(f"{source}: '{clash}' is used both as a value and as a section")
        node = child
    if isinstance(node.get(parts[-1]), dict):
        raise SourceError(
            f"{source}: '{'.'.join(parts)}' is used both as a value and as a section"
        )
    node[parts[-1]] = value


def check_keys(tree: Mapping[Any, Any], source: str, prefix: str = "") -> None:
    """Reject keys that cannot round-trip through dotted paths.

    Provenance and schema lookups address values as "a.b.c"; a literal dot in
    a key would make "a.b" ambiguous, so such keys are refused up front.
    """
    for key, value in tree.items():
        if not isinstance(key, str) or not key:
            raise SourceError(f"{source}: keys must be non-empty strings (found {key!r})")
        if "." in key:
            raise SourceError(
                f"{source}: key '{prefix + key}' contains '.', which is the path separator"
            )
        if isinstance(value, Mapping):
            check_keys(value, source, prefix + key + ".")


def read_file_layer(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Read a ``.json`` or ``.toml`` file whose top level is an object/table."""
    name = os.fspath(path)
    suffix = os.path.splitext(name)[1].lower()
    if suffix not in (".json", ".toml"):
        raise SourceError(
            f"{name}: unsupported extension {suffix or '(none)'}; use .json or .toml"
        )
    try:
        with open(name, "rb") as handle:
            # utf-8-sig: Windows editors and PowerShell routinely prepend a BOM,
            # which json.loads would reject.
            text = handle.read().decode("utf-8-sig")
    except OSError as exc:
        raise SourceError(f"{name}: cannot read file ({exc.strerror or type(exc).__name__})") from exc
    except UnicodeDecodeError as exc:
        raise SourceError(f"{name}: not valid UTF-8") from exc
    try:
        data = json.loads(text) if suffix == ".json" else tomllib.loads(text)
    except json.JSONDecodeError as exc:
        raise SourceError(
            f"{name}: invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}"
        ) from exc
    except tomllib.TOMLDecodeError as exc:
        raise SourceError(f"{name}: invalid TOML: {exc}") from exc
    if not isinstance(data, dict):
        raise SourceError(f"{name}: the top level must be an object/table")
    check_keys(data, name)
    return data


def env_layer(env: Mapping[str, str], prefix: str) -> dict[str, Any]:
    """Build a layer from environment variables named ``<prefix>_<KEY>``.

    ``__`` separates nesting levels and names are lower-cased, so with prefix
    "APP" the variable ``APP_DB__POOL_SIZE`` becomes ``db.pool_size``. A single
    underscore stays part of the key name. Values stay strings; coercion is the
    schema's job. Prefix matching is case-sensitive.
    """
    stem = prefix[:-1] if prefix.endswith("_") else prefix
    if not stem:
        raise SourceError("the environment prefix must not be empty")
    marker = stem + "_"
    tree: dict[str, Any] = {}
    for name in sorted(env):
        if not name.startswith(marker):
            continue
        parts = name[len(marker):].lower().split("__")
        if "" in parts:
            raise SourceError(f"environment variable {name}: empty key segment")
        set_path(tree, parts, env[name], f"environment variable {name}")
    return tree


def overrides_layer(items: Iterable[str]) -> dict[str, Any]:
    """Build a layer from ``KEY=VALUE`` strings such as ``db.port=5433``.

    Only the first ``=`` splits, so values may contain ``=``. Error messages
    name the override by position and never echo the value, which may be a
    secret typed on a command line. Repeating a key keeps the last value.
    """
    tree: dict[str, Any] = {}
    for number, item in enumerate(items, start=1):
        key, separator, value = item.partition("=")
        if not separator:
            raise SourceError(f"override #{number}: expected KEY=VALUE")
        parts = key.strip().split(".")
        if "" in parts:
            raise SourceError(f"override #{number}: invalid key '{key.strip()}'")
        set_path(tree, parts, value, f"override #{number} ({key.strip()})")
    return tree


def extract_overrides(argv: Sequence[str]) -> tuple[list[str], list[str]]:
    """Split ``--set KEY=VALUE`` / ``--set=KEY=VALUE`` out of ``argv``.

    Returns ``(overrides, remaining_argv)`` so the application keeps parsing
    its own flags from what is left.
    """
    overrides: list[str] = []
    remaining: list[str] = []
    tokens = iter(argv)
    for token in tokens:
        if token == "--set":
            try:
                overrides.append(next(tokens))
            except StopIteration:
                raise SourceError("--set needs a KEY=VALUE argument") from None
        elif token.startswith("--set="):
            overrides.append(token[len("--set="):])
        else:
            remaining.append(token)
    return overrides, remaining


def _forget(provenance: dict[str, str], path: str) -> None:
    """Drop provenance for ``path`` and everything beneath it."""
    below = path + "."
    for known in [key for key in provenance if key == path or key.startswith(below)]:
        del provenance[known]


def _merge_into(
    target: dict[str, Any],
    provenance: dict[str, str],
    source: Mapping[str, Any],
    label: str,
    prefix: str,
) -> None:
    for key, value in source.items():
        path = prefix + key
        if isinstance(value, Mapping):
            existing = target.get(key)
            if not isinstance(existing, dict):
                _forget(provenance, path)
                existing = target[key] = {}
            _merge_into(existing, provenance, value, label, path + ".")
            if not existing:
                provenance[path] = label
        else:
            _forget(provenance, path)
            target[key] = copy.deepcopy(value)
            provenance[path] = label


def merge_layers(layers: Sequence[Layer]) -> tuple[dict[str, Any], dict[str, str]]:
    """Deep-merge ``layers`` in order; later layers win.

    Returns ``(tree, provenance)`` where provenance maps each leaf's dotted
    path to the label of the layer that supplied its final value. Nested dicts
    merge key by key; every other value (lists included) is replaced whole. A
    value replacing a section of a different shape discards that section's
    provenance too, so the report never lists leaves that no longer exist.
    """
    tree: dict[str, Any] = {}
    provenance: dict[str, str] = {}
    for label, source in layers:
        _merge_into(tree, provenance, source, label, "")
    return tree, provenance


def flatten(tree: Mapping[str, Any], prefix: str = "") -> dict[str, Any]:
    """Map dotted leaf paths to values. An empty dict counts as a leaf."""
    flat: dict[str, Any] = {}
    for key, value in tree.items():
        path = prefix + key
        if isinstance(value, Mapping) and value:
            flat.update(flatten(value, path + "."))
        else:
            flat[path] = value
    return flat


def unflatten(flat: Mapping[str, Any]) -> dict[str, Any]:
    """Inverse of :func:`flatten`."""
    tree: dict[str, Any] = {}
    for path, value in flat.items():
        set_path(tree, path.split("."), value, "result")
    return tree
