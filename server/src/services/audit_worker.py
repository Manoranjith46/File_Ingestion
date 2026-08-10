"""Background worker for consuming audit events from Redis and persisting them."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from redis.exceptions import TimeoutError as RedisTimeoutError

from sqlalchemy import select
from sqlalchemy.orm import Session

import redis

from config.database import get_session_local
from config.redis_server import audit_stream_name, server as redis_server
from helpers.get_env import get_env
from models.audit_model import AuditLog
from utils.actions import is_valid_audit_action

logger = logging.getLogger(__name__)

AUDIT_WORKER_POLL_SECONDS = int(get_env("Log_TimeOut", default="5", required=False))
AUDIT_WORKER_BATCH_SIZE = int(get_env("LOG_LIMIT", default="100", required=False))
AUDIT_WORKER_GROUP = "audit-consumer-group"
AUDIT_WORKER_CONSUMER = "audit-worker-1"


def persist_audit_logs(db: Session, payloads: list[dict[str, Any]]) -> int:
    """Persist a batch of audit payloads to the database using idempotency on request_id."""
    if not payloads:
        return 0

    request_ids = [payload["request_id"] for payload in payloads if payload.get("request_id")]
    if not request_ids:
        return 0

    existing_request_ids = {
        row[0]
        for row in db.execute(
            select(AuditLog.request_id).where(AuditLog.request_id.in_(request_ids))
        ).all()
    }

    new_payloads = [payload for payload in payloads if payload.get("request_id") not in existing_request_ids]
    if not new_payloads:
        return 0

    valid_payloads = [payload for payload in new_payloads if is_valid_audit_action(payload.get("action"))]
    if not valid_payloads:
        return 0

    for payload in valid_payloads:
        db.add(
            AuditLog(
                request_id=payload["request_id"],
                user_id=payload.get("user_id"),
                method=payload.get("method", "GET"),
                path=payload.get("path", "/"),
                status_code=int(payload.get("status_code", 0)),
                duration_ms=float(payload.get("duration_ms", 0.0)),
                ip_address=payload.get("ip_address"),
                user_agent=payload.get("user_agent"),
                action=payload.get("action"),
            )
        )

    db.commit()
    return len(new_payloads)


def _decode_message_payload(raw_data: Any) -> dict[str, Any]:
    """Decode a Redis audit payload whether it arrives as bytes, str, or a dict."""
    if isinstance(raw_data, dict):
        payload_value = raw_data.get("payload")
    else:
        payload_value = raw_data

    if isinstance(payload_value, bytes):
        payload_value = payload_value.decode("utf-8")
    if isinstance(payload_value, str):
        try:
            return json.loads(payload_value)
        except json.JSONDecodeError:
            logger.warning("Audit worker could not decode message payload %r", payload_value)
            return {}
    if isinstance(payload_value, dict):
        return payload_value
    return {}


def _read_audit_batch() -> list[Any]:
    """Read a batch of audit entries from Redis without blocking the main event loop."""
    return redis_server.xreadgroup(
        AUDIT_WORKER_GROUP,
        AUDIT_WORKER_CONSUMER,
        {audit_stream_name: ">"},
        count=AUDIT_WORKER_BATCH_SIZE,
        block=1000,
    )


def _ack_audit_messages(message_ids: list[Any]) -> None:
    """Acknowledge processed audit messages in Redis."""
    for message_id in message_ids:
        redis_server.xack(audit_stream_name, AUDIT_WORKER_GROUP, message_id)


async def _drain_audit_stream() -> None:
    """Consume batches from the audit Redis stream and persist them to the database."""
    pending_payloads: list[dict[str, Any]] = []
    pending_message_ids: list[Any] = []
    last_activity_at = asyncio.get_running_loop().time()

    while True:
        try:
            items = await asyncio.to_thread(_read_audit_batch)
            if items:
                for stream, entries in items:
                    for message_id, data in entries:
                        try:
                            payload = _decode_message_payload(data)
                        except Exception:
                            logger.warning("Audit worker could not decode message %s", message_id)
                            continue
                        if not payload:
                            continue
                        pending_payloads.append(payload)
                        pending_message_ids.append(message_id)
                        last_activity_at = asyncio.get_running_loop().time()

                if len(pending_payloads) >= AUDIT_WORKER_BATCH_SIZE:
                    payloads_to_persist = pending_payloads[:AUDIT_WORKER_BATCH_SIZE]
                    message_ids_to_ack = pending_message_ids[:AUDIT_WORKER_BATCH_SIZE]
                    pending_payloads = pending_payloads[AUDIT_WORKER_BATCH_SIZE:]
                    pending_message_ids = pending_message_ids[AUDIT_WORKER_BATCH_SIZE:]
                    await _persist_pending_batch(payloads_to_persist, message_ids_to_ack)

            now = asyncio.get_running_loop().time()
            if pending_payloads and (now - last_activity_at) >= AUDIT_WORKER_POLL_SECONDS:
                payloads_to_persist = pending_payloads
                message_ids_to_ack = pending_message_ids
                pending_payloads = []
                pending_message_ids = []
                await _persist_pending_batch(payloads_to_persist, message_ids_to_ack)

            if not items:
                await asyncio.sleep(0)
        except RedisTimeoutError:
            await asyncio.sleep(AUDIT_WORKER_POLL_SECONDS)
        except Exception:
            logger.exception("Audit worker loop hit an unexpected error")
            await asyncio.sleep(AUDIT_WORKER_POLL_SECONDS)


async def _persist_pending_batch(payloads: list[dict[str, Any]], message_ids: list[Any]) -> None:
    """Persist a buffered batch of audit payloads and acknowledge the Redis messages."""
    if not payloads:
        return

    db = get_session_local()()
    try:
        inserted_count = persist_audit_logs(db, payloads)
        if inserted_count:
            logger.info("Audit worker persisted %s log entries", inserted_count)
        await asyncio.to_thread(_ack_audit_messages, message_ids)
    except Exception:
        logger.exception("Audit worker failed while persisting batch")
    finally:
        db.close()


async def start_audit_worker() -> None:
    """Start the audit worker loop in the background."""
    try:
        await asyncio.to_thread(
            redis_server.xgroup_create,
            audit_stream_name,
            AUDIT_WORKER_GROUP,
            id="0",
            mkstream=True,
        )
    except Exception:
        pass

    asyncio.create_task(_drain_audit_stream())


def get_audit_worker_health() -> dict[str, Any]:
    """Return a simple health payload for the audit worker."""
    return {"status": "ok", "message": "Audit worker is running"}
