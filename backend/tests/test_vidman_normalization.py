from __future__ import annotations

import sys
from pathlib import Path
from decimal import Decimal

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from backend.app.db import Base
from backend.app.models import (
    VidmanAccount,
    VidmanCanonicalProduct,
    VidmanImportRun,
    VidmanNormalizedItem,
    VidmanPriceList,
    VidmanRawCanonicalLink,
    VidmanRawItem,
)
from backend.app.services.vidman_normalization import parse_vidman_product, process_vidman_stage2


def _session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _seed_raw(db, *, name: str, manufacturer: str = "Кусум Хелтхер", account_login: str = "a", main_id: int = 9373, price=100):
    account = db.scalar(select(VidmanAccount).where(VidmanAccount.login == account_login))
    if account is None:
        account = VidmanAccount(login=account_login, display_name=account_login)
        db.add(account)
        db.flush()
    price_list = db.scalar(
        select(VidmanPriceList).where(VidmanPriceList.account_id == account.id, VidmanPriceList.main_id == main_id)
    )
    if price_list is None:
        price_list = VidmanPriceList(account_id=account.id, main_id=main_id, name=f"plk {main_id}")
        db.add(price_list)
        db.flush()
    run = VidmanImportRun(account_id=account.id, status="success")
    db.add(run)
    db.flush()
    raw = VidmanRawItem(
        import_run_id=run.id,
        account_id=account.id,
        price_list_id=price_list.id,
        main_id=main_id,
        page_number=1,
        row_number=db.scalar(select(func.count(VidmanRawItem.id))) + 1,
        raw_name=name,
        raw_manufacturer=manufacturer,
        price=price,
        row_hash=f"{account_login}:{main_id}:{name}:{price}",
    )
    db.add(raw)
    db.commit()
    return raw


def _canonical_signatures(db):
    return sorted(db.scalars(select(VidmanCanonicalProduct.canonical_signature)).all())


def test_pack_count_keeps_l_cet_30_and_100_distinct():
    first = parse_vidman_product("L-Цет 5 мг №30 табл.", "Кусум Хелтхер")
    second = parse_vidman_product("L-Цет 5 мг №100 табл.", "Кусум Хелтхер")

    assert first.pack_count == 30
    assert second.pack_count == 100
    assert first.dosage_value == second.dosage_value
    assert first.normalized_signature != second.normalized_signature


def test_pack_count_keeps_l_lizin_60_and_120_distinct():
    first = parse_vidman_product("L-ЛИЗИН №60", "")
    second = parse_vidman_product("L-ЛИЗИН №120", "")

    assert first.base_name == "l-лизин"
    assert second.base_name == "l-лизин"
    assert first.normalized_signature != second.normalized_signature


def test_ambiguous_dosage_number_keeps_l_thyroxine_50_and_100_distinct():
    first = parse_vidman_product("L-тироксин 50 Берлин Хеми №50, табл.", "Берлин-Хеми")
    second = parse_vidman_product("L-тироксин 100 Берлин Хеми №50, табл.", "BERLIN CHEMIE")

    assert first.dosage_value != second.dosage_value
    assert first.dosage_unit == ""
    assert first.normalized_manufacturer == second.normalized_manufacturer == "берлин хеми"
    assert first.normalized_signature != second.normalized_signature


def test_decimal_signature_keeps_significant_integer_zeroes():
    assert "|100mg|" in parse_vidman_product("Тест 100 мг N10 табл", "").normalized_signature
    assert "|1000mg|" in parse_vidman_product("Тест 1000 мг N10 табл", "").normalized_signature
    assert "|5000iu|" in parse_vidman_product("Тест 5000 МЕ N10 капс", "").normalized_signature
    assert "|100mg|" in parse_vidman_product("Тест 100.00 мг N10 табл", "").normalized_signature
    assert "|0.05mg|" in parse_vidman_product("Тест 0.050 мг N10 табл", "").normalized_signature


def test_brand_and_product_line_numbers_do_not_become_dosage():
    parsed = parse_vidman_product("911 Венолгон гель д/ног 100 мл", "Твинс Тэк")

    assert parsed.dosage_value is None
    assert parsed.volume_value == 100
    assert parsed.volume_unit == "ml"
    assert "911" in parsed.base_name
    assert "|911|" not in parsed.normalized_signature

    kabrita = parse_vidman_product("KABRITA 1 GOLD НА КОЗЬЕМ МОЛОКЕ 800,0", "")
    assert kabrita.dosage_value is None
    assert "kabrita 1" in kabrita.base_name


def test_glued_pack_patterns_parse_without_becoming_dosage():
    tablet = parse_vidman_product("АФОБАЗОЛ 0,01N30 ТАБЛ", "")
    vial = parse_vidman_product("БИЦИЛЛИН N50ФЛ", "")
    ampoule = parse_vidman_product("МЕКСИДОЛ 2МЛN10АМП", "")

    assert tablet.pack_count == 30
    assert tablet.dosage_value == Decimal("0.01")
    assert tablet.dosage_form == "tablet"
    assert "n30" not in tablet.base_name

    assert vial.pack_count == 50
    assert vial.dosage_form == "vial"

    assert ampoule.volume_value == 2
    assert ampoule.volume_unit == "ml"
    assert ampoule.pack_count == 10
    assert ampoule.dosage_form == "ampoule"
    assert ampoule.dosage_value is None


def test_international_units_are_structured_and_preserve_value():
    parsed = parse_vidman_product("АКВАДЕТРИМ ФОРТЕ 10000 МЕ N30 КАПС", "")

    assert parsed.dosage_value == 10000
    assert parsed.dosage_unit == "iu"
    assert parsed.pack_count == 30
    assert "|10000iu|" in parsed.normalized_signature


def test_ratio_strength_preserves_package_volume_in_signature():
    first = parse_vidman_product("СУМАМЕД ФОРТЕ 0,2/5МЛ 15МЛ ПОР Д/ПРИГ СУСП", "PLIVA")
    second = parse_vidman_product("СУМАМЕД ФОРТЕ 0,2/5МЛ 30МЛ ПОР Д/ПРИГ СУСП", "PLIVA")

    assert first.concentration_value == second.concentration_value == Decimal("0.2")
    assert first.concentration_unit == second.concentration_unit == "per_5ml"
    assert first.volume_value == 15
    assert second.volume_value == 30
    assert "ratio:0.2/5ml" in first.normalized_signature
    assert first.normalized_signature != second.normalized_signature


def test_stage_2_3_sumamed_ratio_package_volume_stays_distinct():
    first = parse_vidman_product("Сумамед форте 200 мг/5 мл 15 мл", "Плива Хрватска")
    second = parse_vidman_product("Сумамед форте 200 мг/5 мл 30 мл", "Плива Хрватска")

    assert first.concentration_value == second.concentration_value == Decimal("200")
    assert first.concentration_unit == second.concentration_unit == "per_5ml"
    assert first.volume_value == Decimal("15")
    assert second.volume_value == Decimal("30")
    assert "ratio:200mg/5ml" in first.normalized_signature
    assert "package_volume:15ml" in first.normalized_signature
    assert "package_volume:30ml" in second.normalized_signature
    assert first.normalized_signature != second.normalized_signature


def test_stage_2_3_sumamed_glued_ratio_package_volume_stays_distinct():
    first = parse_vidman_product("Сумамед форте 200мг-5мл 15мл", "Pliva")
    second = parse_vidman_product("Сумамед форте 200мг-5мл 30мл", "Pliva")

    assert "ratio:200mg/5ml" in first.normalized_signature
    assert "package_volume:15ml" in first.normalized_signature
    assert "package_volume:30ml" in second.normalized_signature
    assert first.normalized_signature != second.normalized_signature


def test_stage_2_3_multi_strengths_stay_distinct():
    first = parse_vidman_product("Эдарби Кло 40 мг 12.5 мг №28 табл.", "Такеда")
    second = parse_vidman_product("Эдарби Кло 40 мг 25 мг №28 табл.", "Такеда")

    assert "strengths:40mg+12.5mg" in first.normalized_signature
    assert "strengths:40mg+25mg" in second.normalized_signature
    assert first.pack_count == second.pack_count == 28
    assert first.normalized_signature != second.normalized_signature


def test_stage_2_3_combination_strengths_preserve_all_components():
    parsed = parse_vidman_product("Амоксициллин 500 мг + Клавулановая кислота 125 мг", "")

    assert "strengths:500mg+125mg" in parsed.normalized_signature


def test_stage_2_3_product_name_equal_to_manufacturer_keeps_base_name():
    polysorb = parse_vidman_product("Полисорб МП 50 г порошок", "Полисорб МП")
    malavit = parse_vidman_product("Малавит 50 мл раствор", "Малавит")

    assert polysorb.base_name == "полисорб мп"
    assert polysorb.normalized_signature
    assert "50g" in polysorb.normalized_signature
    assert malavit.base_name == "малавит"
    assert malavit.normalized_signature
    assert "50ml" in malavit.normalized_signature


def test_non_pharmaceutical_parenthetical_variants_stay_distinct():
    first = parse_vidman_product("КОНТАКТНЫЕ ЛИНЗЫ COOPER VISION BIOMEDICS 55 EVOLUTION ASPHERE 8,6 (-4.00) N6", "")
    second = parse_vidman_product("КОНТАКТНЫЕ ЛИНЗЫ COOPER VISION BIOMEDICS 55 EVOLUTION ASPHERE 8,6 (-4.50) N6", "")
    assert first.normalized_signature != second.normalized_signature

    pad = parse_vidman_product("ЭЛЕКТРОГРЕЛКА ЗДРАВИЦА (САПОЖЕК)", "")
    medium = parse_vidman_product("ЭЛЕКТРОГРЕЛКА ЗДРАВИЦА (СРЕДНЯЯ)", "")
    assert pad.normalized_signature != medium.normalized_signature

    ribbed = parse_vidman_product("ПРЕЗЕРВАТИВЫ VIVA (РЕБРИСТЫЕ) N3", "")
    ultrathin = parse_vidman_product("ПРЕЗЕРВАТИВЫ VIVA N3 (УЛЬТРАТОНКИЕ)", "")
    assert ribbed.normalized_signature != ultrathin.normalized_signature


def test_safe_trailing_separator_oversplits_collapse():
    first = parse_vidman_product("ЦЕФ III 1,0+ЛИДО 1% 3,5МЛ В/М N1 ФЛ ХФ", "ХИМФАРМ АО")
    second = parse_vidman_product("ЦЕФ III 1,0+ЛИДО 1% 3,5МЛ В/М N1 ФЛ ХФ /", "ХИМФАРМ АО")

    assert first.normalized_signature == second.normalized_signature


def test_safe_trailing_period_and_space_cleanup_collapse():
    assert (
        parse_vidman_product("НАЗИВИН КАПЛИ НАЗ", "").normalized_signature
        == parse_vidman_product("НАЗИВИН КАПЛИ НАЗ.", "").normalized_signature
    )
    assert (
        parse_vidman_product("product  name", "").normalized_signature
        == parse_vidman_product("product name", "").normalized_signature
    )
    assert (
        parse_vidman_product("product / variant", "").normalized_signature
        == parse_vidman_product("product/variant", "").normalized_signature
    )


def test_stage_2_2_safety_cases_remain_distinct():
    assert (
        parse_vidman_product("КОНТАКТНЫЕ ЛИНЗЫ COOPER VISION BIOMEDICS 55 EVOLUTION ASPHERE 8,6 (-4.00) N6", "").normalized_signature
        != parse_vidman_product("КОНТАКТНЫЕ ЛИНЗЫ COOPER VISION BIOMEDICS 55 EVOLUTION ASPHERE 8,6 (-4.50) N6", "").normalized_signature
    )
    assert (
        parse_vidman_product("КОРРЕКТОР ОСАНКИ МЕД ЭЛАСТ N2 (0108 СОMFORT)", "").normalized_signature
        != parse_vidman_product("КОРРЕКТОР ОСАНКИ МЕД ЭЛАСТ N2 (0108-01 СОMFORT)", "").normalized_signature
    )
    assert (
        parse_vidman_product("БИНТ МЕД СТЕР 14СМХ7М (ЯЧ19Х15) BIOTENDAX", "").normalized_signature
        != parse_vidman_product("БИНТ МЕД СТЕР 14СМХ7М (ЯЧ24Х20) BIOTENDAX", "").normalized_signature
    )
    assert parse_vidman_product("L-Цет 5 мг №30 табл.", "").normalized_signature != parse_vidman_product("L-Цет 5 мг №100 табл.", "").normalized_signature
    assert (
        parse_vidman_product("СУМАМЕД ФОРТЕ 0,2/5МЛ 15МЛ ПОР Д/ПРИГ СУСП", "").normalized_signature
        != parse_vidman_product("СУМАМЕД ФОРТЕ 0,2/5МЛ 30МЛ ПОР Д/ПРИГ СУСП", "").normalized_signature
    )
    assert (
        parse_vidman_product("МУКОПЛАНТ 0,154/100МЛ СИРОП ОТ КАШЛЯ С ПЛЮЩОМ 100МЛ", "").normalized_signature
        != parse_vidman_product("МУКОПЛАНТ 0,154/100МЛ СИРОП ОТ КАШЛЯ С ПЛЮЩОМ 250МЛ", "").normalized_signature
    )
    assert (
        parse_vidman_product("ПРЕЗЕРВАТИВЫ VIVA (РЕБРИСТЫЕ) N3", "").normalized_signature
        != parse_vidman_product("ПРЕЗЕРВАТИВЫ VIVA N3 (УЛЬТРАТОНКИЕ)", "").normalized_signature
    )


def test_eye_drops_percent_and_volume_formatting_normalize_together():
    first = parse_vidman_product("L-оптик 0,5% 5мл гл.капли", "")
    second = parse_vidman_product("l оптик 0.5 % 5 мл глазные капли", "")

    assert first.concentration_value == second.concentration_value
    assert first.volume_value == second.volume_value
    assert first.dosage_form == second.dosage_form == "eye_drops"
    assert first.normalized_signature == second.normalized_signature


def test_tablet_and_capsule_form_aliases_normalize_together():
    assert parse_vidman_product("А тест таб.", "").dosage_form == "tablet"
    assert parse_vidman_product("А тест табл.", "").dosage_form == "tablet"
    assert parse_vidman_product("А тест таблетки", "").dosage_form == "tablet"
    assert parse_vidman_product("А тест капс.", "").dosage_form == "capsule"
    assert parse_vidman_product("А тест капсул", "").dosage_form == "capsule"


def test_spacing_and_decimal_aliases_normalize_together():
    assert parse_vidman_product("Тест 5мл", "").normalized_signature == parse_vidman_product("Тест 5 мл", "").normalized_signature
    assert parse_vidman_product("Тест 0,5%", "").normalized_signature == parse_vidman_product("Тест 0.5 %", "").normalized_signature


def test_empty_manufacturer_does_not_break_parser():
    parsed = parse_vidman_product("L-ЛИЗИН №60", "")

    assert parsed.normalized_manufacturer == ""
    assert parsed.base_name == "l-лизин"
    assert parsed.normalized_signature == "l-лизин|pack60"


def test_same_product_across_accounts_plks_and_prices_links_to_one_canonical():
    db = _session()
    _seed_raw(db, name="L-Цет 5 мг №30 табл.", account_login="first", main_id=9373, price=100)
    _seed_raw(db, name="L-Цет 5мг №30 табл.", account_login="second", main_id=9167, price=150)

    summary = process_vidman_stage2(db, only_unprocessed=True)

    assert summary.auto_linked == 2
    assert db.scalar(select(func.count(VidmanCanonicalProduct.id))) == 1
    canonical = db.scalar(select(VidmanCanonicalProduct))
    assert canonical.accounts_count == 2
    assert canonical.plks_count == 2
    assert db.scalar(select(func.count(VidmanRawCanonicalLink.id))) == 2


def test_different_price_does_not_create_different_canonical_product():
    db = _session()
    _seed_raw(db, name="L-Цет 5 мг №30 табл.", price=100)
    _seed_raw(db, name="L-Цет 5 мг №30 табл.", account_login="b", main_id=9373, price=200)

    process_vidman_stage2(db, only_unprocessed=True)

    assert db.scalar(select(func.count(VidmanCanonicalProduct.id))) == 1


def test_processing_is_idempotent_and_keeps_hard_conflicts_separate():
    db = _session()
    _seed_raw(db, name="L-Цет 5 мг №30 табл.", price=100)
    _seed_raw(db, name="L-Цет 5 мг №100 табл.", price=100)

    process_vidman_stage2(db, only_unprocessed=True)
    first_signatures = _canonical_signatures(db)
    process_vidman_stage2(db, only_unprocessed=True)
    second_signatures = _canonical_signatures(db)

    assert len(first_signatures) == 2
    assert first_signatures == second_signatures
    assert db.scalar(select(func.count(VidmanNormalizedItem.id))) == 2
    assert db.scalar(select(func.count(VidmanRawCanonicalLink.id))) == 2
