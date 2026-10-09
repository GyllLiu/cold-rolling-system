"""Streaming field profiles with deterministic bounded percentile samples."""

from __future__ import annotations

import csv
import math
import random
from pathlib import Path
from typing import Any

from .outlier_detector import percentile


class FieldProfile:
    def __init__(self, name: str, seed: int, reservoir_size: int = 50000) -> None:
        self.name = name
        self.total = 0
        self.missing = 0
        self.minimum: float | None = None
        self.maximum: float | None = None
        self.sample: list[float] = []
        self.unique: set[str] = set()
        self.unique_truncated = False
        self.reservoir_size = reservoir_size
        self.random = random.Random(seed)
        self.boolean_seen = False
        self.numeric_seen = False

    def add(self, value: Any) -> None:
        self.total += 1
        if value is None or value == "":
            self.missing += 1
            return
        if isinstance(value, bool):
            self.boolean_seen = True
            key = str(value).lower()
        else:
            try:
                number = float(value)
            except (TypeError, ValueError):
                key = str(value)
            else:
                if not math.isfinite(number):
                    self.missing += 1
                    return
                self.numeric_seen = True
                key = f"{number:.9g}"
                self.minimum = number if self.minimum is None else min(self.minimum, number)
                self.maximum = number if self.maximum is None else max(self.maximum, number)
                observed = self.total - self.missing
                if len(self.sample) < self.reservoir_size:
                    self.sample.append(number)
                else:
                    index = self.random.randrange(observed)
                    if index < self.reservoir_size:
                        self.sample[index] = number
        if not self.unique_truncated:
            self.unique.add(key)
            if len(self.unique) > 10000:
                self.unique_truncated = True
                self.unique.clear()

    def row(self) -> dict[str, Any]:
        if self.boolean_seen and not self.numeric_seen:
            data_type = "boolean"
        elif self.numeric_seen:
            data_type = "numeric"
        else:
            data_type = "string/unknown"
        output: dict[str, Any] = {
            "field": self.name,
            "data_type": data_type,
            "sampled_row_count": self.total,
            "missing_count": self.missing,
            "missing_rate": self.missing / self.total if self.total else 1.0,
            "unique_count": ">10000" if self.unique_truncated else len(self.unique),
            "min": self.minimum if self.minimum is not None else "",
            "p01": percentile(self.sample, 0.01) or "",
            "p05": percentile(self.sample, 0.05) or "",
            "median": percentile(self.sample, 0.50) or "",
            "p95": percentile(self.sample, 0.95) or "",
            "p99": percentile(self.sample, 0.99) or "",
            "max": self.maximum if self.maximum is not None else "",
            "percentile_sample_size": len(self.sample),
        }
        return output


class DataProfiler:
    def __init__(self, field_names: list[str], seed: int) -> None:
        self.fields = {
            name: FieldProfile(name, seed + index)
            for index, name in enumerate(field_names)
        }

    def add_point(self, point: dict[str, Any]) -> None:
        for name, profile in self.fields.items():
            profile.add(point.get(name))

    def rows(self) -> list[dict[str, Any]]:
        return [profile.row() for profile in self.fields.values()]

    def write_csv(self, path: Path) -> None:
        rows = self.rows()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
