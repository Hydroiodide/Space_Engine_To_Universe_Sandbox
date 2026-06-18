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
                    ch1 = optional gas/vapour pressure (Pa); zero by default
                          for static imported atmospheres
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
import os
import time

import numpy as np

from constants import log_debug, safe_float, se_bool, parse_se_surface_preset

# Active channel semantics:
# material0 ch1 = gas/vapor pressure, disabled by default for static imports.
# material0 ch2 = ice/frozen mask.
# material0 ch3 = liquid/water/specular mask; water is high/white, land is low/black.

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

MAX_SECONDS_PER_BODY_SURFACE = 10.0
MAX_CRATERS_PER_BODY = 600
MAX_VOLCANOES_PER_BODY = 80
MAX_RIFTS_PER_BODY = 80
MAX_CANYONS_PER_BODY = 80
MAX_RIVERS_PER_BODY = 120
MAX_EXPENSIVE_OCTAVES = 8
SURFACE_WORK_RES_MAX_W = 512
SURFACE_WORK_RES_MAX_H = 256
SURFACE_FINAL_RES_SINGLE_BODY = (1024, 512)
SURFACE_GENERATE_LOWRES_THEN_UPSCALE = True


# ─────────────────────────────────────────────────────────────────────────────
# SEEDING
# ─────────────────────────────────────────────────────────────────────────────

def _make_seed(name: str, radius_m: float, mass_kg: float, sma_m: float = 0.0,
               preset: str = "", surf_style: str = "",
               randomize: tuple[float, float, float] = (0.0, 0.0, 0.0)) -> int:
    raw = (
        f"{name}|{radius_m:.3f}|{mass_kg:.3e}|{sma_m:.3e}|{preset}|{surf_style}|"
        f"{randomize[0]:.9g}|{randomize[1]:.9g}|{randomize[2]:.9g}"
    )
    return int(hashlib.md5(raw.encode()).hexdigest(), 16) & 0x7FFFFFFF


def _parse_randomize(value) -> tuple[float, float, float]:
    if isinstance(value, (list, tuple)) and len(value) >= 3:
        return tuple(safe_float(value[i], 0.0) for i in range(3))
    text = str(value or "").strip().strip("()[]")
    parts = [part.strip() for part in text.replace(";", ",").split(",")]
    if len(parts) >= 3:
        return tuple(safe_float(parts[i], 0.0) for i in range(3))
    return 0.0, 0.0, 0.0


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


def _sphere_xyz(lon: np.ndarray, lat: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    cos_lat = np.cos(lat)
    return (
        (cos_lat * np.cos(lon)).astype(np.float32),
        np.sin(lat).astype(np.float32),
        (cos_lat * np.sin(lon)).astype(np.float32),
    )


def _hash3(ix: np.ndarray, iy: np.ndarray, iz: np.ndarray, seed: int) -> np.ndarray:
    """Deterministic integer hash returning decorrelated values in [-1, 1]."""
    x = ix.astype(np.uint32, copy=False)
    y = iy.astype(np.uint32, copy=False)
    z = iz.astype(np.uint32, copy=False)
    h = x * np.uint32(0x8DA6B343) ^ y * np.uint32(0xD8163841)
    h ^= z * np.uint32(0xCB1AB31F) ^ np.uint32(seed & 0xFFFFFFFF)
    h ^= h >> np.uint32(16)
    h *= np.uint32(0x7FEB352D)
    h ^= h >> np.uint32(15)
    h *= np.uint32(0x846CA68B)
    h ^= h >> np.uint32(16)
    return (h.astype(np.float64) / 2147483647.5 - 1.0).astype(np.float32)


def _value_noise3(x: np.ndarray, y: np.ndarray, z: np.ndarray,
                  frequency: float, seed: int,
                  offset: tuple[float, float, float] = (0.0, 0.0, 0.0)) -> np.ndarray:
    frequency = max(0.01, float(frequency))
    px = x * frequency + np.float32(offset[0])
    py = y * frequency + np.float32(offset[1])
    pz = z * frequency + np.float32(offset[2])
    x0 = np.floor(px).astype(np.int32); y0 = np.floor(py).astype(np.int32); z0 = np.floor(pz).astype(np.int32)
    tx = _smooth(px - x0); ty = _smooth(py - y0); tz = _smooth(pz - z0)
    out = np.zeros_like(x, dtype=np.float32)
    for dz in (0, 1):
        wz = tz if dz else 1.0 - tz
        for dy in (0, 1):
            wy = ty if dy else 1.0 - ty
            for dx in (0, 1):
                wx = tx if dx else 1.0 - tx
                out += _hash3(x0 + dx, y0 + dy, z0 + dz, seed) * wx * wy * wz
    return out


def _spherical_fbm(x: np.ndarray, y: np.ndarray, z: np.ndarray,
                   base_frequency: float, octaves: int, seed: int,
                   offset: tuple[float, float, float] = (0.0, 0.0, 0.0),
                   lacunarity: float = 2.0, gain: float = 0.5,
                   max_frequency: float | None = None) -> np.ndarray:
    result = np.zeros_like(x, dtype=np.float32)
    amplitude = 1.0
    norm = 0.0
    frequency = max(0.01, float(base_frequency))
    for octave in range(max(1, int(octaves))):
        if max_frequency is not None and frequency > max_frequency:
            break
        result += _value_noise3(x, y, z, frequency, seed + octave * 1013, offset) * amplitude
        norm += amplitude
        amplitude *= gain
        frequency *= lacunarity
    return result / max(norm, 1e-9)


def _spherical_ridged(x: np.ndarray, y: np.ndarray, z: np.ndarray,
                      frequency: float, octaves: int, seed: int,
                      offset: tuple[float, float, float] = (0.0, 0.0, 0.0),
                      max_frequency: float | None = None) -> np.ndarray:
    n = _spherical_fbm(x, y, z, frequency, octaves, seed, offset,
                       lacunarity=2.05, gain=0.52, max_frequency=max_frequency)
    return np.square(1.0 - np.abs(n)).astype(np.float32)


# ─────────────────────────────────────────────────────────────────────────────
# SE SURFACE PARAMETER PARSER
# ─────────────────────────────────────────────────────────────────────────────

SE_SURFACE_PARAM_RANGES = {
    "seaLevel": (-1.0, 2.0), "snowLevel": (-1.0, 2.0),
    "tropicLatitude": (0.0, 1.0), "icecapLatitude": (0.0, 2.0),
    "icecapHeight": (0.0, 2.0), "climatePole": (0.0, 1.0),
    "climateTropic": (0.0, 1.0), "climateEquator": (0.0, 1.0),
    "humidity": (0.0, 1.0), "heightTempGrad": (0.0, 1.0),
    "beachWidth": (0.0, 0.002), "tropicWidth": (0.0, 1.0),
    "mainFreq": (0.0, 5.0), "venusFreq": (0.0, 2.0),
    "venusMagn": (0.0, 5.0), "mareFreq": (0.001, 1e3),
    "mareDensity": (0.0, 1.0), "terraceProb": (0.0, 1.0),
    "erosion": (0.0, 1.0), "montesMagn": (0.0, 10.0),
    "montesFreq": (0.0, 1e3), "montesSpiky": (0.0, 1.0),
    "montesFraction": (0.0, 1.0), "dunesMagn": (0.0, 10.0),
    "dunesFreq": (0.0, 1e5), "dunesFraction": (0.0, 1.0),
    "hillsMagn": (0.0, 10.0), "hillsFreq": (0.0, 1e4),
    "hillsFraction": (0.0, 1.0), "hills2Fraction": (0.0, 1.0),
    "riversMagn": (0.0, 100.0), "riversFreq": (0.0, 10.0),
    "riversSin": (0.0, 10.0), "riftsMagn": (0.0, 100.0),
    "riftsFreq": (0.0, 10.0), "riftsSin": (0.0, 10.0),
    "eqridgeMagn": (0.0, 1.0), "eqridgeWidth": (0.001, 1.0),
    "eqridgeModMagn": (0.0, 2.5), "eqridgeModFreq": (0.0, 10.0),
    "canyonsMagn": (0.0, 10.0), "canyonsFreq": (0.0, 1e3),
    "canyonsFraction": (0.0, 1.0), "cracksMagn": (0.0, 10.0),
    "cracksFreq": (0.0, 15.0), "cracksOctaves": (0.0, 15.0),
    "craterMagn": (0.0, 10.0), "craterFreq": (0.0, 100.0),
    "craterDensity": (0.0, 1.0), "craterOctaves": (0.0, 30.0),
    "craterRayedFactor": (0.0, 1.0), "volcanoMagn": (0.0, 1.0),
    "volcanoFreq": (0.0, 2.0), "volcanoDensity": (0.0, 1.0),
    "volcanoOctaves": (0.0, 5.0), "volcanoActivity": (0.0, 2.0),
    "volcanoFlows": (0.0, 1.0), "volcanoRadius": (0.0, 1.0),
    "volcanoTemp": (0.0, 3000.0), "lavaCoverTidal": (0.0, 1.0),
    "lavaCoverSun": (0.0, 1.0), "lavaCoverYoung": (0.0, 1.0),
    "colorDistMagn": (0.0, 1.0), "colorDistFreq": (0.0001, 1e4),
    "BumpHeight": (0.001, 1000.0), "BumpOffset": (0.001, 1000.0),
}


def clamp01(x):
    return max(0.0, min(1.0, float(x)))


def norm_param(name, value, default=0.0):
    lo, hi = SE_SURFACE_PARAM_RANGES.get(name, (0.0, 1.0))
    if hi == lo:
        return default
    return clamp01((float(value) - lo) / (hi - lo))


def log_norm_param(name, value, default=0.0):
    lo, hi = SE_SURFACE_PARAM_RANGES.get(name, (0.0, 1.0))
    value = float(value)
    if value <= 0:
        return 0.0
    if hi <= lo:
        return default
    lo = max(lo, 1e-9)
    value = max(value, lo)
    return clamp01((math.log10(value) - math.log10(lo)) / (math.log10(hi) - math.log10(lo)))


def _smoothstep(edge0: float, edge1: float, x: np.ndarray) -> np.ndarray:
    if edge1 == edge0:
        return (x >= edge1).astype(np.float32)
    if edge1 < edge0:
        t = np.clip((edge0 - x) / (edge0 - edge1), 0.0, 1.0)
    else:
        t = np.clip((x - edge0) / (edge1 - edge0), 0.0, 1.0)
    return (t * t * (3.0 - 2.0 * t)).astype(np.float32)


def _normalize_percentile(arr: np.ndarray,
                          p_lo: float = 1.0,
                          p_hi: float = 99.0) -> np.ndarray:
    lo, hi = np.percentile(arr, [p_lo, p_hi])
    if not np.isfinite(lo) or not np.isfinite(hi) or hi <= lo:
        lo, hi = float(np.nanmin(arr)), float(np.nanmax(arr))
    if hi <= lo:
        return np.full_like(arr, 0.5, dtype=np.float32)
    return np.clip((arr - lo) / (hi - lo), 0.0, 1.0).astype(np.float32)


def _box_blur_wrap(arr: np.ndarray, radius: int = 1, passes: int = 1) -> np.ndarray:
    out = arr.astype(np.float32, copy=True)
    radius = int(max(0, radius))
    if radius == 0:
        return out
    for _ in range(max(1, passes)):
        acc = out.copy()
        count = 1.0
        for r in range(1, radius + 1):
            acc += np.roll(out, r, axis=1) + np.roll(out, -r, axis=1)
            count += 2.0
        out = acc / count

        acc = out.copy()
        count = 1.0
        idx = np.arange(out.shape[0])
        for r in range(1, radius + 1):
            acc += np.take(out, np.clip(idx + r, 0, out.shape[0] - 1), axis=0)
            acc += np.take(out, np.clip(idx - r, 0, out.shape[0] - 1), axis=0)
            count += 2.0
        out = acc / count
    return out.astype(np.float32)


def _sample_wrap(arr: np.ndarray, x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Bilinear sample an equirectangular map with horizontal wrapping."""
    h, w = arr.shape
    x = np.mod(x, w)
    y = np.clip(y, 0.0, h - 1.0)
    x0 = np.floor(x).astype(np.int32)
    y0 = np.floor(y).astype(np.int32)
    x1 = (x0 + 1) % w
    y1 = np.minimum(y0 + 1, h - 1)
    fx = (x - x0).astype(np.float32)
    fy = (y - y0).astype(np.float32)
    return (arr[y0, x0] * (1.0 - fx) * (1.0 - fy) +
            arr[y0, x1] * fx * (1.0 - fy) +
            arr[y1, x0] * (1.0 - fx) * fy +
            arr[y1, x1] * fx * fy).astype(np.float32)


def _force_horizontal_wrap(arr: np.ndarray) -> np.ndarray:
    if arr.shape[1] > 1:
        arr = arr.copy()
        arr[:, -1] = arr[:, 0]
    return arr.astype(np.float32)


def _surface_stage_log(body: str, stage: str, started: float,
                       size: tuple[int, int] | None = None,
                       count: int | None = None,
                       total: bool = False) -> float:
    elapsed = time.perf_counter() - started
    bits = [f"[surface-stage] body='{body}'", f"stage='{stage}'"]
    if size:
        bits.append(f"size={size[0]}x{size[1]}")
    if count is not None:
        bits.append(f"count={count}")
    bits.append(("total" if total else "time") + f"={elapsed:.3f}s")
    log_debug(" ".join(bits), "SURFACE")
    return time.perf_counter()


def _surface_budget_exceeded(body: str, started: float, stage: str) -> bool:
    elapsed = time.perf_counter() - started
    if elapsed <= MAX_SECONDS_PER_BODY_SURFACE:
        return False
    log_debug(
        f"[surface-stage] body='{body}' stage='{stage}' warning='time budget exceeded; skipping optional detail' "
        f"elapsed={elapsed:.3f}s budget={MAX_SECONDS_PER_BODY_SURFACE:.1f}s",
        "SURFACE_WARN",
    )
    return True


def _resize_float_map(arr: np.ndarray, out_w: int, out_h: int) -> np.ndarray:
    src_h, src_w = arr.shape[:2]
    if src_w == out_w and src_h == out_h:
        return arr.astype(np.float32, copy=False)
    ry = np.clip(
        (np.arange(out_h, dtype=np.float32) + 0.5) * src_h / out_h - 0.5,
        0, src_h - 1)
    rx = np.clip(
        (np.arange(out_w, dtype=np.float32) + 0.5) * src_w / out_w - 0.5,
        0, src_w - 1)
    y0 = np.floor(ry).astype(np.int32)
    x0 = np.floor(rx).astype(np.int32)
    y1 = np.minimum(y0 + 1, src_h - 1)
    x1 = np.minimum(x0 + 1, src_w - 1)
    fy = (ry - y0)[:, np.newaxis]
    fx = (rx - x0)[np.newaxis, :]
    resized = (
        arr[np.ix_(y0, x0)] * (1.0 - fy) * (1.0 - fx) +
        arr[np.ix_(y1, x0)] * fy * (1.0 - fx) +
        arr[np.ix_(y0, x1)] * (1.0 - fy) * fx +
        arr[np.ix_(y1, x1)] * fy * fx
    )
    return resized.astype(np.float32)


def parse_space_engine_surface(surf: dict) -> dict:
    from constants import safe_float

    def g(key, default):
        return safe_float(surf.get(key, default))

    def freq(key, default, out_min, out_max):
        raw = g(key, default)
        lo, hi = SE_SURFACE_PARAM_RANGES.get(key, (0.0, 1.0))
        if hi / max(lo, 1e-9) > 100.0:
            n = log_norm_param(key, raw, 0.0)
        else:
            n = norm_param(key, raw, 0.0)
        return out_min + n * (out_max - out_min)

    def magn(key, default, se_max):
        return norm_param(key, g(key, default), max(0.0, min(1.0, default / se_max if se_max else default)))

    def direct(key, default):
        return max(0.0, min(1.0, g(key, default)))

    p = {}

    # Sea / snow  (SE: -1→2)
    raw_sl   = g("seaLevel",  0.3)
    raw_snow = g("snowLevel", 0.8)

    # seaLevel: treat as direct ocean fraction ÷ 2
    # SE=0   → 0% ocean, SE=1 → 50%, SE=2 → 100%, SE=-1 → 0% (no ocean)
    p["sourceSeaLevel"] = raw_sl
    p["seaLevel"] = max(0.0, min(0.98, raw_sl / 2.0)) if raw_sl > 0 else 0.0

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
    p["climateSteppeMin"] = direct("climateSteppeMin", 0.25)
    p["climateSteppeMax"] = direct("climateSteppeMax", 0.55)
    p["climateForestMin"] = direct("climateForestMin", 0.45)
    p["climateForestMax"] = direct("climateForestMax", 0.80)
    p["climateGrassMin"] = direct("climateGrassMin", 0.35)
    p["climateGrassMax"] = direct("climateGrassMax", 0.75)

    # Frequencies  (SE → noise-space 0.05–3.0)
    p["mainFreq"]    = freq("mainFreq",    1.0, 0.5, 6.0)
    p["venusFreq"]   = freq("venusFreq",   0.4, 1.0, 12.0)
    p["mareFreq"]    = freq("mareFreq",    0.01, 1.0, 24.0)
    p["montesFreq"]  = freq("montesFreq",  1.0, 2.0, 64.0)
    p["hillsFreq"]   = freq("hillsFreq",   1.0, 4.0, 96.0)
    p["dunesFreq"]   = freq("dunesFreq",   2.0, 12.0, 128.0)
    p["riftsFreq"]   = freq("riftsFreq",   1.0, 1.0, 24.0)
    p["canyonsFreq"] = freq("canyonsFreq", 1.0, 2.0, 64.0)
    p["cracksFreq"]  = freq("cracksFreq",  0.5, 8.0, 128.0)
    p["craterFreq"]  = freq("craterFreq",  0.5, 0.5, 1.0)
    p["volcanoFreq"] = freq("volcanoFreq", 0.3, 0.5, 2.0)
    p["riversFreq"]  = freq("riversFreq",  1.0, 2.0, 48.0)

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

    p["venusMagn"]       = magn("venusMagn", 0.0, 5.0)
    p["mareDensity"]     = direct("mareDensity", 0.0)
    p["eqridgeMagn"]     = direct("eqridgeMagn", 0.0)
    p["eqridgeWidth"]    = max(0.001, min(1.0, g("eqridgeWidth", 0.05)))
    p["eqridgeModMagn"]  = max(0.0, min(2.5, g("eqridgeModMagn", 0.0))) / 2.5
    p["eqridgeModFreq"]  = freq("eqridgeModFreq", 1.0, 1.0, 16.0)
    p["cracksOctaves"]   = max(1, min(15, int(round(g("cracksOctaves", 3)))))
    p["craterOctaves"]   = max(1, min(30, int(round(g("craterOctaves", 3)))))
    p["craterRayedFactor"] = direct("craterRayedFactor", 0.0)
    p["volcanoOctaves"]  = max(1, min(5, int(round(g("volcanoOctaves", 2)))))
    p["volcanoTemp"]     = max(0.0, min(3000.0, g("volcanoTemp", 0.0)))
    p["colorDistMagn"]   = direct("colorDistMagn", 0.15)
    p["colorDistFreq"]   = freq("colorDistFreq", 1.0, 1.0, 96.0)
    p["terraceProb"]     = direct("terraceProb", 0.0)

    # BumpHeight is carried as Space Engine kilometers; builder converts it to
    # Universe Sandbox radius-relative relief.
    p["BumpHeight"] = g("BumpHeight", 10.0)
    p["BumpOffset"] = g("BumpOffset", 0.0)

    return p


# ─────────────────────────────────────────────────────────────────────────────
# STAGE 1 — CONTINENTAL HEIGHTMAP
# ─────────────────────────────────────────────────────────────────────────────

def _legacy_generate_heightmap(p: dict, seed: int) -> np.ndarray:
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

def _legacy_generate_temperature(p: dict, seed: int,
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

def _legacy_generate_humidity(p: dict, seed: int,
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

def _legacy_generate_biomes(p: dict,
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

def _legacy_generate_albedo(p: dict, seed: int,
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

def _legacy_generate_material0_reference(p: dict, seed: int,
                                      T_map: np.ndarray,
                                      albedo: np.ndarray,
                                      height_map: np.ndarray) -> np.ndarray:
    """
    Legacy material0 generator retained for reverse-engineering reference only.
    The active pipeline uses the phase-aware generate_material0 defined below.

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


def _legacy_generate_material1_reference(T_map: np.ndarray) -> np.ndarray:
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


def _great_circle_distance(lon: np.ndarray, lat: np.ndarray,
                           lon0: float, lat0: float) -> np.ndarray:
    dlon = (lon - lon0 + math.pi) % (2.0 * math.pi) - math.pi
    s = (np.sin((lat - lat0) * 0.5) ** 2 +
         np.cos(lat) * math.cos(lat0) * np.sin(dlon * 0.5) ** 2)
    return (2.0 * np.arcsin(np.sqrt(np.clip(s, 0.0, 1.0)))).astype(np.float32)


def _feature_centers(seed: int, count: int, polar_ok: bool = False):
    rng = np.random.default_rng(seed & 0xFFFFFFFF)
    for _ in range(max(0, count)):
        lon0 = float(rng.uniform(0.0, 2.0 * math.pi))
        z = float(rng.uniform(-1.0, 1.0) if polar_ok else rng.uniform(-0.88, 0.88))
        yield lon0, math.asin(z), float(rng.random())


def detect_terrain_family(surface, body):
    surf = surface if isinstance(surface, dict) else {}
    preset_info = parse_se_surface_preset(surf.get("Preset", ""))
    if preset_info.get("terrain_family"):
        tf = preset_info["terrain_family"]
        if tf == "volcanic":
            return {"primary": "tectonic_chaotic", "secondary": "rifted", "blend": "max"}
        if tf == "volcanic_mars":
            return {"primary": "volcanic_mars", "secondary": "cratered_sparse", "blend": "masked_add"}
        if tf == "cratered_old":
            return {"primary": "cratered_old", "secondary": "cratered_sparse", "blend": "masked_add"}
        if tf == "icy_fractured":
            return {"primary": "icy_fractured", "secondary": "cratered_sparse", "blend": "max"}
        if tf == "tectonic_chaotic":
            return {"primary": "tectonic_chaotic", "secondary": "rifted", "blend": "max"}
        if tf == "hybrid":
            return {"primary": "hybrid", "secondary": "rifted", "blend": "detail_overlay"}
    raw_class = str((body or {}).get("raw_data", {}).get("Class", (body or {}).get("Class", ""))).lower()
    archetype = str((body or {}).get("archetype", "")).lower()
    hint = " ".join(str(surf.get(k, "")) for k in ("DiffMap", "BumpMap", "Preset")).lower()
    crater_score = norm_param("craterDensity", surf.get("craterDensity", 0.0)) + norm_param("craterMagn", surf.get("craterMagn", 0.0))
    rift_score = norm_param("riftsMagn", surf.get("riftsMagn", 0.0)) + norm_param("canyonsMagn", surf.get("canyonsMagn", 0.0))
    chaos_score = norm_param("venusMagn", surf.get("venusMagn", 0.0)) + norm_param("cracksMagn", surf.get("cracksMagn", 0.0))
    volcanic_score = norm_param("volcanoDensity", surf.get("volcanoDensity", 0.0)) + norm_param("volcanoMagn", surf.get("volcanoMagn", 0.0)) + norm_param("mareDensity", surf.get("mareDensity", 0.0))
    if "mars" in hint or volcanic_score > 0.35:
        primary = "volcanic_mars"
        secondary = "cratered_sparse" if crater_score > 0.08 or "mars" in hint else "rifted"
        blend = "masked_add"
    elif "moon" in hint or crater_score > 0.8:
        primary, secondary, blend = "cratered_old", "cratered_sparse", "masked_add"
    elif "pluto" in hint or "ice" in hint or archetype == "ice" or "ice" in raw_class:
        primary, secondary, blend = "icy_fractured", "cratered_sparse", "max"
    elif "venus" in hint or chaos_score > 0.45:
        primary, secondary, blend = "tectonic_chaotic", "rifted" if rift_score > 0.2 else "icy_fractured", "max"
    elif rift_score > 0.35:
        primary, secondary, blend = "rifted", "tectonic_chaotic", "max"
    else:
        primary, secondary, blend = "hybrid", "rifted" if rift_score > 0.1 else "cratered_sparse", "detail_overlay"
    return {"primary": primary, "secondary": secondary, "blend": blend}


def _surface_options(obj: dict | None) -> dict:
    obj = obj if isinstance(obj, dict) else {}
    raw = obj.get("raw_data", {}) if isinstance(obj.get("raw_data", {}), dict) else {}
    opts = {}
    raw_opts = raw.get("_surface_options", {}) if isinstance(raw.get("_surface_options"), dict) else {}
    obj_opts = obj.get("_surface_options", {}) if isinstance(obj.get("_surface_options"), dict) else {}
    opts.update(raw_opts)
    opts.update(obj_opts)
    return opts


def _surface_option(obj: dict | None, key: str, default=None):
    obj = obj if isinstance(obj, dict) else {}
    raw = obj.get("raw_data", {}) if isinstance(obj.get("raw_data", {}), dict) else {}
    opts = _surface_options(obj)
    if key in obj:
        return obj.get(key)
    if key in opts:
        return opts.get(key)
    if key in raw:
        return raw.get(key)
    return default


def _strict_same_surface(obj: dict | None) -> bool:
    value = _surface_option(obj, "strict_same_surface", False)
    return bool(value) if isinstance(value, bool) else se_bool(value)


def _shared_surface_key(obj: dict | None):
    if not _strict_same_surface(obj):
        return None
    group = (
        _surface_option(obj, "shared_surface_group")
        or _surface_option(obj, "same_surface_seed")
        or "strict_same_surface"
    )
    return str(group)


def _stable_surface_seed(text: str) -> int:
    return int(hashlib.md5(str(text).encode("utf-8")).hexdigest(), 16) & 0x7FFFFFFF


def _family_bias(p: dict, family: str) -> dict:
    q = dict(p)
    if family == "volcanic_mars":
        q["venusMagn"] = max(q["venusMagn"], 0.35)
        q["mareDensity"] = max(q["mareDensity"], 0.18)
        q["montesMagn"] = max(q["montesMagn"], 0.28)
        q["craterMagn"] = max(q["craterMagn"], 0.20)
        q["craterDensity"] = max(q["craterDensity"], 0.18)
    elif family == "cratered_old":
        q["craterMagn"] = max(q["craterMagn"], 0.55)
        q["craterDensity"] = max(q["craterDensity"], 0.60)
        q["erosion"] = min(q["erosion"], 0.25)
        q["mareDensity"] = max(q["mareDensity"], 0.20)
    elif family == "cratered_sparse":
        q["craterMagn"] = max(q["craterMagn"], 0.25)
        q["craterDensity"] = max(q["craterDensity"], 0.20)
    elif family == "tectonic_chaotic":
        q["venusMagn"] = max(q["venusMagn"], 0.65)
        q["cracksMagn"] = max(q["cracksMagn"], 0.28)
        q["montesMagn"] = max(q["montesMagn"], 0.18)
    elif family == "rifted":
        q["riftsMagn"] = max(q["riftsMagn"], 0.28)
        q["canyonsMagn"] = max(q["canyonsMagn"], 0.25)
        q["canyonsFraction"] = max(q["canyonsFraction"], 0.30)
    elif family == "icy_fractured":
        q["venusMagn"] = max(q["venusMagn"], 0.35)
        q["cracksMagn"] = max(q["cracksMagn"], 0.35)
        q["craterDensity"] = max(q["craterDensity"], 0.16)
        q["erosion"] = max(q["erosion"], 0.35)
    else:
        q["erosion"] = max(q["erosion"], 0.35)
        q["hillsMagn"] = max(q["hillsMagn"], 0.18)
    return q


def _family_overlay(p: dict, seed: int, family: str) -> np.ndarray:
    lon, lat, lat_sin, lat_abs, cos_lat = _GRIDS
    overlay = np.zeros_like(lon, dtype=np.float32)
    if family in ("cratered_old", "cratered_sparse"):
        density = 0.55 if family == "cratered_old" else 0.24
        count = min(MAX_CRATERS_PER_BODY, int(8 + density * lon.size / 120.0))
        for i, (lon0, lat0, rnd) in enumerate(_feature_centers(seed + 701, count, polar_ok=True)):
            radius = (0.018 + 0.11 * rnd) / (1.0 + (i % 7) * 0.18)
            x = _great_circle_distance(lon, lat, lon0, lat0) / max(0.008, radius)
            overlay += (-np.clip(1.0 - x * x, 0.0, 1.0) + np.exp(-((x - 1.0) / 0.15) ** 2) * 0.42) * 0.12
    elif family == "rifted":
        r = _ridged(lon, lat, max(0.18, p["riftsFreq"] * 0.9), 4, seed + 733)
        overlay -= _smoothstep(0.72, 0.97, r) * 0.30
        overlay += _ridged(lon + 0.7, lat, max(0.20, p["canyonsFreq"] * 0.6), 3, seed + 739) * 0.08
    elif family == "tectonic_chaotic":
        folds = _ridged(lon + _fbm(lon, lat, 0.35, 3, 2.0, 0.5, seed + 751), lat, max(0.25, p["venusFreq"] * 0.8), 5, seed + 757)
        overlay += folds * 0.24 + _fbm(lon * 1.2, lat, 0.6, 4, 2.0, 0.5, seed + 761) * 0.10
    elif family == "icy_fractured":
        cracks = _ridged(lon, lat, max(0.3, p["cracksFreq"] * 0.7), 5, seed + 769)
        overlay += cracks * 0.18 - _smoothstep(0.78, 0.96, cracks) * 0.16
    elif family == "volcanic_mars":
        swell = _fbm(lon, lat, 0.22, 3, 2.0, 0.55, seed + 773)
        overlay += np.maximum(0.0, swell) * 0.22
        overlay -= _smoothstep(0.72, 0.95, _ridged(lon, lat, 0.35, 3, seed + 779)) * 0.12
    else:
        overlay += _fbm(lon, lat, 0.55, 4, 2.0, 0.5, seed + 787) * 0.12
    return overlay.astype(np.float32)


def _blend_secondary_terrain(primary: np.ndarray, secondary: np.ndarray, blend: str) -> np.ndarray:
    if blend == "max":
        return np.maximum(primary, secondary)
    if blend == "lerp":
        return primary * 0.70 + secondary * 0.30
    if blend == "masked_add":
        mask = _smoothstep(float(np.percentile(primary, 45)), float(np.percentile(primary, 85)), primary)
        return primary + secondary * (0.20 + 0.65 * mask)
    if blend == "detail_overlay":
        return primary + (secondary - np.mean(secondary)) * 0.35
    return primary + secondary * 0.35


def _legacy_generate_heightmap_v2(p: dict, seed: int, body_name: str | None = None) -> np.ndarray:
    body_name = str(body_name or "unknown")
    total_start = time.perf_counter()
    stage_start = total_start
    family = str(p.get("terrain_family", "hybrid"))
    secondary_family = p.get("secondary_family")
    blend = str(p.get("terrain_blend", "detail_overlay"))
    p = _family_bias(p, family)

    lon, lat, lat_sin, lat_abs, cos_lat = _GRIDS
    h, w = lon.shape
    p = dict(p)
    p["cracksOctaves"] = min(int(p.get("cracksOctaves", 1)), MAX_EXPENSIVE_OCTAVES)
    p["craterOctaves"] = min(int(p.get("craterOctaves", 1)), MAX_EXPENSIVE_OCTAVES)
    p["volcanoOctaves"] = min(int(p.get("volcanoOctaves", 1)), MAX_EXPENSIVE_OCTAVES)

    warp_strength = 0.10 + p["venusMagn"] * 0.28
    warp_x = _fbm(lon, lat, max(0.08, p["mainFreq"] * 0.28 + p["venusFreq"] * 0.08), 4, 2.0, 0.55, seed + 13)
    warp_y = _fbm(lon, lat, max(0.08, p["mainFreq"] * 0.23 + p["venusFreq"] * 0.06), 4, 2.0, 0.55, seed + 17)
    lon_w = lon + warp_x * warp_strength * math.pi
    lat_w = np.clip(lat + warp_y * warp_strength * 0.75, -math.pi * 0.5, math.pi * 0.5)

    continents = _fbm(lon_w, lat_w, max(0.12, p["mainFreq"] * 0.42), 6, 2.0, 0.56, seed + 23)
    basins = _fbm(lon_w + 1.7, lat_w - 0.4, max(0.08, p["mainFreq"] * 0.22), 4, 2.1, 0.58, seed + 29)
    height = continents * 0.62 - np.maximum(0.0, -basins) * 0.28
    stage_start = _surface_stage_log(body_name, "macro_noise", stage_start, size=(w, h))

    if p["mareDensity"] > 0.01:
        mare = _ridged(lon_w, lat_w, max(0.08, p["mareFreq"]), 3, seed + 31)
        height -= _smoothstep(0.55, 0.95, mare) * p["mareDensity"] * 0.18
    if p["montesMagn"] > 0.01:
        ridges = _ridged(lon_w, lat_w, max(0.12, p["montesFreq"] * 1.25), 5, seed + 41)
        cover = _smoothstep(1.0 - p["montesFraction"] * 0.75, 1.0, _normalize_percentile(continents, 5.0, 95.0))
        height += (ridges ** (1.0 + p["montesSpiky"] * 1.8)) * cover * p["montesMagn"] * 0.42
    if p["hillsMagn"] > 0.01:
        hills = _fbm(lon_w, lat_w, max(0.2, p["hillsFreq"] * 1.4), 4, 2.05, 0.48, seed + 53)
        h2 = _fbm(lon_w + 2.1, lat_w, max(0.25, p["hillsFreq"] * 2.5), 3, 2.1, 0.45, seed + 57)
        height += (hills * p["hillsFraction"] + h2 * p["hills2Fraction"]) * p["hillsMagn"] * 0.16
    if p["dunesMagn"] > 0.01 and p["dunesFraction"] > 0.01:
        dunes = np.sin(lon_w * max(1.0, p["dunesFreq"] * 1.8) + warp_y * 5.0)
        dunes += _fbm(lon_w, lat_w, max(0.4, p["dunesFreq"] * 2.0), 2, 2.2, 0.4, seed + 61)
        height += dunes * p["dunesMagn"] * p["dunesFraction"] * 0.035
    stage_start = _surface_stage_log(body_name, "structural_noise", stage_start, size=(w, h))

    if p["riftsMagn"] > 0.01:
        r = _ridged(lon_w + warp_x * 0.8, lat_w, max(0.12, p["riftsFreq"]), 4, seed + 71)
        height -= _smoothstep(0.72, 0.96, r) * p["riftsMagn"] * 0.34
    stage_start = _surface_stage_log(body_name, "rifts", stage_start, size=(w, h), count=1 if p["riftsMagn"] > 0.01 else 0)

    if p["canyonsMagn"] > 0.01:
        c = np.abs(_fbm(lon_w, lat_w, max(0.15, p["canyonsFreq"] * 1.6), 4, 2.3, 0.45, seed + 83))
        terrain_gate = _smoothstep(0.35, 0.72, _normalize_percentile(height, 5.0, 95.0))
        height -= _smoothstep(0.02, 0.22, 1.0 - c) * terrain_gate * p["canyonsMagn"] * p["canyonsFraction"] * 0.22
    stage_start = _surface_stage_log(body_name, "canyons", stage_start, size=(w, h), count=1 if p["canyonsMagn"] > 0.01 else 0)

    if p["cracksMagn"] > 0.01:
        cracks = np.zeros_like(height)
        for i in range(p["cracksOctaves"]):
            n = np.abs(_value_noise(lon_w, lat_w, p["cracksFreq"] * (1.5 + i * 0.55), seed + 101 + i))
            cracks += _smoothstep(0.90, 0.99, 1.0 - n) / (i + 1.0)
        height -= cracks * p["cracksMagn"] * 0.035
    stage_start = _surface_stage_log(body_name, "channels", stage_start, size=(w, h), count=p["cracksOctaves"] if p["cracksMagn"] > 0.01 else 0)

    if p["eqridgeMagn"] > 0.001:
        ridge = np.exp(-(lat_sin / max(0.025, p["eqridgeWidth"] * 0.33)) ** 2)
        mod = 1.0 + p["eqridgeModMagn"] * _fbm(lon, lat, p["eqridgeModFreq"], 3, 2.0, 0.5, seed + 117)
        height += ridge * mod * p["eqridgeMagn"] * 0.45
    if p["volcanoMagn"] > 0.01 and p["volcanoDensity"] > 0.01:
        raw_count = int(4 + p["volcanoDensity"] * (14 + h * w / 420.0))
        count = min(MAX_VOLCANOES_PER_BODY, raw_count)
        if count < raw_count:
            log_debug(
                f"[surface-stage] body='{body_name}' stage='volcanoes' warning='capped feature count' "
                f"requested={raw_count} capped={count}",
                "SURFACE_WARN",
            )
        if _surface_budget_exceeded(body_name, total_start, "volcanoes"):
            count = 0
        for lon0, lat0, rnd in _feature_centers(seed + 131, count):
            radius = (0.10 + 0.22 * p["volcanoRadius"]) * (0.55 + rnd)
            d = _great_circle_distance(lon, lat, lon0, lat0)
            cone = np.clip(1.0 - d / radius, 0.0, 1.0)
            caldera = np.exp(-(d / max(0.015, radius * 0.17)) ** 2)
            height += (cone ** 1.6 - caldera * 0.28) * p["volcanoMagn"] * 0.32
    else:
        count = 0
    stage_start = _surface_stage_log(body_name, "volcanoes", stage_start, size=(w, h), count=count)

    if p["craterMagn"] > 0.01 and p["craterDensity"] > 0.01:
        raw_count = int(5 + p["craterDensity"] * (26 + h * w / 170.0))
        count = min(MAX_CRATERS_PER_BODY, raw_count)
        if count < raw_count:
            log_debug(
                f"[surface-stage] body='{body_name}' stage='craters' warning='capped feature count' "
                f"requested={raw_count} capped={count}",
                "SURFACE_WARN",
            )
        if _surface_budget_exceeded(body_name, total_start, "craters"):
            count = 0
        for i, (lon0, lat0, rnd) in enumerate(_feature_centers(seed + 151, count, polar_ok=True)):
            radius = (0.028 + 0.18 * rnd) * (0.55 + p["craterFreq"] * 0.18) / (1.0 + (i % max(1, p["craterOctaves"])) * 0.20)
            x = _great_circle_distance(lon, lat, lon0, lat0) / max(0.01, radius)
            bowl = -np.clip(1.0 - x * x, 0.0, 1.0)
            rim = np.exp(-((x - 1.0) / 0.13) ** 2) * 0.45
            ejecta = np.exp(-((x - 1.45) / 0.45) ** 2) * 0.08 * p["craterRayedFactor"]
            height += (bowl + rim + ejecta) * p["craterMagn"] * 0.16
    else:
        count = 0
    stage_start = _surface_stage_log(body_name, "craters", stage_start, size=(w, h), count=count)

    primary_overlay = _family_overlay(p, seed + 503, family)
    height = _blend_secondary_terrain(height, primary_overlay, "detail_overlay")
    if secondary_family:
        secondary_overlay = _family_overlay(p, seed + 547, str(secondary_family))
        height = _blend_secondary_terrain(height, secondary_overlay, blend)

    if p["erosion"] > 0.01:
        smooth = _box_blur_wrap(height, radius=max(1, int(1 + p["erosion"] * 3)), passes=2)
        height = height * (1.0 - p["erosion"] * 0.55) + smooth * (p["erosion"] * 0.55)
    stage_start = _surface_stage_log(body_name, "finishing", stage_start, size=(w, h))

    height = _normalize_percentile(height, 0.8, 99.2)
    sea = p["seaLevel"]
    if sea > 0.0:
        threshold = float(np.percentile(height, sea * 100.0))
        if 0.02 < threshold < 0.98:
            below = np.clip(height / threshold, 0.0, 1.0) * sea
            above = sea + np.clip((height - threshold) / (1.0 - threshold), 0.0, 1.0) * (1.0 - sea)
            height = np.where(height < threshold, below, above)
            shelf = _smoothstep(sea - 0.08, sea + 0.04, height)
            height = height * (0.88 + 0.12 * shelf) + sea * (1.0 - shelf) * 0.06
    else:
        height = 0.18 + height * 0.82
    _surface_stage_log(body_name, "done", total_start, size=(w, h), total=True)
    return _force_horizontal_wrap(np.clip(height, 0.0, 1.0))


def generate_heightmap(p: dict, seed: int, body_name: str | None = None) -> np.ndarray:
    """Generate canonical rocky terrain from seamless 3D noise on a sphere."""
    body_name = str(body_name or "unknown")
    lon, lat, lat_sin, _, _ = _GRIDS
    x, y, z = _sphere_xyz(lon, lat)
    h, w = x.shape
    nyquist = max(2.0, min(w / 6.0, h / 3.0))
    offset = tuple(float(v) * 17.0 for v in p.get("randomize", (0.0, 0.0, 0.0)))

    # Warp the sampling vector itself, then renormalize it back to the sphere.
    # venusMagn=0 intentionally means no Venus-style domain warp.
    warp_strength = float(p.get("venusMagn", 0.0)) * 0.32
    if warp_strength > 0.0:
        wf = min(float(p.get("venusFreq", 2.0)), nyquist * 0.35)
        wx = _spherical_fbm(x, y, z, wf, 3, seed + 11, offset, max_frequency=nyquist)
        wy = _spherical_fbm(x, y, z, wf, 3, seed + 17, offset, max_frequency=nyquist)
        wz = _spherical_fbm(x, y, z, wf, 3, seed + 23, offset, max_frequency=nyquist)
        xw = x + wx * warp_strength; yw = y + wy * warp_strength; zw = z + wz * warp_strength
        length = np.sqrt(xw * xw + yw * yw + zw * zw)
        xw /= length; yw /= length; zw /= length
    else:
        xw, yw, zw = x, y, z

    main_f = min(float(p.get("mainFreq", 2.0)), nyquist * 0.22)
    continental = _spherical_fbm(xw, yw, zw, main_f, 6, seed + 31, offset,
                                 lacunarity=1.92, gain=0.54, max_frequency=nyquist)
    basins = _spherical_fbm(xw, yw, zw, min(main_f * 0.72 + 0.35, nyquist),
                            4, seed + 43, offset, lacunarity=2.07, gain=0.56,
                            max_frequency=nyquist)
    height = continental * 0.72 - np.maximum(0.0, basins) * 0.30

    mare_density = float(p.get("mareDensity", 0.0))
    if mare_density > 0.0:
        mare = _spherical_ridged(xw, yw, zw, min(p["mareFreq"], nyquist), 3,
                                 seed + 53, offset, nyquist)
        height -= _smoothstep(0.62, 0.92, mare) * mare_density * 0.24

    mountains = _spherical_ridged(xw, yw, zw, min(p["montesFreq"], nyquist), 4,
                                   seed + 61, offset, nyquist)
    mountain_regions = _spherical_fbm(xw, yw, zw, min(main_f * 1.5, nyquist), 3,
                                      seed + 67, offset, max_frequency=nyquist)
    mountain_gate = _smoothstep(1.0 - p["montesFraction"] * 1.25, 0.75, mountain_regions)
    mountain_gate *= _smoothstep(-0.20, 0.28, continental)
    height += np.power(mountains, 1.3 + p["montesSpiky"] * 2.2) * mountain_gate * p["montesMagn"] * 0.55

    hills = _spherical_fbm(xw, yw, zw, min(p["hillsFreq"], nyquist), 4,
                           seed + 73, offset, max_frequency=nyquist)
    hills2 = _spherical_fbm(xw, yw, zw, min(p["hillsFreq"] * 1.8, nyquist), 3,
                            seed + 79, offset, max_frequency=nyquist)
    height += (hills * p["hillsFraction"] + hills2 * p["hills2Fraction"]) * p["hillsMagn"] * 0.20

    if p["riftsMagn"] > 0.0:
        rifts = _spherical_ridged(xw, yw, zw, min(p["riftsFreq"], nyquist), 4,
                                  seed + 83, offset, nyquist)
        height -= _smoothstep(0.86, 0.975, rifts) * p["riftsMagn"] * 0.42
    if p["canyonsMagn"] > 0.0:
        canyon_n = np.abs(_spherical_fbm(xw, yw, zw, min(p["canyonsFreq"], nyquist),
                                         4, seed + 89, offset, max_frequency=nyquist))
        canyon = _smoothstep(0.055, 0.0, canyon_n)
        height -= canyon * _smoothstep(-0.05, 0.30, continental) * p["canyonsMagn"] * p["canyonsFraction"] * 0.34
    if p["cracksMagn"] > 0.0:
        cracks = _spherical_ridged(xw, yw, zw, min(p["cracksFreq"], nyquist),
                                   min(p["cracksOctaves"], 5), seed + 97, offset, nyquist)
        height -= _smoothstep(0.94, 0.992, cracks) * p["cracksMagn"] * 0.11

    if p["riversMagn"] > 0.0:
        flow = np.abs(_spherical_fbm(xw, yw, zw, min(p["riversFreq"], nyquist), 4,
                                     seed + 101, offset, max_frequency=nyquist))
        river = _smoothstep(0.035, 0.0, flow) * _smoothstep(-0.08, 0.32, continental)
        height -= river * p["riversMagn"] * 0.12

    if p["eqridgeMagn"] > 0.0:
        ridge = np.exp(-np.square(lat_sin / max(0.02, p["eqridgeWidth"] * 0.35)))
        mod = 1.0 + p["eqridgeModMagn"] * _spherical_fbm(
            x, y, z, min(p["eqridgeModFreq"], nyquist), 3, seed + 107, offset,
            max_frequency=nyquist)
        height += ridge * np.clip(mod, 0.0, 2.0) * p["eqridgeMagn"] * 0.35

    crater_count = min(MAX_CRATERS_PER_BODY, int(p["craterDensity"] * (18 + h * w / 260.0)))
    for i, (lon0, lat0, rnd) in enumerate(_feature_centers(seed + 127, crater_count, polar_ok=True)):
        radius = (0.025 + 0.16 * rnd * rnd) / (1.0 + (i % max(1, p["craterOctaves"])) * 0.16)
        radius *= 1.25 - 0.55 * p["craterFreq"]
        d = _great_circle_distance(lon, lat, lon0, lat0) / max(radius, 0.008)
        bowl = -np.clip(1.0 - d * d, 0.0, 1.0)
        rim = np.exp(-np.square((d - 1.0) / 0.13)) * 0.48
        ejecta = np.exp(-np.square((d - 1.45) / 0.48)) * 0.10 * p["craterRayedFactor"]
        height += (bowl + rim + ejecta) * p["craterMagn"] * 0.18

    volcano_count = min(MAX_VOLCANOES_PER_BODY, int(p["volcanoDensity"] * (4 + h * w / 900.0)))
    for lon0, lat0, rnd in _feature_centers(seed + 149, volcano_count):
        radius = (0.055 + p["volcanoRadius"] * 0.18) * (0.65 + rnd * 0.7)
        d = _great_circle_distance(lon, lat, lon0, lat0)
        cone = np.clip(1.0 - d / radius, 0.0, 1.0)
        caldera = np.exp(-np.square(d / max(0.012, radius * 0.16)))
        height += (np.power(cone, 1.55) - caldera * 0.32) * p["volcanoMagn"] * 0.38

    if p["terraceProb"] > 0.0:
        terraced = np.round(height * 18.0) / 18.0
        height = height * (1.0 - p["terraceProb"] * 0.35) + terraced * p["terraceProb"] * 0.35
    if p["erosion"] > 0.0:
        smooth = _box_blur_wrap(height, radius=1 + int(p["erosion"] * 2), passes=1)
        height = height * (1.0 - p["erosion"] * 0.42) + smooth * p["erosion"] * 0.42
    height = _normalize_percentile(height, 0.5, 99.5)
    _validate_rocky_structure(height, body_name)
    return np.clip(height, 0.0, 1.0).astype(np.float32)


def _terrain_validation_metrics(height: np.ndarray) -> dict:
    row_model = np.mean(height, axis=1, keepdims=True)
    total = float(np.std(height))
    longitude = float(np.std(height - row_model))
    gx = float(np.mean(np.square(np.roll(height, -1, axis=1) - height)))
    gy = float(np.mean(np.square(np.diff(height, axis=0)))) if height.shape[0] > 1 else 0.0
    return {
        "total_variation": total,
        "longitude_structure": longitude,
        "longitude_structure_ratio": longitude / max(total, 1e-9),
        "horizontal_gradient_energy": gx,
        "vertical_gradient_energy": gy,
    }


def _validate_rocky_structure(height: np.ndarray, body_name: str = "unknown") -> dict:
    metrics = _terrain_validation_metrics(height)
    log_debug(
        f"[surface-validation] body='{body_name}' longitude_structure_ratio="
        f"{metrics['longitude_structure_ratio']:.4f} horizontal_gradient_energy="
        f"{metrics['horizontal_gradient_energy']:.6g} vertical_gradient_energy="
        f"{metrics['vertical_gradient_energy']:.6g}", "SURFACE_VALIDATE")
    if metrics["total_variation"] > 1e-5 and metrics["longitude_structure_ratio"] < 0.25:
        raise ValueError("Surface validation failed: generated rocky terrain collapsed into latitude bands.")
    return metrics


def _validate_seam(channel: np.ndarray, label: str) -> float:
    arr = np.asarray(channel, dtype=np.float32)
    if arr.ndim == 3:
        arr = arr[:, :, 0]
    edge_error = float(np.mean(np.abs(arr[:, 0] - arr[:, -1])))
    channel_range = float(np.ptp(arr))
    tolerance = max(1e-5, channel_range * 0.18)
    if edge_error > tolerance:
        raise ValueError(f"Surface seam validation failed for {label}: edge error {edge_error:.6g}.")
    return edge_error


def _repair_ice_mask(height: np.ndarray, liquid: np.ndarray,
                     temperature: np.ndarray, ice: np.ndarray,
                     body_name: str, obj: dict = None) -> np.ndarray:
    """Clamp implausible ice pixels rather than aborting export."""
    ice_pixels = ice > 1e-6
    if not np.any(ice_pixels):
        return ice

    _, _, _, lat_abs, _ = _GRIDS

    raw  = obj.get("raw_data", {}) if isinstance(obj, dict) else {}
    surf = raw.get("Surface", {}) if isinstance(raw.get("Surface"), dict) else {}

    from constants import safe_float as _sf
    snow_level  = float(np.clip(_sf(surf.get("snowLevel",  0.85)), 0.0, 1.0))
    icecap_lat  = float(np.clip(_sf(surf.get("icecapLatitude", 0.70)), 0.0, 1.0))

    polar     = lat_abs >= max(0.55, min(0.95, icecap_lat))
    high_thr  = max(0.65, min(0.995, snow_level))
    high_thr  = max(high_thr, float(np.percentile(height, 85)))
    highland  = height >= high_thr

    avg_temp   = float(np.nanmean(temperature))
    cold_limit = 273.15 if avg_temp > 285.0 else 278.0
    cold       = temperature <= cold_limit

    plausible = cold | polar | highland
    bad       = ice_pixels & ~plausible
    bad_ratio = float(np.mean(bad)) / max(float(np.mean(ice_pixels)), 1e-9)

    if bad_ratio <= 0.0:
        return ice

    repaired = np.array(ice, copy=True, dtype=np.float32)
    repaired[bad] = 0.0

    # If repair wiped all ice but source had it, preserve a minimal polar cap.
    if not np.any(repaired > 1e-6):
        polar_cold = polar & (temperature <= max(278.0, cold_limit + 5.0))
        if np.any(polar_cold):
            repaired[polar_cold] = np.maximum(repaired[polar_cold], 0.15)

    log_debug(
        f"[ice-repair] body='{body_name}' avg_temp={avg_temp:.2f}K "
        f"ice_fraction={float(np.mean(ice_pixels)):.4f} bad_ratio={bad_ratio:.3f} "
        f"snowLevel={snow_level:.3f} icecapLatitude={icecap_lat:.3f}; "
        f"clamped implausible ice instead of failing export",
        "SURFACE_WARN",
    )
    return repaired


def _validate_geography(height: np.ndarray, liquid: np.ndarray,
                        temperature: np.ndarray, ice: np.ndarray,
                        body_name: str) -> None:
    water = liquid > 0.0
    fraction = float(np.mean(water))
    if 0.05 <= fraction <= 0.95:
        if not np.any(water) or not np.any(~water):
            raise ValueError(f"Surface validation failed: '{body_name}' lacks land or water.")
        row_only = np.std(water.astype(np.float32) - np.mean(water, axis=1, keepdims=True))
        if row_only < 0.08:
            raise ValueError("Surface validation failed: generated rocky terrain collapsed into latitude bands.")
        if np.any(liquid[~water] != 0.0) or not np.any(liquid[water] > 0.0):
            raise ValueError(f"Surface validation failed: invalid liquid semantics for '{body_name}'.")
        if float(np.mean(height[water])) >= float(np.mean(height[~water])):
            raise ValueError(f"Surface validation failed: water does not occupy basins for '{body_name}'.")
    ice_pixels = ice > 1e-6
    if np.any(ice_pixels):
        _, _, _, lat_abs, _ = _GRIDS
        plausible = (temperature < 278.0) | (lat_abs > 0.70) | (height > 0.72)
        plausible_ratio = float(np.mean(plausible[ice_pixels]))
        if plausible_ratio < 0.95:
            log_debug(
                f"[ice-validate] body='{body_name}' plausible_ratio={plausible_ratio:.3f} "
                f"ice_fraction={float(np.mean(ice_pixels)):.4f}; "
                f"implausible ice was already repaired or is minor; skipping fatal check",
                "SURFACE_WARN",
            )


def map_se_sea_level(source_sea_level: float, preset: str = "",
                     has_ocean: bool = False, no_ocean: bool = False) -> float:
    """Map SE seaLevel to a monotonic target water fraction, independent of height units."""
    if no_ocean or (not has_ocean and source_sea_level <= 0.0):
        return 0.0
    if source_sea_level <= 0.0:
        return 0.30 if has_ocean else 0.0
    preset_bias = 0.08 if "ocean" in str(preset).lower() or "aquaria" in str(preset).lower() else 0.0
    return float(np.clip(source_sea_level / 2.0 + preset_bias, 0.0, 0.98))


def _liquid_depth_from_height(p: dict, height_map: np.ndarray, arch: str = "rocky",
                              allow_liquid: bool = True) -> np.ndarray:
    target_fraction = float(p.get("target_water_fraction", p.get("seaLevel", 0.0)))
    if target_fraction <= 0.0 or arch == "lava" or not allow_liquid:
        p["_last_sea_threshold"] = 0.0
        return np.zeros_like(height_map, dtype=np.float32)
    target_fraction = float(np.clip(target_fraction, 0.0, 0.98))
    threshold = float(np.quantile(height_map, target_fraction))
    p["_last_sea_threshold"] = threshold
    water_mask = height_map <= threshold
    depth_scale = max(0.035, float(np.percentile(height_map, 75) - np.percentile(height_map, 15)))
    depth = np.clip((threshold - height_map) / depth_scale, 0.0, 1.0)
    if p.get("water_mode") == "lacustrine":
        max_depth = min(1.0, max(0.0, p.get("ocean_depth_km", 0.0)) /
                        max(0.05, p.get("liquid_channel_km_per_unit", 1.0)))
        if np.max(depth) > 0.0:
            depth *= max_depth / float(np.max(depth))
    return np.where(water_mask, depth, 0.0).astype(np.float32)


def generate_temperature(p: dict, seed: int, height_map: np.ndarray,
                         est_temp_k: float = 288.0,
                         liquid_depth: np.ndarray | None = None) -> np.ndarray:
    lon, lat, lat_sin, lat_abs, cos_lat = _GRIDS
    x, y, z = _sphere_xyz(lon, lat)
    target = float(est_temp_k if np.isfinite(est_temp_k) else 288.0)
    sea = float(p.get("_last_sea_threshold", 0.0))
    water = liquid_depth if liquid_depth is not None else _liquid_depth_from_height(p, height_map)
    water_mask = water > 0.0
    land = (~water_mask).astype(np.float32)
    swing = np.clip(target * 0.14 + 8.0, 18.0, 120.0)
    temp = target + (p["climateEquator"] - 0.5) * 16.0 - swing * (lat_abs ** 1.55) + (p["climatePole"] - 0.5) * 12.0 * lat_abs
    temp += np.exp(-(lat_sin / max(0.08, p["tropicLatitude"] + p["tropicWidth"] * 0.35)) ** 2) * p["climateTropic"] * 5.0
    elev_rel = (height_map - sea) / max(0.08, 1.0 - sea)
    temp -= np.maximum(0.0, elev_rel) * (8.0 + p["heightTempGrad"] * 34.0)
    offset = tuple(float(v) * 17.0 for v in p.get("randomize", (0.0, 0.0, 0.0)))
    weather = _spherical_fbm(x, y, z, max(1.0, p["mainFreq"]), 4, seed + 211, offset,
                             max_frequency=max(2.0, min(height_map.shape) / 4.0))
    temp += weather * (2.0 + 5.0 * land) * (1.0 - 0.45 * p["humidity"])
    moderated = _box_blur_wrap(temp, radius=2, passes=2)
    temp = np.where(water_mask, temp * 0.45 + moderated * 0.55, temp)
    temp -= _smoothstep(p["icecapLatitude"], 1.0, lat_abs) * (4.0 + 9.0 * p["icecapHeight"])
    volcanic = _smoothstep(0.82, 1.0, height_map) * p["volcanoActivity"]
    temp += volcanic * min(900.0, p["volcanoTemp"] * 0.35)
    temp += target - float(np.mean(temp))
    return np.clip(temp, 30.0, 2000.0).astype(np.float32)


def generate_humidity(p: dict, seed: int, height_map: np.ndarray,
                      T_map: np.ndarray, liquid_depth: np.ndarray | None = None) -> np.ndarray:
    lon, lat, _, lat_abs, _ = _GRIDS
    x, y, z = _sphere_xyz(lon, lat)
    sea = float(p.get("_last_sea_threshold", 0.0))
    liquid = liquid_depth if liquid_depth is not None else _liquid_depth_from_height(p, height_map)
    water = liquid > 0.0
    proximity = water.astype(np.float32)
    for _ in range(10):
        proximity = np.maximum(proximity, _box_blur_wrap(proximity, radius=2, passes=1) * 0.94)
    offset = tuple(float(v) * 17.0 for v in p.get("randomize", (0.0, 0.0, 0.0)))
    weather = _spherical_fbm(x, y, z, max(1.0, p["mainFreq"] * 1.4), 4,
                             seed + 229, offset, max_frequency=max(2.0, min(height_map.shape) / 4.0))
    elev = np.maximum(0.0, height_map - sea) / max(0.08, 1.0 - sea)
    east_slope = np.maximum(0.0, height_map - np.roll(height_map, 2, axis=1))
    tropic = np.exp(-np.square(lat_abs / max(0.12, p["tropicLatitude"] + p["tropicWidth"])))
    humidity = (p["humidity"] * 0.42 + proximity * 0.42 + tropic * p["climateTropic"] * 0.18 +
                weather * 0.16 - elev * 0.22 - east_slope * 1.8)
    humidity += p["riversMagn"] * _smoothstep(0.08, 0.0, np.abs(weather)) * 0.16
    humidity -= _smoothstep(305.0, 370.0, T_map) * 0.18
    return np.clip(humidity, 0.0, 1.0).astype(np.float32)


def generate_biomes(p: dict, height_map: np.ndarray, T_map: np.ndarray,
                    humidity: np.ndarray, liquid_depth: np.ndarray | None = None,
                    arch: str = "rocky") -> np.ndarray:
    sea = float(p.get("_last_sea_threshold", 0.0))
    liquid = liquid_depth if liquid_depth is not None else _liquid_depth_from_height(p, height_map)
    water = liquid > 0.0
    land = ~water
    coast = land & (height_map <= sea + 0.035)
    high = land & (height_map > max(p["snowLevel"], 0.72))
    biome = np.full(height_map.shape, 4, dtype=np.int8)
    biome[water] = 0
    biome[water & (liquid < 0.18)] = 1
    biome[coast] = 2
    biome[land & (humidity < p["climateSteppeMin"]) & (T_map > 273.0)] = 3
    biome[land & (humidity >= p["climateGrassMin"]) & (humidity <= p["climateGrassMax"])] = 4
    biome[land & (humidity >= p["climateForestMin"]) & (humidity <= p["climateForestMax"]) & (T_map > 268.0)] = 5
    biome[land & (humidity > p["climateForestMax"]) & (T_map > 292.0)] = 6
    biome[land & (T_map < 268.0)] = 7
    biome[high] = 8
    biome[land & (T_map < 250.0)] = 9
    if arch == "lava" or p["volcanoActivity"] > 0.65:
        biome[land & (height_map > 0.84)] = 10
    return biome


def _ice_thickness(p: dict, height_map: np.ndarray, T_map: np.ndarray,
                   liquid_depth: np.ndarray, arch: str = "rocky") -> np.ndarray:
    _, _, _, lat_abs, _ = _GRIDS
    cold = _smoothstep(273.15, 238.0, T_map)
    polar = _smoothstep(p["icecapLatitude"], 1.0, lat_abs)
    high = _smoothstep(max(0.05, p["snowLevel"]), 1.0, height_map) * p["icecapHeight"]
    water_ice = liquid_depth * _smoothstep(271.0, 250.0, T_map) * 0.75
    ice = cold * (0.40 + 1.15 * polar + 0.75 * high) + water_ice * 1.15
    ice = _box_blur_wrap(np.clip(ice, 0.0, 1.0), radius=2, passes=2)
    max_ice = 0.025 if arch == "ice" or float(np.mean(T_map)) < 210.0 else 0.00635
    return np.clip(ice * max_ice * 1.20, 0.0, max_ice).astype(np.float32)


def generate_albedo(p: dict, seed: int, height_map: np.ndarray, T_map: np.ndarray,
                    humidity: np.ndarray, biome: np.ndarray,
                    liquid_depth: np.ndarray | None = None,
                    ice_thickness: np.ndarray | None = None) -> np.ndarray:
    if liquid_depth is None:
        liquid_depth = _liquid_depth_from_height(p, height_map)
    if ice_thickness is None:
        ice_thickness = _ice_thickness(p, height_map, T_map, liquid_depth)
    sea = float(p.get("_last_sea_threshold", 0.0))
    land_elev = np.clip((height_map - sea) / max(0.08, 1.0 - sea), 0.0, 1.0)
    desert = (1.0 - humidity) * _smoothstep(275.0, 305.0, T_map)
    vegetation = humidity * _smoothstep(255.0, 292.0, T_map) * (1.0 - _smoothstep(303.0, 330.0, T_map))
    rock = 0.22 + 0.20 * desert + 0.08 * land_elev
    veg = 0.11 + 0.09 * (1.0 - humidity)
    albedo = rock * (1.0 - vegetation) + veg * vegetation
    albedo = np.where(liquid_depth > 0.0, 0.045 + 0.075 * (1.0 - liquid_depth), albedo)
    beach = _smoothstep(sea - 0.015, sea + 0.035, height_map) * (1.0 - _smoothstep(sea + 0.035, sea + 0.09, height_map))
    albedo = albedo * (1.0 - beach * 0.55) + 0.42 * beach * 0.55
    ice_frac = _smoothstep(0.00045, 0.0045, ice_thickness)
    snow_brightness = 0.68 + 0.30 * ice_frac
    albedo = albedo * (1.0 - ice_frac) + snow_brightness * ice_frac
    x, y, z = _sphere_xyz(_GRIDS[0], _GRIDS[1])
    offset = tuple(float(v) * 17.0 for v in p.get("randomize", (0.0, 0.0, 0.0)))
    texture = _spherical_fbm(x, y, z, p["colorDistFreq"], 3, seed + 301, offset,
                              max_frequency=max(2.0, min(height_map.shape) / 4.0))
    albedo += texture * p["colorDistMagn"] * (0.045 + 0.035 * (liquid_depth <= 0.0))
    albedo += (liquid_depth <= 0.0) * 0.032
    return np.clip(albedo, 0.035, 0.985).astype(np.float32)


def _base_pressure_pa(obj: dict) -> float:
    raw = obj.get("raw_data") or {}
    flags = raw.get("_source_flags", {}) if isinstance(raw.get("_source_flags"), dict) else {}
    if se_bool(raw.get("NoAtmosphere", "false")) or flags.get("has_no_atmosphere"):
        return 0.0
    for value in ((obj.get("atm_info") or {}).get("Pressure"),
                  (obj.get("atm_info") or {}).get("pressure"),
                  ((obj.get("raw_data") or {}).get("Atmosphere") or {}).get("Pressure")):
        if value is not None:
            return max(0.0, safe_float(value, 0.0) * 101325.0)
    if obj.get("atm_info") or (obj.get("raw_data") or {}).get("Atmosphere"):
        return 101325.0 * 0.18
    return 0.0


def _pressure_map(p: dict, height_map: np.ndarray, T_map: np.ndarray,
                  base_pressure_pa: float,
                  liquid_depth: np.ndarray | None = None) -> np.ndarray:
    if base_pressure_pa <= 0.0:
        return np.zeros_like(height_map, dtype=np.float32)
    sea = float(p.get("_last_sea_threshold", 0.0))
    if liquid_depth is None:
        liquid_depth = _liquid_depth_from_height(p, height_map)
    rel_elev = (height_map - sea) / max(0.10, 1.0 - sea)
    elev_m = rel_elev * (3500.0 + max(0.0, p.get("BumpHeight", 0.0)) * 100.0)
    scale_height = np.clip(8000.0 * (T_map / 288.0), 2500.0, 30000.0)
    pressure = base_pressure_pa * np.exp(-elev_m / scale_height)
    return np.clip(pressure, 0.0, base_pressure_pa * 4.0).astype(np.float32)


def _surface_gas_mode_runtime() -> tuple[str, bool]:
    try:
        import globals_compat as runtime
        gas_mode = str(getattr(runtime, "SURFACE_GAS_PRESSURE_MODE", "off")).strip().lower()
        static = bool(getattr(runtime, "STATIC_IMPORTED_ATMOSPHERES", True))
    except Exception:
        from constants import SURFACE_GAS_PRESSURE_MODE, STATIC_IMPORTED_ATMOSPHERES
        gas_mode = str(SURFACE_GAS_PRESSURE_MODE).strip().lower()
        static = bool(STATIC_IMPORTED_ATMOSPHERES)
    return gas_mode, static


SAFE_PASSIVE_SURFACE_MODES = {
    "none", "preview_only", "liquid_mask_only", "full_us_like",
}


def _surface_data_mode_runtime() -> tuple[str, bool, bool]:
    """Return normalized surface mode, attachment flag, and active-physics flag."""
    try:
        import globals_compat as runtime
        mode = str(getattr(runtime, "SURFACE_DATA_MODE", "full_us_like")).strip().lower()
        attach = bool(getattr(runtime, "ATTACH_SURFACE_GRID_COMPONENT", True))
        active = bool(getattr(runtime, "ACTIVE_SURFACE_PHYSICS", False))
    except Exception:
        from constants import (
            SURFACE_DATA_MODE, ATTACH_SURFACE_GRID_COMPONENT,
            ACTIVE_SURFACE_PHYSICS,
        )
        mode = str(SURFACE_DATA_MODE).strip().lower()
        attach = bool(ATTACH_SURFACE_GRID_COMPONENT)
        active = bool(ACTIVE_SURFACE_PHYSICS)
    aliases = {
        "off": "none",
        "preview": "preview_only",
        "liquid": "liquid_mask_only",
        "water_mask": "liquid_mask_only",
        "passive": "full_us_like",
        "passive_attached": "full_us_like",
        "full": "full_us_like",
        "active": "active_legacy",
        "legacy": "active_legacy",
    }
    mode = aliases.get(mode, mode)
    if mode not in {
        "none", "preview_only", "liquid_mask_only", "full_us_like", "active_legacy"
    }:
        mode = "full_us_like"
    if mode in {"none", "preview_only"}:
        attach = False
    if mode == "full_us_like":
        active = False
    return mode, attach, active


def generate_material0(p: dict, seed: int, T_map: np.ndarray, albedo: np.ndarray,
                       height_map: np.ndarray, liquid_depth: np.ndarray | None = None,
                       ice_thickness: np.ndarray | None = None,
                       base_pressure_pa: float = 0.0) -> np.ndarray:
    if liquid_depth is None:
        liquid_depth = _liquid_depth_from_height(p, height_map)
    if ice_thickness is None:
        ice_thickness = _ice_thickness(p, height_map, T_map, liquid_depth)
    ch0 = np.clip(height_map * 0.28, 0.0, 0.28)
    return np.stack([
        ch0.astype(np.float32),
        _pressure_map(p, height_map, T_map, base_pressure_pa, liquid_depth),
        ice_thickness.astype(np.float32),
        liquid_depth.astype(np.float32),
    ], axis=-1).astype(np.float32)


def generate_material1(T_map: np.ndarray, liquid_depth: np.ndarray | None = None,
                       ice_thickness: np.ndarray | None = None,
                       biome: np.ndarray | None = None) -> np.ndarray:
    hc = np.full(T_map.shape, 72000.0, dtype=np.float32)
    if biome is not None:
        hc = np.where(np.isin(biome, (3, 8)), 61000.0, hc)
        hc = np.where(biome == 10, 79000.0, hc)
    if liquid_depth is not None:
        hc = np.where(liquid_depth > 0.0, 86000.0, hc)
    if ice_thickness is not None:
        hc = np.where(ice_thickness > 0.0003, 68000.0, hc)
    zeros = np.zeros_like(hc)
    return np.stack([zeros, hc, zeros, zeros], axis=-1)


def generate_normal_map(height_map: np.ndarray, strength: float = 5.0) -> np.ndarray:
    dx = (np.roll(height_map, -1, axis=1) - np.roll(height_map, 1, axis=1)) * strength
    dy = np.empty_like(height_map)
    dy[1:-1] = (height_map[2:] - height_map[:-2]) * strength
    dy[0] = (height_map[1] - height_map[0]) * strength
    dy[-1] = (height_map[-1] - height_map[-2]) * strength
    nx = -dx; ny = -dy; nz = np.ones_like(height_map)
    length = np.sqrt(nx * nx + ny * ny + nz * nz)
    return np.stack((nx / length, ny / length, nz / length), axis=-1).astype(np.float32)


def compute_water_settings(surface: dict, ocean_block: dict, terrain: dict | None,
                           radius_m: float, bump_height_km: float) -> dict:
    surface = surface if isinstance(surface, dict) else {}
    ocean_block = ocean_block if isinstance(ocean_block, dict) else {}
    sea_level = max(0.0, safe_float(surface.get("seaLevel", 0.0)))
    ocean_depth_km = max(0.0, safe_float(ocean_block.get("Depth", 0.0)))
    is_lacustrine = bool(ocean_depth_km > 0.0 and ocean_depth_km < 1.0 and sea_level <= 0.05)
    if is_lacustrine:
        target_water_fraction = float(np.clip(sea_level * 4.0, 0.005, 0.12))
        mode = "lacustrine"
    else:
        target_water_fraction = float(np.clip(sea_level / 2.0 if sea_level > 0.0 else 0.0, 0.0, 0.95))
        mode = "ocean"
    # material0 ch3 is normalized against the generated terrain relief in US.
    # Encoding shallow water as depth_km directly would therefore turn 0.261 km
    # into 0.261 of a 16.1 km relief range.  One channel unit must represent the
    # full relief range so channel * km_per_unit recovers the source depth.
    liquid_channel_km_per_unit = max(0.05, abs(safe_float(bump_height_km, 0.0)))
    legacy_us_sea_level_km = ocean_depth_km * liquid_channel_km_per_unit
    corrected_channel_max = min(1.0, ocean_depth_km / liquid_channel_km_per_unit)
    corrected_us_sea_level_km = corrected_channel_max * liquid_channel_km_per_unit
    return {
        "mode": mode,
        "is_lacustrine": is_lacustrine,
        "sea_level": sea_level,
        "ocean_depth_km": ocean_depth_km,
        "target_water_fraction": target_water_fraction,
        "liquid_channel_km_per_unit": liquid_channel_km_per_unit,
        "legacy_us_sea_level_km": legacy_us_sea_level_km,
        "computed_us_sea_level_km": corrected_us_sea_level_km,
    }


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
    randomize = _parse_randomize(surf.get("Randomize", (0.0, 0.0, 0.0)))
    shared_key = _shared_surface_key(obj)
    if shared_key:
        seed = _stable_surface_seed(f"shared:{shared_key}")
    else:
        seed = _make_seed(
            obj.get("name", "unknown"),
            obj.get("radius_m", 1.0),
            obj.get("mass_kg",  1.0),
            safe_float(obj.get("orbit", {}).get("SemiMajorAxis", 0.0)),
            str(surf.get("Preset", "")),
            str(surf.get("SurfStyle", "")),
            randomize,
        )
    src_t = float(obj.get("source_temp_k") or est_temp_k)
    bw, bh, _, _, _ = atlas_layout(n_bodies_total)

    log_debug(f"Generating terrain for '{obj['name']}' (map={bw}x{bh}) "
              f"(arch={arch}, seed={seed}, T≈{src_t:.0f}K)", "SURFACE")

    # Parse SE parameters
    p = parse_space_engine_surface(surf)
    p["randomize"] = randomize
    p["preset"] = str(surf.get("Preset", ""))
    p["surfStyle"] = str(surf.get("SurfStyle", ""))
    diff_hint = " ".join(str(surf.get(k, "")) for k in ("DiffMap", "BumpMap", "DiffMapAlpha")).lower()
    if "mars" in diff_hint:
        p["mainFreq"] = max(p["mainFreq"], 0.55)
        p["venusMagn"] = max(p["venusMagn"], 0.45)
        p["montesMagn"] = max(p["montesMagn"], 0.30)
        p["montesFreq"] = max(p["montesFreq"], 0.40)
        p["hillsMagn"] = max(p["hillsMagn"], 0.16)
        p["craterMagn"] = max(p["craterMagn"], 0.28)
        p["craterDensity"] = max(p["craterDensity"], 0.28)
        p["craterFreq"] = max(p["craterFreq"], 0.55)
        p["canyonsMagn"] = max(p["canyonsMagn"], 0.16)
        p["canyonsFraction"] = max(p["canyonsFraction"], 0.25)
        p["humidity"] = max(p["humidity"], 0.45)
    elif "earth" in diff_hint:
        p["mainFreq"] = max(p["mainFreq"], 0.70)
        p["venusMagn"] = max(p["venusMagn"], 0.25)
        p["montesMagn"] = max(p["montesMagn"], 0.18)
        p["hillsMagn"] = max(p["hillsMagn"], 0.18)
        p["craterMagn"] = min(p["craterMagn"], 0.05)
        p["craterDensity"] = min(p["craterDensity"], 0.04)
        p["erosion"] = max(p["erosion"], 0.45)
        p["humidity"] = max(p["humidity"], 0.62)

    family = detect_terrain_family(surf, obj)
    opts = _surface_options(obj)
    p["terrain_family"] = str(opts.get("terrain_family") or family["primary"])
    p["secondary_family"] = opts.get("secondary_family", family["secondary"])
    p["terrain_blend"] = str(opts.get("terrain_blend") or family["blend"])

    flags = raw.get("_source_flags", {}) if isinstance(raw.get("_source_flags"), dict) else {}
    has_ocean_block = isinstance(raw.get("Ocean"), dict) or bool(flags.get("has_ocean_block"))
    no_ocean = se_bool(raw.get("NoOcean", "false")) or bool(flags.get("has_no_ocean"))
    ocean_block = raw.get("Ocean", {}) if isinstance(raw.get("Ocean"), dict) else {}

    source_sea_level = safe_float(surf.get("seaLevel", 0.0), 0.0)
    p["target_water_fraction"] = map_se_sea_level(
        source_sea_level, p["preset"], has_ocean_block, no_ocean)
    p["seaLevel"] = p["target_water_fraction"]
    if has_ocean_block and not no_ocean:
        water_settings = compute_water_settings(
            surf, ocean_block, None, obj.get("radius_m", 0.0), p.get("BumpHeight", 0.0)
        )
        if water_settings["is_lacustrine"]:
            p["water_mode"] = "lacustrine"
            p["target_water_fraction"] = water_settings["target_water_fraction"]
            p["ocean_depth_km"] = water_settings["ocean_depth_km"]
            p["liquid_channel_km_per_unit"] = water_settings["liquid_channel_km_per_unit"]
            p["seaLevel"] = max(0.001, min(0.12, water_settings["target_water_fraction"]))
            p["target_water_fraction"] = p["seaLevel"]
            log_debug(
                f"[water-depth] Body='{obj.get('name', 'unknown')}' mode='lacustrine' "
                f"seaLevel={water_settings['sea_level']:.6g} "
                f"source_depth_km={water_settings['ocean_depth_km']:.6g} "
                f"target_water_fraction={water_settings['target_water_fraction']:.6g} "
                f"generated_max_depth_km={water_settings['computed_us_sea_level_km']:.6g}",
                "WATER_DEPTH",
            )
            if water_settings["legacy_us_sea_level_km"] > water_settings["ocean_depth_km"] * 1.5:
                log_debug(
                    f"[water-depth-warning] Body='{obj.get('name', 'unknown')}' "
                    f"computed_us_sea_level_km={water_settings['legacy_us_sea_level_km']:.6g} "
                    f"exceeds source_depth_km={water_settings['ocean_depth_km']:.6g}; "
                    "applying shallow-water scale correction",
                    "WATER_DEPTH_WARN",
                )

    work_w, work_h = bw, bh
    use_work_res = bool(
        SURFACE_GENERATE_LOWRES_THEN_UPSCALE
        and (bw > SURFACE_WORK_RES_MAX_W or bh > SURFACE_WORK_RES_MAX_H)
    )
    if use_work_res:
        scale = min(SURFACE_WORK_RES_MAX_W / max(1, bw), SURFACE_WORK_RES_MAX_H / max(1, bh), 1.0)
        work_w = max(64, int(round(bw * scale)))
        work_h = max(32, int(round(bh * scale)))
        if work_w % 2:
            work_w += 1
        if work_h % 2:
            work_h += 1
        log_debug(
            f"[surface-stage] body='{obj['name']}' stage='work_resolution' "
            f"size={bw}x{bh} work={work_w}x{work_h}",
            "SURFACE",
        )

    # Override module-level grids to match this body's active generation resolution.
    global _GRIDS, TILE_W, TILE_H
    saved_grids = _GRIDS
    saved_tw, saved_th = TILE_W, TILE_H

    try:
        body_start = time.perf_counter()
        TILE_W = work_w
        TILE_H = work_h
        _GRIDS = _make_grids_at(work_w, work_h)
        height = generate_heightmap(p, seed, obj.get("name", "unknown"))

        if use_work_res:
            upscale_start = time.perf_counter()
            height = _resize_float_map(height, bw, bh)
            _surface_stage_log(obj.get("name", "unknown"), "upscale", upscale_start, size=(bw, bh))

        TILE_W = bw
        TILE_H = bh
        _GRIDS = _make_grids_at(bw, bh)

        # Run material/climate stages at final atlas-cell resolution.
        stage_start = time.perf_counter()
        liquid  = _liquid_depth_from_height(p, height, arch, allow_liquid=not no_ocean)
        source_water_pixels = liquid > 1e-9
        source_land_pixels = ~source_water_pixels
        mean_on_water = float(np.mean(source_water_pixels[source_water_pixels])) if np.any(source_water_pixels) else 0.0
        mean_on_land = float(np.mean(source_water_pixels[source_land_pixels])) if np.any(source_land_pixels) else 0.0
        log_debug(
            f"[liquid-mask] Body='{obj.get('name', 'unknown')}' "
            f"sea_threshold={safe_float(p.get('_last_sea_threshold', p.get('seaLevel', 0.0))):.6g} "
            f"water_fraction={float(np.mean(source_water_pixels)):.6g} "
            f"mean_on_water={mean_on_water:.6g} mean_on_land={mean_on_land:.6g} "
            f"inverted={bool(mean_on_water <= mean_on_land and np.any(source_water_pixels))}",
            "SURFACE_WATER",
        )
        stage_start = _surface_stage_log(obj.get("name", "unknown"), "liquid", stage_start, size=(bw, bh))
        T_map   = generate_temperature(p, seed, height, src_t, liquid)
        stage_start = _surface_stage_log(obj.get("name", "unknown"), "temperature", stage_start, size=(bw, bh))
        humid   = generate_humidity(p, seed, height, T_map, liquid)
        stage_start = _surface_stage_log(obj.get("name", "unknown"), "humidity", stage_start, size=(bw, bh))
        biome   = generate_biomes(p, height, T_map, humid, liquid, arch)
        stage_start = _surface_stage_log(obj.get("name", "unknown"), "biomes", stage_start, size=(bw, bh))
        if p["dunesMagn"] > 0.0 and p["dunesFraction"] > 0.0:
            lon, lat, _, _, _ = _GRIDS
            x, y, z = _sphere_xyz(lon, lat)
            dry_land = (liquid <= 0.0) & (humid < p["climateSteppeMin"]) & (T_map > 270.0)
            dune_noise = _spherical_fbm(
                x, y, z, min(p["dunesFreq"], max(2.0, min(height.shape) / 3.0)),
                2, seed + 271, tuple(v * 17.0 for v in randomize),
                max_frequency=max(2.0, min(height.shape) / 3.0))
            dune_mask = dry_land.astype(np.float32) * p["dunesFraction"]
            height = np.clip(height + dune_noise * dune_mask * p["dunesMagn"] * 0.025, 0.0, 1.0)
            liquid = _liquid_depth_from_height(p, height, arch, allow_liquid=not no_ocean)
            T_map = generate_temperature(p, seed, height, src_t, liquid)
            humid = generate_humidity(p, seed, height, T_map, liquid)
            biome = generate_biomes(p, height, T_map, humid, liquid, arch)
        ice     = _ice_thickness(p, height, T_map, liquid, arch)
        stage_start = _surface_stage_log(obj.get("name", "unknown"), "ice", stage_start, size=(bw, bh))
        albedo  = generate_albedo(p, seed, height, T_map, humid, biome, liquid, ice)
        stage_start = _surface_stage_log(obj.get("name", "unknown"), "albedo", stage_start, size=(bw, bh))
        ice = _repair_ice_mask(height, liquid, T_map, ice, obj.get("name", "unknown"), obj)
        try:
            _validate_geography(height, liquid, T_map, ice, obj.get("name", "unknown"))
        except ValueError as _vge:
            if "implausible ice placement" in str(_vge):
                log_debug(f"[surface-warning] {_vge}; continuing with repaired ice mask", "SURFACE_WARN")
            else:
                raise
        for label, channel in (
            ("height", height), ("liquid", liquid), ("temperature", T_map),
            ("humidity", humid), ("albedo", albedo), ("ice", ice),
        ):
            _validate_seam(channel, label)

        # Flip vertically: row 0 = north pole in US2's convention.
        height = height[::-1, :]
        T_map  = T_map[::-1,  :]
        albedo = albedo[::-1, :]
        liquid = liquid[::-1, :]
        ice    = ice[::-1, :]
        humid  = humid[::-1, :]
        biome  = biome[::-1, :]

    # Validate — no NaN / Inf
        for name, arr in [("height", height), ("T_map", T_map),
                          ("albedo", albedo), ("liquid", liquid), ("ice", ice)]:
            if not np.isfinite(arr).all():
                log_debug(f"  WARNING: non-finite values in {name}, clamping", "SURFACE_WARN")
                arr[:] = np.nan_to_num(arr, nan=0.5, posinf=1.0, neginf=0.0)

        base_pressure = _base_pressure_pa(obj)
        # Respect SURFACE_GAS_PRESSURE_MODE: default "off" zeroes ch1 so
        # material0.surface does not fight Celestial.AtmosphereMass in US.
        _gas_mode, _static = _surface_gas_mode_runtime()
        if _gas_mode == "off":
            log_debug(
                f"[surface-gas] Body='{obj.get('name', 'unknown')}' mode='{_gas_mode}' "
                f"static={_static} reason='surface gas channel disabled'",
                "SURFACE_GAS",
            )
            base_pressure = 0.0
        mat1     = generate_material1(T_map, liquid, ice, biome)
        hc_map   = mat1[:, :, 1]
        zeros_hw = np.zeros((bh, bw), dtype=np.float32)

        data_layer = np.stack([T_map, albedo, zeros_hw, zeros_hw], axis=-1)

        mat0 = generate_material0(p, seed, T_map, albedo, height, liquid, ice, base_pressure)
        result = {
            "data":      data_layer,
            "material0": mat0,
            "material1": mat1,
            "material2": generate_material2(hc_map),
            "material3": generate_material3(hc_map),
            "height_norm": height,
            "sea_threshold": float(p.get("_last_sea_threshold", 0.0)),
            "water_mask": liquid > 0.0,
            "liquid_depth": liquid,
            "land_mask": liquid <= 0.0,
            "altitude_m": ((height - float(p.get("_last_sea_threshold", 0.0))) *
                           max(1000.0, abs(p.get("BumpHeight", 10.0)) * 1000.0)).astype(np.float32),
            "normal_map": generate_normal_map(height),
            "temperature_map": T_map,
            "humidity_map": humid,
            "biome_map": biome,
            "ice_mask": ice,
            "albedo_map": albedo,
            "pressure_map": mat0[:, :, 1],
            "heat_capacity_map": hc_map,
            "validation": _terrain_validation_metrics(height),
        }
        _surface_stage_log(obj.get("name", "unknown"), "body_complete", body_start, size=(bw, bh), total=True)
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

    The 1024x512 atlas uses exact square tiers so every body cell remains 2:1.
    """
    n = max(1, int(n_bodies))
    if n <= 1:
        grid = 1
    elif n <= 4:
        grid = 2
    elif n <= 16:
        grid = 4
    elif n <= 64:
        grid = 8
    else:
        grid = 16
    gc = gr = grid
    bw = ATLAS_W // grid
    bh = ATLAS_H // grid
    assert bw == bh * 2
    tw = bw // TILE_W
    th = bh // TILE_H
    log_debug(f"[atlas] bodies={n_bodies} grid={grid}x{grid} cell={bw}x{bh}", "SURFACE")
    return bw, bh, gc, gr, tw * th


def body_atlas_tiles(body_idx: int, n_bodies: int) -> list[int]:
    """
    Returns the ordered list of AtlasIndex values that belong to body_idx.
    The first element is what goes in SurfaceGridComponent.AtlasIndex.
    """
    if body_idx < 0 or body_idx >= min(max(1, n_bodies), ATLAS_CAPACITY):
        return []
    return [body_idx]


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


def _sanitize_surface_gas_channel(m0: np.ndarray, bodies: list,
                                  n_for_atlas: int) -> np.ndarray:
    """Final archive-level guard against active pressure maps on static imports."""
    del bodies, n_for_atlas
    gas_mode, static = _surface_gas_mode_runtime()
    surface_mode, _attach, _active = _surface_data_mode_runtime()
    channel = m0[:, :, 1]
    before_min = float(np.nanmin(channel)) if channel.size else 0.0
    before_max = float(np.nanmax(channel)) if channel.size else 0.0
    before_mean = float(np.nanmean(channel)) if channel.size else 0.0
    before_nonzero = int(np.count_nonzero(np.abs(channel) > 1e-9))
    force_passive = surface_mode != "active_legacy" and (
        gas_mode == "off" or static
    )
    if force_passive:
        channel[:] = 0.0
        reason = (
            "static imported atmosphere overrides requested surface gas channel"
            if static and gas_mode != "off"
            else "disable active surface pressure map"
        )
        log_debug(
            f"[surface-gas-sanitize] mode='{gas_mode}' surface_mode='{surface_mode}' "
            f"static={static} "
            f"ch1_before_min={before_min:.6g} ch1_before_max={before_max:.6g} "
            f"ch1_before_mean={before_mean:.6g} "
            f"ch1_before_nonzero={before_nonzero} ch1_after_max=0 "
            f"reason='{reason}'",
            "SURFACE_GAS",
        )
    else:
        log_debug(
            f"[surface-gas-sanitize] mode='{gas_mode}' static={static} "
            f"ch1_max={before_max:.6g} ch1_mean={before_mean:.6g} "
            f"ch1_nonzero={before_nonzero} reason='raw surface gas channel allowed by user'",
            "SURFACE_GAS",
        )
    return m0


def _validate_surface_gas_channel(m0: np.ndarray) -> None:
    gas_mode, static = _surface_gas_mode_runtime()
    surface_mode, _attach, _active = _surface_data_mode_runtime()
    channel = m0[:, :, 1]
    ch_min = float(np.nanmin(channel)) if channel.size else 0.0
    ch_mean = float(np.nanmean(channel)) if channel.size else 0.0
    ch_max = float(np.nanmax(channel)) if channel.size else 0.0
    nonzero = int(np.count_nonzero(np.abs(channel) > 1e-9))
    must_be_zero = surface_mode != "active_legacy"
    passed = not must_be_zero or (ch_max == 0.0 and ch_min == 0.0 and nonzero == 0)
    log_debug(
        f"[surface-gas-validate] mode='{gas_mode}' surface_mode='{surface_mode}' "
        f"static={static} ch1_min={ch_min:.6g} "
        f"ch1_mean={ch_mean:.6g} ch1_max={ch_max:.6g} nonzero={nonzero} "
        f"{'PASS' if passed else 'FAIL'}",
        "SURFACE_GAS",
    )
    if not passed:
        raise ValueError(
            "Surface validation failed: material0.surface ch1 is nonzero outside "
            "Active legacy / dangerous mode. This would let Universe Sandbox rewrite "
            "imported atmosphere pressure."
        )


def _surface_channel_stats(channel: np.ndarray) -> tuple[float, float, float, int]:
    if not channel.size:
        return 0.0, 0.0, 0.0, 0
    return (
        float(np.nanmin(channel)),
        float(np.nanmean(channel)),
        float(np.nanmax(channel)),
        int(np.count_nonzero(np.abs(channel) > 1e-9)),
    )


def sanitize_surface_arrays_for_mode(data: np.ndarray, m0: np.ndarray,
                                     m1: np.ndarray, m2: np.ndarray,
                                     m3: np.ndarray, bodies=None,
                                     n_for_atlas: int = 0):
    """Apply final channel semantics immediately before archive serialization."""
    mode, attach, active = _surface_data_mode_runtime()
    safe_mode = mode in SAFE_PASSIVE_SURFACE_MODES
    raw_liquid = np.clip(np.array(m0[:, :, 3], copy=True), 0.0, 1.0)
    raw_ice = np.clip(np.array(m0[:, :, 2], copy=True), 0.0, 1.0)

    if mode == "liquid_mask_only":
        liquid_mask = (raw_liquid > 1e-9).astype(np.float32)
        data[:, :, :] = 0.0
        m0[:, :, :] = 0.0
        m1[:, :, :] = 0.0
        m2[:, :, :] = 0.0
        m3[:, :, :] = 0.0
        m0[:, :, 3] = liquid_mask
        try:
            import globals_compat as runtime
            export_ice = bool(getattr(runtime, "SAFE_SURFACE_EXPORT_ICE_MASK", False))
        except Exception:
            export_ice = False
        if export_ice:
            m0[:, :, 2] = raw_ice
    elif mode in {"none", "preview_only"}:
        m0[:, :, 1] = 0.0
        m1[:, :, :] = 0.0
        m2[:, :, :] = 0.0
        m3[:, :, :] = 0.0
    elif mode == "full_us_like":
        m0[:, :, 1] = 0.0
        m1[:, :, :] = 0.0
        m2[:, :, :] = 0.0
        m3[:, :, :] = 0.0
        log_debug(
            "[surface-passive] mode='full_us_like' kept visual maps, zeroed active physics channels",
            "SURFACE_CHANNEL",
        )
    elif mode == "active_legacy":
        log_debug(
            "[surface-warning] Active legacy surface physics can rewrite atmosphere pressure.",
            "SURFACE_WARN",
        )

    failures = []
    for label, array in (
        ("material0", m0), ("material1", m1),
        ("material2", m2), ("material3", m3),
    ):
        ch_min, ch_mean, ch_max, nonzero = _surface_channel_stats(array[:, :, 1])
        log_debug(
            f"[surface-channel] mode='{mode}' attach={attach} active={active} "
            f"{label}.ch1 min={ch_min:.6g} mean={ch_mean:.6g} "
            f"max={ch_max:.6g} nonzero={nonzero}",
            "SURFACE_CHANNEL",
        )
        if safe_mode and (ch_min != 0.0 or ch_max != 0.0 or nonzero != 0):
            failures.append(label)
    if failures:
        raise ValueError(
            "Surface physics validation failed: active material channel is nonzero "
            f"in safe surface mode ({', '.join(failures)})."
        )

    if mode != "active_legacy" and np.allclose(
        m0[:, :, 3], data[:, :, 1], rtol=1e-6, atol=1e-7
    ) and np.any(m0[:, :, 3] > 1e-9):
        raise ValueError(
            "Surface validation failed: material0.surface ch3 duplicates albedo; "
            "it must contain only liquid/water mask or depth."
        )
    liquid_min, liquid_mean, liquid_max, liquid_nonzero = _surface_channel_stats(
        m0[:, :, 3]
    )
    log_debug(
        f"[surface-channel] material0.ch3 liquid_mask min={liquid_min:.6g} "
        f"mean={liquid_mean:.6g} max={liquid_max:.6g} nonzero={liquid_nonzero}",
        "SURFACE_CHANNEL",
    )
    if liquid_min < 0.0 or liquid_max > 1.0:
        raise ValueError("Liquid mask validation failed: material0 ch3 must stay in 0..1.")

    if mode == "liquid_mask_only" and bodies and n_for_atlas > 0:
        bw, bh, grid_cols, _grid_rows, _tiles = atlas_layout(n_for_atlas)
        for idx, obj in enumerate(bodies[:n_for_atlas]):
            row = idx // grid_cols
            col = idx % grid_cols
            ys = slice(row * bh, (row + 1) * bh)
            xs = slice(col * bw, (col + 1) * bw)
            source_liquid = raw_liquid[ys, xs]
            final_liquid = m0[ys, xs, 3]
            water_pixels = source_liquid > 1e-9
            land_pixels = ~water_pixels
            mean_on_water = float(np.mean(final_liquid[water_pixels])) if np.any(water_pixels) else 0.0
            mean_on_land = float(np.mean(final_liquid[land_pixels])) if np.any(land_pixels) else 0.0
            raw = obj.get("raw_data", {}) if isinstance(obj, dict) else {}
            no_ocean = se_bool(raw.get("NoOcean", "false"))
            ocean = raw.get("Ocean", {}) if isinstance(raw.get("Ocean"), dict) else {}
            expects_ocean = bool(ocean) and not no_ocean
            water_fraction = float(np.count_nonzero(final_liquid > 0.01)) / max(1, final_liquid.size)
            inverted = bool(np.any(water_pixels) and mean_on_water <= mean_on_land)
            name = obj.get("name", "unknown") if isinstance(obj, dict) else "unknown"
            log_debug(
                f"[liquid-mask] Body='{name}' ocean={expects_ocean} no_ocean={no_ocean} "
                f"water_fraction={water_fraction:.6g} mean_on_water={mean_on_water:.6g} "
                f"mean_on_land={mean_on_land:.6g} inverted={inverted}",
                "SURFACE_WATER",
            )
            if inverted:
                raise ValueError(
                    "Liquid mask validation failed: material0 ch3 appears inverted. "
                    "Water must be high/white and land must be low/black."
                )
            if no_ocean and np.any(final_liquid > 0.01):
                raise ValueError(f"Liquid mask validation failed: NoOcean body '{name}' has water pixels.")
            if expects_ocean and not np.any(final_liquid > 0.01):
                raise ValueError(f"Liquid mask validation failed: ocean body '{name}' has no water pixels.")
    return data, m0, m1, m2, m3

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
    shared_layers = {}

    for obj in bodies[:capacity]:
        group_key = _shared_surface_key(obj)
        cache_key = (group_key, n_for_atlas) if group_key else None
        if cache_key and cache_key in shared_layers:
            layers = {name: arr.copy() for name, arr in shared_layers[cache_key].items()}
        else:
            src_t  = float(obj.get("source_temp_k") or 291.0)
            layers = generate_body_surfaces(obj, est_temp_k=src_t, n_bodies_total=n_for_atlas)
            if cache_key:
                shared_layers[cache_key] = {
                    name: arr.copy() for name, arr in layers.items()
                    if name in {"data", "material0", "material1", "material2", "material3"}
                }
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
    m0 = _sanitize_surface_gas_channel(m0, bodies[:capacity], n_for_atlas)
    da, m0, m1, m2, m3 = sanitize_surface_arrays_for_mode(
        da, m0, m1, m2, m3, bodies[:capacity], n_for_atlas
    )
    _validate_surface_gas_channel(m0)

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


def _save_debug_preview(path, arr, mode="auto") -> None:
    """Optional PNG writer for inspecting generated float maps."""
    try:
        from PIL import Image
    except Exception as exc:
        raise RuntimeError("PIL/Pillow is required only for debug preview PNGs") from exc

    a = np.asarray(arr, dtype=np.float32)
    if a.ndim == 3 and a.shape[2] in (3, 4):
        rgb = a[:, :, :3]
        if mode == "normal":
            rgb = rgb * 0.5 + 0.5
        Image.fromarray((np.clip(rgb, 0.0, 1.0) * 255.0).astype(np.uint8), mode="RGB").save(path)
        return
    finite = np.isfinite(a)
    if not finite.any():
        a8 = np.zeros(a.shape, dtype=np.uint8)
    else:
        if mode == "temperature":
            lo, hi = 220.0, 330.0
        elif mode == "pressure":
            vals = a[finite & (a > 0.0)]
            lo, hi = (float(np.percentile(vals, 1)), float(np.percentile(vals, 99))) if vals.size else (0.0, 1.0)
        elif mode in ("ice", "liquid", "elevation", "aerial"):
            lo, hi = float(np.nanmin(a)), float(np.nanmax(a))
        else:
            lo, hi = np.percentile(a[finite], [1.0, 99.0])
        if hi <= lo:
            hi = lo + 1.0
        a8 = (np.clip((a - lo) / (hi - lo), 0.0, 1.0) * 255.0).astype(np.uint8)
    Image.fromarray(a8, mode="L").save(path)


def debug_dump_surface_previews(layers: dict, output_dir, prefix="surface") -> None:
    os.makedirs(output_dir, exist_ok=True)
    previews = {
        "generated_height": (layers.get("height_norm", layers["material0"][:, :, 0]), "elevation"),
        "generated_normal": (layers.get("normal_map"), "normal"),
        "generated_water_mask": (layers.get("water_mask", layers["material0"][:, :, 3] > 0), "liquid"),
        "generated_liquid_depth": (layers.get("liquid_depth", layers["material0"][:, :, 3]), "liquid"),
        "generated_temperature": (layers.get("temperature_map", layers["data"][:, :, 0]), "temperature"),
        "generated_humidity": (layers.get("humidity_map"), "auto"),
        "generated_biomes": (layers.get("biome_map"), "auto"),
        "generated_albedo": (layers.get("albedo_map", layers["data"][:, :, 1]), "auto"),
        "generated_pressure": (layers.get("pressure_map", layers["material0"][:, :, 1]), "pressure"),
        "generated_ice": (layers.get("ice_mask", layers["material0"][:, :, 2]), "ice"),
        "generated_heat_capacity": (layers.get("heat_capacity_map", layers["material1"][:, :, 1]), "pressure"),
        "surface_temps": (layers["data"][:, :, 0], "temperature"),
        "aerial": (layers["data"][:, :, 1], "aerial"),
        "elevation": (layers["material0"][:, :, 0], "elevation"),
        "gas_pressure_vapor_channel_material0_ch1": (layers["material0"][:, :, 1], "pressure"),
        "ice_frozen_mask": (layers["material0"][:, :, 2], "ice"),
        "liquid_water_mask": (layers["material0"][:, :, 3], "liquid"),
    }
    for name, (arr, mode) in previews.items():
        if arr is not None:
            _save_debug_preview(os.path.join(output_dir, f"{prefix}_{name}.png"), arr, mode)
    if all(key in layers for key in ("biome_map", "water_mask", "ice_mask", "height_norm")):
        biome = layers["biome_map"]
        palette = np.array([
            [0.02, 0.10, 0.28], [0.04, 0.28, 0.48], [0.76, 0.66, 0.40],
            [0.58, 0.42, 0.20], [0.40, 0.55, 0.23], [0.13, 0.37, 0.12],
            [0.05, 0.48, 0.16], [0.44, 0.48, 0.43], [0.42, 0.38, 0.34],
            [0.92, 0.95, 0.98], [0.85, 0.18, 0.03],
        ], dtype=np.float32)
        composite = palette[np.clip(biome, 0, len(palette) - 1)]
        _save_debug_preview(os.path.join(output_dir, f"{prefix}_generated_composite.png"), composite, "rgb")
    gas_mode, static = _surface_gas_mode_runtime()
    if gas_mode == "off" or (static and gas_mode != "raw"):
        log_debug(
            "[surface-preview] Gas Pressure / Vapor Channel (material0 ch1) "
            "disabled for static imported atmosphere",
            "SURFACE_GAS",
        )
    log_debug("[surface-preview] Ice / Frozen Mask (material0 ch2)", "SURFACE_PREVIEW")
    log_debug("[surface-preview] Liquid / Water Mask (material0 ch3)", "SURFACE_PREVIEW")


def debug_dump_atlas_body_previews(surface_archive, output_dir, n_bodies, prefix="atlas") -> None:
    """Dump per-body atlas crops, matching what the converter assigns by body order."""
    os.makedirs(output_dir, exist_ok=True)
    with zipfile.ZipFile(surface_archive, "r") as zf:
        arrays = {
            "temperature": np.frombuffer(zf.read("data.surface"), dtype="<f4").reshape((ATLAS_H, ATLAS_W, 4))[:, :, 0],
            "aerial": np.frombuffer(zf.read("data.surface"), dtype="<f4").reshape((ATLAS_H, ATLAS_W, 4))[:, :, 1],
            "elevation": np.frombuffer(zf.read("material0.surface"), dtype="<f4").reshape((ATLAS_H, ATLAS_W, 4))[:, :, 0],
            "gas_pressure_vapor_channel_material0_ch1": np.frombuffer(zf.read("material0.surface"), dtype="<f4").reshape((ATLAS_H, ATLAS_W, 4))[:, :, 1],
            "ice": np.frombuffer(zf.read("material0.surface"), dtype="<f4").reshape((ATLAS_H, ATLAS_W, 4))[:, :, 2],
            "liquid": np.frombuffer(zf.read("material0.surface"), dtype="<f4").reshape((ATLAS_H, ATLAS_W, 4))[:, :, 3],
            "heat": np.frombuffer(zf.read("material1.surface"), dtype="<f4").reshape((ATLAS_H, ATLAS_W, 4))[:, :, 1],
        }
    bw, bh, gc, _, _ = atlas_layout(n_bodies)
    modes = {
        "temperature": "temperature",
        "aerial": "aerial",
        "elevation": "elevation",
        "gas_pressure_vapor_channel_material0_ch1": "pressure",
        "ice": "ice",
        "liquid": "liquid",
        "heat": "pressure",
    }
    for name, arr in arrays.items():
        finite = np.asarray(arr, dtype=np.float32)
        lo, hi = np.percentile(finite[np.isfinite(finite)], [1.0, 99.0])
        if hi <= lo:
            hi = lo + 1.0
        gray = np.clip((finite - lo) / (hi - lo), 0.0, 1.0)
        marked = np.repeat(gray[:, :, None], 3, axis=2)
        for xline in range(bw, ATLAS_W, bw):
            marked[:, max(0, xline - 1):xline + 1] = (1.0, 0.15, 0.05)
        for yline in range(bh, ATLAS_H, bh):
            marked[max(0, yline - 1):yline + 1, :] = (1.0, 0.15, 0.05)
        _save_debug_preview(
            os.path.join(output_dir, f"{prefix}_full_{name}_cells.png"), marked, "rgb")
    for body_idx in range(min(n_bodies, ATLAS_CAPACITY)):
        bc = body_idx % gc
        br = body_idx // gc
        y0 = br * bh
        x0 = bc * bw
        for name, arr in arrays.items():
            crop = arr[y0:y0 + bh, x0:x0 + bw]
            _save_debug_preview(
                os.path.join(output_dir, f"{prefix}_body{body_idx:02d}_{name}.png"),
                crop,
                modes.get(name, "auto"),
            )


def _surface_stats(arr: np.ndarray) -> dict:
    a = np.asarray(arr, dtype=np.float64)
    a = a[np.isfinite(a)]
    if a.size == 0:
        return {k: float("nan") for k in ("min", "p01", "p05", "p25", "mean", "median", "p75", "p95", "p99", "max", "std", "nonzero_frac")}
    p01, p05, p25, p75, p95, p99 = np.percentile(a, [1, 5, 25, 75, 95, 99])
    return {
        "min": float(np.min(a)),
        "p01": float(p01),
        "p05": float(p05),
        "p25": float(p25),
        "mean": float(np.mean(a)),
        "median": float(np.median(a)),
        "p75": float(p75),
        "p95": float(p95),
        "p99": float(p99),
        "max": float(np.max(a)),
        "std": float(np.std(a)),
        "nonzero_frac": float(np.count_nonzero(a) / a.size),
    }


def _format_surface_stats(label: str, arr: np.ndarray) -> str:
    s = _surface_stats(arr)
    return (
        f"{label}: min={s['min']:.6g} p01={s['p01']:.6g} p05={s['p05']:.6g} "
        f"p25={s['p25']:.6g} mean={s['mean']:.6g} median={s['median']:.6g} "
        f"p75={s['p75']:.6g} p95={s['p95']:.6g} p99={s['p99']:.6g} "
        f"max={s['max']:.6g} std={s['std']:.6g} nonzero={s['nonzero_frac']:.3f}"
    )


def debug_compare_surface_archive(path) -> list[str]:
    """Read a surface.zip/.ubox and return Universe Sandbox-style channel stats."""
    names = ("data.surface", "material0.surface", "material1.surface", "material2.surface", "material3.surface")
    out = []
    with zipfile.ZipFile(path, "r") as zf:
        arrays = {
            name: np.frombuffer(zf.read(name), dtype="<f4").reshape((ATLAS_H, ATLAS_W, 4))
            for name in names
        }
    labels = [
        ("data ch0", arrays["data.surface"][:, :, 0]),
        ("data ch1", arrays["data.surface"][:, :, 1]),
        ("material0 ch0", arrays["material0.surface"][:, :, 0]),
        ("material0 ch1", arrays["material0.surface"][:, :, 1]),
        ("material0 ch2", arrays["material0.surface"][:, :, 2]),
        ("material0 ch3", arrays["material0.surface"][:, :, 3]),
        ("material1 ch1", arrays["material1.surface"][:, :, 1]),
        ("material2 ch1", arrays["material2.surface"][:, :, 1]),
        ("material3 ch1", arrays["material3.surface"][:, :, 1]),
    ]
    for label, arr in labels:
        out.append(_format_surface_stats(label, arr))
    a = arrays["data.surface"][:, :, 1].ravel()
    b = arrays["material0.surface"][:, :, 3].ravel()
    used = (a != 0.0) | (b != 0.0)
    if (
        np.count_nonzero(used) > 2
        and float(np.std(a[used])) > 1e-12
        and float(np.std(b[used])) > 1e-12
    ):
        corr = float(np.corrcoef(a[used], b[used])[0, 1])
    else:
        corr = 0.0
    out.append(f"corr(data ch1, material0 ch3)={corr:.6g}")
    return out


def debug_mars_like_surface(output_dir=None, write_previews=False) -> dict:
    """Tiny local validation routine; does not need reference files."""
    mock = {
        "name": "Debug Mars Like",
        "archetype": "terra",
        "decl_type": "Planet",
        "radius_m": 3389500.0,
        "mass_kg": 6.4171e23,
        "source_temp_k": 291.0,
        "atm_info": {"Pressure": 0.18},
        "orbit": {"SemiMajorAxis": 2.279e11},
        "raw_data": {
            "Class": "Terra",
            "Atmosphere": {"Pressure": 0.18},
            "Surface": {
                "seaLevel": 0.28,
                "snowLevel": 0.55,
                "icecapLatitude": 0.72,
                "icecapHeight": 0.55,
                "climatePole": 0.2,
                "climateTropic": 0.55,
                "climateEquator": 0.78,
                "humidity": 0.45,
                "heightTempGrad": 0.55,
                "mainFreq": 1.2,
                "venusMagn": 1.1,
                "montesMagn": 3.0,
                "montesFreq": 80.0,
                "montesSpiky": 0.65,
                "montesFraction": 0.55,
                "hillsMagn": 1.5,
                "hillsFreq": 400.0,
                "hillsFraction": 0.55,
                "riftsMagn": 10.0,
                "riftsFreq": 1.4,
                "craterMagn": 3.5,
                "craterFreq": 25.0,
                "craterDensity": 0.32,
                "craterOctaves": 5,
                "craterRayedFactor": 0.2,
                "erosion": 0.35,
                "BumpHeight": 9000.0,
            },
        },
    }
    layers = generate_body_surfaces(mock, est_temp_k=291.0, n_bodies_total=1)
    checks = [
        ("data ch0", layers["data"][:, :, 0]),
        ("data ch1", layers["data"][:, :, 1]),
        ("material0 ch0", layers["material0"][:, :, 0]),
        ("material0 ch1", layers["material0"][:, :, 1]),
        ("material0 ch2", layers["material0"][:, :, 2]),
        ("material0 ch3", layers["material0"][:, :, 3]),
        ("material1 ch1", layers["material1"][:, :, 1]),
    ]
    for label, arr in checks:
        print(_format_surface_stats(label, arr))
        assert np.isfinite(arr).all(), label
    assert np.min(layers["data"][:, :, 0]) > 0.0
    land = layers["material0"][:, :, 0] > 1e-4
    liquid = layers["material0"][:, :, 3]
    assert np.max(liquid[land]) <= 1e-5
    assert np.count_nonzero(liquid > 0.0) > 0
    assert not np.allclose(layers["data"][:, :, 1], liquid)
    assert float(np.max(layers["material0"][:, :, 2])) <= 0.011
    if write_previews and output_dir:
        debug_dump_surface_previews(layers, output_dir, "mars_like")
    return layers