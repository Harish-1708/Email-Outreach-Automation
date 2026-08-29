import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from launch_logic import build_status_override, STATUS_ACTIVE, STATUS_PAUSED


def test_build_status_override_sets_status_on_empty_override():
    updated = build_status_override({}, STATUS_ACTIVE)
    assert updated == {"status": "active"}


def test_build_status_override_preserves_sending_and_schedule():
    raw = {"sending": {"daily_limit": 100}, "schedule": {"timezone": "UTC"}}
    updated = build_status_override(raw, STATUS_PAUSED)
    assert updated["status"] == "paused"
    assert updated["sending"] == {"daily_limit": 100}
    assert updated["schedule"] == {"timezone": "UTC"}


def test_build_status_override_never_mutates_input():
    raw = {"status": "draft"}
    build_status_override(raw, STATUS_ACTIVE)
    assert raw == {"status": "draft"}


def test_build_status_override_overwrites_existing_status():
    raw = {"status": "paused"}
    updated = build_status_override(raw, STATUS_ACTIVE)
    assert updated["status"] == "active"


def test_build_status_override_can_set_draft_to_active_launch():
    raw = {"status": "draft"}
    updated = build_status_override(raw, STATUS_ACTIVE)
    assert updated["status"] == "active"
