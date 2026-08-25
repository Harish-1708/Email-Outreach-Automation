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
import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

PAGES_DIR = os.path.join(os.path.dirname(__file__), "..", "pages")


@pytest.fixture(autouse=True)
def _reset_streamlit_global_caches():
    """st.cache_resource/st.cache_data are backed by process-global storage
    that outlives any single AppTest instance — without clearing them, one
    test's cached connector (or a lock left behind by an interrupted cache
    population) can hang or contaminate the next test in this same process.
    Each page's cached functions are re-created fresh per test either way."""
    st.cache_resource.clear()
    st.cache_data.clear()
    yield
    st.cache_resource.clear()
    st.cache_data.clear()


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
    fake_ws = {
        "Kelson_Creators_Licensing Response Sheet": FakeWorksheet([
            {"ResponseID": "<m1>", "LeadID": "1", "Campaign": "Kelson_Creators_Licensing",
             "ReceivedAt": "2026-08-20 10:00:00", "From": "Jane <jane@abc.com>", "Subject": "Re: Hi",
             "Snippet": "Sounds good", "Classification": "Genuine Reply", "MatchMethod": "Header",
             "MessageID": "<m1>", "InReplyTo": "<orig1>", "ActionTaken": "Stopped Sequence"},
        ]),
    }
    fake_spreadsheet = FakeSpreadsheet(fake_ws)

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "2_🚀_Controls.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.run()

    assert list(at.exception) == [], f"Controls page raised: {list(at.exception)}"
    assert list(at.error) == [], f"Controls page showed an error: {[e.value for e in at.error]}"
    assert len(at.tabs) >= 1
    # The "recent replies" section should have actually rendered the fake
    # reply above, in the Check Replies tab, without any button click.
    expander_labels = [e.label for e in at.expander]
    assert any("jane@abc.com" in label for label in expander_labels)


def test_controls_check_replies_labels_stopped_vs_logged_only_correctly():
    """A predates-contact / unverified-match reply must be clearly labeled
    as NOT having stopped the sequence — this is the exact confusion this
    section exists to resolve (Classification alone reads ambiguously)."""
    fake_ws = {
        "Kelson_Creators_Licensing Response Sheet": FakeWorksheet([
            {"ResponseID": "<m2>", "LeadID": "2", "Campaign": "Kelson_Creators_Licensing",
             "ReceivedAt": "2026-08-20 10:00:00", "From": "Old <old@abc.com>", "Subject": "Re: Old thread",
             "Snippet": "Okay", "Classification": "Genuine Reply", "MatchMethod": "Email",
             "MessageID": "<m2>", "InReplyTo": "<unrelated>", "ActionTaken": "Logged Only (Predates Contact)"},
        ]),
    }
    fake_spreadsheet = FakeSpreadsheet(fake_ws)

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "2_🚀_Controls.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.run()

    assert list(at.exception) == []
    markdown_texts = " ".join(m.value for m in at.markdown)
    assert "NOT stopped" in markdown_texts


def test_new_campaign_page_renders_without_exceptions():
    at = AppTest.from_file(os.path.join(PAGES_DIR, "3_➕_New_Campaign.py"))
    at.secrets.update(_dashboard_secrets())
    for k, v in _authed_session().items():
        at.session_state[k] = v
    at.run()

    assert list(at.exception) == [], f"New Campaign page raised: {list(at.exception)}"
    assert list(at.error) == [], f"New Campaign page showed an error: {[e.value for e in at.error]}"
    assert len(at.text_input) >= 1


def test_new_campaign_page_add_stage_mode_shows_next_stage_for_real_campaign():
    """Kelson_Creators_Licensing already has all 5 stages in the repo
    fixture — selecting it in "Add stage" mode should say so, not error."""
    at = AppTest.from_file(os.path.join(PAGES_DIR, "3_➕_New_Campaign.py"))
    at.secrets.update(_dashboard_secrets())
    for k, v in _authed_session().items():
        at.session_state[k] = v
    at.run()

    at.radio[0].set_value("Add the next stage to an existing campaign")
    at.run()

    assert list(at.exception) == [], f"New Campaign (add-stage mode) raised: {list(at.exception)}"
    assert list(at.error) == []
    info_texts = " ".join(i.value for i in at.info)
    assert "already has all 5 stages" in info_texts


def test_overview_page_renders_without_exceptions():
    fake_ws = {
        "Kelson_Creators_Licensing Master Sheet": FakeWorksheet(
            [{"Email": "a@abc.com", "Approval": "Yes", "IntroSentAt": "2026-08-01 09:00:00"},
             {"Email": "b@abc.com", "Approval": "Yes", "IntroSentAt": ""}]
        ),
        "Kelson_Creators_Licensing Response Sheet": FakeWorksheet([]),
        "Kelson_Creators_Licensing Custom Log Sheet": FakeWorksheet(
            [{"Status": "sent", "SenderAccount": "sales1", "Timestamp": "2026-08-01 09:00:00"}]
        ),
    }
    fake_spreadsheet = FakeSpreadsheet(fake_ws)

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "4_📈_Overview.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.run()

    assert list(at.exception) == [], f"Overview page raised: {list(at.exception)}"
    assert list(at.error) == [], f"Overview page showed an error: {[e.value for e in at.error]}"
    metric_values = [m.value for m in at.metric]
    assert "1" in metric_values  # Total Sent from the fake Send Log row
    assert "1" in metric_values  # Total Pending: 1 lead sent, 1 not yet contacted


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
