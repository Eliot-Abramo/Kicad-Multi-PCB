"""
Multi-Board PCB Manager - Constants and Configuration
======================================================

This module defines all constant values and configuration parameters
used throughout the plugin. Centralizing these values makes it easy
to modify behavior and maintain consistency.

Author: Eliot
License: MIT
"""

# =============================================================================
# Directory and File Names
# =============================================================================

# Sub-directory where all board-specific folders are created
# Each board gets its own folder inside this directory
BOARDS_DIR = "boards"

# Configuration file that stores project-wide multiboard settings
# Stored in the project root alongside .kicad_pro
CONFIG_FILE = ".kicad_multiboard.json"

# =============================================================================
# Library Names
# =============================================================================

# Footprint library for board block footprints
# These are visual representations of sub-boards that can be placed
# on a parent/assembly board to show physical board relationships
BLOCK_LIB_NAME = "MultiBoard_Blocks"

# Footprint library for port marker footprints
# Ports represent electrical connections between boards (connectors,
# flex cables, etc.) and are placed at board edges
PORT_LIB_NAME = "MultiBoard_Ports"

# =============================================================================
# Configuration Version
# =============================================================================

# Current configuration file version
# Used for migration if the config format changes in future versions
CONFIG_VERSION = "10.0"

# =============================================================================
# Default Values
# =============================================================================

# Default dimensions for block footprints (in mm)
DEFAULT_BLOCK_WIDTH = 50.0
DEFAULT_BLOCK_HEIGHT = 35.0

# Default port position (0.0 to 1.0 along edge)
DEFAULT_PORT_POSITION = 0.5

# =============================================================================
# File Patterns
# =============================================================================

# Temporary netlist file name (cleaned up after use)
TEMP_NETLIST_NAME = ".multiboard_netlist.xml"

# Debug log file name
DEBUG_LOG_NAME = "multiboard_debug.log"
