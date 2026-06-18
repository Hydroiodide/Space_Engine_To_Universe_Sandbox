"""
description_generator.py
Procedural, deterministic, LLM-free system description generator.
Produces Wikipedia/NMS-style summaries from exported simulation facts.
"""

import re
import math
import random
import hashlib

# ─── helpers ─────────────────────────────────────────────────────────────────

def _stable_hash(text: str) -> int:
    return int(hashlib.md5(str(text).encode()).hexdigest(), 16)


def _rng(seed) -> random.Random:
    return random.Random(_stable_hash(str(seed)))


def _cardinal(n: int) -> str:
    WORDS = {1:"one",2:"two",3:"three",4:"four",5:"five",
             6:"six",7:"seven",8:"eight",9:"nine",10:"ten",
             11:"eleven",12:"twelve"}
    return WORDS.get(n, str(n))


def _plural(word: str, n: int) -> str:
    if n == 1:
        return f"one {word}"
    return f"{_cardinal(n)} {word}s"


def _article(word: str) -> str:
    return "an" if word[:1].lower() in "aeiou" else "a"


# ─── star classification ──────────────────────────────────────────────────────

_SPECTRAL_KIND = {
    "O": ("O-type", "hot blue"), "B": ("B-type", "blue-white"),
    "A": ("A-type", "white"), "F": ("F-type", "yellow-white"),
    "G": ("G-type", "yellow"), "K": ("K-type", "orange"),
    "M": ("M-type", "red"), "L": ("L-type", "brown dwarf"),
    "T": ("T-type", "brown dwarf"), "Y": ("Y-type", "brown dwarf"),
}


def _spectral_letter(raw_class: str) -> str:
    m = re.match(r'([OBAFGKMLTYWRCD])', str(raw_class).strip(), re.IGNORECASE)
    return m.group(1).upper() if m else "G"


def _describe_star(star_info: dict) -> str:
    cls = str(star_info.get("class", "G")).strip()
    kind = star_info.get("kind", "normal_star")
    if kind == "black_hole":
        return "black hole"
    if kind == "neutron_star":
        return "neutron star"
    if kind == "white_dwarf":
        return "white dwarf"
    if kind in ("brown_dwarf", "planemo"):
        return "brown dwarf companion"
    letter = _spectral_letter(cls)
    label, _ = _SPECTRAL_KIND.get(letter, ("G-type", "yellow"))
    lum = ""
    if "V" in cls and "IV" not in cls:
        lum = " main-sequence star"
    elif "III" in cls:
        lum = " giant"
    elif any(s in cls for s in ("Ia", "Ib", "I ")):
        lum = " supergiant"
    return f"{label}{lum} star"


def _star_phrase(stars: list) -> str:
    """Natural multi-star description."""
    if not stars:
        return "an unknown stellar primary"
    if len(stars) == 1:
        return f"{_article(_describe_star(stars[0]))} {_describe_star(stars[0])}"
    if len(stars) == 2:
        a, b = _describe_star(stars[0]), _describe_star(stars[1])
        if a == b:
            return f"a pair of {a}s"
        return f"a {_describe_star(stars[0])} and a {_describe_star(stars[1])}"
    return f"{_cardinal(len(stars))} stellar components"


def _system_type(stars: list, bary_depth: int) -> str:
    n = len(stars)
    kinds = {s.get("kind", "normal_star") for s in stars}
    has_bh  = "black_hole" in kinds
    has_ns  = "neutron_star" in kinds
    has_bd  = "brown_dwarf" in kinds or "planemo" in kinds
    if n == 1:
        if has_bh:
            return "single black-hole system"
        if has_ns:
            return "single neutron-star system"
        return "single-star system"
    if n == 2:
        if has_bh:
            return "system with a black-hole primary"
        if has_ns:
            return "system with a neutron-star remnant"
        if has_bd:
            return "system with a brown dwarf companion"
        if bary_depth >= 2:
            return "compact binary system"
        return "binary system"
    if n == 3:
        return "hierarchical triple system" if bary_depth >= 2 else "triple system"
    if n == 4:
        return "quadruple system"
    if n == 5:
        return "quintuple system"
    return f"{n}-star system"


# ─── world classification ─────────────────────────────────────────────────────

_OCEAN_CLASSES = {"aquaria","ocean","marine","panthalassic","oceania"}
_ICEWORLD_CLASSES = {"ice","tundra","glacial","snowball","cryogenic"}
_LAVA_CLASSES = {"lava","volcanic","ferria","magma"}
_GAS_CLASSES = {"jupitergiant","gasgiant","gaspuff","hotjupiter","subneptune"}
_ICE_GIANT_CLASSES = {"neptune","icegiant","uranus"}
_DESERT_CLASSES = {"desert","arid","barren","selena","stygian"}


def _world_kind(world: dict) -> str:
    cls = str(world.get("class", "")).lower().replace(" ", "").replace("-", "")
    se_class = world.get("se_class", cls)
    if cls in _OCEAN_CLASSES or se_class in _OCEAN_CLASSES:
        depth = world.get("ocean_depth_km", 0)
        sea   = world.get("sea_level", 0)
        if depth >= 1.0 or sea >= 0.05:
            return "ocean world"
        return "shallow-ocean Terra"
    if cls in _ICEWORLD_CLASSES:
        return "icy world"
    if cls in _LAVA_CLASSES:
        return "lava world"
    if cls in _GAS_CLASSES or "giant" in cls or "jupiter" in cls:
        return "gas giant"
    if cls in _ICE_GIANT_CLASSES or "neptune" in cls or "icegiant" in cls:
        return "Neptune-class giant"
    if cls in _DESERT_CLASSES:
        return "barren rocky world"
    if cls == "terra":
        return "Terra world"
    return "rocky world"


def _world_qualifier(world: dict) -> str:
    """Optional qualifier like 'ringed', 'life-bearing', 'marine'."""
    parts = []
    if world.get("has_rings"):
        parts.append("ringed")
    life = world.get("life_info", {})
    if life and life.get("has_life"):
        biome = life.get("biome", "").lower()
        ltype = "organic" if life.get("is_organic") else "exotic"
        mtype = "multicellular" if life.get("is_multicellular", False) else "unicellular"
        parts.append(f"with {ltype} {mtype} life")
    return " ".join(parts)


# ─── sentence builders ────────────────────────────────────────────────────────

def _life_worlds_phrase(life_worlds: list) -> str:
    n = len(life_worlds)
    if n == 0:
        return ""
    if n == 1:
        w = life_worlds[0]
        kind  = _world_kind(w)
        life  = w.get("life_info", {})
        ltype = "organic" if life.get("is_organic") else "exotic"
        mtype = "multicellular" if life.get("is_multicellular", False) else "unicellular"
        biome = life.get("biome", "")
        biome_str = f" {biome.lower()}" if biome else ""
        return f"a {kind} harboring {ltype}{biome_str} {mtype} life"
    return f"{_cardinal(n)} life-bearing worlds"


def _planet_family_phrase(worlds: list, rng: random.Random) -> str:
    if not worlds:
        return ""
    n = len(worlds)
    kinds  = {}
    for w in worlds:
        k = _world_kind(w)
        kinds[k] = kinds.get(k, 0) + 1
    phrases = []
    for kind, count in sorted(kinds.items(), key=lambda x: -x[1]):
        if count == 1:
            phrases.append(f"a {kind}")
        else:
            phrases.append(f"{_cardinal(count)} {kind}s")
    if len(phrases) == 0:
        return f"{_cardinal(n)} major worlds"
    if len(phrases) == 1:
        return phrases[0]
    return ", ".join(phrases[:-1]) + f" and {phrases[-1]}"


def _poi_sentences(facts: dict, rng: random.Random) -> list:
    """Generate 0-2 additional POI sentences."""
    sentences = []
    export_rings = facts.get("export_rings", True)

    # life worlds
    life_worlds = facts.get("life_worlds", [])
    if life_worlds:
        verb = "is" if len(life_worlds) == 1 else "are"
        sentences.append(
            f"Notable among its worlds {verb} " + _life_worlds_phrase(life_worlds) + "."
        )
    # neutron star / black hole
    stars = facts.get("stars", [])
    for s in stars:
        if s.get("kind") == "neutron_star":
            sentences.append(
                f"The system harbors a neutron star remnant, {s.get('name','')}, "
                f"the collapsed core of a former massive star.".strip()
            )
        elif s.get("kind") == "black_hole":
            sentences.append(
                f"A stellar-mass black hole, {s.get('name','')}, dominates the system's core.".strip()
            )
    # binary planet
    if facts.get("binary_terra_pair"):
        sentences.append(
            "The system contains a rare double-Terra pair, two Earthlike worlds orbiting a shared barycenter."
        )
    # ringed worlds — only if rings were exported
    if export_rings:
        ringed = facts.get("ringed_world_count", 0)
        if ringed >= 2:
            sentences.append(f"{_cardinal(ringed).capitalize()} of its worlds carry prominent ring systems.")
        elif ringed == 1:
            sentences.append("One of its worlds carries a prominent ring system.")
    # satellite features
    if facts.get("has_nested_moons"):
        sentences.append("An unusual nested moon system exists within the satellite population.")
    if facts.get("has_shepherd_moons") and export_rings:
        sentences.append("Candidate shepherd moons graze the edges of the ring systems.")
    # high-inclination
    hi_inc = facts.get("high_inclination_worlds", [])
    if hi_inc:
        sentences.append(
            f"At least one world follows a steeply inclined orbit, tilted over {hi_inc[0]:.0f}°."
        )
    return sentences[:2]  # cap at 2


# ─── main API ─────────────────────────────────────────────────────────────────

def build_system_description_facts(
    parsed_objects: dict,
    exported_entities: list,
    flags: dict,
) -> dict:
    """
    Build a facts dict from the actually-exported simulation entities.
    Uses parsed_objects only to look up source details for exported bodies.
    """
    export_moons       = bool(flags.get("EXPORT_MOONS", True))
    export_rings       = bool(flags.get("EXPORT_RINGS", True))
    export_dwarf_moons = bool(flags.get("EXPORT_DWARF_MOONS", True))
    export_comets      = bool(flags.get("EXPORT_COMETS", False))

    # Build id→obj_name map from exported entities
    id_to_name: dict = {}
    for ent in exported_entities:
        eid = ent.get("Id")
        if eid is not None:
            id_to_name[eid] = ent.get("Name", str(eid))

    # Gather system name from first barycenter or first star
    system_name = ""
    stars, planets, moons, dwarf_moons, ring_bodies, comets_list = [], [], [], [], [], []

    # Build lookup from name to parsed object
    name_to_obj: dict = {}
    for decl, obj in parsed_objects.items():
        name_to_obj[obj.get("name", "")] = obj

    # Gather from exported entities
    exported_names = {ent.get("Name", "") for ent in exported_entities}
    bary_depth = 0

    for decl, obj in parsed_objects.items():
        if obj.get("name", "") not in exported_names:
            continue
        raw   = obj.get("raw_data", {}) or {}
        dtype = str(obj.get("decl_type", "")).lower()
        cls   = str(raw.get("Class", "")).strip()

        if obj.get("is_star") or dtype in ("star","blackhole","neutronstar","whitedwarf"):
            from builder import classify_spaceengine_stellar_body
            stl = classify_spaceengine_stellar_body(raw)
            entry = {
                "name": obj.get("name", ""),
                "class": cls,
                "kind": stl["kind"],
                "teff": obj.get("teff", 0),
            }
            stars.append(entry)
            if not system_name:
                # use parent or barycenter name as system name
                system_name = raw.get("ParentBody", "") or obj.get("name", "")

        elif dtype in ("planet","dwarfplanet"):
            life_info = raw.get("_life_info", {})
            ocean_report = raw.get("_ocean_depot_report", {})
            has_rings = bool(raw.get("Ring") or raw.get("Rings"))
            if has_rings and not export_rings:
                has_rings = False
            entry = {
                "name":  obj.get("name", ""),
                "class": cls,
                "se_class": cls.lower().replace(" ", "").replace("-", ""),
                "ocean_depth_km": float(ocean_report.get("source_depth_km", 0.0)),
                "sea_level": float(raw.get("Surface", {}).get("seaLevel", 0.0)
                                   if isinstance(raw.get("Surface"), dict) else 0.0),
                "has_rings": has_rings,
                "life_info": life_info if life_info and life_info.get("has_life") else {},
                "incl": float(raw.get("Incline", raw.get("Inclination", 0.0))),
            }
            planets.append(entry)

        elif dtype in ("moon","dwarfmoon") and export_moons:
            if dtype == "dwarfmoon" and not export_dwarf_moons:
                continue
            life_info = raw.get("_life_info", {})
            has_rings = bool(raw.get("Ring") or raw.get("Rings")) and export_rings
            entry = {
                "name":    obj.get("name", ""),
                "class":   cls,
                "se_class": cls.lower().replace(" ", "").replace("-", ""),
                "parent":  raw.get("ParentBody", ""),
                "has_rings": has_rings,
                "life_info": life_info if life_info and life_info.get("has_life") else {},
                "incl": float(raw.get("Incline", raw.get("Inclination", 0.0))),
                "is_dwarf": dtype == "dwarfmoon",
            }
            if dtype == "dwarfmoon":
                dwarf_moons.append(entry)
            else:
                moons.append(entry)

        elif dtype == "asteroid":
            ring_bodies.append(obj.get("name", ""))

        elif dtype == "comet" and export_comets:
            comets_list.append(obj.get("name", ""))

        if obj.get("is_barycenter"):
            bary_depth = max(bary_depth, 1)
        if obj.get("_bary_component_children"):
            bary_depth = max(bary_depth, 2)

    # System name fallback
    if not system_name and exported_entities:
        system_name = exported_entities[0].get("Name", "Unknown System")
    # Strip trailing body suffix like " A" or " Star"
    sn_parts = system_name.rsplit(" ", 1)
    if len(sn_parts) == 2 and len(sn_parts[1]) <= 2 and sn_parts[1].isalpha():
        system_name = sn_parts[0]

    # Life worlds
    life_worlds = [w for w in planets + (moons if export_moons else [])
                   if w.get("life_info", {}).get("has_life")]

    # Ringed world count — only real planetary rings when ring export is enabled
    # Exclude asteroid belt entities (names ending " Asteroid Belt") and
    # ring particles not starting with "@" from influencing the count.
    ringed = 0
    if export_rings:
        for w in planets + (moons if export_moons else []):
            if w.get("has_rings"):
                name_w = w.get("name", "")
                if not (name_w.endswith(" Asteroid Belt") or
                        ("Ring Particle" in name_w and not name_w.startswith("@"))):
                    ringed += 1

    # Binary terra pair
    terra_planets = [w for w in planets
                     if w.get("se_class", "") in ("terra","marine","ocean","aquaria","panthalassic")]
    binary_terra_pair = len(terra_planets) >= 2 and bary_depth >= 2

    # High-inclination
    hi_inc_worlds = sorted(
        [abs(w.get("incl", 0.0)) for w in planets + (moons if export_moons else [])
         if abs(w.get("incl", 0.0)) > 30],
        reverse=True,
    )

    # Nested moons (moons whose parent is also a moon)
    moon_names = {m["name"] for m in moons}
    nested = any(m.get("parent", "") in moon_names for m in moons + dwarf_moons)

    return {
        "system_name":         system_name,
        "star_count":          len(stars),
        "stars":               stars,
        "bary_depth":          bary_depth,
        "planet_count":        len(planets),
        "moon_count":          len(moons),
        "dwarf_moon_count":    len(dwarf_moons),
        "comet_count":         len(comets_list),
        "ring_particle_count": len(ring_bodies),
        "ringed_world_count":  ringed,
        "life_worlds":         life_worlds,
        "binary_terra_pair":   binary_terra_pair,
        "high_inclination_worlds": hi_inc_worlds,
        "has_nested_moons":    nested,
        "has_shepherd_moons":  False,  # geometric detection not yet implemented
        "export_moons":        export_moons,
        "export_rings":        export_rings,
        "export_dwarf_moons":  export_dwarf_moons,
        "export_comets":       export_comets,
    }


def generate_system_description(facts: dict, style: str = "wiki_short") -> str:
    """
    Generate a deterministic natural-language description from facts.
    Styles: "off", "one_sentence", "wiki_short", "wiki_long"
    """
    if style == "off":
        return "Converted from Space Engine."

    rng  = _rng(facts.get("system_name", "Unknown"))
    name = facts.get("system_name", "This system")
    stars   = facts.get("stars", [])
    planets = []  # not stored individually — use counts
    n_planets    = facts.get("planet_count", 0)
    n_moons      = facts.get("moon_count", 0)
    n_dwarf      = facts.get("dwarf_moon_count", 0)
    life_worlds  = facts.get("life_worlds", [])
    ringed       = facts.get("ringed_world_count", 0)
    bary_depth   = facts.get("bary_depth", 0)
    export_moons = facts.get("export_moons", True)
    export_rings = facts.get("export_rings", True)

    sys_type   = _system_type(stars, bary_depth)
    star_desc  = _star_phrase(stars)
    art        = "an" if sys_type[0] in "aeiou" else "a"

    # ── Opener ───────────────────────────────────────────────────────────────
    OPENERS = [
        f"{name} is {art} {sys_type} centered on {star_desc}.",
        f"{name} is {art} {sys_type} formed by {star_desc}.",
        f"{name} is {art} {sys_type} where {star_desc} host a diverse collection of worlds.",
    ]
    opener = rng.choice(OPENERS)

    if style == "one_sentence":
        # Build single-sentence summary
        parts = [opener.rstrip(".")]
        if n_planets > 0:
            parts.append(f" Its planetary family includes {_cardinal(n_planets)} major worlds")
        if life_worlds:
            parts.append(f", including {_life_worlds_phrase(life_worlds[:1])}")
        if ringed and export_rings:
            parts.append(f" and {_plural('ringed world', ringed)}")
        return "".join(parts) + "."

    # ── wiki_short ────────────────────────────────────────────────────────────
    sentences = [opener]

    # Planet family sentence
    if n_planets > 0:
        planet_intro = rng.choice([
            "Its planetary family includes",
            "The system hosts",
            "Among its major worlds are",
        ])
        family_parts = []
        if n_planets == 1:
            family_parts.append("a single major world")
        else:
            family_parts.append(f"{_cardinal(n_planets)} major worlds")
        # Note life worlds — use full list for correct plural
        if life_worlds:
            family_parts.append(f"including {_life_worlds_phrase(life_worlds)}")
        # Note ringed worlds only when rings were exported
        if ringed and export_rings:
            family_parts.append(f"{_plural('ringed world', ringed)}")
        sentences.append(f"{planet_intro} {', '.join(family_parts)}.")

    # Satellite sentence — "natural satellites", not "known moons"
    if export_moons and (n_moons > 0 or n_dwarf > 0):
        total_sats = n_moons + n_dwarf
        if total_sats == 1:
            sat_desc = "one natural satellite"
        else:
            sat_desc = f"{_cardinal(total_sats)} natural satellites"
        if facts.get("has_nested_moons"):
            sat_desc += ", including an unusual nested moon hierarchy"
        sentences.append(f"The system also includes {sat_desc}.")

    if style == "wiki_short":
        return " ".join(sentences)

    # ── wiki_long: add POI sentences ─────────────────────────────────────────
    poi = _poi_sentences(facts, rng)
    sentences.extend(poi)
    return " ".join(sentences)


def safe_generate_description(
    parsed_objects: dict,
    exported_entities: list,
    flags: dict,
    style: str = "wiki_short",
) -> str:
    """Wrapper that never raises — returns fallback on any error."""
    try:
        from constants import log_debug
        facts = build_system_description_facts(parsed_objects, exported_entities, flags)
        log_debug(
            f"[description-facts] stars={facts['star_count']} planets={facts['planet_count']} "
            f"moons={facts['moon_count']} life_worlds={len(facts['life_worlds'])} "
            f"ringed_worlds={facts['ringed_world_count']}",
            "DESCRIPTION",
        )
        desc = generate_system_description(facts, style=style)
        log_debug(f"[description] generated='{desc[:120]}{'...' if len(desc)>120 else ''}'", "DESCRIPTION")
        return desc
    except Exception as exc:
        try:
            from constants import log_debug
            log_debug(f"[description-warning] generation failed: {exc}; using fallback", "DESCRIPTION")
        except Exception:
            pass
        return "Converted from Space Engine."