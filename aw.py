#!/usr/bin/env python3
"""Run the CLI from any directory: python <path>/aw.py status"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ai_workflow.cli import main  # noqa: E402

if __name__ == '__main__':
    sys.exit(main())
