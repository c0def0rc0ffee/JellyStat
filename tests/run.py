#!/usr/bin/env python3
"""
<summary>
Run every tests/test_*.py in its own interpreter and stop at the first failure.
</summary>
<remarks>
Each test file is a script that exits non zero when a check fails, so the
runner only has to run them one after another and pass the first bad exit
code on. One interpreter per file, because each installs its own Kodi stubs
into sys.modules and expects a fresh import of the add-on.

Usage:
    python3 tests/run.py
</remarks>
"""

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    """
    <summary>
    Run the test files in name order; return the first non zero exit code, or 0.
    </summary>
    <returns>The exit code for the process.</returns>
    """
    names = sorted(name for name in os.listdir(HERE)
                   if name.startswith("test_") and name.endswith(".py"))
    for name in names:
        print("==== %s" % name)
        sys.stdout.flush()
        result = subprocess.run([sys.executable, os.path.join(HERE, name)])
        if result.returncode:
            print("tests: %s failed with exit code %d" % (name, result.returncode))
            return result.returncode
    print("tests: all %d files passed" % len(names))
    return 0


if __name__ == "__main__":
    sys.exit(main())
