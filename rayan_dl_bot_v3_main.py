#!/usr/bin/env python3
"""Entry point that imports the v3 bot main()."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from rayan_dl_bot_v3.bot import main

if __name__ == "__main__":
    main()
