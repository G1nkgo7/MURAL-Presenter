"""Asset operations shared by the Image entry point."""

from .deck_core import (
    fetch_images,
    finalize_assets,
    material_figure,
    register_user_image,
)
from .image_background import (
    inspect_image,
    print_report as print_image_report,
    remove_checkerboard,
)

__all__ = [
    "fetch_images",
    "finalize_assets",
    "inspect_image",
    "material_figure",
    "print_image_report",
    "register_user_image",
    "remove_checkerboard",
]

