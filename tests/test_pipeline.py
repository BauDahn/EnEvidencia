from enevidencia.models import Source, SourceType
from enevidencia.pipeline import filter_sources, load_config, topic_terms

CFG = load_config()


def test_terminos_del_tema_prefieren_nombres_propios_y_quitan_genericos():
    tt = topic_terms("Cierre de la fábrica Seat en Martorell, España", CFG["terminos_genericos"])
    assert tt.kind == "nombres propios" and tt.terms == ["seat", "martorell"]
    tt = topic_terms("Renta del contrato de alquiler", CFG["terminos_genericos"])
    assert tt.terms == ["renta", "alquiler"] and "contrato" not in tt.terms


def test_minimo_configurable_y_acentos():
    s = Source("https://a.es/1", "Texto sobre MARTORELL sin la otra palabra. " * 20, SourceType.SECONDARY)
    ok = filter_sources([s], "Fábrica Seat Martorell", {**CFG, "min_terminos_tema": 1})[0][0]
    no = filter_sources([s], "Fábrica Seat Martorell", {**CFG, "min_terminos_tema": 2})[0][0]
    assert ok.accepted and not no.accepted and "términos del tema" in no.reason
