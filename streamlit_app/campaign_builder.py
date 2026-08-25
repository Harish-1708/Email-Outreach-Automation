"""Phase 3 — new-campaign creation logic. Kept pure/testable: nothing here
touches the network. The Streamlit page collects form input, calls these
functions, and hands the result to GitHubClient.open_campaign_pull_request.

Deliberately opens a PULL REQUEST rather than committing straight to main
(see github_client.open_campaign_pull_request) — this is the one part of
the whole system that can introduce new files into the automation repo, so
a human still reviews it before the campaign goes live. Also deliberately
scoped to just the Intro stage on creation: auto-discovery already treats a
single-stage campaign as fully valid (see README Section 5), and follow-ups
can be added the same way (or by hand) any time afterward.
"""
import re
from typing import Dict, List, Optional

CAMPAIGN_NAME_RE = re.compile(r"^[A-Za-z0-9_]+$")
VARIANT_LETTERS = ["A", "B", "C", "D"]


def validate_campaign_name(name: str, existing_campaigns: List[str]) -> Optional[str]:
    """Returns an error message, or None if the name is valid."""
    if not name or not name.strip():
        return "Campaign name is required."
    if not CAMPAIGN_NAME_RE.match(name):
        return "Use only letters, numbers, and underscores — this becomes a folder name."
    if name in existing_campaigns:
        return f"A campaign named '{name}' already exists."
    return None


def validate_variant_content(subject: str, body: str) -> Optional[str]:
    if not subject or not subject.strip():
        return "Subject is required."
    if not body or not body.strip():
        return "Body is required."
    return None


def build_template_file_content(subject: str, body: str) -> bytes:
    """Matches outreach.load_template's expected format exactly: a
    'Subject: ...' first line, a blank line, then the body."""
    return f"Subject: {subject.strip()}\n\n{body.strip()}\n".encode("utf-8")


def build_campaign_files(campaign_name: str, variants: Dict[str, Dict[str, str]]) -> List[Dict]:
    """variants: {'A': {'subject': ..., 'body': ...}, 'B': {...}, ...}
    (only Intro is created here — see module docstring). Returns the
    [{'path':..., 'content': bytes}] list GitHubClient.
    open_campaign_pull_request expects."""
    files = []
    for letter in VARIANT_LETTERS:
        if letter not in variants:
            continue
        content = build_template_file_content(variants[letter]["subject"], variants[letter]["body"])
        files.append({"path": f"templates/{campaign_name}/intro_{letter}.txt", "content": content})
    return files


def branch_name_for_campaign(campaign_name: str) -> str:
    return f"add-campaign-{campaign_name.lower()}"


def pr_title_for_campaign(campaign_name: str) -> str:
    return f"Add campaign: {campaign_name}"


def pr_body_for_campaign(campaign_name: str, variant_count: int, created_by: str) -> str:
    return (
        f"Adds the `templates/{campaign_name}/` folder with {variant_count} Intro "
        f"variant(s), created from the Streamlit control panel by **{created_by}**.\n\n"
        f"Auto-discovery will pick this campaign up as soon as this PR is merged — "
        f"no other changes needed. Follow-up stages can be added later the same way "
        f"(see README Section 5).\n\n"
        f"Review the template content below before merging."
    )
