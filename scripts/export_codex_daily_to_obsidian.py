#!/usr/bin/env python3
from __future__ import annotations

import argparse
import html
import json
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


DATE_RE = re.compile(r"^#\s+(\d{4}-\d{2}-\d{2})\s*$")
PROJECT_RE = re.compile(r"^##\s+(.+?)\s*$")
TASK_RE = re.compile(r"^###\s+(.+?)\s*$")
THREAD_RE = re.compile(r"^thread:\s*([0-9a-f-]+)\s*$", re.IGNORECASE)
TASK_META_RE = re.compile(r"^task:\s*(.+?)\s*$", re.IGNORECASE)
PARENT_RE = re.compile(r"^parent:\s*(.+?)\s*$", re.IGNORECASE)
STARTED_RE = re.compile(r"^started:\s*(.+?)\s*$", re.IGNORECASE)
FINISHED_RE = re.compile(r"^finished:\s*(.+?)\s*$", re.IGNORECASE)
DURATION_RE = re.compile(r"^duration:\s*(.+?)\s*$", re.IGNORECASE)
UPDATED_RE = re.compile(r"^updated:\s*(.+?)\s*$", re.IGNORECASE)
DAILY_FILENAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.md$")
DURATION_PART_RE = re.compile(r"(\d+)\s*(д|ч|м|d|h|m)\b", re.IGNORECASE)

PROJECT_PARENTS: dict[str, str] = {}


@dataclass
class Task:
    title: str
    bullets: list[str] = field(default_factory=list)
    thread_id: str = ""
    task_id: str = ""
    note_id: str = ""
    parent_project: str = ""
    started_at: str = ""
    finished_at: str = ""
    duration: str = ""
    updated_at: str = ""


@dataclass
class ProjectDay:
    date: str
    project: str
    tasks: list[Task] = field(default_factory=list)


@dataclass
class SourceMessage:
    role: str
    text: str


@dataclass
class ThreadSource:
    thread_id: str
    session_id: str
    session_path: str
    messages: list[SourceMessage] = field(default_factory=list)


def safe_filename(value: str) -> str:
    value = value.strip()
    value = value.replace("/", "_")
    value = re.sub(r"[^a-zA-Z0-9._ -]+", "_", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value or "note"


def wiki_link(target: str, label: str | None = None) -> str:
    if label and label != target:
        return f"[[{target}|{label}]]"
    return f"[[{target}]]"


def daily_note_target(date: str) -> str:
    return f"Daily/{date[:4]}/{date[5:7]}/{date}"


def daily_note_path(vault: Path, date: str) -> Path:
    return vault / "Daily" / date[:4] / date[5:7] / f"{date}.md"


def task_note_target(task: Task) -> str:
    note_id = task.note_id or task.thread_id
    return f"Threads/{note_id}"


def parse_iso_datetime(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def format_duration(seconds: float) -> str:
    minutes = max(1, round(seconds / 60))
    days, remainder = divmod(minutes, 24 * 60)
    hours, minutes = divmod(remainder, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}д")
    if hours:
        parts.append(f"{hours}ч")
    if minutes or not parts:
        parts.append(f"{minutes}м")
    return " ".join(parts)


def parse_duration_seconds(value: str) -> int | None:
    total = 0
    for amount, unit in DURATION_PART_RE.findall(value):
        multiplier = {"д": 86400, "d": 86400, "ч": 3600, "h": 3600, "м": 60, "m": 60}.get(
            unit.lower()
        )
        if multiplier:
            total += int(amount) * multiplier
    return total or None


def task_duration_seconds(task: Task) -> float | None:
    if task.duration:
        parsed = parse_duration_seconds(task.duration)
        if parsed is not None:
            return parsed
    if not task.started_at or not task.finished_at:
        return None
    started = parse_iso_datetime(task.started_at)
    finished = parse_iso_datetime(task.finished_at)
    if not started or not finished:
        return None
    seconds = (finished - started).total_seconds()
    if seconds < 0:
        return None
    return seconds


def task_duration(task: Task) -> str:
    if task.duration:
        return task.duration
    seconds = task_duration_seconds(task)
    if seconds is None:
        return ""
    return format_duration(seconds)


def day_duration_summary(days: list[ProjectDay]) -> tuple[float, int]:
    total = 0.0
    count = 0
    for day in days:
        for task in day.tasks:
            seconds = task_duration_seconds(task)
            if seconds is None:
                continue
            total += seconds
            count += 1
    return total, count


def render_day_duration_summary(days: list[ProjectDay]) -> str:
    total, count = day_duration_summary(days)
    duration = format_duration(total) if count else "0м"
    return f"> Итого по задачам: {duration} · учтено задач: {count}"


def project_lineage(project: str, parent_map: dict[str, str]) -> list[str]:
    lineage = [project]
    seen = {project}
    while parent := parent_map.get(lineage[-1]):
        if parent in seen:
            break
        lineage.append(parent)
        seen.add(parent)
    return lineage


def root_project(project: str, parent_map: dict[str, str]) -> str:
    return project_lineage(project, parent_map)[-1]


def project_links(project: str, project_note_map: dict[str, str], parent_map: dict[str, str]) -> str:
    notes = [
        f"Projects/{project_note_map[name]}"
        for name in project_lineage(project, parent_map)
        if name in project_note_map
    ]
    return " · ".join(wiki_link(note, note.removeprefix("Projects/")) for note in notes)


def build_children_map(parent_map: dict[str, str]) -> dict[str, list[str]]:
    children: dict[str, list[str]] = {}
    for child, parent in parent_map.items():
        children.setdefault(parent, []).append(child)
    for child_list in children.values():
        child_list.sort()
    return children


def collect_descendants(project: str, children_map: dict[str, list[str]]) -> list[str]:
    descendants: list[str] = []
    stack = list(reversed(children_map.get(project, [])))
    seen: set[str] = set()
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        descendants.append(current)
        for child in reversed(children_map.get(current, [])):
            stack.append(child)
    return descendants


def parse_daily_file(path: Path) -> list[ProjectDay]:
    lines = path.read_text(encoding="utf-8").splitlines()
    date = ""
    project = ""
    current_task: Task | None = None
    current_day: ProjectDay | None = None
    days: list[ProjectDay] = []

    for raw in lines:
        line = raw.rstrip()
        if not line:
            continue

        if match := DATE_RE.match(line):
            date = match.group(1)
            continue

        if match := PROJECT_RE.match(line):
            project = match.group(1).strip()
            current_day = ProjectDay(date=date, project=project)
            days.append(current_day)
            current_task = None
            continue

        if match := TASK_RE.match(line):
            if current_day is None:
                continue
            current_task = Task(title=match.group(1).strip())
            current_day.tasks.append(current_task)
            continue

        if line.startswith("- ") and current_task is not None:
            current_task.bullets.append(line[2:].strip())
            continue

        if line == "<!--":
            current_task = current_task or Task(title="Без названия")
            continue

        if current_task is not None:
            if match := THREAD_RE.match(line):
                current_task.thread_id = match.group(1).strip()
                continue
            if match := TASK_META_RE.match(line):
                current_task.task_id = match.group(1).strip()
                continue
            if match := PARENT_RE.match(line):
                current_task.parent_project = match.group(1).strip()
                continue
            if match := STARTED_RE.match(line):
                current_task.started_at = match.group(1).strip()
                continue
            if match := FINISHED_RE.match(line):
                current_task.finished_at = match.group(1).strip()
                continue
            if match := DURATION_RE.match(line):
                current_task.duration = match.group(1).strip()
                continue
            if match := UPDATED_RE.match(line):
                current_task.updated_at = match.group(1).strip()
                continue

    return days


def render_daily(date: str, days: list[ProjectDay], project_note_map: dict[str, str], parent_map: dict[str, str]) -> str:
    projects = sorted({day.project for day in days})
    project_value = ", ".join(f'"{project}"' for project in projects)
    root_groups: dict[str, list[ProjectDay]] = {}
    for day in days:
        root_groups.setdefault(root_project(day.project, parent_map), []).append(day)
    lines = [
        "---",
        f'date: "{date}"',
        f"project: [{project_value}]",
        'source: "codex-daily"',
        "---",
        "",
        f"# {date}",
        "",
        render_day_duration_summary(days),
        "",
    ]
    for root, group_days in sorted(root_groups.items()):
        root_links = project_links(root, project_note_map, parent_map)
        lines.extend([f"> [!summary]- {root}", ">", f"> Project: {root_links}", ">"])
        for day in sorted(group_days, key=lambda item: item.project):
            nested = day.project != root or len(group_days) > 1
            prefix = "> >" if nested else ">"
            if nested:
                lines.extend([f"> > [!summary]- {day.project}", "> >", f"> > Project: {project_links(day.project, project_note_map, parent_map)}", "> >"])
            for task in day.tasks:
                thread_note = task_note_target(task) if task.thread_id else ""
                lines.append(f"{prefix} ### {task.title}")
                if thread_note:
                    lines.append(f"{prefix} - Thread: {wiki_link(thread_note, task.thread_id)}")
                duration = task_duration(task)
                if duration:
                    lines.append(f"{prefix} - Duration: {duration}")
                if task.started_at:
                    lines.append(f"{prefix} - Started: {task.started_at}")
                if task.finished_at:
                    lines.append(f"{prefix} - Finished: {task.finished_at}")
                if task.updated_at:
                    lines.append(f"{prefix} - Updated: {task.updated_at}")
                for bullet in task.bullets:
                    lines.append(f"{prefix} - {bullet}")
                lines.append(prefix)
            if nested:
                lines.append(">")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_project(
    project: str,
    entries: list[ProjectDay],
    project_note: str,
    parent_note: str | None = None,
    child_notes: list[str] | None = None,
) -> str:
    ordered_entries = sorted(entries, key=lambda entry: (entry.date, entry.project))
    lines = [
        "---",
        f'title: "{project}"',
        'type: "project"',
        'source: "codex-daily"',
        "---",
        "",
        f"# {project}",
        "",
    ]
    if parent_note:
        lines.extend(["## Parent", f"- {wiki_link(parent_note, parent_note.removeprefix('Projects/'))}", ""])
    if child_notes:
        lines.append("## Children")
        lines.append("")
        lines.append("> [!summary]- Children")
        for note in child_notes:
            lines.append(f"> - {wiki_link(note, note.removeprefix('Projects/'))}")
        lines.append("")
    lines.append("## Days")
    seen_dates: set[str] = set()
    dates: list[str] = []
    for entry in ordered_entries:
        if entry.date in seen_dates:
            continue
        seen_dates.add(entry.date)
        dates.append(entry.date)
    dates_by_year: dict[str, dict[str, list[str]]] = {}
    for date in dates:
        dates_by_year.setdefault(date[:4], {}).setdefault(date[5:7], []).append(date)
    for year, months in sorted(dates_by_year.items()):
        lines.extend(["", f"> [!summary]- {year}", ">"])
        for month, month_dates in sorted(months.items()):
            lines.extend([f"> > [!summary]- {month}", "> >"])
            for date in month_dates:
                lines.append(f"> > - {wiki_link(daily_note_target(date), date)}")
            lines.append("> >")
        lines.append(">")
    lines.append("")
    lines.append("## Threads")
    lines.append("")
    lines.append("> [!summary]- Threads")
    seen_threads: set[str] = set()
    for entry in ordered_entries:
        for task in entry.tasks:
            note_id = task.note_id or task.thread_id
            if note_id and note_id not in seen_threads:
                seen_threads.add(note_id)
                lines.append(f"> - {wiki_link(task_note_target(task), task.title)}")
    return "\n".join(lines).rstrip() + "\n"


def render_thread(
    task: Task,
    day: ProjectDay,
    project_notes: list[str],
    source: ThreadSource | None = None,
) -> str:
    project_links = " · ".join(
        wiki_link(note, note.removeprefix("Projects/")) for note in project_notes
    )
    duration = task_duration(task)
    frontmatter = [
        "---",
        f'title: "{task.title}"',
        f'thread: "{task.thread_id}"',
        f'thread_id: "{task.thread_id}"',
        f'task_id: "{task.task_id}"',
    ]
    if source:
        frontmatter.extend(
            [
                f'session_id: "{source.session_id}"',
                f'session_path: "{source.session_path}"',
            ]
        )
    frontmatter.extend(
        [
            f'date: "{day.date}"',
            f'project: "{day.project}"',
        ]
    )
    if task.started_at:
        frontmatter.append(f'started: "{task.started_at}"')
    if task.finished_at:
        frontmatter.append(f'finished: "{task.finished_at}"')
    if duration:
        frontmatter.append(f'duration: "{duration}"')
    frontmatter.extend(
        [
            'type: "thread"',
            'source: "codex-daily"',
            "---",
        ]
    )
    lines = [
        *frontmatter,
        "",
        f"# {task.title}",
        "",
        f"- Day: {wiki_link(daily_note_target(day.date), day.date)}",
        f"- Projects: {project_links}",
    ]
    if duration:
        lines.append(f"- Duration: {duration}")
    if task.started_at:
        lines.append(f"- Started: {task.started_at}")
    if task.finished_at:
        lines.append(f"- Finished: {task.finished_at}")
    if task.updated_at:
        lines.append(f"- Updated: {task.updated_at}")
    if task.thread_id:
        lines.append(f"- Raw thread: {wiki_link(f'Sources/{task.thread_id}', task.thread_id)}")
    lines.append("")
    for bullet in task.bullets:
        lines.append(f"- {bullet}")
    return "\n".join(lines).rstrip() + "\n"


def render_source_thread(
    source: ThreadSource,
    task: Task,
    day: ProjectDay,
    project_notes: list[str],
) -> str:
    project_links = " · ".join(
        wiki_link(note, note.removeprefix("Projects/")) for note in project_notes
    )
    lines = [
        "---",
        f'title: "Raw {task.title}"',
        f'thread_id: "{source.thread_id}"',
        f'session_id: "{source.session_id}"',
        f'session_path: "{source.session_path}"',
        f'date: "{day.date}"',
        f'project: "{day.project}"',
        'type: "source-thread"',
        'source: "codex-daily"',
        "---",
        "",
        f"# Raw {task.title}",
        "",
        f"- Thread: `{source.thread_id}`",
        f"- Session: `{source.session_id}`",
        f"- Source file: `{source.session_path}`",
        f"- Projects: {project_links}",
        "",
        "## Transcript",
    ]
    for index, message in enumerate(source.messages, start=1):
        lines.extend(
            [
                "",
                f"### {message.role} {index}",
                "",
                "<details>",
                f"<summary>{message.role}</summary>",
                "",
                "<pre>",
                html.escape(message.text),
                "</pre>",
                "</details>",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def render_index(projects: dict[str, list[ProjectDay]], project_notes: dict[str, str]) -> str:
    lines = [
        "---",
        'title: "Codex Index"',
        'type: "index"',
        'source: "codex-daily"',
        "---",
        "",
        "# Codex Index",
        "",
        "## Projects",
    ]
    for project in sorted(projects):
        lines.append(f"- {wiki_link(f'Projects/{project_notes[project]}', project)}")
    lines.append("")
    lines.append("## Daily Notes")
    dates = sorted({day.date for days in projects.values() for day in days})
    current_year = ""
    current_month = ""
    for date in dates:
        year = date[:4]
        month = date[5:7]
        if year != current_year:
            lines.extend(["", f"### {year}"])
            current_year = year
            current_month = ""
        if month != current_month:
            lines.extend(["", f"#### {month}"])
            current_month = month
        lines.append(f"- {wiki_link(daily_note_target(date), date)}")
    return "\n".join(lines).rstrip() + "\n"


def render_time_report(
    days: list[ProjectDay],
    project_notes: dict[str, str],
    parent_map: dict[str, str],
) -> str:
    root_totals: dict[str, float] = {}
    root_counts: dict[str, int] = {}
    project_totals: dict[str, float] = {}
    project_counts: dict[str, int] = {}
    day_totals: dict[str, dict[str, float]] = {}
    day_counts: dict[str, dict[str, int]] = {}

    for day in days:
        for task in day.tasks:
            seconds = task_duration_seconds(task)
            if seconds is None:
                continue
            root = root_project(day.project, parent_map)
            root_totals[root] = root_totals.get(root, 0) + seconds
            root_counts[root] = root_counts.get(root, 0) + 1
            project_totals[day.project] = project_totals.get(day.project, 0) + seconds
            project_counts[day.project] = project_counts.get(day.project, 0) + 1
            day_totals.setdefault(day.date, {})
            day_counts.setdefault(day.date, {})
            day_totals[day.date][day.project] = day_totals[day.date].get(day.project, 0) + seconds
            day_counts[day.date][day.project] = day_counts[day.date].get(day.project, 0) + 1

    def project_label(project: str) -> str:
        note = project_notes.get(project)
        if note:
            return f"[[Projects/{note}\\|{project}]]"
        return project

    def append_total_table(lines: list[str], totals: dict[str, float], counts: dict[str, int]) -> None:
        lines.extend(["| Проект | Время | Задачи |", "| --- | ---: | ---: |"])
        for project, seconds in sorted(totals.items(), key=lambda item: (-item[1], item[0])):
            lines.append(f"| {project_label(project)} | {format_duration(seconds)} | {counts[project]} |")
        if not totals:
            lines.append("| Нет данных | 0м | 0 |")

    lines = [
        "# Время по проектам",
        "",
        "> Историческое время восстановлено из session metadata и может быть примерным.",
        "",
        "## Главные проекты",
        "",
    ]
    append_total_table(lines, root_totals, root_counts)
    lines.extend(["", "## Все проекты", ""])
    append_total_table(lines, project_totals, project_counts)
    lines.extend(["", "## По дням"])
    for date in sorted(day_totals, reverse=True):
        lines.extend(["", f"### {date}", ""])
        lines.extend(["| Проект | Время | Задачи |", "| --- | ---: | ---: |"])
        for project, seconds in sorted(day_totals[date].items(), key=lambda item: (-item[1], item[0])):
            lines.append(f"| {project_label(project)} | {format_duration(seconds)} | {day_counts[date][project]} |")
    return "\n".join(lines).rstrip() + "\n"


def validate_daily_exports(vault: Path, daily_by_date: dict[str, list[ProjectDay]]) -> None:
    problems: list[str] = []
    for date, days in sorted(daily_by_date.items()):
        expected_tasks = sum(len(day.tasks) for day in days)
        path = daily_note_path(vault, date)
        if not path.exists():
            problems.append(f"{date}: missing {path}")
            continue
        exported_tasks = sum(
            1
            for line in path.read_text(encoding="utf-8").splitlines()
            if re.match(r"^(?:>\s*)*###\s+", line)
        )
        if exported_tasks != expected_tasks:
            problems.append(f"{date}: expected {expected_tasks} tasks, exported {exported_tasks}")
    if problems:
        raise SystemExit("Daily export validation failed:\n" + "\n".join(problems))


def remove_stale_flat_daily_notes(vault: Path) -> None:
    daily_root = vault / "Daily"
    if not daily_root.exists():
        return
    for path in daily_root.glob("*.md"):
        if not DAILY_FILENAME_RE.match(path.name):
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except Exception:
            continue
        if 'source: "codex-daily"' in content:
            path.unlink()


def remove_stale_thread_notes(vault: Path, active_note_ids: set[str]) -> None:
    thread_root = vault / "Threads"
    if not thread_root.exists():
        return
    for path in thread_root.glob("*.md"):
        if path.stem in active_note_ids:
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except Exception:
            continue
        if 'source: "codex-daily"' in content:
            path.unlink()


def source_signature(source: Path) -> dict[str, float]:
    latest = 0.0
    files = []
    for path in sorted(source.glob("*.md")):
        stat = path.stat()
        latest = max(latest, stat.st_mtime)
        files.append([path.name, stat.st_mtime, stat.st_size])
    return {"latest_mtime": latest, "files": files, "count": len(files)}


def discover_session_files() -> dict[str, Path]:
    session_files = (
        list(Path.home().joinpath(".codex/sessions").glob("**/*.jsonl"))
        + list(Path.home().joinpath(".codex/archived_sessions").glob("*.jsonl"))
    )
    result: dict[str, Path] = {}
    for path in session_files:
        thread_id = ""
        try:
            with path.open(encoding="utf-8", errors="ignore") as handle:
                for line in handle:
                    try:
                        payload = json.loads(line)
                    except Exception:
                        continue
                    if payload.get("type") == "session_meta":
                        meta = payload.get("payload", {})
                        thread_id = meta.get("id") or meta.get("session_id") or ""
                        break
        except Exception:
            continue
        if not thread_id:
            continue
        old = result.get(thread_id)
        if not old or ("/sessions/" in str(path) and "/archived_sessions/" in str(old)):
            result[thread_id] = path
    return result


def parse_thread_source(thread_id: str, session_path: Path) -> ThreadSource:
    session_id = thread_id
    messages: list[SourceMessage] = []
    with session_path.open(encoding="utf-8", errors="ignore") as handle:
        for line in handle:
            try:
                payload = json.loads(line)
            except Exception:
                continue
            if payload.get("type") != "response_item":
                continue
            item = payload.get("payload", {})
            if item.get("type") != "message":
                continue
            role = item.get("role", "")
            if role not in {"user", "assistant"}:
                continue
            text = "\n".join(
                chunk.get("text", "")
                for chunk in item.get("content", [])
                if chunk.get("type") in {"input_text", "output_text"}
            ).strip()
            if text:
                messages.append(SourceMessage(role=role, text=text))
    return ThreadSource(thread_id=thread_id, session_id=session_id, session_path=str(session_path), messages=messages)


def assign_task_note_ids(days: list[ProjectDay]) -> None:
    tasks_by_thread: dict[str, list[tuple[ProjectDay, Task]]] = {}
    for day in days:
        for task in day.tasks:
            if task.thread_id:
                tasks_by_thread.setdefault(task.thread_id, []).append((day, task))

    for thread_id, tasks in tasks_by_thread.items():
        duplicate_thread = len(tasks) > 1
        used_ids: set[str] = set()
        for day, task in tasks:
            if duplicate_thread or task.task_id:
                base = task.task_id or f"{day.project}-{task.title}"
                suffix = safe_filename(base)
                note_id = f"{thread_id}--{suffix}"
                counter = 2
                while note_id in used_ids:
                    note_id = f"{thread_id}--{suffix}-{counter}"
                    counter += 1
                task.note_id = note_id
                used_ids.add(note_id)
            else:
                task.note_id = thread_id


def load_project_parent_map(path: Path) -> dict[str, str]:
    if not path.exists():
        return dict(PROJECT_PARENTS)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as error:
        raise SystemExit(f"Failed to read project parent map {path}: {error}") from error
    if not isinstance(payload, dict):
        raise SystemExit(f"Project parent map must be a JSON object: {path}")
    result = dict(PROJECT_PARENTS)
    for child, parent in payload.items():
        if not isinstance(child, str) or not isinstance(parent, str):
            raise SystemExit(f"Project parent map values must be strings: {path}")
        child = child.strip()
        parent = parent.strip()
        if child and parent:
            result[child] = parent
    return result


def build_parent_map(days: list[ProjectDay], configured_parents: dict[str, str]) -> dict[str, str]:
    parent_map = {child: parent for child, parent in configured_parents.items()}
    for day in days:
        for task in day.tasks:
            parent = task.parent_project.strip()
            if parent and parent != day.project:
                parent_map[day.project] = parent
    return parent_map


def main() -> int:
    parser = argparse.ArgumentParser(description="Export ~/codex-daily into an Obsidian vault.")
    parser.add_argument("--source", default=str(Path.home() / "codex-daily"), help="Source folder with daily logs")
    parser.add_argument("--vault", default=str(Path.home() / "codex-obsidian"), help="Destination Obsidian vault")
    parser.add_argument("--clean", action="store_true", help="Remove previously generated vault files before writing")
    parser.add_argument(
        "--project-parents",
        default=str(Path.home() / "codex-tools/project-parents.json"),
        help="Optional JSON object mapping child projects to parent projects",
    )
    parser.add_argument(
        "--state-file",
        default=str(Path.home() / "Library/Application Support/codex-tools/obsidian-sync-state.json"),
        help="Cache file with source signature",
    )
    parser.add_argument("--force", action="store_true", help="Export even if source signature did not change")
    args = parser.parse_args()

    source = Path(args.source).expanduser().resolve()
    vault = Path(args.vault).expanduser().resolve()
    project_parents_file = Path(args.project_parents).expanduser().resolve()
    state_file = Path(args.state_file).expanduser().resolve()

    if not source.exists():
        raise SystemExit(f"Source folder not found: {source}")

    daily_files = sorted(source.glob("*.md"))
    if not daily_files:
        raise SystemExit(f"No markdown files found in {source}")

    current_signature = source_signature(source)
    if state_file.exists() and not args.force:
        try:
            previous = json.loads(state_file.read_text(encoding="utf-8"))
        except Exception:
            previous = {}
        if previous.get("latest_mtime") == current_signature["latest_mtime"] and previous.get("count") == current_signature["count"]:
            return 0

    daily_pages: list[ProjectDay] = []
    for daily_file in daily_files:
        daily_pages.extend(parse_daily_file(daily_file))
    assign_task_note_ids(daily_pages)

    if args.clean and vault.exists():
        shutil.rmtree(vault)

    (vault / "Daily").mkdir(parents=True, exist_ok=True)
    (vault / "Projects").mkdir(parents=True, exist_ok=True)
    (vault / "Threads").mkdir(parents=True, exist_ok=True)
    (vault / "Sources").mkdir(parents=True, exist_ok=True)
    (vault / "Reports").mkdir(parents=True, exist_ok=True)

    project_map: dict[str, list[ProjectDay]] = {}
    project_notes: dict[str, str] = {}
    used_names: set[str] = set()
    thread_map: dict[str, tuple[ProjectDay, Task]] = {}
    session_files = discover_session_files()
    parent_map = build_parent_map(daily_pages, load_project_parent_map(project_parents_file))
    children_map = build_children_map(parent_map)

    for day in daily_pages:
        project_map.setdefault(day.project, []).append(day)
        for task in day.tasks:
            if task.thread_id:
                thread_map[task.note_id or task.thread_id] = (day, task)

    for project in list(project_map):
        for parent in project_lineage(project, parent_map)[1:]:
            project_map.setdefault(parent, [])

    for project in sorted(project_map):
        base = safe_filename(project)
        candidate = base
        counter = 2
        while candidate in used_names:
            candidate = f"{base}__{counter}"
            counter += 1
        project_notes[project] = candidate
        used_names.add(candidate)

    daily_by_date: dict[str, list[ProjectDay]] = {}
    for day in daily_pages:
        daily_by_date.setdefault(day.date, []).append(day)

    for date, days in daily_by_date.items():
        path = daily_note_path(vault, date)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            render_daily(date, days, project_notes, parent_map),
            encoding="utf-8",
        )
    remove_stale_flat_daily_notes(vault)
    validate_daily_exports(vault, daily_by_date)

    for project, days in project_map.items():
        project_note = f"Projects/{project_notes[project]}"
        child_notes = [f"Projects/{project_notes[name]}" for name in children_map.get(project, []) if name in project_notes]
        scoped_days: list[ProjectDay] = []
        for scope_project in [project] + collect_descendants(project, children_map):
            scoped_days.extend(project_map.get(scope_project, []))
        (vault / "Projects" / f"{project_notes[project]}.md").write_text(
            render_project(
                project,
                scoped_days,
                project_note,
                None if len(project_lineage(project, parent_map)) == 1 else f"Projects/{project_notes[project_lineage(project, parent_map)[1]]}",
                child_notes,
            ),
            encoding="utf-8",
        )

    rendered_sources: set[str] = set()
    for note_id, (day, task) in thread_map.items():
        lineage = [
            f"Projects/{project_notes[name]}"
            for name in project_lineage(day.project, parent_map)
            if name in project_notes
        ]
        source = None
        session_path = session_files.get(task.thread_id)
        if session_path:
            source = parse_thread_source(task.thread_id, session_path)
        (vault / "Threads" / f"{note_id}.md").write_text(
            render_thread(task, day, lineage, source),
            encoding="utf-8",
        )
        if source and task.thread_id not in rendered_sources:
            rendered_sources.add(task.thread_id)
            (vault / "Sources" / f"{task.thread_id}.md").write_text(
                render_source_thread(source, task, day, lineage),
                encoding="utf-8",
            )
    remove_stale_thread_notes(vault, set(thread_map))

    (vault / "Codex Index.md").write_text(render_index(project_map, project_notes), encoding="utf-8")
    (vault / "Reports" / "Project Time.md").write_text(
        render_time_report(daily_pages, project_notes, parent_map),
        encoding="utf-8",
    )
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps(current_signature, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
