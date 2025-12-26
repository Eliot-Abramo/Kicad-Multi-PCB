#!/bin/bash
# Multi-Board Manager Installation Script
# Automatically detects OS and installs to correct KiCad plugins directory

set -e

PLUGIN_NAME="multi_pcb_manager_v2"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "================================================"
echo "Multi-Board PCB Manager - Installation"
echo "================================================"

# Detect KiCad version
detect_kicad_version() {
    for ver in "9.0" "8.0" "7.0"; do
        if [ -d "$HOME/.local/share/kicad/$ver" ] || \
           [ -d "$HOME/Library/Preferences/kicad/$ver" ]; then
            echo "$ver"
            return
        fi
    done
    echo "8.0"  # Default
}

KICAD_VERSION=$(detect_kicad_version)
echo "Target KiCad version: $KICAD_VERSION"

# Detect OS and set plugin directory
case "$(uname -s)" in
    Linux*)
        PLUGIN_DIR="$HOME/.local/share/kicad/$KICAD_VERSION/scripting/plugins"
        ;;
    Darwin*)
        PLUGIN_DIR="$HOME/Library/Preferences/kicad/$KICAD_VERSION/scripting/plugins"
        ;;
    MINGW*|MSYS*|CYGWIN*)
        PLUGIN_DIR="$APPDATA/kicad/$KICAD_VERSION/scripting/plugins"
        ;;
    *)
        echo "Unknown OS. Please install manually."
        exit 1
        ;;
esac

echo "Plugin directory: $PLUGIN_DIR"

# Create directory if needed
mkdir -p "$PLUGIN_DIR"

# Remove old installation
if [ -d "$PLUGIN_DIR/$PLUGIN_NAME" ]; then
    echo "Removing previous installation..."
    rm -rf "$PLUGIN_DIR/$PLUGIN_NAME"
fi

# Copy plugin
cp -r "$SCRIPT_DIR" "$PLUGIN_DIR/$PLUGIN_NAME"

echo ""
echo "================================================"
echo "✓ Installation complete!"
echo "================================================"
echo ""
echo "Next steps:"
echo "  1. Open KiCad PCB Editor"
echo "  2. Tools → External Plugins → Refresh Plugins"
echo "  3. Tools → External Plugins → Multi-Board Manager"
echo ""
