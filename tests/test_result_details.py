"""Verify result extraction and sensor attributes without a Home Assistant runtime."""

import ast
from datetime import datetime
import json
import logging
import math
from pathlib import Path
from types import SimpleNamespace

import pytest


COMPONENT_PATH = Path(__file__).resolve().parents[1] / "custom_components" / "smartschool"


def load_class(filename, class_name, methods=None, **namespace):
    source = ast.parse((COMPONENT_PATH / filename).read_text(encoding="utf-8"))
    cls = next(node for node in source.body if isinstance(node, ast.ClassDef) and node.name == class_name)
    if methods is not None:
        cls.body = [node for node in cls.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and node.name in methods]
    cls.bases = []
    exec(compile(ast.Module(body=[cls], type_ignores=[]), filename, "exec"), namespace)
    return namespace[class_name]


@pytest.fixture
def coordinator():
    cls = load_class(
        "coordinator.py", "ComponentUpdateCoordinator",
        {"extract_results", "result_detail", "get_result_details"},
        math=math, DOMAIN="smartschool", _LOGGER=logging.getLogger(__name__),
    )
    instance = cls()
    instance._unique_user_id = "student"
    return instance


def result(description="7,5 / 10", **overrides):
    teacher = SimpleNamespace(name=SimpleNamespace(
        startingWithLastName="Peeters Lotte", startingWithFirstName="Lotte Peeters"))
    fields = dict(
        name="Chapter 1", date=datetime.fromisoformat("2026-10-30T10:30:00+01:00"),
        graphic=SimpleNamespace(description=description),
        component=SimpleNamespace(name="Test"),
        courses=[SimpleNamespace(name="Mathematics - Class A", teachers=[teacher])],
        gradebookOwner=teacher,
        feedback=[SimpleNamespace(text="Good work")],
        feedbacks=[SimpleNamespace(text="Good work"), SimpleNamespace(text="Check question 2")],
        doesCount=True, type="normal",
    )
    fields.update(overrides)
    return SimpleNamespace(**fields)


def test_details_and_sensor_attribute(coordinator):
    coordinator._results = [result()]
    coordinator.extract_results()
    expected = [{
        "subject": "Mathematics", "category": "Test",
        "date": "2026-10-30T10:30:00+01:00", "title": "Chapter 1",
        "obtained": 7.5, "maximum": 10.0, "percentage": 75.0,
        "teacher": "Peeters Lotte", "feedback": "Good work\nCheck question 2",
    }]
    assert coordinator.get_result_details() == expected
    assert json.loads(json.dumps(expected, allow_nan=False)) == expected
    sensor_cls = load_class(
        "sensor.py", "ComponentResultsSensor", {"extra_state_attributes"},
        ATTR_ATTRIBUTION="attribution", NAME="Smartschool", _LOGGER=logging.getLogger(__name__),
    )
    sensor = sensor_cls()
    sensor._data = SimpleNamespace(
        _total_result=coordinator._total_result,
        _include_result_details=True,
        _result_details=coordinator.get_result_details(),
        _results_per_course=coordinator._results_per_course,
        _results_per_course_max=coordinator._results_per_course_max,
    )
    sensor._username, sensor._school, sensor._last_update = "student", "school", None
    attrs = sensor.extra_state_attributes
    assert attrs["result_details"] == expected
    assert attrs["total result"] == attrs["Mathematics (Peeters Lotte) - Test"] == "75.0%"
    sensor._data._include_result_details = False
    without_details = sensor.extra_state_attributes
    assert "result_details" not in without_details
    assert without_details == {key: value for key, value in attrs.items() if key != "result_details"}


def test_all_evaluations_are_included_but_only_counted_normal_results_affect_total(coordinator):
    coordinator._results = [result(), result("0/10", doesCount=False), result("0/20", type="project")]
    coordinator.extract_results()
    assert len(coordinator.get_result_details()) == 3
    assert coordinator._total_result == 75.0
    assert coordinator._results_per_course_max == {"Mathematics (Peeters Lotte)": {"Test": 10.0}}


@pytest.mark.parametrize("description,obtained,maximum,percentage", [
    ("0/10", 0.0, 10.0, 0.0),
    ("1/3", 1.0, 3.0, 33.33),
    ("0/0", 0.0, 0.0, None),
    ("Absent", None, None, None),
    (None, None, None, None),
    ("bad/10", None, None, None),
    ("5/bad", None, None, None),
    ("nan/10", None, None, None),
    ("5/inf", None, None, None),
])
def test_unscored_and_invalid_scores(coordinator, description, obtained, maximum, percentage):
    coordinator._results = [result(description)]
    coordinator.extract_results()
    detail = coordinator.get_result_details()[0]
    assert (detail["obtained"], detail["maximum"], detail["percentage"]) == (obtained, maximum, percentage)
    json.dumps(detail, allow_nan=False)


def test_missing_course_component_and_feedback(coordinator):
    coordinator._results = [result(courses=[], component=None, feedback=None, feedbacks=[])]
    coordinator.extract_results()
    detail = coordinator.get_result_details()[0]
    assert detail["subject"] == "Unknown Course"
    assert detail["category"] == "normal"
    assert detail["teacher"] == "Peeters Lotte"
    assert detail["feedback"] == ""


@pytest.mark.parametrize("empty_results", [None, []])
def test_empty_refresh_clears_previous_details(coordinator, empty_results):
    coordinator._results = [result()]
    coordinator.extract_results()
    coordinator._results = empty_results
    coordinator.extract_results()
    assert coordinator.get_result_details() == []
    assert coordinator._total_result is None
    assert coordinator._results_per_course == {}
