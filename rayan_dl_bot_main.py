#!/usr/bin/env python3
"""Entry point for Rayan Downloader Bot."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from rayan_dl_bot.bot import main

if __name__ == "__main__":
    main()