import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from campaign_builder import (
    validate_campaign_name, validate_variant_content, build_template_file_content,
    build_campaign_files, get_next_stage_for_campaign, branch_name_for_campaign,
    pr_title_for_campaign, pr_body_for_campaign,
)

TEMPLATES_ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "templates")


def test_validate_campaign_name_rejects_blank():
    assert validate_campaign_name("", []) is not None
    assert validate_campaign_name("   ", []) is not None


def test_validate_campaign_name_rejects_special_characters():
    assert validate_campaign_name("Foo Bar", []) is not None
    assert validate_campaign_name("Foo-Bar", []) is not None
    assert validate_campaign_name("Foo/Bar", []) is not None


def test_validate_campaign_name_accepts_letters_numbers_underscores():
    assert validate_campaign_name("Foo_Bar_123", []) is None


def test_validate_campaign_name_rejects_duplicates():
    assert validate_campaign_name("Existing", ["Existing", "Other"]) is not None


def test_validate_variant_content_requires_subject_and_body():
    assert validate_variant_content("", "body") is not None
    assert validate_variant_content("subject", "") is not None
    assert validate_variant_content("subject", "body") is None


def test_build_template_file_content_matches_outreach_expected_format():
    content = build_template_file_content("Hi {{FirstName}}", "Body text here")
    text = content.decode("utf-8")
    assert text.startswith("Subject: Hi {{FirstName}}\n\n")
    assert "Body text here" in text


def test_build_template_file_content_round_trips_with_outreach_load_template(tmp_path):
    # Prove the file this builds is actually readable by outreach.load_template,
    # not just "looks right" — this is the real compatibility contract.
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
    import outreach

    content = build_template_file_content("Quick idea for {{CompanyName}}", "Hi {{FirstName}},\n\nBody.")
    campaign_dir = tmp_path / "TestCampaign"
    campaign_dir.mkdir()
    (campaign_dir / "intro_A.txt").write_bytes(content)

    tmpl = outreach.load_template(str(campaign_dir), "intro", "A")
    assert tmpl["subject"] == "Quick idea for {{CompanyName}}"
    assert "Hi {{FirstName}}," in tmpl["body"]


def test_build_campaign_files_only_includes_provided_variants():
    files = build_campaign_files("Foo", "intro", {"A": {"subject": "S", "body": "B"}})
    assert len(files) == 1
    assert files[0]["path"] == "templates/Foo/intro_A.txt"


def test_build_campaign_files_multiple_variants_in_order():
    variants = {
        "A": {"subject": "SA", "body": "BA"},
        "B": {"subject": "SB", "body": "BB"},
    }
    files = build_campaign_files("Foo", "intro", variants)
    paths = [f["path"] for f in files]
    assert paths == ["templates/Foo/intro_A.txt", "templates/Foo/intro_B.txt"]


def test_build_campaign_files_uses_given_stage_prefix():
    files = build_campaign_files("Foo", "followup1", {"A": {"subject": "S", "body": "B"}})
    assert files[0]["path"] == "templates/Foo/followup1_A.txt"


# ---------- get_next_stage_for_campaign — against the REAL sample campaign ----------

def test_get_next_stage_for_fully_built_campaign_returns_none():
    # Kelson_Creators_Licensing already has all 5 stages in the repo fixture.
    result = get_next_stage_for_campaign("Kelson_Creators_Licensing", TEMPLATES_ROOT)
    assert result is None


def test_get_next_stage_for_partial_campaign_returns_next_stage_and_matching_variants(tmp_path):
    campaign_dir = tmp_path / "PartialCampaign"
    campaign_dir.mkdir()
    for letter in ["A", "B"]:
        (campaign_dir / f"intro_{letter}.txt").write_text(f"Subject: Hi {letter}\n\nBody {letter}")

    result = get_next_stage_for_campaign("PartialCampaign", str(tmp_path))
    assert result == ("followup1", ["A", "B"])


def test_get_next_stage_requires_exact_variant_match_downstream(tmp_path):
    # Sanity-check the underlying contract this function relies on: adding
    # followup1 with a MISMATCHED variant set is what outreach.py itself
    # would reject — get_next_stage_for_campaign's return value is exactly
    # what avoids ever attempting that combination in the first place.
    import outreach

    campaign_dir = tmp_path / "MismatchCampaign"
    campaign_dir.mkdir()
    (campaign_dir / "intro_A.txt").write_text("Subject: Hi\n\nBody")
    (campaign_dir / "intro_B.txt").write_text("Subject: Hi\n\nBody")
    (campaign_dir / "followup1_A.txt").write_text("Subject: Hi\n\nBody")  # missing B — mismatched

    with pytest.raises(outreach.ConfigError, match="Inconsistent variants"):
        outreach.discover_stages_and_variants(str(campaign_dir), stage_wait_days={})


def test_branch_and_pr_naming_new_campaign():
    assert branch_name_for_campaign("MyCampaign", "intro") == "add-intro-mycampaign"
    assert pr_title_for_campaign("MyCampaign", "intro", is_new_campaign=True) == "Add campaign: MyCampaign"


def test_branch_and_pr_naming_add_stage():
    assert branch_name_for_campaign("MyCampaign", "followup1") == "add-followup1-mycampaign"
    assert pr_title_for_campaign("MyCampaign", "followup1", is_new_campaign=False) == \
        "Add followup1 to campaign: MyCampaign"


def test_pr_body_mentions_creator_and_variant_count_new_campaign():
    body = pr_body_for_campaign("MyCampaign", "intro", 2, "alice", is_new_campaign=True)
    assert "alice" in body
    assert "2 Intro variant" in body


def test_pr_body_mentions_stage_for_add_stage():
    body = pr_body_for_campaign("MyCampaign", "followup1", 2, "alice", is_new_campaign=False)
    assert "followup1" in body
    assert "existing" in body
    assert "alice" in body
