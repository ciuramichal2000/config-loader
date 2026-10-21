"""Command line: show what the layers resolve to, with secrets masked.

    python -m layeredconfig --file app.json --env-prefix APP --set db.port=5433

There is no schema on the command line, so nothing is coerced: environment and
``--set`` values appear as strings.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from typing import TextIO

from .core import load
from .errors import ConfigError


def main(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m layeredconfig",
        description="Resolve layered configuration and print it with secrets masked.",
        allow_abbrev=False,
    )
    parser.add_argument("--file", action="append", default=[], dest="files", metavar="PATH",
                        help="JSON or TOML file; repeat to layer several (later wins)")
    parser.add_argument("--env-prefix", metavar="PREFIX",
                        help="read environment variables named PREFIX_KEY (use __ to nest)")
    parser.add_argument("--set", action="append", default=[], dest="overrides",
                        metavar="KEY=VALUE", help="override a dotted key; repeatable")
    parser.add_argument("--provenance", action="store_true",
                        help="print which layer each value came from instead of JSON")
    args = parser.parse_args(argv)

    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr
    try:
        config = load(
            files=args.files,
            env=env,
            env_prefix=args.env_prefix,
            overrides=args.overrides,
        )
    except ConfigError as exc:
        print(f"error: {exc}", file=err)
        return 2
    if args.provenance:
        print(config.explain(), file=out)
    else:
        print(json.dumps(config.masked(), indent=2, ensure_ascii=False, default=str), file=out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
