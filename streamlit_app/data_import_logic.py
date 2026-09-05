"""Pure logic for the Data tab (Phase C). Nothing here touches the
network — CSV parsing and mapping happen entirely in-process. The actual
Sheet write happens via the same pattern as everywhere else in this app:
commit a JSON payload file, trigger a GitHub Actions workflow that reads
it and does the real write with the Editor-scoped credential. Streamlit
itself never gets Sheets write access, here or anywhere else.
"""
import csv
import io
import json
import re
from datetime import datetime
from typing import Dict, List, Optional, Tuple

KNOWN_FIELDS = ["FirstName", "LastName", "Email", "Company"]

NEW_CUSTOM_FIELD_OPTION = "\u2795 New custom field..."


def validate_custom_field_name(name: str, reserved_names: List[str]) -> Optional[str]:
    """A brand-new custom field name (typed when mapping a CSV column
    that doesn't match anything existing) can't be blank, and can't
    collide with one of the system's own tracked columns — that WOULD
    silently corrupt the system's own tracking data the moment this
    import writes to the Sheet, since a write only ever goes by column
    NAME, with no notion of "this one's reserved"."""
    name = (name or "").strip()
    if not name:
        return "Enter a name for the new custom field, or choose Skip instead."
    reserved_lower = {r.lower() for r in reserved_names}
    if name.lower() in reserved_lower:
        return f"'{name}' is already used internally by this system — pick a different name."
    return None


def parse_csv_bytes(raw_bytes: bytes) -> Tuple[List[str], List[Dict[str, str]]]:
    """Returns (column_names, rows). utf-8-sig handles a BOM from Excel
    exports without choking on it.

    A genuinely duplicate column NAME in the source CSV isn't corrected
    here — Python's own csv.DictReader silently keeps only the LAST
    duplicate-named column's value per row, discarding the earlier
    one(s), before this function ever sees the data. See
    find_duplicate_columns, which the Data tab calls separately to warn
    about this rather than staying silent about data that's already
    gone by this point."""
    text = raw_bytes.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    columns = reader.fieldnames or []
    rows = [dict(row) for row in reader]
    return columns, rows


def find_duplicate_columns(columns: List[str]) -> List[str]:
    """Column names appearing more than once in the CSV's own header row
    — sorted, de-duplicated. A real, actionable warning signal: for any
    name in this list, Python's csv.DictReader has already silently
    kept only the LAST occurrence's value per row and discarded the
    earlier one(s) before parse_csv_bytes even returns — the fix has to
    happen in the source file (rename one of the columns), not here."""
    seen = set()
    duplicates = set()
    for col in columns:
        if col in seen:
            duplicates.add(col)
        seen.add(col)
    return sorted(duplicates)


def _normalize(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def build_default_mapping(csv_columns: List[str], custom_columns: List[str],
                           reserved_names: Optional[List[str]] = None) -> Dict[str, str]:
    """Best-effort auto-mapping by normalized name match, so the user
    usually just reviews/adjusts rather than mapping from scratch.
    Returns {csv_column: target_field_or_empty_string} — "" means
    unmapped/skip. Known fields (FirstName/LastName/Email/Company) are
    preferred over a same-named custom column, in case of a clash.

    A column that doesn't match anything (including no existing custom
    column) defaults to a NEW custom field using ITS OWN name — the
    common case, bringing in creator-outreach data that's never been
    imported before, needs zero extra clicks or retyping. The one
    exception: a column whose own name collides with one of the
    system's own reserved tracking columns defaults to Skip instead,
    rather than risking a silent overwrite of real tracking data —
    same rule validate_custom_field_name enforces when someone
    explicitly types a new field name instead of using this default."""
    reserved_lower = {r.lower() for r in (reserved_names or [])}
    known_by_norm = {_normalize(f): f for f in KNOWN_FIELDS}
    custom_by_norm = {_normalize(c): c for c in custom_columns}
    mapping = {}
    for col in csv_columns:
        key = _normalize(col)
        if key in known_by_norm:
            mapping[col] = known_by_norm[key]
        elif key in custom_by_norm:
            mapping[col] = custom_by_norm[key]
        elif col.strip().lower() in reserved_lower:
            mapping[col] = ""
        else:
            mapping[col] = col.strip()
    return mapping


def apply_mapping(rows: List[Dict[str, str]], mapping: Dict[str, str]) -> List[Dict[str, str]]:
    """mapping: {csv_column: target_field}. Columns mapped to "" are
    dropped. Every value is stripped of surrounding whitespace."""
    mapped_rows = []
    for row in rows:
        mapped = {}
        for csv_col, target_field in mapping.items():
            if not target_field:
                continue
            mapped[target_field] = (row.get(csv_col) or "").strip()
        mapped_rows.append(mapped)
    return mapped_rows


def validate_mapping(mapping: Dict[str, str]) -> Optional[str]:
    if "Email" not in mapping.values():
        return "Map at least one column to Email — it's the only required field."
    return None


def count_valid_rows(mapped_rows: List[Dict[str, str]]) -> int:
    """How many rows actually have an email — the number that will really
    get imported, before duplicates against the existing Sheet are even
    considered (that check only happens server-side, since only the
    server has the full current lead list at write time)."""
    return sum(1 for r in mapped_rows if (r.get("Email") or "").strip())


def build_import_payload(mapped_rows: List[Dict[str, str]], allow_duplicate_emails: bool = False) -> Dict:
    """allow_duplicate_emails: when True, a row whose email already
    exists as a lead in this campaign is still imported as its own new
    row, rather than skipped — for a real, recurring case: contacting
    the same creator again for a genuinely different video, tracked as
    its own Asana task. Sending itself stays completely unaffected —
    outreach.py's own eligibility logic only ever considers the FIRST
    row for a given email eligible to actually be emailed, regardless
    of this flag."""
    return {"leads": mapped_rows, "allow_duplicate_emails": allow_duplicate_emails}


def import_payload_path(campaign_name: str, timestamp: Optional[str] = None) -> str:
    ts = timestamp or datetime.now().strftime("%Y-%m-%d-%H%M%S")
    return f"imports/{campaign_name}/{ts}.json"


def build_removal_payload(lead_ids: List[str]) -> Dict:
    return {"lead_ids": [str(lid) for lid in lead_ids]}


def removal_payload_path(campaign_name: str, timestamp: Optional[str] = None) -> str:
    ts = timestamp or datetime.now().strftime("%Y-%m-%d-%H%M%S")
    return f"removals/{campaign_name}/{ts}.json"


def payload_to_bytes(payload: Dict) -> bytes:
    return json.dumps(payload, indent=2).encode("utf-8")


# ---------- Lead table filtering ----------

FILTER_ALL = "All"
FILTER_PENDING_APPROVAL = "Pending Approval"
FILTER_IN_PROGRESS = "In Progress"
FILTER_REPLIED = "Replied"
FILTER_BOUNCED = "Bounced"
FILTER_REMOVED = "Removed"

FILTER_OPTIONS = [FILTER_ALL, FILTER_PENDING_APPROVAL, FILTER_IN_PROGRESS, FILTER_REPLIED,
                   FILTER_BOUNCED, FILTER_REMOVED]


def filter_leads(leads: List[Dict], status_filter: str) -> List[Dict]:
    if status_filter == FILTER_ALL:
        return leads
    if status_filter == FILTER_PENDING_APPROVAL:
        return [l for l in leads if (l.get("Approval") or "") not in ("Yes",)]
    if status_filter == FILTER_REMOVED:
        return [l for l in leads if l.get("Status") == "Removed"]
    if status_filter == FILTER_REPLIED:
        return [l for l in leads if l.get("Status") == "Stopped - Replied"]
    if status_filter == FILTER_BOUNCED:
        return [l for l in leads if l.get("Status") == "Stopped - Bounced"]
    if status_filter == FILTER_IN_PROGRESS:
        return [l for l in leads
                if (l.get("IntroSentAt") or "").strip() and not (l.get("Status") or "").startswith("Stopped")
                and l.get("Status") != "Removed"]
    return leads


def search_leads(leads: List[Dict], query: str) -> List[Dict]:
    if not query or not query.strip():
        return leads
    q = query.strip().lower()
    return [
        l for l in leads
        if q in (l.get("FirstName") or "").lower()
        or q in (l.get("LastName") or "").lower()
        or q in (l.get("Email") or "").lower()
        or q in (l.get("Company") or "").lower()
    ]


def build_full_lead_table(leads: List[Dict], header_order: Optional[List[str]] = None) -> Dict[str, List]:
    """Builds a {column_name: [values]} dict covering EVERY field
    present across the leads, not a fixed subset — so custom columns
    from a CSV import (Client, Product, Content Score, etc.) show up in
    the table automatically, the same way they already do in the Sheet
    itself. '_row' (internal bookkeeping — the Sheet row number, never
    meant for display) is always excluded.

    Column order: header_order (the Sheet's own actual column order,
    fetched separately) determines the order for any column it names;
    anything present in the leads but not in header_order (shouldn't
    normally happen, but a defensive fallback in case a lead somehow
    has a field the current header doesn't) is appended afterward,
    alphabetically, so nothing is ever silently dropped from view."""
    all_keys = set()
    for lead in leads:
        all_keys.update(lead.keys())
    all_keys.discard("_row")

    if header_order:
        ordered_keys = [k for k in header_order if k in all_keys]
        remaining = sorted(all_keys - set(header_order))
        ordered_keys += remaining
    else:
        ordered_keys = sorted(all_keys)

    return {key: [lead.get(key, "") for lead in leads] for key in ordered_keys}
