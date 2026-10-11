"""Entry point of the PyInstaller build (phonemic.exe).

Started without arguments it opens the desktop window; with arguments it runs
the command-line version, e.g. ``phonemic.exe --list-devices``. The exe has no
console, so use ``--log-file`` to see command-line output.
"""

import multiprocessing
import sys

from phonemic.__main__ import main as cli_main
from phonemic.gui import main as gui_main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(cli_main() if len(sys.argv) > 1 else gui_main([]))
