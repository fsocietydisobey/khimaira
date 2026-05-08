"""DCE (Dead Code Eliminator) node factories."""

from chimera.nodes.deadcode.reaper import build_deadcode_reaper_node
from chimera.nodes.deadcode.seeker import build_deadcode_seeker_node
from chimera.nodes.deadcode.shatterer import build_deadcode_shatterer_node

__all__ = [
    "build_deadcode_seeker_node",
    "build_deadcode_shatterer_node",
    "build_deadcode_reaper_node",
]
