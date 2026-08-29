"""Pure logic for the Sequences tab (Phase D). Nothing here touches the
network — reading existing template content is local file access (the
Streamlit process already has the repo checked out), and writing happens
via the same commit-then-the-file-is-live pattern as everywhere else.

The one thing this module has to get right that a naive "edit any template"
UI would get wrong: variants are CAMPAIGN-WIDE, not per-stage. Every stage
must offer the exact same variant letters (outreach.discover_stages_and_
variants enforces this — see its docstring). So "add a variant" can never
mean "add it to just this one stage" — that would immediately make the
campaign invalid. It has to mean "add this variant to every stage that
currently exists," committed together in one save.
"""
import os
import sys
from typing import Dict, List, Optional, Tuple

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import outreach  # noqa: E402

VARIANT_LETTERS = ["A", "B", "C", "D"]


def get_existing_stages_and_variants(campaign_name: str, templates_root: str) -> Tuple[List[Dict], List[str]]:
    """Thin wrapper for a clear, Sequences-tab-specific name — same
    function New Campaign's get_next_stage_for_campaign already relies on,
    so this can never drift from what outreach.py itself enforces."""
    campaign_dir = os.path.join(templates_root, campaign_name)
    return outreach.discover_stages_and_variants(campaign_dir, stage_wait_days={})


def load_variant_content(campaign_name: str, stage_prefix: str, variant: str, templates_root: str) -> Dict[str, str]:
    campaign_dir = os.path.join(templates_root, campaign_name)
    return outreach.load_template(campaign_dir, stage_prefix, variant)


def next_available_variant_letter(existing_variants: List[str]) -> Optional[str]:
    for letter in VARIANT_LETTERS:
        if letter not in existing_variants:
            return letter
    return None  # already at the 4-variant maximum


def build_variant_edit_file(campaign_name: str, stage_prefix: str, variant: str,
                             subject: str, body: str) -> Dict:
    """One file, for editing an EXISTING variant (or adding a new one to a
    single stage as part of a campaign-wide add — see
    build_new_variant_files_for_all_stages). Same file format as
    campaign_builder.build_template_file_content, kept here as its own
    small function so this module doesn't need to import campaign_builder
    just for one line."""
    content = f"Subject: {subject.strip()}\n\n{body.strip()}\n".encode("utf-8")
    return {"path": f"templates/{campaign_name}/{stage_prefix}_{variant}.txt", "content": content}


def build_new_variant_files_for_all_stages(campaign_name: str, stages: List[Dict], new_variant: str,
                                            contents_by_stage: Dict[str, Dict[str, str]]) -> List[Dict]:
    """contents_by_stage: {stage_prefix: {"subject": ..., "body": ...}} —
    MUST have an entry for every stage in `stages`, enforced by
    validate_new_variant_contents before this is ever called. Returns one
    file per stage, all for the same new variant letter, meant to be
    committed together in a single save so the campaign is never
    momentarily in an inconsistent (some-stages-have-it) state on disk."""
    files = []
    for stage in stages:
        prefix = stage["template_prefix"]
        content = contents_by_stage[prefix]
        files.append(build_variant_edit_file(campaign_name, prefix, new_variant,
                                              content["subject"], content["body"]))
    return files


def validate_new_variant_contents(stages: List[Dict], contents_by_stage: Dict[str, Dict[str, str]]) -> List[str]:
    """Returns a list of error messages (empty = valid). Every stage needs
    a body; only the FIRST stage requires a non-blank subject (a later
    stage may deliberately leave Subject blank to continue the thread —
    see outreach.render_email's docstring)."""
    errors = []
    for idx, stage in enumerate(stages):
        prefix = stage["template_prefix"]
        content = contents_by_stage.get(prefix, {})
        subject = (content.get("subject") or "").strip()
        body = (content.get("body") or "").strip()
        if idx == 0 and not subject:
            errors.append(f"{stage['name']}: Subject is required (this is the first stage).")
        if not body:
            errors.append(f"{stage['name']}: Body is required.")
    return errors


def has_content_changed(original: Dict[str, str], edited_subject: str, edited_body: str) -> bool:
    """Whether an edited variant actually differs from what's on disk —
    used to decide what belongs in the batched Save commit, so an
    untouched variant a user merely glanced at never gets re-committed."""
    return original.get("subject", "") != edited_subject.strip() or original.get("body", "") != edited_body.strip()
