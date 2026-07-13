#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from datetime import datetime, tzinfo
from pathlib import Path

from export_codex_daily_to_obsidian import format_duration, parse_duration_seconds, parse_iso_datetime


PROJECT_RE = re.compile(r"^##\s+(.+?)\s*$")
TASK_RE = re.compile(r"^###\s+(.+?)\s*$")
META_RE = re.compile(r"^([a-zA-Z_]+):\s*(.*?)\s*$")
DAILY_FILE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.md$")
DATE_TITLE_RE = re.compile(r"^#\s+\d{4}-\d{2}-\d{2}\s*$")
SUMMARY_RE = re.compile(r"^>\s*Итого по задачам:.*$")


@dataclass
class SessionTiming:
    thread_id: str
    started_at: datetime
    path: Path


@dataclass
class TaskBlock:
    path: Path
    project: str
    title: str
    comment_start: int
    comment_end: int
    meta: dict[str, str]

    @property
    def thread_id(self) -> str:
        return self.meta.get("thread", "")


@dataclass
class BackfillPlan:
    block: TaskBlock
    started_at: str
    finished_at: str
    duration: str
    reason: str


def local_timezone() -> tzinfo:
    return datetime.now().astimezone().tzinfo


def format_iso(value: datetime, timezone: tzinfo) -> str:
    return value.astimezone(timezone).replace(microsecond=0).isoformat()


def parse_timestamp(value: str, timezone: tzinfo) -> datetime | None:
    parsed = parse_iso_datetime(value)
    if not parsed:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone)
    return parsed


def discover_session_timings() -> dict[str, SessionTiming]:
    roots = [
        Path.home() / ".codex/sessions",
        Path.home() / ".codex/archived_sessions",
    ]
    result: dict[str, SessionTiming] = {}
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.glob("**/*.jsonl")):
            timing = read_session_timing(path)
            if not timing:
                continue
            old = result.get(timing.thread_id)
            if not old or ("/sessions/" in str(path) and "/archived_sessions/" in str(old.path)):
                result[timing.thread_id] = timing
    return result


def read_session_timing(path: Path) -> SessionTiming | None:
    first_timestamp: datetime | None = None
    with path.open(encoding="utf-8", errors="ignore") as handle:
        for line in handle:
            try:
                payload = json.loads(line)
            except Exception:
                continue
            timestamp = payload.get("timestamp", "")
            parsed_timestamp = parse_iso_datetime(timestamp) if timestamp else None
            if parsed_timestamp and first_timestamp is None:
                first_timestamp = parsed_timestamp
            if payload.get("type") != "session_meta":
                continue
            meta = payload.get("payload", {})
            thread_id = meta.get("id") or meta.get("session_id") or ""
            started_raw = meta.get("timestamp") or timestamp
            started_at = parse_iso_datetime(started_raw) if started_raw else first_timestamp
            if thread_id and started_at:
                return SessionTiming(thread_id=thread_id, started_at=started_at, path=path)
    return None


def parse_daily_file(path: Path) -> list[TaskBlock]:
    lines = path.read_text(encoding="utf-8").splitlines()
    project = ""
    title = ""
    blocks: list[TaskBlock] = []
    index = 0
    while index < len(lines):
        line = lines[index].rstrip()
        if match := PROJECT_RE.match(line):
            project = match.group(1).strip()
        elif match := TASK_RE.match(line):
            title = match.group(1).strip()
        elif line == "<!--":
            comment_start = index
            index += 1
            meta: dict[str, str] = {}
            while index < len(lines) and lines[index].rstrip() != "-->":
                if match := META_RE.match(lines[index].strip()):
                    meta[match.group(1).lower()] = match.group(2).strip()
                index += 1
            if index < len(lines):
                blocks.append(
                    TaskBlock(
                        path=path,
                        project=project,
                        title=title,
                        comment_start=comment_start,
                        comment_end=index,
                        meta=meta,
                    )
                )
        index += 1
    return blocks


def load_task_blocks(source: Path) -> list[TaskBlock]:
    blocks: list[TaskBlock] = []
    for path in sorted(source.glob("*.md")):
        if DAILY_FILE_RE.match(path.name):
            blocks.extend(parse_daily_file(path))
    return blocks


def build_plans(
    blocks: list[TaskBlock],
    sessions: dict[str, SessionTiming],
    timezone: tzinfo,
    include_ambiguous: bool,
) -> tuple[list[BackfillPlan], list[str]]:
    plans: list[BackfillPlan] = []
    skipped: list[str] = []
    blocks_by_thread: dict[str, list[TaskBlock]] = {}
    for block in blocks:
        if block.thread_id:
            blocks_by_thread.setdefault(block.thread_id, []).append(block)

    for thread_blocks in blocks_by_thread.values():
        thread_blocks.sort(key=lambda block: (block.path.name, block.comment_start))
        ambiguous_thread = len(thread_blocks) > 1
        previous_finished: datetime | None = None
        for position, block in enumerate(thread_blocks):
            if block.meta.get("started") and block.meta.get("finished") and block.meta.get("duration"):
                previous_finished = parse_timestamp(block.meta["finished"], timezone) or previous_finished
                continue

            session = sessions.get(block.thread_id)
            if not session:
                skipped.append(skip_message(block, "не найдена session history"))
                continue

            finished_raw = block.meta.get("finished") or block.meta.get("updated")
            if not finished_raw:
                skipped.append(skip_message(block, "нет updated/finished"))
                continue
            finished = parse_timestamp(finished_raw, timezone)
            if not finished:
                skipped.append(skip_message(block, f"не удалось прочитать finished: {finished_raw}"))
                continue

            started = parse_timestamp(block.meta["started"], timezone) if block.meta.get("started") else None
            reason = "session start"
            if not started:
                if ambiguous_thread and not include_ambiguous:
                    skipped.append(skip_message(block, "неоднозначный multi-task thread"))
                    previous_finished = finished
                    continue
                if position == 0:
                    started = session.started_at
                elif previous_finished and previous_finished < finished:
                    started = previous_finished
                    reason = "previous task finish"
                elif include_ambiguous:
                    started = session.started_at
                    reason = "session start, ambiguous multi-task thread"
                else:
                    skipped.append(skip_message(block, "неоднозначный multi-task thread"))
                    previous_finished = finished
                    continue

            seconds = (finished - started).total_seconds()
            if seconds < 0:
                skipped.append(skip_message(block, "finished раньше started"))
                previous_finished = finished
                continue

            plans.append(
                BackfillPlan(
                    block=block,
                    started_at=block.meta.get("started") or format_iso(started, timezone),
                    finished_at=block.meta.get("finished") or format_iso(finished, timezone),
                    duration=block.meta.get("duration") or format_duration(seconds),
                    reason=reason,
                )
            )
            previous_finished = finished
    return plans, skipped


def skip_message(block: TaskBlock, reason: str) -> str:
    return f"{block.path.name}: {block.project} / {block.title}: {reason}"


def apply_plans(plans: list[BackfillPlan]) -> None:
    by_path: dict[Path, list[BackfillPlan]] = {}
    for plan in plans:
        by_path.setdefault(plan.block.path, []).append(plan)

    for path, path_plans in by_path.items():
        lines = path.read_text(encoding="utf-8").splitlines()
        for plan in sorted(path_plans, key=lambda item: item.block.comment_start, reverse=True):
            replace_comment_metadata(lines, plan)
        update_daily_summary(lines)
        path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def refresh_daily_summaries(source: Path, write: bool) -> int:
    changed = 0
    for path in sorted(source.glob("*.md")):
        if not DAILY_FILE_RE.match(path.name):
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        original = list(lines)
        update_daily_summary(lines)
        if lines == original:
            continue
        changed += 1
        if write:
            path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return changed


def update_daily_summary(lines: list[str]) -> None:
    total = 0
    count = 0
    in_comment = False
    for raw in lines:
        line = raw.strip()
        if line == "<!--":
            in_comment = True
            continue
        if line == "-->":
            in_comment = False
            continue
        if not in_comment:
            continue
        match = META_RE.match(line)
        if not match or match.group(1).lower() != "duration":
            continue
        seconds = parse_duration_seconds(match.group(2))
        if seconds is None:
            continue
        total += seconds
        count += 1

    summary = f"> Итого по задачам: {format_duration(total) if count else '0м'} · учтено задач: {count}"
    filtered = [line for line in lines if not SUMMARY_RE.match(line.strip())]
    title_index = next((index for index, line in enumerate(filtered) if DATE_TITLE_RE.match(line)), None)
    if title_index is None:
        filtered.insert(0, summary)
    else:
        insert_at = title_index + 1
        while insert_at < len(filtered) and not filtered[insert_at].strip():
            insert_at += 1
        filtered[title_index + 1 : insert_at] = ["", summary, ""]
    lines[:] = filtered


def replace_comment_metadata(lines: list[str], plan: BackfillPlan) -> None:
    start = plan.block.comment_start
    end = plan.block.comment_end
    original = lines[start + 1 : end]
    existing_keys = {
        match.group(1).lower()
        for line in original
        if (match := META_RE.match(line.strip()))
    }
    additions = {
        "started": plan.started_at,
        "finished": plan.finished_at,
        "duration": plan.duration,
    }
    updated: list[str] = []
    inserted = False
    for line in original:
        if match := META_RE.match(line.strip()):
            key = match.group(1).lower()
            if key in additions:
                updated.append(f"{key}: {additions[key]}")
                continue
            if key == "updated" and not inserted:
                for addition_key, addition_value in additions.items():
                    if addition_key not in existing_keys:
                        updated.append(f"{addition_key}: {addition_value}")
                inserted = True
        updated.append(line)
    if not inserted:
        for addition_key, addition_value in additions.items():
            if addition_key not in existing_keys:
                updated.append(f"{addition_key}: {addition_value}")
    lines[start + 1 : end] = updated


def print_report(plans: list[BackfillPlan], skipped: list[str], dry_run: bool) -> None:
    action = "Будет обновлено" if dry_run else "Обновлено"
    print(f"{action}: {len(plans)}")
    for plan in plans:
        block = plan.block
        print(
            f"- {block.path.name}: {block.project} / {block.title}: "
            f"{plan.started_at} -> {plan.finished_at} = {plan.duration} ({plan.reason})"
        )
    if skipped:
        print(f"Пропущено: {len(skipped)}")
        for item in skipped:
            print(f"- {item}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Backfill started/finished/duration fields in ~/codex-daily reports."
    )
    parser.add_argument("--source", default=str(Path.home() / "codex-daily"), help="Daily reports folder")
    parser.add_argument("--write", action="store_true", help="Write changes instead of dry-run")
    parser.add_argument(
        "--include-ambiguous",
        action="store_true",
        help="Fill repeated thread tasks from session start when task boundaries are ambiguous",
    )
    parser.add_argument(
        "--refresh-summary",
        action="store_true",
        help="Recalculate the daily summary line from existing duration fields",
    )
    args = parser.parse_args()

    source = Path(args.source).expanduser().resolve()
    if not source.exists():
        raise SystemExit(f"Source folder not found: {source}")

    timezone = local_timezone()
    sessions = discover_session_timings()
    blocks = load_task_blocks(source)
    plans, skipped = build_plans(blocks, sessions, timezone, args.include_ambiguous)
    print_report(plans, skipped, dry_run=not args.write)
    if args.write and plans:
        apply_plans(plans)
    if args.refresh_summary:
        changed = refresh_daily_summaries(source, write=args.write)
        action = "Будет обновлено итогов" if not args.write else "Обновлено итогов"
        print(f"{action}: {changed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
