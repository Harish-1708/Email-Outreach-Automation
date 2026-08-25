"""Pure functions for building the Send Batch dispatch payload and
enforcing the typed-confirmation gate — kept separate from the Streamlit
page so both are unit-testable without a running app.
"""
from typing import Dict, Optional

REQUIRED_CONFIRM_TEXT = "SEND"


def confirmation_is_valid(typed_text: str) -> bool:
    """Exact match, same as the GitHub Actions workflow's own guard job —
    this is a DELIBERATE friction point against misclicks. It is enforced
    here, in Streamlit, BEFORE the dispatch call is ever made — not just
    passed through as a pre-filled field."""
    return typed_text == REQUIRED_CONFIRM_TEXT


def build_send_inputs(campaign: str, stage: str, batch_size: int, variant: str = "Auto",
                       daily_limit: Optional[int] = None,
                       per_account_daily_limit: Optional[int] = None,
                       sender_rotation: Optional[bool] = None) -> Dict[str, str]:
    """Builds the exact input dict send_batch.yml's workflow_dispatch
    expects. All values are strings — GitHub Actions inputs are always
    strings regardless of the underlying type."""
    inputs = {
        "campaign": campaign,
        "stage": stage,
        "batch_size": str(batch_size),
        "variant": variant or "Auto",
        "confirm": REQUIRED_CONFIRM_TEXT,
    }
    if daily_limit is not None:
        inputs["daily_limit"] = str(daily_limit)
    if per_account_daily_limit is not None:
        inputs["per_account_daily_limit"] = str(per_account_daily_limit)
    if sender_rotation is not None:
        inputs["sender_rotation"] = "true" if sender_rotation else "false"
    return inputs


def build_preview_inputs(campaign: str, stage: str, batch_size: int, variant: str = "Auto") -> Dict[str, str]:
    """For triggering preview_batch.yml itself (optional — Streamlit's own
    in-app Preview via preview_logic.py is faster and doesn't need this,
    but some setups may still want the GitHub-run version as an audit
    trail)."""
    return {
        "campaign": campaign,
        "stage": stage,
        "batch_size": str(batch_size),
        "variant": variant or "Auto",
    }


def build_check_replies_inputs(campaign: str) -> Dict[str, str]:
    return {"campaign": campaign}
