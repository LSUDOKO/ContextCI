"""ContextCI entrypoint — orchestrates the four phases.

    1. Parse the PR diff for schema changes          (diff_parser)
    2. Pull lineage + governance context from DataHub (datahub_mcp_client)
    3. Analyze blast radius and generate migrations   (blast_analyzer, code_generator)
    4. Report on GitHub and write tags back to DataHub (github_reporter, datahub_mcp_client)

Exits 1 when the verdict is "block", which fails the GitHub Action and stops the
merge. Any other failure — DataHub down, no API key, a GitHub hiccup — degrades
to a warning rather than breaking the build.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import List, Optional

from .blast_analyzer import analyze
from .datahub_mcp_client import DataHubMCPClient
from .diff_parser import parse_pull_request
from .github_reporter import GitHubReporter, render_comment
from .models import ChangeVerdict, RecommendedAction, RiskLevel, RunResult, SchemaChange

logger = logging.getLogger("contextci")

REPORT_PATH = "contextci-report.json"


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _pr_url(repo: str, pr_number: int) -> str:
    return f"https://github.com/{repo}/pull/{pr_number}"


def run(repo_full_name: str, pr_number: int) -> RunResult:
    """Phases 1-3 plus the DataHub write-back. Returns the aggregate verdict."""
    platform = os.getenv("DATAHUB_PLATFORM", "postgres")
    env = os.getenv("DATAHUB_ENV", "PROD")

    changes: List[SchemaChange] = parse_pull_request(repo_full_name, pr_number)
    logger.info("phase 1: %d schema change(s) detected", len(changes))

    result = RunResult()
    if not changes:
        return result

    client = DataHubMCPClient()
    if not client.available:
        result.degraded = True
        result.degraded_reason = client.last_error or "DataHub connection unavailable"

    try:
        for change in changes:
            context = client.build_lineage_context(change, platform=platform, env=env)
            logger.info(
                "phase 2: %s -> %s (%d downstream)",
                change.identity,
                context.dataset_urn or "unresolved",
                len(context.downstream),
            )

            report = analyze(change, context)
            logger.info(
                "phase 3: %s risk=%s action=%s",
                change.identity,
                report.risk_level.value,
                report.recommended_action.value,
            )
            result.verdicts.append(ChangeVerdict(change=change, context=context, report=report))

        _write_back(client, result, repo_full_name, pr_number)
    finally:
        client.close()

    return result


def _write_back(
    client: DataHubMCPClient, result: RunResult, repo_full_name: str, pr_number: int
) -> None:
    """Phase 4b — mutate the DataHub graph so the catalog records the pending change."""
    if not client.available:
        logger.info("phase 4b: skipped, DataHub unavailable")
        return

    pr_url = _pr_url(repo_full_name, pr_number)
    for verdict in result.verdicts:
        if not verdict.report.is_breaking or not verdict.context.dataset_urn:
            continue
        target = (
            f"column `{verdict.change.column}`"
            if verdict.change.column
            else f"table `{verdict.change.table}`"
        )
        note = (
            f"Pending PR #{pr_number}: {verdict.change.change_type.value.replace('_', ' ')} on "
            f"{target}. Blast radius: {len(verdict.report.affected_assets)} downstream asset(s), "
            f"risk {verdict.report.risk_level.value}. Flagged by ContextCI."
        )
        downstream_urns = [
            a.urn for a in verdict.report.affected_assets if a.risk is not RiskLevel.LOW
        ]
        outcome = client.write_back(
            source_urn=verdict.context.dataset_urn,
            downstream_urns=downstream_urns,
            risk_level=verdict.report.risk_level,
            note=note,
            pr_url=pr_url,
            column_name=verdict.change.column or "",
            requires_security_review=verdict.report.governance_gate.requires_security_review,
        )
        logger.info(
            "phase 4b: %d/%d DataHub mutations applied for %s",
            sum(1 for ok in outcome.values() if ok),
            len(outcome),
            verdict.change.identity,
        )


def report_to_github(result: RunResult, repo_full_name: str, pr_number: int) -> None:
    """Phase 4a — post the comment and, when enabled, push the generated fixes."""
    reporter = GitHubReporter(repo_full_name, pr_number)
    try:
        body = render_comment(result, datahub_url=os.getenv("DATAHUB_FRONTEND_URL"))
        url = reporter.post_or_update_comment(body, create_if_missing=bool(result.verdicts))
        logger.info("phase 4a: comment %s", url or "skipped (no schema changes)")

        if _env_flag("CONTEXTCI_AUTOFIX") and result.overall_risk in (
            RiskLevel.MEDIUM,
            RiskLevel.HIGH,
            RiskLevel.CRITICAL,
        ):
            fixes = [f for v in result.verdicts for f in v.report.generated_fixes]
            written = reporter.commit_fixes(fixes, pr_number)
            logger.info("phase 4a: committed %d fix file(s)", len(written))
    finally:
        reporter.close()


def _save_report(result: RunResult) -> None:
    with open(REPORT_PATH, "w", encoding="utf-8") as handle:
        json.dump(result.model_dump(mode="json"), handle, indent=2)
    logger.info("wrote %s", REPORT_PATH)


def main(argv: Optional[List[str]] = None) -> int:
    logging.basicConfig(
        level=os.getenv("CONTEXTCI_LOG_LEVEL", "INFO"),
        format="%(levelname)s %(name)s: %(message)s",
    )

    repo_full_name = os.getenv("GITHUB_REPOSITORY")
    pr_raw = os.getenv("PR_NUMBER")
    if not repo_full_name or not pr_raw:
        logger.error(
            "GITHUB_REPOSITORY and PR_NUMBER must be set "
            "(the bundled workflow sets both from the pull_request event)"
        )
        return 2

    pr_number = int(pr_raw)
    result = run(repo_full_name, pr_number)
    _save_report(result)

    try:
        report_to_github(result, repo_full_name, pr_number)
    except Exception as exc:  # noqa: BLE001 - a reporting failure must not mask the verdict
        logger.error("could not report to GitHub: %s", exc)

    action = result.overall_action
    print(f"ContextCI verdict: {action.value} (risk: {result.overall_risk.value})")
    if action is RecommendedAction.BLOCK:
        print("Blocking merge: this change breaks downstream assets. See the PR comment.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
