"""Compatibility entry point for the desktop Settings guide."""

from osia.gui.guide_slideshow import GuideSlideshowDialog
from osia.gui.guide_visuals import _ScaledPixmapLabel


class OnboardingDialog(GuideSlideshowDialog):
    """Expose the guide through the desktop onboarding API."""


__all__ = ["OnboardingDialog", "_ScaledPixmapLabel"]
