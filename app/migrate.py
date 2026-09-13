"""Применение SQL-миграций. Все файлы идемпотентны, выполняются при каждом старте."""
import glob
import os

from common import db, log

SQL_DIR = os.path.join(os.path.dirname(__file__), "sql")


def migrate():
    files = sorted(glob.glob(os.path.join(SQL_DIR, "*.sql")))
    with db() as conn:
        for path in files:
            with open(path, encoding="utf-8") as f:
                sql = f.read()
            try:
                conn.execute(sql)
                log.info("Миграция применена: %s", os.path.basename(path))
            except Exception as e:  # noqa: BLE001
                log.error("Миграция %s: %s", os.path.basename(path), e)
                raise


if __name__ == "__main__":
    migrate()
