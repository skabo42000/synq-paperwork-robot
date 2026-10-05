"""Shared test setup: runs before any test file is imported."""

import os

# app.py builds the real app on import and refuses to start without a password.
# Tests never use the real password (or .env); they build their own app with a fake store.
os.environ.setdefault("PORTAL_PASSWORD", "test-password-not-real")
