from fastapi import HTTPException
from ..models import ArchiveStudy,ClimateWindow,FeatureSpec
from ..utils.serialize import record
from .analysis_service import prepare_snapshot
from .merge_service import pending_conflicts, backfill_origin_uids

def get_study(db,sid):
    row=db.get(ArchiveStudy,sid)
    if not row:raise HTTPException(404,"研究不存在")
    return row
def create_snapshot(db,study):return {"study":record(study),"features":[record(f) for f in db.query(FeatureSpec).filter_by(study_id=study.id).order_by(FeatureSpec.position)],"windows":[record(w) for w in db.query(ClimateWindow).filter_by(study_id=study.id).order_by(ClimateWindow.code)]}
def list_studies(db):
    backfill_origin_uids(db)
    out=[]
    for s in db.query(ArchiveStudy).order_by(ArchiveStudy.code):
        snapshot=create_snapshot(db,s);supported=True;reason='拆分完整、训练满秩；可运行稳健研究'
        try:prepare_snapshot(snapshot)
        except ValueError as e:supported=False;reason=str(e)
        conflicts=pending_conflicts(db,s,retire=False)
        if conflicts:supported=False;reason=f"存在{conflicts}个待裁决资料包；裁决前不可创建研究"
        out.append({**record(s),"counts":{role:sum(w['split']==role for w in snapshot['windows']) for role in ['train','cal','test']},"feature_count":len(snapshot['features']),"supported":supported,"reason":reason,"merge_revision":s.merge_revision,"pending_conflicts":conflicts})
    return out
