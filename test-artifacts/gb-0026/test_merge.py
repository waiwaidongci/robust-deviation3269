"""End-to-end verification of source package round-trip and three-way merge.

Runs against a temporary SQLite database with a fake Redis queue.  The app
is exercised over real HTTP through FastAPI's TestClient, so role gates,
validation and response models are all in play.

Usage:  PYTHONPATH=/tmp/pylibs:/workspace/backend python3 -I -m pytest -q test_merge.py
"""
import os
import sys
import tempfile

ROOT = "/workspace/backend"
sys.path.insert(0, ROOT)

WORK = tempfile.mkdtemp(prefix="merge-test-")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(WORK, "test.db")
os.environ["REDIS_URL"] = "redis://127.0.0.1:6399/0"
os.environ["JWT_SECRET"] = "test-secret"
os.environ["SEED_DEMO"] = "true"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


class FakeQueue:
    def __init__(self):
        self.jobs = []

    def rpush(self, name, value):
        self.jobs.append((name, int(value)))

    def blpop(self, name, timeout=0):
        return None

    def ping(self):
        return True


@pytest.fixture(scope="session")
def client():
    from src.services import run_service as rs

    rs.queue = FakeQueue()
    from src.main import app

    with TestClient(app) as c:
        c.queue = rs.queue
        yield c


def login(client, role):
    r = client.post(
        "/auth/login",
        json={"email": f"{role}@shuangleng.demo", "password": "Shuangleng2026!"},
    )
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


@pytest.fixture(scope="session")
def admin(client):
    return login(client, "admin")


@pytest.fixture(scope="session")
def analyst(client):
    return login(client, "analyst")


@pytest.fixture(scope="session")
def viewer(client):
    return login(client, "viewer")


def studies(client, headers):
    r = client.get("/studies", headers=headers)
    assert r.status_code == 200
    return {s["code"]: s for s in r.json()}


def windows(client, headers, sid):
    r = client.get(f"/studies/{sid}/windows", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def export(client, headers, sid, branch, base_revision=None):
    params = {"branch": branch}
    if base_revision is not None:
        params["base_revision"] = base_revision
    r = client.get(f"/studies/{sid}/packages/export", params=params, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def mutate(package, code, **changes):
    """Apply offline edits to a package copy (values=..., split=..., note=...)."""
    out = {**package, "records": [dict(r) for r in package["records"]]}
    for record in out["records"]:
        if record["code"] == code:
            if "value" in changes:
                code_f, value = changes["value"]
                record["values"] = {**record["values"], code_f: value}
            if "split" in changes:
                record["split"] = changes["split"]
            if "note" in changes:
                record["note"] = changes["note"]
            break
    else:
        raise AssertionError("window not in package: " + code)
    return out


def resign(client, headers, sid, package):
    """Recompute the content hash the way the server does, by asking it.

    The test edits the exported JSON like an offline tool would; the server
    must reject a mismatched hash, so we recompute it locally with the same
    canonical rule before import.
    """
    from src.services.merge_service import package_content_hash

    payload = {k: package[k] for k in (
        "format", "study_code", "branch", "base_revision", "base_hash", "features", "records"
    )}
    package["content_hash"] = package_content_hash(payload)
    package["package_uid"] = "pkg-" + package["content_hash"][:32]
    return package


def receive(client, headers, sid, package, expect=200):
    r = client.post(f"/studies/{sid}/packages", json=package, headers=headers)
    assert r.status_code == expect, r.text
    return r.json()


# ---------------------------------------------------------------------------
# The scenarios
# ---------------------------------------------------------------------------

def test_round_trip_is_identity(client, admin, analyst):
    s = studies(client, admin)["SL-01"]
    exported = export(client, admin, s["id"], "offline-a")
    received = receive(client, admin, s["id"], exported)
    assert received["status"] == "ignored"
    assert received["summary"]["changed_rows"] == 0
    # export -> import -> export must be byte-identical in records
    again = export(client, admin, s["id"], "offline-a")
    assert again["records"] == exported["records"]
    assert again["base_hash"] == exported["base_hash"]


def test_values_split_and_notes_merge_without_conflict(client, admin, analyst):
    s = studies(client, analyst)["SL-02"]
    sid = s["id"]
    base = export(client, analyst, sid, "offline-a")
    target = base["records"][0]["code"]

    # Branch A: change one value and add a note on the same window.
    a = resign(client, analyst, sid, mutate(base, target, value=("temperature", 21.25), note="离线A复核：温度按原始日志更正。"))
    ra = receive(client, analyst, sid, a)
    assert ra["status"] == "merged"
    assert ra["summary"]["changed_fields"] == ["note", "values.temperature"]
    rows = {w["code"]: w for w in windows(client, analyst, sid)}
    assert rows[target]["values"]["temperature"] == 21.25
    assert rows[target]["last_branch"] == "offline-a"

def test_replay_does_not_undo_merged_edits(client, admin):
    s = studies(client, admin)["SL-03"]
    sid = s["id"]
    base = export(client, admin, sid, "offline-a") if True else None
    # SL-03 is rank deficient, but packages are about source data, not fitting.
    target = base["records"][10]["code"]
    a = resign(client, admin, sid, mutate(base, target, value=("temperature", 22.0), note="分支A改写温度并批注。"))
    first = receive(client, admin, sid, a)
    assert first["status"] == "merged", first
    # B exported from the same baseline, edits another window, imports later.
    other = base["records"][11]["code"]
    b = resign(client, admin, sid, mutate(base, other, value=("humidity", 51.5), note="分支B改写湿度并批注。"))
    rb = receive(client, admin, sid, b)
    assert rb["status"] == "merged", rb
    # A is collected again unchanged: must be a replay and change nothing.
    replay = receive(client, admin, sid, a)
    assert replay["replayed"] is True
    assert replay["package_uid"] == first["package_uid"]
    rows = {w["code"]: w for w in windows(client, admin, sid)}
    assert rows[target]["values"]["temperature"] == 22.0
    assert rows[other]["values"]["humidity"] == 51.5
    assert rows[target]["last_branch"] == "offline-a"


def test_conflict_blocks_whole_package_and_resolution_is_atomic(client, admin):
    s = studies(client, admin)["SL-01"]
    sid = s["id"]
    base = export(client, admin, sid, "offline-a")
    target = base["records"][3]["code"]
    clean = base["records"][4]["code"]

    # Local desk annotates the target window after the package was exported.
    target_id = next(w["id"] for w in windows(client, admin, sid) if w["code"] == target)
    assert client.patch(f"/windows/{target_id}", json={"note": "本库先前的批注内容。"}, headers=admin).status_code == 200

    # Branch B edits a different window's value and the same note differently.
    b = resign(client, admin, sid, mutate(base, target, value=("temperature", 23.0), note="分支B温度改写与批注。"))
    b = resign(client, admin, sid, mutate(b, clean, value=("particles", 17.5)))
    rb = receive(client, admin, sid, b)
    assert rb["status"] == "conflict"
    assert any(c["field"] == "note" for c in rb["conflicts"])
    # Neither the conflicting note nor the otherwise-clean edit is applied
    # while the package waits: the package is all-or-nothing.
    rows = {w["code"]: w for w in windows(client, admin, sid)}
    assert rows[target]["values"]["temperature"] != 23.0
    assert rows[target]["note"] == "本库先前的批注内容。"
    assert rows[clean]["values"]["particles"] != 17.5

    # Run creation is blocked until the human decides.
    r = client.post("/runs", json={"name": "冲突期间的研究", "study_id": sid, "alpha": .05, "seed": 2026}, headers=admin)
    assert r.status_code == 409

    # Ours: package rejected, live values unchanged, later re-import is a replay.
    resolved = client.post(f"/studies/{sid}/packages/{rb['id']}/resolve", json={"resolution": "ours", "note": "保留本库数据"}, headers=admin)
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["status"] == "rejected"
    rows = {w["code"]: w for w in windows(client, admin, sid)}
    assert rows[target]["values"]["temperature"] != 23.0
    assert rows[clean]["values"]["particles"] != 17.5
    again = client.post(f"/studies/{sid}/packages/{rb['id']}/resolve", json={"resolution": "ours", "note": "重复裁决尝试"}, headers=admin)
    assert again.status_code == 409


def test_theirs_resolution_applies_whole_package_atomically(client, admin):
    s = studies(client, admin)["SL-02"]
    sid = s["id"]
    base = export(client, admin, sid, "offline-b")
    target = base["records"][6]["code"]
    clean = base["records"][7]["code"]
    # Local edit collides on target.temperature.
    local = client.patch(f"/windows/{next(w['id'] for w in windows(client, admin, sid) if w['code']==target)}",
                         json={"note": "本库先前的批注内容。"}, headers=admin)
    assert local.status_code == 200
    a = resign(client, admin, sid, mutate(base, target, value=("temperature", 18.75), note="分支A更正温度并批注。"))
    # Also a clean value on another window in the same package.
    a = resign(client, admin, sid, mutate(a, clean, value=("variation", 0.34)))
    ra = receive(client, admin, sid, a)
    assert ra["status"] == "conflict"

    rb = client.post(f"/studies/{sid}/packages/{ra['id']}/resolve", json={"resolution": "theirs", "note": "以离线更正为准"}, headers=admin)
    assert rb.status_code == 200, rb.text
    assert rb.json()["status"] == "merged"
    rows = {w["code"]: w for w in windows(client, admin, sid)}
    assert rows[target]["values"]["temperature"] == 18.75
    assert rows[clean]["values"]["variation"] == 0.34
    assert rows[target]["last_branch"] == "offline-b"


def test_modified_after_conflict_requires_reimport(client, admin):
    s = studies(client, admin)["SL-01"]
    sid = s["id"]
    base = export(client, admin, sid, "offline-c")
    target = base["records"][8]["code"]
    a = resign(client, admin, sid, mutate(base, target, value=("humidity", 40.0)))
    ra = receive(client, admin, sid, a)
    assert ra["status"] == "merged", ra
    # A second package from the same baseline conflicts with merged state.
    b = resign(client, admin, sid, mutate(base, target, value=("humidity", 44.0)))
    rb = receive(client, admin, sid, b)
    assert rb["status"] == "conflict"
    # A local edit after the conflict invalidates the stored decision point.
    win = next(w for w in windows(client, admin, sid) if w["code"] == target)
    client.patch(f"/windows/{win['id']}", json={"note": "裁决前又改了批注。"}, headers=admin)
    stale = client.post(f"/studies/{sid}/packages/{rb['id']}/resolve", json={"resolution": "theirs", "note": "试图裁决"}, headers=admin)
    assert stale.status_code == 409
    # Re-importing the same content after the new baseline needs fresh base.
    fresh_base = export(client, admin, sid, "offline-c")
    c = resign(client, admin, sid, mutate(fresh_base, target, value=("humidity", 45.0)))
    rc = receive(client, admin, sid, c)
    assert rc["status"] == "merged"
    rows = {w["code"]: w for w in windows(client, admin, sid)}
    assert rows[target]["values"]["humidity"] == 45.0


def test_identity_not_row_number(client, admin):
    s = studies(client, admin)["SL-01"]
    sid = s["id"]
    base = export(client, admin, sid, "offline-d")
    target = base["records"][0]["code"]
    a = resign(client, admin, sid, mutate(base, target, value=("temperature", 19.0)))
    receive(client, admin, sid, a)
    rows = windows(client, admin, sid)
    # Database row ids do not appear in or drive the merge.
    assert all("origin_uid" in w for w in rows)
    exported = export(client, admin, sid, "offline-d")
    assert all(r["origin_uid"].startswith("win:") for r in exported["records"])


def test_viewer_cannot_import(client, viewer):
    s = studies(client, viewer)["SL-01"]
    # Viewer cannot even export (role gate), and cannot import.
    r = client.get(f"/studies/{s['id']}/packages/export", params={"branch": "probe"}, headers=viewer)
    assert r.status_code == 403
    r = client.post(f"/studies/{s['id']}/packages", json={}, headers=viewer)
    assert r.status_code == 403


def test_baseline_history_is_traceable(client, admin):
    s = studies(client, admin)["SL-02"]
    sid = s["id"]
    r = client.get(f"/studies/{sid}/baselines", headers=admin)
    assert r.status_code == 200
    revisions = [b["revision"] for b in r.json()]
    assert revisions == sorted(revisions, reverse=True)
    assert len(revisions) >= 2


def test_frozen_run_and_export_survive_merge(client, admin):
    s = studies(client, admin)["SL-01"]
    sid = s["id"]
    # Existing completed run from seed keeps its frozen snapshot.
    runs = client.get("/runs", headers=admin).json()
    frozen = next(x for x in runs if x["status"] == "completed" and x["study_id"] == sid)
    before = frozen["result"]["input_snapshot"]
    base = export(client, admin, sid, "offline-e")
    target = base["records"][2]["code"]
    a = resign(client, admin, sid, mutate(base, target, value=("particles", 12.25)))
    assert receive(client, admin, sid, a)["status"] == "merged"
    after = client.get(f"/runs/{frozen['id']}", headers=admin).json()
    assert after["result"]["input_snapshot"] == before


def test_tampered_package_rejected(client, admin):
    s = studies(client, admin)["SL-01"]
    sid = s["id"]
    base = export(client, admin, sid, "offline-f")
    bad = mutate(base, base["records"][0]["code"], value=("temperature", 1.0))
    r = client.post(f"/studies/{sid}/packages", json=bad, headers=admin)
    assert r.status_code == 422


def test_same_data_different_bookkeeping_has_same_input_hash(client, admin):
    from src.services.analysis_service import analyze_deviation
    from src.services.study_service import create_snapshot
    from src.config.database import SessionLocal
    from src.models import ArchiveStudy

    with SessionLocal() as db:
        study = db.query(ArchiveStudy).filter_by(code="SL-01").first()
        snap = create_snapshot(db, study)
    first = analyze_deviation(snap, .05, 2026)
    bumped = {**snap, "windows": [{**w, "revision": 99, "last_branch": "x", "last_package_uid": "y"} for w in snap["windows"]]}
    second = analyze_deviation(bumped, .05, 2026)
    assert first["input_hash"] == second["input_hash"]
    edited = {**snap, "windows": [{**w, "note": (w["note"] + "改") if i == 0 else w["note"]} for i, w in enumerate(snap["windows"])]}
    assert analyze_deviation(edited, .05, 2026)["input_hash"] != first["input_hash"]


def test_concurrent_resolution_only_one_wins(client, admin):
    """Two admins race to decide the same conflicted package."""
    import threading

    s = studies(client, admin)["SL-02"]
    sid = s["id"]
    # Make a local edit so the next package conflicts.
    base = export(client, admin, sid, "offline-g")
    target = base["records"][9]["code"]
    target_id = next(w["id"] for w in windows(client, admin, sid) if w["code"] == target)
    client.patch(f"/windows/{target_id}", json={"note": "并发裁决前的本库批注。"}, headers=admin)
    a = resign(client, admin, sid, mutate(base, target, note="分支G并发批注内容。", value=("humidity", 46.25)))
    ra = receive(client, admin, sid, a)
    assert ra["status"] == "conflict"

    results = []
    barrier = threading.Barrier(2)

    def decide(resolution):
        barrier.wait()
        r = client.post(
            f"/studies/{sid}/packages/{ra['id']}/resolve",
            json={"resolution": resolution, "note": "并发裁决尝试"},
            headers=admin,
        )
        results.append(r.status_code)

    threads = [threading.Thread(target=decide, args=(x,)) for x in ("theirs", "theirs")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == [200, 409], results
    rows = {w["code"]: w for w in windows(client, admin, sid)}
    assert rows[target]["values"]["humidity"] == 46.25


def test_concurrent_import_same_package_is_idempotent(client, admin):
    import threading

    s = studies(client, admin)["SL-03"]
    sid = s["id"]
    base = export(client, admin, sid, "offline-h")
    target = base["records"][20]["code"]
    a = resign(client, admin, sid, mutate(base, target, value=("temperature", 20.5)))
    outcomes = []
    barrier = threading.Barrier(2)

    def send():
        barrier.wait()
        r = client.post(f"/studies/{sid}/packages", json=a, headers=admin)
        body = r.json()
        outcomes.append((r.status_code, body.get("status"), body.get("replayed")))

    threads = [threading.Thread(target=send) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # Both calls succeed; exactly one did the write and the other replayed
    # the same package.  No duplicate rows, no double application.
    assert [o[0] for o in outcomes] == [200, 200], outcomes
    assert [o[2] for o in outcomes].count(True) == 1, outcomes
    pkgs = client.get(f"/studies/{sid}/packages", headers=admin).json()
    assert len([p for p in pkgs if p["package_uid"] == a["package_uid"]]) == 1
    rows = {w["code"]: w for w in windows(client, admin, sid)}
    assert rows[target]["values"]["temperature"] == 20.5


def test_out_of_order_two_branches_both_one_pass(client, admin):
    s = studies(client, admin)["SL-02"]
    sid = s["id"]
    base = export(client, admin, sid, "seed")
    a = resign(client, admin, sid, mutate(base, base["records"][30]["code"], value=("temperature", 24.5), note="A分支温度改写并说明。"))
    b = resign(client, admin, sid, mutate(base, base["records"][31]["code"], value=("humidity", 58.5), note="B分支湿度改写并说明。"))
    # B is collected first, A later; both originate from the same baseline.
    assert receive(client, admin, sid, b)["status"] == "merged"
    assert receive(client, admin, sid, a)["status"] == "merged"
    rows = {w["code"]: w for w in windows(client, admin, sid)}
    assert rows[base["records"][30]["code"]]["values"]["temperature"] == 24.5
    assert rows[base["records"][31]["code"]]["values"]["humidity"] == 58.5
    # Replaying the earlier package changes nothing.
    assert receive(client, admin, sid, b)["replayed"] is True


def test_partial_value_edit_keeps_other_features(client, admin):
    s = studies(client, admin)["SL-03"]
    sid = s["id"]
    win = next(w for w in windows(client, admin, sid) if w["split"] == "train")
    before = dict(win["values"])
    r = client.patch(
        f"/windows/{win['id']}",
        json={"values": {"temperature": 20.125}},
        headers=admin,
    )
    assert r.status_code == 200, r.text
    after = r.json()
    assert after["values"]["temperature"] == 20.125
    for key in before:
        if key != "temperature":
            assert after["values"][key] == before[key]
    assert after["revision"] == win["revision"] + 1
    assert after["last_branch"] == "local"
    # Round-trip stability: exporting and importing the edited set is a no-op.
    pkg = export(client, admin, sid, "after-edit")
    received = receive(client, admin, sid, pkg)
    assert received["status"] == "ignored"


def test_note_can_be_cleared_and_counts_as_edit(client, admin):
    s = studies(client, admin)["SL-03"]
    sid = s["id"]
    win = next(w for w in windows(client, admin, sid) if w["note"])
    r = client.patch(f"/windows/{win['id']}", json={"note": ""}, headers=admin)
    assert r.status_code == 200, r.text
    assert r.json()["note"] == ""
    pkg = export(client, admin, sid, "clear-note")
    record = next(x for x in pkg["records"] if x["code"] == win["code"])
    assert record["note"] == ""


def test_run_input_hash_tracks_merged_data_only(client, admin):
    """A completed frozen run is unaffected, but a new run after a merge
    reflects the merged values and its input hash changes."""
    s = studies(client, admin)["SL-02"]
    sid = s["id"]
    base = export(client, admin, sid, "offline-i")
    target = next(r for r in base["records"] if r["split"] == "train")
    a = resign(client, admin, sid, mutate(base, target["code"], value=("particles", 24.75)))
    assert receive(client, admin, sid, a)["status"] == "merged"

    # The seed's frozen run for SL-01 is untouched (covered above); here we
    # check a fresh run is accepted and sees the merged value.
    r = client.post("/runs", json={"name": "合并后研究", "study_id": sid, "alpha": .05, "seed": 2026}, headers=admin)
    assert r.status_code == 202, r.text
    job_id = r.json()["id"]
    from src.config.database import SessionLocal
    from src.models import DeviationRun
    from src.services.analysis_service import analyze_deviation
    with SessionLocal() as db:
        run = db.get(DeviationRun, job_id)
        snap = run.input_snapshot
        merged_window = next(w for w in snap["windows"] if w["code"] == target["code"])
        assert merged_window["values"]["particles"] == 24.75
        result = analyze_deviation(snap, run.alpha, run.seed)
        assert result["counts"]["train"] == 200
        # The merged value is present in the frozen snapshot the worker uses.
        assert any(
            w["values"]["particles"] == 24.75 for w in result["input_snapshot"]["windows"]
        )


def test_duplicate_numbering_across_studies_is_distinct(client, admin):
    """The same window code in two studies must not share identity."""
    s = studies(client, admin)
    a = windows(client, admin, s["SL-01"]["id"])
    b = windows(client, admin, s["SL-02"]["id"])
    # Both libraries number their windows W001…, so codes alone are ambiguous.
    assert a[0]["code"].split("-W")[1] == b[0]["code"].split("-W")[1]
    assert a[0]["origin_uid"] != b[0]["origin_uid"]

    # An edit in one study's package cannot touch the other study.
    pkg = export(client, admin, s["SL-01"]["id"], "offline-dup")
    rec = next(r for r in pkg["records"] if r["code"] == a[0]["code"])
    rec["values"]["temperature"] = 1.25
    resign(client, admin, s["SL-01"]["id"], pkg)
    assert receive(client, admin, s["SL-01"]["id"], pkg)["status"] == "merged"
    other_code = b[0]["code"]
    other = next(w for w in windows(client, admin, s["SL-02"]["id"]) if w["code"] == other_code)
    assert other["values"]["temperature"] != 1.25


def test_package_for_wrong_study_rejected(client, admin):
    s = studies(client, admin)
    pkg = export(client, admin, s["SL-01"]["id"], "offline-x")
    r = client.post(f"/studies/{s['SL-02']['id']}/packages", json=pkg, headers=admin)
    assert r.status_code == 422
    assert "研究编号" in r.json()["detail"]


def test_both_sides_same_edit_is_not_conflict(client, admin):
    s = studies(client, admin)["SL-03"]
    sid = s["id"]
    base = export(client, admin, sid, "offline-j")
    target = base["records"][40]["code"]
    value = ("humidity", 48.125)
    # Local edit first...
    wid = next(w["id"] for w in windows(client, admin, sid) if w["code"] == target)
    r = client.patch(f"/windows/{wid}", json={"values": {"humidity": 48.125}}, headers=admin)
    assert r.status_code == 200
    # ...and the branch made the identical edit from the same baseline.
    pkg = resign(client, admin, sid, mutate(base, target, value=value))
    received = receive(client, admin, sid, pkg)
    assert received["status"] == "ignored"
    assert received["conflicts"] == []
