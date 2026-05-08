"""Backward-compat shim. Use `python -m car_replay` instead."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from car_replay.cli import main

if __name__ == "__main__":
    main()
