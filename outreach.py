#!/usr/bin/env python3
"""
Outreach Automation — single-file version, SMTP/IMAP edition.

Everything runs from GitHub Actions. There is no local-run step anywhere
in this design — not even a one-time browser OAuth flow. Authentication is
Gmail App Passwords (generated once on a Google webpage, pasted into a
GitHub secret), not OAuth.

Sends over SMTP (smtp.gmail.com:465, SSL), reads replies over IMAP
(imap.gmail.com:993, SSL). Built for multiple sending accounts from the
start: each lead can specify which account to send from (Master sheet
column `SenderAccount`), falling back to a configured default.

Usage:
    python outreach.py preview        --campaign NAME --stage NAME --batch-size N [--variant A]
    python outreach.py send           --campaign NAME --stage NAME --batch-size N [--variant A]
    python outreach.py check-replies  --campaign NAME

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
from typing import Dict, List, Optional

import yaml
from dateutil import parser as dateparser


# =============================================================================
# SECTION 1: Constants — sheet column names, single source of truth
# =============================================================================

MASTER_COLUMNS = [
    "LeadID",
    "FirstName",          # Optional — blank renders as "there" in templates
    "LastName",            # Optional
    "Email",                # MANDATORY — the only required field per lead
    "Company",              # Optional — blank renders as "your team"
    "Campaign",
    "Approval",             # Pending | Yes | No | Paused (blank behaves as Pending)
    "SenderAccount",        # Optional — which account (from EMAIL_ACCOUNTS_JSON) to
                             # send from. Blank = use the configured default. Once a
                             # lead's first send resolves an account, it's written
                             # back here so every later stage reuses the SAME
                             # account (so the recipient sees one consistent sender).
    "RequestedAction",      # Free-text, NOT read by the system. A place for you to
                             # note intent before triggering the actual workflow.
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
    "NextEligibleAt",       # Computed after each send: when eligible for next stage
    "ReplyStatus",          # "" | Replied
    "ReplyAt",
    "LastInboundClassification",
    "LastInboundAt",
    "Status",                # Doubles as "Last Action": Pending | Intro Sent | ... |
                              # Stopped - Replied | Stopped - Bounced |
                              # Stopped - Rejected | Paused | Completed
    "LastActionAt",          # Timestamp companion to Status
    "Error",
    "MessageID",              # RFC 2822 Message-ID of the most recent send
    "ThreadReferences",       # Accumulated References header chain, for proper
                               # email threading without depending on any one
                               # provider's proprietary thread-ID concept
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
    "MatchMethod",       # Header | Email — how this message was matched to a lead.
                          # Header (In-Reply-To/References matched a Message-ID we
                          # sent) is the stronger signal, checked first.
    "MessageID",
    "InReplyTo",
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
    "SenderAccount",
    "Status",            # sent | error
    "MessageID",
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
#
# One shared Google Sheet across all campaigns. Each campaign gets its own
# three tabs (Master/Responses/SendLog), auto-named "<campaign>_Master" etc.
# and auto-CREATED the first time that campaign is used — adding a new
# campaign is just adding a YAML block, nothing to set up by hand.
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
    cfg["master_tab"] = cfg.get("master_tab") or f"{campaign_name}_Master"
    cfg["responses_tab"] = cfg.get("responses_tab") or f"{campaign_name}_Responses"
    cfg["send_log_tab"] = cfg.get("send_log_tab") or f"{campaign_name}_SendLog"
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
# SECTION 3: Google Sheets connector (Master + Responses + SendLog tabs)
#
# Unaffected by the email-transport change below — still a service account
# (fully headless, no browser step ever, since day one).
# =============================================================================

SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


class SheetsConnector:
    def __init__(self, sheet_id: str, master_tab: str, responses_tab: str, send_log_tab: str):
        import gspread
        from google.oauth2.service_account import Credentials

        self.sheet_id = sheet_id
        self._gspread = gspread

        raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
        if not raw:
            raise RuntimeError(
                "GOOGLE_SERVICE_ACCOUNT_JSON env var is not set. "
                "Put the full service account key JSON there (as a GitHub secret)."
            )
        info = json.loads(raw)
        creds = Credentials.from_service_account_info(info, scopes=SHEETS_SCOPES)
        self._client = gspread.authorize(creds)
        self._spreadsheet = self._client.open_by_key(sheet_id)

        # Auto-create every tab this campaign needs, the first time it runs.
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
                    "Fix the sheet header manually, or delete the tab so it gets "
                    "recreated automatically on the next run."
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
#
# Only Email is mandatory per lead. Every other field renders gracefully
# with a sensible default when blank, rather than leaking a literal
# "{{FirstName}}" into an outgoing email.
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


def render_text(text: str, lead: Dict[str, str]) -> str:
    def _replace(match):
        var_name = match.group(1)
        sheet_col = TEMPLATE_VARIABLE_MAP.get(var_name)
        if sheet_col is None:
            return match.group(0)  # unknown variable — leave as-is
        value = (lead.get(sheet_col) or "").strip()
        if value:
            return value
        return DEFAULT_VALUES.get(var_name, "")

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
#
# Email is the only mandatory field. A lead with no email address can never
# be sent to, so it's filtered out here rather than causing a send failure
# later.
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
        if not (lead.get("Email") or "").strip():
            continue  # Email is mandatory — nothing else can compensate for it
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
# SECTION 7: Email accounts — multi-account SMTP/IMAP with App Passwords
#
# No OAuth, no client_secret.json, no browser flow, no refresh tokens.
# Every account's {address, app_password} lives in ONE GitHub secret
# (EMAIL_ACCOUNTS_JSON), so adding accounts never means touching workflow
# YAML or adding new secrets one at a time.
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
    """Priority: the lead's own SenderAccount cell (locked in after first send)
    > the campaign's default_sender_account > the global default_account.
    Raises ValueError (caught per-lead by send_batch) if nothing resolves or
    the resolved name isn't a configured account."""
    requested = (lead.get("SenderAccount") or "").strip()
    if requested:
        if requested not in accounts:
            raise ValueError(f"Unknown SenderAccount '{requested}' — not in EMAIL_ACCOUNTS_JSON.")
        return requested

    campaign_default = campaign_cfg.get("default_sender_account", "")
    if campaign_default:
        if campaign_default not in accounts:
            raise ValueError(f"Campaign default_sender_account '{campaign_default}' not in EMAIL_ACCOUNTS_JSON.")
        return campaign_default

    global_default = campaign_cfg.get("_global_default_account", "")
    if not global_default:
        raise ValueError("No SenderAccount on the lead, and no default account is configured.")
    if global_default not in accounts:
        raise ValueError(f"Default account '{global_default}' not in EMAIL_ACCOUNTS_JSON.")
    return global_default


# =============================================================================
# SECTION 8: SMTP sending
#
# Message construction (_build_outbound_message) is a pure function, kept
# separate from the actual network call (smtp_send), so headers/threading
# logic is fully unit-testable without touching a real SMTP server.
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


# =============================================================================
# SECTION 9: IMAP reading
#
# Same split: _message_to_dict (pure, parses an already-fetched message) is
# unit-tested directly; imap_fetch_recent (the actual network call) is not,
# same as the SMTP side above.
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
    """Fetches inbox messages from since_dt onward. IMAP's SEARCH SINCE is
    date-granularity only (not time), so this may return a few extra older
    messages from the same calendar day — those get filtered here by their
    actual Date header, and Message-ID-based dedupe in check_replies() is
    the real safeguard against reprocessing anything twice."""
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


# =============================================================================
# SECTION 10: Batch building + sending
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
    """Computes eligible leads + assigns variants WITHOUT sending or writing
    anything. Safe to call repeatedly for preview purposes.

    forced_variant: if given (e.g. "A"), every lead in the batch gets this
    variant instead of the balanced auto-rotation.
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

        # Thread continuation: for follow-ups, reply within the RFC 2822
        # thread started at Intro (In-Reply-To / References), rather than
        # starting a fresh, disconnected email each time.
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
            "in_reply_to": in_reply_to, "references": references,
        })
    return plan


def send_batch(campaign_cfg: Dict, sheets: SheetsConnector, accounts: Dict[str, Dict[str, str]],
               stage_name: str, batch_size: int, forced_variant: Optional[str] = None) -> List[Dict]:
    """Re-fetches leads fresh, then sends one-by-one with jittered delay and
    per-lead error isolation. Every send (success or failure) is logged to
    the SendLog tab under a single BatchID for this run."""
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
        account_name = ""
        try:
            account_name = resolve_sender_account(lead, campaign_cfg, accounts)
            account = accounts[account_name]
            sent = smtp_send(account["address"], account["app_password"], to=lead["Email"],
                              subject=item["subject"], body_text=item["body"],
                              in_reply_to=item["in_reply_to"], references=item["references"])
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
                "BatchID": batch_id, "Timestamp": now_str, "LeadID": lead.get("LeadID", ""),
                "Email": lead.get("Email", ""), "Campaign": campaign_name, "Stage": stage_name,
                "Variant": item["variant"], "SenderAccount": account_name, "Status": "sent",
                "MessageID": sent["message_id"], "Error": "",
            })
            results.append({"lead_id": lead.get("LeadID"), "email": lead.get("Email"),
                             "status": "sent", "variant": item["variant"], "batch_id": batch_id,
                             "account": account_name})
        except Exception as exc:  # noqa: BLE001 - isolate per-lead failures
            sheets.update_lead_fields(row, {"Error": str(exc)[:500]})
            sheets.append_send_log({
                "BatchID": batch_id, "Timestamp": now_str, "LeadID": lead.get("LeadID", ""),
                "Email": lead.get("Email", ""), "Campaign": campaign_name, "Stage": stage_name,
                "Variant": item["variant"], "SenderAccount": account_name, "Status": "error",
                "MessageID": "", "Error": str(exc)[:500],
            })
            results.append({"lead_id": lead.get("LeadID"), "email": lead.get("Email"),
                             "status": "error", "error": str(exc), "batch_id": batch_id})

        if i < len(plan) - 1:
            time.sleep(random.uniform(delay_min * 60, delay_max * 60))

    return results


# =============================================================================
# SECTION 11: Reply monitor — checks every configured account's inbox
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

    # Message-ID (via In-Reply-To/References) is the strong match — checked
    # first. Email address is the fallback for whenever no header match is
    # found (e.g. a reply to a much older message than the one on file).
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
            print(f"WARNING: IMAP check failed for account '{account_name}': {exc}", file=sys.stderr)
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
                continue  # not from a known lead, by either signal

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

            sheets.update_lead_fields(matched_lead["_row"], master_updates)
            sheets.append_response({
                "ResponseID": msg["message_id"] or f"noid-{now_str}", "LeadID": matched_lead.get("LeadID", ""),
                "Campaign": campaign_name, "ReceivedAt": now_str, "From": msg["from"], "Subject": msg["subject"],
                "Snippet": msg["snippet"], "Classification": classification, "MatchMethod": match_method,
                "MessageID": msg["message_id"], "InReplyTo": msg.get("in_reply_to", ""),
                "ActionTaken": action_taken,
            })
            actions.append({"lead_id": matched_lead.get("LeadID", ""), "email": matched_lead.get("Email", ""),
                             "classification": classification, "action": action_taken,
                             "match_method": match_method, "account": account_name})

    return actions


# =============================================================================
# SECTION 12: Main CLI commands (preview, send, check-replies)
# =============================================================================

def _connect_sheets(campaign_cfg) -> SheetsConnector:
    return SheetsConnector(
        sheet_id=campaign_cfg["sheet_id"],
        master_tab=campaign_cfg["master_tab"],
        responses_tab=campaign_cfg["responses_tab"],
        send_log_tab=campaign_cfg["send_log_tab"],
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
    accounts = load_email_accounts()

    forced_variant = None if args.variant in (None, "Auto") else args.variant
    results = send_batch(campaign_cfg, sheets, accounts, args.stage, args.batch_size, forced_variant=forced_variant)

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
        print(f"  OK    {r['email']} (variant {r['variant']}, account {r['account']})")
    for r in errors:
        print(f"  ERROR {r['email']}: {r['error']}")


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


# =============================================================================
# SECTION 13: Argument parsing / entry point
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

    args = parser.parse_args()
    try:
        args.func(args)
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
