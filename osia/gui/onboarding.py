"""Compatibility entry point for the catalog-backed OSIA Guide slideshow."""

from osia.gui.guide_slideshow import GuideSlideshowDialog
from osia.gui.guide_visuals import _ScaledPixmapLabel


class OnboardingDialog(GuideSlideshowDialog):
    """Preserve the public onboarding API while using the v4 slideshow shell."""


__all__ = ["OnboardingDialog", "_ScaledPixmapLabel"]
