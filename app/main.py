import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.routers import calls, internal, stream

# Ensure our app loggers surface at INFO level alongside uvicorn's own logs
logging.basicConfig(level=logging.INFO)
logging.getLogger("app").setLevel(logging.INFO)

app = FastAPI(
    title="Voice AI Receptionist",
    description="AI receptionist for dental practices — answers calls, captures bookings.",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["POST", "GET"],
    allow_headers=["*"],
)

app.include_router(calls.router)
app.include_router(stream.router)
app.include_router(internal.router)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
