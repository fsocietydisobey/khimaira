"""DCE (Dead Code Eliminator) node factories."""

from khimaira.nodes.deadcode.reaper import build_deadcode_reaper_node
from khimaira.nodes.deadcode.seeker import build_deadcode_seeker_node
from khimaira.nodes.deadcode.shatterer import build_deadcode_shatterer_node

__all__ = [
    "build_deadcode_seeker_node",
    "build_deadcode_shatterer_node",
    "build_deadcode_reaper_node",
]
