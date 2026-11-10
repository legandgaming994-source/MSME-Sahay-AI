"""Backend selection shared by the MSME database tests (same convention as test_camt_import).

Default: a temporary SQLite file. With LAMBDA_ERP_TEST_DB=postgresql://... the public schema is dropped and
recreated, so the same test runs unchanged on PostgreSQL.
"""

import os
import tempfile


def reset_test_db(prefix: str) -> str:
    url = os.environ.get("LAMBDA_ERP_TEST_DB")
    if not url:
        fd, path = tempfile.mkstemp(suffix=".db", prefix=prefix)
        os.close(fd)
        return path
    import psycopg

    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
    return url


def assert_backend(expected_postgres: bool) -> None:
    """Fail loudly if the active database is not the one the run was asked to use."""
    from lambda_erp.database import get_db

    dialect = get_db().dialect
    want = "postgres" if expected_postgres else "sqlite"
    if dialect != want:
        raise SystemExit(f"Test ran on {dialect!r} but {want!r} was requested (LAMBDA_ERP_TEST_DB).")
    print(f"INFO  database backend: {dialect}")
