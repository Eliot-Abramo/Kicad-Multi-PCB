# Multi-Board PCB Manager

A KiCad 9+ plugin for managing multiple PCBs from a single schematic.

## Overview

Design your complete system in one schematic, then split it across multiple PCBs. Each board gets independent layout, stackup, and manufacturing outputs while staying synchronized with the shared schematic.

## Features

- **Shared Schematic**: One schematic, multiple PCBs. Edit once, reflected everywhere.
- **Component Assignment**: Place components on specific boards during update.
- **Native Packing**: New components placed in grid; use KiCad's 'P' key to pack optimally.
- **Inter-Board Ports**: Define and verify connections between boards.
- **Block Footprints**: Visual board representations for assembly views.
- **DRC Integration**: Run design checks across all boards.
- **Hierarchy View**: See all boards from any sub-PCB with current board highlighted.

## Installation

Copy the `multiboard_manager` folder to:

| OS | Path |
|---|---|
| Windows | `%APPDATA%\kicad\9.0\scripting\plugins\` |
| Linux | `~/.local/share/kicad/9.0/scripting/plugins/` |
| macOS | `~/Library/Application Support/kicad/9.0/scripting/plugins/` |

Restart KiCad. Access via **Tools → External Plugins → Multi-Board Manager**.

## Quick Start

1. Open your main project PCB
2. Launch Multi-Board Manager from External Plugins
3. Click **New** to create a sub-board
4. Double-click to open the board project in KiCad
5. Click **Update** to sync components from the schematic

## How It Works

### Shared Schematic

When you create a sub-board, the plugin creates a **hardlink** to your root schematic. Both paths point to the same physical file—edit either one, you're editing the same data.

```
project.kicad_sch          ──┬──► Same file on disk
boards/PSU/PSU.kicad_sch   ──┘
```

### Component Assignment

Components are assigned to boards implicitly:

- Components already on a board stay there
- Components marked `DNP` or `Exclude from Board` are skipped
- Remaining components go to whichever board you Update first

To move a component: delete it from the current board, then Update the target board.

### Inter-Board Ports

For systems with electrical connections between boards:

1. Select a board → **Ports**
2. Add ports for signals crossing board boundaries
3. Set the net name each port connects to
4. Run **Check All** to verify connectivity

## Project Structure

```
MyProject/
├── MyProject.kicad_sch        ← Root schematic (edit here)
├── MyProject.kicad_pcb        ← Root PCB (optional assembly view)
├── .kicad_multiboard.json     ← Plugin config
├── MultiBoard_Blocks.pretty/  ← Generated block footprints
│
└── boards/
    └── PowerSupply/
        ├── PowerSupply.kicad_sch  ← Hardlink (same file as root)
        ├── PowerSupply.kicad_pcb  ← Independent PCB
        └── PowerSupply.kicad_pro
```

## Troubleshooting

**kicad-cli not found**

Ensure KiCad's bin directory is in your PATH, or the plugin will search standard installation locations.

**Components not appearing**

- Check for `DNP=Yes` or `Exclude from Board=Yes` properties
- Component may already be on another board
- Verify a footprint is assigned in the schematic

**Schematic changes not visible in another window**

If you have the same schematic open in two KiCad windows, each caches its own copy in memory. Close and reopen to see changes. This is standard application behavior.

## License

MIT
