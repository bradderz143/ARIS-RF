"""ARIS library: pieces that do not depend on the big engine file (aris.py).

  config     where ARIS keeps its data (same rules as the engine)
  rfpower    channel power from an IQ capture (numpy only; also used by remote nodes)
  nodes      the receivers ARIS can measure with: local dongles now, remote node agents later
  floorplan  floorplan images, their real size, and where each node sits on them
  store      locate sessions and the readings taken in them (SQLite)
  measure    take a power reading at one frequency on several nodes at the same time
  localize   turn readings at known positions into a heat map and a best-guess position

Nothing here transmits. Readings are signal strength only (metadata), never content.
"""
__all__ = ["config", "rfpower", "nodes", "floorplan", "store", "measure", "localize"]
