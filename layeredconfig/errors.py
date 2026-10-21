"""Exception types raised by layeredconfig.

Everything the package raises on purpose derives from ConfigError, so a caller
can catch one type at the boundary and let genuine bugs surface normally.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass


class ConfigError(Exception):
    """Base class for every error this package raises deliberately."""


class SourceError(ConfigError):
    """A config file, environment variable or command-line override is unusable."""


class SchemaError(ConfigError):
    """The schema itself is malformed; this is a programming error, not bad config."""


class CoercionError(ConfigError):
    """A single value cannot be converted to the declared type."""


@dataclass(frozen=True)
class Problem:
    """One validation finding. ``code`` is "missing", "unknown" or "type"."""

    path: str
    code: str
    message: str


class ValidationError(ConfigError):
    """The merged configuration violates the schema.

    All problems are collected and reported together so a broken deployment
    can be fixed in one pass instead of one error per restart.
    """

    def __init__(self, problems: Iterable[Problem]) -> None:
        self.problems: tuple[Problem, ...] = tuple(
            sorted(problems, key=lambda problem: (problem.path, problem.code))
        )
        lines = "\n".join(f"  - {p.path}: {p.message}" for p in self.problems)
        super().__init__(
            f"invalid configuration ({len(self.problems)} problem(s)):\n{lines}"
        )
