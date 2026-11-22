#!/usr/bin/env python3
# Double-click this file to start Binance Price Viewer without a console
# window (.pyw files are opened by the Windows Python launcher, pyw.exe).
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from binance_viewer import main  # noqa: E402

main()
