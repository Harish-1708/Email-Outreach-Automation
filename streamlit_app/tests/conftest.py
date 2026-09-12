"""Shared pytest fixtures for the Streamlit test suite.

Why this exists
---------------
Several tests used to call into code that resolves `templates/` and
`config/` RELATIVE TO THE CURRENT WORKING DIRECTORY, with no argument to
point them elsewhere. Run from the repo root, those tests therefore read
the REAL production campaigns — and asserted things like "this campaign
has all 5 stages built" and "these are the configured sender accounts".

That coupling meant a legitimate production change (dropping a campaign
from 5 stages to 3, renaming an account slot) turned the suite red even
though nothing was broken, which is exactly what happened. A test suite
that fails when production data legitimately changes teaches people to
ignore it.

`fixture_repo` below chdir's into a small, committed, unchanging repo
tree under `fixtures/repo/` that the test suite owns. Tests that need a
campaign to exist use FIXTURE_CAMPAIGN instead of naming a real one.
Nothing under `fixtures/repo/` should ever be edited to track
production; if a test needs a different shape, add another fixture
campaign rather than mutating this one.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import config  # noqa: E402

FIXTURE_REPO = os.path.join(os.path.dirname(__file__), "fixtures", "repo")

# The stable sample campaign: 5 stages (intro + followup1-4) x variants A-D.
FIXTURE_CAMPAIGN = "Sample_Campaign"

# Templates root inside the fixture repo, for helpers that take an explicit
# templates_root argument rather than reading config.* at call time.
FIXTURE_TEMPLATES = os.path.join(FIXTURE_REPO, "templates")


@pytest.fixture
def fixture_repo_without_accounts(monkeypatch, fixture_repo):
    """Same as fixture_repo, but with the account-slot mapping pointed at
    a path that does not exist — for tests asserting the app's behaviour
    when NO email accounts are configured at all. Depends on
    fixture_repo, so the campaign/config isolation still applies."""
    monkeypatch.setattr(config, "EMAIL_ACCOUNT_SLOT_MAPPING_ABS_PATH",
                         os.path.join(FIXTURE_REPO, "config", "_no_such_slots_file.yaml"))
    return fixture_repo


@pytest.fixture
def fixture_repo(monkeypatch):
    """Points the app's path constants at the fixture repo, so code that
    resolves campaigns and config through `config.*` sees stable,
    test-owned data instead of live production campaigns.

    This patches the module attributes rather than chdir'ing, because
    config.py builds ABSOLUTE paths from its own file location — the
    working directory is irrelevant to it. The app modules deliberately
    read `config.X` at CALL time (see preview_logic's own note), which
    is exactly what makes patching here take effect everywhere.

    monkeypatch undoes all of it at teardown, even if the test fails."""
    monkeypatch.setattr(config, "SETTINGS_PATH",
                         os.path.join(FIXTURE_REPO, "config", "settings.yaml"))
    monkeypatch.setattr(config, "CAMPAIGNS_DIR",
                         os.path.join(FIXTURE_REPO, "config", "campaigns"))
    monkeypatch.setattr(config, "TEMPLATES_ROOT",
                         os.path.join(FIXTURE_REPO, "templates"))
    monkeypatch.setattr(config, "EMAIL_ACCOUNT_SLOT_MAPPING_ABS_PATH",
                         os.path.join(FIXTURE_REPO, "config", "email_account_slots.yaml"))
    return FIXTURE_REPO
