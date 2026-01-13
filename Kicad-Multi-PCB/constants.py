"""
Multi-Board PCB Manager - Constants
===================================

Central location for all magic numbers and configuration values.

Notes
-----
- CONFIG_VERSION: Version stored in the JSON config file.
- BLOCK_LIB_NAME / PORT_LIB_NAME: Project footprint libraries generated
  on the fly (.pretty folders in the project dir). This avoids touching
  KiCad's global libs and keeps projects portable.

Performance Knobs
-----------------
The plugin runs inside KiCad's GUI process. Heavy operations without care
cause "Not Responding" freezes. The manager uses caching and these constants
allow tuning the trade-offs.

Author: Eliot Abramo
License: MIT
"""

# =============================================================================
# Directory and File Names
# =============================================================================

BOARDS_DIR = "boards"
"""Subdirectory where all sub-board projects are created."""

CONFIG_FILE = ".kicad_multiboard.json"
"""Plugin configuration file name (hidden on Unix)."""

# =============================================================================
# Library Names
# =============================================================================

BLOCK_LIB_NAME = "MultiBoard_Blocks"
"""Footprint library for board block representations."""

PORT_LIB_NAME = "MultiBoard_Ports"
"""Footprint library for port marker footprints."""

# =============================================================================
# Configuration
# =============================================================================

CONFIG_VERSION = "12.0"
"""
Configuration file version.

Increment when making breaking changes to the JSON schema.
"""

# =============================================================================
# Default Values
# =============================================================================

DEFAULT_BLOCK_WIDTH = 50.0
"""Default width of generated block footprints in mm."""

DEFAULT_BLOCK_HEIGHT = 35.0
"""Default height of generated block footprints in mm."""

DEFAULT_PORT_POSITION = 0.5
"""Default port position along edge (0.0 to 1.0, 0.5 = center)."""

# =============================================================================
# File Patterns
# =============================================================================

TEMP_NETLIST_NAME = ".multiboard_netlist.xml"
"""Temporary netlist file name (cleaned up after use)."""

DEBUG_LOG_NAME = "multiboard_debug.log"
"""Debug log file name."""

# =============================================================================
# Performance Tuning
# =============================================================================

MAX_PARALLEL_FOOTPRINTS = 8
"""
Maximum footprints to process in parallel.

Note: pcbnew is not thread-safe, so this is only used for pure Python work.
"""

PACK_GRID_SPACING = 10.0
"""Grid spacing for component packing in mm."""

PACK_MAX_PER_ROW = 10
"""Maximum components per row when packing new footprints."""
