"""Authenticated, offline-safe contest replay scenarios."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from gongxu.db.models import User
from gongxu.demo.service import DemoScenarioError, build_demo_scenario
from gongxu.security import auth
from gongxu.security.csrf import require_csrf

router = APIRouter(prefix="/demo", tags=["demo"])


@router.post("/load/{scenario}", dependencies=[Depends(require_csrf)])
def load_demo_scenario(
    scenario: str,
    _current_user: User = Depends(auth.get_current_user),
) -> dict[str, Any]:
    """Return a deterministic replay while clearly identifying its fixture source."""
    try:
        return build_demo_scenario(scenario)
    except DemoScenarioError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

