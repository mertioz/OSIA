"""Console diagnostics that tolerate detached or closed output streams."""

import builtins
import sys


def diagnostic_print(*values, **kwargs) -> None:
    """Print normally, falling back to stderr if the output stream fails.

    Windowed Windows processes can retain an unusable console handle. Only
    diagnostic write/flush failures are handled here; runtime operations and
    their exceptions remain outside this boundary.
    """
    try:
        builtins.print(*values, **kwargs)
    except (OSError, ValueError):
        if sys.stderr is None or kwargs.get("file") is sys.stderr:
            return
        try:
            builtins.print(*values, **{**kwargs, "file": sys.stderr})
        except (OSError, ValueError):
            # Neither diagnostic sink is usable. Do not abort a Qt callback
            # or interrupt playback invalidation for an unavailable console.
            pass
