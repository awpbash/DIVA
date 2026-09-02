"""Access-policy admin endpoint.

Backs the "Access policy" UI panel. Lets an operator choose which fact types
are sensitive (hidden + blurred in Restricted view) by clicking, instead of
hand-editing configs/policy/sensitivity.yaml. In-memory: changes apply
immediately to answers + blur, and reset to the YAML seed on restart (real
persistence/auth comes later).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from ..rag import policy as policy_mod
from .auth import require_admin

# Admin-only: PUT mutates the GLOBAL access policy (an anonymous caller could
# previously un-hide financial data for everyone). The panel that used this is
# gone; the routes stay gated until formally removed.
router = APIRouter(tags=["policy"], dependencies=[Depends(require_admin)])


class PolicyUpdate(BaseModel):
    hidden_labels: list[str]


@router.get("/policy")
async def get_policy() -> dict:
    """Current editable state: all fact labels + the ones hidden for the
    low-privilege role."""
    return policy_mod.policy_state()


@router.put("/policy")
async def put_policy(body: PolicyUpdate) -> dict:
    """Set exactly which fact labels are hidden in Restricted view. Applies
    live (answer redaction + chip/PDF blur all re-derive from this)."""
    return policy_mod.set_hidden_labels(body.hidden_labels)
