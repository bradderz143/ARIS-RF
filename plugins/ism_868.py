"""The 863-870 MHz short-range-device band as a named band, everywhere.

A small example plug-in. The region data already gives Europe this band (with its own Spectrum chip); in other
regions 863-870 MHz is used for other things (in the Americas, trunked land-mobile radio), so this plug-in adds
only the name: after it loads, "scan the 868 band" and "record the 868 band" work in any region, for a station
near a region's edge or a device brought from elsewhere. Passing preset=True to add_band would also add a Spectrum
chip. Delete this file, or add "ism_868" to the config's "plugins_disabled" list, if you have no use for it.
"""

ABOUT = "863-870 MHz short-range-device band: scans and band captures understand 'the 868 band'"


def setup(aris):
    aris.add_band("868", 863.0, 870.0, "868 MHz band (863-870)", preset=False)
