from fastapi import HTTPException
from ..models import ClimateWindow,FeatureSpec
from ..utils.serialize import record
from .study_service import get_study
from .audit_service import log
from .merge_service import study_lock,lock_study_row,reconcile_stale_conflicts,canonical_record

def list_windows(db,sid):get_study(db,sid);return [record(w) for w in db.query(ClimateWindow).filter_by(study_id=sid).order_by(ClimateWindow.code)]

def annotate(db,wid,data,user):
    row=db.get(ClimateWindow,wid)
    if not row:raise HTTPException(404,"窗口不存在")
    study=get_study(db,row.study_id)
    changes=[]
    with study_lock(study.id):
        lock_study_row(db,study.id)
        if data.note is not None:
            row.note=data.note;changes.append('note')
        if data.split is not None and data.split!=row.split:
            row.split=data.split;changes.append('split')
        if data.values is not None:
            codes={f.code for f in db.query(FeatureSpec).filter_by(study_id=study.id)}
            extra=set(data.values)-codes
            if extra:raise HTTPException(422,"数值含未定义特征，不得增删四量")
            merged={**row.values,**data.values}
            try:
                clean=canonical_record({"origin_uid":row.origin_uid,"code":row.code,"split":data.split or row.split,"values":merged,"note":data.note if data.note is not None else row.note})
            except ValueError as e:raise HTTPException(422,str(e))
            if clean['values']!=row.values:changes.append('values')
            row.values=clean['values']
        if not changes:
            db.commit()
            return record(row)
        row.revision=(row.revision or 0)+1;row.last_branch="local";row.last_package_uid=""
        log(db,user.email,"window.edit",f"{wid}:{','.join(changes)}:r{row.revision}")
        # This edit moves the live study past any conflict decision point, so
        # the waiting package is retired rather than applied to newer data.
        reconcile_stale_conflicts(db,study)
        db.commit()
    return record(row)
