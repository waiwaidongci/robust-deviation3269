from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.orm import Session
import json
from ..config.database import get_db
from ..controllers.dependencies import current_user, roles
from ..constants.roles import Role
from ..schemas.merge import PackageInput, PackageOutput, ResolveInput, BaselineOutput
from ..services.merge_service import (
    export_package,
    package_payload as _out,
    import_package,
    resolve_package,
    list_packages,
    list_baselines,
    baseline_payload,
    ensure_baseline,
)
from ..services.study_service import get_study
router = APIRouter(tags=["merge"])


@router.get("/studies/{sid}/baselines", response_model=list[BaselineOutput])
def baselines(sid: int, db: Session = Depends(get_db), user=Depends(current_user)):
    study = get_study(db, sid)
    ensure_baseline(db, study)
    db.commit()
    return [baseline_payload(b) for b in list_baselines(db, sid)]


@router.get("/studies/{sid}/packages/export")
def export(
    sid: int,
    branch: str = Query(min_length=1, max_length=64),
    base_revision: int | None = Query(default=None, ge=1),
    db: Session = Depends(get_db),
    user=Depends(roles(Role.ADMIN, Role.ANALYST)),
):
    study = get_study(db, sid)
    payload = export_package(db, study, branch, base_revision)
    body = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    return Response(
        content=body,
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="windows-{study.code}-{payload["branch"]}.json"'
        },
    )


@router.get("/studies/{sid}/packages", response_model=list[PackageOutput])
def packages(sid: int, db: Session = Depends(get_db), user=Depends(current_user)):
    get_study(db, sid)
    return [_out(p) for p in list_packages(db, sid)]


@router.post("/studies/{sid}/packages", response_model=PackageOutput)
def receive(
    sid: int,
    data: PackageInput,
    db: Session = Depends(get_db),
    user=Depends(roles(Role.ADMIN, Role.ANALYST)),
):
    study = get_study(db, sid)
    return import_package(db, study, data.model_dump(), user)


@router.post("/studies/{sid}/packages/{pid}/resolve", response_model=PackageOutput)
def resolve(
    sid: int,
    pid: int,
    data: ResolveInput,
    db: Session = Depends(get_db),
    user=Depends(roles(Role.ADMIN)),
):
    study = get_study(db, sid)
    return resolve_package(db, study, pid, data, user)
