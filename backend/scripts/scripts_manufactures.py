import re
import pandas as pd
from rapidfuzz import fuzz, process
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

MY_FILE = os.path.join(BASE_DIR, "my_manufacturers.xlsx")
WIDMAN_FILE = os.path.join(BASE_DIR, "widman_manufacturers.xlsx")

OUTPUT_XLSX = os.path.join(BASE_DIR, "manufacturer_result.xlsx")
OUTPUT_ALIASES = os.path.join(BASE_DIR, "aliases_ready.txt")


# =========================
# НОРМАЛИЗАЦИЯ (НОВАЯ)
# =========================

LEGAL_WORDS = [
    "ООО","ОАО","АО","ЗАО","ТОО",
    "LTD","LIMITED","GMBH","D.D","DD",
    "S.A","SA","S.P.A","SPA","SRL","S.R.L",
    "LLP","INC","CO","KG"
]

GENERIC_WORDS = [
    "PHARMA","PHARM","PHARMACEUTICAL","LAB","LABORATORY",
    "MEDICAL","GROUP","COMPANY","CO","INC"
]

def normalize(name: str) -> str:
    if not name:
        return ""

    name = str(name).upper()
    name = name.replace("Ё", "Е")

    # убрать спецсимволы
    name = re.sub(r"[\"'`.,\-]", " ", name)

    # убрать юр формы
    for w in LEGAL_WORDS:
        name = re.sub(rf"\b{w}\b", "", name)

    # оставить только буквы/цифры
    name = re.sub(r"[^A-ZА-Я0-9 ]", " ", name)

    # убрать мусор слова
    words = []
    for w in name.split():
        if w in GENERIC_WORDS:
            continue
        if len(w) <= 2:
            continue
        words.append(w)

    return " ".join(words)


def main_token(name: str) -> str:
    parts = name.split()
    return parts[0] if parts else ""


# =========================
# ЗАГРУЗКА
# =========================

my_df = pd.read_excel(MY_FILE)
widman_df = pd.read_excel(WIDMAN_FILE)

my_raw = my_df["manufacturer_raw"].dropna().tolist()
widman_raw = widman_df["manufacturer_raw"].dropna().tolist()

# нормализуем
my_norm_map = {raw: normalize(raw) for raw in my_raw}
widman_norm_map = {raw: normalize(raw) for raw in widman_raw}

my_norm_values = list(set(my_norm_map.values()))

# =========================
# MATCHING
# =========================

results = []
aliases = {}

for w_raw, w_norm in widman_norm_map.items():
    if not w_norm:
        continue

    match = process.extractOne(
        w_norm,
        my_norm_values,
        scorer=fuzz.token_set_ratio
    )

    if not match:
        continue

    best_norm, score, _ = match

    # найти оригинал
    best_raw = next((k for k, v in my_norm_map.items() if v == best_norm), "")

    # 🔒 УМНАЯ ФИЛЬТРАЦИЯ
    w_token = main_token(w_norm)
    b_token = main_token(best_norm)

    safe = False

    if score >= 95 and w_token == b_token:
        safe = True

    decision = "AUTO" if safe else "REVIEW"

    if safe:
        aliases[w_norm] = best_norm

    results.append({
        "widman_raw": w_raw,
        "widman_norm": w_norm,
        "my_raw": best_raw,
        "my_norm": best_norm,
        "score": score,
        "decision": decision
    })


# =========================
# СОХРАНЕНИЕ
# =========================

df = pd.DataFrame(results)

# excel
with pd.ExcelWriter(OUTPUT_XLSX) as writer:
    df.to_excel(writer, sheet_name="all_matches", index=False)
    df[df["decision"] == "AUTO"].to_excel(writer, sheet_name="auto", index=False)
    df[df["decision"] == "REVIEW"].to_excel(writer, sheet_name="review", index=False)

# aliases txt
with open(OUTPUT_ALIASES, "w", encoding="utf-8") as f:
    f.write("_ALIASES.update({\n")
    for k, v in sorted(aliases.items()):
        f.write(f'    "{k}": "{v}",\n')
    f.write("})\n")

print("Готово:")
print("Excel:", OUTPUT_XLSX)
print("Aliases:", OUTPUT_ALIASES)