#!/bin/bash
# US breakout scanner cron wrapper (proxy env + venv python)
export HTTP_PROXY=http://127.0.0.1:7897
export HTTPS_PROXY=http://127.0.0.1:7897
export ALL_PROXY=http://127.0.0.1:7897
exec /home/larryguo/.hermes/hermes-agent/venv/bin/python3 \
  /home/larryguo/Projects/us-breakout-scanner/us_breakout_monitor.py "$@"
