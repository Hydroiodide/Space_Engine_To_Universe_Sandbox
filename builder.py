"""
builder.py
Universe Sandbox JSON entity assembly.  Generates Body dicts, ring particles,
atmosphere components, depot compositions, and the .ubox archive.
No imports from converter or main.
"""

import math
import colorsys
import re
import random

from constants import (
    log_debug, safe_float, _now_utc,
    GRAVITATIONAL_CONSTANT, EARTH_MASS_KG, EARTH_RADIUS_M,
    AU_TO_METERS, AU_TO_KM, TOKEN_MASS, GYR_TO_SECONDS,
    SE_TO_US_DEPOT, US_ATM_DEPOT_KEYS,
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


def _named_texture_key(diffmap: str):
    d = diffmap.lower()
    for k in _NAMED_BODY_TEXTURES:
        if k in d:
            return k
    return None


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
    if has_exotic_life and force_exotic_magenta:
        return "RGBA(1.000, 0.588, 0.784, 1.000)"
    if (has_organic_life or (has_life and not has_exotic_life)) and force_organic_green:
        return "RGBA(0.275, 0.863, 0.275, 1.000)"
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
        return _WATER
    comp = ocean_data.get("Composition", {})
    if not comp:
        return _WATER
    if safe_float(comp.get("NH3", 0)) > 10:   return "RGBA(0.800, 0.700, 0.300, 1.000)"
    if safe_float(comp.get("SO2", 0)) > 5 or safe_float(comp.get("H2S", 0)) > 1:
        return "RGBA(0.500, 0.600, 0.200, 1.000)"
    if safe_float(comp.get("Cl2", 0)) > 0.01: return "RGBA(0.200, 0.700, 0.600, 1.000)"
    return "RGBA(0.100, 0.200, 0.500, 1.000)"


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
    cloud_blocks = raw.get("Clouds")
    clouds_list: list = []
    if isinstance(cloud_blocks, list):
        clouds_list = [c for c in cloud_blocks if isinstance(c, dict)]
    elif isinstance(cloud_blocks, dict):
        clouds_list = [cloud_blocks]

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

    layer_coverages = [safe_float(c.get("Coverage", 0.5)) for c in clouds_list]
    coverage = min(1.0, sum(layer_coverages))

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
        f"clouds archetype={archetype} layers={len(clouds_list)} "
        f"styleA={style_a}({result['cloud_set_a']}) styleB={style_b}({result['cloud_set_b']}) "
        f"cov={result['coverage']:.3f} op={result['opacity']:.3f} "
        f"cyclone={cyclone_magn:.2f} stripes={stripe_zones:.1f}", "CLOUD")
    return result


def classify_stellar_object(raw_class: str, teff: float, lum_watts: float,
                             radius_m: float, mass_kg: float) -> dict:
    from constants import (SE_NEUTRON_STAR_CLASSES, SE_WHITE_DWARF_CLASSES,
                           US_STAR_TYPE_MAIN_SEQUENCE, US_STAR_TYPE_NEUTRON)
    cs = str(raw_class or "").strip().upper()
    cs_nospace = cs.replace(" ", "")
    if cs == "X" or "BLACKHOLE" in cs_nospace or cs == "BH":
        return {"us_category":"blackhole","star_type":US_STAR_TYPE_MAIN_SEQUENCE,
                "lum_class":"REM","description":"Black hole","fallback":False,"fallback_reason":""}
    if cs in SE_NEUTRON_STAR_CLASSES or cs_nospace in SE_NEUTRON_STAR_CLASSES:
        return {"us_category":"star","star_type":US_STAR_TYPE_NEUTRON,
                "lum_class":"NS","description":"Neutron star","fallback":False,"fallback_reason":""}
    if cs in SE_WHITE_DWARF_CLASSES or cs_nospace in SE_WHITE_DWARF_CLASSES:
        return {"us_category":"star","star_type":US_STAR_TYPE_MAIN_SEQUENCE,
                "lum_class":"VII","description":"White dwarf","fallback":False,"fallback_reason":""}
    lum_class = "V"
    for pattern, lc in [(r'\bIAB\b',"Iab"),(r'\bIA\b',"Ia"),(r'\bIB\b',"Ib"),
                        (r'\bIII\b',"III"),(r'\bII\b',"II"),(r'\bIV\b',"IV"),
                        (r'\bVI\b',"VI"),(r'\bVII\b',"VII"),(r'\bI\b',"I")]:
        if re.search(pattern, cs): lum_class = lc; break
    if lum_class in ("Ia","Ib","Iab","I"):
        return {"us_category":"star","star_type":US_STAR_TYPE_MAIN_SEQUENCE,"lum_class":lum_class,
                "description":f"Supergiant ({lum_class})","fallback":False,"fallback_reason":""}
    if lum_class == "II":
        return {"us_category":"star","star_type":US_STAR_TYPE_MAIN_SEQUENCE,"lum_class":"II",
                "description":"Bright giant","fallback":False,"fallback_reason":""}
    if lum_class == "III":
        return {"us_category":"star","star_type":US_STAR_TYPE_MAIN_SEQUENCE,"lum_class":"III",
                "description":"Giant","fallback":False,"fallback_reason":""}
    if re.search(r'\b[LTY]\d', cs) or teff < 2500:
        return {"us_category":"star","star_type":US_STAR_TYPE_MAIN_SEQUENCE,"lum_class":"M",
                "description":"Red dwarf","fallback":False,"fallback_reason":""}
    return {"us_category":"star","star_type":US_STAR_TYPE_MAIN_SEQUENCE,"lum_class":"V",
            "description":"Main sequence","fallback":False,"fallback_reason":""}


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
    if pressure_atm <= 0 or mass_kg <= 0 or radius_m <= 0:
        return 0.0
    g = GRAVITATIONAL_CONSTANT * mass_kg / radius_m**2
    return max(0.0, 4 * math.pi * radius_m**2 * pressure_atm * 101_325.0 / g)


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


def build_depots(archetype, mass_kg, atm_info, has_ocean, radius_m, sea_level) -> dict:
    from constants import _builtin_db
    mat = _builtin_db()["materials"].get(archetype, _builtin_db()["materials"]["rocky"])
    acc = {mineral: mass_kg * fraction for mineral, fraction in mat.items() if fraction > 0}
    is_gas = archetype in ("gas_giant", "ice_giant", "star")
    if atm_info and atm_info.get("pressure", 0) > 0:
        atm_mass  = _calc_atmosphere_mass(radius_m, mass_kg, atm_info["pressure"])
        comp      = atm_info.get("comp", {})
        total_pct = sum(safe_float(v) for v in comp.values()) or 100.0
        for mol, pct_str in comp.items():
            pct      = safe_float(pct_str)
            gas_mass = max(TOKEN_MASS, (pct / total_pct) * atm_mass)
            if mol == "CO":
                acc["Carbon Dioxide"] = acc.get("Carbon Dioxide", 0) + gas_mass * 0.64
                acc["Oxygen"]         = acc.get("Oxygen",         0) + gas_mass * 0.36
            else:
                us_name = SE_TO_US_DEPOT.get(mol)
                if us_name:
                    acc[us_name] = acc.get(us_name, 0) + gas_mass
    if not is_gas:
        acc.pop("Water", None)
    if not is_gas and radius_m > 0:
        if has_ocean:
            cov  = min(1.0, sea_level) if sea_level > 0.01 else 0.65
            acc["Water"] = cov * 4 * math.pi * radius_m**2 * 3_700.0 * 1_025.0
        elif sea_level > 0.01:
            acc["Water"] = sea_level * 4 * math.pi * radius_m**2 * 200.0 * 1_025.0
    result = {key: {"Mass": float(max(0, acc.get(key, 0.0))), "LockSurfaceTracking": False}
              for key in US_ATM_DEPOT_KEYS}
    for key, val in acc.items():
        if key not in result:
            result[key] = {"Mass": float(max(0, val)), "LockSurfaceTracking": False}
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
        "Name":  f"{parent_name} Ring Particle",
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

def _build_surface_grid_component(atlas_index: int,
                                  radius_m: float,
                                  bump_height: float = 10.0) -> dict:
    if atlas_index < 0 or atlas_index >= 256:
        raise ValueError(f"AtlasIndex {atlas_index} out of range [0, 255]")
    elevation_ratio = min(bump_height / max(radius_m, 1.0), 0.05)
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

    is_gas  = archetype in ("gas_giant", "ice_giant")
    has_atm = bool(atm_info and atm_info.get("pressure", 0) > 0)
    comp_dict    = atm_info.get("comp", {}) if atm_info else {}
    atm_pressure = atm_info.get("pressure", 0) if atm_info else 0
    ui_color     = get_ui_color(archetype, is_star, has_life, se_class, comp_dict,
                                est_temp or teff, has_ocean, atm_pressure,
                                has_exotic_life=has_exotic_life,
                                has_organic_life=has_organic_life,
                                radius_m=radius_m, mass_kg=mass_kg)

    ocean_block = obj_data.get("Ocean", {})
    water_color = derive_water_color_from_ocean(ocean_block) if isinstance(ocean_block, dict) else _WATER
    
    # ── CLOUD ANALYSIS ────────────────────────────────────────────────────────
    cloud_result  = analyse_cloud_layers(obj_data if obj_data else {}, archetype)
    _cloud_set_a  = cloud_result["cloud_set_a"]
    _cloud_set_b  = cloud_result["cloud_set_b"]
    _custom_cloud = cloud_result["custom_appearance"]
    cloud_cov     = cloud_result["coverage"] if use_clouds else 0.0
    _cloud_opacity= cloud_result["opacity"]  if use_clouds else 0.0
    _cloud_color  = cloud_result["color_rgba"]

    palette_key, water_hint = _palette_from_preset(surface_preset)

    if palette_key is None:
        palette_key = _ARCHETYPE_DEFAULT_PALETTE.get(archetype, "barren")
    if water_hint:
        use_water = True

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
        planet_colors = list(_PLANET_PALETTES.get(palette_key, _PLANET_PALETTES.get("barren", [_WHITE]*3)))
        gas_colors = []

    # ── DENSITY ────────────────────────────────────────────────────────────
    if radius_m > 0:
        vol_m3      = (4.0/3.0) * math.pi * (radius_m**3)
        density_kgm = mass_kg / vol_m3 if vol_m3 > 0 else 5514.0
    else:
        density_kgm = 5514.0

    # ── CELESTIAL COMPONENT ────────────────────────────────────────────────
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
        "StarType": 1 if is_star else 0,
        "Category": 2 if is_star else 3,
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
            "AtmosphereMass":             float(_calc_atmosphere_mass(radius_m, mass_kg, atm_info["pressure"])),
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
    hm_result = _pick_heightmaps(archetype, obj_id)
    if len(hm_result) == 12:
        hm0,nm0,hm1,nm1,mix0,mix1,off0,off1,fh0,fv0,fh1,fv1 = hm_result
    else:
        hm0=nm0=hm1=nm1=""; mix0=mix1=1.0; off0=off1=0.0; fh0=fv0=fh1=fv1=False

    atmosphere_color = get_atmosphere_color_from_se(atm_info, archetype, has_life)
    se_model  = atm_info.get("model", "") if atm_info else ""
    haze_type = get_haze_type_from_se_model(se_model)

    # ── PLANET APPEARANCE ──────────────────────────────────────────────────
    # Ice: on for ice archetype and cold worlds that could have polar caps
    _cold = (est_temp is not None and est_temp < 260.0) or archetype == "ice"
    use_ice = archetype == "ice" or (
        _cold and archetype in ("rocky","ocean","terra")
        and not (est_temp is not None and est_temp > 310.0)
    )

    # Clouds: on when there is enough atmosphere
    _atm_pressure = atm_info.get("pressure", 0) if atm_info else 0
    use_clouds    = has_atm or _atm_pressure > 0.01

    # Vegetation colour by life type
    if has_exotic_life:        veg_color = "RGBA(0.600, 0.050, 0.800, 1.000)"
    elif has_aerial_life:      veg_color = "RGBA(0.100, 0.750, 0.350, 1.000)"
    else:                      veg_color = "RGBA(0.150, 0.500, 0.150, 1.000)"

    # City lights: on for organic/aerial life with a breathable atmosphere
    _o2 = safe_float((atm_info.get("comp", {}) if atm_info else {}).get("O2", 0))
    city_light_source = 1 if (has_organic_life or has_aerial_life) and _o2 > 15.0 else 0

    import random as _rnd
    _city_seed = _rnd.Random(id(obj_name)).randint(0, 99999)

    planet_app = {} if is_gas else {
        # Surface colours
        "Colors":            planet_colors,
        "UserChangedColors": bool(planet_colors),
        "originalColors":    planet_colors,
        "customColors":      planet_colors,
        "Contrast":          0.03,

        # Water
        "UseWater":          use_water,
        "WaterColorMode":    0,
        "WaterColor":        water_color,
        "originalWaterColor": water_color,
        "customWaterColor":  water_color,

        # Ice / snow
        "UseIce":            use_ice,
        "IceColorMode":      0,
        "IceColor":          "RGBA(0.900, 0.900, 0.900, 1.000)",
        "originalIceColor":  "RGBA(0.900, 0.900, 0.900, 1.000)",
        "customIceColor":    "RGBA(0.900, 0.900, 0.900, 1.000)",
        "IceNoise":          0.5,
        "IceOpacityNoise":   1,
        "UseSnow":           use_ice,
        "SnowColorMode":     0,
        "SnowColor":         "RGBA(0.900, 0.900, 0.900, 1.000)",
        "originalSnowColor": "RGBA(0.900, 0.900, 0.900, 1.000)",
        "customSnowColor":   "RGBA(0.900, 0.900, 0.900, 1.000)",
        "SnowNoise":         0.5,

        # Vegetation
        "UseVegetation":           has_life,
        "VegetationColorMode":     0,
        "VegetationColor":         veg_color,
        "originalVegetationColor": veg_color,
        "customVegetationColor":   veg_color,
        "VegetationMode":          1,
        "VegetationHabitabilityMode": 1,

        # Atmosphere
        "ShowAtmosphere":          has_atm,
        "ShowAtmosphereClouds":    use_clouds,
        "AtmosphereSimulationMode": 0,
        "AtmosphereColorMode":     0,
        "AtmosphereColor":         atmosphere_color,
        "originalAtmosphereColor": atmosphere_color,
        "customAtmosphereColor":   atmosphere_color,
        "customAtmosphereOpacity": 0.2,
        "RayleighSimulationMode":  0,
        "RayleighScatteringStrength":        1,
        "DefaultRayleighScatteringStrength": 1,
        "HazeType":                haze_type,
        "UseDynamicEmissive":      True,

        # Clouds
        "CloudColorMode":    0,
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
        "cloudSpeedAtEquatorA": -10.0,
        "cloudSpeedAtEquatorB":  -8.0,
        "bandRotationA":     0.5,
        "bandRotationB":     0.5,
        "poleRotationA":     0.47,
        "poleRotationB":     0.89,

        # City lights
        "CityLightSource":   city_light_source,
        "CityLightMode":     0,
        "CityLightSeed":     _city_seed,
        "CityLightsHabitibilityMode": 0,
        "CityLightsColorMode":   0,
        "CityLightsColor":       "RGBA(1.000, 0.850, 0.400, 1.000)",
        "originalCityLightsColor": "RGBA(1.000, 0.850, 0.400, 1.000)",
        "customCityLightsColor": "RGBA(1.000, 0.850, 0.400, 1.000)",
        "CityLightsBrightness":  200,
        "CityLightsBrightnessThresholds": "(100.00, 10.00)",
    }

    appearance_component = {
        "$type": "AppearanceComponent",
        "PrefabSource": "",
        "ColorMapSource": "",  "IceMapSource": "",
        "HeightMapSource":  "" if is_gas else hm0,
        "NormalMapSource":  "" if is_gas else nm0,
        "HeightMapSource2": "" if is_gas else hm1,
        "NormalMapSource2": "" if is_gas else nm1,
        "UseDiffuse": True,
        "EmissiveMapSource": "Textures/planet_cities" if not is_star else "",
        "SpecularMapSource": "", "VegetationMapSource": "",
        "UseNormals": bool(nm0), "NormalMapStrength": 1,
        "UseHeightMap0": bool(hm0), "HeightMapMix0": float(mix0),
        "HeightMapOffset0": float(off0), "HeightMapFlipH0": bool(fh0), "HeightMapFlipV0": bool(fv0),
        "UseHeightMap1": bool(hm1), "HeightMapMix1": float(mix1),
        "HeightMapOffset1": float(off1), "HeightMapFlipH1": bool(fh1), "HeightMapFlipV1": bool(fv1),
        "LightColor": "RGBA(0.000, 0.000, 0.000, 0.000)",
        "Tint": "RGBA(1.000, 1.000, 1.000, 1.000)",
        "Planet":    {} if is_gas else planet_app,
        "GasGiant":  gas_giant_app if is_gas else {},
        "BlackHole": {"Color":_TRANS,"ColorMode":0,"originalColor":_TRANS,"customColor":_TRANS},
        "Prefab":    {"Color":_WHITE,"ColorMode":0,"originalColor":_WHITE,"customColor":_TRANS,
                      "Metallic":0,"Smoothness":0.5},
    }

    components = [celestial, appearance_component]
    depots = build_depots(archetype, mass_kg, atm_info, has_ocean, radius_m, sea_level)
    components.append({
        "$type": "CompositionComponent",
        "targetRadius": float(max(radius_m, 1.0)),
        "SimulateRadius": False, "RadiusTuningFactor": 1,
        "depots": depots,
    })

    if atlas_index is not None:
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
