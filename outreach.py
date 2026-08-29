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
import concurrent.futures
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
    "ThreadSubject",           # Set automatically whenever a stage sends with a
                                # non-blank Subject. Leave a template's Subject
                                # line blank to continue this same thread
                                # ("Re: <ThreadSubject>") instead of starting a
                                # new one — see render_email / Section 5.
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
    "ActionTaken",       # Stopped Sequence | Logged Only |
                         # Logged Only (Unverified Match) | Logged Only (Predates Contact)
                         # — only a Header-matched message can produce "Stopped Sequence"
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
    "Status",            # sent | error | skipped
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
STATUS_REMOVED = "Removed"  # soft-remove from the Data tab — never a hard delete,
                            # see import_leads/update_lead_statuses docstrings

TERMINAL_STATUSES = {
    STATUS_STOPPED_REPLIED,
    STATUS_STOPPED_BOUNCED,
    STATUS_STOPPED_REJECTED,
    STATUS_PAUSED,
    STATUS_COMPLETED,
    STATUS_REMOVED,
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
ERR_SENDER_CAPACITY = "Sender Capacity Reached"

DATETIME_FMT = "%Y-%m-%d %H:%M:%S"


def stage_field_names(index: int) -> dict:
    """index 0 -> Intro fields, index 1 -> FollowUp1 fields, etc."""
    prefix = "Intro" if index == 0 else f"FollowUp{index}"
    return {"sent_at": f"{prefix}SentAt", "variant": f"{prefix}Variant"}


class MissingSenderAccountError(ValueError):
    pass


class InvalidEmailFormatError(ValueError):
    pass


class SenderCapacityReachedError(ValueError):
    """Raised when every account eligible to send to a given lead (whether
    chosen manually, via rotation, or via the single default) has already
    hit its per-account daily limit. Not a fault — the lead is deferred to
    a later run, not treated as failed."""
    pass


# =============================================================================
# SECTION 2: Config loading — template-folder discovery, no central list
#
# A campaign "exists" the moment templates/<name>/ exists — nothing needs
# to be registered anywhere just to make a campaign name recognized. Global
# settings (shared sheet, default account, and the DEFAULT campaign
# settings every campaign inherits) live in config/settings.yaml. A
# campaign only needs its own config/campaigns/<name>.yaml if it wants to
# override something from the defaults — most campaigns need nothing there
# at all.
# =============================================================================

class ConfigError(Exception):
    pass


class CampaignPausedError(RuntimeError):
    """Raised by send_batch when campaign_cfg["status"] == "paused" —
    never raised by build_batch/preview, which stay usable regardless of
    pause state so a paused campaign can still be reviewed."""
    pass


def load_settings(path: str = "config/settings.yaml") -> dict:
    if not os.path.exists(path):
        raise ConfigError(f"Settings file not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if "shared_sheet_id" not in data:
        raise ConfigError(f"{path} is missing 'shared_sheet_id'.")
    if "default_campaign_settings" not in data:
        raise ConfigError(f"{path} is missing 'default_campaign_settings'.")
    return data


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merges override into a COPY of base. Nested dicts merge
    key by key (so e.g. an override can set just sending.daily_limit
    without redefining the whole sending block); anything else — including
    lists like stages/variants — is replaced wholesale by the override,
    since merging a list element-by-element isn't meaningful here."""
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def discover_campaign_names(templates_root: str = "templates") -> List[str]:
    """Every campaign that currently exists, full stop — determined purely
    by having a subfolder under templates/. This is what dashboard --all
    iterates over; there is no other list of "configured campaigns"."""
    if not os.path.isdir(templates_root):
        return []
    return sorted(
        name for name in os.listdir(templates_root)
        if os.path.isdir(os.path.join(templates_root, name)) and not name.startswith(".")
    )


# The only fixed, canonical stage order the system knows about. A campaign
# never needs all five — auto-discovery below stops at the first stage
# with no template files at all, so 1 stage is just as valid as 5.
CANONICAL_STAGE_ORDER = ["intro", "followup1", "followup2", "followup3", "followup4"]
ALL_VARIANT_LETTERS = ["A", "B", "C", "D"]


def discover_stages_and_variants(templates_dir: str, stage_wait_days: Dict[str, int]) -> Tuple[List[Dict], List[str]]:
    """Auto-detects a campaign's stage sequence and variant set purely from
    which template files exist — no YAML declaration required. Minimum is
    1 stage + 1 variant (just intro_A.txt); maximum is 5 stages x A-D.

    Two things keep this safe rather than silently permissive:
    - Stages must be CONTIGUOUS from Intro — a gap (e.g. intro + followup2
      but no followup1 files) is a configuration error, not "skip a stage".
    - Every included stage must offer the EXACT SAME variant letters as
      Intro. A campaign with fewer variants entirely (e.g. just A) is
      fine; a LATER stage quietly missing ONE variant that an earlier
      stage has is almost always an accidental missing file, not an
      intentional design, so it's rejected with a clear message instead
      of silently shrinking just that stage.
    """
    if not os.path.isdir(templates_dir):
        raise ConfigError(f"Templates directory not found: {templates_dir}")

    stages: List[Dict] = []
    canonical_variants: Optional[List[str]] = None

    for prefix in CANONICAL_STAGE_ORDER:
        found_variants = [v for v in ALL_VARIANT_LETTERS
                           if os.path.exists(os.path.join(templates_dir, f"{prefix}_{v}.txt"))]
        if not found_variants:
            break  # first gap — stages must be contiguous, stop here

        if canonical_variants is None:
            canonical_variants = found_variants
        elif found_variants != canonical_variants:
            missing = sorted(set(canonical_variants) - set(found_variants))
            extra = sorted(set(found_variants) - set(canonical_variants))
            problems = []
            if missing:
                problems.append(f"missing variant(s) {missing} (present in '{stages[0]['name']}')")
            if extra:
                problems.append(f"has extra variant(s) {extra} not present in '{stages[0]['name']}'")
            raise ConfigError(
                f"Inconsistent variants for stage '{prefix}' in {templates_dir}: {'; '.join(problems)}. "
                "Every stage must offer the same variant letters — or specify 'stages' and 'variants' "
                "explicitly together in this campaign's override file if that's genuinely intentional."
            )

        stages.append({
            "name": prefix, "template_prefix": prefix,
            "wait_days_after_previous": stage_wait_days.get(prefix, 0),
        })

    if not stages:
        raise ConfigError(
            f"No template files found in {templates_dir}. Expected at least 'intro_A.txt' "
            "(or another variant letter)."
        )

    return stages, canonical_variants


def get_campaign(campaign_name: str, settings_path: str = "config/settings.yaml",
                  campaigns_dir: str = "config/campaigns", templates_root: str = "templates") -> dict:
    settings = load_settings(settings_path)
    shared_sheet_id = settings.get("shared_sheet_id", "")

    # The actual safety gate: no templates folder, no campaign — regardless
    # of what name was typed into a workflow input.
    templates_dir = os.path.join(templates_root, campaign_name)
    if not os.path.isdir(templates_dir):
        raise ConfigError(
            f"No templates found for campaign '{campaign_name}' — expected a folder at "
            f"'{templates_dir}'. Create it with your template files before running this campaign. "
            f"Currently available campaigns: {', '.join(discover_campaign_names(templates_root)) or '(none)'}"
        )

    default_settings = dict(settings.get("default_campaign_settings", {}))
    stage_wait_days = default_settings.pop("stage_wait_days", {})
    cfg = default_settings  # sending, reply_monitor — genuinely shared defaults

    override = {}
    override_path = os.path.join(campaigns_dir, f"{campaign_name}.yaml")
    if os.path.exists(override_path):
        with open(override_path, "r", encoding="utf-8") as f:
            override = yaml.safe_load(f) or {}
        non_shape_override = {k: v for k, v in override.items() if k not in ("stages", "variants")}
        cfg = _deep_merge(cfg, non_shape_override)

    has_stages = "stages" in override
    has_variants = "variants" in override
    if has_stages != has_variants:
        raise ConfigError(
            f"Campaign '{campaign_name}' override specifies only one of 'stages'/'variants' — "
            "specify both together explicitly, or neither to auto-discover from template files."
        )

    if has_stages and has_variants:
        # Explicit declaration — the strict path: every implied file MUST exist.
        cfg["stages"] = override["stages"]
        cfg["variants"] = override["variants"]
        cfg["templates_dir"] = cfg.get("templates_dir") or templates_dir
        _validate_templates_exist(campaign_name, cfg)
    else:
        # No explicit shape — auto-discover from whatever's actually there.
        discovered_stages, discovered_variants = discover_stages_and_variants(templates_dir, stage_wait_days)
        cfg["stages"] = discovered_stages
        cfg["variants"] = discovered_variants
        cfg["templates_dir"] = cfg.get("templates_dir") or templates_dir

    cfg["sheet_id"] = cfg.get("sheet_id") or shared_sheet_id
    cfg["master_tab"] = cfg.get("master_tab") or f"{campaign_name} Master Sheet"
    cfg["responses_tab"] = cfg.get("responses_tab") or f"{campaign_name} Response Sheet"
    cfg["send_log_tab"] = cfg.get("send_log_tab") or f"{campaign_name} Custom Log Sheet"
    cfg["error_log_tab"] = cfg.get("error_log_tab") or f"{campaign_name} Error Log"
    cfg["dashboard_tab"] = cfg.get("dashboard_tab") or f"{campaign_name} Dashboard"
    # "active" preserves every campaign's actual current behavior before
    # this field existed — only a NEW campaign explicitly created as
    # "draft" (via the Streamlit New Campaign flow) should ever start
    # anywhere other than active. Never default to "draft" here; that
    # would silently pause every pre-existing campaign the moment this
    # ships.
    cfg["status"] = cfg.get("status") or "active"
    cfg["_campaign_name"] = campaign_name
    cfg["_global_default_account"] = (settings.get("email_accounts") or {}).get("default_account", "")

    _validate_campaign(campaign_name, cfg)
    return cfg


def _validate_campaign(name: str, cfg: dict) -> None:
    required = ["templates_dir", "stages", "variants", "sending"]
    for key in required:
        if key not in cfg:
            raise ConfigError(f"Campaign '{name}' is missing required key '{key}'")

    if not cfg.get("sheet_id") or str(cfg["sheet_id"]).startswith("PUT_YOUR"):
        raise ConfigError(
            f"Campaign '{name}': no sheet_id resolved. Set 'shared_sheet_id' in "
            "config/settings.yaml (or 'sheet_id' in this campaign's override file)."
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

    if "per_account_daily_limit" in sending and sending["per_account_daily_limit"] is not None:
        limit = sending["per_account_daily_limit"]
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            raise ConfigError(
                f"Campaign '{name}': sending.per_account_daily_limit must be a positive integer, "
                f"got {limit!r}."
            )
    if "sender_rotation" in sending and not isinstance(sending["sender_rotation"], bool):
        raise ConfigError(f"Campaign '{name}': sending.sender_rotation must be true or false.")
    if "rotation_accounts" in sending and sending["rotation_accounts"] is not None:
        if not isinstance(sending["rotation_accounts"], list) or not sending["rotation_accounts"]:
            raise ConfigError(
                f"Campaign '{name}': sending.rotation_accounts must be a non-empty list of account "
                "names, or omitted entirely to rotate across all EMAIL_ACCOUNTS_JSON accounts."
            )


def _validate_templates_exist(name: str, cfg: dict) -> None:
    """Deliberately does NOT infer stages/variants from whatever template
    files happen to exist — that would let a silently-missing file quietly
    shrink a campaign's sequence. Instead: the configured stages/variants
    are the source of truth, and every file they imply must actually be
    present, or this fails loudly before any send is attempted."""
    missing = []
    for stage in cfg["stages"]:
        for variant in cfg["variants"]:
            path = os.path.join(cfg["templates_dir"], f"{stage['template_prefix']}_{variant}.txt")
            if not os.path.exists(path):
                missing.append(path)
    if missing:
        listing = "\n".join(f"  - {p}" for p in missing)
        raise ConfigError(
            f"Campaign '{name}' is missing {len(missing)} template file(s):\n{listing}\n"
            "Add these files, or adjust this campaign's stages/variants."
        )


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

    def append_lead(self, fields: Dict[str, str]) -> None:
        """Appends ONE new row. Unlike update_lead_fields, this is NOT
        restricted to MASTER_COLUMNS — it builds the row against whatever
        the sheet's ACTUAL header row currently is, so any custom trailing
        columns (Title, Website, LinkedIn, ...) get filled in correctly
        too. Any field not present in `fields` is left blank in that
        column, not an error — a CSV import commonly won't map every
        column."""
        header = self.master_ws.row_values(1)
        row = [str(fields.get(col, "")) for col in header]
        self.master_ws.append_row(row, value_input_option="RAW")

    def update_lead_statuses(self, row_numbers_to_status: Dict[int, str]) -> None:
        """Bulk status update (e.g. soft-remove) — one batch_update call
        covering every row, not one API call per row. Always stamps
        LastActionAt alongside Status, matching every other status write
        in this file."""
        gspread = self._gspread
        now_str = datetime.now().strftime(DATETIME_FMT)
        status_col = MASTER_COLUMNS.index("Status") + 1
        last_action_col = MASTER_COLUMNS.index("LastActionAt") + 1
        updates = []
        for row_number, status in row_numbers_to_status.items():
            updates.append({"range": gspread.utils.rowcol_to_a1(row_number, status_col), "values": [[status]]})
            updates.append({"range": gspread.utils.rowcol_to_a1(row_number, last_action_col), "values": [[now_str]]})
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


def render_email(templates_dir: str, template_prefix: str, variant: str, lead: Dict[str, str],
                  is_first_stage: bool = False) -> Dict:
    """Renders subject + body. A template's Subject line can be left BLANK
    to mean "continue the same thread" instead of starting a new one —
    mirrors the same convention other outreach tools use. When blank:

    - The outgoing subject becomes "Re: " + the lead's stored
      ThreadSubject (the most recent NON-blank subject actually sent to
      this lead), so Gmail/Outlook thread it together with the earlier
      message instead of starting a new conversation. Already-"Re:"
      subjects aren't double-prefixed.
    - ThreadSubject itself is left unchanged (still continuing the same
      original subject, not resetting it).

    A non-blank Subject always "resets" ThreadSubject going forward to
    whatever was actually rendered, so a later stage can deliberately
    start a fresh subject/thread, and any stage after THAT can continue
    from it with a blank Subject again.

    is_first_stage=True (the very first stage in the sequence) can never
    use a blank Subject — there's no previous thread to continue from a
    first message, so this raises immediately rather than sending a blank
    or guessed subject line.
    """
    tmpl = load_template(templates_dir, template_prefix, variant)
    missing: List[str] = []
    rendered_subject = render_text(tmpl["subject"], lead, missing_out=missing)
    body = render_text(tmpl["body"], lead, missing_out=missing)

    is_continuation = False
    if rendered_subject.strip():
        subject = rendered_subject
        thread_subject = rendered_subject
    else:
        if is_first_stage:
            raise TemplateError(
                f"{template_prefix}_{variant}.txt has a blank Subject line, but this is the FIRST stage "
                "in the sequence — there's no previous thread to continue. Every first-stage template "
                "needs a non-blank Subject."
            )
        existing_thread_subject = (lead.get("ThreadSubject") or "").strip()
        if not existing_thread_subject:
            raise TemplateError(
                f"{template_prefix}_{variant}.txt has a blank Subject line (continue-the-thread), but "
                f"lead '{lead.get('LeadID', '?')}' has no ThreadSubject recorded to continue from — this "
                "usually means the lead was sent to before this feature existed. Either put a Subject in "
                "this template, or fill in ThreadSubject manually in the Master Sheet for this lead."
            )
        subject = existing_thread_subject if existing_thread_subject.lower().startswith("re:") \
            else f"Re: {existing_thread_subject}"
        thread_subject = existing_thread_subject
        is_continuation = True

    seen = set()
    deduped_missing = []
    for name in missing:
        if name not in seen:
            seen.add(name)
            deduped_missing.append(name)

    return {"subject": subject, "body": body, "missing_variables": deduped_missing,
            "thread_subject": thread_subject, "is_continuation": is_continuation}


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


def find_duplicate_email_leads(leads: List[Dict]) -> Dict[str, List[Dict]]:
    """{email: [lead, lead, ...]} for every email address appearing in MORE
    THAN ONE Master Sheet row — always a data-entry mistake within a single
    campaign (a copy-paste, or re-adding a lead under a new LeadID), never
    a legitimate case. get_eligible_leads already protects against
    actually sending to more than one of them regardless of whether this
    warning is surfaced — this is purely for visibility, so the mistake
    gets noticed and cleaned up rather than silently persisting."""
    by_email: Dict[str, List[Dict]] = {}
    for lead in leads:
        email = (lead.get("Email") or "").strip().lower()
        if not email:
            continue
        by_email.setdefault(email, []).append(lead)
    return {email: rows for email, rows in by_email.items() if len(rows) > 1}


def get_eligible_leads(leads: List[Dict], stages: List[Dict], stage_index: int,
                        ignore_wait_days: bool = False) -> List[Dict]:
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
            continue  # the PREVIOUS stage must actually have been sent — this
                       # is stage ORDER, never skippable, unaffected by
                       # ignore_wait_days (that only overrides the WAIT, not
                       # the requirement that the previous stage happened)

        if ignore_wait_days:
            eligible.append(lead)
            continue

        prev_sent_dt = _parse_dt(prev_sent_raw)
        if prev_sent_dt is None:
            continue

        if now >= prev_sent_dt + timedelta(days=wait_days):
            eligible.append(lead)

    # De-duplicate by EMAIL ADDRESS, not row — the checks above only ever
    # protect a single row against being sent to twice. If the same email
    # exists as two separate rows (a copy-paste mistake, or re-adding a
    # lead under a new LeadID), both rows independently pass every check
    # and this would otherwise send the same person the same stage twice
    # in the same run. Keeps the first (lowest row number) occurrence —
    # see find_duplicate_email_leads for surfacing this as a warning
    # rather than silently dropping it.
    deduped = []
    seen_emails = set()
    for lead in eligible:
        email = (lead.get("Email") or "").strip().lower()
        if email in seen_emails:
            continue
        seen_emails.add(email)
        deduped.append(lead)
    return deduped


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
    """Original single-default resolution (no rotation): lead override >
    campaign default > global default. Still used directly whenever
    sender_rotation is off. Kept unchanged so nothing that depended on it
    before breaks."""
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


def get_rotation_accounts(campaign_cfg: Dict, accounts: Dict[str, Dict[str, str]]) -> List[str]:
    """Which accounts are in the rotation pool. Defaults to every account in
    EMAIL_ACCOUNTS_JSON; narrow it with sending.rotation_accounts."""
    configured = campaign_cfg.get("sending", {}).get("rotation_accounts")
    if configured:
        return [a for a in configured if a in accounts]
    return list(accounts.keys())


def pick_rotation_account(rotation_accounts: List[str], per_account_daily_limit: Optional[int],
                           sent_today_by_account: Dict[str, int],
                           batch_assigned_counts: Dict[str, int]) -> Optional[str]:
    """Picks the least-used-today account from the rotation pool that still
    has capacity. Returns None if every account is at its cap."""
    eligible = []
    for acct in rotation_accounts:
        used = sent_today_by_account.get(acct, 0) + batch_assigned_counts.get(acct, 0)
        if per_account_daily_limit is not None and used >= per_account_daily_limit:
            continue
        eligible.append((acct, used))
    if not eligible:
        return None
    min_used = min(u for _, u in eligible)
    least_used = [a for a, u in eligible if u == min_used]
    return random.choice(least_used)


def _check_account_capacity(account_name: str, per_account_daily_limit: Optional[int],
                             sent_today_by_account: Dict[str, int],
                             batch_assigned_counts: Dict[str, int]) -> None:
    if per_account_daily_limit is None:
        return
    used = sent_today_by_account.get(account_name, 0) + batch_assigned_counts.get(account_name, 0)
    if used >= per_account_daily_limit:
        raise SenderCapacityReachedError(
            f"Account '{account_name}' has reached its per-account daily limit ({per_account_daily_limit}).")


def resolve_sender_account_for_send(lead: Dict, campaign_cfg: Dict, accounts: Dict[str, Dict[str, str]],
                                     sent_today_by_account: Dict[str, int],
                                     batch_assigned_counts: Dict[str, int]) -> str:
    """The resolution actually used at send time. Priority, per the design
    goal of keeping manual assignment as an override rather than replacing
    it:

    1. The lead's own SenderAccount cell, if set — ALWAYS wins, and is
       checked against the per-account daily limit exactly like every other
       path (a manual pin doesn't bypass the safety cap; if that account is
       full, this lead is deferred rather than silently rerouted to a
       different account than the sheet says).
    2. If sending.sender_rotation is enabled — auto-pick the least-used
       eligible account from the rotation pool.
    3. Otherwise, the original single-default behavior (resolve_sender_account).

    Raises MissingSenderAccountError or SenderCapacityReachedError, both
    caught and classified by send_batch's per-lead error isolation.
    """
    sending_cfg = campaign_cfg.get("sending", {})
    per_account_limit = sending_cfg.get("per_account_daily_limit")

    requested = (lead.get("SenderAccount") or "").strip()
    if requested:
        if requested not in accounts:
            raise MissingSenderAccountError(f"Unknown SenderAccount '{requested}' — not in EMAIL_ACCOUNTS_JSON.")
        _check_account_capacity(requested, per_account_limit, sent_today_by_account, batch_assigned_counts)
        return requested

    if sending_cfg.get("sender_rotation"):
        rotation_accounts = get_rotation_accounts(campaign_cfg, accounts)
        if not rotation_accounts:
            raise MissingSenderAccountError(
                "sender_rotation is enabled but no valid accounts are configured "
                "(check sending.rotation_accounts and EMAIL_ACCOUNTS_JSON)."
            )
        chosen = pick_rotation_account(rotation_accounts, per_account_limit,
                                        sent_today_by_account, batch_assigned_counts)
        if chosen is None:
            raise SenderCapacityReachedError(
                f"All rotation accounts are at their per-account daily limit ({per_account_limit}).")
        return chosen

    chosen = resolve_sender_account(lead, campaign_cfg, accounts)
    _check_account_capacity(chosen, per_account_limit, sent_today_by_account, batch_assigned_counts)
    return chosen


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
    if isinstance(exc, SenderCapacityReachedError):
        return ERR_SENDER_CAPACITY
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

    parsed_date = None
    date_header = headers.get("date", "")
    if date_header:
        try:
            parsed_date = parsedate_to_datetime(date_header)
            if parsed_date.tzinfo is not None:
                parsed_date = parsed_date.replace(tzinfo=None)
        except (TypeError, ValueError):
            parsed_date = None

    return {
        "message_id": message_id, "in_reply_to": in_reply_to, "references": references,
        "subject": subject, "from": from_, "headers": headers, "body": body,
        "snippet": body[:500], "date": parsed_date,
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

            msg_date = parsed["date"]
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


def _count_sent_today_by_account(send_log: List[Dict]) -> Dict[str, int]:
    """Per-account send counts for today, sourced from SendLog (the only
    place that records which account each individual send used, with a
    timestamp — Master only keeps the most recent account per lead)."""
    today = datetime.now().strftime("%Y-%m-%d")
    counts: Dict[str, int] = {}
    for row in send_log:
        if row.get("Status") == "sent" and str(row.get("Timestamp", "")).startswith(today):
            acct = row.get("SenderAccount", "")
            if acct:
                counts[acct] = counts.get(acct, 0) + 1
    return counts


def backfill_thread_subjects(campaign_cfg: Dict, leads: List[Dict]) -> List[Dict]:
    """One-time migration helper: for every lead whose ThreadSubject is
    blank, reconstructs it by re-rendering the subject of the most
    recently sent stage that actually HAD a non-blank subject — walking
    backward through sent stages (last to first) and skipping over any
    that themselves used the blank-means-continue convention, since
    there's nothing to extract from those. This handles both:

    - Leads sent entirely before ThreadSubject existed (every stage had a
      real subject — the very first one checked always works).
    - Leads with a MIX of old real-subject stages and newer
      blank-subject-continuation stages (e.g. Intro had a real subject,
      but a later followup already used a blank Subject to continue it) —
      those newer blank stages are skipped over rather than causing the
      whole lookup to give up, since the real subject is still recoverable
      from the stage before them.

    Never overwrites an already-set ThreadSubject, and never writes
    anything itself — returns a plan the caller writes back (same
    "compute first, act separately" shape as build_batch), so this is
    naturally safe to dry-run and to re-run repeatedly.

    IMPORTANT CAVEAT: this assumes the stage it ultimately finds hasn't
    had its template Subject edited since it was actually sent. If you've
    since changed that wording, the backfilled value will be wrong for
    leads sent under the old one. There's no historical record of the
    exact subject actually sent (SendLog doesn't store it) — re-rendering
    the current template is a practical best effort, not a guarantee. For
    leads where getting this exactly right matters, set ThreadSubject
    manually from your actual sent mail instead — carefully: a
    copy-pasted subject from the WRONG lead/campaign will "work" (no
    error) but silently produce a mis-threaded reply, since nothing here
    can verify a manually-entered value against what was really sent.
    """
    stages = campaign_cfg["stages"]
    results = []
    for lead in leads:
        lead_id = lead.get("LeadID", "")
        if (lead.get("ThreadSubject") or "").strip():
            results.append({"lead_id": lead_id, "status": "skipped_already_set"})
            continue

        sent_indices = [i for i in range(len(stages) - 1, -1, -1)
                         if (lead.get(stage_field_names(i)["sent_at"]) or "").strip()]

        if not sent_indices:
            results.append({"lead_id": lead_id, "status": "skipped_not_sent_yet"})
            continue

        found = None
        skipped_blank_stages = []
        last_error = None
        last_error_stage = None
        for idx in sent_indices:
            fields = stage_field_names(idx)
            variant = (lead.get(fields["variant"]) or "").strip()
            stage_label = stages[idx]["name"]
            if not variant:
                continue  # try an earlier stage rather than giving up entirely
            try:
                tmpl = load_template(campaign_cfg["templates_dir"], stages[idx]["template_prefix"], variant)
                subject = render_text(tmpl["subject"], lead)
            except Exception as exc:  # noqa: BLE001 - isolate per-lead template issues, keep trying earlier stages
                last_error, last_error_stage = str(exc), stage_label
                continue
            if not subject.strip():
                skipped_blank_stages.append(stage_label)  # this one also used blank-continuation — try earlier
                continue
            found = {"subject": subject, "stage": stage_label, "row": lead["_row"]}
            break

        if found is not None:
            results.append({"lead_id": lead_id, "status": "backfilled", "thread_subject": found["subject"],
                             "row": found["row"], "stage": found["stage"]})
        elif last_error is not None:
            results.append({"lead_id": lead_id, "status": "error", "stage": last_error_stage, "error": last_error})
        elif skipped_blank_stages:
            results.append({"lead_id": lead_id, "status": "skipped_template_now_blank",
                             "stage": skipped_blank_stages[0]})
        else:
            results.append({"lead_id": lead_id, "status": "skipped_unknown_variant",
                             "stage": stages[sent_indices[0]]["name"]})
    return results


def import_leads(sheets: SheetsConnector, campaign_name: str, new_leads: List[Dict[str, str]]) -> Dict[str, int]:
    """Appends new_leads as new Master Sheet rows. Skips any row with no
    Email (mandatory everywhere else in this system) and any row whose
    Email already exists among current leads (case-insensitive) — the
    same "first row wins" assumption find_duplicate_email_leads already
    enforces elsewhere, just applied at import time instead of left for
    that check to flag later.

    Every imported lead's Approval is left BLANK (Pending) unless the
    caller explicitly set it in that row's dict — a bulk import should
    never default new leads straight to "Yes" and eligible to send; a
    human needs to approve them first, same as any manually-added row.
    """
    existing = sheets.get_all_leads()
    existing_ids = [int(l["LeadID"]) for l in existing if str(l.get("LeadID", "")).strip().isdigit()]
    next_id = (max(existing_ids) + 1) if existing_ids else 1
    existing_emails = {(l.get("Email") or "").strip().lower() for l in existing if (l.get("Email") or "").strip()}

    imported = 0
    skipped_duplicate = 0
    skipped_no_email = 0
    for lead in new_leads:
        email = (lead.get("Email") or "").strip()
        if not email:
            skipped_no_email += 1
            continue
        if email.lower() in existing_emails:
            skipped_duplicate += 1
            continue
        row = dict(lead)
        row["LeadID"] = str(next_id)
        row["Campaign"] = campaign_name
        row.setdefault("Approval", "")
        sheets.append_lead(row)
        existing_emails.add(email.lower())
        next_id += 1
        imported += 1

    return {"imported": imported, "skipped_duplicate": skipped_duplicate, "skipped_no_email": skipped_no_email}


def remove_leads(sheets: SheetsConnector, lead_ids: List[str]) -> Dict[str, int]:
    """Soft-remove: sets Status=Removed for every matching LeadID — NEVER
    a hard delete. The row and everything already sent to that lead stays
    exactly as it was; get_eligible_leads already excludes any
    TERMINAL_STATUSES status, which Removed is now part of, so a removed
    lead simply stops being picked up for anything further."""
    existing = sheets.get_all_leads()
    lead_id_set = {str(lid) for lid in lead_ids}
    row_updates = {l["_row"]: STATUS_REMOVED for l in existing if str(l.get("LeadID", "")) in lead_id_set}
    sheets.update_lead_statuses(row_updates)
    found_ids = {str(l.get("LeadID", "")) for l in existing if l["_row"] in row_updates}
    return {"removed": len(row_updates), "not_found": len(lead_id_set - found_ids)}


def build_batch(campaign_cfg: Dict, leads: List[Dict], stage_name: str, batch_size: int,
                 forced_variant: Optional[str] = None, ignore_wait_days: bool = False) -> List[Dict]:
    """Computes eligible leads + assigns variants + renders emails WITHOUT
    sending or writing anything. Safe to call repeatedly for preview.

    ignore_wait_days=True skips ONLY the wait_days_after_previous timing
    check for stages after the first — every other eligibility rule still
    applies unchanged: Approval must be Yes, the lead can't be in a
    terminal/replied state, this stage can't already be sent, and the
    PREVIOUS stage must actually have been sent (stage order is never
    skippable, only the wait between stages is overridable). This is the
    "send this follow-up now regardless of schedule" override.
    """
    stages = campaign_cfg["stages"]
    variants = campaign_cfg["variants"]
    idx = _stage_index(stages, stage_name)
    fields = stage_field_names(idx)

    if forced_variant is not None and forced_variant not in variants:
        raise ValueError(f"Variant '{forced_variant}' is not in campaign variants: {variants}")

    eligible = get_eligible_leads(leads, stages, idx, ignore_wait_days=ignore_wait_days)[:batch_size]

    batch_counts = {v: 0 for v in variants}
    plan = []
    for lead in eligible:
        if forced_variant is not None:
            variant = forced_variant
        else:
            variant = pick_variant(leads, fields["variant"], variants, batch_counts)
            batch_counts[variant] += 1
        rendered = render_email(campaign_cfg["templates_dir"], stages[idx]["template_prefix"], variant, lead,
                                 is_first_stage=(idx == 0))

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
            "thread_subject": rendered["thread_subject"], "is_continuation": rendered["is_continuation"],
        })
    return plan


def _resolve_account_for_round(lead: Dict, campaign_cfg: Dict, accounts: Dict[str, Dict[str, str]],
                                confirmed_batch_counts: Dict[str, int], sent_today_by_account: Dict[str, int],
                                used_accounts_this_round: set) -> Tuple[str, object]:
    """Resolution used when building one CONCURRENT sending round (see
    send_batch). Returns one of:

      ("account", account_name) — ready to send this round.
      ("defer", None)           — this lead's account is already busy this
                                   round (e.g. two leads both pinned to the
                                   same SenderAccount, or every rotation
                                   account is already taken this round).
                                   Not an error — retried in the next round.
      ("error", exception)      — permanently unsendable this run (unknown
                                   account, or genuinely out of capacity
                                   across every eligible account).

    Capacity itself (sending.per_account_daily_limit) is always checked
    against confirmed_batch_counts — successes actually confirmed from
    completed rounds so far — never against same-round reservations, so a
    lead is never falsely told "at capacity" just because another lead
    happens to be using that account in the same round.
    """
    per_account_limit = campaign_cfg.get("sending", {}).get("per_account_daily_limit")
    requested = (lead.get("SenderAccount") or "").strip()

    if requested:
        if requested not in accounts:
            return ("error", MissingSenderAccountError(
                f"Unknown SenderAccount '{requested}' — not in EMAIL_ACCOUNTS_JSON."))
        try:
            _check_account_capacity(requested, per_account_limit, sent_today_by_account, confirmed_batch_counts)
        except SenderCapacityReachedError as exc:
            return ("error", exc)
        if requested in used_accounts_this_round:
            return ("defer", None)
        return ("account", requested)

    sending_cfg = campaign_cfg.get("sending", {})
    if sending_cfg.get("sender_rotation"):
        rotation_accounts = get_rotation_accounts(campaign_cfg, accounts)
        if not rotation_accounts:
            return ("error", MissingSenderAccountError(
                "sender_rotation is enabled but no valid accounts are configured "
                "(check sending.rotation_accounts and EMAIL_ACCOUNTS_JSON)."))

        def has_capacity(acct: str) -> bool:
            if per_account_limit is None:
                return True
            used = sent_today_by_account.get(acct, 0) + confirmed_batch_counts.get(acct, 0)
            return used < per_account_limit

        capacity_ok = [a for a in rotation_accounts if has_capacity(a)]
        if not capacity_ok:
            return ("error", SenderCapacityReachedError(
                f"All rotation accounts are at their per-account daily limit ({per_account_limit})."))

        # Prefer an account not already claimed by another lead THIS round —
        # this is what makes concurrent sending safe: every job fired at the
        # same time in one round uses a different account.
        available_now = [a for a in capacity_ok if a not in used_accounts_this_round]
        if not available_now:
            return ("defer", None)  # every currently-usable account is busy this round only

        chosen = pick_rotation_account(available_now, per_account_limit, sent_today_by_account,
                                        confirmed_batch_counts)
        return ("account", chosen)

    # No manual pin, no rotation — the single campaign/global default account.
    try:
        chosen = resolve_sender_account(lead, campaign_cfg, accounts)
        _check_account_capacity(chosen, per_account_limit, sent_today_by_account, confirmed_batch_counts)
    except (MissingSenderAccountError, SenderCapacityReachedError) as exc:
        return ("error", exc)
    if chosen in used_accounts_this_round:
        return ("defer", None)
    return ("account", chosen)


def _record_send_failure(sheets: "SheetsConnector", campaign_name: str, batch_id: str, stage_name: str,
                          item: Dict, exc: Exception, account_name: str = "") -> Dict:
    lead = item["lead"]
    row = lead["_row"]
    lead_id = lead.get("LeadID", "")
    lead_email = lead.get("Email", "")
    now_str = datetime.now().strftime(DATETIME_FMT)
    error_type = classify_send_exception(exc)
    is_skip = isinstance(exc, SenderCapacityReachedError)
    outcome_status = "skipped" if is_skip else "error"
    try:
        sheets.update_lead_fields(row, {"Error": str(exc)[:500]})
        sheets.append_send_log({
            "BatchID": batch_id, "Timestamp": now_str, "LeadID": lead_id, "Email": lead_email,
            "Campaign": campaign_name, "Stage": stage_name, "Variant": item["variant"],
            "SenderAccount": account_name, "Status": outcome_status, "MessageID": "",
            "Error": str(exc)[:500],
        })
    except Exception:  # noqa: BLE001 - the error log entry below is the durable record either way
        pass
    log_error(sheets, campaign_name, error_type, str(exc), lead_id=lead_id, email_addr=lead_email,
              stage=stage_name, batch_id=batch_id)
    return {"lead_id": lead_id, "email": lead_email, "status": outcome_status,
            "error": str(exc), "error_type": error_type, "batch_id": batch_id}


def _record_send_success(sheets: "SheetsConnector", campaign_name: str, batch_id: str, stage_name: str,
                          fields: Dict[str, str], idx: int, stages: List[Dict], item: Dict,
                          account_name: str, sent: Dict[str, str]) -> Dict:
    lead = item["lead"]
    row = lead["_row"]
    lead_id = lead.get("LeadID", "")
    lead_email = lead.get("Email", "")
    now = datetime.now()
    now_str = now.strftime(DATETIME_FMT)
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
            "ThreadSubject": item["thread_subject"],
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
        return {"lead_id": lead_id, "email": lead_email, "status": "sent",
                "variant": item["variant"], "batch_id": batch_id, "account": account_name}
    except Exception as sheets_exc:  # noqa: BLE001
        # The send genuinely happened, so this account WAS used, and the
        # caller still needs to count it against confirmed_batch_counts —
        # flagged distinctly so it's not confused with an ordinary send
        # failure (the email is already in the recipient's inbox).
        log_error(sheets, campaign_name, ERR_SHEETS_API,
                  f"Email sent successfully (Message-ID {sent['message_id']}) but failed to update "
                  f"the sheet: {sheets_exc}. Check manually to avoid a duplicate resend.",
                  lead_id=lead_id, email_addr=lead_email, stage=stage_name, batch_id=batch_id)
        return {"lead_id": lead_id, "email": lead_email, "status": "sent_but_sheet_error",
                "error": str(sheets_exc), "batch_id": batch_id, "account": account_name}


def send_batch(campaign_cfg: Dict, sheets: SheetsConnector, accounts: Dict[str, Dict[str, str]],
               stage_name: str, batch_size: int, forced_variant: Optional[str] = None,
               ignore_wait_days: bool = False) -> List[Dict]:
    """Sends the batch in CONCURRENT ROUNDS instead of one email at a time.

    Each round is built greedily so that no two jobs in it share a sender
    account — meaning every job in a round can genuinely fire at the same
    moment (real threads, real simultaneous SMTP connections), one send per
    account per round. A lead whose resolved account is already taken by
    another lead in the current round isn't an error — it's simply carried
    over to the next round. With N distinct sender accounts, this produces
    exactly the "N accounts to N leads at once, then wait, then the next N"
    behavior. With a single account (or nothing but manually-pinned leads
    all sharing one account), every round naturally has exactly one job —
    identical in effect to the old fully-sequential behavior, so nothing
    changes for single-account setups.

    sending.delay_min_minutes / delay_max_minutes are the wait BETWEEN
    ROUNDS now, not between individual emails — the pacing that used to
    apply per-send now applies per-account (each account only sends its
    next email one round-delay later).

    Every attempt (sent, error, or skipped) is logged to SendLog under one
    BatchID, and every error is also classified and logged to the Error
    Log. Failures are isolated per lead and never abort the batch. A lead
    that can never be sent this run (unknown account, genuinely at
    capacity) is recorded immediately, without waiting for a round.

    ignore_wait_days=True overrides ONLY the scheduled wait between stages
    (e.g. send followup1 today even though it's not due for 3 more days) —
    see build_batch's docstring for exactly what is and isn't skipped.

    Raises CampaignPausedError immediately, before touching anything, if
    campaign_cfg["status"] == "paused" — Preview/build_batch are
    deliberately NOT gated by this, so a paused campaign can still be
    reviewed, just not sent.
    """
    if campaign_cfg.get("status") == "paused":
        raise CampaignPausedError(
            f"Campaign '{campaign_cfg.get('_campaign_name', '')}' is paused — no batch will be sent. "
            "Resume it (set status back to 'active') first if this was unintentional."
        )

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

    if effective_batch_size <= 0:
        return []

    plan = build_batch(campaign_cfg, leads, stage_name, effective_batch_size, forced_variant=forced_variant,
                        ignore_wait_days=ignore_wait_days)
    if not plan:
        return []

    idx = _stage_index(stages, stage_name)
    fields = stage_field_names(idx)
    batch_id = make_batch_id()

    send_log = sheets.get_all_send_log()
    sent_today_by_account = _count_sent_today_by_account(send_log)
    confirmed_batch_counts: Dict[str, int] = {}  # confirmed successes from ROUNDS ALREADY COMPLETED this run

    results: List[Optional[Dict]] = [None] * len(plan)
    pending: List[Tuple[int, Dict]] = list(enumerate(plan))

    while pending:
        round_jobs: List[Tuple[int, Dict, str]] = []
        used_accounts_this_round: set = set()
        still_pending: List[Tuple[int, Dict]] = []

        for orig_idx, item in pending:
            lead = item["lead"]
            lead_email = lead.get("Email", "")

            if not is_valid_email_format(lead_email):
                exc = InvalidEmailFormatError(f"'{lead_email}' is not a valid email address format")
                results[orig_idx] = _record_send_failure(sheets, campaign_name, batch_id, stage_name, item, exc)
                continue

            status, value = _resolve_account_for_round(lead, campaign_cfg, accounts, confirmed_batch_counts,
                                                         sent_today_by_account, used_accounts_this_round)
            if status == "error":
                results[orig_idx] = _record_send_failure(sheets, campaign_name, batch_id, stage_name, item, value)
                continue
            if status == "defer":
                still_pending.append((orig_idx, item))
                continue

            account_name = value
            used_accounts_this_round.add(account_name)
            round_jobs.append((orig_idx, item, account_name))

        if round_jobs:
            # Fire every job in this round AT THE SAME TIME — real threads,
            # real concurrent SMTP connections, one per account. Sheets
            # writes are deliberately NOT done from these threads (gspread's
            # Worksheet object isn't meant to be hit concurrently) — only
            # the network send itself is parallelized.
            sent_outcomes: Dict[int, Tuple[str, object]] = {}
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(round_jobs)) as executor:
                future_to_job = {
                    executor.submit(smtp_send, accounts[acct]["address"], accounts[acct]["app_password"],
                                     to=job_item["lead"].get("Email", ""), subject=job_item["subject"],
                                     body_text=job_item["body"], in_reply_to=job_item["in_reply_to"],
                                     references=job_item["references"]): orig_idx
                    for orig_idx, job_item, acct in round_jobs
                }
                for future in concurrent.futures.as_completed(future_to_job):
                    orig_idx = future_to_job[future]
                    try:
                        sent_outcomes[orig_idx] = ("ok", future.result())
                    except Exception as exc:  # noqa: BLE001 - isolate per-lead SMTP failures within the round
                        sent_outcomes[orig_idx] = ("error", exc)

            # Persist results sequentially, back on the main thread, in the
            # round's original order.
            for orig_idx, job_item, acct in round_jobs:
                outcome, payload = sent_outcomes[orig_idx]
                if outcome == "ok":
                    result = _record_send_success(sheets, campaign_name, batch_id, stage_name, fields, idx,
                                                   stages, job_item, acct, payload)
                    if result["status"] in ("sent", "sent_but_sheet_error"):
                        confirmed_batch_counts[acct] = confirmed_batch_counts.get(acct, 0) + 1
                else:
                    result = _record_send_failure(sheets, campaign_name, batch_id, stage_name, job_item,
                                                   payload, account_name=acct)
                results[orig_idx] = result

        pending = still_pending
        if pending:
            time.sleep(random.uniform(delay_min * 60, delay_max * 60))

    return results


# =============================================================================
# SECTION 12: Reply monitor
# =============================================================================

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def _extract_email(from_header: str) -> str:
    match = EMAIL_RE.search(from_header or "")
    return match.group(0).lower() if match else ""


def _most_recent_send_at(lead: Dict) -> Optional[datetime]:
    """Latest send timestamp across every stage for this lead. Used to
    sanity-check whether an inbound message could plausibly be a reply to
    THIS campaign's own outreach — it can't be a reply to something that
    hadn't been sent to this lead yet."""
    latest = None
    for field in ("IntroSentAt", "FollowUp1SentAt", "FollowUp2SentAt", "FollowUp3SentAt", "FollowUp4SentAt"):
        dt = _parse_dt(lead.get(field, ""))
        if dt is not None and (latest is None or dt > latest):
            latest = dt
    return latest


# Reply-matching outcomes. A Header match (inbound In-Reply-To/References
# contains a Message-ID this system actually sent) is the only signal
# trusted enough to stop a live sequence. An Email-only match (same sender
# address, no header link) is real signal worth logging, but NEVER trusted
# on its own to stop anything — the same address can legitimately appear
# across more than one campaign (or a previous, since-replaced campaign),
# and sender-address alone can't tell those apart. See README "Reply
# matching safety" for the full rationale and the production incident that
# prompted this.
ACTION_STOPPED = "Stopped Sequence"
ACTION_LOGGED_ONLY = "Logged Only"
ACTION_LOGGED_UNVERIFIED = "Logged Only (Unverified Match)"
ACTION_LOGGED_UNRELATED = "Logged Only (Predates Contact)"


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

            master_updates = {"LastInboundClassification": classification, "LastInboundAt": now_str,
                               "LastActionAt": now_str}

            if match_method == "Header":
                # Strong signal: this message is provably part of THIS
                # campaign's own outbound thread. Safe to act on.
                action_taken = ACTION_LOGGED_ONLY
                if classification == CLASSIFICATION_GENUINE:
                    master_updates.update({"ReplyStatus": "Replied", "ReplyAt": now_str,
                                            "Status": STATUS_STOPPED_REPLIED})
                    action_taken = ACTION_STOPPED
                elif classification == CLASSIFICATION_BOUNCE_HARD:
                    master_updates["Status"] = STATUS_STOPPED_BOUNCED
                    action_taken = ACTION_STOPPED
            else:
                # Sender-only match — logged for visibility, but NEVER
                # allowed to stop a sequence (ReplyStatus/Status are
                # deliberately left untouched below in both branches).
                last_sent = _most_recent_send_at(matched_lead)
                msg_date = msg.get("date")
                if last_sent is not None and msg_date is not None and msg_date < last_sent:
                    # Chronologically impossible to be a reply to this
                    # campaign's outreach — almost certainly a stale message
                    # tied to a different (possibly deleted) campaign that
                    # happens to share this lead's email address.
                    action_taken = ACTION_LOGGED_UNRELATED
                else:
                    action_taken = ACTION_LOGGED_UNVERIFIED

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

    sent_today_by_account = _count_sent_today_by_account(send_log)
    per_account_limit = campaign_cfg.get("sending", {}).get("per_account_daily_limit")
    rotation_on = bool(campaign_cfg.get("sending", {}).get("sender_rotation"))
    if sent_today_by_account or rotation_on:
        rows.append(("Sender Usage Today", "Sender Rotation Enabled", "Yes" if rotation_on else "No"))
        usage_accounts = sorted(set(sent_today_by_account.keys()) | set(accounts_seen))
        for acct in usage_accounts:
            used_today = sent_today_by_account.get(acct, 0)
            if per_account_limit is not None:
                rows.append(("Sender Usage Today", f"{acct} - Sent Today", f"{used_today} / {per_account_limit}"))
            else:
                rows.append(("Sender Usage Today", f"{acct} - Sent Today", f"{used_today} (no per-account cap set)"))

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

    duplicates = find_duplicate_email_leads(leads)
    if duplicates:
        print(f"WARNING: {len(duplicates)} email address(es) appear on more than one Master Sheet row — "
              "only the first (lowest row number) of each is ever eligible; the rest are silently excluded "
              "from sending, but should be cleaned up:")
        for email_addr, rows in duplicates.items():
            lead_ids = ", ".join(str(r.get("LeadID", "?")) for r in rows)
            print(f"  {email_addr} — rows {[r['_row'] for r in rows]} (LeadIDs: {lead_ids})")
        print()

    forced_variant = None if args.variant in (None, "Auto") else args.variant
    plan = build_batch(campaign_cfg, leads, args.stage, args.batch_size, forced_variant=forced_variant,
                        ignore_wait_days=args.ignore_wait_days)

    if not plan:
        print(f"No eligible leads found for stage '{args.stage}'.")
        return

    if args.ignore_wait_days:
        print(f"NOTE: --ignore-wait-days is set — the scheduled wait for stage '{args.stage}' was skipped "
              "for this preview. Every other eligibility rule (Approval, not already sent, previous stage "
              "actually sent, no reply) still applied normally.\n")
    print(f"{len(plan)} eligible lead(s) for stage '{args.stage}':\n")
    for item in plan:
        lead = item["lead"]
        print("=" * 70)
        print(f"Lead ID:  {lead.get('LeadID')}")
        print(f"To:       {lead.get('FirstName')} {lead.get('LastName')} <{lead.get('Email')}>")
        if not is_valid_email_format(lead.get("Email", "")):
            print("          WARNING: this email address doesn't look correctly formatted.")
        print(f"Variant:  {item['variant']}")
        print(f"Subject:  {item['subject']}"
              + ("  (continuing existing thread)" if item["is_continuation"] else ""))
        print("-" * 70)
        print(item["body"])
        if item["missing_variables"]:
            print(f"\nWARNING: unrecognized template variable(s), rendered blank: "
                  f"{', '.join('{{' + v + '}}' for v in item['missing_variables'])}")
    print("=" * 70)
    print("\nNothing has been sent. Re-run with the 'send' command to actually send this batch.")


def apply_sending_overrides(campaign_cfg: Dict, daily_limit: Optional[int] = None,
                             per_account_daily_limit: Optional[int] = None,
                             sender_rotation: Optional[str] = None) -> List[str]:
    """Applies CLI-provided overrides for a single run — never touches
    campaigns.yaml. Replaces campaign_cfg['sending'] with a fresh copy
    before mutating it, so nothing shared/cached elsewhere is affected.
    Returns the list of keys that were actually overridden (for reporting).
    Raises ValueError for an invalid override value."""
    campaign_cfg["sending"] = dict(campaign_cfg["sending"])
    sending = campaign_cfg["sending"]
    overridden = []

    if daily_limit is not None:
        if daily_limit <= 0:
            raise ValueError("--daily-limit must be a positive integer.")
        sending["daily_limit"] = daily_limit
        overridden.append("daily_limit")

    if per_account_daily_limit is not None:
        if per_account_daily_limit <= 0:
            raise ValueError("--per-account-daily-limit must be a positive integer.")
        sending["per_account_daily_limit"] = per_account_daily_limit
        overridden.append("per_account_daily_limit")

    if sender_rotation is not None:
        sending["sender_rotation"] = (sender_rotation == "true")
        overridden.append("sender_rotation")

    return overridden


def cmd_send(args):
    campaign_cfg = get_campaign(args.campaign)
    sheets = _connect_sheets(campaign_cfg)
    accounts = load_email_accounts()

    duplicates = find_duplicate_email_leads(sheets.get_all_leads())
    if duplicates:
        print(f"WARNING: {len(duplicates)} email address(es) appear on more than one Master Sheet row — "
              "only the first (lowest row number) of each is ever eligible; the rest are silently excluded "
              "from sending, but should be cleaned up:")
        for email_addr, rows in duplicates.items():
            lead_ids = ", ".join(str(r.get("LeadID", "?")) for r in rows)
            print(f"  {email_addr} — rows {[r['_row'] for r in rows]} (LeadIDs: {lead_ids})")
        print()

    try:
        overridden = apply_sending_overrides(campaign_cfg, args.daily_limit,
                                              args.per_account_daily_limit, args.sender_rotation)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    sending = campaign_cfg["sending"]
    print(
        f"Sending config for this run: daily_limit={sending.get('daily_limit')}"
        f"{' [overridden]' if 'daily_limit' in overridden else ' [from config]'}, "
        f"per_account_daily_limit={sending.get('per_account_daily_limit')}"
        f"{' [overridden]' if 'per_account_daily_limit' in overridden else ' [from config]'}, "
        f"sender_rotation={sending.get('sender_rotation', False)}"
        f"{' [overridden]' if 'sender_rotation' in overridden else ' [from config]'}\n"
    )

    forced_variant = None if args.variant in (None, "Auto") else args.variant
    if args.ignore_wait_days:
        print(f"NOTE: --ignore-wait-days is set — the scheduled wait for stage '{args.stage}' will be "
              "skipped for this run. Every other eligibility rule (Approval, not already sent, previous "
              "stage actually sent, no reply) still applies normally.\n")
    try:
        results = send_batch(campaign_cfg, sheets, accounts, args.stage, args.batch_size,
                              forced_variant=forced_variant, ignore_wait_days=args.ignore_wait_days)
    except CampaignPausedError as exc:
        print(f"SKIPPED: {exc}")
        return

    if not results:
        print(f"No eligible leads to send for stage '{args.stage}' "
              "(none eligible, or today's sending limit already reached).")
        return

    batch_id = results[0].get("batch_id", "")
    sent = [r for r in results if r["status"] == "sent"]
    sent_but_sheet_error = [r for r in results if r["status"] == "sent_but_sheet_error"]
    skipped = [r for r in results if r["status"] == "skipped"]
    errors = [r for r in results if r["status"] == "error"]

    print(f"Batch ID: {batch_id}")
    print(f"Sent {len(sent)} email(s), {len(sent_but_sheet_error)} sent-but-sheet-error, "
          f"{len(skipped)} skipped (sender capacity), {len(errors)} error(s).\n")
    for r in sent:
        print(f"  OK    {r['email']} (variant {r['variant']}, account {r['account']})")
    for r in sent_but_sheet_error:
        print(f"  WARN  {r['email']}: email sent, but sheet update failed ({r['error']}) — check manually")
    for r in skipped:
        print(f"  SKIP  {r['email']}: {r['error']}")
    for r in errors:
        print(f"  ERROR {r['email']}: [{r['error_type']}] {r['error']}")


def cmd_backfill_thread_subject(args):
    """One-time migration command — see backfill_thread_subjects' docstring
    for exactly what this does and its accuracy caveat. Defaults to
    --dry-run in the GitHub Actions workflow (not here in the raw CLI, to
    match every other command's behavior of doing what you asked)."""
    campaign_cfg = get_campaign(args.campaign)
    sheets = _connect_sheets(campaign_cfg)
    leads = sheets.get_all_leads()

    results = backfill_thread_subjects(campaign_cfg, leads)
    to_backfill = [r for r in results if r["status"] == "backfilled"]
    skipped = [r for r in results if r["status"] not in ("backfilled",)]

    action_word = "Would backfill" if args.dry_run else "Backfilling"
    print(f"{action_word} ThreadSubject for {len(to_backfill)} lead(s).\n")
    for r in to_backfill:
        print(f"  Lead {r['lead_id']} (from {r['stage']}): {r['thread_subject']!r}")

    if skipped:
        print(f"\n{len(skipped)} lead(s) skipped (no action needed/possible):")
        for r in skipped:
            detail = f" ({r['stage']})" if r.get("stage") else ""
            extra = f" — {r['error']}" if r.get("error") else ""
            print(f"  Lead {r['lead_id']}: {r['status']}{detail}{extra}")

    if args.dry_run:
        print("\nDRY RUN — nothing was written. Re-run without --dry-run to actually write these.")
        return

    for r in to_backfill:
        sheets.update_lead_fields(r["row"], {"ThreadSubject": r["thread_subject"]})
    print(f"\nWrote ThreadSubject for {len(to_backfill)} lead(s).")


def cmd_import_leads(args):
    """Reads {"leads": [{...}, ...]} from --file and appends them to the
    Master Sheet. This is the ONLY thing that ever writes to the Master
    Sheet from a bulk-import path — invoked by import_leads.yml after
    Streamlit commits the mapped payload file, never called with
    Streamlit-supplied data any other way."""
    campaign_cfg = get_campaign(args.campaign)
    sheets = _connect_sheets(campaign_cfg)
    with open(args.file, "r", encoding="utf-8") as f:
        payload = json.load(f)
    new_leads = payload.get("leads", [])
    if not new_leads:
        print("No leads in payload file — nothing to do.")
        return
    summary = import_leads(sheets, args.campaign, new_leads)
    print(f"Imported {summary['imported']} lead(s).")
    if summary["skipped_duplicate"]:
        print(f"Skipped {summary['skipped_duplicate']} duplicate email(s) (already in the Master Sheet).")
    if summary["skipped_no_email"]:
        print(f"Skipped {summary['skipped_no_email']} row(s) with no email address.")


def cmd_remove_leads(args):
    """Reads {"lead_ids": ["5", "8", ...]} from --file and sets their
    Status to Removed — never a hard delete, see remove_leads' docstring."""
    campaign_cfg = get_campaign(args.campaign)
    sheets = _connect_sheets(campaign_cfg)
    with open(args.file, "r", encoding="utf-8") as f:
        payload = json.load(f)
    lead_ids = payload.get("lead_ids", [])
    if not lead_ids:
        print("No lead_ids in payload file — nothing to do.")
        return
    summary = remove_leads(sheets, lead_ids)
    print(f"Removed {summary['removed']} lead(s) (Status set to '{STATUS_REMOVED}').")
    if summary["not_found"]:
        print(f"{summary['not_found']} LeadID(s) in the payload weren't found in the Master Sheet.")


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
        settings = load_settings()
        shared_sheet_id = settings.get("shared_sheet_id", "")
        campaign_names = discover_campaign_names()
        if not campaign_names:
            print("No campaigns found — no subfolders under templates/.")
            return
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
    p_preview.add_argument("--ignore-wait-days", action="store_true",
                            help="Skip the scheduled wait for this stage (e.g. followup1's 3-day wait) "
                                 "for THIS RUN ONLY. Every other eligibility rule still applies — the "
                                 "previous stage must actually have been sent, this stage can't already "
                                 "be sent, Approval must be Yes, and no reply must have been received.")
    p_preview.set_defaults(func=cmd_preview)

    p_send = sub.add_parser("send", help="Actually send a batch")
    p_send.add_argument("--campaign", required=True)
    p_send.add_argument("--stage", required=True)
    p_send.add_argument("--batch-size", type=int, required=True)
    p_send.add_argument("--variant", default="Auto",
                         help="Force a specific variant letter for the whole batch. Default: Auto")
    p_send.add_argument("--ignore-wait-days", action="store_true",
                         help="Skip the scheduled wait for this stage (e.g. followup1's 3-day wait) "
                              "for THIS RUN ONLY. Every other eligibility rule still applies — the "
                              "previous stage must actually have been sent, this stage can't already "
                              "be sent, Approval must be Yes, and no reply must have been received.")
    p_send.add_argument("--daily-limit", type=int, default=None,
                         help="Override sending.daily_limit for THIS RUN ONLY — never written to "
                              "campaigns.yaml. Omit to use the value from config.")
    p_send.add_argument("--per-account-daily-limit", type=int, default=None,
                         help="Override sending.per_account_daily_limit for THIS RUN ONLY — never "
                              "written to campaigns.yaml. Omit to use the value from config.")
    p_send.add_argument("--sender-rotation", choices=["true", "false"], default=None,
                         help="Override sending.sender_rotation for THIS RUN ONLY — never written "
                              "to campaigns.yaml. Omit to use the value from config.")
    p_send.set_defaults(func=cmd_send)

    p_replies = sub.add_parser("check-replies", help="Check all configured inboxes for new replies/bounces")
    p_replies.add_argument("--campaign", required=True)
    p_replies.set_defaults(func=cmd_check_replies)

    p_backfill = sub.add_parser("backfill-thread-subject",
                                 help="One-time migration: fill in ThreadSubject for leads already mid-sequence "
                                      "before that feature existed (see backfill_thread_subjects docstring for "
                                      "exactly how, and its accuracy caveat)")
    p_backfill.add_argument("--campaign", required=True)
    p_backfill.add_argument("--dry-run", action="store_true",
                             help="Show what would be backfilled without writing anything")
    p_backfill.set_defaults(func=cmd_backfill_thread_subject)

    p_import = sub.add_parser("import-leads", help="Bulk-import leads from a JSON payload file")
    p_import.add_argument("--campaign", required=True)
    p_import.add_argument("--file", required=True, help='Path to a JSON file: {"leads": [{...}, ...]}')
    p_import.set_defaults(func=cmd_import_leads)

    p_remove = sub.add_parser("remove-leads",
                               help="Soft-remove leads (sets Status=Removed, never a hard delete) "
                                    "from a JSON payload file")
    p_remove.add_argument("--campaign", required=True)
    p_remove.add_argument("--file", required=True, help='Path to a JSON file: {"lead_ids": ["5", "8", ...]}')
    p_remove.set_defaults(func=cmd_remove_leads)

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
