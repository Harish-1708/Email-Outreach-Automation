import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from campaign_builder import (
    validate_campaign_name, validate_variant_content, build_template_file_content,
    build_campaign_files, branch_name_for_campaign, pr_title_for_campaign, pr_body_for_campaign,
)


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
    files = build_campaign_files("Foo", {"A": {"subject": "S", "body": "B"}})
    assert len(files) == 1
    assert files[0]["path"] == "templates/Foo/intro_A.txt"


def test_build_campaign_files_multiple_variants_in_order():
    variants = {
        "A": {"subject": "SA", "body": "BA"},
        "B": {"subject": "SB", "body": "BB"},
    }
    files = build_campaign_files("Foo", variants)
    paths = [f["path"] for f in files]
    assert paths == ["templates/Foo/intro_A.txt", "templates/Foo/intro_B.txt"]


def test_branch_and_pr_naming():
    assert branch_name_for_campaign("MyCampaign") == "add-campaign-mycampaign"
    assert pr_title_for_campaign("MyCampaign") == "Add campaign: MyCampaign"


def test_pr_body_mentions_creator_and_variant_count():
    body = pr_body_for_campaign("MyCampaign", 2, "alice")
    assert "alice" in body
    assert "2 Intro variant" in body
