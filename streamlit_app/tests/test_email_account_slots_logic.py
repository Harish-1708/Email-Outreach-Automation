import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from email_account_slots_logic import (
    parse_slot_mapping, serialize_slot_mapping, find_next_free_slot,
    add_account_to_mapping, remove_account_from_mapping, update_account_address_in_mapping,
    get_account_names, read_local_slot_mapping,
)


# ---------- parse_slot_mapping ----------

def test_parse_slot_mapping_empty_string_returns_empty_dict():
    assert parse_slot_mapping("") == {}
    assert parse_slot_mapping("   ") == {}


def test_parse_slot_mapping_parses_real_yaml():
    raw = "sales1:\n  slot: 1\n  address: sales1@gmail.com\nsales2:\n  slot: 2\n  address: sales2@gmail.com\n"
    mapping = parse_slot_mapping(raw)
    assert mapping == {
        "sales1": {"slot": 1, "address": "sales1@gmail.com"},
        "sales2": {"slot": 2, "address": "sales2@gmail.com"},
    }


def test_parse_slot_mapping_missing_address_defaults_to_empty_string():
    raw = "sales1:\n  slot: 1\n"
    mapping = parse_slot_mapping(raw)
    assert mapping["sales1"]["address"] == ""


def test_parse_slot_mapping_coerces_slot_to_int():
    raw = "sales1:\n  slot: '1'\n  address: a@b.com\n"  # YAML string, not int
    mapping = parse_slot_mapping(raw)
    assert mapping["sales1"]["slot"] == 1
    assert isinstance(mapping["sales1"]["slot"], int)


# ---------- serialize_slot_mapping ----------

def test_serialize_slot_mapping_round_trips_through_parse():
    mapping = {"sales1": {"slot": 1, "address": "sales1@gmail.com"}}
    raw_bytes = serialize_slot_mapping(mapping)
    reparsed = parse_slot_mapping(raw_bytes.decode("utf-8"))
    assert reparsed == mapping


def test_serialize_slot_mapping_empty_dict():
    raw_bytes = serialize_slot_mapping({})
    assert parse_slot_mapping(raw_bytes.decode("utf-8")) == {}


# ---------- find_next_free_slot ----------

def test_find_next_free_slot_empty_mapping_returns_1():
    assert find_next_free_slot({}, slot_count=10) == 1


def test_find_next_free_slot_skips_used_slots():
    mapping = {"sales1": {"slot": 1, "address": "a@b.com"}, "sales2": {"slot": 2, "address": "b@c.com"}}
    assert find_next_free_slot(mapping, slot_count=10) == 3


def test_find_next_free_slot_fills_gaps_not_just_appends():
    # slot 2 was freed by a removal — should be reused, not skipped.
    mapping = {"sales1": {"slot": 1, "address": "a@b.com"}, "sales3": {"slot": 3, "address": "c@d.com"}}
    assert find_next_free_slot(mapping, slot_count=10) == 2


def test_find_next_free_slot_returns_none_when_full():
    mapping = {f"acct{i}": {"slot": i, "address": f"a{i}@b.com"} for i in range(1, 11)}
    assert find_next_free_slot(mapping, slot_count=10) is None


# ---------- add_account_to_mapping ----------

def test_add_account_to_mapping_assigns_next_free_slot():
    mapping = {"sales1": {"slot": 1, "address": "a@b.com"}}
    updated = add_account_to_mapping(mapping, "sales2", "b@c.com", slot_count=10)
    assert updated["sales2"] == {"slot": 2, "address": "b@c.com"}


def test_add_account_to_mapping_never_mutates_input():
    mapping = {"sales1": {"slot": 1, "address": "a@b.com"}}
    add_account_to_mapping(mapping, "sales2", "b@c.com", slot_count=10)
    assert mapping == {"sales1": {"slot": 1, "address": "a@b.com"}}  # untouched


def test_add_account_to_mapping_rejects_duplicate_name():
    mapping = {"sales1": {"slot": 1, "address": "a@b.com"}}
    try:
        add_account_to_mapping(mapping, "sales1", "new@b.com", slot_count=10)
        assert False, "should have raised ValueError"
    except ValueError as exc:
        assert "already exists" in str(exc)


def test_add_account_to_mapping_rejects_when_all_slots_full():
    mapping = {f"acct{i}": {"slot": i, "address": f"a{i}@b.com"} for i in range(1, 11)}
    try:
        add_account_to_mapping(mapping, "one_too_many", "x@y.com", slot_count=10)
        assert False, "should have raised ValueError"
    except ValueError as exc:
        assert "slots are full" in str(exc)


# ---------- remove_account_from_mapping ----------

def test_remove_account_from_mapping_removes_entry():
    mapping = {"sales1": {"slot": 1, "address": "a@b.com"}, "sales2": {"slot": 2, "address": "b@c.com"}}
    updated = remove_account_from_mapping(mapping, "sales1")
    assert "sales1" not in updated
    assert "sales2" in updated


def test_remove_account_from_mapping_never_mutates_input():
    mapping = {"sales1": {"slot": 1, "address": "a@b.com"}}
    remove_account_from_mapping(mapping, "sales1")
    assert mapping == {"sales1": {"slot": 1, "address": "a@b.com"}}


def test_remove_account_from_mapping_missing_name_is_a_noop_not_an_error():
    mapping = {"sales1": {"slot": 1, "address": "a@b.com"}}
    updated = remove_account_from_mapping(mapping, "never_existed")
    assert updated == mapping


def test_remove_then_add_reuses_freed_slot():
    mapping = {"sales1": {"slot": 1, "address": "a@b.com"}, "sales2": {"slot": 2, "address": "b@c.com"}}
    after_remove = remove_account_from_mapping(mapping, "sales1")
    after_add = add_account_to_mapping(after_remove, "sales3", "c@d.com", slot_count=10)
    assert after_add["sales3"]["slot"] == 1  # reused the freed slot, not slot 3


# ---------- update_account_address_in_mapping ----------

def test_update_account_address_keeps_same_slot():
    mapping = {"sales1": {"slot": 1, "address": "old@b.com"}}
    updated = update_account_address_in_mapping(mapping, "sales1", "new@b.com")
    assert updated["sales1"] == {"slot": 1, "address": "new@b.com"}


def test_update_account_address_never_mutates_input():
    mapping = {"sales1": {"slot": 1, "address": "old@b.com"}}
    update_account_address_in_mapping(mapping, "sales1", "new@b.com")
    assert mapping["sales1"]["address"] == "old@b.com"


def test_update_account_address_raises_for_unknown_account():
    try:
        update_account_address_in_mapping({}, "ghost", "x@y.com")
        assert False, "should have raised ValueError"
    except ValueError as exc:
        assert "ghost" in str(exc)


# ---------- get_account_names ----------

def test_get_account_names_sorted():
    mapping = {"sales2": {"slot": 2, "address": "b@c.com"}, "sales1": {"slot": 1, "address": "a@b.com"}}
    assert get_account_names(mapping) == ["sales1", "sales2"]


def test_get_account_names_empty_mapping():
    assert get_account_names({}) == []


# ---------- read_local_slot_mapping ----------

def test_read_local_slot_mapping_returns_empty_when_file_missing(tmp_path):
    missing_path = str(tmp_path / "config" / "email_account_slots.yaml")
    assert read_local_slot_mapping(missing_path) == {}


def test_read_local_slot_mapping_reads_real_file(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    path = config_dir / "email_account_slots.yaml"
    path.write_text("sales1:\n  slot: 1\n  address: sales1@gmail.com\n")
    mapping = read_local_slot_mapping(str(path))
    assert mapping == {"sales1": {"slot": 1, "address": "sales1@gmail.com"}}
