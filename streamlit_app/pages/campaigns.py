import os
import sys
import time

import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from auth import login_gate, current_user  # noqa: E402
from page_state import mark_active_page  # noqa: E402
from config import WORKFLOW_IMPORT_LEADS, WORKFLOW_REMOVE_LEADS, WORKFLOW_DASHBOARD, TEMPLATES_ROOT, CAMPAIGNS_DIR  # noqa: E402
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
from sequences_logic import (  # noqa: E402
    get_existing_stages_and_variants, load_variant_content, next_available_variant_letter,
    build_variant_edit_file, build_new_variant_files_for_all_stages, validate_new_variant_contents,
    has_content_changed,
)
from campaign_builder import (  # noqa: E402
    get_next_stage_for_campaign, build_campaign_files, validate_variant_content,
    validate_campaign_name, commit_message_for_campaign,
)
from settings_logic import (  # noqa: E402
    load_raw_override, validate_settings, build_updated_override, override_to_yaml_bytes, override_file_path,
)
from schedule_logic import (  # noqa: E402
    validate_schedule, build_updated_schedule_override, get_current_schedule,
    timezone_display_name, COMMON_TIMEZONES, DAY_OPTIONS,
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


@st.cache_data(ttl=30, show_spinner=False)
def _fetch_full_campaign_data_cached(campaign_name: str):
    """Cached by campaign_name (a plain string — hashable, a clean cache
    key), NOT by campaign_cfg (a dict — unhashable, and re-fetching per
    rerun regardless of ttl was exactly the Sheets-quota bug this fixes).
    Streamlit reruns the ENTIRE script on almost every widget interaction
    — every mapping dropdown touched while setting up a CSV import, every
    filter change, every keystroke in search — so without this cache, a
    few minutes of ordinary use on this page alone can comfortably exceed
    Google's 60-reads/minute/user quota and return a 429."""
    campaign_cfg = get_campaign_cfg(campaign_name)
    leads, responses, send_log = _fetch_sheet_data(campaign_cfg)
    error_log = _get_connector().get_all_error_log(campaign_cfg["error_log_tab"])
    return leads, responses, send_log, error_log


@st.cache_data(ttl=30, show_spinner=False)
def _get_master_header_cached(campaign_name: str):
    """Same fix, same reason — this was being re-fetched on every single
    mapping-dropdown adjustment during CSV import, which is precisely the
    highest-interaction-density moment on this whole page."""
    campaign_cfg = get_campaign_cfg(campaign_name)
    return _get_connector().get_header(campaign_cfg["master_tab"])


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
# New Campaign — inline modal, no separate page. Minimal by design: just a
# name and one Intro variant. Everything else (more variants, follow-ups,
# leads, schedule, settings) happens after, inside the campaign's own
# Sequences/Data/Settings tabs — this dialog's only job is getting a new
# campaign to the point where it can be opened at all.
# =============================================================================
def _initialize_campaign_tabs(campaign_name: str) -> None:
    """Triggers the Dashboard workflow right after a commit — running it
    connects to the Sheet, which creates every needed tab (Master,
    Responses, Send Log, Error Log, Dashboard) with the correct header as
    a side effect. This is what avoids the "tab doesn't exist" error on
    the first visit to a brand new campaign — no manual Preview/Send run
    required first."""
    try:
        client = _get_github_client()
        client.dispatch_workflow(WORKFLOW_DASHBOARD, {"campaign": campaign_name})
    except GitHubActionsError as exc:
        st.warning(
            f"Campaign created, but couldn't auto-initialize its Sheet tabs: {exc}. "
            f"Run 'Update Dashboard' manually for '{campaign_name}' from the GitHub Actions "
            "tab, or just open the campaign once — either will create them."
        )


PLACEHOLDER_INTRO_SUBJECT = "Write your subject here"
PLACEHOLDER_INTRO_BODY = (
    "Write your intro email here.\n\n"
    "This is placeholder text — edit it in the Sequences tab before sending anything."
)


@st.dialog("New Campaign")
def _new_campaign_dialog(existing_campaigns):
    st.caption(
        "Just the name — you'll write the actual email in the Sequences tab once the campaign exists "
        "(a placeholder Intro is created for you to edit there)."
    )
    campaign_name = st.text_input("Campaign name (letters, numbers, underscores only)")
    confirm = st.checkbox("Create now — it's live immediately, no approval step")

    col1, col2 = st.columns(2)
    with col1:
        create_clicked = st.button("Create Campaign", type="primary", disabled=not confirm)
    with col2:
        if st.button("Cancel"):
            st.session_state["show_new_campaign_dialog"] = False
            st.rerun()

    if create_clicked:
        name_error = validate_campaign_name(campaign_name, existing_campaigns)
        if name_error:
            st.error(name_error)
        else:
            try:
                files = build_campaign_files(
                    campaign_name, "intro",
                    {"A": {"subject": PLACEHOLDER_INTRO_SUBJECT, "body": PLACEHOLDER_INTRO_BODY}},
                )
                client = _get_github_client()
                client.commit_campaign_files_directly(
                    files=files,
                    commit_message=commit_message_for_campaign(campaign_name, "intro", 1, current_user(),
                                                                 is_new_campaign=True),
                )
                _load_hub_rows.clear()
                with st.spinner("Initializing Sheet tabs..."):
                    time.sleep(1)
                    _initialize_campaign_tabs(campaign_name)
                # Deliberately NOT auto-navigating into the new campaign:
                # Streamlit Cloud's local checkout only picks up this commit
                # once it redeploys (triggered by the GitHub webhook, not
                # instant) — jumping straight to the detail view right now
                # would very likely hit "No templates found" against the
                # still-stale local checkout. Staying on the hub and saying
                # so plainly is the honest version of this UX.
                st.session_state["show_new_campaign_dialog"] = False
                st.success(
                    f"'{campaign_name}' created with a placeholder Intro. It'll appear in the list below "
                    "within a minute or so, once the app finishes redeploying — open it and go to "
                    "Sequences to write the real email."
                )
            except GitHubActionsError as exc:
                st.error(f"Failed to create campaign: {exc}")


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
        if "show_new_campaign_dialog" not in st.session_state:
            st.session_state["show_new_campaign_dialog"] = False
        if st.button("＋ New Campaign", width="stretch"):
            st.session_state["show_new_campaign_dialog"] = True
        # Re-checked on EVERY rerun, not just the click that opened it —
        # st.dialog only stays visually open across a rerun triggered by a
        # widget INSIDE it if the code path that calls the dialog function
        # is reached again; gating solely on the button's own return value
        # (True only on the exact rerun it was clicked) would silently
        # close the dialog the instant you touched anything inside it.
        #
        # But that sticky flag creates a SECOND problem on its own: it
        # survives navigating to a completely different page and back,
        # since session_state is shared across the whole app — without the
        # check below, the dialog would silently reopen every time you
        # returned to this page, even long after you closed it. Only reset
        # it on a genuine arrival at this page (mark_active_page), never
        # on a rerun caused by a widget inside the dialog itself.
        if mark_active_page("campaigns"):
            st.session_state["show_new_campaign_dialog"] = False
        if st.session_state["show_new_campaign_dialog"]:
            try:
                existing_campaigns = list_campaigns()
            except Exception:  # noqa: BLE001
                existing_campaigns = []
            _new_campaign_dialog(existing_campaigns)

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
                    header = _get_master_header_cached(campaign_cfg["_campaign_name"])
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


def _render_sequences_tab(campaign_cfg, leads):
    campaign_name = campaign_cfg["_campaign_name"]

    try:
        stages, existing_variants = get_existing_stages_and_variants(campaign_name, TEMPLATES_ROOT)
    except Exception as exc:  # noqa: BLE001
        st.error(f"Couldn't read templates for '{campaign_name}': {exc}")
        return

    sample_lead = leads[0] if leads else {}

    # ---------- Edit existing variants ----------
    st.subheader("Edit templates")
    st.caption(
        "Locked by default to prevent accidental changes. Unlock a variant to edit it, then Save Changes "
        "at the bottom commits everything you've changed in one go."
    )
    pending_edits = {}  # (stage_prefix, variant) -> (subject, body)

    for idx, stage in enumerate(stages):
        prefix = stage["template_prefix"]
        is_first_stage = (idx == 0)
        st.markdown(f"**{stage['name']}**")
        for variant in existing_variants:
            try:
                original = load_variant_content(campaign_name, prefix, variant, TEMPLATES_ROOT)
            except Exception as exc:  # noqa: BLE001
                st.warning(f"{prefix}_{variant}: couldn't load — {exc}")
                continue

            with st.expander(f"Variant {variant}"):
                unlocked = st.checkbox("🔓 Unlock to edit", key=f"unlock_{prefix}_{variant}")
                subject_label = "Subject (required — this is the first stage)" if is_first_stage else \
                    "Subject (blank continues the previous thread)"
                subject = st.text_input(
                    subject_label,
                    value=original["subject"], key=f"subject_{prefix}_{variant}", disabled=not unlocked,
                )
                body = st.text_area("Body", value=original["body"], key=f"body_{prefix}_{variant}",
                                     height=150, disabled=not unlocked)
                if unlocked and has_content_changed(original, subject, body):
                    pending_edits[(prefix, variant)] = (subject, body, is_first_stage)
                    st.caption("✏️ Changed — will be included in Save Changes")

                if sample_lead:
                    preview_subject = outreach.render_text(subject, sample_lead)
                    preview_body = outreach.render_text(body, sample_lead)
                    st.markdown("**Preview** (using your first lead)")
                    st.write(f"Subject: {preview_subject}")
                    st.text(preview_body)

    if pending_edits:
        validation_errors = []
        for (prefix, variant), (subject, body, is_first) in pending_edits.items():
            err = validate_variant_content(subject, body, is_first_stage=is_first)
            if err:
                validation_errors.append(f"{prefix}_{variant}: {err}")

        if validation_errors:
            for e in validation_errors:
                st.error(e)
        elif st.button(f"💾 Save Changes ({len(pending_edits)} template(s))", type="primary"):
            try:
                files = [build_variant_edit_file(campaign_name, prefix, variant, subject, body)
                         for (prefix, variant), (subject, body, _) in pending_edits.items()]
                client = _get_github_client()
                client.commit_campaign_files_directly(
                    files=files,
                    commit_message=f"Edit {len(files)} template(s) in {campaign_name} "
                                    f"(via Streamlit, by {current_user()})",
                )
                st.success(f"Saved {len(files)} template(s). May take a minute to actually reflect here "
                           "while the app redeploys — the change is live for sending immediately either way.")
            except GitHubActionsError as exc:
                st.error(f"Save failed: {exc}")

    st.divider()

    # ---------- Add a new variant (campaign-wide — see sequences_logic docstring) ----------
    next_letter = next_available_variant_letter(existing_variants)
    with st.expander(f"➕ Add variant {next_letter}" if next_letter else "➕ Add variant (maximum reached)"):
        if not next_letter:
            st.info("Every stage already has all 4 variants (A–D) — that's the maximum.")
        else:
            st.caption(f"Variant {next_letter} needs content for EVERY existing stage — variants are "
                       "campaign-wide, not per-stage, so this adds it everywhere at once.")
            contents_by_stage = {}
            for stage in stages:
                prefix = stage["template_prefix"]
                st.markdown(f"**{stage['name']}**")
                subject = st.text_input("Subject", key=f"newvariant_subject_{prefix}")
                body = st.text_area("Body", key=f"newvariant_body_{prefix}", height=120)
                contents_by_stage[prefix] = {"subject": subject, "body": body}

            if st.button(f"Add Variant {next_letter}", type="primary"):
                errors = validate_new_variant_contents(stages, contents_by_stage)
                if errors:
                    for e in errors:
                        st.error(e)
                else:
                    try:
                        files = build_new_variant_files_for_all_stages(campaign_name, stages, next_letter,
                                                                        contents_by_stage)
                        client = _get_github_client()
                        client.commit_campaign_files_directly(
                            files=files,
                            commit_message=f"Add variant {next_letter} to {campaign_name} "
                                            f"(via Streamlit, by {current_user()})",
                        )
                        st.success(f"Variant {next_letter} added to all {len(stages)} stage(s). May take a "
                                   "minute to actually reflect here while the app redeploys — it's live for "
                                   "sending immediately either way.")
                    except GitHubActionsError as exc:
                        st.error(f"Failed to add variant: {exc}")

    # ---------- Add a follow-up stage (reuses the exact New Campaign "Add Stage" logic) ----------
    try:
        next_stage = get_next_stage_for_campaign(campaign_name, TEMPLATES_ROOT)
    except Exception as exc:  # noqa: BLE001
        next_stage = None
        st.error(f"Couldn't determine next stage: {exc}")

    with st.expander("➕ Add a follow-up stage" if next_stage else "➕ Add a follow-up stage (none left)"):
        if next_stage is None:
            st.info("This campaign already has all 5 stages.")
        else:
            stage_prefix, required_variants = next_stage
            st.write(f"**Next stage:** `{stage_prefix}` · **Required variants:** {', '.join(required_variants)}")
            variant_inputs = {}
            for letter in required_variants:
                st.markdown(f"**Variant {letter}**")
                subject = st.text_input(
                    "Subject (leave blank to continue the previous thread)",
                    key=f"followup_subject_{letter}",
                )
                body = st.text_area("Body", key=f"followup_body_{letter}", height=120)
                variant_inputs[letter] = {"subject": subject, "body": body}

            if st.button(f"Add {stage_prefix}", type="primary"):
                errors = []
                for letter, content in variant_inputs.items():
                    content_error = validate_variant_content(content["subject"], content["body"],
                                                               is_first_stage=False)
                    if content_error:
                        errors.append(f"Variant {letter}: {content_error}")
                if errors:
                    for e in errors:
                        st.error(e)
                else:
                    try:
                        files = build_campaign_files(campaign_name, stage_prefix, variant_inputs)
                        client = _get_github_client()
                        client.commit_campaign_files_directly(
                            files=files,
                            commit_message=f"Add {stage_prefix} to {campaign_name} "
                                            f"(via Streamlit, by {current_user()})",
                        )
                        st.success(f"'{stage_prefix}' added. May take a minute to actually reflect here while "
                                   "the app redeploys — it's live for sending immediately either way.")
                    except GitHubActionsError as exc:
                        st.error(f"Failed to add stage: {exc}")


def _render_settings_tab(campaign_cfg):
    campaign_name = campaign_cfg["_campaign_name"]
    sending = campaign_cfg.get("sending", {})

    account_directory = dict(st.secrets.get("email_accounts_directory", {}))
    available_accounts = list(account_directory.keys())

    st.subheader("Sender accounts")
    if not available_accounts:
        st.info(
            "No accounts configured in Streamlit Secrets yet — add [email_accounts_directory] "
            "(see the Email Accounts page) to pick specific accounts here. Sending will still use "
            "whatever's configured directly in EMAIL_ACCOUNTS_JSON either way."
        )
        rotation_accounts = list(sending.get("rotation_accounts") or [])
    else:
        # Driven entirely by session_state (keyed per campaign) rather than
        # a `default=` argument — that's what actually lets "Select all"
        # work. A plain `rotation_accounts = available_accounts` after the
        # button click only reassigns a local variable for this one run;
        # the multiselect widget's own displayed state doesn't change, and
        # a LATER click on Save re-reads the widget fresh, silently
        # discarding what "Select all" did. Mutating session_state before
        # the widget is created, then rerunning, is what actually sticks.
        ms_key = f"settings_rotation_accounts_{campaign_name}"
        if ms_key not in st.session_state:
            st.session_state[ms_key] = [a for a in (sending.get("rotation_accounts") or [])
                                         if a in available_accounts]

        if st.button("Select all accounts"):
            st.session_state[ms_key] = available_accounts
            st.rerun()

        rotation_accounts = st.multiselect(
            "🔍 Search sender accounts", available_accounts, key=ms_key,
            help="Leave empty to use every configured account for rotation.",
        )

    sender_rotation = st.checkbox("Rotate across multiple sender accounts", value=bool(sending.get("sender_rotation")))

    st.divider()
    st.subheader("Sending limits")
    daily_limit = st.number_input("Daily limit (across all accounts)", min_value=1,
                                   value=int(sending.get("daily_limit", 100)))
    has_per_account_limit = st.checkbox("Set a per-account daily limit",
                                         value=sending.get("per_account_daily_limit") is not None)
    per_account_daily_limit = None
    if has_per_account_limit:
        per_account_daily_limit = st.number_input(
            "Per-account daily limit", min_value=1,
            value=int(sending.get("per_account_daily_limit") or 20),
        )

    st.divider()
    if st.button("💾 Save Settings", type="primary"):
        errors = validate_settings(daily_limit, per_account_daily_limit)
        if errors:
            for e in errors:
                st.error(e)
        else:
            try:
                raw_override = load_raw_override(campaign_name, CAMPAIGNS_DIR)
                updated = build_updated_override(raw_override, daily_limit, per_account_daily_limit,
                                                  sender_rotation, rotation_accounts)
                client = _get_github_client()
                client.create_file(
                    override_file_path(campaign_name), override_to_yaml_bytes(updated),
                    message=f"Update settings for {campaign_name} (via Streamlit, by {current_user()})",
                )
                st.success("Settings saved. May take a minute to actually reflect here while the app "
                           "redeploys — it's in effect for sending immediately either way.")
            except GitHubActionsError as exc:
                st.error(f"Save failed: {exc}")


def _render_schedule_tab(campaign_cfg):
    campaign_name = campaign_cfg["_campaign_name"]
    current = get_current_schedule(campaign_cfg)

    st.caption(
        "Leave this alone and your campaign sends anytime — matching how it's always worked. Setting a "
        "schedule restricts Send Batch to only run within the window and days you choose below (Preview "
        "still works anytime either way)."
    )

    display_names = [d for d, _ in COMMON_TIMEZONES]
    iana_names = [i for _, i in COMMON_TIMEZONES]
    current_display = timezone_display_name(current["timezone"]) or display_names[0]
    selected_display = st.selectbox("Time zone", display_names, index=display_names.index(current_display))
    selected_timezone = iana_names[display_names.index(selected_display)]

    col1, col2 = st.columns(2)
    with col1:
        window_start = st.text_input("Start time (24-hour, HH:MM)", value=current["window_start"])
    with col2:
        window_end = st.text_input("End time (24-hour, HH:MM)", value=current["window_end"])

    st.write("Days")
    day_cols = st.columns(7)
    send_days = []
    for col, (label, code) in zip(day_cols, DAY_OPTIONS):
        with col:
            if st.checkbox(label[:3], value=code in current["send_days"], key=f"schedule_day_{code}"):
                send_days.append(code)

    if st.button("💾 Save Schedule", type="primary"):
        errors = validate_schedule(selected_timezone, window_start, window_end, send_days)
        if errors:
            for e in errors:
                st.error(e)
        else:
            try:
                raw_override = load_raw_override(campaign_name, CAMPAIGNS_DIR)
                updated = build_updated_schedule_override(raw_override, selected_timezone, window_start,
                                                            window_end, send_days)
                client = _get_github_client()
                client.create_file(
                    override_file_path(campaign_name), override_to_yaml_bytes(updated),
                    message=f"Update schedule for {campaign_name} (via Streamlit, by {current_user()})",
                )
                st.success("Schedule saved. May take a minute to actually reflect here while the app "
                           "redeploys — it's in effect for sending immediately either way.")
            except GitHubActionsError as exc:
                st.error(f"Save failed: {exc}")


def _render_stub_tab(tab_name: str, phase_letter: str):
    st.info(
        f"**{tab_name} isn't built yet.** This is planned as Phase {phase_letter} — see the Campaigns Hub "
        "plan for what it'll do. For now, use the Controls / New Campaign pages directly for this campaign."
    )


def _render_campaign_detail(campaign_name: str):
    col1, col2 = st.columns([4, 1])
    with col1:
        if st.button("← Back to Campaigns"):
            del st.session_state["selected_campaign"]
            st.rerun()
    with col2:
        if st.button("🔄 Refresh data"):
            _fetch_full_campaign_data_cached.clear()
            _get_master_header_cached.clear()

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
            leads, responses, send_log, error_log = _fetch_full_campaign_data_cached(campaign_name)
        except Exception as exc:  # noqa: BLE001
            st.warning(f"Couldn't load Sheet data yet: {exc}")
            leads, responses, send_log, error_log = [], [], [], []

    tabs = st.tabs(["📊 Analytics", "📋 Data", "✉️ Sequences", "📅 Schedule", "⚙️ Settings", "💬 Responses"])
    with tabs[0]:
        _render_analytics_tab(campaign_cfg, leads, responses, send_log, error_log)
    with tabs[1]:
        _render_data_tab(campaign_cfg, leads)
    with tabs[2]:
        _render_sequences_tab(campaign_cfg, leads)
    with tabs[3]:
        _render_schedule_tab(campaign_cfg)
    with tabs[4]:
        _render_settings_tab(campaign_cfg)
    with tabs[5]:
        _render_stub_tab("Responses (inbox, reply-from-app)", "H")


# =============================================================================
selected = st.session_state.get("selected_campaign")
if selected:
    _render_campaign_detail(selected)
else:
    _render_hub()
