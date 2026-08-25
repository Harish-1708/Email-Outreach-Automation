import os
import sys

import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from auth import login_gate, current_user  # noqa: E402
from github_client import GitHubClient, GitHubActionsError  # noqa: E402
from preview_logic import list_campaigns  # noqa: E402
from campaign_builder import (  # noqa: E402
    validate_campaign_name, validate_variant_content, build_campaign_files,
    branch_name_for_campaign, pr_title_for_campaign, pr_body_for_campaign, VARIANT_LETTERS,
)

st.set_page_config(page_title="New Campaign", page_icon="➕", layout="wide")

if not login_gate():
    st.stop()

st.title("➕ New Campaign")
st.markdown(
    """
This creates a campaign's **Intro** templates and opens a **pull request** —
it never commits directly to `main`. Auto-discovery treats a single-stage
campaign as fully valid, so this is enough to launch with; add follow-up
stages later the same way (or by hand), any time.

Someone still needs to **merge the PR** before the campaign becomes
available to Preview/Send — this is intentional: it's the one action in
this whole app that can introduce new files into the automation repo.
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

campaign_name = st.text_input("Campaign name (letters, numbers, underscores only)")

num_variants = st.slider("Number of Intro variants (A/B/C/D)", min_value=1, max_value=4, value=1)

variant_inputs = {}
for letter in VARIANT_LETTERS[:num_variants]:
    st.subheader(f"Variant {letter}")
    subject = st.text_input(f"Subject ({letter})", key=f"subject_{letter}")
    body = st.text_area(f"Body ({letter})", key=f"body_{letter}", height=150)
    variant_inputs[letter] = {"subject": subject, "body": body}

if st.button("Open Pull Request", type="primary"):
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
            files = build_campaign_files(campaign_name, variant_inputs)
            client = _get_github_client()
            branch = branch_name_for_campaign(campaign_name)
            pr = client.open_campaign_pull_request(
                branch_name=branch,
                files=files,
                pr_title=pr_title_for_campaign(campaign_name),
                pr_body=pr_body_for_campaign(campaign_name, len(files), current_user()),
            )
            st.success(f"Pull request opened: {pr.get('html_url', '')}")
            st.info("Merge it to make this campaign available to Preview/Send.")
        except GitHubActionsError as exc:
            st.error(f"Failed to open pull request: {exc}")
