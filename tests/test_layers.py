import os, sys; sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from layeredconfig.errors import SourceError
from layeredconfig.layers import (
    env_layer,
    extract_overrides,
    flatten,
    merge_layers,
    overrides_layer,
    read_file_layer,
    unflatten,
)


# --- merging and provenance -------------------------------------------------


def test_deep_merge_replaces_only_the_overridden_leaf():
    tree, provenance = merge_layers([
        ("defaults", {"db": {"host": "localhost", "port": 5432}, "debug": False}),
        ("file:a.json", {"db": {"host": "db.internal"}}),
        ("env", {"db": {"port": "6000"}}),
    ])
    assert tree == {"db": {"host": "db.internal", "port": "6000"}, "debug": False}
    assert provenance == {"db.host": "file:a.json", "db.port": "env", "debug": "defaults"}


def test_later_layer_always_wins_for_the_same_leaf():
    _, provenance = merge_layers([("one", {"k": 1}), ("two", {"k": 2}), ("three", {"k": 3})])
    assert provenance == {"k": "three"}
    tree, _ = merge_layers([("one", {"k": 1}), ("two", {"k": 2}), ("three", {"k": 3})])
    assert tree == {"k": 3}


def test_lists_are_replaced_not_concatenated():
    tree, _ = merge_layers([("a", {"hosts": ["x", "y"]}), ("b", {"hosts": ["z"]})])
    assert tree == {"hosts": ["z"]}


def test_value_replacing_a_section_drops_the_sections_provenance():
    tree, provenance = merge_layers([
        ("a", {"db": {"host": "h", "port": 1}}),
        ("b", {"db": "sqlite"}),
    ])
    assert tree == {"db": "sqlite"}
    assert provenance == {"db": "b"}


def test_section_replacing_a_value_drops_the_values_provenance():
    tree, provenance = merge_layers([("a", {"db": "sqlite", "x": 1}), ("b", {"db": {"host": "h"}})])
    assert tree == {"db": {"host": "h"}, "x": 1}
    assert provenance == {"db.host": "b", "x": "a"}


def test_empty_section_does_not_wipe_an_existing_section():
    tree, provenance = merge_layers([("a", {"db": {"host": "h"}}), ("b", {"db": {}})])
    assert tree == {"db": {"host": "h"}}
    assert provenance == {"db.host": "a"}


def test_empty_section_on_its_own_is_a_leaf_with_provenance():
    tree, provenance = merge_layers([("a", {}), ("b", {"cache": {}})])
    assert tree == {"cache": {}}
    assert provenance == {"cache": "b"}


def test_merge_copies_values_so_later_mutation_of_a_layer_is_invisible():
    source = {"hosts": ["a"]}
    tree, _ = merge_layers([("a", source)])
    source["hosts"].append("b")
    assert tree == {"hosts": ["a"]}


def test_flatten_and_unflatten_round_trip_including_empty_sections():
    tree = {"a": {"b": 1, "c": {}}, "d": [1, 2]}
    flat = flatten(tree)
    assert flat == {"a.b": 1, "a.c": {}, "d": [1, 2]}
    assert unflatten(flat) == tree


# --- environment layer ------------------------------------------------------


def test_env_double_underscore_nests_and_names_are_lowercased():
    env = {
        "APP_DB__HOST": "h",
        "APP_DB__POOL_SIZE": "4",
        "APP_DEBUG": "1",
        "OTHER_X": "ignored",
        "APPDATA": "ignored: no underscore after the prefix",
    }
    assert env_layer(env, "APP") == {"db": {"host": "h", "pool_size": "4"}, "debug": "1"}


def test_env_prefix_with_trailing_underscore_is_equivalent():
    env = {"APP_A__B": "1"}
    assert env_layer(env, "APP_") == env_layer(env, "APP") == {"a": {"b": "1"}}


def test_env_prefix_match_is_case_sensitive():
    assert env_layer({"app_x": "1"}, "APP") == {}


def test_env_empty_prefix_is_rejected_rather_than_importing_the_whole_environment():
    with pytest.raises(SourceError, match="prefix"):
        env_layer({"PATH": "/bin"}, "")


@pytest.mark.parametrize("name", ["APP_DB____HOST", "APP_", "APP___X__"])
def test_env_empty_key_segment_is_rejected(name):
    with pytest.raises(SourceError, match="empty key segment"):
        env_layer({name: "v"}, "APP")


def test_env_variable_cannot_be_both_value_and_section():
    with pytest.raises(SourceError, match="both as a value and as a section"):
        env_layer({"APP_DB": "x", "APP_DB__HOST": "y"}, "APP")


# --- command-line overrides -------------------------------------------------


def test_overrides_split_on_first_equals_only():
    layer = overrides_layer(["db.port=5433", "url=postgres://h/x?a=b"])
    assert layer == {"db": {"port": "5433"}, "url": "postgres://h/x?a=b"}


def test_overrides_repeated_key_keeps_the_last_value():
    assert overrides_layer(["a=1", "a=2"]) == {"a": "2"}


def test_overrides_allow_an_empty_value():
    assert overrides_layer(["name="]) == {"name": ""}


@pytest.mark.parametrize("item", ["novalue", "=v", "a..b=1", ".a=1", "a.=1"])
def test_overrides_malformed_items_are_rejected(item):
    with pytest.raises(SourceError):
        overrides_layer([item])


def test_override_errors_never_echo_the_value():
    with pytest.raises(SourceError) as info:
        overrides_layer(["secrets=1", "secrets.token=hunter2-super-secret"])
    assert "hunter2-super-secret" not in str(info.value)
    assert "both as a value and as a section" in str(info.value)


def test_extract_overrides_pulls_out_set_flags_and_leaves_the_rest():
    argv = ["--verbose", "--set", "a.b=1", "--set=c=2", "run", "--port", "80"]
    overrides, remaining = extract_overrides(argv)
    assert overrides == ["a.b=1", "c=2"]
    assert remaining == ["--verbose", "run", "--port", "80"]


def test_extract_overrides_dangling_set_is_an_error():
    with pytest.raises(SourceError, match="--set needs"):
        extract_overrides(["--verbose", "--set"])


# --- files ------------------------------------------------------------------


def test_reads_json_file(tmp_path):
    path = tmp_path / "c.json"
    path.write_text('{"db": {"port": 5432}, "tags": ["a"]}', encoding="utf-8")
    assert read_file_layer(path) == {"db": {"port": 5432}, "tags": ["a"]}


def test_reads_toml_file(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text('title = "x"\n[db]\nport = 5\n', encoding="utf-8")
    assert read_file_layer(path) == {"title": "x", "db": {"port": 5}}


def test_extension_is_matched_case_insensitively(tmp_path):
    path = tmp_path / "C.JSON"
    path.write_text('{"a": 1}', encoding="utf-8")
    assert read_file_layer(path) == {"a": 1}


def test_utf8_bom_is_tolerated(tmp_path):
    path = tmp_path / "c.json"
    path.write_bytes(b"\xef\xbb\xbf" + b'{"a": 1}')
    assert read_file_layer(path) == {"a": 1}


def test_missing_file_names_the_file(tmp_path):
    missing = tmp_path / "nope.json"
    with pytest.raises(SourceError) as info:
        read_file_layer(missing)
    assert str(missing) in str(info.value)


def test_invalid_json_reports_line_and_column(tmp_path):
    path = tmp_path / "c.json"
    path.write_text('{\n  "a": 1,\n  "b": \n}', encoding="utf-8")
    with pytest.raises(SourceError, match="line 4, column 1"):
        read_file_layer(path)


def test_invalid_toml_is_a_source_error(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text("a = \n", encoding="utf-8")
    with pytest.raises(SourceError, match="invalid TOML"):
        read_file_layer(path)


def test_unsupported_extension_is_rejected_before_reading(tmp_path):
    with pytest.raises(SourceError, match=r"\.yaml"):
        read_file_layer(tmp_path / "c.yaml")


def test_json_top_level_must_be_an_object(tmp_path):
    path = tmp_path / "c.json"
    path.write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(SourceError, match="top level"):
        read_file_layer(path)


def test_non_utf8_file_is_a_source_error(tmp_path):
    path = tmp_path / "c.json"
    path.write_bytes(b'{"a": "\xff\xfe"}')
    with pytest.raises(SourceError, match="UTF-8"):
        read_file_layer(path)


def test_key_containing_a_dot_is_rejected_with_its_full_path(tmp_path):
    path = tmp_path / "c.json"
    path.write_text('{"a": {"b.c": 1}}', encoding="utf-8")
    with pytest.raises(SourceError, match=r"a\.b\.c"):
        read_file_layer(path)
