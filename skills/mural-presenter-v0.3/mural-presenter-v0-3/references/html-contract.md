# HTML 契约

Orchestrator 的 `scaffold` 为每页生成一个轻量 fragment。Slide 填充 markers 之间的区域并
可增加页面级 CSS，但保留共享结构和定版文案。

## 普通内容页

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

内容页标题/页头位置、安全区和页脚为共享结构；Slide 负责正文和局部视觉表达。页面 CSS
不重画 `.slide`、`.slide-inner` 或 `.page-frame`。只有计划声明非 base
`canvas_variant` 时，才可针对该精确根 class 改整页色场。

## 封面、过渡页或结尾页

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

`.special-background` 与 `.special-overlay` 覆盖 1600×900；`.special-safe` 只约束
可读文案，不承载页面底色。特殊页不能包含 `.slide-inner`、`.page-frame` 或
`.page-body`。

## 所有权

必须保留：

- 唯一根 section，以及匹配的 `id`、`data-slide`、页型/页族/frame、适用时的
  special layout 与 canvas variant；
- 页头元素和标题链原文；
- 纯数字 divider `section-number`；
- 计划需要时的唯一页脚；
- 唯一一对 fill markers；
- 内容页或特殊页结构包装。

Slide 可以：

- 替换 fill markers 之间的全部内容；
- 增加以 `#slide-NN` 限定的局部 CSS；
- 在不改字的前提下给标题/副标题加入 `<br>` 或带 class 的 `<span>`；
- 完善标题排印和本页母题，但不替换 `special_layout` 所定共享页头网格。

不能增加脚本、远程依赖、滚动、可见 URL、引用/来源行、重复页脚或第二套标题/章节标签。
