import os
import sys

import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from auth import login_gate  # noqa: E402
from config import REPO_ROOT, SETTINGS_PATH  # noqa: E402
from preview_logic import list_campaigns, get_campaign_cfg  # noqa: E402
from sheets_readonly import ReadOnlySheetsConnector, ReadOnlySheetsError  # noqa: E402
from accounts_logic import aggregate_sent_today_by_account, build_account_rows  # noqa: E402

if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
import outreach  # noqa: E402

# Page config is set once, centrally, in app.py via st.navigation/st.Page —
# calling st.set_page_config here too would raise an error.

if not login_gate():
    st.stop()

st.title("📧 Email Accounts")
st.caption(
    "Shows which sender accounts are configured and how much each has sent today — never the actual "
    "SMTP credentials. Those live only in the GitHub Secret EMAIL_ACCOUNTS_JSON, used exclusively by "
    "GitHub Actions; this app never has access to them."
)


@st.cache_resource(show_spinner=False)
def _get_connector() -> ReadOnlySheetsConnector:
    sa_info = dict(st.secrets["google_sheets_readonly"]["service_account_json"])
    sheet_id = st.secrets.get("shared_sheet_id", "")
    return ReadOnlySheetsConnector(service_account_info=sa_info, sheet_id=sheet_id)


account_directory = dict(st.secrets.get("email_accounts_directory", {}))
if not account_directory:
    st.warning(
        "No accounts configured yet. Add [email_accounts_directory] to Streamlit Secrets — just account "
        "names and addresses, e.g.:\n\n"
        '```toml\n[email_accounts_directory]\nsales1 = "sales1@yourdomain.com"\nsales2 = "sales2@yourdomain.com"\n```\n\n'
        "No app passwords needed here — those stay in GitHub Secrets only."
    )
    st.stop()

settings = outreach.load_settings(SETTINGS_PATH)
default_account = settings.get("email_accounts", {}).get("default_account", "")

try:
    campaigns = list_campaigns()
except Exception as exc:  # noqa: BLE001
    st.error(f"Couldn't list campaigns: {exc}")
    campaigns = []


@st.cache_data(ttl=30, show_spinner=False)
def _load_send_logs(campaign_names):
    connector = _get_connector()
    logs = {}
    unavailable = []
    for name in campaign_names:
        try:
            campaign_cfg = get_campaign_cfg(name)
            logs[name] = connector.get_all_send_log(campaign_cfg["send_log_tab"])
        except ReadOnlySheetsError:
            unavailable.append(name)
    return logs, unavailable


send_logs_by_campaign, unavailable_campaigns = _load_send_logs(tuple(campaigns))
sent_today_by_account = aggregate_sent_today_by_account(send_logs_by_campaign)
rows = build_account_rows(account_directory, sent_today_by_account, default_account)

cols = st.columns(len(rows)) if rows else []
for col, row in zip(cols, rows):
    label = row["name"] + (" ⭐ default" if row["is_default"] else "")
    col.metric(label, f"{row['sent_today']} sent today")
    col.caption(row["address"])

st.divider()
st.dataframe(
    {
        "Account": [r["name"] for r in rows],
        "Address": [r["address"] for r in rows],
        "Sent Today (all campaigns)": [r["sent_today"] for r in rows],
        "Default": ["Yes" if r["is_default"] else "" for r in rows],
    },
    use_container_width=True,
    hide_index=True,
)

if unavailable_campaigns:
    st.caption(
        f"{len(unavailable_campaigns)} campaign(s) not counted yet (no Send Log tab exists — "
        f"nothing sent for them yet): {', '.join(unavailable_campaigns)}"
    )
