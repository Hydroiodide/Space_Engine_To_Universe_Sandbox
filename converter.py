"""converter.py — SE → US translation logic and US → SE back-export."""

import json, re, os, math, uuid, zipfile, base64, random
import colorsys
from io import BytesIO

import numpy as np

import constants as _const
from constants import (
    log_debug, safe_float, _now_utc, CONVERTER_VERSION, write_conversion_log,
    GRAVITATIONAL_CONSTANT, EARTH_MASS_KG, EARTH_RADIUS_M,
    AU_TO_METERS, AU_TO_KM, GYR_TO_SECONDS,
    VALID_SE_TYPES, _ARCHETYPE_DEFAULT_TEMPS,
    SOLAR_RADIUS_M, US_STAR_TYPE_MAIN_SEQUENCE, US_STAR_TYPE_NEUTRON,
    parse_se_surface_preset, WATER_COLORS, VEGETATION_COLORS, ICE_SNOW_COLORS,
)
from surface_generator import (
    ATLAS_H, ATLAS_W, atlas_layout, body_atlas_tiles,
    should_generate_surface, write_surface_archive,
)

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
    _calc_atmosphere_mass, compute_atmosphere_mass_and_pressure,
    normalize_se_atmosphere, combine_cloud_coverage, _cloud_blocks,
    SE_TO_US_DEPOT, SE_OCEAN_TO_US_DEPOT, active_ocean_depot_names,
    enforce_atmosphere_depot_consistency,
    enforce_static_surface_volatile_safety,
    pressure_from_atmosphere_mass,
    classify_stellar_object, get_star_color_from_se, analyse_cloud_layers,
    apply_source_flags,
    reset_heightmap_selection_history,
)
from constants import detect_brown_dwarf_type
from constants import se_bool


def mat_identity():
    return [[1.0, 0.0, 0.0],
            [0.0, 1.0, 0.0],
            [0.0, 0.0, 1.0]]


def mat_mul(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)]
            for i in range(3)]


def mat_vec_mul(matrix, vector):
    return [sum(matrix[i][j] * vector[j] for j in range(3)) for i in range(3)]


def rot_x(deg):
    angle = math.radians(safe_float(deg))
    c, s = math.cos(angle), math.sin(angle)
    return [[1.0, 0.0, 0.0], [0.0, c, -s], [0.0, s, c]]


def rot_z(deg):
    angle = math.radians(safe_float(deg))
    c, s = math.cos(angle), math.sin(angle)
    return [[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]]


def equator_frame_from_obliquity(obliquity_deg, eq_asc_node_deg):
    """Match the converter's historical tilt convention: Rz(EAN) * Rx(obliquity)."""
    return mat_mul(rot_z(eq_asc_node_deg), rot_x(obliquity_deg))


def apply_frame_to_state(pos, vel, frame):
    return mat_vec_mul(frame, pos), mat_vec_mul(frame, vel)


def _normalized(vector):
    mag = math.sqrt(sum(component * component for component in vector))
    return [component / mag for component in vector] if mag > 1e-15 else [0.0, 0.0, 1.0]


def orbit_normal_from_state(position, velocity):
    return _normalized([
        position[1] * velocity[2] - position[2] * velocity[1],
        position[2] * velocity[0] - position[0] * velocity[2],
        position[0] * velocity[1] - position[1] * velocity[0],
    ])


def angle_between_vectors_deg(a, b):
    a_n, b_n = _normalized(a), _normalized(b)
    dot = max(-1.0, min(1.0, sum(a_n[i] * b_n[i] for i in range(3))))
    return math.degrees(math.acos(dot))


def apply_startup_simulation_defaults(sim_json: dict) -> None:
    """Force the safe release startup state after all template/default merges."""
    import globals_compat as runtime

    start_paused = bool(getattr(runtime, "START_PAUSED", _const.START_PAUSED))
    realtime = bool(getattr(
        runtime, "START_SIMULATION_SPEED_REALTIME",
        _const.START_SIMULATION_SPEED_REALTIME,
    ))
    time_step = max(1e-9, safe_float(getattr(
        runtime, "DEFAULT_TIME_STEP_PER_REAL_SEC",
        _const.DEFAULT_TIME_STEP_PER_REAL_SEC,
    ), 1.0))
    disable_auto_speed = bool(getattr(
        runtime, "DISABLE_AUTOSPEED_ON_EXPORT",
        _const.DISABLE_AUTOSPEED_ON_EXPORT,
    ))

    sim_json["Pause"] = start_paused
    if realtime:
        sim_json["TargetTimeStepPerRealSec"] = time_step
        sim_json["MaximalTimeStepPerRealSec"] = time_step
        sim_json["TimeStep"] = time_step
        sim_json["SimulationSpeed"] = time_step
    sim_json["TimePassed"] = 0.0
    auto_speed = sim_json.setdefault("AutoSpeed", {})
    if not isinstance(auto_speed, dict):
        auto_speed = {}
        sim_json["AutoSpeed"] = auto_speed
    if disable_auto_speed:
        auto_speed["Enabled"] = False
    else:
        auto_speed["Enabled"] = True

    log_debug(
        f"[startup] Pause={sim_json.get('Pause')} "
        f"TargetTimeStepPerRealSec={sim_json.get('TargetTimeStepPerRealSec')} "
        f"MaximalTimeStepPerRealSec={sim_json.get('MaximalTimeStepPerRealSec')} "
        f"AutoSpeed.Enabled={auto_speed.get('Enabled')}",
        "STARTUP",
    )


def spin_axis_from_frame(frame):
    return _normalized(mat_vec_mul(frame, [0.0, 0.0, 1.0]))


def effective_tilt_from_spin_frame(frame):
    """Represent a composed spin axis using the builder's existing two angles."""
    x, y, z = spin_axis_from_frame(frame)
    obliquity = math.degrees(math.acos(max(-1.0, min(1.0, z))))
    eq_asc_node = math.degrees(math.atan2(x, -y)) % 360.0 if obliquity > 1e-12 else 0.0
    return obliquity, eq_asc_node


def get_orbit_reference_frame(obj_decl, obj, parent_decl, parent_obj,
                              parsed_objects, flags):
    """Choose the absolute frame in which a child's orbital elements are read."""
    if not parent_decl or not parent_obj:
        return mat_identity()

    parent_orbit = parent_obj.get("_abs_orbit_frame", mat_identity())
    parent_spin = parent_obj.get("_abs_spin_frame")
    if parent_spin is None:
        parent_spin = mat_mul(
            parent_orbit,
            equator_frame_from_obliquity(
                parent_obj.get("obliquity_deg", 0.0), parent_obj.get("eq_asc_deg", 0.0)
            ),
        )

    ref_plane = str((obj.get("orbit") or {}).get("RefPlane", "")).strip().lower()
    if ((flags.get("inherit_moon_tilt", True) or ref_plane == "equator")
            and not parent_obj.get("is_star")
            and not parent_obj.get("is_barycenter")):
        return parent_spin

    if (flags.get("align_to_star_equator", False)
            and parent_obj.get("is_star")
            and not obj.get("is_star")):
        return parent_spin

    if (flags.get("inherit_star_tilt", False)
            and obj.get("is_star")
            and parent_obj.get("is_barycenter")):
        return parent_spin

    return parent_orbit


def _orbit_reference_mode(obj, parent_obj, flags):
    if not parent_obj:
        return "root"
    ref_plane = str((obj.get("orbit") or {}).get("RefPlane", "")).strip().lower()
    if ((flags.get("inherit_moon_tilt", True) or ref_plane == "equator")
            and not parent_obj.get("is_star")
            and not parent_obj.get("is_barycenter")):
        return "parent_abs_equator"
    if (flags.get("align_to_star_equator", False)
            and parent_obj.get("is_star") and not obj.get("is_star")):
        return "star_equator"
    if (flags.get("inherit_star_tilt", False)
            and obj.get("is_star") and parent_obj.get("is_barycenter")):
        return "barycenter_equator"
    return "parent_orbit"


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

def _check_stellar_rotation(name: str, radius_m: float, mass_kg: float,
                            rot_period_h: float, source_field: str) -> None:
    """Warn if stellar rotation implies an equatorial speed > 50% of breakup."""
    if radius_m <= 0 or mass_kg <= 0 or rot_period_h == 0.0:
        return
    omega = 2.0 * math.pi / abs(rot_period_h * 3600.0)
    equator_speed = omega * radius_m
    breakup_speed = math.sqrt(GRAVITATIONAL_CONSTANT * mass_kg / radius_m)
    if equator_speed > 0.5 * breakup_speed:
        log_debug(
            f"STELLAR ROTATION WARNING '{name}': "
            f"period={rot_period_h:.4f}h "
            f"equator_speed={equator_speed/1000:.1f}km/s "
            f"breakup_speed={breakup_speed/1000:.1f}km/s "
            f"source='{source_field}'",
            "ROT_WARN"
        )


def _fmt_vec3(values) -> str:
    """Universe Sandbox vec3 string."""
    return f"{values[0]:.6f};{values[1]:.6f};{values[2]:.6f}"


def _fmt_quat_identity() -> str:
    """Universe Sandbox/Unity identity quaternion string: x;y;z;w."""
    return "0.000000;0.000000;0.000000;1.000000"


def _vec_len(values) -> float:
    return math.sqrt(sum(float(x) * float(x) for x in values))


def _period_days_from_orbit(orbit: dict | None,
                            child_obj: dict | None = None,
                            bary_obj: dict | None = None) -> float:
    """Read an SE orbit period as days with barycenter-context unit inference."""
    orbit = orbit or {}
    if "PeriodDays" in orbit:
        return safe_float(orbit["PeriodDays"])
    if "PeriodYears" in orbit:
        return safe_float(orbit["PeriodYears"]) * 365.25
    if "Period" in orbit:
        period = safe_float(orbit["Period"])
        child_type = (child_obj or {}).get("decl_type", "").strip().lower()
        if child_type in ("moon", "dwarfmoon") or (bary_obj or {}).get("_plain_period_days"):
            return period
        return period * 365.25
    return 0.0


def _orbit_sma_m(orbit: dict | None, assume_au_for_plain: bool = True) -> float:
    """Read an SE semimajor axis as meters."""
    orbit = orbit or {}
    if "SemiMajorAxisKm" in orbit:
        return safe_float(orbit["SemiMajorAxisKm"]) * 1_000.0
    if "SemiMajorAxisAU" in orbit:
        return safe_float(orbit["SemiMajorAxisAU"]) * AU_TO_METERS
    if "SemiMajorAxisAu" in orbit:
        return safe_float(orbit["SemiMajorAxisAu"]) * AU_TO_METERS
    if "SemiMajorAxis" in orbit:
        value = safe_float(orbit["SemiMajorAxis"])
        return value * (AU_TO_METERS if assume_au_for_plain else 1_000.0)
    return 0.0


def _direct_bary_children(bary_decl, parsed_objects) -> list[str]:
    """Return direct children of a barycenter, including nested barycenters."""
    return [
        decl for decl, obj in parsed_objects.items()
        if obj.get("parent_decl") == bary_decl
    ]


def _subtree_mass_kg(decl, parsed_objects) -> float:
    """
    Effective mass for a barycentric participant.

    Non-barycenters count their own mass plus all descendants. Barycenters count
    recursive real descendants and only fall back to placeholder mass if empty.
    """
    obj = parsed_objects.get(decl, {})
    children = [
        child_decl for child_decl, child_obj in parsed_objects.items()
        if child_obj.get("parent_decl") == decl
    ]
    child_mass = sum(_subtree_mass_kg(child_decl, parsed_objects) for child_decl in children)
    own_mass = safe_float(obj.get("mass_kg", 0.0))
    if obj.get("is_barycenter"):
        return child_mass if child_mass > 0 else own_mass
    return own_mass + child_mass


def _orbit_sma_for_bary_child_m(child_obj, bary_obj, total_mass_kg) -> float:
    """Read a direct barycenter child's semimajor axis as meters."""
    orbit = child_obj.get("_orig_orbit", child_obj.get("orbit", {})) or {}
    if "SemiMajorAxisKm" in orbit:
        return safe_float(orbit["SemiMajorAxisKm"]) * 1_000.0
    if "SemiMajorAxisAU" in orbit:
        return safe_float(orbit["SemiMajorAxisAU"]) * AU_TO_METERS
    if "SemiMajorAxisAu" in orbit:
        return safe_float(orbit["SemiMajorAxisAu"]) * AU_TO_METERS
    if "SemiMajorAxis" not in orbit:
        return 0.0

    value = safe_float(orbit["SemiMajorAxis"])
    period_days = _period_days_from_orbit(orbit, child_obj, bary_obj)
    if period_days > 0 and total_mass_kg > 0:
        period_years = period_days / 365.25
        mass_solar = total_mass_kg / 1.98847e30
        a_au_kepler = (period_years**2 * mass_solar) ** (1.0 / 3.0)
        if a_au_kepler > 0 and abs(value - a_au_kepler) / a_au_kepler < 0.10:
            return value * AU_TO_METERS

    child_type = child_obj.get("decl_type", "").strip().lower()
    if child_type in ("moon", "dwarfmoon") or bary_obj.get("_plain_period_days"):
        return value * 1_000.0
    if value < 1000:
        return value * AU_TO_METERS
    return value * 1_000.0


def _compute_barycentric_pair_state_vectors(child_a, child_b, parsed_objects,
                                            orbit_template: dict | None = None) -> dict:
    """
    Build barycentric state vectors for any two direct barycenter participants.

    SE stores each star's barycentric semimajor axis. US needs absolute free-body
    state vectors, so we generate the relative A-B orbit and split it by mass.
    """
    obj_a = parsed_objects[child_a]
    obj_b = parsed_objects[child_b]
    bary_decl = obj_a.get("parent_decl") or obj_b.get("parent_decl")
    bary_obj = parsed_objects.get(bary_decl, {})
    orbit_a = obj_a.get("_orig_orbit", obj_a.get("orbit", {})) or {}
    orbit_b = obj_b.get("_orig_orbit", obj_b.get("orbit", {})) or {}
    template = orbit_template or orbit_b or orbit_a

    mass_a = _subtree_mass_kg(child_a, parsed_objects)
    mass_b = _subtree_mass_kg(child_b, parsed_objects)
    mass_total = mass_a + mass_b
    if mass_a <= 0 or mass_b <= 0 or mass_total <= 0:
        raise ValueError(
            f"barycentric pair '{obj_a.get('name')}' + '{obj_b.get('name')}': invalid masses"
        )

    a_a_m = _orbit_sma_for_bary_child_m(obj_a, bary_obj, mass_total)
    a_b_m = _orbit_sma_for_bary_child_m(obj_b, bary_obj, mass_total)
    a_rel_m = a_a_m + a_b_m
    if a_rel_m <= 0 or not math.isfinite(a_rel_m):
        raise ValueError(
            f"barycentric pair '{obj_a.get('name')}' + '{obj_b.get('name')}': missing relative semimajor axis"
        )

    ecc  = max(0.0, min(0.9999, safe_float(template.get("Eccentricity", 0.0))))
    inc  = safe_float(template.get("Inclination", 0.0))
    asc  = safe_float(template.get("AscendingNode", 0.0))
    arg  = safe_float(template.get("ArgOfPericenter", 0.0))
    mean = safe_float(template.get("MeanAnomaly", 0.0))

    r_rel, v_rel = orbital_elements_to_state_vectors(
        a_rel_m, ecc, inc, asc, arg, mean, mass_total
    )
    r_rel = [0.0 if not math.isfinite(x) else x for x in r_rel]
    v_rel = [0.0 if not math.isfinite(x) else x for x in v_rel]

    mu = GRAVITATIONAL_CONSTANT * mass_total
    r_ab = _vec_len(r_rel)
    v_relative = _vec_len(v_rel)
    if r_ab <= 0 or not math.isfinite(r_ab):
        raise ValueError(
            f"barycentric pair '{obj_a.get('name')}' + '{obj_b.get('name')}': invalid separation"
        )
    vis_viva_term = mu * ((2.0 / r_ab) - (1.0 / a_rel_m))
    v_expected = math.sqrt(max(0.0, vis_viva_term)) if math.isfinite(vis_viva_term) else 0.0
    v_escape = math.sqrt(2.0 * mu / r_ab)
    rescaled_velocity = (
        v_relative <= 0.0
        or not math.isfinite(v_relative)
        or v_relative >= v_escape
    )
    if rescaled_velocity:
        if v_relative > 0.0 and math.isfinite(v_relative):
            direction = [x / v_relative for x in v_rel]
        else:
            # Fallback direction for pathological zero-vector output.
            ref = [0.0, 0.0, 1.0]
            if abs(r_rel[0]) < 1e-9 and abs(r_rel[1]) < 1e-9:
                ref = [0.0, 1.0, 0.0]
            tangent = [
                r_rel[1] * ref[2] - r_rel[2] * ref[1],
                r_rel[2] * ref[0] - r_rel[0] * ref[2],
                r_rel[0] * ref[1] - r_rel[1] * ref[0],
            ]
            tangent_len = _vec_len(tangent)
            direction = [x / tangent_len for x in tangent] if tangent_len > 0 else [1.0, 0.0, 0.0]
        v_rel = [direction[i] * v_expected for i in range(3)]
        v_relative = _vec_len(v_rel)

    log_debug(
        f"BINARY_VELOCITY pair='{obj_a.get('name')} + {obj_b.get('name')}' "
        f"r_AB={r_ab:.6e}m "
        f"v_relative={v_relative:.6e}m/s "
        f"v_expected={v_expected:.6e}m/s "
        f"v_escape={v_escape:.6e}m/s "
        f"rescaled={rescaled_velocity}",
        "BINARY_DBG"
    )
    if v_relative >= v_escape:
        raise ValueError(
            f"barycentric pair '{obj_a.get('name')}' + '{obj_b.get('name')}' invalid: "
            f"relative velocity {v_relative:.6f}m/s >= escape velocity {v_escape:.6f}m/s"
        )

    # A is the primary/more massive star. B is the companion.
    r_a = [-(mass_b / mass_total) * x for x in r_rel]
    r_b = [ +(mass_a / mass_total) * x for x in r_rel]
    v_a = [-(mass_b / mass_total) * x for x in v_rel]
    v_b = [ +(mass_a / mass_total) * x for x in v_rel]

    return {
        "mass_a": mass_a,
        "mass_b": mass_b,
        "mass_total": mass_total,
        "a_a_m": a_a_m,
        "a_b_m": a_b_m,
        "a_rel_m": a_rel_m,
        "ecc": ecc,
        "period_days": _period_days_from_orbit(template, obj_b, bary_obj),
        "r_rel": r_rel,
        "v_rel": v_rel,
        "r_a": r_a,
        "r_b": r_b,
        "v_a": v_a,
        "v_b": v_b,
    }


def _compute_binary_pair_state_vectors(child_a: dict, child_b: dict,
                                       orbit_template: dict | None = None) -> dict:
    """Build barycentric state vectors for a two-star SE barycenter."""
    orbit_a = child_a.get("_orig_orbit", child_a.get("orbit", {})) or {}
    orbit_b = child_b.get("_orig_orbit", child_b.get("orbit", {})) or {}
    template = orbit_template or orbit_b or orbit_a

    mass_a = safe_float(child_a.get("mass_kg", 0.0))
    mass_b = safe_float(child_b.get("mass_kg", 0.0))
    mass_total = mass_a + mass_b
    if mass_a <= 0 or mass_b <= 0 or mass_total <= 0:
        raise ValueError(
            f"binary '{child_a.get('name')}' + '{child_b.get('name')}': invalid masses"
        )

    a_a_m = _orbit_sma_m(orbit_a, assume_au_for_plain=True)
    a_b_m = _orbit_sma_m(orbit_b, assume_au_for_plain=True)
    a_rel_m = a_a_m + a_b_m
    if a_rel_m <= 0 or not math.isfinite(a_rel_m):
        raise ValueError(
            f"binary '{child_a.get('name')}' + '{child_b.get('name')}': missing relative semimajor axis"
        )

    ecc  = max(0.0, min(0.9999, safe_float(template.get("Eccentricity", 0.0))))
    inc  = safe_float(template.get("Inclination", 0.0))
    asc  = safe_float(template.get("AscendingNode", 0.0))
    arg  = safe_float(template.get("ArgOfPericenter", 0.0))
    mean = safe_float(template.get("MeanAnomaly", 0.0))

    r_rel, v_rel = orbital_elements_to_state_vectors(
        a_rel_m, ecc, inc, asc, arg, mean, mass_total
    )
    r_rel = [0.0 if not math.isfinite(x) else x for x in r_rel]
    v_rel = [0.0 if not math.isfinite(x) else x for x in v_rel]

    mu = GRAVITATIONAL_CONSTANT * mass_total
    r_ab = _vec_len(r_rel)
    v_relative = _vec_len(v_rel)
    if r_ab <= 0 or not math.isfinite(r_ab):
        raise ValueError(
            f"binary '{child_a.get('name')}' + '{child_b.get('name')}': invalid separation"
        )
    vis_viva_term = mu * ((2.0 / r_ab) - (1.0 / a_rel_m))
    v_expected = math.sqrt(max(0.0, vis_viva_term)) if math.isfinite(vis_viva_term) else 0.0
    v_escape = math.sqrt(2.0 * mu / r_ab)
    rescaled_velocity = (
        v_relative <= 0.0
        or not math.isfinite(v_relative)
        or v_relative >= v_escape
    )
    if rescaled_velocity:
        if v_relative > 0.0 and math.isfinite(v_relative):
            direction = [x / v_relative for x in v_rel]
        else:
            ref = [0.0, 0.0, 1.0]
            if abs(r_rel[0]) < 1e-9 and abs(r_rel[1]) < 1e-9:
                ref = [0.0, 1.0, 0.0]
            tangent = [
                r_rel[1] * ref[2] - r_rel[2] * ref[1],
                r_rel[2] * ref[0] - r_rel[0] * ref[2],
                r_rel[0] * ref[1] - r_rel[1] * ref[0],
            ]
            tangent_len = _vec_len(tangent)
            direction = [x / tangent_len for x in tangent] if tangent_len > 0 else [1.0, 0.0, 0.0]
        v_rel = [direction[i] * v_expected for i in range(3)]
        v_relative = _vec_len(v_rel)

    log_debug(
        f"BINARY_VELOCITY pair='{child_a.get('name')} + {child_b.get('name')}' "
        f"r_AB={r_ab:.6e}m "
        f"v_relative={v_relative:.6e}m/s "
        f"v_expected={v_expected:.6e}m/s "
        f"v_escape={v_escape:.6e}m/s "
        f"rescaled={rescaled_velocity}",
        "BINARY_DBG"
    )
    if v_relative <= 0.0 or v_relative >= v_escape:
        raise ValueError(
            f"binary '{child_a.get('name')}' + '{child_b.get('name')}' invalid: "
            f"relative velocity {v_relative:.6f}m/s, escape velocity {v_escape:.6f}m/s"
        )

    r_a = [-(mass_b / mass_total) * x for x in r_rel]
    r_b = [ +(mass_a / mass_total) * x for x in r_rel]
    v_a = [-(mass_b / mass_total) * x for x in v_rel]
    v_b = [ +(mass_a / mass_total) * x for x in v_rel]

    return {
        "mass_a": mass_a,
        "mass_b": mass_b,
        "mass_total": mass_total,
        "a_a_m": a_a_m,
        "a_b_m": a_b_m,
        "a_rel_m": a_rel_m,
        "ecc": ecc,
        "period_days": _period_days_from_orbit(template, child_b, None),
        "r_rel": r_rel,
        "v_rel": v_rel,
        "r_a": r_a,
        "r_b": r_b,
        "v_a": v_a,
        "v_b": v_b,
        "r_ab": r_ab,
        "v_relative": v_relative,
        "v_expected": v_expected,
        "v_escape": v_escape,
        "rescaled_velocity": rescaled_velocity,
    }

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
    Convert SE barycenter systems to US-compatible state vectors.

    Multi-child barycenters are kept as suspended US BarycenterComponent
    markers, with each direct participant assigned a barycentric state-vector
    offset. This applies to stars, planets, gas giants, and nested barycenters.

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
        children = _direct_bary_children(bary_decl, parsed_objects)

        if not children:
            bary_obj["_consumed"] = True
            continue

        if len(children) == 1:
            child_decl = children[0]
            child_obj = parsed_objects[child_decl]
            if not child_obj.get("is_barycenter") and bary_orbit:
                child_obj["orbit"] = dict(bary_orbit)
            child_obj["parent_decl"] = bary_obj.get("parent_decl")
            bary_obj["_consumed"] = True
            continue

        bary_parent_decl = bary_obj.get("parent_decl")
        bary_parent = parsed_objects.get(bary_parent_decl, {})
        bary_obj["_plain_period_days"] = bool(
            bary_parent_decl and not bary_parent.get("is_star") and not bary_parent.get("is_barycenter")
        )

        for child_decl in children:
            parsed_objects[child_decl]["_orig_orbit"] = dict(
                parsed_objects[child_decl].get("orbit", {}))

        effective_masses = {
            child_decl: _subtree_mass_kg(child_decl, parsed_objects)
            for child_decl in children
        }
        total_effective_mass = sum(effective_masses.values())
        if total_effective_mass <= 0:
            bary_obj["_consumed"] = True
            log_debug(
                f"Bary flatten '{bary_obj['name']}': skipped, no effective child mass",
                "BARY_WARN"
            )
            continue
        bary_obj["mass_kg"] = total_effective_mass

        _canonical_period_days = None
        if bary_orbit:
            _canonical_period_days = _period_days_from_orbit(bary_orbit, None, bary_obj) or None
        if _canonical_period_days is None:
            for child_decl in children:
                child_orb = parsed_objects[child_decl].get("orbit", {})
                _canonical_period_days = _period_days_from_orbit(
                    child_orb, parsed_objects[child_decl], bary_obj) or None
                if _canonical_period_days is not None:
                    break
        if _canonical_period_days is None:
            _sma_m = _orbit_sma_for_bary_child_m(parsed_objects[children[0]], bary_obj, total_effective_mass)
            if _sma_m > 0:
                _period_s = 2 * math.pi * math.sqrt(
                    _sma_m**3 / (GRAVITATIONAL_CONSTANT * total_effective_mass))
                _canonical_period_days = _period_s / 86_400.0

        if _canonical_period_days and _canonical_period_days > 0:
            for child_decl in children:
                child_obj = parsed_objects[child_decl]
                child_obj["orbit"]["PeriodDays"] = str(_canonical_period_days)
                child_obj["_canonical_period_days"] = _canonical_period_days
                _tidal = str(child_obj.get("raw_data", {}).get("TidalLocked", "false")).strip().lower() in ("true", "1")
                if _tidal and safe_float(child_obj.get("rot_period_h", 0.0)) == 0.0:
                    child_obj["rot_period_h"] = _canonical_period_days * 24.0
            log_debug(
                f"Bary '{bary_obj['name']}': canonical period = {_canonical_period_days:.6f} days "
                f"applied to {len(children)} children", "BARY")

        bary_obj["_emit_us_barycenter"] = True
        bary_obj["_bary_component_children"] = list(children)
        bary_obj["_binary_children"] = list(children[:2])
        star_children = [d for d in children if parsed_objects[d].get("is_star")]
        main_binary_pair = None
        if len(star_children) >= 2:
            primary_decl = max(star_children, key=lambda d: parsed_objects[d].get("mass_kg", 0.0))
            secondary_candidates = [d for d in star_children if d != primary_decl]
            secondary_candidates.sort(
                key=lambda d: _orbit_sma_for_bary_child_m(parsed_objects[d], bary_obj, total_effective_mass)
            )
            main_binary_pair = (primary_decl, secondary_candidates[0])
            bary_obj["_binary_children"] = [primary_decl, secondary_candidates[0]]

        if len(children) == 2:
            state = _compute_barycentric_pair_state_vectors(
                children[0], children[1], parsed_objects,
                parsed_objects[children[1]].get("orbit", {}))
            offsets = {
                children[0]: (state["r_a"], state["v_a"]),
                children[1]: (state["r_b"], state["v_b"]),
            }
            bary_obj["_bary_state_debug"] = state
            period_days = state["period_days"]
            period_years = period_days / 365.25 if period_days > 0 else 0.0
            log_debug(
                f"BARY_EXPORT pair barycenter='{bary_obj['name']}' "
                f"A='{parsed_objects[children[0]]['name']}' id={parsed_objects[children[0]]['id']} "
                f"B='{parsed_objects[children[1]]['name']}' id={parsed_objects[children[1]]['id']} "
                f"M_A={state['mass_a']:.9e}kg M_B={state['mass_b']:.9e}kg "
                f"a_A={state['a_a_m']/AU_TO_METERS:.10f}AU "
                f"a_B={state['a_b_m']/AU_TO_METERS:.10f}AU "
                f"a_rel={state['a_rel_m']/AU_TO_METERS:.10f}AU "
                f"e={state['ecc']:.10f} period_years={period_years:.10f} "
                f"period_days={period_days:.10f}",
                "BINARY_DBG"
            )
        else:
            offsets = {}
            nonzero_positions = 0
            for child_decl in children:
                child_obj = parsed_objects[child_decl]
                orbit = child_obj.get("_orig_orbit", child_obj.get("orbit", {})) or {}
                sma_m = _orbit_sma_for_bary_child_m(child_obj, bary_obj, total_effective_mass)
                ecc  = max(0.0, min(0.9999, safe_float(orbit.get("Eccentricity", 0.0))))
                inc  = safe_float(orbit.get("Inclination", 0.0))
                asc  = safe_float(orbit.get("AscendingNode", 0.0))
                arg  = safe_float(orbit.get("ArgOfPericenter", 0.0))
                mean = safe_float(orbit.get("MeanAnomaly", 0.0))
                if sma_m > 0 and total_effective_mass > 0:
                    rp, rv = orbital_elements_to_state_vectors(
                        sma_m, ecc, inc, asc, arg, mean, total_effective_mass)
                else:
                    rp, rv = [0.0, 0.0, 0.0], [0.0, 0.0, 0.0]
                rp = [0.0 if not math.isfinite(x) else x for x in rp]
                rv = [0.0 if not math.isfinite(x) else x for x in rv]
                offsets[child_decl] = (rp, rv)
                if _vec_len(rp) > 0:
                    nonzero_positions += 1
            log_debug(
                f"BARY WARNING '{bary_obj['name']}': {len(children)} direct children "
                f"approximated with individual barycentric orbits; nonzero_positions={nonzero_positions}",
                "BINARY_WARN"
            )

        if main_binary_pair:
            primary_decl, sec_decl = main_binary_pair
            star_a = parsed_objects[primary_decl]
            star_b = parsed_objects[sec_decl]
            binary_state = _compute_binary_pair_state_vectors(star_a, star_b, star_b.get("orbit", {}))
            offsets[primary_decl] = (binary_state["r_a"], binary_state["v_a"])
            offsets[sec_decl] = (binary_state["r_b"], binary_state["v_b"])
            star_a["_binary_offset_state_eci"] = offsets[primary_decl]
            star_b["_binary_offset_state_eci"] = offsets[sec_decl]
            star_a["_binary_bary_decl"] = bary_decl
            star_b["_binary_bary_decl"] = bary_decl
            star_a["_binary_free_body"] = True
            star_b["_binary_free_body"] = True
            bary_obj["_binary_state_debug"] = binary_state
            period_days = binary_state["period_days"]
            period_years = period_days / 365.25 if period_days > 0 else 0.0
            radius_sum = star_a.get("radius_m", 0.0) + star_b.get("radius_m", 0.0)
            distance_ab = _vec_len([
                binary_state["r_b"][i] - binary_state["r_a"][i]
                for i in range(3)
            ])
            if distance_ab <= radius_sum:
                raise ValueError(
                    f"binary '{star_a['name']} + {star_b['name']}' invalid: "
                    f"distance_AB={distance_ab:.6f}m <= radius_A+radius_B={radius_sum:.6f}m"
                )
            log_debug(
                f"BINARY_EXPORT barycenter='{bary_obj['name']}' "
                f"starA='{star_a['name']}' id={star_a['id']} "
                f"starB='{star_b['name']}' id={star_b['id']} "
                f"M_A={binary_state['mass_a']:.9e}kg M_B={binary_state['mass_b']:.9e}kg "
                f"M_total={binary_state['mass_total']:.9e}kg "
                f"a_A={binary_state['a_a_m']/AU_TO_METERS:.10f}AU "
                f"a_B={binary_state['a_b_m']/AU_TO_METERS:.10f}AU "
                f"a_rel={binary_state['a_rel_m']/AU_TO_METERS:.10f}AU "
                f"e={binary_state['ecc']:.10f} "
                f"period_years={period_years:.10f} period_days={period_days:.10f} "
                f"distance_AB={distance_ab:.6e}m "
                f"relative_velocity={binary_state['v_relative']:.6e}m/s "
                f"expected_visviva_velocity={binary_state['v_expected']:.6e}m/s "
                f"escape_velocity={binary_state['v_escape']:.6e}m/s "
                f"rescaled={binary_state['rescaled_velocity']}",
                "BINARY_DBG"
            )

        for child_decl in children:
            child_obj = parsed_objects[child_decl]
            child_obj["parent_decl"] = bary_decl
            child_obj["_bary_offset_state_eci"] = offsets.get(child_decl, ([0.0]*3, [0.0]*3))
            child_obj["_bary_bary_decl"] = bary_decl
            child_obj["_bary_free_body"] = True
            if main_binary_pair and child_decl in main_binary_pair:
                child_obj["_binary_offset_state_eci"] = child_obj["_bary_offset_state_eci"]
                child_obj["_binary_bary_decl"] = bary_decl
                child_obj["_binary_free_body"] = True

        log_debug(
            f"Bary flatten '{bary_obj['name']}': {len(children)} participant(s) exported "
            f"as barycentric free bodies; effective_mass={total_effective_mass:.9e}kg",
            "BARY"
        )
        bary_obj["_consumed"] = True
        continue



def _find_root_star(decl, parsed_objects):
    def _descendant_star(start_decl, seen):
        if start_decl in seen:
            return None
        seen.add(start_decl)
        obj = parsed_objects.get(start_decl)
        if not obj:
            return None
        if obj.get("is_star"):
            return start_decl
        if obj.get("is_barycenter"):
            for child_decl in obj.get("_bary_component_children", obj.get("_binary_children", [])):
                found = _descendant_star(child_decl, seen)
                if found:
                    return found
        for child_decl, child_obj in parsed_objects.items():
            if child_obj.get("parent_decl") == start_decl:
                found = _descendant_star(child_decl, seen)
                if found:
                    return found
        return None

    visited = set()
    cur = parsed_objects.get(decl, {}).get("parent_decl")
    while cur and cur not in visited:
        visited.add(cur)
        obj = parsed_objects.get(cur)
        if obj is None: break
        if obj["is_star"]: return cur
        if obj.get("is_barycenter") and (obj.get("_bary_component_children") or obj.get("_binary_children")):
            found = _descendant_star(cur, set())
            if found:
                return found
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


def _parse_numeric_vector(value, expected: int, label: str) -> list[float]:
    if isinstance(value, (list, tuple)):
        values = list(value)
    else:
        text = str(value or "").strip().strip("()[]")
        values = [part.strip() for part in re.split(r"[;,]", text) if part.strip()]
    if len(values) != expected:
        raise ValueError(f"{label}: expected {expected} values, found {len(values)}")
    result = [float(item) for item in values]
    if not all(math.isfinite(item) for item in result):
        raise ValueError(f"{label}: contains NaN or Infinity")
    return result


def _has_component(entity: dict, component_type: str) -> bool:
    return any(c.get("$type") == component_type for c in entity.get("Components", []))


def _component(entity: dict, component_type: str) -> dict:
    return next((c for c in entity.get("Components", []) if c.get("$type") == component_type), {})


def _find_nonfinite(value, path="simulation") -> list[str]:
    bad = []
    if isinstance(value, float) and not math.isfinite(value):
        bad.append(path)
    elif isinstance(value, dict):
        for key, child in value.items():
            bad.extend(_find_nonfinite(child, f"{path}.{key}"))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            bad.extend(_find_nonfinite(child, f"{path}[{index}]"))
    return bad


def _has_civilization_signal(raw: dict) -> bool:
    if not isinstance(raw, dict):
        return False
    for key in ("Inhabited", "Civilized", "Civilised", "Artificial", "HasCities",
                "CityLights", "Technosphere"):
        if key in raw and se_bool(raw.get(key)):
            return True
    life = raw.get("Life")
    if isinstance(life, dict):
        return any(key in life and se_bool(life.get(key)) for key in (
            "Civilized", "Civilised", "Intelligent", "Artificial", "Technosphere"
        ))
    return isinstance(life, str) and any(
        token in life.lower() for token in ("civil", "intelligent", "technolog")
    )


def validate_simulation_entities(ubox_sim, surface_zip_payload=None, manifest=None,
                                 source_by_id=None, surface_settings=None) -> dict:
    """Validate release-critical entity, hierarchy, appearance, and surface invariants."""
    source_by_id = source_by_id or {}
    warnings = []
    errors = []
    entities = ubox_sim.get("Entities", []) if isinstance(ubox_sim, dict) else []
    ids = [entity.get("Id") for entity in entities]
    id_set = {entity_id for entity_id in ids if isinstance(entity_id, int)}

    if len(id_set) != len(ids):
        errors.append("entity IDs are missing or duplicated")
    nonfinite = _find_nonfinite(ubox_sim)
    if nonfinite:
        errors.append("non-finite numeric values: " + ", ".join(nonfinite[:8]))

    import globals_compat as runtime_flags
    surface_mode, _generate_surface, attach_surface_grid, active_surface_physics = (
        _surface_runtime_settings(surface_settings or runtime_flags)
    )
    if getattr(runtime_flags, "START_PAUSED", _const.START_PAUSED) and ubox_sim.get("Pause") is not True:
        errors.append("simulation startup must be paused")
    if getattr(runtime_flags, "START_SIMULATION_SPEED_REALTIME", _const.START_SIMULATION_SPEED_REALTIME):
        expected_step = safe_float(getattr(
            runtime_flags, "DEFAULT_TIME_STEP_PER_REAL_SEC",
            _const.DEFAULT_TIME_STEP_PER_REAL_SEC,
        ), 1.0)
        for key in ("TargetTimeStepPerRealSec", "MaximalTimeStepPerRealSec", "TimeStep", "SimulationSpeed"):
            if abs(safe_float(ubox_sim.get(key, 0.0)) - expected_step) > 1e-9:
                errors.append(f"simulation startup {key} must be {expected_step}")
    if safe_float(ubox_sim.get("TimePassed", 0.0)) != 0.0:
        errors.append("new simulation TimePassed must be 0.0")
    auto_speed = ubox_sim.get("AutoSpeed", {})
    if (
        getattr(runtime_flags, "DISABLE_AUTOSPEED_ON_EXPORT", _const.DISABLE_AUTOSPEED_ON_EXPORT)
        and (not isinstance(auto_speed, dict) or auto_speed.get("Enabled") is not False)
    ):
        errors.append("simulation startup AutoSpeed.Enabled must be false")

    atlas_users = 0
    surface_entities = []
    for entity in entities:
        name = str(entity.get("Name", "<unnamed>"))
        entity_id = entity.get("Id")
        source = source_by_id.get(entity_id, {})
        raw = source.get("raw_data", {}) if isinstance(source, dict) else {}
        is_particle = _has_component(entity, "ParticleComponent")
        is_barycenter = _has_component(entity, "BarycenterComponent")
        appearance = _component(entity, "AppearanceComponent")
        planet = appearance.get("Planet", {}) if isinstance(appearance, dict) else {}
        gas = appearance.get("GasGiant", {}) if isinstance(appearance, dict) else {}
        celestial = _component(entity, "Celestial")
        is_star = bool(source.get("is_star") or safe_float(celestial.get("StarType", 0.0)) > 0.0)
        surface_grid = _component(entity, "SurfaceGridComponent")
        composition = _component(entity, "CompositionComponent")
        depots = composition.get("depots", {}) if isinstance(composition, dict) else {}

        if not is_barycenter:
            for key in ("Mass", "Radius"):
                value = entity.get(key)
                if not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                    errors.append(f"{name}: {key} must be finite and > 0")
        elif not entity.get("Suspended"):
            errors.append(f"{name}: barycenter marker must be suspended")

        for key, size in (("Position", 3), ("Velocity", 3), ("Orientation", 4),
                          ("AngularVelocity", 3)):
            try:
                _parse_numeric_vector(entity.get(key), size, f"{name}.{key}")
            except (TypeError, ValueError) as exc:
                errors.append(str(exc))

        for key in ("Parent", "CustomOrbitParentId"):
            ref = entity.get(key, -1)
            if ref not in (-1, 0, None) and ref not in id_set:
                errors.append(f"{name}: {key} references missing id {ref}")
        relative_to = entity.get("RelativeTo", 0)
        if relative_to not in (-1, 0, None) and relative_to not in id_set:
            errors.append(f"{name}: RelativeTo references missing id {relative_to}")

        if source and source.get("decl_type", "").lower() in ("moon", "dwarfmoon"):
            intended_free = bool(source.get("_bary_free_body") or source.get("_binary_free_body"))
            if entity.get("Parent", -1) == -1 and not intended_free:
                errors.append(f"{name}: moon became an unintended root free body")

        atlas_index = surface_grid.get("AtlasIndex", -1) if surface_grid else -1
        if atlas_index >= 0:
            atlas_users += 1
            surface_entities.append((entity, source, atlas_index))
            if surface_zip_payload is None:
                errors.append(f"{name}: AtlasIndex={atlas_index} but no surface archive exists")
            if not 0 <= atlas_index <= 255:
                errors.append(f"{name}: AtlasIndex={atlas_index} is outside 0..255")
        if gas and atlas_index != -1:
            errors.append(f"{name}: gas giant must use AtlasIndex=-1")
        if is_star and atlas_index != -1:
            errors.append(f"{name}: star must not reference the surface atlas")

        if planet:
            if planet.get("CityLightSource") == 0:
                errors.append(f"{name}: CityLightSource=0 is not release-safe")
            city_enabled = bool(planet.get("UseDynamicEmissive"))
            if city_enabled and planet.get("CityLightSource") != 1:
                errors.append(f"{name}: enabled procedural city lights must use CityLightSource=1")
            if city_enabled and source and not source.get("has_life") and not _has_civilization_signal(raw):
                errors.append(f"{name}: city lights are enabled without life or civilization metadata")
            if not city_enabled:
                if appearance.get("EmissiveMapSource"):
                    errors.append(f"{name}: disabled city lights still have EmissiveMapSource")
                if safe_float(planet.get("CityLightsBrightness", 0.0)) != 0.0:
                    errors.append(f"{name}: disabled city lights have nonzero brightness")
                if safe_float(planet.get("CityLightSeed", 0.0)) != 0.0:
                    errors.append(f"{name}: disabled city lights have nonzero seed")
            if raw and se_bool(raw.get("NoOcean", "false")) and planet.get("UseWater"):
                errors.append(f"{name}: NoOcean true but UseWater is enabled")

            texture_fields = (
                appearance.get("ColorMapSource", ""), appearance.get("EmissiveMapSource", ""),
                appearance.get("VegetationMapSource", ""), appearance.get("HeightMapSource", ""),
                appearance.get("HeightMapSource2", ""),
            )
            explicit_earth = name.strip().lower() == "earth" or "earth" in str(source.get("surface_preset", "")).lower()
            if not explicit_earth and any("earth" in str(path).lower() for path in texture_fields if path):
                errors.append(f"{name}: non-Earth procedural body references Earth assets")

            ocean = raw.get("Ocean", {}) if isinstance(raw.get("Ocean"), dict) else {}
            surface = raw.get("Surface", {}) if isinstance(raw.get("Surface"), dict) else {}
            depth_km = max(0.0, safe_float(ocean.get("Depth", 0.0)))
            sea_level = max(0.0, safe_float(surface.get("seaLevel", 0.0)))
            if (
                not se_bool(raw.get("NoOcean", "false"))
                and 0.0 < depth_km < 1.0
                and sea_level <= 0.05
            ):
                if safe_float(appearance.get("HeightMapMix0", 0.0)) > 0.350001:
                    errors.append(f"{name}: lacustrine HeightMapMix0 exceeds 0.35")
                if safe_float(appearance.get("HeightMapMix1", 0.0)) > 0.200001:
                    errors.append(f"{name}: lacustrine HeightMapMix1 exceeds 0.20")
                ocean_comp = ocean.get("Composition", {}) if isinstance(ocean.get("Composition"), dict) else {}
                depot_names = {
                    SE_OCEAN_TO_US_DEPOT.get(str(molecule), str(molecule))
                    for molecule, percent in ocean_comp.items()
                    if safe_float(percent) > 0.0
                }
                if not depot_names:
                    depot_names = {"Water"}
                depot_report = raw.get("_ocean_depot_report", {})
                atmo_report = raw.get("_atmosphere_normalization_report", {})
                atmospheric_masses = atmo_report.get("atmospheric_depot_masses_kg", {})

                # Use per-source provenance when available so that atmospheric H2O
                # (which can be orders of magnitude larger than a lacustrine lake)
                # does not falsely trigger the lacustrine ocean cap.
                source_masses = raw.get("_depot_source_masses_kg", {})
                ocean_source  = source_masses.get("ocean", {})
                atm_source    = source_masses.get("atmosphere", {})

                if ocean_source:
                    # Preferred path: builder recorded per-source masses.
                    ocean_water = max(0.0, safe_float(ocean_source.get("Water", 0.0)))
                    atm_water   = max(0.0, safe_float(atm_source.get("Water", 0.0)))
                else:
                    # Fallback: use ocean depot report us_depot_mass_kg which is the
                    # capped lacustrine target itself, and subtract atmospheric share.
                    total_water_depot = max(
                        0.0,
                        safe_float(depots.get("Water", {}).get("Mass", 0.0)),
                    )
                    atm_water = max(0.0, safe_float(atmospheric_masses.get("Water", 0.0)))
                    ocean_water = max(0.0, total_water_depot - atm_water)

                total_water_depot = max(
                    0.0, safe_float(depots.get("Water", {}).get("Mass", 0.0))
                )
                target_mass  = max(0.0, safe_float(depot_report.get("us_depot_mass_kg", 0.0)))
                mass_cap     = max(0.0, safe_float(entity.get("Mass", 0.0))) * 1e-8
                allowed_mass = min(target_mass * 1.05, mass_cap) if target_mass > 0.0 else mass_cap

                if ocean_water > max(allowed_mass, 1.0):
                    errors.append(
                        f"{name}: ocean/lake Water depot mass {ocean_water:.6g} kg "
                        f"exceeds capped lacustrine target {target_mass:.6g} kg"
                    )
                _lac_export_mode    = depot_report.get("ocean_depot_export_mode", "unknown")
                _requested_ocean_mode = str(getattr(
                    runtime_flags, "OCEAN_DEPOT_EXPORT_MODE",
                    getattr(_const, "OCEAN_DEPOT_EXPORT_MODE", "legacy"),
                ))
                _pass_fail = "PASS" if ocean_water <= max(allowed_mass, 1.0) else "FAIL"
                log_debug(
                    f"[validate-water] Body='{name}' water_mode='lacustrine' "
                    f"requested_ocean_depot_mode='{_requested_ocean_mode}' "
                    f"effective_export_mode='{_lac_export_mode}' "
                    f"depth_km={depth_km:.6g} seaLevel={sea_level:.6g} "
                    f"total_water_depot_mass={total_water_depot:.6g} "
                    f"atmospheric_water_depot_mass={atm_water:.6g} "
                    f"ocean_water_depot_mass={ocean_water:.6g} "
                    f"target_lacustrine_ocean_water={target_mass:.6g} {_pass_fail}",
                    "VALIDATE_WATER",
                )

            cloud_blocks = _cloud_blocks(raw)
            if cloud_blocks and not se_bool(raw.get("NoClouds", "false")):
                expected_cloud_coverage = combine_cloud_coverage(cloud_blocks)
                actual_cloud_coverage   = safe_float(planet.get("CloudCoverage", 0.0))
                strict_cloud_validation = bool(getattr(
                    runtime_flags, "STRICT_CLOUD_COVERAGE_VALIDATION",
                    getattr(_const, "STRICT_CLOUD_COVERAGE_VALIDATION", False),
                ))
                mismatch = abs(actual_cloud_coverage - round(expected_cloud_coverage, 4)) > 1e-6
                if mismatch:
                    _obj_decl = str(raw.get("_decl_type", raw.get("decl_type", ""))).lower()
                    is_star_like = bool(
                        raw.get("_is_star") or raw.get("is_star")
                        or str(raw.get("Type", "")).lower() == "star"
                        or _obj_decl in ("star", "browndwarf", "brown_dwarf",
                                         "blackhole", "neutronstar", "whitedwarf")
                        or detect_brown_dwarf_type(str(raw.get("Class", "")), 0, raw)[0]
                    )
                    has_planet_cloud_fields = bool(
                        planet and (
                            "CloudCoverage" in planet
                            or "CloudOpacity" in planet
                            or "ShowAtmosphereClouds" in planet
                        )
                    )
                    cloud_msg = (
                        f"{name}: CloudCoverage={actual_cloud_coverage:.6g}; "
                        f"expected union coverage {expected_cloud_coverage:.6g}"
                    )
                    if strict_cloud_validation and (not is_star_like) and has_planet_cloud_fields:
                        errors.append(cloud_msg)
                    else:
                        warnings.append(
                            cloud_msg + " (non-fatal: strict cloud validation off"
                            + (" or star/brown-dwarf object)" if is_star_like else ")")
                        )
                        log_debug(
                            f"[validate-cloud-warning] Body='{name}' "
                            f"is_star_like={is_star_like} strict={strict_cloud_validation} "
                            f"has_planet_cloud_fields={has_planet_cloud_fields} "
                            f"actual={actual_cloud_coverage:.6g} expected={expected_cloud_coverage:.6g}",
                            "VALIDATE_CLOUD_WARN",
                        )

            atmo_report = raw.get("_atmosphere_normalization_report", {})
            if atmo_report.get("applied"):
                final_partials = atmo_report.get("final_partials_atm", {})
                caps = atmo_report.get("caps_atm", {})
                for molecule in ("H2O", "SO2", "CO2", "O2"):
                    if molecule in caps and safe_float(final_partials.get(molecule, 0.0)) > safe_float(caps[molecule]) * 1.001:
                        errors.append(f"{name}: normalized {molecule} exceeds its chemistry cap")
                for warning in atmo_report.get("warnings", []):
                    warnings.append(f"{name}: {warning}")
                    log_debug(
                        f"[validate-atmo] Body='{name}' {warning}",
                        "VALIDATE_ATMO_WARN",
                    )

            atmosphere_audit = raw.get("_atmosphere_audit", {})
            if atmosphere_audit:
                mass_error = safe_float(atmosphere_audit.get("mass_error_fraction", 0.0))
                depot_error = atmosphere_audit.get("depot_mass_error_fraction")
                pressure_error = safe_float(atmosphere_audit.get("pressure_error_fraction", 0.0))
                source_pressure_error = safe_float(
                    atmosphere_audit.get("source_pressure_error_fraction", 0.0)
                )
                target = errors if atmosphere_audit.get("strict_consistency", True) else warnings
                if mass_error > 0.05:
                    target.append(f"{name}: celestial atmosphere mass consistency error {mass_error:.1%}")
                if depot_error is not None and safe_float(depot_error) > 0.05:
                    target.append(f"{name}: atmospheric depot mass consistency error {safe_float(depot_error):.1%}")
                if pressure_error > 0.05:
                    target.append(f"{name}: atmosphere mass pressure consistency error {pressure_error:.1%}")
                if source_pressure_error > 0.05:
                    target.append(
                        f"{name}: final pressure differs from source pressure by "
                        f"{source_pressure_error:.1%}"
                    )
                if atmosphere_audit.get("surface_water_depot_applicable", False):
                    water_mass = safe_float(atmosphere_audit.get("surface_water_depot_after_kg", 0.0))
                    water_cap = safe_float(atmosphere_audit.get("surface_water_depot_cap_kg", 0.0))
                    water_locked = bool(atmosphere_audit.get("surface_water_depot_locked", False))
                    if water_mass > water_cap * 1.000001:
                        target.append(
                            f"{name}: static surface Water depot {water_mass:.6g} kg exceeds cap {water_cap:.6g} kg"
                        )
                    if not water_locked:
                        target.append(f"{name}: static surface Water depot is not locked")
                    if atmosphere_audit.get("water_depot_can_rewrite_atmosphere", False):
                        target.append(f"{name}: Water depot can rewrite imported atmosphere")
                volatile_safety = atmosphere_audit.get("volatile_safety", {})
                if volatile_safety.get("static_imported_atmosphere", False):
                    expected_atm_mass = safe_float(
                        volatile_safety.get("expected_atmosphere_mass_kg", 0.0)
                    )
                    _cur_depot_mode = str(volatile_safety.get(
                        "static_depot_mode",
                        getattr(runtime_flags, "STATIC_ATMOSPHERE_DEPOT_MODE", "none"),
                    ))
                    if _cur_depot_mode == "none":
                        # Validate all reactive volatile depots are zero
                        after_masses = volatile_safety.get("volatile_depot_masses_after_kg", {})
                        for depot_name, actual_mass in after_masses.items():
                            if safe_float(actual_mass) > max(1.0, expected_atm_mass * 1e-12):
                                target.append(
                                    f"{name}: depot_mode='none' but {depot_name} has mass "
                                    f"{safe_float(actual_mass):.6g} kg"
                                )
                        # No lock requirement — there are no atmospheric depots
                    elif _cur_depot_mode.endswith("_unlocked"):
                        carrier = volatile_safety.get("carrier_name")
                        if carrier and not volatile_safety.get("gas_depots_locked", True):
                            pass  # correct — carrier should be unlocked
                        elif carrier and volatile_safety.get("gas_depots_locked", False):
                            target.append(
                                f"{name}: depot_mode='{_cur_depot_mode}' but carrier depot is locked"
                            )
                        after_masses = volatile_safety.get("volatile_depot_masses_after_kg", {})
                        for depot_name, actual_mass in after_masses.items():
                            if depot_name == carrier:
                                continue
                            if safe_float(actual_mass) > max(1.0, expected_atm_mass * 1e-12):
                                target.append(
                                    f"{name}: depot_mode='{_cur_depot_mode}' "
                                    f"non-carrier depot {depot_name} has mass {safe_float(actual_mass):.6g} kg"
                                )
                    elif _cur_depot_mode.endswith("_locked"):
                        if not volatile_safety.get("gas_depots_locked", False):
                            target.append(
                                f"{name}: depot_mode='{_cur_depot_mode}' but gas depots are not locked"
                            )
                        # Check non-carrier volatile depots are zero
                        carrier = volatile_safety.get("carrier_name")
                        after_masses = volatile_safety.get("volatile_depot_masses_after_kg", {})
                        for depot_name, actual_mass in after_masses.items():
                            if depot_name == carrier:
                                continue
                            if safe_float(actual_mass) > max(1.0, expected_atm_mass * 1e-12):
                                target.append(
                                    f"{name}: depot_mode='{_cur_depot_mode}' "
                                    f"non-carrier depot {depot_name} has mass {safe_float(actual_mass):.6g} kg"
                                )
                    if volatile_safety.get("ocean_depot_export_mode") == "visual_only":
                        atmospheric_masses = volatile_safety.get("atmospheric_depot_masses_kg", {})
                        after_masses = volatile_safety.get("volatile_depot_masses_after_kg", {})
                        for depot_name, actual_mass in after_masses.items():
                            expected_gas = safe_float(atmospheric_masses.get(depot_name, 0.0))
                            excess = max(0.0, safe_float(actual_mass) - expected_gas)
                            if excess > max(1.0, expected_atm_mass * 1e-12):
                                target.append(
                                    f"{name}: visual-only ocean left active {depot_name} excess "
                                    f"{excess:.6g} kg"
                                )
                    if atmosphere_audit.get("surface_gas_channel_mode", "off") != "off":
                        target.append(f"{name}: static imported atmosphere has surface gas channel enabled")

        raw_class = str(raw.get("Class", "")).strip().lower()
        atmosphere = raw.get("Atmosphere", {}) if isinstance(raw.get("Atmosphere"), dict) else {}
        if raw_class in ("neptune", "uranus", "icegiant", "jupiter", "gasgiant") and atmosphere:
            expected = compute_atmosphere_mass_and_pressure(source, atmosphere, raw.get("Class", ""))
            actual_mass = safe_float(celestial.get("AtmosphereMass", 0.0))
            if expected["pressure_atm"] >= 100.0 and actual_mass < expected["atmosphere_mass_kg"] * 0.5:
                errors.append(
                    f"{name}: giant atmosphere collapsed to {actual_mass:.6g} kg; "
                    f"expected about {expected['atmosphere_mass_kg']:.6g} kg"
                )
            if planet or not gas:
                errors.append(f"{name}: giant class must use GasGiant appearance only")

        if is_particle and surface_grid:
            warnings.append(f"{name}: particle unexpectedly contains SurfaceGridComponent")

    if atlas_users > 256:
        errors.append(f"surface atlas has {atlas_users} users, exceeding 256")
    if not attach_surface_grid and atlas_users:
        errors.append(
            f"surface mode '{surface_mode}' must not attach SurfaceGridComponent atlas "
            f"indices, but {atlas_users} body/bodies reference the atlas"
        )
    if surface_zip_payload is not None:
        required_surface_files = {
            "info", "data.surface", "material0.surface", "material1.surface",
            "material2.surface", "material3.surface",
        }
        expected_surface_bytes = ATLAS_W * ATLAS_H * 4 * 4
        surface_arrays = {}
        try:
            with zipfile.ZipFile(BytesIO(surface_zip_payload), "r") as surface_zip:
                surface_names = set(surface_zip.namelist())
                missing = sorted(required_surface_files - surface_names)
                if missing:
                    errors.append("surface archive is missing: " + ", ".join(missing))
                for surface_name in sorted(required_surface_files - {"info"}):
                    if surface_name not in surface_names:
                        continue
                    info = surface_zip.getinfo(surface_name)
                    if info.file_size != expected_surface_bytes:
                        errors.append(
                            f"surface archive {surface_name} has {info.file_size} bytes; "
                            f"expected {expected_surface_bytes}"
                        )
                        continue
                    array = np.frombuffer(surface_zip.read(surface_name), dtype="<f4")
                    if not np.isfinite(array).all():
                        errors.append(f"surface archive {surface_name} contains NaN or Infinity")
                    surface_arrays[surface_name] = array.reshape((ATLAS_H, ATLAS_W, 4))
        except (OSError, zipfile.BadZipFile, ValueError) as exc:
            errors.append(f"surface archive is invalid: {exc}")

        material0 = surface_arrays.get("material0.surface")
        surface_data = surface_arrays.get("data.surface")
        safe_surface_mode = surface_mode in {
            "none", "preview_only", "liquid_mask_only", "full_us_like",
        }
        for surface_name in (
            "material0.surface", "material1.surface",
            "material2.surface", "material3.surface",
        ):
            array = surface_arrays.get(surface_name)
            if array is None:
                continue
            channel = array[:, :, 1]
            ch_min = float(np.nanmin(channel)) if channel.size else 0.0
            ch_mean = float(np.nanmean(channel)) if channel.size else 0.0
            ch_max = float(np.nanmax(channel)) if channel.size else 0.0
            nonzero = int(np.count_nonzero(np.abs(channel) > 1e-9))
            log_debug(
                f"[surface-channel] mode='{surface_mode}' {surface_name[:-8]}.ch1 "
                f"min={ch_min:.6g} mean={ch_mean:.6g} max={ch_max:.6g} "
                f"nonzero={nonzero}",
                "SURFACE_CHANNEL",
            )
            if safe_surface_mode and (
                ch_min != 0.0 or ch_max != 0.0 or nonzero != 0
            ):
                errors.append(
                    "Surface physics validation failed: active material channel is "
                    f"nonzero in safe surface mode ({surface_name} ch1)."
                )
        if surface_mode == "liquid_mask_only":
            zero_checks = []
            if surface_data is not None:
                zero_checks.append(("data.surface", surface_data))
            if material0 is not None:
                zero_checks.extend((
                    ("material0.surface ch0", material0[:, :, 0]),
                    ("material0.surface ch1", material0[:, :, 1]),
                ))
                if not bool(getattr(runtime_flags, "SAFE_SURFACE_EXPORT_ICE_MASK", True)):
                    zero_checks.append(("material0.surface ch2", material0[:, :, 2]))
            for name in ("material1.surface", "material2.surface", "material3.surface"):
                if surface_arrays.get(name) is not None:
                    zero_checks.append((name, surface_arrays[name]))
            for label, array in zero_checks:
                max_abs = float(np.max(np.abs(array))) if array.size else 0.0
                if max_abs != 0.0:
                    errors.append(
                        f"Liquid / Water Mask Only validation failed: {label} must be zero; "
                        f"max_abs={max_abs:.6g}"
                    )
        if material0 is not None:
            gas_mode = str(getattr(runtime_flags, "SURFACE_GAS_PRESSURE_MODE", "off")).strip().lower()
            static_atmospheres = bool(getattr(runtime_flags, "STATIC_IMPORTED_ATMOSPHERES", True))
            gas_channel = material0[:, :, 1]
            gas_min = float(np.nanmin(gas_channel)) if gas_channel.size else 0.0
            gas_mean = float(np.nanmean(gas_channel)) if gas_channel.size else 0.0
            gas_max = float(np.nanmax(gas_channel)) if gas_channel.size else 0.0
            gas_nonzero = int(np.count_nonzero(np.abs(gas_channel) > 1e-9))
            gas_must_be_zero = surface_mode != "active_legacy"
            gas_pass = not gas_must_be_zero or (
                gas_min == 0.0 and gas_max == 0.0 and gas_nonzero == 0
            )
            log_debug(
                f"[surface-gas-validate] mode='{gas_mode}' static={static_atmospheres} "
                f"ch1_min={gas_min:.6g} ch1_mean={gas_mean:.6g} ch1_max={gas_max:.6g} "
                f"nonzero={gas_nonzero} {'PASS' if gas_pass else 'FAIL'}",
                "SURFACE_GAS",
            )
            if not gas_pass:
                errors.append(
                    "Surface validation failed: material0.surface ch1 is nonzero outside "
                    "Active legacy / dangerous mode. This would let Universe Sandbox rewrite "
                    "imported atmosphere pressure."
                )
        if surface_mode == "full_us_like":
            if active_surface_physics:
                errors.append("Full US-like surface must have ACTIVE_SURFACE_PHYSICS disabled")
            gas_mode = str(getattr(
                runtime_flags, "SURFACE_GAS_PRESSURE_MODE", "off"
            )).strip().lower()
            if bool(getattr(runtime_flags, "STATIC_IMPORTED_ATMOSPHERES", True)) and gas_mode != "off":
                errors.append(
                    "Full US-like surface with static imported atmospheres requires "
                    "Surface Gas Channel Off"
                )
        if material0 is not None and surface_data is not None and surface_mode != "active_legacy":
            if np.any(material0[:, :, 3] > 1e-9) and np.allclose(
                material0[:, :, 3], surface_data[:, :, 1], rtol=1e-6, atol=1e-7
            ):
                errors.append(
                    "surface archive material0.surface ch3 duplicates data.surface ch1 "
                    "albedo; liquid channel must be water mask/depth"
                )
        if material0 is not None and surface_entities:
            body_w, body_h, grid_cols, _grid_rows, _tiles = atlas_layout(atlas_users)
            for entity, source, atlas_index in surface_entities:
                raw = source.get("raw_data", {}) if isinstance(source, dict) else {}
                if not raw:
                    continue
                cell_col = atlas_index % grid_cols
                cell_row = atlas_index // grid_cols
                liquid = material0[
                    cell_row * body_h:(cell_row + 1) * body_h,
                    cell_col * body_w:(cell_col + 1) * body_w,
                    3,
                ]
                liquid_max = float(np.max(liquid)) if liquid.size else 0.0
                liquid_mean = float(np.mean(liquid)) if liquid.size else 0.0
                liquid_nonzero = int(np.count_nonzero(np.abs(liquid) > 1e-9))
                name = entity.get("Name", "<unnamed>")
                log_debug(
                    f"[liquid-channel] Body='{name}' ch3_mean={liquid_mean:.6g} "
                    f"ch3_max={liquid_max:.6g} nonzero={liquid_nonzero} "
                    "source='water mask/depth, not albedo'",
                    "SURFACE_WATER",
                )
                if surface_data is not None:
                    albedo = surface_data[
                        cell_row * body_h:(cell_row + 1) * body_h,
                        cell_col * body_w:(cell_col + 1) * body_w,
                        1,
                    ]
                    if (
                        liquid.size and np.any(liquid > 1e-9)
                        and albedo.shape == liquid.shape and np.allclose(
                        liquid, albedo, rtol=1e-6, atol=1e-7
                        )
                    ):
                        errors.append(
                            f"{name}: material0.surface ch3 duplicates data.surface ch1 albedo; "
                            "liquid channel must be water mask/depth"
                        )
                if se_bool(raw.get("NoOcean", "false")) and liquid_max > 1e-5:
                    errors.append(f"{name}: NoOcean surface has liquid channel max {liquid_max:.6g}")
                ocean_exists = isinstance(raw.get("Ocean"), dict) and bool(raw.get("Ocean"))
                if (
                    surface_mode == "liquid_mask_only" and ocean_exists
                    and not se_bool(raw.get("NoOcean", "false"))
                    and liquid_nonzero == 0
                ):
                    errors.append(
                        f"{name}: Liquid / Water Mask Only mode has no water pixels for an ocean body"
                    )

                ocean = raw.get("Ocean", {}) if isinstance(raw.get("Ocean"), dict) else {}
                surface = raw.get("Surface", {}) if isinstance(raw.get("Surface"), dict) else {}
                depth_km = max(0.0, safe_float(ocean.get("Depth", 0.0)))
                sea_level = max(0.0, safe_float(surface.get("seaLevel", 0.0)))
                if (
                    surface_mode != "liquid_mask_only"
                    and 0.0 < depth_km < 1.0 and sea_level <= 0.05
                ):
                    bump_height_km = max(0.05, abs(safe_float(surface.get("BumpHeight", 0.0))))
                    implied_depth_km = liquid_max * bump_height_km
                    if implied_depth_km > max(depth_km * 1.10, depth_km + 0.01):
                        errors.append(
                            f"{name}: shallow source depth {depth_km:.6g} km produced "
                            f"{implied_depth_km:.6g} km liquid channel"
                        )
    if manifest is not None:
        surface_entry = next((entry for entry in manifest.get("Entries", [])
                              if entry.get("BaseType") == "SurfaceData"), None)
        try:
            validate_manifest(
                manifest,
                surface_entry.get("Path") if surface_entry else None,
                ubox_sim.get("Name", ""),
                surface_zip_payload,
            )
        except ValueError as exc:
            errors.append(str(exc))

    log_debug(
        f"[validate] bodies={len(entities)} atlas_users={atlas_users} "
        f"warnings={len(warnings)} errors={len(errors)}",
        "VALIDATE",
    )
    for warning in warnings:
        log_debug(f"[validate-warning] {warning}", "VALIDATE_WARN")
    if errors:
        for error in errors:
            log_debug(f"[validate-error] {error}", "VALIDATE_ERROR")
        raise ValueError("Simulation validation failed: " + "; ".join(errors[:8]))
    return {"warnings": warnings, "errors": errors, "body_count": len(entities),
            "atlas_users": atlas_users}


def validate_ubox_file(path: str) -> dict:
    """Open and validate an existing generated .ubox archive."""
    with zipfile.ZipFile(path, "r") as zf:
        names = set(zf.namelist())
        if "manifest.json" not in names:
            raise ValueError("archive is missing manifest.json")
        manifest = json.loads(zf.read("manifest.json"))
        sim_names = [name for name in names if name.startswith("simulation-") and name.endswith(".json")
                     and "-info" not in name and "-ui-state" not in name]
        if len(sim_names) != 1:
            raise ValueError(f"archive must contain one simulation JSON, found {len(sim_names)}")
        simulation = json.loads(zf.read(sim_names[0]))
        missing_paths = [entry.get("Path") for entry in manifest.get("Entries", [])
                         if entry.get("Path") and entry.get("Path") not in names]
        if missing_paths:
            raise ValueError("archive is missing manifest file(s): " + ", ".join(missing_paths))
        surface_entry = next((entry for entry in manifest.get("Entries", [])
                              if entry.get("BaseType") == "SurfaceData"), None)
        surface_payload = zf.read(surface_entry["Path"]) if surface_entry and surface_entry.get("Path") in names else None
        validate_manifest(manifest, surface_entry.get("Path") if surface_entry else None,
                          simulation.get("Name", ""), surface_payload)
        summary = (
            json.loads(zf.read("conversion-summary.json"))
            if "conversion-summary.json" in names else {}
        )
        result = validate_simulation_entities(
            simulation, surface_payload, None, None, summary.get("surface")
        )
        if "conversion-summary.json" not in names:
            result["warnings"].append("archive has no conversion-summary.json")
        result.update({"path": os.path.abspath(path), "simulation": simulation.get("Name", ""),
                       "files": len(names), "has_summary": "conversion-summary.json" in names,
                       "warning_count": len(result["warnings"])})
        return result


def _surface_runtime_settings(runtime_flags) -> tuple[str, bool, bool, bool]:
    def _value(name, default):
        if isinstance(runtime_flags, dict):
            aliases = {
                "SURFACE_DATA_MODE": "mode",
                "GENERATE_SURFACE_DATA": "enabled",
                "ATTACH_SURFACE_GRID_COMPONENT": "surface_grid_attached",
                "ACTIVE_SURFACE_PHYSICS": "active_surface_physics",
            }
            return runtime_flags.get(name, runtime_flags.get(aliases.get(name), default))
        return getattr(runtime_flags, name, default)

    mode = str(_value("SURFACE_DATA_MODE", "liquid_mask_only")).strip().lower()
    aliases = {
        "off": "none", "preview": "preview_only", "liquid": "liquid_mask_only",
        "water_mask": "liquid_mask_only", "passive": "full_us_like",
        "passive_attached": "full_us_like", "full": "full_us_like",
        "active": "active_legacy", "legacy": "active_legacy",
    }
    mode = aliases.get(mode, mode)
    if mode not in {
        "none", "preview_only", "liquid_mask_only", "full_us_like", "active_legacy"
    }:
        mode = "liquid_mask_only"
    generate = bool(_value("GENERATE_SURFACE_DATA", True)) and mode != "none"
    attach = bool(_value("ATTACH_SURFACE_GRID_COMPONENT", True))
    active = bool(_value("ACTIVE_SURFACE_PHYSICS", False))
    if mode in {"none", "preview_only"}:
        attach = False
    if mode == "full_us_like":
        active = False
    return mode, generate, attach, active


def convert_to_ubox(se_data, output_file="SE_Import.ubox",
                    belt_asteroid_input="100%",
                    planetary_ring_input="100%",
                    comet_input="100%",
                    export_comets=True,
                    export_moons=True,
                    export_dwarf_moons=True,
                    export_dwarf_planets=True,
                    export_rings=True,
                    status_callback=None,
                    source_name=None):
    def _status(msg):
        if status_callback: status_callback(msg)
        log_debug(msg, "STATUS")

    log_start_index = len(_const.CONVERSION_LOG)
    _const.SYSTEM_AGE_SECONDS = None
    reset_heightmap_selection_history()

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
                elif "Period"      in _orb_tmp: raw_rot_h = safe_float(_orb_tmp["Period"]) * 365.25 * 24.0
            rot_period_h = raw_rot_h
            if is_star and rot_period_h != 0.0:
                _src = "TidalLocked+Period" if is_tidal else "RotationPeriod"
                _check_stellar_rotation(clean_name, radius_m, mass_kg, rot_period_h, _src)
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
                "diffmap":diffmap,"raw_data":obj_data,
                **(obj_data.get("_source_flags", {}) if isinstance(obj_data.get("_source_flags"), dict) else {}),
                "body_type":None,
            }
            current_id += 1

    parsed_count_before_filters = len(parsed_objects)

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

    ring_particles_generated = 0
    ring_particles_kept = 0

    # Memoised absolute state (ECI, meters)
    _cache = {}
    
    import globals_compat as _gc
    _frame_flags = {
        "inherit_moon_tilt": getattr(_gc, "INHERIT_MOON_AXIAL_TILT", True),
        "inherit_star_tilt": getattr(_gc, "INHERIT_STAR_AXIAL_TILT", False),
        "align_to_star_equator": getattr(_gc, "ALIGN_ORBITS_TO_STAR_EQUATOR", False),
    }

    def _store_absolute_frames(obj, orbit_frame, mode):
        obj["_abs_orbit_frame"] = orbit_frame
        obj["_abs_spin_frame"] = mat_mul(
            orbit_frame,
            equator_frame_from_obliquity(
                obj.get("obliquity_deg", 0.0), obj.get("eq_asc_deg", 0.0)
            ),
        )
        obj["_orbit_frame_mode"] = mode
        obj["_orbit_normal_abs"] = spin_axis_from_frame(orbit_frame)
        obj["_spin_axis_abs"] = spin_axis_from_frame(obj["_abs_spin_frame"])
        effective_obl, effective_ean = effective_tilt_from_spin_frame(obj["_abs_spin_frame"])
        obj["_effective_obliquity_deg"] = effective_obl
        obj["_effective_eq_asc_node_deg"] = effective_ean
    
    def get_abs_state(decl, _seen=None, _depth=0):
        if decl in _cache: return _cache[decl]
        if _depth > 64: return [0.0]*3, [0.0]*3
        if _seen is None: _seen = set()
        if decl in _seen: return [0.0]*3, [0.0]*3
        _seen.add(decl)

        obj = parsed_objects.get(decl)
        if obj is None:
            result = ([0.0]*3, [0.0]*3)
        elif obj.get("_binary_offset_state_eci") or obj.get("_bary_offset_state_eci"):
            offset_key = "_binary_offset_state_eci" if obj.get("_binary_offset_state_eci") else "_bary_offset_state_eci"
            bary_key = "_binary_bary_decl" if obj.get("_binary_bary_decl") else "_bary_bary_decl"
            bary_decl = obj.get(bary_key)
            if bary_decl and bary_decl in parsed_objects and bary_decl != decl:
                cp, cv = get_abs_state(bary_decl, _seen, _depth + 1)
            else:
                cp, cv = [0.0]*3, [0.0]*3
            op, ov = obj[offset_key]
            result = ([cp[i] + op[i] for i in range(3)],
                      [cv[i] + ov[i] for i in range(3)])
            # The free-body solver already encoded this orbit's physical plane.
            _store_absolute_frames(obj, mat_identity(), "bary_free_body_preserved")
        else:
            pdecl = obj["parent_decl"]
            if not pdecl or pdecl not in parsed_objects:
                result = ([0.0]*3, [0.0]*3)
                _store_absolute_frames(obj, mat_identity(), "root")
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
                par_s = parsed_objects.get(pdecl)
                reference_frame = get_orbit_reference_frame(
                    decl, obj, pdecl, par_s, parsed_objects, _frame_flags
                )
                frame_mode = _orbit_reference_mode(obj, par_s, _frame_flags)
                rp, rv = apply_frame_to_state(rp, rv, reference_frame)
                _store_absolute_frames(obj, reference_frame, frame_mode)

                orbit_normal = orbit_normal_from_state(rp, rv)
                obj["_orbit_normal_abs"] = orbit_normal
                normal_text = "(" + ",".join(f"{value:.6f}" for value in orbit_normal) + ")"
                log_debug(
                    f"[orbit-frame] Body='{obj.get('name', decl)}' "
                    f"parent='{par_s.get('name', pdecl)}' mode='{frame_mode}' "
                    f"global_orbit_normal={normal_text}",
                    "ORBIT_FRAME",
                )

                if frame_mode == "star_equator":
                    log_debug(
                        f"[orbit-frame] Body='{obj.get('name', decl)}' "
                        f"parent='{par_s.get('name', pdecl)}' mode='star_equator' "
                        f"star_obl={safe_float(par_s.get('obliquity_deg', 0.0)):.3f} "
                        f"star_ean={safe_float(par_s.get('eq_asc_deg', 0.0)):.3f} "
                        f"global_orbit_normal={normal_text}",
                        "ORBIT_FRAME",
                    )
                elif frame_mode == "parent_abs_equator":
                    parent_spin_axis = par_s.get(
                        "_spin_axis_abs", spin_axis_from_frame(par_s["_abs_spin_frame"])
                    )
                    angle_to_equator = angle_between_vectors_deg(orbit_normal, parent_spin_axis)
                    expected_inc = abs(inc) % 360.0
                    if expected_inc > 180.0:
                        expected_inc = 360.0 - expected_inc
                    log_debug(
                        f"[orbit-frame] Body='{obj.get('name', decl)}' "
                        f"parent='{par_s.get('name', pdecl)}' mode='parent_abs_equator' "
                        f"parent_orbit_frame='{par_s.get('_orbit_frame_mode', 'unknown')}' "
                        f"parent_obl={safe_float(par_s.get('obliquity_deg', 0.0)):.3f} "
                        f"parent_ean={safe_float(par_s.get('eq_asc_deg', 0.0)):.3f} "
                        f"moon_global_normal={normal_text} angle_to_parent_equator={angle_to_equator:.6f}",
                        "ORBIT_FRAME",
                    )
                    log_debug(
                        f"[tilt-check] Moon='{obj.get('name', decl)}' "
                        f"parent='{par_s.get('name', pdecl)}' "
                        f"angle(moon_orbit_normal,parent_spin_axis)={angle_to_equator:.6f} "
                        f"expected_from_orbit_inc={expected_inc:.6f}",
                        "TILT_CHECK",
                    )

                result = ([pp[i]+rp[i] for i in range(3)], [pv[i]+rv[i] for i in range(3)])
        if obj is not None and obj.get("is_star"):
            _orb_d = obj.get("orbit", {})
            _sma_log = 0.0
            if "SemiMajorAxis" in _orb_d:
                _sma_log = safe_float(_orb_d["SemiMajorAxis"])
            elif "SemiMajorAxisAU" in _orb_d:
                _sma_log = safe_float(_orb_d["SemiMajorAxisAU"])
            elif "SemiMajorAxisAu" in _orb_d:
                _sma_log = safe_float(_orb_d["SemiMajorAxisAu"])
            elif "SemiMajorAxisKm" in _orb_d:
                _sma_log = safe_float(_orb_d["SemiMajorAxisKm"]) / AU_TO_KM
            _rot_h = obj.get("rot_period_h", 0.0)
            _is_tid = str(obj.get("raw_data", {}).get("TidalLocked", "false")).strip().lower() in ("true", "1")
            _raw_period = safe_float(_orb_d.get("Period", _orb_d.get("PeriodDays", "0")))
            _period_days = _raw_period * 365.25 if "Period" in _orb_d else _raw_period
            _pos_au = [x / AU_TO_METERS for x in result[0]]
            _vel_kms = [x / 1000.0 for x in result[1]]
            _r_au = math.sqrt(sum(x**2 for x in _pos_au))
            _v_kms = math.sqrt(sum(x**2 for x in _vel_kms))
            _pdecl_log = obj.get("parent_decl")
            log_debug(
                f"BINARY_STATE '{obj['name']}' "
                f"parent='{parsed_objects.get(_pdecl_log,{}).get('name','<root>')}' "
                f"raw_Period={_raw_period:.8f} period_days={_period_days:.4f}d "
                f"TidalLocked={_is_tid} rot_period_h={_rot_h:.4f}h ({_rot_h/24:.4f}d) "
                f"SMA={_sma_log:.8f}AU "
                f"bary_r={_r_au:.8f}AU bary_v={_v_kms:.4f}km/s",
                "BINARY_DBG"
            )
        _cache[decl] = result
        return result

    def _normalization_temperature(decl, obj):
        if obj.get("is_star"):
            return obj.get("teff", 5800.0)
        raw = obj.get("raw_data", {})
        source_temp = obj.get("source_temp_k")
        if source_temp is not None and source_temp > 0.0:
            return source_temp
        star_decl = _find_root_star(decl, parsed_objects)
        if star_decl:
            body_pos, _ = get_abs_state(decl)
            star_pos, _ = get_abs_state(star_decl)
            distance = math.sqrt(sum((body_pos[i] - star_pos[i]) ** 2 for i in range(3))) or 1.0
            atmosphere = raw.get("Atmosphere", {}) if isinstance(raw.get("Atmosphere"), dict) else {}
            estimate = estimate_eq_temp(
                distance,
                parsed_objects[star_decl]["lum_watts"],
                safe_float(raw.get("AlbedoBond", 0.3)),
                safe_float(atmosphere.get("Greenhouse", 0.0)),
            )
        else:
            estimate = _ARCHETYPE_DEFAULT_TEMPS.get(obj.get("archetype", "rocky"), 280.0)
        if (
            obj.get("archetype") in ("rocky", "ocean", "terra")
            and (obj.get("has_life") or obj.get("has_ocean"))
            and str(raw.get("Class", "")).strip().lower()
            in ("terra", "aquaria", "ocean", "marine", "panthalassic")
        ):
            estimate = max(285.0, min(305.0, estimate))
        return estimate

    # Normalize chemistry before surface generation and entity construction so
    # pressure, molecular weight, depots, colors, and validation share one input.
    for decl, obj in parsed_objects.items():
        if obj.get("is_star") or obj.get("is_barycenter"):
            continue
        raw = obj.get("raw_data", {})
        atmosphere = raw.get("Atmosphere")
        if not isinstance(atmosphere, dict):
            continue
        normalized, report = normalize_se_atmosphere(
            atmosphere,
            raw.get("_life_info", raw.get("Life")),
            raw.get("Surface", {}),
            raw.get("Ocean", {}),
            raw.get("Class", obj.get("archetype", "")),
            _normalization_temperature(decl, obj),
            {
                "enabled": getattr(_gc, "NORMALIZE_SE_ATMOSPHERE", True),
                "mode": getattr(_gc, "NORMALIZE_SE_ATMOSPHERE_MODE", "stability"),
                "body_name": obj.get("name", decl),
                "no_ocean": se_bool(raw.get("NoOcean", "false")),
            },
        )
        raw["_raw_atmosphere"] = atmosphere
        raw["Atmosphere"] = normalized
        raw["_atmosphere_normalization_report"] = report
        obj["_atmosphere_normalization_report"] = report
        flags = dict(raw.get("_source_flags", {}) or {})
        flags["raw_atmosphere_composition"] = dict(normalized.get("Composition", {}))
        raw["_source_flags"] = flags
        obj["raw_atmosphere_composition"] = dict(normalized.get("Composition", {}))
        obj["atm_info"] = {
            "pressure": safe_float(normalized.get("Pressure", 0.0)),
            "density": safe_float(normalized.get("Density", 0.0)),
            "height": safe_float(normalized.get("Height", 0.0)),
            "comp": dict(normalized.get("Composition", {})),
            "hue": normalized.get("Hue"),
            "saturation": normalized.get("Saturation"),
            "model": normalized.get("Model", ""),
            "opacity": safe_float(normalized.get("Opacity", 1.0)),
        }

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
    surface_bodies = []
    import globals_compat as _gc
    surface_mode, generate_surface, attach_surface_grid, active_surface_physics = (
        _surface_runtime_settings(_gc)
    )
    # Apply static atmosphere surface policy
    _surface_policy = str(getattr(_gc, "STATIC_IMPORTED_ATMOSPHERE_SURFACE_POLICY",
                                  "no_grid")).strip().lower()
    # "no_grid" — do not attach SurfaceGridComponent for any body; archive still generated
    # "passive_grid" — attach but active channels must be zeroed (enforced in surface_generator)
    # "active_legacy" — dangerous, keep legacy behaviour
    if _surface_policy == "no_grid":
        attach_surface_grid = False
    elif _surface_policy == "passive_grid":
        active_surface_physics = False  # passive_grid must never enable active physics
    # active_legacy: leave attach and active as determined by _surface_runtime_settings
    _gc.SURFACE_DATA_MODE = surface_mode
    _gc.GENERATE_SURFACE_DATA = generate_surface
    _gc.ATTACH_SURFACE_GRID_COMPONENT = attach_surface_grid
    _gc.ACTIVE_SURFACE_PHYSICS = active_surface_physics
    log_debug(
        f"[surface-mode] mode='{surface_mode}' generated_archive={generate_surface} "
        f"attach_surface_grid={attach_surface_grid} active_physics={active_surface_physics} "
        f"surface_policy='{_surface_policy}'",
        "SURFACE_MODE",
    )
    if generate_surface:
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
            if attach_surface_grid:
                for idx, solid_obj in enumerate(surface_bodies[:256]):
                    surface_atlas_indices[id(solid_obj)] = body_atlas_tiles(
                        idx, n_surface_for_atlas
                    )[0]
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
        if should_generate_surface(obj) and not attach_surface_grid:
            log_debug(
                f"[surface-mode] Body='{obj.get('name', 'unknown')}' AtlasIndex=-1 "
                "SurfaceGridComponent='disabled' reason='preview-only surface archive'",
                "SURFACE_MODE",
            )

        abs_pos_eci, abs_vel_eci = get_abs_state(decl)
        us_pos = eci_to_us(abs_pos_eci); us_vel = eci_to_us(abs_vel_eci)
        pdecl   = obj["parent_decl"]
        par_obj = parsed_objects.get(pdecl) if pdecl else None

        # flatten_barycenters already re-parented all bary children to real bodies.
        # No barycenter parents remain at this point.
        parent_id = par_obj["id"] if par_obj else -1
        if obj.get("_bary_free_body") or obj.get("_binary_free_body"):
            parent_id = -1

        obj_dtype = obj.get("decl_type","").strip().lower()
        if obj["is_star"]:
            category = "star"
        elif obj_dtype in ("moon","dwarfmoon"):
            category = "moon"
        elif obj_dtype in ("dwarfplanet",):
            category = "planet"
        else:
            category = "moon" if (par_obj and not par_obj.get("is_star") and not par_obj.get("is_barycenter")) else "planet"

        root_star_decl = _find_root_star(decl, parsed_objects)
        if root_star_decl is None and obj["is_star"]:
            root_star_decl = decl
        root_star_id = parsed_objects[root_star_decl]["id"] if root_star_decl and root_star_decl in parsed_objects else -1
        if obj.get("_bary_free_body") or obj.get("_binary_free_body") or obj.get("is_star") or parent_id == -1:
            relative_to_id = 0
        elif par_obj and par_obj.get("_emit_us_barycenter"):
            relative_to_id = par_obj["id"]
        else:
            relative_to_id = root_star_id if root_star_id != -1 else 0

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
            elif (
                obj.get("archetype") in ("rocky", "ocean", "terra")
                and (obj.get("has_life") or obj.get("has_ocean"))
                and str(raw.get("Class", "")).strip().lower() in ("terra", "aquaria", "ocean", "marine", "panthalassic")
            ):
                est_temp = max(285.0, min(305.0, est_temp))

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
            obliquity_deg=obj.get("_effective_obliquity_deg", obj["obliquity_deg"]),
            eq_asc_node_deg=obj.get("_effective_eq_asc_node_deg", obj["eq_asc_deg"]),
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

        if obj.get("_bary_free_body") or obj.get("_binary_free_body"):
            entity["Parent"] = -1
            entity["CustomOrbitParentId"] = -1
            entity["RelativeTo"] = 0
        elif par_obj and par_obj.get("_emit_us_barycenter"):
            entity["Parent"] = par_obj["id"]
            entity["CustomOrbitParentId"] = par_obj["id"]
            entity["RelativeTo"] = par_obj["id"]

        raw      = obj.get("raw_data", {}); atm = raw.get("Atmosphere", {})
        obj_type = obj.get("decl_type","").lower()
        raw_class= raw.get("Class","").upper()

        # Classify using the full priority chain (Class-first)
        if obj.get("is_star"):
            from builder import classify_spaceengine_stellar_body
            _stl = classify_spaceengine_stellar_body(raw)
            _kind = _stl["kind"]

            if _kind == "black_hole" or obj_type == "blackhole":
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
            elif _kind in ("neutron_star", "white_dwarf") or obj_type in ("neutronstar","whitedwarf"):
                entity["Category"] = "star"
                entity["Components"] = [c for c in entity.get("Components",[])
                                         if c["$type"] != "CompositionComponent"]
                if _kind == "neutron_star" or obj_type == "neutronstar":
                    for c in entity["Components"]:
                        if c["$type"] == "Celestial":
                            c["MagneticField"] = 1e8
                            c["StarType"] = 4  # US_STAR_TYPE_NEUTRON
                for c in entity.get("Components",[]):
                    if c["$type"] == "AppearanceComponent" and "Planet" in c:
                        c["Planet"].update({"Colors":[],"originalColors":[],"customColors":[],
                                            "ShowAtmosphere":False,"UseWater":False,"UseIce":False,"UseVegetation":False})
            else:
                entity["Category"] = "star"
                is_bd_label = _kind == "brown_dwarf"
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
                        preset_info = parse_se_surface_preset(obj.get("surface_preset", ""))
                        if preset_info.get("surface_palette"):
                            colors = list(preset_info["surface_palette"])
                            c["Planet"]["Colors"] = colors
                            c["Planet"]["originalColors"] = list(colors)
                            c["Planet"]["customColors"] = list(colors)
                            c["Planet"]["UserChangedColors"] = True
                        else:
                            c["Planet"]["Colors"][0] = c["Planet"]["originalColors"][0] = \
                                c["Planet"]["customColors"][0] = primary_c
                        water_hint = preset_info.get("water_color_hint")
                        plant_color = preset_info.get("plant_color")
                        if plant_color in VEGETATION_COLORS:
                            c["Planet"]["VegetationColor"] = c["Planet"]["customVegetationColor"] = \
                                c["Planet"]["originalVegetationColor"] = VEGETATION_COLORS[plant_color]
                            c["Planet"]["UseVegetation"] = True
                        ice_hint = preset_info.get("ice_color_hint")
                        if ice_hint in ICE_SNOW_COLORS:
                            for key in ("IceColor", "originalIceColor", "customIceColor", "SnowColor", "originalSnowColor", "customSnowColor"):
                                c["Planet"][key] = ICE_SNOW_COLORS[ice_hint]
                        if 'water' in ov:
                            if not c["Planet"].get("WaterColor"):
                                wc = get_rgba(ov['water'],(0,17,47))
                                c["Planet"]["WaterColor"] = c["Planet"]["customWaterColor"] = \
                                    c["Planet"]["originalWaterColor"] = wc
                        if 'vegetation' in ov:
                            vc = get_rgba(ov['vegetation'],(25,76,20))
                            if plant_color not in VEGETATION_COLORS:
                                c["Planet"]["VegetationColor"] = c["Planet"]["customVegetationColor"] = \
                                    c["Planet"]["originalVegetationColor"] = vc

            if isinstance(atm, dict):
                press = float(atm.get("Pressure",0)); gh_p = float(atm.get("Greenhouse",0))
                mass_pressure = compute_atmosphere_mass_and_pressure(obj, atm, raw.get("Class", obj.get("archetype", "")))
                req_atm_mass = mass_pressure["atmosphere_mass_kg"]
                for c in entity.get("Components",[]):
                    if c["$type"] == "Celestial":
                        c["AtmosphereMass"] = req_atm_mass
                        t_eq = max(32.0, t_surf - gh_p)
                        c["EmissivityIR"] = (max(0.005, min((t_eq/t_surf)**4, 1.0))
                                             if t_surf > t_eq and t_surf > 10 else 1.0)

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

        apply_source_flags(entity, obj)

        final_atmo = raw.get("Atmosphere", {}) if isinstance(raw.get("Atmosphere"), dict) else {}
        if final_atmo and not obj.get("is_star"):
            mass_pressure = compute_atmosphere_mass_and_pressure(
                obj, final_atmo, raw.get("Class", obj.get("archetype", ""))
            )
            expected_mass = mass_pressure["atmosphere_mass_kg"]
            final_pressure = mass_pressure["pressure_atm"]
            source_atmo = raw.get("_raw_atmosphere", final_atmo)
            source_pressure = safe_float(source_atmo.get("Pressure", final_pressure))
            source_pressure_present = "Pressure" in source_atmo or "pressure" in source_atmo
            normalization_report = raw.get("_atmosphere_normalization_report", {})
            intent = normalization_report.get("intent", "gas_giant" if mass_pressure["mode"] == "giant" else "unknown")
            surface_depot_names = active_ocean_depot_names(raw)
            strict_atmosphere = bool(getattr(
                _gc, "STRICT_ATMOSPHERE_MASS_CONSISTENCY",
                getattr(_const, "STRICT_ATMOSPHERE_MASS_CONSISTENCY", True),
            ))
            if mass_pressure["mode"] != "giant":
                enforce_atmosphere_depot_consistency(
                    entity,
                    final_atmo,
                    expected_mass,
                    {
                        "include_water_vapor": "Water" not in surface_depot_names,
                        "surface_depot_names": surface_depot_names,
                        "strict": strict_atmosphere,
                    },
                    normalization_report,
                )
                enforce_static_surface_volatile_safety(
                    entity,
                    raw,
                    expected_mass,
                    report=normalization_report,
                )

            celestial = _component(entity, "Celestial")
            celestial_mass = safe_float(celestial.get("AtmosphereMass", 0.0))
            depot_sum = normalization_report.get("atmospheric_depot_mass_kg")
            implied_pressure = pressure_from_atmosphere_mass(
                obj.get("mass_kg", 0.0), obj.get("radius_m", 0.0), celestial_mass
            )
            mass_error = (
                abs(celestial_mass - expected_mass) / expected_mass
                if expected_mass > 0.0 else 0.0
            )
            depot_error = (
                abs(safe_float(depot_sum) - expected_mass) / expected_mass
                if expected_mass > 0.0 and depot_sum is not None else None
            )
            pressure_error = (
                abs(implied_pressure - final_pressure) / final_pressure
                if final_pressure > 0.0 else 0.0
            )
            source_pressure_error = (
                abs(final_pressure - source_pressure) / source_pressure
                if source_pressure_present and source_pressure > 0.0
                and mass_pressure["mode"] != "giant" else 0.0
            )
            audit_warnings = []
            if mass_error > 0.05:
                audit_warnings.append(
                    f"Body='{obj['name']}' celestial atmosphere mass differs from expected by {mass_error:.1%}"
                )
            if depot_error is not None and depot_error > 0.05:
                audit_warnings.append(
                    f"Body='{obj['name']}' atmospheric depot mass differs from expected by {depot_error:.1%}"
                )
            if pressure_error > 0.05:
                audit_warnings.append(
                    f"Body='{obj['name']}' atmosphere mass implies {implied_pressure:.6g}atm, "
                    f"not final {final_pressure:.6g}atm"
                )
            if source_pressure_error > 0.05:
                audit_warnings.append(
                    f"Body='{obj['name']}' final pressure differs from Space Engine "
                    f"source pressure by {source_pressure_error:.1%}"
                )
            if final_pressure >= 10.0 and mass_pressure["mode"] != "giant":
                gas_channel_mode = getattr(_gc, "SURFACE_GAS_PRESSURE_MODE", "off")
                gas_depots_locked = normalization_report.get("gas_depots_locked", False)
                static_atm = bool(getattr(_gc, "STATIC_IMPORTED_ATMOSPHERES", True))
                mitigated = (gas_channel_mode == "off" or static_atm) and gas_depots_locked
                if mitigated:
                    audit_warnings.append(
                        f"[atmo-readback-risk] Body='{obj['name']}' expected "
                        f"{final_pressure:.6g} atm / "
                        f"{expected_mass / _const.EARTH_ATMOSPHERE_MASS_KG:.6g} Earth atmospheres; "
                        "mitigated: static imported atmosphere, gas channel off, gas depots locked; "
                        "if US UI shows ~1 atm, US is recalculating after load"
                    )
                else:
                    audit_warnings.append(
                        f"[atmo-readback-risk] Body='{obj['name']}' expected "
                        f"{final_pressure:.6g} atm / "
                        f"{expected_mass / _const.EARTH_ATMOSPHERE_MASS_KG:.6g} Earth atmospheres; "
                        "static imported atmosphere not fully locked"
                    )
            _cur_depot_mode_audit = str(getattr(_gc, "STATIC_ATMOSPHERE_DEPOT_MODE", "none"))
            _cur_surface_policy_audit = str(getattr(_gc, "STATIC_IMPORTED_ATMOSPHERE_SURFACE_POLICY", "no_grid"))
            volatile_safety = {
                "static_imported_atmosphere": bool(getattr(_gc, "STATIC_IMPORTED_ATMOSPHERES", True)),
                "expected_atmosphere_mass_kg": expected_mass,
                "ocean_depot_export_mode": normalization_report.get("ocean_depot_export_mode", "visual_only"),
                "surface_water_depot_before_kg": normalization_report.get("surface_water_depot_before_kg", 0.0),
                "surface_water_depot_after_kg": normalization_report.get("surface_water_depot_after_kg", 0.0),
                "surface_water_depot_cap_kg": normalization_report.get("surface_water_depot_cap_kg", 0.0),
                "surface_other_volatile_cap_kg": normalization_report.get("surface_other_volatile_cap_kg", 0.0),
                "unsupported_ocean_depots_removed": normalization_report.get("unsupported_ocean_depots_removed", []),
                "unsupported_ocean_solutes_ignored": normalization_report.get("unsupported_ocean_solutes_ignored", []),
                "atmospheric_depot_masses_kg": normalization_report.get("atmospheric_depot_masses_kg", {}),
                "volatile_depot_masses_after_kg": normalization_report.get("volatile_depot_masses_after_kg", {}),
                "atmospheric_depots_locked": normalization_report.get("atmospheric_depots_locked", False),
                "gas_depots_locked": normalization_report.get("gas_depots_locked", False),
                "liquid_depots_locked": normalization_report.get("liquid_depots_locked", False),
                "water_depot_can_rewrite_atmosphere": normalization_report.get("water_depot_can_rewrite_atmosphere", False),
                "static_depot_mode": _cur_depot_mode_audit,
                "carrier_name": normalization_report.get("carrier_name"),
                "carrier_mass_kg": normalization_report.get("carrier_mass_kg", 0.0),
            }
            _earth_atm_mass = _const.EARTH_ATMOSPHERE_MASS_KG
            log_debug(
                f"[static-atmo-audit] Body='{obj['name']}'\n"
                f"  source_pressure_atm={source_pressure:.6g}\n"
                f"  expected_earth_atmospheres={expected_mass / _earth_atm_mass:.6g}\n"
                f"  celestial_earth_atmospheres={celestial_mass / _earth_atm_mass:.6g}\n"
                f"  depot_mode='{_cur_depot_mode_audit}'\n"
                f"  carrier={normalization_report.get('carrier_name')!r}\n"
                f"  carrier_earth_atmospheres="
                f"{normalization_report.get('carrier_mass_kg', 0.0) / _earth_atm_mass:.6g}\n"
                f"  reactive_depots_zero={_cur_depot_mode_audit in ('none', 'carrier_unlocked', 'carrier_locked')}\n"
                f"  surface_gas_ch1_zero={getattr(_gc, 'SURFACE_GAS_PRESSURE_MODE', 'off') == 'off'}\n"
                f"  surface_active_physics={active_surface_physics}\n"
                f"  surface_policy='{_cur_surface_policy_audit}'",
                "ATMO_STATIC",
            )
            audit = {
                "classification": intent,
                "source_pressure_atm": source_pressure,
                "final_pressure_atm": final_pressure,
                "expected_atmosphere_mass_kg": expected_mass,
                "expected_earth_atmospheres": expected_mass / _const.EARTH_ATMOSPHERE_MASS_KG,
                "celestial_atmosphere_mass_kg": celestial_mass,
                "atmospheric_depot_mass_kg": depot_sum,
                "mass_error_fraction": mass_error,
                "depot_mass_error_fraction": depot_error,
                "implied_pressure_atm": implied_pressure,
                "pressure_error_fraction": pressure_error,
                "source_pressure_error_fraction": source_pressure_error,
                "depot_consistency_applicable": mass_pressure["mode"] != "giant",
                "strict_consistency": strict_atmosphere,
                "water_vapor_included": normalization_report.get("water_vapor_included", False),
                "surface_gas_channel_mode": getattr(_gc, "SURFACE_GAS_PRESSURE_MODE", "off"),
                "surface_mode": surface_mode,
                "ocean_depot_mode": normalization_report.get(
                    "ocean_depot_export_mode", "visual_only"
                ),
                "active_surface_physics": active_surface_physics,
                "static_imported_atmosphere": bool(getattr(_gc, "STATIC_IMPORTED_ATMOSPHERES", True)),
                "gas_depots_locked": normalization_report.get("gas_depots_locked", False),
                "static_depot_mode": _cur_depot_mode_audit,
                "surface_policy": _cur_surface_policy_audit,
                "water_depot_counted_as_atmosphere": "Water" in surface_depot_names and normalization_report.get("water_vapor_included", False),
                "surface_water_depot_before_kg": normalization_report.get("surface_water_depot_before_kg", 0.0),
                "surface_water_depot_after_kg": normalization_report.get("surface_water_depot_after_kg", 0.0),
                "surface_water_depot_cap_kg": normalization_report.get("surface_water_depot_cap_kg", 0.0),
                "surface_water_depot_locked": normalization_report.get("surface_water_depot_locked", False),
                "surface_water_depot_removed_or_capped": normalization_report.get("surface_water_depot_removed_or_capped", False),
                "surface_water_depot_applicable": normalization_report.get("surface_water_depot_applicable", False),
                "water_depot_can_rewrite_atmosphere": normalization_report.get("water_depot_can_rewrite_atmosphere", False),
                "volatile_safety": volatile_safety,
                "expected_readback_stable": (
                    (getattr(_gc, "SURFACE_GAS_PRESSURE_MODE", "off") == "off"
                     or bool(getattr(_gc, "STATIC_IMPORTED_ATMOSPHERES", True)))
                    and normalization_report.get("gas_depots_locked", False)
                    and not normalization_report.get("water_depot_can_rewrite_atmosphere", False)
                ),
                "warnings": audit_warnings,
            }
            raw["_atmosphere_audit"] = audit
            obj["_atmosphere_audit"] = audit
            ok = mass_error <= 0.05 and pressure_error <= 0.05 and source_pressure_error <= 0.05 and (
                depot_error is None or depot_error <= 0.05
            )
            log_debug(
                f"[atmo-audit] Body='{obj['name']}' class='{raw.get('Class', intent)}' "
                f"P_source={source_pressure:.6g}atm P_final={final_pressure:.6g}atm "
                f"expected_mass={expected_mass:.6g}kg "
                f"earth_atm={expected_mass / _const.EARTH_ATMOSPHERE_MASS_KG:.6g} "
                f"celestial={celestial_mass:.6g} depot_sum={safe_float(depot_sum):.6g} "
                f"implied_pressure={implied_pressure:.6g}atm surface_mode='{surface_mode}' "
                f"surface_gas='{audit['surface_gas_channel_mode']}' "
                f"ocean_depot='{audit['ocean_depot_mode']}' "
                f"active_surface_physics={active_surface_physics} "
                f"readback_stable={audit['expected_readback_stable']} ok={ok}",
                "ATMO_AUDIT",
            )
            log_debug(
                f"[atmo-readback] Body='{obj['name']}' expected_pressure={final_pressure:.6g}atm "
                f"expected_mass={expected_mass:.6g}kg stable={audit['expected_readback_stable']}",
                "ATMO_READBACK",
            )

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
            ring_particles_generated += orig
            particles = apply_limit_filter(particles, planetary_ring_input, "planetary_rings")
            ring_particles_kept += len(particles)
            log_debug(f"Rings '{obj['name']}': {orig} gen, {len(particles)} kept", "RING_PARTICLES")
            ubox_sim["Entities"].extend(particles)
            next_id += orig  # reserve the full generated ID range; gaps are OK, duplicates are not
            print(f"  Rings  {len(particles)}/{orig} for '{obj['name']}'")

    # Emit suspended US barycenter markers for all barycentric systems.
    for bary_decl, bary_obj in list(parsed_objects.items()):
        if not bary_obj.get("_emit_us_barycenter"):
            continue
        child_decls = bary_obj.get("_bary_component_children") or bary_obj.get("_binary_children") or []
        component_decls = child_decls
        component_decls = [d for d in component_decls if d in parsed_objects]
        if len(component_decls) < 2:
            log_debug(
                f"BARY_EXPORT skipped '{bary_obj['name']}': fewer than two component children",
                "BINARY_WARN"
            )
            continue

        emitted_ids = {e.get("Id") for e in ubox_sim["Entities"]}
        component_ids = []
        for child_decl in component_decls:
            child_obj = parsed_objects[child_decl]
            child_id = child_obj["id"]
            if child_obj.get("is_barycenter"):
                if child_obj.get("_emit_us_barycenter"):
                    component_ids.append(child_id)
                else:
                    log_debug(
                        f"BARY_EXPORT skipped child barycenter '{child_obj['name']}' "
                        f"for '{bary_obj['name']}': no emitted barycenter marker",
                        "BINARY_WARN"
                    )
            elif child_id in emitted_ids:
                component_ids.append(child_id)
            else:
                log_debug(
                    f"BARY_EXPORT skipped child '{child_obj['name']}' for '{bary_obj['name']}': "
                    f"body was not emitted",
                    "BINARY_WARN"
                )
        if len(component_ids) < 2:
            log_debug(
                f"BARY_EXPORT skipped '{bary_obj['name']}': fewer than two emitted component ids "
                f"after filters ({component_ids})",
                "BINARY_WARN"
            )
            continue

        real_decls = [d for d in component_decls if not parsed_objects[d].get("is_barycenter")]
        positions = {}
        velocities = {}
        nonzero_positions = 0
        for child_decl in component_decls:
            pos, vel = get_abs_state(child_decl)
            if not all(math.isfinite(x) for x in pos + vel):
                raise ValueError(
                    f"barycenter '{bary_obj['name']}' invalid: child "
                    f"'{parsed_objects[child_decl]['name']}' has non-finite state"
                )
            positions[child_decl] = pos
            velocities[child_decl] = vel
            if _vec_len(pos) > 0:
                nonzero_positions += 1

        if len(component_decls) >= 3:
            if nonzero_positions == 0:
                raise ValueError(
                    f"barycenter '{bary_obj['name']}' invalid: all {len(component_decls)} child positions are zero"
                )
            log_debug(
                f"BARY_EXPORT warning barycenter='{bary_obj['name']}': "
                f"{len(component_decls)} direct children emitted with approximate barycentric states",
                "BINARY_WARN"
            )

        if len(real_decls) == 2:
            a_decl, b_decl = real_decls
            a_obj = parsed_objects[a_decl]
            b_obj = parsed_objects[b_decl]
            distance_ab = _vec_len([positions[b_decl][i] - positions[a_decl][i] for i in range(3)])
            radius_sum = a_obj.get("radius_m", 0.0) + b_obj.get("radius_m", 0.0)
            if distance_ab <= radius_sum:
                raise ValueError(
                    f"barycenter '{bary_obj['name']}' invalid: "
                    f"distance_AB={distance_ab:.6f}m <= radius_A+radius_B={radius_sum:.6f}m"
                )

        com_pos_eci, com_vel_eci = get_abs_state(bary_decl)
        com_pos_us = eci_to_us(com_pos_eci)
        com_vel_us = eci_to_us(com_vel_eci)
        total_mass = bary_obj.get("mass_kg", 0.0) or sum(
            _subtree_mass_kg(d, parsed_objects) for d in component_decls
        )
        max_radius = max(
            [parsed_objects[d].get("radius_m", 0.0) for d in real_decls] + [1.0]
        )
        bary_name = f"{bary_obj['name']} Barycenter"

        bary_entity, _, _, _, _, _ = build_ubox_entity(
            bary_obj["id"], bary_name, "star", "star",
            total_mass, max_radius,
            com_pos_us, com_vel_us, -1, True,
            relative_to_id=0,
            teff=0.0,
            lum_watts=0.0,
            rot_period_h=0.0,
            obliquity_deg=0.0,
            eq_asc_node_deg=0.0,
            atm_info={},
            has_ocean=False,
            use_water=False,
            mag_field=0.0,
            mag_pole_angle=0.0,
            sea_level=0.0,
            surface_preset="",
            est_temp=0.0,
            has_life=False,
            has_exotic_life=False,
            has_organic_life=False,
            has_aerial_life=False,
            diffmap=None,
            se_class="",
            dist_au=0.0,
            star_teff=0.0,
            atlas_index=None,
            obj_data={},
        )
        bary_entity.update({
            "$type": "Body",
            "Id": bary_obj["id"],
            "Name": bary_name,
            "Components": [
                {
                    "$type": "BarycenterComponent",
                    "Bodies": component_ids,
                    "UseAutoName": True,
                }
            ],
            "Mass": total_mass,
            "PhysicsMass": total_mass,
            "Radius": 0.0,
            "Density": 0.0,
            "Suspended": True,
            "Parent": -1,
            "CustomOrbitParentId": -1,
            "RelativeTo": 0,
            "Flags": 18,
            "DisplayFlags": 3,
            "Position": _fmt_vec3(com_pos_us),
            "Velocity": _fmt_vec3(com_vel_us),
        })
        ubox_sim["Entities"].append(bary_entity)

        log_debug(
            f"BARYCENTER_EMIT name={bary_entity['Name']} id={bary_obj['id']} bodies={component_ids}",
            "BINARY_DBG"
        )
        log_debug(
            f"BARY_EXPORT final barycenter='{bary_entity['Name']}' id={bary_obj['id']} "
            f"BarycenterComponent_created=True bodies={component_ids} "
            f"component_count={len(component_ids)} total_mass={total_mass:.9e}kg",
            "BINARY_DBG"
        )
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

    # Validate suspended barycenter markers after every entity has been added.
    # ── Barycenter repair pass ────────────────────────────────────────────────
    # After all entities are emitted, some BarycenterComponent.Bodies entries
    # may reference IDs that were never emitted (e.g. a nested barycenter that
    # was itself skipped because its own children were filtered out).  Strip
    # those stale IDs and remove any barycenter whose Bodies list collapses to
    # fewer than 2 valid members.  Run iteratively until stable so that removing
    # one barycenter can clean up a parent that referenced it.
    changed = True
    while changed:
        changed = False
        valid_ids = {e.get("Id") for e in ubox_sim["Entities"]}
        to_remove_ids: set = set()
        for entity in ubox_sim["Entities"]:
            bc = next(
                (c for c in entity.get("Components", []) if c.get("$type") == "BarycenterComponent"),
                None,
            )
            if bc is None:
                continue
            before = list(bc.get("Bodies", []))
            after  = [i for i in before if i in valid_ids]
            if len(after) < 2:
                log_debug(
                    f"BARY_REPAIR removing barycenter '{entity.get('Name', entity.get('Id'))}' "
                    f"id={entity.get('Id')}: Bodies collapsed from {before} to {after} "
                    f"after filter (fewer than 2 valid members)",
                    "BINARY_WARN",
                )
                to_remove_ids.add(entity.get("Id"))
                changed = True
            elif len(after) < len(before):
                log_debug(
                    f"BARY_REPAIR trimmed Bodies for '{entity.get('Name', entity.get('Id'))}' "
                    f"id={entity.get('Id')}: removed stale ids {set(before) - set(after)}",
                    "BINARY_WARN",
                )
                bc["Bodies"] = after
                changed = True
        if to_remove_ids:
            ubox_sim["Entities"] = [
                e for e in ubox_sim["Entities"] if e.get("Id") not in to_remove_ids
            ]
            # Also strip removed ids from any remaining barycenter Bodies lists
            for entity in ubox_sim["Entities"]:
                bc = next(
                    (c for c in entity.get("Components", []) if c.get("$type") == "BarycenterComponent"),
                    None,
                )
                if bc is not None:
                    bc["Bodies"] = [i for i in bc.get("Bodies", []) if i not in to_remove_ids]

    all_entity_ids = {e.get("Id") for e in ubox_sim["Entities"]}
    entity_by_id = {e.get("Id"): e for e in ubox_sim["Entities"]}
    for decl, obj in parsed_objects.items():
        if not obj.get("_binary_free_body"):
            continue
        entity = entity_by_id.get(obj["id"])
        if entity is None:
            continue
        if entity.get("Parent") != -1 or entity.get("CustomOrbitParentId") != -1:
            raise ValueError(
                f"binary body '{obj['name']}' invalid: Parent={entity.get('Parent')} "
                f"CustomOrbitParentId={entity.get('CustomOrbitParentId')}, expected -1/-1"
            )

    emitted_bary_ids = {
        e.get("Id") for e in ubox_sim["Entities"]
        if any(c.get("$type") == "BarycenterComponent" for c in e.get("Components", []))
    }
    for bary_decl, bary_obj in parsed_objects.items():
        if not bary_obj.get("_emit_us_barycenter"):
            continue

        child_decls = bary_obj.get("_bary_component_children") or bary_obj.get("_binary_children") or []
        expected_component_ids = []
        for child_decl in child_decls:
            child_obj = parsed_objects.get(child_decl)
            if not child_obj:
                continue
            child_id = child_obj["id"]
            if child_obj.get("is_barycenter"):
                if child_id in emitted_bary_ids:
                    expected_component_ids.append(child_id)
            elif child_id in all_entity_ids:
                expected_component_ids.append(child_id)

        if len(expected_component_ids) < 2:
            log_debug(
                f"BARYCENTER_VALIDATE skipped '{bary_obj['name']}': fewer than two emitted components",
                "BINARY_WARN"
            )
            continue

        matches = [
            e for e in ubox_sim["Entities"]
            if e.get("Id") == bary_obj["id"]
            and any(c.get("$type") == "BarycenterComponent" for c in e.get("Components", []))
        ]
        if len(matches) == 0:
            # Removed by repair pass (too few valid bodies after export filters) — not an error.
            log_debug(
                f"BARYCENTER_VALIDATE skipped '{bary_obj['name']}': "
                f"entity was removed by repair pass (export filter eliminated its children)",
                "BINARY_WARN",
            )
            continue
        if len(matches) != 1:
            raise ValueError(
                f"barycenter '{bary_obj['name']}' invalid: expected exactly one "
                f"BarycenterComponent entity, found {len(matches)}"
            )

        component = next(
            c for c in matches[0].get("Components", [])
            if c.get("$type") == "BarycenterComponent"
        )
        component_ids = list(component.get("Bodies", []))
        missing_ids = [body_id for body_id in component_ids if body_id not in all_entity_ids]
        if missing_ids:
            raise ValueError(
                f"barycenter '{bary_obj['name']}' invalid: component body ids missing "
                f"from simulation entities: {missing_ids}"
            )
        # Use the post-repair component_ids as the source of truth; expected_component_ids is
        # computed from _bary_component_children which may include bodies filtered by export
        # options.  Validate that every emitted Bodies entry is valid, not that it equals the
        # pre-filter expected set.
        if not set(component_ids).issubset(set(expected_component_ids) | all_entity_ids):
            raise ValueError(
                f"barycenter '{bary_obj['name']}' invalid: component ids {component_ids} "
                f"contain ids outside expected emitted set {expected_component_ids}"
            )
        binary_decls = bary_obj.get("_binary_children", [])
        if len(binary_decls) == 2 and all(d in parsed_objects for d in binary_decls):
            obj_a = parsed_objects[binary_decls[0]]
            obj_b = parsed_objects[binary_decls[1]]
            if obj_a.get("_binary_free_body") and obj_b.get("_binary_free_body"):
                pos_a, vel_a = get_abs_state(binary_decls[0])
                pos_b, vel_b = get_abs_state(binary_decls[1])
                distance_ab = _vec_len([pos_b[i] - pos_a[i] for i in range(3)])
                rel_vel = _vec_len([vel_b[i] - vel_a[i] for i in range(3)])
                radius_sum = obj_a.get("radius_m", 0.0) + obj_b.get("radius_m", 0.0)
                mass_total = obj_a.get("mass_kg", 0.0) + obj_b.get("mass_kg", 0.0)
                escape_velocity = math.sqrt(2.0 * GRAVITATIONAL_CONSTANT * mass_total / distance_ab) if distance_ab > 0 and mass_total > 0 else 0.0
                if distance_ab <= radius_sum:
                    raise ValueError(
                        f"binary '{obj_a['name']} + {obj_b['name']}' invalid: "
                        f"distance_AB={distance_ab:.6f}m <= radius_A+radius_B={radius_sum:.6f}m"
                    )
                if rel_vel <= 0.0 or not math.isfinite(rel_vel):
                    raise ValueError(
                        f"binary '{obj_a['name']} + {obj_b['name']}' invalid: zero/non-finite relative velocity"
                    )
                if escape_velocity > 0 and rel_vel >= escape_velocity:
                    raise ValueError(
                        f"binary '{obj_a['name']} + {obj_b['name']}' invalid: "
                        f"relative_velocity={rel_vel:.6f}m/s >= escape_velocity={escape_velocity:.6f}m/s"
                    )
                log_debug(
                    f"BINARY_VALIDATE barycenter='{bary_obj['name']}' "
                    f"bodies={[obj_a['id'], obj_b['id']]} "
                    f"distance_AB={distance_ab:.6e}m "
                    f"relative_velocity={rel_vel:.6e}m/s "
                    f"escape_velocity={escape_velocity:.6e}m/s",
                    "BINARY_DBG"
                )
        log_debug(
            f"BARYCENTER_VALIDATE name={matches[0].get('Name')} id={bary_obj['id']} bodies={component_ids}",
            "BINARY_DBG"
        )

    # Apply this after every entity/template/default pass so nothing can restore
    # an unsafe startup speed before the final simulation JSON is serialized.
    apply_startup_simulation_defaults(ubox_sim)

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
    source_by_id = {
        obj["id"]: obj for obj in parsed_objects.values()
        if isinstance(obj.get("id"), int)
    }
    validation = validate_simulation_entities(
        ubox_sim,
        surface_zip_payload=surface_zip_payload,
        manifest=manifest,
        source_by_id=source_by_id,
    )

    source_entities = [
        entity for entity in ubox_sim["Entities"]
        if entity.get("Id") in source_by_id
        and not _has_component(entity, "ParticleComponent")
        and not _has_component(entity, "BarycenterComponent")
    ]
    type_counts = {"stars": 0, "planets": 0, "moons": 0, "dwarf_planets": 0, "comets": 0}
    for entity in source_entities:
        source = source_by_id[entity["Id"]]
        decl_type = str(source.get("decl_type", "")).strip().lower()
        if source.get("is_star"):
            type_counts["stars"] += 1
        elif decl_type in ("moon", "dwarfmoon"):
            type_counts["moons"] += 1
        elif decl_type == "dwarfplanet":
            type_counts["dwarf_planets"] += 1
        elif decl_type == "comet":
            type_counts["comets"] += 1
        else:
            type_counts["planets"] += 1

    normalization_reports = [
        obj.get("raw_data", {}).get("_atmosphere_normalization_report", {})
        for obj in parsed_objects.values()
        if isinstance(obj.get("raw_data"), dict)
    ]
    normalization_reports = [report for report in normalization_reports if report]
    atmosphere_audit = {
        obj.get("name", str(obj.get("id", "unknown"))): obj.get("raw_data", {}).get("_atmosphere_audit")
        for obj in parsed_objects.values()
        if isinstance(obj.get("raw_data"), dict) and obj.get("raw_data", {}).get("_atmosphere_audit")
    }
    warnings_by_category = {
        "atmosphere": [],
        "water": [],
        "barycenter": [],
        "surface": [],
        "validation": [],
    }
    warning_keys = set()

    def _warning_category(level, message):
        text = f"{level} {message}".lower()
        if "atmo" in text or "atmosphere" in text or "chemistry" in text:
            return "atmosphere"
        if "water" in text or "ocean" in text or "lacustrine" in text or "liquid" in text:
            return "water"
        if "bary" in text or "binary" in text:
            return "barycenter"
        if "surface" in text or "heightmap" in text or "atlas" in text:
            return "surface"
        return "validation"

    def _add_summary_warning(category, message):
        clean = str(message).strip()
        while re.match(r"^\[[^\]]+\]\s*", clean):
            clean = re.sub(r"^\[[^\]]+\]\s*", "", clean, count=1)
        canonical = re.sub(r"\s+", " ", clean).strip().lower()
        if not clean or canonical in warning_keys:
            return
        warning_keys.add(canonical)
        warnings_by_category[category].append(clean)

    atmosphere_report_warnings = []
    for report in normalization_reports:
        body_name = str(report.get("body", "unknown"))
        for warning in report.get("warnings", []):
            atmosphere_report_warnings.append(str(warning))
            _add_summary_warning("atmosphere", f"Body='{body_name}' {warning}")
    for audit in atmosphere_audit.values():
        for warning in audit.get("warnings", []):
            _add_summary_warning("atmosphere", warning)

    for line in _const.CONVERSION_LOG[log_start_index:]:
        match = re.match(r"^\[[^\]]+\]\s+\[([^\]]+)\]\s+(.*)$", line)
        level = match.group(1) if match else ""
        message = match.group(2) if match else line
        if "WARN" not in level.upper():
            continue
        category = _warning_category(level, message)
        # Normalization reports are the authoritative atmosphere warnings.
        # Skip alternate debug/validator renderings of the same chemistry event.
        if category == "atmosphere" and normalization_reports:
            continue
        _add_summary_warning(category, message)

    for warning in validation["warnings"]:
        warning_text = str(warning)
        if any(warning_text.endswith(report_warning) for report_warning in atmosphere_report_warnings):
            continue
        _add_summary_warning(_warning_category("VALIDATE", warning_text), warning_text)

    warning_lines = [
        warning
        for category in ("atmosphere", "water", "barycenter", "surface", "validation")
        for warning in warnings_by_category[category]
    ]
    surface_enabled = bool(generate_surface)
    surface_bodies_generated = min(len(surface_bodies), 256) if surface_zip_payload else 0
    surface_slots = len(surface_atlas_indices)
    conversion_summary = {
        "title": "Space Engine to Universe Sandbox Conversion Summary",
        "converter_version": CONVERTER_VERSION,
        "timestamp_utc": now,
        "source": str(source_name or sim_name),
        "output": os.path.abspath(output_file),
        "counts": {
            "bodies_parsed": parsed_count_before_filters,
            "entities_exported": len(ubox_sim["Entities"]),
            **type_counts,
            "asteroids_kept": len(surviving_rings),
            "asteroids_total": len(belt_asteroids),
            "ring_particles_kept": ring_particles_kept,
            "ring_particles_total": ring_particles_generated,
            "comets_kept": len(surviving_comets),
            "comets_total": len(comets),
        },
        "surface": {
            "enabled": surface_enabled,
            "mode": surface_mode,
            "archive_generated": surface_zip_payload is not None,
            "surface_grid_attached": bool(attach_surface_grid),
            "active_surface_physics": bool(active_surface_physics),
            "bodies_generated": surface_bodies_generated,
            "atlas_slots_used": surface_slots,
            "atlas_capacity": 256,
            "static_imported_atmospheres": bool(
                getattr(_gc, "STATIC_IMPORTED_ATMOSPHERES", True)
            ),
            "surface_gas_pressure_mode": str(
                getattr(_gc, "SURFACE_GAS_PRESSURE_MODE", "off")
            ),
            "ocean_depot_export_mode": str(
                getattr(_gc, "OCEAN_DEPOT_EXPORT_MODE", "visual_only")
            ),
        },
        "atmosphere_normalization": {
            "enabled": bool(getattr(_gc, "NORMALIZE_SE_ATMOSPHERE", True)),
            "mode": str(getattr(_gc, "NORMALIZE_SE_ATMOSPHERE_MODE", "stability")),
            "bodies_examined": len(normalization_reports),
            "bodies_normalized": sum(bool(report.get("changed")) for report in normalization_reports),
            "bodies_changed": sum(bool(report.get("changed")) for report in normalization_reports),
            "chemistry_warnings": sum(len(report.get("warnings", [])) for report in normalization_reports),
        },
        "atmosphere_audit": atmosphere_audit,
        "startup": {
            "paused": bool(ubox_sim.get("Pause")),
            "target_time_step_per_real_sec": safe_float(ubox_sim.get("TargetTimeStepPerRealSec", 0.0)),
            "max_time_step_per_real_sec": safe_float(ubox_sim.get("MaximalTimeStepPerRealSec", 0.0)),
            "time_step": safe_float(ubox_sim.get("TimeStep", 0.0)),
            "simulation_speed": safe_float(ubox_sim.get("SimulationSpeed", 0.0)),
            "time_passed": safe_float(ubox_sim.get("TimePassed", 0.0)),
            "auto_speed_enabled": bool(ubox_sim.get("AutoSpeed", {}).get("Enabled", False)),
        },
        "validation": {
            "warnings": len(warning_lines),
            "errors": 0,
            "atlas_users": validation["atlas_users"],
        },
        "warnings": warning_lines,
        "warnings_by_category": warnings_by_category,
        "known_limitations": [
            "Shallow sea readouts in Universe Sandbox remain approximate.",
            "Built-in visual heightmap overlays are approximate.",
            "Very high simulation speed can destabilize moon systems.",
            "Binary and multiple systems use approximate N-body initial states.",
            "Procedural surface maps are not exact Space Engine textures.",
        ],
    }
    log_debug(
        f"[release-summary] exported={len(ubox_sim['Entities'])} warnings={len(warning_lines)} "
        f"surface_slots={surface_slots}/256 output='{os.path.abspath(output_file)}'",
        "SUMMARY",
    )
    try:
        from description_generator import safe_generate_description
        _desc_style = str(getattr(_gc, "DESCRIPTION_STYLE", "wiki_short"))
        _desc_flags = {
            "EXPORT_MOONS":       bool(getattr(_gc, "EXPORT_MOONS", True)),
            "EXPORT_RINGS":       bool(getattr(_gc, "EXPORT_RINGS", True)),
            "EXPORT_DWARF_MOONS": bool(getattr(_gc, "EXPORT_DWARF_MOONS", True)),
            "EXPORT_COMETS":      bool(getattr(_gc, "EXPORT_COMETS", False)),
        }
        _sim_description = safe_generate_description(
            parsed_objects, ubox_sim["Entities"], _desc_flags, style=_desc_style
        )
    except Exception as _desc_exc:
        _sim_description = "Converted from Space Engine."
        log_debug(f"[description-warning] description generation error: {_desc_exc}", "DESCRIPTION")
    info = {"Header":{"BaseType":"WorkshopItem","AssetType":"JSON","TypeName":"info.json",
                       "BuildRevision":"46923","LastModifiedUTC":now},
            "Name":sim_name,"Description":_sim_description,"Flags":"None",
            "Tags":[{"$v":"Simulations"}]}
    ui_state = {"Header":{"BaseType":"UIState","AssetType":"JSON","TypeName":"ui-state.json",
                           "BuildRevision":"46923","LastModifiedUTC":now}}

    _status("Writing simulation archive")
    with zipfile.ZipFile(output_file, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(sfn, json.dumps(ubox_sim, indent=4))
        zf.writestr("manifest.json", json.dumps(manifest, indent=4))
        zf.writestr(ifn, json.dumps(info,     indent=4))
        zf.writestr(ufn, json.dumps(ui_state, indent=4))
        zf.writestr("conversion-summary.json", json.dumps(conversion_summary, indent=4))
        if surface_zip_path and surface_zip_payload is not None:
            zf.writestr(surface_zip_path, surface_zip_payload)
    _status("Finalizing conversion")
    debug_log_path = None
    if _const.DEBUG_MODE:
        debug_log_path = os.path.splitext(output_file)[0] + "-conversion.log"
        debug_log_path = write_conversion_log(debug_log_path, log_start_index)

    conversion_report = {
        "output_path": os.path.abspath(output_file),
        "warning_count": len(warning_lines),
        "warnings": warning_lines,
        "surface_enabled": surface_enabled,
        "surface_slots": surface_slots,
        "surface_bodies_generated": surface_bodies_generated,
        "surface_mode": surface_mode,
        "surface_grid_attached": bool(attach_surface_grid),
        "entity_count": len(ubox_sim["Entities"]),
        "debug_log_path": os.path.abspath(debug_log_path) if debug_log_path else None,
        "summary": conversion_summary,
    }
    print(f"Success  {len(ubox_sim['Entities'])} objects → {output_file}")
    return conversion_report

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