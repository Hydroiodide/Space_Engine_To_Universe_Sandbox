"""
builder.py
Universe Sandbox JSON entity assembly.  Generates Body dicts, ring particles,
atmosphere components, depot compositions, and the .ubox archive.
No imports from converter or main.
"""

import math
import colorsys
import copy
import re
import random
import hashlib

from constants import (
    log_debug, safe_float, _now_utc,
    GRAVITATIONAL_CONSTANT, EARTH_MASS_KG, EARTH_RADIUS_M,
    EARTH_ATMOSPHERE_MASS_KG,
    STD_ATM_PA,
    AU_TO_METERS, AU_TO_KM, TOKEN_MASS, GYR_TO_SECONDS,
    SE_TO_US_DEPOT, SE_ATMOSPHERE_TO_US_DEPOT, SE_OCEAN_TO_US_DEPOT,
    US_ATM_DEPOT_KEYS, TRACE_PERCENT, TRACE_PLACEHOLDER_MASS,
    se_bool,
    USE_BUILTIN_HEIGHTMAP_OVERLAYS, DISABLE_BUILTIN_HEIGHTMAPS_FOR_GENERATED_SURFACES,
    ALLOW_CITY_LIGHT_SOURCE_0, USE_PROCEDURAL_CITY_LIGHT_SOURCE,
    PROCEDURAL_CITY_LIGHT_SOURCE, GENERATE_CITY_LIGHTS_FOR_LIFE_WORLDS,
    GENERATE_CITY_LIGHTS_FOR_HABITABLE_WORLDS,
    GENERATE_CITY_LIGHTS_FOR_MULTICELLULAR_TERRESTRIAL,
    GENERATE_CITY_LIGHTS_ONLY_IF_CIVILIZED, CONFIG_ENABLE_MICROBIAL_SURFACE_MATS,
    ALLOW_PRESET_PLANTS_WITHOUT_LIFE, ALLOW_EARTH_HEIGHTMAP_FOR_NON_EARTH,
    ALLOW_EARTH_TEXTURES_ONLY_FOR_EXPLICIT_EARTH,
    LACUSTRINE_DEPOT_MASS_SCALE,
    MAX_SOLID_PLANET_SURFACE_VOLATILE_FRACTION,
    MAX_LACUSTRINE_VOLATILE_FRACTION,
    WATER_COLORS, VEGETATION_COLORS, ICE_SNOW_COLORS,
    ATMOSPHERE_MODEL_COLORS,
    SE_HUE_KEYS, SE_ATMOSPHERE_MODEL_HUE_RGB,
    SE_TITAN_LAND_HUE_RGB, SE_TITAN_SKY_HUE_RGB,
    SE_ATMOSPHERE_VISUAL_OPACITY,
    US_CLOUD_STYLES,
    parse_se_surface_preset, parse_life_block, choose_life_debug_color,
    _ATM, _BLACK, _WHITE, _WATER, _TRANS,
    _PLANET_PALETTES, _ARCHETYPE_DEFAULT_PALETTE, _ARCHETYPE_DEFAULT_TEMPS,
    _HEIGHTMAP_SETS, _NAMED_BODY_TEXTURES,
    _SE_PRESET_CLASS_COLORS,
    _pick_gas_palette, _generate_gas_giant_palette_from_preset,
    detect_brown_dwarf_type, determine_sudarsky_class,
    calculate_average_hsv_from_palette,
    get_haze_type_from_se_model,
    SYSTEM_AGE_SECONDS,
)
import constants as _const


def _get_system_age():
    return _const.SYSTEM_AGE_SECONDS


# ─────────────────────────────────────────────────────────────────────────────
# SMALL HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def eci_to_us(v: list) -> list:
    return [v[0], -v[2], v[1]]


def us_to_eci(v: list) -> list:
    return [v[0], v[2], -v[1]]


def parse_vec3(s) -> list:
    if not s:
        return [0.0, 0.0, 0.0]
    p = str(s).split(";")
    return [safe_float(x) for x in p] if len(p) == 3 else [0.0, 0.0, 0.0]


def parse_ring_color(s) -> tuple:
    nums = re.findall(r'[\d.]+', str(s))
    return (float(nums[0]), float(nums[1]), float(nums[2])) if len(nums) >= 3 else (0.7, 0.7, 0.7)


def parse_rgba(s) -> tuple:
    nums = re.findall(r'[\d.]+', str(s))
    if len(nums) >= 4:
        return float(nums[0]), float(nums[1]), float(nums[2]), float(nums[3])
    if len(nums) >= 3:
        return float(nums[0]), float(nums[1]), float(nums[2]), 1.0
    return 0.5, 0.5, 0.5, 1.0


def _palette_from_preset(preset: str):
    return _PLANET_PALETTES.get(preset, (None, False))


def _rgba_from_se_palette_value(value) -> str | None:
    if isinstance(value, (list, tuple)) and len(value) >= 3:
        nums = [safe_float(x) for x in value[:4]]
    else:
        text = str(value or "")
        if "rgb" not in text.lower() and not re.search(r'[-+]?\d+(?:\.\d+)?[,;]\s*[-+]?\d+', text):
            return None
        nums = [safe_float(x) for x in re.findall(r'[-+]?\d+(?:\.\d+)?', text)[:4]]
    if len(nums) < 3:
        return None
    r, g, b = nums[:3]
    a = nums[3] if len(nums) >= 4 else 1.0
    if max(abs(r), abs(g), abs(b)) > 1.0:
        r, g, b = r / 255.0, g / 255.0, b / 255.0
    if a > 1.0:
        a = a / 255.0
    return _rgba_string((r, g, b, a))


def _extract_space_engine_palette(raw: dict) -> list[str]:
    """Find explicit SE surface palette colors without using vegetation/debug colors."""
    raw = raw if isinstance(raw, dict) else {}
    surface = raw.get("Surface", {}) if isinstance(raw.get("Surface"), dict) else {}
    palette = []
    seen = set()

    def maybe_add(value):
        color = _rgba_from_se_palette_value(value)
        if color and color not in seen:
            seen.add(color)
            palette.append(color)

    def scan(obj):
        if isinstance(obj, dict):
            for key, value in obj.items():
                key_l = str(key).lower()
                if isinstance(value, (dict, list, tuple)):
                    if any(token in key_l for token in ("palette", "color", "colour")):
                        scan(value)
                    continue
                if any(token in key_l for token in ("palette", "color", "colour")):
                    maybe_add(value)
        elif isinstance(obj, (list, tuple)):
            if len(obj) >= 3 and all(not isinstance(v, (dict, list, tuple)) for v in obj[:3]):
                maybe_add(obj)
            else:
                for item in obj:
                    scan(item)
        else:
            maybe_add(obj)

    scan(surface)
    for key in ("Palette", "SurfacePalette", "Colors", "Colours"):
        if key in raw:
            scan(raw[key])
    return palette[:8] if len(palette) >= 3 else []


def _pick_heightmaps(archetype: str, obj_id: int) -> tuple:
    sets = _HEIGHTMAP_SETS.get(archetype)
    if not sets:
        return "", "", "", "", 1.0, 0.0, 0.0, 0.0, False, False, False, False
    hm0, nm0, hm1, nm1 = sets[obj_id % len(sets)]
    rng  = random.Random(obj_id * 7_919)
    mix0 = rng.uniform(-0.8, 0.8)
    mix1 = rng.uniform(-0.8, 0.8)
    off0 = float((obj_id * 73) % 400)
    off1 = float((obj_id * 137 + 100) % 400)
    fh0  = bool(obj_id % 2);          fv0 = bool((obj_id // 2) % 2)
    fh1  = bool((obj_id // 4) % 2);   fv1 = bool((obj_id // 8) % 2)
    return hm0, nm0, hm1, nm1, mix0, mix1, off0, off1, fh0, fv0, fh1, fv1


BUILTIN_ELEVATION_MAPS = {
    "planet1": {"family": "cratered_old"},
    "planet2": {"family": "tectonic_chaotic"},
    "planet3": {"family": "tectonic_chaotic"},
    "planet4": {"family": "tectonic_chaotic"},
    "planet5": {"family": "tectonic_chaotic"},
    "planet6": {"family": "rifted"},
    "planet7": {"family": "rifted"},
    "planet8": {"family": "tectonic_chaotic"},
    "planet9": {"family": "cratered_old"},
    "planet10": {"family": "cratered_old"},
    "planet11": {"family": "cratered_old"},
    "planet12": {"family": "tectonic_chaotic"},
    "planet13": {"family": "icy_fractured"},
    "planet14": {"family": "rifted"},
    "planet15": {"family": "tectonic_chaotic"},
    "planet16": {"family": "cratered_mars_compatible"},
    "callisto": {"family": "icy_cratered"},
    "ceres": {"family": "dwarf_cratered"},
    "deimos": {"family": "small_moon_cratered"},
    "dione": {"family": "icy_fractured"},
    "earth": {"family": "terra_continental"},
    "enceladus": {"family": "icy_fractured"},
    "europa": {"family": "icy_fractured"},
    "ganymede": {"family": "icy_cratered"},
    "iapetus": {"family": "icy_ridged_cratered"},
    "io": {"family": "volcanic"},
    "mars": {"family": "volcanic_mars"},
    "mercury": {"family": "cratered_old"},
    "mimas": {"family": "icy_cratered"},
    "moon": {"family": "cratered_old"},
    "phobos": {"family": "small_moon_cratered"},
    "pluto": {"family": "icy_dwarf"},
    "rhea": {"family": "icy_cratered"},
    "sedna": {"family": "icy_dwarf"},
    "tethys": {"family": "icy_cratered"},
    "titan": {"family": "icy_eroded"},
    "venus": {"family": "tectonic_chaotic"},
    "vesta": {"family": "dwarf_cratered"},
}
_BUILTIN_HEIGHTMAP_KEYS = tuple(BUILTIN_ELEVATION_MAPS)

_SHARED_HEIGHTMAP_CACHE = {}
_HEIGHTMAP_SELECTION_HISTORY = []


def reset_heightmap_selection_history() -> None:
    _HEIGHTMAP_SELECTION_HISTORY.clear()


def _map_key(name: str) -> str:
    key = str(name or "").strip().replace("\\", "/").split("/")[-1]
    if key.endswith("_height_normals"):
        key = key[:-15]
    if key.endswith("_height"):
        key = key[:-7]
    return key


def _height_path(map_key: str) -> str:
    key = _map_key(map_key)
    return f"Textures/Planets/{key}_height" if key else ""


def _normal_path(map_key: str) -> str:
    key = _map_key(map_key)
    return f"Textures/Planets/{key}_height_normals" if key else ""


def _stable_u32(text: str) -> int:
    return int(hashlib.md5(str(text).encode("utf-8")).hexdigest(), 16) & 0xFFFFFFFF


def _surface_raw_family(obj_data: dict, archetype: str, surface_preset: str = "") -> str:
    raw = obj_data if isinstance(obj_data, dict) else {}
    surf = raw.get("Surface", {}) if isinstance(raw.get("Surface"), dict) else {}
    preset_info = parse_se_surface_preset(surface_preset or surf.get("Preset", ""))
    if preset_info.get("terrain_family"):
        tf = preset_info["terrain_family"]
        if tf == "volcanic":
            return "tectonic_chaotic" if archetype in ("lava", "rocky") else "hybrid"
        return tf
    hint = " ".join(str(surf.get(k, "")) for k in ("DiffMap", "BumpMap", "Preset"))
    hint = f"{hint} {surface_preset or ''} {raw.get('Class', '')}".lower()
    crater_score = safe_float(surf.get("craterDensity", 0.0)) + safe_float(surf.get("craterMagn", 0.0)) / 10.0
    rift_score = safe_float(surf.get("riftsMagn", 0.0)) / 100.0 + safe_float(surf.get("canyonsMagn", 0.0)) / 10.0
    chaos_score = safe_float(surf.get("venusMagn", 0.0)) / 5.0 + safe_float(surf.get("cracksMagn", 0.0)) / 10.0
    volcanic_score = (
        safe_float(surf.get("volcanoDensity", 0.0)) +
        safe_float(surf.get("volcanoMagn", 0.0)) +
        safe_float(surf.get("volcanoActivity", 0.0)) / 2.0 +
        safe_float(surf.get("mareDensity", 0.0))
    )
    if "mars" in hint or volcanic_score > 0.35:
        return "volcanic_mars"
    if "moon" in hint or crater_score > 0.55:
        return "cratered_old"
    if "pluto" in hint or "ice" in hint or archetype == "ice":
        return "icy_fractured" if chaos_score > 0.2 or rift_score > 0.2 else "cratered_sparse"
    if "venus" in hint or chaos_score > 0.35:
        return "tectonic_chaotic"
    if rift_score > 0.30:
        return "rifted"
    if archetype == "ocean" or "terra" in hint or "earth" in hint:
        return "hybrid"
    return "hybrid"


def _shared_surface_settings(raw: dict) -> tuple[bool, str]:
    raw = raw if isinstance(raw, dict) else {}
    opts = raw.get("_surface_options", {}) if isinstance(raw.get("_surface_options"), dict) else {}
    strict = bool(raw.get("strict_same_surface") or opts.get("strict_same_surface"))
    group = raw.get("shared_surface_group", opts.get("shared_surface_group", raw.get("same_surface_seed", opts.get("same_surface_seed", ""))))
    return strict, str(group or "")


def weighted_pick_top_k(candidates, seed, k=5, strict_same_surface=False):
    ranked = sorted(candidates, key=lambda item: (-item["score"], item["primary"], item["secondary"]))
    if not ranked:
        raise ValueError("heightmap selector received no candidates")
    if strict_same_surface:
        return ranked[0], ranked[:1]
    best = max(1e-9, ranked[0]["score"])
    pool = [item for item in ranked if item["score"] >= best * 0.86 or item["score"] >= best - 12.0]
    pool = pool[:max(1, k)]
    idx = _stable_u32(seed) % len(pool)
    return pool[idx], pool


def select_builtin_heightmap_pair(surface, body, terrain_family,
                                  preset_info=None,
                                  strict_same_surface=False,
                                  shared_seed=None):
    """Select optional Universe Sandbox built-in visual height maps.

    These references only drive US visual bump/normal overlays; they do not
    replace the generated physical .surface archive.
    """
    surface = surface if isinstance(surface, dict) else {}
    body = body if isinstance(body, dict) else {}
    raw = body.get("raw_data", {}) if isinstance(body.get("raw_data", {}), dict) else body
    raw_surface = raw.get("Surface", {}) if isinstance(raw.get("Surface"), dict) else {}
    if not surface:
        surface = raw_surface
    preset_info = preset_info or parse_se_surface_preset(
        body.get("surface_preset") or surface.get("Preset") or raw_surface.get("Preset") or ""
    )

    cache_key = str(shared_seed or "") if strict_same_surface and shared_seed else ""
    if cache_key and cache_key in _SHARED_HEIGHTMAP_CACHE:
        cached = dict(_SHARED_HEIGHTMAP_CACHE[cache_key])
        log_debug(
            f"[heightmap] Body='{body.get('name', raw.get('Name', 'unknown'))}' "
            f"family='{terrain_family}' selected='{cached['primary']} + {cached['secondary']}' "
            f"reason='strict same-map cached: {cached['reason']}'",
            "HEIGHTMAP",
        )
        return cached

    name = str(body.get("name") or raw.get("Name") or body.get("obj_name") or "").lower()
    archetype = str(body.get("archetype") or "").lower()
    raw_class = str(body.get("se_class") or raw.get("Class") or "").lower()
    preset = str(body.get("surface_preset") or surface.get("Preset") or "").lower()
    diffmap = str(surface.get("DiffMap") or body.get("diffmap") or "").lower()
    bumpmap = str(surface.get("BumpMap") or "").lower()
    hint = f"{diffmap} {bumpmap} {preset} {raw_class} {name}".lower()

    no_ocean = se_bool(raw.get("NoOcean", body.get("has_no_ocean", False)))
    no_atm = se_bool(raw.get("NoAtmosphere", body.get("has_no_atmosphere", False)))
    has_ocean = bool((isinstance(raw.get("Ocean"), dict) or body.get("has_ocean") or body.get("use_water")) and not no_ocean)
    has_atm = bool((isinstance(raw.get("Atmosphere"), dict) or body.get("atm_info")) and not no_atm)
    has_life = bool(isinstance(raw.get("Life"), dict) or body.get("has_life"))
    radius_m = safe_float(body.get("radius_m", raw.get("Radius", 0.0)), 0.0)
    if radius_m and radius_m < 20_000.0:
        radius_m *= 1000.0
    temp_k = safe_float(
        body.get("est_temp", body.get("source_temp_k", raw.get("Temperature", raw.get("Temp", 0.0)))),
        0.0,
    )

    def rawf(key, default=0.0):
        return safe_float(surface.get(key, raw_surface.get(key, default)), default)

    humidity = rawf("humidity", 0.0)
    crater = max(rawf("craterDensity", 0.0), rawf("craterMagn", 0.0) / 10.0, rawf("craterOctaves", 0.0) / 30.0)
    rift = max(rawf("riftsMagn", 0.0) / 100.0, rawf("riftsFreq", 0.0) / 10.0,
               rawf("canyonsMagn", 0.0) / 10.0, rawf("canyonsFreq", 0.0) / 1000.0)
    chaos = max(rawf("venusMagn", 0.0) / 5.0, rawf("venusFreq", 0.0) / 2.0,
                rawf("cracksMagn", 0.0) / 10.0, rawf("cracksFreq", 0.0) / 15.0)
    volcano = max(rawf("volcanoActivity", 0.0) / 2.0, rawf("volcanoDensity", 0.0),
                  rawf("volcanoMagn", 0.0), rawf("lavaCoverSun", 0.0),
                  rawf("lavaCoverTidal", 0.0), rawf("lavaCoverYoung", 0.0),
                  rawf("mareDensity", 0.0))
    snow = rawf("snowLevel", 2.0)
    icecap = rawf("icecapLatitude", 2.0)
    icecap_height = rawf("icecapHeight", 0.0)
    preset_key = preset_info.get("key", "")
    preset_style = preset_info.get("style")
    preset_state = preset_info.get("state")
    preset_family = preset_info.get("family")
    preset_flags = preset_info.get("appearance_flags", {})
    atmosphere = raw.get("Atmosphere", {}) if isinstance(raw.get("Atmosphere"), dict) else {}
    atmosphere_comp = atmosphere.get("Composition", {}) if isinstance(atmosphere.get("Composition"), dict) else {}
    venus_signal = bool(
        preset_style == "venus"
        or "venus" in preset
        or "venus" in diffmap
        or "venus" in bumpmap
        or rawf("venusMagn", 0.0) >= 1.0
        or (
            safe_float(atmosphere.get("Pressure", 0.0)) >= 10.0
            and safe_float(atmosphere_comp.get("CO2", 0.0)) >= 50.0
        )
        or safe_float(atmosphere_comp.get("SO2", 0.0)) >= 0.03
    )
    explicit_earth = _explicit_earth_like(name, raw, preset_info)
    allow_earth_height = bool(
        ALLOW_EARTH_HEIGHTMAP_FOR_NON_EARTH
        or (explicit_earth and ALLOW_EARTH_TEXTURES_ONLY_FOR_EXPLICIT_EARTH)
    )

    candidates = {}

    def add(primary, secondary, score, reason, mix0=0.78, mix1=0.42):
        primary = _map_key(primary)
        secondary = _map_key(secondary)
        if primary not in _BUILTIN_HEIGHTMAP_KEYS or secondary not in _BUILTIN_HEIGHTMAP_KEYS:
            return
        key = (primary, secondary)
        item = candidates.setdefault(key, {
            "primary": primary,
            "secondary": secondary,
            "score": 0.0,
            "reason": reason,
            "reason_score": -1.0,
            "mix0": mix0,
            "mix1": mix1,
        })
        item["score"] += float(score)
        if score > item["reason_score"]:
            item["reason"] = reason
            item["reason_score"] = float(score)
            item["mix0"] = mix0
            item["mix1"] = mix1

    fallback_pairs = [
        ("planet5", "planet7", 0.66, 0.45),
        ("planet3", "planet12", 0.62, 0.48),
        ("planet10", "planet1", 0.72, 0.36),
        ("planet6", "planet13", 0.68, 0.46),
    ]
    for i, (a, b, m0, m1) in enumerate(fallback_pairs):
        add(a, b, 1.0 - i * 0.05, "neutral varied fallback", m0, m1)

    def add_pool(pairs, score, reason):
        for idx, pair in enumerate(pairs):
            primary, secondary = pair[:2]
            mix0 = pair[2] if len(pair) > 2 else 0.78
            mix1 = pair[3] if len(pair) > 3 else 0.42
            add(primary, secondary, max(0.1, score - idx * 2.2), reason, mix0, mix1)

    wet_terra_pool = [
        ("planet5", "planet7", 0.72, 0.46), ("planet3", "planet12", 0.68, 0.48),
        ("planet7", "planet13", 0.72, 0.45), ("planet7", "planet14", 0.70, 0.42),
        ("planet12", "planet5", 0.68, 0.46), ("planet5", "planet8", 0.72, 0.54),
    ]
    earth_pool = [
        ("earth", "planet5", 0.82, 0.42), ("earth", "planet7", 0.80, 0.46),
        ("earth", "venus", 0.76, 0.38),
    ]
    mars_pool = [
        ("mars", "planet16", 0.90, 0.35), ("mars", "moon", 0.86, 0.32),
        ("mars", "mercury", 0.84, 0.30), ("mars", "planet7", 0.86, 0.40),
        ("mars", "planet10", 0.84, 0.33), ("planet16", "mars", 0.74, 0.48),
        ("planet10", "mars", 0.78, 0.40), ("planet7", "mars", 0.80, 0.42),
    ]
    chaos_pool = [
        ("venus", "planet5", 0.80, 0.52), ("venus", "planet8", 0.80, 0.52),
        ("venus", "planet15", 0.78, 0.50), ("planet5", "planet8", 0.72, 0.56),
        ("planet3", "planet12", 0.70, 0.50), ("planet4", "planet15", 0.70, 0.50),
        ("planet12", "planet5", 0.70, 0.50), ("planet8", "planet3", 0.70, 0.50),
    ]
    rift_pool = [
        ("planet7", "planet5", 0.82, 0.46), ("planet14", "mars", 0.78, 0.36),
        ("planet6", "planet8", 0.74, 0.52), ("planet7", "planet14", 0.80, 0.42),
        ("planet14", "planet5", 0.78, 0.44), ("planet6", "planet13", 0.74, 0.48),
        ("mars", "planet7", 0.84, 0.40), ("planet5", "planet14", 0.74, 0.45),
    ]
    crater_pool = [
        ("moon", "planet1", 0.82, 0.38), ("moon", "planet10", 0.82, 0.35),
        ("mercury", "planet10", 0.82, 0.36), ("planet10", "planet16", 0.80, 0.34),
        ("planet10", "planet1", 0.76, 0.35), ("planet9", "planet11", 0.74, 0.40),
        ("planet16", "planet10", 0.76, 0.34), ("callisto", "mercury", 0.76, 0.34),
    ]
    icy_pool = [
        ("pluto", "planet16", 0.78, 0.34), ("sedna", "planet1", 0.74, 0.34),
        ("europa", "planet13", 0.76, 0.50), ("enceladus", "planet7", 0.78, 0.42),
        ("callisto", "planet1", 0.76, 0.38), ("rhea", "planet9", 0.74, 0.36),
        ("ganymede", "planet13", 0.74, 0.44), ("dione", "planet6", 0.74, 0.45),
    ]
    volcanic_pool = [
        ("io", "planet8", 0.84, 0.48), ("io", "planet5", 0.82, 0.46),
        ("mars", "planet16", 0.88, 0.34), ("planet8", "planet15", 0.72, 0.55),
        ("planet5", "planet8", 0.72, 0.48),
    ]
    small_pool = [
        ("phobos", "moon", 0.78, 0.35), ("deimos", "planet1", 0.76, 0.36),
        ("vesta", "planet9", 0.76, 0.40), ("ceres", "planet10", 0.74, 0.36),
        ("mimas", "moon", 0.74, 0.35), ("iapetus", "planet16", 0.74, 0.36),
        ("dione", "planet6", 0.74, 0.45),
    ]

    if preset_key:
        if allow_earth_height and (preset_key == "custom_earth" or preset_style == "earth"):
            add_pool(earth_pool, 180.0, "explicit Earth/custom_Earth")
        if preset_style in ("mars", "mars2", "earth2mars") or preset_info.get("terrain_family") == "volcanic_mars":
            add_pool(mars_pool, 64.0, f"{preset_style or preset_key} preset")
        elif preset_style == "earth" or preset_state == "wet" and preset_family in ("terra", "aquaria"):
            add_pool(wet_terra_pool, 54.0, f"{preset_key} wet/terra preset")
        elif preset_style in ("moon", "mercury", "vesta", "ceres") or preset_state == "airless":
            add_pool(crater_pool, 52.0, f"{preset_key} airless/cratered preset")
        elif preset_style in ("europa", "ganymede", "callisto", "pluto", "triton", "sedna", "eris", "enceladus", "dione", "tethys", "titan", "titan2", "white"):
            add_pool(icy_pool, 54.0, f"{preset_key} icy preset")
            if preset_style in ("titan", "titan2"):
                add_pool([("titan", "planet13", 0.78, 0.46), ("titan", "planet7", 0.78, 0.42)], 58.0, f"{preset_key} titan preset")
        elif preset_style in ("io", "io1", "io2"):
            add_pool(volcanic_pool, 58.0, f"{preset_key} volcanic preset")
        elif preset_style == "venus":
            add_pool(chaos_pool, 56.0, f"{preset_key} venus/chaotic preset")
        elif preset_family == "carbonia":
            add_pool(chaos_pool + crater_pool, 42.0, f"{preset_key} carbon-rich preset")
        elif preset_family == "ferria":
            add_pool(crater_pool, 44.0, f"{preset_key} ferria/metallic preset")

    if "mars" in diffmap or "mars" in bumpmap:
        add("mars", "planet16", 105.0, "DiffMap/BumpMap contains Mars", 0.92, 0.36)
        add("mars", "moon", 96.0, "DiffMap/BumpMap contains Mars", 0.88, 0.32)
        add("mars", "mercury", 92.0, "DiffMap/BumpMap contains Mars", 0.86, 0.30)
    elif "mars" in name:
        add("mars", "planet16", 46.0, "body name contains Mars", 0.90, 0.35)
        add("mars", "moon", 39.0, "body name contains Mars", 0.86, 0.32)

    if terrain_family == "volcanic_mars":
        add_pool(mars_pool, 34.0, "terrain family volcanic_mars")
    elif terrain_family in ("cratered_old", "cratered_sparse"):
        add_pool(crater_pool, 27.0, f"terrain family {terrain_family}")
    elif terrain_family == "rifted":
        add_pool(rift_pool, 30.0, "terrain family rifted")
    elif terrain_family == "tectonic_chaotic":
        add_pool(chaos_pool, 30.0, "terrain family tectonic_chaotic")
    elif terrain_family == "icy_fractured":
        add_pool(icy_pool, 30.0, "terrain family icy_fractured")
    elif terrain_family == "hybrid":
        add_pool(wet_terra_pool, 16.0, "terrain family hybrid")

    if allow_earth_height and ("earth" in diffmap or "earth" in bumpmap):
        add("earth", "planet5", 92.0, "DiffMap/BumpMap contains Earth", 0.82, 0.42)
        add("earth", "planet7", 86.0, "DiffMap/BumpMap contains Earth", 0.80, 0.46)
        add("earth", "venus", 78.0, "DiffMap/BumpMap contains Earth", 0.76, 0.38)

    wet_score = 0.0
    wet_reasons = []
    if raw_class in ("terra", "aquaria", "ocean", "marine", "panthalassic") or archetype == "ocean":
        wet_score += 18.0; wet_reasons.append("Terra/ocean class")
    if has_ocean:
        wet_score += 28.0; wet_reasons.append("Ocean")
    if has_life:
        wet_score += 24.0; wet_reasons.append("Life")
    if humidity >= 0.55:
        wet_score += 14.0; wet_reasons.append("high humidity")
    if wet_score > 0.0 and "mars" not in diffmap and "mars" not in bumpmap:
        reason = " + ".join(wet_reasons)
        add_pool(wet_terra_pool, wet_score + 18.0, reason)

    dry_score = 0.0
    dry_reasons = []
    if no_ocean:
        dry_score += 18.0; dry_reasons.append("NoOcean")
    if no_atm:
        dry_score += 14.0; dry_reasons.append("NoAtmosphere")
    if humidity <= 0.18:
        dry_score += 8.0; dry_reasons.append("low humidity")
    if crater >= 0.22:
        dry_score += 22.0 * crater; dry_reasons.append("high craterDensity")
    if any(x in hint for x in ("arid", "desert", "barren", "selena")):
        dry_score += 10.0; dry_reasons.append("dry preset")
    if dry_score > 12.0:
        reason = " + ".join(dry_reasons)
        add_pool(crater_pool, dry_score + 16.0, reason)

    icy_score = 0.0
    icy_reasons = []
    if any(x in hint for x in ("ice", "pluto", "europa", "enceladus", "callisto", "sedna", "rhea")) or archetype == "ice":
        icy_score += 26.0; icy_reasons.append("icy class/preset")
    if temp_k and temp_k < 235.0:
        icy_score += 18.0; icy_reasons.append("low temperature")
    if icecap < 0.8 or icecap_height > 0.4 or snow <= 0.35:
        icy_score += 12.0; icy_reasons.append("snow/ice flags")
    if icy_score > 0.0:
        reason = " + ".join(icy_reasons)
        add_pool(icy_pool, icy_score + 15.0, reason)

    if rift >= 0.18:
        reason = "rifts/canyons parameters"
        score = 25.0 + rift * 30.0
        add_pool(rift_pool, score + 10.0, reason)

    if chaos >= 0.18:
        reason = "venus/crack/chaos parameters"
        score = 24.0 + chaos * 28.0
        add_pool(chaos_pool, score + 10.0, reason)

    if volcano >= 0.18:
        reason = "volcano/lava/mare parameters"
        score = 25.0 + volcano * 35.0
        add_pool(volcanic_pool, score + 12.0, reason)

    small_body = radius_m and radius_m < 1_200_000.0
    if small_body or any(x in raw_class for x in ("asteroid", "moon", "dwarf")):
        score = 26.0 + crater * 16.0 + (12.0 if no_ocean else 0.0) + (8.0 if no_atm else 0.0)
        reason = "small moon/asteroid-like body"
        add_pool(small_pool, score + 35.0, reason)

    recent_primaries = _HEIGHTMAP_SELECTION_HISTORY[-6:]
    venus_count = sum(1 for primary in _HEIGHTMAP_SELECTION_HISTORY if primary == "venus")
    venus_share = venus_count / max(1, len(_HEIGHTMAP_SELECTION_HISTORY))
    for item in candidates.values():
        repeat_count = recent_primaries.count(item["primary"])
        if repeat_count:
            item["score"] -= 12.0 * repeat_count
            item["reason"] += f"; repeated-primary penalty x{repeat_count}"
        if "venus" in (item["primary"], item["secondary"]) and not venus_signal:
            item["score"] -= 80.0
            item["reason"] += "; venus penalized due to no venus signal"
        elif "venus" in (item["primary"], item["secondary"]) and venus_share > 0.20:
            item["score"] -= 35.0
            item["reason"] += "; venus global-use penalty"

    seed_basis = shared_seed if strict_same_surface and shared_seed else (
        body.get("heightmap_seed")
        or body.get("id")
        or f"{name}|{raw_class}|{terrain_family}|{radius_m:.0f}"
    )
    chosen, pool = weighted_pick_top_k(list(candidates.values()), seed_basis, k=6, strict_same_surface=strict_same_surface)
    seed = _stable_u32(seed_basis)
    rng = random.Random(seed ^ _stable_u32(f"{chosen['primary']}|{chosen['secondary']}"))
    mix0 = max(0.2, min(1.0, chosen["mix0"] + rng.uniform(-0.04, 0.04)))
    mix1 = max(0.0, min(0.85, chosen["mix1"] + rng.uniform(-0.04, 0.04)))
    result = {
        "primary": chosen["primary"],
        "secondary": chosen["secondary"],
        "height0": _height_path(chosen["primary"]),
        "height1": _height_path(chosen["secondary"]),
        "normal0": _normal_path(chosen["primary"]),
        "normal1": _normal_path(chosen["secondary"]),
        "mix0": mix0,
        "mix1": mix1,
        "offset0": float(seed % 400),
        "offset1": float((seed * 7 + 113) % 400),
        "flip_h0": bool((seed >> 1) & 1),
        "flip_v0": bool((seed >> 2) & 1),
        "flip_h1": bool((seed >> 3) & 1),
        "flip_v1": bool((seed >> 4) & 1),
        "reason": chosen["reason"],
        "score": float(chosen["score"]),
    }
    _HEIGHTMAP_SELECTION_HISTORY.append(result["primary"])
    if cache_key:
        _SHARED_HEIGHTMAP_CACHE[cache_key] = dict(result)

    top = ",".join(f"{item['primary']}+{item['secondary']}" for item in pool[:5])
    log_debug(
        f"[heightmap] Body='{body.get('name', raw.get('Name', 'unknown'))}' "
        f"family='{terrain_family}' preset='{preset_info.get('key', '')}' top='{top}' "
        f"selected='{result['primary']} + {result['secondary']}' reason='{result['reason']}'",
        "HEIGHTMAP",
    )
    return result


def _pick_visual_heightmaps(archetype: str, obj_id: int,
                            obj_data: dict | None = None,
                            surface_preset: str = "",
                            body_context: dict | None = None) -> tuple:
    if not USE_BUILTIN_HEIGHTMAP_OVERLAYS or DISABLE_BUILTIN_HEIGHTMAPS_FOR_GENERATED_SURFACES:
        return "", "", "", "", 1.0, 0.0, 0.0, 0.0, False, False, False, False
    raw = obj_data or {}
    strict, group = _shared_surface_settings(raw)
    opts = raw.get("_surface_options", {}) if isinstance(raw.get("_surface_options"), dict) else {}
    if strict and opts.get("terrain_family"):
        family = str(opts.get("terrain_family"))
    else:
        family = _surface_raw_family(raw, archetype, surface_preset)
    _surf_for_preset = raw.get("Surface", {}) if isinstance(raw.get("Surface"), dict) else {}
    preset_info = parse_se_surface_preset(surface_preset or _surf_for_preset.get("Preset", ""))
    body = dict(body_context or {})
    body.setdefault("id", obj_id)
    body.setdefault("archetype", archetype)
    body.setdefault("raw_data", raw)
    body.setdefault("surface_preset", surface_preset)
    selected = select_builtin_heightmap_pair(
        raw.get("Surface", {}) if isinstance(raw.get("Surface"), dict) else {},
        body,
        family,
        preset_info=preset_info,
        strict_same_surface=strict,
        shared_seed=group or None,
    )
    return (
        selected["height0"], selected["normal0"],
        selected["height1"], selected["normal1"],
        selected["mix0"], selected["mix1"],
        selected["offset0"], selected["offset1"],
        selected["flip_h0"], selected["flip_v0"],
        selected["flip_h1"], selected["flip_v1"],
    )


def _named_texture_key(diffmap: str):
    d = diffmap.lower()
    for k in _NAMED_BODY_TEXTURES:
        if k in d:
            return k
    return None


def _explicit_earth_like(obj_name: str, raw: dict, preset_info: dict) -> bool:
    name = str(obj_name or raw.get("Name", "")).strip().lower()
    raw_class = str(raw.get("Class", "")).strip().lower()
    key = preset_info.get("key", "")
    surf = raw.get("Surface", {}) if isinstance(raw.get("Surface"), dict) else {}
    diff = f"{surf.get('DiffMap', '')} {surf.get('BumpMap', '')}".lower()
    return (
        key == "custom_earth"
        or name in ("earth", "terra")
        or raw_class == "earth"
        or "earth/" in diff
        or diff.endswith("earth")
    )


def _explicit_inhabited(raw: dict) -> bool:
    for key in ("Inhabited", "Civilized", "Civilised", "Artificial", "HasCities", "CityLights", "Technosphere"):
        if key in raw and se_bool(raw.get(key)):
            return True
    life = raw.get("Life")
    if isinstance(life, dict):
        for key in ("Civilized", "Civilised", "Intelligent", "Artificial", "Technosphere"):
            if key in life and se_bool(life.get(key)):
                return True
    if isinstance(life, str) and any(t in life.lower() for t in ("civil", "intelligent", "technolog")):
        return True
    return False


def _city_light_settings(obj_id: int, obj_name: str, raw: dict,
                         preset_info: dict, has_life: bool,
                         has_organic_life: bool, has_aerial_life: bool,
                         has_atm: bool, is_gas: bool, is_star: bool,
                         life_info: dict | None = None) -> dict:
    life_info = life_info or parse_life_block(raw.get("Life") if isinstance(raw, dict) else None)
    flags = preset_info.get("appearance_flags", {})
    no_atm = se_bool(raw.get("NoAtmosphere", "false"))
    airless = bool(flags.get("airless"))
    explicit = _explicit_inhabited(raw)
    cls = str(raw.get("Class", "")).lower()
    type_name = str(raw.get("Type", "")).lower()
    radius = safe_float(raw.get("Radius", raw.get("RadiusKm", 0.0)))
    hostile_lava = any(flags.get(k) for k in ("lava", "magma")) or "lava" in preset_info.get("key", "")
    small_dead = radius > 0 and radius < 1500 and not (has_life or explicit)
    excluded = bool(
        is_star or is_gas
        or any(token in cls or token in type_name for token in ("asteroid", "comet", "barycenter", "black", "star"))
        or (small_dead and not explicit)
        or (hostile_lava and not explicit)
        or ((airless or no_atm) and not explicit)
    )
    life_allowed = GENERATE_CITY_LIGHTS_FOR_LIFE_WORLDS and (has_life or has_organic_life or has_aerial_life)
    terrestrial_city_allowed = bool(
        GENERATE_CITY_LIGHTS_FOR_MULTICELLULAR_TERRESTRIAL
        and life_info.get("has_terrestrial")
        and life_info.get("is_multicellular")
    )
    habitable_allowed = bool(
        GENERATE_CITY_LIGHTS_FOR_HABITABLE_WORLDS
        and has_atm
        and (flags.get("wet") or flags.get("allow_water") or cls in ("terra", "aquaria"))
    )
    if GENERATE_CITY_LIGHTS_ONLY_IF_CIVILIZED:
        enabled = explicit and not excluded
    else:
        enabled = bool((explicit or life_allowed or terrestrial_city_allowed or habitable_allowed) and not excluded)

    source = int(PROCEDURAL_CITY_LIGHT_SOURCE if USE_PROCEDURAL_CITY_LIGHT_SOURCE else 1)
    if not ALLOW_CITY_LIGHT_SOURCE_0 and source == 0:
        source = 1
    seed = (_stable_u32(f"city:{obj_id}:{obj_name}:{preset_info.get('key', '')}") % 99999) + 1

    if not enabled:
        reason = "barren/no life/no civilization"
        if life_info.get("has_marine") and not life_info.get("has_terrestrial"):
            reason = "marine life does not imply civilization"
        elif life_info.get("has_aerial") and not life_info.get("has_terrestrial"):
            reason = "aerial life does not imply civilization"
        elif life_info.get("has_subglacial") and not life_info.get("has_terrestrial"):
            reason = "subglacial life does not imply civilization"
        elif life_info.get("has_life"):
            reason = "life without civilization metadata"
        if is_star or is_gas:
            reason = "not a solid inhabited planet"
        elif excluded:
            reason = "excluded body type or hostile/airless"
        log_debug(
            f"[citylights] Body='{obj_name}' enabled=False source={source} emissive='' "
            f"brightness=0 seed=0 reason='{reason}'",
            "CITYLIGHTS",
        )
        return {
            "enabled": False,
            "emissive": "",
            "source": source,
            "seed": 0,
            "brightness": 0,
            "reason": reason,
        }

    reason_bits = []
    if explicit:
        reason_bits.append("civilized/procedural analog")
    if life_allowed:
        reason_bits.append("life option")
    if habitable_allowed:
        reason_bits.append("habitable option")
    reason = " + ".join(reason_bits) or "procedural city lights"
    brightness = 180 if explicit else 120
    log_debug(
        f"[citylights] Body='{obj_name}' enabled=True source={source} "
        f"emissive='Textures/planet_cities' seed={seed} reason='{reason}'",
        "CITYLIGHTS",
    )
    return {
        "enabled": True,
        "emissive": "Textures/planet_cities",
        "source": source,
        "seed": seed,
        "brightness": brightness,
        "reason": reason,
    }


# ─────────────────────────────────────────────────────────────────────────────
# ATMOSPHERE / COLOUR HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def get_atmosphere_color_from_se(atm_info, archetype: str, has_life: bool) -> str:
    if not atm_info:
        return "RGBA(0.200, 0.600, 1.000, 1.000)"
    comp = atm_info.get("comp", {})
    if comp:
        co2 = safe_float(comp.get("CO2",  0)); o2  = safe_float(comp.get("O2",  0))
        n2  = safe_float(comp.get("N2",   0)); ch4 = safe_float(comp.get("CH4", 0))
        nh3 = safe_float(comp.get("NH3",  0)); so2 = safe_float(comp.get("SO2", 0))
        if co2 > 90 and so2 > 0.1:  return "RGBA(0.900, 0.700, 0.300, 1.000)"
        if ch4 > 1.0:                return "RGBA(0.800, 0.500, 0.200, 1.000)"
        if 90 < co2 < 97:            return "RGBA(0.850, 0.550, 0.350, 1.000)"
        if nh3 > 5.0:                return "RGBA(0.600, 0.800, 0.400, 1.000)"
        if n2 > 70 and o2 > 15:      return "RGBA(0.200, 0.600, 1.000, 1.000)"
    hue = atm_info.get("hue"); sat = atm_info.get("saturation")
    if hue is not None and sat is not None and hue != 0:
        try:
            r, g, b = colorsys.hsv_to_rgb(float(hue) % 1.0, max(0.0, min(1.0, float(sat))), 0.85)
            return f"RGBA({r:.3f}, {g:.3f}, {b:.3f}, 1.000)"
        except (ValueError, TypeError):
            pass
    arch = (archetype or "").lower()
    if "ice" in arch:     return "RGBA(0.700, 0.800, 0.950, 1.000)"
    if "ocean" in arch or "terra" in arch: return "RGBA(0.200, 0.600, 1.000, 1.000)"
    if "lava" in arch:    return "RGBA(0.900, 0.400, 0.200, 1.000)"
    if "rocky" in arch:   return "RGBA(0.600, 0.600, 0.600, 1.000)"
    return "RGBA(0.200, 0.600, 1.000, 1.000)"


def _clamp01(value) -> float:
    return max(0.0, min(1.0, safe_float(value)))


def _rgba_string(color) -> str:
    r, g, b, a = color
    return f"RGBA({_clamp01(r):.3f}, {_clamp01(g):.3f}, {_clamp01(b):.3f}, {_clamp01(a):.3f})"


def _blend_rgba(a, b, t: float) -> tuple:
    t = _clamp01(t)
    return tuple(float(a[i]) * (1.0 - t) + float(b[i]) * t for i in range(4))


def _normalized_comp(comp: dict) -> dict:
    values = {str(k): max(0.0, safe_float(v)) for k, v in (comp or {}).items()}
    total = sum(values.values()) or 1.0
    return {k: v / total for k, v in values.items()}


def _atmosphere_model_key(model: str) -> str:
    model_clean = str(model or "").strip().lower()
    for key in ATMOSPHERE_MODEL_COLORS:
        if model_clean == key.lower():
            return key
    return "Earth"


def _chemistry_atmosphere_color(model: str, comp: dict) -> tuple[tuple, str, float]:
    frac = _normalized_comp(comp)
    n2o2 = frac.get("N2", 0.0) + frac.get("O2", 0.0)
    co2 = frac.get("CO2", 0.0)
    so2 = frac.get("SO2", 0.0) + frac.get("H2S", 0.0)
    methane = frac.get("CH4", 0.0) + frac.get("C2H6", 0.0) + frac.get("C2H2", 0.0)
    h2he = frac.get("H2", 0.0) + frac.get("He", 0.0)
    h2o = frac.get("H2O", 0.0)
    chlorine = frac.get("Cl2", 0.0)
    m = str(model or "").lower()
    if chlorine > 0.05 or "chlorine" in m:
        return (0.50, 0.85, 0.25, 1.0), "chlorine chemistry", 0.75
    if "titan" in m or (methane > 0.15 and n2o2 > 0.3):
        return (0.85, 0.56, 0.22, 1.0), "hydrocarbon haze", 0.70
    if "neptune" in m or (methane > 0.05 and h2he > 0.4):
        return (0.25, 0.45, 0.95, 1.0), "methane giant tint", 0.65
    if so2 > 0.03 or "venus" in m:
        return (0.90, 0.72, 0.42, 1.0), "sulfur/venus chemistry", 0.70
    if co2 > 0.35 or "mars" in m:
        return (0.78, 0.47, 0.27, 1.0), "CO2 dust haze", 0.55
    if h2o > 0.25:
        return (0.70, 0.82, 1.00, 1.0), "water vapor haze", 0.45
    if n2o2 > 0.65:
        return (0.227, 0.604, 1.000, 1.0), "N2/O2 Rayleigh", 0.65
    if h2he > 0.60:
        return (0.76, 0.62, 0.46, 1.0), "hydrogen/helium atmosphere", 0.55
    return (0.50, 0.65, 0.90, 1.0), "weak chemistry signal", 0.25


def _pressure_based_opacity(pressure_atm: float) -> float:
    if pressure_atm <= 0:
        return 0.0
    return _clamp01(0.08 + math.log10(max(pressure_atm, 0.001) * 10.0) * 0.18)


def _life_cloud_tint(life_info: dict) -> tuple[str | None, float, str]:
    if not life_info.get("has_aerial"):
        return None, 0.0, "no aerial life"
    if life_info.get("is_exotic"):
        tint = "RGBA(0.560, 0.180, 0.760, 1.000)"
        strength = 0.18 if life_info.get("is_unicellular") else 0.30
        return tint, strength, "Aerial exotic life affects clouds, not vegetation"
    if life_info.get("is_organic"):
        tint = "RGBA(0.180, 0.760, 0.620, 1.000)"
        strength = 0.12 if life_info.get("is_unicellular") else 0.22
        return tint, strength, "Aerial organic life affects clouds, not vegetation"
    return None, 0.0, "aerial life without chemistry class"


def _se_hue_position(hue: float) -> float:
    h = max(-0.5, min(0.5, safe_float(hue, 0.0)))
    if h >= 0.0:
        return h / 0.1
    return 6.0 + ((h + 0.5) / 0.1)


def _interpolate_se_hue_rgb(table: dict, hue: float) -> tuple:
    keys      = SE_HUE_KEYS
    h         = max(-0.5, min(0.5, safe_float(hue, 0.0)))
    p         = _se_hue_position(h)
    positions = sorted((_se_hue_position(k), k) for k in keys)
    for i in range(len(positions) - 1):
        p0, k0 = positions[i]
        p1, k1 = positions[i + 1]
        if p0 <= p <= p1:
            t  = (p - p0) / max(1e-9, p1 - p0)
            c0 = table[k0]; c1 = table[k1]
            return tuple((c0[j] + (c1[j] - c0[j]) * t) / 255.0 for j in range(3)) + (1.0,)
    # wrap from -0.1 back to 0.0
    p0, k0 = positions[-1]
    p1_val  = positions[0][0] + 11.0
    k1      = positions[0][1]
    p_adj   = p if p >= positions[0][0] else p + 11.0
    t = (p_adj - p0) / max(1e-9, p1_val - p0)
    c0 = table[k0]; c1 = table[k1]
    return tuple((c0[j] + (c1[j] - c0[j]) * t) / 255.0 for j in range(3)) + (1.0,)


def _apply_se_saturation(rgb: tuple, saturation) -> tuple:
    sat = max(0.0, min(1.5, safe_float(saturation, 1.0)))
    r, g, b, a = rgb
    lum = 0.2126*r + 0.7152*g + 0.0722*b
    if sat <= 1.0:
        out = tuple(lum + (c - lum) * sat for c in (r, g, b))
    else:
        boost = 1.0 + (sat - 1.0) * 0.35
        out   = tuple(max(0.0, min(1.0, lum + (c - lum) * boost)) for c in (r, g, b))
    return out + (a,)


def _se_model_hue_visual_rgba(atm_info: dict, model_key: str) -> tuple:
    hue = safe_float(atm_info.get("hue", atm_info.get("Hue", 0.0)), 0.0)
    sat = safe_float(atm_info.get("saturation", atm_info.get("Saturation", 1.0)), 1.0)
    if model_key == "Titan":
        sky  = _interpolate_se_hue_rgb(SE_TITAN_SKY_HUE_RGB, hue)
        land = _interpolate_se_hue_rgb(SE_TITAN_LAND_HUE_RGB, hue)
        rgb  = tuple(sky[i] * 0.7 + land[i] * 0.3 for i in range(3)) + (1.0,)
    else:
        table = SE_ATMOSPHERE_MODEL_HUE_RGB.get(model_key)
        if table is None:
            pressure = safe_float(atm_info.get("pressure", atm_info.get("Pressure", 1.0)), 1.0)
            table = SE_ATMOSPHERE_MODEL_HUE_RGB["Thick"] if pressure >= 5.0 else SE_ATMOSPHERE_MODEL_HUE_RGB["Earth"]
        rgb = _interpolate_se_hue_rgb(table, hue)
    return _apply_se_saturation(rgb, sat)


def _se_visual_opacity(atm_info: dict, model_key: str) -> float:
    pressure   = max(0.0, safe_float(atm_info.get("pressure", atm_info.get("Pressure", 1.0)), 1.0))
    se_opacity = max(0.0, min(1.0, safe_float(atm_info.get("opacity", atm_info.get("Opacity", 1.0)), 1.0)))
    lo, hi     = SE_ATMOSPHERE_VISUAL_OPACITY.get(model_key, SE_ATMOSPHERE_VISUAL_OPACITY["Earth"])
    pressure_factor = math.log10(pressure + 1.0) / math.log10(31.0)
    pressure_factor = max(0.0, min(1.0, pressure_factor))
    opacity = lo + (hi - lo) * pressure_factor
    opacity *= max(0.25, se_opacity)
    return round(max(lo, min(hi, opacity)), 4)


def _soft_chemistry_tint(model: str, comp: dict) -> tuple:
    chem, reason, weight = _chemistry_atmosphere_color(model, comp)
    return chem, reason, min(0.15, weight * 0.20)


def compute_atmosphere_appearance(obj_name: str, atm_info: dict, raw: dict,
                                  preset_info: dict, has_atm: bool,
                                  atmosphere_mass: float, archetype: str,
                                  life_info: dict | None = None) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    if se_bool(raw.get("NoAtmosphere", "false")) or not has_atm or atmosphere_mass <= 0.0:
        color = "RGBA(0.000, 0.000, 0.000, 0.000)"
        result = {"show": False, "color": color, "opacity": 0.0, "rayleigh": 0.0, "haze": 0, "reason": "NoAtmosphere/no mass"}
        log_debug(f"[atmosphere] Body='{obj_name}' model='None' haze=0 opacity=0.00 color='{color}' reason='{result['reason']}'", "ATMOSPHERE")
        return result

    atm_info = atm_info or {}
    model = atm_info.get("model", "") or atm_info.get("Model", "") or "Earth"
    model_key = _atmosphere_model_key(model)

    # Use SE model+hue table as visual source of truth instead of generic HSV
    base = _se_model_hue_visual_rgba(atm_info, model_key)

    comp = atm_info.get("comp", {}) or {}
    if not comp and isinstance(raw.get("Atmosphere"), dict):
        comp = raw["Atmosphere"].get("Composition", {}) or {}

    # Chemistry is a subtle tint only (max 15% weight)
    chem, chem_reason, chem_weight = _soft_chemistry_tint(model, comp)
    pressure = safe_float(atm_info.get("pressure", atm_info.get("Pressure", 0.0)))
    color = _rgba_string(_blend_rgba(base, chem, chem_weight))

    life_tint, life_strength, life_reason = _life_cloud_tint(life_info or {})
    if life_tint:
        color = _blend_rgba_strings(color, life_tint, life_strength * 0.45)

    # Opacity from SE model + pressure + SE Opacity field
    opacity = _se_visual_opacity(atm_info, model_key)

    # Rayleigh by model
    if model_key in ("Ethereal", "Pluto", "Thin", "Mars"):
        rayleigh = 0.35
    elif model_key in ("Earth", "Biogenic"):
        rayleigh = 0.65
    elif model_key in ("Thick", "Titan", "Venus"):
        rayleigh = 0.25
    elif model_key in ("Jupiter", "Neptune", "Sun"):
        rayleigh = 0.40
    elif model_key == "Chlorine":
        rayleigh = 0.35
    else:
        rayleigh = 0.50

    haze   = get_haze_type_from_se_model(model)
    reason = f"{chem_reason} + {model_key} model"
    if life_tint:
        reason += f" + {life_reason}"

    hue_val = safe_float(atm_info.get("hue", atm_info.get("Hue", 0.0)), 0.0)
    sat_val = safe_float(atm_info.get("saturation", atm_info.get("Saturation", 1.0)), 1.0)
    log_debug(
        f"[atmo-visual] Body='{obj_name}' model='{model_key}' "
        f"hue={hue_val:.3f} saturation={sat_val:.3f} "
        f"pressure_atm={pressure:.6g} color='{color}' opacity={opacity:.3f} "
        f"haze={haze} source='se_model_hue_table' chemistry_tint='{chem_reason}'",
        "ATMOSPHERE",
    )
    return {"show": True, "color": color, "opacity": round(opacity, 4),
            "rayleigh": round(rayleigh, 4), "haze": haze, "reason": reason}


def calculate_scattering_color(model_name, bright, sat, hue) -> list:
    test_data = {
        "earth":   [{"bright":  1,"sat":1,"hue":0.0,"rgb":(255, 34, 72)},
                    {"bright": 10,"sat":2,"hue":0.0,"rgb":(255, 31,109)},
                    {"bright": 10,"sat":0,"hue":0.0,"rgb":(255, 76, 74)},
                    {"bright": 10,"sat":1,"hue":0.5,"rgb":(255, 64, 59)}],
        "jupiter": [{"bright":  1,"sat":1,"hue":0.0,"rgb":(255, 31, 67)},
                    {"bright":  1,"sat":1,"hue":0.5,"rgb":(255, 25, 75)},
                    {"bright": 10,"sat":2,"hue":0.0,"rgb":(255, 66,144)},
                    {"bright": 20,"sat":1,"hue":0.0,"rgb":(219, 91,136)}],
        "mars":    [{"bright":  1,"sat":1,"hue":0.0,"rgb":(255, 38, 46)},
                    {"bright": 10,"sat":1,"hue":0.5,"rgb":(255, 56, 61)},
                    {"bright": 10,"sat":2,"hue":0.0,"rgb":(255, 49, 55)},
                    {"bright": 20,"sat":1,"hue":0.0,"rgb":(255, 50, 50)}],
        "neptune": [{"bright":  1,"sat":1,"hue":0.0,"rgb":(255, 57,255)},
                    {"bright": 10,"sat":1,"hue":0.5,"rgb":(255, 87, 43)},
                    {"bright": 10,"sat":2,"hue":0.0,"rgb":(255, 56,169)},
                    {"bright": 20,"sat":1,"hue":0.0,"rgb":(220, 79, 79)}],
    }
    model_clean = str(model_name or "earth").strip().lower()
    for key in test_data:
        if key in model_clean:
            model_clean = key
            break
    data_points = test_data.get(model_clean, test_data["earth"])
    bright_f = safe_float(bright); sat_f = safe_float(sat); hue_f = safe_float(hue) % 1.0
    distances = sorted(
        [(math.sqrt((p["bright"]-bright_f)**2 + (p["sat"]-sat_f)**2
                    + (p["hue"]-hue_f)**2 * 0.25), p) for p in data_points],
        key=lambda x: x[0]
    )
    if len(distances) >= 2 and distances[0][0] > 0.001:
        d1, p1 = distances[0]; d2, p2 = distances[1]
        total_d = d1 + d2 + 0.0001
        w1 = (total_d - d1) / total_d; w2 = (total_d - d2) / total_d
        r = (p1["rgb"][0]*w1 + p2["rgb"][0]*w2) / 255.0
        g = (p1["rgb"][1]*w1 + p2["rgb"][1]*w2) / 255.0
        b = (p1["rgb"][2]*w1 + p2["rgb"][2]*w2) / 255.0
    else:
        cr = distances[0][1]["rgb"]
        r, g, b = cr[0]/255.0, cr[1]/255.0, cr[2]/255.0
    if hue_f > 0.01:
        try:
            h_c, s_c, v_c = colorsys.rgb_to_hsv(r, g, b)
            r, g, b = colorsys.hsv_to_rgb(hue_f, s_c, v_c)
        except Exception:
            pass
    return [max(0.0,min(1.0,r)), max(0.0,min(1.0,g)), max(0.0,min(1.0,b)), 1.0]


def map_gas_giant_bands(se_cloud_data, planet_class_name) -> dict:
    if not se_cloud_data:
        return {"BandCount":12,"ColorVariance":0.5,"BandTurbulence":2,
                "StormFrequency":0.5,"StormDensity":0.3,"SpotRadius":0.8}
    stripe_zones = safe_float(se_cloud_data.get("stripeZones",   5.0))
    stripe_fluct = safe_float(se_cloud_data.get("stripeFluct",   0.5))
    stripe_twist = safe_float(se_cloud_data.get("stripeTwist",   5.0))
    cyclone_freq = safe_float(se_cloud_data.get("cycloneFreq",   1.0))
    cyclone_dens = safe_float(se_cloud_data.get("cycloneDensity",0.5))
    cyclone_magn = safe_float(se_cloud_data.get("cycloneMagn",   1.0))
    num_bands = int(6 + stripe_zones * 4.2)
    if   stripe_fluct < 0.3: color_variance = stripe_fluct / 3.0
    elif stripe_fluct < 0.6: color_variance = 0.1 + (stripe_fluct - 0.3) * 1.33
    elif stripe_fluct < 1.0: color_variance = 0.5 + (stripe_fluct - 0.6) * 1.0
    else:                    color_variance = min(1.0, 0.9 + (stripe_fluct - 1.0) * 0.1)
    return {
        "BandCount":      num_bands,
        "ColorVariance":  color_variance,
        "BandTurbulence": int(min(4, stripe_twist / 5.0)),
        "StormFrequency": min(20.0, cyclone_freq * 10.0),
        "StormDensity":   min(1.0, max(0.0, cyclone_dens)),
        "SpotRadius":     0.3 + min(1.2, cyclone_magn * 0.6),
        "SpotBrightness": 1.0 if cyclone_magn < 0.5 else 1.0 + (cyclone_magn - 0.5) * 0.4,
    }


def get_ui_color(archetype, is_star, has_life, se_class, comp, temp,
                 has_ocean=False, pressure=0, has_exotic_life=False,
                 has_organic_life=False, radius_m=0, mass_kg=0) -> str:
    import globals_compat as _gc
    force_organic_green  = getattr(_gc, "UI_FORCE_ORGANIC_GREEN",  True)
    force_exotic_magenta = getattr(_gc, "UI_FORCE_EXOTIC_MAGENTA", True)
    se_lo = str(se_class).lower()
    life_debug = choose_life_debug_color({
        "is_exotic": bool(has_exotic_life),
        "is_organic": bool(has_organic_life),
    })
    if life_debug == "RGBA(1.000, 0.000, 1.000, 1.000)" and force_exotic_magenta:
        return life_debug
    if life_debug == "RGBA(0.000, 1.000, 0.000, 1.000)" and force_organic_green:
        return life_debug
    if not is_star and not has_life and radius_m > 0 and mass_kg > 0:
        esi = calculate_esi(temp, radius_m, mass_kg, pressure)
        has_o2 = safe_float(comp.get("O2", 0)) > 10.0
        has_n2 = safe_float(comp.get("N2", 0)) > 40.0
        if esi >= 0.9 and has_o2 and has_n2 and pressure > 0.1:
            return "RGBA(0.667, 1.000, 0.667, 1.000)"
    if is_star:
        if temp > 10000: return "RGBA(0.400, 0.600, 1.000, 1.000)"
        if temp > 7500:  return "RGBA(0.800, 0.900, 1.000, 1.000)"
        if temp > 5200:  return "RGBA(1.000, 0.950, 0.500, 1.000)"
        if temp > 3700:  return "RGBA(1.000, 0.600, 0.100, 1.000)"
        return                  "RGBA(1.000, 0.200, 0.050, 1.000)"
    if archetype in ("gas_giant", "ice_giant"):
        if   temp < 150:  return "RGBA(0.839, 0.549, 0.275, 1.000)"
        elif temp < 250:  return "RGBA(0.855, 0.529, 0.290, 1.000)"
        elif temp < 900:  return "RGBA(0.863, 0.510, 0.322, 1.000)"
        elif temp < 1400: return "RGBA(0.875, 0.486, 0.361, 1.000)"
        else:             return "RGBA(0.894, 0.463, 0.408, 1.000)"
    if has_ocean or any(k in se_lo for k in ("oceania","superoceania","panthalassic","marine","ocean")):
        return "RGBA(0.118, 0.275, 0.784, 1.000)"
    is_ferria = any(k in se_lo for k in ("ferria","metalrich","metal rich"))
    if is_ferria:
        return "RGBA(0.471, 0.314, 0.314, 1.000)" if (archetype == "lava" or temp > 700) else "RGBA(0.549, 0.549, 0.549, 1.000)"
    if archetype == "lava" or any(k in se_lo for k in ("lava","volcanic")):
        return "RGBA(0.745, 0.294, 0.098, 1.000)"
    if archetype == "ice" or any(k in se_lo for k in ("aquaria","ice","glacial","tundra","cryo","titan")):
        return "RGBA(0.314, 0.824, 1.000, 1.000)"
    if archetype == "rocky" or any(k in se_lo for k in ("arid","barren","desert","selena","terra","carbonia")):
        return "RGBA(0.745, 0.745, 0.745, 1.000)"
    return "RGBA(0.600, 0.600, 0.600, 1.000)"


def calculate_esi(temp_k, radius_m, mass_kg, pressure_atm, albedo=0.3) -> float:
    if radius_m <= 0 or mass_kg <= 0:
        return 0.0
    EARTH_R = 6_371_000.0; EARTH_D = 5.52; EARTH_T = 288.0; EARTH_P = 1.0
    density  = max(0.1, min(20.0, mass_kg / max(1.0, (4/3)*math.pi*radius_m**3) / 1000.0))
    temp_k   = max(10.0, min(1500.0, temp_k))
    pressure_atm = max(0.001, min(1000.0, pressure_atm))
    x_r   = max(0.0, min(1.0, 1.0 - abs(radius_m - EARTH_R) / EARTH_R))
    x_rho = max(0.0, min(1.0, 1.0 - abs(density  - EARTH_D) / EARTH_D))
    x_t   = max(0.0, min(1.0, 1.0 - abs(temp_k   - EARTH_T) / EARTH_T))
    x_p   = max(0.0, min(1.0, 1.0 - abs(pressure_atm - EARTH_P) / EARTH_P))
    w_r, w_rho, w_t, w_p = 0.57, 1.07, 5.58, 0.70
    tw = w_r + w_rho + w_t + w_p
    return max(0.0, min(1.0, (x_r**w_r * x_rho**w_rho * x_t**w_t * x_p**w_p) ** (1.0/tw)))


def derive_water_color_from_ocean(ocean_data) -> str:
    if not isinstance(ocean_data, dict):
        return WATER_COLORS["default"]
    comp = ocean_data.get("Composition", {})
    if not comp:
        return WATER_COLORS["default"]
    if safe_float(comp.get("CH4", 0)) > 5 or safe_float(comp.get("C2H6", 0)) > 5:
        return WATER_COLORS["titan"]
    if safe_float(comp.get("NH3", 0)) > 10:   return "RGBA(0.800, 0.700, 0.300, 1.000)"
    if safe_float(comp.get("SO2", 0)) > 5 or safe_float(comp.get("H2S", 0)) > 1:
        return "RGBA(0.500, 0.600, 0.200, 1.000)"
    if safe_float(comp.get("Cl2", 0)) > 0.01: return "RGBA(0.200, 0.700, 0.600, 1.000)"
    return WATER_COLORS["default"]


LIFE_VEGETATION_FALLBACKS = {
    ("Organic", "Multicellular", "Terrestrial"): "RGBA(0.100, 0.300, 0.080, 1.000)",
    ("Organic", "Unicellular", "Terrestrial"): "RGBA(0.220, 0.300, 0.120, 1.000)",
    ("Exotic", "Multicellular", "Terrestrial"): "RGBA(0.500, 0.060, 0.700, 1.000)",
    ("Exotic", "Unicellular", "Terrestrial"): "RGBA(0.260, 0.080, 0.320, 1.000)",
}

WATER_LIFE_TINTS = {
    ("Organic", "Unicellular", "Marine"): "RGBA(0.000, 0.180, 0.150, 1.000)",
    ("Organic", "Multicellular", "Marine"): "RGBA(0.000, 0.260, 0.180, 1.000)",
    ("Exotic", "Unicellular", "Marine"): "RGBA(0.180, 0.050, 0.300, 1.000)",
    ("Exotic", "Multicellular", "Marine"): "RGBA(0.320, 0.050, 0.500, 1.000)",
}


def _class_key(life_info: dict) -> str:
    if life_info.get("is_exotic"):
        return "Exotic"
    if life_info.get("is_organic"):
        return "Organic"
    return "Organic"


def _complexity_key(life_info: dict) -> str:
    return "Multicellular" if life_info.get("is_multicellular") else "Unicellular"


def _blend_rgba_strings(base: str, tint: str, strength: float) -> str:
    return _rgba_string(_blend_rgba(parse_rgba(base), parse_rgba(tint), strength))


def should_enable_vegetation(life_info: dict, preset_info: dict, body_class: str, archetype: str) -> bool:
    body_class_l = str(body_class or "").strip().lower()
    if archetype in ("gas_giant", "ice_giant", "star") or body_class_l in ("gasgiant", "icegiant", "star", "barycenter"):
        return False
    if preset_info.get("explicit_plants") and (
        life_info.get("has_terrestrial") or ALLOW_PRESET_PLANTS_WITHOUT_LIFE
    ):
        return True
    if life_info.get("has_terrestrial") and life_info.get("is_multicellular"):
        return True
    if CONFIG_ENABLE_MICROBIAL_SURFACE_MATS and life_info.get("has_terrestrial") and life_info.get("is_unicellular"):
        return True
    return False


def compute_vegetation_appearance(life_info: dict, preset_info: dict,
                                  body_class: str, archetype: str) -> dict:
    plant_color = preset_info.get("plant_color")
    enabled = should_enable_vegetation(life_info, preset_info, body_class, archetype)
    if plant_color in VEGETATION_COLORS:
        return {
            "enabled": enabled,
            "color": VEGETATION_COLORS[plant_color],
            "mode": 1 if enabled else 0,
            "reason": f"preset plant token {plant_color}" if enabled else "preset color present but vegetation disabled by body type",
        }
    if not enabled:
        if life_info.get("has_aerial") and not life_info.get("has_terrestrial"):
            reason = "Aerial life only; no terrestrial vegetation"
        elif life_info.get("has_marine") and not life_info.get("has_terrestrial"):
            reason = "Marine life only; no terrestrial multicellular vegetation"
        elif life_info.get("has_subglacial") and not life_info.get("has_terrestrial"):
            reason = "Subglacial life only; no surface vegetation"
        elif life_info.get("has_terrestrial") and life_info.get("is_unicellular"):
            reason = "Terrestrial unicellular life; microbial mats disabled"
        else:
            reason = "no terrestrial multicellular life or plant preset"
        return {"enabled": False, "color": VEGETATION_COLORS["green"], "mode": 0, "reason": reason}

    cls = _class_key(life_info)
    complexity = _complexity_key(life_info)
    color = LIFE_VEGETATION_FALLBACKS.get((cls, complexity, "Terrestrial"), VEGETATION_COLORS["green"])
    reason = f"{cls} {complexity} terrestrial life"
    return {"enabled": True, "color": color, "mode": 1 if complexity == "Multicellular" else 0, "reason": reason}


def _is_h2o_dominant_ocean(ocean_info: dict) -> bool:
    if not isinstance(ocean_info, dict):
        return False
    comp = ocean_info.get("Composition", {})
    if not isinstance(comp, dict):
        return False
    h2o   = safe_float(comp.get("H2O", 0.0))
    total = sum(max(0.0, safe_float(v)) for v in comp.values()) or 1.0
    return h2o / total >= 0.50


def _brighten_h2o_ocean_color(color: str) -> str:
    r, g, b, a = parse_rgba(color)
    r = max(r, 20 / 255.0)
    g = max(g, 55 / 255.0)
    b = max(b, 90 / 255.0)
    lum = 0.2126*r + 0.7152*g + 0.0722*b
    min_lum = 0.22
    if lum < min_lum:
        scale = min_lum / max(lum, 1e-6)
        r = min(1.0, r * scale)
        g = min(1.0, g * scale)
        b = min(1.0, b * scale)
    return _rgba_string((r, g, b, a))


def _ocean_chemistry_reason(ocean_info: dict) -> str:
    comp = ocean_info.get("Composition", {}) if isinstance(ocean_info, dict) else {}
    if not isinstance(comp, dict) or not comp:
        return "default H2O-like ocean"
    if safe_float(comp.get("CH4", 0)) > 1 or safe_float(comp.get("C2H6", 0)) > 1:
        return "hydrocarbon ocean chemistry"
    if safe_float(comp.get("N2", 0)) > 10 or safe_float(comp.get("CO", 0)) > 1:
        return "N2/CO cryogenic ocean chemistry"
    if safe_float(comp.get("SO2", 0)) > 1 or safe_float(comp.get("H2S", 0)) > 1:
        return "sulfur ocean chemistry"
    if safe_float(comp.get("NaCl", 0)) > 1:
        return "salty brine chemistry"
    return "ocean chemistry"


def compute_water_color(ocean_info: dict, preset_info: dict, life_info: dict,
                        temperature_info=None) -> dict:
    hint = preset_info.get("water_color_hint")
    if hint in WATER_COLORS:
        color = WATER_COLORS[hint]
        reason = f"{hint} preset"
    else:
        color = derive_water_color_from_ocean(ocean_info)
        reason = _ocean_chemistry_reason(ocean_info)

    comp = ocean_info.get("Composition", {}) if isinstance(ocean_info, dict) else {}
    if isinstance(comp, dict):
        if safe_float(comp.get("N2", 0)) > 10 or safe_float(comp.get("CO", 0)) > 1:
            color = _blend_rgba_strings(color, "RGBA(0.620, 0.460, 0.250, 1.000)", 0.35)
            reason += " + N2/CO cryogenic tint"
        if safe_float(comp.get("NaCl", 0)) > 1:
            color = _blend_rgba_strings(color, "RGBA(0.060, 0.220, 0.170, 1.000)", 0.18)
            reason += " + brine tint"

    if life_info.get("has_marine"):
        tint_key = (_class_key(life_info), _complexity_key(life_info), "Marine")
        tint = WATER_LIFE_TINTS.get(tint_key)
        if tint:
            strength = 0.16 if life_info.get("is_unicellular") else 0.28
            color = _blend_rgba_strings(color, tint, strength)
            reason += f" + {tint_key[0]} {tint_key[1]} marine tint"
    elif life_info.get("has_subglacial"):
        tint = "RGBA(0.080, 0.260, 0.240, 1.000)" if life_info.get("is_organic") else "RGBA(0.230, 0.110, 0.320, 1.000)"
        color = _blend_rgba_strings(color, tint, 0.16)
        reason += " + subglacial brine tint"

    # H2O-dominant ocean brightness clamp — never export oil-black water
    if _is_h2o_dominant_ocean(ocean_info):
        before = color
        if hint in ("oil", "carbon_black"):
            color  = WATER_COLORS["default"]
            reason += " + H2O override dark preset"
        color = _brighten_h2o_ocean_color(color)
        if color != before:
            reason += " + H2O visible brightness clamp"

    return {"color": color, "reason": reason}


def extract_cloud_layers(obj_data: dict) -> dict:
    raw = obj_data.get("raw_data", {}) if "raw_data" in obj_data else obj_data
    result = analyse_cloud_layers(raw, "rocky")
    return {"coverage": result["coverage"], "height": 15.0, "velocity": 20.0}

def _norm_velocity(raw_vel: float) -> float:
    """SE cloud velocity (km/h or unitless) → US m/s, clamped to ±6000."""
    return max(-6000.0, min(6000.0, raw_vel * 0.2778))  # km/h → m/s


def analyse_cloud_layers(obj_data: dict, archetype: str) -> dict:
    from constants import US_CLOUD_STYLES
    raw = obj_data if isinstance(obj_data, dict) else {}
    clouds_list = _cloud_blocks(raw)

    is_gas = archetype in ("gas_giant", "ice_giant")
    surf = raw.get("Surface", {})
    surf = surf if isinstance(surf, dict) else {}
    stripe_zones = safe_float(surf.get("stripeZones",    0.0)) / 10.0
    stripe_twist = safe_float(surf.get("stripeTwist",    0.0)) / 20.0
    cyclone_magn = safe_float(surf.get("cycloneMagn",    0.0)) / 20.0
    cyclone_freq = safe_float(surf.get("cycloneFreq",    0.0)) / 2.0
    cyclone_dens = safe_float(surf.get("cycloneDensity", 0.0)) / 1.0
    cyclone_oct  = safe_float(surf.get("cycloneOctaves", 0.0)) / 10.0

    if not clouds_list:
        if is_gas:
            return {"cloud_set_a":US_CLOUD_STYLES["Streaks"],"cloud_set_b":US_CLOUD_STYLES["Turbulent"],
                    "custom_appearance":True,"coverage":1.0,"opacity":1.0,
                    "color_rgba":"RGBA(1.000, 1.000, 1.000, 1.000)"}
        return {"cloud_set_a":US_CLOUD_STYLES["Fluffy"],"cloud_set_b":US_CLOUD_STYLES["Fluffy"],
                "custom_appearance":False,"coverage":0.5,"opacity":0.8,
                "color_rgba":"RGBA(1.000, 1.000, 1.000, 1.000)"}

    def _style_for_layer(c: dict) -> str:
        coverage = safe_float(c.get("Coverage",    0.5))
        velocity = _norm_velocity(safe_float(c.get("Velocity",   20.0)))
        octaves  = safe_float(c.get("mainOctaves",  4.0)) / 20.0
        freq     = safe_float(c.get("mainFreq",     1.0)) / 3.0
        bump     = safe_float(c.get("BumpHeight",   0.0))
        mc = c.get("ModulateColor", "")
        alpha = 1.0
        if mc:
            nums = re.findall(r'[\d.]+', str(mc))
            if len(nums) >= 4: alpha = safe_float(nums[3])
        if is_gas:
            strong_bands = stripe_zones >= 0.3 or stripe_twist >= 0.15
            if strong_bands or velocity >= 2000:              return "Streaks"
            if octaves >= 0.30 or freq >= 0.50 or bump >= 0.3: return "Turbulent"
            if cyclone_magn >= 0.075:                         return "Storm"
            return "Streaks"
        if coverage >= 0.80:
            if velocity >= 2000 or bump >= 0.4: return "Storm"
            return "Thick"
        if coverage >= 0.40:
            if octaves >= 0.35 or freq >= 0.67: return "Turbulent"
            if velocity >= 3000:              return "Storm"
            return "Fluffy"
        if coverage >= 0.10:
            if alpha < 0.5: return "Streaks"
            return "Wispy"
        if cyclone_magn >= 0.075 and cyclone_freq >= 0.5: return "Storm"
        if cyclone_dens >= 0.7:                             return "Storm"
        return "Thin"

    scored = [_style_for_layer(c) for c in clouds_list]
    if is_gas and (stripe_zones >= 3 or stripe_twist >= 3) and "Streaks" not in scored:
        scored[0] = "Streaks"

    from collections import Counter
    counts = Counter(scored)
    ranked = [s for s, _ in counts.most_common()]
    style_a = ranked[0]
    style_b = ranked[1] if len(ranked) > 1 else ranked[0]
    custom  = (style_a != style_b)

    layer_coverages = [_clamp01(c.get("Coverage", c.get("coverage", 0.0)))
                       for c in clouds_list]
    coverage = combine_cloud_coverage(clouds_list)

    alphas = []
    for c in clouds_list:
        mc = c.get("ModulateColor", "")
        if mc:
            nums = re.findall(r'[\d.]+', str(mc))
            if len(nums) >= 4: alphas.append(safe_float(nums[3]))
    if alphas:
        base_opacity = sum(alphas) / len(alphas)
        opacity = min(1.0, base_opacity * (1.0 + (len(clouds_list) - 1) * 0.12))
    else:
        opacity = min(1.0, coverage * 1.1)

    r_sum = g_sum = b_sum = 0.0; n_col = 0
    for c in clouds_list:
        mc = c.get("ModulateColor", "")
        if mc:
            nums = re.findall(r'[\d.]+', str(mc))
            if len(nums) >= 3:
                r_sum += safe_float(nums[0]); g_sum += safe_float(nums[1])
                b_sum += safe_float(nums[2]); n_col += 1
    if n_col:
        cr = min(1.0, r_sum/n_col); cg = min(1.0, g_sum/n_col); cb = min(1.0, b_sum/n_col)
    else:
        cr = cg = cb = 1.0
    color_rgba = f"RGBA({cr:.3f}, {cg:.3f}, {cb:.3f}, 1.000)"

    result = {
        "cloud_set_a":       US_CLOUD_STYLES.get(style_a, 1),
        "cloud_set_b":       US_CLOUD_STYLES.get(style_b, 1),
        "custom_appearance": custom,
        "coverage":          round(coverage, 4),
        "opacity":           round(max(0.0, min(1.0, opacity)), 4),
        "color_rgba":        color_rgba,
    }
    log_debug(
        f"[clouds] Body='{raw.get('Name', 'unknown')}' archetype='{archetype}' "
        f"layers={len(clouds_list)} coverages={layer_coverages} total_coverage={coverage:.4g} "
        f"styleA={style_a}({result['cloud_set_a']}) styleB={style_b}({result['cloud_set_b']}) "
        f"opacity={result['opacity']:.3f} "
        f"cyclone={cyclone_magn:.2f} stripes={stripe_zones:.1f}", "CLOUD")
    return result


def _cloud_blocks(raw: dict) -> list:
    cloud_blocks = raw.get("Clouds") if isinstance(raw, dict) else None
    if isinstance(cloud_blocks, list):
        return [c for c in cloud_blocks if isinstance(c, dict)]
    if isinstance(cloud_blocks, dict):
        layers = []
        layer_keys = (
            "Opacity", "Coverage", "coverage", "ModulateColor", "Composition",
            "DiffMap", "BumpMap", "Height", "Velocity", "BumpHeight",
            "mainFreq", "mainOctaves", "stripeZones", "stripeTwist",
        )
        for value in cloud_blocks.values():
            if isinstance(value, dict) and any(k in value for k in layer_keys):
                layers.append(value)
        if layers:
            return layers
        return [cloud_blocks]
    return []


def _cloud_color_for_model(model: str, comp: dict, preset_info: dict) -> tuple[str, str]:
    frac = _normalized_comp(comp)
    model_lo = str(model or "").lower()
    key = preset_info.get("key", "")
    if "chlorine" in model_lo or frac.get("Cl2", 0.0) > 0.05:
        return "RGBA(0.750, 0.900, 0.420, 1.000)", "chlorine clouds"
    if "titan" in model_lo or "titan" in key or frac.get("CH4", 0.0) > 0.10:
        return "RGBA(0.780, 0.560, 0.280, 1.000)", "hydrocarbon clouds"
    if "venus" in model_lo or "thick" in model_lo or frac.get("SO2", 0.0) > 0.03:
        return "RGBA(0.930, 0.820, 0.560, 1.000)", "sulfur/thick clouds"
    if "mars" in model_lo or frac.get("CO2", 0.0) > 0.35:
        return "RGBA(0.820, 0.700, 0.560, 1.000)", "dust/CO2 clouds"
    if "jupiter" in model_lo or "neptune" in model_lo or frac.get("NH3", 0.0) > 0.02:
        return "RGBA(0.900, 0.820, 0.660, 1.000)", "ammonia/giant clouds"
    return "RGBA(1.000, 1.000, 1.000, 1.000)", "water/ice clouds"


def combine_cloud_coverage(layers: list) -> float:
    clear_fraction = 1.0
    for layer in layers:
        c = _clamp01(layer.get("Coverage", layer.get("coverage", 0.0)))
        clear_fraction *= (1.0 - c)
    return _clamp01(1.0 - clear_fraction)


def compute_us_cloud_opacity(total_coverage: float, atmosphere_opacity: float,
                             cloud_layers: list, explicit_opacity_values: list) -> float:
    if not cloud_layers:
        return 0.0
    if explicit_opacity_values:
        return _clamp01(sum(explicit_opacity_values) / 2.0)
    base = 0.35 + 0.75 * _clamp01(total_coverage)
    atm_boost = 0.75 + 0.5 * _clamp01(atmosphere_opacity)
    return _clamp01(base * atm_boost)


def choose_cloud_sets(atmo_model: str, total_coverage: float,
                      avg_velocity: float, preset_info: dict) -> tuple[str, str]:
    if total_coverage <= 0:
        return "None", "None"
    model = str(atmo_model or "").strip()
    if model in ("Venus", "Titan", "Thick"):
        return "Thick", "Turbulent"
    if model == "Mars":
        return "Sparse", "Streaks"
    if abs(avg_velocity) > 100 or total_coverage > 0.85:
        return "Storm", "Turbulent"
    if total_coverage > 0.55:
        return "Thick", "Turbulent"
    if total_coverage > 0.20:
        return "Fluffy", "Wispy"
    return "Thin", "Wispy"


def _cloud_speed_fields(avg_velocity: float, obj_name: str) -> dict:
    seed = _stable_u32(f"cloud-speed:{obj_name}:{avg_velocity:.3f}")
    sign_a = -1.0 if seed & 1 else 1.0
    sign_b = -sign_a if seed & 2 else sign_a
    speed = max(4.0, min(120.0, abs(avg_velocity) if abs(avg_velocity) > 0 else 20.0))
    return {
        "cloudSpeedAtEquatorA": round(sign_a * speed, 2),
        "cloudSpeedAtEquatorB": round(sign_b * speed * 0.72, 2),
        "bandRotationA": round(sign_a * min(3.0, speed / 45.0), 2),
        "bandRotationB": round(sign_b * min(3.0, speed / 55.0), 2),
        "poleRotationA": round(sign_a * min(2.0, speed / 80.0), 2),
        "poleRotationB": round(sign_b * min(2.0, speed / 95.0), 2),
    }


def compute_cloud_appearance(obj_name: str, raw: dict, atm_info: dict,
                             preset_info: dict, has_atm: bool,
                             use_clouds: bool, archetype: str,
                             atmosphere_opacity: float = 0.0,
                             life_info: dict | None = None) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    layers = _cloud_blocks(raw)
    no_clouds = se_bool(raw.get("NoClouds", "false"))
    no_atm = se_bool(raw.get("NoAtmosphere", "false"))
    if no_clouds or no_atm or not has_atm or not use_clouds or not layers:
        color = "RGBA(1.000, 1.000, 1.000, 1.000)"
        log_debug(
            f"[clouds] Body='{obj_name}' layers={len(layers)} se_opacity_sum=0.0 "
            f"us_opacity=0.0 coverage=0.0 setA='None' setB='None' color='{color}'",
            "CLOUDS",
        )
        return {
            "show": False,
            "cloud_set_a": US_CLOUD_STYLES["None"],
            "cloud_set_b": US_CLOUD_STYLES["None"],
            "custom_appearance": False,
            "coverage": 0.0,
            "opacity": 0.0,
            "color_rgba": color,
            "style_a": "None",
            "style_b": "None",
            "speed_fields": _cloud_speed_fields(0.0, obj_name),
        }

    coverages = []
    velocities = []
    explicit_opacity_values = []
    colors = []
    for layer in layers:
        if "Opacity" in layer:
            explicit_opacity_values.append(max(0.0, safe_float(layer.get("Opacity", 0.0))))
        elif "ModulateColor" in layer:
            explicit_opacity_values.append(max(0.0, parse_rgba(layer.get("ModulateColor"))[3]))
        else:
            pass
        if "Coverage" in layer:
            coverages.append(_clamp01(layer.get("Coverage", 0.0)))
        elif "coverage" in layer:
            coverages.append(_clamp01(layer.get("coverage", 0.0)))
        else:
            layer_has_cloud_texture = any(k in layer for k in ("DiffMap", "BumpMap", "ModulateColor", "Composition"))
            layer_has_explicit_opacity = "Opacity" in layer or "ModulateColor" in layer
            coverages.append(1.0 if layer_has_cloud_texture and not layer_has_explicit_opacity else 0.0)
        velocities.append(safe_float(layer.get("Velocity", 0.0)))
        if layer.get("ModulateColor"):
            colors.append(parse_rgba(layer["ModulateColor"]))

    clear_fraction = 1.0
    for c in coverages:
        clear_fraction *= (1.0 - c)
    coverage = _clamp01(1.0 - clear_fraction)
    if coverage <= 0.0 and explicit_opacity_values:
        coverage = _clamp01(sum(explicit_opacity_values) / max(1, len(explicit_opacity_values)))
    us_opacity = compute_us_cloud_opacity(coverage, atmosphere_opacity, layers, explicit_opacity_values)
    avg_velocity = sum(velocities) / len(velocities) if velocities else 0.0

    model = (atm_info or {}).get("model", "") or (atm_info or {}).get("Model", "")
    comp = (atm_info or {}).get("comp", {}) or {}
    if not comp and isinstance(raw.get("Atmosphere"), dict):
        comp = raw["Atmosphere"].get("Composition", {}) or {}
    model_lo = str(model or "").lower()
    is_gas = archetype in ("gas_giant", "ice_giant")
    if is_gas or model_lo in ("jupiter", "neptune"):
        style_a, style_b = "Turbulent", "Storm"
    else:
        style_a, style_b = choose_cloud_sets(model, coverage, avg_velocity, preset_info)

    if colors:
        r = sum(c[0] for c in colors) / len(colors)
        g = sum(c[1] for c in colors) / len(colors)
        b = sum(c[2] for c in colors) / len(colors)
        color = _rgba_string((r, g, b, 1.0))
        color_reason = "explicit layer color"
    else:
        color, color_reason = _cloud_color_for_model(model, comp, preset_info)
    life_tint, life_strength, life_reason = _life_cloud_tint(life_info or {})
    if life_tint:
        color = _blend_rgba_strings(color, life_tint, life_strength)
        color_reason += f" + {life_reason}"
        log_debug(
            f"[cloud-life] Body='{obj_name}' applied=True reason='{life_reason}'",
            "CLOUD_LIFE",
        )

    log_debug(
        f"[clouds] Body='{obj_name}' layers={len(layers)} coverages={[round(c, 3) for c in coverages]} "
        f"total_coverage={coverage:.3f} opacity={us_opacity:.2f} setA='{style_a}' setB='{style_b}' "
        f"color='{color}' reason='{color_reason}'",
        "CLOUDS",
    )
    speed_fields = _cloud_speed_fields(avg_velocity, obj_name)
    return {
        "show": bool(us_opacity > 0.0 and coverage > 0.0),
        "cloud_set_a": US_CLOUD_STYLES.get(style_a, US_CLOUD_STYLES["Fluffy"]),
        "cloud_set_b": US_CLOUD_STYLES.get(style_b, US_CLOUD_STYLES["Wispy"]),
        "custom_appearance": style_a != style_b,
        "coverage": round(coverage, 4),
        "opacity": round(us_opacity, 4),
        "color_rgba": color,
        "style_a": style_a,
        "style_b": style_b,
        "speed_fields": speed_fields,
    }


def _first_spectral_token(class_text: str) -> str:
    """Return the leading spectral type token from a SE class string."""
    cs = str(class_text or "").strip().upper()
    # Multi-char special tokens first
    for tok in ("WR", "WD", "DA", "DB", "DC", "DO", "DQ", "DZ", "DAB", "DAH", "NS"):
        if cs.startswith(tok):
            return tok
    # Single char tokens
    if cs:
        return cs[0]
    return ""


def classify_spaceengine_stellar_body(raw: dict) -> dict:
    """
    Classify a SE stellar body into kind/category/star_type using Class-first priority.
    Returns {"kind", "category", "star_type", "reason"}.
    """
    from constants import (
        SE_NEUTRON_STAR_CLASSES, SE_WHITE_DWARF_CLASSES,
        US_STAR_TYPE_MAIN_SEQUENCE, US_STAR_TYPE_NEUTRON,
        US_STAR_TYPE_WHITE_DWARF, US_STAR_TYPE_SPECIAL,
        US_CATEGORY_BLACK_HOLE, US_CATEGORY_STAR, US_CATEGORY_BROWN_DWARF,
    )
    cls    = str(raw.get("Class", "")).strip()
    cs     = cls.upper()
    cs_ns  = cs.replace(" ", "")
    token  = _first_spectral_token(cls)
    decl   = str(raw.get("_decl_type", raw.get("decl_type", raw.get("Type", "")))).strip().lower()
    mass_sol = safe_float(raw.get("MassSol", raw.get("mass_sol", 0.0)), 0.0)
    surface  = raw.get("Surface", {})
    preset   = str(surface.get("Preset", "")).lower() if isinstance(surface, dict) else ""

    # ── Wormhole ─────────────────────────────────────────────────────────────
    if token == "Z" or decl == "wormhole":
        return {"kind": "wormhole", "category": US_CATEGORY_BLACK_HOLE,
                "star_type": US_STAR_TYPE_SPECIAL,
                "reason": "Class Z wormhole — special fallback"}

    # ── Black hole ───────────────────────────────────────────────────────────
    is_black_hole = (
        token == "X"
        or decl in ("blackhole", "black hole") or "blackhole" in decl or "black_hole" in decl
    )
    if is_black_hole:
        return {"kind": "black_hole", "category": US_CATEGORY_BLACK_HOLE,
                "star_type": US_STAR_TYPE_SPECIAL,
                "reason": "Class X / black hole"}

    # ── Neutron star ─────────────────────────────────────────────────────────
    is_neutron = (
        token == "Q"
        or cs in SE_NEUTRON_STAR_CLASSES or cs_ns in SE_NEUTRON_STAR_CLASSES
        or decl in ("neutronstar", "neutron star", "pulsar") or "neutron" in decl or "pulsar" in decl
    )
    if is_neutron:
        return {"kind": "neutron_star", "category": US_CATEGORY_STAR,
                "star_type": US_STAR_TYPE_NEUTRON,
                "reason": "Class Q / neutron star"}

    # ── White dwarf ──────────────────────────────────────────────────────────
    is_wd = (
        token in ("WD", "DA", "DB", "DC", "DO", "DQ", "DZ", "DAB", "DAH")
        or cs in SE_WHITE_DWARF_CLASSES or cs_ns in SE_WHITE_DWARF_CLASSES
        or decl in ("whitedwarf", "white dwarf") or ("white" in decl and "dwarf" in decl)
    )
    if is_wd:
        return {"kind": "white_dwarf", "category": US_CATEGORY_STAR,
                "star_type": US_STAR_TYPE_WHITE_DWARF,
                "reason": "white dwarf class"}

    # ── Brown dwarf ──────────────────────────────────────────────────────────
    is_bd = (
        token in ("L", "T", "Y")
        or "browndwarf" in preset or "brown_dwarf" in preset or "brown dwarf" in cs
        or (mass_sol > 0.0 and mass_sol < 0.075 and decl in ("star", ""))
    )
    if is_bd:
        return {"kind": "brown_dwarf", "category": US_CATEGORY_BROWN_DWARF,
                "star_type": US_STAR_TYPE_SPECIAL,
                "reason": "L/T/Y brown dwarf or substellar mass"}

    # ── Planemo ──────────────────────────────────────────────────────────────
    if token == "P" or decl == "planemo":
        return {"kind": "planemo", "category": US_CATEGORY_BROWN_DWARF,
                "star_type": US_STAR_TYPE_SPECIAL,
                "reason": "Class P planemo"}

    # ── Normal fusion star ───────────────────────────────────────────────────
    return {"kind": "normal_star", "category": US_CATEGORY_STAR,
            "star_type": US_STAR_TYPE_MAIN_SEQUENCE,
            "reason": "normal stellar class"}


def classify_stellar_object(raw_class: str, teff: float, lum_watts: float,
                             radius_m: float, mass_kg: float) -> dict:
    """Legacy wrapper kept for any callers outside build_ubox_entity."""
    from constants import (
        SE_NEUTRON_STAR_CLASSES, SE_WHITE_DWARF_CLASSES,
        US_STAR_TYPE_MAIN_SEQUENCE, US_STAR_TYPE_NEUTRON,
        US_STAR_TYPE_WHITE_DWARF, US_STAR_TYPE_SPECIAL,
        US_CATEGORY_BLACK_HOLE, US_CATEGORY_STAR, US_CATEGORY_BROWN_DWARF,
    )
    proxy = {"Class": raw_class, "MassSol": mass_kg / _const.SOLAR_MASS_KG if mass_kg else 0.0}
    result = classify_spaceengine_stellar_body(proxy)
    # Map new fields back to old dict shape
    cs = str(raw_class or "").strip().upper()
    # Determine lum_class the old way for non-special types
    lum_class = "V"
    for pattern, lc in [(r'\bIAB\b',"Iab"),(r'\bIA\b',"Ia"),(r'\bIB\b',"Ib"),
                        (r'\bIII\b',"III"),(r'\bII\b',"II"),(r'\bIV\b',"IV"),
                        (r'\bVI\b',"VI"),(r'\bVII\b',"VII"),(r'\bI\b',"I")]:
        if re.search(pattern, cs): lum_class = lc; break
    lum_map = {"black_hole": "REM", "neutron_star": "NS", "white_dwarf": "VII", "brown_dwarf": "M"}
    result["lum_class"]   = lum_map.get(result["kind"], lum_class)
    result["us_category"] = result["kind"] if result["kind"] == "blackhole" else result.get("kind", "star")
    result["description"] = result["kind"].replace("_", " ").title()
    result["fallback"]    = False
    result["fallback_reason"] = ""
    return result


def get_star_color_from_se(obj_data: dict, teff: float) -> str | None:
    color_raw = obj_data.get("Color", "") if isinstance(obj_data, dict) else ""
    if not color_raw: return None
    nums = re.findall(r'[\d.]+', str(color_raw))
    if len(nums) >= 3:
        r, g, b = (min(1.0, safe_float(n)) for n in nums[:3])
        a = min(1.0, safe_float(nums[3])) if len(nums) >= 4 else 1.0
        if r > 0 or g > 0 or b > 0:
            return f"RGBA({r:.3f}, {g:.3f}, {b:.3f}, {a:.3f})"
    return None


# ─────────────────────────────────────────────────────────────────────────────
# DEPOT BUILDER
# ─────────────────────────────────────────────────────────────────────────────

def _calc_atmosphere_mass(radius_m, mass_kg, pressure_atm) -> float:
    return expected_atmosphere_mass_from_pressure(mass_kg, radius_m, pressure_atm)


def expected_atmosphere_mass_from_pressure(body_mass_kg, radius_m, pressure_atm):
    if pressure_atm <= 0 or body_mass_kg <= 0 or radius_m <= 0:
        return 0.0
    gravity = GRAVITATIONAL_CONSTANT * body_mass_kg / max(radius_m * radius_m, 1e-30)
    pressure_pa = pressure_atm * STD_ATM_PA
    return max(
        0.0,
        4.0 * math.pi * radius_m * radius_m * pressure_pa / max(gravity, 1e-30),
    )


def pressure_from_atmosphere_mass(body_mass_kg, radius_m, atmosphere_mass_kg):
    if atmosphere_mass_kg <= 0 or body_mass_kg <= 0 or radius_m <= 0:
        return 0.0
    gravity = GRAVITATIONAL_CONSTANT * body_mass_kg / max(radius_m * radius_m, 1e-30)
    pressure_pa = atmosphere_mass_kg * gravity / (
        4.0 * math.pi * max(radius_m * radius_m, 1e-30)
    )
    return max(0.0, pressure_pa / STD_ATM_PA)


def _is_giant_atmosphere_class(body_class: str, archetype: str = "") -> bool:
    text = f"{body_class or ''} {archetype or ''}".strip().lower()
    return any(token in text for token in (
        "neptune", "uranus", "icegiant", "ice giant", "jupiter",
        "gasgiant", "gas giant", "gas_giant", "ice_giant", "jovian",
        "hotjupiter", "subbrowndwarf",
    ))


def compute_atmosphere_mass_and_pressure(body: dict, atmosphere_block: dict | None,
                                         body_class: str = "") -> dict:
    """Return class-aware SE->US pressure and atmosphere mass."""
    body = body if isinstance(body, dict) else {}
    atmosphere_block = atmosphere_block if isinstance(atmosphere_block, dict) else {}
    raw = body.get("raw_data", {}) if isinstance(body.get("raw_data"), dict) else body
    flags = _source_flags(raw)
    name = body.get("name") or raw.get("Name") or "unknown"
    archetype = body.get("archetype", "")
    mass_kg = safe_float(body.get("mass_kg", 0.0))
    radius_m = safe_float(body.get("radius_m", 0.0))
    if flags["has_no_atmosphere"] or not atmosphere_block or mass_kg <= 0 or radius_m <= 0:
        return {
            "mode": "none",
            "pressure_atm": 0.0,
            "atmosphere_mass_kg": 0.0,
            "earth_atmospheres": 0.0,
            "gravity": 0.0,
            "hydro_atm": 0.0,
            "scaled_atm": 0.0,
        }

    se_pressure = max(0.0, safe_float(atmosphere_block.get("Pressure", atmosphere_block.get("pressure", 0.0))))
    density = max(0.0, safe_float(atmosphere_block.get("Density", atmosphere_block.get("density", 0.0))))
    height_km = max(0.0, safe_float(atmosphere_block.get("Height", atmosphere_block.get("height", 0.0))))
    g = GRAVITATIONAL_CONSTANT * mass_kg / max(radius_m ** 2, 1e-9)
    giant = _is_giant_atmosphere_class(body_class or raw.get("Class", ""), archetype)
    hydro_atm = 0.0
    scaled_atm = 0.0
    mode = "terrestrial"

    if giant:
        mode = "giant"
        if density > 0.0 and height_km > 0.0 and g > 0.0:
            hydro_pa = density * g * height_km * 1000.0
            hydro_atm = hydro_pa / 101325.0
            hydro_scale = 0.25
            scaled_atm = hydro_atm * hydro_scale
        pressure_atm = max(se_pressure, scaled_atm)
        atm_mass = _calc_atmosphere_mass(radius_m, mass_kg, pressure_atm)
        min_mass = mass_kg * 1e-4
        max_mass = mass_kg * 0.25
        if pressure_atm > 0.0:
            atm_mass = max(min_mass, min(max_mass, atm_mass))
    else:
        pressure_atm = se_pressure
        atm_mass = _calc_atmosphere_mass(radius_m, mass_kg, pressure_atm)
        log_debug(
            f"[atmo-density] Body='{name}' SE density={density:.6g} kg/m3 "
            f"pressure={se_pressure:.6g} atm height={height_km:.6g} km "
            "mode='pressure_source_of_truth'",
            "ATMO_DENSITY",
        )

    earth_atm = atm_mass / EARTH_ATMOSPHERE_MASS_KG if EARTH_ATMOSPHERE_MASS_KG > 0 else 0.0
    log_mode = "giant" if giant else "terrestrial_pressure_source_of_truth"
    log_debug(
        f"[atmosphere-mass] Body='{name}' class='{body_class or raw.get('Class', '')}' "
        f"atmosphere_model='{atmosphere_block.get('Model', '')}' mode='{log_mode}' "
        f"se_pressure={se_pressure:.6g} density={density:.6g} height_km={height_km:.6g} "
        f"g={g:.3g} hydro_atm={hydro_atm:.6g} scaled_atm={scaled_atm:.6g} "
        f"final_pressure_atm={pressure_atm:.6g} atm_mass={atm_mass:.6g} earth_atm={earth_atm:.6g}",
        "ATMOSPHERE_MASS",
    )
    return {
        "mode": mode,
        "pressure_atm": float(pressure_atm),
        "atmosphere_mass_kg": float(atm_mass),
        "earth_atmospheres": float(earth_atm),
        "gravity": float(g),
        "hydro_atm": float(hydro_atm),
        "scaled_atm": float(scaled_atm),
    }


def _calc_mean_mw(comp_dict) -> float:
    MW = {"N2":28,"O2":32,"CO2":44,"H2O":18,"CH4":16,"Ar":40,"He":4,"H2":2,"Ne":20,
          "SO2":64,"NH3":17,"CO":28,"C2H6":30,"C2H2":26,"H2S":34,"Kr":84,"Xe":131,
          "N2O":44,"C2H4":28,"C3H8":44,"Cl2":71,"O3":48,"HCl":36.5,"C8H18":114}
    tot_mw = tot_pct = 0.0
    for mol, pct in comp_dict.items():
        w = MW.get(mol)
        if w:
            p = safe_float(pct); tot_mw += w*p; tot_pct += p
    return (tot_mw / tot_pct) if tot_pct > 0 else 29.0


def _calc_dof(comp_dict) -> int:
    mono = {"He","Ne","Ar","Kr","Xe"}; di = {"N2","O2","H2","CO","HCl","Cl2"}
    tot = wm = wd = wp = 0.0
    for mol, pct in comp_dict.items():
        p = safe_float(pct); tot += p
        if mol in mono: wm += p
        elif mol in di: wd += p
        else: wp += p
    if tot == 0: return 5
    return 3 if (wm >= wd and wm >= wp) else (7 if wp >= wd else 5)


def _source_flags(obj: dict | None) -> dict:
    obj = obj or {}
    flags = dict(obj.get("_source_flags", {}) or {})
    ocean = obj.get("Ocean")
    atm = obj.get("Atmosphere")
    surf = obj.get("Surface")
    flags.setdefault("has_no_ocean", se_bool(obj.get("NoOcean", "false")))
    flags.setdefault("has_ocean_block", isinstance(ocean, dict))
    flags.setdefault("has_no_atmosphere", se_bool(obj.get("NoAtmosphere", "false")))
    flags.setdefault("has_atmosphere_block", isinstance(atm, dict))
    flags.setdefault("has_no_clouds", se_bool(obj.get("NoClouds", "false")))
    flags.setdefault("has_clouds_block", isinstance(obj.get("Clouds"), (dict, list)))
    flags.setdefault("has_no_lava", se_bool(obj.get("NoLava", "false")))
    flags.setdefault("raw_atmosphere_composition", dict(atm.get("Composition", {})) if isinstance(atm, dict) and isinstance(atm.get("Composition", {}), dict) else {})
    flags.setdefault("raw_ocean_composition", dict(ocean.get("Composition", {})) if isinstance(ocean, dict) and isinstance(ocean.get("Composition", {}), dict) else {})
    flags.setdefault("raw_ocean_depth", safe_float(ocean.get("Depth", 0.0)) if isinstance(ocean, dict) else 0.0)
    flags.setdefault("raw_surface_sea_level", safe_float(surf.get("seaLevel", 0.0)) if isinstance(surf, dict) else 0.0)
    return flags


def composition_percent_to_partial_pressures(composition_percent: dict,
                                             total_pressure_atm: float) -> dict:
    values = {str(gas): max(0.0, safe_float(percent))
              for gas, percent in (composition_percent or {}).items()}
    total_percent = sum(values.values())
    pressure = max(0.0, safe_float(total_pressure_atm))
    if total_percent <= 0.0 or pressure <= 0.0:
        return {}
    return {gas: pressure * percent / total_percent for gas, percent in values.items()}


def partial_pressures_to_composition_percent(partial_pressures_atm: dict) -> dict:
    values = {str(gas): max(0.0, safe_float(pressure))
              for gas, pressure in (partial_pressures_atm or {}).items()}
    total_pressure = sum(values.values())
    if total_pressure <= 0.0:
        return {}
    return {gas: pressure / total_pressure * 100.0 for gas, pressure in values.items()
            if pressure > 0.0}


def saturation_vapor_pressure_water_atm(temp_K: float) -> float:
    """Buck saturation pressure over water/ice, sufficient for converter use."""
    temp_c = max(-100.0, min(99.0, safe_float(temp_K, 288.0) - 273.15))
    if temp_c >= 0.0:
        e_kpa = 0.61121 * math.exp(
            (18.678 - temp_c / 234.5) * (temp_c / (257.14 + temp_c))
        )
    else:
        e_kpa = 0.61115 * math.exp(
            (23.036 - temp_c / 333.7) * (temp_c / (279.82 + temp_c))
        )
    return max(0.0, e_kpa / 101.325)


def cap_partial_pressure(partials: dict, gas: str, cap_atm: float) -> float:
    before = max(0.0, safe_float(partials.get(gas, 0.0)))
    after = min(before, max(0.0, safe_float(cap_atm)))
    partials[gas] = after
    return before - after


def backfill_removed_pressure(partials: dict, removed_pressure_atm: float,
                              total_pressure_atm: float,
                              mode: str = "stability") -> dict:
    remaining = max(0.0, safe_float(removed_pressure_atm))
    total_pressure = max(0.0, safe_float(total_pressure_atm))
    ar_target_fraction = 0.0093 if mode in ("habitability", "aggressive") else 0.005
    ar_target = total_pressure * ar_target_fraction
    ar_before = max(0.0, safe_float(partials.get("Ar", 0.0)))
    ar_add = min(remaining, max(0.0, ar_target - ar_before))
    if ar_add > 0.0:
        partials["Ar"] = ar_before + ar_add
        remaining -= ar_add
    if remaining > 0.0:
        partials["N2"] = max(0.0, safe_float(partials.get("N2", 0.0))) + remaining
    return {"Ar": ar_add, "N2": remaining}


def _atmosphere_normalizer_config(config: dict | None = None) -> dict:
    result = {
        "enabled": _const.NORMALIZE_SE_ATMOSPHERE,
        "mode": _const.NORMALIZE_SE_ATMOSPHERE_MODE,
        "preserve_total_pressure": _const.NORMALIZE_ATMOSPHERE_PRESERVE_TOTAL_PRESSURE,
        "normalize_h2o": _const.NORMALIZE_H2O_VAPOR_TO_SATURATION,
        "normalize_so2": _const.NORMALIZE_SO2_ON_WET_OXYGENATED_WORLDS,
        "normalize_co2": _const.NORMALIZE_CO2_FOR_HABITABLE_WORLDS,
        "only_solid": _const.ATMOSPHERE_NORMALIZER_ONLY_FOR_SOLID_WORLDS,
        "skip_venus_like": _const.ATMOSPHERE_NORMALIZER_SKIP_VENUS_LIKE,
        "skip_gas_giants": _const.ATMOSPHERE_NORMALIZER_SKIP_GAS_GIANTS,
        "body_name": "unknown",
    }
    try:
        import globals_compat as runtime
        result["enabled"] = getattr(runtime, "NORMALIZE_SE_ATMOSPHERE", result["enabled"])
        result["mode"] = getattr(runtime, "NORMALIZE_SE_ATMOSPHERE_MODE", result["mode"])
    except ImportError:
        pass
    if config:
        result.update(config)
    result["mode"] = str(result.get("mode", "stability")).strip().lower()
    if result["mode"] == "off":
        result["enabled"] = False
    return result


def classify_atmosphere_intent(body_class, surface_block, ocean_block,
                               atmosphere_block, life_info):
    """Classify the source atmosphere without assuming Terra means Earth-like."""
    surface = surface_block if isinstance(surface_block, dict) else {}
    ocean = ocean_block if isinstance(ocean_block, dict) else {}
    atmosphere = atmosphere_block if isinstance(atmosphere_block, dict) else {}
    if isinstance(life_info, dict) and "has_life" in life_info:
        life = life_info
    else:
        life = parse_life_block(life_info if life_info else None)

    class_text = str(body_class or "").strip().lower().replace("_", "")
    if any(token in class_text for token in (
        "jupiter", "neptune", "uranus", "gasgiant", "icegiant", "jovian", "browndwarf"
    )):
        return "gas_giant"

    pressure = max(0.0, safe_float(atmosphere.get("Pressure", 0.0)))
    composition = atmosphere.get("Composition", {})
    composition = composition if isinstance(composition, dict) else {}
    partials = composition_percent_to_partial_pressures(composition, pressure)
    composition_total = sum(max(0.0, safe_float(value)) for value in composition.values())
    co2_fraction = (
        max(0.0, safe_float(composition.get("CO2", 0.0))) / composition_total
        if composition_total > 0.0 else 0.0
    )
    p_co2 = safe_float(partials.get("CO2", 0.0))
    p_o2 = safe_float(partials.get("O2", 0.0))
    no_ocean = se_bool(surface.get("_NoOcean", surface.get("NoOcean", "false")))
    has_ocean = bool(ocean) and not no_ocean
    has_life = bool(life.get("has_life"))
    model = str(atmosphere.get("Model", "")).strip().lower()
    preset = str(surface.get("Preset", "")).strip().lower()

    if has_life and has_ocean and p_o2 > 0.01:
        return "habitable_life_world"
    if has_ocean and p_o2 > 0.01:
        return "wet_oxygenated_world"
    if co2_fraction > 0.5 and pressure < 2.0 and not has_life:
        return "mars_like_co2"
    if (
        (p_co2 > 1.0 and pressure > 5.0 and not has_life and not has_ocean)
        or (
            model in ("venus", "thick", "jupiter")
            and co2_fraction > 0.5
            and pressure > 10.0
            and not has_life
        )
        or ("venus" in preset and not has_life)
    ):
        return "venus_like_greenhouse"
    if co2_fraction > 0.25 and p_co2 > 0.1 and not has_life and not has_ocean:
        return "dry_co2_greenhouse"

    toxic_partial = sum(safe_float(partials.get(gas, 0.0)) for gas in (
        "SO2", "CO", "CH4", "NH3", "Cl2", "H2S"
    ))
    if not has_life and not has_ocean and (
        toxic_partial > 0.01 or any(token in class_text for token in ("toxic", "lava", "ferria"))
    ):
        return "toxic_exotic"
    return "unknown"


def normalize_se_atmosphere(atmosphere_block, life_info, surface_block, ocean_block,
                            body_class, surface_temperature_K, config=None):
    """Return a normalized copy of an SE atmosphere and a detailed report."""
    atmosphere = copy.deepcopy(atmosphere_block) if isinstance(atmosphere_block, dict) else {}
    if isinstance(life_info, dict) and not life_info:
        life = parse_life_block(None)
    elif isinstance(life_info, dict) and "has_life" in life_info:
        life = life_info
    else:
        life = parse_life_block(life_info)
    surface = surface_block if isinstance(surface_block, dict) else {}
    ocean = ocean_block if isinstance(ocean_block, dict) else {}
    cfg = _atmosphere_normalizer_config(config)
    name = str(cfg.get("body_name") or "unknown")
    mode = cfg["mode"]
    pressure = max(0.0, safe_float(atmosphere.get("Pressure", 0.0)))
    composition = atmosphere.get("Composition", {})
    composition = composition if isinstance(composition, dict) else {}
    partials = composition_percent_to_partial_pressures(composition, pressure)
    raw_partials = dict(partials)
    report = {
        "applied": False, "changed": False, "mode": mode, "body": name,
        "raw_pressure_atm": pressure, "final_pressure_atm": pressure,
        "raw_partials_atm": raw_partials, "final_partials_atm": dict(partials),
        "removed_atm": {}, "backfill_atm": {}, "caps_atm": {}, "warnings": [],
        "surface_temperature_K": safe_float(surface_temperature_K, 288.0),
    }

    if not cfg["enabled"] or not atmosphere or pressure <= 0.0 or not composition:
        report["skip_reason"] = "disabled or no usable atmosphere"
        return atmosphere, report

    class_text = str(body_class or "").strip().lower().replace("_", "")
    preset_text = str(surface.get("Preset", "")).strip().lower()
    classification_surface = dict(surface)
    classification_surface["_NoOcean"] = cfg.get("no_ocean", False)
    intent = classify_atmosphere_intent(
        body_class, classification_surface, ocean, atmosphere, life
    )
    report["intent"] = intent
    if cfg["skip_gas_giants"] and intent == "gas_giant":
        report["skip_reason"] = "gas/ice giant"
        return atmosphere, report
    if cfg["only_solid"] and any(token in class_text for token in ("star", "barycenter")):
        report["skip_reason"] = "not a solid world"
        return atmosphere, report

    has_ocean = bool(ocean) and not bool(cfg.get("no_ocean", False))
    wet_world = bool(
        has_ocean
        or "wet" in preset_text
        or str(surface.get("DiffMapAlpha", "")).strip().lower() == "water"
    )
    oxygenated = safe_float(partials.get("O2", 0.0)) >= 0.01
    life_bearing = bool(life.get("has_life"))
    temp_k = safe_float(surface_temperature_K, 288.0)
    cleanup_intent = intent in ("habitable_life_world", "wet_oxygenated_world")
    forced_habitability = bool(cfg.get("habitable_candidate") or cfg.get("force_habitability"))
    report.update({"applied": True, "wet": wet_world, "has_ocean": has_ocean,
                   "oxygenated": oxygenated, "life_bearing": life_bearing,
                   "cleanup_intent": cleanup_intent})

    raw_comp_text = " ".join(
        f"{gas}={safe_float(composition.get(gas, 0.0)):.6g}%"
        for gas in ("N2", "H2O", "O2", "CO2", "SO2", "Ar") if gas in composition
    )
    log_debug(f"[atmo-raw] Body='{name}' P={pressure:.6g}atm {raw_comp_text}", "ATMO_RAW")
    log_debug(
        f"[atmo-normalize] Body='{name}' mode='{mode}' classified='{intent}' wet={wet_world} "
        f"oxygenated={oxygenated} life={life_bearing} temp={temp_k:.2f}K",
        "ATMO_NORMALIZE",
    )

    removed_total = 0.0
    aggressive_cleanup = mode == "aggressive"
    habitability_cleanup = mode == "habitability" and (
        cleanup_intent or forced_habitability or life_bearing or (wet_world and oxygenated)
    )
    stability_cleanup = mode == "stability" and (
        cleanup_intent or life_bearing or (wet_world and oxygenated)
    )
    should_cap_h2o = aggressive_cleanup or habitability_cleanup or stability_cleanup
    if cfg["normalize_h2o"] and "H2O" in partials and should_cap_h2o:
        rh_max = 0.8 if mode in ("habitability", "aggressive") else 1.0
        saturation = saturation_vapor_pressure_water_atm(temp_k)
        h2o_cap = saturation * rh_max
        report["caps_atm"]["H2O"] = h2o_cap
        before = safe_float(partials.get("H2O", 0.0))
        removed = cap_partial_pressure(partials, "H2O", h2o_cap)
        report["h2o_saturation_atm"] = saturation
        report["h2o_cap_atm"] = h2o_cap
        if removed > 0.0:
            report["removed_atm"]["H2O"] = removed
            removed_total += removed
            warning = (f"raw H2O={before:.6g}atm exceeds saturation cap={h2o_cap:.6g}atm; "
                       f"condensed {removed:.6g}atm")
            report["warnings"].append(warning)
            log_debug(
                f"[atmo-normalize] Body='{name}' H2O raw={before:.6g}atm cap={h2o_cap:.6g}atm "
                f"condensed={removed:.6g}atm action='condense/backfill'",
                "ATMO_NORMALIZE_WARN",
            )
    elif "H2O" in partials:
        before = safe_float(partials.get("H2O", 0.0))
        saturation = saturation_vapor_pressure_water_atm(temp_k)
        if before > saturation:
            warning = (
                f"classified='{intent}'; H2O={before:.6g}atm high steam content preserved "
                f"in {mode} mode"
            )
            report["warnings"].append(warning)
            log_debug(f"[validate-atmo] Body='{name}' {warning}", "ATMO_PRESERVE_WARN")

    should_cap_so2 = aggressive_cleanup or (
        (stability_cleanup or habitability_cleanup) and wet_world and oxygenated
    )
    if cfg["normalize_so2"] and "SO2" in partials and should_cap_so2:
        so2_ppm_cap = 0.1 if mode == "aggressive" else (1.0 if mode == "habitability" else 20.0)
        so2_cap = pressure * so2_ppm_cap * 1e-6
        report["caps_atm"]["SO2"] = so2_cap
        before = safe_float(partials.get("SO2", 0.0))
        before_ppm = before / pressure * 1e6 if pressure > 0.0 else 0.0
        removed = cap_partial_pressure(partials, "SO2", so2_cap)
        report["so2_cap_ppm"] = so2_ppm_cap
        if removed > 0.0:
            report["removed_atm"]["SO2"] = removed
            removed_total += removed
            warning = (f"raw SO2={before_ppm:.6g}ppm is unstable on wet oxygenated world; "
                       f"normalized to {so2_ppm_cap:.6g}ppm")
            report["warnings"].append(warning)
            log_debug(
                f"[atmo-normalize] Body='{name}' SO2 raw={before_ppm:.6g}ppm "
                f"cap={so2_ppm_cap:.6g}ppm removed={removed / pressure * 1e6:.6g}ppm "
                f"action='convert_to_ocean_acidity/backfill'",
                "ATMO_NORMALIZE_WARN",
            )
    elif "SO2" in partials:
        before = safe_float(partials.get("SO2", 0.0))
        warning_threshold = pressure * 20.0e-6
        if before > warning_threshold:
            warning = (
                f"classified='{intent}'; SO2={before:.6g}atm toxic content preserved "
                f"because no wet oxygenated cleanup applies"
            )
            report["warnings"].append(warning)
            log_debug(f"[validate-atmo] Body='{name}' {warning}", "ATMO_PRESERVE_WARN")

    if cfg["normalize_co2"] and "CO2" in partials:
        co2_cap = None
        co2_eligible = cleanup_intent or life_bearing or forced_habitability
        if mode == "stability" and co2_eligible:
            co2_cap = 0.10
        elif mode == "habitability" and (co2_eligible or (wet_world and oxygenated)):
            co2_cap = 0.01
        elif mode == "aggressive":
            co2_cap = 0.003
        before = safe_float(partials.get("CO2", 0.0))
        if co2_eligible and before > 0.01:
            warning = f"classified='{intent}'; CO2 partial pressure is high at {before:.6g}atm"
            report["warnings"].append(warning)
            log_debug(
                f"[validate-atmo] Body='{name}' {warning}",
                "ATMO_NORMALIZE_WARN",
            )
        elif co2_cap is None and before > 0.01:
            warning = (
                f"classified='{intent}'; CO2={before:.6g}atm greenhouse content preserved "
                f"in {mode} mode; normalize only in aggressive mode"
            )
            report["warnings"].append(warning)
            log_debug(f"[validate-atmo] Body='{name}' {warning}", "ATMO_PRESERVE_WARN")
        if co2_cap is not None and before > co2_cap:
            report["caps_atm"]["CO2"] = co2_cap
            removed = cap_partial_pressure(partials, "CO2", co2_cap)
            report["removed_atm"]["CO2"] = removed
            report["co2_cap_atm"] = co2_cap
            removed_total += removed
            log_debug(
                f"[atmo-normalize] Body='{name}' CO2 raw={before:.6g}atm cap={co2_cap:.6g}atm "
                f"removed={removed:.6g}atm mode='{mode}'",
                "ATMO_NORMALIZE_WARN",
            )

    if (aggressive_cleanup or habitability_cleanup) and life_bearing and "O2" in partials:
        report["caps_atm"]["O2"] = 0.35
        before = safe_float(partials.get("O2", 0.0))
        removed = cap_partial_pressure(partials, "O2", 0.35)
        if removed > 0.0:
            report["removed_atm"]["O2"] = removed
            removed_total += removed
            report["warnings"].append(f"O2 partial pressure capped from {before:.6g}atm to 0.35atm")

    if cfg["preserve_total_pressure"]:
        if removed_total > 0.0:
            report["backfill_atm"] = backfill_removed_pressure(partials, removed_total, pressure, mode)
        final_pressure = pressure
    else:
        final_pressure = sum(partials.values())

    final_composition = partial_pressures_to_composition_percent(partials)
    atmosphere["Pressure"] = final_pressure
    atmosphere["Composition"] = final_composition
    report["changed"] = any(value > 0.0 for value in report["removed_atm"].values())
    report["final_pressure_atm"] = final_pressure
    report["final_partials_atm"] = dict(partials)
    report["normalized_composition_percent"] = dict(final_composition)
    if report["backfill_atm"]:
        log_debug(
            f"[atmo-normalize] Body='{name}' backfill N2={report['backfill_atm'].get('N2', 0.0):.6g}atm "
            f"Ar={report['backfill_atm'].get('Ar', 0.0):.6g}atm "
            f"preserve_pressure={cfg['preserve_total_pressure']} final_P={final_pressure:.6g}atm",
            "ATMO_NORMALIZE",
        )
    final_text = " ".join(
        f"{gas}={safe_float(final_composition.get(gas, 0.0)):.6g}%"
        for gas in ("N2", "H2O", "O2", "CO2", "SO2", "Ar") if gas in final_composition
    )
    log_debug(f"[atmo-final] Body='{name}' P={final_pressure:.6g}atm {final_text}", "ATMO_FINAL")
    return atmosphere, report


def is_lacustrine_world(surface: dict, ocean_block: dict, no_ocean=False) -> bool:
    if no_ocean or not isinstance(ocean_block, dict) or not ocean_block:
        return False
    surface = surface if isinstance(surface, dict) else {}
    sea_level = safe_float(surface.get("seaLevel", 0.0))
    depth_km = safe_float(ocean_block.get("Depth", 0.0))
    return bool(depth_km > 0.0 and depth_km < 1.0 and sea_level <= 0.05)


def compute_lacustrine_depot_scale(source_depth_km: float,
                                   target_water_fraction: float) -> float:
    del source_depth_km, target_water_fraction
    return max(1e-5, min(2e-3, LACUSTRINE_DEPOT_MASS_SCALE))


def distribute_ocean_depot_mass(ocean_composition: dict,
                                total_us_depot_mass: float) -> dict:
    composition = ocean_composition if isinstance(ocean_composition, dict) else {}
    if not composition:
        return {"Water": max(0.0, total_us_depot_mass)} if total_us_depot_mass > 0.0 else {}
    supported = {
        molecule: max(0.0, safe_float(value))
        for molecule, value in composition.items()
        if molecule in SE_OCEAN_TO_US_DEPOT and safe_float(value) > 0.0
    }
    total_pct = sum(supported.values())
    if total_pct <= 0.0:
        return {}
    distributed = {}
    for molecule, percent in supported.items():
        depot = SE_OCEAN_TO_US_DEPOT.get(molecule)
        if depot and percent > 0.0:
            _merge_mass(distributed, depot, total_us_depot_mass * percent / total_pct)
    return distributed


def _ocean_depot_export_mode_runtime() -> str:
    try:
        import globals_compat as runtime
    except ImportError:
        runtime = _const
    mode = str(getattr(
        runtime, "OCEAN_DEPOT_EXPORT_MODE", _const.OCEAN_DEPOT_EXPORT_MODE
    )).strip().lower().replace(" ", "_")
    aliases = {
        "visual": "visual_only",
        "visual_only": "visual_only",
        "capped": "capped",
        "legacy": "legacy",
        "legacy_full_mass": "legacy",
        "full_active_depot": "legacy",
    }
    mode = aliases.get(mode, "visual_only")
    # Preserve compatibility with older callers that only set this flag.
    if bool(getattr(runtime, "EXPORT_FULL_OCEAN_MASS_AS_DEPOT", False)):
        mode = "legacy"
    return mode


def active_ocean_depot_names(raw: dict) -> set:
    """Return ocean chemicals that are actually exported as active depots."""
    raw = raw if isinstance(raw, dict) else {}
    if se_bool(raw.get("NoOcean", "false")):
        return set()
    if _ocean_depot_export_mode_runtime() == "visual_only":
        return set()
    ocean = raw.get("Ocean", {}) if isinstance(raw.get("Ocean"), dict) else {}
    composition = ocean.get("Composition", {}) if isinstance(ocean.get("Composition"), dict) else {}
    return {
        SE_OCEAN_TO_US_DEPOT[molecule]
        for molecule, percent in composition.items()
        if safe_float(percent) > 0.0 and molecule in SE_OCEAN_TO_US_DEPOT
    }


def _empty_depots() -> dict:
    return {key: {"Mass": 0.0, "LockSurfaceTracking": False} for key in US_ATM_DEPOT_KEYS}


def _merge_mass(acc: dict, name: str, mass: float) -> None:
    if name and mass > 0:
        acc[name] = acc.get(name, 0.0) + float(mass)


def _is_h2_he_dominated(comp: dict) -> bool:
    total = sum(max(0.0, safe_float(v)) for v in comp.values())
    if total <= 0:
        return False
    return (safe_float(comp.get("H2", 0.0)) + safe_float(comp.get("He", 0.0))) / total >= 0.55


def _is_ocean_class(raw_class: str, archetype: str) -> bool:
    cls = str(raw_class or "").strip().lower()
    return archetype == "ocean" or cls in ("aquaria", "ocean", "marine", "panthalassic")


def _surface_water_fraction(raw: dict, ocean_depth_km: float = 0.0) -> tuple[float, str]:
    raw = raw if isinstance(raw, dict) else {}
    surf = raw.get("Surface", {}) if isinstance(raw.get("Surface"), dict) else {}
    sea_level = max(0.0, safe_float(surf.get("seaLevel", 0.0)))
    if ocean_depth_km > 0.0 and ocean_depth_km < 1.0 and sea_level <= 0.05:
        frac = max(0.005, min(0.12, sea_level * 4.0))
        return frac, "lacustrine"
    if sea_level > 0.0:
        return max(0.005, min(0.95, sea_level / 2.0)), "ocean"
    return 1.0, "global"


def build_bulk_composition(obj: dict) -> dict:
    mass_kg = safe_float(obj.get("mass_kg", 0.0))
    archetype = obj.get("archetype", "rocky")
    raw = obj.get("raw_data", {}) or {}
    flags = _source_flags(raw)
    raw_class = raw.get("Class", "")
    atm_comp = flags.get("raw_atmosphere_composition", {}) or {}
    has_real_ocean = flags["has_ocean_block"] and not flags["has_no_ocean"]

    if archetype == "star":
        return {"Hydrogen": mass_kg * 0.74, "Helium": mass_kg * 0.26}

    if archetype == "gas_giant" and _is_h2_he_dominated(atm_comp):
        h2 = max(0.0, safe_float(atm_comp.get("H2", 0.0)))
        he = max(0.0, safe_float(atm_comp.get("He", 0.0)))
        total = h2 + he or 1.0
        envelope = mass_kg * 0.985
        return {
            "Hydrogen": envelope * h2 / total,
            "Helium": envelope * he / total,
            "Iron": mass_kg * 0.005,
            "Silicate": mass_kg * 0.010,
        }

    if archetype == "gas_giant":
        return {"Hydrogen": mass_kg * 0.745, "Helium": mass_kg * 0.235, "Iron": mass_kg * 0.01, "Silicate": mass_kg * 0.01}

    if archetype == "ice_giant":
        total = sum(max(0.0, safe_float(v)) for v in atm_comp.values())
        h2 = max(0.0, safe_float(atm_comp.get("H2", 0.0)))
        he = max(0.0, safe_float(atm_comp.get("He", 0.0)))
        h2he = h2 + he
        waterish = sum(max(0.0, safe_float(atm_comp.get(k, 0.0))) for k in ("H2O", "CO2", "CO", "CH4", "NH3", "SO2"))
        if total > 0.0 and h2he / total >= 0.35:
            envelope = mass_kg * 0.24
            hhe_total = h2he or 1.0
            return {
                "Hydrogen": envelope * h2 / hhe_total,
                "Helium": envelope * he / hhe_total,
                "Water": mass_kg * 0.30,
                "Silicate": mass_kg * 0.34,
                "Iron": mass_kg * 0.12,
            }
        if total > 0.0 and waterish / total >= 0.35:
            return {
                "Water": mass_kg * 0.48,
                "Carbon Dioxide": mass_kg * 0.08 * (safe_float(atm_comp.get("CO2", 0.0)) / max(waterish, 1e-9)),
                "Methane": mass_kg * 0.04 * (safe_float(atm_comp.get("CH4", 0.0)) / max(waterish, 1e-9)),
                "Ammonia": mass_kg * 0.03 * (safe_float(atm_comp.get("NH3", 0.0)) / max(waterish, 1e-9)),
                "Hydrogen": mass_kg * 0.08,
                "Helium": mass_kg * 0.03,
                "Silicate": mass_kg * 0.23,
                "Iron": mass_kg * 0.10,
            }
        nitrogenish = sum(max(0.0, safe_float(atm_comp.get(k, 0.0))) for k in ("N2", "CH4", "NH3"))
        if total > 0.0 and nitrogenish / total >= 0.35:
            # N2/CH4/NH3-dominated = Titan-like solid/icy body.
            # Atmosphere percentages must NOT scale to bulk planet mass (would give ~7e26 kg N).
            # Use conservative solid/icy fallback.
            return {
                "Iron":     mass_kg * 0.215,
                "Silicate": mass_kg * 0.785,
            }
        return {"Water": mass_kg * 0.34, "Hydrogen": mass_kg * 0.10, "Helium": mass_kg * 0.04, "Silicate": mass_kg * 0.36, "Iron": mass_kg * 0.16}

    if has_real_ocean or _is_ocean_class(raw_class, archetype):
        # Ocean/Aquaria worlds carry substantial bulk internal water.
        bulk_non_atm = mass_kg  # atmosphere is tiny compared to bulk
        return {
            "Water":    bulk_non_atm * 0.454,
            "Silicate": bulk_non_atm * 0.473,
            "Iron":     bulk_non_atm * 0.073,
        }

    if archetype == "ice" or "ice" in str(raw_class).lower():
        water_frac = 0.30 if not flags["has_no_ocean"] else 0.18
        return {"Iron": mass_kg * 0.15, "Silicate": mass_kg * (0.85 - water_frac), "Water": mass_kg * water_frac}

    if archetype == "lava":
        return {"Iron": mass_kg * 0.45, "Silicate": mass_kg * 0.55}

    return {"Iron": mass_kg * 0.32, "Silicate": mass_kg * 0.68}


def choose_static_atmosphere_carrier(est_temp_k: float = 300.0,
                                     pressure_atm: float = 1.0,
                                     body_class: str = "") -> str:
    """Return an inert carrier gas name for static imported atmosphere depots."""
    try:
        import globals_compat as _rt
        cold    = getattr(_rt, "STATIC_ATMOSPHERE_CARRIER_COLD",    _const.STATIC_ATMOSPHERE_CARRIER_COLD)
        default = getattr(_rt, "STATIC_ATMOSPHERE_CARRIER_DEFAULT", _const.STATIC_ATMOSPHERE_CARRIER_DEFAULT)
    except ImportError:
        cold    = _const.STATIC_ATMOSPHERE_CARRIER_COLD
        default = _const.STATIC_ATMOSPHERE_CARRIER_DEFAULT
    return cold if est_temp_k < 90 else default


_REACTIVE_VOLATILE_DEPOTS = frozenset({
    "Water", "Methane", "Ammonia", "Carbon Dioxide", "Sulfur Dioxide",
    "Hydrogen", "Helium", "Oxygen", "Nitrogen", "Argon",
})


def build_atmosphere_depots(obj: dict, req_atm_mass: float) -> dict:
    raw   = obj.get("raw_data", {}) or {}
    flags = _source_flags(raw)
    if flags["has_no_atmosphere"] or req_atm_mass <= 0.0:
        return {}

    try:
        import globals_compat as _rt
        static     = bool(getattr(_rt, "STATIC_IMPORTED_ATMOSPHERES",  _const.STATIC_IMPORTED_ATMOSPHERES))
        depot_mode = str(getattr(_rt, "STATIC_ATMOSPHERE_DEPOT_MODE",  _const.STATIC_ATMOSPHERE_DEPOT_MODE))
    except ImportError:
        static     = bool(getattr(_const, "STATIC_IMPORTED_ATMOSPHERES", True))
        depot_mode = str(getattr(_const, "STATIC_ATMOSPHERE_DEPOT_MODE", "none"))

    archetype = obj.get("archetype", "rocky")
    is_solid  = archetype not in ("gas_giant", "ice_giant", "star")

    # Stars must not get atmosphere depots; their Hydrogen/Helium comes from build_bulk_composition.
    if archetype == "star":
        return {}

    if static and is_solid:
        if depot_mode == "none":
            log_debug(
                f"[static-atmo-none] Body='{obj.get('name','unknown')}' "
                f"depot_mode='none' all atmospheric depots zeroed",
                "ATMO_STATIC",
            )
            return {}

        if depot_mode in ("carrier_unlocked", "carrier_locked"):
            raw_atm  = raw.get("Atmosphere", {}) if isinstance(raw.get("Atmosphere"), dict) else {}
            est_temp = safe_float(raw.get("Temp", raw.get("EstimatedTemp", 300.0)))
            pressure = safe_float(raw_atm.get("Pressure",
                       (obj.get("atm_info") or {}).get("pressure", 1.0)))
            carrier  = choose_static_atmosphere_carrier(est_temp, pressure, str(raw.get("Class", "")))
            log_debug(
                f"[static-atmo-carrier] Body='{obj.get('name','unknown')}' "
                f"carrier='{carrier}' mode='{depot_mode}' "
                f"mass={req_atm_mass:.6g}kg real_composition_metadata_only",
                "ATMO_STATIC",
            )
            return {carrier: req_atm_mass}

        # chemical_unlocked / chemical_locked — fall through to chemical distribution

    atmo    = raw.get("Atmosphere", {}) if isinstance(raw.get("Atmosphere"), dict) else {}
    report  = raw.get("_atmosphere_normalization_report", {})
    partials = report.get("final_partials_atm", {}) if isinstance(report, dict) else {}
    if not partials:
        comp     = flags.get("raw_atmosphere_composition", {}) or (obj.get("atm_info") or {}).get("comp", {}) or {}
        pressure = safe_float(atmo.get("Pressure", (obj.get("atm_info") or {}).get("pressure", 0.0)))
        partials = composition_percent_to_partial_pressures(comp, pressure)
    return distribute_atmosphere_mass_from_partials(partials, req_atm_mass)


ATMOSPHERE_MOLECULAR_WEIGHTS = {
    "N2": 28.0134, "O2": 31.998, "CO2": 44.0095, "H2O": 18.015,
    "SO2": 64.066, "CO": 28.010, "Ar": 39.948, "He": 4.0026,
    "H2": 2.0159, "CH4": 16.043, "NH3": 17.031,
}


def distribute_atmosphere_mass_from_partials(partials_atm, expected_atm_mass_kg):
    """Distribute atmosphere mass by mole fraction and molecular weight."""
    partials = partials_atm if isinstance(partials_atm, dict) else {}
    weighted = []
    for molecule, molecular_weight in ATMOSPHERE_MOLECULAR_WEIGHTS.items():
        partial = max(0.0, safe_float(partials.get(molecule, 0.0)))
        if partial <= 0.0:
            continue
        depot = SE_TO_US_DEPOT.get(molecule)
        if molecule == "CO":
            depot = "Carbon Dioxide"
        if depot:
            weighted.append((molecule, depot, partial * molecular_weight))
    weight_total = sum(weight for _molecule, _depot, weight in weighted)
    if weight_total <= 0.0:
        return {}
    result = {}
    for molecule, depot, weight in weighted:
        _merge_mass(result, depot, expected_atm_mass_kg * weight / weight_total)
        if molecule == "CO":
            log_debug(
                "[atmo-depot-warning] Carbon Monoxide approximated as Carbon Dioxide depot",
                "ATMO_DEPOT_WARN",
            )
    return result


def enforce_atmosphere_depot_consistency(entity, final_atmo, expected_atm_mass_kg,
                                         vapor_policy, report):
    """Make solid-world gas depots agree with pressure-derived atmosphere mass."""
    final_atmo = final_atmo if isinstance(final_atmo, dict) else {}
    vapor_policy = vapor_policy if isinstance(vapor_policy, dict) else {}
    report = report if isinstance(report, dict) else {}
    components = {component.get("$type"): component for component in entity.get("Components", [])}
    composition = components.get("CompositionComponent", {})
    depots = composition.get("depots", {}) if isinstance(composition, dict) else {}
    celestial = components.get("Celestial", {})
    if not depots or not celestial:
        return report

    try:
        import globals_compat as _runtime
        static     = bool(getattr(_runtime, "STATIC_IMPORTED_ATMOSPHERES",   _const.STATIC_IMPORTED_ATMOSPHERES))
        depot_mode = str(getattr(_runtime, "STATIC_ATMOSPHERE_DEPOT_MODE",   _const.STATIC_ATMOSPHERE_DEPOT_MODE))
        lock_atm_depots = bool(getattr(_runtime, "LOCK_IMPORTED_ATMOSPHERIC_DEPOTS",
                                       _const.LOCK_IMPORTED_ATMOSPHERIC_DEPOTS))
    except ImportError:
        static          = bool(getattr(_const, "STATIC_IMPORTED_ATMOSPHERES", True))
        depot_mode      = str(getattr(_const, "STATIC_ATMOSPHERE_DEPOT_MODE", "none"))
        lock_atm_depots = bool(getattr(_const, "LOCK_IMPORTED_ATMOSPHERIC_DEPOTS", True))

    # Detect solid body from entity components (no GasGiantComponent / StarComponent)
    is_solid = not any(
        c.get("$type") in ("GasGiantComponent", "StarComponent")
        for c in entity.get("Components", [])
    )

    if static and is_solid and expected_atm_mass_kg > 0.0 and depot_mode in (
        "none", "carrier_unlocked", "carrier_locked"
    ):
        old_total = sum(max(0.0, safe_float(item.get("Mass", 0.0))) for item in depots.values())

        if depot_mode == "none":
            # Zero every reactive volatile depot; balance mass into Silicate.
            removed = 0.0
            for name in _REACTIVE_VOLATILE_DEPOTS:
                d = depots.get(name)
                if isinstance(d, dict):
                    removed += max(0.0, safe_float(d.get("Mass", 0.0)))
                    d["Mass"] = 0.0
            if removed > 0.0:
                depots.setdefault("Silicate", {"Mass": 0.0, "LockSurfaceTracking": False})
                depots["Silicate"]["Mass"] = max(
                    0.0, safe_float(depots["Silicate"].get("Mass", 0.0)) + removed
                )
            celestial["AtmosphereMass"] = float(expected_atm_mass_kg)
            atm_names   = set()
            carrier_name = None
            depot_sum   = 0.0
            should_lock = False

        else:  # carrier_unlocked / carrier_locked
            should_lock  = depot_mode == "carrier_locked"
            existing_atm = {n for n in _REACTIVE_VOLATILE_DEPOTS
                            if max(0.0, safe_float(depots.get(n, {}).get("Mass", 0.0))) > 0.0}
            carrier_name = next(iter(existing_atm), None)
            if carrier_name is None:
                est_temp     = safe_float(final_atmo.get("Temp", final_atmo.get("EstimatedTemp", 300.0)))
                carrier_name = choose_static_atmosphere_carrier(est_temp, 1.0, "")
            # Zero all non-carrier reactive depots
            removed = 0.0
            for name in _REACTIVE_VOLATILE_DEPOTS:
                if name == carrier_name:
                    continue
                d = depots.get(name)
                if isinstance(d, dict):
                    removed += max(0.0, safe_float(d.get("Mass", 0.0)))
                    d["Mass"] = 0.0
            if removed > 0.0:
                depots.setdefault("Silicate", {"Mass": 0.0, "LockSurfaceTracking": False})
                depots["Silicate"]["Mass"] = max(
                    0.0, safe_float(depots["Silicate"].get("Mass", 0.0)) + removed
                )
            # Set carrier to exact expected mass
            depots.setdefault(carrier_name, {"Mass": 0.0, "LockSurfaceTracking": False})
            depots[carrier_name]["Mass"] = float(expected_atm_mass_kg)
            depots[carrier_name]["LockSurfaceTracking"] = should_lock
            celestial["AtmosphereMass"] = float(expected_atm_mass_kg)
            atm_names  = {carrier_name}
            depot_sum  = float(expected_atm_mass_kg)

        log_debug(
            f"[atmo-consistency] Body='{entity.get('Name','unknown')}' depot_mode='{depot_mode}' "
            f"carrier={carrier_name!r} lock={should_lock} "
            f"mass={expected_atm_mass_kg:.6g}kg celestial_set=True reactive_zeroed=True",
            "ATMO_STATIC",
        )
        report["atmospheric_depot_names"]   = sorted(atm_names)
        report["atmospheric_depot_masses_kg"] = {n: float(expected_atm_mass_kg) for n in atm_names}
        report["atmospheric_depot_mass_kg"] = depot_sum if depot_mode != "none" else 0.0
        report["water_vapor_included"]       = False
        report["surface_depot_names"]        = []
        report["gas_depots_locked"]          = should_lock
        report["atmospheric_depots_locked"]  = should_lock
        report["static_depot_mode"]          = depot_mode
        report["carrier_name"]               = carrier_name
        report["carrier_mass_kg"]            = depot_sum if depot_mode != "none" else 0.0
        return report

    # chemical_unlocked / chemical_locked / non-solid / gas-giant path
    partials = report.get("final_partials_atm", {})
    if not partials:
        partials = composition_percent_to_partial_pressures(
            final_atmo.get("Composition", {}), final_atmo.get("Pressure", 0.0)
        )
    partials = dict(partials)
    include_water_vapor = bool(vapor_policy.get("include_water_vapor", True))
    excluded_depots = set(vapor_policy.get("surface_depot_names", ()))
    if not include_water_vapor or "Water" in excluded_depots:
        partials.pop("H2O", None)
    for molecule, depot_name in list(SE_TO_US_DEPOT.items()):
        if depot_name in excluded_depots:
            partials.pop(molecule, None)

    atmospheric_masses = distribute_atmosphere_mass_from_partials(
        partials, expected_atm_mass_kg
    )
    atmospheric_names = set(atmospheric_masses)
    old_total = sum(max(0.0, safe_float(item.get("Mass", 0.0))) for item in depots.values())
    before_sum = sum(
        max(0.0, safe_float(depots.get(name, {}).get("Mass", 0.0)))
        for name in atmospheric_names
    )
    celestial_before = safe_float(celestial.get("AtmosphereMass", 0.0))
    strict = bool(vapor_policy.get("strict", True))
    if strict:
        for name, mass in atmospheric_masses.items():
            depots.setdefault(name, {"Mass": 0.0, "LockSurfaceTracking": False})
            depots[name]["Mass"] = float(max(0.0, mass))
            if lock_atm_depots:
                depots[name]["LockSurfaceTracking"] = True
        after_total = sum(max(0.0, safe_float(item.get("Mass", 0.0))) for item in depots.values())
        mass_balance_delta = old_total - after_total
        if abs(mass_balance_delta) > 0.0:
            depots.setdefault("Silicate", {"Mass": 0.0, "LockSurfaceTracking": False})
            depots["Silicate"]["Mass"] = max(
                0.0, safe_float(depots["Silicate"].get("Mass", 0.0)) + mass_balance_delta
            )
        celestial["AtmosphereMass"] = float(max(0.0, expected_atm_mass_kg))
    depot_sum = sum(
        max(0.0, safe_float(depots.get(name, {}).get("Mass", 0.0)))
        for name in atmospheric_names
    )
    if expected_atm_mass_kg > 0.0 and (
        abs(before_sum - expected_atm_mass_kg) / expected_atm_mass_kg > 0.05
        or abs(celestial_before - expected_atm_mass_kg) / expected_atm_mass_kg > 0.05
    ):
        if strict:
            log_debug(
                f"[atmo-fix] Body='{entity.get('Name', 'unknown')}' depot_sum={before_sum:.6g} "
                f"celestial={celestial_before:.6g} expected={expected_atm_mass_kg:.6g}; "
                "rescaled atmospheric depots to expected pressure",
                "ATMO_FIX_WARN",
            )
        else:
            log_debug(
                f"[atmo-mass-mismatch] Body='{entity.get('Name', 'unknown')}' "
                f"depot_sum={before_sum:.6g} celestial={celestial_before:.6g} "
                f"expected={expected_atm_mass_kg:.6g}; strict consistency disabled",
                "ATMO_MASS_WARN",
            )
    report["atmospheric_depot_names"] = sorted(atmospheric_names)
    report["atmospheric_depot_masses_kg"] = {
        name: float(mass) for name, mass in atmospheric_masses.items()
    }
    report["atmospheric_depot_mass_kg"] = depot_sum
    report["water_vapor_included"] = "Water" in atmospheric_names
    report["surface_depot_names"] = sorted(excluded_depots)
    report["gas_depots_locked"] = lock_atm_depots and strict
    return report


def enforce_static_surface_volatile_safety(entity, raw, expected_atm_mass_kg,
                                           options=None, report=None):
    """Keep surface/ocean chemistry from mutating a static imported atmosphere."""
    options = options if isinstance(options, dict) else {}
    report = report if isinstance(report, dict) else {}
    source = raw.get("raw_data", raw) if isinstance(raw, dict) else {}
    body_class = str(source.get("Class", "")).strip().lower().replace(" ", "")
    if body_class in ("star", "jupiter", "neptune", "uranus", "gasgiant", "icegiant"):
        return report
    if expected_atm_mass_kg <= 0.0:
        return report

    try:
        import globals_compat as runtime
    except ImportError:
        runtime = _const

    static_atmospheres = bool(options.get(
        "static_imported_atmospheres",
        getattr(runtime, "STATIC_IMPORTED_ATMOSPHERES", _const.STATIC_IMPORTED_ATMOSPHERES),
    ))
    lock_gases = bool(options.get(
        "lock_atmospheric_depots",
        getattr(runtime, "LOCK_IMPORTED_ATMOSPHERIC_DEPOTS", _const.LOCK_IMPORTED_ATMOSPHERIC_DEPOTS),
    ))
    lock_liquids = bool(options.get(
        "lock_liquid_depots",
        getattr(runtime, "LOCK_IMPORTED_LIQUID_DEPOTS", _const.LOCK_IMPORTED_LIQUID_DEPOTS),
    ))
    max_fraction = max(0.0, safe_float(options.get(
        "max_surface_water_fraction",
        getattr(runtime, "MAX_STATIC_SURFACE_WATER_DEPOT_FRACTION_OF_ATMOSPHERE",
                _const.MAX_STATIC_SURFACE_WATER_DEPOT_FRACTION_OF_ATMOSPHERE),
    )))
    other_fraction = max(0.0, safe_float(options.get(
        "max_other_volatile_fraction",
        getattr(runtime, "MAX_STATIC_SURFACE_OTHER_VOLATILE_FRACTION_OF_ATMOSPHERE",
                _const.MAX_STATIC_SURFACE_OTHER_VOLATILE_FRACTION_OF_ATMOSPHERE),
    )))

    components = {component.get("$type"): component for component in entity.get("Components", [])}
    composition = components.get("CompositionComponent", {})
    depots = composition.get("depots", {}) if isinstance(composition, dict) else {}
    if not isinstance(depots, dict):
        return report

    ocean = source.get("Ocean", {}) if isinstance(source.get("Ocean"), dict) else {}
    ocean_comp = ocean.get("Composition", {}) if isinstance(ocean.get("Composition"), dict) else {}
    no_ocean = se_bool(source.get("NoOcean", "false"))
    has_ocean = bool(ocean) and not no_ocean
    ocean_report = source.get("_ocean_depot_report", {})
    export_mode = str(ocean_report.get(
        "ocean_depot_export_mode",
        _ocean_depot_export_mode_runtime(),
    ))
    atmospheric_names = set(report.get("atmospheric_depot_names", ()))
    atmospheric_masses = {
        str(name): max(0.0, safe_float(mass))
        for name, mass in report.get("atmospheric_depot_masses_kg", {}).items()
    }
    guarded_names = {
        "Water", "Nitrogen", "Argon", "Methane", "Ammonia",
        "Carbon Dioxide", "Sulfur Dioxide", "Hydrogen", "Helium", "Oxygen",
    }
    water_cap = expected_atm_mass_kg * max_fraction
    other_cap = expected_atm_mass_kg * other_fraction
    before_by_name = {
        name: max(0.0, safe_float(depots.get(name, {}).get("Mass", 0.0)))
        for name in guarded_names
    }
    removed_by_name = {}
    # Read depot mode
    try:
        import globals_compat as _rt_vol
        _depot_mode_vol = str(getattr(_rt_vol, "STATIC_ATMOSPHERE_DEPOT_MODE",
                                      getattr(_const, "STATIC_ATMOSPHERE_DEPOT_MODE", "none")))
    except ImportError:
        _depot_mode_vol = str(getattr(_const, "STATIC_ATMOSPHERE_DEPOT_MODE", "none"))

    if static_atmospheres and _depot_mode_vol in ("none", "carrier_unlocked", "carrier_locked"):
        # Hard-zero all reactive volatiles except the carrier (if any).
        carrier_name = report.get("carrier_name") if _depot_mode_vol != "none" else None
        should_lock  = _depot_mode_vol == "carrier_locked"
        for name in guarded_names:
            depot = depots.get(name)
            if not isinstance(depot, dict):
                continue
            if name == carrier_name:
                depot["LockSurfaceTracking"] = should_lock
                continue
            current = max(0.0, safe_float(depot.get("Mass", 0.0)))
            if current > 0.0:
                removed_by_name[name] = current
                depot["Mass"] = 0.0
    elif static_atmospheres:
        for name in guarded_names:
            depot = depots.get(name)
            if not isinstance(depot, dict):
                continue
            expected_gas_mass = atmospheric_masses.get(name, 0.0)
            current = max(0.0, safe_float(depot.get("Mass", 0.0)))
            if export_mode == "visual_only":
                surface_allowance = 0.0
            elif export_mode == "legacy":
                surface_allowance = max(0.0, current - expected_gas_mass)
            elif name == "Water":
                surface_allowance = water_cap
            else:
                surface_allowance = other_cap
            allowed = expected_gas_mass + surface_allowance
            if current > allowed * 1.000001:
                removed_by_name[name] = current - allowed
                depot["Mass"] = float(allowed)
            if name in atmospheric_names and lock_gases:
                depot["LockSurfaceTracking"] = True
            elif current > 0.0 and lock_liquids:
                depot["LockSurfaceTracking"] = True

    removed_total = sum(removed_by_name.values())
    if removed_total > 0.0:
        depots.setdefault("Silicate", {"Mass": 0.0, "LockSurfaceTracking": False})
        depots["Silicate"]["Mass"] = max(
            0.0, safe_float(depots["Silicate"].get("Mass", 0.0)) + removed_total
        )
    if static_atmospheres and lock_gases:
        for name in atmospheric_names:
            depot = depots.get(name)
            if isinstance(depot, dict):
                depot["LockSurfaceTracking"] = True
    if static_atmospheres and has_ocean and lock_liquids:
        depots.setdefault("Water", {"Mass": 0.0, "LockSurfaceTracking": False})
        depots["Water"]["LockSurfaceTracking"] = True

    water = depots.get("Water", {})
    water_total_after = max(0.0, safe_float(water.get("Mass", 0.0)))
    atmospheric_water = atmospheric_masses.get("Water", 0.0)
    surface_water_after = max(0.0, water_total_after - atmospheric_water)
    calculated_surface_water_before = max(
        max(0.0, safe_float(ocean_report.get("physical_ocean_mass_kg", 0.0))),
        max(0.0, before_by_name.get("Water", 0.0) - atmospheric_water),
    ) if has_ocean else 0.0
    ratio = calculated_surface_water_before / expected_atm_mass_kg
    if calculated_surface_water_before > water_cap and static_atmospheres:
        log_debug(
            f"[volatile-risk] Body='{entity.get('Name', 'unknown')}' "
            f"Water depot {calculated_surface_water_before:.6g}kg is {ratio:.6g}x atmosphere mass; "
            f"action='{'visual_only' if export_mode == 'visual_only' else 'cap_and_lock'}'",
            "VOLATILE_RISK",
        )
    if has_ocean and static_atmospheres:
        log_debug(
            f"[volatile-lock] Body='{entity.get('Name', 'unknown')}' Water depot "
            f"surface_after={surface_water_after:.6g}kg total_after={water_total_after:.6g}kg "
            f"LockSurfaceTracking={bool(water.get('LockSurfaceTracking', False))}",
            "VOLATILE_LOCK",
        )

    unsupported_depots = set()
    for molecule, percent in ocean_comp.items():
        if safe_float(percent) <= 0.0 or molecule in SE_OCEAN_TO_US_DEPOT:
            continue
        if molecule == "NaCl":
            # Report the active Argon reservoir removed from old converter output.
            # NaCl is not, and must never become, a chemistry/depot mapping.
            unsupported_depots.add("Argon")
        elif molecule in SE_ATMOSPHERE_TO_US_DEPOT:
            unsupported_depots.add(SE_ATMOSPHERE_TO_US_DEPOT[molecule])
    unsupported_depots = sorted(unsupported_depots)
    unsupported_solutes = sorted(
        str(molecule) for molecule, percent in ocean_comp.items()
        if safe_float(percent) > 0.0 and molecule not in SE_OCEAN_TO_US_DEPOT
    )
    gas_locked = bool(
        static_atmospheres and lock_gases
        and all(bool(depots.get(name, {}).get("LockSurfaceTracking", False)) for name in atmospheric_names)
    )
    liquid_locked = bool(
        not surface_water_after
        or (lock_liquids and bool(water.get("LockSurfaceTracking", False)))
    )

    report.update({
        "ocean_depot_export_mode": export_mode,
        "surface_water_depot_before_kg": calculated_surface_water_before,
        "surface_water_depot_after_kg": surface_water_after,
        "surface_water_depot_cap_kg": water_cap,
        "surface_water_depot_locked": bool(water.get("LockSurfaceTracking", False)),
        "surface_water_depot_removed_or_capped": bool(
            calculated_surface_water_before > surface_water_after
        ),
        "surface_other_volatile_cap_kg": other_cap,
        "unsupported_ocean_depots_removed": unsupported_depots,
        "unsupported_ocean_solutes_ignored": unsupported_solutes,
        "volatile_depot_masses_before_kg": before_by_name,
        "volatile_depot_masses_after_kg": {
            name: max(0.0, safe_float(depots.get(name, {}).get("Mass", 0.0)))
            for name in guarded_names
        },
        "volatile_depot_masses_removed_kg": removed_by_name,
        "water_depot_can_rewrite_atmosphere": bool(
            static_atmospheres and (
                surface_water_after > water_cap * 1.000001
                or (surface_water_after > 0.0 and not bool(water.get("LockSurfaceTracking", False)))
            )
        ),
        "surface_water_depot_applicable": bool(static_atmospheres and has_ocean),
        "gas_depots_locked": gas_locked,
        "atmospheric_depots_locked": gas_locked,
        "liquid_depots_locked": liquid_locked,
    })
    return report


def build_ocean_depots(obj: dict, expected_atm_mass_kg: float = 0.0) -> dict:
    raw = obj.get("raw_data", {}) or {}
    flags = _source_flags(raw)
    ocean_like = _is_ocean_class(raw.get("Class", ""), obj.get("archetype", ""))
    if flags["has_no_ocean"] or (not flags["has_ocean_block"] and not ocean_like):
        return {}
    comp = flags.get("raw_ocean_composition", {}) or {}
    radius_m = safe_float(obj.get("radius_m", 0.0))
    mass_kg = safe_float(obj.get("mass_kg", 0.0))
    if radius_m <= 0:
        return {}

    depth_km = max(0.0, flags.get("raw_ocean_depth", 0.0))
    water_fraction, water_mode = _surface_water_fraction(raw, depth_km)
    if depth_km > 0:
        physical_ocean_mass = (
            4.0 * math.pi * radius_m**2 * water_fraction * depth_km * 1000.0 * 1025.0
        )
    else:
        physical_ocean_mass = mass_kg * MAX_SOLID_PLANET_SURFACE_VOLATILE_FRACTION if ocean_like else 0.0

    if water_mode == "lacustrine":
        depot_scale = compute_lacustrine_depot_scale(depth_km, water_fraction)
        before_cap = physical_ocean_mass * depot_scale
        volatile_cap = mass_kg * MAX_LACUSTRINE_VOLATILE_FRACTION
    else:
        depot_scale = 1.0
        before_cap = physical_ocean_mass
        volatile_cap = mass_kg * MAX_SOLID_PLANET_SURFACE_VOLATILE_FRACTION
    try:
        import globals_compat as runtime
    except ImportError:
        runtime = _const
    is_solid = obj.get("archetype") not in ("gas_giant", "ice_giant", "star")
    requested_mode = _ocean_depot_export_mode_runtime()
    if is_solid and requested_mode == "visual_only":
        ocean_mass = 0.0
        export_mode = "visual_only"
    elif requested_mode == "legacy":
        if water_mode == "lacustrine":
            ocean_mass = min(before_cap, volatile_cap) if volatile_cap > 0.0 else before_cap
            export_mode = "legacy_lacustrine_capped"
        else:
            ocean_mass = physical_ocean_mass
            # For Terra/Earth-like worlds (not bulk ocean worlds), cap to a
            # mass-scaled Earth ocean mass so Ocean.Depth doesn't export ~4 ocean masses.
            cls_lo = str(raw.get("Class", "")).strip().lower()
            if not _is_ocean_class(raw.get("Class", ""), obj.get("archetype", "")) \
                    and cls_lo in ("terra", "earthlike", "earth", "") and is_solid:
                from constants import EARTH_OCEAN_MASS_US, EARTH_MASS_KG as _EMK
                terra_cap = EARTH_OCEAN_MASS_US * (mass_kg / _EMK)
                if ocean_mass > terra_cap:
                    ocean_mass = terra_cap
                    log_debug(
                        f"[ocean-depot] Body='{raw.get('Name', obj.get('name', 'unknown'))}' "
                        f"terra_ocean_cap applied: {terra_cap:.6g} kg",
                        "WATER_DEPOT",
                    )
            export_mode = "legacy"
    else:
        ocean_mass = min(before_cap, volatile_cap) if volatile_cap > 0.0 else before_cap
        if is_solid and expected_atm_mass_kg > 0.0:
            static_cap = expected_atm_mass_kg * max(
                0.0, safe_float(getattr(
                    runtime, "MAX_STATIC_SURFACE_WATER_DEPOT_FRACTION_OF_ATMOSPHERE",
                    _const.MAX_STATIC_SURFACE_WATER_DEPOT_FRACTION_OF_ATMOSPHERE,
                ))
            )
            ocean_mass = min(ocean_mass, static_cap)
            volatile_cap = min(volatile_cap, static_cap) if volatile_cap > 0.0 else static_cap
        export_mode = "capped"
    dominant = max(comp, key=lambda key: safe_float(comp[key])) if comp else "H2O"
    ignored_solutes = sorted(
        str(molecule) for molecule, percent in comp.items()
        if safe_float(percent) > 0.0 and molecule not in SE_OCEAN_TO_US_DEPOT
    )

    report = {
        "mode": water_mode,
        "source_depth_km": depth_km,
        "target_water_fraction": water_fraction,
        "physical_ocean_mass_kg": physical_ocean_mass,
        "depot_scale": depot_scale,
        "before_cap_kg": before_cap,
        "volatile_cap_kg": volatile_cap,
        "us_depot_mass_kg": ocean_mass,
        "dominant_molecule": dominant,
        "ocean_depot_export_mode": export_mode,
        "ignored_active_depot_solutes": ignored_solutes,
    }
    raw["_ocean_depot_report"] = report

    _body_log_name = raw.get("Name", obj.get("name", "unknown"))
    _sea_level_log = flags.get("raw_surface_sea_level", 0.0)
    log_debug(
        f"[water-depth] Body='{_body_log_name}' mode='{water_mode}' "
        f"seaLevel={_sea_level_log:.6g} source_depth_km={depth_km:.6g} "
        f"target_water_fraction={water_fraction:.6g} generated_max_depth_km={depth_km:.6g} "
        f"composition='{dominant}'",
        "WATER_DEPTH",
    )
    if water_mode == "lacustrine":
        log_debug(
            f"[ocean-depot] Body='{_body_log_name}' requested_mode='{requested_mode}' "
            f"water_mode='lacustrine' effective_export_mode='{export_mode}' "
            f"depth_km={depth_km:.6g} seaLevel={_sea_level_log:.6g} "
            f"active_water_depot_mass_kg={ocean_mass:.6g} "
            f"capped_target_kg={min(before_cap, volatile_cap) if volatile_cap > 0.0 else before_cap:.6g}",
            "WATER_DEPOT",
        )
    else:
        log_debug(
            f"[ocean-depot] Body='{_body_log_name}' requested_mode='{requested_mode}' "
            f"water_mode='{water_mode}' effective_export_mode='{export_mode}' "
            f"depth_km={depth_km:.6g} seaLevel={_sea_level_log:.6g} "
            f"active_water_depot_mass_kg={ocean_mass:.6g} "
            f"physical_ocean_mass_kg={physical_ocean_mass:.6g}",
            "WATER_DEPOT",
        )
    log_debug(
        f"[water-depot] Body='{_body_log_name}' "
        f"physical_mass={physical_ocean_mass:.6g} scale={depot_scale:.6g} "
        f"us_depot_mass={ocean_mass:.6g} export_mode='{export_mode}' "
        f"dominant='{SE_OCEAN_TO_US_DEPOT.get(dominant, dominant)}'",
        "WATER_DEPOT",
    )
    for molecule in ignored_solutes:
        log_debug(
            f"[ocean-solute] Body='{raw.get('Name', obj.get('name', 'unknown'))}' "
            f"molecule='{molecule}' ignored_for_active_depots=True "
            "reason='salt/unsupported ocean chemistry is visual/debug only'",
            "OCEAN_CHEMISTRY_WARN",
        )
    if ocean_mass < physical_ocean_mass:
        reason = "lacustrine volatile scaling/cap" if water_mode == "lacustrine" else "solid planet volatile cap"
        log_debug(
            f"[water-depot-clamp] Body='{raw.get('Name', obj.get('name', 'unknown'))}' "
            f"reason='{reason}' before={physical_ocean_mass:.6g} after={ocean_mass:.6g}",
            "WATER_DEPOT_WARN",
        )
    return distribute_ocean_depot_mass(comp, ocean_mass)


def apply_source_flags(entity: dict, obj: dict) -> None:
    raw = obj.get("raw_data", {}) or obj
    flags = _source_flags(raw)
    for comp in entity.get("Components", []):
        if comp.get("$type") == "Celestial":
            if flags["has_no_atmosphere"]:
                comp["AtmosphereMass"] = 0.0
        if comp.get("$type") == "AppearanceComponent":
            planet = comp.get("Planet", {})
            gas = comp.get("GasGiant", {})
            if flags["has_no_ocean"] and planet:
                planet["UseWater"] = False
            if flags["has_no_atmosphere"] and planet:
                planet["ShowAtmosphere"] = False
                planet["ShowAtmosphereClouds"] = False
                planet["CloudOpacity"] = 0.0
                planet["CloudCoverage"] = 0.0
                planet["customAtmosphereOpacity"] = 0.0
                planet["RayleighScatteringStrength"] = 0.0
                planet["DefaultRayleighScatteringStrength"] = 0.0
            elif flags["has_no_clouds"] and planet:
                planet["ShowAtmosphereClouds"] = False
                planet["CloudOpacity"] = 0.0
                planet["CloudCoverage"] = 0.0
            if flags["has_no_clouds"] and gas:
                gas["CloudOpacity"] = 0.0
                gas["CloudCoverage"] = 0.0


def force_visual_state_initialization(entity: dict, obj: dict) -> None:
    """Make imported atmosphere and water visuals explicit in the initial JSON."""
    raw = obj.get("raw_data", {}) if isinstance(obj, dict) else {}
    flags = _source_flags(raw)
    atmosphere = raw.get("Atmosphere", {}) if isinstance(raw.get("Atmosphere"), dict) else {}
    ocean = raw.get("Ocean", {}) if isinstance(raw.get("Ocean"), dict) else {}
    celestial = next(
        (c for c in entity.get("Components", []) if c.get("$type") == "Celestial"), {}
    )
    appearance = next(
        (c for c in entity.get("Components", []) if c.get("$type") == "AppearanceComponent"), {}
    )
    planet = appearance.get("Planet", {}) if isinstance(appearance, dict) else {}
    if not planet:
        return

    has_atmosphere = bool(atmosphere) and not flags.get("has_no_atmosphere", False)
    if has_atmosphere:
        planet["ShowAtmosphere"] = True
        planet["AtmosphereSimulationMode"] = 0
        planet["AtmosphereColorMode"] = 1
        planet["customAtmosphereOpacity"] = max(
            0.08, safe_float(planet.get("customAtmosphereOpacity", 0.0))
        )
        rayleigh = max(0.15, safe_float(planet.get("RayleighScatteringStrength", 0.0)))
        planet["RayleighScatteringStrength"] = rayleigh
        planet["DefaultRayleighScatteringStrength"] = rayleigh
        celestial["AtmosphereLayers"] = max(1, int(safe_float(
            celestial.get("AtmosphereLayers", 1), 1
        )))
    has_water = bool(ocean) and not flags.get("has_no_ocean", False)
    if has_water:
        # Recompute from SE source so we never keep an oil-black stale color
        try:
            from constants import parse_se_surface_preset, parse_life_block
            _preset_info = parse_se_surface_preset(
                raw.get("Surface", {}).get("Preset", "")
                if isinstance(raw.get("Surface"), dict) else ""
            )
            _life_info = raw.get("_life_info") or parse_life_block(raw.get("Life", {}))
            _water_app  = compute_water_color(ocean, _preset_info, _life_info, {})
            water_color = _water_app.get("color") or planet.get("WaterColor") or WATER_COLORS["default"]
        except Exception:
            water_color = planet.get("WaterColor") or WATER_COLORS["default"]
        planet["UseWater"] = True
        planet["WaterColorMode"] = 1
        planet["WaterColor"] = water_color
        planet["originalWaterColor"] = water_color
        planet["customWaterColor"] = water_color

    log_debug(
        f"[visual-init] Body='{entity.get('Name', 'unknown')}' atmosphere={has_atmosphere} "
        f"ShowAtmosphere={bool(planet.get('ShowAtmosphere', False))} "
        f"mass={safe_float(celestial.get('AtmosphereMass', 0.0)):.6g}",
        "VISUAL_INIT",
    )
    log_debug(
        f"[visual-init] Body='{entity.get('Name', 'unknown')}' water={has_water} "
        f"UseWater={bool(planet.get('UseWater', False))} liquid_mask={has_water}",
        "VISUAL_INIT",
    )


def build_depots(archetype, mass_kg, atm_info, has_ocean, radius_m, sea_level, obj_data=None) -> dict:
    obj = {
        "name": (obj_data or {}).get("_converter_body_name", "unknown"),
        "archetype": archetype,
        "mass_kg": mass_kg,
        "radius_m": radius_m,
        "atm_info": atm_info or {},
        "raw_data": obj_data or {},
    }
    req_atm_mass = 0.0
    flags = _source_flags(obj_data or {})
    atm_block = (obj_data or {}).get("Atmosphere", {}) if isinstance((obj_data or {}).get("Atmosphere"), dict) else (atm_info or {})
    if not flags["has_no_atmosphere"] and atm_info:
        req_atm_mass = compute_atmosphere_mass_and_pressure(
            obj, atm_block, (obj_data or {}).get("Class", archetype)
        )["atmosphere_mass_kg"]

    acc = {}
    atmosphere = build_atmosphere_depots(obj, req_atm_mass)
    ocean = build_ocean_depots(obj, req_atm_mass)
    is_solid = archetype not in ("gas_giant", "ice_giant", "star")

    # Record provenance so validators can compare per-source masses,
    # not the merged total depot which combines atmosphere + ocean + bulk.
    if obj_data is not None:
        obj_data["_depot_source_masses_kg"] = {
            "atmosphere": dict(atmosphere),
            "ocean": dict(ocean),
        }

    if is_solid:
        for name, mass in atmosphere.items():
            _merge_mass(acc, name, mass)
        # Atmospheric composition is authoritative. Ocean depots are optional,
        # phase-safe active reservoirs and are empty by default for static imports.
        for name, mass in ocean.items():
            _merge_mass(acc, name, mass)

        non_bulk = sum(acc.values())
        bulk_mass = max(0.0, mass_kg - non_bulk)
        if archetype == "lava":
            iron_frac, silicate_frac = 0.45, 0.55
        elif archetype == "ice" or "ice" in str((obj_data or {}).get("Class", "")).lower():
            ocean_report = (obj_data or {}).get("_ocean_depot_report", {})
            is_lacustrine = ocean_report.get("mode") == "lacustrine"
            # US treats the Water depot as surface-available liquid. Keep the
            # shallow source reservoir authoritative instead of adding a huge
            # bulk-ice depot that would flood the body.
            water_frac = 0.0 if is_lacustrine else (0.18 if flags["has_no_ocean"] else 0.30)
            _merge_mass(acc, "Water", bulk_mass * water_frac)
            bulk_mass *= (1.0 - water_frac)
            iron_frac, silicate_frac = 0.15, 0.85
        else:
            iron_frac, silicate_frac = 0.30, 0.70
        total_rock_frac = max(1e-9, iron_frac + silicate_frac)
        _merge_mass(acc, "Iron", bulk_mass * iron_frac / total_rock_frac)
        _merge_mass(acc, "Silicate", bulk_mass * silicate_frac / total_rock_frac)
    else:
        bulk = build_bulk_composition(obj)
        for source in (bulk, ocean, atmosphere):
            for name, mass in source.items():
                _merge_mass(acc, name, mass)

        total = sum(acc.values())
        if total > 0 and archetype in ("gas_giant", "ice_giant", "star"):
            scale = mass_kg / total
            for name in list(acc):
                acc[name] *= scale
        if archetype in ("gas_giant", "ice_giant"):
            rock_core = acc.get("Iron", 0.0) + acc.get("Silicate", 0.0)
            h_he = acc.get("Hydrogen", 0.0) + acc.get("Helium", 0.0)
            volatiles = sum(acc.get(k, 0.0) for k in ("Water", "Methane", "Ammonia", "Carbon Dioxide", "Sulfur Dioxide"))
            log_debug(
                f"[composition] Body='{(obj_data or {}).get('Name', 'unknown')}' "
                f"class='{(obj_data or {}).get('Class', archetype)}' mode='{archetype}' "
                f"rock_core={rock_core:.6g} volatiles={volatiles:.6g} h_he={h_he:.6g}",
                "COMPOSITION",
            )

    result = _empty_depots()
    for key, val in acc.items():
        result.setdefault(key, {"Mass": 0.0, "LockSurfaceTracking": False})
        result[key]["Mass"] = float(max(0.0, val))
    return result


# ─────────────────────────────────────────────────────────────────────────────
# ROTATION
# ─────────────────────────────────────────────────────────────────────────────

def compute_rotation_us(rot_period_h, obliquity_deg, eq_asc_node_deg) -> tuple:
    """
    Compute the US orientation quaternion, rotation axis, and angular velocity.

    q_US = Rz(EAN)*Rx(obl) converted to US coordinates:
      qx =  cos(EAN/2)*sin(obl/2)
      qy = -sin(EAN/2)*cos(obl/2)
      qz =  sin(EAN/2)*sin(obl/2)
      qw =  cos(EAN/2)*cos(obl/2)
    """
    if abs(rot_period_h) < 1e-10 or not math.isfinite(rot_period_h):
        return "0;-1;0", "0;0;0", "0;0;0;1"
    rx, ry, rz = 0.0, -1.0, 0.0
    omega = math.copysign(2 * math.pi / abs(rot_period_h * 3600.0), rot_period_h)
    eps = math.radians(safe_float(obliquity_deg))
    N   = math.radians(safe_float(eq_asc_node_deg))
    so = math.sin(eps / 2); co = math.cos(eps / 2)
    se = math.sin(N   / 2); ce = math.cos(N   / 2)
    qx =  ce * so; qy = -se * co; qz = se * so; qw = ce * co
    mag = math.sqrt(qx*qx + qy*qy + qz*qz + qw*qw)
    if mag > 1e-10:
        qx /= mag; qy /= mag; qz /= mag; qw /= mag
    return (f"{rx:f};{ry:f};{rz:f}",
            f"{omega*rx:f};{omega*ry:f};{omega*rz:f}",
            f"{qx:f};{qy:f};{qz:f};{qw:f}")


def calculate_banding_offsets_proper(obliquity_deg, eq_asc_node_deg, rotation_axis=None) -> str:
    yaw   = safe_float(eq_asc_node_deg)
    pitch = safe_float(obliquity_deg)
    return f"{yaw:.4f};{pitch:.4f};0.0000;0.0000"


# ─────────────────────────────────────────────────────────────────────────────
# ORBITAL MECHANICS
# ─────────────────────────────────────────────────────────────────────────────

def orbital_elements_to_state_vectors(sma_m, ecc, inc_deg, asc_deg, arg_deg,
                                      mean_anom_deg, parent_mass_kg) -> tuple:
    mu = GRAVITATIONAL_CONSTANT * parent_mass_kg
    if sma_m <= 0 or parent_mass_kg <= 0:
        return [0.0]*3, [0.0]*3
    ecc  = max(0.0, min(safe_float(ecc), 0.9990))
    inc  = math.radians(safe_float(inc_deg))
    asc  = math.radians(safe_float(asc_deg))
    arg  = math.radians(safe_float(arg_deg))
    M    = math.radians(safe_float(mean_anom_deg) % 360)
    E    = M + ecc * math.sin(M)
    for _ in range(200):
        d  = 1.0 - ecc * math.cos(E)
        if abs(d) < 1e-15: break
        dE = (M - E + ecc * math.sin(E)) / d
        E += dE
        if abs(dE) < 1e-12: break
    nu  = 2 * math.atan2(math.sqrt(1+ecc)*math.sin(E/2), math.sqrt(1-ecc)*math.cos(E/2))
    r   = sma_m * (1 - ecc * math.cos(E))
    p   = sma_m * (1 - ecc**2)
    if p <= 0:
        return [0.0]*3, [0.0]*3
    h   = math.sqrt(mu * p)
    xpf =  r * math.cos(nu); ypf =  r * math.sin(nu)
    vxpf = -(mu/h)*math.sin(nu); vypf = (mu/h)*(ecc + math.cos(nu))
    cO, sO = math.cos(asc), math.sin(asc)
    ci, si = math.cos(inc), math.sin(inc)
    cw, sw = math.cos(arg), math.sin(arg)
    Qxx=cO*cw-sO*sw*ci; Qxy=-cO*sw-sO*cw*ci
    Qyx=sO*cw+cO*sw*ci; Qyy=-sO*sw+cO*cw*ci
    Qzx=sw*si;            Qzy=cw*si
    pos = [Qxx*xpf+Qxy*ypf, Qyx*xpf+Qyy*ypf, Qzx*xpf+Qzy*ypf]
    vel = [Qxx*vxpf+Qxy*vypf, Qyx*vxpf+Qyy*vypf, Qzx*vxpf+Qzy*vypf]
    pos = [v if math.isfinite(v) else 0.0 for v in pos]
    vel = [v if math.isfinite(v) else 0.0 for v in vel]
    return pos, vel


def estimate_eq_temp(dist_m, star_lum_watts, albedo=0.3, greenhouse=0.0) -> float:
    if dist_m <= 0 or star_lum_watts <= 0:
        return 280.0
    sigma = 5.670374419e-8
    T_bb  = (star_lum_watts * (1.0 - max(0.0, min(0.99, albedo)))
             / (16.0 * math.pi * sigma * dist_m**2)) ** 0.25
    return max(20.0, min(5_000.0, T_bb + greenhouse))


# ─────────────────────────────────────────────────────────────────────────────
# RING PARTICLE BUILDERS
# ─────────────────────────────────────────────────────────────────────────────

def build_ring_particles(planet_id, planet_name, planet_pos_us, planet_vel_us,
                         planet_mass_kg, ring_data, start_id,
                         obliquity_deg=0.0, eq_asc_node_deg=0.0) -> list:
    """Build ring particles in the planet's equatorial plane."""
    inner_km     = safe_float(ring_data.get("InnerRadius",  1.2e4))
    outer_km     = safe_float(ring_data.get("OuterRadius",  2.5e4))
    thickness_km = max(safe_float(ring_data.get("Thickness",   0.1)), 0.001)
    density      = safe_float(ring_data.get("Density",        0.5))
    density_sc   = safe_float(ring_data.get("densityScale",   1.0))
    rocks_sp     = max(safe_float(ring_data.get("RocksSpacing", 1.0)), 0.001)
    rocks_mx     = safe_float(ring_data.get("RocksMaxSize",   0.005))
    inner_m      = inner_km * 1_000.0
    outer_m      = outer_km * 1_000.0
    thickness_m  = max(thickness_km * 1_000.0, 50.0)
    fr, fg, fb = parse_ring_color(ring_data.get("FrontColor",    "(0.700 0.700 0.700)"))
    br, bg, bb = parse_ring_color(ring_data.get("BackDustColor", "(1.000 0.980 0.880)"))
    # Scale particle count with ring area, density, and SE packing parameters.
    ring_area_factor = max(0.1, (outer_km - inner_km) / max(inner_km, 1.0))
    raw_count = density * density_sc * ring_area_factor * 1200.0 / max(rocks_sp, 0.1)
    n_particles = max(60, min(2000, int(raw_count)))
    rng = random.Random(planet_id * 31_337)

    obl_rad = math.radians(obliquity_deg); ean_rad = math.radians(eq_asc_node_deg)
    cos_obl = math.cos(obl_rad); sin_obl = math.sin(obl_rad)
    cos_ean = math.cos(ean_rad); sin_ean = math.sin(ean_rad)

    def _rot_eq(x, y, z):
        # Rz(EAN) * Rx(obl): first rotate by obl around X, then by EAN around Z
        x1 = x; y1 = y*cos_obl - z*sin_obl; z1 = y*sin_obl + z*cos_obl
        x2 = x1*cos_ean - y1*sin_ean; y2 = x1*sin_ean + y1*cos_ean; z2 = z1
        return x2, y2, z2

    particles = []
    age = _get_system_age() or 0
    for i in range(n_particles):
        pid   = start_id + i
        theta = rng.uniform(0.0, 2.0 * math.pi)
        r     = rng.uniform(inner_m, outer_m)
        t     = (r - inner_m) / max(outer_m - inner_m, 1.0)
        cr = max(0.0, min(1.0, fr + (br-fr)*t + rng.uniform(-0.03, 0.03)))
        cg = max(0.0, min(1.0, fg + (bg-fg)*t + rng.uniform(-0.03, 0.03)))
        cb = max(0.0, min(1.0, fb + (bb-fb)*t + rng.uniform(-0.03, 0.03)))
        color  = f"RGBA({cr:.3f}, {cg:.3f}, {cb:.3f}, 1.000)"
        px_eq, py_eq, pz_eq = _rot_eq(r*math.cos(theta), r*math.sin(theta),
                                        rng.uniform(-thickness_m/2, thickness_m/2))
        rel_us = eci_to_us([px_eq, py_eq, pz_eq])
        pos_us = [planet_pos_us[j] + rel_us[j] for j in range(3)]
        v_orb  = math.sqrt(GRAVITATIONAL_CONSTANT * planet_mass_kg / r) if r > 0 and planet_mass_kg > 0 else 0.0
        vx_eq, vy_eq, vz_eq = _rot_eq(-v_orb*math.sin(theta), v_orb*math.cos(theta), 0.0)
        vel_rel_us = eci_to_us([vx_eq, vy_eq, vz_eq])
        vel_us = [planet_vel_us[j] + vel_rel_us[j] for j in range(3)]
        size     = rng.uniform(2.0, 4.5)
        seed_val = rng.randint(0, 999_999)
        rot_a    = rng.uniform(0, 360); rot_c = rng.uniform(0, 1)
        body = {
            "Header": {"BaseType":"Object","AssetType":"JSON","TypeName":"Body",
                        "BuildRevision":"46923","LastModifiedUTC":_now_utc()},
            "$type": "Body",
            "Name":  f"@{planet_name} Ring Particle",
            "Components": [
                {"$type":"ParticleComponent","Type":1,"Size":size,
                 "Color2":"RGBA(0.000, 0.000, 0.000, 0.000)",
                 "StartEnergy":0,"Energy":0,"Age":age,"Seed":seed_val,
                 "DoubleData":  f"{rng.uniform(-180,180):.4f};{rng.uniform(-90,90):.4f};{rng.uniform(-90,90):.4f}",
                 "RandomOffset":f"({rng.uniform(-0.5,0.5):.2f}, {rng.uniform(-0.3,0.3):.2f}, {rng.uniform(-0.5,0.5):.2f})",
                 "AllowTransition":False,
                 "Rotation":f"({rot_a:.5f}, 2.00000, {rot_c:.5f}, 100000.00000)",
                 "ShadingMode":1,"DecayMode":1,"originalColor":color,"Materials":{"Silicate":1}},
                {"$type":"HeatComponent","SurfaceTemperature":150.0,"TemperatureInitialized":True,
                 "Albedo":0,"SurfaceHeatCapacity":171_057.0,"SpecificHeatCapacity":0,
                 "UserChangedSurfaceHeatCapacity":False,"OverrideStartingTemp":False,
                 "EmitsLight":False,"BlackbodyNoise":1,"UseBlackbodyNoise":False},
            ],
            "Id":               pid,
            "Age":              age,
            "Color":            color, "DefaultGUIColor": color,
            "CustomColor":      "RGBA(0.000, 0.000, 0.000, 0.000)",
            "CustomGUIColor":   "RGBA(0.000, 0.000, 0.000, 0.000)",
            "UserChangedColor": False, "UserChangedGUIColor": False,
            "ColorPalette":     2, "PhysicsMass": 1.0, "Mass": 1.0,
            "Radius":           0.04174929, "GravityRadius": 0,
            "Density":          3_280.68, "Generation": 0,
            "Flags":            146, "DisplayFlags": 3,
            "Orientation":      "0;0;0;1", "AngularVelocity": "0;0;0", "RotationAxis": "0;-1;0",
            "Position":         f"{pos_us[0]:f};{pos_us[1]:f};{pos_us[2]:f}",
            "Velocity":         f"{vel_us[0]:f};{vel_us[1]:f};{vel_us[2]:f}",
            "Suspended": False, "LockPosition": False, "LockRotation": False, "LockDeformation": False,
            "ColorMode": 0, "GUIColorMode": 0,
            "Parent":           planet_id, "Source": -1, "Group": 0, "CustomOrbitParentId": -1,
            "LockedProperties": [], "Origin": 0, "Category": "", "BudgetType": 0,
            "InMajorCollision": False, "NonSphericalGravityEnabled": False,
            "J2": 0, "DatabaseID": "00000000-0000-0000-0000-000000000000", "Description": None,
        }
        particles.append(body)
    return particles


def build_asteroid_ring_particle(asteroid_id, asteroid_name, parent_name,
                                  asteroid_radius_m, asteroid_mass_kg,
                                  asteroid_pos_us, asteroid_vel_us) -> dict:
    size_km  = max(0.5, min(5.0, asteroid_radius_m / 1000.0))
    seed_val = random.randint(0, 999_999)
    rng      = random.Random(asteroid_id * 31337)
    rot_a    = rng.uniform(0, 360); rot_p = rng.uniform(0.5, 2.0)
    color    = "RGBA(0.467, 0.426, 0.316, 1.000)"
    age      = _get_system_age() or 0
    return {
        "Header": {"BaseType":"Object","AssetType":"JSON","TypeName":"Body",
                   "BuildRevision":"46923","LastModifiedUTC":_now_utc()},
        "$type": "Body",
        "Name":  f"{parent_name} Asteroid Belt",
        "Components": [
            {"$type":"ParticleComponent","Type":1,"Size":size_km,
             "Color2":"RGBA(0.000, 0.000, 0.000, 0.000)",
             "StartEnergy":0,"Energy":0,"Age":age,"Seed":seed_val,
             "DoubleData":  f"{rng.uniform(-180,180):.6f};{rng.uniform(-90,90):.6f};{rng.uniform(-90,90):.6f}",
             "RandomOffset":f"({rng.uniform(-0.5,0.5):.3f}, {rng.uniform(-0.3,0.3):.3f}, {rng.uniform(-0.5,0.5):.3f})",
             "AllowTransition":False,
             "Rotation":f"({rot_a:.5f}, 2.00000, {rot_p:.5f}, 100000.00000)",
             "ShadingMode":1,"DecayMode":1,"originalColor":color,"Materials":{"Silicate":0.5}},
            {"$type":"HeatComponent","SurfaceTemperature":166.3278,"StartingTemperature":0,
             "TemperatureInitialized":True,"Albedo":0,"SurfaceHeatCapacity":135768.302,
             "SpecificHeatCapacity":0,"UserChangedSurfaceHeatCapacity":False,
             "OverrideStartingTemp":False,"BlackbodyColorMode":0,
             "originalBlackbodyColor":"RGBA(0.000, 0.000, 0.000, 1.000)",
             "customBlackbodyColor":"RGBA(0.000, 0.000, 0.000, 0.000)",
             "EmitsLight":True,"BlackbodyNoise":1,"UseBlackbodyNoise":False,"LuminanceMode":0,
             "CustomLuminosity":0.598817},
        ],
        "Id":               asteroid_id,
        "HorizonID":        "",
        "Age":              age,
        "Color":            color, "DefaultGUIColor": color,
        "CustomColor":      "RGBA(0.000, 0.000, 0.000, 0.000)",
        "CustomGUIColor":   "RGBA(0.000, 0.000, 0.000, 0.000)",
        "UserChangedColor": False, "UserChangedGUIColor": False,
        "ColorPalette":     2,
        "PhysicsMass":      0.5, "Mass": 0.5,
        "Radius":           0.04174929, "GravityRadius": 0,
        "Density":          3280.68, "Generation": 0,
        "Flags":            146, "DisplayFlags": 3,
        "Orientation":      "0;0;0;1", "AngularVelocity": "0;0;0", "RotationAxis": "0;-1;0",
        "Position":         f"{asteroid_pos_us[0]:.6f};{asteroid_pos_us[1]:.6f};{asteroid_pos_us[2]:.6f}",
        "Velocity":         f"{asteroid_vel_us[0]:.6f};{asteroid_vel_us[1]:.6f};{asteroid_vel_us[2]:.6f}",
        "Suspended": False, "LockPosition": False, "LockRotation": False, "LockDeformation": False,
        "ColorMode": 0, "GUIColorMode": 0,
        "Parent":           -1, "Source": -1, "Group": 0, "CustomOrbitParentId": -1,
        "LockedProperties": [], "Origin": 0, "Category": "", "BudgetType": 0,
        "InMajorCollision": False, "NonSphericalGravityEnabled": False,
        "J2": 0, "DatabaseID": "00000000-0000-0000-0000-000000000000", "Description": None,
    }


def particles_to_se_rings(particles, planet_pos_us):
    if not particles:
        return None
    distances = []; rgba_list = []
    for p in particles:
        pv  = parse_vec3(p.get("Position", "0;0;0"))
        rel = [pv[j] - planet_pos_us[j] for j in range(3)]
        d   = math.sqrt(sum(x**2 for x in rel))
        if d > 0: distances.append(d)
        r_t, g_t, b_t, _ = parse_rgba(p.get("Color", "RGBA(0.5,0.5,0.5,1)"))
        rgba_list.append((r_t, g_t, b_t))
    if not distances: return None
    inner_m  = min(distances) * 0.95; outer_m = max(distances) * 1.05
    inner_km = inner_m / 1_000.0;    outer_km = outer_m / 1_000.0
    edge_km  = (inner_km + outer_km) / 2.0
    mean_km  = (sum(distances) / len(distances)) / 1_000.0
    avg_r = sum(c[0] for c in rgba_list) / len(rgba_list)
    avg_g = sum(c[1] for c in rgba_list) / len(rgba_list)
    avg_b = sum(c[2] for c in rgba_list) / len(rgba_list)
    density = min(1.0, max(0.1, len(particles) / 400.0))
    rot_h = 7.98
    if particles:
        p0  = particles[0]
        pv  = parse_vec3(p0.get("Position","0;0;0"))
        vv  = parse_vec3(p0.get("Velocity","0;0;0"))
        rel = us_to_eci([pv[j]-planet_pos_us[j] for j in range(3)])
        r0  = math.sqrt(sum(x**2 for x in rel))
        spd = math.sqrt(sum(x**2 for x in vv))
        if r0 > 0 and spd > 0:
            rot_h = (2 * math.pi * r0 / spd) / 3_600.0
    bk = max(0.0,min(1.0,avg_r+0.10)); gk = max(0.0,min(1.0,avg_g-0.20)); bv = max(0.0,min(1.0,avg_b-0.30))
    return {
        "InnerRadius": inner_km, "OuterRadius": outer_km, "EdgeRadius": edge_km, "MeanRadius": mean_km,
        "Thickness":   max(0.01,(outer_km-inner_km)*0.001), "RocksMaxSize": 0.00231, "RocksSpacing": 1,
        "DustDrawDist": 173, "ChartRadius": edge_km, "RotationPeriod": rot_h,
        "Brightness": 10, "FrontBright": 0.966, "BackBright": 0.681,
        "Density": density, "Opacity": density, "SelfShadow": density, "PlanetShadow": density,
        "Hapke": 1, "SpotBright": 2.51, "SpotWidth": 0.0333, "SpotBrightCB": 0, "SpotWidthCB": 0.001,
        "frequency": 7.21, "densityScale": 1.52, "densityOffset": -0.415, "densityPower": 0.996,
        "colorContrast": 0.0759,
        "FrontColor":     f"({avg_r:.3f} {avg_g:.3f} {avg_b:.3f})",
        "BackThickColor": f"({bk:.3f} {gk:.3f} {bv:.3f})",
        "BackIceColor":   "(0.300 0.700 1.000)",
        "BackDustColor":  "(1.000 0.980 0.880)",
    }


# ─────────────────────────────────────────────────────────────────────────────
# MAIN ENTITY BUILDER
# ─────────────────────────────────────────────────────────────────────────────

def _attach_surface_grid_runtime() -> bool:
    try:
        import globals_compat as runtime
        mode = str(getattr(runtime, "SURFACE_DATA_MODE", "liquid_mask_only")).strip().lower()
        attach = bool(getattr(runtime, "ATTACH_SURFACE_GRID_COMPONENT", True))
    except Exception:
        mode = str(getattr(_const, "SURFACE_DATA_MODE", "liquid_mask_only")).strip().lower()
        attach = bool(getattr(_const, "ATTACH_SURFACE_GRID_COMPONENT", True))
    return attach and mode in {"liquid_mask_only", "full_us_like", "active_legacy"}


def _build_surface_grid_component(atlas_index: int,
                                  radius_m: float,
                                  bump_height: float = 10.0) -> dict:
    if atlas_index < 0 or atlas_index >= 256:
        raise ValueError(f"AtlasIndex {atlas_index} out of range [0, 255]")
    # Space Engine Surface.BumpHeight is effectively kilometers for these
    # terrain presets. US needs radius-relative relief, with a small style boost
    # to match terraformed Mars-like reference terrain.
    elevation_ratio = min((max(0.0, bump_height) * 1000.0 / max(radius_m, 1.0)) * 1.8, 0.05)
    return {
        "$type": "SurfaceGridComponent",
        "AtlasIndex": atlas_index,
        "ElevationToRadiusRatio": round(elevation_ratio, 6),
    }


def build_ubox_entity(obj_id, obj_name, category,
                      archetype, mass_kg, radius_m,
                      us_pos, us_vel, parent_id,
                      is_star=False,
                      relative_to_id=-1,
                      teff=5800.0, lum_watts=3.828e26,
                      rot_period_h=0.0, obliquity_deg=0.0, eq_asc_node_deg=0.0,
                      barycenter_obliquity=0.0, barycenter_eq_asc_node=0.0,
                      atm_info=None, has_ocean=False, use_water=False,
                      mag_field=0.0, mag_pole_angle=0.0,
                      sea_level=0.0, surface_preset="",
                      est_temp=None,
                      has_life=False, has_exotic_life=False, has_aerial_life=False,
                      has_organic_life=False,
                      diffmap="", se_class="",
                      dist_au=5.0, star_teff=5800.0,
                      use_clouds=True,
                      inherit_star_axial_tilt=False,
                      atlas_index=None,
                      obj_data=None) -> tuple:

    if obj_data is None:
        obj_data = {}
    obj_data["_converter_body_name"] = obj_name

    age      = _get_system_age() or 0.0
    pos_str  = f"{us_pos[0]:f};{us_pos[1]:f};{us_pos[2]:f}"
    vel_str  = f"{us_vel[0]:f};{us_vel[1]:f};{us_vel[2]:f}"
    
    # ── ROTATION US TILT INHERITANCE ──────────────────────────────────────
    _eff_obliquity = obliquity_deg
    _eff_eq_asc    = eq_asc_node_deg
    if is_star and inherit_star_axial_tilt:
        if abs(barycenter_obliquity) > 1e-6 or abs(barycenter_eq_asc_node) > 1e-6:
            _eff_obliquity = barycenter_obliquity
            _eff_eq_asc    = barycenter_eq_asc_node
    rot_axis_str, ang_vel_str, quat_str = compute_rotation_us(
        rot_period_h, _eff_obliquity, _eff_eq_asc)
    rot_axis = parse_vec3(rot_axis_str)

    source_flags = _source_flags(obj_data)
    surf_for_preset = obj_data.get("Surface", {}) if isinstance(obj_data.get("Surface"), dict) else {}
    preset_info = parse_se_surface_preset(surface_preset or surf_for_preset.get("Preset", ""))
    preset_flags = preset_info.get("appearance_flags", {})
    if obj_data is not None:
        obj_data["_preset_info"] = preset_info
    life_info = parse_life_block(obj_data.get("Life") if isinstance(obj_data, dict) else None)
    if obj_data is not None:
        obj_data["_life_info"] = life_info
    has_life = bool(has_life or life_info["has_life"])
    has_exotic_life = bool(has_exotic_life or life_info["is_exotic"])
    has_organic_life = bool(has_organic_life or life_info["is_organic"])
    has_aerial_life = bool(has_aerial_life or life_info["has_aerial"])
    raw_atmosphere = obj_data.get("Atmosphere")
    if isinstance(raw_atmosphere, dict) and not obj_data.get("_atmosphere_normalization_report"):
        normalized_atmosphere, normalization_report = normalize_se_atmosphere(
            raw_atmosphere,
            life_info,
            obj_data.get("Surface", {}),
            obj_data.get("Ocean", {}),
            se_class or obj_data.get("Class", archetype),
            est_temp if est_temp is not None else _ARCHETYPE_DEFAULT_TEMPS.get(archetype, 280.0),
            {
                "body_name": obj_name,
                "no_ocean": se_bool(obj_data.get("NoOcean", "false")),
            },
        )
        obj_data["_raw_atmosphere"] = copy.deepcopy(raw_atmosphere)
        obj_data["Atmosphere"] = normalized_atmosphere
        obj_data["_atmosphere_normalization_report"] = normalization_report
        flags_for_normalized = dict(obj_data.get("_source_flags", {}) or {})
        flags_for_normalized["raw_atmosphere_composition"] = dict(
            normalized_atmosphere.get("Composition", {})
        )
        obj_data["_source_flags"] = flags_for_normalized
        atm_info = dict(atm_info or {})
        atm_info.update({
            "pressure": safe_float(normalized_atmosphere.get("Pressure", 0.0)),
            "density": safe_float(normalized_atmosphere.get("Density", atm_info.get("density", 0.0))),
            "height": safe_float(normalized_atmosphere.get("Height", atm_info.get("height", 0.0))),
            "comp": dict(normalized_atmosphere.get("Composition", {})),
            "hue": normalized_atmosphere.get("Hue", atm_info.get("hue")),
            "saturation": normalized_atmosphere.get("Saturation", atm_info.get("saturation")),
            "model": normalized_atmosphere.get("Model", atm_info.get("model", "")),
            "opacity": safe_float(normalized_atmosphere.get("Opacity", atm_info.get("opacity", 1.0))),
        })
        source_flags = _source_flags(obj_data)
    log_debug(
        f"[life] Body='{obj_name}' class='{','.join(sorted(life_info['classes'])) or 'None'}' "
        f"type='{','.join(sorted(life_info['types'])) or 'None'}' "
        f"biome='{','.join(sorted(life_info['biomes'])) or 'None'}' "
        f"organic={life_info['is_organic']} exotic={life_info['is_exotic']} "
        f"unicellular={life_info['is_unicellular']} multicellular={life_info['is_multicellular']} "
        f"subglacial={life_info['has_subglacial']} aerial={life_info['has_aerial']} "
        f"marine={life_info['has_marine']} terrestrial={life_info['has_terrestrial']}",
        "LIFE",
    )
    debug_life_color = choose_life_debug_color(life_info)
    if debug_life_color:
        debug_reason = "Exotic life debug marker" if life_info["is_exotic"] else "Organic life debug marker"
        log_debug(
            f"[debug-color] Body='{obj_name}' gui/trail='{debug_life_color}' reason='{debug_reason}'",
            "DEBUG_COLOR",
        )
    log_debug(
        f"[preset] Body='{obj_name}' raw='{preset_info.get('raw', '')}' "
        f"family='{preset_info.get('family')}' state='{preset_info.get('state')}' "
        f"style='{preset_info.get('style')}' palette='{preset_info.get('surface_palette_hint')}' "
        f"explicit_plants={preset_info.get('explicit_plants')} plant_color={preset_info.get('plant_color')} "
        f"water='{preset_info.get('water_color_hint')}'",
        "PRESET",
    )
    is_gas  = archetype in ("gas_giant", "ice_giant")
    has_atm = bool(
        not source_flags["has_no_atmosphere"]
        and (
            source_flags["has_atmosphere_block"]
            or (atm_info and atm_info.get("pressure", 0) > 0)
        )
    )
    comp_dict    = atm_info.get("comp", {}) if atm_info else {}
    atm_pressure = atm_info.get("pressure", 0) if atm_info else 0
    atm_block_for_mass = obj_data.get("Atmosphere", {}) if isinstance(obj_data.get("Atmosphere"), dict) else (atm_info or {})
    atm_mass_info = compute_atmosphere_mass_and_pressure(
        {
            "name": obj_name,
            "archetype": archetype,
            "mass_kg": mass_kg,
            "radius_m": radius_m,
            "raw_data": obj_data,
        },
        atm_block_for_mass,
        se_class or obj_data.get("Class", archetype),
    ) if has_atm and not is_star else {"pressure_atm": atm_pressure, "atmosphere_mass_kg": 0.0}
    effective_atm_pressure = atm_mass_info.get("pressure_atm", atm_pressure)
    if atm_info is not None and effective_atm_pressure and effective_atm_pressure != atm_pressure:
        atm_info = dict(atm_info)
        atm_info["pressure"] = effective_atm_pressure
        atm_pressure = effective_atm_pressure
    ui_color     = get_ui_color(archetype, is_star, has_life, se_class, comp_dict,
                                est_temp or teff, has_ocean, atm_pressure,
                                has_exotic_life=has_exotic_life,
                                has_organic_life=has_organic_life,
                                radius_m=radius_m, mass_kg=mass_kg)

    ocean_block = obj_data.get("Ocean", {})
    water_hint_key = preset_info.get("water_color_hint")
    water_appearance = compute_water_color(
        ocean_block if isinstance(ocean_block, dict) else {},
        preset_info, life_info, {"est_temp": est_temp},
    )
    water_color = water_appearance["color"]
    water_reason = water_appearance["reason"]
    
    # ── CLOUD ANALYSIS ────────────────────────────────────────────────────────
    use_clouds = bool(
        has_atm
        and not source_flags["has_no_clouds"]
        and (use_clouds or source_flags["has_clouds_block"] or preset_flags.get("expect_clouds"))
    )
    cloud_result  = analyse_cloud_layers(obj_data if obj_data else {}, archetype) if not is_gas else {
        "cloud_set_a": 0, "cloud_set_b": 0, "custom_appearance": False,
        "coverage": 0.0, "opacity": 0.0, "color_rgba": _WHITE,
    }
    _cloud_set_a  = cloud_result["cloud_set_a"]
    _cloud_set_b  = cloud_result["cloud_set_b"]
    _custom_cloud = cloud_result["custom_appearance"]
    cloud_cov     = cloud_result["coverage"] if use_clouds else 0.0
    _cloud_opacity= cloud_result["opacity"]  if use_clouds else 0.0
    _cloud_color  = cloud_result["color_rgba"]

    palette_key, water_hint = _palette_from_preset(surface_preset)

    if palette_key is None:
        palette_key = _ARCHETYPE_DEFAULT_PALETTE.get(archetype, "barren")
    preset_palette = preset_info.get("surface_palette")
    if (water_hint or preset_flags.get("allow_water")) and not source_flags["has_no_ocean"]:
        use_water = True
    if source_flags["has_no_ocean"]:
        use_water = False
        water_reason = "NoOcean true overrides seaLevel and DiffMapAlpha"
    if is_gas:
        use_water = False
        water_reason = "gas/ice giant uses atmospheric banding, not terrestrial water"
    se_palette = _extract_space_engine_palette(obj_data)

    # ── COLOUR SELECTION ───────────────────────────────────────────────────
    if is_gas:
        planet_colors = [_BLACK, _BLACK, _BLACK]
        raw_class = obj_data.get("Class", "") if obj_data else ""
        is_bd, _, _ = detect_brown_dwarf_type(raw_class, est_temp if est_temp else teff, obj_data or {})
        preset_lo = (surface_preset or "").lower()
        if is_bd or ("jupiter" in preset_lo or "neptune" in preset_lo):
            gas_colors = list(_generate_gas_giant_palette_from_preset(
                surface_preset or "", mass_kg, teff,
                atm_info.get("hue") if atm_info else None,
                atm_info.get("saturation") if atm_info else None,
                has_life=has_life, has_aerial_life=has_aerial_life,
                has_organic_life=has_organic_life, has_exotic_life=has_exotic_life,
                dist_au=dist_au, star_teff=star_teff,
                is_brown_dwarf=is_bd, raw_class=raw_class))
        else:
            gas_colors = list(_pick_gas_palette(archetype, mass_kg, teff, atm_info,
                                                has_life=has_life, has_aerial_life=has_aerial_life,
                                                has_organic_life=has_organic_life,
                                                has_exotic_life=has_exotic_life,
                                                dist_au=dist_au, star_teff=star_teff))
    elif is_star:
        planet_colors = []; gas_colors = []
    else:
        if se_palette:
            planet_colors = list(se_palette)
            log_debug(
                f"[palette] Body='{obj_name}' source='Space Engine palette' "
                f"colors={len(se_palette)} first='{se_palette[0]}'",
                "PALETTE",
            )
        else:
            planet_colors = list(preset_palette or _PLANET_PALETTES.get(palette_key, _PLANET_PALETTES.get("barren", [_WHITE]*3)))
            log_debug(
                f"[palette] Body='{obj_name}' source='preset/archetype' "
                f"key='{palette_key}' colors={len(planet_colors)} first='{planet_colors[0] if planet_colors else ''}'",
                "PALETTE",
            )
        gas_colors = []

    # ── DENSITY ────────────────────────────────────────────────────────────
    if radius_m > 0:
        vol_m3      = (4.0/3.0) * math.pi * (radius_m**3)
        density_kgm = mass_kg / vol_m3 if vol_m3 > 0 else 5514.0
    else:
        density_kgm = 5514.0

    # ── CELESTIAL COMPONENT ────────────────────────────────────────────────
    # Derive correct StarType and Category from SE class before building Celestial.
    if is_star:
        _stl_cls  = classify_spaceengine_stellar_body(obj_data)
        _star_type_val = _stl_cls["star_type"]
        _category_val  = _stl_cls["category"]
        log_debug(
            f"[star-type] Body='{obj_data.get('Name', obj_name)}' "
            f"decl='{obj_data.get('_decl_type', obj_data.get('decl_type', ''))}' "
            f"class='{obj_data.get('Class', '')}' "
            f"token='{_first_spectral_token(obj_data.get('Class', ''))}' "
            f"category={_category_val} star_type={_star_type_val} "
            f"kind='{_stl_cls['kind']}' reason='{_stl_cls['reason']}'",
            "STAR_TYPE",
        )
    else:
        _star_type_val = 0
        _category_val  = 3

    celestial = {
        "$type": "Celestial",
        "SurfaceTemperatureOverride": float(teff if is_star else 0.0),
        "AtmosphereMass": 0.0,
        "MeanMolecularWeightDryAir": 28.97,
        "DegreesOfFreedom": 5,
        "AtmosphereHeightMultiplier": 1,
        "UseSimulatedEmissivity": False,
        "EmissivityIR": 0.78,
        "AtmosphereLayers": 1,
        "Luminosity": float(lum_watts if is_star else 0.0),
        "ColdStar": False, "Realistic": True, "CanBeRealistic": True,
        "StarType": _star_type_val,
        "Category": _category_val,
        "FluxFromStars": 0.0,
        "MagneticField": float(mag_field),
        "MagPoleAngle":  float(mag_pole_angle),
        "MagPoleAxis":   f"({rot_axis_str.split(';')[0]}, {rot_axis_str.split(';')[1]}, {rot_axis_str.split(';')[2]})",
        "Viscosity": 10000000000000.0, "Cohesion": 4836916.33967568, "CohesionMode": 0,
        "Deformation": "1.0;0.0;0.0;0.0;1.0;0.0;0.0;0.0;1.0", "VisualDeformationScale": 1.0,
        "NeedsStabilization": False,
        "SmoothLuminosityToIsochrone": False, "SmoothRadiusToIsochrone": False,
        "SmoothTemperatureToIsochrone": False,
        "SmoothLuminosityLastOutputValue": 0.0, "SmoothRadiusLastOutputValue": 0.0,
        "SmoothTemperatureLastOutputValue": 0.0,
    }
    if has_atm and not is_star:
        celestial.update({
            "AtmosphereMass":             float(atm_mass_info["atmosphere_mass_kg"]),
            "MeanMolecularWeightDryAir":  float(_calc_mean_mw(comp_dict)),
            "DegreesOfFreedom":           int(_calc_dof(comp_dict)),
            "UseSimulatedEmissivity":     True,
        })

    # ── BANDING / SCATTERING (gas giants) ─────────────────────────────────
    banding_offsets = "0;0;0;0"
    gas_giant_app   = {}
    if is_gas:
        banding_offsets = calculate_banding_offsets_proper(obliquity_deg, eq_asc_node_deg, rot_axis)
        se_cloud_data = obj_data.get("Surface", {}) if obj_data else {}
        band_params   = map_gas_giant_bands(se_cloud_data, surface_preset)
        atm_model     = atm_info.get("model", "jupiter") if atm_info else "jupiter"
        bright_val    = safe_float(atm_info.get("Brightness", 5.0)) if atm_info else 5.0
        sat_val       = safe_float(atm_info.get("Saturation", 1.0)) if atm_info else 1.0
        hue_val       = safe_float(atm_info.get("Hue",        0.0)) if atm_info else 0.0
        sc_rgba        = calculate_scattering_color(atm_model, bright_val, sat_val, hue_val)
        sc_str         = f"RGBA({sc_rgba[0]:.3f}, {sc_rgba[1]:.3f}, {sc_rgba[2]:.3f}, {sc_rgba[3]:.3f})"
        gas_giant_app  = {
            "Contrast": 0, "Colors": gas_colors,
            "UserChangedColors": bool(gas_colors),
            "originalColors": gas_colors, "customColors": gas_colors,
            "BandingOffsets": banding_offsets,
            "BandCount":       band_params.get("BandCount", 12),
            "ColorVariance":   band_params.get("ColorVariance", 0.5),
            "BandTurbulence":  band_params.get("BandTurbulence", 2),
            "StormFrequency":  band_params.get("StormFrequency", 0.5),
            "StormDensity":    band_params.get("StormDensity",   0.3),
            "SpotRadius":      band_params.get("SpotRadius",     0.8),
            "SpotBrightness":  band_params.get("SpotBrightness", 1.0),
            "ScatteringColor": sc_str,
            "originalScatteringColor": sc_str, "customScatteringColor": sc_str,
        }

    # ── HEIGHTMAPS ─────────────────────────────────────────────────────────
    hm_context = {
        "id": obj_id,
        "name": obj_name,
        "archetype": archetype,
        "se_class": se_class,
        "surface_preset": surface_preset,
        "diffmap": diffmap,
        "radius_m": radius_m,
        "mass_kg": mass_kg,
        "est_temp": est_temp,
        "has_life": has_life,
        "has_ocean": has_ocean,
        "use_water": use_water,
        "atm_info": atm_info or {},
        "raw_data": obj_data,
    }
    hm_result = _pick_visual_heightmaps(archetype, obj_id, obj_data, surface_preset, hm_context)
    if len(hm_result) == 12:
        hm0,nm0,hm1,nm1,mix0,mix1,off0,off1,fh0,fv0,fh1,fv1 = hm_result
    else:
        hm0=nm0=hm1=nm1=""; mix0=mix1=1.0; off0=off1=0.0; fh0=fv0=fh1=fv1=False

    raw_surface = obj_data.get("Surface", {}) if isinstance(obj_data.get("Surface"), dict) else {}
    raw_ocean = obj_data.get("Ocean", {}) if isinstance(obj_data.get("Ocean"), dict) else {}
    source_sea_level = max(0.0, safe_float(raw_surface.get("seaLevel", 0.0)))
    source_ocean_depth_km = max(0.0, safe_float(raw_ocean.get("Depth", 0.0)))
    is_lacustrine = bool(
        not source_flags["has_no_ocean"]
        and source_ocean_depth_km > 0.0
        and source_ocean_depth_km < 1.0
        and source_sea_level <= 0.05
    )
    if is_lacustrine:
        mix0 = min(mix0, 0.35)
        mix1 = min(mix1, 0.20)
        log_debug(
            f"[heightmap] Body='{obj_name}' mode='lacustrine' overlay_mix0={mix0:.3f} "
            f"overlay_mix1={mix1:.3f} source_depth_km={source_ocean_depth_km:.6g}",
            "HEIGHTMAP",
        )

    # ── PLANET APPEARANCE ──────────────────────────────────────────────────
    # Ice/snow are visual appearance toggles. Preset/source intent can enable
    # them even when the physical material ice map remains temperature-limited.
    surf_params_for_ice = obj_data.get("Surface", {}) if isinstance(obj_data.get("Surface"), dict) else {}
    _cold = (est_temp is not None and est_temp < 260.0) or archetype == "ice"
    source_ice_hint = (
        safe_float(surf_params_for_ice.get("icecapHeight", 0.0)) > 0.0
        or safe_float(surf_params_for_ice.get("icecapLatitude", 2.0)) < 1.0
        or safe_float(surf_params_for_ice.get("snowLevel", 2.0)) < 1.0
    )
    use_ice = bool(
        archetype == "ice"
        or preset_flags.get("use_ice")
        or preset_flags.get("use_snow")
        or source_ice_hint
        or (_cold and archetype in ("rocky", "ocean", "terra"))
    )

    # Clouds: on when there is enough atmosphere
    _atm_pressure = atm_info.get("pressure", 0) if atm_info else 0
    use_clouds    = bool(
        (has_atm or _atm_pressure > 0.01)
        and not source_flags["has_no_clouds"]
        and (use_clouds or source_flags["has_clouds_block"] or preset_flags.get("expect_clouds"))
    )
    atmosphere_app = compute_atmosphere_appearance(
        obj_name, atm_info or {}, obj_data or {}, preset_info, has_atm,
        celestial.get("AtmosphereMass", 0.0), archetype, life_info,
    )
    if is_gas:
        cloud_result = {
            "cloud_set_a": 0, "cloud_set_b": 0, "custom_appearance": False,
            "coverage": 0.0, "opacity": 0.0, "color_rgba": _WHITE,
            "speed_fields": {},
        }
    else:
        cloud_result = compute_cloud_appearance(
            obj_name, obj_data if obj_data else {}, atm_info or {}, preset_info,
            atmosphere_app["show"], use_clouds, archetype,
            atmosphere_app["opacity"], life_info,
        )
    _cloud_set_a  = cloud_result["cloud_set_a"]
    _cloud_set_b  = cloud_result["cloud_set_b"]
    _custom_cloud = cloud_result["custom_appearance"]
    cloud_cov     = cloud_result["coverage"]
    _cloud_opacity= cloud_result["opacity"]
    _cloud_color  = cloud_result["color_rgba"]
    _cloud_speed_fields = cloud_result["speed_fields"]
    atmosphere_color = atmosphere_app["color"]
    haze_type = atmosphere_app["haze"]

    plant_color = preset_info.get("plant_color")
    vegetation_app = compute_vegetation_appearance(life_info, preset_info, se_class, archetype)
    veg_color = vegetation_app["color"]
    use_vegetation = vegetation_app["enabled"] and not is_gas
    log_debug(
        f"[water] Body='{obj_name}' enabled={use_water} color='{water_color}' reason='{water_reason}'",
        "WATER",
    )
    log_debug(
        f"[vegetation] Body='{obj_name}' enabled={use_vegetation} color='{veg_color}' "
        f"reason='{vegetation_app['reason']}'",
        "VEGETATION",
    )

    city_settings = _city_light_settings(
        obj_id, obj_name, obj_data, preset_info,
        has_life, has_organic_life, has_aerial_life,
        has_atm, is_gas, is_star, life_info,
    )
    city_light_source = city_settings["source"]
    _city_seed = city_settings["seed"]
    ice_color_key = preset_info.get("ice_color_hint") or "default"
    ice_color = ICE_SNOW_COLORS.get(ice_color_key, ICE_SNOW_COLORS["default"])

    planet_app = {} if is_gas else {
        # Surface colours
        "Colors":            planet_colors,
        "UserChangedColors": bool(planet_colors),
        "originalColors":    planet_colors,
        "customColors":      planet_colors,
        "Contrast":          0.03,

        # Water
        "UseWater":          use_water,
        "WaterColorMode":    1,
        "WaterColor":        water_color,
        "originalWaterColor": water_color,
        "customWaterColor":  water_color,

        # Ice / snow
        "UseIce":            use_ice,
        "IceColorMode":      1,
        "IceColor":          ice_color,
        "originalIceColor":  ice_color,
        "customIceColor":    ice_color,
        "IceNoise":          0.5,
        "IceOpacityNoise":   1,
        "UseSnow":           use_ice,
        "SnowColorMode":     1,
        "SnowColor":         ice_color,
        "originalSnowColor": ice_color,
        "customSnowColor":   ice_color,
        "SnowNoise":         0.5,

        # Vegetation
        "UseVegetation":           use_vegetation,
        "VegetationColorMode":     1,
        "VegetationColor":         veg_color,
        "originalVegetationColor": veg_color,
        "customVegetationColor":   veg_color,
        "VegetationMode":          vegetation_app["mode"],
        "VegetationHabitabilityMode": 1,

        # Atmosphere
        "ShowAtmosphere":          atmosphere_app["show"],
        "ShowAtmosphereClouds":    cloud_result["show"],
        "AtmosphereSimulationMode": 0,
        "AtmosphereColorMode":     1,
        "AtmosphereColor":         atmosphere_color,
        "originalAtmosphereColor": atmosphere_color,
        "customAtmosphereColor":   atmosphere_color,
        "customAtmosphereOpacity": atmosphere_app["opacity"],
        "RayleighSimulationMode":  0,
        "RayleighScatteringStrength":        atmosphere_app["rayleigh"],
        "DefaultRayleighScatteringStrength": atmosphere_app["rayleigh"],
        "HazeType":                haze_type,
        "UseDynamicEmissive":      city_settings["enabled"],

        # Clouds
        "CloudColorMode":    1,
        "CloudColor":        _cloud_color,
        "originalCloudColor": _cloud_color,
        "customCloudColor":  _cloud_color,
        "CustomCloudAppearance": _custom_cloud,
        "CloudSetA":         _cloud_set_a,
        "CloudSetB":         _cloud_set_b,
        "CloudCoverage":     cloud_cov,
        "CloudOpacity":      _cloud_opacity,
        "CloudOpacitySimulationMode": 0,
        "CloudSpeedSimulationMode":   0,
        "cloudSpeedAtEquatorA": _cloud_speed_fields["cloudSpeedAtEquatorA"],
        "cloudSpeedAtEquatorB": _cloud_speed_fields["cloudSpeedAtEquatorB"],
        "bandRotationA":     _cloud_speed_fields["bandRotationA"],
        "bandRotationB":     _cloud_speed_fields["bandRotationB"],
        "poleRotationA":     _cloud_speed_fields["poleRotationA"],
        "poleRotationB":     _cloud_speed_fields["poleRotationB"],

        # City lights
        "CityLightSource":   city_light_source,
        "CityLightMode":     0,
        "CityLightSeed":     _city_seed,
        "CityLightsHabitibilityMode": 0,
        "CityLightsColorMode":   0,
        "CityLightsColor":       "RGBA(1.000, 0.850, 0.400, 1.000)",
        "originalCityLightsColor": "RGBA(1.000, 0.850, 0.400, 1.000)",
        "customCityLightsColor": "RGBA(1.000, 0.850, 0.400, 1.000)",
        "CityLightsBrightness":  city_settings["brightness"],
        "CityLightsBrightnessThresholds": "(100.00, 10.00)",
    }
    if not is_gas and not is_star:
        log_debug(
            f"[appearance] Body='{obj_name}' UseWater={use_water} UseIce={use_ice} "
            f"UseSnow={use_ice} UseVegetation={use_vegetation} "
            f"water='{water_hint_key or 'default'}' vegetation='{plant_color or 'default'}' "
            f"ice='{ice_color_key}'",
            "APPEARANCE",
        )

    appearance_component = {
        "$type": "AppearanceComponent",
        "PrefabSource": "",
        "ColorMapSource": "",  "IceMapSource": "",
        "HeightMapSource":  "" if is_gas else hm0,
        "NormalMapSource":  "" if is_gas else nm0,
        "HeightMapSource2": "" if is_gas else hm1,
        "NormalMapSource2": "" if is_gas else nm1,
        "UseDiffuse": True,
        "EmissiveMapSource": "" if is_star else city_settings["emissive"],
        "SpecularMapSource": "", "VegetationMapSource": "",
        "UseNormals": bool(nm0) and not is_gas, "NormalMapStrength": 1,
        "UseHeightMap0": bool(hm0) and not is_gas, "HeightMapMix0": float(mix0),
        "HeightMapOffset0": float(off0), "HeightMapFlipH0": bool(fh0), "HeightMapFlipV0": bool(fv0),
        "UseHeightMap1": bool(hm1) and not is_gas, "HeightMapMix1": float(mix1),
        "HeightMapOffset1": float(off1), "HeightMapFlipH1": bool(fh1), "HeightMapFlipV1": bool(fv1),
        "LightColor": "RGBA(0.000, 0.000, 0.000, 0.000)",
        "Tint": "RGBA(1.000, 1.000, 1.000, 1.000)",
        "Planet":    {} if is_gas else planet_app,
        "GasGiant":  gas_giant_app if is_gas else {},
        "BlackHole": {"Color":_TRANS,"ColorMode":0,"originalColor":_TRANS,"customColor":_TRANS},
        "Prefab":    {"Color":_WHITE,"ColorMode":0,"originalColor":_WHITE,"customColor":_TRANS,
                      "Metallic":0,"Smoothness":0.5},
    }
    planet_for_validation = appearance_component.get("Planet", {})
    if planet_for_validation:
        if not ALLOW_CITY_LIGHT_SOURCE_0 and planet_for_validation.get("CityLightSource") == 0:
            planet_for_validation["CityLightSource"] = PROCEDURAL_CITY_LIGHT_SOURCE or 1
        if not city_settings["enabled"]:
            appearance_component["EmissiveMapSource"] = ""
            planet_for_validation["UseDynamicEmissive"] = False
            planet_for_validation["CityLightSource"] = PROCEDURAL_CITY_LIGHT_SOURCE or 1
            planet_for_validation["CityLightSeed"] = 0
            planet_for_validation["CityLightsBrightness"] = 0
        if source_flags["has_no_atmosphere"]:
            planet_for_validation["ShowAtmosphere"] = False
            planet_for_validation["ShowAtmosphereClouds"] = False
            planet_for_validation["CloudOpacity"] = 0.0
            planet_for_validation["CloudCoverage"] = 0.0
            planet_for_validation["customAtmosphereOpacity"] = 0.0
        if source_flags["has_no_clouds"]:
            planet_for_validation["ShowAtmosphereClouds"] = False
            planet_for_validation["CloudOpacity"] = 0.0
            planet_for_validation["CloudCoverage"] = 0.0

    components = [celestial, appearance_component]
    depots = build_depots(archetype, mass_kg, atm_info, has_ocean, radius_m, sea_level, obj_data)
    components.append({
        "$type": "CompositionComponent",
        "targetRadius": float(max(radius_m, 1.0)),
        "SimulateRadius": False, "RadiusTuningFactor": 1,
        "depots": depots,
    })

    attach_surface_grid = _attach_surface_grid_runtime()
    if attach_surface_grid and atlas_index is not None and not is_gas:
        surf_params = obj_data.get("Surface", {}) if isinstance(obj_data, dict) else {}
        if not isinstance(surf_params, dict):
            surf_params = {}
        bump_h = safe_float(surf_params.get("BumpHeight", 10.0), 10.0)
        components.append(_build_surface_grid_component(
            int(atlas_index), radius_m, bump_h))
    else:
        components.append({
            "$type": "SurfaceGridComponent",
            "AtlasIndex": -1,
            "ElevationToRadiusRatio": 0.0,
        })
        if atlas_index is not None and not is_gas:
            log_debug(
                f"[surface-mode] Body='{obj_name}' AtlasIndex=-1 "
                "SurfaceGridComponent='disabled' reason='surface preview is not attached'",
                "SURFACE_MODE",
            )

    if est_temp is None:
        est_temp = teff if is_star else _ARCHETYPE_DEFAULT_TEMPS.get(archetype, 280.0)
    components.append({
        "$type": "HeatComponent",
        "SurfaceTemperature": float(est_temp), "StartingTemperature": float(est_temp),
        "TemperatureInitialized": True, "Albedo": 0.3,
        "SurfaceHeatCapacity": 0, "SpecificHeatCapacity": 0,
        "UserChangedSurfaceHeatCapacity": False,
        "OverrideStartingTemp": not is_star,
        "BlackbodyColorMode": 0,
        "originalBlackbodyColor": "RGBA(0.000, 0.000, 0.000, 1.000)",
        "customBlackbodyColor":   "RGBA(0.000, 0.000, 0.000, 0.000)",
        "EmitsLight": is_star, "BlackbodyNoise": 1, "UseBlackbodyNoise": False,
        "LuminanceMode": 0, "CustomLuminosity": float(lum_watts if is_star else 0.0),
    })
    if is_star:
        components.append({"$type":"LightComponent","Fade":1})

    entity = {
        "Header": {"BaseType":"Object","AssetType":"JSON","TypeName":"Body",
                   "BuildRevision":"46923","LastModifiedUTC":_now_utc()},
        "$type": "Body",
        "Id":               int(obj_id),
        "HorizonID":        "",
        "Age":              float(age),
        "Name":             str(obj_name),
        "Category":         str(category),
        "Mass":             float(mass_kg),
        "Radius":           float(max(radius_m, 1.0)),
        "GravityRadius":    0,
        "Density":          float(density_kgm),
        "Generation":       0,
        "Flags":            371, "DisplayFlags": 3,
        "PhysicsMass":      float(mass_kg),
        "ColorPalette":     2,
        "Position":         pos_str, "Velocity": vel_str,
        "Orientation":      quat_str, "RotationAxis": rot_axis_str, "AngularVelocity": ang_vel_str,
        "Color":            ui_color, "DefaultGUIColor": ui_color,
        "CustomColor":      "RGBA(0.000, 0.000, 0.000, 0.000)",
        "CustomGUIColor":   "RGBA(0.000, 0.000, 0.000, 0.000)",
        "UserChangedColor": False, "UserChangedGUIColor": False,
        "ColorMode": 0, "GUIColorMode": 0,
        "PullOthers": True, "PulledByOthers": True, "Collision": True,
        "Suspended": False, "LockPosition": False, "LockRotation": False, "LockDeformation": False,
        "Components": components,
    }
    if parent_id != -1:
        entity["Parent"] = parent_id
        entity["RelativeTo"] = relative_to_id if relative_to_id != -1 else parent_id
        entity["CustomOrbitParentId"] = parent_id
    else:
        entity["Parent"] = -1
        entity["RelativeTo"] = relative_to_id if relative_to_id != -1 else obj_id
    entity.update({
        "Source": -1, "Group": 0, "Origin": 0, "BudgetType": 0,
        "InMajorCollision": False, "NonSphericalGravityEnabled": False, "J2": 0.0,
        "DatabaseID": "00000000-0000-0000-0000-000000000000",
        "Description": None, "LockedProperties": [],
    })
    if archetype and archetype.lower() in ("asteroid", "comet"):
        entity["CustomOrbitParentId"] = -1
        entity["RelativeTo"] = relative_to_id if relative_to_id != -1 else obj_id

    apply_source_flags(entity, {
        "raw_data": obj_data or {},
        "archetype": archetype,
        "mass_kg": mass_kg,
        "radius_m": radius_m,
        "atm_info": atm_info or {},
    })

    final_atmo = obj_data.get("Atmosphere", {}) if isinstance(obj_data.get("Atmosphere"), dict) else {}
    if final_atmo and not is_star and not is_gas:
        atmosphere_mass = compute_atmosphere_mass_and_pressure(
            {
                "name": obj_name,
                "archetype": archetype,
                "mass_kg": mass_kg,
                "radius_m": radius_m,
                "raw_data": obj_data,
            },
            final_atmo,
            se_class or obj_data.get("Class", archetype),
        )["atmosphere_mass_kg"]
        surface_depot_names = active_ocean_depot_names(obj_data)
        try:
            import globals_compat as runtime
            strict_atmosphere = getattr(
                runtime, "STRICT_ATMOSPHERE_MASS_CONSISTENCY",
                _const.STRICT_ATMOSPHERE_MASS_CONSISTENCY,
            )
        except ImportError:
            strict_atmosphere = _const.STRICT_ATMOSPHERE_MASS_CONSISTENCY
        normalization_report = obj_data.get("_atmosphere_normalization_report", {})
        enforce_atmosphere_depot_consistency(
            entity,
            final_atmo,
            atmosphere_mass,
            {
                "include_water_vapor": "Water" not in surface_depot_names,
                "surface_depot_names": surface_depot_names,
                "strict": strict_atmosphere,
            },
            normalization_report,
        )
        enforce_static_surface_volatile_safety(
            entity,
            obj_data,
            atmosphere_mass,
            report=normalization_report,
        )

    force_visual_state_initialization(entity, {
        "raw_data": obj_data or {},
        "archetype": archetype,
    })

    return entity, quat_str, rot_axis_str, ang_vel_str, obliquity_deg, eq_asc_node_deg


# ─────────────────────────────────────────────────────────────────────────────
# AGE PROPAGATION
# ─────────────────────────────────────────────────────────────────────────────

def apply_system_age(us_obj: dict, se_obj: dict, se_class: str) -> None:
    if type(se_obj) is dict and "Age" in se_obj:
        try:
            age_s = float(se_obj["Age"]) * GYR_TO_SECONDS
            us_obj["Age"] = age_s
            for c in us_obj.get("Components", []):
                if c["$type"] == "Celestial":
                    c["Age"] = age_s
            if type(se_class) is str:
                cl = se_class.lower()
                if any(x in cl for x in ("star","blackhole","neutronstar","whitedwarf")):
                    _const.SYSTEM_AGE_SECONDS = age_s
        except (ValueError, TypeError, AttributeError):
            pass
    elif _const.SYSTEM_AGE_SECONDS is not None:
        us_obj["Age"] = _const.SYSTEM_AGE_SECONDS
        for c in us_obj.get("Components", []):
            if c["$type"] == "Celestial":
                c["Age"] = _const.SYSTEM_AGE_SECONDS


# ─────────────────────────────────────────────────────────────────────────────
# BACK-EXPORT (US → SE)
# ─────────────────────────────────────────────────────────────────────────────

def calc_orbital_elements(obj_pos, obj_vel, par_pos, par_vel, par_mass) -> dict:
    r  = [obj_pos[i]-par_pos[i] for i in range(3)]
    v  = [obj_vel[i]-par_vel[i] for i in range(3)]
    mu = GRAVITATIONAL_CONSTANT * par_mass
    rm = math.sqrt(sum(x**2 for x in r)); vm = math.sqrt(sum(x**2 for x in v))
    if rm == 0 or par_mass == 0:
        return {"sma":0,"ecc":0,"inc":0,"asc":0,"arg":0,"mean":0}
    hx=r[1]*v[2]-r[2]*v[1]; hy=r[2]*v[0]-r[0]*v[2]; hz=r[0]*v[1]-r[1]*v[0]
    hm = math.sqrt(hx**2+hy**2+hz**2)
    if hm == 0: return {"sma":0,"ecc":0,"inc":0,"asc":0,"arg":0,"mean":0}
    inc = math.degrees(math.acos(max(-1.,min(1.,hz/hm))))
    ex=(v[1]*hz-v[2]*hy)/mu-r[0]/rm; ey=(v[2]*hx-v[0]*hz)/mu-r[1]/rm; ez=(v[0]*hy-v[1]*hx)/mu-r[2]/rm
    em = math.sqrt(ex**2+ey**2+ez**2)
    nx=-hy; ny=hx; nm=math.sqrt(nx**2+ny**2)
    asc = 0.0
    if nm:
        asc = math.degrees(math.acos(max(-1.,min(1.,nx/nm))))
        if ny < 0: asc = 360. - asc
    arg = 0.0
    if nm and em:
        arg = math.degrees(math.acos(max(-1.,min(1.,(nx*ex+ny*ey)/(nm*em)))))
        if ez < 0: arg = 360. - arg
    mean = 0.0
    if em:
        dot = ex*r[0]+ey*r[1]+ez*r[2]
        nu  = math.acos(max(-1.,min(1.,dot/(em*rm))))
        if sum(r[i]*v[i] for i in range(3)) < 0: nu = 2*math.pi-nu
        if em < 1:
            try:
                E    = 2*math.atan(math.tan(nu/2)/math.sqrt((1+em)/(abs(1-em)+1e-9)))
                mean = math.degrees(E-em*math.sin(E))
            except Exception: mean = math.degrees(nu)
        else: mean = math.degrees(nu)
    en  = vm**2/2-mu/rm
    sma = (-mu/(2*en)) if en else 0.0
    return {"sma":sma/AU_TO_METERS,"ecc":em,"inc":inc,"asc":asc,"arg":arg,"mean":mean}


def rotation_axis_to_se(av_str: str, ra_str: str) -> tuple:
    av_us = parse_vec3(av_str); ra_us = parse_vec3(ra_str)
    av    = us_to_eci(av_us);    ra    = us_to_eci(ra_us)
    omega = math.sqrt(sum(x**2 for x in av))
    if omega < 1e-20: return 0., 0., 0.
    rp = (2*math.pi/omega)/3_600.
    if sum(av[i]*ra[i] for i in range(3)) < 0: rp = -rp
    rz  = max(-1.,min(1.,ra[2]))
    obl = math.degrees(math.acos(rz))
    ean = math.degrees(math.atan2(ra[1],ra[0]))
    if ean < 0: ean += 360.
    return rp, obl, ean