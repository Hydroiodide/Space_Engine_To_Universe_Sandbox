# SE→US2 Converter

A graphical tool that converts Space Engine solar system export files (`.sc`) into Universe Sandbox 2 simulation files (`.ubox`).

The converter is designed to preserve planetary systems, orbital relationships, rings, moons, asteroids, and other supported objects as accurately as possible between the two programs. However, Space Engine and Universe Sandbox use different physics models, simulation methods, numerical solvers, and stability assumptions. As a result, systems that appear stable in Space Engine may evolve differently in Universe Sandbox over time. Long-term orbital drift, resonance changes, close encounters, ejections, collisions, and other emergent gravitational effects may occur after conversion, especially in densely populated or highly dynamic systems.

One of the goals of this converter is to make it possible to observe how a Space Engine system behaves under Universe Sandbox's physics simulation, including cases where the resulting system becomes chaotic, unstable, or evolves in unexpected ways.

---

## Features

- Converts stars, planets, moons, dwarf planets, dwarf moons, ring systems, asteroids, and comets
- Correct axial-tilt inheritance — rings and moons orbit in the planet's equatorial plane
- Gas giant cloud palettes matched to Space Engine surface presets and Sudarsky class
- Atmospheric scattering applied as a tint over existing cloud colours, not as a replacement
- Barycenter flattening with correct mass-ratio orbital scaling
- Configurable keep-limits for belt asteroids, ring particles, and comets
- Batch mode for converting an entire export folder at once
  
---

## Architecture

The project is split into five modules with a strict one-way dependency chain:

```
constants.py  ←  scanner.py  ←  builder.py  ←  converter.py  ←  main.py
```

| File | Responsibility |
|---|---|
| `constants.py` | Physical constants, colour palettes, SE↔US lookup tables, global logging. No dependencies on other project files. |
| `scanner.py` | `.sc` file parser, orbital element extraction, directory pre-scan, object filtering. |
| `builder.py` | Universe Sandbox JSON entity assembly — orbits, rotation quaternions, ring particles, atmosphere depots, back-export helpers. |
| `converter.py` | Body classification, barycenter hierarchy flattening, the main `convert_to_ubox` loop, and the US→SE back-export pipeline. |
| `main.py` | Tkinter graphical interface, button callbacks, and the application entry point. |

A sixth file, `globals_compat.py`, is a three-line shim that lets `builder.py` read UI flags (e.g. force-green for organic life) without importing `main.py`.

---

## Requirements

- Python 3.10 or later
- No third-party packages required — only the Python standard library

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

### Command line (headless)

Place one or more `.sc` files in the working directory and run:

```
python main.py
```

The script detects `.sc` files automatically and writes a `.ubox` next to each one.

To back-export a `.ubox` to `.sc`, place the `.ubox` in the working directory and run the same command.

---

## Building a standalone Windows executable

See the [Build from source](#build-from-source) section below.

---

## Build from source

### 1. Install PyInstaller

```
pip install pyinstaller
```

### 2. Compile

```
pyinstaller --onefile --noconsole --name "SE-US2-Converter" --add-data "globals_compat.py;." main.py
```

The compiled executable will be at `dist/SE-US2-Converter.exe`.

### 3. Optional — include a custom icon

```
pyinstaller --onefile --noconsole --name "SE-US2-Converter" --icon icon.ico --add-data "globals_compat.py;." main.py
```

---

## File formats

| Extension | Description |
|---|---|
| `.sc` | Space Engine solar system script — plain text, parsed by `scanner.py` |
| `.ubox` | Universe Sandbox 2 simulation — a ZIP archive containing JSON files |

---

## Known limitations

- Ring particle count is capped at 2000 per planet by default; raise the limit in the GUI if needed.
- Procedural textures and volumetric clouds from Space Engine have no direct equivalent in Universe Sandbox and are approximated by palette selection.
- Comet tails, nebulae, and galaxy objects are not converted.
- This project was developed with extensive assistance from AI tools. While the converter has been tested, some portions of the codebase, generated logic, documentation, comments, and this README may contain mistakes, inaccuracies, incomplete implementations, inefficient solutions, or AI-generated hallucinations.
- Users should verify conversion results before relying on them for scientific accuracy, simulation accuracy, or large-scale projects.
- If you encounter unexpected behavior, incorrect object properties, missing data, unusual visual results, or documentation inconsistencies, assume a converter bug is possible and report it with the source .sc file whenever possible.
Space Engine and Universe Sandbox use different simulation models, rendering systems, and object representations. Some information cannot be converted perfectly and may require manual adjustment after import.

---

## License

MIT — see [LICENSE](LICENSE).
