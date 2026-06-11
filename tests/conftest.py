"""Shared test bootstrap.

app.config reads env exactly once, at first import. Each test module
historically did its own ``os.environ.setdefault(...)`` before importing
the app, which works per-file but breaks full-suite runs: whichever test
module pytest imports first freezes the *other* module's variable as ""
(e.g. test_review_routes imports app.config before test_megaphone has a
chance to set MEGAPHONE_API_TOKEN → 3 megaphone tests fail, and vice
versa for CRITICA_API_KEYS → every authed route test 401s). conftest.py
is imported before any test module, so set both here.
"""
import os

os.environ.setdefault("CRITICA_API_KEYS", "test-key")
os.environ.setdefault("MEGAPHONE_API_TOKEN", "test-megaphone-token")
