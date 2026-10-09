"""Robust statistics used for spike flags without overwriting raw values."""

from __future__ import annotations

import math
import statistics
from typing import Iterable, Optional


def finite(values: Iterable[object]) -> list[float]:
    output: list[float] = []
    for value in values:
        if isinstance(value, bool) or value is None:
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            output.append(number)
    return output


def percentile(values: Iterable[object], fraction: float) -> Optional[float]:
    numbers = sorted(finite(values))
    if not numbers:
        return None
    if len(numbers) == 1:
        return numbers[0]
    position = (len(numbers) - 1) * fraction
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return numbers[lower]
    weight = position - lower
    return numbers[lower] * (1 - weight) + numbers[upper] * weight


def mad(values: Iterable[object]) -> Optional[float]:
    numbers = finite(values)
    if not numbers:
        return None
    center = statistics.median(numbers)
    return statistics.median(abs(value - center) for value in numbers)


def cv(values: Iterable[object]) -> Optional[float]:
    numbers = finite(values)
    if len(numbers) < 2:
        return None
    mean = statistics.fmean(numbers)
    if abs(mean) < 1e-12:
        return None
    return statistics.pstdev(numbers) / abs(mean)


def hampel_spike_count(values: Iterable[object], threshold: float = 3.5) -> int:
    numbers = finite(values)
    if len(numbers) < 5:
        return 0
    center = statistics.median(numbers)
    scale = mad(numbers)
    if scale is None or scale <= 1e-12:
        return 0
    return sum(abs(value - center) / (1.4826 * scale) > threshold for value in numbers)
