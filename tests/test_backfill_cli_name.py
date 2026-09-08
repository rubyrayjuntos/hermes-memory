"""Old console script name must not silently run ABOUT backfill (#73)."""
from hermes_memory.backfill import deprecated_main


def test_deprecated_backfill_name_exits_2(capsys):
    assert deprecated_main([]) == 2
    err = capsys.readouterr().err
    assert "hermes-memory-backfill-about" in err
    assert "replay_conversation_manifold.py --live" in err
