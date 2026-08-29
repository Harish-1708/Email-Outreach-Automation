"""Pure logic for tracking which EMAIL_ACCOUNT_SLOT_N secrets are in use.

GitHub Secrets can never be read back — not by this app, not by any
token — so Streamlit has no way to ask GitHub "which slots are free?"
directly. Instead, a small, non-secret YAML file is committed to the
repo (config/email_account_slots.yaml) mapping each account's name to
its slot number AND its address. Neither of those is sensitive — the
address is the visible "From" on every email that account sends anyway —
so this file is safe to commit in plain text, unlike the app_password,
which only ever exists inside an encrypted GitHub Secret.

This file becomes the one authoritative "account directory" once an
account is managed through this app — see accounts_logic.py, which
merges it with the legacy Streamlit-secrets-based directory during the
transition (same merge pattern as outreach.load_email_accounts).
"""
import os
import sys
from typing import Dict, List, Optional

import yaml

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import outreach  # noqa: E402

SLOT_MAPPING_PATH = "config/email_account_slots.yaml"


def parse_slot_mapping(raw_yaml: str) -> Dict[str, Dict]:
    """{account_name: {"slot": int, "address": str}}. Returns {} for an
    empty or not-yet-existing file — every account slot management
    feature starts from zero accounts tracked, not an error."""
    if not raw_yaml or not raw_yaml.strip():
        return {}
    data = yaml.safe_load(raw_yaml) or {}
    mapping = {}
    for name, entry in data.items():
        mapping[name] = {"slot": int(entry["slot"]), "address": entry.get("address", "")}
    return mapping


def serialize_slot_mapping(mapping: Dict[str, Dict]) -> bytes:
    # sort_keys so the committed file's diff is stable/reviewable rather
    # than reordering on every edit based on dict insertion order.
    return yaml.safe_dump(mapping, sort_keys=True, default_flow_style=False).encode("utf-8")


def find_next_free_slot(mapping: Dict[str, Dict], slot_count: int = outreach.EMAIL_ACCOUNT_SLOT_COUNT) -> Optional[int]:
    used_slots = {entry["slot"] for entry in mapping.values()}
    for i in range(1, slot_count + 1):
        if i not in used_slots:
            return i
    return None


def add_account_to_mapping(mapping: Dict[str, Dict], account_name: str, address: str,
                            slot_count: int = outreach.EMAIL_ACCOUNT_SLOT_COUNT) -> Dict[str, Dict]:
    """Returns a NEW mapping — never mutates the input. Raises ValueError
    if the name already exists (use edit, not add) or every slot is full."""
    if account_name in mapping:
        raise ValueError(f"Account '{account_name}' already exists (slot {mapping[account_name]['slot']}).")
    next_slot = find_next_free_slot(mapping, slot_count)
    if next_slot is None:
        raise ValueError(f"All {slot_count} account slots are full.")
    updated = dict(mapping)
    updated[account_name] = {"slot": next_slot, "address": address}
    return updated


def remove_account_from_mapping(mapping: Dict[str, Dict], account_name: str) -> Dict[str, Dict]:
    """Returns a NEW mapping with account_name removed — never mutates
    the input, and never raises if the name wasn't present, since the
    caller's goal ("this shouldn't be tracked") is already satisfied
    either way."""
    updated = dict(mapping)
    updated.pop(account_name, None)
    return updated


def update_account_address_in_mapping(mapping: Dict[str, Dict], account_name: str, new_address: str) -> Dict[str, Dict]:
    """Returns a NEW mapping with account_name's address updated — the
    slot number never changes on an edit, only add/remove touch it.
    Raises ValueError if the account isn't tracked."""
    if account_name not in mapping:
        raise ValueError(f"Account '{account_name}' isn't tracked in the slot mapping.")
    updated = dict(mapping)
    updated[account_name] = {"slot": mapping[account_name]["slot"], "address": new_address}
    return updated


def get_account_names(mapping: Dict[str, Dict]) -> List[str]:
    return sorted(mapping.keys())
