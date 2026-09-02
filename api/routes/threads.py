"""api/routes/threads.py — per-user chat history.

The whole conversation blob (the frontend's thread shape) persisted per account, so a user's
history follows their login instead of living in one browser's localStorage. Stored as JSON —
no need to model messages relationally for a demo — keyed to the authenticated user.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from .. import appdb
from .auth import current_user

router = APIRouter(prefix="/threads", tags=["threads"])


@router.get("")
def get_threads(user: dict = Depends(current_user)) -> dict:
    """This user's persisted conversations (opaque JSON blob), or null if none yet."""
    return {"data": appdb.get_threads(user["email"])}


# Thread state is chat history for one account. Generous for a real user,
# bounded so a single authenticated caller cannot grow the database without
# limit by PUTting an arbitrary string.
_MAX_THREAD_BYTES = 4 * 1024 * 1024


class ThreadBody(BaseModel):
    data: str = Field(max_length=_MAX_THREAD_BYTES)  # serialised thread state


@router.put("")
def put_threads(body: ThreadBody, user: dict = Depends(current_user)) -> dict:
    appdb.put_threads(user["email"], body.data)
    return {"ok": True}
