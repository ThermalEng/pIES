"""Minimal-privilege GitHub REST API client for stateless market PR creation."""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx
import yaml
from pies_sdk import validate_publishers_registry

from pies_market_gateway.config import GatewaySettings


class GitHubGatewayError(Exception):
    """Raised when GitHub API communication fails or returns unexpected state."""


@dataclass(frozen=True, slots=True)
class PullRequestResult:
    """Result of an idempotent market PR lookup or creation."""

    duplicate: bool
    pr_number: int
    pr_url: str
    branch: str


class GitHubSubmissionClient:
    """Thin httpx client speaking GitHub REST v3 for reading `publishers.yaml` and opening PRs."""

    def __init__(self, settings: GatewaySettings) -> None:
        self._settings = settings
        self._owner, self._repo = self._split_repo(settings.market_repo)
        self._base_url = settings.github_api_base.rstrip("/")
        self._headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if settings.github_token:
            self._headers["Authorization"] = f"Bearer {settings.github_token}"

    @staticmethod
    def _split_repo(repo_slug: str) -> tuple[str, str]:
        parts = [p.strip() for p in repo_slug.split("/") if p.strip()]
        if len(parts) != 2:
            raise GitHubGatewayError(f"MARKET_REPO must be '<owner>/<repo>', got {repo_slug!r}")
        return parts[0], parts[1]

    def _client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self._base_url,
            headers=self._headers,
            timeout=self._settings.http_timeout_seconds,
        )

    def fetch_publishers_registry(self) -> tuple[dict[str, Any], str | None]:
        """Fetch and validate `publishers.yaml` from `market_base_branch`."""
        url = f"/repos/{self._owner}/{self._repo}/contents/publishers.yaml"
        with self._client() as client:
            resp = client.get(url, params={"ref": self._settings.market_base_branch})
            if resp.status_code == 404:
                empty = validate_publishers_registry(
                    {"schema": "ies.publishers", "schema_version": "1.0.0", "publishers": {}}
                )
                return empty, None
            if resp.status_code >= 400:
                raise GitHubGatewayError(
                    f"Failed to read publishers.yaml from GitHub: HTTP {resp.status_code}"
                )
            body = resp.json()
        raw_bytes = base64.b64decode(body["content"])
        raw_text = raw_bytes.decode("utf-8")
        if "@" in raw_text:
            raise GitHubGatewayError("Upstream publishers.yaml contains forbidden '@' character")
        parsed = yaml.safe_load(raw_text)
        validated = validate_publishers_registry(parsed)
        return validated, str(body["sha"])

    def find_open_pr_for_branch(self, branch_name: str) -> PullRequestResult | None:
        """Return existing open PR for deterministic `branch_name` if one exists."""
        url = f"/repos/{self._owner}/{self._repo}/pulls"
        with self._client() as client:
            resp = client.get(
                url,
                params={
                    "state": "open",
                    "head": f"{self._owner}:{branch_name}",
                    "base": self._settings.market_base_branch,
                },
            )
            if resp.status_code >= 400:
                raise GitHubGatewayError(f"Failed to list open PRs: HTTP {resp.status_code}")
            pulls = resp.json()
        if isinstance(pulls, list) and pulls:
            first = pulls[0]
            return PullRequestResult(
                duplicate=True,
                pr_number=int(first["number"]),
                pr_url=str(first["html_url"]),
                branch=branch_name,
            )
        return None

    def create_submission_pr(
        self,
        *,
        branch_name: str,
        files: Mapping[str, bytes],
        publishers_sha: str | None,
        submission_metadata: Mapping[str, Any],
        pr_title: str,
        commit_message: str,
    ) -> PullRequestResult:
        """Atomically build Git blobs/tree/commit before creating/updating ref and opening PR."""
        del publishers_sha
        existing = self.find_open_pr_for_branch(branch_name)
        if existing is not None:
            return existing

        with self._client() as client:
            ref_resp = client.get(
                f"/repos/{self._owner}/{self._repo}/git/ref/heads/{self._settings.market_base_branch}"
            )
            if ref_resp.status_code >= 400:
                raise GitHubGatewayError(
                    f"Failed to resolve base branch {self._settings.market_base_branch!r}: "
                    f"HTTP {ref_resp.status_code}"
                )
            base_commit_sha = str(ref_resp.json()["object"]["sha"])

            base_commit_resp = client.get(
                f"/repos/{self._owner}/{self._repo}/git/commits/{base_commit_sha}"
            )
            if base_commit_resp.status_code >= 400:
                raise GitHubGatewayError(
                    f"Failed to read base commit {base_commit_sha!r}: HTTP {base_commit_resp.status_code}"
                )
            base_tree_sha = str(base_commit_resp.json()["tree"]["sha"])

            tree_entries: list[dict[str, str]] = []
            for rel_path in sorted(files):
                blob_resp = client.post(
                    f"/repos/{self._owner}/{self._repo}/git/blobs",
                    json={
                        "content": base64.b64encode(files[rel_path]).decode("ascii"),
                        "encoding": "base64",
                    },
                )
                if blob_resp.status_code >= 400:
                    raise GitHubGatewayError(
                        f"Failed to create blob for {rel_path!r}: HTTP {blob_resp.status_code}"
                    )
                tree_entries.append(
                    {
                        "path": rel_path,
                        "mode": "100644",
                        "type": "blob",
                        "sha": str(blob_resp.json()["sha"]),
                    }
                )

            tree_resp = client.post(
                f"/repos/{self._owner}/{self._repo}/git/trees",
                json={"base_tree": base_tree_sha, "tree": tree_entries},
            )
            if tree_resp.status_code >= 400:
                raise GitHubGatewayError(f"Failed to create git tree: HTTP {tree_resp.status_code}")
            new_tree_sha = str(tree_resp.json()["sha"])

            commit_resp = client.post(
                f"/repos/{self._owner}/{self._repo}/git/commits",
                json={
                    "message": commit_message,
                    "tree": new_tree_sha,
                    "parents": [base_commit_sha],
                },
            )
            if commit_resp.status_code >= 400:
                raise GitHubGatewayError(f"Failed to create git commit: HTTP {commit_resp.status_code}")
            new_commit_sha = str(commit_resp.json()["sha"])

            create_ref = client.post(
                f"/repos/{self._owner}/{self._repo}/git/refs",
                json={"ref": f"refs/heads/{branch_name}", "sha": new_commit_sha},
            )
            if create_ref.status_code == 422:
                raced = self.find_open_pr_for_branch(branch_name)
                if raced is not None:
                    return raced
                # Branch exists without an open PR (e.g. retry after transient PR creation error).
                patch_ref = client.patch(
                    f"/repos/{self._owner}/{self._repo}/git/refs/heads/{branch_name}",
                    json={"sha": new_commit_sha, "force": True},
                )
                if patch_ref.status_code >= 400:
                    raise GitHubGatewayError(
                        f"Failed to update existing submission branch {branch_name!r}: "
                        f"HTTP {patch_ref.status_code}"
                    )
            elif create_ref.status_code >= 400:
                raise GitHubGatewayError(
                    f"Failed to create branch {branch_name!r}: HTTP {create_ref.status_code}"
                )

            pr_body = (
                "Automated pIES Market artifact submission (stateless gateway; zero plaintext email).\n\n"
                "```json\n"
                f"{json.dumps(dict(submission_metadata), ensure_ascii=False, indent=2, sort_keys=True)}\n"
                "```\n"
            )
            pr_resp = client.post(
                f"/repos/{self._owner}/{self._repo}/pulls",
                json={
                    "title": pr_title,
                    "head": branch_name,
                    "base": self._settings.market_base_branch,
                    "body": pr_body,
                },
            )
            if pr_resp.status_code == 422:
                raced = self.find_open_pr_for_branch(branch_name)
                if raced is not None:
                    return raced
            if pr_resp.status_code >= 400:
                raise GitHubGatewayError(f"Failed to create PR: HTTP {pr_resp.status_code}")
            pr_data = pr_resp.json()
            return PullRequestResult(
                duplicate=False,
                pr_number=int(pr_data["number"]),
                pr_url=str(pr_data["html_url"]),
                branch=branch_name,
            )
