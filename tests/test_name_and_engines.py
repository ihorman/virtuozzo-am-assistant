"""Regression tests for the MariaDB / env-name / auto-fix feedback round.

User feedback (2026-11-24): creating a MariaDB failed because the generated
environment name exceeded the tool validator cap, the engine was missing from
the wizard options, and the error card offered no auto-fix.
"""

import pytest

from agent.diagnose import _build_repair_plan, repair_plan_is_safe
from agent.flows import _normalize_db, _normalize_engine
from agent.tools_util import ENV_NAME_RE, slugify_env_name
from mcp.security import ParamRejected, validate_args
from mcp.tools import _ENV_NAME


# --- slugify_env_name -------------------------------------------------------

def test_slugify_shortens_long_names():
    slug = slugify_env_name("MariaDB Production Main Database For Customers")
    assert len(slug) <= 30
    assert ENV_NAME_RE.match(slug)


def test_slugify_is_identity_for_valid_names():
    assert slugify_env_name("env-5516365") == "env-5516365"


def test_slugify_cleans_symbols_and_case():
    assert slugify_env_name("My Cool App!!") == "my-cool-app"
    assert slugify_env_name("123app") == "e-123app"
    assert slugify_env_name("") == "app"


# --- engine coverage --------------------------------------------------------

def test_mariadb_is_a_known_engine():
    assert _normalize_engine("MariaDB") == "mariadb"
    assert _normalize_engine("MariaDB 10.11") == "mariadb"


def test_mariadb_in_deploy_db_choices():
    assert _normalize_db("MariaDB 10.11") == "mariadb:10.11"


# --- validation cap leaves room for real names ------------------------------

def test_env_name_validator_accepts_reasonable_names():
    validate_args({"properties": {"env_name": _ENV_NAME}},
                  {"env_name": "a" * 40}, tool="db_add")


def test_env_name_validator_still_rejects_absurd_names():
    with pytest.raises(ParamRejected):
        validate_args({"properties": {"env_name": _ENV_NAME}},
                      {"env_name": "a" * 70}, tool="db_add")


# --- auto-fix repair plan for invalid names ---------------------------------

def test_repair_plan_fixes_too_long_env_name():
    plan = _build_repair_plan(
        "invalid_parameter", "db_add",
        {"env_name": "MariaDB Production Main Database For Customers",
         "engine": "mariadb"},
        summary="argument [env_name] of tool [db_add] must be at most "
                "32 characters")
    assert plan is not None
    assert plan["class"] == "fix_env_name"
    fixed = plan["steps"][0]["args"]["env_name"]
    assert fixed == slugify_env_name(
        "MariaDB Production Main Database For Customers")
    assert repair_plan_is_safe(plan)


def test_repair_plan_absent_for_unrelated_errors():
    assert _build_repair_plan(
        "invalid_parameter", "db_add", {"env_name": "short-name"},
        summary="argument [cloudlets] of tool [db_add] must be an integer"
    ) is None
