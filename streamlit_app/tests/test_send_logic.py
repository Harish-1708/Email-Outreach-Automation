import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from send_logic import (
    confirmation_is_valid, build_send_inputs, build_preview_inputs, build_check_replies_inputs,
)


def test_confirmation_requires_exact_match():
    assert confirmation_is_valid("SEND") is True
    assert confirmation_is_valid("send") is False
    assert confirmation_is_valid("Send") is False
    assert confirmation_is_valid("SEND ") is False
    assert confirmation_is_valid("") is False


def test_build_send_inputs_minimal():
    inputs = build_send_inputs(campaign="Foo", stage="intro", batch_size=10)
    assert inputs == {
        "campaign": "Foo", "stage": "intro", "batch_size": "10",
        "variant": "Auto", "confirm": "SEND",
    }


def test_build_send_inputs_includes_overrides_only_when_set():
    inputs = build_send_inputs(campaign="Foo", stage="intro", batch_size=10,
                                daily_limit=50, per_account_daily_limit=5, sender_rotation=True)
    assert inputs["daily_limit"] == "50"
    assert inputs["per_account_daily_limit"] == "5"
    assert inputs["sender_rotation"] == "true"


def test_build_send_inputs_omits_overrides_when_none():
    inputs = build_send_inputs(campaign="Foo", stage="intro", batch_size=10)
    assert "daily_limit" not in inputs
    assert "per_account_daily_limit" not in inputs
    assert "sender_rotation" not in inputs


def test_build_send_inputs_sender_rotation_false_is_string_false_not_omitted():
    inputs = build_send_inputs(campaign="Foo", stage="intro", batch_size=10, sender_rotation=False)
    assert inputs["sender_rotation"] == "false"


def test_build_send_inputs_always_includes_literal_send_confirm():
    inputs = build_send_inputs(campaign="Foo", stage="intro", batch_size=10)
    assert inputs["confirm"] == "SEND"


def test_build_preview_inputs():
    assert build_preview_inputs("Foo", "intro", 5) == {
        "campaign": "Foo", "stage": "intro", "batch_size": "5", "variant": "Auto",
    }


def test_build_check_replies_inputs():
    assert build_check_replies_inputs("Foo") == {"campaign": "Foo"}
