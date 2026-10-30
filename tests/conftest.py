"""Test setup.

App tests need a database loaded with db/schema.sql, db/seed.sql and
db/demo.sql. Point TEST_DATABASE_URL at it:

    createdb nbc_test
    psql nbc_test -f db/schema.sql -f db/seed.sql -f db/demo.sql
    TEST_DATABASE_URL=postgresql:///nbc_test pytest

Without TEST_DATABASE_URL the database tests are skipped. Tests that create
accounts use unique emails, so the database can be reused between runs.
"""

from __future__ import annotations

import os
import re

import pytest

from app import create_app

DEMO_PASSWORD = "demo-password"


@pytest.fixture(scope="session")
def database_url() -> str:
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not set")
    return url


@pytest.fixture()
def app(database_url):
    return create_app({"APP_ENV": "local", "DATABASE_URL": database_url, "TESTING": True})


@pytest.fixture()
def client(app):
    return app.test_client()


def csrf_from(client, path: str) -> str:
    html = client.get(path).get_data(as_text=True)
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


@pytest.fixture()
def post_form(client):
    """POST a form the way a browser would: load the page first for its CSRF token."""
    def _post(path: str, data: dict, **kwargs):
        return client.post(path, data={**data, "csrf_token": csrf_from(client, path)}, **kwargs)
    return _post


@pytest.fixture()
def sign_in(client, post_form):
    """Sign in as someone (signing out whoever was signed in first)."""
    def _sign_in(email: str, password: str = DEMO_PASSWORD):
        with client.session_transaction() as sess:
            sess.clear()
        return post_form("/sign-in", {"email": email, "password": password})
    return _sign_in
