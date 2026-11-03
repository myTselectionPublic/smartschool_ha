"""Exercise the options flow without requiring Home Assistant to be installed."""

import ast
import asyncio
from collections import OrderedDict
import logging
from pathlib import Path
import runpy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import voluptuous as vol


COMPONENT_PATH = Path(__file__).resolve().parents[1] / "custom_components" / "smartschool"
OPTION = "include_result_details"


class Flow:
    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__()

    @property
    def config_entry(self):
        return self._entry

    def async_show_form(self, **kwargs):
        return {"type": "form", **kwargs}

    def async_create_entry(self, **kwargs):
        return {"type": "create_entry", **kwargs}


@pytest.fixture
def flow_namespace():
    source = ast.parse((COMPONENT_PATH / "config_flow.py").read_text(encoding="utf-8"))
    namespace = runpy.run_path(str(COMPONENT_PATH / "const.py"))
    namespace.update(
        vol=vol, OrderedDict=OrderedDict, callback=lambda fn: fn,
        config_entries=SimpleNamespace(ConfigFlow=Flow, OptionsFlow=Flow, CONN_CLASS_CLOUD_POLL="cloud_poll"),
        CONF_USERNAME="username", CONF_PASSWORD="password", NAME="Smartschool",
    )
    nodes = [node for node in source.body if isinstance(node, (ast.ClassDef, ast.FunctionDef))]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "config_flow.py", "exec"), namespace)
    return namespace


def test_setup_schema_offers_enabled_default_and_accepts_false(flow_namespace):
    schema = vol.Schema(flow_namespace["create_schema"](None))
    credentials = {"smartschool_domain": "school.smartschool.be", "username": "student",
                   "password": "password", "mfa": "2000-01-01"}
    assert schema(credentials)[OPTION] is True
    assert schema({**credentials, OPTION: False})[OPTION] is False


@pytest.mark.parametrize("data,options,expected", [
    ({}, {}, True),
    ({OPTION: False}, {}, False),
    ({OPTION: True}, {OPTION: False}, False),
    ({OPTION: False}, {OPTION: True}, True),
])
def test_configure_form_uses_saved_value(flow_namespace, data, options, expected):
    handler = flow_namespace["ComponentFlowHandler"].async_get_options_flow(None)
    handler._entry = SimpleNamespace(data=data, options=options)
    form = asyncio.run(handler.async_step_init())
    assert form["step_id"] == "init"
    assert form["data_schema"]({}) == {OPTION: expected}


@pytest.mark.parametrize("enabled", [False, True])
def test_saving_options_preserves_other_settings_and_credentials(flow_namespace, enabled):
    handler = flow_namespace["ComponentOptionsHandler"]()
    credentials = {"username": "student", "password": "password"}
    handler._entry = SimpleNamespace(data=credentials.copy(), options={"refresh_interval": 45})
    result = asyncio.run(handler.async_step_init({OPTION: enabled}))
    assert result == {"type": "create_entry", "title": "",
                      "data": {"refresh_interval": 45, OPTION: enabled}}
    assert handler.config_entry.data == credentials


def test_sensor_setup_applies_option_over_initial_config():
    source = ast.parse((COMPONENT_PATH / "sensor.py").read_text(encoding="utf-8"))
    setup = next(node for node in source.body if isinstance(node, ast.AsyncFunctionDef)
                 and node.name == "async_setup_entry")
    dry_setup = AsyncMock()
    namespace = {"DOMAIN": "smartschool", "NAME": "Smartschool", "dry_setup": dry_setup,
                 "_LOGGER": logging.getLogger(__name__)}
    exec(compile(ast.Module(body=[setup], type_ignores=[]), "sensor.py", "exec"), namespace)
    entry = SimpleNamespace(entry_id="entry", data={OPTION: True, "username": "student"},
                            options={OPTION: False})
    coordinator = object()
    hass = SimpleNamespace(data={"smartschool": {"entry": {"coordinator": coordinator}}})
    add = Mock()
    asyncio.run(namespace["async_setup_entry"](hass, entry, add))
    dry_setup.assert_awaited_once_with(hass, {OPTION: False, "username": "student"}, add, coordinator)


def test_options_update_reloads_entry():
    source = ast.parse((COMPONENT_PATH / "__init__.py").read_text(encoding="utf-8"))
    reload = next(node for node in source.body if isinstance(node, ast.AsyncFunctionDef)
                  and node.name == "async_update_options")
    namespace = {"HomeAssistant": object, "ConfigEntry": object}
    exec(compile(ast.Module(body=[reload], type_ignores=[]), "__init__.py", "exec"), namespace)
    hass = SimpleNamespace(config_entries=SimpleNamespace(async_reload=AsyncMock()))
    entry = SimpleNamespace(entry_id="entry")
    asyncio.run(namespace["async_update_options"](hass, entry))
    hass.config_entries.async_reload.assert_awaited_once_with("entry")
