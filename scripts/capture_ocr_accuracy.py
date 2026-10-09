"""Read-only exact-match score of the first OCR response against reviewed cards.

Usage: python scripts/capture_ocr_accuracy.py --db /path/to/records.sqlite3
No production record is changed. Scores are meaningful only after manual reviews.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3

KEYS = ("batch_no", "alloy", "card_state")


def prediction(raw_json: str | None) -> dict:
    if not raw_json:
        return {}
    try:
        body = json.loads(raw_json)
        body = body.get("primary", body)
        content = body["choices"][0]["message"]["content"].strip()
        if content.startswith("```"):
            content = content.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        values = json.loads(content)
        return values if isinstance(values, dict) else {}
    except (ValueError, TypeError, KeyError, IndexError, AttributeError):
        return {}


def score(path: Path) -> dict:
    uri = path.resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute("""SELECT r.fields_json AS truth,o.fields_json AS effective,
              (SELECT a.raw_json FROM photo_ocr_attempts a
               WHERE a.photo_id=r.photo_id AND a.raw_json IS NOT NULL
               ORDER BY a.id DESC LIMIT 1) AS raw_json
            FROM capture_photo_reviews r JOIN photos p ON p.id=r.photo_id
            JOIN photo_ocr o ON o.photo_id=r.photo_id
            WHERE p.replaced_by_photo_id IS NULL
              AND r.id=(SELECT id FROM capture_photo_reviews WHERE photo_id=r.photo_id ORDER BY id DESC LIMIT 1)""").fetchall()
    counts = {key: 0 for key in KEYS}
    reviewed = scored = all_three = 0
    for row in rows:
        truth, effective = json.loads(row["truth"]), json.loads(row["effective"] or "{}")
        if any(truth.get(key) != effective.get(key) for key in KEYS):
            continue  # A later OCR change invalidated this review.
        reviewed += 1
        if not row["raw_json"]:
            continue
        scored += 1
        raw = prediction(row["raw_json"])
        matches = []
        for key in KEYS:
            actual = raw.get(key)
            if isinstance(actual, str):
                actual = actual.strip().upper() if key == "card_state" else actual.strip()
            match = actual == truth[key]
            counts[key] += match
            matches.append(match)
        all_three += all(matches)
    return {"reviewed": reviewed, "scored": scored,
            "per_field": {key: {"correct": counts[key], "total": scored,
                                "accuracy": round(counts[key] / scored, 4) if scored else None} for key in KEYS},
            "all_three": {"correct": all_three, "total": scored,
                          "accuracy": round(all_three / scored, 4) if scored else None}}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, required=True)
    print(json.dumps(score(parser.parse_args().db), ensure_ascii=False, indent=2))
