import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import text
from .config.database import Base,engine
from .config.migrate import run_upgrade
from .config.settings import SEED_DEMO
from . import models
from .routes import auth,studies,windows,features,runs,notes,audits,merge
from .middlewares.errors import ErrorMiddleware
from .middlewares.rate_limit import RateLimitMiddleware
from .services.run_service import queue
@asynccontextmanager
async def lifespan(app):
    run_upgrade()
    Base.metadata.create_all(engine)
    try:
        from .config.database import SessionLocal
        from .services.merge_service import backfill_origin_uids
        with SessionLocal() as db:
            filled = backfill_origin_uids(db)
            db.commit()
            if filled:
                logging.info("backfilled %s window origin identities", filled)
    except Exception:
        logging.exception("origin identity backfill failed")
    if SEED_DEMO:
        try:
            from scripts.seed_demo import seed
            seed()
        except Exception:logging.exception("demo seed failed")
    yield
app=FastAPI(title="霜棱库房多量偏离研究簿",lifespan=lifespan)
@app.exception_handler(RequestValidationError)
async def validation_error(request,exc):
    details=[{"loc":[str(v) for v in e["loc"]],"msg":e["msg"]} for e in exc.errors()]
    return JSONResponse(status_code=422,content={"detail":"；".join("/".join(e["loc"])+": "+e["msg"] for e in details)})
app.add_middleware(ErrorMiddleware);app.add_middleware(RateLimitMiddleware)
for module in [auth,studies,windows,features,runs,notes,audits,merge]:app.include_router(module.router)
@app.get("/health")
def health():
    with engine.connect() as c:c.execute(text("select 1"))
    queue.ping();return {"status":"healthy","service":"robust-deviation"}
