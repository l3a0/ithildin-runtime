"""The package that puts the modules together: entry points, the supervisor and the loop.

docs/architecture/05-building-blocks.md's `## Package layout and the import
rule` holds what may import what. Importing `runtime.loop` runs this file, so
it imports nothing but `core`, and today nothing at all.
"""
