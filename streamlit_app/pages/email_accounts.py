import json
import os
import sys

import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from page_state import mark_active_page  # noqa: E402
from auth import login_gate, current_user  # noqa: E402
from config import REPO_ROOT, SETTINGS_PATH, WORKFLOW_CHECK_ACCOUNT_HEALTH, EMAIL_ACCOUNT_SLOT_MAPPING_ABS_PATH  # noqa: E402
from preview_logic import list_campaigns, get_campaign_cfg  # noqa: E402
from sheets_readonly import ReadOnlySheetsConnector, ReadOnlySheetsError  # noqa: E402
from github_client import GitHubClient, GitHubActionsError  # noqa: E402
from accounts_logic import (  # noqa: E402
    aggregate_sent_today_by_account, build_account_rows, build_health_lookup, merge_account_directories,
)
from email_account_slots_logic import (  # noqa: E402
    SLOT_MAPPING_PATH, serialize_slot_mapping, add_account_to_mapping,
    remove_account_from_mapping, update_account_address_in_mapping, read_local_slot_mapping,
)

if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
import outreach  # noqa: E402

# Page config is set once, centrally, in app.py via st.navigation/st.Page —
# calling st.set_page_config here too would raise an error.

# Computed exactly once per script run — used both to gate login normally
# and, later, to reset the Add Account dialog only on a genuine return
# visit (not a rerun triggered by a widget inside the dialog itself).
_just_arrived = mark_active_page("email_accounts")

if not login_gate():
    st.stop()

st.title("📧 Email Accounts")
st.caption(
    "Shows which sender accounts are configured and how much each has sent today — never the actual "
    "SMTP credentials. Add/Edit/Remove briefly passes a password through this app's memory only for the "
    "instant it takes to encrypt and send it to GitHub — it's never stored, logged, or displayed anywhere."
)


@st.cache_resource(show_spinner=False)
def _get_connector() -> ReadOnlySheetsConnector:
    sa_info = dict(st.secrets["google_sheets_readonly"]["service_account_json"])
    sheet_id = st.secrets.get("shared_sheet_id", "")
    return ReadOnlySheetsConnector(service_account_info=sa_info, sheet_id=sheet_id)


@st.cache_resource(show_spinner=False)
def _get_github_client() -> GitHubClient:
    gh = st.secrets["github"]
    return GitHubClient(token=gh["token"], owner=gh["owner"], repo=gh["repo"])


slot_mapping = read_local_slot_mapping(EMAIL_ACCOUNT_SLOT_MAPPING_ABS_PATH)
streamlit_secret_directory = dict(st.secrets.get("email_accounts_directory", {}))
account_directory = merge_account_directories(streamlit_secret_directory, slot_mapping)

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


@st.cache_data(ttl=120, show_spinner=False)
def _load_account_health():
    return _get_connector().get_account_health()


# =============================================================================
# Add Account — a real dialog, same session-state pattern (and same
# navigation-reopen fix) as the New Campaign dialog on the Campaigns page.
# =============================================================================
@st.dialog("Add Email Account")
def _add_account_dialog(current_mapping):
    st.caption(
        "The password is encrypted in your browser's connection to GitHub before it's ever sent — this "
        "app never stores it. If this name matches an account currently only in EMAIL_ACCOUNTS_JSON, "
        "adding it here takes over management of that account going forward."
    )
    name = st.text_input("Account name (e.g. sales1)", key="add_account_name")
    address = st.text_input("Email address", key="add_account_address")
    app_password = st.text_input("App password (16 characters, no spaces)", type="password",
                                  key="add_account_password")
    confirm = st.checkbox("Add this account now", key="add_account_confirm")

    col1, col2 = st.columns(2)
    with col1:
        submit_clicked = st.button("Add Account", type="primary", disabled=not confirm)
    with col2:
        if st.button("Cancel", key="cancel_add_account"):
            st.session_state["show_add_account_dialog"] = False
            st.rerun()

    if submit_clicked:
        errors = []
        if not name or not name.strip():
            errors.append("Account name is required.")
        if not address or "@" not in address:
            errors.append("A valid email address is required.")
        if not app_password or not app_password.strip():
            errors.append("App password is required.")

        if errors:
            for e in errors:
                st.error(e)
        else:
            try:
                updated_mapping = add_account_to_mapping(current_mapping, name.strip(), address.strip())
                slot = updated_mapping[name.strip()]["slot"]
                secret_payload = json.dumps({
                    "name": name.strip(), "address": address.strip(), "app_password": app_password.strip(),
                })
                client = _get_github_client()
                client.set_secret(f"EMAIL_ACCOUNT_SLOT_{slot}", secret_payload)
                client.create_file(
                    SLOT_MAPPING_PATH, serialize_slot_mapping(updated_mapping),
                    message=f"Add email account '{name.strip()}' to slot {slot} (via Streamlit, by {current_user()})",
                )
                try:
                    client.dispatch_workflow(WORKFLOW_CHECK_ACCOUNT_HEALTH, {})
                except GitHubActionsError:
                    pass  # non-critical — the periodic health check will still catch it within 2 hours
                st.session_state["show_add_account_dialog"] = False
                st.success(
                    f"'{name.strip()}' added to slot {slot}. May take a minute to reflect here while the "
                    "app redeploys — a connection check was triggered too."
                )
            except (ValueError, GitHubActionsError) as exc:
                st.error(f"Failed to add account: {exc}")


if "show_add_account_dialog" not in st.session_state:
    st.session_state["show_add_account_dialog"] = False
if st.button("➕ Add Account"):
    st.session_state["show_add_account_dialog"] = True
# Reset on genuine navigation, not on reruns triggered by widgets inside
# the dialog itself — same fix as the New Campaign dialog on Campaigns.
# Uses the SAME _just_arrived computed once at the top of the script;
# calling mark_active_page a second time here would always see "no
# change" (the first call just set it), silently breaking this check.
if _just_arrived:
    st.session_state["show_add_account_dialog"] = False
if st.session_state["show_add_account_dialog"]:
    _add_account_dialog(slot_mapping)

if not account_directory:
    st.info(
        "No accounts configured yet — click ➕ Add Account above, or (legacy path) add "
        "[email_accounts_directory] to Streamlit Secrets for accounts still managed directly in "
        "EMAIL_ACCOUNTS_JSON."
    )
    st.stop()

if st.button("🔄 Refresh"):
    _load_send_logs.clear()
    _load_account_health.clear()

send_logs_by_campaign, unavailable_campaigns = _load_send_logs(tuple(campaigns))
sent_today_by_account = aggregate_sent_today_by_account(send_logs_by_campaign)

health_records = _load_account_health()
health_lookup = build_health_lookup(health_records)
rows = build_account_rows(account_directory, sent_today_by_account, default_account, health_lookup=health_lookup)

_STATUS_ICONS = {"Connected": "🟢", "Disconnected": "🔴", "Unknown": "⚪"}

st.dataframe(
    {
        "Account": [r["name"] + (" ⭐" if r["is_default"] else "") for r in rows],
        "Address": [r["address"] for r in rows],
        "Status": [f"{_STATUS_ICONS.get(r['status'], '⚪')} {r['status']}" for r in rows],
        "Detail": [r["status_detail"] if r["status"] == "Disconnected" else "" for r in rows],
        "Last Checked": [r["checked_at"] or "—" for r in rows],
        "Sent Today (all campaigns)": [r["sent_today"] for r in rows],
        "Default": ["Yes" if r["is_default"] else "" for r in rows],
    },
    width="stretch",
    hide_index=True,
)

if not health_records:
    st.caption(
        "No connection status yet — the 'Check Account Health' workflow runs automatically every 2 "
        "hours, or trigger it manually from the GitHub Actions tab to check now."
    )

if unavailable_campaigns:
    st.caption(
        f"{len(unavailable_campaigns)} campaign(s) not counted yet (no Send Log tab exists — "
        f"nothing sent for them yet): {', '.join(unavailable_campaigns)}"
    )

# =============================================================================
# Manage — Edit/Remove, only for accounts tracked in the slot mapping file.
# An account that only exists in the legacy EMAIL_ACCOUNTS_JSON / Streamlit
# secret isn't manageable here — its slot isn't known (GitHub Secrets can't
# be read back to find out). Use Add Account with the same name to bring
# it under this app's management first.
# =============================================================================
manageable_names = sorted(slot_mapping.keys())
if manageable_names:
    st.divider()
    st.subheader("Manage accounts")
    st.caption("Only accounts added or migrated through this app can be edited or removed here.")

    for name in manageable_names:
        entry = slot_mapping[name]
        with st.expander(f"⚙️ {name} (slot {entry['slot']})"):
            new_address = st.text_input("Email address", value=entry["address"], key=f"manage_address_{name}")
            new_password = st.text_input(
                "New app password (leave blank to keep the current one)", type="password",
                key=f"manage_password_{name}",
            )

            edit_col, remove_col = st.columns(2)
            with edit_col:
                if st.button("💾 Save Changes", key=f"manage_save_{name}"):
                    try:
                        client = _get_github_client()
                        if new_password and new_password.strip():
                            secret_payload = json.dumps({
                                "name": name, "address": new_address.strip(), "app_password": new_password.strip(),
                            })
                            client.set_secret(f"EMAIL_ACCOUNT_SLOT_{entry['slot']}", secret_payload)
                        if new_address.strip() != entry["address"]:
                            updated_mapping = update_account_address_in_mapping(slot_mapping, name, new_address.strip())
                            client.create_file(
                                SLOT_MAPPING_PATH, serialize_slot_mapping(updated_mapping),
                                message=f"Update address for '{name}' (via Streamlit, by {current_user()})",
                            )
                        st.success(f"'{name}' updated. May take a minute to reflect here while the app redeploys.")
                    except (ValueError, GitHubActionsError) as exc:
                        st.error(f"Failed to update: {exc}")

            with remove_col:
                confirm_remove = st.checkbox("Confirm removal", key=f"manage_confirm_remove_{name}")
                if st.button("🗑️ Remove", key=f"manage_remove_{name}", disabled=not confirm_remove):
                    try:
                        client = _get_github_client()
                        client.delete_secret(f"EMAIL_ACCOUNT_SLOT_{entry['slot']}")
                        updated_mapping = remove_account_from_mapping(slot_mapping, name)
                        client.create_file(
                            SLOT_MAPPING_PATH, serialize_slot_mapping(updated_mapping),
                            message=f"Remove email account '{name}' (via Streamlit, by {current_user()})",
                        )
                        st.success(f"'{name}' removed. May take a minute to reflect here while the app redeploys.")
                    except GitHubActionsError as exc:
                        st.error(f"Failed to remove: {exc}")
