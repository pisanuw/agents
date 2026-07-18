"""Daily rotating-by-name log file + stderr, UTC, line format: ISO LEVEL component msg."""
from __future__ import annotations

import logging
import sys
import time

from cagent import clock, config

LOGS = config.REPO_ROOT / "logs"
_configured = False


def setup() -> logging.Logger:
    global _configured
    logger = logging.getLogger("cagent")
    if _configured:
        return logger
    LOGS.mkdir(parents=True, exist_ok=True)
    path = LOGS / f"tick-{clock.today()}.log"
    fmt = logging.Formatter("%(asctime)s %(levelname)s cagent %(message)s",
                            datefmt="%Y-%m-%dT%H:%M:%SZ")
    fmt.converter = time.gmtime
    logger.setLevel(logging.INFO)
    fh = logging.FileHandler(path)
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    _configured = True
    return logger
