#!/usr/bin/env python3
"""
ARIS web GUI: AI chat with the ARIS console, saved signals, spectrum (one receiver or all overlaid), a floorplan
Map that shows where a transmitter probably is, an Activity page (patrol, captures, recent investigations) and a
Devices page (sensors, vehicles' tyre sensors, remotes, Wi-Fi and Bluetooth).

  Run:        python3 aris_gui.py                 (serves on 0.0.0.0:5000; ARIS_GUI_HOST=127.0.0.1 keeps it local)
  Offline:    on the computer itself open http://127.0.0.1:5000 (works with no network); from a phone with no
              Wi-Fi, use USB tethering and the address printed at startup.
  Login:      python3 aris_gui.py --set-password  (set or change the login)
  Extra:      python3 aris_gui.py --add-user NAME < hashfile   /   --remove-user NAME

The code lives in arisweb/ (next to this file). Login details live in $ARIS_DATA_DIR/gui_secrets.json (mode 600),
never in the source. Env: ARIS_DATA_DIR, ARIS_SCRIPT (default: the aris.py next to this file), ARIS_GUI_HOST,
ARIS_GUI_PORT
"""
import os
import sys

_HERE = os.path.dirname(os.path.realpath(__file__))     # follows a link to the checkout
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

if "--set-password" in sys.argv:                      # needs only werkzeug, works before anything else loads
    from arisweb.auth import cmd_set_password
    cmd_set_password()
    sys.exit(0)
for _flag in ("--add-user", "--remove-user"):          # extra logins: a password hash on stdin / a name
    if _flag in sys.argv:
        from arisweb import auth as _auth
        _i = sys.argv.index(_flag)
        _name = sys.argv[_i + 1] if _i + 1 < len(sys.argv) else ""
        (_auth.cmd_add_user if _flag == "--add-user" else _auth.cmd_remove_user)(_name)
        sys.exit(0)

try:
    from arisweb.app import main
except ImportError as e:
    if "arisweb" in str(e) or "arislib" in str(e):
        sys.exit(f"ARIS GUI files are missing next to {os.path.realpath(__file__)} ({e}).\n"
                 "Since v4.6 ARIS runs from its folder (git checkout). See deploy/install.sh.")
    raise

if __name__ == "__main__":
    main()
