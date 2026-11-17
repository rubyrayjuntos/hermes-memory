import pytest
from scripts.eval_memory import _validate_eval_dsn


def test_eval_memory_refuses_port_5450_even_with_eval_database_name():
    with pytest.raises(SystemExit, match="port 5450"):
        _validate_eval_dsn("postgres://hermes@127.0.0.1:5450/hermes_memory_eval")
    with pytest.raises(SystemExit, match="port 5450"):
        _validate_eval_dsn("host=127.0.0.1 port=5450 dbname=hermes_memory_eval")


def test_eval_memory_requires_an_eval_database():
    with pytest.raises(SystemExit, match="must name"):
        _validate_eval_dsn("postgres://hermes@127.0.0.1:5453/hermes_memory")


def test_eval_memory_allows_a_named_eval_database_on_another_port():
    _validate_eval_dsn("postgres://hermes@127.0.0.1:5453/hermes_memory_eval")
