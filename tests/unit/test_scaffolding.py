"""Phase 0 smoke test: proves the package installs and imports cleanly.

Real coverage starts in Phase 1 (core data layer).
"""

import xpman


def test_package_importable():
    assert xpman is not None
