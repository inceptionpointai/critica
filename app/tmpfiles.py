"""Temp-file lifecycle helpers — same shape as qualitas/app/tmpfiles.py."""
import logging
import time
from contextlib import contextmanager
from pathlib import Path

from . import config

log = logging.getLogger("critica.tmpfiles")


def _under_tmp(path) -> bool:
    p = Path(path).resolve()
    return str(p).startswith(str(config.TMP_DIR.resolve()))


@contextmanager
def managed(path: str):
    """Yields the path; deletes on exit IFF it lives under TMP_DIR.
    Files passed in by callers (file:// or bare paths NOT under TMP_DIR)
    are never touched."""
    try:
        yield path
    finally:
        if _under_tmp(path):
            try:
                Path(path).unlink(missing_ok=True)
            except Exception as e:
                log.warning("failed to clean up %s: %s", path, e)


def sweep_orphans() -> None:
    """Startup sweep: delete temp files older than TTL.
    Bounds the impact of any past crashes that left files behind."""
    cutoff = time.time() - config.TMP_TTL_S
    swept = 0
    for f in config.TMP_DIR.glob("c_*"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink(missing_ok=True)
                swept += 1
        except OSError:
            continue
    if swept:
        log.info("startup sweep removed %d orphan temp files", swept)
