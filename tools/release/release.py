"""Release tooling: PR title rules, CHANGELOG.md, and each release's version and notes from its PRs' titles."""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path

TITLE_RE = re.compile(r"^(?P<type>[a-z]+)(?:\([^)]+\))?!?: \S", re.IGNORECASE)
ALLOWED_TYPES = ("feat", "fix", "perf", "refactor", "docs", "test", "ci", "build", "chore", "revert")
# Our tracker's keys only: any word-number pair would refuse `pre-1.0`, `Python-3` or `HTTP-429`.
TICKET_KEYS = ("AIR",)
TICKET_RE = re.compile(rf"\b(?:{'|'.join(TICKET_KEYS)})-\d+\b", re.IGNORECASE)
BREAKING_RE = re.compile(r"^[a-z]+(?:\([^)]+\))?!: ", re.IGNORECASE)
PREFIX_RE = re.compile(r"^[a-z]+(?:\([^)]*\))?!?: ", re.IGNORECASE)

TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
LINE_RE = re.compile(r"^(\d+)\.(\d+)$")
SUPPORT_BRANCH_RE = re.compile(r"^support/v(\d+\.\d+)$")
# A merge commit names its PR first, a squash merge last.
PR_SUBJECT_RE = re.compile(r"^Merge pull request #(\d+) |\(#(\d+)\)$")
# Set by hand only: the autolabeler never removes a label, so a retitled PR would keep a stale one.
EXCLUDED_LABELS = frozenset({"internal"})
# A code span shows a backslash literally, so it is kept whole and only the text around it escaped.
TITLE_ESCAPE_RE = re.compile(r"(`+)(?!`).*?(?<!`)\1(?!`)|[\\<*_&]")
# The sections of a main-line release, in order, and the bump each asks for.
SECTIONS = (("Breaking changes", "major"), ("Features", "minor"), ("Fixes", "patch"))
# The version a build reports: pyproject.toml states it, uv.lock records it; only a release writes either line.
PYPROJECT = Path("pyproject.toml")
LOCK = Path("uv.lock")
STATIC_VERSION_RE = re.compile(r'^version = "([^"]*)"$', re.MULTILINE)
PROJECT_NAME_RE = re.compile(r'^name = "([^"]*)"$', re.MULTILINE)
CHANGELOG = Path("CHANGELOG.md")
RELEASE_COMMIT_RE = re.compile(r"^chore\(release\): v(\d+\.\d+\.\d+)$")
PR_FIELDS = "number title merged baseRefName headRefName labels(first: 100) { nodes { name } }"
PRS_QUERY = "query($owner: String!, $name: String!) { repository(owner: $owner, name: $name) { %s } }"
PRS_PER_QUERY = 50

Version = tuple[int, int, int]
Line = tuple[int, int]


class ReleaseError(Exception):
    """A release that must not happen; the message says why."""


def _raises_minor(title: str) -> bool:
    match = TITLE_RE.match(title)
    return match is not None and (match["type"].lower() == "feat" or BREAKING_RE.match(title) is not None)


def title_errors(title: str, denylist: Iterable[str], base: str = "", head: str = "") -> list[str]:
    # A Release PR lists the PRs it brings and never itself, so its title is free.
    if base == "release" and head == "main":
        return []
    errors = []
    match = TITLE_RE.match(title)
    if match is None:
        errors.append("start with a type and a colon, e.g. 'feat: …' or 'fix(api): …'")
    elif match["type"].lower() not in ALLOWED_TYPES:
        errors.append(f"type {match['type']!r} is not one of {', '.join(ALLOWED_TYPES)}")
    if not title.isascii():
        found = "".join(dict.fromkeys(c for c in title if not c.isascii()))
        errors.append(f"ASCII only: replace {found!r}, the changelog is kept plain ASCII")
    if ticket := TICKET_RE.search(title):
        errors.append(f"no ticket numbers: {ticket[0]!r} is one, and the title is a client-facing changelog line")
    lowered = title.lower()
    for term in denylist:
        # Lookarounds, not \b, which never matches beside a name that starts or ends in punctuation.
        if re.search(rf"(?<!\w){re.escape(term.lower())}(?!\w)", lowered):
            errors.append(f"names {term!r}, which must never reach a client-facing changelog")
    if base.startswith("support/") and _raises_minor(title):
        errors.append(f"{base} ships patches only: use 'fix: …', never a feature or a breaking change")
    return errors


def prepend_changelog(path: Path, version: str, notes: str, today: datetime.date) -> None:
    header = "# Changelog\n"
    existing = path.read_text() if path.exists() else header
    body = existing.removeprefix(header).lstrip("\n")
    section = f"## v{version} ({today.isoformat()})\n\n{notes}\n"
    path.write_text(f"{header}\n{section}" + (f"\n{body}" if body else ""))


def changelog_section(path: Path, version: str, text: str | None = None) -> str:
    """The notes CHANGELOG.md (or TEXT, its content) holds for VERSION, as ``prepend_changelog`` wrote them."""
    text = path.read_text() if text is None else text
    match = re.search(rf"^## v{re.escape(version)} \([^)]*\)\n\n(.*?)(?=^## v|\Z)", text, re.M | re.S)
    if match is None:
        raise ReleaseError(f"{path} has no v{version} section")
    return match[1].strip()


def _versions(tags: Iterable[str]) -> list[Version]:
    return [(int(m[1]), int(m[2]), int(m[3])) for m in map(TAG_RE.match, tags) if m]


def parse_line(text: str) -> Line:
    match = LINE_RE.match(text)
    if match is None:
        raise ReleaseError(f"{text!r} is not a line: give MAJOR.MINOR, e.g. 0.2")
    return int(match[1]), int(match[2])


def support_line(branch: str) -> Line:
    match = SUPPORT_BRANCH_RE.match(branch)
    if match is None:
        raise ReleaseError(f"{branch!r} is not a support line: those are named support/vMAJOR.MINOR")
    return parse_line(match[1])


def support_base(line: Line, tags: Iterable[str]) -> str:
    """The line's highest tag: where its support branch starts, and what its next patch counts from."""
    # The newest line is patched through release; a second way to tag it would reuse versions.
    versions = _versions(tags)
    patches = [v for v in versions if v[:2] == line]
    name = ".".join(map(str, line))
    if not patches:
        raise ReleaseError(f"no v{name}.* tag: there is no {name} line to support")
    if max(versions)[:2] == line:
        raise ReleaseError(f"{name} is the newest line: patch it with a hotfix/* PR into release")
    return "v" + ".".join(map(str, max(patches)))


def next_patch(tag: str) -> str:
    major, minor, patch = _versions([tag])[0]
    return f"{major}.{minor}.{patch + 1}"


def _entry(title: str, number: int) -> str:
    text = PREFIX_RE.sub("", title, count=1)
    escaped = TITLE_ESCAPE_RE.sub(lambda m: m[0] if m[1] else f"\\{m[0]}", text)
    return f"- {escaped} (#{number})"


def changelog_line(title: str, number: int, labels: Iterable[str] = ()) -> str | None:
    """A support PR's changelog line, written as main's are; None if it ships nothing."""
    match = TITLE_RE.match(title)
    if match is None or match["type"].lower() != "fix" or EXCLUDED_LABELS.intersection(labels):
        return None
    return _entry(title, number)


def _section(title: str, labels: Iterable[str]) -> str | None:
    match = TITLE_RE.match(title)
    if match is None or EXCLUDED_LABELS.intersection(labels):
        return None
    if BREAKING_RE.match(title):
        return "Breaking changes"
    return {"feat": "Features", "fix": "Fixes"}.get(match["type"].lower())


def _denylist() -> list[str]:
    return [t.strip() for t in os.environ.get("CHANGELOG_DENYLIST", "").split(",") if t.strip()]


def _listed_title_errors(pr: dict, denylist: Iterable[str]) -> list[str]:
    # The PR title check never saw a title edited after merging, or one merged before a rule existed.
    errors = title_errors(pr["title"], denylist, pr["baseRefName"], pr["headRefName"])
    return [f"#{pr['number']} {pr['title']!r}: {error}" for error in errors]


def _refuse_bad_titles(errors: list[str]) -> None:
    if errors:
        raise ReleaseError("edit these PR titles, then re-run: " + "; ".join(errors))


def ships_on_main_line(pr: dict) -> bool:
    """A PR into main, or a hotfix into release. The Release PR is left out: its PRs are listed themselves."""
    return pr["baseRefName"] == "main" or (pr["baseRefName"] == "release" and pr["headRefName"].startswith("hotfix/"))


def release_notes(prs: Iterable[dict], denylist: Iterable[str] = ()) -> tuple[str, str]:
    """The bump (major, minor, patch, or "" if nothing ships) and the notes of a main-line release."""
    sections: dict[str, list[str]] = {name: [] for name, _ in SECTIONS}
    errors: list[str] = []
    for pr in prs:
        # A PR into any other branch reached main through a PR into main, which is listed instead.
        if not ships_on_main_line(pr):
            continue
        section = _section(pr["title"], (label["name"] for label in pr["labels"]))
        if section:
            errors += _listed_title_errors(pr, denylist)
            sections[section].append(_entry(pr["title"], pr["number"]))
    _refuse_bad_titles(errors)
    bump = next((bump for name, bump in SECTIONS if sections[name]), "")
    notes = "\n\n".join(f"### {name}\n\n" + "\n".join(lines) for name, lines in sections.items() if lines)
    return bump, notes


def next_version(tag: str, bump: str) -> str:
    major, minor, patch = _versions([tag])[0]
    # Before 1.0 a breaking change bumps the minor.
    if bump == "major" and major > 0:
        return f"{major + 1}.0.0"
    if bump in ("major", "minor"):
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def last_release(tags: Iterable[str]) -> str:
    versions = _versions(tags)
    if not versions:
        raise ReleaseError("no vX.Y.Z tag in this branch's history: publish the baseline release first")
    return "v" + ".".join(map(str, max(versions)))


def pr_numbers(subjects: Iterable[str]) -> list[int]:
    """The PRs these commits merged, in the order given, each once."""
    numbers: list[int] = []
    for subject in subjects:
        match = PR_SUBJECT_RE.search(subject)
        if match and (number := int(match[1] or match[2])) not in numbers:
            numbers.append(number)
    return numbers


def support_notes(prs: Iterable[dict], branch: str, denylist: Iterable[str] = ()) -> str:
    """The notes of a support release: one line per fix: PR merged into BRANCH; empty if none."""
    lines = []
    errors: list[str] = []
    for pr in prs:
        if pr["baseRefName"] != branch:
            # Only a rebase merge or a direct push puts another branch's PR on the first-parent path.
            print(
                f"::warning::#{pr['number']} merged into {pr['baseRefName']}, not {branch}: left out", file=sys.stderr
            )
            continue
        if _raises_minor(pr["title"]):
            raise ReleaseError(f"#{pr['number']} {pr['title']!r} is a feature or breaking: {branch} ships patches only")
        line = changelog_line(pr["title"], pr["number"], (label["name"] for label in pr["labels"]))
        if line:
            errors += _listed_title_errors(pr, denylist)
            lines.append(line)
    _refuse_bad_titles(errors)
    return "### Fixes\n\n" + "\n".join(lines) if lines else ""


def support_preview(branch: str, title: str, number: int, tags: Iterable[str], labels: Iterable[str] = ()) -> str:
    base = support_base(support_line(branch), tags)
    labels = list(labels)
    line = changelog_line(title, number, labels)
    if line is None:
        excluded = EXCLUDED_LABELS.intersection(labels)
        reason = f"it is labeled `{min(excluded)}`" if excluded else "only a `fix:` PR releases on a support line"
        return f"Merging this publishes nothing: {reason}. {branch} stays at {base}.\n"
    return (
        f"## Merging this publishes v{next_patch(base)}\n\n"
        f"The next patch on {branch}, after {base}; it does not reach `main`. Changelog line:\n\n"
        f"### Fixes\n\n{line}\n"
    )


def _bumped(path: Path, pattern: re.Pattern[str], replacement: str) -> str:
    text, count = pattern.subn(replacement, path.read_text())
    if count != 1:
        raise ReleaseError(f"{path} has no single version line to bump")
    return text


def _locked_version_re(pyproject: str) -> re.Pattern[str]:
    # The name pyproject.toml states, not a constant: a line cut before a rename locks the old one.
    match = PROJECT_NAME_RE.search(pyproject)
    name = re.sub(r"[-_.]+", "-", match[1]).lower() if match else ""
    return re.compile(rf'^(name = "{re.escape(name)}"\nversion = )"([^"]*)"$', re.MULTILINE)


def set_version(pyproject: Path, lock: Path, version: str) -> None:
    """Write VERSION into pyproject.toml and uv.lock, so every build of the release commit reports it."""
    texts = {
        pyproject: _bumped(pyproject, STATIC_VERSION_RE, f'version = "{version}"'),
        lock: _bumped(lock, _locked_version_re(pyproject.read_text()), rf'\g<1>"{version}"'),
    }
    for path, text in texts.items():
        path.write_text(text)


def stated_versions(pyproject: str, lock: str) -> dict[str, str | None]:
    """The version each file states, ``None`` where it states none."""
    static = STATIC_VERSION_RE.search(pyproject)
    locked = _locked_version_re(pyproject).search(lock)
    return {str(PYPROJECT): static and static[1], str(LOCK): locked and locked[2]}


def version_change_errors(
    before: dict[str, str | None], after: dict[str, str | None], released: str | None = None
) -> list[str]:
    """Why a PR's merge commit states a version the release workflows did not write; empty if none."""
    errors = []
    stated, locked = after[str(PYPROJECT)], after[str(LOCK)]
    if stated is not None and locked != stated:
        errors.append(f"{LOCK} records version {locked!r} where {PYPROJECT} states {stated!r}")
    # A merge may bring in a release the base lacks (a back-merge, a Release PR): its newest tag's version.
    if stated != before[str(PYPROJECT)] and stated != released:
        errors.append(f"{PYPROJECT} changes the version from {before[str(PYPROJECT)]!r} to {stated!r}")
    return errors


def _run(*args: str) -> str:
    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode != 0:
        raise ReleaseError(f"{' '.join(args[:3])} failed: {result.stderr.strip()}")
    return result.stdout


def _tags() -> list[str]:
    return _run("git", "tag", "--list", "v*").split()


def _merged_tags() -> list[str]:
    return _run("git", "tag", "--merged", "HEAD", "--list", "v*").split()


def _graphql(query: str) -> dict:
    # Not _run: a number naming no issue or PR is an error beside the data, and the data is still good.
    result = subprocess.run(
        ["gh", "api", "graphql", "-F", "owner={owner}", "-F", "name={repo}", "-f", f"query={query}"],
        capture_output=True,
        text=True,
    )
    try:
        return json.loads(result.stdout)["data"]["repository"]
    except (ValueError, KeyError, TypeError):
        raise ReleaseError(f"gh api graphql failed: {result.stderr.strip()}") from None


def merged_prs(numbers: list[int]) -> list[dict]:
    """The merged PRs among NUMBERS, in order: a subject's ``(#N)`` may name an issue or an unmerged PR."""
    prs = []
    for start in range(0, len(numbers), PRS_PER_QUERY):
        chunk = numbers[start : start + PRS_PER_QUERY]
        fields = " ".join(
            f"n{n}: issueOrPullRequest(number: {n}) {{ ... on PullRequest {{ {PR_FIELDS} }} }}" for n in chunk
        )
        found = _graphql(PRS_QUERY % fields)
        for number in chunk:
            pr = found.get(f"n{number}") or {}
            if pr.get("merged"):
                prs.append({**pr, "labels": pr["labels"]["nodes"]})
    return prs


def _untagged_releases(base: str) -> list[tuple[str, str]]:
    """(commit, version) of each release commit after BASE, oldest first: a run that died before its tag."""
    log = _run("git", "log", "--first-parent", "--reverse", "--format=%H %s", f"{base}..HEAD")
    lines = (line.split(" ", 1) for line in log.splitlines())
    return [(commit, match[1]) for commit, subject in lines if (match := RELEASE_COMMIT_RE.match(subject))]


def _counted_from(base: str) -> tuple[str, str]:
    """The release the next one counts from, and the commit its PRs are listed after; publish tags it first."""
    untagged = _untagged_releases(base)
    if not untagged:
        return base, base
    commit, version = untagged[-1]
    return f"v{version}", commit


def plan_release(notes_file: Path, pending: dict | None = None) -> str:
    """The version release publishes, with PENDING (a hotfix PR not merged yet) counted as merged."""
    base, since = _counted_from(last_release(_merged_tags()))
    # Not --first-parent: the PRs a Release PR brings were merged into main, off release's first-parent path.
    subjects = _run("git", "log", "--format=%s", f"{since}..HEAD").splitlines()
    prs = merged_prs(pr_numbers(subjects)) + ([pending] if pending else [])
    bump, notes = release_notes(prs, _denylist())
    if not bump:
        return ""
    notes_file.write_text(notes + "\n")
    return next_version(base, bump)


def plan_support_release(branch: str, notes_file: Path) -> str:
    base, since = _counted_from(support_base(support_line(branch), _tags()))
    subjects = _run("git", "log", "--first-parent", "--format=%s", f"{since}..HEAD").splitlines()
    notes = support_notes(merged_prs(pr_numbers(subjects)), branch, _denylist())
    if not notes:
        return ""
    notes_file.write_text(notes + "\n")
    return next_patch(base)


def _release_exists(tag: str) -> bool:
    return subprocess.run(["gh", "release", "view", tag], capture_output=True).returncode == 0


def _tag_and_release(version: str, commit: str, notes: str, latest: bool) -> None:
    tag = f"v{version}"
    if not _run("git", "tag", "--list", tag).strip():
        _run("git", "tag", tag, commit)
        _run("git", "push", "origin", f"refs/tags/{tag}")
    flag = "--latest" if latest else "--latest=false"
    _run("gh", "release", "create", tag, "--verify-tag", "--title", tag, "--notes", notes, flag)
    print(f"::notice::Published {tag}", file=sys.stderr)


def _finish_release(commit: str, version: str, latest: bool) -> None:
    notes = changelog_section(CHANGELOG, version, _run("git", "show", f"{commit}:{CHANGELOG}"))
    _tag_and_release(version, commit, notes, latest)


def _finish_unfinished(base: str, latest: bool) -> None:
    """Publish the release an earlier run left half done, if any: tagged but unpublished, or committed but untagged."""
    # Only a tag a release commit carries: a baseline tag was published by hand, or never.
    if RELEASE_COMMIT_RE.match(_run("git", "log", "-1", "--format=%s", base).strip()) and not _release_exists(base):
        _finish_release(base, base.removeprefix("v"), latest)
    # Tagged where it was committed, not at the tip, so a PR merged after it waits for the next release.
    for commit, version in _untagged_releases(base):
        _finish_release(commit, version, latest)


def publish(branch: str, notes_file: Path) -> str:
    """Release what BRANCH merged since its last tag, after finishing a release a failed run left; the new version."""
    support = branch != "release"
    latest = not support

    def base() -> str:
        return support_base(support_line(branch), _tags()) if support else last_release(_merged_tags())

    _finish_unfinished(base(), latest)
    version = plan_support_release(branch, notes_file) if support else plan_release(notes_file)
    if not version:
        print(f"::notice::Nothing to release: no PR that ships merged into {branch} since {base()}.", file=sys.stderr)
        return ""
    notes = notes_file.read_text().strip()
    prepend_changelog(CHANGELOG, version, notes, datetime.date.today())
    set_version(PYPROJECT, LOCK, version)
    _run("git", "add", "-u")
    _run("git", "add", str(CHANGELOG))
    _run("git", "commit", "-m", f"chore(release): v{version}")
    _run("git", "push", "origin", f"HEAD:refs/heads/{branch}")
    _tag_and_release(version, "HEAD", notes, latest)
    return version


def _newest_release() -> str | None:
    tags = _merged_tags()
    return last_release(tags).removeprefix("v") if _versions(tags) else None


def check_version() -> list[str]:
    # A PR's merge commit against its first parent, the tip of the branch it merges into.
    before = stated_versions(_run("git", "show", f"HEAD^1:{PYPROJECT}"), _run("git", "show", f"HEAD^1:{LOCK}"))
    after = stated_versions(PYPROJECT.read_text(), LOCK.read_text())
    return version_change_errors(before, after, _newest_release())


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check-title", help="fail unless TITLE can become a changelog line")
    check.add_argument("title")
    check.add_argument("--base", default="", help="the branch the PR merges into")
    check.add_argument("--head", default="", help="the branch the PR merges from")
    changelog = commands.add_parser("changelog", help="prepend a section to CHANGELOG.md")
    changelog.add_argument("version")
    changelog.add_argument("notes_file", type=Path)
    changelog.add_argument("--path", type=Path, default=Path("CHANGELOG.md"))
    section = commands.add_parser("changelog-section", help="print the notes CHANGELOG.md holds for VERSION")
    section.add_argument("version")
    section.add_argument("--path", type=Path, default=Path("CHANGELOG.md"))
    main_plan = commands.add_parser("plan", help="the version HEAD publishes on release; notes to NOTES_FILE")
    main_plan.add_argument("notes_file", type=Path)
    main_plan.add_argument("--pr", type=int, help="a hotfix PR into release, not merged yet, to count as merged")
    main_plan.add_argument("--title", default="", help="that PR's title; pass it as --title=TITLE")
    main_plan.add_argument("--labels", default="", help="that PR's labels, comma-separated")
    main_plan.add_argument("--head", default="", help="the branch that PR merges from")
    publish_cmd = commands.add_parser("publish", help="commit, tag and publish what BRANCH merged since its last tag")
    publish_cmd.add_argument("branch")
    publish_cmd.add_argument("notes_file", type=Path)
    base = commands.add_parser("support-base", help="the tag support/vLINE starts from")
    base.add_argument("line")
    preview = commands.add_parser("support-preview", help="the description of a PR into a support line")
    preview.add_argument("branch")
    preview.add_argument("title")
    preview.add_argument("number", type=int)
    preview.add_argument("--labels", default="", help="the PR's labels, comma-separated")
    plan = commands.add_parser("support-plan", help="the version a push to BRANCH publishes; notes to NOTES_FILE")
    plan.add_argument("branch")
    plan.add_argument("notes_file", type=Path)
    set_ver = commands.add_parser("set-version", help="write VERSION into pyproject.toml and uv.lock")
    set_ver.add_argument("version")
    set_ver.add_argument("--pyproject", type=Path, default=PYPROJECT)
    set_ver.add_argument("--lock", type=Path, default=LOCK)
    commands.add_parser("check-version", help="fail if this PR's merge commit changes the version")
    args = parser.parse_args(argv)

    try:
        if args.command == "check-title":
            errors = title_errors(args.title, _denylist(), args.base, args.head)
            for error in errors:
                print(f"PR title: {error}", file=sys.stderr)
            return 1 if errors else 0
        if args.command == "changelog":
            prepend_changelog(args.path, args.version, args.notes_file.read_text().strip(), datetime.date.today())
        elif args.command == "changelog-section":
            print(changelog_section(args.path, args.version))
        elif args.command == "plan":
            pending = None
            if args.pr is not None:
                labels = [{"name": label} for label in args.labels.split(",") if label]
                pending = {
                    "number": args.pr,
                    "title": args.title,
                    "labels": labels,
                    "baseRefName": "release",
                    "headRefName": args.head,
                }
            print(plan_release(args.notes_file, pending))
        elif args.command == "publish":
            print(publish(args.branch, args.notes_file))
        elif args.command == "support-base":
            print(support_base(parse_line(args.line), _tags()))
        elif args.command == "support-preview":
            labels = [label for label in args.labels.split(",") if label]
            print(support_preview(args.branch, args.title, args.number, _tags(), labels), end="")
        elif args.command == "support-plan":
            print(plan_support_release(args.branch, args.notes_file))
        elif args.command == "set-version":
            set_version(args.pyproject, args.lock, args.version)
        elif args.command == "check-version":
            errors = check_version()
            for error in errors:
                print(f"::error::{error}: only a release writes the version", file=sys.stderr)
            return 1 if errors else 0
    except ReleaseError as error:
        print(f"::error::{error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
