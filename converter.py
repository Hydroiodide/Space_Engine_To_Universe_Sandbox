"""converter.py — SE → US translation logic and US → SE back-export."""

import json, re, os, math, uuid, zipfile, base64, random
import colorsys

import constants as _const
from constants import (
    log_debug, safe_float, _now_utc,
    GRAVITATIONAL_CONSTANT, EARTH_MASS_KG, EARTH_RADIUS_M,
    AU_TO_METERS, AU_TO_KM, GYR_TO_SECONDS,
    VALID_SE_TYPES, _ARCHETYPE_DEFAULT_TEMPS,
    SOLAR_RADIUS_M, US_STAR_TYPE_MAIN_SEQUENCE, US_STAR_TYPE_NEUTRON,
)
from surface_generator import body_atlas_tiles, should_generate_surface, write_surface_archive

from scanner import (
    parse_se_file, extract_physical_properties, extract_se_extras,
    apply_limit_filter,
)
from builder import (
    eci_to_us, us_to_eci, parse_vec3, parse_rgba,
    orbital_elements_to_state_vectors, estimate_eq_temp,
    build_ubox_entity, build_ring_particles, build_asteroid_ring_particle,
    particles_to_se_rings, apply_system_age,
    calc_orbital_elements, rotation_axis_to_se,
    calculate_banding_offsets_proper, compute_rotation_us,
    get_ui_color, _generate_gas_giant_palette_from_preset,
    _calc_atmosphere_mass,
    classify_stellar_object, get_star_color_from_se, analyse_cloud_layers,
)
from constants import detect_brown_dwarf_type


# ─── Translation DB ───────────────────────────────────────────────────────────

def _builtin_db():
    return {
        "materials": {
            "rocky":     {"Iron":0.32,"Silicate":0.68},
            "ocean":     {"Iron":0.20,"Silicate":0.50},
            "ice":       {"Iron":0.15,"Silicate":0.55},
            "lava":      {"Iron":0.45,"Silicate":0.55},
            "gas_giant": {"Hydrogen":0.745,"Helium":0.235,"Iron":0.01,"Silicate":0.01},
            "ice_giant": {"Hydrogen":0.60,"Helium":0.35,"Water":0.03,"Iron":0.01,"Silicate":0.01},
            "star":      {"Hydrogen":0.74,"Helium":0.26},
        },
        "planet_classes": {
            "Terra":"rocky","Selena":"rocky","Desert":"rocky","Barren":"rocky",
            "Ferria":"lava","Lava":"lava","Volcanic":"lava",
            "Ocean":"ocean","Aquaria":"ocean","Panthalassic":"ocean","Marine":"ocean","Jungle":"ocean",
            "Ice":"ice","IceLow":"ice","Tundra":"ice","Glacial":"ice","Titan":"ice",
            "GasGiant":"gas_giant","Jupiter":"gas_giant","GasPuff":"gas_giant",
            "HotJupiter":"gas_giant","SubBrownDwarf":"gas_giant",
            "IceGiant":"ice_giant","Neptune":"ice_giant","Uranus":"ice_giant",
            "Toxic":"rocky","Carbonia":"rocky",
        },
        "aurora_to_magnetic": [
            {"north_bright_max":0.00,"north_radius_max":    0,"field":0.0,   "pole_angle":0.0},
            {"north_bright_max":0.15,"north_radius_max":  600,"field":0.5,   "pole_angle":5.0},
            {"north_bright_max":0.40,"north_radius_max": 2000,"field":1.0,   "pole_angle":11.0},
            {"north_bright_max":0.70,"north_radius_max": 5000,"field":3.0,   "pole_angle":12.0},
            {"north_bright_max":1.00,"north_radius_max":15000,"field":15.0,  "pole_angle":-10.0},
            {"north_bright_max":9999,"north_radius_max":99999,"field":6220.0,"pole_angle":-10.0},
        ],
    }

# Patch constants module so scanner's _get_magnetic_params can find it
_const._builtin_db = _builtin_db

def load_db():
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, "translation_db.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            for k, v in _builtin_db().items():
                data.setdefault(k, v)
            return data
    except FileNotFoundError:
        return _builtin_db()

DB = load_db()


# ─── Body classification ──────────────────────────────────────────────────────

def classify_archetype(obj_decl, obj_data, is_star, is_barycenter, mass_kg):
    if is_star or is_barycenter:
        return "star"
    cls_lo = obj_data.get("Class", "").strip().lower()
    for se_class, arch in DB["planet_classes"].items():
        if cls_lo == se_class.lower():
            return arch
    atm = obj_data.get("Atmosphere", {})
    if isinstance(atm, dict):
        p = safe_float(atm.get("Pressure", 0)); h = safe_float(atm.get("Height", 0))
        if p > 500:
            return "ice_giant" if any(k in cls_lo for k in ("ice","neptune","uranus")) else "gas_giant"
        if p > 100 and h > 50:
            return "ice_giant" if any(k in cls_lo for k in ("ice","neptune","uranus")) else "gas_giant"
    if any(k in cls_lo for k in ("gas","jupiter","jovian","subbrawn","hot")):  return "gas_giant"
    if any(k in cls_lo for k in ("icegiant","ice giant","neptun","uranus")):   return "ice_giant"
    if any(k in cls_lo for k in ("ocean","aquar","panthalass")):               return "ocean"
    if any(k in cls_lo for k in ("ice","glacial","tundra","cryo")):            return "ice"
    if any(k in cls_lo for k in ("lava","volcan","ferr")):                     return "lava"
    if mass_kg > EARTH_MASS_KG * 50: return "gas_giant"
    if mass_kg > EARTH_MASS_KG * 10: return "ice_giant"
    return "rocky"


def classify_body_type(obj_decl, obj_data, par_obj=None):
    obj_type  = obj_decl.split()[0].lower()
    raw_class = obj_data.get("Class", "").lower()
    if obj_type == "dwarfplanet": return "dwarf_planet"
    if obj_type == "dwarfmoon":   return "dwarf_moon"
    if obj_type == "moon":        return "moon"
    if obj_type == "planet":      return "planet"
    if "dwarfplanet" in raw_class or "dwarf planet" in raw_class: return "dwarf_planet"
    if "dwarfmoon"   in raw_class or "dwarf moon"   in raw_class: return "dwarf_moon"
    if par_obj is None: return "planet"
    parent_body_type = par_obj.get("body_type")
    if parent_body_type in ("moon","dwarf_moon","dwarf_planet"): return "dwarf_moon"
    if par_obj.get("is_star", False):
        return "dwarf_planet" if (isinstance(obj_data.get("mass_kg"), (int,float))
                                  and obj_data["mass_kg"] < EARTH_MASS_KG * 0.1) else "planet"
    par_type  = par_obj.get("decl_type","").lower()
    par_class = par_obj.get("raw_data",{}).get("Class","").lower() if par_obj.get("raw_data") else ""
    if par_type in ("dwarfplanet","dwarf_planet") or "dwarfplanet" in par_class: return "dwarf_moon"
    return "moon"


def detect_star_type(raw_class, teff, lum_watts, radius_m, mass_kg):
    class_str = str(raw_class or "").strip().upper()
    if "BROWNDWARF" in class_str or "BROWN DWARF" in class_str:
        for c in ("Y","T","L"):
            if c in class_str: return "Brown Dwarf", c, "BD"
        return "Brown Dwarf", "L", "BD"
    m = re.search(r'\b([LTY])[\d.]', class_str)
    if m: return "Brown Dwarf", m.group(1), "BD"
    if teff and teff < 2700 and (not lum_watts or lum_watts < 1e24):
        if teff < 700:  return "Brown Dwarf","Y","BD"
        if teff < 1300: return "Brown Dwarf","T","BD"
        return "Brown Dwarf","L","BD"
    if "BLACKHOLE"   in class_str or "BH" in class_str: return "Black Hole","BH","REM"
    if "NEUTRONSTAR" in class_str or "NS" in class_str: return "Neutron Star","NS","REM"
    if "WHITEDWARF"  in class_str or "WD" in class_str: return "White Dwarf","WD","VII"
    sm = re.search(r'([OBAFGKM])(\d)', class_str)
    spectral = sm.group(1) if sm else "G"
    lum_class = "V"
    if "IV"  in class_str: lum_class = "IV"
    elif class_str.count("I") >= 3: lum_class = "Ia"
    elif class_str.count("I") == 2: lum_class = "Ib"
    elif "II"  in class_str: lum_class = "II"
    elif "III" in class_str: lum_class = "III"
    elif "VI"  in class_str: lum_class = "VI"
    if "CARBON" in class_str: return "Carbon Star",  f"C{spectral}", lum_class
    if "ZIRCONIUM" in class_str: return "Zirconium Star", f"S{spectral}", lum_class
    return "Main Sequence", spectral, lum_class


# ─── Hierarchy helpers ────────────────────────────────────────────────────────

def resolve_parent(pname, parsed_objects):
    if not pname: return None
    for decl, obj in parsed_objects.items():
        if obj["name"] == pname: return decl
    for pt in ["Barycenter","Star","BlackHole","NeutronStar","WhiteDwarf",
               "Planet","DwarfPlanet","Moon","DwarfMoon","Asteroid"]:
        c = f'{pt} "{pname}"'
        if c in parsed_objects: return c
    for decl in parsed_objects:
        if f'"{pname}"' in decl or f'"{pname}/' in decl: return decl
    return None


def flatten_barycenters(parsed_objects):
    """
    Convert SE barycenter systems to US-compatible direct orbits.

    US is a full N-body simulator — it does not need barycenters.
    The wobble SE encodes via a barycenter is reproduced automatically
    by US physics once positions and velocities are correct.

    Strategy per barycenter:
      1. Identify the PRIMARY (most massive direct child).
      2. Give the primary the BARYCENTER'S own orbit (around the star/parent).
         This puts the primary at the correct heliocentric distance.
      3. Re-parent all SECONDARIES to the primary.
         Their existing SE offsets (e.g. Moon 379665 km from EMB) become
         their orbit around the primary — which is correct because SE
         already stores individual distances from the barycentre centre,
         and for the secondary the bary centre ≈ inside the primary.
      4. Mark the barycenter as consumed so pass 2 skips emitting it.

    Earth-Moon example:
      EMB orbit: SMA=1.000 AU around Sun
      Earth SMA: 3.16e-5 AU (~4729 km) from EMB  → tiny, ignored (Earth IS near bary)
      Moon SMA:  379665 km from EMB               → becomes Moon orbit around Earth
      Result: Earth gets SMA=1.000 AU around Sun, Moon gets SMA=379665 km around Earth.
    """
    # Process deepest barycenters first (bottom-up) so nested systems work correctly
    def _bary_depth(decl, po, d=0):
        par = po.get(decl,{}).get("parent_decl")
        if par and po.get(par,{}).get("is_barycenter"):
            return _bary_depth(par, po, d+1)
        return d

    bary_decls = sorted(
        [d for d, o in parsed_objects.items() if o["is_barycenter"]],
        key=lambda d: _bary_depth(d, parsed_objects),
        reverse=True
    )

    for bary_decl in bary_decls:
        bary_obj = parsed_objects[bary_decl]
        bary_orbit = bary_obj.get("orbit", {})

        # Direct children (non-bary only — nested barycenters handled in their own pass)
        children = [k for k, v in parsed_objects.items()
                    if v.get("parent_decl") == bary_decl and not v["is_barycenter"]]

        if not children:
            bary_obj["_consumed"] = True
            continue

        # Split children into binary siblings (other stars) vs circumbinary bodies (planets).
        # Stars in a binary pair need their separation reconstructed.
        # Circumbinary planets/moons already have correct AU orbits — just re-parent them.
        primary_decl = max(children, key=lambda k: parsed_objects[k]["mass_kg"])
        star_secondaries = [k for k in children if k != primary_decl]
        planet_children = []

        # Save the primary's original bary-offset for binary separation calculation
        parsed_objects[primary_decl]["_orig_orbit"] = dict(
            parsed_objects[primary_decl].get("orbit", {}))

        # Primary inherits the bary's heliocentric orbit
        if bary_orbit:
            parsed_objects[primary_decl]["orbit"] = dict(bary_orbit)
        parsed_objects[primary_decl]["parent_decl"] = bary_obj.get("parent_decl")

        # Determine the canonical period for this barycentric system.
        # Prefer an explicit Period* key from the barycenter orbit; fall back to
        # computing one from total system mass + primary's SMA.
        _canonical_period_days = None

        # Try explicit keys from the barycenter orbit first
        if bary_orbit:
            if "PeriodDays" in bary_orbit:
                _canonical_period_days = safe_float(bary_orbit["PeriodDays"])
            elif "PeriodYears" in bary_orbit:
                _canonical_period_days = safe_float(bary_orbit["PeriodYears"]) * 365.25
            elif "Period" in bary_orbit:
                _canonical_period_days = safe_float(bary_orbit["Period"]) * 365.25

        # Try explicit keys from any child's orbit block (they should all agree)
        if _canonical_period_days is None:
            for child_decl in children:
                child_orb = parsed_objects[child_decl].get("orbit", {})
                if "PeriodDays" in child_orb:
                    _canonical_period_days = safe_float(child_orb["PeriodDays"]); break
                elif "PeriodYears" in child_orb:
                    _canonical_period_days = safe_float(child_orb["PeriodYears"]) * 365.25; break
                elif "Period" in child_orb:
                    _canonical_period_days = safe_float(child_orb["Period"]) * 365.25; break

        # Last resort: Kepler from total system mass + primary SMA
        if _canonical_period_days is None:
            _total_mass = sum(parsed_objects[c]["mass_kg"] for c in children)
            _prim_orb   = parsed_objects[primary_decl].get("orbit", {})
            _sma_m      = 0.0
            if "SemiMajorAxisKm" in _prim_orb:
                _sma_m = safe_float(_prim_orb["SemiMajorAxisKm"]) * 1_000.0
            elif "SemiMajorAxis" in _prim_orb:
                _sma_m = safe_float(_prim_orb["SemiMajorAxis"]) * AU_TO_METERS
            if _sma_m > 0 and _total_mass > 0:
                _period_s = 2 * math.pi * math.sqrt(_sma_m**3 / (GRAVITATIONAL_CONSTANT * _total_mass))
                _canonical_period_days = _period_s / 86_400.0

        # Stamp the canonical period onto every child's orbit block so
        # tidal-lock derivation in Pass 1 always sees a consistent value.
        if _canonical_period_days and _canonical_period_days > 0:
            for child_decl in children:
                parsed_objects[child_decl]["orbit"]["PeriodDays"] = str(_canonical_period_days)
                parsed_objects[child_decl]["_canonical_period_days"] = _canonical_period_days
            log_debug(
                f"Bary '{bary_obj['name']}': canonical period = {_canonical_period_days:.6f} days "
                f"applied to {len(children)} children", "BARY")

        # Use Period + Kepler to determine AU vs km for a bare SemiMajorAxis value.
        # This handles deep-field systems where SMA>100 is still in AU.
        def _sma_to_km(orb, parent_mass_kg):
            if "SemiMajorAxisKm" in orb:
                return safe_float(orb["SemiMajorAxisKm"])
            if "SemiMajorAxisAU" in orb:
                return safe_float(orb["SemiMajorAxisAU"]) * AU_TO_KM
            if "SemiMajorAxisAu" in orb:
                return safe_float(orb["SemiMajorAxisAu"]) * AU_TO_KM
            if "SemiMajorAxis" not in orb:
                return 0.0
            v = safe_float(orb["SemiMajorAxis"])
            # Use Period to disambiguate AU vs km via Kepler's third law:
            # T_years² = a_AU³ / M_solar  →  a_AU = (T² × M)^(1/3)
            period_years = None
            if   "PeriodDays"  in orb: period_years = safe_float(orb["PeriodDays"]) / 365.25
            elif "PeriodYears" in orb: period_years = safe_float(orb["PeriodYears"])
            elif "Period"      in orb: period_years = safe_float(orb["Period"])
            if period_years and period_years > 0 and parent_mass_kg > 0:
                M_solar = parent_mass_kg / 1.98847e30
                a_au_kepler = (period_years**2 * M_solar) ** (1.0/3.0)
                # If the raw value matches the Kepler AU estimate within 10%, it's AU
                if a_au_kepler > 0 and abs(v - a_au_kepler) / a_au_kepler < 0.10:
                    return v * AU_TO_KM
            # Fallback: values ≥ 0.001 AU (~150,000 km) that have a year-scale period
            # are almost certainly AU; values stored as pure km are rarely > 1e6 AU
            if period_years and period_years > 1.0:
                return v * AU_TO_KM   # long period → AU scale
            if v < 1000:
                return v * AU_TO_KM   # small value → AU
            return v                  # large value without period → km

        # Binary star secondaries: orbit = full separation from bary (already their SMA)
        bary_mass = bary_obj.get("mass_kg", parsed_objects[primary_decl]["mass_kg"])
        for sec_decl in star_secondaries:
            sec_orb = parsed_objects[sec_decl].get("orbit", {})
            sep_km  = _sma_to_km(sec_orb, bary_mass)
            if sep_km > 0:
                new_orb = dict(sec_orb)
                for _k in ("SemiMajorAxis","SemiMajorAxisAU","SemiMajorAxisAu","SemiMajorAxisKm"):
                    new_orb.pop(_k, None)
                new_orb["SemiMajorAxisKm"] = str(sep_km)
                parsed_objects[sec_decl]["orbit"] = new_orb
            parsed_objects[sec_decl]["parent_decl"] = primary_decl
            log_debug(f"Bary flatten (binary): '{parsed_objects[sec_decl]['name']}' "
                      f"orbits '{parsed_objects[primary_decl]['name']}' at {sep_km:.0f} km", "BARY")

        # Circumbinary planets/moons: keep their existing AU orbit, just re-parent to primary
        for pl_decl in planet_children:
            parsed_objects[pl_decl]["parent_decl"] = primary_decl
            sma_raw = parsed_objects[pl_decl].get("orbit",{}).get("SemiMajorAxis","—")
            log_debug(f"Bary flatten (circumbinary): '{parsed_objects[pl_decl]['name']}' "
                      f"orbits '{parsed_objects[primary_decl]['name']}' SMA={sma_raw} AU", "BARY")

        log_debug(
            f"Bary flatten '{bary_obj['name']}': primary='{parsed_objects[primary_decl]['name']}' "
            f"gets bary orbit, {len(star_secondaries)+len(planet_children)} child(ren) re-parented",
            "BARY"
        )
        bary_obj["_consumed"] = True


def _find_root_star(decl, parsed_objects):
    visited = set()
    cur = parsed_objects.get(decl, {}).get("parent_decl")
    while cur and cur not in visited:
        visited.add(cur)
        obj = parsed_objects.get(cur)
        if obj is None: break
        if obj["is_star"]: return cur
        cur = obj.get("parent_decl")
    return None


# ─── Chemical / surface colour maps (populated at conversion time) ─────────────

CHEMICAL_COLOR_MAP     = {}
SURFACE_FEATURE_COLOR_MAP = {}

_CHEM_COLORS = {
    'ammonia_life':(210,180,100),'ammonia_cloud':(230,230,220),'water_cloud':(200,220,255),
    'cloudless_rayleigh':(50,100,220),'alkali_metal':(40,30,60),'silicate_cloud':(180,50,20),
    'helium_giant':(220,230,240),'brown_dwarf_purple':(100,20,100),'jool_green':(80,200,80),
    'mars_atm_dust':(210,140,90),'rock_silicate':(180,150,120),'barren_rock':(130,130,130),
    'ice_white':(235,245,255),'volcanic_basalt':(60,50,50),'terra_green':(80,140,90),
    'gliese_magenta':(255,50,150),'glass_rain_blue':(20,40,200),'pitch_black':(15,15,15),
    'lava_glow':(255,60,10),'toxic_venus':(245,235,180),'basalt_gray':(50,50,55),
    'tholin_red':(140,60,30),'chlorine_yellow_green':(160,220,50),'sulfur_yellow':(230,200,40),
    'tres_2b_black':(5,5,5),'wasp_76b_gray':(100,100,110),'white_clouds':(240,240,240),
    'copper_oxide_teal':(40,180,160),
}
_SURFACE_COLORS = {
    'reverse_earth_land':(40,80,160),'reverse_earth_water':(30,180,80),
    'ammonia_ocean':(150,180,200),'retinol_purple':(140,40,200),
    'iron_green_ocean':(40,150,90),'iron_rust_ocean':(180,40,40),
}
CHEMICAL_COLOR_MAP.update(_CHEM_COLORS)
SURFACE_FEATURE_COLOR_MAP.update(_SURFACE_COLORS)


def _infer_composition_keywords(obj_id, archetype, atm_info, has_life,
                                surface_preset, raw_data, has_ocean=False):
    keywords = []; overrides = {}
    comp = (atm_info or {}).get('comp', {})
    def _pct(mol): return float(comp.get(mol, 0)) if comp.get(mol) else 0.0
    preset_lo = (surface_preset or '').lower()
    arch      = (archetype or '').lower()
    albedo    = float(raw_data.get("AlbedoBond", 0.3))
    press     = float((atm_info or {}).get("Pressure", 0))
    teff      = float(raw_data.get("Te", raw_data.get("Teff", 280)))

    if arch in ("gas_giant","ice_giant"):
        mass_kg = float(raw_data.get("Mass",0)) * EARTH_MASS_KG if raw_data.get("Mass") else 0
        if mass_kg > 2.5e28:                                          keywords.append('brown_dwarf_purple')
        elif teff > 2000 and mass_kg > EARTH_MASS_KG*100:            keywords.append('wasp_76b_gray')
        elif albedo < 0.05 and teff > 1000:                           keywords.append('tres_2b_black')
        elif albedo < 0.08 and teff > 1000:                           keywords.append('pitch_black')
        elif 400 <= teff <= 650 and mass_kg > 1e27:                   keywords.append('gliese_magenta')
        elif 900 <= teff <= 1300:                                     keywords.append('glass_rain_blue')
        elif _pct("He") > _pct("H2") and _pct("He") > 40.0:          keywords.append('helium_giant')
        elif has_life and _pct("SO2") > 1.0:                          keywords.append('jool_green')
        else:
            if   teff < 150:  keywords.append('ammonia_cloud')
            elif teff < 250:  keywords.append('water_cloud')
            elif teff < 900:  keywords.append('cloudless_rayleigh')
            elif teff < 1400: keywords.append('alkali_metal')
            else:             keywords.append('silicate_cloud')
        if has_life:
            pick = obj_id % 3
            if   pick == 0: keywords.append('bioluminescence_blue')
            elif pick == 1: keywords.append('bioluminescence_green')
            else:           keywords.append('bioluminescence_red')
        return keywords, overrides

    if arch == 'lava' and teff > 1200:
        keywords.append('lava_glow'); overrides['rock'] = 'rock_iron'
    elif arch == 'lava' or 'ferr' in preset_lo or 'iron' in preset_lo:
        keywords.append('mars_atm_dust'); overrides['rock'] = 'rock_iron'
    if albedo < 0.12 and arch in ('asteroid','desert','rocky','moon'): keywords.append('basalt_gray')
    if press > 10 and _pct("CO2") > 50:              keywords.append('toxic_venus')
    if _pct('CH4') > 5.0:
        keywords.extend(['hydrocarbon_haze','tholin_red']); overrides['water'] = 'water'
    if _pct('SO2') > 5.0 or arch == 'toxic' or 'iodine' in preset_lo: keywords.append('sulfur_yellow')
    if _pct('Cl2') > 5.0:   keywords.append('chlorine_yellow_green')
    if 'copper' in preset_lo: keywords.append('copper_oxide_teal')
    if arch == 'ice' and teff < 100 and press < 0.1: keywords.append('tholin_red')
    if has_life and _pct('NH3') > 1.0:
        keywords.append('ammonia_life')
        if has_ocean: overrides['water'] = 'ammonia_ocean'
    if has_ocean and arch == 'ocean':
        if has_life and _pct('O2') < 1.0:
            overrides['water'] = 'iron_green_ocean' if _pct('CO2') > 10.0 else 'retinol_purple'
        elif _pct('CO2') < 1.0 and _pct('O2') > 10.0:
            overrides['water'] = 'reverse_earth_water'; overrides['vegetation'] = 'reverse_earth_land'
    if not keywords:
        if press < 0.01 and arch in ('rocky','moon','asteroid','desert') and not has_ocean:
            keywords.append('basalt_gray')
        elif arch in ('desert','rocky','asteroid','comet'): keywords.append('rock_silicate')
        elif arch == 'ice':   keywords.append('ice_white')
        elif arch == 'lava':  keywords.append('volcanic_basalt')
        elif arch == 'ocean': keywords.append('white_clouds')
        elif arch == 'terra': keywords.append('terra_green')
        else:                 keywords.append('barren_rock')
    return keywords, overrides


# ─── Main SE → US conversion ──────────────────────────────────────────────────

def _new_id() -> str:
    return base64.urlsafe_b64encode(uuid.uuid4().bytes).decode().rstrip("=")


def build_manifest(sim_name: str,
                   sim_id: str,
                   info_id: str,
                   ui_state_id: str,
                   sim_path: str,
                   info_path: str,
                   ui_state_path: str,
                   now: str,
                   surface_zip_path: str | None = None,
                   thumbnail_id: str | None = None,
                   preview_id: str | None = None) -> tuple[dict, str | None]:
    surface_zip_id = None
    entries = []

    if surface_zip_path is not None:
        surface_zip_id = _new_id()
        entries.append({
            "AssetType": "Ubox",
            "BaseType": "SurfaceData",
            "TypeName": "surface.zip",
            "BuildRevision": 46923,
            "LastModifiedUTC": now,
            "Path": os.path.basename(surface_zip_path),
            "ID": surface_zip_id,
            "Dependencies": [],
        })

    sim_deps = []
    if thumbnail_id:
        sim_deps.append({"$v": thumbnail_id})
    if preview_id:
        sim_deps.append({"$v": preview_id})
    if surface_zip_id:
        sim_deps.append({"$v": surface_zip_id})
    sim_deps.append({"$v": ui_state_id})
    sim_deps.append({"$v": info_id})

    sim_entry = {
        "Name": sim_name,
        "AssetType": "JSON",
        "BaseType": "Simulation",
        "TypeName": "simulation.json",
        "BuildRevision": 46923,
        "LastModifiedUTC": now,
        "Path": sim_path,
        "ID": sim_id,
        "Dependencies": sim_deps,
    }
    info_entry = {
        "Name": f"simulation-{sim_name}-info",
        "AssetType": "JSON",
        "BaseType": "WorkshopItem",
        "TypeName": "info.json",
        "BuildRevision": 46923,
        "LastModifiedUTC": now,
        "Path": info_path,
        "ID": info_id,
        "Dependencies": [],
    }
    ui_entry = {
        "AssetType": "JSON",
        "BaseType": "UIState",
        "TypeName": "ui-state.json",
        "BuildRevision": 46923,
        "LastModifiedUTC": now,
        "Path": ui_state_path,
        "ID": ui_state_id,
        "Dependencies": [],
    }

    return {
        "Header": {
            "BuildRevision": 46923,
            "BuildName": "Universe Sandbox",
            "LastModifiedUTC": now,
            "EntryPoints": [sim_id],
            "EntryPoint": sim_id,
        },
        "Entries": [sim_entry] + entries + [ui_entry, info_entry],
    }, surface_zip_id


def validate_manifest(manifest: dict,
                      surface_zip_path: str | None,
                      sim_name: str,
                      surface_zip_payload: bytes | None = None) -> None:
    entries = manifest.get("Entries", [])
    sim_entry = next((e for e in entries if e.get("BaseType") == "Simulation"), None)

    if sim_entry is None:
        raise ValueError("manifest: missing Simulation entry")

    if surface_zip_path is None:
        return

    zip_filename = os.path.basename(surface_zip_path)
    surf_entries = [e for e in entries if e.get("BaseType") == "SurfaceData"]
    if not surf_entries:
        raise ValueError("manifest: SurfaceData entry missing but surface zip was generated")

    se = surf_entries[0]
    if se.get("AssetType") != "Ubox":
        raise ValueError(f"manifest: SurfaceData AssetType is '{se.get('AssetType')}', expected 'Ubox'")
    if se.get("TypeName") != "surface.zip":
        raise ValueError(f"manifest: SurfaceData TypeName is '{se.get('TypeName')}', expected 'surface.zip'")
    if se.get("Path") != zip_filename:
        raise ValueError(
            f"manifest: SurfaceData Path '{se.get('Path')}' "
            f"does not match zip filename '{zip_filename}'"
        )

    surf_id = se["ID"]
    sim_deps = [d.get("$v", d) for d in sim_entry.get("Dependencies", [])]
    if surf_id not in sim_deps:
        raise ValueError(
            f"manifest: Simulation entry does not depend on SurfaceData id '{surf_id}'"
        )

    if surface_zip_payload is None and not os.path.isfile(surface_zip_path):
        raise ValueError(f"manifest: surface zip path does not exist on disk: '{surface_zip_path}'")
    if surface_zip_payload is not None and len(surface_zip_payload) == 0:
        raise ValueError("manifest: surface zip payload is empty")

    log_debug("Manifest validation passed (SurfaceData present and linked)", "MANIFEST")


def convert_to_ubox(se_data, output_file="SE_Import.ubox",
                    belt_asteroid_input="100%",
                    planetary_ring_input="100%",
                    comet_input="100%",
                    export_comets=True,
                    export_moons=True,
                    export_dwarf_moons=True,
                    export_dwarf_planets=True,
                    export_rings=True,
                    status_callback=None):
    def _status(msg):
        if status_callback: status_callback(msg)
        log_debug(msg, "STATUS")

    _const.SYSTEM_AGE_SECONDS = None

    sim_name = os.path.splitext(os.path.basename(output_file))[0]
    sim_id   = _new_id()
    info_id  = _new_id()
    ui_id    = _new_id()
    sfn = f"simulation-{sim_name}.json"
    ifn = f"simulation-{sim_name}-info.json"
    ufn = f"simulation-{sim_name}-ui-state.json"
    now = _now_utc()

    ubox_sim = {
        "Header": {"BaseType":"Simulation","AssetType":"JSON","TypeName":"simulation.json",
                   "BuildRevision":"46923","LastModifiedUTC":now},
        "$type": "Simulation",
        "Name":  sim_name,
        "Gravity": GRAVITATIONAL_CONSTANT,
        "IntegratorId":2,"IntegrationMode":1,"AdaptiveIntegration":True,
        "CollisionMode":1,"TimeStep":1.0,"TargetFrameRate":60,"SimulationSpeed":1.0,
        "Entities": [],
    }

    # Pass 1: parse all objects
    _status("Parsing objects")
    parsed_objects = {}
    current_id = 1
    for item in se_data:
        for obj_decl, obj_data in item.items():
            if not isinstance(obj_data, dict) or obj_decl.startswith("Remove"):
                continue
            parts      = obj_decl.split(" ", 1)
            decl_type  = parts[0]
            raw_name   = parts[1].strip('"') if len(parts) > 1 else parts[0]
            clean_name = raw_name.split("/")[0].strip()
            log_debug(f"Parsing: {decl_type} '{clean_name}'", "PARSE")
            is_star    = decl_type in {"Star","BlackHole","NeutronStar","WhiteDwarf"}
            is_bary    = decl_type == "Barycenter"
            mass_kg, radius_m, parent_name = extract_physical_properties(obj_decl, obj_data)
            (atm_info, has_ocean, use_water, mag_field, mag_pole, sea_level,
             preset_name, has_life, has_exotic_life, has_aerial_life,
             has_organic_life, diffmap) = extract_se_extras(obj_data)
            archetype    = classify_archetype(obj_decl, obj_data, is_star, is_bary, mass_kg)
            orbit_data   = obj_data.get("Orbit") if isinstance(obj_data.get("Orbit"), dict) else {}
            teff         = safe_float(obj_data.get("Teff",          5_800.0))
            # Surface / mean temperature from SE source — used as StartingTemperature
            _se_surf_temp = (
                obj_data.get("Temperature") or
                obj_data.get("Tsurf")       or
                obj_data.get("Tmean")       or
                obj_data.get("Te")
            )
            source_temp_k = safe_float(_se_surf_temp) if _se_surf_temp is not None else None
            lum_watts    = safe_float(obj_data.get("Luminosity",        1.0)) * 3.828e26
            raw_rot_h    = safe_float(obj_data.get("RotationPeriod", 0.0))
            is_tidal     = str(obj_data.get("TidalLocked", "false")).strip().lower() in ("true","1")
            if is_tidal and raw_rot_h == 0.0:
                # Derive rotation from orbital period
                _orb_tmp = obj_data.get("Orbit", {}) if isinstance(obj_data.get("Orbit"), dict) else {}
                if   "PeriodDays"  in _orb_tmp: raw_rot_h = safe_float(_orb_tmp["PeriodDays"]) * 24.0
                elif "PeriodYears" in _orb_tmp: raw_rot_h = safe_float(_orb_tmp["PeriodYears"]) * 8_765.81
                elif "Period"      in _orb_tmp: raw_rot_h = safe_float(_orb_tmp["Period"]) * 24.0
            rot_period_h = raw_rot_h
            obliquity    = safe_float(obj_data.get("Obliquity",         0.0))
            eq_asc       = safe_float(obj_data.get("EqAscendNode",      0.0))
            is_bd, bd_type, _ = detect_brown_dwarf_type(obj_data.get("Class",""), teff, obj_data)
            parsed_objects[obj_decl] = {
                "id":current_id,"name":clean_name,"decl_type":decl_type,
                "mass_kg":mass_kg,"radius_m":radius_m,
                "original_parent_name":parent_name,"parent_decl":None,
                "is_star":is_star,"is_barycenter":is_bary,"archetype":archetype,
                "is_brown_dwarf":is_bd,"brown_dwarf_type":bd_type,
                "teff":teff,"lum_watts":lum_watts,
                "rot_period_h":rot_period_h,"obliquity_deg":obliquity,"eq_asc_deg":eq_asc,
                "source_temp_k":source_temp_k,
                "orbit":orbit_data,
                "atm_info":atm_info,"has_ocean":has_ocean,"use_water":use_water,
                "mag_field":mag_field,"mag_pole":mag_pole,"sea_level":sea_level,
                "surface_preset":preset_name,
                "has_life":has_life,"has_exotic_life":has_exotic_life,
                "has_aerial_life":has_aerial_life,"has_organic_life":has_organic_life,
                "diffmap":diffmap,"raw_data":obj_data,"body_type":None,
            }
            current_id += 1

    # Pass 1.25: link parents
    for decl, obj in parsed_objects.items():
        pname = obj["original_parent_name"]
        if pname:
            p = resolve_parent(pname, parsed_objects)
            obj["parent_decl"] = None if p == decl else p

    # Pass 1.5: flatten barycenters
    _status("Resolving barycenter hierarchy")
    flatten_barycenters(parsed_objects)

    # Pass 1.3: validate barycentric period consistency
    for decl, obj in parsed_objects.items():
        if not obj["is_barycenter"]: continue
        sibling_periods = []
        sibling_names   = []
        for cdecl, cobj in parsed_objects.items():
            if cobj.get("parent_decl") != decl: continue
            orb = cobj.get("orbit", {})
            p = None
            if "PeriodDays"  in orb: p = safe_float(orb["PeriodDays"])
            elif "PeriodYears" in orb: p = safe_float(orb["PeriodYears"]) * 365.25
            elif "Period"      in orb: p = safe_float(orb["Period"]) * 365.25
            if p and p > 0:
                sibling_periods.append(p)
                sibling_names.append(cobj["name"])
        if len(sibling_periods) >= 2:
            p_min, p_max = min(sibling_periods), max(sibling_periods)
            if p_max / max(p_min, 1e-12) > 1.001:   # > 0.1% mismatch
                log_debug(
                    f"WARNING period mismatch under barycenter '{obj['name']}': "
                    + ", ".join(f"{n}={p:.6f}d" for n, p in zip(sibling_names, sibling_periods)),
                    "BARY_WARN")

    # Bucket and filter belt asteroids + comets
    belt_asteroids = []; comets = []
    for decl, obj in parsed_objects.items():
        dt = obj.get("decl_type","").strip().lower()
        rc = (obj.get("raw_data") or {}).get("Class","").strip().lower()
        if dt == "asteroid":                              belt_asteroids.append(decl)
        elif dt == "comet":                               comets.append(decl)
        elif dt in ("moon","dwarfmoon") and rc == "comet": comets.append(decl)

    surviving_rings  = set(apply_limit_filter(belt_asteroids, belt_asteroid_input, "belt_asteroids"))
    surviving_comets = set(apply_limit_filter(comets, comet_input if export_comets else "0", "comets"))
    to_discard = (set(belt_asteroids) | set(comets)) - surviving_rings - surviving_comets
    for decl in to_discard:
        parsed_objects.pop(decl, None)

    # Memoised absolute state (ECI, meters)
    _cache = {}
    
    def _apply_tilt(rp: list, rv: list, obl_deg: float, ean_deg: float) -> tuple:
        """Rotate position+velocity vectors by Rz(EAN)*Rx(obl)."""
        obl_r = math.radians(obl_deg); ean_r = math.radians(ean_deg)
        c_o = math.cos(obl_r); s_o = math.sin(obl_r)
        c_e = math.cos(ean_r); s_e = math.sin(ean_r)
        def _rot(v):
            x1=v[0]; y1=v[1]*c_o-v[2]*s_o; z1=v[1]*s_o+v[2]*c_o
            x2=x1*c_e-y1*s_e; y2=x1*s_e+y1*c_e
            return [x2,y2,z1]
        return _rot(rp), _rot(rv)
    
    def get_abs_state(decl, _seen=None, _depth=0):
        if decl in _cache: return _cache[decl]
        if _depth > 64: return [0.0]*3, [0.0]*3
        if _seen is None: _seen = set()
        if decl in _seen: return [0.0]*3, [0.0]*3
        _seen.add(decl)

        obj = parsed_objects.get(decl)
        if obj is None:
            result = ([0.0]*3, [0.0]*3)
        else:
            pdecl = obj["parent_decl"]
            if not pdecl or pdecl not in parsed_objects:
                result = ([0.0]*3, [0.0]*3)
            else:
                pp, pv = get_abs_state(pdecl, _seen, _depth + 1)
                pm     = parsed_objects[pdecl]["mass_kg"]
                orb    = obj["orbit"]
                sma_m  = 0.0

                if "SemiMajorAxisKm" in orb:
                    sma_m = safe_float(orb["SemiMajorAxisKm"]) * 1_000.0
                elif "SemiMajorAxisAU" in orb or "SemiMajorAxisAu" in orb:
                    sma_m = safe_float(orb.get("SemiMajorAxisAU") or orb.get("SemiMajorAxisAu",0)) * AU_TO_METERS
                elif "SemiMajorAxis" in orb:
                    sma_val   = safe_float(orb["SemiMajorAxis"])
                    obj_dtype = obj.get("decl_type","").strip().lower()
                    par_d     = parsed_objects.get(pdecl, {})
                    # Bodies orbiting a star or barycenter always use AU.
                    # Bodies orbiting a planet/moon use km.
                    # Moons/asteroids/comets declared as such always use km
                    # unless their immediate parent is a star or barycenter.
                    par_is_star_or_bary = par_d.get("is_star") or par_d.get("is_barycenter")
                    if par_is_star_or_bary:
                        is_satellite = False
                    else:
                        is_satellite = (
                            obj_dtype in ("moon","dwarfmoon","asteroid","comet")
                            or par_d.get("decl_type","").lower() in ("planet","dwarfplanet","moon","dwarfmoon")
                        )
                    sma_m = sma_val * (1_000.0 if is_satellite else AU_TO_METERS)

                if sma_m <= 0 or not math.isfinite(sma_m):
                    peri = safe_float(orb.get("PericenterDistance",0))
                    ecc  = safe_float(orb.get("Eccentricity",0))
                    if peri > 0 and 0 <= ecc < 0.9999:
                        sma_m = peri / max(0.0001, 1.0 - ecc)

                if sma_m <= 0 or not math.isfinite(sma_m):
                    period_val = None
                    if   "PeriodDays"  in orb: period_val = safe_float(orb["PeriodDays"])  * 86_400.0
                    elif "PeriodYears" in orb: period_val = safe_float(orb["PeriodYears"]) * 31_557_600.0
                    elif "Period"      in orb:
                        period_val = safe_float(orb["Period"])
                        # Barycenter parents are treated like star parents for period units:
                        # a body orbiting a bary at planetary distance has Period in years.
                        par_is_star_or_bary = (parsed_objects[pdecl].get("is_star")
                                               or parsed_objects[pdecl].get("is_barycenter"))
                        period_val *= 31_557_600.0 if par_is_star_or_bary else 86_400.0
                    if period_val and period_val > 0 and pm > 0:
                        sma_m = (GRAVITATIONAL_CONSTANT * pm * period_val**2 / (4*math.pi**2)) ** (1/3)

                if sma_m <= 0 or not math.isfinite(sma_m):
                    sma_m = max(parsed_objects[pdecl].get("radius_m", EARTH_RADIUS_M) * 3.0, 1e8)

                ecc  = max(0.0, min(0.9999, safe_float(orb.get("Eccentricity",   0))))
                inc  = safe_float(orb.get("Inclination",    0))
                asc  = safe_float(orb.get("AscendingNode",  0))
                arg  = safe_float(orb.get("ArgOfPericenter",0))
                mean = safe_float(orb.get("MeanAnomaly",    0))

                if sma_m > 0 and pm > 0:
                    rp, rv = orbital_elements_to_state_vectors(sma_m, ecc, inc, asc, arg, mean, pm)
                else:
                    rp = [sma_m, 0.0, 0.0]; rv = [0.0, 0.0, 0.0]

                rp = [0.0 if not math.isfinite(x) else x for x in rp]
                rv = [0.0 if not math.isfinite(x) else x for x in rv]

                # ──── UNIFIED TILT SYSTEM ────────────────────────────────────
                import globals_compat as _gc
                _inherit_moon_tilt     = getattr(_gc, "INHERIT_MOON_AXIAL_TILT",      True)
                _inherit_star_tilt     = getattr(_gc, "INHERIT_STAR_AXIAL_TILT",      False)
                _align_to_star_equator = getattr(_gc, "ALIGN_ORBITS_TO_STAR_EQUATOR", False)

                par_s = parsed_objects.get(pdecl)

                # Moon inherits parent PLANET tilt
                if (_inherit_moon_tilt and par_s
                        and not par_s.get("is_star") and not par_s.get("is_barycenter")):
                    p_obl = safe_float(par_s.get("obliquity_deg", 0.0))
                    p_ean = safe_float(par_s.get("eq_asc_deg",    0.0))
                    if abs(p_obl) > 1e-6 or abs(p_ean) > 1e-6:
                        rp, rv = _apply_tilt(rp, rv, p_obl, p_ean)

                # Star inherits parent BARYCENTER tilt
                if (_inherit_star_tilt and par_s and par_s.get("is_barycenter")):
                    p_obl = safe_float(par_s.get("obliquity_deg", 0.0))
                    p_ean = safe_float(par_s.get("eq_asc_deg",    0.0))
                    if abs(p_obl) > 1e-6 or abs(p_ean) > 1e-6:
                        rp, rv = _apply_tilt(rp, rv, p_obl, p_ean)

                # Non-star aligns to parent STAR equatorial plane
                if (_align_to_star_equator and par_s and par_s.get("is_star")
                        and not obj.get("is_star")):
                    star_obl = safe_float(par_s.get("obliquity_deg", 0.0))
                    star_ean = safe_float(par_s.get("eq_asc_deg",    0.0))
                    if _inherit_star_tilt:
                        grandpar = parsed_objects.get(par_s.get("parent_decl", ""))
                        if grandpar and grandpar.get("is_barycenter"):
                            star_obl = safe_float(grandpar.get("obliquity_deg", star_obl))
                            star_ean = safe_float(grandpar.get("eq_asc_deg",    star_ean))
                    if abs(star_obl) > 1e-6 or abs(star_ean) > 1e-6:
                        rp, rv = _apply_tilt(rp, rv, star_obl, star_ean)
                        log_debug(f"Equator-align '{obj['name']}' → star '{par_s['name']}' "
                                  f"obl={star_obl:.3f} ean={star_ean:.3f}", "EQUATOR")

                result = ([pp[i]+rp[i] for i in range(3)], [pv[i]+rv[i] for i in range(3)])
        _cache[decl] = result
        return result

    # Pass 2: emit entities
    _stellar_validation_log: list = []
    _cloud_validation_log:   list = []
    _status("Building Universe Sandbox entities")
    next_id = max((o["id"] for o in parsed_objects.values()), default=0) + 1

    def _will_emit_entity(obj: dict) -> bool:
        if obj.get("is_barycenter"):
            return False
        obj_type = obj.get("decl_type","").strip().lower()
        if obj_type == "asteroid": return False
        if obj_type == "moon"        and not export_moons:         return False
        if obj_type == "dwarfmoon"   and not export_dwarf_moons:   return False
        if obj_type == "dwarfplanet" and not export_dwarf_planets:  return False
        if obj_type == "comet"       and not export_comets:         return False
        return True

    surface_zip_path = None
    surface_zip_payload = None
    surface_atlas_indices = {}
    import globals_compat as _gc
    if getattr(_gc, "GENERATE_SURFACE_DATA", True):
        surface_bodies = [
            obj for obj in parsed_objects.values()
            if _will_emit_entity(obj) and should_generate_surface(obj)
        ]
        if surface_bodies:
            import io
            surface_zip_path = f"simulation-{sim_name}-surface.zip"
            surface_zip_bytes = io.BytesIO()
            _status("Generating surface data")
            write_surface_archive(surface_bodies, surface_zip_bytes, sim_name)
            surface_zip_payload = surface_zip_bytes.getvalue()
            n_surface_for_atlas = min(len(surface_bodies), 256)
            for idx, solid_obj in enumerate(surface_bodies[:256]):
                surface_atlas_indices[id(solid_obj)] = body_atlas_tiles(idx, n_surface_for_atlas)[0]
            for solid_obj in surface_bodies[256:]:
                log_debug(
                    f"WARNING: '{solid_obj['name']}' exceeds atlas capacity, no surface",
                    "SURFACE_WARN")

    # Barycenters are consumed by flatten_barycenters — never emitted as entities.
    # US simulates the wobble physically from positions/velocities alone.
    for decl, obj in list(parsed_objects.items()):
        if not obj["is_barycenter"]: continue
        # Skip — already consumed
        continue

    for decl, obj in list(parsed_objects.items()):
        if not _will_emit_entity(obj): continue
        obj_type = obj.get("decl_type","").strip().lower()
        atlas_idx = surface_atlas_indices.get(id(obj))

        abs_pos_eci, abs_vel_eci = get_abs_state(decl)
        us_pos = eci_to_us(abs_pos_eci); us_vel = eci_to_us(abs_vel_eci)
        pdecl   = obj["parent_decl"]
        par_obj = parsed_objects.get(pdecl) if pdecl else None

        # flatten_barycenters already re-parented all bary children to real bodies.
        # No barycenter parents remain at this point.
        parent_id = par_obj["id"] if par_obj else -1

        obj_dtype = obj.get("decl_type","").strip().lower()
        if obj["is_star"]:
            category = "star"
        elif obj_dtype in ("moon","dwarfmoon"):
            category = "moon"
        elif obj_dtype in ("dwarfplanet",):
            category = "planet"
        else:
            category = "moon" if (par_obj and not par_obj.get("is_star")) else "planet"

        root_star_decl = _find_root_star(decl, parsed_objects)
        if root_star_decl is None and obj["is_star"]:
            root_star_decl = decl
        root_star_id = parsed_objects[root_star_decl]["id"] if root_star_decl and root_star_decl in parsed_objects else -1
        relative_to_id = root_star_id if root_star_id != -1 else obj["id"]

        if obj["is_star"]:
            est_temp = obj["teff"]
        else:
            star_decl = _find_root_star(decl, parsed_objects)
            raw       = obj["raw_data"]
            albedo    = safe_float(raw.get("AlbedoBond", 0.3))
            atm_raw   = raw.get("Atmosphere", {})
            gh        = safe_float(atm_raw.get("Greenhouse",0.0)) if isinstance(atm_raw,dict) else 0.0
            if star_decl:
                star_pos_eci = _cache.get(star_decl, ([0.0]*3,[0.0]*3))[0]
                dist = math.sqrt(sum((abs_pos_eci[i]-star_pos_eci[i])**2 for i in range(3))) or 1.0
                est_temp = estimate_eq_temp(dist, parsed_objects[star_decl]["lum_watts"], albedo, gh)
            else:
                est_temp = _ARCHETYPE_DEFAULT_TEMPS.get(obj["archetype"], 280.0)
            # Override with exact SE source temperature when available (req 13)
            src_t = obj.get("source_temp_k")
            if src_t is not None and src_t > 0:
                est_temp = src_t

        # Distance for gas giant palette
        star_decl2 = _find_root_star(decl, parsed_objects)
        if star_decl2:
            sp = _cache.get(star_decl2,([0.0]*3,[0.0]*3))[0]
            dist_au_gg = math.sqrt(sum((abs_pos_eci[i]-sp[i])**2 for i in range(3))) / AU_TO_METERS
            star_teff_gg = parsed_objects[star_decl2]["teff"]
        else:
            dist_au_gg = 5.0; star_teff_gg = 5800.0

        (entity, quat_str, rot_axis_str, ang_vel_str, obliquity_deg, eq_asc_node_deg) = build_ubox_entity(
            obj["id"], obj["name"], category, obj["archetype"],
            obj["mass_kg"], obj["radius_m"], us_pos, us_vel, parent_id,
            obj["is_star"],
            relative_to_id=relative_to_id,
            teff=obj["teff"],
            lum_watts=obj["lum_watts"],
            rot_period_h=obj["rot_period_h"],
            obliquity_deg=obj["obliquity_deg"],
            eq_asc_node_deg=obj["eq_asc_deg"],
            atm_info=obj["atm_info"],
            has_ocean=obj["has_ocean"],
            use_water=obj["use_water"],
            mag_field=obj["mag_field"],
            mag_pole_angle=obj["mag_pole"],
            sea_level=obj["sea_level"],
            surface_preset=obj["surface_preset"],
            est_temp=est_temp,
            has_life=obj["has_life"],
            has_exotic_life=obj.get("has_exotic_life",False),
            has_organic_life=obj.get("has_organic_life",False),
            has_aerial_life=obj.get("has_aerial_life",False),
            diffmap=obj["diffmap"],
            se_class=obj["raw_data"].get("Class",""),
            dist_au=dist_au_gg,
            star_teff=star_teff_gg,
            atlas_index=atlas_idx,
            obj_data=obj.get("raw_data",{}),
        )

        raw      = obj.get("raw_data", {}); atm = raw.get("Atmosphere", {})
        obj_type = obj.get("decl_type","").lower()
        raw_class= raw.get("Class","").upper()

        # Special-case star subtypes
        if obj.get("is_star"):
            if obj_type == "blackhole":
                entity["Category"] = "Black Hole"
                entity["Components"] = [c for c in entity.get("Components",[])
                                         if c["$type"] != "CompositionComponent"]
                if not any(c["$type"]=="BlackHole" for c in entity["Components"]):
                    entity["Components"].append({"$type":"BlackHole","Color":"RGBA(0.000,0.000,0.000,1.000)"})
                for c in entity["Components"]:
                    if c["$type"] == "HeatComponent":
                        c["SurfaceTemperature"] = 2.7; c["StartingTemperature"] = 2.7
                    if c["$type"] == "Celestial": c["Luminosity"] = 0
                    if c["$type"] == "AppearanceComponent":
                        c["Tint"] = "RGBA(0,0,0,1)"
                        for key in ("Colors","originalColors","customColors"):
                            if "Planet" in c: c["Planet"][key] = []
                        if "Planet" in c:
                            c["Planet"].update({"ShowAtmosphere":False,"UseWater":False,"UseIce":False,"UseVegetation":False})
            elif obj_type in ("neutronstar","whitedwarf"):
                entity["Category"] = "star"
                entity["Components"] = [c for c in entity.get("Components",[])
                                         if c["$type"] != "CompositionComponent"]
                if obj_type == "neutronstar":
                    for c in entity["Components"]:
                        if c["$type"] == "Celestial": c["MagneticField"] = 1e8
                for c in entity.get("Components",[]):
                    if c["$type"] == "AppearanceComponent" and "Planet" in c:
                        c["Planet"].update({"Colors":[],"originalColors":[],"customColors":[],
                                            "ShowAtmosphere":False,"UseWater":False,"UseIce":False,"UseVegetation":False})
            else:
                entity["Category"] = "star"
                is_bd_label = any(bd in raw_class for bd in [" L"," T"," Y","BROWN DWARF","BROWN"])
                for c in entity.get("Components",[]):
                    if c["$type"] == "AppearanceComponent":
                        if is_bd_label:
                            c["Tint"] = "RGBA(0.600, 0.100, 0.900, 1.000)"
                            c["LightColor"] = "RGBA(0.500, 0.100, 0.800, 1.000)"
                        if "Planet" in c:
                            c["Planet"].update({"Colors":[],"originalColors":[],"customColors":[],
                                                "ShowAtmosphere":False,"UseWater":False,"UseIce":False,"UseVegetation":False})
                    if is_bd_label and c["$type"] == "HeatComponent":
                        t_s = c.get("StartingTemperature",1000)
                        if t_s > 2500:
                            c["StartingTemperature"] = c["SurfaceTemperature"] = random.uniform(800,1800)
        else:
            t_surf = 288.0
            for c in entity.get("Components",[]):
                if c["$type"] == "HeatComponent":
                    t_surf = c.get("StartingTemperature", 288.0)
                if c["$type"] == "AppearanceComponent":
                    kw, ov = _infer_composition_keywords(
                        obj["id"], obj["archetype"], obj["atm_info"],
                        obj["has_life"], obj["surface_preset"], raw, obj["has_ocean"])

                    def get_rgba(name, default_rgb):
                        rgb = CHEMICAL_COLOR_MAP.get(name, SURFACE_FEATURE_COLOR_MAP.get(name, default_rgb))
                        return f"RGBA({rgb[0]/255:.3f}, {rgb[1]/255:.3f}, {rgb[2]/255:.3f}, 1.000)"

                    primary_c = get_rgba(kw[0] if kw else 'barren_rock', (130,130,130))
                    is_gas    = obj.get("archetype","") in ("gas_giant","ice_giant")
                    rng       = random.Random(obj["id"])

                    if is_gas and "GasGiant" in c:
                        corrected_banding = calculate_banding_offsets_proper(
                            obj.get("obliquity_deg",0.0), obj.get("eq_asc_deg",0.0), None)
                        c["GasGiant"]["BandingOffsets"] = corrected_banding
                        atm_hue_val = obj.get("atm_info",{}).get("hue")        if obj.get("atm_info") else None
                        atm_sat_val = obj.get("atm_info",{}).get("saturation") if obj.get("atm_info") else None
                        raw_class_a = raw.get("Class","")
                        is_bd_a, _, _ = detect_brown_dwarf_type(raw_class_a, obj.get("teff",0), raw)
                        fresh = list(_generate_gas_giant_palette_from_preset(
                            obj.get("surface_preset",""), obj.get("mass_kg",0), obj.get("teff",0),
                            atm_hue_val, atm_sat_val,
                            has_life=obj.get("has_life",False),
                            has_aerial_life=obj.get("has_aerial_life",False),
                            has_organic_life=obj.get("has_organic_life",False),
                            has_exotic_life=obj.get("has_exotic_life",False),
                            dist_au=dist_au_gg, star_teff=star_teff_gg,
                            is_brown_dwarf=is_bd_a, raw_class=raw_class_a))
                        for key in ("Colors","UserChangedColors","originalColors","customColors"):
                            c["GasGiant"][key] = fresh if key != "UserChangedColors" else True
                    elif not is_gas and "Planet" in c:
                        rot_h = float(raw.get("RotationPeriod",10.0)) or 10.0
                        speed = min(3.0, max(0.2, 10.0/abs(rot_h)))
                        c["Planet"]["cloudSpeedAtEquatorA"] = round(rng.uniform(-25,-5)*speed,2)
                        c["Planet"]["cloudSpeedAtEquatorB"] = round(rng.uniform(-20,-2)*speed,2)
                        c["Planet"]["bandRotationA"] = round(rng.uniform(-1.5,1.5)*speed,2)
                        c["Planet"]["bandRotationB"] = round(rng.uniform(-1.5,1.5)*speed,2)
                        c["Planet"]["CloudOpacity"]  = round(rng.uniform(0.6,1.0),2)
                        c["Planet"]["Colors"][0] = c["Planet"]["originalColors"][0] = \
                            c["Planet"]["customColors"][0] = primary_c
                        if 'water' in ov:
                            wc = get_rgba(ov['water'],(0,17,47))
                            c["Planet"]["WaterColor"] = c["Planet"]["customWaterColor"] = \
                                c["Planet"]["originalWaterColor"] = wc
                        if 'vegetation' in ov:
                            vc = get_rgba(ov['vegetation'],(25,76,20))
                            c["Planet"]["VegetationColor"] = c["Planet"]["customVegetationColor"] = \
                                c["Planet"]["originalVegetationColor"] = vc

            if isinstance(atm, dict):
                press = float(atm.get("Pressure",0)); gh_p = float(atm.get("Greenhouse",0))
                req_atm_mass = 0
                if press > 0:
                    r_m = obj.get("radius_m",1); m_kg = obj.get("mass_kg",1)
                    g   = GRAVITATIONAL_CONSTANT * m_kg / (r_m**2)
                    req_atm_mass = (press * 101325.0 * 4.0 * math.pi * r_m**2) / g
                for c in entity.get("Components",[]):
                    if c["$type"] == "Celestial":
                        if req_atm_mass > 0: c["AtmosphereMass"] = req_atm_mass
                        t_eq = max(32.0, t_surf - gh_p)
                        c["EmissivityIR"] = (max(0.005, min((t_eq/t_surf)**4, 1.0))
                                             if t_surf > t_eq and t_surf > 10 else 1.0)
                    if c["$type"] == "CompositionComponent" and req_atm_mass > 0:
                        depots   = c.get("depots",{})
                        gas_keys = ["Nitrogen","Oxygen","Carbon Dioxide","Methane",
                                    "Argon","Helium","Hydrogen","Sulfur Dioxide","Ammonia"]
                        gas_mass = sum(depots[k]["Mass"] for k in gas_keys if k in depots)
                        freeze_pts = {"Water":273,"Sulfur Dioxide":201,"Ammonia":195,
                                      "Carbon Dioxide":195,"Methane":90,"Argon":84,
                                      "Nitrogen":63,"Oxygen":54}
                        is_giant = obj.get("archetype","") in ("gas_giant","ice_giant")
                        safe_gas = "Hydrogen" if is_giant else ("Helium" if t_surf<65 else "Nitrogen")
                        if safe_gas not in depots:
                            depots[safe_gas] = {"Mass":0,"LockSurfaceTracking":False}
                        if is_giant and "Composition" in atm:
                            se_comp = atm.get("Composition",{}); total_p = sum(float(v) for v in se_comp.values())
                            cmap = {"H2":"Hydrogen","He":"Helium","CH4":"Methane","NH3":"Ammonia",
                                    "H2O":"Water","CO2":"Carbon Dioxide","N2":"Nitrogen",
                                    "O2":"Oxygen","Ar":"Argon","SO2":"Sulfur Dioxide"}
                            if total_p > 0 and gas_mass > 0:
                                for k in gas_keys:
                                    depots.pop(k, None)
                                # Re-create safe_gas entry after the pop cleared it
                                depots.setdefault(safe_gas, {"Mass":0,"LockSurfaceTracking":False})
                                for se_k, pct_str in se_comp.items():
                                    un = cmap.get(se_k)
                                    if un:
                                        ms = (float(pct_str)/total_p) * gas_mass
                                        if t_surf < freeze_pts.get(un,0) and un != safe_gas:
                                            depots[safe_gas]["Mass"] += ms
                                        else:
                                            depots.setdefault(un,{"Mass":0,"LockSurfaceTracking":False})["Mass"] += ms
                        elif gas_mass > 0:
                            scale = (req_atm_mass/gas_mass) if not is_giant else 1.0
                            for k in list(gas_keys):
                                if k in depots:
                                    sm2 = depots[k]["Mass"] * scale
                                    if t_surf < freeze_pts.get(k,0) and k != safe_gas:
                                        depots[safe_gas]["Mass"] += sm2; del depots[k]
                                    else:
                                        depots[k]["Mass"] = sm2
                        elif not is_giant:
                            depots[safe_gas]["Mass"] += req_atm_mass

        apply_system_age(entity, raw, f"{obj_type} {raw_class}")

        # Req 12: hot/torrid/lava planets emit thermal light
        _is_hot = (
            obj.get("archetype") == "lava"
            or (est_temp is not None and est_temp > 700)
            or any(k in obj.get("surface_preset","").lower()
                   for k in ("hot","torrid","lava","volcanic","ferria"))
        )
        if _is_hot and not obj.get("is_star"):
            for _c in entity.get("Components",[]):
                if _c.get("$type") == "HeatComponent":
                    _c["EmitsLight"] = True
                    if est_temp and est_temp > 0:
                        _c["SurfaceTemperature"]  = float(est_temp)
                        _c["StartingTemperature"] = float(est_temp)
                    break

        ubox_sim["Entities"].append(entity)

        # Planetary ring particles
        ring_data = obj["raw_data"].get("Rings")
        if isinstance(ring_data, dict) and export_rings:
            particles = build_ring_particles(
                obj["id"], obj["name"], us_pos, us_vel, obj["mass_kg"],
                ring_data, next_id,
                obliquity_deg=obj["obliquity_deg"], eq_asc_node_deg=obj["eq_asc_deg"],
            )
            _status(f"Generating rings for {obj['name']}")
            orig = len(particles)
            particles = apply_limit_filter(particles, planetary_ring_input, "planetary_rings")
            log_debug(f"Rings '{obj['name']}': {orig} gen, {len(particles)} kept", "RING_PARTICLES")
            ubox_sim["Entities"].extend(particles)
            next_id += len(particles)
            print(f"  Rings  {len(particles)}/{orig} for '{obj['name']}'")

    # Belt asteroid ring particles
    remaining_asteroids = {}
    for decl in surviving_rings:
        obj = parsed_objects.get(decl)
        if not obj: continue
        pn = obj.get("original_parent_name","Unknown")
        remaining_asteroids.setdefault(pn, []).append((decl, obj))

    for parent_name, ast_list in remaining_asteroids.items():
        for decl, aster in ast_list:
            ap_eci, av_eci = get_abs_state(decl)
            ap_us = eci_to_us(ap_eci); av_us = eci_to_us(av_eci)
            rp = build_asteroid_ring_particle(
                aster["id"], aster["name"], parent_name,
                aster["radius_m"], aster["mass_kg"], ap_us, av_us)
            pid = next((p["id"] for p in parsed_objects.values()
                        if p["name"]==parent_name and not p.get("is_barycenter")), -1)
            if pid != -1: rp["Parent"] = pid
            ubox_sim["Entities"].append(rp)

    # Write .ubox archive
    manifest, surface_zip_id = build_manifest(
        sim_name=sim_name,
        sim_id=sim_id,
        info_id=info_id,
        ui_state_id=ui_id,
        sim_path=sfn,
        info_path=ifn,
        ui_state_path=ufn,
        now=now,
        surface_zip_path=surface_zip_path,
    )
    validate_manifest(manifest, surface_zip_path, sim_name, surface_zip_payload)
    info = {"Header":{"BaseType":"WorkshopItem","AssetType":"JSON","TypeName":"info.json",
                       "BuildRevision":"46923","LastModifiedUTC":now},
            "Name":sim_name,"Description":"Imported from Space Engine.","Flags":"None",
            "Tags":[{"$v":"Simulations"}]}
    ui_state = {"Header":{"BaseType":"UIState","AssetType":"JSON","TypeName":"ui-state.json",
                           "BuildRevision":"46923","LastModifiedUTC":now}}

    _status("Writing simulation archive")
    with zipfile.ZipFile(output_file, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(sfn, json.dumps(ubox_sim, indent=4))
        zf.writestr("manifest.json", json.dumps(manifest, indent=4))
        zf.writestr(ifn, json.dumps(info,     indent=4))
        zf.writestr(ufn, json.dumps(ui_state, indent=4))
        if surface_zip_path and surface_zip_payload is not None:
            zf.writestr(surface_zip_path, surface_zip_payload)
    _status("Finalizing conversion")
    print(f"Success  {len(ubox_sim['Entities'])} objects → {output_file}")

# ─── US → SE back-export ─────────────────────────────────────────────────────

def _infer_se_class_from_depots(depots, is_star):
    if is_star: return "Star","star"
    total = sum(d.get("Mass",0) for d in depots.values()) or 1
    frac  = {k: v.get("Mass",0)/total for k,v in depots.items()}
    h=frac.get("Hydrogen",0); he=frac.get("Helium",0); w=frac.get("Water",0)
    if h+he > 0.5: return ("IceGiant","ice_giant") if w > 0.10 else ("GasGiant","gas_giant")
    if w > 0.35: return "Ocean","ocean"
    if w > 0.15: return "Terra","ocean"
    if frac.get("Silicate",0)+frac.get("Iron",0) > 0.8: return "Terra","rocky"
    return "Selena","rocky"


def _depots_to_se_composition(depots, archetype):
    total  = sum(d.get("Mass",0) for d in depots.values()) or 1
    frac   = {k: v.get("Mass",0)/total*100 for k,v in depots.items()}
    result = {}
    if archetype in ("gas_giant","ice_giant","star"):
        if frac.get("Hydrogen",0) > 0: result["H"]   = frac["Hydrogen"]
        if frac.get("Helium",  0) > 0: result["He"]  = frac["Helium"]
        if frac.get("Water",   0) > 5: result["Ice"] = frac["Water"]
    else:
        if frac.get("Silicate",0) > 0: result["Rock"] = frac["Silicate"]
        if frac.get("Iron",    0) > 0: result["Fe"]   = frac["Iron"]
        if frac.get("Water",   0) > 5: result["Ice"]  = frac["Water"]
    return result


def export_se_sc(entities, output_file, ring_map=None):
    if not entities: return
    if ring_map is None: ring_map = {}
    lines = []
    for e in entities:
        is_star = e.get("IsStar", False)
        t = "Star" if is_star else "Planet"
        depots = {}
        for comp in e.get("Components",[]):
            if comp.get("$type") == "CompositionComponent":
                depots = comp.get("depots",{})
        se_class, archetype = _infer_se_class_from_depots(depots, is_star)
        se_comp             = _depots_to_se_composition(depots, archetype)
        lines += [f'{t}\t"{e["Name"]}"', "{"]
        if e.get("ParentName"): lines.append(f'\tParentBody\t"{e["ParentName"]}"')
        if not is_star: lines.append(f'\tClass\t\t"{se_class}"')
        lines += [f"\tMass\t\t{e['Mass']/EARTH_MASS_KG}", f"\tRadius\t\t{e['Radius']/1_000.0}"]
        if is_star:
            lines += [f"\tLuminosity\t{e.get('Luminosity',1.0)}", f"\tTeff\t\t{e.get('Teff',5800.0)}"]
        rp = e.get("RotationPeriod",0.0)
        if rp:
            lines += [f"\tRotationPeriod\t{rp}",f"\tObliquity\t{e.get('Obliquity',0.0)}",
                      f"\tEqAscendNode\t{e.get('EqAscendNode',0.0)}"]
        if se_comp:
            lines += ["\n\tComposition","\t{"]
            for mol,pct in se_comp.items(): lines.append(f"\t\t{mol:<8}{pct:.2f}")
            lines.append("\t}")
        ring = ring_map.get(e["Name"])
        if ring:
            lines += ["\n\tRings","\t{"]
            for k,v in ring.items():
                lines.append(f"\t\t{k:<20}{v:.6g}" if isinstance(v,float) else f"\t\t{k:<20}{v}")
            lines.append("\t}")
        orb = e.get("Orbit",{})
        if orb and orb.get("sma",0) > 0:
            lines += ["\n\tOrbit","\t{",'\t\tRefPlane\t"Equator"',
                      f"\t\tSemiMajorAxis\t{orb['sma']}",f"\t\tEccentricity\t{orb['ecc']}",
                      f"\t\tInclination\t{orb['inc']}",f"\t\tAscendingNode\t{orb['asc']}",
                      f"\t\tArgOfPericenter\t{orb['arg']}",f"\t\tMeanAnomaly\t{orb['mean']}","\t}"]
        lines.append("}\n")
    with open(output_file,"w",encoding="utf-8") as f:
        f.write("\n".join(lines))


def process_ubox_data(sim_data):
    ents_list = sim_data.get("Entities",[])
    if not ents_list: return
    sn   = sim_data.get("Name","Exported System")
    safe = re.sub(r'[\\/*?:"<>|]',"",sn).strip()
    ring_particle_map = {}; body_list = []
    for ent in ents_list:
        name = ent.get("Name","")
        if "@" in name and "Ring Particle" in name:
            ring_particle_map.setdefault(ent.get("Parent",-1),[]).append(ent)
        else: body_list.append(ent)
    ents = {}
    for ent in body_list:
        eid = ent.get("Id",-1)
        rp_s,ob,ea = rotation_axis_to_se(ent.get("AngularVelocity","0;0;0"), ent.get("RotationAxis","0;-1;0"))
        raw_pos = parse_vec3(ent.get("Position","0;0;0"))
        raw_vel = parse_vec3(ent.get("Velocity","0;0;0"))
        ents[eid] = {"Name":ent.get("Name",f"Object_{eid}"),
                     "Mass":safe_float(ent.get("Mass",0.0)),"Radius":safe_float(ent.get("Radius",0.0)),
                     "PositionUS":raw_pos,"Position":us_to_eci(raw_pos),"Velocity":us_to_eci(raw_vel),
                     "ParentId":ent.get("Parent",-1),"IsStar":False,
                     "Teff":5800.0,"Luminosity":1.0,
                     "RotationPeriod":rp_s,"Obliquity":ob,"EqAscendNode":ea,
                     "Orbit":{},"Components":ent.get("Components",[])}
        for comp in ent.get("Components",[]):
            if comp.get("$type") in ("HeatComponent","Celestial"):
                if int(comp.get("StarType",0)) > 0: ents[eid]["IsStar"] = True
                ents[eid]["Teff"]       = safe_float(comp.get("SurfaceTemperatureOverride",5800.0))
                ents[eid]["Luminosity"] = safe_float(comp.get("Luminosity",3.828e26)) / 3.828e26
    ring_map = {}
    for pid, particles in ring_particle_map.items():
        if pid in ents:
            rd = particles_to_se_rings(particles, ents[pid]["PositionUS"])
            if rd: ring_map[ents[pid]["Name"]] = rd
    export_list = []
    for eid, ent in ents.items():
        pid = ent["ParentId"]
        if pid != -1 and pid in ents:
            ent["ParentName"] = ents[pid]["Name"]
            ent["Orbit"] = calc_orbital_elements(
                ent["Position"],ent["Velocity"],ents[pid]["Position"],ents[pid]["Velocity"],ents[pid]["Mass"])
        export_list.append(ent)
    stars   = [e for e in export_list if e.get("IsStar")]
    planets = [e for e in export_list if not e.get("IsStar")]
    if stars:
        sf = f"{safe} Star.sc"; export_se_sc(stars, sf, ring_map)
        print(f"Success  {len(stars)} stars → {sf}")
    if planets:
        pf = f"{safe} Planet.sc"; export_se_sc(planets, pf, ring_map)
        print(f"Success  {len(planets)} planets → {pf}")


def convert_ubox_json_to_se(f):
    with open(f,"r",encoding="utf-8") as fh:
        try: process_ubox_data(json.load(fh))
        except json.JSONDecodeError: print(f"Error  {f} is not valid JSON.")

def convert_ubox_zip_to_se(f):
    with zipfile.ZipFile(f,"r") as z:
        for name in z.namelist():
            if name.startswith("simulation") and name.endswith(".json") \
               and "info" not in name and "ui" not in name:
                process_ubox_data(json.loads(z.open(name).read().decode("utf-8")))
                return
