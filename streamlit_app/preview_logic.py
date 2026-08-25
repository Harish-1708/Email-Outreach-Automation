"""Preview never sends anything and never writes anything, so it runs
DIRECTLY inside Streamlit — no GitHub Actions round trip needed. It reuses
outreach.build_batch exactly as the CLI does, against a read-only fetch of
current lead data. This also means SMTP credentials (EMAIL_ACCOUNTS_JSON)
never need to exist anywhere near the Streamlit process — only Send needs
them, and Send always happens via GitHub Actions, which is the only place
that credential lives.
"""
import sys
from typing import Dict, List, Optional

from config import REPO_ROOT, SETTINGS_PATH, CAMPAIGNS_DIR, TEMPLATES_ROOT

if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import outreach  # noqa: E402


def get_campaign_cfg(campaign_name: str) -> Dict:
    return outreach.get_campaign(
        campaign_name,
        settings_path=SETTINGS_PATH,
        campaigns_dir=CAMPAIGNS_DIR,
        templates_root=TEMPLATES_ROOT,
    )


def list_campaigns() -> List[str]:
    return outreach.discover_campaign_names(TEMPLATES_ROOT)


def run_preview(campaign_name: str, stage_name: str, batch_size: int,
                 leads: List[Dict], forced_variant: Optional[str] = None) -> List[Dict]:
    """Returns outreach.build_batch's plan list unmodified — same function
    the CLI/GitHub Action's `preview` command calls. No sending, no sheet
    writes; leads must already be fetched (read-only) by the caller."""
    campaign_cfg = get_campaign_cfg(campaign_name)
    variant = None if forced_variant in (None, "Auto") else forced_variant
    return outreach.build_batch(campaign_cfg, leads, stage_name, batch_size, forced_variant=variant)
