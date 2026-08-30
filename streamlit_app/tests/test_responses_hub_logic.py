import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from responses_hub_logic import (
    tag_responses_with_campaign, response_key, filter_responses, count_unread,
    sort_responses_newest_first, get_campaign_names_present, build_reply_summary_label,
    find_response_by_key, STATUS_FILTER_ALL, INBOX_FILTER_ALL, INBOX_FILTER_UNREAD,
)


def _response(**overrides):
    r = {
        "ResponseID": "r1", "LeadID": "5", "From": "lead@abc.com", "Subject": "Re: Hi",
        "Classification": "Genuine Reply", "ReceivedAt": "2026-08-29 10:00:00", "_campaign": "Foo",
    }
    r.update(overrides)
    return r


# ---------- tag_responses_with_campaign ----------

def test_tag_responses_with_campaign_adds_campaign_key():
    tagged = tag_responses_with_campaign([{"ResponseID": "r1"}], "Foo")
    assert tagged[0]["_campaign"] == "Foo"


def test_tag_responses_with_campaign_never_mutates_input():
    original = [{"ResponseID": "r1"}]
    tag_responses_with_campaign(original, "Foo")
    assert "_campaign" not in original[0]


def test_tag_responses_with_campaign_empty_list():
    assert tag_responses_with_campaign([], "Foo") == []


def test_tag_responses_with_campaign_multiple_responses():
    tagged = tag_responses_with_campaign([{"ResponseID": "r1"}, {"ResponseID": "r2"}], "Foo")
    assert all(r["_campaign"] == "Foo" for r in tagged)


# ---------- response_key ----------

def test_response_key_combines_campaign_and_response_id():
    key = response_key(_response(_campaign="Foo", ResponseID="r1"))
    assert key == "Foo:r1"


def test_response_key_distinguishes_same_response_id_across_campaigns():
    key1 = response_key(_response(_campaign="Foo", ResponseID="r1"))
    key2 = response_key(_response(_campaign="Bar", ResponseID="r1"))
    assert key1 != key2


# ---------- filter_responses ----------

def test_filter_responses_status_all_returns_everything():
    responses = [_response(Classification="Genuine Reply"), _response(Classification="Bounce (Hard)")]
    result = filter_responses(responses, STATUS_FILTER_ALL, STATUS_FILTER_ALL, INBOX_FILTER_ALL, set())
    assert len(result) == 2


def test_filter_responses_by_status():
    responses = [_response(ResponseID="r1", Classification="Genuine Reply"),
                 _response(ResponseID="r2", Classification="Bounce (Hard)")]
    result = filter_responses(responses, "Genuine Reply", STATUS_FILTER_ALL, INBOX_FILTER_ALL, set())
    assert len(result) == 1
    assert result[0]["ResponseID"] == "r1"


def test_filter_responses_by_campaign():
    responses = [_response(ResponseID="r1", _campaign="Foo"), _response(ResponseID="r2", _campaign="Bar")]
    result = filter_responses(responses, STATUS_FILTER_ALL, "Foo", INBOX_FILTER_ALL, set())
    assert len(result) == 1
    assert result[0]["_campaign"] == "Foo"


def test_filter_responses_status_and_campaign_combined():
    responses = [
        _response(ResponseID="r1", _campaign="Foo", Classification="Genuine Reply"),
        _response(ResponseID="r2", _campaign="Foo", Classification="Bounce (Hard)"),
        _response(ResponseID="r3", _campaign="Bar", Classification="Genuine Reply"),
    ]
    result = filter_responses(responses, "Genuine Reply", "Foo", INBOX_FILTER_ALL, set())
    assert len(result) == 1
    assert result[0]["ResponseID"] == "r1"


def test_filter_responses_unread_only():
    responses = [_response(ResponseID="r1", _campaign="Foo"), _response(ResponseID="r2", _campaign="Foo")]
    read_keys = {"Foo:r1"}
    result = filter_responses(responses, STATUS_FILTER_ALL, STATUS_FILTER_ALL, INBOX_FILTER_UNREAD, read_keys)
    assert len(result) == 1
    assert result[0]["ResponseID"] == "r2"


def test_filter_responses_all_three_filters_combined():
    responses = [
        _response(ResponseID="r1", _campaign="Foo", Classification="Genuine Reply"),
        _response(ResponseID="r2", _campaign="Foo", Classification="Genuine Reply"),
        _response(ResponseID="r3", _campaign="Bar", Classification="Genuine Reply"),
    ]
    read_keys = {"Foo:r1"}
    result = filter_responses(responses, "Genuine Reply", "Foo", INBOX_FILTER_UNREAD, read_keys)
    assert len(result) == 1
    assert result[0]["ResponseID"] == "r2"


def test_filter_responses_empty_list():
    assert filter_responses([], STATUS_FILTER_ALL, STATUS_FILTER_ALL, INBOX_FILTER_ALL, set()) == []


# ---------- count_unread ----------

def test_count_unread_all_unread():
    responses = [_response(ResponseID="r1"), _response(ResponseID="r2")]
    assert count_unread(responses, set()) == 2


def test_count_unread_some_read():
    responses = [_response(ResponseID="r1", _campaign="Foo"), _response(ResponseID="r2", _campaign="Foo")]
    assert count_unread(responses, {"Foo:r1"}) == 1


def test_count_unread_all_read():
    responses = [_response(ResponseID="r1", _campaign="Foo")]
    assert count_unread(responses, {"Foo:r1"}) == 0


def test_count_unread_empty_list():
    assert count_unread([], set()) == 0


# ---------- sort_responses_newest_first ----------

def test_sort_responses_newest_first():
    responses = [
        _response(ResponseID="old", ReceivedAt="2026-08-01 09:00:00"),
        _response(ResponseID="new", ReceivedAt="2026-08-29 09:00:00"),
    ]
    sorted_responses = sort_responses_newest_first(responses)
    assert sorted_responses[0]["ResponseID"] == "new"
    assert sorted_responses[1]["ResponseID"] == "old"


def test_sort_responses_newest_first_empty_list():
    assert sort_responses_newest_first([]) == []


# ---------- get_campaign_names_present ----------

def test_get_campaign_names_present_deduplicates_and_sorts():
    responses = [_response(_campaign="Zeta"), _response(_campaign="Alpha"), _response(_campaign="Zeta")]
    assert get_campaign_names_present(responses) == ["Alpha", "Zeta"]


def test_get_campaign_names_present_empty_list():
    assert get_campaign_names_present([]) == []


def test_get_campaign_names_present_skips_blank_campaign():
    responses = [_response(_campaign=""), _response(_campaign="Foo")]
    assert get_campaign_names_present(responses) == ["Foo"]


# ---------- build_reply_summary_label ----------

def test_build_reply_summary_label_includes_sender_subject_campaign():
    label = build_reply_summary_label(_response(From="lead@abc.com", Subject="Re: Hi", _campaign="Foo"))
    assert "lead@abc.com" in label
    assert "Re: Hi" in label
    assert "Foo" in label


def test_build_reply_summary_label_handles_missing_fields():
    label = build_reply_summary_label({})
    assert "unknown sender" in label
    assert "no subject" in label


# ---------- find_response_by_key ----------

def test_find_response_by_key_found():
    responses = [_response(ResponseID="r1", _campaign="Foo"), _response(ResponseID="r2", _campaign="Foo")]
    found = find_response_by_key(responses, "Foo:r2")
    assert found["ResponseID"] == "r2"


def test_find_response_by_key_not_found_returns_none():
    responses = [_response(ResponseID="r1", _campaign="Foo")]
    assert find_response_by_key(responses, "Foo:nonexistent") is None


def test_find_response_by_key_empty_list():
    assert find_response_by_key([], "Foo:r1") is None
