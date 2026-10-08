"""``/api/notifications`` — the Nub Agent app's notification bell (maiavm fork).

Contract the app consumes (maiavm-desktop ``src/lib/hermes-rest.ts``):

* ``GET /api/notifications?since=<ISO 8601>`` → ``{"notifications": [item, ...]}``, newest
  first; ``since`` keeps items strictly newer. Item: ``{id, title, read, source?, kind?,
  url?, repo?, at?}``.
* ``POST /api/notifications/read`` with ``{"ids": [...]}`` → ``{"ok": true, "updated": n}``.

Both sit behind the dashboard's normal ``/api/`` auth (not the plugin prefix). Store and
producers: :mod:`hermes_cli.notifications_bus`.
"""

from __future__ import annotations

import asyncio
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from hermes_cli import notifications_bus as bus

router = APIRouter()

_MAX_IDS = 500
_MAX_ID_LEN = 200


class _ReadBody(BaseModel):
    ids: List[str] = Field(default_factory=list, max_length=_MAX_IDS)


@router.get("/api/notifications")
async def get_notifications(since: Optional[str] = Query(default=None, max_length=64)):
    cutoff = None
    if since:
        cutoff = bus._parse_at(since)
        if cutoff is None:
            raise HTTPException(status_code=400, detail="since must be an ISO 8601 timestamp")
    items = await asyncio.to_thread(bus.list_notifications, cutoff)
    return {"notifications": items}


@router.post("/api/notifications/read")
async def post_notifications_read(body: _ReadBody):
    ids = [i for i in body.ids if 0 < len(i) <= _MAX_ID_LEN]
    updated = await asyncio.to_thread(bus.mark_read, ids)
    return {"ok": True, "updated": updated}
