"""The resolved Config object and the load() entry point."""

from __future__ import annotations

import copy
import json
import os
from collections.abc import Mapping, Sequence
from typing import Any

from .layers import (
    Layer,
    check_keys,
    env_layer,
    flatten,
    merge_layers,
    overrides_layer,
    read_file_layer,
    unflatten,
)
from .masking import mask_tree
from .schema import Schema


class Config:
    """An immutable-by-convention resolved configuration with provenance.

    ``repr``, ``str``, :meth:`masked` and :meth:`explain` never contain secret
    values, so a Config can be logged as is. :meth:`get`, ``[]`` and
    :meth:`as_dict` return the real values because the application needs them;
    all of them hand out copies, so callers cannot alter the Config by accident.
    """

    def __init__(
        self,
        values: Mapping[str, Any],
        provenance: Mapping[str, str],
        secret_paths: frozenset[str] = frozenset(),
    ) -> None:
        self._values: dict[str, Any] = copy.deepcopy(dict(values))
        self._provenance: dict[str, str] = dict(provenance)
        self._secret_paths = secret_paths

    @property
    def provenance(self) -> dict[str, str]:
        """Map of dotted leaf path to the label of the layer that set it."""
        return dict(self._provenance)

    def as_dict(self) -> dict[str, Any]:
        """The full nested configuration with real (unmasked) values."""
        return copy.deepcopy(self._values)

    def masked(self) -> dict[str, Any]:
        """The nested configuration with secret values replaced by a mask."""
        return mask_tree(self._values, self._secret_paths)

    def __getitem__(self, path: str) -> Any:
        node: Any = self._values
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                raise KeyError(path)
            node = node[part]
        return copy.deepcopy(node)

    def __contains__(self, path: object) -> bool:
        if not isinstance(path, str):
            return False
        try:
            self[path]
        except KeyError:
            return False
        return True

    def get(self, path: str, default: Any = None) -> Any:
        """Value at a dotted path (a leaf or a whole section), else ``default``."""
        try:
            return self[path]
        except KeyError:
            return default

    def explain(self) -> str:
        """One line per leaf: ``path = value  <- layer``, secrets masked."""
        shown = flatten(self.masked())
        rows = [
            (path, json.dumps(shown[path], ensure_ascii=False, default=str), self._provenance[path])
            for path in sorted(shown)
        ]
        if not rows:
            return "(empty configuration)"
        path_width = max(len(path) for path, _, _ in rows)
        value_width = max(len(value) for _, value, _ in rows)
        return "\n".join(
            f"{path.ljust(path_width)} = {value.ljust(value_width)}  <- {layer}"
            for path, value, layer in rows
        )

    def __repr__(self) -> str:
        return f"Config({self.masked()!r})"

    __str__ = __repr__


def load(
    *,
    schema: Schema | None = None,
    defaults: Mapping[str, Any] | None = None,
    files: Sequence[str | os.PathLike[str]] = (),
    env: Mapping[str, str] | None = None,
    env_prefix: str | None = None,
    overrides: Sequence[str] = (),
) -> Config:
    """Resolve configuration from layers, lowest priority first.

    1. ``defaults``: the schema's declared defaults, then the ``defaults``
       mapping on top (both are labelled "defaults").
    2. ``files``: each ``.json`` / ``.toml`` file in the order given, labelled
       ``file:<path>``.
    3. Environment variables ``<env_prefix>_<KEY>`` (``__`` nests), labelled
       "env". Only read when ``env_prefix`` is set; ``env`` defaults to
       ``os.environ`` and exists so tests and callers can inject a mapping.
    4. ``overrides``: ``KEY=VALUE`` strings, e.g. from ``extract_overrides``,
       labelled "cli".

    With a ``schema`` the merged result is validated and coerced, raising
    ValidationError listing every problem. Without one, values keep the type
    their source gave them, so environment and command-line values stay strings.
    """
    layers: list[Layer] = []
    if schema is not None:
        layers.append(("defaults", schema.defaults()))
    if defaults:
        check_keys(defaults, "defaults")
        layers.append(("defaults", defaults))
    for path in files:
        layers.append((f"file:{os.fspath(path)}", read_file_layer(path)))
    if env_prefix is not None:
        layers.append(("env", env_layer(os.environ if env is None else env, env_prefix)))
    if overrides:
        layers.append(("cli", overrides_layer(overrides)))

    tree, provenance = merge_layers(layers)
    secret_paths: frozenset[str] = frozenset()
    if schema is not None:
        tree = unflatten(schema.validate(flatten(tree)))
        secret_paths = schema.secret_paths()
    return Config(tree, provenance, secret_paths)
