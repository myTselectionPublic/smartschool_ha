"""Exercise the real HTTP session code without importing Home Assistant."""

import importlib
import ast
import hashlib
import json
import logging
from datetime import datetime
from pathlib import Path
import sys
import types
from unittest.mock import Mock

import pytest
from requests import Response
from requests.cookies import create_cookie


API_PATH = Path(__file__).resolve().parents[1] / "custom_components/smartschool/smartschool_api"
# Use a separate package so other model tests' module stubs cannot replace the
# actual session or HTML parser under test.
package = types.ModuleType("_smartschool_login_tests")
package.__path__ = [str(API_PATH)]
sys.modules.setdefault(package.__name__, package)
session_module = importlib.import_module(f"{package.__name__}.session")
Smartschool = session_module.Smartschool
AppCredentials = importlib.import_module(f"{package.__name__}.credentials").AppCredentials


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


@pytest.fixture
def component_session_class():
    # Load the session adapter without HA imports or rate-limit delays. Its
    # login method still constructs the real credentials and HTTP session.
    source = ast.parse((API_PATH.parent / "utils.py").read_text(encoding="utf-8"))
    cls = next(node for node in source.body if isinstance(node, ast.ClassDef)
               and node.name == "ComponentSession")
    for method in cls.body:
        if isinstance(method, ast.FunctionDef):
            method.decorator_list = []
    namespace = dict(
        Path=Path, json=json, hashlib=hashlib, datetime=datetime,
        AppCredentials=AppCredentials, Smartschool=Smartschool,
        _LOGGER=logging.getLogger("component_session_test"),
    )
    exec(compile(ast.Module(body=[cls], type_ignores=[]), str(API_PATH.parent / "utils.py"), "exec"), namespace)
    return namespace["ComponentSession"]


def login(adapter, username="student", password="secret", domain="school.smartschool.be", mfa="JBSWY3DPEHPK3PXP"):
    adapter.login(username, password, domain, mfa)
    return adapter.smartschool


def test_refresh_reuses_http_session_and_cookies(component_session_class, tmp_path):
    adapter = component_session_class(cookie_directory=tmp_path)
    first = login(adapter)
    first._session.cookies.set_cookie(create_cookie("device", "known-device", domain="school.smartschool.be"))
    first.authenticated_user = {"id": 42}
    second = login(adapter)
    assert second is first
    assert list(second._session.cookies)[0].value == "known-device"
    assert second.authenticated_user == {"id": 42}


@pytest.mark.parametrize("changed", [
    {"username": "other"}, {"domain": "other.smartschool.be"},
    {"password": "new-secret"}, {"mfa": "GEZDGNBVGY3TQOJQ"},
])
def test_changed_credentials_replace_session(component_session_class, tmp_path, changed):
    adapter = component_session_class(cookie_directory=tmp_path)
    first = login(adapter)
    assert login(adapter, **changed) is not first


def test_cookies_survive_restart_and_are_isolated_by_account(component_session_class, tmp_path):
    first = login(component_session_class(cookie_directory=tmp_path))
    first._session.cookies.set_cookie(create_cookie(
        "device", "known-device", domain=".smartschool.be", secure=True,
        rest={"HttpOnly": None, "SameSite": "Lax"},
    ))
    first._session.cookies.set_cookie(create_cookie(
        "session", "signed-in", domain="school.smartschool.be", discard=True,
    ))
    first._session.cookies.set_cookie(create_cookie(
        "expired", "discard-me", domain="school.smartschool.be", expires=1,
    ))
    first._save_cookies()

    restored = login(component_session_class(cookie_directory=tmp_path))
    cookies = {cookie.name: cookie for cookie in restored._session.cookies}
    assert set(cookies) == {"device", "session"}
    assert cookies["device"].secure
    assert cookies["device"].domain == ".smartschool.be"
    assert cookies["device"].has_nonstandard_attr("HttpOnly")
    assert cookies["device"].get_nonstandard_attr("SameSite") == "Lax"
    assert cookies["session"].value == "signed-in"

    restored._session.get = Mock(return_value=response(bootstrap({"id": 42})))
    restored._session.post = Mock()
    restored.get("/data")
    restored._session.post.assert_not_called()
    assert restored.authenticated_user == {"id": 42}

    for changed in ({"username": "other"}, {"domain": "other.smartschool.be"}):
        other = login(component_session_class(cookie_directory=tmp_path), **changed)
        assert list(other._session.cookies) == []
        assert other.cookie_file != first.cookie_file


def test_request_saves_rotated_cookies(school, tmp_path):
    from http.cookiejar import LWPCookieJar

    school.cookie_file = tmp_path / "session.cookies"
    school._session.cookies = LWPCookieJar()
    school._session.get.return_value = response(bootstrap({"id": 42}))

    def post(*args, **kwargs):
        school._session.cookies.set_cookie(create_cookie("session", "rotated-token"))
        return response("{}")

    school._session.post.side_effect = post
    school.post("/data")
    restored = Smartschool(cookie_file=school.cookie_file)
    assert list(restored._session.cookies)[0].value == "rotated-token"


def test_expired_server_session_logs_in_with_saved_device_cookie(component_session_class, tmp_path):
    first = login(component_session_class(cookie_directory=tmp_path))
    first._session.cookies.set_cookie(create_cookie("device", "known-device", domain="school.smartschool.be"))
    first._save_cookies()

    restored = login(component_session_class(cookie_directory=tmp_path))
    login_form = ('<form name="login_form"><input name="login_form[_username]">'
                  '<input name="login_form[_password]"></form>')
    restored._session.get = Mock(side_effect=[
        response(path="/login"), response(login_form, "/login"), response("{}"),
    ])

    def post(*args, **kwargs):
        assert {cookie.name: cookie.value for cookie in restored._session.cookies}["device"] == "known-device"
        restored._session.cookies.set_cookie(create_cookie("session", "new-session", domain="school.smartschool.be"))
        return response(bootstrap({"id": 42}))

    restored._session.post = Mock(side_effect=post)
    restored.get("/data")
    restored._session.post.assert_called_once()
    restarted = login(component_session_class(cookie_directory=tmp_path))
    assert {cookie.name: cookie.value for cookie in restarted._session.cookies} == {
        "device": "known-device", "session": "new-session",
    }


def test_corrupt_cookie_file_allows_fresh_login(tmp_path, caplog):
    cookie_file = tmp_path / "session.cookies"
    cookie_file.write_text("not a cookie jar", encoding="utf-8")
    restored = Smartschool(cookie_file=cookie_file)
    assert list(restored._session.cookies) == []
    assert "new login will be required" in caplog.text


def test_cookie_write_failure_does_not_break_request(school, tmp_path, monkeypatch, caplog):
    from http.cookiejar import LWPCookieJar

    school.cookie_file = tmp_path / "session.cookies"
    school._session.cookies = LWPCookieJar()
    school._session.get.return_value = response(bootstrap({"id": 42}))
    monkeypatch.setattr(session_module.os, "replace", Mock(side_effect=OSError("disk error")))
    assert school.get("/data").status_code == 200
    assert "Could not save Smartschool cookies" in caplog.text
    assert list(tmp_path.iterdir()) == []
