# MURAL Studio — product Web UI

This is the reserved location of the interactive MURAL-Presenter Web UI. It is intentionally separate
from [`../../site/`](../../site/), which serves the public project page, blog, and paper preview.

Studio will eventually expose:

- project creation, user brief, audience, speaker intent, and material upload;
- deck blueprint, style system, page map, and slide-group responsibility;
- live lifecycle events and resumable run status;
- rendered slide canvas, contact sheet, group and whole-deck review findings;
- natural-language edits routed to page, group, evidence, or deck scope; and
- HTML, PPTX, PDF, and image export.

The browser must not hold model-provider credentials or import the Python runtime. It communicates
only with [`../../services/api/`](../../services/api/). Framework choice, persistence, authentication,
and collaborative editing remain open until the runnable implementation is frozen.
