#!/usr/bin/env python3
"""Fast-forward destination branches from source branches using a GitHub App."""

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

import jwt
import requests


class Status(Enum):
    CREATED = "created"
    FORWARDED = "forwarded"
    UP_TO_DATE = "up_to_date"
    FAILED = "failed"


@dataclass(frozen=True)
class RepoEntry:
    org: str
    repo: str
    source: str
    destination: str

    @property
    def label(self) -> str:
        return f"{self.org}/{self.repo} ({self.source} -> {self.destination})"


@dataclass(frozen=True)
class Result:
    entry: RepoEntry
    status: Status
    message: str


class GitHubApp:
    def __init__(self, *, app_id: int, private_key: str) -> None:
        self._app_id = app_id
        self._private_key = private_key
        self._tokens: dict[str, str] = {}

    def _generate_jwt(self) -> str:
        now = int(time.time())
        payload = {"iat": now, "exp": now + 600, "iss": self._app_id}
        return jwt.encode(payload, self._private_key, algorithm="RS256")

    def get_token(self, org: str) -> str:
        if org in self._tokens:
            return self._tokens[org]

        encoded_jwt = self._generate_jwt()
        headers = {
            "Authorization": f"Bearer {encoded_jwt}",
            "Accept": "application/vnd.github.v3+json",
        }

        resp = requests.get(
            "https://api.github.com/app/installations", headers=headers
        )
        resp.raise_for_status()

        installation_id: int | None = None
        for installation in resp.json():
            if installation["account"]["login"] == org:
                installation_id = installation["id"]
                break

        if installation_id is None:
            raise ValueError(f"no installation found for org '{org}'")

        resp = requests.post(
            f"https://api.github.com/app/installations/{installation_id}/access_tokens",
            headers=headers,
        )
        resp.raise_for_status()

        token: str = resp.json()["token"]
        self._tokens[org] = token
        return token


def _sanitize(text: str) -> str:
    return re.sub(r"https://x-access-token:[^@]+@", "https://***@", text)


def _run(args: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=True)


def _clone(
    url: str, branch: str, dest: Path
) -> subprocess.CompletedProcess[str] | None:
    try:
        return _run(
            ["git", "clone", "-b", branch, "--single-branch", url, str(dest)],
            cwd=dest.parent,
        )
    except subprocess.CalledProcessError as e:
        print(f"  clone {branch}: {_sanitize(e.stderr.strip())}")
        return None


def fast_forward(app: GitHubApp, entry: RepoEntry) -> Result:
    token = app.get_token(entry.org)
    repo_url = f"https://x-access-token:{token}@github.com/{entry.org}/{entry.repo}.git"

    with tempfile.TemporaryDirectory() as tmp:
        repo_dir = Path(tmp) / entry.repo

        if _clone(repo_url, entry.destination, repo_dir) is None:
            if _clone(repo_url, entry.source, repo_dir) is None:
                return Result(entry, Status.FAILED, f"could not clone {entry.source}")

            _run(["git", "config", "user.name", "assisted-installer-ci[bot]"], cwd=repo_dir)
            _run(["git", "config", "user.email", "noreply@github.com"], cwd=repo_dir)
            _run(["git", "checkout", "-b", entry.destination], cwd=repo_dir)
            _run(["git", "push", "origin", entry.destination], cwd=repo_dir)
            return Result(entry, Status.CREATED, f"created {entry.destination} from {entry.source}")

        before = _run(["git", "rev-parse", "HEAD"], cwd=repo_dir).stdout.strip()

        try:
            _run(["git", "pull", "--ff-only", "origin", entry.source], cwd=repo_dir)
        except subprocess.CalledProcessError:
            return Result(entry, Status.FAILED, "cannot fast-forward, branches may have diverged")

        after = _run(["git", "rev-parse", "HEAD"], cwd=repo_dir).stdout.strip()

        if before == after:
            return Result(entry, Status.UP_TO_DATE, "already up to date")

        log = _run(
            ["git", "log", "--pretty=oneline", f"{before}..{after}"], cwd=repo_dir
        ).stdout.strip()
        count = len(log.splitlines())

        try:
            _run(["git", "push", "origin", entry.destination], cwd=repo_dir)
        except subprocess.CalledProcessError:
            return Result(entry, Status.FAILED, "fast-forwarded locally but push failed")

        return Result(entry, Status.FORWARDED, f"{count} commit(s)\n{log}")


def load_config(path: Path) -> list[RepoEntry]:
    data = json.loads(path.read_text())
    return [
        RepoEntry(
            org=item["org"],
            repo=item["repo"],
            source=item["source"],
            destination=item["destination"],
        )
        for item in data
    ]


def main() -> None:
    app_id_raw = os.environ.get("APP_ID")
    private_key = os.environ.get("APP_PRIVATE_KEY")
    if not app_id_raw or not private_key:
        print("ERROR: APP_ID and APP_PRIVATE_KEY env vars are required")
        sys.exit(1)

    config_path = Path(os.environ.get("CONFIG_FILE", Path(__file__).parent / "config.json"))
    entries = load_config(config_path)
    app = GitHubApp(app_id=int(app_id_raw), private_key=private_key)

    print(f"Fast-forwarding {len(entries)} repos")
    results: list[Result] = []

    for entry in entries:
        print(f"\n=== {entry.label} ===")
        try:
            result = fast_forward(app, entry)
        except Exception as e:
            result = Result(entry, Status.FAILED, _sanitize(str(e)))
        results.append(result)
        print(f"{result.status.value}: {result.message}")

    print("\n" + "=" * 40)
    print("SUMMARY")
    print("=" * 40)

    by_status: dict[Status, list[Result]] = {}
    for r in results:
        by_status.setdefault(r.status, []).append(r)

    for status in Status:
        group = by_status.get(status, [])
        if group:
            print(f"\n{status.value} ({len(group)}):")
            for r in group:
                print(f"  {r.entry.label}")

    failed = by_status.get(Status.FAILED, [])
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
