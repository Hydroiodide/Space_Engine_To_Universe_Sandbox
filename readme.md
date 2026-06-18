# SE→US2 Converter

A graphical tool that converts Space Engine solar system export files (`.sc`) into Universe Sandbox 2 simulation files (`.ubox`).

Space Engine and Universe Sandbox use different physics models, simulation methods, and numerical solvers. Systems that appear stable in Space Engine may evolve differently in Universe Sandbox over time. Long-term orbital drift, close encounters, ejections, and collisions may occur, especially in densely populated or highly dynamic systems. This is expected behavior.

---

## Features

- Converts stars, planets, moons, dwarf planets, dwarf moons, ring systems, asteroids, and comets
- Binary and multiple-star systems exported with suspended barycenter helper bodies; stars, planets, and moons remain free N-body objects with real positions and velocities
- Correct axial-tilt inheritance — rings and moons orbit in the planet's equatorial plane
- Gas giant cloud palettes matched to Space Engine surface presets and Sudarsky class
<<<<<<< HEAD
- Atmospheric scattering applied as a tint over existing cloud colours
- Barycenter flattening with correct mass-ratio orbital scaling
- Configurable keep-limits for belt asteroids, ring particles, and comets
- Batch mode for converting an entire export folder at once
- **Surface data generation** — procedural temperature, albedo, vapour pressure, and heat capacity maps written as a Universe Sandbox-compatible surface atlas
- Surface atlas layout scales automatically with body count (1–256 bodies)
- All Space Engine `Surface{}` parameters normalised to correct physical ranges before generation
- Debug PNG export for visual verification of generated maps before packaging
=======
- Atmospheric color derived from Space Engine `Atmosphere.Model`, `Hue`, and `Saturation` using per-model color tables, not generic HSV conversion
- Configurable keep-limits for belt asteroids, ring particles, and comets
- Batch mode for converting an entire export folder at once
- **Procedural surface maps** — temperature, albedo, vapour pressure, and heat capacity maps written as a Universe Sandbox-compatible surface atlas
- Surface atlas layout scales automatically with body count (1–256 bodies)
- Debug PNG export for visual verification of generated maps
>>>>>>> 6c2c4b4 (Big Update)

---

## Architecture

<<<<<<< HEAD
The project is split into six modules with a strict one-way dependency chain:

=======
>>>>>>> 6c2c4b4 (Big Update)
```
constants.py  ←  scanner.py  ←  builder.py  ←  converter.py  ←  surface_generator.py  ←  main.py
```

| File | Responsibility |
|---|---|
<<<<<<< HEAD
| `constants.py` | Physical constants, colour palettes, SE↔US lookup tables, global logging. No dependencies on other project files. |
| `scanner.py` | `.sc` file parser, orbital element extraction, directory pre-scan, object filtering. |
| `builder.py` | Universe Sandbox JSON entity assembly — orbits, rotation quaternions, ring particles, atmosphere depots, `SurfaceGridComponent` wiring. |
| `converter.py` | Body classification, barycenter hierarchy flattening, the main `convert_to_ubox` loop, manifest generation, and validation. |
| `surface_generator.py` | Procedural planetary map generation and surface atlas packing. Reads SE `Surface{}` parameters and writes `data.surface`, `material0–3.surface`, and `info` into a ZIP archive. |
| `main.py` | Tkinter graphical interface, button callbacks, and the application entry point. |

A seventh file, `globals_compat.py`, is a three-line shim that lets `builder.py` read UI flags (e.g. force-green for organic life) without importing `main.py`.
=======
| `constants.py` | Physical constants, colour palettes, SE↔US lookup tables, global logging |
| `scanner.py` | `.sc` file parser, orbital element extraction, directory pre-scan, object filtering |
| `builder.py` | Universe Sandbox JSON entity assembly — orbits, rotation, ring particles, atmosphere depots, `SurfaceGridComponent` |
| `converter.py` | Body classification, barycenter hierarchy, the main conversion loop, manifest generation, and validation |
| `surface_generator.py` | Procedural planetary map generation and surface atlas packing |
| `main.py` | Tkinter graphical interface and application entry point |
| `globals_compat.py` | Thin shim so `builder.py` can read runtime UI flags without importing `main.py` |
| `description_generator.py` | Deterministic procedural system description generator |
>>>>>>> 6c2c4b4 (Big Update)

---

## Requirements

- Python 3.10 or later
- [NumPy](https://numpy.org/) — required for surface map generation
- [Pillow](https://python-pillow.org/) — required for debug PNG export

<<<<<<< HEAD
Install dependencies:

=======
>>>>>>> 6c2c4b4 (Big Update)
```
pip install numpy pillow
```

---

## Usage

### Graphical interface

```
python main.py
```

1. Click **Select .sc File** to pick a single export, or **Select Folder (Batch)** to process a whole directory.
2. Adjust keep-limits for asteroid belts, ring particles, and comets using percentage (`25%`) or exact count (`500`).
3. Toggle exports for moons, dwarf moons, dwarf planets, rings, and comets with the checkboxes.
4. Set the output folder or leave it at the detected Universe Sandbox Simulations directory.
5. Click **Convert**.

<<<<<<< HEAD
The log window shows progress in real time. Enable **Debug Logging** for verbose output.
=======
Exported simulations start paused by default at 1 simulated hour per real second. Both values can be changed in the advanced settings before export.
>>>>>>> 6c2c4b4 (Big Update)

---

## Build a standalone Windows executable

<<<<<<< HEAD
### 1. Install PyInstaller

```
pip install pyinstaller
```

### 2. Compile

```
pyinstaller --onefile --noconsole --name "SE-US2-Converter" --hidden-import numpy --hidden-import numpy.core --hidden-import PIL --hidden-import PIL.Image --hidden-import tkinter --hidden-import tkinter.ttk --hidden-import tkinter.filedialog --hidden-import tkinter.messagebox --collect-all numpy main.py
```

The compiled executable will be at `dist/SE-US2-Converter.exe`.

### 3. Optional — include a custom icon

Add `--icon icon.ico` to the command above.
=======
```powershell
python -m pip install pyinstaller numpy
python -m PyInstaller --noconfirm --clean --onefile --windowed --name "SE-US2-Converter" main.py
```

Output: `dist\SE-US2-Converter.exe`
>>>>>>> 6c2c4b4 (Big Update)

---

## File formats

| Extension | Description |
|---|---|
| `.sc` | Space Engine solar system script — plain text, parsed by `scanner.py` |
| `.ubox` | Universe Sandbox 2 simulation — a ZIP archive containing JSON and binary surface data |

### Surface archive format

<<<<<<< HEAD
The surface archive (`simulation-<name>-surface.zip`) contains:

| File | Content |
|---|---|
| `info` | `{"size": 512}` — atlas dimensions hint |
| `data.surface` | Float32 RGBA atlas. ch0 = surface temperature (K), ch1 = diffuse albedo (0–1) |
| `material0.surface` | ch0 = terrain mask, ch1 = vapour pressure (Pa), ch2 = secondary mask, ch3 = continuous mask |
| `material1.surface` | ch1 = rock heat capacity (J/m²K) |
| `material2.surface` | ch1 = water heat capacity (mat1 ÷ 3.728) |
| `material3.surface` | ch1 = atmosphere heat capacity (mat1 ÷ 83.60) |

Atlas layout scales with body count:
=======
| File | Content |
|---|---|
| `info` | `{"size": 512}` — atlas dimensions hint |
| `data.surface` | Float32 RGBA atlas. ch0 = surface temperature (K), ch1 = diffuse albedo |
| `material0.surface` | ch0 = terrain mask, ch1 = vapour pressure (Pa), ch2 = ice mask, ch3 = liquid mask |
| `material1.surface` | ch1 = rock heat capacity (J/m²K) |
| `material2.surface` | ch1 = water heat capacity |
| `material3.surface` | ch1 = atmosphere heat capacity |

Atlas layout by body count:
>>>>>>> 6c2c4b4 (Big Update)

| Bodies | Body resolution | Grid |
|---|---|---|
| 1 | 1024 × 512 | 1 × 1 |
| 2 | 1024 × 256 | 1 × 2 |
| 4 | 512 × 256 | 2 × 2 |
| 8 | 512 × 128 | 2 × 4 |
| 16 | 256 × 128 | 4 × 4 |
| 32 | 256 × 64 | 4 × 8 |
| 64 | 128 × 64 | 8 × 8 |
| 128 | 128 × 32 | 8 × 16 |
| 256 | 64 × 32 | 16 × 16 |
<<<<<<< HEAD
=======

---

## Conversion notes

### Barycenters

Binary and multiple-star systems are exported using suspended barycenter helper bodies. The actual stars, planets, and moons remain free N-body objects and carry real Cartesian position and velocity vectors. Barycenters exist only to preserve the hierarchical grouping in Universe Sandbox's interface.

### Depots and bulk composition

- **Stars** export only Hydrogen (~74%) and Helium (~26%) bulk depots. Space Engine `Atmosphere` blocks on stars are ignored for depot purposes.
- **Earth/Terra-like surface oceans** are capped to a mass-scaled Earth ocean mass so that large `Ocean.Depth` values do not create four-times-Earth-ocean exports.
- **Aquaria, Ocean, Marine, Panthalassic worlds** preserve large bulk Water depots (~45% of body mass) instead of being treated as dry rocky planets.
- **N₂/CH₄-dominated Titan-like bodies** use a solid/icy bulk fallback (Iron + Silicate) instead of scaling atmosphere percentages to bulk planet mass, which would produce non-physical Nitrogen masses.
- **Gas giants** preserve H/He-dominant bulk composition.

### Atmosphere safety

By default, imported Space Engine atmospheres use `Celestial.AtmosphereMass` derived from SE `Pressure`, with no active volatile depots. Surface gas pressure channels are disabled. This prevents Universe Sandbox from rewriting atmosphere mass through phase chemistry over simulation time.

Advanced options allow carrier-gas depot modes and passive/active surface grid modes for testing, but these can cause pressure drift and are not the default.

### Rings and asteroid belts

- Asteroid belt particles are named `{Star} Asteroid Belt`.
- Real planetary ring particles use the `@{Planet} Ring Particle` naming pattern.
- Ring particle limits below 100% no longer cause duplicate entity ID errors — the full generated ID range is always reserved.
- When **Export Rings** is disabled, the system description will not mention ringed worlds.

### Validation

Before writing the `.ubox` file, the converter validates:

- Entity IDs are unique and complete
- Barycenter component references point to emitted entities
- Surface archive tiles match the manifest
- Atmosphere and depot consistency
- Surface active-physics flags

Validation errors stop export and are shown in the log window.
>>>>>>> 6c2c4b4 (Big Update)

---

## Known limitations

- Space Engine and Universe Sandbox use different physics and rendering systems. Converted systems are not guaranteed to remain stable or visually identical over simulation time.
- Ring particle count defaults to 2000 per planet; raise the limit in the GUI if needed.
- Procedural textures and volumetric clouds from Space Engine have no direct equivalent in Universe Sandbox and are approximated by palette selection.
- Comet tails, nebulae, and galaxy objects are not converted.
<<<<<<< HEAD
- Surface maps are procedurally generated from Space Engine `Surface{}` parameters, not exported from Space Engine directly. Visual results approximate the source world but will not be pixel-accurate.
- Gas, ice thickness, liquid depth, and temperature gradient views in Universe Sandbox are driven by its own physics simulation using the surface atlas as initial conditions. Results depend on the simulation state and may differ from Space Engine's rendering.
- This project was developed with extensive assistance from AI tools. Some portions of the codebase, generated logic, and documentation may contain mistakes, inaccuracies, or incomplete implementations. Verify conversion results before relying on them for scientific or large-scale use. Report bugs with the source `.sc` file whenever possible.
=======
- Surface maps are procedurally generated from Space Engine `Surface{}` parameters, not exported from Space Engine directly.
- Active legacy surface physics (non-default) can change pressure and temperature over time as Universe Sandbox runs its own climate simulation.
- Very large systems may be slow to simulate in Universe Sandbox depending on hardware.
- This project was developed with extensive AI assistance. Some portions may contain mistakes or incomplete implementations. Verify conversion results and report bugs with the source `.sc` file when possible.
>>>>>>> 6c2c4b4 (Big Update)

---

## License

MIT — see [LICENSE](LICENSE).