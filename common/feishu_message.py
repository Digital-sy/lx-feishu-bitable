#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Feishu IM message helpers."""
from __future__ import annotations

import json
from typing import Any, Dict

import httpx

from .feishu_bitable import FeishuBitableClient
from .logger import get_logger

logger = get_logger("feishu_message")


async def send_interactive_card(
    client: FeishuBitableClient,
    receive_id: str,
    receive_id_type: str,
    card: Dict[str, Any],
) -> str:
    """Send an interactive card to one Feishu recipient."""
    receive_id = (receive_id or "").strip()
    receive_id_type = (receive_id_type or "open_id").strip()
    allowed_types = {"open_id", "user_id", "union_id", "email", "chat_id"}

    if not receive_id:
        raise ValueError("飞书消息接收人 ID 不能为空")
    if receive_id_type not in allowed_types:
        raise ValueError(
            "receive_id_type 仅支持: " + ", ".join(sorted(allowed_types))
        )

    token = await client.get_tenant_access_token()
    url = f"{client.api_base}/im/v1/messages"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=utf-8",
    }
    payload = {
        "receive_id": receive_id,
        "msg_type": "interactive",
        "content": json.dumps(card, ensure_ascii=False),
    }

    timeout = httpx.Timeout(60.0, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout) as http_client:
        response = await http_client.post(
            url,
            headers=headers,
            params={"receive_id_type": receive_id_type},
            json=payload,
        )

    try:
        result = response.json()
    except Exception as exc:
        raise RuntimeError(
            f"飞书消息接口返回非 JSON，HTTP {response.status_code}: "
            f"{response.text[:500]}"
        ) from exc

    if result.get("code") != 0:
        raise RuntimeError(f"发送飞书卡片失败: {result}")

    message_id = str(((result.get("data") or {}).get("message_id")) or "")
    logger.info(
        f"飞书卡片发送成功: receive_id_type={receive_id_type}, "
        f"message_id={message_id or '未返回'}"
    )
    return message_id
