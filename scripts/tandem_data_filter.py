"""Shared factory eligibility rule for all route/physics builders."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from golden_data.src.tandem_filter import is_tandem_record, tandem_exclusion_reason  # noqa: E402,F401
