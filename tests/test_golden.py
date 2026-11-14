"""The generated config from the fixed test data (seed.py) must not change
at all. If a change is meant: python tests/seed.py --update, and review."""
import difflib
import os

import seed


def test_generated_config_unchanged(env):
    seed.seed()
    got = seed.build_all()
    diffs = []
    for f in seed.FILES:
        want = open(os.path.join(seed.GOLDEN, f)).read()
        if got[f] != want:
            diffs += list(difflib.unified_diff(want.splitlines(True), got[f].splitlines(True), f"golden/{f}", f"new/{f}"))
    assert not diffs, "".join(diffs[:80])


def test_migrations_run_twice(env):
    seed.seed()
    import pbx, billing
    pbx.get_db(); billing.get_db()  # a second open must change nothing
    assert seed.build_all()["pjsip_extensions.conf"] == open(os.path.join(seed.GOLDEN, "pjsip_extensions.conf")).read()
