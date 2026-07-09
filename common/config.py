#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Project configuration loaded from environment variables and .env."""
import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = BASE_DIR / ".env"

if ENV_FILE.exists():
    load_dotenv(ENV_FILE)
else:
    print(f"警告: .env 文件不存在于 {ENV_FILE}，请复制 config.example.env 为 .env 后再配置")


class Settings:
    def __init__(self) -> None:
        self.BASE_DIR = BASE_DIR
        self.ENVIRONMENT = os.getenv("ENVIRONMENT", "production")
        self.LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
        self.LOG_DIR = BASE_DIR / os.getenv("LOG_DIR", "logs")

        self.DB_HOST = os.getenv("DB_HOST", "localhost")
        self.DB_PORT = int(os.getenv("DB_PORT", "3306"))
        self.DB_USER = os.getenv("DB_USER", "")
        self.DB_PASSWORD = os.getenv("DB_PASSWORD", "")
        self.DB_DATABASE = os.getenv("DB_DATABASE", "ods")
        self.DB_CHARSET = os.getenv("DB_CHARSET", "utf8mb4")
        self.DB_CONNECT_TIMEOUT = int(os.getenv("DB_CONNECT_TIMEOUT", "10"))
        self.DB_READ_TIMEOUT = int(os.getenv("DB_READ_TIMEOUT", "600"))
        self.DB_WRITE_TIMEOUT = int(os.getenv("DB_WRITE_TIMEOUT", "600"))

        self.SOURCE_TABLE = os.getenv("SOURCE_TABLE", "ods_lx_product_performance")
        self.DATE_COLUMN = os.getenv("DATE_COLUMN", "dt")
        self.WINDOW_MODE = os.getenv("WINDOW_MODE", "latest_date")

        self.FEISHU_APP_ID = os.getenv("FEISHU_APP_ID", "")
        self.FEISHU_APP_SECRET = os.getenv("FEISHU_APP_SECRET", "")
        self.FEISHU_APP_TOKEN = os.getenv("FEISHU_APP_TOKEN", "REclw3BsTiCMFcknZfIcdbySn1b")
        self.FEISHU_API_BASE = os.getenv("FEISHU_API_BASE", "https://open.feishu.cn/open-apis")
        self.FEISHU_90D_TABLE_ID = os.getenv("FEISHU_90D_TABLE_ID", "")
        self.FEISHU_90D_TABLE_NAME = os.getenv("FEISHU_90D_TABLE_NAME", "90天数据")
        self.FEISHU_7D_TABLE_ID = os.getenv("FEISHU_7D_TABLE_ID", "")
        self.FEISHU_7D_TABLE_NAME = os.getenv("FEISHU_7D_TABLE_NAME", "7天数据")
        self.MAX_FEISHU_RECORDS = int(os.getenv("MAX_FEISHU_RECORDS", "0"))

        self.LOG_DIR.mkdir(parents=True, exist_ok=True)

    @property
    def db_config(self) -> dict:
        return {
            "host": self.DB_HOST,
            "port": self.DB_PORT,
            "user": self.DB_USER,
            "password": self.DB_PASSWORD,
            "database": self.DB_DATABASE,
            "charset": self.DB_CHARSET,
            "connect_timeout": self.DB_CONNECT_TIMEOUT,
            "read_timeout": self.DB_READ_TIMEOUT,
            "write_timeout": self.DB_WRITE_TIMEOUT,
            "autocommit": False,
        }

    def validate(self) -> bool:
        errors = []
        if not self.DB_HOST:
            errors.append("DB_HOST 未配置")
        if not self.DB_USER:
            errors.append("DB_USER 未配置")
        if not self.DB_PASSWORD:
            errors.append("DB_PASSWORD 未配置")
        if not self.DB_DATABASE:
            errors.append("DB_DATABASE 未配置")
        if not self.FEISHU_APP_ID:
            errors.append("FEISHU_APP_ID 未配置")
        if not self.FEISHU_APP_SECRET:
            errors.append("FEISHU_APP_SECRET 未配置")
        if not self.FEISHU_APP_TOKEN:
            errors.append("FEISHU_APP_TOKEN 未配置")
        if not self.SOURCE_TABLE:
            errors.append("SOURCE_TABLE 未配置")
        if not self.DATE_COLUMN:
            errors.append("DATE_COLUMN 未配置")
        if self.WINDOW_MODE not in {"latest_date", "current_date"}:
            errors.append("WINDOW_MODE 只能是 latest_date 或 current_date")
        if not self.FEISHU_90D_TABLE_ID and not self.FEISHU_90D_TABLE_NAME:
            errors.append("FEISHU_90D_TABLE_ID / FEISHU_90D_TABLE_NAME 至少配置一个")
        if not self.FEISHU_7D_TABLE_ID and not self.FEISHU_7D_TABLE_NAME:
            errors.append("FEISHU_7D_TABLE_ID / FEISHU_7D_TABLE_NAME 至少配置一个")

        if errors:
            print("配置错误:")
            for error in errors:
                print(f"  - {error}")
            return False
        return True


settings = Settings()
