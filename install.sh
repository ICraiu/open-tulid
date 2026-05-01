#!/usr/bin/env bash
set -euo pipefail

echo "Installing open-tulid..."
pip install -e .

echo ""
echo "Running initialization..."
tulid init

echo ""
echo "Installation complete!"
