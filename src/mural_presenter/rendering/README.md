# Rendering and export

Reserved for deterministic HTML rendering and delivery adapters.

Expected capabilities include browser capture, per-slide images, contact sheets, overflow and font
diagnostics, print/PDF output, PPTX conversion, image export, and fidelity reports. Rendering produces
inspection evidence; it does not decide semantic acceptance on its own.

Every render should record viewport, browser/runtime version, font and asset resolution status,
source revision, and output checksums so visual QC can be reproduced.
