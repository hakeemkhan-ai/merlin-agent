"""Versioned contract regressions and explicitly gated live fixture evaluations.

Offline results exercise fake-backed unittest contracts, never model quality.
Live grading reads authenticated task history and checksum-verified checkpoints;
it never runs repository code on the machine hosting this evaluator.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import sys
import tarfile
import time
import unittest
from collections import Counter
from collections.abc import Mapping, Sequence
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote, urlsplit
from uuid import uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

ROOT = Path(__file__).resolve().parents[2]
CATALOGUE = ROOT / "evals" / "scenarios.v1.json"
FIXTURES = ROOT / "evals" / "fixtures"
LABELS = {
    "offline": "Offline contract checks with faked providers; NOT a real model benchmark.",
    "live": "Live fixture evaluations; only independently checked objectives count as passes.",
}
Status = Literal["passed", "failed", "inconclusive", "skipped"]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Objective(Contract):
    kind: Literal[
        "file_equals",
        "file_contains",
        "file_absent",
        "json_equals",
        "tool_output_contains",
        "tool_data_equals",
        "tool_succeeded",
        "child_integrated",
        "artifact_png",
    ]
    path: str = ""
    tool: str = ""
    key: list[str] = Field(default_factory=list)
    expected: Any = None

    @model_validator(mode="after")
    def validate_target(self) -> Objective:
        if self.kind.startswith("file_") or self.kind == "json_equals":
            safe_relative(self.path)
        elif self.kind != "child_integrated" and not self.tool:
            raise ValueError("A tool objective requires its concrete tool name")
        return self


class Scenario(Contract):
    id: str = Field(pattern=r"^[a-z][a-z0-9-]+$")
    category: Literal[
        "edits", "navigation", "browser", "context", "delegation", "recovery", "github"
    ]
    description: str
    offline_tests: list[str] = Field(min_length=1)
    live_prompt: str | None = None
    live_unavailable_reason: str | None = None
    allow_changes: list[str] = Field(default_factory=list)
    objectives: list[Objective] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_modes(self) -> Scenario:
        if bool(self.live_prompt) == bool(self.live_unavailable_reason):
            raise ValueError("Declare either a live prompt or an explicit unsupported reason")
        if self.live_prompt and not self.objectives:
            raise ValueError("Live scenarios require machine-checkable objectives")
        for path in self.allow_changes:
            safe_relative(path)
        for name in self.offline_tests:
            if not re.fullmatch(r"test_[a-z_]+\.[A-Za-z]+Tests\.test_[a-z_]+", name):
                raise ValueError("Offline tests must identify one concrete unittest method")
        return self


class Catalogue(Contract):
    version: Literal[1]
    scenarios: list[Scenario] = Field(min_length=20)

    @model_validator(mode="after")
    def unique_ids(self) -> Catalogue:
        ids = [scenario.id for scenario in self.scenarios]
        if len(ids) != len(set(ids)):
            raise ValueError("Scenario IDs must be unique")
        return self


class Evidence(Contract):
    check: str
    status: Status
    detail: str
    duration_seconds: float = Field(default=0, ge=0)


class ScenarioResult(Contract):
    scenario_id: str
    category: str
    status: Status
    duration_seconds: float = Field(ge=0)
    evidence: list[Evidence]
    task_id: str | None = None
    usage: dict[str, Any] | None = None


class Report(Contract):
    schema_version: Literal[1] = 1
    catalogue_version: int = 1
    mode: Literal["offline", "live"]
    label: str
    started_at: str
    duration_seconds: float = Field(ge=0)
    results: list[ScenarioResult]

    @property
    def exit_code(self) -> int:
        return 0 if self.results and all(item.status == "passed" for item in self.results) else 1

    def document(self) -> dict[str, Any]:
        return {**self.model_dump(), "summary": dict(Counter(item.status for item in self.results))}


def safe_relative(path: str) -> str:
    from merlin.sandbox._remote import relative_name

    if relative_name(path) != path:
        raise ValueError("Evaluation paths must be canonical relative paths")
    return path


def load_catalogue(path: Path = CATALOGUE) -> Catalogue:
    return Catalogue.model_validate_json(path.read_text())


def select_scenarios(catalogue: Catalogue, ids: Sequence[str]) -> list[Scenario]:
    unknown = set(ids) - {item.id for item in catalogue.scenarios}
    if unknown:
        raise ValueError("Unknown scenario IDs: " + ", ".join(sorted(unknown)))
    return [item for item in catalogue.scenarios if not ids or item.id in ids]


class RecordingResult(unittest.TestResult):
    """Keep assertion evidence without dumping fixture values or credentials."""

    def __init__(self) -> None:
        super().__init__()
        self.evidence: list[Evidence] = []
        self.started = 0.0

    def startTest(self, test: unittest.TestCase) -> None:  # noqa: N802
        super().startTest(test)
        self.started = time.perf_counter()

    def record(self, test: unittest.TestCase, status: Status, detail: str) -> None:
        self.evidence.append(
            Evidence(
                check=test.id(),
                status=status,
                detail=detail,
                duration_seconds=time.perf_counter() - self.started,
            )
        )

    def addSuccess(self, test: unittest.TestCase) -> None:  # noqa: N802
        super().addSuccess(test)
        self.record(test, "passed", "Concrete unittest assertions passed")

    def addFailure(self, test: unittest.TestCase, err: Any) -> None:  # noqa: N802
        super().addFailure(test, err)
        self.record(test, "failed", "Assertion failed; rerun this test ID for local diagnostics")

    def addError(self, test: unittest.TestCase, err: Any) -> None:  # noqa: N802
        super().addError(test, err)
        self.record(test, "failed", "Test raised an error; rerun this test ID for diagnostics")

    def addSkip(self, test: unittest.TestCase, reason: str) -> None:  # noqa: N802
        super().addSkip(test, reason)
        self.record(test, "skipped", "Unittest skipped this contract; it did not pass")

    def addExpectedFailure(self, test: unittest.TestCase, err: Any) -> None:  # noqa: N802
        super().addExpectedFailure(test, err)
        self.record(test, "failed", "Expected failures do not satisfy an evaluation objective")

    def addUnexpectedSuccess(self, test: unittest.TestCase) -> None:  # noqa: N802
        super().addUnexpectedSuccess(test)
        self.record(test, "failed", "Unexpected unittest success requires contract review")

    def addSubTest(self, test: unittest.TestCase, subtest: Any, err: Any) -> None:  # noqa: N802
        super().addSubTest(test, subtest, err)
        if err is not None:
            self.record(test, "failed", "A concrete unittest subtest failed")


def combined_status(evidence: Sequence[Evidence]) -> Status:
    if any(item.status == "failed" for item in evidence):
        return "failed"
    if any(item.status == "inconclusive" for item in evidence):
        return "inconclusive"
    if not evidence or any(item.status == "skipped" for item in evidence):
        return "skipped"
    return "passed"


def run_offline(scenarios: Sequence[Scenario], tests_dir: Path = ROOT / "tests") -> Report:
    """Run the catalogue's concrete tests in process, using their provider fakes."""
    started, clock = datetime.now(UTC).isoformat(), time.perf_counter()
    results: list[ScenarioResult] = []
    original_path = sys.path[:]
    sys.path.insert(0, str(tests_dir.resolve()))
    try:
        for scenario in scenarios:
            scenario_clock = time.perf_counter()
            evidence: list[Evidence] = []
            for test_id in scenario.offline_tests:
                loader = unittest.TestLoader()
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    suite = loader.loadTestsFromName(test_id)
                    if loader.errors or suite.countTestCases() != 1:
                        evidence.append(
                            Evidence(
                                check=test_id,
                                status="failed",
                                detail="The concrete unittest ID could not be loaded exactly once",
                            )
                        )
                        continue
                    outcome = RecordingResult()
                    suite.run(outcome)
                evidence.extend(outcome.evidence)
                if not outcome.evidence:
                    evidence.append(
                        Evidence(
                            check=test_id,
                            status="failed",
                            detail="No unittest outcome was recorded",
                        )
                    )
            results.append(
                ScenarioResult(
                    scenario_id=scenario.id,
                    category=scenario.category,
                    status=combined_status(evidence),
                    evidence=evidence,
                    duration_seconds=time.perf_counter() - scenario_clock,
                )
            )
    finally:
        sys.path[:] = original_path
    return Report(
        mode="offline",
        label=LABELS["offline"],
        started_at=started,
        duration_seconds=time.perf_counter() - clock,
        results=results,
    )


class LiveConfiguration(Contract):
    api_url: str
    repository: str
    owner_token: str = Field(repr=False)
    github_token: str = Field(repr=False)

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> LiveConfiguration:
        env = os.environ if environ is None else environ
        if env.get("MERLIN_EVAL_LIVE") != "1":
            raise ValueError("Live evaluations require MERLIN_EVAL_LIVE=1")
        names = {
            "api_url": "MERLIN_EVAL_API_URL",
            "repository": "MERLIN_EVAL_REPOSITORY",
            "owner_token": "MERLIN_EVAL_OWNER_TOKEN",
            "github_token": "MERLIN_EVAL_GITHUB_TOKEN",
        }
        if any(not env.get(name) for name in names.values()):
            raise ValueError(
                "Live evaluations require dedicated API, repository, and read credentials"
            )
        repository = env[names["repository"]]
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise ValueError("MERLIN_EVAL_REPOSITORY must be owner/repository")
        if env.get("MERLIN_EVAL_FIXTURE_ACK") != repository:
            raise ValueError("MERLIN_EVAL_FIXTURE_ACK must name the dedicated fixture repository")
        url = urlsplit(env[names["api_url"]])
        if (
            not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
            or url.path not in {"", "/"}
            or not (
                url.scheme == "https"
                or (url.scheme == "http" and url.hostname in {"127.0.0.1", "localhost", "::1"})
            )
        ):
            raise ValueError("The evaluation API must use HTTPS or sandbox-local loopback HTTP")
        if len(env[names["owner_token"]]) < 32:
            raise ValueError("The evaluation owner credential must contain at least 32 characters")
        return cls(**{key: env[name] for key, name in names.items()})


def git_blob_sha(content: bytes) -> str:
    return hashlib.sha1(  # Git's object identity, not a security checksum.
        f"blob {len(content)}\0".encode() + content,
        usedforsecurity=False,
    ).hexdigest()


def fixture_files(root: Path = FIXTURES) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }


def checkpoint_files(content: bytes, expected_sha256: str) -> dict[str, bytes]:
    """Verify an archive without extracting or executing anything in it."""
    from merlin.sandbox._remote import MANIFEST, validate_archive

    if hashlib.sha256(content).hexdigest() != expected_sha256:
        raise ValueError("Checkpoint SHA-256 does not match recorded metadata")
    validate_archive(content)
    files: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(content), mode="r:") as archive:
        for member in archive.getmembers():
            if member.name == MANIFEST or member.name.startswith(".git/"):
                continue
            stream = archive.extractfile(member)
            if stream is not None:
                files[member.name] = stream.read()
    return files


def tool_results(task: Mapping[str, Any], tool: str) -> list[dict[str, Any]]:
    """Match persisted responses to concrete dispatched call IDs and names."""
    history = task.get("history", [])
    ids = {
        call["id"]
        for entry in history
        for call in entry.get("_merlin_calls", [])
        if call.get("name") == tool
    }
    return [
        response["response"]
        for entry in history
        for part in entry.get("parts", [])
        if isinstance(part, dict)
        and isinstance(response := part.get("function_response"), dict)
        and response.get("name") == tool
        and response.get("id") in ids
        and isinstance(response.get("response"), dict)
        and response["response"].get("ok") is True
        and not response["response"].get("uncertain")
    ]


def lookup(value: Any, keys: Sequence[str]) -> Any:
    for key in keys:
        if not isinstance(value, dict) or key not in value:
            raise KeyError(key)
        value = value[key]
    return value


def grade_objectives(
    scenario: Scenario,
    task: Mapping[str, Any],
    files: Mapping[str, bytes],
    baseline: Mapping[str, str],
    repository: str,
    artifacts: Mapping[str, bytes] | None = None,
) -> list[Evidence]:
    """Check protected source and objectives, independently of final model prose."""
    evidence: list[Evidence] = []
    allowed = set(scenario.allow_changes)
    changed = sorted(
        path
        for path in set(baseline) | set(files)
        if path not in allowed
        and (
            path not in files or path not in baseline or git_blob_sha(files[path]) != baseline[path]
        )
    )
    evidence.append(
        Evidence(
            check="preserve-unrelated-files",
            status="failed" if changed else "passed",
            detail="Unrequested source changes detected"
            if changed
            else "All protected source hashes match",
        )
    )
    for index, objective in enumerate(scenario.objectives):
        passed = False
        try:
            if objective.kind == "file_equals":
                passed = files.get(objective.path) == str(objective.expected).encode()
            elif objective.kind == "file_contains":
                passed = str(objective.expected).encode() in files.get(objective.path, b"")
            elif objective.kind == "file_absent":
                passed = objective.path not in files
            elif objective.kind == "json_equals":
                passed = (
                    lookup(json.loads(files[objective.path]), objective.key) == objective.expected
                )
            elif objective.kind == "child_integrated":
                spawned = tool_results(task, "spawn_child")
                integrated = tool_results(task, "integrate_child")
                child_ids = {item.get("data", {}).get("child_id") for item in spawned}
                passed = any(
                    item.get("data", {}).get("child_id") in child_ids - {None}
                    for item in integrated
                )
            else:
                responses = tool_results(task, objective.tool)
                if objective.kind == "tool_succeeded":
                    passed = bool(responses)
                elif objective.kind == "tool_output_contains":
                    passed = any(
                        str(objective.expected) in item.get("output", "") for item in responses
                    )
                elif objective.kind == "tool_data_equals":
                    expected = (
                        repository if objective.expected == "$repository" else objective.expected
                    )
                    passed = any(
                        lookup(item.get("data", {}), objective.key) == expected
                        for item in responses
                    )
                elif objective.kind == "artifact_png":
                    passed = any(
                        (artifacts or {}).get(name, b"").startswith(b"\x89PNG\r\n\x1a\n")
                        for item in responses
                        for name in item.get("artifacts", [])
                    )
        except (KeyError, ValueError, TypeError):
            passed = False
        evidence.append(
            Evidence(
                check=f"objective-{index + 1}:{objective.kind}",
                status="passed" if passed else "failed",
                detail="Recorded objective verified"
                if passed
                else "Recorded objective was not met",
            )
        )
    return evidence


class LiveRunner:
    """Call a dedicated Merlin deployment; every task has publishing permissions []."""

    def __init__(
        self,
        configuration: LiveConfiguration,
        *,
        timeout_seconds: float = 600,
        poll_seconds: float = 1,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.configuration = configuration
        self.timeout_seconds = timeout_seconds
        self.poll_seconds = poll_seconds
        self.api = httpx.Client(
            base_url=configuration.api_url.rstrip("/"),
            timeout=30,
            transport=transport,
            headers={"Authorization": f"Bearer {configuration.owner_token}"},
            follow_redirects=False,
        )
        self.github = httpx.Client(
            base_url="https://api.github.com",
            timeout=30,
            transport=transport,
            headers={
                "Authorization": f"Bearer {configuration.github_token}",
                "Accept": "application/vnd.github+json",
            },
            follow_redirects=False,
        )

    def close(self) -> None:
        self.api.close()
        self.github.close()

    @staticmethod
    def request(client: httpx.Client, method: str, path: str, **kwargs: Any) -> httpx.Response:
        response = client.request(method, path, **kwargs)
        if not 200 <= response.status_code < 300:
            # Do not include server response bodies, model output or credentials.
            raise ValueError(f"Evaluation service returned HTTP {response.status_code}")
        return response

    def baseline(self) -> tuple[str, dict[str, str]]:
        """Pin the fixture commit and independently hash every repository blob."""
        repository = self.configuration.repository
        info = self.request(self.github, "GET", f"/repos/{repository}").json()
        branch = quote(info["default_branch"], safe="")
        commit = self.request(self.github, "GET", f"/repos/{repository}/commits/{branch}").json()
        tree_sha = commit["commit"]["tree"]["sha"]
        tree = self.request(
            self.github,
            "GET",
            f"/repos/{repository}/git/trees/{tree_sha}",
            params={"recursive": "1"},
        ).json()
        if tree.get("truncated"):
            raise ValueError("Fixture repository tree is truncated")
        hashes: dict[str, str] = {}
        from merlin.sandbox._remote import excluded

        for item in tree["tree"]:
            if item["type"] == "tree":
                continue
            path = safe_relative(item["path"])
            if (
                item["type"] != "blob"
                or item.get("mode") not in {"100644", "100755"}
                or excluded(path)
            ):
                raise ValueError("Fixture repositories require regular, non-secret source files")
            hashes[path] = item["sha"]
        expected = fixture_files()
        if any(hashes.get(path) != git_blob_sha(value) for path, value in expected.items()):
            raise ValueError("Repository does not match the dedicated version-one fixture files")
        # The marker is versioned with the fixtures and verified through the immutable tree.
        marker = json.loads(expected[".merlin-eval-fixture.json"])
        if marker != {"version": 1, "dedicated": True}:
            raise ValueError("The dedicated evaluation fixture marker is invalid")
        return str(commit["sha"]), hashes

    def snapshot(self, task_id: str) -> dict[str, Any]:
        return dict(self.request(self.api, "GET", f"/v1/tasks/{task_id}").json())

    def checkpoint(self, task_id: str, base_sha: str) -> tuple[dict[str, bytes], str]:
        metadata = self.request(self.api, "GET", f"/v1/tasks/{task_id}/checkpoint").json()
        if not metadata:
            raise ValueError("No final checkpoint is available; correctness is inconclusive")
        if metadata.get("base_sha") != base_sha:
            raise ValueError("The workspace did not start at the pinned fixture commit")
        path = safe_relative(metadata["artifact"])
        content = self.request(
            self.api,
            "GET",
            f"/v1/tasks/{task_id}/artifacts/{quote(path, safe='/')}",
        ).content
        return checkpoint_files(content, metadata["sha256"]), str(metadata["sha256"])

    def run_scenario(
        self, scenario: Scenario, base_sha: str, baseline: dict[str, str]
    ) -> ScenarioResult:
        clock = time.perf_counter()
        task_id: str | None = None
        usage: dict[str, Any] | None = None
        evidence: list[Evidence] = []
        if not scenario.live_prompt:
            return ScenarioResult(
                scenario_id=scenario.id,
                category=scenario.category,
                status="inconclusive",
                duration_seconds=time.perf_counter() - clock,
                evidence=[
                    Evidence(
                        check="live-support",
                        status="inconclusive",
                        detail=scenario.live_unavailable_reason or "No live grader",
                    )
                ],
            )
        try:
            response = self.request(
                self.api,
                "POST",
                "/v1/tasks",
                json={
                    "prompt": scenario.live_prompt,
                    "repository": self.configuration.repository,
                    "session_id": "eval-" + uuid4().hex,
                    "permissions": [],
                    "allowed_domains": [],
                    "budget": {"max_calls": 40, "max_cost_usd": 2, "max_sandbox_seconds": 900},
                },
                headers={"Idempotency-Key": "eval-" + uuid4().hex},
            )
            task_id = str(response.json()["id"])
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", task_id):
                raise ValueError("The service returned an invalid task ID")
            deadline = time.monotonic() + self.timeout_seconds
            task = self.snapshot(task_id)
            while task.get("state") in {"queued", "running", "waiting_for_children"}:
                if time.monotonic() >= deadline:
                    self.request(self.api, "POST", f"/v1/tasks/{task_id}/cancel")
                    raise ValueError("Evaluation timed out; cancellation requested")
                time.sleep(self.poll_seconds)
                task = self.snapshot(task_id)
            if task.get("state") != "succeeded":
                evidence.append(
                    Evidence(
                        check="task-terminal-state",
                        status="failed",
                        detail="Task did not succeed; approvals are never automatically granted",
                    )
                )
                if task.get("state") == "waiting_for_approval":
                    self.request(self.api, "POST", f"/v1/tasks/{task_id}/cancel")
            else:
                usage = dict(self.request(self.api, "GET", f"/v1/tasks/{task_id}/usage").json())
                files, checksum = self.checkpoint(task_id, base_sha)
                evidence.append(
                    Evidence(check="checkpoint-sha256", status="passed", detail=checksum)
                )
                artifacts = {}
                for objective in scenario.objectives:
                    if objective.kind == "artifact_png":
                        for result in tool_results(task, objective.tool):
                            for name in result.get("artifacts", []):
                                path = safe_relative(name)
                                artifacts[path] = self.request(
                                    self.api,
                                    "GET",
                                    f"/v1/tasks/{task_id}/artifacts/{quote(path, safe='/')}",
                                ).content
                evidence.extend(
                    grade_objectives(
                        scenario,
                        task,
                        files,
                        baseline,
                        self.configuration.repository,
                        artifacts,
                    )
                )
        except (httpx.HTTPError, ValueError, KeyError, TypeError, tarfile.TarError):
            evidence.append(
                Evidence(
                    check="live-evidence",
                    status="inconclusive",
                    detail=(
                        "Independent service or checkpoint evidence unavailable or invalid; "
                        "no pass awarded"
                    ),
                )
            )
        return ScenarioResult(
            scenario_id=scenario.id,
            category=scenario.category,
            task_id=task_id,
            usage=usage,
            status=combined_status(evidence),
            evidence=evidence,
            duration_seconds=time.perf_counter() - clock,
        )

    def run(self, scenarios: Sequence[Scenario]) -> Report:
        started, clock = datetime.now(UTC).isoformat(), time.perf_counter()
        base_sha, baseline = self.baseline()
        results = [self.run_scenario(scenario, base_sha, baseline) for scenario in scenarios]
        return Report(
            mode="live",
            label=LABELS["live"],
            started_at=started,
            duration_seconds=time.perf_counter() - clock,
            results=results,
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--offline", action="store_true", help=LABELS["offline"])
    mode.add_argument("--live", action="store_true", help="Explicitly gated remote fixture trials")
    parser.add_argument(
        "--scenario", action="append", default=[], help="Select a versioned scenario ID"
    )
    parser.add_argument(
        "--output", type=Path, help="Also save JSON evidence outside source control"
    )
    parser.add_argument(
        "--timeout", type=float, default=600, help="Live timeout per task in seconds"
    )
    args = parser.parse_args(argv)
    try:
        if not 0 < args.timeout <= 3600:
            raise ValueError("Timeout must be between zero and 3600 seconds")
        scenarios = select_scenarios(load_catalogue(), args.scenario)
        if args.offline:
            report = run_offline(scenarios)
        else:
            configuration = LiveConfiguration.from_environment()
            runner = LiveRunner(configuration, timeout_seconds=args.timeout)
            try:
                report = runner.run(scenarios)
            finally:
                runner.close()
    except (OSError, ValueError, KeyError, TypeError, httpx.HTTPError):
        # Validation errors can contain input credentials: never serialize the exception.
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "status": "error",
                    "detail": "Evaluation configuration, fixture verification, or selection failed",
                    "hint": "See docs/evaluations.md for the required gates and fixture setup",
                }
            )
        )
        return 2
    document = json.dumps(report.document(), indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(document)
    print(document, end="")
    return report.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
