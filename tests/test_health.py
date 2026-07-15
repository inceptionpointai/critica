"""Tests for /health/ready across all four LLM_BACKEND values.

Regression coverage for the bug where LLM_BACKEND=vertex fell through
readiness() with no matching branch -> {"status": "not_ready",
"reason": "invalid LLM_BACKEND='vertex'"} even when correctly configured,
so GKE pods would never go Ready under the vertex backend.

config.py reads env once at import time into plain module attributes;
these tests monkeypatch those attributes directly (not the environment)
since readiness()/_llm_label() read config.<NAME> fresh on every call.
"""
import os

# Auth is gated by CRITICA_API_KEYS — set it before importing the app.
os.environ.setdefault("CRITICA_API_KEYS", "test-key")

from fastapi.testclient import TestClient  # noqa: E402

from app import config  # noqa: E402
from app.main import app  # noqa: E402

client = TestClient(app)


def test_ready_bedrock_missing_token(monkeypatch):
    monkeypatch.setattr(config, "LLM_BACKEND", "bedrock")
    monkeypatch.delenv("AWS_WEB_IDENTITY_TOKEN_FILE", raising=False)
    resp = client.get("/health/ready")
    assert resp.json()["status"] == "not_ready"


def test_ready_vertex_missing_project(monkeypatch):
    monkeypatch.setattr(config, "LLM_BACKEND", "vertex")
    monkeypatch.setattr(config, "GOOGLE_CLOUD_PROJECT", "")
    resp = client.get("/health/ready")
    body = resp.json()
    assert body == {"status": "not_ready", "reason": "GOOGLE_CLOUD_PROJECT unset"}


def test_ready_vertex_with_project(monkeypatch):
    monkeypatch.setattr(config, "LLM_BACKEND", "vertex")
    monkeypatch.setattr(config, "GOOGLE_CLOUD_PROJECT", "ipointai-staging")
    resp = client.get("/health/ready")
    assert resp.json() == {
        "status": "ready",
        "backend": "vertex",
        "project": "ipointai-staging",
    }


def test_ready_anthropic_with_key(monkeypatch):
    monkeypatch.setattr(config, "LLM_BACKEND", "anthropic")
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "sk-test")
    resp = client.get("/health/ready")
    assert resp.json() == {"status": "ready", "backend": "anthropic"}


def test_ready_invalid_backend(monkeypatch):
    monkeypatch.setattr(config, "LLM_BACKEND", "not-a-real-backend")
    resp = client.get("/health/ready")
    body = resp.json()
    assert body["status"] == "not_ready"
    assert "invalid LLM_BACKEND" in body["reason"]


def test_llm_label_vertex(monkeypatch):
    monkeypatch.setattr(config, "LLM_BACKEND", "vertex")
    monkeypatch.setattr(config, "GOOGLE_CLOUD_PROJECT", "ipointai-prod")
    monkeypatch.setattr(config, "VERTEX_MODEL", "claude-opus-4-8")
    resp = client.get("/health")
    assert resp.json()["llm"] == "claude-opus-4-8"
