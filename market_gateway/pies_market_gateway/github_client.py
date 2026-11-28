"""Minimal-privilege GitHub REST API client for stateless market PR creation."""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx
import yaml
from pies_sdk import PiesSdkError, validate_publishers_registry

from pies_market_gateway.config import GatewaySettings


class GitHubGatewayError(Exception):
    """Raised when GitHub API communication fails or returns unexpected state."""


class SubmissionConflictError(Exception):
    """Raised when a submission branch already exists with a conflicting payload."""


@dataclass(frozen=True, slots=True)
class BaseRegistrySnapshot:
    """Snapshot of `publishers.yaml` pinned to one base commit and tree SHA."""

    publishers_doc: dict[str, Any]
    base_commit_sha: str
    base_tree_sha: str


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

    def _resolve_branch_commit(self, client: httpx.Client, branch_name: str) -> tuple[str, str]:
        ref_resp = client.get(
            f"/repos/{self._owner}/{self._repo}/git/ref/heads/{branch_name}"
        )
        if ref_resp.status_code >= 400:
            raise GitHubGatewayError(
                f"Failed to resolve branch {branch_name!r}: HTTP {ref_resp.status_code}"
            )
        commit_sha = str(ref_resp.json()["object"]["sha"])

        commit_resp = client.get(
            f"/repos/{self._owner}/{self._repo}/git/commits/{commit_sha}"
        )
        if commit_resp.status_code >= 400:
            raise GitHubGatewayError(
                f"Failed to read commit {commit_sha!r}: HTTP {commit_resp.status_code}"
            )
        tree_sha = str(commit_resp.json()["tree"]["sha"])
        return commit_sha, tree_sha

    def _read_blob_bytes(self, client: httpx.Client, blob_sha: str) -> bytes:
        blob_resp = client.get(f"/repos/{self._owner}/{self._repo}/git/blobs/{blob_sha}")
        if blob_resp.status_code >= 400:
            raise GitHubGatewayError(
                f"Failed to read git blob {blob_sha!r}: HTTP {blob_resp.status_code}"
            )
        return base64.b64decode(blob_resp.json()["content"])

    @staticmethod
    def _parse_publishers_bytes(raw_bytes: bytes) -> dict[str, Any]:
        try:
            raw_text = raw_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise GitHubGatewayError("Upstream publishers.yaml is not valid UTF-8") from exc
        if "@" in raw_text:
            raise GitHubGatewayError("Upstream publishers.yaml contains forbidden '@' character")
        try:
            parsed = yaml.safe_load(raw_text)
            return validate_publishers_registry(parsed)
        except (yaml.YAMLError, PiesSdkError) as exc:
            raise GitHubGatewayError(f"Invalid publishers.yaml payload: {exc}") from exc

    def _assert_existing_branch_matches(
        self,
        client: httpx.Client,
        *,
        branch_name: str,
        files: Mapping[str, bytes],
        publisher: str,
        submission_prefix: str,
        created_blob_shas: Mapping[str, str] | None = None,
    ) -> None:
        _, tree_sha = self._resolve_branch_commit(client, branch_name)
        tree_resp = client.get(
            f"/repos/{self._owner}/{self._repo}/git/trees/{tree_sha}",
            params={"recursive": "1"},
        )
        if tree_resp.status_code >= 400:
            raise GitHubGatewayError(
                f"Failed to inspect tree {tree_sha!r} for branch {branch_name!r}: "
                f"HTTP {tree_resp.status_code}"
            )
        branch_blobs = {
            str(entry["path"]): str(entry["sha"])
            for entry in tree_resp.json().get("tree", [])
            if entry.get("type") == "blob"
        }

        expected_artifact_paths = {p for p in files if p != "publishers.yaml"}
        existing_artifact_paths = {p for p in branch_blobs if p.startswith(submission_prefix)}
        if existing_artifact_paths != expected_artifact_paths:
            raise SubmissionConflictError(
                f"Submission branch {branch_name!r} already exists with a different file set"
            )

        for rel_path in sorted(expected_artifact_paths):
            existing_sha = branch_blobs[rel_path]
            if created_blob_shas is not None and created_blob_shas.get(rel_path) == existing_sha:
                continue
            existing_bytes = self._read_blob_bytes(client, existing_sha)
            if existing_bytes != files[rel_path]:
                raise SubmissionConflictError(
                    f"Submission branch {branch_name!r} already exists with conflicting {rel_path!r}"
                )

        if "publishers.yaml" in files:
            pub_sha = branch_blobs.get("publishers.yaml")
            if pub_sha is None:
                raise SubmissionConflictError(
                    f"Submission branch {branch_name!r} is missing expected publishers.yaml"
                )
            existing_pub_doc = self._parse_publishers_bytes(self._read_blob_bytes(client, pub_sha))
            expected_pub_doc = self._parse_publishers_bytes(files["publishers.yaml"])
            existing_entry = existing_pub_doc["publishers"].get(publisher)
            expected_entry = expected_pub_doc["publishers"].get(publisher)
            if (
                existing_entry is None
                or expected_entry is None
                or existing_entry["email_digest"] != expected_entry["email_digest"]
                or existing_entry["status"] != expected_entry["status"]
            ):
                raise SubmissionConflictError(
                    f"Submission branch {branch_name!r} conflicts on publisher {publisher!r} claim"
                )

    def fetch_publishers_registry(self) -> BaseRegistrySnapshot:
        """Fetch `publishers.yaml` pinned to the resolved base commit and tree SHA."""
        with self._client() as client:
            base_commit_sha, base_tree_sha = self._resolve_branch_commit(
                client, self._settings.market_base_branch
            )
            url = f"/repos/{self._owner}/{self._repo}/contents/publishers.yaml"
            resp = client.get(url, params={"ref": base_commit_sha})
            if resp.status_code >= 400:
                raise GitHubGatewayError(
                    f"Failed to read publishers.yaml from GitHub: HTTP {resp.status_code}"
                )
            body = resp.json()
        raw_bytes = base64.b64decode(body["content"])
        validated = self._parse_publishers_bytes(raw_bytes)
        return BaseRegistrySnapshot(
            publishers_doc=validated,
            base_commit_sha=base_commit_sha,
            base_tree_sha=base_tree_sha,
        )

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
        base_commit_sha: str,
        base_tree_sha: str,
        publisher: str,
        submission_prefix: str,
        submission_metadata: Mapping[str, Any],
        pr_title: str,
        commit_message: str,
    ) -> PullRequestResult:
        """Atomically build blobs/tree/commit on a pinned base commit and open a PR without force-pushes."""
        with self._client() as client:
            existing = self.find_open_pr_for_branch(branch_name)
            if existing is not None:
                self._assert_existing_branch_matches(
                    client,
                    branch_name=branch_name,
                    files=files,
                    publisher=publisher,
                    submission_prefix=submission_prefix,
                )
                return existing

            created_blob_shas: dict[str, str] = {}
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
                blob_sha = str(blob_resp.json()["sha"])
                created_blob_shas[rel_path] = blob_sha
                tree_entries.append(
                    {
                        "path": rel_path,
                        "mode": "100644",
                        "type": "blob",
                        "sha": blob_sha,
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
                self._assert_existing_branch_matches(
                    client,
                    branch_name=branch_name,
                    files=files,
                    publisher=publisher,
                    submission_prefix=submission_prefix,
                    created_blob_shas=created_blob_shas,
                )
                raced = self.find_open_pr_for_branch(branch_name)
                if raced is not None:
                    return raced
                # Matching branch already exists without an open PR: reuse it without force-overwriting.
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
                    self._assert_existing_branch_matches(
                        client,
                        branch_name=branch_name,
                        files=files,
                        publisher=publisher,
                        submission_prefix=submission_prefix,
                        created_blob_shas=created_blob_shas,
                    )
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
