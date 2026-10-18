"""Lambda entry point; reuse the same FastAPI routes and startup validation."""

import logging

from mangum import Mangum

from app.main import app

# Lambda supplies the stream handler; enable the existing startup INFO events.
logging.getLogger("uvicorn.error").setLevel(logging.INFO)
handler = Mangum(app, lifespan="on")
