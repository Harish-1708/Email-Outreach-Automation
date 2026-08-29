import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from accounts_logic import aggregate_sent_today_by_account, build_account_rows
from datetime import datetime

TODAY = datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def test_aggregate_sums_across_campaigns_for_same_account():
    send_logs = {
        "CampaignA": [{"Status": "sent", "Timestamp": TODAY, "SenderAccount": "sales1"}],
        "CampaignB": [{"Status": "sent", "Timestamp": TODAY, "SenderAccount": "sales1"}],
    }
    totals = aggregate_sent_today_by_account(send_logs)
    assert totals == {"sales1": 2}


def test_aggregate_keeps_different_accounts_separate():
    send_logs = {
        "CampaignA": [{"Status": "sent", "Timestamp": TODAY, "SenderAccount": "sales1"}],
        "CampaignB": [{"Status": "sent", "Timestamp": TODAY, "SenderAccount": "sales2"}],
    }
    totals = aggregate_sent_today_by_account(send_logs)
    assert totals == {"sales1": 1, "sales2": 1}


def test_aggregate_ignores_non_sent_status():
    send_logs = {"CampaignA": [{"Status": "error", "Timestamp": TODAY, "SenderAccount": "sales1"}]}
    assert aggregate_sent_today_by_account(send_logs) == {}


def test_aggregate_empty_input():
    assert aggregate_sent_today_by_account({}) == {}


def test_build_account_rows_sorted_by_name():
    directory = {"sales2": "sales2@x.com", "sales1": "sales1@x.com"}
    rows = build_account_rows(directory, {}, default_account="sales1")
    assert [r["name"] for r in rows] == ["sales1", "sales2"]


def test_build_account_rows_includes_send_count_and_default_flag():
    directory = {"sales1": "sales1@x.com", "sales2": "sales2@x.com"}
    rows = build_account_rows(directory, {"sales1": 5}, default_account="sales2")

    by_name = {r["name"]: r for r in rows}
    assert by_name["sales1"]["sent_today"] == 5
    assert by_name["sales1"]["is_default"] is False
    assert by_name["sales2"]["sent_today"] == 0
    assert by_name["sales2"]["is_default"] is True


def test_build_account_rows_empty_directory():
    assert build_account_rows({}, {}, default_account="sales1") == []
