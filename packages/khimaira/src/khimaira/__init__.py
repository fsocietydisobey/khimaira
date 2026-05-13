"""khimaira — multi-model AI orchestration framework.

Three pillars: context resolver, runtime manager, AI dispatcher. Pure-CLI
substrate (no API SDK calls). See docs/ARCHITECTURE.md for the full map.
"""

try:
    # _version.py is generated at build time by hatch-vcs (PEP 440 dev
    # version like 0.1.0.dev42+gc22b2db on dev commits, clean
    # 0.2.0 on tagged releases). Present in installed wheels and after
    # `uv sync` in source checkouts.
    from khimaira._version import __version__
except ImportError:
    # Source-checkout fallback for environments where hatch-vcs hasn't
    # run yet (e.g. ad-hoc pytest from a fresh clone). Surfaces a
    # clearly-bogus version so callers know they're not on a real build.
    __version__ = "0.0.0+unknown"
