"""Masking of secret values so they never reach logs or printed output."""

from __future__ import annotations

import copy
import re
from collections.abc import Mapping
from typing import Any

MASK = "********"

# Substring match on the whole dotted path: over-masking (e.g. "token_ttl") is
# the safe failure mode, leaking a credential is not.
_SECRET_NAME = re.compile(r"password|token|secret", re.IGNORECASE)


def is_secret_path(path: str, extra: frozenset[str] = frozenset()) -> bool:
    """Return True if the value at ``path`` must be masked.

    ``extra`` holds paths the schema flagged explicitly, for names the pattern
    cannot guess (an ``api_key``, for instance).
    """
    return path in extra or _SECRET_NAME.search(path) is not None


def mask_tree(
    tree: Mapping[str, Any],
    extra: frozenset[str] = frozenset(),
    prefix: str = "",
) -> dict[str, Any]:
    """Return a deep copy of ``tree`` with every secret leaf replaced by MASK."""
    masked: dict[str, Any] = {}
    for key, value in tree.items():
        path = prefix + key
        if isinstance(value, Mapping) and value:
            masked[key] = mask_tree(value, extra, path + ".")
        elif is_secret_path(path, extra):
            masked[key] = MASK
        else:
            masked[key] = copy.deepcopy(value)
    return masked
