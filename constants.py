"""
Multi-Board PCB Manager - Constants
===================================

This is the one place where the magic numbers live.

A couple of notes:
- `CONFIG_VERSION` is the version stored in the JSON config.
- `BLOCK_LIB_NAME` / `PORT_LIB_NAME` are project footprint libs we generate
  on the fly (`.pretty` folders in the project dir). This avoids touching KiCad’s
  global libs, and keeps projects portable.

Performance knobs
-----------------
The plugin has to run inside KiCad’s GUI process. If we do heavyweight stuff
without care, KiCad “Not Responding” happens. The manager uses caching + tries
to avoid work, and these constants let us tune the trade-offs.

Author: Eliot
License: MIT
"""

# =============================================================================
# Directory and File Names
# =============================================================================

BOARDS_DIR = "boards"
CONFIG_FILE = ".kicad_multiboard.json"

# =============================================================================
# Library Names
# =============================================================================

BLOCK_LIB_NAME = "MultiBoard_Blocks"
PORT_LIB_NAME = "MultiBoard_Ports"

# =============================================================================
# Configuration
# =============================================================================

CONFIG_VERSION = "10.2"

# =============================================================================
# Default Values
# =============================================================================

DEFAULT_BLOCK_WIDTH = 50.0
DEFAULT_BLOCK_HEIGHT = 35.0
DEFAULT_PORT_POSITION = 0.5

# =============================================================================
# File Patterns
# =============================================================================

TEMP_NETLIST_NAME = ".multiboard_netlist.xml"
DEBUG_LOG_NAME = "multiboard_debug.log"

# =============================================================================
# Performance Tuning
# =============================================================================

# Maximum footprints to load in parallel
MAX_PARALLEL_FOOTPRINTS = 8

# Grid spacing for component packing (mm)
PACK_GRID_SPACING = 10.0

# Maximum components per row when packing
PACK_MAX_PER_ROW = 10
