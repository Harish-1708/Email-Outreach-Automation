"""Unit tests for outreach.py (SMTP/IMAP edition)."""

import email as email_module
import os
import sys
from datetime import datetime, timedelta
from email.mime.text import MIMEText

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import outreach  # noqa: E402


TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "..", "templates", "sample_campaign")

STAGES = [
    {"name": "intro", "template_prefix": "intro", "wait_days_after_previous": 0},
    {"name": "followup1", "template_prefix": "followup1", "wait_days_after_previous": 3},
]
FMT = "%Y-%m-%d %H:%M:%S"


def make_lead(**overrides):
    lead = {
        "_row": 2, "Approval": "Yes", "Status": "", "ReplyStatus": "",
        "Email": "john@abc.com",
        "IntroSentAt": "", "IntroVariant": "",
        "FollowUp1SentAt": "", "FollowUp1Variant": "",
        "MessageID": "", "ThreadReferences": "", "SenderAccount": "",
    }
    lead.update(overrides)
    return lead


# =============================================================================
# classify_message (unchanged logic, transport-independent)
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
    leads = [{"IntroVariant": "A"}, {"IntroVariant": "A"}, {"IntroVariant": "B"}, {"IntroVariant": ""}]
    variant = outreach.pick_variant(leads, "IntroVariant", ["A", "B", "C", "D"])
    assert variant in ("C", "D")


def test_respects_in_batch_counts():
    leads = [{"IntroVariant": ""} for _ in range(4)]
    batch_counts = {"A": 5, "B": 0, "C": 0, "D": 0}
    variant = outreach.pick_variant(leads, "IntroVariant", ["A", "B", "C", "D"], batch_counts)
    assert variant in ("B", "C", "D")


def test_variant_selection_stays_balanced_over_many_picks():
    variants = ["A", "B", "C", "D"]
    leads = []
    for _ in range(40):
        v = outreach.pick_variant(leads, "IntroVariant", variants)
        leads.append({"IntroVariant": v})
    counts = {v: sum(1 for l in leads if l["IntroVariant"] == v) for v in variants}
    assert max(counts.values()) - min(counts.values()) <= 1


# =============================================================================
# get_eligible_leads — Email is the only mandatory field
# =============================================================================

def test_intro_stage_new_lead_is_eligible():
    eligible = outreach.get_eligible_leads([make_lead()], STAGES, 0)
    assert len(eligible) == 1


def test_lead_without_email_is_never_eligible():
    eligible = outreach.get_eligible_leads([make_lead(Email="")], STAGES, 0)
    assert len(eligible) == 0


def test_lead_with_only_email_filled_is_eligible():
    # FirstName, LastName, Company all blank — only Email present.
    lead = make_lead(FirstName="", LastName="", Company="")
    eligible = outreach.get_eligible_leads([lead], STAGES, 0)
    assert len(eligible) == 1


def test_intro_stage_already_sent_is_excluded():
    eligible = outreach.get_eligible_leads([make_lead(IntroSentAt="2026-08-01 10:00:00")], STAGES, 0)
    assert len(eligible) == 0


def test_pending_approval_is_excluded():
    eligible = outreach.get_eligible_leads([make_lead(Approval="Pending")], STAGES, 0)
    assert len(eligible) == 0


def test_blank_approval_behaves_as_not_approved():
    eligible = outreach.get_eligible_leads([make_lead(Approval="")], STAGES, 0)
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
# Template rendering — optional fields get graceful defaults
# =============================================================================

def test_render_text_substitutes_known_variables():
    lead = {"FirstName": "John", "Company": "ABC Events"}
    result = outreach.render_text("Hi {{FirstName}} from {{CompanyName}}", lead)
    assert result == "Hi John from ABC Events"


def test_render_text_leaves_unknown_variables_untouched():
    result = outreach.render_text("Hi {{NotARealVar}}", {})
    assert result == "Hi {{NotARealVar}}"


def test_render_text_blank_first_name_gets_default_not_literal_placeholder():
    result = outreach.render_text("Hi {{FirstName}},", {"FirstName": ""})
    assert result == "Hi there,"
    assert "{{" not in result


def test_render_text_blank_company_gets_default():
    result = outreach.render_text("at {{CompanyName}}", {"Company": ""})
    assert result == "at your team"


def test_all_20_templates_load_and_render_without_leftover_placeholders_even_with_blank_fields():
    # Deliberately blank FirstName/LastName/Company to prove optional fields
    # never leak "{{...}}" into an outgoing email.
    lead = {"FirstName": "", "LastName": "", "Company": "", "Email": "john@abc.com"}
    stages = ["intro", "followup1", "followup2", "followup3", "followup4"]
    variants = ["A", "B", "C", "D"]
    count = 0
    for stage in stages:
        for variant in variants:
            rendered = outreach.render_email(TEMPLATES_DIR, stage, variant, lead)
            assert rendered["subject"], f"{stage}_{variant}: empty subject"
            assert "{{" not in rendered["subject"], f"{stage}_{variant}: unrendered var in subject"
            assert "{{" not in rendered["body"], f"{stage}_{variant}: unrendered var in body"
            count += 1
    assert count == 20


def test_templates_contain_no_diaz_or_event_branding():
    stages = ["intro", "followup1", "followup2", "followup3", "followup4"]
    variants = ["A", "B", "C", "D"]
    for stage in stages:
        for variant in variants:
            path = os.path.join(TEMPLATES_DIR, f"{stage}_{variant}.txt")
            content = open(path, encoding="utf-8").read().lower()
            assert "diaz" not in content
            assert "festival" not in content
            assert "eventname" not in content


# =============================================================================
# config loader — shared sheet + auto-derived per-campaign tab names
# =============================================================================

def _write_config(tmp_path, extra_campaign_yaml="", shared_sheet_id="real_sheet_id_123"):
    config_content = f"""
shared_sheet_id: "{shared_sheet_id}"
email_accounts:
  default_account: "sales1"
campaigns:
  test_campaign:
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
{extra_campaign_yaml}
"""
    config_file = tmp_path / "campaigns.yaml"
    config_file.write_text(config_content)
    return str(config_file)


def test_get_campaign_auto_derives_tab_names_from_campaign_key(tmp_path):
    path = _write_config(tmp_path)
    cfg = outreach.get_campaign("test_campaign", path=path)
    assert cfg["sheet_id"] == "real_sheet_id_123"
    assert cfg["master_tab"] == "test_campaign_Master"
    assert cfg["responses_tab"] == "test_campaign_Responses"
    assert cfg["send_log_tab"] == "test_campaign_SendLog"
    assert cfg["_global_default_account"] == "sales1"


def test_get_campaign_rejects_missing_shared_sheet_id(tmp_path):
    path = _write_config(tmp_path, shared_sheet_id="")
    try:
        outreach.get_campaign("test_campaign", path=path)
        assert False, "should have raised ConfigError"
    except outreach.ConfigError:
        pass


def test_get_campaign_explicit_tab_override_wins_over_auto_derivation(tmp_path):
    path = _write_config(tmp_path, extra_campaign_yaml="    master_tab: \"CustomMaster\"")
    cfg = outreach.get_campaign("test_campaign", path=path)
    assert cfg["master_tab"] == "CustomMaster"
    assert cfg["responses_tab"] == "test_campaign_Responses"  # still auto-derived


def test_get_campaign_missing_campaign_raises():
    try:
        outreach.get_campaign("does_not_exist", path="config/campaigns.yaml")
        assert False, "should have raised ConfigError"
    except outreach.ConfigError:
        pass


# =============================================================================
# resolve_sender_account — lead override > campaign default > global default
# =============================================================================

ACCOUNTS = {
    "sales1": {"address": "sales1@gmail.com", "app_password": "aaaa bbbb cccc dddd"},
    "sales2": {"address": "sales2@gmail.com", "app_password": "eeee ffff gggg hhhh"},
}


def test_resolve_uses_lead_override_first():
    lead = make_lead(SenderAccount="sales2")
    campaign_cfg = {"_global_default_account": "sales1"}
    assert outreach.resolve_sender_account(lead, campaign_cfg, ACCOUNTS) == "sales2"


def test_resolve_falls_back_to_campaign_default():
    lead = make_lead(SenderAccount="")
    campaign_cfg = {"_global_default_account": "sales1", "default_sender_account": "sales2"}
    assert outreach.resolve_sender_account(lead, campaign_cfg, ACCOUNTS) == "sales2"


def test_resolve_falls_back_to_global_default():
    lead = make_lead(SenderAccount="")
    campaign_cfg = {"_global_default_account": "sales1"}
    assert outreach.resolve_sender_account(lead, campaign_cfg, ACCOUNTS) == "sales1"


def test_resolve_raises_for_unknown_lead_override():
    lead = make_lead(SenderAccount="does_not_exist")
    campaign_cfg = {"_global_default_account": "sales1"}
    try:
        outreach.resolve_sender_account(lead, campaign_cfg, ACCOUNTS)
        assert False, "should have raised ValueError"
    except ValueError:
        pass


def test_resolve_raises_when_nothing_configured():
    lead = make_lead(SenderAccount="")
    campaign_cfg = {"_global_default_account": ""}
    try:
        outreach.resolve_sender_account(lead, campaign_cfg, ACCOUNTS)
        assert False, "should have raised ValueError"
    except ValueError:
        pass


# =============================================================================
# SMTP message construction (pure — no real network)
# =============================================================================

def test_build_outbound_message_sets_core_headers():
    msg, message_id = outreach._build_outbound_message(
        "me@work.com", "lead@abc.com", "Hello", "Body text"
    )
    assert msg["From"] == "me@work.com"
    assert msg["To"] == "lead@abc.com"
    assert msg["Subject"] == "Hello"
    assert msg["Message-ID"] == message_id
    assert message_id  # non-empty
    assert msg["In-Reply-To"] is None
    assert msg["References"] is None


def test_build_outbound_message_sets_threading_headers_when_provided():
    msg, _ = outreach._build_outbound_message(
        "me@work.com", "lead@abc.com", "Re: Hello", "Body",
        in_reply_to="<abc@mail.gmail.com>", references="<abc@mail.gmail.com>",
    )
    assert msg["In-Reply-To"] == "<abc@mail.gmail.com>"
    assert msg["References"] == "<abc@mail.gmail.com>"


# =============================================================================
# IMAP message parsing (pure — no real network)
# =============================================================================

def _make_raw_email(subject="Re: Hello", from_addr="john@abc.com", body="Sure, let's talk.",
                     in_reply_to=None, references=None):
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = "me@work.com"
    msg["Message-ID"] = "<reply123@mail.gmail.com>"
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    return msg.as_bytes()


def test_parse_email_message_extracts_core_fields():
    raw = _make_raw_email(in_reply_to="<intro1@mail.gmail.com>", references="<intro1@mail.gmail.com>")
    parsed = outreach._parse_email_message(raw)
    assert parsed["subject"] == "Re: Hello"
    assert parsed["from"] == "john@abc.com"
    assert parsed["message_id"] == "<reply123@mail.gmail.com>"
    assert parsed["in_reply_to"] == "<intro1@mail.gmail.com>"
    assert parsed["references"] == "<intro1@mail.gmail.com>"
    assert "Sure, let's talk" in parsed["body"]


def test_parse_email_message_handles_missing_threading_headers():
    raw = _make_raw_email()
    parsed = outreach._parse_email_message(raw)
    assert parsed["in_reply_to"] == ""
    assert parsed["references"] == ""


# =============================================================================
# NextEligibleAt / BatchID
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


def test_make_batch_id_format():
    batch_id = outreach.make_batch_id()
    assert batch_id.startswith("BATCH-")
    assert len(batch_id) == len("BATCH-20260819-103000")


# =============================================================================
# build_batch — variant override + thread continuation
# =============================================================================

def test_build_batch_forced_variant_applies_to_all():
    campaign_cfg = {"templates_dir": TEMPLATES_DIR, "variants": ["A", "B", "C", "D"], "stages": STAGES}
    leads = [make_lead(_row=2, LeadID="L1", Email="john@abc.com"),
             make_lead(_row=3, LeadID="L2", Email="jane@xyz.com")]
    plan = outreach.build_batch(campaign_cfg, leads, "intro", 10, forced_variant="B")
    assert len(plan) == 2
    assert all(item["variant"] == "B" for item in plan)


def test_build_batch_rejects_invalid_forced_variant():
    campaign_cfg = {"templates_dir": TEMPLATES_DIR, "variants": ["A", "B", "C", "D"], "stages": STAGES}
    leads = [make_lead(_row=2, LeadID="L1", Email="john@abc.com")]
    try:
        outreach.build_batch(campaign_cfg, leads, "intro", 10, forced_variant="Z")
        assert False, "should have raised ValueError"
    except ValueError:
        pass


def test_intro_stage_never_sets_in_reply_to():
    campaign_cfg = {"templates_dir": TEMPLATES_DIR, "variants": ["A", "B", "C", "D"], "stages": STAGES}
    leads = [make_lead(_row=2, LeadID="L1", Email="john@abc.com", MessageID="<old@mail.gmail.com>")]
    plan = outreach.build_batch(campaign_cfg, leads, "intro", 10)
    assert plan[0]["in_reply_to"] is None


def test_followup_continues_thread_via_in_reply_to_and_references():
    campaign_cfg = {"templates_dir": TEMPLATES_DIR, "variants": ["A", "B", "C", "D"], "stages": STAGES}
    old = (datetime.now() - timedelta(days=5)).strftime(FMT)
    leads = [make_lead(_row=2, LeadID="L1", Email="john@abc.com", IntroSentAt=old,
                        MessageID="<intro1@mail.gmail.com>", ThreadReferences="")]
    plan = outreach.build_batch(campaign_cfg, leads, "followup1", 10)
    assert len(plan) == 1
    assert plan[0]["in_reply_to"] == "<intro1@mail.gmail.com>"
    assert plan[0]["references"] == "<intro1@mail.gmail.com>"


def test_followup_accumulates_references_chain():
    campaign_cfg = {"templates_dir": TEMPLATES_DIR, "variants": ["A", "B", "C", "D"],
                     "stages": STAGES + [{"name": "followup2", "template_prefix": "followup2",
                                           "wait_days_after_previous": 4}]}
    old = (datetime.now() - timedelta(days=5)).strftime(FMT)
    leads = [make_lead(_row=2, LeadID="L1", Email="john@abc.com", IntroSentAt=old, FollowUp1SentAt=old,
                        MessageID="<fu1@mail.gmail.com>", ThreadReferences="<intro1@mail.gmail.com>")]
    plan = outreach.build_batch(campaign_cfg, leads, "followup2", 10)
    assert plan[0]["in_reply_to"] == "<fu1@mail.gmail.com>"
    assert plan[0]["references"] == "<intro1@mail.gmail.com> <fu1@mail.gmail.com>"


# =============================================================================
# Full send_batch integration (fake Sheets + monkeypatched smtp_send)
# =============================================================================

class FakeSheets:
    def __init__(self, leads):
        self._leads = leads
        self.send_log = []
        self.responses = []
        self._logged_ids = set()

    def get_all_leads(self):
        return [dict(lead) for lead in self._leads]

    def update_lead_fields(self, row_number, fields):
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


def _base_campaign_cfg():
    return {
        "templates_dir": TEMPLATES_DIR, "variants": ["A", "B", "C", "D"], "stages": STAGES,
        "sending": {"daily_limit": 100, "delay_min_minutes": 0, "delay_max_minutes": 0},
        "_campaign_name": "test_campaign", "_global_default_account": "sales1",
    }


def test_send_batch_writes_batch_id_message_id_and_next_eligible_at(monkeypatch):
    campaign_cfg = _base_campaign_cfg()
    leads = [make_lead(_row=2, LeadID="L1", Email="john@abc.com")]
    fake_sheets = FakeSheets(leads)

    def fake_smtp_send(address, app_password, to, subject, body_text, in_reply_to=None, references=None):
        return {"message_id": "<msg1@mail.gmail.com>"}

    monkeypatch.setattr(outreach, "smtp_send", fake_smtp_send)

    results = outreach.send_batch(campaign_cfg, fake_sheets, ACCOUNTS, "intro", 10)

    assert len(results) == 1
    assert results[0]["status"] == "sent"
    assert results[0]["batch_id"].startswith("BATCH-")
    assert results[0]["account"] == "sales1"

    assert len(fake_sheets.send_log) == 1
    assert fake_sheets.send_log[0]["Status"] == "sent"
    assert fake_sheets.send_log[0]["SenderAccount"] == "sales1"

    updated = fake_sheets._leads[0]
    assert updated["IntroSentAt"]
    assert updated["NextEligibleAt"]
    assert updated["LastActionAt"]
    assert updated["MessageID"] == "<msg1@mail.gmail.com>"
    assert updated["SenderAccount"] == "sales1"  # locked in for future stages


def test_send_batch_locks_in_resolved_account_for_reuse():
    # After a lead's SenderAccount is written back, resolve_sender_account
    # should use it directly rather than re-resolving the default.
    lead = make_lead(SenderAccount="sales1")
    campaign_cfg = {"_global_default_account": "sales2"}  # different default, deliberately
    assert outreach.resolve_sender_account(lead, campaign_cfg, ACCOUNTS) == "sales1"


def test_send_batch_error_isolation_does_not_write_send_at(monkeypatch):
    campaign_cfg = _base_campaign_cfg()
    leads = [make_lead(_row=2, LeadID="L1", Email="john@abc.com")]
    fake_sheets = FakeSheets(leads)

    def failing_smtp_send(address, app_password, to, subject, body_text, in_reply_to=None, references=None):
        raise RuntimeError("simulated SMTP failure")

    monkeypatch.setattr(outreach, "smtp_send", failing_smtp_send)

    results = outreach.send_batch(campaign_cfg, fake_sheets, ACCOUNTS, "intro", 10)

    assert len(results) == 1
    assert results[0]["status"] == "error"
    assert fake_sheets.send_log[0]["Status"] == "error"
    assert fake_sheets._leads[0]["IntroSentAt"] == ""
    assert fake_sheets._leads[0]["Error"]


def test_send_batch_unknown_sender_account_is_isolated_per_lead(monkeypatch):
    campaign_cfg = _base_campaign_cfg()
    leads = [make_lead(_row=2, LeadID="L1", Email="john@abc.com", SenderAccount="ghost_account")]
    fake_sheets = FakeSheets(leads)

    def fake_smtp_send(*a, **kw):
        raise AssertionError("smtp_send should never be called for an unresolvable account")

    monkeypatch.setattr(outreach, "smtp_send", fake_smtp_send)

    results = outreach.send_batch(campaign_cfg, fake_sheets, ACCOUNTS, "intro", 10)
    assert results[0]["status"] == "error"
    assert "ghost_account" in results[0]["error"]


# =============================================================================
# check_replies — header-based match preferred over email-address match
# =============================================================================

def test_check_replies_matches_by_message_id_header_first(monkeypatch):
    leads = [make_lead(_row=2, LeadID="L1", Email="john@abc.com", MessageID="<intro1@mail.gmail.com>")]
    fake_sheets = FakeSheets(leads)

    def fake_imap_fetch_recent(address, app_password, since_dt):
        return [{
            "message_id": "<reply1@mail.gmail.com>", "in_reply_to": "<intro1@mail.gmail.com>",
            "references": "<intro1@mail.gmail.com>", "subject": "Re: Hello",
            "from": "someone-else@notjohn.com",  # deliberately NOT john's address
            "headers": {}, "body": "sure, let's talk", "snippet": "sure, let's talk",
        }]

    monkeypatch.setattr(outreach, "imap_fetch_recent", fake_imap_fetch_recent)

    actions = outreach.check_replies(fake_sheets, {"sales1": ACCOUNTS["sales1"]}, lookback_hours=24,
                                      campaign_name="test_campaign")
    assert len(actions) == 1
    assert actions[0]["lead_id"] == "L1"
    assert actions[0]["match_method"] == "Header"
    assert actions[0]["classification"] == outreach.CLASSIFICATION_GENUINE
    assert fake_sheets.responses[0]["Campaign"] == "test_campaign"
    assert fake_sheets.responses[0]["MatchMethod"] == "Header"


def test_check_replies_falls_back_to_email_when_no_header_match(monkeypatch):
    leads = [make_lead(_row=2, LeadID="L1", Email="john@abc.com", MessageID="")]
    fake_sheets = FakeSheets(leads)

    def fake_imap_fetch_recent(address, app_password, since_dt):
        return [{
            "message_id": "<reply2@mail.gmail.com>", "in_reply_to": "", "references": "",
            "subject": "Re: Hello", "from": "john@abc.com",
            "headers": {}, "body": "sounds good", "snippet": "sounds good",
        }]

    monkeypatch.setattr(outreach, "imap_fetch_recent", fake_imap_fetch_recent)

    actions = outreach.check_replies(fake_sheets, {"sales1": ACCOUNTS["sales1"]}, lookback_hours=24)
    assert len(actions) == 1
    assert actions[0]["match_method"] == "Email"


def test_check_replies_genuine_reply_stops_sequence(monkeypatch):
    leads = [make_lead(_row=2, LeadID="L1", Email="john@abc.com", MessageID="<intro1@mail.gmail.com>")]
    fake_sheets = FakeSheets(leads)

    def fake_imap_fetch_recent(address, app_password, since_dt):
        return [{
            "message_id": "<reply3@mail.gmail.com>", "in_reply_to": "<intro1@mail.gmail.com>",
            "references": "<intro1@mail.gmail.com>", "subject": "Re: Hello", "from": "john@abc.com",
            "headers": {}, "body": "yes let's talk", "snippet": "yes let's talk",
        }]

    monkeypatch.setattr(outreach, "imap_fetch_recent", fake_imap_fetch_recent)

    outreach.check_replies(fake_sheets, {"sales1": ACCOUNTS["sales1"]}, lookback_hours=24)
    updated = fake_sheets._leads[0]
    assert updated["Status"] == outreach.STATUS_STOPPED_REPLIED
    assert updated["ReplyStatus"] == "Replied"
    assert updated["LastActionAt"]


def test_check_replies_one_account_imap_failure_does_not_block_others(monkeypatch):
    leads = [make_lead(_row=2, LeadID="L1", Email="john@abc.com", MessageID="")]
    fake_sheets = FakeSheets(leads)

    def flaky_imap_fetch_recent(address, app_password, since_dt):
        if address == "sales1@gmail.com":
            raise RuntimeError("simulated IMAP outage")
        return [{
            "message_id": "<reply4@mail.gmail.com>", "in_reply_to": "", "references": "",
            "subject": "Re: Hello", "from": "john@abc.com",
            "headers": {}, "body": "interested", "snippet": "interested",
        }]

    monkeypatch.setattr(outreach, "imap_fetch_recent", flaky_imap_fetch_recent)

    actions = outreach.check_replies(fake_sheets, ACCOUNTS, lookback_hours=24)
    assert len(actions) == 1  # sales2's message still got processed despite sales1 failing
    assert actions[0]["account"] == "sales2"


# =============================================================================
# load_email_accounts
# =============================================================================

def test_load_email_accounts_parses_json(monkeypatch):
    monkeypatch.setenv("EMAIL_ACCOUNTS_JSON",
                        '{"sales1": {"address": "a@b.com", "app_password": "xxxx"}}')
    accounts = outreach.load_email_accounts()
    assert accounts["sales1"]["address"] == "a@b.com"


def test_load_email_accounts_missing_env_raises(monkeypatch):
    monkeypatch.delenv("EMAIL_ACCOUNTS_JSON", raising=False)
    try:
        outreach.load_email_accounts()
        assert False, "should have raised RuntimeError"
    except RuntimeError:
        pass


def test_load_email_accounts_rejects_incomplete_entry(monkeypatch):
    monkeypatch.setenv("EMAIL_ACCOUNTS_JSON", '{"sales1": {"address": "a@b.com"}}')  # missing app_password
    try:
        outreach.load_email_accounts()
        assert False, "should have raised RuntimeError"
    except RuntimeError:
        pass
