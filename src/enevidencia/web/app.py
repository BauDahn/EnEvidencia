"""Portal web local (FastAPI). Muestra evidencia comparada; no emite veredictos.

Flujo automático ("Investigar tema"): el LLM PROPONE (consultas, clasificación, afirmaciones); el código VERIFICA
con criterios objetivos documentados (config/pipeline.yaml, citas literales, URLs, referencias) y acepta sin que
intervenga el usuario (para no introducir su sesgo). Todo queda en la auditoría y puede corregirse después a mano
en la pantalla de revisión.
"""
from __future__ import annotations

import os
import threading
import uuid
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from urllib.parse import quote as urlquote

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import ingestion
from ..classification import classify
from ..classification_llm import classify_with_fallback
from ..comparison import compare, normalize_value
from ..comparison.llm import compare_grouped, group_claims
from ..discovery import discover
from ..extraction import QuoteNotInSource, build_claim, suggest_candidates
from ..extraction.llm import extract_claims
from ..ingestion import fetch_source, source_from_text
from ..llm import LLMError, get_client
from ..models import Fact, SourceType, Status
from ..pipeline import domain_of, filter_sources, load_config
from ..reporting import render_html, render_markdown
from ..reporting.llm import render_full_html, render_full_report
from ..reporting.render import fecha_corta, fecha_es, safe_href
from ..storage import Storage

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


_safe_href = safe_href
_fecha_corta = fecha_corta


TEMPLATES.env.filters["safe_href"] = _safe_href
TEMPLATES.env.filters["fecha"] = _fecha_corta
TEMPLATES.env.globals["hoy"] = fecha_es
TEMPLATES.env.globals["normalize_value"] = normalize_value

LINKS_SCHEMA = """
CREATE TABLE IF NOT EXISTS fact_sources (
    fact_id INTEGER NOT NULL, source_id INTEGER NOT NULL,
    extracted INTEGER NOT NULL DEFAULT 0, discarded INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (fact_id, source_id)
);
"""


def scrub(msg: object) -> str:
    """Nunca mostrar la clave API en mensajes de error."""
    text = str(msg)
    key = os.environ.get("GEMINI_API_KEY", "")
    return text.replace(key, "***") if key else text


def _local(path: str, default: str) -> str:
    """Solo redirecciones internas (evita open redirect)."""
    return path if path.startswith("/") and not path.startswith("//") else default


@dataclass
class Job:
    kind: str
    fact_id: int
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    status: str = "running"          # running | done | error
    log: list[str] = field(default_factory=list)
    error: str = ""
    thread: threading.Thread | None = None
    next_url: str = ""               # a dónde lleva la página de progreso al terminar

    def note(self, msg: str) -> None:
        self.log.append(scrub(msg))


def _is_quota(error: str) -> bool:
    low = str(error).lower()
    return any(m in low for m in ("429", "cuota", "quota", "resource_exhausted", "rate limit", "saturado", "reintentos"))


def create_app(storage: Storage | None = None, llm=None, searcher=None, fetcher=None,
               pipeline_config: dict | None = None) -> FastAPI:
    """llm, searcher y fetcher son inyectables para probar sin red. Si llm es None se crea al necesitarlo."""
    app = FastAPI(title="EnEvidencia")
    app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
    db = storage or Storage(os.environ.get("ENEVIDENCIA_DB", "data/enevidencia.db"))
    db.conn.executescript(LINKS_SCHEMA)
    cfg = pipeline_config if pipeline_config is not None else load_config()
    jobs: dict[str, Job] = {}
    lock = threading.Lock()
    raw_fetch = fetcher or fetch_source

    def safe_fetch(url: str):
        ingestion.validate_url(url)  # también para fetchers inyectados
        return raw_fetch(url)

    def get_llm():
        return llm if llm is not None else get_client()  # puede lanzar LLMError con instrucciones

    def page(request: Request, name: str, **ctx):
        return TEMPLATES.TemplateResponse(request, name, ctx)

    def home_ctx(error: str | None) -> dict:
        """Datos de portada: cada hecho con nº de fuentes/afirmaciones aceptadas y estado descriptivo entre fuentes."""
        cards = []
        for f in db.list_facts():
            recs = db.claims_for_fact(f.id)
            dates = [r.source.published for r in recs if r.source.published]
            cards.append({"fact": f, "claims": len(recs), "sources": len({r.source.id for r in recs}),
                          "status": compare(f.id, recs).status if recs else "", "latest": max(dates) if dates else None})
        cards.reverse()  # lo más reciente primero
        cards.sort(key=lambda c: c["claims"] == 0)  # la apertura es un hecho con afirmaciones, si lo hay (orden estable)
        sources = db.list_sources()
        return {"cards": cards, "facts": db.list_facts(), "sources": sources, "latest_sources": sources[::-1][:6],
                "error": error}

    def link(fact_id: int, source_id: int) -> None:
        with lock:
            db.conn.execute("INSERT OR IGNORE INTO fact_sources(fact_id, source_id) VALUES (?,?)", (fact_id, source_id))
            db.conn.commit()

    def start(job: Job, target) -> Job:
        def run():
            try:
                target(job)
                job.status = "done"
            except LLMError as e:
                job.error, job.status = f"Error del LLM (cuota, red o clave): {scrub(e)}", "error"
            except Exception as e:  # noqa: BLE001
                job.error, job.status = f"Error inesperado: {scrub(e)}", "error"

        jobs[job.id] = job
        job.thread = threading.Thread(target=run, daemon=True)
        job.thread.start()
        return job

    app.state.jobs = jobs
    app.state.wait = lambda job_id, timeout=30: jobs[job_id].thread.join(timeout)

    # ---- tareas en segundo plano
    def investigate_task(fact: Fact, model):
        """Pipeline automático: descubrir -> filtros objetivos -> extraer -> aceptar verificadas -> comparar."""
        def task(job: Job) -> None:
            job.next_url = f"/facts/{fact.id}"
            res = discover(fact.title, model, searcher=searcher, fetcher=safe_fetch, filtrar=False,
                           max_candidates=int(cfg["max_candidatas"]), on_progress=job.note)
            for q in res.queries:
                db.add_audit_query(fact.id, q)
            job.note(f"Etapa 2/4: aplicando filtros objetivos a {len(res.sources)} fuentes descargadas")
            for err in res.errors:
                job.note(f"Descartada: {err}")
            for url, why in res.excluded:
                db.add_audit_source(fact.id, url, "", domain_of(url), "excluded", why)
            decisions, tt = filter_sources(res.sources, fact.title, cfg)
            if tt.terms:
                job.note(f"Términos del tema exigidos ({tt.kind}): {', '.join(tt.terms)}")
            accepted: list = []
            for d in decisions:
                src = d.source
                if d.accepted:
                    src.source_type, src.classification_reason = classify_with_fallback(src, model)
                    src.status = Status.ACCEPTED
                else:
                    src.source_type, src.classification_reason = classify(src.url, src.kind)
                    src.status = Status.REJECTED
                db.add_source(src)
                link(fact.id, src.id)
                db.add_audit_source(fact.id, src.url, src.title, domain_of(src.url),
                                    "accepted" if d.accepted else "excluded", d.reason, src.id)
                job.note(("Aceptada: " if d.accepted else "Excluida: ") + f"{src.url} · {d.reason}")
                if d.accepted:
                    accepted.append(src)
            job.note(f"{len(accepted)} fuentes aceptadas, {len(decisions) - len(accepted) + len(res.excluded)} excluidas.")

            job.note(f"Etapa 3/4: extrayendo afirmaciones de {len(accepted)} fuentes (una a una, respetando la cuota)")
            total, partial = 0, ""
            for i, src in enumerate(accepted, 1):
                job.note(f"[{i}/{len(accepted)}] Extrayendo de {src.url}")
                out = extract_claims(src, fact.id, model, topic=fact.title)
                for c in out.claims:
                    c.status = Status.ACCEPTED   # ya llevan la cita verificada literalmente por el código
                    db.add_claim(c)
                for dsc in out.discarded:
                    db.add_audit_claim(fact.id, src.id, src.url, dsc)
                total += len(out.claims)
                with lock:
                    db.conn.execute("UPDATE fact_sources SET extracted=?, discarded=?, error=? WHERE fact_id=? AND source_id=?",
                                    (0 if out.error else 1, len(out.discarded), scrub(out.error or ""), fact.id, src.id))
                    db.conn.commit()
                job.note(f"  {len(out.claims)} afirmaciones guardadas, {len(out.discarded)} descartadas por la verificación de cita"
                         + (f", error: {scrub(out.error)}" if out.error else ""))
                if out.error and not out.claims and _is_quota(out.error):
                    partial = (f"Resultado parcial: se agotó la cuota o el servicio de IA al procesar {src.url}. "
                               f"Se conservan las {total} afirmaciones ya guardadas; faltan {len(accepted) - i + 1} fuente(s) "
                               "por extraer (puedes reanudar desde la revisión o repetir más tarde). "
                               f"Detalle: {scrub(out.error)}")
                    break
            records = db.claims_for_fact(fact.id)
            job.note(f"Etapa 4/4: comparación de {len(records)} afirmaciones aceptadas de {len({r.source.id for r in records})} fuentes")
            job.note(f"Listo: {len(accepted)} fuentes aceptadas, {total} afirmaciones. Estado entre fuentes: "
                     f"{compare(fact.id, records).status}.")
            if partial:
                job.error = partial
        return task

    def extract_task(fact: Fact, model):
        def task(job: Job) -> None:
            rows = db.conn.execute("SELECT source_id FROM fact_sources WHERE fact_id=? AND extracted=0", (fact.id,)).fetchall()
            todo = [s for s in (db.get_source(r["source_id"]) for r in rows) if s and s.status == Status.ACCEPTED]
            if not todo:
                job.note("No hay fuentes aceptadas pendientes de extraer.")
            total = 0
            for src in todo:
                job.note(f"Extrayendo afirmaciones de {src.url}")
                res = extract_claims(src, fact.id, model, topic=fact.title)
                for c in res.claims:
                    db.add_claim(c)
                for dsc in res.discarded:
                    db.add_audit_claim(fact.id, src.id, src.url, dsc)
                total += len(res.claims)
                with lock:
                    db.conn.execute("UPDATE fact_sources SET extracted=?, discarded=?, error=? WHERE fact_id=? AND source_id=?",
                                    (0 if res.error else 1, len(res.discarded), scrub(res.error or ""), fact.id, src.id))
                    db.conn.commit()
                job.note(f"  {len(res.claims)} propuestas, {len(res.discarded)} descartadas por la verificación de cita"
                         + (f", error: {scrub(res.error)}" if res.error else ""))
                if res.error and not res.claims:
                    job.error = f"Fallo al extraer de {src.url}: {scrub(res.error)}"
            job.note(f"Listo: {total} afirmaciones propuestas para revisar.")
        return task

    # ---- portada y hechos
    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        return page(request, "index.html", **home_ctx(request.query_params.get("error")))

    @app.post("/facts")
    def new_fact(title: str = Form(...), description: str = Form("")):
        fact = db.add_fact(Fact(title.strip(), description.strip()))
        return RedirectResponse(f"/facts/{fact.id}", status_code=303)

    @app.post("/investigate")
    def investigate(request: Request, topic: str = Form(...)):
        topic = topic.strip()
        if not topic:
            return RedirectResponse("/?error=Escribe+un+tema", status_code=303)
        try:
            model = get_llm()
        except LLMError as e:  # sin clave: nada se crea; el modo manual sigue disponible
            return page(request, "index.html", **home_ctx(scrub(e)))
        fact = db.add_fact(Fact(topic))
        job = start(Job("investigar", fact.id, next_url=f"/facts/{fact.id}"), investigate_task(fact, model))
        return RedirectResponse(f"/jobs/{job.id}", status_code=303)

    @app.post("/facts/{fact_id}/extract")
    def extract(request: Request, fact_id: int):
        fact = db.get_fact(fact_id)
        if not fact:
            return PlainTextResponse("Hecho no encontrado", status_code=404)
        try:
            model = get_llm()
        except LLMError as e:
            return RedirectResponse(f"/facts/{fact_id}/review?error={urlquote(scrub(e))}", status_code=303)
        job = start(Job("extraer", fact_id), extract_task(fact, model))
        return RedirectResponse(f"/jobs/{job.id}", status_code=303)

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    def job_view(request: Request, job_id: str):
        job = jobs.get(job_id)
        if not job:
            return PlainTextResponse("Tarea no encontrada", status_code=404)
        return page(request, "job.html", job=job, log=list(job.log), next_url=job.next_url or f"/facts/{job.fact_id}/review")

    @app.get("/facts/{fact_id}/review", response_class=HTMLResponse)
    def review(request: Request, fact_id: int):
        fact = db.get_fact(fact_id)
        if not fact:
            return PlainTextResponse("Hecho no encontrado", status_code=404)
        links = {r["source_id"]: r for r in db.conn.execute("SELECT * FROM fact_sources WHERE fact_id=?", (fact_id,))}
        sources = [s for s in (db.get_source(i) for i in links) if s]
        claims = db.claims_for_fact(fact_id, None)
        by_source: dict[int, list] = {}
        for rec in claims:
            by_source.setdefault(rec.source.id, []).append(rec.claim)
        count = lambda items, st: sum(1 for i in items if i.status == st)  # noqa: E731
        stats = {
            "src_proposed": count(sources, Status.PROPOSED), "src_accepted": count(sources, Status.ACCEPTED),
            "src_rejected": count(sources, Status.REJECTED),
            "cl_proposed": count([r.claim for r in claims], Status.PROPOSED),
            "cl_accepted": count([r.claim for r in claims], Status.ACCEPTED),
            "cl_rejected": count([r.claim for r in claims], Status.REJECTED),
            "discarded": sum(r["discarded"] for r in links.values()),
            "pending_extraction": sum(1 for s in sources if s.status == Status.ACCEPTED and not links[s.id]["extracted"]),
        }
        return page(request, "review.html", fact=fact, sources=sources, links=links, by_source=by_source, stats=stats,
                    types=list(SourceType), Status=Status, error=request.query_params.get("error"))

    @app.post("/sources/{source_id}/status")
    def source_status(source_id: int, status: str = Form(...), next: str = Form("")):
        st = Status(status)
        db.set_source_status(source_id, st)
        if st == Status.REJECTED:  # lo descartado no puede dejar afirmaciones aceptadas en el informe
            with lock:
                db.conn.execute("UPDATE claims SET status=? WHERE source_id=? AND status!=?",
                                (Status.REJECTED.value, source_id, Status.REJECTED.value))
                db.conn.commit()
        return RedirectResponse(_local(next, f"/sources/{source_id}"), status_code=303)

    @app.post("/claims/{claim_id}/status")
    def claim_status(claim_id: int, status: str = Form(...), next: str = Form("/")):
        st = Status(status)
        row = db.conn.execute("SELECT s.status AS ss FROM claims c JOIN sources s ON s.id=c.source_id WHERE c.id=?",
                              (claim_id,)).fetchone()
        if row is None:
            return PlainTextResponse("Afirmación no encontrada", status_code=404)
        if st == Status.ACCEPTED and row["ss"] != Status.ACCEPTED.value:
            return RedirectResponse(_local(next, "/") + "?error=Acepta+primero+la+fuente", status_code=303)
        db.set_claim_status(claim_id, st)
        return RedirectResponse(_local(next, "/"), status_code=303)

    @app.get("/facts/{fact_id}", response_class=HTMLResponse)
    def fact_view(request: Request, fact_id: int):
        fact = db.get_fact(fact_id)
        if not fact:
            return PlainTextResponse("Hecho no encontrado", status_code=404)
        records = db.claims_for_fact(fact_id)
        literal = {r.claim.id: r.claim.value for r in records}
        error = request.query_params.get("error")
        sections = None
        if request.query_params.get("grouped"):
            try:
                groups = group_claims(records, get_llm())
                sections = compare_grouped(fact_id, records, groups)
            except LLMError as e:
                error = f"No se pudo agrupar con IA: {scrub(e)}"
        pending = db.claims_for_fact(fact_id, Status.PROPOSED)
        n_pending_src = db.conn.execute(
            "SELECT COUNT(*) FROM fact_sources f JOIN sources s ON s.id=f.source_id WHERE f.fact_id=? AND s.status=?",
            (fact_id, Status.PROPOSED.value)).fetchone()[0]
        return page(request, "fact.html", fact=fact, cmp=compare(fact_id, records), sections=sections, literal=literal,
                    sources=db.list_sources(Status.ACCEPTED), error=error, pending_claims=len(pending),
                    pending_sources=n_pending_src, m=db.methodology(fact_id))

    @app.get("/facts/{fact_id}/audit", response_class=HTMLResponse)
    def audit_view(request: Request, fact_id: int):
        fact = db.get_fact(fact_id)
        if not fact:
            return PlainTextResponse("Hecho no encontrado", status_code=404)
        return page(request, "audit.html", fact=fact, audit=db.audit(fact_id), m=db.methodology(fact_id), cfg=cfg)

    @app.get("/facts/{fact_id}/report.md", response_class=PlainTextResponse)
    def report_md(fact_id: int):
        fact = db.get_fact(fact_id)
        return PlainTextResponse(render_markdown(db, fact), media_type="text/markdown; charset=utf-8") if fact \
            else PlainTextResponse("No encontrado", status_code=404)

    @app.get("/facts/{fact_id}/report.html", response_class=HTMLResponse)
    def report_html(fact_id: int, descargar: int = 0):
        fact = db.get_fact(fact_id)
        if not fact:
            return PlainTextResponse("No encontrado", status_code=404)
        if descargar:   # versión descargable: sin controles de navegación
            return HTMLResponse(render_html(db, fact), headers={
                "Content-Disposition": f'attachment; filename="enevidencia-informe-{fact_id}.html"'})
        base = f"/facts/{fact_id}"
        return HTMLResponse(render_html(db, fact, controls={
            "back": base, "md": f"{base}/report.md", "download": f"{base}/report.html?descargar=1",
            "alt": {"href": f"{base}/report-ia", "label": "Ver con redacción IA"}}))

    @app.get("/facts/{fact_id}/report-ia")
    def report_ai(fact_id: int, fmt: str = "html", descargar: int = 0):
        fact = db.get_fact(fact_id)
        if not fact:
            return PlainTextResponse("No encontrado", status_code=404)
        base = f"/facts/{fact_id}"
        try:
            if fmt == "md":
                return PlainTextResponse(render_full_report(db, fact, get_llm()), media_type="text/markdown; charset=utf-8")
            if descargar:
                return HTMLResponse(render_full_html(db, fact, get_llm(), scrub=scrub), headers={
                    "Content-Disposition": f'attachment; filename="enevidencia-informe-ia-{fact_id}.html"'})
            return HTMLResponse(render_full_html(db, fact, get_llm(), scrub=scrub, controls={
                "back": base, "md": f"{base}/report-ia?fmt=md", "download": f"{base}/report-ia?descargar=1",
                "alt": {"href": f"{base}/report.html", "label": "Ver sin redacción IA"}}))
        except LLMError as e:
            return RedirectResponse(f"/facts/{fact_id}?error={urlquote(scrub(e))}", status_code=303)

    # ---- fuentes y afirmaciones manuales
    @app.post("/sources")
    def new_source(url: str = Form(...), text: str = Form(""), kind: str = Form(""), outlet: str = Form(""),
                   author: str = Form(""), title: str = Form(""), published: str = Form("")):
        try:
            pub = date.fromisoformat(published) if published else None
            if text.strip():
                src = source_from_text(url, text, kind=kind, outlet=outlet, author=author, title=title, published=pub)
            else:
                src = fetch_source(url, kind=kind)
                src.outlet, src.author, src.title, src.published = (outlet or src.outlet, author or src.author,
                                                                     title or src.title, pub or src.published)
            db.add_source(src)
        except Exception as e:  # noqa: BLE001 - URL no válida, descarga fallida, PDF escaneado...
            return RedirectResponse(f"/?error={urlquote(scrub(e))}", status_code=303)
        return RedirectResponse(f"/sources/{src.id}", status_code=303)

    @app.get("/sources/{source_id}", response_class=HTMLResponse)
    def source_view(request: Request, source_id: int):
        src = db.get_source(source_id)
        if not src:
            return PlainTextResponse("Fuente no encontrada", status_code=404)
        return page(request, "source.html", s=src, facts=db.list_facts(), types=list(SourceType),
                    candidates=suggest_candidates(src.text), error=request.query_params.get("error"))

    @app.post("/sources/{source_id}/type")
    def change_type(source_id: int, source_type: str = Form(...), next: str = Form("")):
        db.set_source_type(source_id, SourceType(source_type))
        return RedirectResponse(_local(next, f"/sources/{source_id}"), status_code=303)

    @app.post("/claims")
    def new_claim(source_id: int = Form(...), fact_id: int = Form(...), statement: str = Form(...),
                  speaker: str = Form(...), quote: str = Form(...), value: str = Form("")):
        src = db.get_source(source_id)
        try:
            db.add_claim(build_claim(src, fact_id, statement, speaker, quote, value))
        except (QuoteNotInSource, ValueError) as e:
            return RedirectResponse(f"/sources/{source_id}?error={urlquote(str(e))}", status_code=303)
        return RedirectResponse(f"/facts/{fact_id}", status_code=303)

    return app


def main() -> None:
    import uvicorn

    uvicorn.run(create_app(), host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
