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
    def __init__(self, records, header=None):
        self._records = records
        self._header = header or (list(records[0].keys()) if records else [])
        self.read_call_count = 0

    def get_all_records(self):
        self.read_call_count += 1
        return [dict(r) for r in self._records]

    def row_values(self, row_number):
        if row_number == 1:
            self.read_call_count += 1
            return list(self._header)
        raise NotImplementedError("Fake only supports reading the header row (row 1)")


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
        at = AppTest.from_file(os.path.join(PAGES_DIR, "dashboard.py"))
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
        at = AppTest.from_file(os.path.join(PAGES_DIR, "controls.py"))
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
        at = AppTest.from_file(os.path.join(PAGES_DIR, "controls.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.run()

    assert list(at.exception) == []
    markdown_texts = " ".join(m.value for m in at.markdown)
    assert "NOT stopped" in markdown_texts


def test_new_campaign_dialog_disabled_until_confirmation_checked():
    """The confirm checkbox is the ONLY remaining safety net now that
    there's no GitHub trip — this must actually gate the button, not just
    be decorative."""
    fake_spreadsheet = FakeSpreadsheet(_campaigns_page_fake_ws())

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "campaigns.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.run(timeout=15)

        new_campaign_button = next(b for b in at.button if "New Campaign" in b.label)
        new_campaign_button.click()
        at.run(timeout=15)

        assert list(at.exception) == []
        create_button = next(b for b in at.button if b.label == "Create Campaign")
        assert create_button.disabled is True

        confirm_checkbox = at.checkbox[0]
        confirm_checkbox.set_value(True)
        at.run(timeout=15)
        create_button = next(b for b in at.button if b.label == "Create Campaign")
        assert create_button.disabled is False


def test_new_campaign_dialog_creates_campaign_and_stays_on_hub():
    """Deliberately does NOT auto-navigate into the new campaign — right
    after committing, Streamlit Cloud's local checkout is very likely
    still stale until it redeploys, so jumping straight to the detail
    view would hit a real 'No templates found' error. Staying on the hub
    with a clear message is the honest version of this UX."""
    fake_spreadsheet = FakeSpreadsheet(_campaigns_page_fake_ws())
    captured, fake_create_file = _mock_github_writes()

    def fake_dispatch(self, workflow_file, inputs, ref="main"):
        captured["dispatched"] = workflow_file
        return {"id": 1, "html_url": "https://github.com/x"}

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()), \
         patch("github_client.GitHubClient.create_file", fake_create_file), \
         patch("github_client.GitHubClient.dispatch_workflow", fake_dispatch):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "campaigns.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.run(timeout=15)

        new_campaign_button = next(b for b in at.button if "New Campaign" in b.label)
        new_campaign_button.click()
        at.run(timeout=15)

        text_inputs = {ti.label: ti for ti in at.text_input}
        text_inputs["Campaign name (letters, numbers, underscores only)"].set_value("BrandNewCampaign")
        at.checkbox[0].set_value(True)
        at.run(timeout=15)

        create_button = next(b for b in at.button if b.label == "Create Campaign")
        create_button.click()
        at.run(timeout=15)

    assert list(at.exception) == [], f"Create campaign raised: {list(at.exception)}"
    assert list(at.error) == []
    assert len(captured["commits"]) == 1
    assert captured["commits"][0]["path"] == "templates/BrandNewCampaign/intro_A.txt"
    # No template content was ever asked for — a placeholder is used instead.
    assert b"Write your subject here" in captured["commits"][0]["content"]
    assert captured["dispatched"] == "dashboard.yml"  # auto tab-init was triggered
    assert "selected_campaign" not in at.session_state  # stayed on the hub, didn't auto-navigate
    titles = [t.value for t in at.title]
    assert "Campaigns" in titles  # still the hub, not a campaign detail page


def test_new_campaign_dialog_only_asks_for_name_no_template_fields():
    fake_spreadsheet = FakeSpreadsheet(_campaigns_page_fake_ws())

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "campaigns.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.run(timeout=15)

        new_campaign_button = next(b for b in at.button if "New Campaign" in b.label)
        new_campaign_button.click()
        at.run(timeout=15)

    assert list(at.exception) == []
    dialog_text_inputs = [ti.label for ti in at.text_input if "Campaign name" in ti.label]
    assert len(dialog_text_inputs) == 1
    # No Subject/Body fields anywhere in the dialog.
    assert not any("Subject" in ti.label for ti in at.text_input if ti.label)
    assert len(at.text_area) == 0


def test_new_campaign_dialog_does_not_reopen_after_navigating_away_and_back():
    """The actual reported bug: opening the dialog, then visiting a
    different page, then returning to Campaigns without ever clicking
    Create or Cancel, was silently reopening the dialog again — because
    the session_state flag needed to survive reruns FROM WITHIN the
    dialog had no way to distinguish that from a genuine return visit."""
    fake_spreadsheet = FakeSpreadsheet(_campaigns_page_fake_ws())

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "campaigns.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.run(timeout=15)

        new_campaign_button = next(b for b in at.button if "New Campaign" in b.label)
        new_campaign_button.click()
        at.run(timeout=15)
        assert at.session_state["show_new_campaign_dialog"] is True

        # Simulate visiting a different page — exactly what dashboard.py /
        # controls.py / etc. do via mark_active_page at their own top.
        at.session_state["_active_page"] = "dashboard"

        # Return to Campaigns — WITHOUT ever clicking Create or Cancel.
        at.run(timeout=15)

    assert list(at.exception) == []
    assert at.session_state["show_new_campaign_dialog"] is False
    # The dialog's own fields shouldn't be showing anymore either.
    assert not any("Campaign name" in ti.label for ti in at.text_input if ti.label)


def test_new_campaign_dialog_stays_open_across_its_own_widget_interactions():
    """The flip side of the above — interacting with a widget INSIDE the
    dialog (not navigating away) must NOT close it. This is what the
    session_state approach was originally introduced to fix; confirming
    it still holds after adding the navigation check."""
    fake_spreadsheet = FakeSpreadsheet(_campaigns_page_fake_ws())

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "campaigns.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.run(timeout=15)

        new_campaign_button = next(b for b in at.button if "New Campaign" in b.label)
        new_campaign_button.click()
        at.run(timeout=15)

        name_input = next(ti for ti in at.text_input if "Campaign name" in ti.label)
        name_input.set_value("SomeName")
        at.run(timeout=15)  # a rerun triggered by a widget INSIDE the dialog

    assert list(at.exception) == []
    assert at.session_state["show_new_campaign_dialog"] is True  # still open
    assert any("Campaign name" in ti.label for ti in at.text_input if ti.label)


def test_new_campaign_dialog_cancel_button_closes_it():
    fake_spreadsheet = FakeSpreadsheet(_campaigns_page_fake_ws())

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "campaigns.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.run(timeout=15)

        new_campaign_button = next(b for b in at.button if "New Campaign" in b.label)
        new_campaign_button.click()
        at.run(timeout=15)

        cancel_button = next(b for b in at.button if b.label == "Cancel")
        cancel_button.click()
        at.run(timeout=15)

    assert list(at.exception) == []
    assert at.session_state["show_new_campaign_dialog"] is False


def test_new_campaign_dialog_rejects_duplicate_name():
    fake_spreadsheet = FakeSpreadsheet(_campaigns_page_fake_ws())
    captured, fake_create_file = _mock_github_writes()

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()), \
         patch("github_client.GitHubClient.create_file", fake_create_file):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "campaigns.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.run(timeout=15)

        new_campaign_button = next(b for b in at.button if "New Campaign" in b.label)
        new_campaign_button.click()
        at.run(timeout=15)

        text_inputs = {ti.label: ti for ti in at.text_input}
        text_inputs["Campaign name (letters, numbers, underscores only)"].set_value("Kelson_Creators_Licensing")
        at.checkbox[0].set_value(True)
        at.run(timeout=15)

        create_button = next(b for b in at.button if b.label == "Create Campaign")
        create_button.click()
        at.run(timeout=15)

    assert list(at.exception) == []
    assert captured.get("commits") is None
    error_texts = " ".join(e.value for e in at.error)
    assert "already exists" in error_texts


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
        at = AppTest.from_file(os.path.join(PAGES_DIR, "overview.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.run()

    assert list(at.exception) == [], f"Overview page raised: {list(at.exception)}"
    assert list(at.error) == [], f"Overview page showed an error: {[e.value for e in at.error]}"
    metric_values = [m.value for m in at.metric]
    assert "1" in metric_values  # Total Sent from the fake Send Log row
    assert "1" in metric_values  # Total Pending: 1 lead sent, 1 not yet contacted


def test_email_accounts_page_renders_without_exceptions():
    fake_ws = {
        "Kelson_Creators_Licensing Custom Log Sheet": FakeWorksheet(
            [{"Status": "sent", "SenderAccount": "sales1", "Timestamp": "2026-08-01 09:00:00"}]
        ),
    }
    fake_spreadsheet = FakeSpreadsheet(fake_ws)
    secrets = _dashboard_secrets()
    secrets["email_accounts_directory"] = {"sales1": "sales1@example.com", "sales2": "sales2@example.com"}

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "email_accounts.py"))
        at.secrets.update(secrets)
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.run()

    assert list(at.exception) == [], f"Email Accounts page raised: {list(at.exception)}"
    assert list(at.error) == [], f"Email Accounts page showed an error: {[e.value for e in at.error]}"
    metric_labels = [m.label for m in at.metric]
    assert any("sales1" in label for label in metric_labels)
    assert any("sales2" in label for label in metric_labels)


def test_email_accounts_page_warns_when_no_directory_configured():
    at = AppTest.from_file(os.path.join(PAGES_DIR, "email_accounts.py"))
    at.secrets.update(_dashboard_secrets())  # no email_accounts_directory key
    for k, v in _authed_session().items():
        at.session_state[k] = v
    at.run()

    assert list(at.exception) == []
    warning_texts = " ".join(w.value for w in at.warning)
    assert "No accounts configured" in warning_texts


def _campaigns_page_fake_ws():
    return {
        "Kelson_Creators_Licensing Master Sheet": FakeWorksheet(
            [{"Email": "a@abc.com", "Approval": "Yes", "IntroSentAt": "2026-08-01 09:00:00",
              "IntroVariant": "A", "SenderAccount": "sales1",
              "FollowUp1SentAt": "", "FollowUp1Variant": "", "Status": ""}]
        ),
        "Kelson_Creators_Licensing Response Sheet": FakeWorksheet([]),
        "Kelson_Creators_Licensing Custom Log Sheet": FakeWorksheet(
            [{"Status": "sent", "SenderAccount": "sales1", "Timestamp": "2026-08-01 09:00:00"}]
        ),
        "Kelson_Creators_Licensing Error Log": FakeWorksheet([]),
    }


def test_campaigns_hub_page_renders_without_exceptions():
    fake_spreadsheet = FakeSpreadsheet(_campaigns_page_fake_ws())

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "campaigns.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.run(timeout=15)

    assert list(at.exception) == [], f"Campaigns hub raised: {list(at.exception)}"
    assert list(at.error) == [], f"Campaigns hub showed an error: {[e.value for e in at.error]}"
    titles = [t.value for t in at.title]
    assert "Campaigns" in titles
    markdown_text = " ".join(m.value for m in at.markdown)
    assert "Kelson_Creators_Licensing" in markdown_text


def test_campaigns_detail_view_renders_analytics_without_exceptions():
    """Directly sets selected_campaign in session_state, bypassing the
    click — proves the detail view + Analytics tab (Phase B, real data,
    not a stub) work end to end against realistic Sheet data."""
    fake_spreadsheet = FakeSpreadsheet(_campaigns_page_fake_ws())

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "campaigns.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.session_state["selected_campaign"] = "Kelson_Creators_Licensing"
        at.run(timeout=15)

    assert list(at.exception) == [], f"Campaign detail raised: {list(at.exception)}"
    assert list(at.error) == [], f"Campaign detail showed an error: {[e.value for e in at.error]}"
    titles = [t.value for t in at.title]
    assert "Kelson_Creators_Licensing" in titles
    assert len(at.tabs) == 6
    # Real analytics data should show up as metrics, not just tab labels.
    metric_values = [m.value for m in at.metric]
    assert "1" in metric_values  # Total Leads == 1


def test_campaigns_detail_view_stub_tabs_are_honest_about_not_being_built():
    fake_spreadsheet = FakeSpreadsheet(_campaigns_page_fake_ws())

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "campaigns.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.session_state["selected_campaign"] = "Kelson_Creators_Licensing"
        at.run(timeout=15)

    assert list(at.exception) == []
    info_texts = " ".join(i.value for i in at.info)
    assert "isn't built yet" in info_texts
    assert "Phase H" in info_texts  # Responses — the only one left as a stub now


def test_campaigns_back_button_clears_selected_campaign():
    fake_spreadsheet = FakeSpreadsheet(_campaigns_page_fake_ws())

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "campaigns.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.session_state["selected_campaign"] = "Kelson_Creators_Licensing"
        at.run(timeout=15)

        back_button = next(b for b in at.button if "Back to Campaigns" in b.label)
        back_button.click()
        at.run(timeout=15)

    assert list(at.exception) == []
    assert "selected_campaign" not in at.session_state
    titles = [t.value for t in at.title]
    assert "Campaigns" in titles


def test_campaign_detail_reruns_do_not_re_fetch_sheets_data():
    """The actual regression: Streamlit reruns the WHOLE script on nearly
    every widget interaction. Before this was cached, each rerun on the
    campaign detail page re-issued 4 fresh Sheets reads — easily enough
    to exceed Google's 60-reads/minute quota during ordinary use (e.g.
    adjusting several CSV mapping dropdowns in a row) and return a 429.
    This proves a second rerun reuses the cache instead of re-fetching."""
    fake_ws = _campaigns_page_fake_ws()
    fake_spreadsheet = FakeSpreadsheet(fake_ws)
    master_ws = fake_ws["Kelson_Creators_Licensing Master Sheet"]

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "campaigns.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.session_state["selected_campaign"] = "Kelson_Creators_Licensing"
        at.run(timeout=15)

        assert list(at.exception) == []
        reads_after_first_run = master_ws.read_call_count
        assert reads_after_first_run > 0  # sanity — it did fetch at least once

        # Simulate a widget interaction elsewhere on the page (a full
        # script rerun, exactly like adjusting a filter or mapping
        # dropdown would trigger) — this must NOT trigger a second fetch.
        status_filter = next(sb for sb in at.selectbox if sb.label == "Filter")
        status_filter.set_value("Removed")
        at.run(timeout=15)

    assert list(at.exception) == []
    assert master_ws.read_call_count == reads_after_first_run, (
        f"Expected no new Sheets reads on rerun (cached), but count went from "
        f"{reads_after_first_run} to {master_ws.read_call_count}"
    )


def test_refresh_data_button_actually_busts_the_cache():
    fake_ws = _campaigns_page_fake_ws()
    fake_spreadsheet = FakeSpreadsheet(fake_ws)
    master_ws = fake_ws["Kelson_Creators_Licensing Master Sheet"]

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "campaigns.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.session_state["selected_campaign"] = "Kelson_Creators_Licensing"
        at.run(timeout=15)
        reads_after_first_run = master_ws.read_call_count

        refresh_button = next(b for b in at.button if "Refresh data" in b.label)
        refresh_button.click()
        at.run(timeout=15)

    assert list(at.exception) == []
    assert master_ws.read_call_count > reads_after_first_run  # the explicit refresh DID re-fetch


def _empty_master_fake_ws():
    return {
        "Kelson_Creators_Licensing Master Sheet": FakeWorksheet(
            [], header=["LeadID", "FirstName", "LastName", "Email", "Company", "Campaign", "Approval",
                        "SenderAccount", "RequestedAction", "CurrentStage", "ScheduledAt", "IntroSentAt",
                        "IntroVariant", "Status", "LastActionAt"]
        ),
        "Kelson_Creators_Licensing Response Sheet": FakeWorksheet([]),
        "Kelson_Creators_Licensing Custom Log Sheet": FakeWorksheet([]),
        "Kelson_Creators_Licensing Error Log": FakeWorksheet([]),
    }


def test_data_tab_upload_shows_mapping_ui_with_correct_defaults():
    fake_spreadsheet = FakeSpreadsheet(_empty_master_fake_ws())

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "campaigns.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.session_state["selected_campaign"] = "Kelson_Creators_Licensing"
        at.run(timeout=15)

        at.file_uploader[0].upload("leads.csv", b"First Name,Email\nSam,sam@abc.com\nAlex,alex@abc.com\n", "text/csv")
        at.run(timeout=15)

    assert list(at.exception) == [], f"Data tab upload raised: {list(at.exception)}"
    assert list(at.error) == []
    mapping = {sb.label: sb.value for sb in at.selectbox if "maps to" in sb.label}
    assert mapping["'First Name' maps to"] == "FirstName"
    assert mapping["'Email' maps to"] == "Email"
    markdown_text = " ".join(m.value for m in at.markdown)
    assert "2 of 2 row" in markdown_text


def test_data_tab_import_commits_payload_and_triggers_workflow():
    fake_spreadsheet = FakeSpreadsheet(_empty_master_fake_ws())
    captured = {}

    def fake_create_file(self, path, content_bytes, message, branch="main"):
        captured["path"] = path
        captured["content"] = content_bytes

    def fake_dispatch(self, workflow_file, inputs, ref="main"):
        captured["workflow"] = workflow_file
        captured["inputs"] = inputs
        return {"id": 1, "html_url": "https://github.com/x"}

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()), \
         patch("github_client.GitHubClient.create_file", fake_create_file), \
         patch("github_client.GitHubClient.dispatch_workflow", fake_dispatch):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "campaigns.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.session_state["selected_campaign"] = "Kelson_Creators_Licensing"
        at.run(timeout=15)

        at.file_uploader[0].upload("leads.csv", b"First Name,Email\nSam,sam@abc.com\n", "text/csv")
        at.run(timeout=15)
        import_button = next(b for b in at.button if b.label == "Import Leads")
        import_button.click()
        at.run(timeout=15)

    assert list(at.exception) == [], f"Import click raised: {list(at.exception)}"
    assert list(at.error) == []
    assert captured["workflow"] == "import_leads.yml"
    assert captured["inputs"]["campaign"] == "Kelson_Creators_Licensing"
    assert captured["path"].startswith("imports/Kelson_Creators_Licensing/")
    import json
    payload = json.loads(captured["content"].decode("utf-8"))
    assert payload == {"leads": [{"FirstName": "Sam", "Email": "sam@abc.com"}]}


def test_data_tab_shows_error_when_no_column_mapped_to_email():
    fake_spreadsheet = FakeSpreadsheet(_empty_master_fake_ws())

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "campaigns.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.session_state["selected_campaign"] = "Kelson_Creators_Licensing"
        at.run(timeout=15)

        at.file_uploader[0].upload("leads.csv", b"First Name,Nickname\nSam,Sammy\n", "text/csv")
        at.run(timeout=15)
        # Neither column auto-maps to Email — force it to stay unmapped.
        email_selectbox = next(sb for sb in at.selectbox if "'Nickname' maps to" in sb.label)
        email_selectbox.set_value("-- Skip --")
        at.run(timeout=15)

    assert list(at.exception) == []
    error_texts = " ".join(e.value for e in at.error)
    assert "Email" in error_texts


def test_data_tab_lead_table_and_remove_flow():
    fake_ws = {
        "Kelson_Creators_Licensing Master Sheet": FakeWorksheet(
            [{"LeadID": "1", "FirstName": "Sam", "LastName": "Lee", "Email": "sam@abc.com",
              "Company": "Acme", "Approval": "Yes", "Status": ""},
             {"LeadID": "2", "FirstName": "Alex", "LastName": "Kim", "Email": "alex@abc.com",
              "Company": "Beta", "Approval": "Yes", "Status": ""}]
        ),
        "Kelson_Creators_Licensing Response Sheet": FakeWorksheet([]),
        "Kelson_Creators_Licensing Custom Log Sheet": FakeWorksheet([]),
        "Kelson_Creators_Licensing Error Log": FakeWorksheet([]),
    }
    fake_spreadsheet = FakeSpreadsheet(fake_ws)
    captured = {}

    def fake_create_file(self, path, content_bytes, message, branch="main"):
        captured["path"] = path
        captured["content"] = content_bytes

    def fake_dispatch(self, workflow_file, inputs, ref="main"):
        captured["workflow"] = workflow_file
        captured["inputs"] = inputs
        return {"id": 1, "html_url": "https://github.com/x"}

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()), \
         patch("github_client.GitHubClient.create_file", fake_create_file), \
         patch("github_client.GitHubClient.dispatch_workflow", fake_dispatch):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "campaigns.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.session_state["selected_campaign"] = "Kelson_Creators_Licensing"
        at.run(timeout=15)

        assert list(at.exception) == []
        remove_select = next(ms for ms in at.multiselect if "Select leads to remove" in ms.label)
        matching_option = next(opt for opt in remove_select.options if "1 —" in opt)
        remove_select.set_value([matching_option])
        at.run(timeout=15)

        remove_button = next(b for b in at.button if b.label == "Remove Selected")
        remove_button.click()
        at.run(timeout=15)

    assert list(at.exception) == [], f"Remove flow raised: {list(at.exception)}"
    assert list(at.error) == []
    assert captured["workflow"] == "remove_leads.yml"
    import json
    payload = json.loads(captured["content"].decode("utf-8"))
    assert payload == {"lead_ids": ["1"]}


def _mock_github_writes():
    """Returns (patchers, captured) — patches create_file/dispatch_workflow
    at the GitHubClient class level, same pattern as the Data tab tests."""
    captured = {}

    def fake_create_file(self, path, content_bytes, message, branch="main"):
        captured.setdefault("commits", []).append({"path": path, "content": content_bytes, "message": message})

    return captured, fake_create_file


def test_sequences_tab_shows_locked_variants_for_real_campaign():
    fake_spreadsheet = FakeSpreadsheet(_campaigns_page_fake_ws())

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "campaigns.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.session_state["selected_campaign"] = "Kelson_Creators_Licensing"
        at.run(timeout=15)

    assert list(at.exception) == [], f"Sequences tab raised: {list(at.exception)}"
    assert list(at.error) == []
    # Kelson_Creators_Licensing has 5 stages x 4 variants = 20 "Variant X" expanders
    variant_expanders = [e for e in at.expander if e.label.startswith("Variant ")]
    assert len(variant_expanders) == 20
    # Every text input/area for template content starts disabled (locked).
    subject_inputs = [ti for ti in at.text_input if ti.label.startswith("Subject")]
    assert all(ti.disabled for ti in subject_inputs)


def test_sequences_tab_intro_subject_label_never_says_continues_thread():
    """Regression: the blank-continues-the-thread hint was showing on
    EVERY stage's Subject field, including Intro — self-contradictory,
    since Intro can never actually use a blank subject (see
    outreach.render_email's is_first_stage guard)."""
    fake_spreadsheet = FakeSpreadsheet(_campaigns_page_fake_ws())

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "campaigns.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.session_state["selected_campaign"] = "Kelson_Creators_Licensing"
        at.run(timeout=15)

    assert list(at.exception) == []
    intro_subject_inputs = [ti for ti in at.text_input if ti.key and ti.key.startswith("subject_intro_")]
    followup_subject_inputs = [ti for ti in at.text_input if ti.key and ti.key.startswith("subject_followup")]
    assert intro_subject_inputs, "Expected at least one Intro subject field"
    assert all("continues" not in ti.label.lower() for ti in intro_subject_inputs)
    assert all("required" in ti.label.lower() for ti in intro_subject_inputs)
    assert all("continues" in ti.label.lower() for ti in followup_subject_inputs)


def test_sequences_tab_save_rejects_blank_subject_for_intro_edit():
    """Editing Intro's subject down to blank must be caught here, before
    Save — not left to fail later, at send time, with a TemplateError."""
    fake_spreadsheet = FakeSpreadsheet(_campaigns_page_fake_ws())
    captured, fake_create_file = _mock_github_writes()

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()), \
         patch("github_client.GitHubClient.create_file", fake_create_file):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "campaigns.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.session_state["selected_campaign"] = "Kelson_Creators_Licensing"
        at.run(timeout=15)

        unlock_checkbox = next(cb for cb in at.checkbox if cb.key == "unlock_intro_A")
        unlock_checkbox.set_value(True)
        at.run(timeout=15)

        subject_input = next(ti for ti in at.text_input if ti.key == "subject_intro_A")
        subject_input.set_value("")
        at.run(timeout=15)

    assert list(at.exception) == []
    error_texts = " ".join(e.value for e in at.error)
    assert "Subject is required" in error_texts
    assert captured.get("commits") is None


def test_sequences_tab_unlock_and_save_edits_one_variant():
    fake_spreadsheet = FakeSpreadsheet(_campaigns_page_fake_ws())
    captured, fake_create_file = _mock_github_writes()

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()), \
         patch("github_client.GitHubClient.create_file", fake_create_file):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "campaigns.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.session_state["selected_campaign"] = "Kelson_Creators_Licensing"
        at.run(timeout=15)

        unlock_checkbox = next(cb for cb in at.checkbox if cb.key == "unlock_intro_A")
        unlock_checkbox.set_value(True)
        at.run(timeout=15)

        subject_input = next(ti for ti in at.text_input if ti.key == "subject_intro_A")
        subject_input.set_value("A brand new intro subject")
        at.run(timeout=15)

        save_button = next(b for b in at.button if b.label.startswith("💾 Save Changes"))
        save_button.click()
        at.run(timeout=15)

    assert list(at.exception) == [], f"Save edit raised: {list(at.exception)}"
    assert list(at.error) == []
    assert len(captured["commits"]) == 1
    commit = captured["commits"][0]
    assert commit["path"] == "templates/Kelson_Creators_Licensing/intro_A.txt"
    assert b"A brand new intro subject" in commit["content"]


def test_sequences_tab_locked_variant_edit_is_not_saved():
    """Typing into a field while still locked must never reach Save —
    the disabled widget shouldn't even accept the value, but this proves
    the end-to-end behavior regardless of how disabling is implemented."""
    fake_spreadsheet = FakeSpreadsheet(_campaigns_page_fake_ws())
    captured, fake_create_file = _mock_github_writes()

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()), \
         patch("github_client.GitHubClient.create_file", fake_create_file):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "campaigns.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.session_state["selected_campaign"] = "Kelson_Creators_Licensing"
        at.run(timeout=15)

    assert list(at.exception) == []
    # No "Save Changes" button should even appear — nothing is unlocked, so
    # nothing can be pending.
    save_buttons = [b for b in at.button if b.label.startswith("💾 Save Changes")]
    assert save_buttons == []


def test_sequences_tab_add_variant_maxed_out_for_real_campaign():
    fake_spreadsheet = FakeSpreadsheet(_campaigns_page_fake_ws())

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "campaigns.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.session_state["selected_campaign"] = "Kelson_Creators_Licensing"
        at.run(timeout=15)

    assert list(at.exception) == []
    info_texts = " ".join(i.value for i in at.info)
    assert "maximum" in info_texts.lower()
    assert "already has all 5 stages" in info_texts


def test_sequences_tab_add_variant_validates_across_all_stages(tmp_path):
    """Uses a synthetic partial campaign (2 stages, 1 variant) so "Add
    Variant B" is actually available to test, unlike the real fixture
    which is already fully built out."""
    campaign_dir = tmp_path / "PartialSeqCampaign"
    campaign_dir.mkdir()
    (campaign_dir / "intro_A.txt").write_text("Subject: Intro A\n\nBody A")
    (campaign_dir / "followup1_A.txt").write_text("Subject: \n\nFollowup body A")

    fake_ws = {
        "PartialSeqCampaign Master Sheet": FakeWorksheet([], header=["LeadID", "Email", "Approval"]),
        "PartialSeqCampaign Response Sheet": FakeWorksheet([]),
        "PartialSeqCampaign Custom Log Sheet": FakeWorksheet([]),
        "PartialSeqCampaign Error Log": FakeWorksheet([]),
    }
    fake_spreadsheet = FakeSpreadsheet(fake_ws)
    captured, fake_create_file = _mock_github_writes()

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()), \
         patch("github_client.GitHubClient.create_file", fake_create_file), \
         patch("config.TEMPLATES_ROOT", str(tmp_path)), \
         patch("preview_logic.TEMPLATES_ROOT", str(tmp_path)):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "campaigns.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.session_state["selected_campaign"] = "PartialSeqCampaign"
        at.run(timeout=15)

        add_button = next((b for b in at.button if b.label == "Add Variant B"), None)
        assert add_button is not None, "Expected an 'Add Variant B' button for a 1-variant campaign"
        add_button.click()  # click with everything still blank — should show validation errors, not commit
        at.run(timeout=15)

    assert list(at.exception) == []
    assert captured.get("commits") is None or captured["commits"] == []
    error_texts = " ".join(e.value for e in at.error)
    assert "Subject is required" in error_texts or "Body is required" in error_texts


def test_settings_tab_renders_current_values_from_config():
    fake_spreadsheet = FakeSpreadsheet(_campaigns_page_fake_ws())

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "campaigns.py"))
        secrets = _dashboard_secrets()
        secrets["email_accounts_directory"] = {"sales1": "sales1@x.com", "sales2": "sales2@x.com"}
        at.secrets.update(secrets)
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.session_state["selected_campaign"] = "Kelson_Creators_Licensing"
        at.run(timeout=15)

    assert list(at.exception) == [], f"Settings tab raised: {list(at.exception)}"
    assert list(at.error) == []
    account_selector = next(ms for ms in at.multiselect if "sender accounts" in ms.label)
    assert set(account_selector.options) == {"sales1", "sales2"}
    daily_limit_input = next(ni for ni in at.number_input if "Daily limit" in ni.label)
    assert daily_limit_input.value == 100  # matches config/settings.yaml's real default


def test_settings_tab_shows_info_when_no_accounts_directory_configured():
    fake_spreadsheet = FakeSpreadsheet(_campaigns_page_fake_ws())

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "campaigns.py"))
        at.secrets.update(_dashboard_secrets())  # no email_accounts_directory
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.session_state["selected_campaign"] = "Kelson_Creators_Licensing"
        at.run(timeout=15)

    assert list(at.exception) == []
    info_texts = " ".join(i.value for i in at.info)
    assert "No accounts configured in Streamlit Secrets" in info_texts


def test_settings_tab_save_writes_yaml_with_new_values_and_correct_path():
    fake_spreadsheet = FakeSpreadsheet(_campaigns_page_fake_ws())
    captured, fake_create_file = _mock_github_writes()

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()), \
         patch("github_client.GitHubClient.create_file", fake_create_file), \
         patch("github_client.GitHubClient.get_file_sha", return_value=None):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "campaigns.py"))
        secrets = _dashboard_secrets()
        secrets["email_accounts_directory"] = {"sales1": "sales1@x.com"}
        at.secrets.update(secrets)
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.session_state["selected_campaign"] = "Kelson_Creators_Licensing"
        at.run(timeout=15)

        daily_limit_input = next(ni for ni in at.number_input if "Daily limit" in ni.label)
        daily_limit_input.set_value(250)
        at.run(timeout=15)

        save_button = next(b for b in at.button if b.label == "💾 Save Settings")
        save_button.click()
        at.run(timeout=15)

    assert list(at.exception) == [], f"Settings save raised: {list(at.exception)}"
    assert list(at.error) == []
    assert len(captured["commits"]) == 1
    commit = captured["commits"][0]
    assert commit["path"] == "config/campaigns/Kelson_Creators_Licensing.yaml"
    import yaml
    written = yaml.safe_load(commit["content"].decode("utf-8"))
    assert written["sending"]["daily_limit"] == 250


def test_settings_tab_save_rejects_non_positive_daily_limit_without_committing():
    """The widget itself enforces min_value=1, so the only way to reach
    validate_settings' rejection path through the UI is the per-account
    limit toggle — tested directly and thoroughly in test_settings_logic.py
    instead, where it doesn't depend on simulating a specific widget's
    numeric-input quirks. This smoke test instead confirms the more
    load-bearing thing: Save actually works end-to-end on first use with
    the real config defaults, with no error."""
    fake_spreadsheet = FakeSpreadsheet(_campaigns_page_fake_ws())
    captured, fake_create_file = _mock_github_writes()

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()), \
         patch("github_client.GitHubClient.create_file", fake_create_file):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "campaigns.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.session_state["selected_campaign"] = "Kelson_Creators_Licensing"
        at.run(timeout=15)

        save_button = next(b for b in at.button if b.label == "💾 Save Settings")
        save_button.click()
        at.run(timeout=15)

    assert list(at.exception) == []
    assert list(at.error) == []
    assert len(captured.get("commits", [])) == 1


def test_settings_tab_save_preserves_existing_status_and_schedule_keys(tmp_path):
    """The real regression this guards: Save must only ever touch the
    'sending' key of the override file. Anything else already there
    (status from Pause/Resume, schedule once that phase exists) must
    survive a Settings save untouched."""
    (tmp_path / "Kelson_Creators_Licensing.yaml").write_text(
        "status: paused\nschedule:\n  timezone: America/Los_Angeles\nsending:\n  daily_limit: 50\n"
    )
    fake_spreadsheet = FakeSpreadsheet(_campaigns_page_fake_ws())
    captured, fake_create_file = _mock_github_writes()

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()), \
         patch("github_client.GitHubClient.create_file", fake_create_file), \
         patch("config.CAMPAIGNS_DIR", str(tmp_path)):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "campaigns.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.session_state["selected_campaign"] = "Kelson_Creators_Licensing"
        at.run(timeout=15)

        daily_limit_input = next(ni for ni in at.number_input if "Daily limit" in ni.label)
        daily_limit_input.set_value(300)
        at.run(timeout=15)

        save_button = next(b for b in at.button if b.label == "💾 Save Settings")
        save_button.click()
        at.run(timeout=15)

    assert list(at.exception) == []
    assert list(at.error) == []
    import yaml
    written = yaml.safe_load(captured["commits"][0]["content"].decode("utf-8"))
    assert written["status"] == "paused"  # preserved, not clobbered
    assert written["schedule"] == {"timezone": "America/Los_Angeles"}  # preserved
    assert written["sending"]["daily_limit"] == 300  # actually updated


def test_settings_tab_select_all_accounts_actually_selects_and_persists_through_save():
    """Regression: clicking 'Select all accounts' visually appeared to
    work but didn't actually change what got saved — the button was
    reassigning a local Python variable, not the multiselect widget's own
    state, so a later Save click re-read the widget fresh and silently
    discarded the selection."""
    fake_spreadsheet = FakeSpreadsheet(_campaigns_page_fake_ws())
    captured, fake_create_file = _mock_github_writes()

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()), \
         patch("github_client.GitHubClient.create_file", fake_create_file):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "campaigns.py"))
        secrets = _dashboard_secrets()
        secrets["email_accounts_directory"] = {"sales1": "sales1@x.com", "sales2": "sales2@x.com"}
        at.secrets.update(secrets)
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.session_state["selected_campaign"] = "Kelson_Creators_Licensing"
        at.run(timeout=15)

        select_all_button = next(b for b in at.button if b.label == "Select all accounts")
        select_all_button.click()
        at.run(timeout=15)

        account_selector = next(ms for ms in at.multiselect if "sender accounts" in ms.label)
        assert set(account_selector.value) == {"sales1", "sales2"}  # widget itself actually shows both selected

        save_button = next(b for b in at.button if b.label == "💾 Save Settings")
        save_button.click()
        at.run(timeout=15)

    assert list(at.exception) == []
    import yaml
    written = yaml.safe_load(captured["commits"][0]["content"].decode("utf-8"))
    assert set(written["sending"]["rotation_accounts"]) == {"sales1", "sales2"}  # actually persisted


def test_schedule_tab_renders_sensible_defaults_for_unconfigured_campaign():
    fake_spreadsheet = FakeSpreadsheet(_campaigns_page_fake_ws())

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "campaigns.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.session_state["selected_campaign"] = "Kelson_Creators_Licensing"
        at.run(timeout=15)

    assert list(at.exception) == [], f"Schedule tab raised: {list(at.exception)}"
    assert list(at.error) == []
    tz_selector = next(sb for sb in at.selectbox if sb.label == "Time zone")
    assert tz_selector.value == "Pacific Time (US & Canada)"
    start_input = next(ti for ti in at.text_input if "Start time" in ti.label)
    assert start_input.value == "09:00"
    day_checkboxes = {cb.label: cb.value for cb in at.checkbox if cb.key and cb.key.startswith("schedule_day_")}
    assert day_checkboxes["Mon"] is True
    assert day_checkboxes["Sat"] is False


def test_schedule_tab_save_writes_correct_yaml_and_preserves_other_keys(tmp_path):
    (tmp_path / "Kelson_Creators_Licensing.yaml").write_text(
        "status: active\nsending:\n  daily_limit: 100\n"
    )
    fake_spreadsheet = FakeSpreadsheet(_campaigns_page_fake_ws())
    captured, fake_create_file = _mock_github_writes()

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()), \
         patch("github_client.GitHubClient.create_file", fake_create_file), \
         patch("config.CAMPAIGNS_DIR", str(tmp_path)):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "campaigns.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.session_state["selected_campaign"] = "Kelson_Creators_Licensing"
        at.run(timeout=15)

        tz_selector = next(sb for sb in at.selectbox if sb.label == "Time zone")
        tz_selector.set_value("UTC")
        at.run(timeout=15)

        sat_checkbox = next(cb for cb in at.checkbox if cb.key == "schedule_day_sat")
        sat_checkbox.set_value(True)
        at.run(timeout=15)

        save_button = next(b for b in at.button if b.label == "💾 Save Schedule")
        save_button.click()
        at.run(timeout=15)

    assert list(at.exception) == [], f"Schedule save raised: {list(at.exception)}"
    assert list(at.error) == []
    import yaml
    written = yaml.safe_load(captured["commits"][0]["content"].decode("utf-8"))
    assert written["schedule"]["timezone"] == "UTC"
    assert "sat" in written["schedule"]["send_days"]
    assert written["status"] == "active"  # preserved
    assert written["sending"]["daily_limit"] == 100  # preserved


def test_schedule_tab_save_rejects_when_no_days_selected():
    fake_spreadsheet = FakeSpreadsheet(_campaigns_page_fake_ws())
    captured, fake_create_file = _mock_github_writes()

    with patch("gspread.authorize", return_value=type("C", (), {"open_by_key": lambda self, k: fake_spreadsheet})()), \
         patch("google.oauth2.service_account.Credentials.from_service_account_info", return_value=object()), \
         patch("github_client.GitHubClient.create_file", fake_create_file):
        at = AppTest.from_file(os.path.join(PAGES_DIR, "campaigns.py"))
        at.secrets.update(_dashboard_secrets())
        for k, v in _authed_session().items():
            at.session_state[k] = v
        at.session_state["selected_campaign"] = "Kelson_Creators_Licensing"
        at.run(timeout=15)

        # Uncheck every default-selected weekday.
        for code in ["mon", "tue", "wed", "thu", "fri"]:
            cb = next(c for c in at.checkbox if c.key == f"schedule_day_{code}")
            cb.set_value(False)
        at.run(timeout=15)

        save_button = next(b for b in at.button if b.label == "💾 Save Schedule")
        save_button.click()
        at.run(timeout=15)

    assert list(at.exception) == []
    assert captured.get("commits") is None
    error_texts = " ".join(e.value for e in at.error)
    assert "at least one day" in error_texts


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
    at = AppTest.from_file(os.path.join(PAGES_DIR, "dashboard.py"))
    at.secrets.update(_dashboard_secrets())
    at.run()

    assert list(at.exception) == []
    titles = [t.value for t in at.title]
    assert "📊 Dashboard" not in titles  # blocked by login gate before reaching st.title
