#!/usr/bin/env python3
"""Hook launcher. Run with `python3 -I` so no environment variable or current directory
can inject modules; the package is loaded only from this repository."""
import os
import sys

_HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from subagent_verify.hook import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
