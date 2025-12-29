# Multi-Board PCB Manager

A KiCad plugin for managing multiple PCBs from a single schematic.

## What This Does

If you've ever designed a product that uses multiple PCBs (power supply + main board, or a modular system), you know the pain: maintaining separate schematics that need to stay in sync, or manually splitting a combined schematic. This plugin solves that.

**One schematic. Multiple PCBs. Zero sync headaches.**

You design your complete system in a single schematic, then assign components to different boards. Each board gets its own PCB with independent stackup, design rules, and manufacturing outputs.

## Features

- **Shared Schematic**: Edit the schematic anywhere, changes appear everywhere
- **Component Assignment**: Update to pull assigned components into each board
- **Inter-Board Ports**: Define connector-to-connector relationships between boards
- **Block Footprints**: Place visual representations of sub-boards on assembly drawings
- **DRC Checking**: Run design rule checks across all boards
- **Cross-Probing**: Click schematic symbols, jump to PCB footprints (works normally)

## Requirements

- KiCad 9.0 or later
- kicad-cli (ships with KiCad)

## Installation

Copy the `multiboard_manager` folder to your KiCad scripting plugins directory:

| OS | Path |
|---|---|
| Windows | `%APPDATA%\kicad\9.0\scripting\plugins\` |
| Linux | `~/.local/share/kicad/9.0/scripting/plugins/` |
| macOS | `~/Library/Application Support/kicad/9.0/scripting/plugins/` |

Restart KiCad. The plugin appears under **Tools → External Plugins → Multi-Board Manager**.

## Quick Start

1. Open your project's main PCB in KiCad
2. Launch **Multi-Board Manager** from the External Plugins menu
3. Click **New** to create a sub-board (e.g., "PowerSupply")
4. Double-click the board to open it in a new PCB editor window
5. In the sub-board's PCB editor, place components as normal
6. Back in the manager, click **Update** to sync from schematic
7. Repeat for additional boards

### Component Assignment

Components are assigned to boards implicitly:

- If a component's footprint is already placed on a board, it stays there
- Components with `DNP=Yes` or `Exclude from Board=Yes` are skipped
- Unplaced components can be added to any board via Update

To move a component between boards:
1. Delete the footprint from its current board
2. Run Update on the target board

### Inter-Board Ports

For connectivity checking between boards (e.g., verifying that POWER_OUT on the PSU connects to POWER_IN on the main board):

1. Select a board and click **Ports**
2. Add ports for signals that cross board boundaries
3. Set the net name each port connects to
4. Run **Check** to verify connections

## How Schematic Sync Works

When you create a sub-board, the plugin creates a [hardlink](https://en.wikipedia.org/wiki/Hard_link) from the sub-board folder to your root schematic files.

```
project/
├── MyProject.kicad_sch          ─┐
├── MyProject.kicad_pcb           │  Root project
├── MyProject.kicad_pro           │
├── .kicad_multiboard.json       ─┘  Plugin config
│
└── boards/
    └── PowerSupply/
        ├── PowerSupply.kicad_sch   ← Hardlink to MyProject.kicad_sch
        ├── PowerSupply.kicad_pcb   ← Separate PCB file
        └── PowerSupply.kicad_pro
```

A hardlink makes two filenames point to the same file on disk. Edit `MyProject.kicad_sch` or `boards/PowerSupply/PowerSupply.kicad_sch`—doesn't matter, they're the same file.

**Caveat**: If you have the schematic open in two KiCad windows simultaneously, each window keeps its own copy in memory. Close and reopen to see changes made elsewhere. This isn't a plugin limitation—it's how file-based applications work.

## Project Structure

After creating boards, your project will look like:

```
project/
├── MyProject.kicad_sch
├── MyProject.kicad_pcb
├── MyProject.kicad_pro
├── fp-lib-table
├── sym-lib-table
├── .kicad_multiboard.json
├── MultiBoard_Blocks.pretty/      ← Generated block footprints
│   ├── Block_PowerSupply.kicad_mod
│   └── Block_MainBoard.kicad_mod
│
└── boards/
    ├── PowerSupply/
    │   ├── PowerSupply.kicad_sch  ← Hardlink
    │   ├── PowerSupply.kicad_pcb
    │   ├── PowerSupply.kicad_pro
    │   ├── fp-lib-table           ← Path-adjusted copy
    │   └── sym-lib-table          ← Path-adjusted copy
    │
    └── MainBoard/
        └── ...
```

## Workflow Tips

**Keep the root PCB for assembly**

Use the root PCB file as an assembly drawing. Place the generated block footprints (from `MultiBoard_Blocks.pretty`) to show board-to-board relationships.

**Use hierarchical sheets**

If your schematic uses hierarchical sheets, they're automatically included. Each sub-board sees the complete hierarchy.

**Manufacturing outputs**

Generate Gerbers/drill files from each sub-board's PCB independently. They're fully standalone KiCad projects.

**Version control**

The hardlinks work fine with Git. The schematic appears as one file to Git regardless of how many boards reference it.

## Configuration File

The `.kicad_multiboard.json` file stores board definitions:

```json
{
  "version": "10.0",
  "root_schematic": "MyProject.kicad_sch",
  "root_pcb": "MyProject.kicad_pcb",
  "boards": {
    "PowerSupply": {
      "name": "PowerSupply",
      "pcb_path": "boards/PowerSupply/PowerSupply.kicad_pcb",
      "description": "5V/3.3V regulator module",
      "block_width": 50.0,
      "block_height": 35.0,
      "ports": {
        "VIN": {"name": "VIN", "net": "+12V", "side": "left", "position": 0.3},
        "VOUT": {"name": "VOUT", "net": "+5V", "side": "right", "position": 0.5}
      }
    }
  }
}
```

## Troubleshooting

**"kicad-cli not found"**

The plugin needs `kicad-cli` for netlist export. It should be in your PATH if you installed KiCad normally. On Windows, the plugin also checks standard installation paths.

**Components not appearing after Update**

- Check if the component has `DNP=Yes` or `Exclude from Board=Yes`
- Check if the component is already placed on another board
- Verify the component has a footprint assigned in the schematic

**Schematic changes not visible**

If you're editing the schematic in one window and have a sub-board's schematic open in another, you need to close and reopen to see changes. This is standard application behavior for file-based documents.

**Hardlinks failing**

On some network drives or unusual filesystems, hardlinks may not work. The plugin falls back to symlinks. If both fail, you'll need to ensure your project is on a local drive.

## License

MIT License. See LICENSE file.

## Contributing

Issues and PRs welcome. The code is organized as:

- `__init__.py` - Plugin registration and entry point
- `constants.py` - Configuration values
- `config.py` - Data models (PortDef, BoardConfig, ProjectConfig)
- `manager.py` - Core logic (MultiBoardManager)
- `dialogs.py` - wxPython UI components
