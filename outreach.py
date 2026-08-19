#!/usr/bin/env python3
"""
Outreach Automation — single-file version.

Everything lives here on purpose: Sheets I/O, Gmail I/O, templating,
variant rotation, eligibility, reply/bounce classification, and the CLI.
The file is organized top-to-bottom in the order data flows, with clear
section headers, so you can read it start to finish instead of jumping
between files.

Usage:
    python outreach.py preview        --campaign NAME --stage NAME --batch-size N
    python outreach.py send           --campaign NAME --stage NAME --batch-size N
    python outreach.py check-replies  --campaign NAME
    python outreach.py setup-sheet    <SHEET_ID>
    python outreach.py generate-token <client_secret.json>

See README.md for full one-time setup (Google Sheets + Gmail credentials).
"""

import argparse
import base64
import json
import os
import random
import re
import sys
import time
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from typing import Dict, List, Optional

import yaml
from dateutil import parser as dateparser


# =============================================================================
# SECTION 1: Constants — sheet column names, single source of truth
# =============================================================================

MASTER_COLUMNS = [
    "LeadID",
    "FirstName",
    "LastName",
    "Email",
    "Company",
    "Event",
    "Campaign",
    "Approval",          # Pending | Yes | No | Paused
    "RequestedAction",   # Free-text, NOT read by the system. A place for you
                          # to note intent ("send FU1 next") before triggering
                          # the actual GitHub Actions workflow. Purely a
                          # bookkeeping aid — the workflow inputs are what
                          # actually control what gets sent.
    "CurrentStage",
    "ScheduledAt",
    "IntroSentAt",
    "IntroVariant",
    "FollowUp1SentAt",
    "FollowUp1Variant",
    "FollowUp2SentAt",
    "FollowUp2Variant",
    "FollowUp3SentAt",
    "FollowUp3Variant",
    "FollowUp4SentAt",
    "FollowUp4Variant",
    "NextEligibleAt",    # Computed after each send: when this lead becomes
                          # eligible for the NEXT stage. Blank if there's no
                          # next stage or nothing has been sent yet.
    "ReplyStatus",       # "" | Replied
    "ReplyAt",
    "LastInboundClassification",
    "LastInboundAt",
    "Status",            # Doubles as "Last Action": Pending | Intro Sent |
                          # ... | Stopped - Replied | Stopped - Bounced |
                          # Stopped - Rejected | Paused | Completed
    "LastActionAt",      # Timestamp companion to Status — updated on every
                          # send, reply, and bounce event.
    "Error",
    "MessageID",
    "ThreadID",
]

RESPONSES_COLUMNS = [
    "ResponseID",
    "LeadID",
    "Campaign",
    "ReceivedAt",
    "From",
    "Subject",
    "Snippet",
    "Classification",   # Genuine Reply | Auto-Reply | Out of Office |
                         # Bounce (Hard) | Bounce (Soft)
    "MatchMethod",       # Thread | Email — how this message was matched to
                          # a lead. Thread is the stronger, preferred match.
    "MessageID",
    "ThreadID",
    "ActionTaken",       # Stopped Sequence | Logged Only
]

SEND_LOG_COLUMNS = [
    "BatchID",
    "Timestamp",
    "LeadID",
    "Email",
    "Campaign",
    "Stage",
    "Variant",
    "Status",            # sent | error
    "MessageID",
    "ThreadID",
    "Error",
]

APPROVAL_YES = "Yes"

STATUS_STOPPED_REPLIED = "Stopped - Replied"
STATUS_STOPPED_BOUNCED = "Stopped - Bounced"
STATUS_STOPPED_REJECTED = "Stopped - Rejected"
STATUS_PAUSED = "Paused"
STATUS_COMPLETED = "Completed"

TERMINAL_STATUSES = {
    STATUS_STOPPED_REPLIED,
    STATUS_STOPPED_BOUNCED,
    STATUS_STOPPED_REJECTED,
    STATUS_PAUSED,
    STATUS_COMPLETED,
}

CLASSIFICATION_GENUINE = "Genuine Reply"
CLASSIFICATION_AUTOREPLY = "Auto-Reply"
CLASSIFICATION_OOO = "Out of Office"
CLASSIFICATION_BOUNCE_HARD = "Bounce (Hard)"
CLASSIFICATION_BOUNCE_SOFT = "Bounce (Soft)"

DATETIME_FMT = "%Y-%m-%d %H:%M:%S"


def stage_field_names(index: int) -> dict:
    """index 0 -> Intro fields, index 1 -> FollowUp1 fields, etc."""
    prefix = "Intro" if index == 0 else f"FollowUp{index}"
    return {"sent_at": f"{prefix}SentAt", "variant": f"{prefix}Variant"}


# =============================================================================
# SECTION 2: Config loading (config/campaigns.yaml)
# =============================================================================

class ConfigError(Exception):
    pass


def load_campaigns(path: str = "config/campaigns.yaml") -> dict:
    if not os.path.exists(path):
        raise ConfigError(f"Config file not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    campaigns = data.get("campaigns")
    if not campaigns:
        raise ConfigError("No 'campaigns' key found in config file.")
    return campaigns


def get_campaign(campaign_name: str, path: str = "config/campaigns.yaml") -> dict:
    campaigns = load_campaigns(path)
    if campaign_name not in campaigns:
        available = ", ".join(campaigns.keys())
        raise ConfigError(f"Campaign '{campaign_name}' not found. Available: {available}")
    cfg = campaigns[campaign_name]
    _validate_campaign(campaign_name, cfg)
    cfg["_campaign_name"] = campaign_name  # used for logging (SendLog, Responses)
    return cfg


def _validate_campaign(name: str, cfg: dict) -> None:
    required_top = ["sheet_id", "master_tab", "responses_tab", "templates_dir", "stages", "variants"]
    for key in required_top:
        if key not in cfg:
            raise ConfigError(f"Campaign '{name}' is missing required key '{key}'")

    if not cfg["sheet_id"] or cfg["sheet_id"].startswith("PUT_YOUR"):
        raise ConfigError(
            f"Campaign '{name}': sheet_id is still a placeholder. "
            "Set it to your real Google Sheet ID in config/campaigns.yaml."
        )
    if len(cfg["stages"]) == 0:
        raise ConfigError(f"Campaign '{name}' has no stages defined.")
    if len(cfg["stages"]) > 5:
        raise ConfigError(
            f"Campaign '{name}' defines {len(cfg['stages'])} stages, but the Master "
            "sheet schema only reserves columns for Intro + 4 follow-ups (5 max)."
        )
    if len(cfg["variants"]) < 1:
        raise ConfigError(f"Campaign '{name}' must define at least one variant.")

    sending = cfg.get("sending", {})
    for key in ["timezone", "window_start", "window_end", "delay_min_minutes", "delay_max_minutes", "daily_limit"]:
        if key not in sending:
            raise ConfigError(f"Campaign '{name}' sending config missing '{key}'")


def _stage_index(stages: List[Dict], stage_name: str) -> int:
    for i, s in enumerate(stages):
        if s["name"] == stage_name:
            return i
    raise ValueError(f"Stage '{stage_name}' not found in campaign config.")


# =============================================================================
# SECTION 3: Google Sheets connector (Master + Responses tabs)
# =============================================================================

SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


class SheetsConnector:
    def __init__(self, sheet_id: str, master_tab: str, responses_tab: str,
                 send_log_tab: str = "SendLog"):
        import gspread
        from google.oauth2.service_account import Credentials

        self.sheet_id = sheet_id
        self._gspread = gspread

        raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
        if not raw:
            raise RuntimeError(
                "GOOGLE_SERVICE_ACCOUNT_JSON env var is not set. "
                "Put the full service account key JSON there (as a GitHub secret in CI)."
            )
        info = json.loads(raw)
        creds = Credentials.from_service_account_info(info, scopes=SHEETS_SCOPES)
        self._client = gspread.authorize(creds)
        self._spreadsheet = self._client.open_by_key(sheet_id)

        self.master_ws = self._get_or_create_ws(master_tab, MASTER_COLUMNS)
        self.responses_ws = self._get_or_create_ws(responses_tab, RESPONSES_COLUMNS)
        self.send_log_ws = self._get_or_create_ws(send_log_tab, SEND_LOG_COLUMNS)

    def _get_or_create_ws(self, title: str, header: List[str]):
        gspread = self._gspread
        try:
            ws = self._spreadsheet.worksheet(title)
        except gspread.exceptions.WorksheetNotFound:
            ws = self._spreadsheet.add_worksheet(title=title, rows=2000, cols=len(header) + 2)
            ws.append_row(header)
            return ws

        existing_header = ws.row_values(1)
        if existing_header != header:
            if not existing_header:
                ws.append_row(header)
            else:
                raise RuntimeError(
                    f"Header row in tab '{title}' does not match the expected schema.\n"
                    f"Expected: {header}\nFound:    {existing_header}\n"
                    "Fix the sheet header manually, or run 'python outreach.py setup-sheet' "
                    "on a fresh sheet."
                )
        return ws

    def get_all_leads(self) -> List[Dict]:
        records = self.master_ws.get_all_records(expected_headers=MASTER_COLUMNS)
        leads = []
        for i, record in enumerate(records, start=2):  # row 1 is header
            record["_row"] = i
            leads.append(record)
        return leads

    def update_lead_fields(self, row_number: int, fields: Dict[str, str]) -> None:
        gspread = self._gspread
        updates = []
        for col_name, value in fields.items():
            if col_name not in MASTER_COLUMNS:
                raise ValueError(f"Unknown Master column '{col_name}'")
            col_index = MASTER_COLUMNS.index(col_name) + 1  # 1-indexed
            updates.append({
                "range": gspread.utils.rowcol_to_a1(row_number, col_index),
                "values": [[value]],
            })
        if updates:
            self.master_ws.batch_update(updates)

    def get_logged_message_ids(self) -> set:
        ids = self.responses_ws.col_values(RESPONSES_COLUMNS.index("MessageID") + 1)
        return set(ids[1:])  # skip header

    def append_response(self, fields: Dict[str, str]) -> None:
        row = [fields.get(col, "") for col in RESPONSES_COLUMNS]
        self.responses_ws.append_row(row, value_input_option="RAW")

    def append_send_log(self, fields: Dict[str, str]) -> None:
        row = [fields.get(col, "") for col in SEND_LOG_COLUMNS]
        self.send_log_ws.append_row(row, value_input_option="RAW")


# =============================================================================
# SECTION 4: Template engine — loads files, substitutes {{Variables}}
# =============================================================================

PLACEHOLDER_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")

TEMPLATE_VARIABLE_MAP = {
    "FirstName": "FirstName",
    "LastName": "LastName",
    "CompanyName": "Company",
    "EventName": "Event",
    "Email": "Email",
}


class TemplateError(Exception):
    pass


def load_template(templates_dir: str, template_prefix: str, variant: str) -> Dict[str, str]:
    """Template file format: first line = 'Subject: ...', blank line, then body."""
    path = os.path.join(templates_dir, f"{template_prefix}_{variant}.txt")
    if not os.path.exists(path):
        raise TemplateError(f"Template file not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    if not content.startswith("Subject:"):
        raise TemplateError(
            f"Template {path} must start with a 'Subject: ...' line, followed by a "
            "blank line and then the email body."
        )
    lines = content.split("\n")
    subject = lines[0][len("Subject:"):].strip()
    body = "\n".join(lines[1:]).lstrip("\n")
    return {"subject": subject, "body": body}


def render_text(text: str, lead: Dict[str, str]) -> str:
    def _replace(match):
        var_name = match.group(1)
        sheet_col = TEMPLATE_VARIABLE_MAP.get(var_name)
        if sheet_col is None:
            return match.group(0)  # unknown variable — leave as-is
        value = lead.get(sheet_col, "")
        return value if value else match.group(0)

    return PLACEHOLDER_RE.sub(_replace, text)


def render_email(templates_dir: str, template_prefix: str, variant: str, lead: Dict[str, str]) -> Dict[str, str]:
    tmpl = load_template(templates_dir, template_prefix, variant)
    return {"subject": render_text(tmpl["subject"], lead), "body": render_text(tmpl["body"], lead)}


# =============================================================================
# SECTION 5: Variant selector — balanced A/B/C/D rotation, independent per stage
# =============================================================================

def pick_variant(leads: List[Dict], variant_field: str, variants: List[str],
                  already_assigned_in_batch: Optional[Dict[str, int]] = None) -> str:
    counts = {v: 0 for v in variants}
    for lead in leads:
        v = lead.get(variant_field, "")
        if v in counts:
            counts[v] += 1

    if already_assigned_in_batch:
        for v, n in already_assigned_in_batch.items():
            if v in counts:
                counts[v] += n

    min_count = min(counts.values())
    least_used = [v for v, c in counts.items() if c == min_count]
    return random.choice(least_used)


# =============================================================================
# SECTION 6: Eligibility — who qualifies for a stage right now
# =============================================================================

def _parse_dt(value: str):
    if not value:
        return None
    try:
        return dateparser.parse(value)
    except (ValueError, TypeError):
        return None


def get_eligible_leads(leads: List[Dict], stages: List[Dict], stage_index: int) -> List[Dict]:
    this_sent_field = stage_field_names(stage_index)["sent_at"]

    prev_sent_field = None
    wait_days = 0
    if stage_index > 0:
        prev_sent_field = stage_field_names(stage_index - 1)["sent_at"]
        wait_days = stages[stage_index].get("wait_days_after_previous", 0)

    eligible = []
    now = datetime.now()

    for lead in leads:
        if lead.get("Approval") != APPROVAL_YES:
            continue
        if lead.get("Status", "") in TERMINAL_STATUSES:
            continue
        if lead.get("ReplyStatus", "") == "Replied":
            continue
        if lead.get(this_sent_field, ""):
            continue  # already sent this stage — duplicate protection

        if stage_index == 0:
            eligible.append(lead)
            continue

        prev_sent_raw = lead.get(prev_sent_field, "")
        if not prev_sent_raw:
            continue  # hasn't received the previous stage yet

        prev_sent_dt = _parse_dt(prev_sent_raw)
        if prev_sent_dt is None:
            continue

        if now >= prev_sent_dt + timedelta(days=wait_days):
            eligible.append(lead)

    return eligible


# =============================================================================
# SECTION 7: Gmail client — send + read, OAuth2 refresh-token auth
# =============================================================================

GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
]


def build_gmail_service():
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    client_id = os.environ.get("GMAIL_CLIENT_ID")
    client_secret = os.environ.get("GMAIL_CLIENT_SECRET")
    refresh_token = os.environ.get("GMAIL_REFRESH_TOKEN")
    missing = [n for n, v in [
        ("GMAIL_CLIENT_ID", client_id),
        ("GMAIL_CLIENT_SECRET", client_secret),
        ("GMAIL_REFRESH_TOKEN", refresh_token),
    ] if not v]
    if missing:
        raise RuntimeError(f"Missing required env vars: {', '.join(missing)}")

    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        client_id=client_id,
        client_secret=client_secret,
        token_uri="https://oauth2.googleapis.com/token",
        scopes=GMAIL_SCOPES,
    )
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def gmail_send(service, sender: str, to: str, subject: str, body_text: str,
               thread_id: Optional[str] = None) -> Dict[str, str]:
    message = MIMEText(body_text)
    message["to"] = to
    message["from"] = sender
    message["subject"] = subject
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()

    body = {"raw": raw}
    if thread_id:
        body["threadId"] = thread_id

    sent = service.users().messages().send(userId="me", body=body).execute()
    return {"message_id": sent.get("id", ""), "thread_id": sent.get("threadId", "")}


def _get_header(headers: List[Dict], name: str) -> str:
    name = name.lower()
    for h in headers:
        if h.get("name", "").lower() == name:
            return h.get("value", "")
    return ""


def _extract_body_text(payload: Dict) -> str:
    if payload.get("mimeType") == "text/plain" and "data" in payload.get("body", {}):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", errors="replace")
    for part in payload.get("parts", []) or []:
        text = _extract_body_text(part)
        if text:
            return text
    return ""


def gmail_list_messages_after(service, after_unix_ts: int, max_results: int = 100) -> List[Dict]:
    query = f"in:inbox after:{after_unix_ts}"
    results = []
    resp = service.users().messages().list(userId="me", q=query, maxResults=max_results).execute()
    for stub in resp.get("messages", []):
        full = service.users().messages().get(userId="me", id=stub["id"], format="full").execute()
        payload = full.get("payload", {})
        headers = payload.get("headers", [])
        results.append({
            "id": full.get("id", ""),
            "thread_id": full.get("threadId", ""),
            "snippet": full.get("snippet", ""),
            "subject": _get_header(headers, "Subject"),
            "from": _get_header(headers, "From"),
            "headers": {h.get("name", "").lower(): h.get("value", "") for h in headers},
            "body": _extract_body_text(payload),
        })
    return results


# =============================================================================
# SECTION 8: Classifier — Genuine Reply vs Auto-Reply vs OOO vs Bounce
# =============================================================================

OOO_KEYWORDS = [
    "out of office", "automatic reply", "auto-reply", "auto reply",
    "away from my desk", "on leave", "on vacation", "currently unavailable",
    "will be back on", "annual leave",
]
BOUNCE_HARD_KEYWORDS = [
    "undeliverable", "delivery has failed", "delivery failed",
    "address not found", "recipient address rejected",
    "mailbox unavailable", "550", "does not exist",
]
BOUNCE_SOFT_KEYWORDS = [
    "mailbox full", "quota exceeded", "temporarily deferred",
    "try again later", "451", "452",
]
BOUNCE_SENDER_PATTERNS = ["mailer-daemon", "postmaster", "mail delivery subsystem"]


def _status_code_severity(text: str) -> str:
    match = re.search(r"\b([245])\.\d\.\d\b", text)
    if not match:
        return ""
    code = match.group(1)
    if code == "5":
        return CLASSIFICATION_BOUNCE_HARD
    if code == "4":
        return CLASSIFICATION_BOUNCE_SOFT
    return ""


def classify_message(headers: Dict[str, str], subject: str, body: str, from_addr: str) -> str:
    headers = {k.lower(): v for k, v in (headers or {}).items()}
    subject_l = (subject or "").lower()
    body_l = (body or "").lower()
    from_l = (from_addr or "").lower()

    # 1. Header-based auto-reply detection (most reliable).
    auto_submitted = headers.get("auto-submitted", "").lower()
    if auto_submitted and auto_submitted != "no":
        return CLASSIFICATION_AUTOREPLY
    if "x-autoreply" in headers or "x-autorespond" in headers:
        return CLASSIFICATION_AUTOREPLY
    if headers.get("precedence", "").lower() in ("bulk", "auto_reply", "junk"):
        return CLASSIFICATION_AUTOREPLY

    # 2. Bounce detection via sender / content type.
    content_type = headers.get("content-type", "").lower()
    is_bounce_sender = any(p in from_l for p in BOUNCE_SENDER_PATTERNS)
    is_dsn = "report-type=delivery-status" in content_type or "multipart/report" in content_type

    if is_bounce_sender or is_dsn:
        severity = _status_code_severity(body_l) or _status_code_severity(subject_l)
        if severity:
            return severity
        if any(k in body_l or k in subject_l for k in BOUNCE_HARD_KEYWORDS):
            return CLASSIFICATION_BOUNCE_HARD
        if any(k in body_l or k in subject_l for k in BOUNCE_SOFT_KEYWORDS):
            return CLASSIFICATION_BOUNCE_SOFT
        return CLASSIFICATION_BOUNCE_HARD  # bounce-shaped but unclear -> safer to stop

    # 3. Keyword fallback for OOO (headers sometimes stripped by relays).
    if any(k in subject_l or k in body_l for k in OOO_KEYWORDS):
        return CLASSIFICATION_OOO

    # 4. Anything left is treated as a genuine reply.
    return CLASSIFICATION_GENUINE


# =============================================================================
# SECTION 9: Batch building + sending
# =============================================================================

def _count_sent_today(leads: List[Dict], stages: List[Dict]) -> int:
    today = datetime.now().strftime("%Y-%m-%d")
    count = 0
    for lead in leads:
        for i in range(len(stages)):
            field = stage_field_names(i)["sent_at"]
            if lead.get(field, "").startswith(today):
                count += 1
    return count


def make_batch_id() -> str:
    return f"BATCH-{datetime.now().strftime('%Y%m%d-%H%M%S')}"


def _compute_next_eligible_at(stages: List[Dict], idx: int, sent_at: datetime) -> str:
    """After sending stage `idx`, works out when the lead becomes eligible
    for the *next* stage, if there is one. Blank string if this was the last
    configured stage for the campaign."""
    if idx + 1 >= len(stages):
        return ""
    next_wait_days = stages[idx + 1].get("wait_days_after_previous", 0)
    return (sent_at + timedelta(days=next_wait_days)).strftime(DATETIME_FMT)


def build_batch(campaign_cfg: Dict, leads: List[Dict], stage_name: str, batch_size: int,
                 forced_variant: Optional[str] = None) -> List[Dict]:
    """Computes eligible leads + assigns variants WITHOUT sending or writing
    anything. Safe to call repeatedly for preview purposes.

    forced_variant: if given (e.g. "A"), every lead in the batch gets this
    variant instead of the balanced auto-rotation — useful for testing one
    variant deliberately. Must be one of campaign_cfg["variants"].
    """
    stages = campaign_cfg["stages"]
    variants = campaign_cfg["variants"]
    idx = _stage_index(stages, stage_name)
    fields = stage_field_names(idx)

    if forced_variant is not None and forced_variant not in variants:
        raise ValueError(f"Variant '{forced_variant}' is not in campaign variants: {variants}")

    eligible = get_eligible_leads(leads, stages, idx)[:batch_size]

    batch_counts = {v: 0 for v in variants}
    plan = []
    for lead in eligible:
        if forced_variant is not None:
            variant = forced_variant
        else:
            variant = pick_variant(leads, fields["variant"], variants, batch_counts)
            batch_counts[variant] += 1
        rendered = render_email(campaign_cfg["templates_dir"], stages[idx]["template_prefix"], variant, lead)
        # Continue the existing Gmail thread for follow-ups (idx > 0) so the
        # conversation stays together in the recipient's inbox, and so reply
        # matching can key off a stable ThreadID for this lead's whole
        # sequence. Intro (idx == 0) always starts a fresh thread.
        existing_thread_id = lead.get("ThreadID", "") if idx > 0 else ""
        plan.append({
            "lead": lead, "variant": variant,
            "subject": rendered["subject"], "body": rendered["body"],
            "thread_id": existing_thread_id or None,
        })
    return plan


def send_batch(campaign_cfg: Dict, sheets: SheetsConnector, gmail_service, sender_address: str,
               stage_name: str, batch_size: int, forced_variant: Optional[str] = None) -> List[Dict]:
    """Re-fetches leads fresh, then sends one-by-one with jittered delay and
    per-lead error isolation (one failure never aborts the rest of the batch).
    Every send (success or failure) is logged to the SendLog tab under a
    single BatchID for this run."""
    stages = campaign_cfg["stages"]
    sending_cfg = campaign_cfg["sending"]
    daily_limit = sending_cfg["daily_limit"]
    delay_min = sending_cfg["delay_min_minutes"]
    delay_max = sending_cfg["delay_max_minutes"]
    campaign_name = campaign_cfg.get("_campaign_name", "")

    leads = sheets.get_all_leads()
    already_today = _count_sent_today(leads, stages)
    remaining_today = max(daily_limit - already_today, 0)
    effective_batch_size = min(batch_size, remaining_today)

    results = []
    if effective_batch_size <= 0:
        return results

    plan = build_batch(campaign_cfg, leads, stage_name, effective_batch_size, forced_variant=forced_variant)
    idx = _stage_index(stages, stage_name)
    fields = stage_field_names(idx)
    batch_id = make_batch_id()

    for i, item in enumerate(plan):
        lead = item["lead"]
        row = lead["_row"]
        now = datetime.now()
        now_str = now.strftime(DATETIME_FMT)
        try:
            sent = gmail_send(gmail_service, sender=sender_address, to=lead["Email"],
                               subject=item["subject"], body_text=item["body"],
                               thread_id=item["thread_id"])
            sheets.update_lead_fields(row, {
                fields["sent_at"]: now_str,
                fields["variant"]: item["variant"],
                "CurrentStage": stage_name,
                "NextEligibleAt": _compute_next_eligible_at(stages, idx, now),
                "Status": f"{stage_name} Sent",
                "LastActionAt": now_str,
                "MessageID": sent["message_id"],
                "ThreadID": sent["thread_id"],
                "Error": "",
            })
            sheets.append_send_log({
                "BatchID": batch_id, "Timestamp": now_str, "LeadID": lead.get("LeadID", ""),
                "Email": lead.get("Email", ""), "Campaign": campaign_name, "Stage": stage_name,
                "Variant": item["variant"], "Status": "sent",
                "MessageID": sent["message_id"], "ThreadID": sent["thread_id"], "Error": "",
            })
            results.append({"lead_id": lead.get("LeadID"), "email": lead.get("Email"),
                             "status": "sent", "variant": item["variant"], "batch_id": batch_id})
        except Exception as exc:  # noqa: BLE001 - isolate per-lead failures
            sheets.update_lead_fields(row, {"Error": str(exc)[:500]})
            sheets.append_send_log({
                "BatchID": batch_id, "Timestamp": now_str, "LeadID": lead.get("LeadID", ""),
                "Email": lead.get("Email", ""), "Campaign": campaign_name, "Stage": stage_name,
                "Variant": item["variant"], "Status": "error",
                "MessageID": "", "ThreadID": "", "Error": str(exc)[:500],
            })
            results.append({"lead_id": lead.get("LeadID"), "email": lead.get("Email"),
                             "status": "error", "error": str(exc), "batch_id": batch_id})

        if i < len(plan) - 1:
            time.sleep(random.uniform(delay_min * 60, delay_max * 60))

    return results


# =============================================================================
# SECTION 10: Reply monitor — scan inbox, classify, log, update sheet
# =============================================================================

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def _extract_email(from_header: str) -> str:
    match = EMAIL_RE.search(from_header or "")
    return match.group(0).lower() if match else ""


def check_replies(sheets: SheetsConnector, gmail_service, lookback_hours: int,
                   campaign_name: str = "") -> List[Dict]:
    leads = sheets.get_all_leads()

    # Thread ID is the strong match: once a lead has a ThreadID on file (set
    # on their first send), every later message in that same Gmail thread is
    # unambiguously theirs, regardless of what address it was sent from or
    # whether the From-header name looks unusual. Email is the fallback for
    # the case where a lead has no ThreadID yet on record (shouldn't happen
    # once Intro has sent, but kept for robustness).
    by_thread = {}
    by_email = {}
    for lead in leads:
        thread_id = (lead.get("ThreadID") or "").strip()
        if thread_id:
            by_thread.setdefault(thread_id, lead)
        email = (lead.get("Email") or "").strip().lower()
        if email:
            by_email.setdefault(email, lead)

    already_logged = sheets.get_logged_message_ids()
    after_ts = int((datetime.now() - timedelta(hours=lookback_hours)).timestamp())
    messages = gmail_list_messages_after(gmail_service, after_ts)

    actions = []
    for msg in messages:
        if msg["id"] in already_logged:
            continue

        lead = by_thread.get(msg["thread_id"])
        match_method = "Thread"
        if lead is None:
            sender_email = _extract_email(msg["from"])
            lead = by_email.get(sender_email)
            match_method = "Email"
        if lead is None:
            continue  # not from a known lead, by either signal

        classification = classify_message(msg["headers"], msg["subject"], msg["body"], msg["from"])
        now_str = datetime.now().strftime(DATETIME_FMT)

        action_taken = "Logged Only"
        master_updates = {"LastInboundClassification": classification, "LastInboundAt": now_str,
                           "LastActionAt": now_str}

        if classification == CLASSIFICATION_GENUINE:
            master_updates.update({"ReplyStatus": "Replied", "ReplyAt": now_str, "Status": STATUS_STOPPED_REPLIED})
            action_taken = "Stopped Sequence"
        elif classification == CLASSIFICATION_BOUNCE_HARD:
            master_updates["Status"] = STATUS_STOPPED_BOUNCED
            action_taken = "Stopped Sequence"

        sheets.update_lead_fields(lead["_row"], master_updates)
        sheets.append_response({
            "ResponseID": msg["id"], "LeadID": lead.get("LeadID", ""), "Campaign": campaign_name,
            "ReceivedAt": now_str, "From": msg["from"], "Subject": msg["subject"], "Snippet": msg["snippet"],
            "Classification": classification, "MatchMethod": match_method,
            "MessageID": msg["id"], "ThreadID": msg["thread_id"], "ActionTaken": action_taken,
        })
        actions.append({"lead_id": lead.get("LeadID", ""), "email": lead.get("Email", ""),
                         "classification": classification, "action": action_taken, "match_method": match_method})

    return actions


# =============================================================================
# SECTION 11: One-time setup helpers (setup-sheet, generate-token)
# =============================================================================

def cmd_setup_sheet(args):
    """Creates the Master, Responses, and SendLog tabs (with correct headers)
    on a Sheet you've already created and shared with your service account."""
    connector = SheetsConnector(sheet_id=args.sheet_id, master_tab="Master", responses_tab="Responses")
    print(f"Master tab ready: {connector.master_ws.title}")
    print(f"Responses tab ready: {connector.responses_ws.title}")
    print(f"SendLog tab ready: {connector.send_log_ws.title}")
    print("\nDone. Add your leads to the Master tab below the header row.")


def cmd_generate_token(args):
    """ONE-TIME interactive step to get a Gmail OAuth refresh token. Needs a
    browser. Run once anywhere; the resulting refresh token then lets
    GitHub Actions send/read mail headlessly forever after."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(args.client_secret_file, GMAIL_SCOPES)
    creds = flow.run_local_server(port=0)

    print("\nSuccess. Save these as GitHub repo secrets:\n")
    print(f"GMAIL_CLIENT_ID={creds.client_id}")
    print(f"GMAIL_CLIENT_SECRET={creds.client_secret}")
    print(f"GMAIL_REFRESH_TOKEN={creds.refresh_token}")


# =============================================================================
# SECTION 12: Main CLI commands (preview, send, check-replies)
# =============================================================================

def _connect_sheets(campaign_cfg) -> SheetsConnector:
    return SheetsConnector(
        sheet_id=campaign_cfg["sheet_id"],
        master_tab=campaign_cfg["master_tab"],
        responses_tab=campaign_cfg["responses_tab"],
        send_log_tab=campaign_cfg.get("send_log_tab", "SendLog"),
    )


def cmd_preview(args):
    campaign_cfg = get_campaign(args.campaign)
    sheets = _connect_sheets(campaign_cfg)
    leads = sheets.get_all_leads()
    forced_variant = None if args.variant in (None, "Auto") else args.variant
    plan = build_batch(campaign_cfg, leads, args.stage, args.batch_size, forced_variant=forced_variant)

    if not plan:
        print(f"No eligible leads found for stage '{args.stage}'.")
        return

    print(f"{len(plan)} eligible lead(s) for stage '{args.stage}':\n")
    for item in plan:
        lead = item["lead"]
        print("=" * 70)
        print(f"Lead ID:  {lead.get('LeadID')}")
        print(f"To:       {lead.get('FirstName')} {lead.get('LastName')} <{lead.get('Email')}>")
        print(f"Variant:  {item['variant']}")
        print(f"Subject:  {item['subject']}")
        print("-" * 70)
        print(item["body"])
    print("=" * 70)
    print("\nNothing has been sent. Re-run with the 'send' command to actually send this batch.")


def cmd_send(args):
    campaign_cfg = get_campaign(args.campaign)
    sheets = _connect_sheets(campaign_cfg)
    gmail_service = build_gmail_service()
    sender_address = os.environ.get("GMAIL_SENDER_ADDRESS")
    if not sender_address:
        print("ERROR: GMAIL_SENDER_ADDRESS env var is not set.", file=sys.stderr)
        sys.exit(1)

    forced_variant = None if args.variant in (None, "Auto") else args.variant
    results = send_batch(campaign_cfg, sheets, gmail_service, sender_address, args.stage, args.batch_size,
                          forced_variant=forced_variant)

    if not results:
        print(f"No eligible leads to send for stage '{args.stage}' "
              "(none eligible, or today's sending limit already reached).")
        return

    batch_id = results[0].get("batch_id", "")
    sent = [r for r in results if r["status"] == "sent"]
    errors = [r for r in results if r["status"] == "error"]

    print(f"Batch ID: {batch_id}")
    print(f"Sent {len(sent)} email(s), {len(errors)} error(s).\n")
    for r in sent:
        print(f"  OK    {r['email']} (variant {r['variant']})")
    for r in errors:
        print(f"  ERROR {r['email']}: {r['error']}")


def cmd_check_replies(args):
    campaign_cfg = get_campaign(args.campaign)
    sheets = _connect_sheets(campaign_cfg)
    gmail_service = build_gmail_service()
    lookback_hours = campaign_cfg.get("reply_monitor", {}).get("lookback_hours", 24)

    actions = check_replies(sheets, gmail_service, lookback_hours, campaign_name=args.campaign)
    if not actions:
        print("No new inbound messages matched to a lead.")
        return

    print(f"Processed {len(actions)} inbound message(s):\n")
    for a in actions:
        print(f"  {a['email']:<35} {a['classification']:<18} ({a['match_method']:<6}) -> {a['action']}")


# =============================================================================
# SECTION 13: Argument parsing / entry point
# =============================================================================

def main():
    parser = argparse.ArgumentParser(description="Outreach automation (single-file)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_preview = sub.add_parser("preview", help="Show what would be sent, without sending")
    p_preview.add_argument("--campaign", required=True)
    p_preview.add_argument("--stage", required=True)
    p_preview.add_argument("--batch-size", type=int, required=True)
    p_preview.add_argument("--variant", default="Auto",
                            help="Force a specific variant letter (e.g. A) for the whole batch, "
                                 "instead of the default balanced auto-rotation. Default: Auto")
    p_preview.set_defaults(func=cmd_preview)

    p_send = sub.add_parser("send", help="Actually send a batch")
    p_send.add_argument("--campaign", required=True)
    p_send.add_argument("--stage", required=True)
    p_send.add_argument("--batch-size", type=int, required=True)
    p_send.add_argument("--variant", default="Auto",
                         help="Force a specific variant letter (e.g. A) for the whole batch, "
                              "instead of the default balanced auto-rotation. Default: Auto")
    p_send.set_defaults(func=cmd_send)

    p_replies = sub.add_parser("check-replies", help="Check inbox for new replies/bounces")
    p_replies.add_argument("--campaign", required=True)
    p_replies.set_defaults(func=cmd_check_replies)

    p_setup = sub.add_parser("setup-sheet", help="One-time: create Master/Responses tabs on a sheet")
    p_setup.add_argument("sheet_id")
    p_setup.set_defaults(func=cmd_setup_sheet)

    p_token = sub.add_parser("generate-token", help="One-time: get a Gmail OAuth refresh token (opens a browser)")
    p_token.add_argument("client_secret_file")
    p_token.set_defaults(func=cmd_generate_token)

    args = parser.parse_args()
    try:
        args.func(args)
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
