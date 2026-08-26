"""Thin GitHub REST API client. Every network call is isolated to this
module and goes through `requests`, so tests can mock `requests.*` directly
without touching real GitHub.

Token scope needed:
- actions: read, actions: write  -> dispatch_workflow, get_run, find_recent_run
- contents: write, pull_requests: write -> create_branch/create_file/
  create_pull_request (Phase 3 only — see README for why this is a
  separate, larger grant you may not want to hand out by default).
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

    # ---------- Phase 3: campaign creation via PR (never a direct commit) ----------

    def get_branch_sha(self, branch: str) -> str:
        url = f"{GITHUB_API}/repos/{self.owner}/{self.repo}/git/ref/heads/{branch}"
        resp = requests.get(url, headers=self._headers, timeout=self.timeout)
        if resp.status_code != 200:
            raise GitHubActionsError(f"Failed to read ref '{branch}': {resp.status_code} {resp.text[:300]}")
        return resp.json()["object"]["sha"]

    def create_branch(self, new_branch: str, base: str = "main") -> None:
        base_sha = self.get_branch_sha(base)
        url = f"{GITHUB_API}/repos/{self.owner}/{self.repo}/git/refs"
        payload = {"ref": f"refs/heads/{new_branch}", "sha": base_sha}
        resp = requests.post(url, json=payload, headers=self._headers, timeout=self.timeout)
        if resp.status_code != 201:
            if resp.status_code == 422 and "already exists" in resp.text.lower():
                raise GitHubActionsError(
                    f"Branch '{new_branch}' already exists — likely a leftover from a previous attempt "
                    "(a closed/abandoned PR, or a merge whose branch wasn't auto-deleted). Delete it on "
                    f"GitHub under Branches, then retry. Raw error: {resp.text[:200]}"
                )
            raise GitHubActionsError(f"Failed to create branch '{new_branch}': {resp.status_code} {resp.text[:300]}")

    def create_file(self, path: str, content_bytes: bytes, message: str, branch: str) -> None:
        url = f"{GITHUB_API}/repos/{self.owner}/{self.repo}/contents/{path}"
        payload = {
            "message": message,
            "content": base64.b64encode(content_bytes).decode("ascii"),
            "branch": branch,
        }
        resp = requests.put(url, json=payload, headers=self._headers, timeout=self.timeout)
        if resp.status_code not in (200, 201):
            raise GitHubActionsError(f"Failed to create file '{path}': {resp.status_code} {resp.text[:300]}")

    def create_pull_request(self, title: str, head: str, base: str, body: str) -> Dict:
        url = f"{GITHUB_API}/repos/{self.owner}/{self.repo}/pulls"
        payload = {"title": title, "head": head, "base": base, "body": body}
        resp = requests.post(url, json=payload, headers=self._headers, timeout=self.timeout)
        if resp.status_code != 201:
            raise GitHubActionsError(f"Failed to open pull request: {resp.status_code} {resp.text[:300]}")
        return resp.json()

    def open_campaign_pull_request(self, branch_name: str, files: List[Dict[str, bytes]],
                                    pr_title: str, pr_body: str, base: str = "main") -> Dict:
        """files: [{'path': 'templates/Foo/intro_A.txt', 'content': b'...'}]
        Creates the branch, commits every file to it, then opens a PR
        against `base`. Nothing is ever committed directly to `base`."""
        self.create_branch(branch_name, base=base)
        for f in files:
            self.create_file(f["path"], f["content"], message=f"Add {f['path']}", branch=branch_name)
        return self.create_pull_request(pr_title, head=branch_name, base=base, body=pr_body)
