"""Application logging setup shared by the API and command-line entry points."""
from __future__ import annotations

import logging
import os


def configure_logging() -> None:
    """Configure concise console logging, controlled by CHATBOT_LOG_LEVEL."""
    level_name = os.getenv("CHATBOT_LOG_LEVEL", "INFO").strip().upper()
    level = logging.getLevelName(level_name)
    if not isinstance(level, int):
        raise ValueError(f"Invalid CHATBOT_LOG_LEVEL: {level_name!r}")

    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger().setLevel(level)
