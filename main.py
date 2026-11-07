"""Entry point: the web pages, their APIs, and the DBOS runtime.

    python main.py      # serves http://localhost:8000

The workflows live in `app/workflows/`; importing them here registers them with
DBOS before launch, so work interrupted by a crash or restart is recovered.
"""

import os

import uvicorn
from dbos import DBOS, DBOSConfig
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, RedirectResponse

from app import logging_setup
from app.api import board_router, workspace_router
from app.workflows import listing_fetch, listing_grading, llm_queue

app = FastAPI(title="Jugg Jobs")
config: DBOSConfig = {
    "name": "dbos-app-starter",
    # Unset: DBOS keeps its own state in a SQLite file next to this one.
    "system_database_url": os.environ.get("DBOS_SYSTEM_DATABASE_URL"),
    # Bump APPLICATION_VERSION (app/workflows/llm_queue.py) whenever a workflow's
    # step sequence changes. DBOS replays a recovered workflow against the
    # current code and aborts if the recorded steps no longer line up.
    "application_version": llm_queue.APPLICATION_VERSION,
    # Optional: connect to DBOS Conductor for a workflow dashboard.
    "conductor_key": os.environ.get("DBOS_CONDUCTOR_KEY"),
}
DBOS(config=config)

app.include_router(board_router)
app.include_router(workspace_router)

# Mirror DBOS/uvicorn logs into logs/dev.log alongside our own logger.
logging_setup.attach("dbos", "uvicorn.error")
logging_setup.section("app start")


def render_page(name: str) -> HTMLResponse:
    """Serve html/<name> with the shared navigation bar injected."""
    with open(os.path.join("html", "_topbar.html"), encoding="utf-8") as file:
        topbar = file.read()
    with open(os.path.join("html", name), encoding="utf-8") as file:
        html = file.read().replace("<!--TOPBAR-->", topbar)
    return HTMLResponse(html)


@app.get("/")
def home():
    return RedirectResponse("/board")


# Every graded posting, sorted by fit.
@app.get("/board")
def board_page():
    return render_page("board.html")


# Postings waiting to be graded, with the Grade and Bulk grade controls.
@app.get("/workspace")
def workspace_page():
    return render_page("workspace.html")


# The paste-the-HTML form: /existing walks the collected links one by one with
# the link prefilled, /other is blank. One page; it reads the mode from its path.
@app.get("/workspace/raw/existing")
@app.get("/workspace/raw/other")
def workspace_raw_form_page():
    return render_page("workspace_raw.html")


# Collected links: paste a Google results block, then "Process all".
@app.get("/workspace/urls")
def workspace_urls_page():
    return render_page("workspace_urls.html")


if __name__ == "__main__":
    DBOS.launch()
    # Queues are registered after launch: register_queue writes the queue's
    # concurrency limits to the system database, which is only open post-launch.
    # Nothing is enqueued before this point, so the limits bind all work.
    llm_queue.register_queues()
    listing_grading.register_queue()
    listing_fetch.register_queue()
    uvicorn.run(app, host="0.0.0.0", port=8000)
