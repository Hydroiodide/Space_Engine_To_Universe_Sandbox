"""
surface_generator.py
Universe Sandbox surface archive generator.

Reads Space Engine Surface{} parameters and generates physically-driven
terrain, temperature, albedo, and material maps.

Atlas format (reverse-engineered from US files):
  Atlas:     1024 x 512 float32 RGBA, little-endian
  Tile:      64 x 32 per body
  Layout:    16 cols x 16 rows = 256 slots max
  Fill:      left→right, top→bottom
  Files:     data.surface, material0-3.surface, info

Channel assignments (confirmed from 100-Earth dataset):
  data.surface      ch0 = surface temperature (K)
                    ch1 = diffuse albedo (0–1)
                    ch2,ch3 = 0
  material0.surface ch0 = terrain mask (0–0.32)
                    ch1 = vapour pressure (Pa, Magnus eq.)
                    ch2 = secondary mask (0–0.17)
                    ch3 = continuous mask (0–1)
  material1.surface ch1 = rock heat capacity (J/m²K, ~67k–90k)
  material2.surface ch1 = mat1_ch1 / 3.728
  material3.surface ch1 = mat1_ch1 / 83.60
"""

import json
import math
import zipfile
import hashlib
import logging

import numpy as np

from constants import log_debug, safe_float

# ── Atlas constants ───────────────────────────────────────────────────────────
TILE_W         = 64
TILE_H         = 32
ATLAS_W        = 1024
ATLAS_H        = 512
ATLAS_COLS     = ATLAS_W // TILE_W    # 16
ATLAS_ROWS     = ATLAS_H // TILE_H    # 16
ATLAS_CAPACITY = ATLAS_COLS * ATLAS_ROWS  # 256

# ── Body filter ───────────────────────────────────────────────────────────────
_SOLID_ARCHETYPES = {"rocky", "ocean", "ice", "lava", "terra"}

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# SEEDING
# ─────────────────────────────────────────────────────────────────────────────

def _make_seed(name: str, radius_m: float, mass_kg: float, sma_m: float = 0.0) -> int:
    raw = f"{name}|{radius_m:.3f}|{mass_kg:.3e}|{sma_m:.3e}"
    return int(hashlib.md5(raw.encode()).hexdigest(), 16) & 0x7FFFFFFF


# ─────────────────────────────────────────────────────────────────────────────
# COORDINATE GRIDS  (module-level cache, rebuilt if tile size changes)
# ─────────────────────────────────────────────────────────────────────────────

def _make_grids_at(w: int, h: int):
    """Build coordinate grids at arbitrary resolution."""
    u = (np.arange(w, dtype=np.float32) + 0.5) / w
    v = (np.arange(h, dtype=np.float32) + 0.5) / h
    U, V    = np.meshgrid(u, v)
    lon     = U * 2.0 * math.pi          # 0 → 2π
    lat     = (V - 0.5) * math.pi        # −π/2 → π/2
    lat_sin = np.sin(lat)                # −1 (S pole) → +1 (N pole)
    lat_abs = np.abs(lat_sin)
    cos_lat = np.cos(lat).astype(np.float32)
    return lon, lat, lat_sin, lat_abs, cos_lat


def _make_grids():
    return _make_grids_at(TILE_W, TILE_H)

_GRIDS = _make_grids()


# ─────────────────────────────────────────────────────────────────────────────
# VECTORISED NOISE  (pure NumPy — no external deps)
# ─────────────────────────────────────────────────────────────────────────────

def _hash_grid(ix: np.ndarray, iy: np.ndarray, seed: int) -> np.ndarray:
    # Use float32 arithmetic to avoid integer overflow entirely
    fx = ix.astype(np.float32) * np.float32(1619)
    fy = iy.astype(np.float32) * np.float32(31337)
    fs = np.float32(seed & 0xFFFF) * np.float32(6971)
    h  = (fx + fy + fs) * np.float32(0.000015259)  # / 65536
    h  = h - np.floor(h)   # frac: 0..1
    # Extra mix
    h  = h * np.float32(2.0) - np.float32(1.0)    # -1..1
    h  = h * (np.float32(3.0) - np.float32(2.0) * np.abs(h))  # smooth
    return h.astype(np.float32)


def _smooth(t: np.ndarray) -> np.ndarray:
    return t * t * t * (t * (t * 6.0 - 15.0) + 10.0)


def _value_noise(lon: np.ndarray, lat: np.ndarray,
                 freq: float, seed: int) -> np.ndarray:
    """Tileable 2-D value noise (seamless longitude wrap)."""
    period = max(1, int(round(freq * TILE_W / (2 * math.pi))))
    x  = lon * freq
    y  = (lat + math.pi / 2) * freq
    x0 = np.floor(x).astype(np.int32);  x1 = x0 + 1
    y0 = np.floor(y).astype(np.int32);  y1 = y0 + 1
    x0w = x0 % period;  x1w = x1 % period
    tx = _smooth(x - x0.astype(np.float32))
    ty = _smooth(y - y0.astype(np.float32))
    v00 = _hash_grid(x0w, y0, seed);  v10 = _hash_grid(x1w, y0, seed)
    v01 = _hash_grid(x0w, y1, seed);  v11 = _hash_grid(x1w, y1, seed)
    return (v00*(1-tx)*(1-ty) + v10*tx*(1-ty) +
            v01*(1-tx)*ty     + v11*tx*ty)


def _fbm(lon, lat, base_freq, octaves, lacunarity, gain, seed) -> np.ndarray:
    result = np.zeros_like(lon, dtype=np.float32)
    amp = 1.0;  freq = base_freq;  norm = 0.0
    for i in range(octaves):
        result += _value_noise(lon, lat, freq, seed + i * 997) * amp
        norm   += amp;  amp *= gain;  freq *= lacunarity
    return result / norm


def _ridged(lon, lat, base_freq, octaves, seed) -> np.ndarray:
    """Ridged multifractal — mountain ranges / rift walls."""
    result = np.zeros_like(lon, dtype=np.float32)
    amp = 1.0;  freq = base_freq;  norm = 0.0
    for i in range(octaves):
        n = _value_noise(lon, lat, freq, seed + i * 1009)
        n = 1.0 - np.abs(n);  n = n * n
        result += n * amp;  norm += amp;  amp *= 0.5;  freq *= 2.1
    return result / norm


# ─────────────────────────────────────────────────────────────────────────────
# SE SURFACE PARAMETER PARSER
# ─────────────────────────────────────────────────────────────────────────────

def parse_space_engine_surface(surf: dict) -> dict:
    from constants import safe_float

    def g(key, default):
        return safe_float(surf.get(key, default))

    def freq(key, default, se_max, noise_max=3.0):
        raw = g(key, default)
        return max(0.05, min(noise_max, (raw / se_max) * noise_max)) if se_max > 0 else 0.05

    def magn(key, default, se_max):
        return max(0.0, min(1.0, g(key, default) / se_max))

    def direct(key, default):
        return max(0.0, min(1.0, g(key, default)))

    p = {}

    # Sea / snow  (SE: -1→2)
    raw_sl   = g("seaLevel",  0.3)
    raw_snow = g("snowLevel", 0.8)

    # seaLevel: treat as direct ocean fraction ÷ 2
    # SE=0   → 0% ocean, SE=1 → 50%, SE=2 → 100%, SE=-1 → 0% (no ocean)
    p["seaLevel"]  = max(0.0, min(0.95, raw_sl / 2.0)) if raw_sl > 0 else 0.0

    # snowLevel: SE -1→2, higher = less snow. Map to elevation threshold 0→1.
    p["snowLevel"] = max(0.0, min(1.0, (raw_snow + 1.0) / 3.0))

    # icecapLatitude: SE 0→2, ≥1.0 = effectively no ice
    raw_ic = g("icecapLatitude", 0.8)
    if raw_ic >= 1.0:
        p["icecapLatitude"] = 0.99   # no meaningful ice
    else:
        p["icecapLatitude"] = min(0.98, math.sin(raw_ic / 2.0 * math.pi / 2.0))

    # icecapHeight  (SE: 0→2  →  0→1)
    p["icecapHeight"] = max(0.0, min(1.0, g("icecapHeight", 0.5) / 2.0))

    # Climate  (SE: 0→1, direct)
    p["climatePole"]    = direct("climatePole",    0.2)
    p["climateTropic"]  = direct("climateTropic",  0.6)
    p["climateEquator"] = direct("climateEquator", 0.8)
    p["humidity"]       = direct("humidity",       0.5)
    p["heightTempGrad"] = direct("heightTempGrad", 0.5)
    p["tropicLatitude"] = direct("tropicLatitude", 0.2)
    p["tropicWidth"]    = max(0.01, min(0.5, g("tropicWidth", 0.2)))
    p["erosion"]        = direct("erosion",        0.3)

    # Frequencies  (SE → noise-space 0.05–3.0)
    p["mainFreq"]    = freq("mainFreq",    1.0,   5.0)
    p["montesFreq"]  = freq("montesFreq",  1.0,   1000.0)
    p["hillsFreq"]   = freq("hillsFreq",   1.0,   10000.0)
    p["dunesFreq"]   = freq("dunesFreq",   2.0,   100000.0)
    p["riftsFreq"]   = freq("riftsFreq",   1.0,   10.0)
    p["canyonsFreq"] = freq("canyonsFreq", 1.0,   1000.0)
    p["cracksFreq"]  = freq("cracksFreq",  0.5,   15.0)
    p["craterFreq"]  = freq("craterFreq",  0.5,   100.0)
    p["volcanoFreq"] = freq("volcanoFreq", 0.3,   2.0)
    p["riversFreq"]  = freq("riversFreq",  1.0,   10.0)

    # Magnitudes  (SE → 0–1)
    p["montesMagn"]  = magn("montesMagn",  0.3,  10.0)
    p["hillsMagn"]   = magn("hillsMagn",   0.2,  10.0)
    p["dunesMagn"]   = magn("dunesMagn",   0.1,  10.0)
    p["riftsMagn"]   = magn("riftsMagn",   0.0,  100.0)
    p["canyonsMagn"] = magn("canyonsMagn", 0.0,  10.0)
    p["cracksMagn"]  = magn("cracksMagn",  0.0,  10.0)
    p["craterMagn"]  = magn("craterMagn",  0.0,  10.0)
    p["volcanoMagn"] = direct("volcanoMagn", 0.0)   # SE already 0–1

    # Fractions / shape  (SE: 0→1, direct)
    p["montesSpiky"]     = direct("montesSpiky",     0.5)
    p["montesFraction"]  = direct("montesFraction",  0.4)
    p["hillsFraction"]   = direct("hillsFraction",   0.5)
    p["hills2Fraction"]  = direct("hills2Fraction",  0.2)
    p["dunesFraction"]   = direct("dunesFraction",   0.2)
    p["canyonsFraction"] = direct("canyonsFraction", 0.2)
    p["craterDensity"]   = direct("craterDensity",   0.0)
    p["volcanoDensity"]  = direct("volcanoDensity",  0.0)
    p["volcanoFlows"]    = direct("volcanoFlows",     0.0)
    p["volcanoRadius"]   = max(0.05, direct("volcanoRadius", 0.3))

    # volcanoActivity  (SE: 0→2  →  0→1)
    p["volcanoActivity"] = max(0.0, min(1.0, g("volcanoActivity", 0.0) / 2.0))

    # riversMagn  (SE: 0→100  →  0→1)
    p["riversMagn"] = max(0.0, min(1.0, g("riversMagn", 0.0) / 100.0))

    # BumpHeight kept in metres
    p["BumpHeight"] = g("BumpHeight", 10.0)
    p["BumpOffset"] = g("BumpOffset", 0.0)

    return p


# ─────────────────────────────────────────────────────────────────────────────
# STAGE 1 — CONTINENTAL HEIGHTMAP
# ─────────────────────────────────────────────────────────────────────────────

def generate_heightmap(p: dict, seed: int) -> np.ndarray:
    """
    Returns float32 (H, W) in [0, 1].
    0 = deepest basin, 1 = highest peak.
    seaLevel controls the land/ocean split point.

    Stages:
      1. Continental mask (low-freq FBM)     → mainFreq
      2. Mountains (ridged multifractal)     → montesMagn, montesFreq, montesSpiky
      3. Hills (secondary FBM)               → hillsMagn, hillsFreq
      4. Dunes (high-freq, low-amplitude)    → dunesMagn, dunesFreq
      5. Rifts (inverted ridged)             → riftsMagn, riftsFreq
      6. Canyons (sharp cuts)                → canyonsMagn, canyonsFreq
      7. Cracks (fine fractures)             → cracksMagn, cracksFreq
      8. Volcanoes (cone shapes)             → volcanoMagn, volcanoDensity
      9. Craters (impact depressions + rims) → craterMagn, craterDensity
     10. Erosion smoothing                   → erosion
    """
    lon, lat, lat_sin, lat_abs, cos_lat = _GRIDS

    # ── Stage 1: Continental skeleton ─────────────────────────────────────
    # Low frequency → large continent/ocean shapes
    continent = _fbm(lon, lat,
                     base_freq=p["mainFreq"] * 0.35,
                     octaves=5, lacunarity=2.0, gain=0.55,
                     seed=seed)

    # ── Stage 2: Mountain systems ─────────────────────────────────────────
    # montesMagn    → amplitude of mountain ranges
    # montesSpiky   → blend ridged (spiky) vs smooth FBM
    # montesFraction→ fraction of terrain covered by mountains
    if p["montesMagn"] > 0.01:
        mtn_ridged = _ridged(lon, lat,
                             base_freq=p["montesFreq"] * 0.8,
                             octaves=5, seed=seed + 10)
        mtn_smooth = _fbm(lon, lat,
                          base_freq=p["montesFreq"],
                          octaves=4, lacunarity=2.1, gain=0.5,
                          seed=seed + 11)
        mountains = (mtn_ridged * p["montesSpiky"] +
                     mtn_smooth * (1.0 - p["montesSpiky"])) * p["montesMagn"]
        # Only apply where continent is already high (montesFraction mask)
        mtn_mask = np.clip((continent + 0.5) * p["montesFraction"] * 2.0, 0.0, 1.0)
        mountains = mountains * mtn_mask
    else:
        mountains = np.float32(0.0)

    # ── Stage 3: Hills ────────────────────────────────────────────────────
    # hillsMagn  → amplitude
    # hillsFraction → coverage blend
    if p["hillsMagn"] > 0.01:
        hills = _fbm(lon, lat,
                     base_freq=p["hillsFreq"],
                     octaves=4, lacunarity=2.0, gain=0.5,
                     seed=seed + 20) * p["hillsMagn"]
        h2 = _fbm(lon, lat,
                  base_freq=p["hillsFreq"] * 1.7,
                  octaves=3, lacunarity=2.0, gain=0.4,
                  seed=seed + 21) * p["hillsMagn"] * p["hills2Fraction"]
        hills = hills * p["hillsFraction"] + h2
    else:
        hills = np.float32(0.0)

    # ── Stage 4: Dunes ────────────────────────────────────────────────────
    # High frequency, low amplitude, additive texture
    if p["dunesMagn"] > 0.01:
        dunes = _fbm(lon, lat,
                     base_freq=p["dunesFreq"],
                     octaves=3, lacunarity=2.2, gain=0.4,
                     seed=seed + 30) * p["dunesMagn"] * p["dunesFraction"]
    else:
        dunes = np.float32(0.0)

    # ── Stage 5: Rifts (negative elevation) ──────────────────────────────
    # riftsMagn → depth of rift valleys
    if p["riftsMagn"] > 0.01:
        rift_raw = _ridged(lon, lat,
                           base_freq=p["riftsFreq"],
                           octaves=4, seed=seed + 40)
        # Invert ridged peaks → rift valleys (subtract from terrain)
        rifts = -(1.0 - rift_raw) * p["riftsMagn"] * 0.4
    else:
        rifts = np.float32(0.0)

    # ── Stage 6: Canyons ─────────────────────────────────────────────────
    # Sharp narrow cuts
    if p["canyonsMagn"] > 0.01:
        canyon_raw = _fbm(lon, lat,
                          base_freq=p["canyonsFreq"],
                          octaves=3, lacunarity=2.5, gain=0.4,
                          seed=seed + 50)
        # Sharpen into narrow cuts using abs
        canyons = -np.abs(canyon_raw) * p["canyonsMagn"] * p["canyonsFraction"] * 0.5
    else:
        canyons = np.float32(0.0)

    # ── Stage 7: Cracks ───────────────────────────────────────────────────
    if p["cracksMagn"] > 0.01:
        crack_raw = _value_noise(lon, lat,
                                 freq=p["cracksFreq"] * 2.0,
                                 seed=seed + 60)
        cracks = -np.abs(crack_raw) * p["cracksMagn"] * 0.15
    else:
        cracks = np.float32(0.0)

    # ── Stage 8: Volcanoes ────────────────────────────────────────────────
    # Generate cone-shaped elevations using local distance fields
    # volcanoDensity → number of cones, volcanoMagn → height
    if p["volcanoMagn"] > 0.01 and p["volcanoDensity"] > 0.01:
        # Use FBM to place volcano centres (peaks of ridged noise)
        vol_base = _ridged(lon, lat,
                           base_freq=p["volcanoFreq"],
                           octaves=3, seed=seed + 70)
        # Sharpen peaks: only high values become volcanoes
        threshold = 1.0 - p["volcanoDensity"] * 0.8
        vol_cones = np.clip((vol_base - threshold) / (1.0 - threshold + 1e-6), 0.0, 1.0)
        vol_cones = vol_cones ** 2  # steepen cone flanks
        # Caldera: subtract small depression at very peak
        caldera = np.clip((vol_base - (threshold + 0.15)) / 0.1, 0.0, 1.0) * 0.3
        volcanoes = (vol_cones - caldera) * p["volcanoMagn"] * 0.6
        # Lava flows: extend from flanks
        if p["volcanoFlows"] > 0.01:
            flow_noise = _fbm(lon, lat,
                              base_freq=p["volcanoFreq"] * 2.0,
                              octaves=2, lacunarity=2.0, gain=0.5,
                              seed=seed + 71)
            flows = np.clip(vol_base * flow_noise * p["volcanoFlows"] * 0.3, 0.0, 0.15)
            volcanoes = volcanoes + flows
    else:
        volcanoes = np.float32(0.0)

    # ── Stage 9: Craters ─────────────────────────────────────────────────
    # craterDensity → how many, craterMagn → depth
    if p["craterMagn"] > 0.01 and p["craterDensity"] > 0.01:
        crater_raw = _fbm(lon, lat,
                          base_freq=p["craterFreq"] * 1.5,
                          octaves=3, lacunarity=2.5, gain=0.5,
                          seed=seed + 80)
        # Bowl shape: abs(noise) → depression, with raised rim
        crater_bowl = -np.abs(crater_raw) * p["craterMagn"] * p["craterDensity"]
        crater_rim  =  np.abs(crater_raw) * p["craterMagn"] * p["craterDensity"] * 0.3
        # Only apply where terrain is already low (craters on flat plains)
        craters = crater_bowl + crater_rim
    else:
        craters = np.float32(0.0)

    # ── Combine all stages ────────────────────────────────────────────────
    height = (continent  * 0.45 +
              mountains  * 0.22 +
              hills      * 0.12 +
              dunes      * 0.04 +
              rifts            +
              canyons          +
              cracks           +
              volcanoes        +
              craters)

    # ── Stage 10: Erosion smoothing ───────────────────────────────────────
    # erosion → lerp toward lowpass version
    if p["erosion"] > 0.05:
        # Simple box-blur approximation (no scipy needed)
        k = max(2, int(p["erosion"] * 5))
        # Horizontal pass
        cs = np.cumsum(height, axis=1)
        smoothed = np.empty_like(height)
        smoothed[:, k:] = (cs[:, k:] - cs[:, :-k]) / k
        smoothed[:, :k] = height[:, :k]
        # Vertical pass
        cs2 = np.cumsum(smoothed, axis=0)
        smoothed2 = np.empty_like(smoothed)
        smoothed2[k:, :] = (cs2[k:, :] - cs2[:-k, :]) / k
        smoothed2[:k, :] = smoothed[:k, :]
        strength = p["erosion"] * 0.55
        height = height * (1.0 - strength) + smoothed2 * strength

    # ── Normalise to [0, 1] ───────────────────────────────────────────────
    lo, hi = float(height.min()), float(height.max())
    if hi > lo:
        height = (height - lo) / (hi - lo)
    else:
        height = np.full_like(height, 0.5)

    # ── Sea-level bias ────────────────────────────────────────────────────
    # seaLevel controls what fraction is ocean.
    # Shift distribution so (1 - seaLevel) fraction is above 0.5.
    target_land = max(0.03, min(0.97, 1.0 - p["seaLevel"]))
    land_threshold = float(np.percentile(height, (1.0 - target_land) * 100.0))
    if land_threshold > 0.01:
        # Stretch so land_threshold maps to 0.5
        height = height / (land_threshold * 2.0)
        lo2, hi2 = float(height.min()), float(height.max())
        if hi2 > lo2:
            height = (height - lo2) / (hi2 - lo2)

    return np.clip(height, 0.0, 1.0).astype(np.float32)


# ─────────────────────────────────────────────────────────────────────────────
# TEMPERATURE MAP  (data.surface ch0)
# ─────────────────────────────────────────────────────────────────────────────

def generate_temperature(p: dict, seed: int,
                         height_map: np.ndarray,
                         est_temp_k: float = 288.0) -> np.ndarray:
    """
    Returns float32 (H, W) surface temperature in Kelvin.

    Sources:
      climatePole    → pole temperature fraction
      climateEquator → equatorial temperature fraction
      tropicLatitude → latitude of tropic band
      icecapLatitude → where polar ice begins
      heightTempGrad → altitude cooling rate
      height_map     → elevation (above sea level = cooler)
    """
    lon, lat, lat_sin, lat_abs, cos_lat = _GRIDS

    # ── Latitude gradient ─────────────────────────────────────────────────
    # Confirmed from 100-Earth dataset: cos(lat) falloff, ~252K poles, ~298K equator
    temp_swing = est_temp_k * 0.18
    T_equator  = est_temp_k + temp_swing * 0.40 * p["climateEquator"]
    T_pole     = est_temp_k - temp_swing * 0.60 * max(0.01, 1.0 - p["climatePole"])
    T_lat      = (T_pole + (T_equator - T_pole) * cos_lat).astype(np.float32)

    # ── Tropic warm band ──────────────────────────────────────────────────
    # tropicLatitude → half-width of warm equatorial band
    tropic_band = np.exp(-lat_abs ** 2 / (2 * (p["tropicLatitude"] + 0.05) ** 2))
    T_lat = T_lat + tropic_band * temp_swing * 0.1 * p["climateTropic"]

    # ── Altitude cooling ──────────────────────────────────────────────────
    # Only cools above sea level (height_map > seaLevel)
    elev_above_sea = np.maximum(0.0, height_map - p["seaLevel"]) / max(0.01, 1.0 - p["seaLevel"])
    alt_cooling    = elev_above_sea * p["heightTempGrad"] * temp_swing * 0.7
    T_lat          = T_lat - alt_cooling

    # ── Polar ice caps ────────────────────────────────────────────────────
    # icecapLatitude: fraction of pole that is glaciated
    # Below icecapLatitude → hard temperature cap
    if p["icecapLatitude"] < 0.99:
        ice_fade = np.clip((lat_abs - p["icecapLatitude"]) / 0.04, 0.0, 1.0)
        T_ice    = np.minimum(T_lat, np.float32(243.0))
        T_lat    = T_lat * (1.0 - ice_fade) + T_ice * ice_fade

    # ── Snow on high mountains ────────────────────────────────────────────
    snow_elev = p["snowLevel"]
    elev_snow_frac = np.clip((height_map - snow_elev) / 0.1, 0.0, 1.0)
    # Snow only forms where temperature is already cold — gates warm tropical peaks
    temp_snow_eligible = np.clip((275.0 - T_lat) / 15.0, 0.0, 1.0)
    mountain_snow = elev_snow_frac * temp_snow_eligible
    T_snow = np.minimum(T_lat, np.float32(263.0))
    T_lat  = T_lat * (1.0 - mountain_snow) + T_snow * mountain_snow

    # ── Small noise perturbation (±2K local variation) ────────────────────
    # Land vs ocean thermal contrast
    is_land   = (height_map > p["seaLevel"]).astype(np.float32)
    ocean_mod = (1.0 - is_land) * 0.15
    land_mod  = is_land * 0.10
    T_lat = T_lat * (1.0 + land_mod - ocean_mod)

    # Terrain-coupled weather-pattern-scale variation
    variation = _fbm(lon, lat,
                     base_freq=p["mainFreq"] * 0.4,
                     octaves=4, lacunarity=2.1, gain=0.55,
                     seed=seed + 100)
    variation_scale = (0.12 + cos_lat * 0.12) * temp_swing
    T_map = T_lat + variation * variation_scale

    return np.clip(T_map, 50.0, 1500.0).astype(np.float32)


# ─────────────────────────────────────────────────────────────────────────────
# HUMIDITY MAP  (internal — drives biomes)
# ─────────────────────────────────────────────────────────────────────────────

def generate_humidity(p: dict, seed: int,
                      height_map: np.ndarray,
                      T_map: np.ndarray) -> np.ndarray:
    """
    Returns float32 (H, W) in [0, 1].

    humidity    → global moisture baseline
    riversMagn  → local river-fed wetness
    Coasts and low elevations are wetter.
    Hot+dry areas become desert.
    """
    lon, lat, lat_sin, lat_abs, cos_lat = _GRIDS

    # Base humidity from parameter
    base = p["humidity"]

    # Coastal wetness: proximity to sea level = more moisture
    coast = 1.0 - np.abs(height_map - p["seaLevel"]) * 3.0
    coast = np.clip(coast, 0.0, 1.0).astype(np.float32)

    # Equatorial / tropic rainfall band
    rain_lat = np.exp(-lat_abs ** 2 / 0.15)  # humid near equator

    # River network (adds local streaks of moisture)
    if p["riversMagn"] > 0.01:
        river_noise = _fbm(lon, lat,
                           base_freq=p["riversFreq"],
                           octaves=3, lacunarity=2.0, gain=0.5,
                           seed=seed + 200)
        river_wet = np.clip(river_noise * p["riversMagn"] * 0.4, 0.0, 0.3)
    else:
        river_wet = np.float32(0.0)

    # Altitude reduces humidity (rain shadow)
    elev_above = np.maximum(0.0, height_map - p["seaLevel"])
    alt_dry    = elev_above * 0.5

    humid_noise = _fbm(lon, lat,
                       base_freq=p["mainFreq"] * 0.6,
                       octaves=4, lacunarity=2.0, gain=0.55,
                       seed=seed + 210)

    humidity = (base * 0.35 +
                coast * 0.30 +
                rain_lat.astype(np.float32) * base * 0.10 +
                humid_noise * 0.20 +
                river_wet -
                alt_dry)

    # Hot areas → drier (temperature feedback)
    T_norm = np.clip((T_map - 250.0) / 150.0, 0.0, 1.0)
    humidity = humidity - T_norm * 0.15

    return np.clip(humidity, 0.0, 1.0).astype(np.float32)


# ─────────────────────────────────────────────────────────────────────────────
# BIOME MAP  (internal — drives albedo)
# ─────────────────────────────────────────────────────────────────────────────

def generate_biomes(p: dict,
                    height_map: np.ndarray,
                    T_map: np.ndarray,
                    humidity: np.ndarray) -> np.ndarray:
    """
    Returns integer array (H, W) with biome IDs.

    0 = deep ocean     4 = steppe/grassland
    1 = shallow ocean  5 = forest
    2 = beach/coast    6 = jungle
    3 = desert/barren  7 = tundra
    8 = mountain rock  9 = snow/ice
   10 = lava/volcanic
    """
    sea  = p["seaLevel"]
    snow = p["snowLevel"]

    is_ocean   = height_map < sea
    is_shallow = (height_map >= sea) & (height_map < sea + 0.04)
    is_beach   = (height_map >= sea + 0.04) & (height_map < sea + 0.06)
    is_land    = height_map >= sea + 0.06
    is_high    = height_map > snow
    is_volcano = (height_map > snow * 0.7) & (p["volcanoMagn"] > 0.3) & (height_map > 0.75)

    T_C = T_map - 273.15  # Celsius

    biome = np.zeros_like(height_map, dtype=np.int8)
    biome[is_ocean]   = 0
    biome[is_shallow] = 1
    biome[is_beach]   = 2

    # Land biomes from temperature + humidity
    desert  = is_land & (humidity < 0.25) & (T_C > 5)
    tundra  = is_land & (T_C < -5) & ~is_high
    steppe  = is_land & (humidity >= 0.25) & (humidity < 0.45) & (T_C >= -5)
    forest  = is_land & (humidity >= 0.45) & (humidity < 0.7)  & (T_C >= 0)
    jungle  = is_land & (humidity >= 0.7)  & (T_C >= 15)
    grass   = is_land & (humidity >= 0.35) & (humidity < 0.6)  & (T_C >= 0) & (T_C < 20)
    mtn     = is_land & is_high & ~is_volcano
    snow_b  = is_land & (T_C < -15)
    lava_b  = is_land & is_volcano

    biome[desert]  = 3
    biome[steppe]  = 4
    biome[grass]   = 4
    biome[forest]  = 5
    biome[jungle]  = 6
    biome[tundra]  = 7
    biome[mtn]     = 8
    biome[snow_b]  = 9
    biome[lava_b]  = 10

    return biome


# ─────────────────────────────────────────────────────────────────────────────
# ALBEDO MAP  (data.surface ch1)
# ─────────────────────────────────────────────────────────────────────────────

def generate_albedo(p: dict, seed: int,
                    height_map: np.ndarray,
                    T_map: np.ndarray,
                    humidity: np.ndarray,
                    biome: np.ndarray) -> np.ndarray:
    """
    Returns float32 (H, W) albedo in [0.05, 0.95].
    Based on biome:
      ocean  0.06  desert 0.35  grassland 0.20  forest 0.12
      jungle 0.10  tundra 0.25  rock      0.28  snow   0.80
      lava   0.05
    """
    _BIOME_ALBEDO = np.array(
        [0.06, 0.10, 0.28, 0.35, 0.20, 0.14, 0.09, 0.25, 0.30, 0.75, 0.05],
        dtype=np.float32)

    albedo = _BIOME_ALBEDO[biome.astype(np.int32)]

    # Add small per-pixel texture noise
    noise = _fbm(_GRIDS[0], _GRIDS[1],
                 base_freq=p["mainFreq"] * 2.0,
                 octaves=3, lacunarity=2.0, gain=0.4,
                 seed=seed + 300)
    albedo = albedo + noise * 0.05

    # icecapHeight → partial ice over ocean raises albedo
    ice_lat  = np.abs(_GRIDS[3])
    ice_frac = np.clip((ice_lat - p["icecapLatitude"]) / 0.04, 0.0, 1.0)
    albedo   = albedo * (1.0 - ice_frac) + 0.75 * ice_frac
    
    return np.clip(albedo, 0.05, 0.75).astype(np.float32)


# ─────────────────────────────────────────────────────────────────────────────
# MATERIAL FILE GENERATORS
# ─────────────────────────────────────────────────────────────────────────────

def generate_material0(p: dict, seed: int,
                       T_map: np.ndarray,
                       albedo: np.ndarray,
                       height_map: np.ndarray) -> np.ndarray:
    """
    material0.surface  (H, W, 4)

    ch0 = terrain mask        0–0.32  (sparse, correlates with land)
    ch1 = vapour pressure Pa  102–35000  (Magnus equation from T_map)
    ch2 = secondary mask      0–0.17  (sparse)
    ch3 = continuous mask     0–1
    """
    lon, lat = _GRIDS[0], _GRIDS[1]

    # ch1: Magnus vapour pressure  e_s = 610.78 * exp(17.27*(T-273)/(T-273+237.3))
    Tc  = T_map - 273.15
    e_s = 610.78 * np.exp(17.27 * Tc / np.maximum(Tc + 237.3, 1.0))
    ch1 = np.clip(e_s, 0.0, 40000.0).astype(np.float32)

    # ch0: land mask (nonzero where land exists)
    land_frac = np.clip((height_map - p["seaLevel"]) / 0.3, 0.0, 1.0)
    ch0_noise = _fbm(lon, lat, base_freq=1.2, octaves=3,
                     lacunarity=2.0, gain=0.4, seed=seed + 400)
    ch0 = np.clip(land_frac * 0.20 + ch0_noise * 0.08 + 0.02, 0.0, 0.32).astype(np.float32)

    # ch2: sparse secondary mask
    ch2_noise = _fbm(lon, lat, base_freq=2.0, octaves=2,
                     lacunarity=2.0, gain=0.4, seed=seed + 401)
    ch2 = np.clip(ch2_noise * 0.07 + 0.02, 0.0, 0.17).astype(np.float32)

    # ch3: continuous 0–1 (albedo-based)
    ch3 = np.clip(albedo, 0.0, 1.0).astype(np.float32)

    return np.stack([ch0, ch1, ch2, ch3], axis=-1)


def generate_material1(T_map: np.ndarray) -> np.ndarray:
    """
    material1.surface  (H, W, 4)

    ch1 = rock surface heat capacity J/m²K
    Formula from reverse engineering: 332328 * exp(-0.00509 * T)
    Colder regions → higher capacity (~90k J/m²K)
    Warmer regions → lower capacity  (~67k J/m²K)
    """
    hc     = (332328.0 * np.exp(-0.00509 * T_map)).astype(np.float32)
    zeros  = np.zeros_like(hc)
    return np.stack([zeros, hc, zeros, zeros], axis=-1)


def generate_material2(hc_map: np.ndarray) -> np.ndarray:
    """material2.surface  ch1 = mat1_ch1 / 3.728 (water heat capacity)"""
    m2    = (hc_map / 3.728).astype(np.float32)
    zeros = np.zeros_like(m2)
    return np.stack([zeros, m2, zeros, zeros], axis=-1)


def generate_material3(hc_map: np.ndarray) -> np.ndarray:
    """material3.surface  ch1 = mat1_ch1 / 83.60 (atmosphere heat capacity)"""
    m3    = (hc_map / 83.60).astype(np.float32)
    zeros = np.zeros_like(m3)
    return np.stack([zeros, m3, zeros, zeros], axis=-1)


# ─────────────────────────────────────────────────────────────────────────────
# PER-BODY GENERATION  (public API)
# ─────────────────────────────────────────────────────────────────────────────

def generate_body_surfaces(obj: dict, est_temp_k: float = 288.0,
                            n_bodies_total: int = 1) -> dict:
    """
    Full pipeline for one body.
    n_bodies_total: total solid body count - determines atlas cell size.
    Returns dict with keys: data, material0, material1, material2, material3
    each a float32 (map_h, map_w, 4) array.
    """
    raw  = obj.get("raw_data", {})
    surf = raw.get("Surface", {}) if isinstance(raw.get("Surface"), dict) else {}
    arch = obj.get("archetype", "rocky")
    seed = _make_seed(
        obj.get("name", "unknown"),
        obj.get("radius_m", 1.0),
        obj.get("mass_kg",  1.0),
        safe_float(obj.get("orbit", {}).get("SemiMajorAxis", 0.0)),
    )
    src_t = float(obj.get("source_temp_k") or est_temp_k)
    bw, bh, _, _, _ = atlas_layout(n_bodies_total)

    log_debug(f"Generating terrain for '{obj['name']}' (map={bw}x{bh}) "
              f"(arch={arch}, seed={seed}, T≈{src_t:.0f}K)", "SURFACE")

    # Parse SE parameters
    p = parse_space_engine_surface(surf)

    # Override sea level for non-ocean archetypes with no explicit seaLevel
    if arch == "lava" and "seaLevel" not in surf:
        p["seaLevel"] = 0.05   # mostly land, very little lava sea
    elif arch == "ice" and "seaLevel" not in surf:
        p["seaLevel"] = 0.45

    # Override module-level grids to match this body's actual resolution.
    global _GRIDS, TILE_W, TILE_H
    saved_grids = _GRIDS
    saved_tw, saved_th = TILE_W, TILE_H
    TILE_W = bw
    TILE_H = bh
    _GRIDS = _make_grids_at(bw, bh)

    try:
        # Run stages
        height  = generate_heightmap(p, seed)
        T_map   = generate_temperature(p, seed, height, src_t)
        humid   = generate_humidity(p, seed, height, T_map)
        biome   = generate_biomes(p, height, T_map, humid)
        albedo  = generate_albedo(p, seed, height, T_map, humid, biome)

        # Flip vertically: row 0 = north pole in US2's convention.
        height = height[::-1, :]
        T_map  = T_map[::-1,  :]
        albedo = albedo[::-1, :]

    # Validate — no NaN / Inf
        for name, arr in [("height", height), ("T_map", T_map),
                          ("albedo", albedo)]:
            if not np.isfinite(arr).all():
                log_debug(f"  WARNING: non-finite values in {name}, clamping", "SURFACE_WARN")
                arr[:] = np.nan_to_num(arr, nan=0.5, posinf=1.0, neginf=0.0)

        mat1     = generate_material1(T_map)
        hc_map   = mat1[:, :, 1]
        zeros_hw = np.zeros((bh, bw), dtype=np.float32)

        data_layer = np.stack([T_map, albedo, zeros_hw, zeros_hw], axis=-1)

        result = {
            "data":      data_layer,
            "material0": generate_material0(p, seed, T_map, albedo, height),
            "material1": mat1,
            "material2": generate_material2(hc_map),
            "material3": generate_material3(hc_map),
        }
    finally:
        _GRIDS = saved_grids
        TILE_W = saved_tw
        TILE_H = saved_th

    return result


# ─────────────────────────────────────────────────────────────────────────────
# ATLAS PACKER
# ─────────────────────────────────────────────────────────────────────────────

def atlas_layout(n_bodies: int) -> tuple[int, int, int, int, int]:
    """
    Returns (body_w, body_h, grid_cols, grid_rows, tiles_per_body).

    The 1024x512 atlas is divided into a grid of identical body-map cells.
    Grid grows by alternating row/col doublings, rows first.
    """
    gc, gr = 1, 1
    while gc * gr < n_bodies:
        if gr <= gc:
            gr *= 2
        else:
            gc *= 2
    bw = ATLAS_W // gc
    bh = ATLAS_H // gr
    tw = bw // TILE_W
    th = bh // TILE_H
    return bw, bh, gc, gr, tw * th


def body_atlas_tiles(body_idx: int, n_bodies: int) -> list[int]:
    """
    Returns the ordered list of AtlasIndex values that belong to body_idx.
    The first element is what goes in SurfaceGridComponent.AtlasIndex.
    """
    bw, bh, gc, _, _ = atlas_layout(n_bodies)
    bc = body_idx % gc
    br = body_idx // gc
    px0 = bc * bw
    py0 = br * bh
    tx0 = px0 // TILE_W
    ty0 = py0 // TILE_H
    tw  = bw  // TILE_W
    th  = bh  // TILE_H
    indices = []
    for ty in range(th):
        for tx in range(tw):
            indices.append((ty0 + ty) * ATLAS_COLS + (tx0 + tx))
    return indices


def _pack_atlas(body_tiles: list, n_bodies_total: int, n_channels: int) -> np.ndarray:
    atlas = np.zeros((ATLAS_H, ATLAS_W, n_channels), dtype=np.float32)
    bw, bh, gc, gr, _ = atlas_layout(n_bodies_total)

    for body_idx, body_map in enumerate(body_tiles):
        if body_idx >= gc * gr:
            log_debug(f"WARNING: body {body_idx} exceeds atlas capacity", "SURFACE_WARN")
            break
        bc = body_idx % gc
        br = body_idx // gc
        x0 = bc * bw
        y0 = br * bh

        src_h, src_w = body_map.shape[:2]

        if src_h == bh and src_w == bw:
            atlas[y0:y0+bh, x0:x0+bw] = body_map
            continue

        # Bilinear stretch — maps src coordinates to dst coordinates
        # Never tiles; always covers the full bw×bh destination region
        ry = np.clip(
            (np.arange(bh, dtype=np.float32) + 0.5) * src_h / bh - 0.5,
            0, src_h - 1)
        rx = np.clip(
            (np.arange(bw, dtype=np.float32) + 0.5) * src_w / bw - 0.5,
            0, src_w - 1)
        y0i = np.floor(ry).astype(np.int32);  y1i = np.minimum(y0i + 1, src_h - 1)
        x0i = np.floor(rx).astype(np.int32);  x1i = np.minimum(x0i + 1, src_w - 1)
        fy = (ry - y0i)[:, np.newaxis]
        fx = (rx - x0i)[np.newaxis, :]

        resized = np.empty((bh, bw, n_channels), dtype=np.float32)
        for ch in range(n_channels):
            s = body_map[:, :, ch]
            resized[:, :, ch] = (s[np.ix_(y0i, x0i)] * (1-fy) * (1-fx) +
                                 s[np.ix_(y1i, x0i)] *    fy  * (1-fx) +
                                 s[np.ix_(y0i, x1i)] * (1-fy) *    fx  +
                                 s[np.ix_(y1i, x1i)] *    fy  *    fx)
        atlas[y0:y0+bh, x0:x0+bw] = resized

    return atlas

# ─────────────────────────────────────────────────────────────────────────────
# ARCHIVE WRITER
# ─────────────────────────────────────────────────────────────────────────────

def write_surface_archive(bodies: list, output, system_name: str) -> None:
    """
    Generate and write the surface archive.
    `output` — BytesIO or file path.
    """
    if not bodies:
        log_debug("No solid-surface bodies — skipping surface archive", "SURFACE")
        return

    n = len(bodies)
    n_for_atlas = min(n, ATLAS_CAPACITY)
    bw, bh, gc, gr, tpb = atlas_layout(n_for_atlas)
    capacity = min(gc * gr, ATLAS_CAPACITY)

    if n > capacity:
        log_debug(f"WARNING: {n} bodies exceed atlas capacity {capacity}. "
                  f"First {capacity} only.", "SURFACE_WARN")

    log_debug(f"Generating surface data for {n} bodies "
              f"(atlas grid {gc}x{gr}, {bw}x{bh}px per body, {tpb} tiles each)...",
              "SURFACE")

    data_t = []; m0_t = []; m1_t = []; m2_t = []; m3_t = []

    for obj in bodies[:capacity]:
        src_t  = float(obj.get("source_temp_k") or 288.0)
        layers = generate_body_surfaces(obj, est_temp_k=src_t, n_bodies_total=n_for_atlas)
        data_t.append(layers["data"])
        m0_t.append(layers["material0"])
        m1_t.append(layers["material1"])
        m2_t.append(layers["material2"])
        m3_t.append(layers["material3"])

    log_debug("Packing atlas...", "SURFACE")
    da = _pack_atlas(data_t, n_for_atlas, 4)
    m0 = _pack_atlas(m0_t,  n_for_atlas, 4)
    m1 = _pack_atlas(m1_t,  n_for_atlas, 4)
    m2 = _pack_atlas(m2_t,  n_for_atlas, 4)
    m3 = _pack_atlas(m3_t,  n_for_atlas, 4)

    info = json.dumps({"size": 512}, indent=2)

    log_debug("Writing surface archive...", "SURFACE")
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("info",              info)
        zf.writestr("data.surface",      da.astype("<f4").tobytes())
        zf.writestr("material0.surface", m0.astype("<f4").tobytes())
        zf.writestr("material1.surface", m1.astype("<f4").tobytes())
        zf.writestr("material2.surface", m2.astype("<f4").tobytes())
        zf.writestr("material3.surface", m3.astype("<f4").tobytes())

    log_debug(f"Surface archive complete: {n} bodies", "SURFACE")


# ─────────────────────────────────────────────────────────────────────────────
# FILTER
# ─────────────────────────────────────────────────────────────────────────────

def should_generate_surface(obj: dict) -> bool:
    if obj.get("is_star") or obj.get("is_barycenter"):
        return False
    decl_type = obj.get("decl_type", "").strip().lower()
    if decl_type in ("asteroid", "comet"):
        return False
    raw_class = (obj.get("raw_data") or {}).get("Class", "").strip().lower()
    if decl_type == "dwarfmoon" and raw_class == "asteroid":
        return False
    if obj.get("radius_m", 0.0) < 100_000.0:
        return False
    return obj.get("archetype", "") in _SOLID_ARCHETYPES
