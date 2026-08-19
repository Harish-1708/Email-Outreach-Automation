"""Unit tests for outreach.py — all in one file to match the single-file app.

Run with:  python -m pytest tests/test_outreach.py -v
"""

import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import outreach  # noqa: E402


# =============================================================================
# classify_message
# =============================================================================

def test_genuine_reply():
    result = outreach.classify_message({}, "Re: Quick idea", "Sure, let's talk next week.", "john@abc.com")
    assert result == outreach.CLASSIFICATION_GENUINE


def test_auto_submitted_header():
    result = outreach.classify_message({"auto-submitted": "auto-replied"}, "Away", "I'm away", "john@abc.com")
    assert result == outreach.CLASSIFICATION_AUTOREPLY


def test_ooo_keyword_fallback():
    result = outreach.classify_message({}, "Out of Office", "I am out of office until Monday.", "john@abc.com")
    assert result == outreach.CLASSIFICATION_OOO


def test_hard_bounce_status_code():
    result = outreach.classify_message(
        {"content-type": "multipart/report; report-type=delivery-status"},
        "Delivery Status Notification (Failure)",
        "550 5.1.1 The email account does not exist.",
        "mailer-daemon@abc.com",
    )
    assert result == outreach.CLASSIFICATION_BOUNCE_HARD


def test_soft_bounce_status_code():
    result = outreach.classify_message(
        {"content-type": "multipart/report; report-type=delivery-status"},
        "Delivery delayed",
        "451 4.2.1 mailbox temporarily full",
        "mailer-daemon@abc.com",
    )
    assert result == outreach.CLASSIFICATION_BOUNCE_SOFT


def test_precedence_bulk():
    result = outreach.classify_message({"precedence": "bulk"}, "Newsletter", "content", "list@abc.com")
    assert result == outreach.CLASSIFICATION_AUTOREPLY


def test_bounce_sender_without_status_code_defaults_hard():
    result = outreach.classify_message({}, "Mail delivery failed", "delivery has failed", "mailer-daemon@abc.com")
    assert result == outreach.CLASSIFICATION_BOUNCE_HARD


# =============================================================================
# pick_variant
# =============================================================================

def test_picks_least_used_variant():
    leads = [
        {"IntroVariant": "A"},
        {"IntroVariant": "A"},
        {"IntroVariant": "B"},
        {"IntroVariant": ""},
    ]
    variant = outreach.pick_variant(leads, "IntroVariant", ["A", "B", "C", "D"])
    # C and D are both at 0 uses, A is at 2, B is at 1 -> must pick C or D
    assert variant in ("C", "D")


def test_respects_in_batch_counts():
    leads = [{"IntroVariant": ""} for _ in range(4)]
    batch_counts = {"A": 5, "B": 0, "C": 0, "D": 0}
    variant = outreach.pick_variant(leads, "IntroVariant", ["A", "B", "C", "D"], batch_counts)
    assert variant in ("B", "C", "D")


def test_empty_leads_returns_a_variant():
    variant = outreach.pick_variant([], "IntroVariant", ["A", "B", "C", "D"])
    assert variant in ("A", "B", "C", "D")


def test_variant_selection_stays_balanced_over_many_picks():
    variants = ["A", "B", "C", "D"]
    leads = []
    for _ in range(40):
        v = outreach.pick_variant(leads, "IntroVariant", variants)
        leads.append({"IntroVariant": v})
    counts = {v: sum(1 for l in leads if l["IntroVariant"] == v) for v in variants}
    # 40 picks across 4 variants -> each should land at exactly 10 with this
    # deterministic least-used strategy (no randomness needed once tied groups
    # shrink to size 1, but ties are broken randomly so allow small slack).
    assert max(counts.values()) - min(counts.values()) <= 1


# =============================================================================
# get_eligible_leads
# =============================================================================

STAGES = [
    {"name": "intro", "template_prefix": "intro", "wait_days_after_previous": 0},
    {"name": "followup1", "template_prefix": "followup1", "wait_days_after_previous": 3},
]
FMT = "%Y-%m-%d %H:%M:%S"


def make_lead(**overrides):
    lead = {
        "_row": 2, "Approval": "Yes", "Status": "", "ReplyStatus": "",
        "IntroSentAt": "", "IntroVariant": "",
        "FollowUp1SentAt": "", "FollowUp1Variant": "",
    }
    lead.update(overrides)
    return lead


def test_intro_stage_new_lead_is_eligible():
    eligible = outreach.get_eligible_leads([make_lead()], STAGES, 0)
    assert len(eligible) == 1


def test_intro_stage_already_sent_is_excluded():
    eligible = outreach.get_eligible_leads([make_lead(IntroSentAt="2026-08-01 10:00:00")], STAGES, 0)
    assert len(eligible) == 0


def test_pending_approval_is_excluded():
    eligible = outreach.get_eligible_leads([make_lead(Approval="Pending")], STAGES, 0)
    assert len(eligible) == 0


def test_followup1_requires_intro_sent_and_wait_period():
    recent = datetime.now().strftime(FMT)
    old = (datetime.now() - timedelta(days=5)).strftime(FMT)

    not_yet_waited = make_lead(IntroSentAt=recent)
    waited_enough = make_lead(IntroSentAt=old)
    never_sent_intro = make_lead(IntroSentAt="")

    eligible = outreach.get_eligible_leads([not_yet_waited, waited_enough, never_sent_intro], STAGES, 1)
    assert eligible == [waited_enough]


def test_replied_lead_is_excluded():
    eligible = outreach.get_eligible_leads([make_lead(ReplyStatus="Replied")], STAGES, 0)
    assert len(eligible) == 0


def test_stopped_status_is_excluded():
    eligible = outreach.get_eligible_leads([make_lead(Status="Stopped - Bounced")], STAGES, 0)
    assert len(eligible) == 0


# =============================================================================
# template engine
# =============================================================================

def test_render_text_substitutes_known_variables():
    lead = {"FirstName": "John", "Company": "ABC Events"}
    result = outreach.render_text("Hi {{FirstName}} from {{CompanyName}}", lead)
    assert result == "Hi John from ABC Events"


def test_render_text_leaves_unknown_variables_untouched():
    result = outreach.render_text("Hi {{NotARealVar}}", {})
    assert result == "Hi {{NotARealVar}}"


def test_render_text_leaves_missing_value_as_placeholder():
    result = outreach.render_text("Hi {{FirstName}}", {"FirstName": ""})
    assert result == "Hi {{FirstName}}"


def test_all_20_templates_load_and_render_without_leftover_placeholders():
    templates_dir = os.path.join(os.path.dirname(__file__), "..", "templates", "diaz_event")
    lead = {"FirstName": "John", "LastName": "Smith", "Company": "ABC Events",
            "Event": "ABC Festival", "Email": "john@abc.com"}
    stages = ["intro", "followup1", "followup2", "followup3", "followup4"]
    variants = ["A", "B", "C", "D"]

    count = 0
    for stage in stages:
        for variant in variants:
            rendered = outreach.render_email(templates_dir, stage, variant, lead)
            assert rendered["subject"], f"{stage}_{variant}: empty subject"
            assert "{{" not in rendered["subject"], f"{stage}_{variant}: unrendered var in subject"
            assert "{{" not in rendered["body"], f"{stage}_{variant}: unrendered var in body"
            count += 1
    assert count == 20


# =============================================================================
# config loader
# =============================================================================

def test_get_campaign_rejects_placeholder_sheet_id(tmp_path):
    config_content = """
campaigns:
  test_campaign:
    sheet_id: "PUT_YOUR_GOOGLE_SHEET_ID_HERE"
    master_tab: "Master"
    responses_tab: "Responses"
    templates_dir: "templates/x"
    variants: ["A"]
    stages:
      - name: intro
        template_prefix: intro
        wait_days_after_previous: 0
    sending:
      timezone: "Asia/Kolkata"
      window_start: "09:00"
      window_end: "17:00"
      delay_min_minutes: 1
      delay_max_minutes: 2
      daily_limit: 10
"""
    config_file = tmp_path / "campaigns.yaml"
    config_file.write_text(config_content)

    try:
        outreach.get_campaign("test_campaign", path=str(config_file))
        assert False, "should have raised ConfigError for placeholder sheet_id"
    except outreach.ConfigError:
        pass


def test_get_campaign_missing_campaign_raises():
    try:
        outreach.get_campaign("does_not_exist", path="config/campaigns.yaml")
        assert False, "should have raised ConfigError"
    except outreach.ConfigError:
        pass


# =============================================================================
# build_batch (integration of eligibility + variant selection + rendering)
# =============================================================================

def test_build_batch_end_to_end():
    templates_dir = os.path.join(os.path.dirname(__file__), "..", "templates", "diaz_event")
    campaign_cfg = {
        "templates_dir": templates_dir,
        "variants": ["A", "B", "C", "D"],
        "stages": STAGES,
    }
    leads = [
        make_lead(_row=2, LeadID="L1", FirstName="John", Email="john@abc.com",
                  Company="ABC Events", Event="ABC Festival"),
        make_lead(_row=3, LeadID="L2", FirstName="Jane", Email="jane@xyz.com",
                  Company="XYZ Corp", Event="ABC Festival"),
    ]
    plan = outreach.build_batch(campaign_cfg, leads, "intro", 10)
    assert len(plan) == 2
    for item in plan:
        assert item["variant"] in ("A", "B", "C", "D")
        assert "{{" not in item["subject"]
        assert "{{" not in item["body"]


# =============================================================================
# NextEligibleAt computation
# =============================================================================

def test_next_eligible_at_computed_for_next_stage():
    now = datetime(2026, 8, 19, 10, 0, 0)
    result = outreach._compute_next_eligible_at(STAGES, 0, now)
    expected = (now + timedelta(days=STAGES[1]["wait_days_after_previous"])).strftime(outreach.DATETIME_FMT)
    assert result == expected


def test_next_eligible_at_blank_for_last_stage():
    now = datetime(2026, 8, 19, 10, 0, 0)
    result = outreach._compute_next_eligible_at(STAGES, len(STAGES) - 1, now)
    assert result == ""


# =============================================================================
# Batch ID
# =============================================================================

def test_make_batch_id_format():
    batch_id = outreach.make_batch_id()
    assert batch_id.startswith("BATCH-")
    # BATCH-YYYYMMDD-HHMMSS
    assert len(batch_id) == len("BATCH-20260819-103000")


# =============================================================================
# Variant override (forced_variant)
# =============================================================================

def test_build_batch_forced_variant_applies_to_all():
    templates_dir = os.path.join(os.path.dirname(__file__), "..", "templates", "diaz_event")
    campaign_cfg = {"templates_dir": templates_dir, "variants": ["A", "B", "C", "D"], "stages": STAGES}
    leads = [
        make_lead(_row=2, LeadID="L1", FirstName="John", Email="john@abc.com",
                  Company="ABC Events", Event="ABC Festival"),
        make_lead(_row=3, LeadID="L2", FirstName="Jane", Email="jane@xyz.com",
                  Company="XYZ Corp", Event="ABC Festival"),
        make_lead(_row=4, LeadID="L3", FirstName="Bob", Email="bob@qrs.com",
                  Company="QRS Inc", Event="ABC Festival"),
    ]
    plan = outreach.build_batch(campaign_cfg, leads, "intro", 10, forced_variant="B")
    assert len(plan) == 3
    assert all(item["variant"] == "B" for item in plan)


def test_build_batch_rejects_invalid_forced_variant():
    templates_dir = os.path.join(os.path.dirname(__file__), "..", "templates", "diaz_event")
    campaign_cfg = {"templates_dir": templates_dir, "variants": ["A", "B", "C", "D"], "stages": STAGES}
    leads = [make_lead(_row=2, LeadID="L1", FirstName="John", Email="john@abc.com",
                        Company="ABC Events", Event="ABC Festival")]
    try:
        outreach.build_batch(campaign_cfg, leads, "intro", 10, forced_variant="Z")
        assert False, "should have raised ValueError for invalid variant"
    except ValueError:
        pass


# =============================================================================
# Thread continuation logic
# =============================================================================

def test_intro_stage_never_continues_a_thread():
    templates_dir = os.path.join(os.path.dirname(__file__), "..", "templates", "diaz_event")
    campaign_cfg = {"templates_dir": templates_dir, "variants": ["A", "B", "C", "D"], "stages": STAGES}
    leads = [make_lead(_row=2, LeadID="L1", FirstName="John", Email="john@abc.com",
                        Company="ABC Events", Event="ABC Festival", ThreadID="thread-123")]
    plan = outreach.build_batch(campaign_cfg, leads, "intro", 10)
    assert plan[0]["thread_id"] is None


def test_followup_continues_existing_thread():
    templates_dir = os.path.join(os.path.dirname(__file__), "..", "templates", "diaz_event")
    campaign_cfg = {"templates_dir": templates_dir, "variants": ["A", "B", "C", "D"], "stages": STAGES}
    old = (datetime.now() - timedelta(days=5)).strftime(FMT)
    leads = [make_lead(_row=2, LeadID="L1", FirstName="John", Email="john@abc.com",
                        Company="ABC Events", Event="ABC Festival",
                        IntroSentAt=old, ThreadID="thread-123")]
    plan = outreach.build_batch(campaign_cfg, leads, "followup1", 10)
    assert len(plan) == 1
    assert plan[0]["thread_id"] == "thread-123"


def test_followup_with_no_existing_thread_id_starts_fresh():
    templates_dir = os.path.join(os.path.dirname(__file__), "..", "templates", "diaz_event")
    campaign_cfg = {"templates_dir": templates_dir, "variants": ["A", "B", "C", "D"], "stages": STAGES}
    old = (datetime.now() - timedelta(days=5)).strftime(FMT)
    leads = [make_lead(_row=2, LeadID="L1", FirstName="John", Email="john@abc.com",
                        Company="ABC Events", Event="ABC Festival",
                        IntroSentAt=old, ThreadID="")]
    plan = outreach.build_batch(campaign_cfg, leads, "followup1", 10)
    assert plan[0]["thread_id"] is None


# =============================================================================
# Full send_batch integration (fake Sheets + fake Gmail send, no real network)
# =============================================================================

class FakeSheets:
    """In-memory stand-in for SheetsConnector, used to test send_batch/
    check_replies logic without touching real Google APIs."""

    def __init__(self, leads):
        self._leads = leads
        self.updates = []       # list of (row, fields) for update_lead_fields
        self.send_log = []      # list of fields dicts appended via append_send_log
        self.responses = []     # list of fields dicts appended via append_response
        self._logged_ids = set()

    def get_all_leads(self):
        return [dict(lead) for lead in self._leads]

    def update_lead_fields(self, row_number, fields):
        self.updates.append((row_number, fields))
        for lead in self._leads:
            if lead["_row"] == row_number:
                lead.update(fields)

    def append_send_log(self, fields):
        self.send_log.append(fields)

    def append_response(self, fields):
        self.responses.append(fields)
        self._logged_ids.add(fields.get("MessageID", ""))

    def get_logged_message_ids(self):
        return set(self._logged_ids)


def test_send_batch_writes_batch_id_and_next_eligible_at(monkeypatch):
    templates_dir = os.path.join(os.path.dirname(__file__), "..", "templates", "diaz_event")
    campaign_cfg = {
        "templates_dir": templates_dir, "variants": ["A", "B", "C", "D"], "stages": STAGES,
        "sending": {"daily_limit": 100, "delay_min_minutes": 0, "delay_max_minutes": 0},
        "_campaign_name": "test_campaign",
    }
    leads = [make_lead(_row=2, LeadID="L1", FirstName="John", Email="john@abc.com",
                        Company="ABC Events", Event="ABC Festival")]
    fake_sheets = FakeSheets(leads)

    def fake_gmail_send(service, sender, to, subject, body_text, thread_id=None):
        return {"message_id": "msg-1", "thread_id": "thread-1"}

    monkeypatch.setattr(outreach, "gmail_send", fake_gmail_send)

    results = outreach.send_batch(campaign_cfg, fake_sheets, gmail_service=None,
                                   sender_address="me@work.com", stage_name="intro", batch_size=10)

    assert len(results) == 1
    assert results[0]["status"] == "sent"
    assert results[0]["batch_id"].startswith("BATCH-")

    assert len(fake_sheets.send_log) == 1
    assert fake_sheets.send_log[0]["Status"] == "sent"
    assert fake_sheets.send_log[0]["Campaign"] == "test_campaign"

    updated_lead = fake_sheets._leads[0]
    assert updated_lead["IntroSentAt"]
    assert updated_lead["NextEligibleAt"]  # followup1 exists in STAGES -> should be set
    assert updated_lead["LastActionAt"]
    assert updated_lead["ThreadID"] == "thread-1"


def test_send_batch_error_isolation_does_not_write_send_at(monkeypatch):
    templates_dir = os.path.join(os.path.dirname(__file__), "..", "templates", "diaz_event")
    campaign_cfg = {
        "templates_dir": templates_dir, "variants": ["A", "B", "C", "D"], "stages": STAGES,
        "sending": {"daily_limit": 100, "delay_min_minutes": 0, "delay_max_minutes": 0},
        "_campaign_name": "test_campaign",
    }
    leads = [make_lead(_row=2, LeadID="L1", FirstName="John", Email="john@abc.com",
                        Company="ABC Events", Event="ABC Festival")]
    fake_sheets = FakeSheets(leads)

    def failing_gmail_send(service, sender, to, subject, body_text, thread_id=None):
        raise RuntimeError("simulated Gmail API failure")

    monkeypatch.setattr(outreach, "gmail_send", failing_gmail_send)

    results = outreach.send_batch(campaign_cfg, fake_sheets, gmail_service=None,
                                   sender_address="me@work.com", stage_name="intro", batch_size=10)

    assert len(results) == 1
    assert results[0]["status"] == "error"
    assert fake_sheets.send_log[0]["Status"] == "error"
    assert fake_sheets._leads[0]["IntroSentAt"] == ""  # never got marked as sent
    assert fake_sheets._leads[0]["Error"]


# =============================================================================
# Thread-aware reply matching
# =============================================================================

def test_check_replies_matches_by_thread_id_first(monkeypatch):
    leads = [make_lead(_row=2, LeadID="L1", FirstName="John", Email="john@abc.com",
                        ThreadID="thread-abc")]
    fake_sheets = FakeSheets(leads)

    def fake_list_messages_after(service, after_ts, max_results=100):
        return [{
            "id": "msg-2", "thread_id": "thread-abc", "snippet": "sure, interested",
            "subject": "Re: intro", "from": "someone-else@notjohn.com",  # deliberately NOT john's email
            "headers": {}, "body": "sure, let's talk",
        }]

    monkeypatch.setattr(outreach, "gmail_list_messages_after", fake_list_messages_after)

    actions = outreach.check_replies(fake_sheets, gmail_service=None, lookback_hours=24,
                                      campaign_name="test_campaign")
    assert len(actions) == 1
    assert actions[0]["lead_id"] == "L1"
    assert actions[0]["match_method"] == "Thread"
    assert actions[0]["classification"] == outreach.CLASSIFICATION_GENUINE
    assert fake_sheets.responses[0]["Campaign"] == "test_campaign"
    assert fake_sheets.responses[0]["MatchMethod"] == "Thread"


def test_check_replies_falls_back_to_email_when_no_thread_match(monkeypatch):
    leads = [make_lead(_row=2, LeadID="L1", FirstName="John", Email="john@abc.com", ThreadID="")]
    fake_sheets = FakeSheets(leads)

    def fake_list_messages_after(service, after_ts, max_results=100):
        return [{
            "id": "msg-3", "thread_id": "some-unrelated-thread", "snippet": "sure",
            "subject": "Re: intro", "from": "john@abc.com",
            "headers": {}, "body": "sounds good",
        }]

    monkeypatch.setattr(outreach, "gmail_list_messages_after", fake_list_messages_after)

    actions = outreach.check_replies(fake_sheets, gmail_service=None, lookback_hours=24)
    assert len(actions) == 1
    assert actions[0]["match_method"] == "Email"


def test_check_replies_genuine_reply_stops_sequence_and_sets_last_action_at(monkeypatch):
    leads = [make_lead(_row=2, LeadID="L1", FirstName="John", Email="john@abc.com", ThreadID="thread-xyz")]
    fake_sheets = FakeSheets(leads)

    def fake_list_messages_after(service, after_ts, max_results=100):
        return [{
            "id": "msg-4", "thread_id": "thread-xyz", "snippet": "yes let's talk",
            "subject": "Re: intro", "from": "john@abc.com",
            "headers": {}, "body": "yes let's talk",
        }]

    monkeypatch.setattr(outreach, "gmail_list_messages_after", fake_list_messages_after)

    outreach.check_replies(fake_sheets, gmail_service=None, lookback_hours=24)
    updated = fake_sheets._leads[0]
    assert updated["Status"] == outreach.STATUS_STOPPED_REPLIED
    assert updated["ReplyStatus"] == "Replied"
    assert updated["LastActionAt"]
