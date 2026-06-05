"""
scanner.py
SE .sc file reading, regex parsing, directory pre-scanning, and
object-category filtering.  No imports from builder, converter, or main.
"""

import os
import re
import random
import math

from constants import (
    log_debug, safe_float,
    VALID_SE_TYPES, EARTH_MASS_KG, EARTH_RADIUS_M, SOLAR_RADIUS_M,
    AU_TO_METERS, GRAVITATIONAL_CONSTANT,
    SOLAR_MASS_KG,
)


# ─────────────────────────────────────────────────────────────────────────────
# SE .sc PARSER
# ─────────────────────────────────────────────────────────────────────────────

def parse_se_file(filepath: str) -> list:
    """Parse a Space Engine .sc file into a list of declaration dicts."""
    with open(filepath, "r", encoding="utf-8") as f:
        lines = f.readlines()
    root = []
    stack = [root]
    current_name = None
    for line in lines:
        line = line.split("//")[0].strip()
        if not line:
            continue
        if line == "{":
            new_node = {}
            if isinstance(stack[-1], list):
                if current_name:
                    stack[-1].append({current_name: new_node})
            elif isinstance(stack[-1], dict):
                if current_name:
                    stack[-1][current_name] = new_node
            stack.append(new_node)
            current_name = None
        elif line == "}":
            if len(stack) > 1:
                stack.pop()
            current_name = None
        else:
            parts = re.findall(r'(?:[^\s,"]|"(?:\\.|[^"])*")+', line)
            if not parts:
                continue
            if isinstance(stack[-1], list):
                if len(parts) >= 2 and parts[0] in VALID_SE_TYPES:
                    current_name = parts[0] + " " + " ".join(parts[1:])
                else:
                    current_name = None
            else:
                if len(parts) >= 2:
                    stack[-1][parts[0]] = " ".join(parts[1:]).strip('"')
                else:
                    current_name = parts[0]
    return root


# ─────────────────────────────────────────────────────────────────────────────
# PHYSICAL PROPERTY EXTRACTION
# ─────────────────────────────────────────────────────────────────────────────

def extract_physical_properties(obj_decl: str, obj_data: dict) -> tuple:
    """Return (mass_kg, radius_m, parent_name) from a parsed SE object block."""
    parent = None
    if "ParentBody" in obj_data:
        parent = obj_data["ParentBody"].strip('"')
    if   "Radius" in obj_data:  radius_m = safe_float(obj_data["Radius"]) * 1_000.0
    elif "RadSol"  in obj_data:  radius_m = safe_float(obj_data["RadSol"]) * SOLAR_RADIUS_M
    else:                        radius_m = EARTH_RADIUS_M
    if   "Mass"    in obj_data:  mass_kg = safe_float(obj_data["Mass"])    * EARTH_MASS_KG
    elif "MassSol" in obj_data:  mass_kg = safe_float(obj_data["MassSol"]) * SOLAR_MASS_KG
    else:                        mass_kg = (4/3) * math.pi * max(radius_m, 1.0)**3 * 3_000.0
    if mass_kg <= 0 or math.isnan(mass_kg):
        mass_kg = 1.0
    return mass_kg, radius_m, parent


def extract_se_extras(obj_data: dict) -> tuple:
    """
    Extract atmosphere, ocean, life, magnetic, and surface properties.
    Returns a 12-tuple used by the converter.
    """
    atm_info = {}
    atm_block = obj_data.get("Atmosphere")
    if isinstance(atm_block, dict):
        comp = atm_block.get("Composition", {})
        if not isinstance(comp, dict):
            comp = {}
        atm_info = {
            "pressure":   safe_float(atm_block.get("Pressure",   0)),
            "density":    safe_float(atm_block.get("Density",    0)),
            "height":     safe_float(atm_block.get("Height",     0)),
            "comp":       comp,
            "hue":        atm_block.get("Hue"),
            "saturation": atm_block.get("Saturation"),
            "model":      atm_block.get("Model", ""),
            "opacity":    safe_float(atm_block.get("Opacity", 1.0)),
        }

    no_ocean_flag = str(obj_data.get("NoOcean", "false")).strip().lower() in ("true", "1")
    has_ocean     = isinstance(obj_data.get("Ocean"), dict)
    sea_level     = 0.0
    preset_name   = ""
    surf = obj_data.get("Surface", {})
    if isinstance(surf, dict):
        sea_level   = safe_float(surf.get("seaLevel", 0))
        preset_name = surf.get("Preset", "")
        if isinstance(preset_name, str):
            preset_name = preset_name.strip('"')
        if not has_ocean and not no_ocean_flag and sea_level > 0.05:
            has_ocean = True

    use_water = has_ocean or sea_level > 0.05
    mag_field, mag_pole = _get_magnetic_params(obj_data.get("Aurora"))

    life_block       = obj_data.get("Life")
    has_life         = isinstance(life_block, dict)
    has_exotic_life  = False
    has_organic_life = False
    has_aerial_life  = False
    diffmap = obj_data.get("Surface", {}).get("Preset", "") if isinstance(obj_data.get("Surface"), dict) else ""

    if has_life:
        class_val        = str(life_block.get("Class", "")).strip().lower()
        biome_val        = str(life_block.get("Biome",  "")).strip().lower()
        has_exotic_life  = class_val == "exotic"
        has_aerial_life  = "aerial" in biome_val
        has_organic_life = not has_exotic_life

    return (atm_info, has_ocean, use_water, mag_field, mag_pole, sea_level,
            preset_name, has_life, has_exotic_life, has_aerial_life, has_organic_life, diffmap)


def _get_magnetic_params(aurora_block) -> tuple:
    from constants import _builtin_db
    if not isinstance(aurora_block, dict):
        return 0.0, 0.0
    nb = safe_float(aurora_block.get("NorthBright", 0))
    nr = safe_float(aurora_block.get("NorthRadius", 0))
    db = _builtin_db()
    for row in db.get("aurora_to_magnetic", []):
        if nb <= row["north_bright_max"] and nr <= row["north_radius_max"]:
            return row["field"], row["pole_angle"]
    last = db["aurora_to_magnetic"][-1]
    return last["field"], last["pole_angle"]


# ─────────────────────────────────────────────────────────────────────────────
# DIRECTORY PRE-SCAN (for GUI live totals)
# ─────────────────────────────────────────────────────────────────────────────

def prescan_sc_directory(directory: str) -> tuple:
    """
    Quickly count ring_particle, comet, and standalone_asteroid objects in a
    directory of .sc files without performing a full conversion.

    Returns: (total_standalone, total_rings, total_comets)
    """
    if not directory or not os.path.isdir(directory):
        return 0, 0, 0

    sc_files = [os.path.join(directory, f)
                for f in os.listdir(directory) if f.lower().endswith(".sc")]

    _decl_re  = re.compile(r'^\s*(Asteroid|Moon|DwarfMoon|Comet)\s+"', re.IGNORECASE)
    _class_re = re.compile(r'^\s*Class\s+"([^"]*)"', re.IGNORECASE)

    total_standalone = total_rings = total_comets = 0

    for sc_path in sc_files:
        try:
            with open(sc_path, "r", encoding="utf-8", errors="replace") as fh:
                current_decl = current_cls = None
                depth = 0
                for raw_line in fh:
                    opens  = raw_line.count("{")
                    closes = raw_line.count("}")
                    m_decl = _decl_re.match(raw_line)
                    if m_decl and depth == 0:
                        if current_decl is not None:
                            cl = (current_cls or "").lower()
                            if current_decl == "asteroid":               total_rings += 1
                            elif current_decl == "comet":                total_comets += 1
                            elif current_decl in ("moon","dwarfmoon") and "asteroid" in cl:
                                total_standalone += 1
                        current_decl = m_decl.group(1).lower()
                        current_cls  = None
                        depth        = 0
                    depth += opens - closes
                    if current_decl and depth > 0:
                        m_cls = _class_re.match(raw_line)
                        if m_cls:
                            current_cls = m_cls.group(1)
                    if current_decl and depth <= 0:
                        cl = (current_cls or "").lower()
                        if current_decl == "asteroid":               total_rings += 1
                        elif current_decl == "comet":                total_comets += 1
                        elif current_decl in ("moon","dwarfmoon") and "asteroid" in cl:
                            total_standalone += 1
                        current_decl = current_cls = None
                        depth = 0
                if current_decl:
                    cl = (current_cls or "").lower()
                    if current_decl == "asteroid":               total_rings += 1
                    elif current_decl == "comet":                total_comets += 1
                    elif current_decl in ("moon","dwarfmoon") and "asteroid" in cl:
                        total_standalone += 1
        except OSError:
            continue

    return total_standalone, total_rings, total_comets


# ─────────────────────────────────────────────────────────────────────────────
# FILTERING HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def apply_limit_filter(object_list: list, user_input_string: str, label: str = "objects") -> list:
    """
    Apply a keep-limit to object_list and return the survivors.

    user_input_string rules:
      - Contains '%'   → treat leading float as percentage of total
      - Plain integer  → absolute count
      - '0' or ''      → keep none
      - Invalid        → keep none
    """
    total = len(object_list)
    if total == 0:
        return []
    raw = (user_input_string or "").strip()
    limit = 0
    if raw in ("", "0"):
        limit = 0
    elif "%" in raw:
        try:
            pct_val = float(raw.replace("%", "").strip())
            limit   = max(1, int(total * (pct_val / 100.0)))
        except (ValueError, TypeError):
            limit = 0
    else:
        try:
            limit = int(raw)
        except (ValueError, TypeError):
            limit = 0

    log_debug(f"apply_limit_filter [{label}]: total={total}, input='{raw}', limit={limit}", "FILTER")

    if limit <= 0:
        log_debug(f"apply_limit_filter [{label}]: keeping 0 (all discarded)", "FILTER")
        return []
    if limit >= total:
        log_debug(f"apply_limit_filter [{label}]: keeping all {total}", "FILTER")
        return list(object_list)
    survivors = random.sample(list(object_list), limit)
    log_debug(f"apply_limit_filter [{label}]: kept {len(survivors)}, discarded {total - len(survivors)}", "FILTER")
    return survivors


def _classify_parsed_object(obj: dict):
    """
    Classify a parsed object into 'ring_particle', 'comet',
    'standalone_asteroid', or None (everything else).
    """
    decl_type = obj.get("decl_type", "").strip().lower()
    raw_data  = obj.get("raw_data") or {}
    raw_class = raw_data.get("Class", "").strip().lower()
    if decl_type == "comet":
        return "comet"
    if raw_class == "comet" and decl_type in ("moon", "dwarfmoon"):
        return "comet"
    if decl_type == "asteroid":
        return "ring_particle"
    if decl_type in ("moon", "dwarfmoon") and raw_class == "asteroid":
        return "standalone_asteroid"
    return None