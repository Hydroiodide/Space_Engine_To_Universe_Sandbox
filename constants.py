"""
constants.py
Physical constants, colour palettes, SE↔US lookup tables, and the
global logging setup used across all other modules.
No dependencies on the other four files in this package.
"""

import math
import colorsys
import random
import re
from datetime import datetime, timezone

# ─────────────────────────────────────────────────────────────────────────────
# GLOBAL DEBUG STATE
# ─────────────────────────────────────────────────────────────────────────────

DEBUG_MODE: bool = False
CONVERSION_LOG: list = []
LOG_CALLBACK = None


def log_debug(msg: str, level: str = "INFO") -> None:
    global CONVERSION_LOG, LOG_CALLBACK
    timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
    log_msg = f"[{timestamp}] [{level}] {msg}"
    CONVERSION_LOG.append(log_msg)
    if DEBUG_MODE:
        print(log_msg)
    if LOG_CALLBACK:
        LOG_CALLBACK(log_msg)


def set_log_callback(callback) -> None:
    global LOG_CALLBACK
    LOG_CALLBACK = callback


# ─────────────────────────────────────────────────────────────────────────────
# PHYSICAL CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

AU_TO_METERS:           float = 149_597_870_700.0
AU_TO_KM:               float = 149_597_870.7
EARTH_MASS_KG:          float = 5.97219e24
SOLAR_MASS_KG:          float = 1.98847e30
EARTH_RADIUS_M:         float = 6_371_000.0
SOLAR_RADIUS_M:         float = 695_700_000.0
GRAVITATIONAL_CONSTANT: float = 6.6740831e-11
GYR_TO_SECONDS:         float = 3.15576e16
TOKEN_MASS:             float = 1e7

# Runtime state
SYSTEM_AGE_SECONDS = None

# ─────────────────────────────────────────────────────────────────────────────
# SE TYPE REGISTRY
# ─────────────────────────────────────────────────────────────────────────────

VALID_SE_TYPES: set = {
    "Star", "Planet", "DwarfPlanet", "Moon", "DwarfMoon",
    "Asteroid", "Comet", "Barycenter",
    "BlackHole", "NeutronStar", "WhiteDwarf",
}

# ─────────────────────────────────────────────────────────────────────────────
# SE → US ATMOSPHERE DEPOT MAPPING
# ─────────────────────────────────────────────────────────────────────────────

SE_TO_US_DEPOT: dict = {
    "N2":    "Nitrogen",
    "CH4":   "Methane",
    "H2":    "Hydrogen",
    "NH3":   "Ammonia",
    "He":    "Helium",
    "H2O":   "Water",
    "Ar":    "Argon",
    "CO2":   "Carbon Dioxide",
    "SO2":   "Sulfur Dioxide",
    "O2":    "Oxygen",
    "C2H2":  "Methane",
    "C2H4":  "Methane",
    "C2H6":  "Methane",
    "C3H8":  "Methane",
    "C8H18": "Methane",
    "Ne":    "Helium",
    "Kr":    "Argon",
    "Xe":    "Argon",
    "SO":    "Sulfur Dioxide",
    "H2S":   "Sulfur Dioxide",
    "Cl2":   "Oxygen",
    "NaCl":  "Argon",
}

US_ATM_DEPOT_KEYS: list = [
    "Iron", "Silicate", "Argon", "Sulfur Dioxide", "Oxygen",
    "Carbon Dioxide", "Water", "Nitrogen", "Ammonia", "Methane",
    "Helium", "Hydrogen",
]

# ─── UNIVERSE SANDBOX CLOUD STYLE IDs ────────────────────────────────────────
US_CLOUD_STYLES: dict = {
    "None":0,"Fluffy":1,"Thick":2,"Storm":3,"Wispy":4,
    "Turbulent":5,"Sparse":6,"Thin":7,"Streaks":8,
}

# ─── STELLAR CLASS → US TYPE MAPPING ─────────────────────────────────────────
US_STAR_TYPE_MAIN_SEQUENCE = 1
US_STAR_TYPE_NEUTRON        = 4

SE_LUMINOSITY_CLASS_NAMES: dict = {
    "Ia":"Hypergiant/Supergiant Ia","Iab":"Supergiant Iab","Ib":"Supergiant Ib",
    "I":"Supergiant","II":"Bright Giant","III":"Giant","IV":"Subgiant",
    "V":"Main Sequence","VI":"Subdwarf","VII":"White Dwarf",
}
SE_NEUTRON_STAR_CLASSES: set = {
    "NS","NEUTRONSTAR","NEUTRON STAR","PULSAR","MAGNETAR","Q","QUARK STAR",
}
SE_WHITE_DWARF_CLASSES: set = {
    "WD","WHITEDWARF","WHITE DWARF",
    "DA","DB","DC","DO","DZ","DQ","DAB","DAH",
}

# ─────────────────────────────────────────────────────────────────────────────
# SE ATMOSPHERE MODEL → HAZE TYPE
# ─────────────────────────────────────────────────────────────────────────────

_SE_ATMOSPHERE_MODEL_TO_HAZE: dict = {
    "None":     0,
    "Biogenic": 0,
    "Chlorine": 5,
    "Earth":    2,
    "Ethereal": 0,
    "Jupiter":  3,
    "Mars":     4,
    "Neptune":  3,
    "Pluto":    1,
    "Sun":      0,
    "Thick":    4,
    "Thin":     1,
    "Titan":    1,
    "Venus":    3,
}


def get_haze_type_from_se_model(model_name: str) -> int:
    if not model_name:
        return 0
    model_clean = str(model_name).strip().lower()
    for se_name, haze_type in _SE_ATMOSPHERE_MODEL_TO_HAZE.items():
        if model_clean == se_name.lower():
            return haze_type
    return 0


# ─────────────────────────────────────────────────────────────────────────────
# COLOUR PALETTES
# ─────────────────────────────────────────────────────────────────────────────

_ATM:   str = "RGBA(0.500, 0.700, 1.000, 1.000)"
_BLACK: str = "RGBA(0.000, 0.000, 0.000, 1.000)"
_WHITE: str = "RGBA(1.000, 1.000, 1.000, 1.000)"
_WATER: str = "RGBA(0.100, 0.200, 0.500, 1.000)"
_TRANS: str = "RGBA(0.000, 0.000, 0.000, 0.000)"

_PLANET_PALETTES: dict = {
    "barren": [
        "RGBA(0.700, 0.650, 0.600, 1.000)",
        "RGBA(0.500, 0.450, 0.400, 1.000)",
        "RGBA(0.300, 0.280, 0.250, 1.000)",
    ],
    "ocean": [
        "RGBA(0.200, 0.400, 0.700, 1.000)",
        "RGBA(0.100, 0.300, 0.600, 1.000)",
        "RGBA(0.050, 0.150, 0.350, 1.000)",
    ],
    "ice": [
        "RGBA(0.850, 0.900, 0.950, 1.000)",
        "RGBA(0.700, 0.800, 0.900, 1.000)",
        "RGBA(0.500, 0.700, 0.850, 1.000)",
    ],
    "lava": [
        "RGBA(0.800, 0.200, 0.050, 1.000)",
        "RGBA(0.600, 0.100, 0.000, 1.000)",
        "RGBA(0.300, 0.050, 0.000, 1.000)",
    ],
    "terra": [
        "RGBA(0.200, 0.500, 0.200, 1.000)",
        "RGBA(0.400, 0.600, 0.400, 1.000)",
        "RGBA(0.600, 0.700, 0.500, 1.000)",
    ],
    "gas_giant": [
        "RGBA(0.920, 0.900, 0.860, 1.000)",
        "RGBA(0.780, 0.740, 0.680, 1.000)",
        "RGBA(0.520, 0.480, 0.420, 1.000)",
    ],
    "ice_giant": [
        "RGBA(0.650, 0.760, 0.820, 1.000)",
        "RGBA(0.480, 0.620, 0.720, 1.000)",
        "RGBA(0.320, 0.460, 0.580, 1.000)",
    ],
}

_ARCHETYPE_DEFAULT_PALETTE: dict = {
    "rocky":     "barren",
    "ocean":     "ocean",
    "ice":       "ice",
    "lava":      "lava",
    "gas_giant": "gas_giant",
    "ice_giant": "ice_giant",
    "star":      "star",
}

_ARCHETYPE_DEFAULT_TEMPS: dict = {
    "rocky":     280.0,
    "ocean":     288.0,
    "ice":       210.0,
    "lava":      700.0,
    "gas_giant": 130.0,
    "ice_giant":  70.0,
    "star":    5_800.0,
}


def _palette_from_preset(preset: str):
    return _PLANET_PALETTES.get(preset, (None, False))


# ─────────────────────────────────────────────────────────────────────────────
# GAS GIANT COLOUR SYSTEM
# ─────────────────────────────────────────────────────────────────────────────

def determine_sudarsky_class(teff: float) -> str:
    if   teff < 150:  return "I"
    elif teff < 250:  return "II"
    elif teff < 900:  return "III"
    elif teff < 1400: return "IV"
    else:             return "V"


def calculate_average_hsv_from_palette(palette: list) -> tuple:
    """
    Calculate the average HSV from a list of 'RGBA(r, g, b, a)' strings.
    Uses circular mean for hue to handle wrap-around correctly.
    """
    if not palette:
        return 0.0, 0.0, 0.7
    h_list, s_list, v_list = [], [], []
    for color_str in palette:
        nums = re.findall(r'[\d.]+', str(color_str))
        if len(nums) >= 3:
            try:
                h, s, v = colorsys.rgb_to_hsv(float(nums[0]), float(nums[1]), float(nums[2]))
                h_list.append(h); s_list.append(s); v_list.append(v)
            except (ValueError, IndexError):
                continue
    if not h_list:
        return 0.0, 0.0, 0.7
    h_cos_sum = sum(math.cos(2 * math.pi * h) for h in h_list)
    h_sin_sum = sum(math.sin(2 * math.pi * h) for h in h_list)
    h_result  = math.atan2(h_sin_sum, h_cos_sum) / (2 * math.pi)
    if h_result < 0:
        h_result += 1.0
    return h_result, sum(s_list) / len(s_list), sum(v_list) / len(v_list)


def _make_gas_palette_v2(sudarsky_class, archetype, atm_hue=None, atm_sat=None,
                         has_life=False, has_aerial_life=False,
                         seed=42, dist_au=5.0, star_teff=5800.0) -> list:
    rng = random.Random(seed)
    n_bands = rng.randint(6, 64)
    dist_factor = max(0.0, min(1.0, 1.0 - (math.log10(max(dist_au, 0.05)) + 1.3) / 3.0))
    star_factor = max(0.0, min(1.0, (star_teff - 3000.0) / 7000.0))
    stellar_idx = dist_factor * 0.6 + star_factor * 0.4
    _CLASS_RANGES = {
        "I":        ((0.07, 0.14), (0.04, 0.22), (0.78, 0.96)),
        "II":       ((0.55, 0.65), (0.00, 0.12), (0.82, 0.98)),
        "III":      ((0.56, 0.64), (0.30, 0.55), (0.12, 0.42)),
        "IV":       ((0.60, 0.70), (0.10, 0.35), (0.08, 0.28)),
        "V":        ((0.00, 0.06), (0.25, 0.55), (0.12, 0.45)),
        "ice_giant":((0.53, 0.62), (0.12, 0.38), (0.55, 0.82)),
    }
    cls_key = "ice_giant" if archetype == "ice_giant" else sudarsky_class
    r_h, r_s, r_v = _CLASS_RANGES.get(cls_key, _CLASS_RANGES["I"])
    if cls_key in ("I", "II"):
        val_boost = stellar_idx * 0.10
        r_v = (min(r_v[0] + val_boost, 0.96), min(r_v[1] + val_boost, 0.99))
        r_s = (r_s[0], max(r_s[0], r_s[1] - stellar_idx * 0.08))
    elif cls_key in ("III", "IV", "V"):
        r_v = (r_v[0], min(r_v[1] + stellar_idx * 0.06, 0.55))
    num_anchors = rng.randint(3, 7)
    anchors = [(rng.uniform(*r_h) % 1.0, rng.uniform(*r_s), rng.uniform(*r_v))
               for _ in range(num_anchors)]
    colors = []
    freq1 = rng.uniform(1.0, 5.0); freq2 = rng.uniform(5.0, 18.0)
    freq3 = rng.uniform(18.0, 38.0); haze_freq = rng.uniform(0.4, 1.4)
    t_values = [rng.random() for _ in range(n_bands)]
    for band_i in range(n_bands):
        t     = t_values[band_i]
        noise = rng.uniform(-0.3, 0.3)
        wave  = (math.sin(t * math.pi * freq1 + noise) * 0.50 +
                 math.sin(t * math.pi * freq2 + noise) * 0.30 +
                 math.sin(t * math.pi * freq3 + noise) * 0.20)
        wave_norm  = max(0.0, min(1.0, (wave + 1.0 + rng.uniform(-0.2, 0.2)) / 2.2))
        haze       = math.sin(t * math.pi * haze_freq) * 0.5 + 0.5
        anchor_idx = max(0, min(num_anchors - 1, int(wave_norm * num_anchors)))
        h_base, s_base, v_base = anchors[anchor_idx]
        h_band = (h_base + rng.uniform(-0.025, 0.025)) % 1.0
        s_band = max(r_s[0], min(r_s[1], s_base + rng.uniform(-0.12, 0.12)))
        v_band = max(r_v[0], min(r_v[1], v_base + rng.uniform(-0.12, 0.12)))
        band_rand = rng.random()
        if has_aerial_life and band_rand < 0.05:
            h_band = 0.333; s_band = rng.uniform(0.90, 1.00); v_band = rng.uniform(0.88, 1.00)
        elif has_life and band_rand < 0.08:
            h_band = 0.333; s_band = rng.uniform(0.60, 0.85); v_band = rng.uniform(0.70, 0.90)
        elif has_life and band_rand < 0.11:
            h_band = rng.uniform(0.90, 0.95); s_band = rng.uniform(0.40, 0.60); v_band = rng.uniform(0.65, 0.85)
        elif band_rand < 0.22:
            h_band = rng.uniform(0.07, 0.14)
            s_band = rng.uniform(0.00, 0.06 + stellar_idx * 0.04)
            v_band = rng.uniform(0.88, 0.99)
        elif band_rand < 0.46:
            h_band = rng.uniform(0.06, 0.14)
            s_band = rng.uniform(0.08, 0.26 - stellar_idx * 0.06)
            v_band = rng.uniform(0.72, 0.92) * (0.85 + haze * 0.15)
        elif band_rand < 0.56:
            s_band = max(0.0, s_band * 0.30); v_band = max(0.06, v_band * 0.55)
        else:
            if atm_hue is not None:
                try:
                    atm_h  = float(atm_hue) % 1.0
                    h_band = max(r_h[0], min(r_h[1], (h_band * 0.75 + atm_h * 0.25) % 1.0))
                except (ValueError, TypeError):
                    pass
            if atm_sat is not None:
                try:
                    s_blend = s_band * 0.70 + float(atm_sat) * 0.30
                    s_band  = min(s_blend, r_s[1])
                except (ValueError, TypeError):
                    pass
            if cls_key not in ("ice_giant",):
                s_band = min(s_band, 0.45)
        a_band  = rng.uniform(0.75, 0.97)
        r_c, g_c, b_c = colorsys.hsv_to_rgb(h_band, s_band, v_band)
        colors.append(f"RGBA({r_c:.3f}, {g_c:.3f}, {b_c:.3f}, {a_band:.3f})")
    rng.shuffle(colors)
    return colors


def _pick_gas_palette(archetype, mass_kg, teff, atm_info=None, has_life=False,
                      has_aerial_life=False, has_organic_life=False,
                      has_exotic_life=False, dist_au=5.0, star_teff=5800.0) -> list:
    hue = atm_info.get("hue")        if atm_info else None
    sat = atm_info.get("saturation") if atm_info else None
    obj_seed = int(abs(mass_kg)) % 99_999
    effective_has_life = has_life or has_aerial_life or has_organic_life or has_exotic_life
    if archetype == "ice_giant":
        return _make_gas_palette_v2("ice_giant", "ice_giant", hue, sat,
                                    has_life=effective_has_life, has_aerial_life=has_aerial_life,
                                    seed=obj_seed, dist_au=dist_au, star_teff=star_teff)
    sc = determine_sudarsky_class(teff)
    return _make_gas_palette_v2(sc, archetype, hue, sat,
                                has_life=effective_has_life, has_aerial_life=has_aerial_life,
                                seed=obj_seed, dist_au=dist_au, star_teff=star_teff)


def _generate_se_preset_class_colors() -> dict:
    raw_palettes = {
        "jupiter_class_i": {"palette": [
            "RGBA(0.820, 0.733, 0.278, 1.000)", "RGBA(0.753, 0.706, 0.494, 1.000)",
            "RGBA(0.824, 0.741, 0.545, 1.000)", "RGBA(0.902, 0.733, 0.616, 1.000)",
            "RGBA(0.604, 0.427, 0.298, 1.000)", "RGBA(0.557, 0.341, 0.310, 1.000)",
            "RGBA(0.408, 0.294, 0.129, 1.000)", "RGBA(0.310, 0.149, 0.224, 1.000)"]},
        "jupiter_class_ii": {"palette": [
            "RGBA(0.545, 0.639, 0.784, 1.000)", "RGBA(0.675, 0.745, 0.796, 1.000)",
            "RGBA(0.557, 0.639, 0.698, 1.000)", "RGBA(0.514, 0.627, 0.682, 1.000)",
            "RGBA(0.608, 0.686, 0.757, 1.000)", "RGBA(0.565, 0.671, 0.761, 1.000)",
            "RGBA(0.486, 0.580, 0.682, 1.000)", "RGBA(0.706, 0.757, 0.639, 1.000)"]},
        "jupiter_class_iii": {"palette": [
            "RGBA(0.298, 0.286, 0.447, 1.000)", "RGBA(0.357, 0.329, 0.447, 1.000)",
            "RGBA(0.137, 0.137, 0.227, 1.000)", "RGBA(0.282, 0.239, 0.408, 1.000)",
            "RGBA(0.357, 0.286, 0.427, 1.000)", "RGBA(0.188, 0.188, 0.267, 1.000)",
            "RGBA(0.027, 0.086, 0.208, 1.000)", "RGBA(0.318, 0.310, 0.420, 1.000)"]},
        "jupiter_class_iv": {"palette": [
            "RGBA(0.376, 0.357, 0.400, 1.000)", "RGBA(0.357, 0.329, 0.447, 1.000)",
            "RGBA(0.137, 0.137, 0.227, 1.000)", "RGBA(0.267, 0.329, 0.420, 1.000)",
            "RGBA(0.459, 0.439, 0.486, 1.000)", "RGBA(0.369, 0.369, 0.439, 1.000)",
            "RGBA(0.176, 0.267, 0.447, 1.000)", "RGBA(0.318, 0.310, 0.420, 1.000)"]},
        "jupiter_class_i_a": {"palette": [
            "RGBA(0.820, 0.733, 0.278, 1.000)", "RGBA(0.753, 0.706, 0.494, 1.000)",
            "RGBA(0.824, 0.741, 0.545, 1.000)", "RGBA(0.706, 0.502, 0.239, 1.000)",
            "RGBA(0.392, 0.204, 0.000, 1.000)", "RGBA(0.439, 0.275, 0.078, 1.000)",
            "RGBA(0.416, 0.278, 0.188, 1.000)", "RGBA(0.914, 0.965, 0.898, 1.000)"]},
        "jupiter_class_i_b": {"palette": [
            "RGBA(0.820, 0.733, 0.278, 1.000)", "RGBA(0.753, 0.706, 0.494, 1.000)",
            "RGBA(0.824, 0.741, 0.545, 1.000)", "RGBA(0.737, 0.557, 0.322, 1.000)",
            "RGBA(0.498, 0.318, 0.090, 1.000)", "RGBA(0.286, 0.102, 0.000, 1.000)",
            "RGBA(0.416, 0.278, 0.188, 1.000)", "RGBA(0.439, 0.459, 0.612, 1.000)"]},
        "jupiter_class_i_c": {"palette": [
            "RGBA(0.820, 0.733, 0.278, 1.000)", "RGBA(0.753, 0.706, 0.494, 1.000)",
            "RGBA(0.824, 0.741, 0.545, 1.000)", "RGBA(0.737, 0.557, 0.322, 1.000)",
            "RGBA(0.769, 0.588, 0.278, 1.000)", "RGBA(0.412, 0.200, 0.071, 1.000)",
            "RGBA(0.416, 0.278, 0.188, 1.000)", "RGBA(0.439, 0.459, 0.612, 1.000)"]},
        "jupiter_class_v": {"palette": [
            "RGBA(0.180, 0.408, 0.749, 1.000)", "RGBA(0.165, 0.588, 0.757, 1.000)",
            "RGBA(0.388, 0.549, 0.506, 1.000)", "RGBA(0.369, 0.435, 0.357, 1.000)",
            "RGBA(0.318, 0.318, 0.388, 1.000)", "RGBA(0.333, 0.365, 0.294, 1.000)",
            "RGBA(0.271, 0.294, 0.259, 1.000)", "RGBA(0.435, 0.612, 0.125, 1.000)"]},
        "jupiter_cold_default": {"palette": [
            "RGBA(0.839, 0.729, 0.569, 1.000)", "RGBA(0.847, 0.749, 0.600, 1.000)",
            "RGBA(0.337, 0.286, 0.200, 1.000)", "RGBA(0.506, 0.388, 0.188, 1.000)",
            "RGBA(0.557, 0.467, 0.318, 1.000)", "RGBA(0.576, 0.498, 0.357, 1.000)",
            "RGBA(0.729, 0.557, 0.294, 1.000)", "RGBA(0.678, 0.490, 0.286, 1.000)"]},
        "jupiter_cold_saturn": {"palette": [
            "RGBA(0.620, 0.549, 0.427, 1.000)", "RGBA(0.710, 0.627, 0.478, 1.000)",
            "RGBA(0.486, 0.400, 0.310, 1.000)", "RGBA(0.773, 0.753, 0.365, 1.000)",
            "RGBA(0.725, 0.639, 0.255, 1.000)", "RGBA(0.486, 0.400, 0.310, 1.000)",
            "RGBA(0.871, 0.514, 0.278, 1.000)", "RGBA(0.804, 0.667, 0.208, 1.000)"]},
        "jupiter_cool_blue": {"palette": [
            "RGBA(0.188, 0.427, 0.847, 1.000)", "RGBA(0.169, 0.447, 0.800, 1.000)",
            "RGBA(0.176, 0.447, 0.827, 1.000)", "RGBA(0.902, 0.718, 0.278, 1.000)",
            "RGBA(0.596, 0.439, 0.102, 1.000)", "RGBA(0.620, 0.482, 0.224, 1.000)",
            "RGBA(0.933, 0.522, 0.380, 1.000)", "RGBA(0.937, 0.882, 0.698, 1.000)"]},
        "jupiter_cool_default": {"palette": [
            "RGBA(0.569, 0.549, 0.478, 1.000)", "RGBA(0.847, 0.859, 0.867, 1.000)",
            "RGBA(0.447, 0.388, 0.259, 1.000)", "RGBA(0.729, 0.565, 0.298, 1.000)",
            "RGBA(1.000, 0.820, 0.588, 1.000)", "RGBA(0.753, 0.514, 0.306, 1.000)",
            "RGBA(0.875, 0.839, 0.380, 1.000)", "RGBA(0.420, 0.388, 0.310, 1.000)"]},
        "jupiter_frigid_default": {"palette": [
            "RGBA(0.827, 0.867, 0.662, 1.000)",
            "RGBA(0.744, 0.674, 0.563, 1.000)",
            "RGBA(0.720, 0.764, 0.648, 1.000)",
            "RGBA(0.880, 0.737, 0.634, 1.000)",
            "RGBA(0.876, 0.859, 0.723, 1.000)",
            "RGBA(0.686, 0.788, 0.751, 1.000)",
            "RGBA(0.787, 0.799, 0.870, 1.000)",
            "RGBA(0.841, 0.880, 0.733, 1.000)"]},
        "jupiter_frigid_neptune": {"palette": [
            "RGBA(0.200, 0.259, 0.329, 1.000)", "RGBA(0.247, 0.329, 0.447, 1.000)",
            "RGBA(0.220, 0.376, 0.557, 1.000)", "RGBA(0.984, 0.298, 0.000, 1.000)",
            "RGBA(0.600, 0.502, 0.133, 1.000)", "RGBA(0.725, 0.706, 0.392, 1.000)",
            "RGBA(0.682, 0.776, 0.569, 1.000)", "RGBA(1.000, 1.000, 1.000, 1.000)"]},
        "jupiter_frigid_uranus": {"palette": [
            "RGBA(0.188, 0.427, 0.847, 1.000)", "RGBA(0.169, 0.447, 0.800, 1.000)",
            "RGBA(0.176, 0.447, 0.827, 1.000)", "RGBA(0.157, 0.459, 0.886, 1.000)",
            "RGBA(0.176, 0.478, 0.859, 1.000)", "RGBA(0.098, 0.447, 0.827, 1.000)",
            "RGBA(0.188, 0.439, 0.859, 1.000)", "RGBA(0.176, 0.427, 0.847, 1.000)"]},
        "jupiter_hot_default": {"palette": [
            "RGBA(0.808, 0.808, 0.808, 1.000)", "RGBA(0.627, 0.647, 0.647, 1.000)",
            "RGBA(0.576, 0.557, 0.576, 1.000)", "RGBA(0.604, 0.545, 0.490, 1.000)",
            "RGBA(0.451, 0.667, 0.655, 1.000)", "RGBA(0.804, 0.545, 0.808, 1.000)",
            "RGBA(0.769, 0.490, 0.910, 1.000)", "RGBA(0.698, 0.643, 0.576, 1.000)"]},
        "neptune_cold_default": {"palette": [
            "RGBA(0.839, 0.729, 0.569, 1.000)", "RGBA(0.847, 0.749, 0.600, 1.000)",
            "RGBA(0.337, 0.286, 0.200, 1.000)", "RGBA(0.459, 0.376, 0.267, 1.000)",
            "RGBA(0.557, 0.467, 0.318, 1.000)", "RGBA(0.576, 0.498, 0.357, 1.000)",
            "RGBA(0.729, 0.627, 0.478, 1.000)", "RGBA(0.686, 0.659, 0.620, 1.000)"]},
        "neptune_cold_saturn": {"palette": [
            "RGBA(0.620, 0.549, 0.427, 1.000)", "RGBA(0.710, 0.627, 0.478, 1.000)",
            "RGBA(0.486, 0.400, 0.310, 1.000)", "RGBA(0.306, 0.251, 0.412, 1.000)",
            "RGBA(0.357, 0.459, 0.290, 1.000)", "RGBA(0.486, 0.400, 0.310, 1.000)",
            "RGBA(0.380, 0.663, 0.463, 1.000)", "RGBA(0.557, 0.459, 0.337, 1.000)"]},
        "neptune_cool_default": {"palette": [
            "RGBA(0.569, 0.549, 0.478, 1.000)", "RGBA(0.847, 0.859, 0.867, 1.000)",
            "RGBA(0.447, 0.388, 0.259, 1.000)", "RGBA(0.325, 0.510, 0.631, 1.000)",
            "RGBA(0.820, 0.741, 0.553, 1.000)", "RGBA(0.545, 0.675, 0.918, 1.000)",
            "RGBA(0.498, 0.494, 0.576, 1.000)", "RGBA(0.420, 0.388, 0.310, 1.000)"]},
        "neptune_frigid_default": {"palette": [
            "RGBA(0.118, 0.200, 0.478, 1.000)", "RGBA(0.329, 0.400, 0.718, 1.000)",
            "RGBA(0.329, 0.400, 0.718, 1.000)", "RGBA(0.600, 0.600, 0.600, 1.000)",
            "RGBA(0.349, 0.427, 0.769, 1.000)", "RGBA(0.467, 0.537, 0.847, 1.000)",
            "RGBA(0.600, 0.647, 0.910, 1.000)", "RGBA(0.988, 0.988, 1.000, 1.000)"]},
        "neptune_frigid_neptune": {"palette": [
            "RGBA(0.200, 0.259, 0.329, 1.000)", "RGBA(0.247, 0.329, 0.447, 1.000)",
            "RGBA(0.220, 0.376, 0.557, 1.000)", "RGBA(0.259, 0.427, 0.529, 1.000)",
            "RGBA(0.239, 0.427, 0.627, 1.000)", "RGBA(0.239, 0.510, 0.776, 1.000)",
            "RGBA(0.000, 0.294, 0.988, 1.000)", "RGBA(0.180, 0.282, 0.761, 1.000)"]},
        "neptune_frigid_uranus": {"palette": [
            "RGBA(0.235, 0.475, 0.894, 1.000)", "RGBA(0.208, 0.506, 0.878, 1.000)",
            "RGBA(0.176, 0.553, 0.827, 1.000)", "RGBA(0.388, 0.584, 0.867, 1.000)",
            "RGBA(0.800, 0.886, 1.000, 1.000)", "RGBA(0.761, 0.906, 0.910, 1.000)",
            "RGBA(0.188, 0.671, 0.859, 1.000)", "RGBA(0.176, 0.408, 0.847, 1.000)"]},
        "neptune_hot_default": {"palette": [
            "RGBA(0.808, 0.808, 0.808, 1.000)", "RGBA(0.627, 0.647, 0.647, 1.000)",
            "RGBA(0.576, 0.557, 0.576, 1.000)", "RGBA(0.161, 0.161, 0.765, 1.000)",
            "RGBA(0.596, 0.678, 0.749, 1.000)", "RGBA(0.769, 1.000, 0.973, 1.000)",
            "RGBA(0.184, 0.702, 0.569, 1.000)", "RGBA(0.651, 0.459, 0.204, 1.000)"]},
        "neptune_temperate_default": {"palette": [
            "RGBA(0.118, 0.200, 0.478, 1.000)", "RGBA(0.329, 0.400, 0.718, 1.000)",
            "RGBA(0.329, 0.400, 0.718, 1.000)", "RGBA(0.290, 0.573, 0.741, 1.000)",
            "RGBA(0.000, 0.373, 0.902, 1.000)", "RGBA(0.467, 0.537, 0.847, 1.000)",
            "RGBA(0.600, 0.647, 0.910, 1.000)", "RGBA(0.988, 0.988, 1.000, 1.000)"]},
        "neptune_torrid_default": {"palette": [
            "RGBA(0.278, 0.259, 0.247, 1.000)", "RGBA(0.298, 0.278, 0.267, 1.000)",
            "RGBA(0.357, 0.329, 0.318, 1.000)", "RGBA(0.400, 0.369, 0.357, 1.000)",
            "RGBA(0.439, 0.427, 0.420, 1.000)", "RGBA(0.518, 0.467, 0.459, 1.000)",
            "RGBA(0.639, 0.620, 0.588, 1.000)", "RGBA(0.608, 0.588, 0.698, 1.000)"]},
        "neptune_warm_default": {"palette": [
            "RGBA(0.118, 0.200, 0.478, 1.000)", "RGBA(0.329, 0.400, 0.718, 1.000)",
            "RGBA(0.329, 0.400, 0.718, 1.000)", "RGBA(0.290, 0.302, 0.902, 1.000)",
            "RGBA(0.349, 0.427, 0.769, 1.000)", "RGBA(0.467, 0.537, 0.847, 1.000)",
            "RGBA(0.600, 0.647, 0.910, 1.000)", "RGBA(0.741, 0.698, 1.000, 1.000)"]},
        "browndwarf_l_default": {"palette": [
            "RGBA(1.000, 1.000, 1.000, 1.000)", "RGBA(0.635, 0.635, 0.675, 1.000)",
            "RGBA(0.443, 0.435, 0.510, 1.000)", "RGBA(0.098, 0.098, 0.149, 1.000)",
            "RGBA(0.153, 0.153, 0.392, 1.000)", "RGBA(0.082, 0.082, 0.573, 1.000)",
            "RGBA(0.075, 0.075, 0.502, 1.000)", "RGBA(0.043, 0.043, 0.384, 1.000)"]},
        "browndwarf_l_pink": {"palette": [
            "RGBA(0.278, 0.259, 0.247, 1.000)", "RGBA(0.220, 0.200, 0.200, 1.000)",
            "RGBA(0.098, 0.059, 0.059, 1.000)", "RGBA(0.078, 0.039, 0.039, 1.000)",
            "RGBA(0.502, 0.031, 0.741, 1.000)", "RGBA(0.427, 0.008, 0.463, 1.000)",
            "RGBA(0.212, 0.059, 0.224, 1.000)", "RGBA(0.161, 0.090, 0.184, 1.000)"]},
        "browndwarf_t_default": {"palette": [
            "RGBA(1.000, 1.000, 1.000, 1.000)", "RGBA(0.569, 0.392, 0.780, 1.000)",
            "RGBA(0.329, 0.227, 0.831, 1.000)", "RGBA(0.239, 0.239, 0.424, 1.000)",
            "RGBA(0.188, 0.188, 0.620, 1.000)", "RGBA(0.412, 0.412, 0.788, 1.000)",
            "RGBA(0.663, 0.663, 0.922, 1.000)", "RGBA(1.000, 1.000, 1.000, 1.000)"]},
        "browndwarf_t_pink": {"palette": [
            "RGBA(1.000, 1.000, 1.000, 1.000)", "RGBA(0.545, 0.557, 0.827, 1.000)",
            "RGBA(0.349, 0.302, 0.624, 1.000)", "RGBA(0.424, 0.227, 0.682, 1.000)",
            "RGBA(0.533, 0.063, 0.561, 1.000)", "RGBA(0.592, 0.196, 0.663, 1.000)",
            "RGBA(0.725, 0.478, 0.851, 1.000)", "RGBA(1.000, 1.000, 1.000, 1.000)"]},
        "browndwarf_y_default": {"palette": [
            "RGBA(1.000, 1.000, 1.000, 1.000)", "RGBA(0.541, 0.259, 0.686, 1.000)",
            "RGBA(0.573, 0.145, 0.573, 1.000)", "RGBA(0.212, 0.290, 0.525, 1.000)",
            "RGBA(0.216, 0.216, 0.655, 1.000)", "RGBA(0.361, 0.361, 0.737, 1.000)",
            "RGBA(0.635, 0.635, 0.933, 1.000)", "RGBA(1.000, 1.000, 1.000, 1.000)"]},
        "browndwarf_y_pink": {"palette": [
            "RGBA(1.000, 1.000, 1.000, 1.000)", "RGBA(0.325, 0.173, 0.467, 1.000)",
            "RGBA(0.361, 0.180, 0.404, 1.000)", "RGBA(0.204, 0.118, 0.380, 1.000)",
            "RGBA(0.408, 0.145, 0.490, 1.000)", "RGBA(0.349, 0.180, 0.596, 1.000)",
            "RGBA(0.675, 0.412, 0.824, 1.000)", "RGBA(1.000, 1.000, 1.000, 1.000)"]},
    }
    result = {}
    for key, data in raw_palettes.items():
        palette = data["palette"]
        h, s, v = calculate_average_hsv_from_palette(palette)
        result[key] = {"palette": palette, "base_hue": h, "base_sat": s, "base_val": v}
    return result


_SE_PRESET_CLASS_COLORS: dict = _generate_se_preset_class_colors()


def detect_brown_dwarf_type(raw_class_str, teff, raw_data) -> tuple:
    if not raw_class_str:
        return False, None, None
    class_lower = str(raw_class_str).strip().lower()
    raw_data_dict = raw_data if isinstance(raw_data, dict) else {}
    surf = raw_data_dict.get("Surface", {})
    if isinstance(surf, dict):
        preset = str(surf.get("Preset", "")).lower()
        if "browndwarf" in preset:
            if "y" in preset: return True, "Y", "browndwarf_y_pink" if "pink" in preset else "browndwarf_y_default"
            if "t" in preset: return True, "T", "browndwarf_t_pink" if "pink" in preset else "browndwarf_t_default"
            if "l" in preset: return True, "L", "browndwarf_l_pink" if "pink" in preset else "browndwarf_l_default"
    match = re.search(r'\b([lty])[\d.]', class_lower)
    if match:
        sc = match.group(1).upper()
        has_pink = "pink" in str(surf.get("Preset", "")).lower() if isinstance(surf, dict) else False
        return True, sc, f"browndwarf_{sc.lower()}_{'pink' if has_pink else 'default'}"
    if "brown dwarf" in class_lower or ("dwarf" in class_lower and teff and teff < 2700):
        if teff < 700:   return True, "Y", "browndwarf_y_default"
        if teff < 1300:  return True, "T", "browndwarf_t_default"
        return True, "L", "browndwarf_l_default"
    return False, None, None


def _generate_gas_giant_palette_from_preset(
        preset_name, mass_kg, teff,
        atm_hue=None, atm_sat=None,
        has_life=False, has_aerial_life=False,
        has_organic_life=False, has_exotic_life=False,
        dist_au=5.0, star_teff=5800.0,
        is_brown_dwarf=False, raw_class="") -> list:

    preset_lo = (preset_name or "").lower().replace("_", " ").replace(".cfg", "")

    if is_brown_dwarf:
        is_bd, _, bd_palette_key = detect_brown_dwarf_type(raw_class, teff, {})
        if is_bd and bd_palette_key and bd_palette_key in _SE_PRESET_CLASS_COLORS:
            preset_colors = _SE_PRESET_CLASS_COLORS[bd_palette_key]["palette"]
            rng = random.Random(int(abs(mass_kg)) % 99_999)
            n_bands = rng.randint(6, 32)
            colors = []
            for i in range(n_bands):
                idx = int((i / max(n_bands - 1, 1)) * (len(preset_colors) - 1))
                nums = re.findall(r'[\d.]+', preset_colors[idx])
                if len(nums) >= 4:
                    r_c, g_c, b_c, a_c = float(nums[0]), float(nums[1]), float(nums[2]), float(nums[3])
                    if teff > 1500:
                        r_c = min(1.0, r_c * 1.15); g_c = min(1.0, g_c * 1.10); b_c = min(1.0, b_c * 1.08)
                    elif teff < 600:
                        r_c = max(0.0, r_c * 0.85); g_c = max(0.0, g_c * 0.85); b_c = max(0.0, b_c * 0.90)
                    r_c = max(0.0, min(1.0, r_c + rng.uniform(-0.05, 0.05)))
                    g_c = max(0.0, min(1.0, g_c + rng.uniform(-0.05, 0.05)))
                    b_c = max(0.0, min(1.0, b_c + rng.uniform(-0.05, 0.05)))
                    colors.append(f"RGBA({r_c:.3f}, {g_c:.3f}, {b_c:.3f}, {rng.uniform(0.75, 0.97):.3f})")
                else:
                    colors.append(preset_colors[idx])
            return colors

    matched_preset = None
    for preset_key, preset_dict in _SE_PRESET_CLASS_COLORS.items():
        if "brown" not in preset_key:
            key_lo = preset_key.replace("_", " ")
            if key_lo in preset_lo or preset_lo in key_lo:
                matched_preset = preset_dict
                break

    if matched_preset is None:
        return list(_pick_gas_palette("gas_giant", mass_kg, teff, None,
                                      has_life, has_aerial_life, has_organic_life,
                                      has_exotic_life, dist_au, star_teff))

    preset_palette = matched_preset.get("palette", [])
    rng = random.Random(int(abs(mass_kg)) % 99_999)
    n_bands = rng.randint(8, 48)

    # Atmospheric tint — computed relative to the palette's own average colour
    atm_hue_shift = 0.0
    atm_sat_scale = 1.0
    if atm_hue is not None and atm_sat is not None:
        try:
            avg_h, _, _ = calculate_average_hsv_from_palette(preset_palette)
            raw_atm_h   = float(atm_hue) % 1.0
            raw_atm_s   = max(0.0, min(2.0, float(atm_sat)))
            delta_h     = raw_atm_h - avg_h
            if delta_h >  0.5: delta_h -= 1.0
            if delta_h < -0.5: delta_h += 1.0
            atm_hue_shift = max(-0.10, min(0.10, delta_h * 0.20))
            atm_sat_scale = max(0.80, min(1.20, 1.0 + (raw_atm_s - 1.0) * 0.15))
        except (ValueError, TypeError):
            pass

    colors = []
    for i in range(n_bands):
        if preset_palette:
            if rng.random() < 0.25:
                expected_idx = int((i / max(n_bands - 1, 1)) * (len(preset_palette) - 1))
                idx = max(0, min(len(preset_palette) - 1, expected_idx + rng.randint(-2, 2)))
            else:
                idx = rng.randint(0, len(preset_palette) - 1)
            nums = re.findall(r'[\d.]+', preset_palette[idx])
            r_b, g_b, b_b = (float(nums[0]), float(nums[1]), float(nums[2])) if len(nums) >= 4 else (0.5, 0.5, 0.5)
        else:
            r_b, g_b, b_b = 0.5, 0.5, 0.5

        band_rand = rng.random()
        if has_aerial_life and band_rand < 0.05:   r_b, g_b, b_b = 0.2, 0.9, 0.3
        elif has_organic_life and band_rand < 0.08: r_b, g_b, b_b = 0.3, 0.7, 0.2
        elif has_exotic_life and band_rand < 0.11:  r_b, g_b, b_b = 0.9, 0.2, 0.9

        h_b, s_b, v_b = colorsys.rgb_to_hsv(r_b, g_b, b_b)
        h_b = (h_b + atm_hue_shift) % 1.0
        s_b = max(0.0, min(1.0, s_b * atm_sat_scale))
        r_b, g_b, b_b = colorsys.hsv_to_rgb(h_b, s_b, v_b)

        wave = math.sin(rng.random() * math.pi * rng.uniform(1.0, 5.0)) * 0.5 + 0.5
        bmod = 0.85 + wave * 0.15
        r_b = max(0.0, min(1.0, r_b * bmod + rng.uniform(-0.04, 0.04)))
        g_b = max(0.0, min(1.0, g_b * bmod + rng.uniform(-0.04, 0.04)))
        b_b = max(0.0, min(1.0, b_b * bmod + rng.uniform(-0.04, 0.04)))
        colors.append(f"RGBA({r_b:.3f}, {g_b:.3f}, {b_b:.3f}, {rng.uniform(0.75, 0.97):.3f})")

    rng.shuffle(colors)
    return colors


# ─────────────────────────────────────────────────────────────────────────────
# HEIGHTMAP SETS
# ─────────────────────────────────────────────────────────────────────────────

_HEIGHTMAP_SETS: dict = {
    "rocky": [
        ("Textures/Planets/planet10_height", "Textures/Planets/planet10_height_normals",
         "Textures/Planets/planet1_height",  "Textures/Planets/planet1_height_normals"),
        ("Textures/Planets/planet4_height",  "Textures/Planets/planet4_height_normals",
         "Textures/Planets/planet7_height",  "Textures/Planets/planet7_height_normals"),
        ("Textures/Planets/planet8_height",  "Textures/Planets/planet8_height_normals",
         "Textures/Planets/planet3_height",  "Textures/Planets/planet3_height_normals"),
    ],
    "ocean": [
        ("Textures/Planets/planet1_height",  "Textures/Planets/planet1_height_normals",
         "Textures/Planets/planet2_height",  "Textures/Planets/planet2_height_normals"),
        ("Textures/Planets/planet3_height",  "Textures/Planets/planet3_height_normals",
         "Textures/Planets/planet6_height",  "Textures/Planets/planet6_height_normals"),
    ],
    "ice": [
        ("Textures/Planets/planet5_height",  "Textures/Planets/planet5_height_normals",
         "Textures/Planets/planet6_height",  "Textures/Planets/planet6_height_normals"),
        ("Textures/Planets/planet2_height",  "Textures/Planets/planet2_height_normals",
         "Textures/Planets/planet5_height",  "Textures/Planets/planet5_height_normals"),
    ],
    "lava": [
        ("Textures/Planets/planet9_height",  "Textures/Planets/planet9_height_normals",
         "Textures/Planets/planet12_height", "Textures/Planets/planet12_height_normals"),
        ("Textures/Planets/planet11_height", "Textures/Planets/planet11_height_normals",
         "Textures/Planets/planet9_height",  "Textures/Planets/planet9_height_normals"),
    ],
}

_NAMED_BODY_TEXTURES: dict = {
    "earth": {
        "ColorMapSource":      "Textures/Planets/earth_diffuse",
        "HeightMapSource":     "Textures/Planets/earth_height",
        "NormalMapSource":     "Textures/Planets/earth_height_normals",
        "HeightMapSource2":    "", "NormalMapSource2": "",
        "EmissiveMapSource":   "Textures/EarthNight_2500x1250Grids",
        "VegetationMapSource": "Textures/Planets/earth_vegetation",
        "UseHeightMap0":    True,  "HeightMapMix0":    1.0, "HeightMapOffset0": 0.0,
        "HeightMapFlipH0":  False, "HeightMapFlipV0":  False,
        "UseHeightMap1":    False, "HeightMapMix1":    1.0, "HeightMapOffset1": 0.0,
        "HeightMapFlipH1":  False, "HeightMapFlipV1":  False,
        "HazeType": 2, "CityLightSource": 0,
        "AtmosphereColor":          "RGBA(0.212, 0.325, 0.510, 1.000)",
        "originalAtmosphereColor":  "RGBA(0.212, 0.325, 0.510, 1.000)",
        "customAtmosphereColor":    "RGBA(0.212, 0.325, 0.510, 1.000)",
        "CloudSetA": 1, "CloudSetB": 4, "CloudCoverage": 0.90, "CloudOpacity": 1.0,
        "planet_colors": [
            "RGBA(1.000, 1.000, 1.000, 1.000)",
            "RGBA(0.500, 0.500, 0.500, 1.000)",
            "RGBA(0.000, 0.000, 0.000, 1.000)",
        ],
    },
    "moon": {
        "ColorMapSource": "", "EmissiveMapSource": "",
        "HeightMapSource":  "Textures/Planets/planet5_height",
        "NormalMapSource":  "Textures/Planets/planet5_height_normals",
        "HeightMapSource2": "", "NormalMapSource2": "",
        "UseHeightMap0": True,  "HeightMapMix0": 1.0, "HeightMapOffset0": 0.0,
        "HeightMapFlipH0": False, "HeightMapFlipV0": False,
        "UseHeightMap1": False, "HeightMapMix1": 1.0, "HeightMapOffset1": 0.0,
        "HeightMapFlipH1": False, "HeightMapFlipV1": False,
        "HazeType": 0, "CityLightSource": 1,
        "planet_colors": [
            "RGBA(0.500, 0.490, 0.480, 0.950)",
            "RGBA(0.360, 0.350, 0.340, 0.750)",
            "RGBA(0.200, 0.195, 0.190, 0.550)",
        ],
    },
    "mars": {
        "ColorMapSource": "", "EmissiveMapSource": "",
        "HeightMapSource":  "Textures/Planets/planet4_height",
        "NormalMapSource":  "Textures/Planets/planet4_height_normals",
        "HeightMapSource2": "", "NormalMapSource2": "",
        "UseHeightMap0": True,  "HeightMapMix0": 1.0, "HeightMapOffset0": 0.0,
        "HeightMapFlipH0": False, "HeightMapFlipV0": False,
        "UseHeightMap1": False, "HeightMapMix1": 1.0, "HeightMapOffset1": 0.0,
        "HeightMapFlipH1": False, "HeightMapFlipV1": False,
        "HazeType": 1, "CityLightSource": 1,
        "AtmosphereColor":         "RGBA(0.700, 0.380, 0.200, 1.000)",
        "originalAtmosphereColor": "RGBA(0.700, 0.380, 0.200, 1.000)",
        "customAtmosphereColor":   "RGBA(0.700, 0.380, 0.200, 1.000)",
        "planet_colors": [
            "RGBA(0.780, 0.420, 0.200, 0.920)",
            "RGBA(0.550, 0.280, 0.120, 0.720)",
            "RGBA(0.310, 0.130, 0.050, 0.520)",
        ],
    },
}


# ─────────────────────────────────────────────────────────────────────────────
# UTILITY
# ─────────────────────────────────────────────────────────────────────────────

def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def safe_float(val, default: float = 0.0) -> float:
    try:
        f = float(val)
        return default if (math.isnan(f) or math.isinf(f)) else f
    except (ValueError, TypeError):
        return default