import os
import sys

import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from auth import login_gate, current_user  # noqa: E402
from config import TEMPLATES_ROOT  # noqa: E402
from github_client import GitHubClient, GitHubActionsError  # noqa: E402
from preview_logic import list_campaigns  # noqa: E402
from campaign_builder import (  # noqa: E402
    validate_campaign_name, validate_variant_content, build_campaign_files,
    get_next_stage_for_campaign, branch_name_for_campaign, pr_title_for_campaign,
    pr_body_for_campaign, VARIANT_LETTERS,
)

st.set_page_config(page_title="New Campaign", page_icon="➕", layout="wide")

if not login_gate():
    st.stop()

st.title("➕ New Campaign / Add Stage")
st.markdown(
    """
Creates template files and opens a **pull request** — it never commits
directly to `main`. Someone still needs to **merge the PR** before the
change is live; that's intentional, since this is the one action in this
app that can introduce new files into the automation repo.
    """
)


@st.cache_resource(show_spinner=False)
def _get_github_client() -> GitHubClient:
    gh = st.secrets["github"]
    return GitHubClient(token=gh["token"], owner=gh["owner"], repo=gh["repo"])


try:
    existing_campaigns = list_campaigns()
except Exception as exc:  # noqa: BLE001
    st.error(f"Couldn't list existing campaigns: {exc}")
    existing_campaigns = []

mode = st.radio(
    "What do you want to do?",
    ["Create a new campaign (Intro)", "Add the next stage to an existing campaign"],
    horizontal=True,
)

# =============================================================================
# Mode 1 — brand new campaign, starts at Intro, variant count is your choice
# =============================================================================
if mode == "Create a new campaign (Intro)":
    campaign_name = st.text_input("Campaign name (letters, numbers, underscores only)")
    num_variants = st.slider("Number of Intro variants (A/B/C/D)", min_value=1, max_value=4, value=1)

    variant_inputs = {}
    for letter in VARIANT_LETTERS[:num_variants]:
        st.subheader(f"Variant {letter}")
        subject = st.text_input(f"Subject ({letter})", key=f"new_subject_{letter}")
        body = st.text_area(f"Body ({letter})", key=f"new_body_{letter}", height=150)
        variant_inputs[letter] = {"subject": subject, "body": body}

    if st.button("Open Pull Request", type="primary", key="new_campaign_submit"):
        errors = []
        name_error = validate_campaign_name(campaign_name, existing_campaigns)
        if name_error:
            errors.append(name_error)
        for letter, content in variant_inputs.items():
            content_error = validate_variant_content(content["subject"], content["body"])
            if content_error:
                errors.append(f"Variant {letter}: {content_error}")

        if errors:
            for e in errors:
                st.error(e)
        else:
            try:
                files = build_campaign_files(campaign_name, "intro", variant_inputs)
                client = _get_github_client()
                branch = branch_name_for_campaign(campaign_name, "intro")
                pr = client.open_campaign_pull_request(
                    branch_name=branch,
                    files=files,
                    pr_title=pr_title_for_campaign(campaign_name, "intro", is_new_campaign=True),
                    pr_body=pr_body_for_campaign(campaign_name, "intro", len(files), current_user(),
                                                  is_new_campaign=True),
                )
                st.success(f"Pull request opened: {pr.get('html_url', '')}")
                st.info("Merge it to make this campaign available to Preview/Send.")
            except GitHubActionsError as exc:
                st.error(f"Failed to open pull request: {exc}")

# =============================================================================
# Mode 2 — add the NEXT stage to an existing campaign. Stage name and
# variant letters are NOT free choices here — they're computed from what
# outreach.py's own auto-discovery would accept next (see
# campaign_builder.get_next_stage_for_campaign), so this can never create
# an inconsistent campaign.
# =============================================================================
else:
    if not existing_campaigns:
        st.info("No existing campaigns to add a stage to yet — create one first.")
    else:
        selected_campaign = st.selectbox("Campaign", existing_campaigns)

        try:
            next_stage = get_next_stage_for_campaign(selected_campaign, TEMPLATES_ROOT)
        except Exception as exc:  # noqa: BLE001
            st.error(f"Couldn't inspect '{selected_campaign}': {exc}")
            next_stage = None

        if next_stage is None:
            st.info(f"'{selected_campaign}' already has all 5 stages — there's nothing left to add.")
        else:
            stage_prefix, required_variants = next_stage
            st.write(f"**Next stage:** `{stage_prefix}` · **Required variants:** {', '.join(required_variants)}")
            st.caption(
                "These aren't a free choice — every stage must offer the exact same variant "
                "letters as the campaign's existing stages, so all of them are required here."
            )

            variant_inputs = {}
            for letter in required_variants:
                st.subheader(f"Variant {letter}")
                subject = st.text_input(f"Subject ({letter})", key=f"stage_subject_{letter}")
                body = st.text_area(f"Body ({letter})", key=f"stage_body_{letter}", height=150)
                variant_inputs[letter] = {"subject": subject, "body": body}

            if st.button("Open Pull Request", type="primary", key="add_stage_submit"):
                errors = []
                for letter, content in variant_inputs.items():
                    content_error = validate_variant_content(content["subject"], content["body"])
                    if content_error:
                        errors.append(f"Variant {letter}: {content_error}")

                if errors:
                    for e in errors:
                        st.error(e)
                else:
                    try:
                        files = build_campaign_files(selected_campaign, stage_prefix, variant_inputs)
                        client = _get_github_client()
                        branch = branch_name_for_campaign(selected_campaign, stage_prefix)
                        pr = client.open_campaign_pull_request(
                            branch_name=branch,
                            files=files,
                            pr_title=pr_title_for_campaign(selected_campaign, stage_prefix, is_new_campaign=False),
                            pr_body=pr_body_for_campaign(selected_campaign, stage_prefix, len(files),
                                                          current_user(), is_new_campaign=False),
                        )
                        st.success(f"Pull request opened: {pr.get('html_url', '')}")
                        st.info(f"Merge it to make '{stage_prefix}' active for '{selected_campaign}'.")
                    except GitHubActionsError as exc:
                        st.error(f"Failed to open pull request: {exc}")
