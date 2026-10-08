"""Shared test fixtures.

These tests target the parts of the sample that are pure/stateless or
easily mockable — fee math, country→platform resolution, token-bucket
rate limiter — so a reviewer can clone, ``pip install -r
requirements.txt && pytest`` and get a green run without having to
provision MySQL, Apple certs or a Stripe account.
"""
import os
import sys


# Ensure the ``app`` package resolves whether tests are run from the repo
# root or from inside the ``tests/`` directory.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# Minimum config env vars so ``app.config.Settings`` validates when
# imported transitively by the modules under test.
os.environ.setdefault("MYSQL_HOST", "localhost")
os.environ.setdefault("MYSQL_USER", "test")
os.environ.setdefault("MYSQL_PASSWORD", "test")
os.environ.setdefault("MYSQL_DB", "test")
os.environ.setdefault("SECRET_KEY", "test_secret")
# A valid Fernet key — needed because app.utils.encryption instantiates
# ``Fernet(settings.ENC_SECURE_KEY)`` at import time.
os.environ.setdefault("ENC_SECURE_KEY", "nYFVpRTKH3yW26Q4tX50Ke4m7hGZfTWqdFIvDnTQQVg=")
