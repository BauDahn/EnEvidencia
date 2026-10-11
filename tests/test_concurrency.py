"""Una conexión SQLite por hilo: sin errores, sin pérdida de filas ni lastrowid cruzados."""
import threading
from concurrent.futures import ThreadPoolExecutor

from conftest import AdminClient as TestClient

from enevidencia.accounts import Accounts
from enevidencia.models import Claim, Fact, Source, SourceType
from enevidencia.storage import Storage
from enevidencia.topics import Topics
from enevidencia.web.app import create_app

N = 12


def _source(i):
    from datetime import datetime
    return Source(url=f"https://x.com/{i}", text=f"texto {i}", source_type=SourceType.SECONDARY,
                  retrieved_at=datetime(2026, 1, 1))


def _run(fn, n=N):
    barrier = threading.Barrier(n)

    def wrap(i):
        barrier.wait()
        return fn(i)

    with ThreadPoolExecutor(n) as ex:
        return list(ex.map(wrap, range(n)))  # propaga cualquier excepción


def test_escrituras_y_lecturas_concurrentes_en_archivo(tmp_path):
    db = Storage(tmp_path / "c.db")
    acc, topics = Accounts(db), Topics(db)
    fact = db.add_fact(Fact("Hecho"))
    owner = acc.register("propietario", "p@example.com", "contraseña-larga-1")
    tid = topics.suggest(owner.id, "Tema para votar en concurrencia")
    users = [acc.register(f"user{i}", f"u{i}@example.com", f"contraseña-larga-{i}") for i in range(N)]

    def work(i):
        out = []
        for k in range(5):
            s = db.add_source(_source(i * 100 + k))
            assert db.get_source(s.id).url == f"https://x.com/{i * 100 + k}"  # lastrowid propio
            c = db.add_claim(Claim(statement=f"a{i}-{k}", speaker="S", quote="q", value="", source_id=s.id,
                                   fact_id=fact.id))
            out.append((s.id, c.id))
            db.list_sources()
        u = acc.register(f"nuevo{i}", f"n{i}@example.com", f"otra-contraseña-{i}")
        assert acc.get_user(u.id).username == f"nuevo{i}"
        topics.toggle_vote(tid, users[i].id)
        return out

    res = _run(work)
    ids = [x for r in res for x, _ in r]
    assert len(ids) == len(set(ids)) == N * 5
    assert db.conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == N * 5
    assert db.conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0] == N * 5
    assert db.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1 + N + N
    assert db.conn.execute("SELECT COUNT(*) FROM topic_votes WHERE topic_id=?", (tid,)).fetchone()[0] == N
    assert db.conn.execute("SELECT COUNT(*) FROM users WHERE role='admin'").fetchone()[0] == 1
    assert db.conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    db.close()


def test_una_conexion_por_hilo(tmp_path):
    db = Storage(tmp_path / "c.db")
    assert db.conn is db.conn
    conns = _run(lambda i: (db.conn, db.conn))
    assert all(a is b for a, b in conns)
    assert len({id(a) for a, _ in conns} | {id(db.conn)}) == N + 1
    db.close()


def test_memoria_compartida_entre_hilos_y_aislada_entre_instancias():
    a, b = Storage(":memory:"), Storage(":memory:")
    a.add_fact(Fact("solo en A"))
    assert b.list_facts() == []
    seen = []
    t = threading.Thread(target=lambda: seen.extend(f.title for f in a.list_facts()))
    t.start(); t.join()
    assert seen == ["solo en A"]
    res = _run(lambda i: a.add_fact(Fact(f"h{i}")).id)
    assert len(set(res)) == N and len(a.list_facts()) == N + 1
    assert b.list_facts() == []


def test_portada_concurrente_mientras_se_insertan_datos(tmp_path):
    db = Storage(tmp_path / "c.db")
    c = TestClient(create_app(db))
    fact = db.add_fact(Fact("Hecho inicial"))
    stop = threading.Event()
    errors = []

    def writer():
        try:
            for i in range(40):
                s = db.add_source(_source(i))
                db.add_claim(Claim(statement=f"a{i}", speaker="S", quote="q", value="", source_id=s.id,
                                   fact_id=fact.id))
                db.add_fact(Fact(f"Hecho {i}"))
        except Exception as e:  # noqa: BLE001
            errors.append(e)
        finally:
            stop.set()

    def reader(i):
        n = 0
        while not stop.is_set() or n < 3:
            for path in ("/", f"/facts/{fact.id}"):
                r = c.get(path)
                assert r.status_code == 200, (path, r.status_code)
            n += 1

    w = threading.Thread(target=writer)
    w.start()
    _run(reader, 6)
    w.join()
    assert not errors
    db.close()
