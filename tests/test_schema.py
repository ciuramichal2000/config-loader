import os, sys; sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from layeredconfig.errors import CoercionError, SchemaError, ValidationError
from layeredconfig.schema import Field, Schema, coerce


# --- scalar coercion --------------------------------------------------------


@pytest.mark.parametrize("raw, expected", [
    ("5432", 5432),
    (" 7 ", 7),
    ("-3", -3),
    ("+12", 12),
    ("007", 7),
    (42, 42),
])
def test_int_accepts_integers_and_plain_decimal_strings(raw, expected):
    result = coerce(raw, Field(int))
    assert result == expected and type(result) is int


@pytest.mark.parametrize("raw", ["", "abc", "1_000", "0x10", "1.5", "٣", True, 5.0, None, [1]])
def test_int_rejects_everything_that_is_not_a_plain_integer(raw):
    with pytest.raises(CoercionError, match="expected int"):
        coerce(raw, Field(int))


def test_int_rejects_digit_strings_beyond_pythons_conversion_limit():
    with pytest.raises(CoercionError, match="expected int"):
        coerce("9" * 5000, Field(int))


@pytest.mark.parametrize("raw, expected", [
    ("true", True), ("TRUE", True), (" Yes ", True), ("on", True), ("1", True),
    ("false", False), ("No", False), ("OFF", False), ("0", False),
    (True, True), (False, False),
])
def test_bool_vocabulary(raw, expected):
    assert coerce(raw, Field(bool)) is expected


@pytest.mark.parametrize("raw", ["maybe", "", "2", "truee", 1, 0, None])
def test_bool_rejects_unknown_words_and_numbers(raw):
    with pytest.raises(CoercionError, match="expected bool"):
        coerce(raw, Field(bool))


@pytest.mark.parametrize("raw, expected", [
    ("3.5", 3.5), ("1e3", 1000.0), (".5", 0.5), ("-2.", -2.0), (2, 2.0), (0.25, 0.25),
])
def test_float_conversions(raw, expected):
    result = coerce(raw, Field(float))
    assert result == expected and type(result) is float


@pytest.mark.parametrize("raw", ["nan", "inf", "-Infinity", "1e999", "1_0.5", "", "x", True, 10**400])
def test_float_rejects_non_finite_and_malformed_input(raw):
    with pytest.raises(CoercionError, match="float"):
        coerce(raw, Field(float))


def test_str_field_refuses_numbers_instead_of_guessing():
    assert coerce("abc", Field(str)) == "abc"
    with pytest.raises(CoercionError, match="expected str, got 123"):
        coerce(123, Field(str))


# --- list coercion ----------------------------------------------------------


def test_comma_separated_string_becomes_a_trimmed_list():
    assert coerce("a, b ,c", Field(list)) == ["a", "b", "c"]


def test_blank_string_is_an_empty_list():
    assert coerce("", Field(list)) == []
    assert coerce("   ", Field(list)) == []


def test_list_items_are_coerced_to_the_item_kind():
    assert coerce("80, 443", Field(list, item_kind=int)) == [80, 443]
    assert coerce("true,no", Field(list, item_kind=bool)) == [True, False]
    assert coerce(["1", 2], Field(list, item_kind=int)) == [1, 2]


def test_list_error_names_the_failing_item_index():
    with pytest.raises(CoercionError, match=r"item 1: expected int, got 'x'"):
        coerce("1,x,3", Field(list, item_kind=int))


@pytest.mark.parametrize("raw", ["a,,b", "a,b,", ",a"])
def test_empty_list_items_are_rejected(raw):
    with pytest.raises(CoercionError, match="empty item"):
        coerce(raw, Field(list))


def test_list_rejects_non_list_non_string_input():
    with pytest.raises(CoercionError, match="expected list, got 5"):
        coerce(5, Field(list))


# --- Field and Schema construction -----------------------------------------


def test_field_rejects_unsupported_types():
    with pytest.raises(SchemaError, match="unsupported field type"):
        Field(dict)
    with pytest.raises(SchemaError, match="unsupported list item type"):
        Field(list, item_kind=list)


def test_required_field_cannot_declare_a_default():
    with pytest.raises(SchemaError, match="required"):
        Field(int, default=1, required=True)


def test_field_default_must_be_coercible_and_is_not_echoed():
    with pytest.raises(SchemaError, match="invalid default") as info:
        Field(int, default="hunter2")
    assert "hunter2" not in str(info.value)


def test_field_without_default_contributes_nothing_to_the_defaults_layer():
    assert Schema({"a": Field(str), "b": Field(bool, default=False)}).defaults() == {"b": False}


def test_schema_accepts_nested_and_dotted_declarations_equivalently():
    nested = Schema({"db": {"port": Field(int)}})
    dotted = Schema({"db.port": Field(int)})
    assert list(nested.fields) == list(dotted.fields) == ["db.port"]


def test_schema_defaults_build_a_nested_tree_of_copies():
    default_hosts = ["a"]
    schema = Schema({"db": {"port": Field(int, default=5432)}, "hosts": Field(list, default=default_hosts),
                     "name": Field(str)})
    defaults = schema.defaults()
    assert defaults == {"db": {"port": 5432}, "hosts": ["a"]}
    defaults["hosts"].append("b")
    assert default_hosts == ["a"]


def test_schema_rejects_value_and_section_with_the_same_name():
    with pytest.raises(SchemaError, match="'db' is declared as a value and as a section"):
        Schema({"db": Field(str), "db.port": Field(int)})


def test_schema_rejects_duplicate_paths_and_bad_keys():
    with pytest.raises(SchemaError, match="declared twice"):
        Schema({"a.b": Field(int), "a": {"b": Field(int)}})
    with pytest.raises(SchemaError, match="invalid schema key"):
        Schema({"a..b": Field(int)})
    with pytest.raises(SchemaError, match="invalid schema key"):
        Schema({"": Field(int)})
    with pytest.raises(SchemaError, match="must be a Field"):
        Schema({"a": 5})


# --- validation -------------------------------------------------------------


def test_validate_coerces_known_keys_and_passes_unknown_ones_through_by_default():
    schema = Schema({"port": Field(int)})
    assert schema.validate({"port": "80", "extra": "x"}) == {"port": 80, "extra": "x"}


def test_validate_reports_all_problems_together_sorted_by_path():
    schema = Schema({"a": Field(str, required=True), "b": Field(int)}, reject_unknown=True)
    with pytest.raises(ValidationError) as info:
        schema.validate({"b": "x", "z": "1"})
    problems = [(p.path, p.code) for p in info.value.problems]
    assert problems == [("a", "missing"), ("b", "type"), ("z", "unknown")]
    assert "3 problem(s)" in str(info.value)


def test_validate_optional_key_without_default_may_be_absent():
    assert Schema({"x": Field(int)}).validate({}) == {}


def test_validate_value_where_a_section_is_declared_is_one_problem_not_two():
    schema = Schema({"db": {"host": Field(str, required=True)}})
    with pytest.raises(ValidationError) as info:
        schema.validate({"db": "sqlite"})
    assert [(p.path, p.code) for p in info.value.problems] == [("db", "type")]


def test_validate_section_where_a_value_is_declared_is_one_problem():
    schema = Schema({"cache": Field(str, required=True)})
    with pytest.raises(ValidationError) as info:
        schema.validate({"cache.size": 1})
    assert [(p.path, p.code) for p in info.value.problems] == [("cache.size", "type")]


def test_validation_message_masks_secret_values_but_shows_ordinary_ones():
    schema = Schema({"db": {"port": Field(int), "password": Field(int)}, "key": Field(int, secret=True)})
    with pytest.raises(ValidationError) as info:
        schema.validate({"db.port": "abc", "db.password": "hunter2", "key": "k-123"})
    text = str(info.value)
    assert "'abc'" in text
    assert "hunter2" not in text
    assert "k-123" not in text
    assert text.count("<masked>") == 2
