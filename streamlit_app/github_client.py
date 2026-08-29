"""Thin GitHub REST API client. Every network call is isolated to this
module and goes through `requests`, so tests can mock `requests.*` directly
without touching real GitHub.

Token scope needed:
- actions: read, actions: write  -> dispatch_workflow, get_run, find_recent_run
- contents: write -> create_file / commit_campaign_files_directly (New
  Campaign page only)
"""
import base64
from typing import Dict, List, Optional

import requests

GITHUB_API = "https://api.github.com"
DEFAULT_TIMEOUT = 20


class GitHubActionsError(Exception):
    pass


class GitHubClient:
    def __init__(self, token: str, owner: str, repo: str, timeout: int = DEFAULT_TIMEOUT):
        self.owner = owner
        self.repo = repo
        self.timeout = timeout
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    # ---------- Triggering + polling workflow runs ----------

    def dispatch_workflow(self, workflow_file: str, inputs: Dict[str, str],
                           ref: str = "main") -> Optional[Dict]:
        """Triggers workflow_dispatch. Returns {'id':..., 'html_url':...}
        directly when the API's return_run_details feature is available;
        returns None otherwise (caller should fall back to
        find_recent_run)."""
        url = f"{GITHUB_API}/repos/{self.owner}/{self.repo}/actions/workflows/{workflow_file}/dispatches"
        payload = {"ref": ref, "inputs": inputs, "return_run_details": True}
        resp = requests.post(url, json=payload, headers=self._headers, timeout=self.timeout)
        if resp.status_code not in (200, 204):
            raise GitHubActionsError(
                f"Failed to dispatch '{workflow_file}': {resp.status_code} {resp.text[:300]}"
            )
        if resp.status_code == 200 and resp.content:
            return resp.json()
        return None

    def get_run(self, run_id: int) -> Dict:
        url = f"{GITHUB_API}/repos/{self.owner}/{self.repo}/actions/runs/{run_id}"
        resp = requests.get(url, headers=self._headers, timeout=self.timeout)
        if resp.status_code != 200:
            raise GitHubActionsError(f"Failed to fetch run {run_id}: {resp.status_code} {resp.text[:300]}")
        return resp.json()

    def find_recent_run(self, workflow_file: str, branch: str = "main") -> Optional[Dict]:
        """Fallback correlation if dispatch_workflow returned None — most
        recent workflow_dispatch run for this workflow/branch. Best-effort;
        can theoretically race with a second concurrent trigger."""
        url = f"{GITHUB_API}/repos/{self.owner}/{self.repo}/actions/workflows/{workflow_file}/runs"
        resp = requests.get(
            url, headers=self._headers,
            params={"event": "workflow_dispatch", "branch": branch, "per_page": 1},
            timeout=self.timeout,
        )
        if resp.status_code != 200:
            raise GitHubActionsError(f"Failed to list runs for '{workflow_file}': {resp.status_code} {resp.text[:300]}")
        runs = resp.json().get("workflow_runs", [])
        return runs[0] if runs else None

    # ---------- Campaign creation — direct commit, no branch/PR ----------
    #
    # Deliberately a direct commit to `base` (main), not a PR: the goal is
    # for campaign creation to never require a trip to GitHub. The
    # remaining safety net is the in-app confirmation the Streamlit page
    # requires before calling this — see campaign_builder.py.

    def create_file(self, path: str, content_bytes: bytes, message: str, branch: str = "main") -> None:
        url = f"{GITHUB_API}/repos/{self.owner}/{self.repo}/contents/{path}"
        payload = {
            "message": message,
            "content": base64.b64encode(content_bytes).decode("ascii"),
            "branch": branch,
        }
        resp = requests.put(url, json=payload, headers=self._headers, timeout=self.timeout)
        if resp.status_code not in (200, 201):
            raise GitHubActionsError(f"Failed to create file '{path}': {resp.status_code} {resp.text[:300]}")

    def commit_campaign_files_directly(self, files: List[Dict[str, bytes]], commit_message: str,
                                        base: str = "main") -> None:
        """files: [{'path': 'templates/Foo/intro_A.txt', 'content': b'...'}].
        Commits every file straight to `base`. Raises on the first failure —
        callers should treat a partial failure as "check the repo", since a
        prior file in the list may have already landed."""
        for f in files:
            self.create_file(f["path"], f["content"], message=commit_message, branch=base)
