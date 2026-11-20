"""Exercise legacy Agenda fallback without live accounts or Home Assistant."""

import ast
import asyncio
from datetime import datetime, timedelta
import importlib
import json
import logging
from pathlib import Path
import sys
import types
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import ValidationError
from pydantic.dataclasses import rebuild_dataclass
from requests import Response


COMPONENT_PATH = Path(__file__).resolve().parents[1] / "custom_components/smartschool"
# Keep real session and model imports separate from the message-model stubs.
package = types.ModuleType("_smartschool_agenda_tests")
package.__path__ = [str(COMPONENT_PATH / "smartschool_api")]
sys.modules.setdefault(package.__name__, package)
session_module = importlib.import_module(f"{package.__name__}.session")
credentials_module = importlib.import_module(f"{package.__name__}.credentials")
objects_module = importlib.import_module(f"{package.__name__}.objects")
LegacyAgendaUnavailable = session_module.LegacyAgendaUnavailable
FutureTasks = objects_module.FutureTasks
rebuild_dataclass(FutureTasks, _types_namespace={"Credentials": credentials_module.Credentials})


def response(path, body="{}", status=200, host="school.smartschool.be"):
    """Build the final response returned by requests after following redirects."""
    result = Response()
    result.status_code = status
    result.url = f"https://{host}{path}"
    result._content = body.encode("utf-8")
    result.encoding = "utf-8"
    return result


@pytest.fixture
def school():
    instance = session_module.Smartschool(creds=credentials_module.AppCredentials(
        "student", "password", "school.smartschool.be", "2000-01-01",
    ))
    instance._session = Mock()
    instance._try_login = Mock()
    return instance


@pytest.mark.parametrize("endpoint", ["/Agenda/Futuretasks/getFuturetasks", "/?module=Agenda"])
def test_retired_agenda_redirect_raises_specific_exception(school, endpoint):
    school._session.post.return_value = response("/?module=Homepage", "<html>Homepage</html>")
    with pytest.raises(LegacyAgendaUnavailable):
        school.post(endpoint)


@pytest.mark.parametrize("endpoint,path,status,host", [
    ("/Agenda/Futuretasks/getFuturetasks", "/login", 200, "school.smartschool.be"),
    ("/Agenda/Futuretasks/getFuturetasks", "/2fa", 200, "school.smartschool.be"),
    ("/Agenda/Futuretasks/getFuturetasks", "/account-verification", 200, "school.smartschool.be"),
    ("/Agenda/Futuretasks/getFuturetasks", "/login?module=Homepage", 200, "school.smartschool.be"),
    ("/Agenda/Futuretasks/getFuturetasks", "/?module=Homepage", 200, "other.smartschool.be"),
    ("/Agenda/Futuretasks/getFuturetasks", "/?module=Homepage", 500, "school.smartschool.be"),
    ("/planner/api/v1/planned-elements", "/?module=Homepage", 200, "school.smartschool.be"),
])
def test_other_responses_are_not_classified_as_retired_agenda(school, endpoint, path, status, host):
    final_response = response(path, status=status, host=host)
    school._session.post.return_value = final_response
    assert school.post(endpoint) is final_response


def test_retired_agenda_exception_survives_futuretasks_validation(school):
    school._session.post.return_value = response("/?module=Homepage", "<html>Homepage</html>")
    with pytest.raises(LegacyAgendaUnavailable):
        FutureTasks(smartschool=school)


def test_active_legacy_agenda_still_parses_tasks(school):
    school._session.post.return_value = response(
        "/Agenda/Futuretasks/getFuturetasks",
        json.dumps({"days": [], "last_assignment_id": 17, "last_date": "2026-11-20"}),
    )
    tasks = FutureTasks(smartschool=school)
    assert tasks.days == []
    assert tasks.last_assignment_id == 17


def test_unexpected_invalid_json_is_still_an_error(school):
    school._session.post.return_value = response("/Agenda/Futuretasks/getFuturetasks", "not JSON")
    with pytest.raises(ValidationError, match="Failed to decode JSON"):
        FutureTasks(smartschool=school)


class UpdateFailed(Exception):
    """Represent the HA error at the coordinator's existing exception boundary."""


@pytest.fixture
def refresh():
    # Exercise the production async method while replacing only HA's scheduler
    # and exception type. Other tests load extraction methods the same way.
    source = ast.parse((COMPONENT_PATH / "coordinator.py").read_text(encoding="utf-8"))
    cls = next(node for node in source.body if isinstance(node, ast.ClassDef)
               and node.name == "ComponentUpdateCoordinator")
    method = next(node for node in cls.body if isinstance(node, ast.AsyncFunctionDef)
                  and node.name == "_async_update_data")
    namespace = dict(
        datetime=datetime, timedelta=timedelta, DOMAIN="smartschool",
        LegacyAgendaUnavailable=LegacyAgendaUnavailable, UpdateFailed=UpdateFailed,
        _LOGGER=logging.getLogger(__name__),
    )
    exec(compile(ast.Module(body=[method], type_ignores=[]), "coordinator.py", "exec"), namespace)
    return namespace["_async_update_data"]


@pytest.fixture
def coordinator():
    async def execute(callback):
        return callback()

    session = types.SimpleNamespace(
        login=Mock(return_value={}), getFutureTasks=Mock(return_value=object()),
        getAgenda=Mock(return_value=[]), getMessages=Mock(return_value=[]),
        getResults=Mock(return_value=[]), getPlanner=Mock(return_value=[]),
    )
    return types.SimpleNamespace(
        _session=session, _hass=types.SimpleNamespace(async_add_executor_job=execute),
        _username="student", _password="password", _smartschool_domain="school.smartschool.be",
        _mfa="2000-01-01", _lists={"ready": []}, _async_local_refresh_data=AsyncMock(),
    )


@pytest.mark.parametrize("method", ["getFutureTasks", "getAgenda"])
def test_retired_agenda_does_not_prevent_loading_planner(refresh, coordinator, method):
    getattr(coordinator._session, method).side_effect = LegacyAgendaUnavailable("Agenda retired")
    assert asyncio.run(refresh(coordinator)) == {"ready": []}
    assert coordinator._future_tasks is None
    assert coordinator._agenda == []
    coordinator._session.getMessages.assert_called_once()
    coordinator._session.getResults.assert_called_once()
    assert coordinator._session.getPlanner.call_count == 2
    coordinator._async_local_refresh_data.assert_awaited_once()


def test_active_legacy_agenda_continues_loading_alongside_planner(refresh, coordinator):
    tasks = coordinator._session.getFutureTasks.return_value
    assert asyncio.run(refresh(coordinator)) == {"ready": []}
    assert coordinator._future_tasks is tasks
    coordinator._session.getAgenda.assert_called_once()
    assert coordinator._session.getPlanner.call_count == 2


@pytest.mark.parametrize("error", [ValueError("Malformed response"),
                                  session_module.SmartSchoolAuthenticationError("Authentication failed")])
def test_unexpected_legacy_failures_are_not_silently_ignored(refresh, coordinator, error):
    coordinator._session.getFutureTasks.side_effect = error
    with pytest.raises(UpdateFailed):
        asyncio.run(refresh(coordinator))
    coordinator._session.getPlanner.assert_not_called()
    coordinator._async_local_refresh_data.assert_not_awaited()


def test_failed_planner_is_not_reported_as_empty_work(refresh, coordinator):
    coordinator._session.getFutureTasks.side_effect = LegacyAgendaUnavailable("Agenda retired")
    coordinator._session.getPlanner.side_effect = ValueError("Planner unavailable")
    with pytest.raises(UpdateFailed, match="Planner unavailable"):
        asyncio.run(refresh(coordinator))
    coordinator._async_local_refresh_data.assert_not_awaited()
