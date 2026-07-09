#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Small Feishu Bitable OpenAPI client for table refresh jobs."""
import asyncio
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional

import httpx

from .config import settings
from .logger import get_logger

logger = get_logger("feishu_bitable")


class FeishuBitableClient:
    def __init__(self, app_token: Optional[str] = None) -> None:
        self.app_token = app_token or settings.FEISHU_APP_TOKEN
        self.api_base = settings.FEISHU_API_BASE.rstrip("/")
        self.app_id = settings.FEISHU_APP_ID
        self.app_secret = settings.FEISHU_APP_SECRET
        self._tenant_access_token: Optional[str] = None

    async def get_tenant_access_token(self, retry_count: int = 3) -> str:
        if self._tenant_access_token:
            return self._tenant_access_token

        url = f"{self.api_base}/auth/v3/tenant_access_token/internal"
        payload = {"app_id": self.app_id, "app_secret": self.app_secret}
        headers = {"Content-Type": "application/json; charset=utf-8"}
        last_error: Optional[Exception] = None

        for attempt in range(1, retry_count + 1):
            try:
                timeout = httpx.Timeout(60.0, connect=10.0)
                async with httpx.AsyncClient(timeout=timeout) as client:
                    response = await client.post(url, headers=headers, json=payload)
                    result = response.json()
                if result.get("code") == 0:
                    self._tenant_access_token = result["tenant_access_token"]
                    logger.info("获取飞书 tenant_access_token 成功")
                    return self._tenant_access_token
                raise RuntimeError(f"获取飞书 token 失败: {result}")
            except Exception as exc:
                last_error = exc
                if attempt < retry_count:
                    wait_seconds = attempt * 2
                    logger.warning(f"获取飞书 token 失败，{wait_seconds} 秒后重试({attempt}/{retry_count}): {exc}")
                    await asyncio.sleep(wait_seconds)
                else:
                    logger.error(f"获取飞书 token 失败，已重试 {retry_count} 次")

        raise RuntimeError(f"获取飞书 token 失败: {last_error}")

    async def _headers(self) -> Dict[str, str]:
        token = await self.get_tenant_access_token()
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        }

    async def list_tables(self) -> Dict[str, str]:
        url = f"{self.api_base}/bitable/v1/apps/{self.app_token}/tables"
        headers = await self._headers()
        table_map: Dict[str, str] = {}
        page_token: Optional[str] = None
        timeout = httpx.Timeout(60.0, connect=10.0)

        while True:
            params: Dict[str, Any] = {"page_size": 100}
            if page_token:
                params["page_token"] = page_token
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.get(url, headers=headers, params=params)
                result = response.json()
            if result.get("code") != 0:
                raise RuntimeError(f"获取飞书数据表列表失败: {result}")
            data = result.get("data") or {}
            for table in data.get("items") or []:
                table_map[table.get("name", "")] = table.get("table_id", "")
            if not data.get("has_more"):
                break
            page_token = data.get("page_token")
            if not page_token:
                break

        logger.info(f"获取到 {len(table_map)} 个飞书数据表")
        return table_map

    async def resolve_table_id(self, table_id: str = "", table_name: str = "") -> str:
        if table_id:
            return table_id
        if not table_name:
            raise ValueError("table_id 为空时必须配置 table_name")
        tables = await self.list_tables()
        resolved = tables.get(table_name)
        if not resolved:
            available = ", ".join(sorted(tables.keys())) or "无"
            raise RuntimeError(f"未找到飞书数据表: {table_name}。当前 app 下已有表: {available}")
        logger.info(f"飞书数据表 {table_name} => {resolved}")
        return resolved

    async def list_fields(self, table_id: str) -> Dict[str, Dict[str, Any]]:
        url = f"{self.api_base}/bitable/v1/apps/{self.app_token}/tables/{table_id}/fields"
        headers = await self._headers()
        fields: Dict[str, Dict[str, Any]] = {}
        page_token: Optional[str] = None
        timeout = httpx.Timeout(60.0, connect=10.0)

        while True:
            params: Dict[str, Any] = {"page_size": 100}
            if page_token:
                params["page_token"] = page_token
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.get(url, headers=headers, params=params)
                result = response.json()
            if result.get("code") != 0:
                raise RuntimeError(f"获取飞书字段失败: {result}")
            data = result.get("data") or {}
            for field in data.get("items") or []:
                fields[field.get("field_name", "")] = field
            if not data.get("has_more"):
                break
            page_token = data.get("page_token")
            if not page_token:
                break

        logger.info(f"飞书表 {table_id} 当前字段数: {len(fields)}")
        return fields

    async def create_field(self, table_id: str, field_name: str, field_type: int, precision: int = 0) -> str:
        url = f"{self.api_base}/bitable/v1/apps/{self.app_token}/tables/{table_id}/fields"
        headers = await self._headers()
        payload: Dict[str, Any] = {"field_name": field_name, "type": field_type}
        if field_type == 2:
            payload["property"] = {"precision": precision, "formatter": "0"}

        timeout = httpx.Timeout(60.0, connect=10.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(url, headers=headers, json=payload)
            result = response.json()

        if result.get("code") == 0:
            field_id = (result.get("data") or {}).get("field", {}).get("field_id", "")
            logger.info(f"创建字段成功: {field_name} ({field_id})")
            return field_id

        msg = str(result.get("msg", ""))
        if "already" in msg.lower() or "exist" in msg.lower() or "重复" in msg or "已存在" in msg:
            logger.warning(f"字段已存在，跳过创建: {field_name}")
            return ""
        raise RuntimeError(f"创建字段失败 {field_name}: {result}")

    async def ensure_fields(self, table_id: str, field_specs: List[Dict[str, Any]]) -> None:
        existing = await self.list_fields(table_id)
        existing_names = set(existing.keys())
        for spec in field_specs:
            name = spec["name"]
            if name in existing_names:
                continue
            await self.create_field(
                table_id=table_id,
                field_name=name,
                field_type=int(spec.get("type", 1)),
                precision=int(spec.get("precision", 0)),
            )

    async def list_record_ids(self, table_id: str) -> List[str]:
        url = f"{self.api_base}/bitable/v1/apps/{self.app_token}/tables/{table_id}/records"
        headers = await self._headers()
        record_ids: List[str] = []
        page_token: Optional[str] = None
        timeout = httpx.Timeout(120.0, connect=30.0)

        while True:
            params: Dict[str, Any] = {"page_size": 500}
            if page_token:
                params["page_token"] = page_token
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.get(url, headers=headers, params=params)
                result = response.json()
            if result.get("code") != 0:
                raise RuntimeError(f"获取飞书记录失败: {result}")
            data = result.get("data") or {}
            for record in data.get("items") or []:
                record_id = record.get("record_id")
                if record_id:
                    record_ids.append(record_id)
            if not data.get("has_more"):
                break
            page_token = data.get("page_token")
            if not page_token:
                break

        logger.info(f"飞书表 {table_id} 当前记录数: {len(record_ids)}")
        return record_ids

    async def delete_all_records(self, table_id: str) -> int:
        record_ids = await self.list_record_ids(table_id)
        if not record_ids:
            logger.info("飞书表无旧记录，无需清空")
            return 0

        url = f"{self.api_base}/bitable/v1/apps/{self.app_token}/tables/{table_id}/records/batch_delete"
        headers = await self._headers()
        timeout = httpx.Timeout(120.0, connect=30.0)
        deleted = 0

        for batch in _chunks(record_ids, 500):
            payload = {"records": batch}
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(url, headers=headers, json=payload)
                result = response.json()
            if result.get("code") != 0:
                raise RuntimeError(f"批量删除飞书记录失败: {result}")
            deleted += len(batch)
            logger.info(f"已删除飞书旧记录: {deleted}/{len(record_ids)}")
        return deleted

    async def batch_create_records(self, table_id: str, records: List[Dict[str, Any]], batch_size: int = 500) -> int:
        if not records:
            logger.warning("没有需要写入飞书的记录")
            return 0

        url = f"{self.api_base}/bitable/v1/apps/{self.app_token}/tables/{table_id}/records/batch_create"
        headers = await self._headers()
        timeout = httpx.Timeout(120.0, connect=30.0)
        written = 0

        for batch in _chunks(records, batch_size):
            payload = {"records": [{"fields": self.to_feishu_fields(row)} for row in batch]}
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(url, headers=headers, json=payload)
                result = response.json()
            if result.get("code") != 0:
                raise RuntimeError(f"批量写入飞书记录失败: {result}")
            written += len(batch)
            logger.info(f"已写入飞书记录: {written}/{len(records)}")
        return written

    def to_feishu_fields(self, row: Dict[str, Any]) -> Dict[str, Any]:
        return {key: self._convert_value(value) for key, value in row.items()}

    @staticmethod
    def _convert_value(value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, Decimal):
            return float(value)
        if isinstance(value, datetime):
            return int(value.timestamp() * 1000)
        if isinstance(value, date):
            dt = datetime.combine(value, datetime.min.time())
            return int(dt.timestamp() * 1000)
        if isinstance(value, (int, float, str, bool, list, dict)):
            return value
        return str(value)



def _chunks(items: List[Any], size: int) -> Iterable[List[Any]]:
    for index in range(0, len(items), size):
        yield items[index:index + size]
