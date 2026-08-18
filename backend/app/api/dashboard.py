from fastapi import APIRouter, Depends, HTTPException

from app.core.auth import get_current_employee, require_access
from app.models.employee import Employee
from app.services import dashboard_service

router = APIRouter()


@router.get("/users/{user_id}/dashboard")
def get_user_dashboard(user_id: str, caller: Employee = Depends(get_current_employee)) -> dict:
    require_access(user_id, caller)
    try:
        return dashboard_service.get_dashboard(user_id)
    except Exception:
        raise HTTPException(status_code=503, detail="The meeting graph is unavailable")
