from __future__ import annotations

from decimal import Decimal

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from backend.app.models import Base, InternalProductNormalized, Product, ProductExtra, VidmanCanonicalProduct
from backend.app.services.internal_product_normalization import (
    parse_internal_product,
    process_internal_product_normalization,
)
from backend.app.services.vidman_normalization import parse_vidman_product
from backend.app.services.vidman_product_matching import AUTO_MATCHED, build_product_indexes, decide_match


def _session():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, future=True)()


def _product(db, code: str, name: str, manufacturer: str = "Maker") -> Product:
    row = Product(code=code, name=name, cost=Decimal("1"))
    db.add(row)
    db.flush()
    db.add(ProductExtra(product_id=row.id, manufacturer=manufacturer))
    db.flush()
    return row


def _canonical(db, name: str, manufacturer: str = "Maker") -> VidmanCanonicalProduct:
    parsed = parse_vidman_product(name, manufacturer)
    row = VidmanCanonicalProduct(
        canonical_name=parsed.base_name,
        canonical_manufacturer=parsed.normalized_manufacturer,
        base_name=parsed.base_name,
        dosage_value=parsed.dosage_value,
        dosage_unit=parsed.dosage_unit,
        concentration_value=parsed.concentration_value,
        concentration_unit=parsed.concentration_unit,
        volume_value=parsed.volume_value,
        volume_unit=parsed.volume_unit,
        weight_value=parsed.weight_value,
        weight_unit=parsed.weight_unit,
        pack_count=parsed.pack_count,
        dosage_form=parsed.dosage_form,
        canonical_signature=parsed.normalized_signature,
    )
    db.add(row)
    db.flush()
    return row


def test_internal_formatting_variants_share_normalized_identity():
    first = parse_internal_product("Элькар 300мг/мл 50мл р-р per os", "ПИК-ФАРМА ПРО ООО")
    second = parse_internal_product("Элькар 300 мг / мл 50 мл раствор per os", "ПИК ФАРМА ПРО ООО")

    assert first.normalized_signature == second.normalized_signature
    assert "ratio:300mg" in first.normalized_signature
    assert "50ml" in first.normalized_signature


def test_internal_identity_safety_cases_stay_distinct():
    assert parse_internal_product("L-тироксин 50 №50 таб", "Berlin Chemie").normalized_signature != parse_internal_product(
        "L-тироксин 100 №50 таб", "Berlin Chemie"
    ).normalized_signature
    assert parse_internal_product("L-Цет 5мг №30 таб", "").normalized_signature != parse_internal_product(
        "L-Цет 5мг №100 таб", ""
    ).normalized_signature
    assert parse_internal_product("Аммиак 10% 20мл Фармация", "Фармация").normalized_signature != parse_internal_product(
        "Аммиак 10% 40мл Фармация", "Фармация"
    ).normalized_signature
    assert parse_internal_product("Тантум Верде 0,15% 120мл раствор", "").normalized_signature != parse_internal_product(
        "Тантум Верде 0,30% 120мл раствор", ""
    ).normalized_signature
    assert parse_internal_product("Агвистат 10/160мг №28 таб", "Вива Фарм ТОО").normalized_signature != parse_internal_product(
        "Агвистат 10/320мг №28 таб", "Вива Фарм ТОО"
    ).normalized_signature
    assert parse_internal_product("Сумамед Форте 200мг/5мл 15мл", "").normalized_signature != parse_internal_product(
        "Сумамед Форте 200мг/5мл 30мл", ""
    ).normalized_signature


def test_internal_manufacturer_and_non_pharma_tokens_are_preserved():
    parsed = parse_internal_product("Перекись водорода 3% 40мл Фармация", "Фармация")
    assert parsed.base_name == "перекись водорода"
    assert parsed.normalized_manufacturer == "фармация"

    manufacturer_name = parse_internal_product("Малавит 50мл раствор", "Малавит")
    assert manufacturer_name.base_name == "малавит"

    model_0108 = parse_internal_product("Корректор осанки №1 мод 0108 Comfort беж", "Tonus Elast ООО")
    model_0109 = parse_internal_product("Корректор осанки №1 мод 0109 Comfort беж", "Tonus Elast ООО")
    assert model_0108.normalized_signature != model_0109.normalized_signature

    small = parse_internal_product("Бинт стер 5х10см Baxtteks", "Baxtteks-Farm OOO")
    large = parse_internal_product("Бинт стер 7х14см Baxtteks", "Baxtteks-Farm OOO")
    assert small.normalized_signature != large.normalized_signature


def test_rebuild_writes_separate_internal_normalized_table_only():
    db = _session()
    product = _product(db, "SKU-1", "Доксициклин-ТК 100мг №10 капс", "ТОО ТК Фарм Актобе")
    original_name = product.name
    original_manufacturer = db.get(ProductExtra, product.id).manufacturer

    summary = process_internal_product_normalization(db, dry_run=False, rebuild=True)

    assert summary.normalized_products == 1
    assert db.scalar(select(func.count(InternalProductNormalized.id))) == 1
    normalized = db.scalar(select(InternalProductNormalized))
    assert normalized.product_id == product.id
    assert normalized.pack_count == 10
    assert normalized.dosage_form == "capsule"
    assert db.get(Product, product.id).name == original_name
    assert db.get(ProductExtra, product.id).manufacturer == original_manufacturer


def test_stage_3_indexes_prefer_stored_internal_normalized_rows():
    db = _session()
    canonical = _canonical(db, "Drug 10mg N20", "Maker")
    product = _product(db, "SKU-1", "Completely different raw name", "Maker")

    process_internal_product_normalization(db, dry_run=False, rebuild=True)
    normalized = db.scalar(select(InternalProductNormalized).where(InternalProductNormalized.product_id == product.id))
    parsed = parse_internal_product("Drug 10mg N20", "Maker")
    normalized.raw_name = product.name
    normalized.normalized_name = parsed.normalized_name
    normalized.base_name = parsed.base_name
    normalized.normalized_manufacturer = parsed.normalized_manufacturer
    normalized.dosage_value = parsed.dosage_value
    normalized.dosage_unit = parsed.dosage_unit
    normalized.pack_count = parsed.pack_count
    normalized.normalized_signature = parsed.normalized_signature
    db.commit()

    decision = decide_match(canonical, build_product_indexes(db))

    assert decision.status == AUTO_MATCHED
    assert decision.product_id == product.id
