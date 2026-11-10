"""The 863-870 MHz short-range-device band (Europe, Africa and much of Asia: ITU Region 1) as a named band.

A small example plug-in: after it loads, the chat understands "scan the 868 band" and "record the 868 band", band
captures cover it (in slices, like the 915 MHz band), and the Spectrum tab gets an "868 MHz" preset. Delete this file,
or add "ism_868" to the config's "plugins_disabled" list, if you have no use for it.
"""

ABOUT = "863-870 MHz short-range-device band: scans, band captures and a Spectrum preset"


def setup(aris):
    aris.add_band("868", 863.0, 870.0, "868 MHz band (863-870)")
