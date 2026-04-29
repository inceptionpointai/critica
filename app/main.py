"""FastAPI application entry."""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from . import __version__, config
from .routes import health, review

log = logging.getLogger("critica")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    from .tmpfiles import sweep_orphans
    sweep_orphans()
    yield


app = FastAPI(
    title="Critica",
    description="Editorial-quality review service for podcast episodes.",
    version=__version__,
    lifespan=lifespan,
)

app.include_router(health.router)
app.include_router(review.router)


@app.get("/")
def root():
    return {
        "service": "critica",
        "version": __version__,
        "docs": "/docs",
        "health": "/health",
    }


def main():
    import uvicorn
    uvicorn.run(app, host=config.HOST, port=config.PORT)


if __name__ == "__main__":
    main()
