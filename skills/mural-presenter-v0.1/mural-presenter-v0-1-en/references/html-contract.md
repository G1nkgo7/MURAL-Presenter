# HTML contract

`scaffold-from-plans` creates one light fragment per page. Slide fills the area
between the markers and may add page-scoped CSS, but preserves the shared
structure and locked copy.

## Ordinary content page

```html
<section class="slide frame-content page-process family-arch-diagram canvas-variant--base"
  id="slide-02" data-slide="02" data-page-type="process"
  data-frame="content" data-page-family="arch-diagram"
  data-canvas-variant="base">
  <div class="slide-inner">
    <div class="page-frame page-frame--content">
      <header class="page-header page-header--content"
        data-scaffold-owned="header">...</header>
      <main class="page-body">
        <div class="content-stage" data-slide-owned="body">
          <!-- SLIDE_AGENT_FILL_START -->
          ...
          <!-- SLIDE_AGENT_FILL_END -->
        </div>
      </main>
      <footer class="page-footer" data-scaffold-owned="footer">...</footer>
    </div>
  </div>
</section>
```

Content title/header position, safe area, and footer are shared. Slide owns the
body and local visual expression. Page-local CSS does not repaint `.slide`,
`.slide-inner`, or `.page-frame`. A planned non-base `canvas_variant` may target
that exact root class.

## Cover, divider, or closing

```html
<section class="slide special-canvas special-cover page-cover family-image-hero layout-poster"
  id="slide-01" data-slide="01" data-page-type="cover"
  data-frame="special" data-page-family="image-hero"
  data-special-layout="poster"
  data-canvas-variant="special">
  <div class="special-background" data-slide-owned="background"></div>
  <div class="special-overlay" data-slide-owned="overlay"></div>
  <div class="special-safe">
    <header class="special-header" data-scaffold-owned="header">...</header>
    <main class="special-body">
      <div class="special-stage" data-slide-owned="body">
        <!-- SLIDE_AGENT_FILL_START -->
        ...
        <!-- SLIDE_AGENT_FILL_END -->
      </div>
    </main>
  </div>
</section>
```

`.special-background` and `.special-overlay` cover 1600×900. `.special-safe`
contains readable copy and never carries the page background. Special pages do
not contain `.slide-inner`, `.page-frame`, or `.page-body`.

## Ownership

Preserve:

- one root section with matching `id`, `data-slide`, page type/family/frame,
  special layout when applicable, and canvas variant;
- the header elements and exact title chain;
- numeric divider `section-number`;
- one matching footer when the plan asks for it;
- one pair of fill markers;
- the content or special structural wrappers.

Slide may:

- replace everything between the fill markers;
- add local CSS scoped to `#slide-NN`;
- add `<br>` or classed inline `<span>` inside title/subtitle without changing
  the words;
- refine title typography and the page-local motif without replacing the shared
  `special_layout` header grid.

Do not add scripts, remote dependencies, scrolling, visible URLs, citation/source
lines, duplicate footers, or a second title/section label.
