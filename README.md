# layeredconfig

A small configuration loader that resolves settings from layers: defaults, then JSON or TOML files, then environment variables, then command-line overrides. Later layers win, nested sections are merged key by key, and a schema converts and validates the result. Every final value remembers which layer it came from, and anything that looks like a secret is masked whenever the configuration is printed or logged. It uses only the Python standard library (`tomllib`, `json`, `argparse`).

## The interesting parts

- **Provenance through the merge.** Deep merge and source tracking happen in one pass, so `config.provenance["db.port"]` is `"cli"`, `"env"`, `"defaults"` or `"file:<path>"`. When a value replaces a whole section of a different shape, the stale provenance of the discarded keys is dropped too.
- **Strict, predictable coercion.** Environment and `--set` values are strings, so the schema turns `"5432"` into `5432`, `"yes"`/`"on"`/`"true"` into `True` and `"a, b"` into `["a", "b"]`. It refuses what Python would quietly accept (`"1_000"`, `"nan"`, `1` as a bool, a number for a string field).
- **All problems at once.** Validation collects missing required keys, unknown keys (optional, catches typos like `APP_DB__PROT`), type errors, and value-versus-section clashes into one `ValidationError`, instead of failing on the first.
- **Secrets stay out of output.** Keys whose dotted path contains `password`, `token` or `secret` (case-insensitive), plus any field declared `secret=True`, are masked in `repr`, `str`, `masked()`, `explain()` and the CLI. Error messages never echo secret values either, including values typed in `--set`.
- **Injectable sources.** The environment is a mapping you can pass in (it defaults to `os.environ`), so callers and tests do not depend on the machine's real environment.

## Usage

```python
import sys
from layeredconfig import Field, Schema, extract_overrides, load

schema = Schema({
    "db": {
        "host": Field(str, default="localhost"),
        "port": Field(int, default=5432),
        "password": Field(str, required=True),
    },
    "debug": Field(bool, default=False),
    "allowed_hosts": Field(list, default=["localhost"]),
}, reject_unknown=True)

overrides, rest = extract_overrides(sys.argv[1:])   # pulls out "--set db.port=5433"
config = load(
    schema=schema,
    files=["app.json", "local.toml"],   # applied in this order
    env_prefix="APP",                   # APP_DB__HOST=x  ->  db.host = "x"
    overrides=overrides,
)

config["db.port"]      # 5433, an int
config.get("db.nope")  # None
print(config.explain())
```

With `app.json` setting `db.host` and `db.password`, `local.toml` setting `debug = true`,
`APP_ALLOWED_HOSTS="example.com, api.example.com"` in the environment and
`--set db.port=5433` on the command line, `explain()` prints:

```
allowed_hosts = ["example.com", "api.example.com"]  <- env
db.host       = "db.internal"                       <- file:app.json
db.password   = "********"                          <- file:app.json
db.port       = 5433                                <- cli
debug         = true                                <- file:local.toml
```

There is also a small command line that shows what the layers resolve to (no schema, so nothing is coerced and values from the environment or `--set` stay strings):

```
python -m layeredconfig --file app.json --file local.toml --env-prefix APP --set db.port=5433 [--provenance]
```

It exits with code 2 and an `error:` message on stderr if a file is unreadable or an override is malformed.

## Behaviour worth knowing

- Environment names are matched case-sensitively against the prefix, `__` nests, and key names are lower-cased: `APP_DB__POOL_SIZE` is `db.pool_size`. A single `_` stays in the key name.
- Lists are replaced by later layers, not concatenated.
- A comma-separated list rejects empty items (`"a,,b"`); an empty string is an empty list.
- Files are `.json` or `.toml` (by extension) and must have an object/table at the top. A UTF-8 byte order mark is accepted.
- `default=None` on a `Field` means "no default"; an optional field with no default is simply absent from the result.

## Limitations

- Keys that contain a literal `.` are rejected in files and defaults, because dotted paths are how values are addressed.
- Secret detection is by name. A key such as `api_key` is only masked if the schema marks it `secret=True`; the pattern also over-masks names like `token_ttl`.
- `get()`, `[]` and `as_dict()` return real values by design; only the printed and logged views are masked. Masking cannot protect a value you print yourself from those.
- Provenance is per layer (`env`), not per variable (`APP_DB__PORT`).
- The command line has no way to supply a schema.
- Not supported: YAML or other file formats, variable interpolation inside values, reloading a changed file, nested lists of objects as schema types.
- Written for Python 3.11+ (`tomllib`); developed and tested on Python 3.14.

## Running the tests

```
python -m pytest -q
```

pytest is the only third-party package used. The tests inject environments and use temporary files; they do not use the network, the clock or randomness. One test starts `python -m layeredconfig` in a subprocess to check the entry point.

Built with AI coding assistance.
