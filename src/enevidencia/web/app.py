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

from fastapi import Depends, FastAPI, Form, Request
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
from ..llm import LLMError, get_client, is_quota_error
from ..models import Fact, SourceType, Status
from ..pipeline import domain_of, filter_sources, load_config
from ..reporting.llm import NarrativeRejected, generate_report
from ..reporting.render import fecha_corta, fecha_es, narrative_blocks, safe_href
from ..storage import Storage
from .auth_routes import install_auth
from .community_routes import install_community
from .limits import BodyLimitMiddleware
from .deps import csrf_check, current_user, require_admin
from .topics_routes import install_topics

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


_is_quota = is_quota_error


def _is_admin(request) -> bool:
    user = current_user(request)
    return bool(user) and bool(user.is_admin)


def _report_ctx_for(db: Storage, fact_id: int, n_accepted: int) -> dict | None:
    """Informe guardado listo para la plantilla (HTML escapado, referencias enlazadas, desactualizado?)."""
    rep = db.get_report(fact_id)
    if not rep:
        return None
    refs = rep["references"]
    return {**rep, "blocks": narrative_blocks(rep["text"], {r["n"] for r in refs}) if rep["text"] else [],
            "outdated": rep["accepted_claims_count"] != n_accepted, "now_count": n_accepted,
            "m": db.methodology(fact_id)}


def create_app(storage: Storage | None = None, llm=None, searcher=None, fetcher=None,
               pipeline_config: dict | None = None) -> FastAPI:
    """llm, searcher y fetcher son inyectables para probar sin red. Si llm es None se crea al necesitarlo."""
    app = FastAPI(title="EnEvidencia", dependencies=[Depends(csrf_check)])
    app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
    db = storage or Storage(os.environ.get("ENEVIDENCIA_DB", "data/enevidencia.db"))
    db.conn.executescript(LINKS_SCHEMA)
    app.add_middleware(BodyLimitMiddleware)  # 413 antes de csrf_check y de leer formularios
    install_auth(app, db, TEMPLATES)  # cuentas, CSRF y cabeceras de seguridad
    admin = [Depends(require_admin)]  # solo el administrador escribe; la lectura es pública
    cfg = pipeline_config if pipeline_config is not None else load_config()
    ingestion.MAX_SOURCE_CHARS = int(cfg.get("max_caracteres_fuente", ingestion.MAX_SOURCE_CHARS))
    ext_limits = {"max_chunks": int(cfg.get("max_trozos_extraccion", 12))}
    jobs: dict[str, Job] = {}
    lock = threading.Lock()
    raw_fetch = fetcher or fetch_source

    def safe_fetch(url: str):
        ingestion.validate_url(url)  # también para fetchers inyectados
        return raw_fetch(url)

    def get_llm():
        return llm if llm is not None else get_client()  # puede lanzar LLMError con instrucciones

    def _report_ctx(fact_id: int, n: int):
        return _report_ctx_for(db, fact_id, n)

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
        return {"top_topics": app.state.topics.top(3), "cards": cards, "facts": db.list_facts(), "sources": sources, "latest_sources": sources[::-1][:6],
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
                out = extract_claims(src, fact.id, model, topic=fact.title, **ext_limits)
                unverified = {id(c) for c in out.unverified}
                for c in out.claims:
                    # cita y atribución verificadas por el código -> aceptada; atribución dudosa -> queda PROPOSED
                    c.status = Status.PROPOSED if id(c) in unverified else Status.ACCEPTED
                    db.add_claim(c)
                for dsc in [*out.discarded, *out.notes]:
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
                res = extract_claims(src, fact.id, model, topic=fact.title, **ext_limits)
                for c in res.claims:
                    db.add_claim(c)
                for dsc in [*res.discarded, *res.notes]:
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

    @app.post("/facts", dependencies=admin)
    def new_fact(title: str = Form(...), description: str = Form("")):
        fact = db.add_fact(Fact(title.strip(), description.strip()))
        return RedirectResponse(f"/facts/{fact.id}", status_code=303)

    def launch_investigation(topic: str, model=None) -> Job:
        """Crea el hecho y lanza el job de investigación (gasta cuota). Lanza LLMError si no hay LLM disponible.
        Reutilizado por /investigate y por /temas/{id}/investigar."""
        model = model or get_llm()
        fact = db.add_fact(Fact(topic))
        return start(Job("investigar", fact.id, next_url=f"/facts/{fact.id}"), investigate_task(fact, model))

    app.state.launch_investigation = launch_investigation

    @app.post("/investigate", dependencies=admin)
    def investigate(request: Request, topic: str = Form(...)):
        topic = topic.strip()
        if not topic:
            return RedirectResponse("/?error=Escribe+un+tema", status_code=303)
        try:
            model = get_llm()
        except LLMError as e:  # sin clave: nada se crea; el modo manual sigue disponible
            return page(request, "index.html", **home_ctx(scrub(e)))
        job = launch_investigation(topic, model)
        return RedirectResponse(f"/jobs/{job.id}", status_code=303)

    @app.post("/facts/{fact_id}/extract", dependencies=admin)
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

    @app.post("/sources/{source_id}/status", dependencies=admin)
    def source_status(source_id: int, status: str = Form(...), next: str = Form("")):
        st = Status(status)
        db.set_source_status(source_id, st)
        if st == Status.REJECTED:  # lo descartado no puede dejar afirmaciones aceptadas en el informe
            with lock:
                db.conn.execute("UPDATE claims SET status=? WHERE source_id=? AND status!=?",
                                (Status.REJECTED.value, source_id, Status.REJECTED.value))
                db.conn.commit()
        return RedirectResponse(_local(next, f"/sources/{source_id}"), status_code=303)

    @app.post("/claims/{claim_id}/status", dependencies=admin)
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
        if request.query_params.get("grouped") and _is_admin(request):  # gasta cuota de IA: solo admin
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
                    pending_sources=n_pending_src, m=db.methodology(fact_id),
                    report=_report_ctx(fact_id, len(records)), is_admin=_is_admin(request))

    @app.get("/facts/{fact_id}/audit", response_class=HTMLResponse)
    def audit_view(request: Request, fact_id: int):
        fact = db.get_fact(fact_id)
        if not fact:
            return PlainTextResponse("Hecho no encontrado", status_code=404)
        return page(request, "audit.html", fact=fact, audit=db.audit(fact_id), m=db.methodology(fact_id), cfg=cfg)

    @app.post("/facts/{fact_id}/report/generate", dependencies=[Depends(require_admin)])
    def report_generate(request: Request, fact_id: int):
        """Redacta el informe con IA UNA vez (gasta cuota) y lo guarda. Solo admin."""
        fact = db.get_fact(fact_id)
        if not fact:
            return PlainTextResponse("No encontrado", status_code=404)
        try:
            model = get_llm()
            generate_report(db, fact, model, model=getattr(model, "model", type(model).__name__))
        except NarrativeRejected as e:
            return RedirectResponse(f"/facts/{fact_id}?error={urlquote(scrub(e))}#informe", status_code=303)
        except LLMError as e:
            return RedirectResponse(f"/facts/{fact_id}?error={urlquote(scrub(e))}#informe", status_code=303)
        return RedirectResponse(f"/facts/{fact_id}#informe", status_code=303)

    # ---- fuentes y afirmaciones manuales
    @app.post("/sources", dependencies=admin)
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

    @app.post("/sources/{source_id}/type", dependencies=admin)
    def change_type(source_id: int, source_type: str = Form(...), next: str = Form("")):
        db.set_source_type(source_id, SourceType(source_type))
        return RedirectResponse(_local(next, f"/sources/{source_id}"), status_code=303)

    @app.post("/claims", dependencies=admin)
    def new_claim(source_id: int = Form(...), fact_id: int = Form(...), statement: str = Form(...),
                  speaker: str = Form(...), quote: str = Form(...), value: str = Form("")):
        src = db.get_source(source_id)
        try:
            db.add_claim(build_claim(src, fact_id, statement, speaker, quote, value))
        except (QuoteNotInSource, ValueError) as e:
            return RedirectResponse(f"/sources/{source_id}?error={urlquote(str(e))}", status_code=303)
        return RedirectResponse(f"/facts/{fact_id}", status_code=303)

    install_topics(app, db)  # /temas (módulo propio)
    install_community(app, db)  # /comunidad y /admin/moderacion (módulo propio)
    return app


def main(argv: list[str] | None = None) -> int:
    """`enevidencia` arranca el servidor (solo 127.0.0.1); `enevidencia make-admin <usuario>` promueve/recupera un admin."""
    import sys

    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "make-admin":
        if len(args) != 2:
            print("Uso: enevidencia make-admin <usuario>")
            return 2
        from ..accounts import Accounts

        db = Storage(os.environ.get("ENEVIDENCIA_DB", "data/enevidencia.db"))
        ok = Accounts(db.conn).make_admin(args[1])
        print(f"{args[1]} es ahora administrador." if ok else f"No existe el usuario {args[1]!r}.")
        return 0 if ok else 1
    import uvicorn

    # uvicorn solo confía en cabeceras X-Forwarded-* de 127.0.0.1 (o FORWARDED_ALLOW_IPS); ver README
    uvicorn.run(create_app(), host="127.0.0.1", port=8000)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
