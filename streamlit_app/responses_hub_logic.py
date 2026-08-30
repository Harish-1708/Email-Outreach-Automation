"""Pure logic for the standalone Responses page — every reply across
every campaign in one place, filterable by classification, campaign,
and inbox/unread. Complements (doesn't replace) the per-campaign
Responses tab inside Campaigns, which stays for in-context work on one
campaign at a time.

Reuses outreach's own classification constants as the Status filter's
options — see the module docstring note in campaigns.py about why this
doesn't (yet) offer Instantly-style Interested/Not Interested sentiment
classification: that's a real, separate feature decision, not something
to fake with a keyword guess here.
"""
from typing import Dict, List, Optional, Set

STATUS_FILTER_ALL = "All"
INBOX_FILTER_ALL = "All"
INBOX_FILTER_UNREAD = "Unread only"

CLASSIFICATION_OPTIONS = [
    STATUS_FILTER_ALL,
    "Genuine Reply",
    "Auto-Reply",
    "Out of Office",
    "Bounce (Hard)",
    "Bounce (Soft)",
]


def tag_responses_with_campaign(responses: List[Dict], campaign_name: str) -> List[Dict]:
    """Returns NEW dicts (never mutates the input) with a "_campaign" key
    added — used when merging responses fetched separately per campaign
    into one combined list, so the UI can filter/display by campaign
    without a second lookup."""
    tagged = []
    for r in responses:
        new_r = dict(r)
        new_r["_campaign"] = campaign_name
        tagged.append(new_r)
    return tagged


def response_key(response: Dict) -> str:
    """A stable identifier for one response, for read/unread tracking —
    ResponseID is the Sheet's own unique key per response row, already
    unique within a campaign; prefixing with the campaign name makes it
    unique across campaigns too, since ResponseID alone could collide
    between two different campaigns' sheets."""
    return f"{response.get('_campaign', '')}:{response.get('ResponseID', '')}"


def filter_responses(responses: List[Dict], status_filter: str, campaign_filter: str,
                      inbox_filter: str, read_keys: Set[str]) -> List[Dict]:
    """status_filter: one of CLASSIFICATION_OPTIONS ("All" = no filter).
    campaign_filter: a campaign name, or "All". inbox_filter:
    INBOX_FILTER_ALL or INBOX_FILTER_UNREAD. read_keys: response_key()
    values already marked read — anything else counts as unread."""
    result = responses
    if status_filter != STATUS_FILTER_ALL:
        result = [r for r in result if r.get("Classification") == status_filter]
    if campaign_filter != STATUS_FILTER_ALL:
        result = [r for r in result if r.get("_campaign") == campaign_filter]
    if inbox_filter == INBOX_FILTER_UNREAD:
        result = [r for r in result if response_key(r) not in read_keys]
    return result


def count_unread(responses: List[Dict], read_keys: Set[str]) -> int:
    return sum(1 for r in responses if response_key(r) not in read_keys)


def sort_responses_newest_first(responses: List[Dict]) -> List[Dict]:
    return sorted(responses, key=lambda r: r.get("ReceivedAt", ""), reverse=True)


def get_campaign_names_present(responses: List[Dict]) -> List[str]:
    """Sorted, de-duplicated list of every campaign that actually has at
    least one response — used to populate the Campaign filter dropdown
    without listing campaigns that have zero replies yet."""
    return sorted({r.get("_campaign", "") for r in responses if r.get("_campaign")})


def search_responses(responses: List[Dict], query: str) -> List[Dict]:
    """Substring, case-insensitive match against sender, subject, snippet,
    and campaign — the fields someone would actually type a name, a
    company, or a keyword hoping to find. A blank query returns
    everything unfiltered, same as not searching at all."""
    query = (query or "").strip().lower()
    if not query:
        return responses
    result = []
    for r in responses:
        haystack = " ".join([
            r.get("From", ""), r.get("Subject", ""), r.get("Snippet", ""), r.get("_campaign", ""),
        ]).lower()
        if query in haystack:
            result.append(r)
    return result


def build_reply_summary_label(response: Dict) -> str:
    """One-line label for a response in the list — sender, subject, and
    which campaign it belongs to, since this page spans every campaign
    at once (unlike the per-campaign Responses tab, where the campaign
    is already implied by context)."""
    sender = response.get("From", "(unknown sender)")
    subject = response.get("Subject", "(no subject)")
    campaign = response.get("_campaign", "")
    return f"{sender} — {subject} · {campaign}"


def find_response_by_key(responses: List[Dict], key: str) -> Optional[Dict]:
    for r in responses:
        if response_key(r) == key:
            return r
    return None
