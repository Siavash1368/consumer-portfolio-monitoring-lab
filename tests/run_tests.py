"""Standard-library test runner for hosts without pytest.

    python tests/run_tests.py

Discovers tests/test_*.py and runs every test_* function, treating
unittest.SkipTest as a skip - the same contract pytest uses, so the suite
runs identically under `pytest` and under this script.
"""
import importlib
import os
import sys
import time
import traceback
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import _paths  # noqa: E402,F401

passed = failed = skipped = 0
t0 = time.time()
for fname in sorted(f for f in os.listdir(HERE) if f.startswith("test_") and f.endswith(".py")):
    mod = importlib.import_module(fname[:-3])
    for name in sorted(n for n in dir(mod) if n.startswith("test_")):
        try:
            getattr(mod, name)()
            passed += 1
            print(f"PASS  {fname}::{name}")
        except unittest.SkipTest as e:
            skipped += 1
            print(f"SKIP  {fname}::{name}  ({e})")
        except Exception:
            failed += 1
            print(f"FAIL  {fname}::{name}")
            traceback.print_exc()
print(f"\n{passed} passed, {failed} failed, {skipped} skipped in {time.time() - t0:.1f}s")
sys.exit(1 if failed else 0)
