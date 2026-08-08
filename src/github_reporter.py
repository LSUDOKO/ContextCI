"""Phase 4a — report the verdict on the pull request.

Posts a single sticky comment (edited in place on re-runs, so synchronising a PR
never spams it), optionally requests review from the downstream data owners, and
optionally commits the generated migrations to the PR branch.
"""

from __future__ import annotations

import logging
import os
import re
from typing import List, Optional

from .models import (
    ChangeVerdict,
    GeneratedFix,
    Owner,
    RecommendedAction,
    RiskLevel,
    RunResult,
)

logger = logging.getLogger(__name__)

# Marks our comment so re-runs edit it instead of adding another one.
COMMENT_MARKER = "<!-- contextci-blast-report -->"

RISK_BADGE = {
    RiskLevel.LOW: "🟢 Low",
    RiskLevel.MEDIUM: "🟡 Medium",
    RiskLevel.HIGH: "🟠 High",
    RiskLevel.CRITICAL: "🔴 Critical",
}

ACTION_HEADLINE = {
    RecommendedAction.BLOCK: "🔴 **Blocked** — this change breaks downstream assets.",
    RecommendedAction.WARN: "🟡 **Warning** — this change may break downstream assets.",
    RecommendedAction.APPROVE: "🟢 **Safe** — no downstream breakage detected.",
}

_GITHUB_HANDLE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")


def _mentions_enabled() -> bool:
    """@-mentioning is opt-in: DataHub owner names are not always GitHub handles,
    and a wrong guess pings an unrelated person on every PR."""
    return os.getenv("CONTEXTCI_MENTION_OWNERS", "false").lower() in ("1", "true", "yes")


def _render_owner(owner: Owner) -> str:
    handle = owner.github_handle
    if _mentions_enabled() and handle and _GITHUB_HANDLE.match(handle):
        return f"@{handle}"
    return f"**{owner.name}**"


def render_comment(result: RunResult, datahub_url: Optional[str] = None) -> str:
    """Build the full Markdown body of the PR comment."""
    lines = [COMMENT_MARKER, "## ContextCI — Schema Change Blast Radius", ""]

    if result.degraded:
        lines += [
            "> ⚠️ **Degraded run.** DataHub was unreachable, so lineage could not be verified. "
            f"The findings below are based on the diff alone.\n> `{result.degraded_reason}`",
            "",
        ]

    if not result.verdicts:
        lines += ["No schema changes detected in this pull request. ✅", ""]
        return "\n".join(lines)

    lines += [
        ACTION_HEADLINE[result.overall_action],
        "",
        f"Overall risk: {RISK_BADGE[result.overall_risk]} · "
        f"{len(result.verdicts)} schema change(s) analyzed",
        "",
    ]

    gated = [v for v in result.verdicts if v.report.governance_gate.requires_security_review]
    if gated:
        lines += [
            "> 🛡️ **Security review required.** This pull request touches regulated or Tier-1 data. "
            "It stays blocked until a data governance owner signs off, independent of the blast radius.",
            "",
        ]
        for verdict in gated:
            for reason in verdict.report.governance_gate.reasons:
                lines.append(f"> - {reason}")
        lines.append("")

    lines += _render_summary_table(result)
    lines.append("")

    for verdict in result.verdicts:
        lines += _render_verdict(verdict, datahub_url)

    owners = _collect_owners(result)
    if owners:
        lines += [
            "### Downstream owners",
            "Owners of the affected assets, from DataHub:",
            "",
            *(f"- {_render_owner(o)}" for o in owners),
            "",
        ]
        if not _mentions_enabled():
            lines.append(
                "_Set the `CONTEXTCI_MENTION_OWNERS` variable to `true` to @-mention them "
                "once you have confirmed the DataHub owner names match GitHub handles._"
            )
            lines.append("")

    lines += [
        "---",
        "<sub>Posted by [ContextCI](https://github.com/LSUDOKO/ContextCI) — "
        "context-aware CI, zero breaking changes. Affected datasets have been tagged in DataHub.</sub>",
    ]
    return "\n".join(lines)


def _render_summary_table(result: RunResult) -> List[str]:
    rows = [
        "| Change | Table | Column | Risk | Downstream | Verdict |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for v in result.verdicts:
        breaking = [a for a in v.report.affected_assets if a.risk is not RiskLevel.LOW]
        rows.append(
            f"| {v.change.change_type.value.replace('_', ' ')} "
            f"| `{v.change.table}` "
            f"| `{v.change.column or '—'}` "
            f"| {RISK_BADGE[v.report.risk_level]} "
            f"| {len(breaking) or len(v.report.affected_assets)} "
            f"| {v.report.recommended_action.value} |"
        )
    return rows


def _render_verdict(verdict: ChangeVerdict, datahub_url: Optional[str]) -> List[str]:
    change, report, context = verdict.change, verdict.report, verdict.context
    target = f"`{change.table}.{change.column}`" if change.column else f"`{change.table}`"
    lines = [
        f"### {RISK_BADGE[report.risk_level]} — {change.change_type.value.replace('_', ' ')} on {target}",
        "",
        report.summary,
        "",
        f"<sub>Found in `{change.source_file}`"
        + (f":{change.source_line}" if change.source_line else "")
        + "</sub>",
        "",
    ]

    if report.affected_assets:
        lines += [
            "<details><summary>"
            f"Blast radius — {len(report.affected_assets)} downstream asset(s)</summary>",
            "",
            "| Asset | Type | Column-level | Owners | Terms |",
            "| --- | --- | --- | --- | --- |",
        ]
        for asset in report.affected_assets:
            owners = ", ".join(o.name for o in asset.owners) or "—"
            terms = ", ".join(asset.glossary_terms) or "—"
            name = asset.name
            if datahub_url:
                name = f"[{asset.name}]({datahub_url.rstrip('/')}/dataset/{asset.urn})"
            lines.append(
                f"| {name} | {asset.type} "
                f"| {'✅ confirmed' if asset.column_level_confirmed else '⚠️ table-level only'} "
                f"| {owners} | {terms} |"
            )
        lines += ["", "</details>", ""]

    if report.generated_fixes:
        lines += [
            f"<details><summary>Suggested migrations ({len(report.generated_fixes)})</summary>",
            "",
        ]
        for fix in report.generated_fixes:
            lines += [
                f"**`{fix.file_path}`** — {fix.description}",
                "",
                f"```{fix.language}",
                fix.code.rstrip(),
                "```",
                "",
            ]
        lines += ["</details>", ""]

    if report.reasoning:
        lines += ["<details><summary>Reasoning</summary>", "", report.reasoning, "", "</details>", ""]

    if context.errors:
        lines += ["<details><summary>Notes</summary>", ""]
        lines += [f"- {e}" for e in context.errors]
        lines += ["", "</details>", ""]

    return lines


def _collect_owners(result: RunResult) -> List[Owner]:
    seen = {}
    for verdict in result.verdicts:
        for asset in verdict.report.affected_assets:
            if asset.risk is RiskLevel.LOW:
                continue
            for owner in asset.owners:
                seen.setdefault(owner.urn, owner)
    return list(seen.values())


class GitHubReporter:
    """Wraps the PyGithub calls ContextCI needs."""

    def __init__(self, repo_full_name: str, pr_number: int, token: Optional[str] = None):
        from github import Auth, Github

        token = token or os.getenv("GITHUB_TOKEN")
        if not token:
            raise RuntimeError("GITHUB_TOKEN is required to comment on the pull request")
        self._client = Github(auth=Auth.Token(token))
        self.repo = self._client.get_repo(repo_full_name)
        self.pull = self.repo.get_pull(pr_number)

    def post_or_update_comment(self, body: str, create_if_missing: bool = True) -> Optional[str]:
        """Post the report, editing our previous comment if one exists.

        ``create_if_missing=False`` updates an existing report but stays silent
        on PRs that never had one — used for the "no schema changes" case so
        ContextCI doesn't comment on every unrelated pull request.
        """
        for comment in self.pull.get_issue_comments():
            if COMMENT_MARKER in (comment.body or ""):
                comment.edit(body)
                logger.info("updated existing ContextCI comment %s", comment.id)
                return comment.html_url
        if not create_if_missing:
            return None
        comment = self.pull.create_issue_comment(body)
        logger.info("posted ContextCI comment %s", comment.id)
        return comment.html_url

    def commit_fixes(self, fixes: List[GeneratedFix], pr_number: int) -> List[str]:
        """Commit generated migrations to the PR branch. Idempotent by content."""
        branch = self.pull.head.ref
        written: List[str] = []
        for fix in fixes:
            message = f"fix(contextci): {fix.description[:60]} (PR #{pr_number})"
            try:
                existing = self.repo.get_contents(fix.file_path, ref=branch)
                if existing.decoded_content.decode("utf-8") == fix.code:
                    logger.info("%s already up to date", fix.file_path)
                    continue
                self.repo.update_file(fix.file_path, message, fix.code, existing.sha, branch=branch)
            except Exception as exc:  # noqa: BLE001 - missing file is the common case
                if "404" not in str(exc) and "Not Found" not in str(exc):
                    logger.warning("could not write %s: %s", fix.file_path, exc)
                    continue
                self.repo.create_file(fix.file_path, message, fix.code, branch=branch)
            written.append(fix.file_path)
            logger.info("committed %s to %s", fix.file_path, branch)
        return written

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:  # noqa: BLE001
            pass
