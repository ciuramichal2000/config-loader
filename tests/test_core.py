import os, sys; sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import json

import pytest

from layeredconfig import (
    MASK,
    Config,
    Field,
    Schema,
    SourceError,
    ValidationError,
    extract_overrides,
    load,
)


def db_schema(**kwargs):
    return Schema(
        {"db": {"host": Field(str, default="localhost"), "port": Field(int, default=5432)}},
        **kwargs,
    )


@pytest.fixture
def json_file(tmp_path):
    path = tmp_path / "app.json"
    path.write_text('{"db": {"port": 6000}}', encoding="utf-8")
    return path


# --- precedence and provenance ---------------------------------------------


@pytest.mark.parametrize("use_file, use_env, use_cli, expected_port, expected_layer", [
    (False, False, False, 5432, "defaults"),
    (True, False, False, 6000, "file"),
    (True, True, False, 7000, "env"),
    (True, True, True, 8000, "cli"),
    (False, False, True, 8000, "cli"),
    (False, True, False, 7000, "env"),
])
def test_priority_is_defaults_then_file_then_env_then_cli(
    json_file, use_file, use_env, use_cli, expected_port, expected_layer
):
    config = load(
        schema=db_schema(),
        files=[json_file] if use_file else [],
        env={"APP_DB__PORT": "7000"} if use_env else {},
        env_prefix="APP",
        overrides=["db.port=8000"] if use_cli else [],
    )
    expected_label = f"file:{json_file}" if expected_layer == "file" else expected_layer
    assert config["db.port"] == expected_port
    assert config.provenance["db.port"] == expected_label
    # Nothing touched the host, so it always comes from the defaults.
    assert config["db.host"] == "localhost"
    assert config.provenance["db.host"] == "defaults"


def test_nested_values_merge_per_key_across_layers(tmp_path):
    path = tmp_path / "app.json"
    path.write_text('{"db": {"host": "h", "pool": {"min": 1, "max": 5}}}', encoding="utf-8")
    config = load(files=[path], env={"APP_DB__POOL__MAX": "9"}, env_prefix="APP")
    assert config.as_dict() == {"db": {"host": "h", "pool": {"min": 1, "max": "9"}}}
    assert config.provenance == {
        "db.host": f"file:{path}",
        "db.pool.min": f"file:{path}",
        "db.pool.max": "env",
    }


def test_files_apply_in_the_order_given_and_toml_works_beside_json(tmp_path):
    first = tmp_path / "base.json"
    second = tmp_path / "local.toml"
    first.write_text('{"a": 1, "b": 1}', encoding="utf-8")
    second.write_text("b = 2\nc = 3\n", encoding="utf-8")

    config = load(files=[first, second])
    assert config.as_dict() == {"a": 1, "b": 2, "c": 3}
    assert config.provenance == {"a": f"file:{first}", "b": f"file:{second}", "c": f"file:{second}"}

    reversed_order = load(files=[second, first])
    assert reversed_order["b"] == 1


def test_explicit_defaults_mapping_sits_on_top_of_schema_defaults_in_the_defaults_layer():
    config = load(schema=db_schema(), defaults={"db": {"port": 9999}})
    assert config["db.port"] == 9999
    assert config["db.host"] == "localhost"
    assert config.provenance == {"db.port": "defaults", "db.host": "defaults"}


def test_defaults_mapping_keys_with_dots_are_rejected():
    with pytest.raises(SourceError, match="defaults"):
        load(defaults={"db.port": 1})


def test_overrides_extracted_from_argv_feed_straight_into_load():
    overrides, remaining = extract_overrides(["serve", "--set", "db.port=5433"])
    config = load(schema=db_schema(), overrides=overrides)
    assert config["db.port"] == 5433
    assert remaining == ["serve"]


def test_environment_defaults_to_the_real_process_environment(monkeypatch):
    monkeypatch.setenv("LCFG_DEMO_LEVEL__DEEP", "yes")
    assert load(env_prefix="LCFG_DEMO")["level.deep"] == "yes"


def test_environment_is_ignored_unless_a_prefix_is_given():
    assert load(env={"APP_X": "1"}).as_dict() == {}


# --- coercion and validation through load ----------------------------------


def test_environment_strings_are_coerced_to_schema_types():
    schema = Schema({
        "db": {"port": Field(int)},
        "debug": Field(bool),
        "ratio": Field(float),
        "hosts": Field(list),
        "ports": Field(list, item_kind=int),
    })
    config = load(
        schema=schema,
        env={
            "APP_DB__PORT": "5433",
            "APP_DEBUG": "true",
            "APP_RATIO": "0.5",
            "APP_HOSTS": "a, b",
            "APP_PORTS": "80, 443",
        },
        env_prefix="APP",
    )
    assert config.as_dict() == {
        "db": {"port": 5433},
        "debug": True,
        "ratio": 0.5,
        "hosts": ["a", "b"],
        "ports": [80, 443],
    }


def test_without_a_schema_nothing_is_coerced():
    config = load(env={"APP_PORT": "5432"}, env_prefix="APP", overrides=["debug=true"])
    assert config["port"] == "5432"
    assert config["debug"] == "true"


def test_bad_value_fails_loading_with_the_path_in_the_message():
    with pytest.raises(ValidationError, match=r"db\.port: expected int, got 'abc'"):
        load(schema=db_schema(), overrides=["db.port=abc"])


def test_missing_required_key_is_reported_and_satisfied_by_any_layer():
    schema = Schema({"db": {"host": Field(str, required=True)}})
    with pytest.raises(ValidationError) as info:
        load(schema=schema)
    assert [(p.path, p.code) for p in info.value.problems] == [("db.host", "missing")]

    config = load(schema=schema, env={"APP_DB__HOST": "h"}, env_prefix="APP")
    assert config["db.host"] == "h"
    assert config.provenance["db.host"] == "env"


def test_reject_unknown_flags_typos_but_default_mode_keeps_them():
    env = {"APP_DB__PROT": "5433"}
    with pytest.raises(ValidationError) as info:
        load(schema=db_schema(reject_unknown=True), env=env, env_prefix="APP")
    assert [(p.path, p.code) for p in info.value.problems] == [("db.prot", "unknown")]

    lenient = load(schema=db_schema(), env=env, env_prefix="APP")
    assert lenient["db.prot"] == "5433"
    assert lenient["db.port"] == 5432


def test_value_replacing_a_declared_section_fails_validation(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text('{"db": "sqlite"}', encoding="utf-8")
    with pytest.raises(ValidationError) as info:
        load(schema=db_schema(), files=[path])
    assert [(p.path, p.code) for p in info.value.problems] == [("db", "type")]


def test_empty_section_in_a_file_does_not_disturb_defaults(tmp_path):
    path = tmp_path / "c.json"
    path.write_text('{"db": {}}', encoding="utf-8")
    config = load(schema=db_schema(), files=[path])
    assert config.as_dict() == {"db": {"host": "localhost", "port": 5432}}


def test_null_in_a_file_is_not_silently_accepted_for_a_typed_key(tmp_path):
    path = tmp_path / "c.json"
    path.write_text('{"db": {"port": null}}', encoding="utf-8")
    with pytest.raises(ValidationError, match=r"db\.port: expected int, got None"):
        load(schema=db_schema(), files=[path])


def test_conflicting_sources_inside_one_layer_fail_before_validation():
    with pytest.raises(SourceError):
        load(schema=db_schema(), overrides=["db=1", "db.port=2"])


# --- secrets ---------------------------------------------------------------


def make_secret_config(tmp_path):
    path = tmp_path / "s.json"
    path.write_text(
        json.dumps({
            "name": "svc",
            "db": {"password": "hunter2"},
            "auth": {"token": "tok-abc"},
            "api_key": "K-123456",
        }),
        encoding="utf-8",
    )
    schema = Schema({
        "name": Field(str),
        "db": {"password": Field(str)},
        "auth": {"token": Field(str)},
        "api_key": Field(str, secret=True),
    })
    return load(schema=schema, files=[path])


def test_no_printed_representation_contains_a_secret(tmp_path):
    config = make_secret_config(tmp_path)
    representations = [
        repr(config),
        str(config),
        f"{config}",
        json.dumps(config.masked()),
        config.explain(),
    ]
    for text in representations:
        for secret in ("hunter2", "tok-abc", "K-123456"):
            assert secret not in text
        assert "svc" in text
        assert MASK in text


def test_masked_view_replaces_name_matched_and_flagged_keys_only(tmp_path):
    masked = make_secret_config(tmp_path).masked()
    assert masked == {
        "name": "svc",
        "db": {"password": MASK},
        "auth": {"token": MASK},
        "api_key": MASK,
    }


def test_real_values_remain_available_to_the_application(tmp_path):
    config = make_secret_config(tmp_path)
    assert config["db.password"] == "hunter2"
    assert config.get("api_key") == "K-123456"
    assert config.as_dict()["auth"]["token"] == "tok-abc"


def test_secret_name_matching_is_case_insensitive_and_works_without_a_schema(tmp_path):
    path = tmp_path / "c.json"
    path.write_text('{"Service": {"ClientSecret": "s3cr3t", "Name": "x"}, "PASSWORD": "pw"}', encoding="utf-8")
    masked = load(files=[path]).masked()
    assert masked == {"Service": {"ClientSecret": MASK, "Name": "x"}, "PASSWORD": MASK}


def test_validation_errors_do_not_leak_secrets():
    schema = Schema({"db": {"password": Field(int)}})
    with pytest.raises(ValidationError) as info:
        load(schema=schema, overrides=["db.password=hunter2"])
    assert "hunter2" not in str(info.value)


# --- the Config object -----------------------------------------------------


def test_explain_aligns_columns_and_masks_secrets():
    config = Config({"a": {"b": 1}, "name": "x"}, {"a.b": "file:x", "name": "env"})
    assert config.explain() == 'a.b  = 1    <- file:x\nname = "x"  <- env'
    secret = Config({"db": {"password": "pw"}}, {"db.password": "env"})
    assert secret.explain() == 'db.password = "********"  <- env'


def test_empty_configuration_explains_itself():
    config = load()
    assert config.as_dict() == {}
    assert config.explain() == "(empty configuration)"


def test_lookup_of_missing_and_nested_paths():
    config = load(schema=db_schema())
    assert "db.port" in config
    assert "db" in config
    assert "db.nope" not in config
    assert "db.port.deeper" not in config
    assert 5 not in config
    assert config.get("db.nope", 7) == 7
    assert config.get("db.nope") is None
    with pytest.raises(KeyError):
        config["db.nope"]


def test_returned_values_are_copies_not_views_of_the_config():
    config = load(schema=Schema({"hosts": Field(list, default=["a"])}))
    section = config["hosts"]
    section.append("b")
    config.as_dict()["hosts"].append("c")
    assert config["hosts"] == ["a"]


def test_optional_key_without_default_is_simply_absent():
    config = load(schema=Schema({"x": Field(int)}))
    assert "x" not in config
    assert config.provenance == {}
