#!/bin/bash
# Double-click to start Rinkside; it opens in your browser. Close this window to stop it.
cd "$(dirname "$0")"
RINKSIDE_OPEN=1 exec python3 server.py
