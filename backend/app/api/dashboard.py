from fastapi import APIRouter, Depends, Request
from sqlmodel import Session

from app.db import get_session
from app.schemas.dashboard import DashboardOverviewOut
from app.services.dashboard import get_dashboard_overview
from app.services.permissions import require_admin

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


@router.get("", response_model=DashboardOverviewOut)
def read_dashboard(
    request: Request,
    session: Session = Depends(get_session),
) -> DashboardOverviewOut:
    require_admin(request)
    return get_dashboard_overview(session)
