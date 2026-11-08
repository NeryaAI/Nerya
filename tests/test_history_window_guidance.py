from pathlib import Path


def test_history_inventory_guidance_preserves_admitted_window():
    root = Path(__file__).resolve().parents[1] / 'nerya/skills/builtin/backtest'
    skill = (root / 'SKILL.md').read_text()
    reference = (root / 'references/history-data.md').read_text()
    assert 'propagate the same admitted window' in skill
    assert 'references/history-data.md' in skill
    assert 'inclusive `start` and exclusive `end` unchanged' in reference
    assert 'explicit `days`' in reference
    assert "Compare each dataset's returned `start`/`end`" in reference
    assert 'read-only inspection, not relabelling the receipt' in reference
    assert 'neither provider support nor protocol state/fee replay capability' in reference
