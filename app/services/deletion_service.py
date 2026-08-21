from __future__ import annotations

import os
import shutil
from dataclasses import dataclass

from ..types import DeletionStateRow, MovieState


@dataclass(frozen=True, slots=True)
class DeletionQueueSummary:
    scheduled: int = 0
    due_now: int = 0
    potential_savings_bytes: int = 0
    sized_paths: int = 0
    missing_or_invalid_paths: int = 0

    @property
    def potential_savings(self) -> str:
        return format_bytes_human(self.potential_savings_bytes)

    def payload(self) -> dict[str, object]:
        return {
            "scheduled": self.scheduled,
            "dueNow": self.due_now,
            "potentialSavingsBytes": self.potential_savings_bytes,
            "potentialSavings": self.potential_savings,
            "sizedPaths": self.sized_paths,
            "missingOrInvalidPaths": self.missing_or_invalid_paths,
        }


def format_bytes_human(size_bytes: int) -> str:
    if size_bytes <= 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    value = float(size_bytes)
    unit_idx = 0
    while value >= 1024 and unit_idx < len(units) - 1:
        value /= 1024.0
        unit_idx += 1
    if unit_idx == 0:
        return f"{int(value)} {units[unit_idx]}"
    return f"{value:.2f} {units[unit_idx]}"


class DeletionService:
    def delete_movie_folder(self, path: str, dry_run: bool) -> tuple[bool, str | None]:
        target_path = os.path.normpath(path.strip())
        if not target_path:
            return False, "empty_path"
        parent = os.path.dirname(target_path)
        if target_path in {"/", "."} or parent in {"", "/"}:
            return False, "invalid_path"
        if not os.path.exists(target_path):
            return True, "already_missing"
        if not os.path.isdir(target_path):
            return False, "not_a_directory"
        if dry_run:
            return True, None

        shutil.rmtree(target_path)
        return True, None

    def directory_size_bytes(self, path: str) -> int | None:
        target_path = os.path.normpath(path.strip())
        if not target_path or not os.path.isdir(target_path):
            return None

        total = 0
        stack: list[str] = [target_path]
        while stack:
            current = stack.pop()
            try:
                with os.scandir(current) as entries:
                    for entry in entries:
                        try:
                            if entry.is_symlink():
                                continue
                            if entry.is_dir(follow_symlinks=False):
                                stack.append(entry.path)
                            elif entry.is_file(follow_symlinks=False):
                                total += entry.stat(follow_symlinks=False).st_size
                        except OSError:
                            continue
            except OSError:
                continue
        return total

    def summarize_queue(
        self,
        scheduled_rows: list[DeletionStateRow],
        movies_by_id: dict[int, MovieState],
        now_ts: int,
    ) -> DeletionQueueSummary:
        due_now = 0
        potential_savings_bytes = 0
        sized_paths = 0
        missing_or_invalid_paths = 0

        for row in scheduled_rows:
            if int(row.delete_after_ts) <= now_ts:
                due_now += 1
            movie_info = movies_by_id.get(row.radarr_id)
            target_path = row.movie_path
            if movie_info and movie_info.path:
                target_path = movie_info.path

            size_bytes = self.directory_size_bytes(target_path)
            if size_bytes is not None:
                potential_savings_bytes += int(size_bytes)
                sized_paths += 1
            else:
                missing_or_invalid_paths += 1

        return DeletionQueueSummary(
            scheduled=len(scheduled_rows),
            due_now=due_now,
            potential_savings_bytes=potential_savings_bytes,
            sized_paths=sized_paths,
            missing_or_invalid_paths=missing_or_invalid_paths,
        )
