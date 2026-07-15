#!/bin/bash
# Build script for Render — installs Playwright + Chromium
pip install -r requirements.txt
python -m playwright install chromium
python -m playwright install-deps chromium
