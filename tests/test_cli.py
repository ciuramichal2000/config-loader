import os, sys; sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import io
import json
import subprocess

import pytest

from layeredconfig.__main__ import main

PROJECT_ROOT = os.path.join(os.path.dirname(__file__), "..")


def run(argv, env=None):
    out, err = io.StringIO(), io.StringIO()
    code = main(argv, env=env, stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


@pytest.fixture
def config_file(tmp_path):
    path = tmp_path / "app.json"
    path.write_text('{"db": {"host": "h", "password": "pw-123"}}', encoding="utf-8")
    return path


def test_prints_merged_json_with_secrets_masked(config_file):
    code, out, err = run(
        ["--file", str(config_file), "--env-prefix", "APP", "--set", "db.host=override"],
        env={"APP_DB__PORT": "1"},
    )
    assert code == 0 and err == ""
    assert json.loads(out) == {"db": {"host": "override", "password": "********", "port": "1"}}
    assert "pw-123" not in out


def test_provenance_flag_prints_the_source_of_each_value(config_file):
    code, out, _ = run(
        ["--file", str(config_file), "--env-prefix", "APP", "--set", "db.host=x", "--provenance"],
        env={"APP_DB__PORT": "1"},
    )
    assert code == 0
    rows = {line.split(" = ")[0].strip(): line.rsplit("<- ", 1)[1] for line in out.splitlines()}
    assert rows == {"db.host": "cli", "db.password": f"file:{config_file}", "db.port": "env"}
    assert "pw-123" not in out


def test_bad_input_returns_exit_code_2_and_a_message_on_stderr(tmp_path):
    code, out, err = run(["--file", str(tmp_path / "missing.json")])
    assert code == 2
    assert out == ""
    assert err.startswith("error: ") and "missing.json" in err


def test_malformed_set_is_reported_without_echoing_it():
    code, _, err = run(["--set", "oops-no-equals-sign"])
    assert code == 2
    assert "KEY=VALUE" in err
    assert "oops-no-equals-sign" not in err


def test_unknown_options_and_abbreviations_are_refused(capsys):
    for argv in (["--bogus"], ["--prov"]):
        with pytest.raises(SystemExit) as info:
            main(argv)
        assert info.value.code == 2
    assert "unrecognized arguments" in capsys.readouterr().err


def test_no_layers_prints_an_empty_object():
    code, out, _ = run([])
    assert code == 0
    assert json.loads(out) == {}


def test_module_entry_point_runs_end_to_end():
    result = subprocess.run(
        [sys.executable, "-m", "layeredconfig", "--set", "a.b=1", "--set", "token=zzz"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0
    assert json.loads(result.stdout) == {"a": {"b": "1"}, "token": "********"}
