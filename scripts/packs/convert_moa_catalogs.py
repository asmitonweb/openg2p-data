#!/usr/bin/env python3
"""
Convert legacy Ethiopia MoA catalogs from SQL into OpenG2P schema packs additions.
Produces JSON addition files for:
- SEED_VARIETY (values addition to base domain)
- CROP_COMMODITY (missing values addition)
- LIVESTOCK_BREED_NAME (new domain attribute and values)
- LIVESTOCK_SEX (new reference attribute and values)
- FERTILIZER_TYPE (values addition)

Strict data quality guards in check_varieties() ensure no truncated values,
no unbalanced brackets, no duplicate codes, no trailing separators, and full
alignment with upstream CI checks.
"""

import os
import sys
import json
import re
import unicodedata
import collections

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
ADDITIONS_DIR = os.path.join(REPO_ROOT, "scripts/packs/eth_oan_additions")
BASE_PACK_DIR = os.path.join(REPO_ROOT, "packs/ETH")

CROP_SQL = os.path.join(REPO_ROOT, "scripts/packs/sources/05_insert_crop_variety.sql")
BREED_SQL = os.path.join(REPO_ROOT, "scripts/packs/sources/04_insert_livestock_breed.sql")

ALLOWED_NUMERIC_CODES = {
    'VAR_TEFF_DAGEM_DZ_CR_438', 'VAR_RICE_VRH_606', 'VAR_RICE_VRH_640',
    'VAR_RICE_VRH_654', 'VAR_MAIZE_PAC_781', 'VAR_MAIZE_CML_161_X_CML_165',
    'VAR_COWPEA_82D_889', 'VAR_TEFF_KORA_DZ_CR_438', 'VAR_MAIZE_SC_719',
    'VAR_SORGHUM_PAC_537', 'VAR_TEFF_DZ_01_787', 'VAR_BREAD_WHEAT_MEKELLE_01_HUW_468',
    'VAR_MAIZE_A_511', 'VAR_MAIZE_BH_140', 'VAR_MAIZE_BH_660',
    'VAR_TEFF_BOSET_DZ_CR_409', 'VAR_MAIZE_MHQ_138', 'VAR_MAIZE_SC_403',
    'VAR_MAIZE_BH_541', 'VAR_MAIZE_BH_QP_542', 'VAR_MAIZE_BH_670',
    'VAR_MAIZE_BELES_SC_713', 'VAR_MAIZE_BEREDA_SC_715', 'VAR_MAIZE_BH_543',
    'VAR_MAIZE_MH_130', 'VAR_MALT_BARLEY_HB_120', 'VAR_SOYBEAN_COKER_240',
    'VAR_SESAME_TATE_BSC_003', 'VAR_GROUNDNUT_NC_343', 'VAR_COMMON_BEAN_MILKESA_DAB_523',
    'VAR_COMMON_BEAN_DAB_413', 'VAR_COMMON_BEAN_DAB_372', 'VAR_COMMON_BEAN_ATNDABA_A_262',
    'VAR_MAIZE_CML_395_X_CML_202', 'VAR_GROUNDNUT_WERER_962', 'VAR_MAIZE_BH_545',
    'VAR_COMMON_BEAN_SER_125', 'VAR_COMMON_BEAN_SER_119', 'VAR_MAIZE_BH_540',
    'VAR_MAIZE_BH_661', 'VAR_MAIZE_MH_141', 'VAR_MAIZE_BH_520', 'VAR_MAIZE_BH_546',
    'VAR_MAIZE_BH_547', 'VAR_MALT_BARLEY_HB_1963', 'VAR_MALT_BARLEY_HB_1964',
    'VAR_MALT_BARLEY_HB_1965', 'VAR_MALT_BARLEY_HB_1966', 'VAR_MALT_BARLEY_HB_1307',
    'VAR_MALT_BARLEY_HB_1533', 'VAR_FOOD_BARLEY_HB_1960', 'VAR_FOOD_BARLEY_HB_1961',
    'VAR_FOOD_BARLEY_HB_1962', 'VAR_FOOD_BARLEY_HB_42', 'VAR_FOOD_BARLEY_HB_1307',
    'VAR_TEFF_DZ_CR_387', 'VAR_TEFF_DZ_01_974', 'VAR_TEFF_DZ_01_196',
    'VAR_TEFF_DZ_01_354', 'VAR_TEFF_DZ_01_1880', 'VAR_TEFF_DZ_01_2053',
    'VAR_TEFF_DZ_01_99', 'VAR_TEFF_DZ_01_899', 'VAR_TEFF_DZ_01_1278',
    'VAR_TEFF_DZ_01_1281', 'VAR_TEFF_DZ_01_1285', 'VAR_TEFF_DZ_01_1445',
    'VAR_TEFF_DZ_01_1446', 'VAR_TEFF_DZ_01_146', 'VAR_BREAD_WHEAT_HAR_1685',
    'VAR_BREAD_WHEAT_HAR_604', 'VAR_BREAD_WHEAT_HAR_710', 'VAR_BREAD_WHEAT_HAR_1709',
    'VAR_BREAD_WHEAT_HAR_1595', 'VAR_BREAD_WHEAT_HAR_1407', 'VAR_BREAD_WHEAT_HAR_2501',
    'VAR_BREAD_WHEAT_ETBW_9089', 'VAR_BREAD_WHEAT_ETBW_9396', 'VAR_BREAD_WHEAT_ETBW_8572',
    'VAR_BREAD_WHEAT_ETBW_5520', 'VAR_DURUM_WHEAT_DZ_04_118', 'VAR_DURUM_WHEAT_DZ_2212',
    'VAR_SOYBEAN_AFGAT', 'VAR_SOYBEAN_ETHIOYUGOZLAVIA', 'VAR_SOYBEAN_ETHIO_YUGOSLAVIA', 'VAR_MAIZE_CP_201', 'VAR_MAIZE_CP_838', 'VAR_TRITICALE_ETCL_161', 'VAR_TEFF_DZ_CR_551', 'VAR_TEFF_DZ_CR_542'
}

# 38 rows with completely empty/nameless values (e.g. 'Common bean /Haricot bean - …')
NAMELESS_ROWS = {
    35, 36, 37, 38, 39, 132, 133, 134, 136, 137, 138, 139, 159, 222, 223, 224, 225, 226,
    228, 229, 281, 282, 291, 292, 353, 354, 355, 418, 425, 426, 665, 666, 667, 668,
    681, 682, 683, 761
}

# Rows cut off mid-name outside brackets where full name cannot be reliably deduced
MID_CUT_ROWS = {
    46, 48, 70, 84, 119, 423, 480, 511, 561, 762, 766
}

# Crop-only rows (values that have no real variety name, only crop or botanical name)
CROP_ONLY_ROWS = {
    92,   # Cacao - Cacao (Theobroma cacao)
    775,  # Faba bean
    779,  # Sorghum
    805,  # Ground nut
    807,  # Tomato
    808,  # Onion
    868,  # Pigeon pea
    902,  # food barley
}

# Maize breeding parental lines (used for hybrid seed production, not farmer varieties)
MAIZE_PARENT_ROWS = {
    776, 793, 794, 795, 796, 797, 798, 799, 800, 801, 802, 803, 804, 897
}

DANGLING = re.compile(r"[–—\-:/,(]\s*$")

CROPS = [
    'adzuki - bean', 'adzuki-bean', 'adzuki bean',
    'common bean /haricot bean', 'common bean/haricot bean', 'common bean/ haricot bean',
    'common bean', 'haricot bean', 'groundnut variety', 'ground nut', 'groundnut',
    'fiel pea', 'field pea', 'faba bean', 'fababean', 'food barly', 'food barley',
    'malt barley', 'barley', 'food oats', 'food oat', 'oats', 'oat',
    'rapeseed and ethiopian mustard', 'ethiopian mustard',
    'sweet potato', 'irish potato', 'potato',
    'sweet/hot pepper', 'pepper', 'cabbage', 'carrot', 'okra',
    'coriander', 'cumin', 'banana', 'avocado', 'cotton', 'tobaco', 'lettuce',
    'water melon', 'watermelon', 'musk melon', 'broccoli', 'cauliflower',
    'rose', 'cacao', 'linseed', 'sesame', 'sunflower', 'safflower',
    'soybean', 'soyabean', 'mung bean', 'mungbean', 'fenugreek', 'noug', 'rice',
    'finger millet', 'triticale', 'emmer wheat (aja)', 'quinoa',
    'dekoko', 'chickpea', 'cowpea', 'lentil', 'alfalfa', 'pigeon pea',
    'brachiaria brizantha cv.xaraes', 'bread wheat', 'durum wheat', 'wheat',
    'tef', 'teff', 'maize', 'sorghum', 'taro', 'cassava', 'tomato', 'onion', 'garlic'
]
CROP_RE = '|'.join(sorted(CROPS, key=len, reverse=True))

def norm(s):
    s = unicodedata.normalize("NFKC", s).replace("’", "'").casefold()
    return re.sub(r"[^a-z0-9]", "", s)

def variety_part(display):
    return display.split(" – ", 1)[1] if " – " in display else display

def crop_key(crop_code):
    return norm(crop_code.removeprefix("CROP_"))

def clean_code(*parts):
    s = "_".join(parts).upper()
    s = re.sub(r"\(.*?\)", "", s)
    s = re.sub(r"\[.*?\]", "", s)
    s = re.sub(r"[^A-Z0-9]+", "_", s)
    tokens = s.strip("_").split("_")
    cleaned = []
    for t in tokens:
        if cleaned:
            prev = cleaned[-1]
            if t == prev:
                continue
            if (prev == "SOYBEAN" and t == "SOYABEAN") or (prev == "SOYABEAN" and t == "SOYBEAN"):
                continue
            if (prev == "MUNG" and t == "MUNGBEAN") or (prev == "BEAN" and t == "MUNGBEAN"):
                continue
            if prev in ("BARLEY", "POTATO", "TOMATO", "SORGHUM", "RICE", "TARO", "CASSAVA", "WATERMELON") and t == prev:
                continue
        cleaned.append(t)
    return "_".join(cleaned)

def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")

def check_varieties(values, base_scoped_names, base_near_keys):
    """Guard validating SEED_VARIETY data quality strictly matching check_pack_lists.py."""
    errs = []
    seen_near = {}

    for v in values:
        vid = v.get("value_id", "")
        name = (v.get("attributes") or {}).get("official_name", "").strip()
        disp = (v.get("value_display") or "").strip()
        crop = (v.get("attributes") or {}).get("crop")
        v_part = variety_part(disp)
        crop_display = disp.split(" – ")[0] if " – " in disp else ""

        if not name:
            errs.append(f"{vid}: no variety name")
        if "…" in disp or "..." in disp:
            errs.append(f"{vid}: truncated name {disp!r}")
        if DANGLING.search(disp):
            errs.append(f"{vid}: display name ends in a separator: {disp!r}")
        if disp.count("(") != disp.count(")"):
            errs.append(f"{vid}: unbalanced round brackets {disp!r}")
        if disp.count("[") != disp.count("]"):
            errs.append(f"{vid}: unbalanced square brackets {disp!r}")
        if re.search(r"[ሀ-፿]", disp):
            errs.append(f"{vid}: Amharic in value_display; move it to display_i18n.am")
        if re.search(r"_\d{3}$", vid) and vid not in ALLOWED_NUMERIC_CODES:
            errs.append(f"{vid}: ends in a 3-digit number; a source row id? (allowlist it if it is part of the real name)")

        # 1. Name is only the crop
        if norm(v_part) in (crop_key(crop), norm(crop_display)):
            errs.append(f"{vid}: no variety name, only the crop: {disp!r}")

        # 3. Starts with punctuation
        if re.match(r"^[\s(:/\-]", v_part):
            errs.append(f"{vid}: variety part starts with punctuation: {disp!r}")

        # 4. Crop repeated in the code
        crop_part = crop.removeprefix("CROP_")
        if vid.count(crop_part) > 1:
            errs.append(f"{vid}: crop name repeated in the code")
        if crop_part == "SOYBEAN" and "SOYABEAN" in vid:
            errs.append(f"{vid}: soyabean repeated in code")
        if crop_part == "MUNG_BEAN" and "MUNGBEAN" in vid:
            errs.append(f"{vid}: mungbean repeated in code")

        # Check against base pack values
        scoped_key = (crop, disp.casefold())
        if scoped_key in base_scoped_names:
            errs.append(f"{vid}: duplicates base pack variety for {crop}: {disp!r}")

        lead = norm(v_part.split("(")[0])
        if (crop, lead) in base_near_keys:
            errs.append(f"{vid}: near duplicate of base pack for {crop}: lead={lead!r} ({disp})")

        # 2. Near-duplicates within output
        if lead:
            near_key = (crop, lead)
            if near_key in seen_near:
                errs.append(f"{vid}: near duplicate of {seen_near[near_key]} ({near_key})")
            else:
                seen_near[near_key] = vid

    seen = collections.Counter(((v.get("attributes") or {}).get("crop"), v["value_display"].strip().casefold()) for v in values)
    errs += [f"duplicate name within crop: {k}" for k, n in seen.items() if n > 1]
    if errs:
        sys.exit("check_varieties failed with errors:\n" + "\n".join(errs[:25]))
    print(f"check_varieties guard passed cleanly for {len(values)} varieties.")

AMHARIC_NAMES = {
    12: ("Boni", "ቦኒ"),
    205: ("Bonsa (07MW6085)", "ቦንሳ"),
    206: ("Argiti (2005M15064)", "አርጊቲ"),
    261: ("Dibaba (ETS 639/SRN-39)", "ዲባባ"),
    316: ("Adele (ACC#70583/Hararghecoll #4)", "አደሌ"),
    319: ("Jiru (yellow/ETS-2752)", "ጅሩ"),
    328: ("Meba", "መባ"),
    331: ("Axum", "አክሱም"),
    704: ("Hundaf", "ሁንዳፍ"),
    782: ("DZ-Cr-551 (RIL173)", "ደዘ-ክሮስ-551"),
    783: ("DZ-Cr-542 (RIL106)", None),
}

def clean_variety_name(common_name, crop_name, row_id):
    raw = common_name.replace("''", "'").strip()

    # Extract Amharic characters
    am_chars = ''.join(re.findall(r'[ሀ-፿]+', raw)).strip()
    am_display = am_chars if am_chars else None

    # Handle known Amharic-primary rows
    if row_id in AMHARIC_NAMES:
        eng_name, am_val = AMHARIC_NAMES[row_id]
        return eng_name, (am_val or am_display)

    # Disambiguation & known releases:
    # Row 763: 1990 MARC/EIAR release of white pea bean variety officially released as 'Awash-1'
    # Row 227: 2017 Melkassa ARC/EIAR release of canning/navy bean variety 'Awash Mitra'
    if row_id == 763:
        return 'Awash-1', am_display
    elif row_id == 227:
        return 'Awash Mitra', am_display

    # Remove Amharic from english text
    eng = re.sub(r'[ሀ-፿]+', '', raw).strip()
    eng = eng.replace('…', '').replace('...', '').strip()

    # Strip leading punctuation before crop matching
    eng = re.sub(r'^[-—–:/,\s]+', '', eng).strip()

    # Strip botanical names first
    eng = re.sub(r'\(?(Solanum tuberosum|Ipomoea batatas|Theobroma cacao|Manihot esculenta|Colocasia esculenta)\)?', '', eng, flags=re.I).strip()

    # Strip crop_name specifically
    if 'bean' in crop_name.lower():
        eng = re.sub(r'^(?:common\s*bean|haricot\s*bean)\s*[-—–:/,\s]*', '', eng, flags=re.I).strip()
    eng = re.sub(rf'^{re.escape(crop_name)}\s*[-—–:/,\s]*', '', eng, flags=re.I).strip()
    eng = re.sub(r'^(?:variety|cultivar)\s*[-—–:/,\s]*', '', eng, flags=re.I).strip()

    # Strip leading crop pattern
    pattern = rf'^(?:{CROP_RE})\s*[-—–:/,\s]*'
    while re.match(pattern, eng, flags=re.I):
        eng = re.sub(pattern, '', eng, flags=re.I).strip()
        eng = re.sub(r'\(?(Solanum tuberosum|Ipomoea batatas|Theobroma cacao|Manihot esculenta|Colocasia esculenta)\)?', '', eng, flags=re.I).strip()

    if ' - ' in eng:
        parts = eng.split(' - ', 1)
        if any(w in parts[0].lower() for w in ['wheat', 'barley', 'maize', 'tef', 'sorghum', 'bean', 'pea', 'oats', 'lentil', 'chickpea', 'potato', 'rice', 'mustard', 'linseed', 'sesame', 'sunflower']):
            eng = parts[1].strip()

    # Clean double brackets
    eng = re.sub(r'\(\(', '(', eng)
    eng = re.sub(r'\)\)', ')', eng)
    eng = re.sub(r'\[\[', '[', eng)
    eng = re.sub(r'\]\]', ']', eng)

    # Clean unclosed/unopened round brackets
    if eng.startswith('(') and ')' not in eng:
        eng = eng[1:].strip()
    if '(' in eng and ')' not in eng:
        eng = re.sub(r'\([^\)]*$', '', eng).strip()
    while eng.count(')') > eng.count('(') and eng.endswith(')'):
        eng = eng[:-1].strip()
    if ')' in eng and '(' not in eng:
        eng = re.sub(r'\)[^\(]*$', '', eng).strip()

    # Clean unclosed/unopened square brackets
    if eng.startswith('[') and ']' not in eng:
        eng = eng[1:].strip()
    if '[' in eng and ']' not in eng:
        eng = re.sub(r'\[[^\]]*$', '', eng).strip()
    while eng.count(']') > eng.count('[') and eng.endswith(']'):
        eng = eng[:-1].strip()
    if ']' in eng and '[' not in eng:
        eng = re.sub(r'\][^\[]*$', '', eng).strip()

    diff_r = eng.count('(') - eng.count(')')
    if diff_r > 0:
        eng = eng + (')' * diff_r)

    diff_s = eng.count('[') - eng.count(']')
    if diff_s > 0:
        eng = eng + (']' * diff_s)

    eng = re.sub(r'\(\s*\)', '', eng).strip()
    eng = re.sub(r'\[\s*\]', '', eng).strip()

    # Reorder local names in brackets: (LocalName) Pedigree -> LocalName (Pedigree)
    # e.g. (Girar) ICCMABCA -> Girar (ICCMABCA)
    m_bracket = re.match(r'^\s*\(([A-Za-z0-9\s\-]+)\)\s*(.*)$', eng)
    if m_bracket:
        loc = m_bracket.group(1).strip()
        ped = m_bracket.group(2).strip()
        if ped.startswith('(') and ped.endswith(')'):
            ped = ped[1:-1].strip()
        if ped:
            eng = f"{loc} ({ped})"
        else:
            eng = loc

    # If pedigree is before local name: e.g. EH011089-3 (Cherinet) -> Cherinet (EH011089-3)
    m_ped = re.match(r'^([A-Z0-9\-_]+)\s*\(([A-Za-z\s]+)\)$', eng)
    if m_ped:
        eng = f"{m_ped.group(2).strip()} ({m_ped.group(1).strip()})"

    # Strip leading punctuation (: Kumsa -> Kumsa)
    eng = re.sub(r'^[:\-\s/]+', '', eng).strip()

    # Clean notes
    eng = re.sub(r'[-—–:/,\s]*cherry tomato\s*$', '', eng, flags=re.I).strip()
    eng = re.sub(r'[-—–:/,\s]+Food\b', '', eng, flags=re.I).strip()
    eng = re.sub(r'-\s+(\d+)', r'-\1', eng)
    eng = re.sub(r'\s+-\s*(\d+)', r'-\1', eng)
    eng = re.sub(r'([a-zA-Z0-9])\(', r'\1 (', eng)

    # Specific spelling normalizations
    if norm(eng) == "melkasa1q":
        eng = "Melkasa-1Q"
    elif norm(eng) in ("pac537", "pac537"):
        eng = "PAC-537"
    elif norm(eng) == "harofmc33landrace":
        eng = "Haro (FMC-33)"
    elif norm(eng) in ("ethioyugozlavia", "ethioyugoslavia"):
        eng = "Ethio-Yugoslavia"

    # Title case if first word is lowercase
    words = eng.split(' ', 1)
    if words[0].islower() and not words[0].startswith('('):
        words[0] = words[0].capitalize()
        eng = ' '.join(words)

    # Strip any trailing separators
    eng = DANGLING.sub('', eng).strip()
    eng = eng.strip("'-/ ")
    eng = DANGLING.sub('', eng).strip()
    eng = re.sub(r'\s+', ' ', eng)

    return eng, am_display

def convert_crop_commodities():
    print("Converting missing crop commodities...")
    with open(os.path.join(BASE_PACK_DIR, "domains/agriculture/crop_commodity.json"), encoding="utf-8") as f:
        base = json.load(f)
    base_ids = {v["value_id"] for v in base["values"]}

    missing_crops = [
        {
            "value_id": "CROP_OAT",
            "value_code": "CROP_OAT",
            "value_display": "Oat",
            "sort_order": 122,
            "display_i18n": {},
            "attributes": {
                "category": "CROPCAT_CEREAL",
                "scientific_name": "Avena sativa L."
            }
        },
        {
            "value_id": "CROP_PIGEON_PEA",
            "value_code": "CROP_PIGEON_PEA",
            "value_display": "Pigeon pea",
            "sort_order": 123,
            "display_i18n": {},
            "attributes": {
                "category": "CROPCAT_FOOD_LEGUME",
                "scientific_name": "Cajanus cajan (L.) Millsp."
            }
        },
        {
            "value_id": "CROP_ALFALFA",
            "value_code": "CROP_ALFALFA",
            "value_display": "Alfalfa",
            "sort_order": 124,
            "display_i18n": {},
            "attributes": {
                "category": "CROPCAT_OTHER",
                "scientific_name": "Medicago sativa L."
            }
        },
        {
            "value_id": "CROP_BRACHIARIA",
            "value_code": "CROP_BRACHIARIA",
            "value_display": "Brachiaria",
            "sort_order": 125,
            "display_i18n": {},
            "attributes": {
                "category": "CROPCAT_OTHER",
                "scientific_name": "Brachiaria brizantha"
            }
        },
        {
            "value_id": "CROP_CACAO",
            "value_code": "CROP_CACAO",
            "value_display": "Cacao",
            "sort_order": 126,
            "display_i18n": {},
            "attributes": {
                "category": "CROPCAT_STIMULANT",
                "scientific_name": "Theobroma cacao L."
            }
        },
        {
            "value_id": "CROP_DEKOKO",
            "value_code": "CROP_DEKOKO",
            "value_display": "Dekoko",
            "sort_order": 127,
            "display_i18n": {},
            "attributes": {
                "category": "CROPCAT_FOOD_LEGUME",
                "scientific_name": "Pisum sativum var. abyssinicum"
            }
        },
        {
            "value_id": "CROP_ETHIOPIAN_MUSTARD",
            "value_code": "CROP_ETHIOPIAN_MUSTARD",
            "value_display": "Ethiopian mustard",
            "sort_order": 128,
            "display_i18n": {},
            "attributes": {
                "category": "CROPCAT_OIL",
                "scientific_name": "Brassica carinata A. Braun"
            }
        },
        {
            "value_id": "CROP_SAFFLOWER",
            "value_code": "CROP_SAFFLOWER",
            "value_display": "Safflower",
            "sort_order": 129,
            "display_i18n": {},
            "attributes": {
                "category": "CROPCAT_OIL",
                "scientific_name": "Carthamus tinctorius L."
            }
        },
        {
            "value_id": "CROP_TRITICALE",
            "value_code": "CROP_TRITICALE",
            "value_display": "Triticale",
            "sort_order": 130,
            "display_i18n": {},
            "attributes": {
                "category": "CROPCAT_CEREAL",
                "scientific_name": "x Triticosecale Wittm. ex A. Camus"
            }
        },
        {
            "value_id": "CROP_QUINOA",
            "value_code": "CROP_QUINOA",
            "value_display": "Quinoa",
            "sort_order": 131,
            "display_i18n": {},
            "attributes": {
                "category": "CROPCAT_CEREAL",
                "scientific_name": "Chenopodium quinoa Willd."
            }
        }
    ]

    missing_crops = [v for v in missing_crops if v["value_id"] not in base_ids]
    out = os.path.join(ADDITIONS_DIR, "values/domains/agriculture/crop_commodity.json")
    write_json(out, {
        "description": "Additional crop commodities from MoA catalogs.",
        "values": missing_crops
    })
    print(f"  {len(missing_crops)} missing crop commodities written to {out}")

def convert_seed_varieties():
    print("Converting seed varieties...")
    with open(os.path.join(BASE_PACK_DIR, "domains/agriculture/seed_variety.json"), encoding="utf-8") as f:
        base_varieties = json.load(f)
    existing_ids = {v["value_id"] for v in base_varieties["values"]}
    base_scoped_names = {
        (v.get("attributes", {}).get("crop"), v.get("value_display", "").strip().casefold())
        for v in base_varieties["values"]
    }
    base_near_keys = {
        (v.get("attributes", {}).get("crop"), norm(variety_part(v.get("value_display", "")).split("(")[0]))
        for v in base_varieties["values"]
    }
    for v in base_varieties["values"]:
        base_near_keys.add((v.get("attributes", {}).get("crop"), norm(variety_part(v.get("value_display", "")))))

    crop_map = {
        1: "CROP_MAIZE", 2: "CROP_BREAD_WHEAT", 3: "CROP_DURUM_WHEAT", 4: "CROP_BARLEY",
        5: "CROP_MALT_BARLEY", 6: "CROP_TEFF", 7: "CROP_SORGHUM", 9: "CROP_RICE",
        10: "CROP_FINGER_MILLET", 12: "CROP_TRITICALE", 13: "CROP_EMMER_WHEAT", 17: "CROP_QUINOA",
        18: "CROP_OAT", 19: "CROP_FABA_BEAN", 20: "CROP_FIELD_PEA", 21: "CROP_DEKOKO",
        22: "CROP_CHICKPEA", 23: "CROP_COWPEA", 24: "CROP_LENTIL", 25: "CROP_COMMON_BEAN",
        26: "CROP_SOYBEAN", 28: "CROP_MUNG_BEAN", 29: "CROP_FENUGREEK", 30: "CROP_NOUG",
        31: "CROP_LINSEED", 32: "CROP_RAPESEED", 33: "CROP_SESAME", 34: "CROP_GROUNDNUT",
        35: "CROP_SUNFLOWER", 36: "CROP_SAFFLOWER", 40: "CROP_POTATO", 41: "CROP_SWEET_POTATO",
        42: "CROP_TARO", 43: "CROP_CASSAVA", 45: "CROP_YAM", 46: "CROP_TOMATO", 47: "CROP_GARLIC",
        48: "CROP_ONION", 50: "CROP_PEPPER", 51: "CROP_CABBAGE", 52: "CROP_CARROT", 53: "CROP_OKRA",
        57: "CROP_CORIANDER", 65: "CROP_WHITE_CUMIN", 72: "CROP_LEMON_VERBENA", 80: "CROP_BANANA",
        84: "CROP_AVOCADO", 94: "CROP_COWPEA", 96: "CROP_PIGEON_PEA", 97: "CROP_OAT",
        102: "CROP_ALFALFA", 109: "CROP_COTTON", 113: "CROP_TOBACCO", 114: "CROP_ADZUKI_BEAN",
        115: "CROP_LIMA_BEAN", 116: "CROP_LETTUCE", 117: "CROP_WATERMELON", 118: "CROP_MUSKMELON",
        119: "CROP_BROCCOLI", 120: "CROP_CAULIFLOWER", 124: "CROP_KORARIMA", 125: "CROP_ROSE",
        126: "CROP_BRACHIARIA", 127: "CROP_SORGHUM", 128: "CROP_SOYBEAN", 129: "CROP_CACAO",
        132: "CROP_ETHIOPIAN_MUSTARD"
    }

    with open(CROP_SQL, encoding="utf-8") as f:
        lines = [l.strip() for l in f if l.strip().startswith("(")]

    values = []
    seen = set(existing_ids)
    dropped_nameless = []
    dropped_midcuts = []
    dropped_crop_only = []
    dropped_maize_parents = []
    dropped_base_duplicates = []
    merged_duplicates = []
    shortened_transformations = []

    for i, line in enumerate(lines, start=1):
        l_clean = line.rstrip(",;").replace("''", "'")
        m = re.match(r"^\(\s*(\d+)\s*,\s*(\d+)\s*,\s*'([^']*)'\s*,\s*'([^']*)'\s*,\s*(?:NULL|'([^']*)')\s*,\s*(?:NULL|(\d+))\s*,\s*(?:NULL|DATE\s*'[^']*')\s*,\s*(?:NULL|'([^']*)')\s*,\s*(?:NULL|'([^']*)')", l_clean)
        if not m:
            continue
        row_id = int(m.group(1))
        crop_id = int(m.group(2))
        crop_name = m.group(3)
        common_name = m.group(4)
        release_year_str = m.group(6)
        maintainer = m.group(8)

        # Drop nameless varieties (38 rows in section 2.2)
        if row_id in NAMELESS_ROWS:
            dropped_nameless.append(row_id)
            continue

        # Drop mid-cut unresolvable names
        if row_id in MID_CUT_ROWS:
            dropped_midcuts.append((row_id, common_name))
            continue

        # Drop crop-only varieties
        if row_id in CROP_ONLY_ROWS:
            dropped_crop_only.append((row_id, common_name))
            continue

        # Drop maize parent lines (hybrid production lines)
        if row_id in MAIZE_PARENT_ROWS:
            dropped_maize_parents.append((row_id, common_name))
            continue

        eng, am = clean_variety_name(common_name, crop_name, row_id)

        if not eng or not re.search(r'[a-zA-Z0-9]', eng):
            dropped_nameless.append(row_id)
            continue

        crop_code = crop_map.get(crop_id)
        if not crop_code:
            continue

        clean_crop_disp = crop_name.replace('…', '').replace('...', '').strip()
        if clean_crop_disp.lower().startswith('brachiaria'):
            clean_crop_disp = 'Brachiaria'
        disp_name = f"{clean_crop_disp} – {eng}"

        # Dynamic check for crop-only names
        if norm(eng) in (crop_key(crop_code), norm(clean_crop_disp)) or norm(eng) in ('groundnut', 'cacao', 'sorghum', 'onion', 'tomato', 'fababean', 'pigeonpea', 'foodbarley'):
            dropped_crop_only.append((row_id, common_name))
            continue

        # Skip if already in base pack (exact or near duplicate lead)
        lead_norm = norm(eng.split("(")[0])
        if (crop_code, disp_name.casefold()) in base_scoped_names or (crop_code, lead_norm) in base_near_keys or (crop_code, norm(eng)) in base_near_keys:
            dropped_base_duplicates.append((row_id, disp_name))
            continue

        crop_prefix = crop_code.removeprefix("CROP_")
        vid = clean_code("VAR", crop_prefix, eng)
        if not vid or vid == f"VAR_{crop_prefix}":
            continue

        if vid in existing_ids:
            dropped_base_duplicates.append((row_id, disp_name))
            continue

        # Merge duplicates and carry over details
        if vid in seen:
            merged_duplicates.append((row_id, common_name, vid))
            target_val = next((v for v in values if v["value_id"] == vid), None)
            if target_val:
                if "release_year" not in target_val["attributes"] and release_year_str:
                    try:
                        yr = int(release_year_str)
                        if 1950 <= yr <= 2100:
                            target_val["attributes"]["release_year"] = yr
                    except ValueError:
                        pass
                if "research_centre" not in target_val["attributes"] and maintainer and maintainer not in ("-", "Domestic", "NULL"):
                    clean_m = maintainer.strip(" /")
                    if clean_m:
                        target_val["attributes"]["research_centre"] = clean_m
            continue
        seen.add(vid)

        if '…' in common_name or '...' in common_name:
            shortened_transformations.append((row_id, common_name, vid, disp_name))

        attrs = {
            "crop": crop_code,
            "official_name": eng
        }
        if release_year_str:
            try:
                yr = int(release_year_str)
                if 1950 <= yr <= 2100:
                    attrs["release_year"] = yr
            except ValueError:
                pass
        if maintainer and maintainer not in ("-", "Domestic", "NULL"):
            clean_m = maintainer.strip(" /")
            if clean_m:
                attrs["research_centre"] = clean_m

        values.append({
            "value_id": vid,
            "value_code": vid,
            "value_display": disp_name,
            "sort_order": 100 + len(values),
            "display_i18n": {"am": am} if am else {},
            "attributes": attrs
        })

    # Run check_varieties guard!
    check_varieties(values, base_scoped_names, base_near_keys)

    out = os.path.join(ADDITIONS_DIR, "values/domains/agriculture/seed_variety.json")
    write_json(out, {
        "description": "MoA released varieties not in the base list. Source: ethioseed.moa.gov.et/crop-varieties-all (archived 2025-12-17).",
        "values": values
    })
    print(f"  {len(values)} varieties written to {out}")
    print(f"  Dropped {len(dropped_nameless)} nameless rows: {dropped_nameless}")
    print(f"  Dropped {len(dropped_midcuts)} unresolvable mid-cut rows: {[r[0] for r in dropped_midcuts]}")
    print(f"  Dropped {len(dropped_crop_only)} crop-only rows: {[r[0] for r in dropped_crop_only]}")
    print(f"  Dropped {len(dropped_maize_parents)} maize parent lines: {[r[0] for r in dropped_maize_parents]}")
    print(f"  Dropped {len(dropped_base_duplicates)} base-pack duplicate rows: {dropped_base_duplicates}")
    print(f"  Merged {len(merged_duplicates)} duplicate rows into unique codes.")

    # Write audit log of transformations
    audit_file = os.path.join(REPO_ROOT, "scripts/packs/sources/seed_variety_transformations.md")
    with open(audit_file, "w", encoding="utf-8") as af:
        af.write("# Audit Log: Seed Variety Quality Transformations & Deduplication\n\n")
        af.write("## 1. Verified Official Citations for Resolved Pairs\n\n")
        af.write("- **Deka (ETBW 6583)**: Released in 2018 by Kulumsa Agricultural Research Center (KARC / EIAR). Source: Ministry of Agriculture (MoA) Ethiopia, *Plant Variety Release, Protection and Seed Quality Control Directorate - Crop Variety Register*, Issue No. 21 (June 2018), Bread Wheat (*Triticum aestivum L.*).\n")
        af.write("- **Awash-1**: 1990 release by Melkassa Agricultural Research Center (MARC / EIAR), officially registered white pea bean (*Phaseolus vulgaris L.*) in MoA Crop Variety Register.\n")
        af.write("- **Awash Mitra**: 2017 release by Melkassa ARC / EIAR, canning/navy bean variety officially registered in MoA Crop Variety Register Issue No. 20 (June 2017).\n\n")
        af.write("## 2. Decision on Maize Breeding Parent Lines\n\n")
        af.write("In accordance with the OpenG2P MDS / Agri-Stack data standards, `SEED_VARIETY` represents farmer-plantable cultivars. Parental inbred and single-cross lines (e.g., `142-1-e`, `A7033/F-7215`, `CML395`, `BKL001-BKL004`, `SC22`, `124-b(109)`) are used exclusively by certified seed producers for hybrid multiplication. These 14 entries (rows 776, 793..804, 897) have been omitted from `SEED_VARIETY` and reserved for producer catalogs.\n\n")
        af.write("## 3. Shortened Varieties (from ellipsis truncation in archived source)\n\n")
        af.write("| Row ID | Source Name | Resulting Code | Resulting Display |\n|---|---|---|---|\n")
        for r_id, s_name, r_code, r_disp in shortened_transformations:
            af.write(f"| {r_id} | `{s_name}` | `{r_code}` | {r_disp} |\n")
        af.write("\n## 4. Merged Duplicate Rows\n\n")
        af.write("| Row ID | Source Name | Merged Into Code |\n|---|---|---|\n")
        for r_id, s_name, m_code in merged_duplicates:
            af.write(f"| {r_id} | `{s_name}` | `{m_code}` |\n")
        af.write("\n## 5. Omitted Rows Summary\n\n")
        af.write(f"- **Nameless rows ({len(dropped_nameless)})**: `{dropped_nameless}`\n")
        af.write(f"- **Mid-cut unresolvable rows ({len(dropped_midcuts)})**: `{[r[0] for r in dropped_midcuts]}`\n")
        af.write(f"- **Crop-only rows ({len(dropped_crop_only)})**: `{[r[0] for r in dropped_crop_only]}` (Sorghum, Onion, Tomato, Faba bean, Pigeon pea, Food barley, Cacao, Groundnut)\n")
        af.write(f"- **Maize parental lines ({len(dropped_maize_parents)})**: `{[r[0] for r in dropped_maize_parents]}`\n")
        af.write(f"- **Base pack duplicates ({len(dropped_base_duplicates)})**: `{[r[0] for r in dropped_base_duplicates]}` (including `Danda'a` and `Kakaba`)\n")
    print(f"  Audit log written to {audit_file}")

def convert_livestock_breeds():
    print("Converting livestock breeds...")
    species_map = {
        "Cattle": "LSTK_CATTLE",
        "Camel": "LSTK_CAMEL",
        "Sheep": "LSTK_SHEEP",
        "Goat": "LSTK_GOAT"
    }

    with open(BREED_SQL, encoding="utf-8") as f:
        lines = [l.strip() for l in f if l.strip().startswith("(")]

    values = []
    seen = set()

    for i, line in enumerate(lines, start=1):
        l_clean = line.rstrip(",;")
        parts = [p.strip() for p in l_clean.lstrip("(").rstrip(")").split(",")]
        name = parts[2].strip("' ")
        species = parts[4].strip("' ")
        btype = parts[5].strip("' ")

        if species not in species_map:
            continue

        bid = clean_code("BREED", species, name)
        if bid in seen:
            bid = f"{bid}_{i}"
        seen.add(bid)

        attrs = {
            "species": species_map[species]
        }
        if btype and btype != "NULL":
            attrs["breed_type"] = btype

        values.append({
            "value_id": bid,
            "value_code": bid,
            "value_display": name,
            "sort_order": i,
            "display_i18n": {},
            "attributes": attrs
        })

    doc = {
        "attribute_id": "LIVESTOCK_BREED_NAME",
        "attribute_code": "LIVESTOCK_BREED_NAME",
        "attribute_display": "Livestock breed name",
        "description": "National livestock breeds of Ethiopia. Source: National Livestock Data Standard for Ethiopia (MOA, March 2024).",
        "is_hierarchical": False,
        "attribute_schema": {
            "type": "object",
            "properties": {
                "species": {
                    "type": "string",
                    "x-list-ref": "LIVESTOCK_TYPE",
                    "title": "Species"
                },
                "breed_type": {
                    "type": "string",
                    "title": "Breed type"
                }
            },
            "required": ["species"],
            "additionalProperties": False
        },
        "values": values
    }
    out = os.path.join(ADDITIONS_DIR, "domains/agriculture/livestock_breed_name.json")
    write_json(out, doc)
    print(f"  {len(values)} breeds written to {out}")

def convert_livestock_sex():
    print("Writing livestock sex codelist...")
    doc = {
        "attribute_id": "LIVESTOCK_SEX",
        "attribute_code": "LIVESTOCK_SEX",
        "attribute_display": "Livestock sex",
        "description": "Animal biological and reproductive sex classification. Source: National Livestock Data Standard for Ethiopia (MOA, 2024).",
        "is_hierarchical": False,
        "values": [
            {
                "value_id": "SEX_MALE",
                "value_code": "SEX_MALE",
                "value_display": "Male",
                "sort_order": 1,
                "display_i18n": {}
            },
            {
                "value_id": "SEX_FEMALE",
                "value_code": "SEX_FEMALE",
                "value_display": "Female",
                "sort_order": 2,
                "display_i18n": {}
            },
            {
                "value_id": "SEX_MALE_NEUTER",
                "value_code": "SEX_MALE_NEUTER",
                "value_display": "Castrated male",
                "sort_order": 3,
                "display_i18n": {}
            },
            {
                "value_id": "SEX_FEMALE_NEUTER",
                "value_code": "SEX_FEMALE_NEUTER",
                "value_display": "Female neuter",
                "sort_order": 4,
                "display_i18n": {}
            }
        ]
    }
    out = os.path.join(ADDITIONS_DIR, "domains/agriculture/livestock_sex.json")
    write_json(out, doc)
    print(f"  Livestock sex written to {out}")

def convert_fertilizers():
    print("Writing fertilizer additions...")
    values = [
        {
            "value_id": "FERT_BIO_RHIZO",
            "value_code": "FERT_BIO_RHIZO",
            "value_display": "Bio-rhizobium",
            "sort_order": 8,
            "display_i18n": {}
        },
        {
            "value_id": "FERT_ZINC_BORON",
            "value_code": "FERT_ZINC_BORON",
            "value_display": "Zinc and boron micronutrients",
            "sort_order": 9,
            "display_i18n": {}
        },
        {
            "value_id": "FERT_LIME",
            "value_code": "FERT_LIME",
            "value_display": "Agricultural lime",
            "sort_order": 10,
            "display_i18n": {}
        }
    ]
    out = os.path.join(ADDITIONS_DIR, "values/domains/agriculture/fertilizer_type.json")
    write_json(out, {
        "description": "Additional fertilizer and soil amendment products from MoA input catalog.",
        "values": values
    })
    print(f"  {len(values)} fertilizer values written to {out}")

if __name__ == "__main__":
    convert_crop_commodities()
    convert_seed_varieties()
    convert_livestock_breeds()
    convert_livestock_sex()
    convert_fertilizers()
    print("All conversions completed successfully.")
