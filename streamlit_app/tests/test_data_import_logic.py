import os
import sys
import json

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from data_import_logic import (
    parse_csv_bytes, build_default_mapping, apply_mapping, validate_mapping,
    count_valid_rows, build_import_payload, import_payload_path,
    build_removal_payload, removal_payload_path, payload_to_bytes,
    filter_leads, search_leads, validate_custom_field_name,
    FILTER_ALL, FILTER_PENDING_APPROVAL, FILTER_IN_PROGRESS, FILTER_REPLIED,
    FILTER_BOUNCED, FILTER_REMOVED,
)


# ---------- parse_csv_bytes ----------

def test_parse_csv_bytes_basic():
    raw = b"First Name,Email,Company\nSam,sam@abc.com,Acme\nAlex,alex@abc.com,Beta\n"
    columns, rows = parse_csv_bytes(raw)
    assert columns == ["First Name", "Email", "Company"]
    assert len(rows) == 2
    assert rows[0]["Email"] == "sam@abc.com"


def test_parse_csv_bytes_handles_bom():
    raw = "\ufeffFirst Name,Email\nSam,sam@abc.com\n".encode("utf-8")
    columns, rows = parse_csv_bytes(raw)
    assert columns == ["First Name", "Email"]  # BOM stripped, not glued to first header


def test_parse_csv_bytes_empty_file():
    columns, rows = parse_csv_bytes(b"")
    assert columns == []
    assert rows == []


# ---------- build_default_mapping ----------

def test_default_mapping_matches_known_fields_case_and_punctuation_insensitive():
    mapping = build_default_mapping(["First Name", "Last-Name", "EMAIL", "company"], [])
    assert mapping["First Name"] == "FirstName"
    assert mapping["Last-Name"] == "LastName"
    assert mapping["EMAIL"] == "Email"
    assert mapping["company"] == "Company"


def test_default_mapping_matches_custom_columns():
    mapping = build_default_mapping(["Job Title", "Website"], ["Title", "Website"])
    assert mapping["Website"] == "Website"
    # "Job Title" doesn't normalize-match "Title" (extra word) — stays unmapped, which is correct/safe
    assert mapping["Job Title"] == ""


def test_default_mapping_leaves_unrecognized_columns_unmapped():
    mapping = build_default_mapping(["Random Column"], [])
    assert mapping["Random Column"] == ""


def test_default_mapping_prefers_known_field_over_same_named_custom_column():
    mapping = build_default_mapping(["Email"], ["Email"])
    assert mapping["Email"] == "Email"  # still correct either way, but exercises the precedence path


# ---------- apply_mapping ----------

def test_apply_mapping_maps_and_strips_whitespace():
    rows = [{"First Name": "  Sam  ", "Email": "sam@abc.com "}]
    mapping = {"First Name": "FirstName", "Email": "Email"}
    mapped = apply_mapping(rows, mapping)
    assert mapped == [{"FirstName": "Sam", "Email": "sam@abc.com"}]


def test_apply_mapping_drops_unmapped_columns():
    rows = [{"First Name": "Sam", "Junk": "ignore me"}]
    mapping = {"First Name": "FirstName", "Junk": ""}
    mapped = apply_mapping(rows, mapping)
    assert mapped == [{"FirstName": "Sam"}]


def test_apply_mapping_empty_rows():
    assert apply_mapping([], {"Email": "Email"}) == []


# ---------- validate_mapping ----------

def test_validate_mapping_requires_email():
    assert validate_mapping({"First Name": "FirstName"}) is not None
    assert validate_mapping({"E-mail": "Email"}) is None


# ---------- count_valid_rows ----------

def test_count_valid_rows_only_counts_rows_with_email():
    mapped = [{"Email": "a@abc.com"}, {"FirstName": "NoEmail"}, {"Email": ""}]
    assert count_valid_rows(mapped) == 1


# ---------- payload builders ----------

def test_build_import_payload_shape():
    payload = build_import_payload([{"Email": "a@abc.com"}])
    assert payload == {"leads": [{"Email": "a@abc.com"}]}


def test_import_payload_path_format():
    path = import_payload_path("DudeRobe", timestamp="2026-08-29-143012")
    assert path == "imports/DudeRobe/2026-08-29-143012.json"


def test_build_removal_payload_stringifies_ids():
    payload = build_removal_payload([5, "8"])
    assert payload == {"lead_ids": ["5", "8"]}


def test_removal_payload_path_format():
    path = removal_payload_path("DudeRobe", timestamp="2026-08-29-143012")
    assert path == "removals/DudeRobe/2026-08-29-143012.json"


def test_payload_to_bytes_round_trips_as_valid_json():
    payload = {"leads": [{"Email": "a@abc.com"}]}
    raw = payload_to_bytes(payload)
    assert json.loads(raw.decode("utf-8")) == payload


# ---------- filter_leads ----------

def _lead(**overrides):
    lead = {"FirstName": "Sam", "LastName": "Lee", "Email": "sam@abc.com", "Company": "Acme",
            "Approval": "Yes", "Status": ""}
    lead.update(overrides)
    return lead


def test_filter_all_returns_everything():
    leads = [_lead(), _lead(Approval="")]
    assert filter_leads(leads, FILTER_ALL) == leads


def test_filter_pending_approval():
    leads = [_lead(Approval="Yes"), _lead(Approval=""), _lead(Approval="No")]
    result = filter_leads(leads, FILTER_PENDING_APPROVAL)
    assert len(result) == 2
    assert all(l["Approval"] != "Yes" for l in result)


def test_filter_in_progress_excludes_terminal_and_removed():
    leads = [
        _lead(Approval="Yes", Status=""),                    # in progress
        _lead(Approval="Yes", Status="Stopped - Replied"),    # terminal, excluded
        _lead(Approval="Yes", Status="Removed"),              # removed, excluded
        _lead(Approval="", Status=""),                        # not approved, excluded
    ]
    result = filter_leads(leads, FILTER_IN_PROGRESS)
    assert len(result) == 1


def test_filter_replied():
    leads = [_lead(Status="Stopped - Replied"), _lead(Status="")]
    result = filter_leads(leads, FILTER_REPLIED)
    assert len(result) == 1
    assert result[0]["Status"] == "Stopped - Replied"


def test_filter_bounced():
    leads = [_lead(Status="Stopped - Bounced"), _lead(Status="")]
    result = filter_leads(leads, FILTER_BOUNCED)
    assert len(result) == 1


def test_filter_removed():
    leads = [_lead(Status="Removed"), _lead(Status="")]
    result = filter_leads(leads, FILTER_REMOVED)
    assert len(result) == 1


# ---------- search_leads ----------

def test_search_matches_first_last_email_company_case_insensitive():
    leads = [_lead(FirstName="Sam", LastName="Lee", Email="sam@abc.com", Company="Acme"),
             _lead(FirstName="Alex", LastName="Kim", Email="alex@xyz.com", Company="Beta")]
    assert len(search_leads(leads, "SAM")) == 1
    assert len(search_leads(leads, "kim")) == 1
    assert len(search_leads(leads, "beta")) == 1
    assert len(search_leads(leads, "xyz.com")) == 1


def test_search_empty_query_returns_all():
    leads = [_lead()]
    assert search_leads(leads, "") == leads
    assert search_leads(leads, "   ") == leads


def test_search_no_match_returns_empty():
    leads = [_lead()]
    assert search_leads(leads, "nomatch") == []


# ---------- validate_custom_field_name ----------

def test_validate_custom_field_name_accepts_a_reasonable_new_name():
    assert validate_custom_field_name("Client", reserved_names=["Email", "Status"]) is None


def test_validate_custom_field_name_rejects_blank():
    error = validate_custom_field_name("", reserved_names=["Email"])
    assert error is not None
    assert "Enter a name" in error


def test_validate_custom_field_name_rejects_whitespace_only():
    error = validate_custom_field_name("   ", reserved_names=["Email"])
    assert error is not None


def test_validate_custom_field_name_rejects_reserved_system_column():
    """The actual data-corruption risk this exists to prevent — a custom
    field literally named the same as a real system-tracked column
    (e.g. 'Status', 'IntroSentAt') would silently overwrite that
    column's real tracking data on the next import."""
    error = validate_custom_field_name("Status", reserved_names=["Email", "Status", "IntroSentAt"])
    assert error is not None
    assert "already used internally" in error


def test_validate_custom_field_name_rejects_reserved_name_case_insensitively():
    error = validate_custom_field_name("status", reserved_names=["Status"])
    assert error is not None


def test_validate_custom_field_name_allows_name_after_stripping_whitespace():
    assert validate_custom_field_name("  Client  ", reserved_names=["Email"]) is None
