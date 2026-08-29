import os
import sys
import time

import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from auth import login_gate, current_user  # noqa: E402
from config import WORKFLOW_IMPORT_LEADS, WORKFLOW_REMOVE_LEADS  # noqa: E402
from github_client import GitHubClient, GitHubActionsError  # noqa: E402
from preview_logic import list_campaigns, get_campaign_cfg  # noqa: E402
from sheets_readonly import ReadOnlySheetsConnector  # noqa: E402
from campaigns_hub_logic import build_campaigns_hub, filter_campaigns_by_search  # noqa: E402
from campaign_analytics_logic import (  # noqa: E402
    build_overview_summary, build_per_stage_table, build_per_variant_table,
    build_sender_table, build_error_summary,
)
from data_import_logic import (  # noqa: E402
    parse_csv_bytes, build_default_mapping, apply_mapping, validate_mapping, count_valid_rows,
    build_import_payload, import_payload_path, build_removal_payload, removal_payload_path,
    payload_to_bytes, filter_leads, search_leads, KNOWN_FIELDS, FILTER_OPTIONS,
)

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
import outreach  # noqa: E402

if not login_gate():
    st.stop()


@st.cache_resource(show_spinner=False)
def _get_github_client() -> GitHubClient:
    gh = st.secrets["github"]
    return GitHubClient(token=gh["token"], owner=gh["owner"], repo=gh["repo"])


@st.cache_resource(show_spinner=False)
def _get_connector() -> ReadOnlySheetsConnector:
    sa_info = dict(st.secrets["google_sheets_readonly"]["service_account_json"])
    sheet_id = st.secrets.get("shared_sheet_id", "")
    return ReadOnlySheetsConnector(service_account_info=sa_info, sheet_id=sheet_id)


def _fetch_sheet_data(campaign_cfg):
    connector = _get_connector()
    leads = connector.get_all_leads(campaign_cfg["master_tab"])
    responses = connector.get_all_responses(campaign_cfg["responses_tab"])
    send_log = connector.get_all_send_log(campaign_cfg["send_log_tab"])
    return leads, responses, send_log


def _fetch_full_campaign_data(campaign_cfg):
    leads, responses, send_log = _fetch_sheet_data(campaign_cfg)
    error_log = _get_connector().get_all_error_log(campaign_cfg["error_log_tab"])
    return leads, responses, send_log, error_log


@st.cache_data(ttl=30, show_spinner=False)
def _load_hub_rows():
    campaign_names = list_campaigns()
    return build_campaigns_hub(campaign_names, get_campaign_cfg, _fetch_sheet_data)


def _relative_time(timestamp_str: str) -> str:
    if not timestamp_str:
        return "—"
    try:
        from datetime import datetime
        dt = datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return timestamp_str
    delta = datetime.now() - dt
    seconds = delta.total_seconds()
    if seconds < 0:
        return timestamp_str
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)} hr ago"
    return f"{int(seconds // 86400)} day(s) ago"


# =============================================================================
# Hub view — list, search, click into a campaign
# =============================================================================
def _render_hub():
    st.title("Campaigns")

    col1, col2 = st.columns([3, 1])
    with col1:
        search = st.text_input("🔍 Search campaigns...", label_visibility="collapsed",
                                placeholder="🔍 Search campaigns...")
    with col2:
        if st.button("＋ New Campaign", width="stretch"):
            st.switch_page("pages/new_campaign.py")

    try:
        rows, errors = _load_hub_rows()
    except Exception as exc:  # noqa: BLE001
        st.error(f"Couldn't load campaigns: {exc}")
        st.stop()

    rows = filter_campaigns_by_search(rows, search)

    if not rows:
        st.info("No campaigns match yet." if search else "No campaigns yet — create one to get started.")
    else:
        for row in rows:
            with st.container(border=True):
                c1, c2, c3, c4, c5, c6 = st.columns([3, 2, 1, 1, 1, 2])
                c1.markdown(f"**{row['name']}**")
                c2.write(row["status_label"])
                c3.write(f"{row['total_leads']} leads")
                c4.write(f"{row['sent']} sent")
                c5.write(f"{row['replies']} replies")
                c6.write(f"Last activity: {_relative_time(row['last_activity'])}")
                if row["problems"]:
                    c2.caption(", ".join(row["problems"]))
                if st.button("Open", key=f"open_{row['name']}"):
                    st.session_state["selected_campaign"] = row["name"]
                    st.rerun()

    if errors:
        with st.expander(f"{len(errors)} campaign(s) couldn't be loaded"):
            for name, message in errors:
                st.write(f"**{name}** — {message}")


# =============================================================================
# Detail view — Analytics is real (Phase B); other tabs are honest stubs.
# =============================================================================
def _render_analytics_tab(campaign_cfg, leads, responses, send_log, error_log):
    dashboard_rows = outreach.compute_campaign_dashboard(campaign_cfg, leads, responses, send_log, error_log)
    overview = build_overview_summary(dashboard_rows)

    cols = st.columns(4)
    cols[0].metric("Total Leads", overview.get("Total Leads (with Email)", "—"))
    cols[1].metric("Emails Sent", overview.get("Total Emails Sent", "—"))
    cols[2].metric("Replies", overview.get("Genuine Replies", "—"))
    cols[3].metric("Reply Rate", overview.get("Reply Rate (Replies / Unique Contacted)", "—"))

    st.divider()
    st.subheader("By stage")
    stage_table = build_per_stage_table(dashboard_rows)
    if stage_table:
        st.dataframe(
            {"Stage": [r["stage"] for r in stage_table], "Sent": [r["sent"] for r in stage_table]},
            width="stretch", hide_index=True,
        )
    else:
        st.caption("No sends yet.")

    st.subheader("By variant")
    variant_table = build_per_variant_table(dashboard_rows)
    if variant_table:
        st.dataframe(
            {
                "Stage": [r["stage"] for r in variant_table],
                "Variant": [r["variant"] for r in variant_table],
                "Sent": [r.get("sent", "0") for r in variant_table],
                "Replies": [r.get("replies", "0") for r in variant_table],
                "Reply Rate": [r.get("reply_rate", "—") for r in variant_table],
            },
            width="stretch", hide_index=True,
        )
    else:
        st.caption("No sends yet.")

    st.subheader("By sender account")
    sender_table = build_sender_table(dashboard_rows)
    if sender_table:
        st.dataframe(
            {
                "Account": [r["account"] for r in sender_table],
                "Sent": [r.get("sent", "0") for r in sender_table],
                "Replies": [r.get("replies", "0") for r in sender_table],
                "Reply Rate": [r.get("reply_rate", "—") for r in sender_table],
            },
            width="stretch", hide_index=True,
        )
    else:
        st.caption("No sends yet.")

    error_summary = build_error_summary(dashboard_rows)
    if error_summary:
        st.subheader("Errors (all time)")
        st.dataframe(
            {"Error type": [r["error_type"] for r in error_summary], "Count": [r["count"] for r in error_summary]},
            width="stretch", hide_index=True,
        )


def _render_data_tab(campaign_cfg, leads):
    campaign_name = campaign_cfg["_campaign_name"]

    with st.expander("➕ Add Leads (upload CSV)", expanded=not leads):
        uploaded = st.file_uploader("Upload CSV", type=["csv"], key="data_csv_upload")
        if uploaded is not None:
            columns, rows = parse_csv_bytes(uploaded.getvalue())
            if not columns:
                st.error("Couldn't read any columns from that file — is it a valid CSV?")
            else:
                try:
                    connector = _get_connector()
                    header = connector.get_header(campaign_cfg["master_tab"])
                    custom_columns = [c for c in header if c not in outreach.MASTER_COLUMNS]
                except Exception:  # noqa: BLE001 - tab may not exist yet for a brand new campaign
                    custom_columns = []

                st.caption(f"{len(rows)} row(s) detected. Map each column below (or leave as Skip).")
                mapping = {}
                default_mapping = build_default_mapping(columns, custom_columns)
                target_options = ["-- Skip --"] + KNOWN_FIELDS + custom_columns
                for col in columns:
                    default = default_mapping.get(col) or "-- Skip --"
                    default_idx = target_options.index(default) if default in target_options else 0
                    choice = st.selectbox(f"'{col}' maps to", target_options, index=default_idx,
                                           key=f"map_{col}")
                    mapping[col] = "" if choice == "-- Skip --" else choice

                mapped_rows = apply_mapping(rows, mapping)
                valid_count = count_valid_rows(mapped_rows)
                mapping_error = validate_mapping(mapping)

                st.write(f"**{valid_count} of {len(rows)} row(s) have an email and will be imported** "
                         "(others are skipped — Email is required). Duplicates against existing leads "
                         "are also skipped automatically, checked at import time.")
                st.caption("Imported leads start as **Pending** — approve them below before they're eligible to send.")

                if mapping_error:
                    st.error(mapping_error)
                elif st.button("Import Leads", type="primary", key="confirm_import"):
                    try:
                        client = _get_github_client()
                        payload = build_import_payload(mapped_rows)
                        path = import_payload_path(campaign_name)
                        client.create_file(path, payload_to_bytes(payload),
                                            message=f"Import {valid_count} lead(s) for {campaign_name} "
                                                     f"(via Streamlit, by {current_user()})")
                        time.sleep(1)
                        client.dispatch_workflow(WORKFLOW_IMPORT_LEADS,
                                                  {"campaign": campaign_name, "payload_path": path})
                        st.success(f"Import triggered — {valid_count} lead(s) will appear within a minute or two.")
                    except GitHubActionsError as exc:
                        st.error(f"Import failed: {exc}")

    st.divider()
    st.subheader("Leads")
    if not leads:
        st.info("No leads yet — add some above.")
        return

    col1, col2 = st.columns([2, 1])
    with col1:
        search_query = st.text_input("Search", key="data_search", placeholder="Name, email, or company...")
    with col2:
        status_filter = st.selectbox("Filter", FILTER_OPTIONS, key="data_filter")

    filtered = search_leads(filter_leads(leads, status_filter), search_query)
    st.caption(f"Showing {len(filtered)} of {len(leads)} lead(s)")

    if filtered:
        st.dataframe(
            {
                "LeadID": [l.get("LeadID", "") for l in filtered],
                "Name": [f"{l.get('FirstName', '')} {l.get('LastName', '')}".strip() for l in filtered],
                "Email": [l.get("Email", "") for l in filtered],
                "Company": [l.get("Company", "") for l in filtered],
                "Approval": [l.get("Approval", "") or "Pending" for l in filtered],
                "Status": [l.get("Status", "") or "—" for l in filtered],
            },
            width="stretch", hide_index=True,
        )

        with st.expander("🗑️ Remove leads"):
            st.caption(
                "Removed leads are never deleted — their row and history stay intact, they're just "
                "marked Removed and excluded from all future sends."
            )
            options = {f"{l.get('LeadID', '')} — {l.get('FirstName', '')} {l.get('LastName', '')} "
                       f"<{l.get('Email', '')}>": l.get("LeadID", "") for l in filtered}
            selected_labels = st.multiselect("Select leads to remove", list(options.keys()), key="remove_select")
            if selected_labels and st.button("Remove Selected", key="confirm_remove"):
                try:
                    lead_ids = [options[label] for label in selected_labels]
                    client = _get_github_client()
                    payload = build_removal_payload(lead_ids)
                    path = removal_payload_path(campaign_name)
                    client.create_file(path, payload_to_bytes(payload),
                                        message=f"Remove {len(lead_ids)} lead(s) from {campaign_name} "
                                                 f"(via Streamlit, by {current_user()})")
                    time.sleep(1)
                    client.dispatch_workflow(WORKFLOW_REMOVE_LEADS,
                                              {"campaign": campaign_name, "payload_path": path})
                    st.success(f"Removal triggered for {len(lead_ids)} lead(s).")
                except GitHubActionsError as exc:
                    st.error(f"Removal failed: {exc}")


def _render_stub_tab(tab_name: str, phase_letter: str):
    st.info(
        f"**{tab_name} isn't built yet.** This is planned as Phase {phase_letter} — see the Campaigns Hub "
        "plan for what it'll do. For now, use the Controls / New Campaign pages directly for this campaign."
    )


def _render_campaign_detail(campaign_name: str):
    if st.button("← Back to Campaigns"):
        del st.session_state["selected_campaign"]
        st.rerun()

    try:
        campaign_cfg = get_campaign_cfg(campaign_name)
    except Exception as exc:  # noqa: BLE001
        st.error(f"Couldn't load '{campaign_name}': {exc}")
        return

    st.title(campaign_name)

    is_draft = (campaign_cfg.get("status") or "active") == "draft"
    if is_draft:
        st.info("📝 Draft — this campaign hasn't been launched yet.")
        leads, responses, send_log, error_log = [], [], [], []
    else:
        try:
            leads, responses, send_log, error_log = _fetch_full_campaign_data(campaign_cfg)
        except Exception as exc:  # noqa: BLE001
            st.warning(f"Couldn't load Sheet data yet: {exc}")
            leads, responses, send_log, error_log = [], [], [], []

    tabs = st.tabs(["📊 Analytics", "📋 Data", "✉️ Sequences", "📅 Schedule", "⚙️ Settings", "💬 Responses"])
    with tabs[0]:
        _render_analytics_tab(campaign_cfg, leads, responses, send_log, error_log)
    with tabs[1]:
        _render_data_tab(campaign_cfg, leads)
    with tabs[2]:
        _render_stub_tab("Sequences (template editor)", "D")
    with tabs[3]:
        _render_stub_tab("Schedule (timezone, window, days)", "E")
    with tabs[4]:
        _render_stub_tab("Settings (senders, limits)", "F")
    with tabs[5]:
        _render_stub_tab("Responses (inbox, reply-from-app)", "H")


# =============================================================================
selected = st.session_state.get("selected_campaign")
if selected:
    _render_campaign_detail(selected)
else:
    _render_hub()
