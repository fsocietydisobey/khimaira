"""Generator — produces structural documentation artifacts.

Takes analyzer output and writes barrel exports, CLAUDE.md skeletons,
dependency graphs, and symbol manifests. Preserves human-written content
on updates by respecting <!-- BEGIN MANUAL --> / <!-- END MANUAL --> markers.
"""
