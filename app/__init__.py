"""Critica — editorial-quality review service for podcast episodes."""
import logging
import os

__version__ = "1.1.0"


def _configure_logging() -> None:
    """Attach a stream handler at module-import time so uvicorn workers
    pick it up. The Dockerfile CMD calls `uvicorn` directly — anything in
    `main()` runs only under `python -m app.main`, not in production."""
    log = logging.getLogger("critica")
    if log.handlers:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s"
    ))
    log.addHandler(handler)
    level_name = os.environ.get("CRITICA_LOG_LEVEL", "INFO").upper()
    log.setLevel(getattr(logging, level_name, logging.INFO))
    log.propagate = False


_configure_logging()
