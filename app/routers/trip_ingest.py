"""The owner's view of quarantined trip batches (MEMNG-1045). Resolve means "the owner has looked at
it": it files nothing, deletes nothing and sends nothing to the door, and `state` stays
`quarantined`, because settle selects undelivered quarantines by it."""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import auth
from app.api_types import Rfc3339Datetime
from app.database import get_session
from app.models import IngestBatch, Pass, TripIngestCursor, User

router = APIRouter(prefix="/api/trip-ingest", tags=["trip-ingest"])


class QuarantinedBatchOut(BaseModel):
    uid: str
    passUid: str
    fileCount: int
    byteTotal: int
    createdAt: Rfc3339Datetime


class QuarantineOut(BaseModel):
    batches: list[QuarantinedBatchOut]
    unknownTotal: int = Field(description="Quarantines for a pass home never issued, ever.")
    unknownOpen: int = Field(description="The total less what the owner cleared.")


class UnknownResolveIn(BaseModel):
    through: int | None = Field(
        default=None, ge=0, description="The `unknownTotal` the owner saw; later ones stay counted."
    )


async def _cursor_row(db: AsyncSession) -> TripIngestCursor | None:
    return (await db.execute(select(TripIngestCursor).limit(1))).scalar_one_or_none()


@router.get("/quarantine", response_model=QuarantineOut)
async def list_quarantine(
    _user: Annotated[User, Depends(auth.require_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> QuarantineOut:
    rows = await db.execute(
        select(IngestBatch, Pass.uid)
        .join(Pass, Pass.id == IngestBatch.pass_id)
        .where(IngestBatch.state == "quarantined", IngestBatch.resolved_at.is_(None))
        .order_by(IngestBatch.id)
    )
    cur = await _cursor_row(db)
    total = cur.quarantined_unknown_total if cur is not None else 0
    cleared = cur.quarantined_unknown_cleared if cur is not None else 0
    return QuarantineOut(
        batches=[
            QuarantinedBatchOut(
                uid=b.uid,
                passUid=pass_uid,
                fileCount=b.file_count,
                byteTotal=b.byte_total,
                createdAt=b.created_at,
            )
            for b, pass_uid in rows.tuples()
        ],
        unknownTotal=total,
        unknownOpen=total - cleared,
    )


async def _resolve_batch(db: AsyncSession, batch_uid: str, user: User) -> None:
    batch = (
        await db.execute(select(IngestBatch).where(IngestBatch.uid == batch_uid).with_for_update())
    ).scalar_one_or_none()
    if batch is None or batch.state != "quarantined":
        raise HTTPException(status_code=404, detail="No such quarantined batch")
    if batch.resolved_at is None:
        batch.resolved_at = datetime.now(UTC)
        batch.resolved_by = user.id
    await db.commit()


@router.post("/quarantine/unknown/resolve", status_code=204)
async def resolve_unknown(
    body: UnknownResolveIn,
    user: Annotated[User, Depends(auth.require_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    """`through` is the total the owner saw: anything since stays counted, so no ABA and no 409.
    With no `through` this is the batch whose door-chosen uid is literally `unknown`, which this
    route would otherwise make unresolvable and its alert unclearable."""
    if body.through is None:
        await _resolve_batch(db, "unknown", user)
        return
    cur = (await db.execute(select(TripIngestCursor).limit(1).with_for_update())).scalar_one()
    cur.quarantined_unknown_cleared = max(
        cur.quarantined_unknown_cleared, min(body.through, cur.quarantined_unknown_total)
    )
    await db.commit()


@router.post("/quarantine/{batch_uid}/resolve", status_code=204)
async def resolve_batch(
    batch_uid: str,
    user: Annotated[User, Depends(auth.require_user)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    await _resolve_batch(db, batch_uid, user)
