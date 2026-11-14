"""Заглушка pymysql поверх SQLite — только для офлайн-тестов (см. README.md рядом)."""
import os
import re
import sqlite3

from . import constants, err  # noqa: F401


class _Cursor:
    def __init__(self, conn):
        self._c = conn._sq.cursor()
        self._conn = conn

    def execute(self, sql, args=None):
        if args is not None:
            sql = sql.replace("%%", "\0").replace("%s", "?").replace("\0", "%")
        m = re.match(r"\s*SET FOREIGN_KEY_CHECKS\s*=\s*(\d)", sql, re.I)
        if m:
            sql = f"PRAGMA foreign_keys = {'ON' if m.group(1) == '1' else 'OFF'}"
        sql = re.sub(r"\)\s*ENGINE=.*$", ")", sql, flags=re.S)
        sql = sql.replace("INT AUTO_INCREMENT PRIMARY KEY", "INTEGER PRIMARY KEY AUTOINCREMENT")
        self._c.execute(sql, args or ())
        return self

    @property
    def description(self):
        return self._c.description

    @property
    def lastrowid(self):
        return self._c.lastrowid

    @property
    def rowcount(self):
        return self._c.rowcount

    def fetchone(self):
        return self._c.fetchone()

    def fetchall(self):
        return self._c.fetchall()


class _Conn:
    def __init__(self, path):
        self._sq = sqlite3.connect(path)
        self._sq.execute("PRAGMA foreign_keys = ON")
        self._sq.create_function("lower", 1, lambda s: s.lower() if isinstance(s, str) else s)

    def cursor(self):
        return _Cursor(self)

    def commit(self):
        self._sq.commit()

    def rollback(self):
        self._sq.rollback()

    def close(self):
        self._sq.close()


def connect(**kw):
    return _Conn(os.environ["FAKE_MYSQL_PATH"])
