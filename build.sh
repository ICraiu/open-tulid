#!/usr/bin/env bash
set -euo pipefail

echo "Building open-tulid..."
if command -v uv &> /dev/null; then
    uv build
    echo ""
    echo "Running tests..."
    uv run pytest -v
else
    python -m build
    echo ""
    echo "Running tests..."
    python -m pytest -v
fi

echo ""
echo "Build complete!"
