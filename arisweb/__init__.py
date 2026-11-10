"""ARIS web GUI (Flask). Start it with aris_gui.py.

  app.py       the Flask app: login, pages, chat API, security headers
  auth.py      login secrets (gui_secrets.json, mode 600) and failed-login lockout
  bridge.py    the AI chat: runs the aris.py console on a pseudo-terminal and relays questions
  engine.py    loads aris.py once inside the GUI process (spectrum, activity and map use it directly)
  spectrum.py  Spectrum tab API
  activity.py  Activity and Signals tab APIs
  mapapi.py    Map tab API: floorplans, nodes, locate sessions, calibration
  templates/   page skeletons;  static/  CSS and JavaScript (no build step)
"""
