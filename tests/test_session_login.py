"""Exercise the real HTTP session code without importing Home Assistant."""

import importlib
import json
from pathlib import Path
import sys
import types
from unittest.mock import Mock

import pytest
from requests import Response


API_PATH = Path(__file__).resolve().parents[1] / "custom_components/smartschool/smartschool_api"
# Use a separate package so other model tests' module stubs cannot replace the
# actual session or HTML parser under test.
package = types.ModuleType("_smartschool_login_tests")
package.__path__ = [str(API_PATH)]
sys.modules.setdefault(package.__name__, package)
session_module = importlib.import_module(f"{package.__name__}.session")
Smartschool = session_module.Smartschool


def response(body="", path="/", status=200):
    result = Response()
    result.status_code = status
    result.url = f"https://school.smartschool.be{path}"
    result._content = body.encode("utf-8")
    result.encoding = "utf-8"
    return result


def bootstrap(user, style="unicode"):
    payload = json.dumps({"vars": {"authenticatedUser": user}}, ensure_ascii=False)
    if style == "unicode":
        literal = "'" + "".join(f"\\u{ord(char):04x}" for char in payload) + "'"
    elif style == "single":
        literal = "'" + payload.replace("\\", "\\\\").replace("'", "\\'") + "'"
    else:
        literal = json.dumps(payload, ensure_ascii=False)
    # Include unrelated data first and trailing code after JSON.parse.
    return ('<script>JSON.parse(\'{"topnav": {}}\');</script>'
            f"<script>page.extend(\nJSON.parse({literal})\n); startPage();</script>")


@pytest.fixture
def school():
    instance = Smartschool()
    instance.creds = types.SimpleNamespace(
        username="student", password="secret", main_url="school.smartschool.be",
        mfa="JBSWY3DPEHPK3PXP",
    )
    instance._session = Mock()
    return instance


@pytest.mark.parametrize("style", ["unicode", "single", "double"])
def test_extracts_user_from_javascript_string(school, style):
    user = {"id": 42, "name": "Zoë D'Haene", "path": r"folder\file", "quote": 'a "quote"'}
    school._extract_authenticated_user(response(bootstrap(user, style)))
    assert school.authenticated_user == user


def test_skips_null_and_malformed_bootstrap_data(school):
    body = '<script>JSON.parse(\'broken\'); JSON.parse(\'[]\');</script>'
    school._extract_authenticated_user(response(body + bootstrap(None) + bootstrap({"id": 42})))
    assert school.authenticated_user == {"id": 42}


def test_existing_session_loads_identity_without_login(school):
    school._session.get.return_value = response(bootstrap({"id": 42}))
    assert school.get_authenticated_user() == {"id": 42}
    school._session.post.assert_not_called()
    school._session.get.assert_called_once_with("https://school.smartschool.be/", allow_redirects=True)


def test_password_login_extracts_identity_from_final_page(school):
    login_form = ('<form name="login_form"><input name="login_form[_username]">'
                  '<input name="login_form[_password]"></form>')
    school._session.get.side_effect = [response(path="/login"), response(login_form, "/login")]
    school._session.post.return_value = response(bootstrap({"id": 42}))
    assert school.get_authenticated_user() == {"id": 42}
    assert school._session.post.call_args.kwargs["data"] == {
        "login_form[_username]": "student", "login_form[_password]": "secret"
    }


def test_2fa_loads_identity_after_verification(school):
    school._session.get.side_effect = [
        response(path="/2fa"),
        response(bootstrap(None), "/2fa"),
        response('{"possibleAuthenticationMechanisms": ["googleAuthenticator"]}', "/2fa/api/v1/config"),
        response(bootstrap({"id": 42})),
    ]
    school._session.post.return_value = response('{}', "/2fa/api/v1/google-authenticator")
    assert school.get_authenticated_user() == {"id": 42}
    assert school._session.post.call_count == 1


def test_missing_identity_reports_parsing_failure(school):
    school._session.get.return_value = response(bootstrap(None))
    with pytest.raises(session_module.SmartSchoolParsingError, match="authenticated user ID"):
        school.get_authenticated_user()


def test_rejected_login_reports_authentication_failure(school):
    school._session.get.return_value = response(path="/login")
    school._do_login = Mock(return_value=response(path="/login"))
    with pytest.raises(session_module.SmartSchoolAuthenticationError):
        school.get_authenticated_user()


def test_planner_resolves_user_before_building_url(school):
    from datetime import date

    planner_module = importlib.import_module(f"{package.__name__}.planner")
    school.get_authenticated_user = Mock(return_value={"id": 42})
    school.json = Mock(return_value=[])
    assert list(planner_module.Planner(school, date(2026, 11, 2), date(2026, 12, 2))) == []
    school.get_authenticated_user.assert_called_once_with()
    assert "/planned-elements/user/42?" in school.json.call_args.args[0]
