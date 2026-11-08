"""Offline Skill instruction regression; not proof of model compliance or LP replay."""
from pathlib import Path
import unittest


REFERENCE = (Path(__file__).resolve().parents[1]
             / "nerya/skills/builtin/backtest/references/replay-capabilities.md")


class LPInsufficientDataReferenceTest(unittest.TestCase):
    def test_insufficient_lp_data_has_bounded_recovery_and_honest_handoff(self):
        text = REFERENCE.read_text(encoding="utf-8")
        heading = "## LP history is insufficient"
        self.assertIn(heading, text, "LP-specific recovery branch is missing")
        branch = " ".join(text.split(heading, 1)[1].split("\n## ", 1)[0].split())
        boundaries = {
            "scope and evidence": (
                "original chain/network, pool/market and requested window",
                "pool identity, price history, APY observations",
                "neither price nor APY alone proves a full LP replay",
                "row_count", "truncated", "sample_truncated",
            ),
            "bounded source discovery": (
                "data_api", "catalog/schema", "docs_url",
                "at most one official-documentation search and one fetch",
                "at most one changed query",
                "stop discovery",
            ),
            "truthful source capabilities": (
                "DefiLlama", "DEX", "lending-only",
                "does not establish that all sources lack history",
            ),
            "candidate and delivery": (
                "delivery_start", "strategy_draft_proposal",
                "same candidate", "where feasible",
                "delivery_update", "blocked",
                "missing inputs", "incomplete",
                "disabled", "completion.allowed",
            ),
        }
        for boundary, phrases in boundaries.items():
            with self.subTest(boundary=boundary):
                for phrase in phrases:
                    self.assertIn(phrase, branch)


if __name__ == "__main__":
    unittest.main()
