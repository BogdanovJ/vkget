#!/bin/sh
set -eu
microsocks -i 0.0.0.0 -p 1080 &
exec python3 /app/server.py
