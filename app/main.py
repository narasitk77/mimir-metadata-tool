import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from app.database import Base, engine
from app.views.routes import router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    yield


app = FastAPI(title="Mimir Metadata AI Tool", version="1.0.0", lifespan=lifespan)
app.include_router(router)
