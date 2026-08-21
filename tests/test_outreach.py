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


def test_render_text_unknown_variable_renders_empty_not_literal_placeholder():
    result = outreach.render_text("Hi {{NotARealVar}}", {})
    assert result == "Hi "
    assert "{{" not in result


def test_render_text_tracks_unknown_variable_as_missing():
    missing = []
    outreach.render_text("Hi {{TotallyUnknownField}}", {}, missing_out=missing)
    assert missing == ["TotallyUnknownField"]


def test_render_text_custom_column_resolves_directly():
    lead = {"Industry": "Healthcare"}
    result = outreach.render_text("Sector: {{Industry}}", lead)
    assert result == "Sector: Healthcare"


def test_render_text_blank_custom_column_renders_empty_without_flagging_missing():
    lead = {"Industry": ""}
    missing = []
    result = outreach.render_text("Sector: {{Industry}}", lead, missing_out=missing)
    assert result == "Sector: "
    assert missing == []  # blank DATA for a real column is not an error


def test_render_email_missing_variables_deduped():
    # Two different templates referencing the same unknown variable twice
    # within one file would only need de-duplication at render_email level;
    # simulate via two render_text calls sharing one missing_out list.
    missing = []
    outreach.render_text("{{Ghost}} and {{Ghost}} again", {}, missing_out=missing)
    seen = set()
    deduped = [m for m in missing if not (m in seen or seen.add(m))]
    assert deduped == ["Ghost"]


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
    assert cfg["master_tab"] == "test_campaign Master Sheet"
    assert cfg["responses_tab"] == "test_campaign Response Sheet"
    assert cfg["send_log_tab"] == "test_campaign Custom Log Sheet"
    assert cfg["error_log_tab"] == "test_campaign Error Log"
    assert cfg["dashboard_tab"] == "test_campaign Dashboard"
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
    assert cfg["responses_tab"] == "test_campaign Response Sheet"  # still auto-derived


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
        self.error_log = []
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

    def append_error_log(self, fields):
        self.error_log.append(fields)

    def get_all_responses(self):
        return list(self.responses)

    def get_all_send_log(self):
        return list(self.send_log)

    def get_all_error_log(self):
        return list(self.error_log)


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


# =============================================================================
# is_valid_email_format
# =============================================================================

def test_valid_email_formats():
    for addr in ["a@b.com", "john.doe+tag@sub.domain.co", "x@y.io"]:
        assert outreach.is_valid_email_format(addr), addr


def test_invalid_email_formats():
    for addr in ["", "no-at-sign", "a@b", "a @b.com", "a@b .com", "justtext"]:
        assert not outreach.is_valid_email_format(addr), addr


# =============================================================================
# classify_send_exception — error monitoring categories
# =============================================================================

def test_classify_missing_sender_account_error():
    exc = outreach.MissingSenderAccountError("nope")
    assert outreach.classify_send_exception(exc) == outreach.ERR_MISSING_SENDER_ACCOUNT


def test_classify_invalid_email_format_error():
    exc = outreach.InvalidEmailFormatError("bad format")
    assert outreach.classify_send_exception(exc) == outreach.ERR_INVALID_EMAIL


def test_classify_smtp_authentication_error():
    import smtplib
    exc = smtplib.SMTPAuthenticationError(535, b"Authentication failed")
    assert outreach.classify_send_exception(exc) == outreach.ERR_AUTH_FAILURE


def test_classify_smtp_recipients_refused():
    import smtplib
    exc = smtplib.SMTPRecipientsRefused({"bad@x.com": (550, b"No such user")})
    assert outreach.classify_send_exception(exc) == outreach.ERR_INVALID_EMAIL


def test_classify_smtp_rate_limit_by_code():
    import smtplib
    exc = smtplib.SMTPResponseException(454, b"4.7.0 Too many login attempts")
    assert outreach.classify_send_exception(exc) == outreach.ERR_RATE_LIMIT


def test_classify_smtp_rate_limit_by_keyword():
    import smtplib
    exc = smtplib.SMTPResponseException(552, b"rate limited, try again later")
    assert outreach.classify_send_exception(exc) == outreach.ERR_RATE_LIMIT


def test_classify_smtp_generic_response_exception_is_send_failure():
    import smtplib
    exc = smtplib.SMTPResponseException(552, b"message too large")
    assert outreach.classify_send_exception(exc) == outreach.ERR_SEND_FAILURE


def test_classify_generic_os_error_is_send_failure():
    exc = OSError("connection reset")
    assert outreach.classify_send_exception(exc) == outreach.ERR_SEND_FAILURE


# =============================================================================
# classify_imap_exception
# =============================================================================

def test_classify_imap_auth_failure_by_message():
    exc = Exception("b'AUTHENTICATIONFAILED Invalid credentials'")
    assert outreach.classify_imap_exception(exc) == outreach.ERR_AUTH_FAILURE


def test_classify_imap_generic_failure():
    exc = Exception("connection timed out")
    assert outreach.classify_imap_exception(exc) == outreach.ERR_REPLY_CHECK


# =============================================================================
# log_error
# =============================================================================

def test_log_error_appends_structured_entry():
    fake_sheets = FakeSheets([])
    outreach.log_error(fake_sheets, "camp1", outreach.ERR_SEND_FAILURE, "boom",
                        lead_id="L1", email_addr="a@b.com", stage="intro", batch_id="BATCH-1")
    assert len(fake_sheets.error_log) == 1
    entry = fake_sheets.error_log[0]
    assert entry["ErrorType"] == outreach.ERR_SEND_FAILURE
    assert entry["Message"] == "boom"
    assert entry["LeadID"] == "L1"
    assert entry["Campaign"] == "camp1"
    assert entry["BatchID"] == "BATCH-1"


def test_log_error_never_raises_even_if_sheet_write_fails():
    class BrokenSheets:
        def append_error_log(self, fields):
            raise RuntimeError("sheets down")

    # Must not raise — this is the whole point of log_error's own try/except.
    outreach.log_error(BrokenSheets(), "camp1", outreach.ERR_SEND_FAILURE, "boom")


# =============================================================================
# _get_or_create_ws — relaxed prefix-based header validation
# =============================================================================

class FakeGspreadExceptions:
    class WorksheetNotFound(Exception):
        pass


class FakeGspreadModule:
    exceptions = FakeGspreadExceptions


class FakeWs:
    def __init__(self, header=None):
        self._header = header if header is not None else []
        self.appended_rows = []

    def row_values(self, n):
        return self._header

    def append_row(self, row):
        self.appended_rows.append(row)
        if not self._header:
            self._header = row


class FakeSpreadsheet:
    def __init__(self, existing=None):
        self._existing = existing or {}
        self.added = {}

    def worksheet(self, title):
        if title in self._existing:
            return self._existing[title]
        raise FakeGspreadExceptions.WorksheetNotFound(title)

    def add_worksheet(self, title, rows, cols):
        ws = FakeWs()
        self.added[title] = ws
        self._existing[title] = ws
        return ws


def test_get_or_create_ws_creates_new_tab_with_header():
    spreadsheet = FakeSpreadsheet()
    ws = outreach._get_or_create_ws(spreadsheet, FakeGspreadModule, "MyTab", ["A", "B"])
    assert ws.appended_rows == [["A", "B"]]
    assert "MyTab" in spreadsheet.added


def test_get_or_create_ws_accepts_extra_trailing_custom_columns():
    existing_ws = FakeWs(header=["A", "B", "Industry", "JobTitle"])
    spreadsheet = FakeSpreadsheet(existing={"MyTab": existing_ws})
    ws = outreach._get_or_create_ws(spreadsheet, FakeGspreadModule, "MyTab", ["A", "B"])
    assert ws is existing_ws  # accepted as-is, no error, custom columns preserved


def test_get_or_create_ws_rejects_missing_required_column():
    existing_ws = FakeWs(header=["A", "WrongColumn"])
    spreadsheet = FakeSpreadsheet(existing={"MyTab": existing_ws})
    try:
        outreach._get_or_create_ws(spreadsheet, FakeGspreadModule, "MyTab", ["A", "B"])
        assert False, "should have raised RuntimeError"
    except RuntimeError:
        pass


def test_get_or_create_ws_fills_header_on_blank_existing_tab():
    existing_ws = FakeWs(header=[])
    spreadsheet = FakeSpreadsheet(existing={"MyTab": existing_ws})
    ws = outreach._get_or_create_ws(spreadsheet, FakeGspreadModule, "MyTab", ["A", "B"])
    assert ws.appended_rows == [["A", "B"]]


# =============================================================================
# write_dashboard / write_all_campaigns_dashboard
# =============================================================================

class FakeDashboardWs:
    def __init__(self):
        self.cleared = False
        self.updated_values = None
        self.updated_range = None

    def clear(self):
        self.cleared = True

    def update(self, values, range_name=None):
        self.updated_values = values
        self.updated_range = range_name


def test_write_dashboard_clears_then_writes_header_and_rows():
    ws = FakeDashboardWs()
    rows = [("Overview", "Total Leads", "5"), ("Overview", "Total Sent", "3")]
    outreach.write_dashboard(ws, rows)
    assert ws.cleared is True
    assert ws.updated_values[0] == outreach.DASHBOARD_COLUMNS
    assert ws.updated_values[1] == ["Overview", "Total Leads", "5"]
    assert ws.updated_values[2] == ["Overview", "Total Sent", "3"]


def test_write_all_campaigns_dashboard_clears_then_writes():
    ws = FakeDashboardWs()
    rows = [["camp1", "10", "8", "8", "7", "1", "0", "2", "25.0%", "50.0%"]]
    outreach.write_all_campaigns_dashboard(ws, rows)
    assert ws.cleared is True
    assert ws.updated_values[0] == outreach.ALL_CAMPAIGNS_DASHBOARD_COLUMNS
    assert ws.updated_values[1] == rows[0]


# =============================================================================
# compute_campaign_dashboard — full synthetic scenario
# =============================================================================

DASH_STAGES = [
    {"name": "intro", "template_prefix": "intro", "wait_days_after_previous": 0},
    {"name": "followup1", "template_prefix": "followup1", "wait_days_after_previous": 3},
    {"name": "followup2", "template_prefix": "followup2", "wait_days_after_previous": 4},
]


def _dash_lead(**overrides):
    lead = {
        "_row": 2, "LeadID": "", "Email": "", "Approval": "Yes", "Status": "", "ReplyStatus": "",
        "CurrentStage": "", "SenderAccount": "",
        "IntroSentAt": "", "IntroVariant": "",
        "FollowUp1SentAt": "", "FollowUp1Variant": "",
        "FollowUp2SentAt": "", "FollowUp2Variant": "",
    }
    lead.update(overrides)
    return lead


def _rows_to_dict(rows):
    return {(r[0], r[1]): r[2] for r in rows}


def test_compute_campaign_dashboard_full_scenario():
    ts = "2026-08-20 10:00:00"
    leads = [
        _dash_lead(_row=2, LeadID="L1", Email="john@abc.com", IntroSentAt=ts, IntroVariant="A",
                   SenderAccount="sales1", CurrentStage="intro", Status="intro Sent"),
        _dash_lead(_row=3, LeadID="L2", Email="jane@abc.com", IntroSentAt=ts, IntroVariant="B",
                   FollowUp1SentAt=ts, FollowUp1Variant="A", FollowUp2SentAt=ts, FollowUp2Variant="A",
                   SenderAccount="sales1", CurrentStage="followup2", Status="followup2 Sent"),
        _dash_lead(_row=4, LeadID="L3", Email="bob@xyz.com", IntroSentAt=ts, IntroVariant="A",
                   SenderAccount="sales2", CurrentStage="intro", Status=outreach.STATUS_STOPPED_REPLIED,
                   ReplyStatus="Replied"),
        _dash_lead(_row=5, LeadID="L4", Email=""),  # no email — excluded from total_leads
    ]
    send_log = [
        {"Status": "sent", "SenderAccount": "sales1"},
        {"Status": "sent", "SenderAccount": "sales1"},
        {"Status": "sent", "SenderAccount": "sales1"},
        {"Status": "sent", "SenderAccount": "sales2"},
        {"Status": "error", "SenderAccount": "sales1"},  # must NOT count toward total_sent
    ]
    responses = [
        {"Classification": outreach.CLASSIFICATION_GENUINE},
        {"Classification": outreach.CLASSIFICATION_BOUNCE_HARD},
        {"Classification": outreach.CLASSIFICATION_BOUNCE_SOFT},
    ]
    error_log = [
        {"Timestamp": "t1", "ErrorType": outreach.ERR_SEND_FAILURE, "Message": "boom1"},
        {"Timestamp": "t2", "ErrorType": outreach.ERR_SEND_FAILURE, "Message": "boom2"},
        {"Timestamp": "t3", "ErrorType": outreach.ERR_INVALID_EMAIL, "Message": "bad@"},
    ]
    campaign_cfg = {"stages": DASH_STAGES}

    rows = outreach.compute_campaign_dashboard(campaign_cfg, leads, responses, send_log, error_log)
    d = _rows_to_dict(rows)

    assert d[("Overview", "Total Leads (with Email)")] == "3"
    assert d[("Overview", "Unique Leads Contacted")] == "3"
    assert d[("Overview", "Total Emails Sent")] == "4"
    assert d[("Overview", "Delivered (est. = Sent minus Hard Bounces)")] == "3"
    assert d[("Overview", "Bounced (Hard)")] == "1"
    assert d[("Overview", "Bounced (Soft)")] == "1"
    assert d[("Overview", "Genuine Replies")] == "1"
    assert d[("Overview", "Reply Rate (Replies / Unique Contacted)")] == "33.3%"
    assert d[("Overview", "Sequence Completion (Reached Final Stage / Unique Contacted)")] == "33.3%"

    assert d[("Per-Stage", "intro - Sent")] == "3"
    assert d[("Per-Stage", "followup1 - Sent")] == "1"
    assert d[("Per-Stage", "followup2 - Sent")] == "1"

    assert d[("Sender Performance", "sales1 - Sent")] == "3"
    assert d[("Sender Performance", "sales1 - Replies")] == "0"
    assert d[("Sender Performance", "sales2 - Sent")] == "1"
    assert d[("Sender Performance", "sales2 - Replies")] == "1"
    assert d[("Sender Performance", "sales2 - Reply Rate")] == "100.0%"

    assert d[("Variant Performance", "intro-A - Sent")] == "2"
    assert d[("Variant Performance", "intro-A - Replies (approx.)")] == "1"
    assert d[("Variant Performance", "intro-B - Sent")] == "1"
    assert d[("Variant Performance", "intro-B - Replies (approx.)")] == "0"

    assert d[("Errors (All Time)", outreach.ERR_SEND_FAILURE)] == "2"
    assert d[("Errors (All Time)", outreach.ERR_INVALID_EMAIL)] == "1"


def test_compute_all_campaigns_row_matches_column_order():
    ts = "2026-08-20 10:00:00"
    leads = [_dash_lead(_row=2, LeadID="L1", Email="john@abc.com", IntroSentAt=ts)]
    send_log = [{"Status": "sent", "SenderAccount": "sales1"}]
    responses = []
    row = outreach.compute_all_campaigns_row("mycamp", leads, responses, send_log, DASH_STAGES)
    assert len(row) == len(outreach.ALL_CAMPAIGNS_DASHBOARD_COLUMNS)
    assert row[0] == "mycamp"
    assert row[1] == "1"  # Total Leads
    assert row[2] == "1"  # Unique Contacted
    assert row[3] == "1"  # Total Sent


# =============================================================================
# send_batch — new error-monitoring integration paths
# =============================================================================

def test_send_batch_invalid_email_format_never_calls_smtp(monkeypatch):
    campaign_cfg = _base_campaign_cfg()
    leads = [make_lead(_row=2, LeadID="L1", Email="not-a-valid-email")]
    fake_sheets = FakeSheets(leads)

    def should_not_be_called(*a, **kw):
        raise AssertionError("smtp_send should never be called for an invalid email format")

    monkeypatch.setattr(outreach, "smtp_send", should_not_be_called)

    results = outreach.send_batch(campaign_cfg, fake_sheets, ACCOUNTS, "intro", 10)
    assert results[0]["status"] == "error"
    assert results[0]["error_type"] == outreach.ERR_INVALID_EMAIL
    assert len(fake_sheets.error_log) == 1
    assert fake_sheets.error_log[0]["ErrorType"] == outreach.ERR_INVALID_EMAIL


def test_send_batch_unknown_sender_account_classified_correctly(monkeypatch):
    campaign_cfg = _base_campaign_cfg()
    leads = [make_lead(_row=2, LeadID="L1", Email="john@abc.com", SenderAccount="ghost_account")]
    fake_sheets = FakeSheets(leads)

    monkeypatch.setattr(outreach, "smtp_send",
                         lambda *a, **kw: (_ for _ in ()).throw(AssertionError("should not be called")))

    results = outreach.send_batch(campaign_cfg, fake_sheets, ACCOUNTS, "intro", 10)
    assert results[0]["status"] == "error"
    assert results[0]["error_type"] == outreach.ERR_MISSING_SENDER_ACCOUNT
    assert fake_sheets.error_log[0]["ErrorType"] == outreach.ERR_MISSING_SENDER_ACCOUNT


def test_send_batch_logs_missing_template_variable_after_successful_send(monkeypatch):
    campaign_cfg = _base_campaign_cfg()
    leads = [make_lead(_row=2, LeadID="L1", Email="john@abc.com")]
    fake_sheets = FakeSheets(leads)

    monkeypatch.setattr(outreach, "smtp_send", lambda *a, **kw: {"message_id": "<msg1@mail.gmail.com>"})
    monkeypatch.setattr(outreach, "render_email", lambda *a, **kw: {
        "subject": "Hi", "body": "Body", "missing_variables": ["Industry"],
    })

    results = outreach.send_batch(campaign_cfg, fake_sheets, ACCOUNTS, "intro", 10)
    assert results[0]["status"] == "sent"
    assert len(fake_sheets.error_log) == 1
    assert fake_sheets.error_log[0]["ErrorType"] == outreach.ERR_MISSING_VARIABLE
    assert "Industry" in fake_sheets.error_log[0]["Message"]


class RaisingUpdateFakeSheets(FakeSheets):
    def update_lead_fields(self, row_number, fields):
        raise RuntimeError("sheets down")


def test_send_batch_sent_but_sheet_error_when_sheet_write_fails_after_send(monkeypatch):
    campaign_cfg = _base_campaign_cfg()
    leads = [make_lead(_row=2, LeadID="L1", Email="john@abc.com")]
    fake_sheets = RaisingUpdateFakeSheets(leads)

    monkeypatch.setattr(outreach, "smtp_send", lambda *a, **kw: {"message_id": "<msg1@mail.gmail.com>"})

    results = outreach.send_batch(campaign_cfg, fake_sheets, ACCOUNTS, "intro", 10)
    assert results[0]["status"] == "sent_but_sheet_error"
    assert len(fake_sheets.error_log) == 1
    assert fake_sheets.error_log[0]["ErrorType"] == outreach.ERR_SHEETS_API
    assert "sent successfully" in fake_sheets.error_log[0]["Message"].lower()


# =============================================================================
# check_replies — IMAP failures now also logged to Error Log
# =============================================================================

def test_check_replies_imap_failure_logs_to_error_log(monkeypatch):
    leads = [make_lead(_row=2, LeadID="L1", Email="john@abc.com", MessageID="")]
    fake_sheets = FakeSheets(leads)

    def flaky_imap_fetch_recent(address, app_password, since_dt):
        raise RuntimeError("simulated IMAP outage")

    monkeypatch.setattr(outreach, "imap_fetch_recent", flaky_imap_fetch_recent)

    outreach.check_replies(fake_sheets, {"sales1": ACCOUNTS["sales1"]}, lookback_hours=24,
                            campaign_name="test_campaign")
    assert len(fake_sheets.error_log) == 1
    assert fake_sheets.error_log[0]["ErrorType"] == outreach.ERR_REPLY_CHECK
    assert fake_sheets.error_log[0]["Campaign"] == "test_campaign"
