import os
import re
import pandas as pd
from rapidfuzz import fuzz, process

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

MY_FILE = os.path.join(BASE_DIR, "my_manufacturers.xlsx")
WIDMAN_FILE = os.path.join(BASE_DIR, "widman_manufacturers.xlsx")
MANUAL_FILE = os.path.join(BASE_DIR, "manual_aliases.xlsx")

OUTPUT_XLSX = os.path.join(BASE_DIR, "manufacturer_result.xlsx")
OUTPUT_ALIASES = os.path.join(BASE_DIR, "aliases_ready.txt")

LEGAL_WORDS = {
    "ООО", "ОАО", "АО", "ЗАО", "ПАО", "ТОО", "ИП",
    "LTD", "LIMITED", "GMBH", "D.D", "DD",
    "S.A", "SA", "S.P.A", "SPA", "SRL", "S.R.L",
    "LLP", "INC", "CORP", "CO", "KG", "AG",
}

COUNTRIES = {
    "ВЕНГРИЯ", "РОССИЯ", "ИНДИЯ", "ГЕРМАНИЯ", "ШВЕЙЦАРИЯ",
    "ФРАНЦИЯ", "ИТАЛИЯ", "ПОЛЬША", "ТУРЦИЯ", "КАЗАХСТАН",
    "США", "КИТАЙ", "ИСПАНИЯ", "СЛОВЕНИЯ", "ХОРВАТИЯ",
    "БЕЛАРУСЬ", "УКРАИНА", "ЧЕХИЯ", "БОЛГАРИЯ",
    "HUNGARY", "RUSSIA", "INDIA", "GERMANY", "SWITZERLAND",
    "FRANCE", "ITALY", "POLAND", "TURKEY", "KAZAKHSTAN",
    "USA", "CHINA", "SPAIN", "SLOVENIA", "CROATIA",
}

GENERIC_WORDS = {
    "PHARMA", "PHARM", "PHARMACEUTICAL", "PHARMACEUTICALS",
    "LABORATORY", "LABORATORIES", "MEDICAL", "GROUP",
    "COMPANY", "FACTORY", "MANUFACTURING", "MANUFACTURE",
}

KEEP_SHORT_WORDS = {
    "YS", "EG", "AB", "MS", "KR", "HK", "ST", "DR",
}

def normalize(name: str) -> str:
    if not name:
        return ""

    name = str(name).upper().replace("Ё", "Е")
    name = name.replace("&AMP;", " ")

    name = re.sub(r"[\"'`«»„“”]", " ", name)
    name = re.sub(r"[.,;:/\\(){}\[\]\-–—]+", " ", name)

    words = []

    for raw_word in name.split():
        word = raw_word.strip()
        if not word:
            continue

        if word in LEGAL_WORDS:
            continue

        if word in COUNTRIES:
            continue

        if word in GENERIC_WORDS:
            continue

        if len(word) <= 2 and word not in KEEP_SHORT_WORDS:
            continue

        words.append(word)

    return re.sub(r"\s+", " ", " ".join(words)).strip()


def significant_tokens(value: str) -> set[str]:
    return {
        token for token in normalize(value).split()
        if token and (len(token) > 2 or token in KEEP_SHORT_WORDS)
    }


def safe_auto_match(w_norm: str, best_norm: str, score: float) -> bool:
    w_tokens = significant_tokens(w_norm)
    b_tokens = significant_tokens(best_norm)

    common = w_tokens & b_tokens

    if score >= 98 and len(common) >= 1:
        return True

    if score >= 95 and len(common) >= 2:
        return True

    return False


def load_manual_aliases() -> dict[str, str]:
    if not os.path.exists(MANUAL_FILE):
        return {}

    df = pd.read_excel(MANUAL_FILE)

    required = {"alias_from", "alias_to", "status"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"manual_aliases.xlsx missing columns: {missing}")

    aliases = {}

    for _, row in df.iterrows():
        status = str(row.get("status") or "").strip().lower()
        if status not in {"approved", "auto", "yes", "true", "1"}:
            continue

        alias_from = normalize(row.get("alias_from"))
        alias_to = normalize(row.get("alias_to"))

        if alias_from and alias_to:
            aliases[alias_from] = alias_to

    return aliases


def main():
    my_df = pd.read_excel(MY_FILE)
    widman_df = pd.read_excel(WIDMAN_FILE)

    if "manufacturer_raw" not in my_df.columns:
        raise ValueError("my_manufacturers.xlsx must contain column: manufacturer_raw")

    if "manufacturer_raw" not in widman_df.columns:
        raise ValueError("widman_manufacturers.xlsx must contain column: manufacturer_raw")

    my_raw = my_df["manufacturer_raw"].dropna().astype(str).tolist()
    widman_raw = widman_df["manufacturer_raw"].dropna().astype(str).tolist()

    my_norm_map = {raw: normalize(raw) for raw in my_raw}
    widman_norm_map = {raw: normalize(raw) for raw in widman_raw}

    my_norm_values = sorted(set(v for v in my_norm_map.values() if v))

    norm_to_my_raw = {}
    for raw, norm in my_norm_map.items():
        if norm:
            norm_to_my_raw.setdefault(norm, raw)

    manual_aliases = load_manual_aliases()

    results = []
    aliases = {}

    for w_raw, w_norm in widman_norm_map.items():
        if not w_norm:
            continue

        if w_norm in manual_aliases:
            best_norm = manual_aliases[w_norm]
            best_raw = norm_to_my_raw.get(best_norm, "")
            score = 100
            decision = "MANUAL"
            aliases[w_norm] = best_norm
        else:
            match = process.extractOne(
                w_norm,
                my_norm_values,
                scorer=fuzz.token_set_ratio,
            )

            if not match:
                continue

            best_norm, score, _ = match
            best_raw = norm_to_my_raw.get(best_norm, "")

            decision = "AUTO" if safe_auto_match(w_norm, best_norm, score) else "REVIEW"

            if decision == "AUTO":
                aliases[w_norm] = best_norm

        results.append({
            "widman_raw": w_raw,
            "widman_norm": w_norm,
            "my_raw": best_raw,
            "my_norm": best_norm,
            "score": score,
            "common_tokens": ", ".join(sorted(significant_tokens(w_norm) & significant_tokens(best_norm))),
            "decision": decision,
        })

    df = pd.DataFrame(results)

    groups_df = (
        pd.DataFrame([
            {
                "widman_norm": norm,
                "widman_raw_variants": " | ".join(sorted(set(raws))),
                "variants_count": len(set(raws)),
            }
            for norm, raws in _group_widman(widman_norm_map).items()
        ])
        .sort_values(["variants_count", "widman_norm"], ascending=[False, True])
    )

    alias_df = pd.DataFrame([
        {"alias_from": k, "alias_to": v}
        for k, v in sorted(aliases.items())
    ])

    with pd.ExcelWriter(OUTPUT_XLSX) as writer:
        df.to_excel(writer, sheet_name="all_matches", index=False)
        df[df["decision"] == "AUTO"].to_excel(writer, sheet_name="auto", index=False)
        df[df["decision"] == "MANUAL"].to_excel(writer, sheet_name="manual", index=False)
        df[df["decision"] == "REVIEW"].to_excel(writer, sheet_name="review", index=False)
        groups_df.to_excel(writer, sheet_name="groups", index=False)
        alias_df.to_excel(writer, sheet_name="aliases", index=False)

    with open(OUTPUT_ALIASES, "w", encoding="utf-8") as f:
        f.write("_ALIASES.update({\n")
        for k, v in sorted(aliases.items()):
            f.write(f'    "{k}": "{v}",\n')
        f.write("})\n")

    print("Готово:")
    print("Excel:", OUTPUT_XLSX)
    print("Aliases:", OUTPUT_ALIASES)
    print("AUTO:", len(df[df["decision"] == "AUTO"]))
    print("MANUAL:", len(df[df["decision"] == "MANUAL"]))
    print("REVIEW:", len(df[df["decision"] == "REVIEW"]))


def _group_widman(widman_norm_map: dict[str, str]) -> dict[str, list[str]]:
    groups = {}
    for raw, norm in widman_norm_map.items():
        if not norm:
            continue
        groups.setdefault(norm, []).append(raw)
    return groups


if __name__ == "__main__":
    main()