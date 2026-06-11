# SE→US2 Converter

A graphical tool that converts Space Engine solar system export files (`.sc`) into Universe Sandbox 2 simulation files (`.ubox`).

The converter is designed to preserve planetary systems, orbital relationships, rings, moons, asteroids, and other supported objects as accurately as possible between the two programs. However, Space Engine and Universe Sandbox use different physics models, simulation methods, numerical solvers, and stability assumptions. As a result, systems that appear stable in Space Engine may evolve differently in Universe Sandbox over time. Long-term orbital drift, resonance changes, close encounters, ejections, collisions, and other emergent gravitational effects may occur after conversion, especially in densely populated or highly dynamic systems.

One of the goals of this converter is to make it possible to observe how a Space Engine system behaves under Universe Sandbox's physics simulation, including cases where the resulting system becomes chaotic, unstable, or evolves in unexpected ways.

---

## Features

- Converts stars, planets, moons, dwarf planets, dwarf moons, ring systems, asteroids, and comets
- Correct axial-tilt inheritance — rings and moons orbit in the planet's equatorial plane
- Gas giant cloud palettes matched to Space Engine surface presets and Sudarsky class
- Atmospheric scattering applied as a tint over existing cloud colours
- Barycenter flattening with correct mass-ratio orbital scaling
- Configurable keep-limits for belt asteroids, ring particles, and comets
- Batch mode for converting an entire export folder at once
- **Surface data generation** — procedural temperature, albedo, vapour pressure, and heat capacity maps written as a Universe Sandbox-compatible surface atlas
- Surface atlas layout scales automatically with body count (1–256 bodies)
- All Space Engine `Surface{}` parameters normalised to correct physical ranges before generation
- Debug PNG export for visual verification of generated maps before packaging

---

## Architecture

The project is split into six modules with a strict one-way dependency chain:

```
constants.py  ←  scanner.py  ←  builder.py  ←  converter.py  ←  surface_generator.py  ←  main.py
```

| File | Responsibility |
|---|---|
| `constants.py` | Physical constants, colour palettes, SE↔US lookup tables, global logging. No dependencies on other project files. |
| `scanner.py` | `.sc` file parser, orbital element extraction, directory pre-scan, object filtering. |
| `builder.py` | Universe Sandbox JSON entity assembly — orbits, rotation quaternions, ring particles, atmosphere depots, `SurfaceGridComponent` wiring. |
| `converter.py` | Body classification, barycenter hierarchy flattening, the main `convert_to_ubox` loop, manifest generation, and validation. |
| `surface_generator.py` | Procedural planetary map generation and surface atlas packing. Reads SE `Surface{}` parameters and writes `data.surface`, `material0–3.surface`, and `info` into a ZIP archive. |
| `main.py` | Tkinter graphical interface, button callbacks, and the application entry point. |

A seventh file, `globals_compat.py`, is a three-line shim that lets `builder.py` read UI flags (e.g. force-green for organic life) without importing `main.py`.

---

## Requirements

- Python 3.10 or later
- [NumPy](https://numpy.org/) — required for surface map generation
- [Pillow](https://python-pillow.org/) — required for debug PNG export

Install dependencies:

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

The log window shows progress in real time. Enable **Debug Logging** for verbose output.

---

## Building a standalone Windows executable

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

---

## File formats

| Extension | Description |
|---|---|
| `.sc` | Space Engine solar system script — plain text, parsed by `scanner.py` |
| `.ubox` | Universe Sandbox 2 simulation — a ZIP archive containing JSON and binary surface data |

### Surface archive format

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

---

## Known limitations

- Ring particle count is capped at 2000 per planet by default; raise the limit in the GUI if needed.
- Procedural textures and volumetric clouds from Space Engine have no direct equivalent in Universe Sandbox and are approximated by palette selection.
- Comet tails, nebulae, and galaxy objects are not converted.
- Surface maps are procedurally generated from Space Engine `Surface{}` parameters, not exported from Space Engine directly. Visual results approximate the source world but will not be pixel-accurate.
- Gas, ice thickness, liquid depth, and temperature gradient views in Universe Sandbox are driven by its own physics simulation using the surface atlas as initial conditions. Results depend on the simulation state and may differ from Space Engine's rendering.
- This project was developed with extensive assistance from AI tools. Some portions of the codebase, generated logic, and documentation may contain mistakes, inaccuracies, or incomplete implementations. Verify conversion results before relying on them for scientific or large-scale use. Report bugs with the source `.sc` file whenever possible.

---

## License

MIT — see [LICENSE](LICENSE).
