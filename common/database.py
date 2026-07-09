#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MySQL connection helpers."""
from contextlib import contextmanager
import re
from typing import Iterator

import pymysql

from .config import settings
from .logger import get_logger

logger = get_logger("database")

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_]+$")


def quote_identifier(name: str) -> str:
    """Safely quote a simple MySQL identifier from configuration."""
    if not _IDENTIFIER_RE.match(name or ""):
        raise ValueError(f"非法 MySQL 标识符: {name!r}。只允许字母、数字、下划线。")
    return f"`{name}`"


def get_db_connection():
    try:
        conn = pymysql.connect(**settings.db_config)
        logger.debug(f"连接到数据库: {settings.DB_HOST}:{settings.DB_PORT}/{settings.DB_DATABASE}")
        return conn
    except Exception as exc:
        logger.error(f"数据库连接失败: {exc}")
        raise


@contextmanager
def db_cursor(dictionary: bool = True) -> Iterator[pymysql.cursors.Cursor]:
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor_cls = pymysql.cursors.DictCursor if dictionary else pymysql.cursors.Cursor
        cursor = conn.cursor(cursor_cls)
        yield cursor
        conn.commit()
    except Exception as exc:
        if conn:
            conn.rollback()
        logger.error(f"数据库操作失败: {exc}")
        raise
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()
            logger.debug("数据库连接已关闭")
