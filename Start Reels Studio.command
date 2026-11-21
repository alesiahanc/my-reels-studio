#!/bin/bash
# Double-click to start Reels Studio. Keep the window open while you use it.
cd "$(dirname "$0")"
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "FFmpeg (the video engine) is not installed yet."
  if command -v brew >/dev/null 2>&1; then
    read -p "Install it now with Homebrew? [Y/n] " ans
    if [[ "$ans" != "n" && "$ans" != "N" ]]; then brew install ffmpeg; fi
  else
    echo "First install Homebrew from https://brew.sh, then run:  brew install ffmpeg"
    read -p "Press Enter to close."; exit 1
  fi
fi

if ! python3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)" 2>/dev/null; then
  echo "Python 3 is missing. Run:  brew install python   (or accept the macOS prompt to install developer tools)"
  read -p "Press Enter to close."; exit 1
fi

python3 server.py
