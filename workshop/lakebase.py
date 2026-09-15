"""Shared Lakebase connection helper — the one place credentials are handled.

Design points (each one is a failure we are deliberately avoiding):

* **Notebook-native auth.** Inside a Databricks notebook ``WorkspaceClient()`` uses the
  notebook's own identity — no local CLI profile, no stored secret. Participants who have
  browser workspace access can connect without any local setup. ``cfg.profile`` is only for
  a facilitator running the CLI from a laptop; it is never needed here.
* **A fresh database credential on every connect.** Lakebase OAuth DB credentials expire after
  one hour. We never cache a token for the process lifetime — we mint one per connection, so a
  notebook left idle over lunch reconnects cleanly instead of failing auth on a stale token.
* **Retry through scale-to-zero.** A scaled-to-zero endpoint wakes in ~100 ms but the first
  connection can be refused. We retry with a short backoff rather than surfacing a cold start as
  an error.

Connection internals (SDK credential call, endpoint host shape) are exercised end to end in the
dry run on a Databricks test workspace, which runs in-notebook where the Databricks SDK is present.

Usage:

    from workshop.lakebase import connect, run
    with connect() as conn:                  # app project
        rows = run("select 1", conn=conn)
    run("select count(*) from information_schema.tables")   # opens/closes its own connection
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Any, Optional, Sequence

import psycopg  # psycopg 3 — in a notebook: %pip install "psycopg[binary]"

from workshop.config import cfg

_w = None


def _client():
    """Lazily created WorkspaceClient using the notebook's ambient identity."""
    global _w
    if _w is None:
        from databricks.sdk import WorkspaceClient
        _w = WorkspaceClient()
    return _w


def _endpoint_host(endpoint_path: str) -> str:
    ep = _client().postgres.get_endpoint(name=endpoint_path)
    d = ep.as_dict()
    try:
        return d["status"]["hosts"]["host"]
    except (KeyError, TypeError) as e:  # shape drift — fail loudly with what we saw
        raise RuntimeError(f"could not read endpoint host from get_endpoint({endpoint_path}): {d}") from e


def _fresh_token(endpoint_path: str) -> str:
    cred = _client().postgres.generate_database_credential(endpoint_path)
    token = getattr(cred, "token", None) or cred.as_dict().get("token")
    if not token:
        raise RuntimeError(f"generate_database_credential returned no token for {endpoint_path}")
    return token


def _pg_user() -> str:
    """The Databricks identity is the Postgres role name (the user's login name)."""
    return _client().current_user.me().user_name


def connect(endpoint_path: Optional[str] = None,
            database: Optional[str] = None,
            *, retries: int = 3, timeout: int = 15) -> "psycopg.Connection":
    """Open a psycopg connection to a Lakebase endpoint with a freshly minted credential.

    Defaults to the application project in ``config.py``. Pass ``cfg.search_endpoint_path`` for
    the Search demo project. Retries through a scale-to-zero cold start.
    """
    endpoint_path = endpoint_path or cfg.endpoint_path
    database = database or cfg.database
    host = _endpoint_host(endpoint_path)
    user = _pg_user()

    last: Optional[Exception] = None
    for attempt in range(retries):
        token = _fresh_token(endpoint_path)  # fresh each attempt — never a stale token
        try:
            return psycopg.connect(
                host=host, dbname=database, user=user, password=token,
                sslmode="require", connect_timeout=timeout,
            )
        except psycopg.OperationalError as e:  # cold start / transient wake
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(
        f"could not connect to {endpoint_path} after {retries} attempts "
        f"(endpoint may be waking from scale-to-zero): {last}"
    )


def connect_search(**kw) -> "psycopg.Connection":
    """Connect to the dedicated Lakebase Search demo project (never a team project)."""
    if cfg.search_project_id.startswith("REPLACE_ME"):
        raise ValueError("search_project_id is not set in config.py — module 04 needs the "
                         "dedicated Search demo project.")
    return connect(endpoint_path=cfg.search_endpoint_path, **kw)


def run(sql: str, params: Optional[Sequence[Any]] = None, *,
        fetch: bool = True, conn: "Optional[psycopg.Connection]" = None):
    """Execute SQL. Opens and closes its own connection unless one is passed in.

    Returns a list of rows for a SELECT (``fetch=True``); commits and returns rowcount otherwise.
    """
    own = conn is None
    c = conn or connect()
    try:
        with c.cursor() as cur:
            cur.execute(sql, params)
            if fetch and cur.description is not None:
                return cur.fetchall()
            if own:
                c.commit()
            return cur.rowcount
    finally:
        if own:
            c.close()


@contextmanager
def connection(**kw):
    """Context manager yielding a connection that is always closed."""
    c = connect(**kw)
    try:
        yield c
    finally:
        c.close()


def whoami() -> dict:
    """Quick identity + endpoint sanity check for the validation notebook."""
    return {
        "user": _pg_user(),
        "app_endpoint": cfg.endpoint_path,
        "database": cfg.database,
    }
