from enevidencia.classification import classify
from enevidencia.models import SourceType

RULES = {"rules": [
    {"type": "institutional", "reason": "dom", "domain_suffixes": [".gob.es", "boe.es"]},
    {"type": "primary", "reason": "hint", "kind_hints": ["entrevista"]},
], "default": "secondary"}


def test_domain_rule():
    assert classify("https://www.boe.es/x", rules=RULES)[0] is SourceType.INSTITUTIONAL
    assert classify("https://madrid.gob.es/x", rules=RULES)[0] is SourceType.INSTITUTIONAL


def test_kind_rule_and_default():
    assert classify("https://diario.com/a", "Entrevista", RULES)[0] is SourceType.PRIMARY
    assert classify("https://diario.com/a", "", RULES)[0] is SourceType.SECONDARY


def test_default_config_loads():
    assert classify("https://www.poderjudicial.es/s", kind="")[0] is SourceType.INSTITUTIONAL
