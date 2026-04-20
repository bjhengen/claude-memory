"""Runtime configuration for consolidation, read from environment variables."""

import os


def _float(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).lower() in ("true", "1", "yes")


# Kill switch
ENABLED = _bool("CONSOLIDATION_ENABLED", True)

# Candidate finder
CANDIDATE_COSINE = _float("CONSOLIDATION_CANDIDATE_COSINE", 0.85)
CANDIDATE_TOP_K = _int("CONSOLIDATION_CANDIDATE_TOP_K", 5)

# Judge
JUDGE_MODEL = os.getenv("CONSOLIDATION_JUDGE_MODEL", "claude-haiku-4-5-20251001")
JUDGE_TIMEOUT_SECONDS = _float("CONSOLIDATION_JUDGE_TIMEOUT_SECONDS", 2.0)

# Autonomy thresholds (per-action asymmetric)
AUTO_MERGE_CONFIDENCE = _float("CONSOLIDATION_AUTO_MERGE_CONFIDENCE", 0.90)
AUTO_SUPERSEDE_CONFIDENCE = _float("CONSOLIDATION_AUTO_SUPERSEDE_CONFIDENCE", 0.95)
QUEUE_MIN_CONFIDENCE = _float("CONSOLIDATION_QUEUE_MIN_CONFIDENCE", 0.60)
