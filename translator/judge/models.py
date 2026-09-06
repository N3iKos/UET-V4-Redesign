"""
judge/models.py — What an evaluation produced.

Plain data, no logic beyond arithmetic on it. The shapes exist to keep the
runner, the parser and the screens speaking the same language:

  RowIssue     one flagged row: which criteria it failed, and the AI's comment
  JudgeResult  one section's parsed reply (or the fact that it was prose)
  JudgeSummary the score and the error-type distribution
  JudgeReport  every section merged into the one report the grid shows
  StagedFix    Fix All's output, held in memory until someone approves it

Nothing here is persisted: the report lives on screen and dies with the screen
(FR-114). The only thing that ever reaches disk is an approved fix, written to
the destination column by the runner.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# The criteria a user can ask for, and therefore the error types a reply may
# carry. `empty` and `failed` are not criteria — they are conditions the runner
# detects itself, before any model is asked (FR-105 / business rule 2).
CRITERIA: tuple[str, ...] = ("mistranslation", "context", "placeholder", "style", "fitting")
DEFAULT_CRITERIA: tuple[str, ...] = ("mistranslation", "context", "placeholder", "style")
ERROR_TYPES: tuple[str, ...] = CRITERIA + ("empty", "failed", "unjudged", "other")

CRITERION_LABELS: dict[str, str] = {
    "mistranslation": "Mistranslation — meaning drifted from the source",
    "context":        "Context — wrong for this row's context",
    "placeholder":    "Placeholder — a token was broken or dropped",
    "style":          "Style — does not match the requested tone",
    "fitting":        "Fitting — length & conciseness (dialog window bounds / natural length ratio)",
}

ERROR_TYPE_LABELS: dict[str, str] = {
    **{key: key for key in CRITERIA},
    "empty":     "empty",
    "failed":    "translate failed",
    "unjudged":  "unjudged",
    "other":     "other",
}

# How bad a flagged row is, graded on what a reader would experience rather than
# on distance from a hypothetical perfect translation. Ordered worst first, which
# is the order every list of them is shown in.
SEVERITIES: tuple[str, ...] = ("critical", "moderate", "minor")

SEVERITY_LABELS: dict[str, str] = {
    "critical": "Critical — a player would misunderstand the story or an instruction",
    "moderate": "Moderate — meaning survives but a nuance is wrong or lost",
    "minor":    "Minor — meaning intact; a style or register choice",
}

# The grading modes Settings offers. `none` is the original behaviour: a row is
# flagged or it is not. `severity` is the default, because a list of 300 flagged
# rows with no ranking is not a report — it is the same problem the evaluation
# was supposed to solve.
GRADING_MODES: tuple[str, ...] = ("severity", "none")


def normalise_severity(value) -> str:
    """
    One reported severity, mapped onto ours. Unknown or absent → `moderate`.

    Not `minor`: a model that failed to grade a row it chose to report has said
    the row is wrong, and defaulting that to the level that means "cosmetic"
    would quietly hide it from a critical-only repair run.
    """
    text = str(value or "").strip().lower()
    if text in SEVERITIES:
        return text
    for name in SEVERITIES:
        if name in text:
            return name
    aliases = {
        "high": "critical", "severe": "critical", "major": "critical",
        "blocker": "critical", "medium": "moderate", "med": "moderate",
        "low": "minor", "cosmetic": "minor", "nit": "minor",
    }
    return aliases.get(text, "moderate")


@dataclass
class RowIssue:
    """One flagged row. `error_types` is never empty — `other` is the floor."""

    row: int
    error_types: list[str] = field(default_factory=list)
    comment: str = ""
    # Only meaningful when the run graded severity; `""` in `none` mode, so a
    # report can be told apart from one whose model simply did not answer.
    severity: str = ""

    def __post_init__(self) -> None:
        if not self.error_types:
            self.error_types = ["other"]

    @property
    def label(self) -> str:
        types = ", ".join(self.error_types)
        return f"{self.severity} · {types}" if self.severity else types

    def matches(
        self,
        criteria: list[str] | None = None,
        severities: list[str] | None = None,
    ) -> bool:
        """
        Does this row fall inside both repair filters?

        Both are ANDed, and an empty filter means "no constraint" rather than
        "nothing" — a caller that passes neither gets every row, which is what
        an unfiltered Fix All should do.
        """
        if criteria and not set(self.error_types) & set(criteria):
            return False
        if severities and self.severity and self.severity not in severities:
            return False
        return True


@dataclass
class JudgeResult:
    """
    One section's reply, parsed.

    `unparsed` is a first-class outcome rather than an error: a model that
    reviewed the rows and answered in prose has said something worth reading,
    and scoring it zero would be a lie about the translation (the same stance
    playground/judging.py takes).

    `rows_seen` is every row number the reply *did* carry a verdict for. It is
    what lets a section that came back in prose on the first attempt — and was
    re-asked until a valid reply landed — count its recovered rows as evaluated
    while still reporting the ones that never got a verdict as unevaluated.
    """

    issues: dict[int, RowIssue] = field(default_factory=dict)
    rows_seen: set[int] = field(default_factory=set)
    unparsed: bool = False
    raw: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    def merge(self, other: "JudgeResult") -> "JudgeResult":
        self.issues.update(other.issues)
        self.rows_seen.update(other.rows_seen)
        self.unparsed = self.unparsed or other.unparsed
        return self


@dataclass
class JudgeSection:
    """
    One request's worth of rows, and how it went.

    A failed section does not sink the report: its rows are reported as
    unevaluated and everything else is still merged (FR-107, error handling).
    """

    index: int
    rows: list[int] = field(default_factory=list)
    estimated_tokens: int = 0
    tokens_used: int = 0
    error: str = ""
    unparsed: bool = False

    @property
    def ok(self) -> bool:
        return not self.error


@dataclass
class JudgeSummary:
    """
    Score = rows with no issue over rows evaluated (FR-113 / business rule 4).

    Rows in a section that failed are *not* counted as correct — they were
    never judged, and counting them would inflate the score with the one thing
    the user most needs to know about.
    """

    total: int = 0
    evaluated: int = 0
    problem_rows: int = 0
    unevaluated: int = 0
    distribution: dict[str, int] = field(default_factory=dict)
    # Rows per severity level. Empty in `none` mode, so a screen can tell a
    # run that did not grade apart from one where nothing was critical.
    severity_counts: dict[str, int] = field(default_factory=dict)
    estimated_tokens: int = 0
    tokens_used: int = 0

    @property
    def score(self) -> float:
        if not self.evaluated:
            return 0.0
        return (self.evaluated - self.problem_rows) / self.evaluated

    @property
    def percent(self) -> float:
        return self.score * 100.0

    def ranked_distribution(self) -> list[tuple[str, int]]:
        return sorted(self.distribution.items(), key=lambda kv: (-kv[1], kv[0]))

    def ranked_severities(self) -> list[tuple[str, int]]:
        """Worst first, and always in that order — not by count.

        A severity list sorted by frequency buries `critical` under `minor`
        whenever the file is mostly stylistic, which is the opposite of useful.
        """
        return [
            (name, self.severity_counts[name])
            for name in SEVERITIES
            if self.severity_counts.get(name)
        ]


@dataclass
class JudgeReport:
    """Everything the screens need: rows, issues, sections, summary."""

    rows: list[tuple[int, str, str]] = field(default_factory=list)  # row, src, dst
    issues: dict[int, RowIssue] = field(default_factory=dict)
    sections: list[JudgeSection] = field(default_factory=list)
    summary: JudgeSummary = field(default_factory=JudgeSummary)
    criteria: list[str] = field(default_factory=list)
    grading_mode: str = "severity"
    unparsed_sections: int = 0

    @property
    def problem_rows(self) -> list[int]:
        return sorted(self.issues)

    @property
    def graded(self) -> bool:
        return self.grading_mode == "severity"

    @property
    def failed_sections(self) -> list[JudgeSection]:
        return [section for section in self.sections if not section.ok]

    def source_for(self, row: int) -> str:
        return next((src for number, src, _dst in self.rows if number == row), "")

    def translation_for(self, row: int) -> str:
        return next((dst for number, _src, dst in self.rows if number == row), "")

    def comment_for(self, row: int) -> str:
        issue = self.issues.get(row)
        return issue.comment if issue else ""

    def severity_for(self, row: int) -> str:
        issue = self.issues.get(row)
        return issue.severity if issue else ""

    def rows_matching(
        self,
        criteria: list[str] | None = None,
        severities: list[str] | None = None,
    ) -> list[int]:
        """Flagged rows passing both repair filters, in sheet order."""
        return [
            row for row in sorted(self.issues)
            if self.issues[row].matches(criteria, severities)
        ]

    def drop(self, row: int) -> None:
        """A row that passed a re-judge is no longer a problem (FR-112)."""
        issue = self.issues.pop(row, None)
        if issue is None:
            return
        for error_type in issue.error_types:
            if error_type in self.summary.distribution:
                self.summary.distribution[error_type] -= 1
                if self.summary.distribution[error_type] <= 0:
                    del self.summary.distribution[error_type]
        if issue.severity and issue.severity in self.summary.severity_counts:
            self.summary.severity_counts[issue.severity] -= 1
            if self.summary.severity_counts[issue.severity] <= 0:
                del self.summary.severity_counts[issue.severity]
        self.summary.problem_rows = len(self.issues)

    def restore(self, issue: RowIssue) -> None:
        """
        Put a dropped issue back, counts and all — the exact inverse of `drop`.

        Reverting an applied fix has to undo the bookkeeping as well as the text.
        A row whose repair was rolled back is flagged again, because the problem
        the evaluation found is back in the file; leaving it out of the counts
        would make the score describe a version of the file that no longer
        exists.
        """
        if issue.row in self.issues:
            return
        self.issues[issue.row] = issue
        for error_type in issue.error_types:
            self.summary.distribution[error_type] = (
                self.summary.distribution.get(error_type, 0) + 1
            )
        if issue.severity:
            self.summary.severity_counts[issue.severity] = (
                self.summary.severity_counts.get(issue.severity, 0) + 1
            )
        self.summary.problem_rows = len(self.issues)

    def replace_translation(self, row: int, text: str) -> None:
        """Reflect an applied fix in the report the grid is showing."""
        for index, (number, src, _dst) in enumerate(self.rows):
            if number == row:
                self.rows[index] = (number, src, text)
                return


@dataclass
class StagedFix:
    """
    Fix All's result, in memory only (FR-111 / AC-103).

    `texts` is what the model proposes, keyed by row. Nothing here has touched
    the file: the runner's `apply_fix` is the only writer, and it only writes
    rows that were explicitly approved.

    `unchanged` is the rows the model deliberately returned as-is because
    fixing them would have changed what the source means. They are a *result*,
    not a failure: a row left alone is the correct outcome when the alternative
    is a confident wrong answer, and it must never be presented as "repaired".
    """

    texts: dict[int, str] = field(default_factory=dict)
    unchanged: list[int] = field(default_factory=list)
    failed: list[int] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    tokens_used: int = 0

    @property
    def rows(self) -> list[int]:
        return sorted(self.texts)

    def __bool__(self) -> bool:
        return bool(self.texts)


@dataclass
class SingleFix:
    """One row's repair (Fix It)."""

    row: int
    text: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.text) and not self.error


@dataclass
class RejudgeVerdict:
    """One row, re-checked (Judge Again)."""

    row: int
    passed: bool = False
    issue: RowIssue | None = None
    unparsed: bool = False
    raw: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error
