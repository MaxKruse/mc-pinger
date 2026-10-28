"""mc-ping-logger configuration.

All knobs are environment variables; see README.md for the full list and
defaults.
"""

import os

LOG_DIR = os.environ.get("LOG_DIR", "/data" if os.path.isdir("/data") else "data")
TARGET_HOST = os.environ.get("TARGET_HOST", "169.155.120.31")
TARGET_PORT = int(os.environ.get("TARGET_PORT", "9080"))
INTERVAL_MIN = int(os.environ.get("INTERVAL_MINUTES", "30"))
# Comma-separated control hosts: they separate "your network is bad" from
# "the server is bad" when the target is unreachable.
CONTROL_HOSTS = [h.strip() for h in os.environ.get("CONTROL_HOSTS", "1.1.1.1").split(",") if h.strip()]
CONTROL_PORT = int(os.environ.get("CONTROL_PORT", "443"))
# Protocol version in the status-ping handshake (769 = 1.21.x). Servers
# generally accept any version for status pings, so this rarely matters.
MC_PROTOCOL_VERSION = int(os.environ.get("MC_PROTOCOL_VERSION", "769"))
KEEP_DAYS = int(os.environ.get("KEEP_DAYS", "90"))
MAX_CYCLES = int(os.environ.get("MAX_CYCLES", "0"))  # 0 = run forever
TEST_TIMEOUT = float(os.environ.get("TEST_TIMEOUT_SECONDS", "10"))
TRACE_TIMEOUT = float(os.environ.get("TRACE_TIMEOUT_SECONDS", "90"))
