
from __future__ import annotations

from datetime import datetime, timedelta


def effective_capacity_by_hour(
    schedule: list[tuple[datetime, int]],
    window_starts: list[datetime],
    window_minutes: int = 60,
) -> dict[datetime, float]:
    if not schedule:
        return {}

    ordered = sorted(schedule, key=lambda item: item[0])
    result: dict[datetime, float] = {}

    for window_start in window_starts:
        window_end = window_start + timedelta(minutes=window_minutes)
        weighted_sum = 0.0
        segment_start = window_start
        current_count = ordered[0][1]
        for checkpoint_time, count in ordered:
            if checkpoint_time <= window_start:
                current_count = count
                continue
            if checkpoint_time >= window_end:
                break
            segment_minutes = (checkpoint_time - segment_start).total_seconds() / 60.0
            weighted_sum += segment_minutes * current_count
            segment_start = checkpoint_time
            current_count = count

        remaining_minutes = (window_end - segment_start).total_seconds() / 60.0
        weighted_sum += remaining_minutes * current_count

        result[window_start] = weighted_sum / window_minutes

    return result


def active_capacity_at(schedule: list[tuple[datetime, int]], timestamp: datetime) -> int | None:
    if not schedule:
        return None
    ordered = sorted(schedule, key=lambda item: item[0])
    active = ordered[0][1]
    for checkpoint_time, count in ordered:
        if checkpoint_time > timestamp:
            break
        active = count
    return active
