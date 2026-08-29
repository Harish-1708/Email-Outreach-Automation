"""Pure logic for the Email Accounts page. Never touches SMTP credentials —
those live only in the GitHub Secret EMAIL_ACCOUNTS_JSON, used exclusively
by GitHub Actions. This page shows account NAMES and ADDRESSES (from a
lightweight companion Streamlit secret containing no passwords) plus
real usage counts pulled from each campaign's Send Log.
"""
import os
import sys
from typing import Dict, List

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import outreach  # noqa: E402


def aggregate_sent_today_by_account(send_logs_by_campaign: Dict[str, List[Dict]]) -> Dict[str, int]:
    """send_logs_by_campaign: {campaign_name: send_log_rows}. Sums today's
    'sent' counts per account across every campaign — an account's daily
    cap is shared across all campaigns that use it, so a per-campaign-only
    count would understate real usage."""
    totals: Dict[str, int] = {}
    for send_log in send_logs_by_campaign.values():
        campaign_counts = outreach._count_sent_today_by_account(send_log)  # noqa: SLF001 - single source of truth
        for acct, count in campaign_counts.items():
            totals[acct] = totals.get(acct, 0) + count
    return totals


def build_account_rows(account_directory: Dict[str, str], sent_today_by_account: Dict[str, int],
                        default_account: str) -> List[Dict]:
    """account_directory: {account_name: address} (no passwords — see
    module docstring). Returns rows sorted by account name, each with
    today's send count and whether it's the global default."""
    rows = []
    for name in sorted(account_directory.keys()):
        rows.append({
            "name": name,
            "address": account_directory[name],
            "sent_today": sent_today_by_account.get(name, 0),
            "is_default": name == default_account,
        })
    return rows
