import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import gspread
import pytest
from sheets_readonly import ReadOnlySheetsConnector, ReadOnlySheetsError


class FakeWorksheet:
    def __init__(self, records):
        self._records = records

    def get_all_records(self):
        return [dict(r) for r in self._records]


class FakeSpreadsheet:
    def __init__(self, worksheets):
        self._worksheets = worksheets  # {title: FakeWorksheet}

    def worksheet(self, title):
        if title not in self._worksheets:
            raise gspread.exceptions.WorksheetNotFound(title)
        return self._worksheets[title]


def test_get_all_leads_adds_row_numbers_starting_at_2():
    ws = FakeWorksheet([{"Email": "a@abc.com"}, {"Email": "b@abc.com"}])
    connector = ReadOnlySheetsConnector(_spreadsheet=FakeSpreadsheet({"Master": ws}))

    leads = connector.get_all_leads("Master")
    assert leads[0]["_row"] == 2
    assert leads[1]["_row"] == 3
    assert leads[0]["Email"] == "a@abc.com"


def test_get_all_responses_passthrough():
    ws = FakeWorksheet([{"MessageID": "<m1>"}])
    connector = ReadOnlySheetsConnector(_spreadsheet=FakeSpreadsheet({"Responses": ws}))
    assert connector.get_all_responses("Responses") == [{"MessageID": "<m1>"}]


def test_get_all_send_log_passthrough():
    ws = FakeWorksheet([{"Status": "sent"}])
    connector = ReadOnlySheetsConnector(_spreadsheet=FakeSpreadsheet({"SendLog": ws}))
    assert connector.get_all_send_log("SendLog") == [{"Status": "sent"}]


def test_get_all_error_log_passthrough():
    ws = FakeWorksheet([{"ErrorType": "Send Failure"}])
    connector = ReadOnlySheetsConnector(_spreadsheet=FakeSpreadsheet({"ErrorLog": ws}))
    assert connector.get_all_error_log("ErrorLog") == [{"ErrorType": "Send Failure"}]


def test_missing_tab_raises_readonly_sheets_error_not_gspread_exception():
    connector = ReadOnlySheetsConnector(_spreadsheet=FakeSpreadsheet({}))
    with pytest.raises(ReadOnlySheetsError, match="doesn't exist yet"):
        connector.get_all_leads("Nonexistent Tab")


def test_connector_requires_service_account_info_or_spreadsheet():
    with pytest.raises(ReadOnlySheetsError):
        ReadOnlySheetsConnector()


def test_connector_has_no_write_methods():
    # Explicit guard against accidental future write-method additions —
    # this connector must remain read-only by construction.
    write_like = {"update_lead_fields", "append_response", "append_send_log",
                  "append_error_log", "clear", "update", "batch_update"}
    connector_methods = {m for m in dir(ReadOnlySheetsConnector) if not m.startswith("_")}
    assert connector_methods.isdisjoint(write_like)
