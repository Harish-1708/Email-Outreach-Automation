"""These tests actually RUN the page scripts (via Streamlit's own
AppTest harness), not just their helper modules — catching import errors,
undefined names, and wrong Streamlit API usage that unit tests of
send_logic/campaign_builder/etc. can't see, since those never execute the
page files themselves. External calls (Google, GitHub) are mocked at the
boundary; nothing here touches a real network.
"""
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import gspread
from streamlit.testing.v1 import AppTest

PAGES_DIR = os.path.join(os.path.dirname(__file__), "..", "pages")


class FakeWorksheet:
    def __init__(self, records):
        self._records = records

    def get_all_records(self):
        return [dict(r) for r in self._records]


class FakeSpreadsheet:
    def __init__(self, worksheets):
        self._worksheets = worksheets

    def worksheet(self, title):
        if title not in self._worksheets:
            raise gspread.exceptions.WorksheetNotFound(title)
        return self._worksheets[title]


def _dashboard_secrets():
    return {
        "shared_sheet_id": "fake-sheet-id",
        "google_sheets_readonly": {"service_account_json": {"type": "service_account"}},
        "github": {"token": "tok", "owner": "acme", "repo": "outreach"},
        "auth_users": {},
    }


def _authed_session():
    return {"auth_user": "alice"}


def test_dashboard_page_renders_without_exceptions():
    fake_ws = {
        "Kelson_Creators_Licensing Master Sheet": FakeWorksheet(
            [{"Email": "a@abc.com", "Approval": "Yes", "IntroSentAt": "2026-08-01 09:00:00"}]
        ),
        "Kelson_Creators_Licensing Response Sheet": FakeWorksheet([]),
        "Kelson_Creators_Licensing Custom Log Sheet": FakeWorksheet(
            [{"Status": "sent", "SenderAccount": "sales1", "Timestamp": "2026-08-01 09:00:00"}]
        ),
        "Kelson_Creators_Licensing Error Log": FakeWorksheet([]),
    }
    fake_spreadsheet = FakeSpreadsheet(fake_ws)

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "1_📊_Dashboard.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.run()

    assert list(at.exception) == [], f"Dashboard page raised: {list(at.exception)}"
    assert list(at.error) == [], f"Dashboard page showed an error: {[e.value for e in at.error]}"
    assert list(at.warning) == [], f"Dashboard page showed a warning: {[w.value for w in at.warning]}"
    assert len(at.selectbox) >= 1  # campaign selector rendered
    # Real assertion on computed content, not just "nothing crashed" — this
    # is what actually catches a wrong-key/wrong-tab-name bug, since a
    # broad except-Exception in the page would otherwise mask it as a
    # graceful st.error with no uncaught exception to catch.
    metric_values = [m.value for m in at.metric]
    assert "1" in metric_values  # Total Leads == 1 from the fake Master Sheet row above


def test_controls_page_renders_without_exceptions():
    with patch("gspread.authorize"), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "2_🚀_Controls.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.run()

    assert list(at.exception) == [], f"Controls page raised: {list(at.exception)}"
    assert list(at.error) == [], f"Controls page showed an error: {[e.value for e in at.error]}"
    assert len(at.tabs) >= 1


def test_new_campaign_page_renders_without_exceptions():
    at = AppTest.from_file(os.path.join(PAGES_DIR, "3_➕_New_Campaign.py"))
    at.secrets.update(_dashboard_secrets())
    for k, v in _authed_session().items():
        at.session_state[k] = v
    at.run()

    assert list(at.exception) == [], f"New Campaign page raised: {list(at.exception)}"
    assert list(at.error) == [], f"New Campaign page showed an error: {[e.value for e in at.error]}"
    assert len(at.text_input) >= 1


def test_login_lockout_after_repeated_failures():
    from auth import hash_password

    salt = "testsalt"
    at = AppTest.from_file(os.path.join(os.path.dirname(PAGES_DIR), "app.py"))
    at.secrets["auth_users"] = {"alice": {"salt": salt, "password_hash": hash_password("testpass", salt)}}
    at.run()

    for _ in range(5):
        at.text_input[0].set_value("alice")
        at.text_input[1].set_value("wrong")
        at.button[0].click()
        at.run()

    assert "Locked for 60s" in at.error[0].value
    assert at.session_state["auth_locked_until"] > 0

    # Even the CORRECT password must be rejected while locked out.
    at.text_input[0].set_value("alice")
    at.text_input[1].set_value("testpass")
    at.button[0].click()
    at.run()
    assert at.session_state["auth_user"] is None
    assert "Try again in" in at.error[0].value


def test_pages_require_login_when_not_authenticated():
    """Every page must call login_gate() and stop — verified here by NOT
    setting auth_user and confirming the page doesn't render its main
    content (Dashboard title never appears)."""
    at = AppTest.from_file(os.path.join(PAGES_DIR, "1_📊_Dashboard.py"))
    at.secrets.update(_dashboard_secrets())
    at.run()

    assert list(at.exception) == []
    titles = [t.value for t in at.title]
    assert "📊 Dashboard" not in titles  # blocked by login gate before reaching st.title
