"""Portal web local (FastAPI). Muestra evidencia comparada; no emite veredictos."""
from __future__ import annotations

import os
from datetime import date
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ..comparison import compare
from ..extraction import QuoteNotInSource, build_claim, suggest_candidates
from ..ingestion import fetch_source, source_from_text
from ..models import Fact, SourceType
from ..reporting import render_html, render_markdown
from ..storage import Storage

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def create_app(storage: Storage | None = None) -> FastAPI:
    app = FastAPI(title="EnEvidencia")
    db = storage or Storage(os.environ.get("ENEVIDENCIA_DB", "data/enevidencia.db"))

    def page(request: Request, name: str, **ctx):
        return TEMPLATES.TemplateResponse(request, name, ctx)

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        return page(request, "index.html", facts=db.list_facts(), sources=db.list_sources())

    @app.post("/facts")
    def new_fact(title: str = Form(...), description: str = Form("")):
        fact = db.add_fact(Fact(title.strip(), description.strip()))
        return RedirectResponse(f"/facts/{fact.id}", status_code=303)

    @app.get("/facts/{fact_id}", response_class=HTMLResponse)
    def fact_view(request: Request, fact_id: int):
        fact = db.get_fact(fact_id)
        if not fact:
            return PlainTextResponse("Hecho no encontrado", status_code=404)
        return page(request, "fact.html", fact=fact, cmp=compare(fact_id, db.claims_for_fact(fact_id)),
                    sources=db.list_sources(), error=request.query_params.get("error"))

    @app.get("/facts/{fact_id}/report.md", response_class=PlainTextResponse)
    def report_md(fact_id: int):
        fact = db.get_fact(fact_id)
        return PlainTextResponse(render_markdown(db, fact), media_type="text/markdown; charset=utf-8") if fact \
            else PlainTextResponse("No encontrado", status_code=404)

    @app.get("/facts/{fact_id}/report.html", response_class=HTMLResponse)
    def report_html(fact_id: int):
        fact = db.get_fact(fact_id)
        return HTMLResponse(render_html(db, fact)) if fact else PlainTextResponse("No encontrado", status_code=404)

    @app.post("/sources")
    def new_source(url: str = Form(...), text: str = Form(""), kind: str = Form(""), outlet: str = Form(""),
                   author: str = Form(""), title: str = Form(""), published: str = Form("")):
        pub = date.fromisoformat(published) if published else None
        if text.strip():
            src = source_from_text(url, text, kind=kind, outlet=outlet, author=author, title=title, published=pub)
        else:
            src = fetch_source(url, kind=kind)
            src.outlet, src.author, src.title, src.published = (outlet or src.outlet, author or src.author,
                                                                 title or src.title, pub or src.published)
        db.add_source(src)
        return RedirectResponse(f"/sources/{src.id}", status_code=303)

    @app.get("/sources/{source_id}", response_class=HTMLResponse)
    def source_view(request: Request, source_id: int):
        src = db.get_source(source_id)
        if not src:
            return PlainTextResponse("Fuente no encontrada", status_code=404)
        return page(request, "source.html", s=src, facts=db.list_facts(), types=list(SourceType),
                    candidates=suggest_candidates(src.text), error=request.query_params.get("error"))

    @app.post("/sources/{source_id}/type")
    def change_type(source_id: int, source_type: str = Form(...)):
        db.set_source_type(source_id, SourceType(source_type))
        return RedirectResponse(f"/sources/{source_id}", status_code=303)

    @app.post("/claims")
    def new_claim(source_id: int = Form(...), fact_id: int = Form(...), statement: str = Form(...),
                  speaker: str = Form(...), quote: str = Form(...), value: str = Form("")):
        src = db.get_source(source_id)
        try:
            db.add_claim(build_claim(src, fact_id, statement, speaker, quote, value))
        except (QuoteNotInSource, ValueError) as e:
            return RedirectResponse(f"/sources/{source_id}?error={e}", status_code=303)
        return RedirectResponse(f"/facts/{fact_id}", status_code=303)

    return app


def main() -> None:
    import uvicorn

    uvicorn.run(create_app(), host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
