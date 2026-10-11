"""Exercise planner extraction without requiring a Home Assistant installation."""

import ast
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import Enum
import logging
from pathlib import Path
import runpy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


COMPONENT_PATH = Path(__file__).resolve().parents[1] / "custom_components" / "smartschool"


class TodoItemStatus(str, Enum):
    NEEDS_ACTION = "needs_action"
    COMPLETED = "completed"


@dataclass
class TodoItem:
    uid: str
    summary: str
    status: TodoItemStatus
    description: str
    due: date | datetime


@pytest.fixture
def coordinator():
    # Load the actual extraction methods in isolation; their dependencies here
    # are the TodoItem value object and the status store, not the HA runtime.
    source = ast.parse((COMPONENT_PATH / "coordinator.py").read_text(encoding="utf-8"))
    cls = next(node for node in source.body if isinstance(node, ast.ClassDef))
    methods = ast.Module(
        body=[node for node in cls.body if isinstance(node, ast.FunctionDef)
              and node.name in {"extract_planner_assignments", "format_planner_date"}],
        type_ignores=[],
    )
    namespace = runpy.run_path(str(COMPONENT_PATH / "const.py"))
    namespace.update(datetime=datetime, timedelta=timedelta, TodoItem=TodoItem,
                     TodoItemStatus=TodoItemStatus, _LOGGER=logging.getLogger("planner_test"))
    exec(compile(methods, str(COMPONENT_PATH / "coordinator.py"), "exec"), namespace)
    coordinator_class = type("PlannerCoordinator", (), {
        name: namespace[name] for name in ("extract_planner_assignments", "format_planner_date")
    })
    instance = coordinator_class()
    instance._course_icons = {}
    instance._unique_user_id = "test_user"
    instance._status_store = Mock()
    instance._status_store.get_status.return_value = "needs_action"
    return instance


def extract(coordinator, item, next_schoolday):
    coordinator._planner_assignments = [item]
    lists = {name: [] for name in ("Taken", "Toetsen", "Meebrengen", "Volgende", "Schooltas")}
    uids = set()
    count = coordinator.extract_planner_assignments(
        *lists, lists, uids, next_schoolday, 0
    )
    return lists, uids, count


def planner_item(whole_day=True):
    start = "2026-09-22T00:00:00+02:00" if whole_day else "2026-09-24T12:45:00+02:00"
    return SimpleNamespace(
        id="all-day-todo" if whole_day else "timed-todo",
        plannedElementType="planned-to-dos",
        assignmentType=None,
        courses=[],
        name="TAAK INLEVEREN WI" if whole_day else "bespreking ijkingsexamen",
        period=SimpleNamespace(dateTimeFrom=datetime.fromisoformat(start), wholeDay=whole_day),
        resolvedStatus="unresolved",
    )


@pytest.mark.parametrize("whole_day", [True, False])
def test_personal_to_dos_are_tasks(coordinator, caplog, whole_day):
    item = planner_item(whole_day)
    lists, uids, count = extract(coordinator, item, item.period.dateTimeFrom.date().isoformat())

    assert count == 1
    assert uids == {item.id}
    assert not lists["Meebrengen"] and not lists["Schooltas"] and not lists["Toetsen"]
    assert len(lists["Taken"]) == len(lists["Volgende"]) == 1
    expected_due = item.period.dateTimeFrom.date() if whole_day else item.period.dateTimeFrom
    for name in ("Taken", "Volgende"):
        todo = lists[name][0]
        assert todo.uid == item.id
        assert todo.description == item.name
        assert todo.status == TodoItemStatus.NEEDS_ACTION
        assert todo.due == expected_due
        assert "Onbekend" not in todo.summary
        if whole_day:
            assert "00:00" not in todo.summary
        else:
            assert "12:45" in todo.summary
    assert "Taak" in lists["Taken"][0].summary
    assert item.name in lists["Volgende"][0].summary
    assert not caplog.records


def test_future_to_do_only_appears_in_tasks(coordinator):
    lists, _, count = extract(coordinator, planner_item(), "2026-09-23")
    assert len(lists["Taken"]) == 1
    assert not lists["Volgende"]
    assert count == 0


def test_resolved_to_do_updates_saved_status(coordinator):
    item = planner_item()
    item.resolvedStatus = "resolved"
    lists, _, _ = extract(coordinator, item, "2026-09-22")
    assert lists["Taken"][0].status == lists["Volgende"][0].status == TodoItemStatus.COMPLETED
    coordinator._status_store.set_status.assert_called_once_with("test_user", item.id, "completed")


@pytest.mark.parametrize("label, expected_list", [("Taak", "Taken"), ("Gewone toets", "Toetsen"), ("Meebrengen", "Meebrengen")])
def test_typed_to_dos_keep_assignment_classification(coordinator, label, expected_list):
    item = planner_item()
    item.assignmentType = SimpleNamespace(name=label, abbreviation="")
    lists, _, _ = extract(coordinator, item, "2026-09-22")
    assert len(lists[expected_list]) == 1
    assert bool(lists["Schooltas"]) == (label == "Meebrengen")


def test_unknown_assignment_keeps_warning_and_fallback(coordinator, caplog):
    item = planner_item()
    item.plannedElementType = "planned-assignments"
    lists, _, _ = extract(coordinator, item, "2026-09-22")
    assert len(lists["Meebrengen"]) == 1
    assert not lists["Taken"]
    assert "unknown type" in caplog.text
