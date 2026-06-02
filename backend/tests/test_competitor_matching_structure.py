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
