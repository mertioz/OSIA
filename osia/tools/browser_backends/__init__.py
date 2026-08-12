"""Browser backend adapters.  Implementations stay isolated from each other."""

from osia.tools.browser_backends.isolated import IsolatedBrowserBackend
from osia.tools.browser_backends.personal import PersonalBrowserBackend

__all__ = ["IsolatedBrowserBackend", "PersonalBrowserBackend"]
