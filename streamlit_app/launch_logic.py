"""Pure logic for Launch/Pause/Resume (Phase G). Just flips the 'status'
key in the campaign's config override file — reuses settings_logic.py's
generic override read/write helpers (load_raw_override, override_to_yaml_
bytes, override_file_path), since status lives in the exact same file as
sending/schedule settings, not a separate store.

Launch/Pause/Resume are deliberately NOT gated by campaign readiness
(campaign_status_logic.compute_campaign_readiness) — that check is purely
informational (shown to the user as a heads-up), because the actual
system doesn't need it to be enforced: outreach.send_batch() already
naturally no-ops if there are no eligible leads, no sender, etc. Blocking
Launch on readiness would just be friction with no real safety benefit.
"""
from typing import Dict

STATUS_ACTIVE = "active"
STATUS_PAUSED = "paused"


def build_status_override(raw_override: Dict, new_status: str) -> Dict:
    """Returns a NEW dict — never mutates raw_override. Only 'status' is
    touched; sending, schedule, and anything else pass through untouched
    — same guarantee settings_logic.build_updated_override and
    schedule_logic.build_updated_schedule_override each make for their
    own key."""
    updated = dict(raw_override)
    updated["status"] = new_status
    return updated
