"""Three-way reconciliation of offline source packages.

The unit of exchange is a *source package*: one study's complete window set
as seen by one offline branch, plus the immutable baseline revision it was
forked from.  Importing never overwrites whole rows.  Each editable field
(split, note, and every feature value) is compared across three versions:

    base    the common baseline stored with the exporting branch
    ours    the live study in this library
    theirs  the imported package

Rules:
    theirs == base            -> keep ours      (stale replay cannot undo us)
    ours   == base            -> take theirs    (branch edit applies)
    theirs == ours            -> keep ours      (both made the same edit)
    otherwise                 -> conflict       (whole package waits for a human)

Nothing is written while a package is in conflict.  An admin resolution
applies one whole side in a single transaction, under a study lock, and
only while the live study still hashes to the state that produced the
reported conflicts.
"""
import threading
from contextlib import contextmanager

from sqlalchemy import text, update
from sqlalchemy.exc import IntegrityError
from fastapi import HTTPException

from ..constants.merge import (
    PACKAGE_FORMAT,
    PACKAGE_MERGED,
    PACKAGE_CONFLICT,
    PACKAGE_REJECTED,
    PACKAGE_IGNORED,
    RESOLUTION_OURS,
    RESOLUTION_THEIRS,
    SPLITS,
    MAX_WINDOWS,
    MAX_CODE_LENGTH,
    MAX_VALUE_ABS,
    MAX_NOTE_LENGTH,
)
from ..models import ArchiveStudy, ClimateWindow, FeatureSpec, SourceMergePackage, StudyBaseline
from ..utils.clock import now
from ..utils.fingerprint import fingerprint
from ..utils.identity import window_uid
from .audit_service import log

_LOCKS = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(study_id):
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(study_id, threading.RLock())


@contextmanager
def study_lock(study_id):
    """Serialize merge decisions inside this process and, on PostgreSQL,
    also take a row lock so other workers block as well."""
    lock = _lock_for(study_id)
    with lock:
        yield


def lock_study_row(db, study_id):
    study = db.get(ArchiveStudy, study_id)
    if not study:
        raise HTTPException(404, "研究不存在")
    try:
        db.execute(
            text("SELECT id FROM archive_studies WHERE id=:id FOR UPDATE"), {"id": study_id}
        )
    except Exception:
        # SQLite and other dialects without row locks still have the
        # in-process study lock and conditional status updates.
        pass
    return study


# ---------------------------------------------------------------------------
# Canonical records and hashing
# ---------------------------------------------------------------------------

def _number(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(label + "须为数值")
    value = float(value)
    if value != value or value in (float("inf"), float("-inf")):
        raise ValueError(label + "须为有限数值")
    if abs(value) > MAX_VALUE_ABS:
        raise ValueError(label + "绝对值不得超过1e9")
    return value


def feature_codes(db, study_id):
    rows = db.query(FeatureSpec).filter_by(study_id=study_id).order_by(FeatureSpec.position).all()
    return [f.code for f in rows]


def canonical_record(raw):
    """Normalise one window record for storage, export and hashing."""
    if not isinstance(raw, dict):
        raise ValueError("窗口记录须为对象")
    uid = str(raw.get("origin_uid") or "").strip()
    if not uid or len(uid) > 80:
        raise ValueError("窗口来源身份缺失或过长")
    code = str(raw.get("code") or "").strip()
    if not code or len(code) > MAX_CODE_LENGTH:
        raise ValueError("窗口编号缺失或过长")
    split = raw.get("split")
    if split not in SPLITS:
        raise ValueError("窗口角色仅train/cal/test")
    values = raw.get("values")
    if not isinstance(values, dict) or not values:
        raise ValueError("窗口数值缺失")
    clean = {}
    for key in sorted(values):
        clean[str(key)] = _number(values[key], f"{code}的{key}")
    note = raw.get("note")
    if note is None:
        note = ""
    if not isinstance(note, str):
        raise ValueError("窗口批注须为文本")
    if len(note) > MAX_NOTE_LENGTH:
        raise ValueError("窗口批注过长")
    return {"origin_uid": uid, "code": code, "split": split, "values": clean, "note": note}


def study_records(db, study_id):
    rows = db.query(ClimateWindow).filter_by(study_id=study_id).order_by(ClimateWindow.code).all()
    records = [
        canonical_record(
            {
                "origin_uid": w.origin_uid,
                "code": w.code,
                "split": w.split,
                "values": w.values or {},
                "note": w.note or "",
            }
        )
        for w in rows
    ]
    return sorted(records, key=lambda r: r["origin_uid"])


def records_hash(records):
    return fingerprint(records)


def package_content_hash(payload):
    material = {
        "format": payload["format"],
        "study_code": payload["study_code"],
        "branch": payload["branch"],
        "base_revision": int(payload["base_revision"]),
        "base_hash": payload["base_hash"],
        "features": [
            {"code": f["code"], "position": int(f["position"])} for f in payload["features"]
        ],
        "records": sorted(payload["records"], key=lambda r: r["origin_uid"]),
    }
    return fingerprint(material)


# ---------------------------------------------------------------------------
# Baselines
# ---------------------------------------------------------------------------

def latest_baseline(db, study_id):
    return (
        db.query(StudyBaseline)
        .filter_by(study_id=study_id)
        .order_by(StudyBaseline.revision.desc())
        .first()
    )


def ensure_baseline(db, study):
    """Create the initial baseline for a study that predates merge support."""
    existing = latest_baseline(db, study.id)
    if existing:
        if not study.merge_revision:
            study.merge_revision = existing.revision
            study.baseline_hash = existing.base_hash
        return existing
    records = study_records(db, study.id)
    if not records:
        return None
    revision = max(1, study.merge_revision or 1)
    row = StudyBaseline(
        study_id=study.id,
        revision=revision,
        base_hash=records_hash(records),
        branch="main",
        records=records,
    )
    db.add(row)
    study.merge_revision = revision
    study.baseline_hash = row.base_hash
    db.flush()
    return row


def ensure_live_baseline(db, study):
    """Return a baseline that exactly matches the live window set.

    Local edits (annotations, or merges applied by another branch) that were
    not the result of a package import still need a traceable fork point
    before serving as a branch basis.  A matching immutable revision is
    reused; otherwise a new one is cut.
    """
    backfill_origin_uids(db)
    records = study_records(db, study.id)
    live_hash = records_hash(records)
    existing = (
        db.query(StudyBaseline)
        .filter_by(study_id=study.id, base_hash=live_hash)
        .order_by(StudyBaseline.revision.desc())
        .first()
    )
    if existing:
        return existing
    latest = latest_baseline(db, study.id)
    revision = max(int(study.merge_revision or 0), latest.revision if latest else 0) + 1
    row = StudyBaseline(
        study_id=study.id,
        revision=revision,
        base_hash=live_hash,
        branch="local",
        records=records,
    )
    db.add(row)
    study.merge_revision = revision
    study.baseline_hash = live_hash
    db.flush()
    return row


def list_baselines(db, study_id):
    return (
        db.query(StudyBaseline)
        .filter_by(study_id=study_id)
        .order_by(StudyBaseline.revision.desc())
        .all()
    )


def backfill_origin_uids(db):
    """Give every pre-merge window its stable source identity.

    Databases created before packages existed have no ``origin_uid``.  The
    identity is derived from study code + window code, so it is reproducible
    and safe to fill in place; measured values are never touched.
    """
    studies = {s.id: s.code for s in db.query(ArchiveStudy)}
    filled = 0
    for window in db.query(ClimateWindow).filter(
        (ClimateWindow.origin_uid.is_(None)) | (ClimateWindow.origin_uid == "")
    ):
        window.origin_uid = window_uid(studies[window.study_id], window.code)
        filled += 1
    if filled:
        db.flush()
    return filled


def baseline_payload(row):
    return {
        "revision": row.revision,
        "base_hash": row.base_hash,
        "branch": row.branch,
        "created_at": row.created_at,
        "window_count": len(row.records or []),
    }


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def export_package(db, study, branch, base_revision=None):
    """Return one branch's view of the study plus its traceable fork point.

    Holding the study lock for the whole read means the exported records and
    the baseline they claim to descend from always describe the same state.
    """
    with study_lock(study.id):
        lock_study_row(db, study.id)
        backfill_origin_uids(db)
        records = study_records(db, study.id)
        if not records:
            raise HTTPException(409, "研究尚无窗口，无法导出资料包")
        ensure_baseline(db, study)
        if base_revision is None:
            baseline = ensure_live_baseline(db, study)
        else:
            baseline = (
                db.query(StudyBaseline)
                .filter_by(study_id=study.id, revision=base_revision)
                .first()
            )
            if not baseline:
                raise HTTPException(409, f"基线修订{base_revision}不存在，无法作为分支依据")
        db.commit()
        base_records = sorted(baseline.records or [], key=lambda r: r["origin_uid"])
        if {r["origin_uid"] for r in base_records} != {r["origin_uid"] for r in records}:
            raise HTTPException(409, "当前窗口集与该基线不一致，不能建立同批资料包")
        features = [
            {"code": f.code, "label": f.label, "unit": f.unit, "position": f.position}
            for f in db.query(FeatureSpec)
            .filter_by(study_id=study.id)
            .order_by(FeatureSpec.position)
        ]
        payload = {
            "format": PACKAGE_FORMAT,
            "study_code": study.code,
            "branch": branch,
            "base_revision": baseline.revision,
            "base_hash": baseline.base_hash,
            "features": features,
            "records": records,
        }
        content_hash = package_content_hash(payload)
        return {
            **payload,
            "package_uid": "pkg-" + content_hash[:32],
            "content_hash": content_hash,
            "exported_at": now(),
        }


# ---------------------------------------------------------------------------
# Merge evaluation
# ---------------------------------------------------------------------------

def _conflict(code, field, base, ours, theirs):
    return {"code": code, "field": field, "base": base, "ours": ours, "theirs": theirs}


def _settle(field, code, base, ours, theirs, conflicts, prefer, changed):
    if theirs == ours:
        return ours
    if theirs == base:
        return ours
    if ours == base:
        if theirs != base:
            changed.add(field)
        return theirs
    conflicts.append(_conflict(code, field, base, ours, theirs))
    if prefer == RESOLUTION_THEIRS:
        changed.add(field)
        return theirs
    return ours


def evaluate_merge(base_records, ours_records, theirs_records, prefer=None):
    """Field-level three-way merge.

    Returns (merged_records, conflicts, changed_fields).  With ``prefer``
    set to ``theirs`` conflict fields resolve to the imported side; this is
    only used after an explicit, locked admin resolution.
    """
    base = {r["origin_uid"]: r for r in base_records}
    ours = {r["origin_uid"]: r for r in ours_records}
    theirs = {r["origin_uid"]: r for r in theirs_records}
    if set(base) != set(theirs):
        raise ValueError("资料包窗口身份集与所声明基线不一致")
    if set(base) != set(ours):
        raise ValueError("当前研究窗口集与基线不一致，请核对来源")
    merged = []
    conflicts = []
    changed = set()
    for uid in sorted(theirs, key=lambda u: theirs[u]["code"]):
        b, o, t = base[uid], ours[uid], theirs[uid]
        if not (b["code"] == o["code"] == t["code"]):
            raise ValueError(f"来源{uid}的编号身份不一致，编号不可用于匹配")
        if set(o["values"]) != set(t["values"]) or set(b["values"]) != set(o["values"]):
            raise ValueError(f"{t['code']}的四量不完整或特征集不一致")
        out = {"origin_uid": o["origin_uid"], "code": o["code"]}
        out["split"] = _settle("split", o["code"], b["split"], o["split"], t["split"], conflicts, prefer, changed)
        out["note"] = _settle("note", o["code"], b["note"], o["note"], t["note"], conflicts, prefer, changed)
        values = {}
        for code in sorted(o["values"]):
            field = "values." + code
            values[code] = _settle(
                field, o["code"], b["values"][code], o["values"][code], t["values"][code], conflicts, prefer, changed
            )
        out["values"] = values
        merged.append(out)
    merged.sort(key=lambda r: r["code"])
    return merged, conflicts, sorted(changed)


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------

def _validate_package(db, study, payload):
    try:
        base_revision = int(payload.get("base_revision"))
    except (TypeError, ValueError):
        raise HTTPException(422, "资料包缺少可比对的基线修订")
    if base_revision < 1:
        raise HTTPException(422, "资料包基线修订不合法")
    payload["base_revision"] = base_revision
    if not isinstance(payload.get("base_hash"), str) or not payload["base_hash"]:
        raise HTTPException(422, "资料包缺少基线指纹")
    if not isinstance(payload.get("branch"), str) or not 1 <= len(payload["branch"]) <= 64:
        raise HTTPException(422, "资料包分支名不合法")
    if not isinstance(payload.get("package_uid"), str) or not payload["package_uid"]:
        raise HTTPException(422, "资料包缺少包编号")
    if payload.get("format") != PACKAGE_FORMAT:
        raise HTTPException(422, "资料包格式不受支持")
    if payload.get("study_code") != study.code:
        raise HTTPException(422, "资料包研究编号与目标研究不一致")
    records = payload.get("records") or []
    if not 1 <= len(records) <= MAX_WINDOWS:
        raise HTTPException(422, "资料包窗口数不合法")
    codes = [str(r.get("code") or "") for r in records]
    uids = [str(r.get("origin_uid") or "") for r in records]
    if len(set(codes)) != len(codes) or len(set(uids)) != len(uids):
        raise HTTPException(422, "资料包存在重复编号或重复来源身份")
    expected_features = feature_codes(db, study.id)
    package_features = payload.get("features") or []
    expected = [
        (f.code, f.position)
        for f in db.query(FeatureSpec)
        .filter_by(study_id=study.id)
        .order_by(FeatureSpec.position)
        .all()
    ]
    if [(f.get("code"), int(f.get("position", -1))) for f in package_features] != expected:
        raise HTTPException(422, "资料包四量定义与目标研究不一致")
    try:
        canonical = [canonical_record(r) for r in records]
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    for record in canonical:
        if set(record["values"]) != set(expected_features):
            raise HTTPException(422, f"{record['code']}的四量不完整或特征集不一致")
    recomputed = package_content_hash(
        {
            "format": payload["format"],
            "study_code": payload["study_code"],
            "branch": payload["branch"],
            "base_revision": payload["base_revision"],
            "base_hash": payload["base_hash"],
            "features": package_features,
            "records": canonical,
        }
    )
    if recomputed != payload.get("content_hash"):
        raise HTTPException(422, "资料包内容指纹不匹配，可能被截断或篡改")
    return canonical, recomputed


def package_payload(package):
    return {
        "id": package.id,
        "package_uid": package.package_uid,
        "study_id": package.study_id,
        "branch": package.branch,
        "base_revision": package.base_revision,
        "base_hash": package.base_hash,
        "content_hash": package.content_hash,
        "status": package.status,
        "summary": package.summary or {},
        "conflicts": package.conflicts or [],
        "state_hash": package.state_hash,
        "resolution": package.resolution,
        "resolved_by": package.resolved_by,
        "resolved_at": package.resolved_at,
        "imported_by": package.imported_by,
        "created_at": package.created_at,
    }


def _existing_package(db, study_id, payload):
    row = db.query(SourceMergePackage).filter_by(package_uid=payload["package_uid"]).first()
    if row:
        if row.study_id != study_id or row.content_hash != payload["content_hash"]:
            raise HTTPException(409, "资料包编号已被不同内容使用，请重新导出")
        return row
    return (
        db.query(SourceMergePackage)
        .filter_by(
            study_id=study_id,
            branch=payload["branch"],
            base_hash=payload["base_hash"],
            content_hash=payload["content_hash"],
        )
        .order_by(SourceMergePackage.id.desc())
        .first()
    )


def import_package(db, study, payload, user):
    with study_lock(study.id):
        lock_study_row(db, study.id)
        backfill_origin_uids(db)
        ensure_baseline(db, study)
        canonical, content_hash = _validate_package(db, study, payload)
        payload = {**payload, "content_hash": content_hash}

        # Duplicate or replayed collection is a no-op and never rewrites
        # windows that were already merged.
        existing = _existing_package(db, study.id, payload)
        if existing:
            db.commit()
            return {**package_payload(existing), "replayed": True}

        if pending_conflicts(db, study):
            db.commit()
            raise HTTPException(409, "研究存在待裁决资料包，请先完成裁决")

        baseline = (
            db.query(StudyBaseline)
            .filter_by(study_id=study.id, base_hash=payload["base_hash"])
            .order_by(StudyBaseline.revision.desc())
            .first()
        )
        if not baseline:
            raise HTTPException(409, "共同基线不可追溯：本库缺少该基线的完整快照")

        base_records = sorted(baseline.records or [], key=lambda r: r["origin_uid"])
        ours = study_records(db, study.id)
        theirs = sorted(canonical, key=lambda r: r["origin_uid"])
        try:
            merged, conflicts, changed = evaluate_merge(base_records, ours, theirs)
        except ValueError as exc:
            raise HTTPException(409, str(exc))

        summary = {
            "window_count": len(theirs),
            "changed_fields": changed,
            "changed_field_count": len(changed),
            "conflict_count": len(conflicts),
        }
        row = SourceMergePackage(
            package_uid=payload["package_uid"],
            study_id=study.id,
            branch=payload["branch"],
            base_revision=payload["base_revision"],
            base_hash=payload["base_hash"],
            content_hash=content_hash,
            base_records=base_records,
            records=theirs,
            status=PACKAGE_CONFLICT if conflicts else PACKAGE_MERGED,
            summary=summary,
            conflicts=conflicts,
            imported_by=user.email,
        )
        if conflicts:
            row.state_hash = records_hash(ours)
            db.add(row)
            try:
                log(db, user.email, "merge.conflict", f"{row.package_uid}:{len(conflicts)}")
                db.commit()
            except IntegrityError:
                db.rollback()
                existing = _existing_package(db, study.id, payload)
                return {**package_payload(existing), "replayed": True}
            return {**package_payload(row), "replayed": False}

        if not changed:
            row.status = PACKAGE_IGNORED
            row.resolution = "noop"
            row.resolved_by = user.email
            row.resolved_at = now()
            row.summary["changed_rows"] = 0
            db.add(row)
            try:
                log(db, user.email, "merge.ignored", row.package_uid)
                db.commit()
            except IntegrityError:
                db.rollback()
                existing = _existing_package(db, study.id, payload)
                return {**package_payload(existing), "replayed": True}
            return {**package_payload(row), "replayed": False}

        _apply_merged(db, study, row, merged, ours, user, resolution="auto")
        db.add(row)
        try:
            log(db, user.email, "merge.merged", row.package_uid)
            db.commit()
        except IntegrityError:
            db.rollback()
            existing = _existing_package(db, study.id, payload)
            return {**package_payload(existing), "replayed": True}
        return {**package_payload(row), "replayed": False}


# ---------------------------------------------------------------------------
# Apply and resolve
# ---------------------------------------------------------------------------

def _apply_merged(db, study, package, merged, ours_records, user, resolution):
    ours = {r["origin_uid"]: r for r in ours_records}
    windows = {w.origin_uid: w for w in db.query(ClimateWindow).filter_by(study_id=study.id)}
    if set(ours) != set(windows):
        raise HTTPException(409, "窗口集与研究快照不一致，拒绝部分应用")
    changed_rows = 0
    for record in merged:
        window = windows.get(record["origin_uid"])
        if not window:
            raise HTTPException(409, "合并结果缺少来源窗口，拒绝部分应用")
        before = ours[record["origin_uid"]]
        if (
            before["split"] == record["split"]
            and before["values"] == record["values"]
            and before["note"] == record["note"]
        ):
            continue
        window.split = record["split"]
        window.values = record["values"]
        window.note = record["note"]
        window.revision = (window.revision or 0) + 1
        window.last_branch = package.branch
        window.last_package_uid = package.package_uid
        changed_rows += 1
    if changed_rows:
        latest = latest_baseline(db, study.id)
        revision = max(
            int(study.merge_revision or 0), latest.revision if latest else 0
        ) + 1
        study.merge_revision = revision
        baseline = StudyBaseline(
            study_id=study.id,
            revision=revision,
            base_hash=records_hash(merged),
            branch=package.branch,
            records=merged,
        )
        db.add(baseline)
        study.baseline_hash = baseline.base_hash
    package.status = PACKAGE_MERGED
    package.resolution = resolution
    package.resolved_by = user.email
    package.resolved_at = now()
    package.summary = {**(package.summary or {}), "changed_rows": changed_rows}
    db.flush()
    return changed_rows


def _claim_resolution(db, package, resolution, user):
    """Atomically mark a pending package as decided.

    Returns True for exactly one caller.  Any other concurrent decider (other
    thread, or another worker on PostgreSQL) sees rowcount 0 and must not
    apply anything.
    """
    result = db.execute(
        update(SourceMergePackage)
        .where(
            SourceMergePackage.id == package.id,
            SourceMergePackage.status == PACKAGE_CONFLICT,
        )
        .values(
            status=PACKAGE_MERGED if resolution == RESOLUTION_THEIRS else PACKAGE_REJECTED,
            resolution=resolution,
            resolved_by=user.email,
            resolved_at=now(),
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        return False
    db.refresh(package)
    return True


def resolve_package(db, study, package_id, data, user):
    with study_lock(study.id):
        lock_study_row(db, study.id)
        retired = {p.id for p in reconcile_stale_conflicts(db, study)}
        if retired:
            # Keep the retirement audit trail even if this decision is
            # refused, then re-take the study row lock for the decision.
            db.commit()
            lock_study_row(db, study.id)
        package = (
            db.query(SourceMergePackage)
            .populate_existing()
            .filter(SourceMergePackage.id == package_id)
            .first()
        )
        if not package or package.study_id != study.id:
            raise HTTPException(404, "资料包不存在")
        if package.status == PACKAGE_MERGED:
            raise HTTPException(409, "资料包已合入；裁决不会撤销已生效修改")
        if package.status == PACKAGE_REJECTED:
            # ``reconcile_stale_conflicts`` commits its retirements; do not
            # roll that audit trail back on the way out.
            if package.id in retired:
                raise HTTPException(409, "研究在待裁决期间已变化，资料包已退回；请基于新基线重新导出后导入")
            raise HTTPException(409, "资料包已完成裁决")
        if package.status != PACKAGE_CONFLICT:
            raise HTTPException(409, "资料包当前不可裁决")

        ours = study_records(db, study.id)
        if package.state_hash != records_hash(ours):
            if not _claim_resolution(db, package, "stale", user):
                db.rollback()
                raise HTTPException(409, "资料包已完成裁决")
            package.resolution = "stale"
            package.resolved_by = "system"
            package.summary = {**(package.summary or {}), "changed_rows": 0, "stale_reason": "live study changed after conflict"}
            log(db, user.email, "merge.resolve_stale", package.package_uid)
            db.commit()
            raise HTTPException(409, "研究在待裁决期间已变化，资料包已退回；请基于新基线重新导出后导入")

        if data.resolution == RESOLUTION_OURS:
            if not _claim_resolution(db, package, RESOLUTION_OURS, user):
                db.rollback()
                raise HTTPException(409, "资料包已完成裁决")
            package.summary = {**(package.summary or {}), "changed_rows": 0}
            log(db, user.email, "merge.resolve_ours", package.package_uid)
            db.commit()
            return package_payload(package)

        base_records = sorted(package.base_records or [], key=lambda r: r["origin_uid"])
        theirs = sorted(package.records or [], key=lambda r: r["origin_uid"])
        try:
            merged, conflicts, _ = evaluate_merge(
                base_records, ours, theirs, prefer=RESOLUTION_THEIRS
            )
        except ValueError as exc:
            raise HTTPException(409, str(exc))
        if package.conflicts != conflicts:
            raise HTTPException(409, "冲突集与导入时不一致，请重新导入资料包")
        if not _claim_resolution(db, package, RESOLUTION_THEIRS, user):
            db.rollback()
            raise HTTPException(409, "资料包已完成裁决")
        _apply_merged(db, study, package, merged, ours, user, resolution=RESOLUTION_THEIRS)
        log(db, user.email, "merge.resolve_theirs", package.package_uid)
        db.commit()
        return package_payload(package)


def list_packages(db, study_id):
    return (
        db.query(SourceMergePackage)
        .filter_by(study_id=study_id)
        .order_by(SourceMergePackage.id.desc())
        .all()
    )


def reconcile_stale_conflicts(db, study):
    """Retire conflicts whose decision point no longer exists.

    A conflict records the live hash it was judged against.  If the study
    moved on (a later local edit, or another merge), that package can never
    be applied and must not block the study forever.  It is retired as
    ``rejected`` with resolution ``stale`` and stays visible in the trail;
    nothing already merged is undone.
    """
    live_hash = records_hash(study_records(db, study.id))
    stale = []
    for package in db.query(SourceMergePackage).filter_by(study_id=study.id, status=PACKAGE_CONFLICT):
        if package.state_hash != live_hash:
            package.status = PACKAGE_REJECTED
            package.resolution = "stale"
            package.resolved_by = "system"
            package.resolved_at = now()
            package.summary = {**(package.summary or {}), "stale_reason": "live study changed after conflict"}
            log(db, "system", "merge.stale", package.package_uid)
            stale.append(package)
    if stale:
        db.flush()
    return stale


def pending_conflicts(db, study, retire=True):
    """Count packages that still block this study.

    Write paths retire conflicts whose decision point has disappeared.  Read
    paths pass ``retire=False`` and only count conflicts still judged against
    the current live state, so a GET never mutates data.
    """
    if retire:
        reconcile_stale_conflicts(db, study)
    live_hash = records_hash(study_records(db, study.id))
    return (
        db.query(SourceMergePackage)
        .filter_by(study_id=study.id, status=PACKAGE_CONFLICT, state_hash=live_hash)
        .count()
    )
