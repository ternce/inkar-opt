from backend.app.services.competitor_matching import _strict_structure_decision, parse_drug_structure


def test_ibufen_dosage_volume_and_implicit_liquid_volume_match():
    product = parse_drug_structure("Ибуфен 100мг/5мл 100мл сусп")
    candidate = parse_drug_structure("Ибуфен 0,1/5 МЛ 100,0 СУСП")

    assert product.dosage == 100
    assert product.dosage_volume == 5
    assert product.volume == 100
    assert product.form == "СУСП"
    assert candidate.dosage == 100
    assert candidate.dosage_volume == 5
    assert candidate.volume == 100
    assert candidate.form == "СУСП"
    assert candidate.base_name == "ИБУФЕН"
    assert _strict_structure_decision(product, candidate) == ("ok", None)


def test_ibufen_rejects_different_total_volume():
    product = parse_drug_structure("Ибуфен 100мг/5мл 100мл сусп")
    candidate = parse_drug_structure("Ибуфен 0,1/5МЛ 120МЛ СУСП")

    assert candidate.dosage == 100
    assert candidate.dosage_volume == 5
    assert candidate.volume == 120
    assert candidate.form == "СУСП"
    assert _strict_structure_decision(product, candidate) == ("reject", "volume_conflict")


def test_multi_form_overlap_does_not_reject_injection_powder_ampoule():
    product = parse_drug_structure("Цефтриаксон 1г амп N1")
    candidate = parse_drug_structure("Цефтриаксон 1г пор д/ин амп N1")

    assert candidate.forms == ("ПОР", "АМП")
    assert _strict_structure_decision(product, candidate) == ("ok", None)


def test_unrelated_forms_still_reject():
    product = parse_drug_structure("Тест 100мг таб N10")
    candidate = parse_drug_structure("Тест 100мг сироп 100мл")

    assert _strict_structure_decision(product, candidate) == ("reject", "form_conflict")


def test_pack_quantity_parses_safe_common_representations():
    cases = {
        "\u211620": 20,
        "N20": 20,
        "20 \u0442\u0430\u0431": 20,
        "20 \u0448\u0442": 20,
        "2x10": 20,
        "2\u00d710": 20,
        "10x2": 20,
    }

    for raw, expected in cases.items():
        assert parse_drug_structure(raw).quantity == expected


def test_pack_quantity_does_not_multiply_volume_or_dosage_notation():
    for raw in (
        "2x5 \u043c\u043b",
        "2\u00d75 ml",
        "2x10 \u043c\u0433",
        "3x2 \u0433",
        "2x5 mg/ml",
    ):
        assert parse_drug_structure(raw).quantity is None
