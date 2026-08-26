"""Phase 3 — campaign creation/extension logic. Kept pure/testable: nothing
here touches the network. The Streamlit page collects form input, calls
these functions, and hands the result to GitHubClient.
open_campaign_pull_request.

Deliberately opens a PULL REQUEST rather than committing straight to main
(see github_client.open_campaign_pull_request) — this is the one part of
the whole system that can introduce new files into the automation repo, so
a human still reviews it before it goes live.

Two modes, both funneling through the same file-building logic:
- A brand NEW campaign — starts at "intro", any 1-4 variant letters.
- The NEXT stage on an EXISTING campaign — the stage after whatever it
  already has, and (unlike a new campaign) the variant letters are NOT a
  free choice: they must exactly match the campaign's existing variants,
  because that's what outreach.discover_stages_and_variants requires (see
  its docstring in outreach.py). get_next_stage_for_campaign reuses that
  exact function rather than reimplementing the rule, so this can never
  produce a combination the core system would reject.
"""
import os
import re
import sys
from typing import Dict, List, Optional, Tuple

CAMPAIGN_NAME_RE = re.compile(r"^[A-Za-z0-9_]+$")
VARIANT_LETTERS = ["A", "B", "C", "D"]

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import outreach  # noqa: E402


def validate_campaign_name(name: str, existing_campaigns: List[str]) -> Optional[str]:
    """Returns an error message, or None if the name is valid."""
    if not name or not name.strip():
        return "Campaign name is required."
    if not CAMPAIGN_NAME_RE.match(name):
        return "Use only letters, numbers, and underscores — this becomes a folder name."
    if name in existing_campaigns:
        return f"A campaign named '{name}' already exists."
    return None


def validate_variant_content(subject: str, body: str, is_first_stage: bool = True) -> Optional[str]:
    """Subject is required for the FIRST stage only — outreach.py's own
    render_email raises if a first-stage template has a blank Subject
    (there's no previous thread to continue from a first message). For any
    later stage, a blank Subject is a legitimate, deliberate choice: it
    means "continue the existing thread" (Re: <previous subject>) instead
    of starting a new one — see render_email's docstring in outreach.py."""
    if is_first_stage and (not subject or not subject.strip()):
        return "Subject is required for the first stage (there's no previous thread to continue from)."
    if not body or not body.strip():
        return "Body is required."
    return None


def build_template_file_content(subject: str, body: str) -> bytes:
    """Matches outreach.load_template's expected format exactly: a
    'Subject: ...' first line, a blank line, then the body."""
    return f"Subject: {subject.strip()}\n\n{body.strip()}\n".encode("utf-8")


def build_campaign_files(campaign_name: str, stage_prefix: str,
                          variants: Dict[str, Dict[str, str]]) -> List[Dict]:
    """variants: {'A': {'subject': ..., 'body': ...}, 'B': {...}, ...}.
    Returns the [{'path':..., 'content': bytes}] list GitHubClient.
    open_campaign_pull_request expects, for ONE stage of a campaign
    (stage_prefix is e.g. 'intro' or 'followup1')."""
    files = []
    for letter in VARIANT_LETTERS:
        if letter not in variants:
            continue
        content = build_template_file_content(variants[letter]["subject"], variants[letter]["body"])
        files.append({"path": f"templates/{campaign_name}/{stage_prefix}_{letter}.txt", "content": content})
    return files


def get_next_stage_for_campaign(campaign_name: str, templates_root: str) -> Optional[Tuple[str, List[str]]]:
    """For an EXISTING campaign, returns (next_stage_prefix,
    required_variant_letters) — the only stage/variant combination
    outreach.py's own auto-discovery would accept next — or None if the
    campaign already has all 5 stages built out.

    Reuses outreach.discover_stages_and_variants directly rather than
    re-deriving the rule, so this can never drift from what the core
    system actually enforces.
    """
    campaign_dir = os.path.join(templates_root, campaign_name)
    stages, variants = outreach.discover_stages_and_variants(campaign_dir, stage_wait_days={})
    existing_prefixes = [s["template_prefix"] for s in stages]
    for prefix in outreach.CANONICAL_STAGE_ORDER:
        if prefix not in existing_prefixes:
            return prefix, variants
    return None  # all 5 stages already exist


def branch_name_for_campaign(campaign_name: str, stage_prefix: str) -> str:
    return f"add-{stage_prefix}-{campaign_name.lower()}"


def pr_title_for_campaign(campaign_name: str, stage_prefix: str, is_new_campaign: bool) -> str:
    if is_new_campaign:
        return f"Add campaign: {campaign_name}"
    return f"Add {stage_prefix} to campaign: {campaign_name}"


def pr_body_for_campaign(campaign_name: str, stage_prefix: str, variant_count: int,
                          created_by: str, is_new_campaign: bool) -> str:
    if is_new_campaign:
        intro = (
            f"Adds the `templates/{campaign_name}/` folder with {variant_count} "
            f"Intro variant(s), created from the Streamlit control panel by **{created_by}**.\n\n"
            f"Auto-discovery will pick this campaign up as soon as this PR is merged — "
            f"no other changes needed."
        )
    else:
        intro = (
            f"Adds `{stage_prefix}` ({variant_count} variant(s)) to the existing "
            f"`{campaign_name}` campaign, created from the Streamlit control panel by "
            f"**{created_by}**.\n\nThis stage becomes active for eligible leads as soon "
            f"as this PR is merged."
        )
    return intro + "\n\nReview the template content below before merging."
