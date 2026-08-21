#!/usr/bin/env python3
"""
Outreach Automation — single-file version, SMTP/IMAP edition.

Runs entirely on GitHub Actions. Authentication is Gmail App Passwords
(generated once on a Google webpage, pasted into a GitHub secret) — no
OAuth, no client_secret.json, no browser code flow, no local execution.

Usage:
    python outreach.py preview        --campaign NAME --stage NAME --batch-size N [--variant A]
    python outreach.py send           --campaign NAME --stage NAME --batch-size N [--variant A]
    python outreach.py check-replies  --campaign NAME
    python outreach.py dashboard      --campaign NAME | --all

See README.md for full setup (Google Sheet + service account + App Passwords).
"""

import argparse
import email
import imaplib
import json
import os
import random
import re
import smtplib
import ssl
import sys
import time
from datetime import datetime, timedelta
from email.header import decode_header
from email.mime.text import MIMEText
from email.utils import make_msgid, formatdate, parsedate_to_datetime
from typing import Dict, List, Optional, Tuple

import yaml
from dateutil import parser as dateparser


# =============================================================================
# SECTION 1: Constants — sheet column names and tab-name conventions
# =============================================================================

MASTER_COLUMNS = [
    "LeadID",
    "FirstName",          # Optional — blank renders as "there" in templates
    "LastName",             # Optional
    "Email",                 # MANDATORY — the only required field per lead
    "Company",                # Optional — blank renders as "your team"
    "Campaign",
    "Approval",               # Pending | Yes | No | Paused (blank behaves as Pending)
    "SenderAccount",          # Optional — which account to send from. Locked in
                                # after first send so later stages match.
    "RequestedAction",        # Free-text, NOT read by the system.
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
    "NextEligibleAt",
    "ReplyStatus",             # "" | Replied
    "ReplyAt",
    "LastInboundClassification",
    "LastInboundAt",
    "Status",                   # Doubles as "Last Action"
    "LastActionAt",
    "Error",
    "MessageID",
    "ThreadReferences",
]
# NOTE: this is the REQUIRED prefix of the Master header row. You may add
# extra columns of your own AFTER these (e.g. "Industry", "JobTitle") and
# reference them directly in templates as {{Industry}} — see Section 4.

RESPONSES_COLUMNS = [
    "ResponseID",
    "LeadID",
    "Campaign",
    "ReceivedAt",
    "From",
    "Subject",
    "Snippet",
    "Classification",
    "MatchMethod",
    "MessageID",
    "InReplyTo",
    "ActionTaken",
]

SEND_LOG_COLUMNS = [
    "BatchID",
    "Timestamp",
    "LeadID",
    "Email",
    "Campaign",
    "Stage",
    "Variant",
    "SenderAccount",
    "Status",            # sent | error
    "MessageID",
    "Error",
]

ERROR_LOG_COLUMNS = [
    "Timestamp",
    "Campaign",
    "ErrorType",
    "LeadID",
    "Email",
    "Stage",
    "BatchID",
    "Message",
]

DASHBOARD_COLUMNS = ["Section", "Metric", "Value"]

ALL_CAMPAIGNS_DASHBOARD_COLUMNS = [
    "Campaign", "Total Leads", "Unique Contacted", "Total Sent",
    "Delivered (est.)", "Bounced (Hard)", "Bounced (Soft)", "Replies",
    "Reply Rate", "Sequence Completion",
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

# Error monitoring categories.
ERR_SEND_FAILURE = "Send Failure"
ERR_AUTH_FAILURE = "Authentication Failure"
ERR_INVALID_EMAIL = "Invalid Email Address"
ERR_SHEETS_API = "Sheets API Error"
ERR_RATE_LIMIT = "Rate-Limit Error"
ERR_MISSING_VARIABLE = "Missing Template Variable"
ERR_MISSING_SENDER_ACCOUNT = "Missing Sender Account"
ERR_REPLY_CHECK = "Reply Check Failure"

DATETIME_FMT = "%Y-%m-%d %H:%M:%S"


def stage_field_names(index: int) -> dict:
    """index 0 -> Intro fields, index 1 -> FollowUp1 fields, etc."""
    prefix = "Intro" if index == 0 else f"FollowUp{index}"
    return {"sent_at": f"{prefix}SentAt", "variant": f"{prefix}Variant"}


class MissingSenderAccountError(ValueError):
    pass


class InvalidEmailFormatError(ValueError):
    pass


# =============================================================================
# SECTION 2: Config loading (config/campaigns.yaml)
#
# One shared Google Sheet across all campaigns. Each campaign gets its own
# 5 tabs, auto-named and auto-CREATED the first time that campaign runs.
# =============================================================================

class ConfigError(Exception):
    pass


def load_config(path: str = "config/campaigns.yaml") -> dict:
    if not os.path.exists(path):
        raise ConfigError(f"Config file not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if "campaigns" not in data or not data["campaigns"]:
        raise ConfigError("No 'campaigns' key found in config file.")
    return data


def get_campaign(campaign_name: str, path: str = "config/campaigns.yaml") -> dict:
    data = load_config(path)
    campaigns = data["campaigns"]
    if campaign_name not in campaigns:
        available = ", ".join(campaigns.keys())
        raise ConfigError(f"Campaign '{campaign_name}' not found. Available: {available}")

    cfg = dict(campaigns[campaign_name])  # shallow copy — never mutate the shared dict
    shared_sheet_id = data.get("shared_sheet_id", "")

    cfg["sheet_id"] = cfg.get("sheet_id") or shared_sheet_id
    cfg["master_tab"] = cfg.get("master_tab") or f"{campaign_name} Master Sheet"
    cfg["responses_tab"] = cfg.get("responses_tab") or f"{campaign_name} Response Sheet"
    cfg["send_log_tab"] = cfg.get("send_log_tab") or f"{campaign_name} Custom Log Sheet"
    cfg["error_log_tab"] = cfg.get("error_log_tab") or f"{campaign_name} Error Log"
    cfg["dashboard_tab"] = cfg.get("dashboard_tab") or f"{campaign_name} Dashboard"
    cfg["_campaign_name"] = campaign_name
    cfg["_global_default_account"] = (data.get("email_accounts") or {}).get("default_account", "")

    _validate_campaign(campaign_name, cfg)
    return cfg


def _validate_campaign(name: str, cfg: dict) -> None:
    required = ["templates_dir", "stages", "variants", "sending"]
    for key in required:
        if key not in cfg:
            raise ConfigError(f"Campaign '{name}' is missing required key '{key}'")

    if not cfg.get("sheet_id") or str(cfg["sheet_id"]).startswith("PUT_YOUR"):
        raise ConfigError(
            f"Campaign '{name}': no sheet_id resolved. Set 'shared_sheet_id' at the "
            "top of config/campaigns.yaml (or 'sheet_id' on this specific campaign)."
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
# SECTION 3: Google Sheets connector
#
# Each campaign gets 5 tabs: Master Sheet, Response Sheet, Custom Log Sheet,
# Error Log, Dashboard — all auto-created on first connection. Header
# validation only requires the REQUIRED columns to be present as a prefix,
# in order; you're free to add extra trailing columns of your own (e.g. for
# custom template variables) without breaking anything.
# =============================================================================

SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def _build_gspread_client():
    import gspread
    from google.oauth2.service_account import Credentials

    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not raw:
        raise RuntimeError(
            "GOOGLE_SERVICE_ACCOUNT_JSON env var is not set. "
            "Put the full service account key JSON there (as a GitHub secret)."
        )
    info = json.loads(raw)
    creds = Credentials.from_service_account_info(info, scopes=SHEETS_SCOPES)
    return gspread.authorize(creds), gspread


def _get_or_create_ws(spreadsheet, gspread_module, title: str, required_header: List[str]):
    try:
        ws = spreadsheet.worksheet(title)
    except gspread_module.exceptions.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=title, rows=2000, cols=max(len(required_header) + 5, 10))
        ws.append_row(required_header)
        return ws

    existing_header = ws.row_values(1)
    if not existing_header:
        ws.append_row(required_header)
    elif existing_header[:len(required_header)] != required_header:
        raise RuntimeError(
            f"Header row in tab '{title}' does not start with the expected columns.\n"
            f"Expected (in this order, as a prefix): {required_header}\n"
            f"Found: {existing_header}\n"
            "Fix the header manually, or delete the tab so it gets recreated "
            "automatically on the next run. Extra columns AFTER the required "
            "ones are fine and preserved."
        )
    return ws


class SheetsConnector:
    def __init__(self, sheet_id: str, master_tab: str, responses_tab: str, send_log_tab: str,
                 error_log_tab: str, dashboard_tab: str):
        self.sheet_id = sheet_id
        self._client, self._gspread = _build_gspread_client()
        self._spreadsheet = self._client.open_by_key(sheet_id)

        self.master_ws = _get_or_create_ws(self._spreadsheet, self._gspread, master_tab, MASTER_COLUMNS)
        self.responses_ws = _get_or_create_ws(self._spreadsheet, self._gspread, responses_tab, RESPONSES_COLUMNS)
        self.send_log_ws = _get_or_create_ws(self._spreadsheet, self._gspread, send_log_tab, SEND_LOG_COLUMNS)
        self.error_log_ws = _get_or_create_ws(self._spreadsheet, self._gspread, error_log_tab, ERROR_LOG_COLUMNS)
        self.dashboard_ws = _get_or_create_ws(self._spreadsheet, self._gspread, dashboard_tab, DASHBOARD_COLUMNS)

    # ---------- Master ----------

    def get_all_leads(self) -> List[Dict]:
        # No expected_headers override: reads the REAL header row, so any
        # extra custom columns you've added come through as dict keys too.
        records = self.master_ws.get_all_records()
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

    # ---------- Responses ----------

    def get_logged_message_ids(self) -> set:
        ids = self.responses_ws.col_values(RESPONSES_COLUMNS.index("MessageID") + 1)
        return set(ids[1:])  # skip header

    def get_all_responses(self) -> List[Dict]:
        return self.responses_ws.get_all_records()

    def append_response(self, fields: Dict[str, str]) -> None:
        row = [fields.get(col, "") for col in RESPONSES_COLUMNS]
        self.responses_ws.append_row(row, value_input_option="RAW")

    # ---------- Send Log ----------

    def get_all_send_log(self) -> List[Dict]:
        return self.send_log_ws.get_all_records()

    def append_send_log(self, fields: Dict[str, str]) -> None:
        row = [fields.get(col, "") for col in SEND_LOG_COLUMNS]
        self.send_log_ws.append_row(row, value_input_option="RAW")

    # ---------- Error Log ----------

    def get_all_error_log(self) -> List[Dict]:
        return self.error_log_ws.get_all_records()

    def append_error_log(self, fields: Dict[str, str]) -> None:
        row = [fields.get(col, "") for col in ERROR_LOG_COLUMNS]
        self.error_log_ws.append_row(row, value_input_option="RAW")


def get_all_campaigns_dashboard_ws(sheet_id: str):
    """The combined cross-campaign dashboard lives in one shared tab, not
    scoped to any single campaign's connector."""
    client, gspread_module = _build_gspread_client()
    spreadsheet = client.open_by_key(sheet_id)
    return _get_or_create_ws(spreadsheet, gspread_module, "All Campaigns Dashboard", ALL_CAMPAIGNS_DASHBOARD_COLUMNS)


# =============================================================================
# SECTION 4: Error logging helper
#
# Used everywhere an error needs recording. Never lets a logging failure
# crash the run it's trying to report on — falls back to stderr.
# =============================================================================

def log_error(sheets: "SheetsConnector", campaign_name: str, error_type: str, message: str,
              lead_id: str = "", email_addr: str = "", stage: str = "", batch_id: str = "") -> None:
    try:
        sheets.append_error_log({
            "Timestamp": datetime.now().strftime(DATETIME_FMT),
            "Campaign": campaign_name, "ErrorType": error_type, "LeadID": lead_id,
            "Email": email_addr, "Stage": stage, "BatchID": batch_id, "Message": str(message)[:500],
        })
    except Exception as log_exc:  # noqa: BLE001 - never let error logging crash the run
        print(f"WARNING: failed to write to error log ({error_type}: {message}): {log_exc}", file=sys.stderr)


# =============================================================================
# SECTION 5: Template engine
#
# Only Email is mandatory per lead. Known variables (FirstName, LastName,
# CompanyName) get graceful defaults when blank. Any OTHER {{Variable}} is
# resolved directly against a matching Master column of the same name —
# this is how custom variables (e.g. {{Industry}}) work, with no code
# changes needed, as long as you've added that column to Master yourself.
# A blank value (known or custom) renders as nothing, never as literal
# "{{...}}" syntax. A variable that matches NO column at all (a likely
# typo) also renders as nothing, but gets tracked and flagged — see
# render_email's missing_variables return value.
# =============================================================================

PLACEHOLDER_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")

TEMPLATE_VARIABLE_MAP = {
    "FirstName": "FirstName",
    "LastName": "LastName",
    "CompanyName": "Company",
    "Email": "Email",
}

DEFAULT_VALUES = {
    "FirstName": "there",
    "LastName": "",
    "CompanyName": "your team",
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


def render_text(text: str, lead: Dict[str, str], missing_out: Optional[List[str]] = None) -> str:
    def _replace(match):
        var_name = match.group(1)
        if var_name in TEMPLATE_VARIABLE_MAP:
            sheet_col = TEMPLATE_VARIABLE_MAP[var_name]
            value = (lead.get(sheet_col) or "").strip()
            return value if value else DEFAULT_VALUES.get(var_name, "")
        if var_name in lead:
            # A real custom column — just blank for this lead. Normal, not an error.
            return (lead.get(var_name) or "").strip()
        # No column anywhere matches this variable name — almost certainly a
        # typo or a template referencing a field that was never added.
        if missing_out is not None:
            missing_out.append(var_name)
        return ""

    return PLACEHOLDER_RE.sub(_replace, text)


def render_email(templates_dir: str, template_prefix: str, variant: str, lead: Dict[str, str]) -> Dict:
    tmpl = load_template(templates_dir, template_prefix, variant)
    missing: List[str] = []
    subject = render_text(tmpl["subject"], lead, missing_out=missing)
    body = render_text(tmpl["body"], lead, missing_out=missing)

    seen = set()
    deduped_missing = []
    for name in missing:
        if name not in seen:
            seen.add(name)
            deduped_missing.append(name)

    return {"subject": subject, "body": body, "missing_variables": deduped_missing}


# =============================================================================
# SECTION 6: Variant selector
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
# SECTION 7: Eligibility
# =============================================================================

def _parse_dt(value: str):
    if not value:
        return None
    try:
        return dateparser.parse(value)
    except (ValueError, TypeError):
        return None


EMAIL_FORMAT_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def is_valid_email_format(value: str) -> bool:
    return bool(EMAIL_FORMAT_RE.match((value or "").strip()))


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
        if not (lead.get("Email") or "").strip():
            continue  # Email is mandatory
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
            continue

        prev_sent_dt = _parse_dt(prev_sent_raw)
        if prev_sent_dt is None:
            continue

        if now >= prev_sent_dt + timedelta(days=wait_days):
            eligible.append(lead)

    return eligible


# =============================================================================
# SECTION 8: Email accounts — multi-account SMTP/IMAP with App Passwords
# =============================================================================

def load_email_accounts() -> Dict[str, Dict[str, str]]:
    raw = os.environ.get("EMAIL_ACCOUNTS_JSON")
    if not raw:
        raise RuntimeError(
            "EMAIL_ACCOUNTS_JSON env var is not set. It should be a JSON object like "
            '{"sales1": {"address": "sales1@gmail.com", "app_password": "xxxx xxxx xxxx xxxx"}}.'
        )
    accounts = json.loads(raw)
    for name, info in accounts.items():
        if "address" not in info or "app_password" not in info:
            raise RuntimeError(f"Account '{name}' in EMAIL_ACCOUNTS_JSON is missing 'address' or 'app_password'.")
    return accounts


def resolve_sender_account(lead: Dict, campaign_cfg: Dict, accounts: Dict[str, Dict[str, str]]) -> str:
    requested = (lead.get("SenderAccount") or "").strip()
    if requested:
        if requested not in accounts:
            raise MissingSenderAccountError(f"Unknown SenderAccount '{requested}' — not in EMAIL_ACCOUNTS_JSON.")
        return requested

    campaign_default = campaign_cfg.get("default_sender_account", "")
    if campaign_default:
        if campaign_default not in accounts:
            raise MissingSenderAccountError(
                f"Campaign default_sender_account '{campaign_default}' not in EMAIL_ACCOUNTS_JSON.")
        return campaign_default

    global_default = campaign_cfg.get("_global_default_account", "")
    if not global_default:
        raise MissingSenderAccountError("No SenderAccount on the lead, and no default account is configured.")
    if global_default not in accounts:
        raise MissingSenderAccountError(f"Default account '{global_default}' not in EMAIL_ACCOUNTS_JSON.")
    return global_default


# =============================================================================
# SECTION 9: SMTP sending
# =============================================================================

def _build_outbound_message(sender_address: str, to: str, subject: str, body_text: str,
                             in_reply_to: Optional[str] = None, references: Optional[str] = None):
    msg = MIMEText(body_text)
    msg["Subject"] = subject
    msg["From"] = sender_address
    msg["To"] = to
    msg["Date"] = formatdate(localtime=True)
    message_id = make_msgid()
    msg["Message-ID"] = message_id
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    return msg, message_id


def smtp_send(address: str, app_password: str, to: str, subject: str, body_text: str,
              in_reply_to: Optional[str] = None, references: Optional[str] = None) -> Dict[str, str]:
    msg, message_id = _build_outbound_message(address, to, subject, body_text, in_reply_to, references)
    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
        server.login(address, app_password)
        server.sendmail(address, [to], msg.as_string())
    return {"message_id": message_id}


def classify_send_exception(exc: Exception) -> str:
    """Maps any exception raised during the send attempt to one of the
    error-monitoring categories."""
    if isinstance(exc, MissingSenderAccountError):
        return ERR_MISSING_SENDER_ACCOUNT
    if isinstance(exc, InvalidEmailFormatError):
        return ERR_INVALID_EMAIL
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return ERR_AUTH_FAILURE
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return ERR_INVALID_EMAIL
    if isinstance(exc, smtplib.SMTPResponseException):
        code = getattr(exc, "smtp_code", None)
        raw_error = getattr(exc, "smtp_error", b"")
        text = f"{raw_error} {exc}".lower()
        if code in (421, 450, 451, 452, 454) or "rate" in text or "too many" in text or "try again later" in text:
            return ERR_RATE_LIMIT
        return ERR_SEND_FAILURE
    if isinstance(exc, (smtplib.SMTPException, OSError, TimeoutError)):
        return ERR_SEND_FAILURE
    if type(exc).__name__ == "APIError" or "gspread" in type(exc).__module__:
        return ERR_SHEETS_API
    return ERR_SEND_FAILURE


# =============================================================================
# SECTION 10: IMAP reading
# =============================================================================

def _decode_header_value(raw: str) -> str:
    if not raw:
        return ""
    parts = decode_header(raw)
    decoded = []
    for text, encoding in parts:
        if isinstance(text, bytes):
            decoded.append(text.decode(encoding or "utf-8", errors="replace"))
        else:
            decoded.append(text)
    return "".join(decoded)


def _extract_plain_text_body(msg) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and "attachment" not in str(part.get("Content-Disposition", "")):
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    return payload.decode(charset, errors="replace")
        return ""
    if msg.get_content_type() == "text/plain":
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            return payload.decode(charset, errors="replace")
    return ""


def _message_to_dict(msg) -> Dict:
    subject = _decode_header_value(msg.get("Subject", ""))
    from_ = _decode_header_value(msg.get("From", ""))
    message_id = (msg.get("Message-ID", "") or "").strip()
    in_reply_to = (msg.get("In-Reply-To", "") or "").strip()
    references = (msg.get("References", "") or "").strip()
    body = _extract_plain_text_body(msg)
    headers = {k.lower(): v for k, v in msg.items()}
    return {
        "message_id": message_id, "in_reply_to": in_reply_to, "references": references,
        "subject": subject, "from": from_, "headers": headers, "body": body,
        "snippet": body[:200],
    }


def _parse_email_message(raw_bytes: bytes) -> Dict:
    msg = email.message_from_bytes(raw_bytes)
    return _message_to_dict(msg)


def imap_fetch_recent(address: str, app_password: str, since_dt: datetime) -> List[Dict]:
    imap = imaplib.IMAP4_SSL("imap.gmail.com", 993)
    try:
        imap.login(address, app_password)
        imap.select("INBOX", readonly=True)
        date_str = since_dt.strftime("%d-%b-%Y")
        status, data = imap.search(None, f'(SINCE "{date_str}")')
        if status != "OK":
            return []
        ids = data[0].split()
        messages = []
        for num in ids:
            status, msg_data = imap.fetch(num, "(RFC822)")
            if status != "OK" or not msg_data or not msg_data[0]:
                continue
            raw = msg_data[0][1]
            parsed = _parse_email_message(raw)

            msg_date = None
            date_header = parsed["headers"].get("date", "")
            if date_header:
                try:
                    msg_date = parsedate_to_datetime(date_header)
                    if msg_date.tzinfo is not None:
                        msg_date = msg_date.replace(tzinfo=None)
                except (TypeError, ValueError):
                    msg_date = None
            if msg_date is not None and msg_date < since_dt:
                continue

            messages.append(parsed)
        return messages
    finally:
        try:
            imap.logout()
        except Exception:  # noqa: BLE001 - best-effort cleanup
            pass


def classify_imap_exception(exc: Exception) -> str:
    text = str(exc).lower()
    if isinstance(exc, imaplib.IMAP4.error) or "authenticationfailed" in text or "invalid credentials" in text \
            or "login failed" in text:
        return ERR_AUTH_FAILURE
    return ERR_REPLY_CHECK


# =============================================================================
# SECTION 11: Batch building + sending
# =============================================================================

def make_batch_id() -> str:
    return f"BATCH-{datetime.now().strftime('%Y%m%d-%H%M%S')}"


def _compute_next_eligible_at(stages: List[Dict], idx: int, sent_at: datetime) -> str:
    if idx + 1 >= len(stages):
        return ""
    next_wait_days = stages[idx + 1].get("wait_days_after_previous", 0)
    return (sent_at + timedelta(days=next_wait_days)).strftime(DATETIME_FMT)


def _count_sent_today(leads: List[Dict], stages: List[Dict]) -> int:
    today = datetime.now().strftime("%Y-%m-%d")
    count = 0
    for lead in leads:
        for i in range(len(stages)):
            field = stage_field_names(i)["sent_at"]
            if lead.get(field, "").startswith(today):
                count += 1
    return count


def build_batch(campaign_cfg: Dict, leads: List[Dict], stage_name: str, batch_size: int,
                 forced_variant: Optional[str] = None) -> List[Dict]:
    """Computes eligible leads + assigns variants + renders emails WITHOUT
    sending or writing anything. Safe to call repeatedly for preview."""
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

        prior_message_id = lead.get("MessageID", "") if idx > 0 else ""
        prior_references = lead.get("ThreadReferences", "") if idx > 0 else ""
        in_reply_to = prior_message_id or None
        if prior_message_id:
            references = f"{prior_references} {prior_message_id}".strip()
        else:
            references = prior_references or None

        plan.append({
            "lead": lead, "variant": variant,
            "subject": rendered["subject"], "body": rendered["body"],
            "missing_variables": rendered["missing_variables"],
            "in_reply_to": in_reply_to, "references": references,
        })
    return plan


def send_batch(campaign_cfg: Dict, sheets: SheetsConnector, accounts: Dict[str, Dict[str, str]],
               stage_name: str, batch_size: int, forced_variant: Optional[str] = None) -> List[Dict]:
    """Sends one lead at a time, isolating failures per-lead. Every attempt
    (success or failure) is logged to SendLog under one BatchID, and every
    error is also classified and logged to the Error Log."""
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
        lead_id = lead.get("LeadID", "")
        lead_email = lead.get("Email", "")
        now = datetime.now()
        now_str = now.strftime(DATETIME_FMT)
        account_name = ""

        try:
            if not is_valid_email_format(lead_email):
                raise InvalidEmailFormatError(f"'{lead_email}' is not a valid email address format")
            account_name = resolve_sender_account(lead, campaign_cfg, accounts)
            account = accounts[account_name]
            sent = smtp_send(account["address"], account["app_password"], to=lead_email,
                              subject=item["subject"], body_text=item["body"],
                              in_reply_to=item["in_reply_to"], references=item["references"])
        except Exception as exc:  # noqa: BLE001 - isolate per-lead send failures
            error_type = classify_send_exception(exc)
            try:
                sheets.update_lead_fields(row, {"Error": str(exc)[:500]})
                sheets.append_send_log({
                    "BatchID": batch_id, "Timestamp": now_str, "LeadID": lead_id, "Email": lead_email,
                    "Campaign": campaign_name, "Stage": stage_name, "Variant": item["variant"],
                    "SenderAccount": account_name, "Status": "error", "MessageID": "", "Error": str(exc)[:500],
                })
            except Exception:  # noqa: BLE001 - the error log entry below is the durable record either way
                pass
            log_error(sheets, campaign_name, error_type, str(exc), lead_id=lead_id, email_addr=lead_email,
                      stage=stage_name, batch_id=batch_id)
            results.append({"lead_id": lead_id, "email": lead_email, "status": "error",
                             "error": str(exc), "error_type": error_type, "batch_id": batch_id})
        else:
            # Send succeeded — now persist state. If THIS fails, the email
            # already went out but the sheet won't reflect it, which risks
            # a duplicate resend next run. Flagged distinctly so it's not
            # confused with an ordinary send failure.
            try:
                sheets.update_lead_fields(row, {
                    fields["sent_at"]: now_str,
                    fields["variant"]: item["variant"],
                    "CurrentStage": stage_name,
                    "NextEligibleAt": _compute_next_eligible_at(stages, idx, now),
                    "Status": f"{stage_name} Sent",
                    "LastActionAt": now_str,
                    "MessageID": sent["message_id"],
                    "ThreadReferences": item["references"] or "",
                    "SenderAccount": account_name,
                    "Error": "",
                })
                sheets.append_send_log({
                    "BatchID": batch_id, "Timestamp": now_str, "LeadID": lead_id, "Email": lead_email,
                    "Campaign": campaign_name, "Stage": stage_name, "Variant": item["variant"],
                    "SenderAccount": account_name, "Status": "sent", "MessageID": sent["message_id"], "Error": "",
                })
                for var_name in item["missing_variables"]:
                    log_error(sheets, campaign_name, ERR_MISSING_VARIABLE,
                              f"Template variable '{{{{{var_name}}}}}' has no matching Master column — "
                              "rendered blank. Check for a typo, or add that column.",
                              lead_id=lead_id, email_addr=lead_email, stage=stage_name, batch_id=batch_id)
                results.append({"lead_id": lead_id, "email": lead_email, "status": "sent",
                                 "variant": item["variant"], "batch_id": batch_id, "account": account_name})
            except Exception as sheets_exc:  # noqa: BLE001
                log_error(sheets, campaign_name, ERR_SHEETS_API,
                          f"Email sent successfully (Message-ID {sent['message_id']}) but failed to update "
                          f"the sheet: {sheets_exc}. Check manually to avoid a duplicate resend.",
                          lead_id=lead_id, email_addr=lead_email, stage=stage_name, batch_id=batch_id)
                results.append({"lead_id": lead_id, "email": lead_email, "status": "sent_but_sheet_error",
                                 "error": str(sheets_exc), "batch_id": batch_id, "account": account_name})

        if i < len(plan) - 1:
            time.sleep(random.uniform(delay_min * 60, delay_max * 60))

    return results


# =============================================================================
# SECTION 12: Reply monitor
# =============================================================================

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def _extract_email(from_header: str) -> str:
    match = EMAIL_RE.search(from_header or "")
    return match.group(0).lower() if match else ""


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

    auto_submitted = headers.get("auto-submitted", "").lower()
    if auto_submitted and auto_submitted != "no":
        return CLASSIFICATION_AUTOREPLY
    if "x-autoreply" in headers or "x-autorespond" in headers:
        return CLASSIFICATION_AUTOREPLY
    if headers.get("precedence", "").lower() in ("bulk", "auto_reply", "junk"):
        return CLASSIFICATION_AUTOREPLY

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
        return CLASSIFICATION_BOUNCE_HARD

    if any(k in subject_l or k in body_l for k in OOO_KEYWORDS):
        return CLASSIFICATION_OOO

    return CLASSIFICATION_GENUINE


def check_replies(sheets: SheetsConnector, accounts: Dict[str, Dict[str, str]], lookback_hours: int,
                   campaign_name: str = "") -> List[Dict]:
    leads = sheets.get_all_leads()

    by_message_id = {}
    by_email = {}
    for lead in leads:
        mid = (lead.get("MessageID") or "").strip()
        if mid:
            by_message_id.setdefault(mid, lead)
        email_addr = (lead.get("Email") or "").strip().lower()
        if email_addr:
            by_email.setdefault(email_addr, lead)

    already_logged = sheets.get_logged_message_ids()
    since_dt = datetime.now() - timedelta(hours=lookback_hours)

    actions = []
    for account_name, account in accounts.items():
        try:
            messages = imap_fetch_recent(account["address"], account["app_password"], since_dt)
        except Exception as exc:  # noqa: BLE001 - one account's IMAP outage shouldn't block the rest
            error_type = classify_imap_exception(exc)
            print(f"WARNING: IMAP check failed for account '{account_name}': {exc}", file=sys.stderr)
            log_error(sheets, campaign_name, error_type, f"IMAP check failed for account '{account_name}': {exc}")
            continue

        for msg in messages:
            if msg["message_id"] and msg["message_id"] in already_logged:
                continue

            combined_refs = f'{msg.get("in_reply_to", "")} {msg.get("references", "")}'
            matched_lead = None
            match_method = None
            for mid, lead in by_message_id.items():
                if mid and mid in combined_refs:
                    matched_lead = lead
                    match_method = "Header"
                    break
            if matched_lead is None:
                sender_email = _extract_email(msg["from"])
                matched_lead = by_email.get(sender_email)
                if matched_lead is not None:
                    match_method = "Email"
            if matched_lead is None:
                continue

            classification = classify_message(msg["headers"], msg["subject"], msg["body"], msg["from"])
            now_str = datetime.now().strftime(DATETIME_FMT)

            action_taken = "Logged Only"
            master_updates = {"LastInboundClassification": classification, "LastInboundAt": now_str,
                               "LastActionAt": now_str}

            if classification == CLASSIFICATION_GENUINE:
                master_updates.update({"ReplyStatus": "Replied", "ReplyAt": now_str,
                                        "Status": STATUS_STOPPED_REPLIED})
                action_taken = "Stopped Sequence"
            elif classification == CLASSIFICATION_BOUNCE_HARD:
                master_updates["Status"] = STATUS_STOPPED_BOUNCED
                action_taken = "Stopped Sequence"

            try:
                sheets.update_lead_fields(matched_lead["_row"], master_updates)
                sheets.append_response({
                    "ResponseID": msg["message_id"] or f"noid-{now_str}", "LeadID": matched_lead.get("LeadID", ""),
                    "Campaign": campaign_name, "ReceivedAt": now_str, "From": msg["from"], "Subject": msg["subject"],
                    "Snippet": msg["snippet"], "Classification": classification, "MatchMethod": match_method,
                    "MessageID": msg["message_id"], "InReplyTo": msg.get("in_reply_to", ""),
                    "ActionTaken": action_taken,
                })
            except Exception as sheets_exc:  # noqa: BLE001
                log_error(sheets, campaign_name, ERR_SHEETS_API,
                          f"Reply detected but failed to update the sheet: {sheets_exc}",
                          lead_id=matched_lead.get("LeadID", ""), email_addr=matched_lead.get("Email", ""))
                continue

            actions.append({"lead_id": matched_lead.get("LeadID", ""), "email": matched_lead.get("Email", ""),
                             "classification": classification, "action": action_taken,
                             "match_method": match_method, "account": account_name})

    return actions


# =============================================================================
# SECTION 13: Dashboards
#
# Fully recomputed and rewritten every run (not appended to) — a snapshot
# of current state, not a log. "Delivered" is an ESTIMATE (Sent minus
# confirmed hard bounces) since SMTP provides no real delivery receipt;
# labeled as such rather than overclaiming precision.
# "Variant Performance" reply attribution is approximate: a reply is
# attributed to whichever stage/variant was the lead's most recent send
# (CurrentStage) at the time they replied.
# =============================================================================

def _pct(numerator: int, denominator: int) -> str:
    if denominator <= 0:
        return "0.0%"
    return f"{(numerator / denominator) * 100:.1f}%"


def compute_campaign_dashboard(campaign_cfg: Dict, leads: List[Dict], responses: List[Dict],
                                send_log: List[Dict], error_log: List[Dict]) -> List[Tuple[str, str, str]]:
    stages = campaign_cfg["stages"]

    total_leads = sum(1 for l in leads if (l.get("Email") or "").strip())
    contacted = [l for l in leads if any((l.get(stage_field_names(i)["sent_at"]) or "").strip()
                                          for i in range(len(stages)))]
    unique_contacted = len(contacted)

    total_sent = sum(1 for r in send_log if r.get("Status") == "sent")
    bounced_hard = sum(1 for r in responses if r.get("Classification") == CLASSIFICATION_BOUNCE_HARD)
    bounced_soft = sum(1 for r in responses if r.get("Classification") == CLASSIFICATION_BOUNCE_SOFT)
    genuine_replies = sum(1 for r in responses if r.get("Classification") == CLASSIFICATION_GENUINE)
    delivered_est = max(total_sent - bounced_hard, 0)

    last_stage_field = stage_field_names(len(stages) - 1)["sent_at"] if stages else None
    completed = sum(1 for l in leads if last_stage_field and (l.get(last_stage_field) or "").strip()) \
        if last_stage_field else 0

    rows: List[Tuple[str, str, str]] = []
    rows.append(("Overview", "Last Updated", datetime.now().strftime(DATETIME_FMT)))
    rows.append(("Overview", "Total Leads (with Email)", str(total_leads)))
    rows.append(("Overview", "Unique Leads Contacted", str(unique_contacted)))
    rows.append(("Overview", "Total Emails Sent", str(total_sent)))
    rows.append(("Overview", "Delivered (est. = Sent minus Hard Bounces)", str(delivered_est)))
    rows.append(("Overview", "Bounced (Hard)", str(bounced_hard)))
    rows.append(("Overview", "Bounced (Soft)", str(bounced_soft)))
    rows.append(("Overview", "Genuine Replies", str(genuine_replies)))
    rows.append(("Overview", "Reply Rate (Replies / Unique Contacted)", _pct(genuine_replies, unique_contacted)))
    rows.append(("Overview", "Sequence Completion (Reached Final Stage / Unique Contacted)",
                  _pct(completed, unique_contacted)))

    for i, stage in enumerate(stages):
        sent_field = stage_field_names(i)["sent_at"]
        sent_count = sum(1 for l in leads if (l.get(sent_field) or "").strip())
        rows.append(("Per-Stage", f"{stage['name']} - Sent", str(sent_count)))

    accounts_seen = sorted({(l.get("SenderAccount") or "").strip() for l in contacted
                             if (l.get("SenderAccount") or "").strip()})
    for acct in accounts_seen:
        acct_leads = [l for l in contacted if (l.get("SenderAccount") or "").strip() == acct]
        acct_sent = sum(1 for r in send_log if r.get("Status") == "sent" and r.get("SenderAccount") == acct)
        acct_replies = sum(1 for l in acct_leads if l.get("Status") == STATUS_STOPPED_REPLIED)
        rows.append(("Sender Performance", f"{acct} - Sent", str(acct_sent)))
        rows.append(("Sender Performance", f"{acct} - Replies", str(acct_replies)))
        rows.append(("Sender Performance", f"{acct} - Reply Rate", _pct(acct_replies, len(acct_leads))))

    variant_sent_counts: Dict[str, int] = {}
    for i, stage in enumerate(stages):
        sent_field = stage_field_names(i)["sent_at"]
        variant_field = stage_field_names(i)["variant"]
        for l in leads:
            if (l.get(sent_field) or "").strip():
                v = (l.get(variant_field) or "").strip()
                if v:
                    key = f"{stage['name']}-{v}"
                    variant_sent_counts[key] = variant_sent_counts.get(key, 0) + 1

    variant_reply_counts: Dict[str, int] = {}
    stage_name_to_index = {s["name"]: i for i, s in enumerate(stages)}
    for l in leads:
        if l.get("Status") == STATUS_STOPPED_REPLIED:
            current_stage = l.get("CurrentStage", "")
            idx = stage_name_to_index.get(current_stage)
            if idx is not None:
                v = (l.get(stage_field_names(idx)["variant"]) or "").strip()
                if v:
                    key = f"{current_stage}-{v}"
                    variant_reply_counts[key] = variant_reply_counts.get(key, 0) + 1

    for key in sorted(variant_sent_counts.keys()):
        sent_n = variant_sent_counts[key]
        reply_n = variant_reply_counts.get(key, 0)
        rows.append(("Variant Performance", f"{key} - Sent", str(sent_n)))
        rows.append(("Variant Performance", f"{key} - Replies (approx.)", str(reply_n)))
        rows.append(("Variant Performance", f"{key} - Reply Rate (approx.)", _pct(reply_n, sent_n)))

    error_counts: Dict[str, int] = {}
    for e in error_log:
        et = e.get("ErrorType", "Unknown")
        error_counts[et] = error_counts.get(et, 0) + 1
    for et in sorted(error_counts.keys()):
        rows.append(("Errors (All Time)", et, str(error_counts[et])))

    recent_errors = error_log[-10:] if error_log else []
    for e in recent_errors:
        label = f"{e.get('Timestamp', '')} - {e.get('ErrorType', '')}"
        rows.append(("Recent Errors", label, str(e.get("Message", ""))[:200]))

    return rows


def compute_all_campaigns_row(campaign_name: str, leads: List[Dict], responses: List[Dict],
                               send_log: List[Dict], stages: List[Dict]) -> List[str]:
    total_leads = sum(1 for l in leads if (l.get("Email") or "").strip())
    contacted = sum(1 for l in leads if any((l.get(stage_field_names(i)["sent_at"]) or "").strip()
                                             for i in range(len(stages))))
    total_sent = sum(1 for r in send_log if r.get("Status") == "sent")
    bounced_hard = sum(1 for r in responses if r.get("Classification") == CLASSIFICATION_BOUNCE_HARD)
    bounced_soft = sum(1 for r in responses if r.get("Classification") == CLASSIFICATION_BOUNCE_SOFT)
    replies = sum(1 for r in responses if r.get("Classification") == CLASSIFICATION_GENUINE)
    delivered_est = max(total_sent - bounced_hard, 0)
    last_stage_field = stage_field_names(len(stages) - 1)["sent_at"] if stages else None
    completed = sum(1 for l in leads if last_stage_field and (l.get(last_stage_field) or "").strip()) \
        if last_stage_field else 0

    return [
        campaign_name, str(total_leads), str(contacted), str(total_sent), str(delivered_est),
        str(bounced_hard), str(bounced_soft), str(replies), _pct(replies, contacted), _pct(completed, contacted),
    ]


def write_dashboard(dashboard_ws, rows: List[Tuple[str, str, str]]) -> None:
    dashboard_ws.clear()
    values = [DASHBOARD_COLUMNS] + [list(r) for r in rows]
    dashboard_ws.update(values, "A1")


def write_all_campaigns_dashboard(ws, campaign_rows: List[List[str]]) -> None:
    ws.clear()
    values = [ALL_CAMPAIGNS_DASHBOARD_COLUMNS] + campaign_rows
    ws.update(values, "A1")


# =============================================================================
# SECTION 14: Main CLI commands
# =============================================================================

def _connect_sheets(campaign_cfg) -> SheetsConnector:
    return SheetsConnector(
        sheet_id=campaign_cfg["sheet_id"],
        master_tab=campaign_cfg["master_tab"],
        responses_tab=campaign_cfg["responses_tab"],
        send_log_tab=campaign_cfg["send_log_tab"],
        error_log_tab=campaign_cfg["error_log_tab"],
        dashboard_tab=campaign_cfg["dashboard_tab"],
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
        if not is_valid_email_format(lead.get("Email", "")):
            print("          WARNING: this email address doesn't look correctly formatted.")
        print(f"Variant:  {item['variant']}")
        print(f"Subject:  {item['subject']}")
        print("-" * 70)
        print(item["body"])
        if item["missing_variables"]:
            print(f"\nWARNING: unrecognized template variable(s), rendered blank: "
                  f"{', '.join('{{' + v + '}}' for v in item['missing_variables'])}")
    print("=" * 70)
    print("\nNothing has been sent. Re-run with the 'send' command to actually send this batch.")


def cmd_send(args):
    campaign_cfg = get_campaign(args.campaign)
    sheets = _connect_sheets(campaign_cfg)
    accounts = load_email_accounts()

    forced_variant = None if args.variant in (None, "Auto") else args.variant
    results = send_batch(campaign_cfg, sheets, accounts, args.stage, args.batch_size, forced_variant=forced_variant)

    if not results:
        print(f"No eligible leads to send for stage '{args.stage}' "
              "(none eligible, or today's sending limit already reached).")
        return

    batch_id = results[0].get("batch_id", "")
    sent = [r for r in results if r["status"] == "sent"]
    sent_but_sheet_error = [r for r in results if r["status"] == "sent_but_sheet_error"]
    errors = [r for r in results if r["status"] == "error"]

    print(f"Batch ID: {batch_id}")
    print(f"Sent {len(sent)} email(s), {len(sent_but_sheet_error)} sent-but-sheet-error, "
          f"{len(errors)} error(s).\n")
    for r in sent:
        print(f"  OK    {r['email']} (variant {r['variant']}, account {r['account']})")
    for r in sent_but_sheet_error:
        print(f"  WARN  {r['email']}: email sent, but sheet update failed ({r['error']}) — check manually")
    for r in errors:
        print(f"  ERROR {r['email']}: [{r['error_type']}] {r['error']}")


def cmd_check_replies(args):
    campaign_cfg = get_campaign(args.campaign)
    sheets = _connect_sheets(campaign_cfg)
    accounts = load_email_accounts()
    lookback_hours = campaign_cfg.get("reply_monitor", {}).get("lookback_hours", 24)

    actions = check_replies(sheets, accounts, lookback_hours, campaign_name=args.campaign)
    if not actions:
        print("No new inbound messages matched to a lead.")
        return

    print(f"Processed {len(actions)} inbound message(s):\n")
    for a in actions:
        print(f"  {a['email']:<35} {a['classification']:<18} ({a['match_method']:<6}, {a['account']}) -> {a['action']}")


def cmd_dashboard(args):
    if not args.all and not args.campaign:
        print("Specify --campaign NAME or --all", file=sys.stderr)
        sys.exit(1)

    if args.all:
        data = load_config()
        shared_sheet_id = data.get("shared_sheet_id", "")
        campaign_names = list(data["campaigns"].keys())
        all_rows = []
        for name in campaign_names:
            campaign_cfg = get_campaign(name)
            sheets = _connect_sheets(campaign_cfg)
            leads = sheets.get_all_leads()
            responses = sheets.get_all_responses()
            send_log = sheets.get_all_send_log()
            error_log = sheets.get_all_error_log()
            rows = compute_campaign_dashboard(campaign_cfg, leads, responses, send_log, error_log)
            write_dashboard(sheets.dashboard_ws, rows)
            all_rows.append(compute_all_campaigns_row(name, leads, responses, send_log, campaign_cfg["stages"]))
            print(f"Updated dashboard for '{name}'")
        if shared_sheet_id and all_rows:
            ws = get_all_campaigns_dashboard_ws(shared_sheet_id)
            write_all_campaigns_dashboard(ws, all_rows)
            print("Updated combined 'All Campaigns Dashboard'")
    else:
        campaign_cfg = get_campaign(args.campaign)
        sheets = _connect_sheets(campaign_cfg)
        leads = sheets.get_all_leads()
        responses = sheets.get_all_responses()
        send_log = sheets.get_all_send_log()
        error_log = sheets.get_all_error_log()
        rows = compute_campaign_dashboard(campaign_cfg, leads, responses, send_log, error_log)
        write_dashboard(sheets.dashboard_ws, rows)
        print(f"Updated dashboard for '{args.campaign}'")


# =============================================================================
# SECTION 15: Argument parsing / entry point
# =============================================================================

def main():
    parser = argparse.ArgumentParser(description="Outreach automation (SMTP/IMAP, single-file)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_preview = sub.add_parser("preview", help="Show what would be sent, without sending")
    p_preview.add_argument("--campaign", required=True)
    p_preview.add_argument("--stage", required=True)
    p_preview.add_argument("--batch-size", type=int, required=True)
    p_preview.add_argument("--variant", default="Auto",
                            help="Force a specific variant letter for the whole batch. Default: Auto")
    p_preview.set_defaults(func=cmd_preview)

    p_send = sub.add_parser("send", help="Actually send a batch")
    p_send.add_argument("--campaign", required=True)
    p_send.add_argument("--stage", required=True)
    p_send.add_argument("--batch-size", type=int, required=True)
    p_send.add_argument("--variant", default="Auto",
                         help="Force a specific variant letter for the whole batch. Default: Auto")
    p_send.set_defaults(func=cmd_send)

    p_replies = sub.add_parser("check-replies", help="Check all configured inboxes for new replies/bounces")
    p_replies.add_argument("--campaign", required=True)
    p_replies.set_defaults(func=cmd_check_replies)

    p_dash = sub.add_parser("dashboard", help="Recompute and write the dashboard tab(s)")
    p_dash.add_argument("--campaign", help="Campaign name (omit if using --all)")
    p_dash.add_argument("--all", action="store_true",
                         help="Update dashboards for every configured campaign, plus the combined "
                              "All Campaigns Dashboard")
    p_dash.set_defaults(func=cmd_dashboard)

    args = parser.parse_args()
    try:
        args.func(args)
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
