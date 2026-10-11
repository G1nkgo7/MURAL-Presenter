# MURAL Presenter · Street Mural Identity

The `street-v2` identity takes its direction from the author's hand-painted campaign mural:
several robot collaborators making one shared presentation. Navy ink contours, opaque paint,
teal and cobalt strokes, warm ivory and small golden-yellow accents connect every asset.

![Identity overview](mural-brand-overview.png)

## Production assets

| Asset | Size | Background | Intended use |
| --- | --- | --- | --- |
| [Primary M mark](mural-mark.png) | 1254 × 1254 | Transparent | Compact project identity and interface mark. |
| [Avatar](mural-avatar.png) | 1254 × 1254 | Ivory | Square or circular profile display. |
| [Painter mascot](mural-mascot.png) | 1254 × 1254 | Transparent | Illustration, stickers and supporting project visuals. |
| [Light wordmark](mural-wordmark-light.png) | 2172 × 724 | Transparent | Wordmark on light surfaces. |
| [Dark wordmark](mural-wordmark-dark.png) | 2172 × 724 | Transparent | Wordmark on dark surfaces. |
| [Light banner](mural-banner-light.png) | 2172 × 724 | Ivory mural | README header and wide promotional placements. |
| [Dark banner](mural-banner-dark.png) | 2171 × 724 | Navy mural | README header in dark mode. |
| [Paper sharing card](mural-social-card.png) | 1733 × 907 | Ivory mural | Publicity graphic with the current full paper title. |
| [Identity overview](mural-brand-overview.png) | 1536 × 1024 | Ivory | Visual direction and presentation of the system. |

The individual PNGs are the production masters. The generated overview illustrates their
relationship; use the individual files for exact application artwork. Four masters have real
alpha transparency. These are raster illustrations, not editable vector artwork.

## Use and composition

- Use the M mark for small interface placements. Use the mascot where its tools and pose can remain readable.
- Keep the M a pure letterform: its central opening has no eyes, face panel or lower smile-shaped chevron.
- Draw each robot with two attached, round-tipped antennae. Painters have closed smiling arches; the assembler and reader have substantial dark pupils, cyan irises, small catchlights and varied natural gazes. Do not simplify these into blank ovals or identical centered dots.
- Keep the forehead area of each navy face screen matte, without blue or white specular patches. Preserve the small catchlights inside the open eyes.
- Use the native PNG masters without artificial upscaling, post-sharpening or lossy re-encoding. For a new redraw, return to the original references instead of feeding repeatedly patched outputs into another generation.
- Preserve aspect ratios and leave clear space around the mark. Avoid stretching, cropping letters or placing text across the robots' faces.
- Pair the light wordmark with light surfaces and the dark wordmark with navy or similarly dark surfaces.
- The banners carry the stable name **MURAL Presenter**. Keep the full paper title as selectable text below the banner.
- The sharing card embeds **Multi-Agent Units of Responsibility for Authoring Long-Horizon Presentations**; review that wording when the manuscript title changes.
- Use one banner per placement. The light/dark pair is selected by the reader's theme, not displayed as two competing headers.

## Palette

These reference colors guide accompanying typography and layouts; hand-painted pixels vary.

| Token | Reference | Role |
| --- | --- | --- |
| Ivory | `#F7EFDC` | Light wall and paper surfaces. |
| Navy | `#102B3E` | Ink, typography and dark surfaces. |
| Teal | `#009D9A` | Primary M plane and collaborative authoring motif. |
| Cobalt | `#1476E8` | Secondary paint, charts and working clothes. |
| Gold | `#FFBD24` | Small highlights and complementary paint strokes. |

## Provenance and maintenance

Created on 2026-10-10 with the built-in `image_gen` tool, using the author's
`mural-wall-handpainted-20261008.webp` as the visual reference. Its checksum is recorded in
[manifest.json](manifest.json), alongside the dimensions, alpha status and SHA-256 of each master.
The full generation and revision prompts are retained in [generation-prompts.json](generation-prompts.json).

Revision 2 (2026-10-10) replaces the face-like M and repairs antennae and eye consistency
across all nine assets. Filenames and dimensions are unchanged, so existing README references
load the corrected artwork. The previous revision remains available in Git at
[`4b3e71a`](https://github.com/G1nkgo7/MURAL-Presenter/tree/4b3e71ad1018cadff1ccf42b8da02a2c63de5e59/assets/logo/exports/street-v2).

Revision 3 restores closed-eye smiles to the ladder painters and the original standalone mascot,
while retaining coherent open eyes on the assembler and reader. Expression variety is intentional;
the faceless M and repaired antennae are retained. The full revision-2 artwork remains in Git at
[`8061b20`](https://github.com/G1nkgo7/MURAL-Presenter/tree/8061b207e0a0ee13a0921aa33258653fd635f75e/assets/logo/exports/street-v2).

The root English and Chinese READMEs use the banners and standalone M title mark. Their brand
sections preview the mark, avatar and mascot, with theme-aware wordmarks and a sharing-card download.
Website and WebUI application assets
are a separate integration step. Earlier PNG/SVG exports in the parent directory remain intact;
they are historical artwork and should not be mixed into new `street-v2` layouts.
Manuscript method figures retain their original scientific content and are maintained separately.

Revision 4 uses the original reader's pupil-bearing eyes as the expression reference and gives
the assembler the same lively eye construction. The closed-eye painter, repaired antennae and
faceless M are retained. Both banners, the sharing card and the overview are updated; the other
five masters are unchanged from revision 3.

Revision 5 (historical, superseded by revision 6) restores the four promotional images from the original `4b3e71a` PNGs and applies
local pixel repairs, explicitly requested by the author. The reader's original face is protected
pixel for pixel. M and antenna patches come from `8061b20`; the assembler's pupils are local
edits within its original eye shapes. No new image generation, whole-image resizing, sharpening
or lossy encoding was used. [pixel-repair.json](pixel-repair.json) records the source commits,
repair regions, checksums and unchanged-pixel checks. The other five masters remain unchanged.

Revision 6 established the redrawn campaign artwork. At the author's request, the four promotional
images were freshly drawn after the local eye repairs proved visually unsatisfactory. The light
banner starts from the original campaign mural, original banner and faceless M reference; the
dark banner, sharing card and overview follow that fresh master. Open eyes now have coherent
pupils and catchlights, with varied gazes alongside the painter's closed-eye smile. Each robot
has two attached antennae. The generated files are copied at their native dimensions without
resizing, sharpening or re-encoding; the banners are 2172 × 724, not 4K. The other five masters
remain byte-identical. Revision 5 and its repair report remain available for historical reference
at [`e2823a4`](https://github.com/G1nkgo7/MURAL-Presenter/tree/e2823a4b51e8f8c11b341b40760e93dffd63b3d8/assets/logo/exports/street-v2).

Revision 7 is the current production artwork. Targeted built-in `image_gen` edits remove the
blue/white forehead reflections from the four campaign images while retaining the varied eye
expressions and small catchlights inside open eyes. The overview also uses matte face screens.
These are generated edits, not pixel-identical repairs outside a mask. Selected PNGs are saved
at their native dimensions without resizing or re-encoding; the dark banner is now 2171 × 724.
The other five masters are unchanged. Revision 6 remains in Git at
[`20815e4`](https://github.com/G1nkgo7/MURAL-Presenter/tree/20815e4/assets/logo/exports/street-v2).

中文说明：本套包含主标识、头像、吉祥物、两版透明字标、两版横幅、论文分享图及视觉总览。
中英文 README 已接入横幅、标题旁的 M 标识、主标识／头像／吉祥物预览、深浅色字标和分享图下载。
原网站、WebUI 与旧版素材保留，后续应用时以本目录的独立文件为准。
第 2 次修订移除了 M 内部的人脸元素，并统一机器人的双天线、椭圆眼睛；上一版可从 Git 历史恢复。
第 3 次修订恢复画家与独立吉祥物的眯眼笑，保留另外两位角色的睁眼表情，只要求睁眼画法协调，不统一所有表情。
第 4 次修订恢复最初读书角色带瞳孔的灵动眼睛，并补齐中间角色的瞳孔；保留眯眼笑、修好的天线和无脸 M。
第 5 次修订回到初版原始 PNG 做局部像素修补，原样保留读书角色的脸部；修补范围外的像素与初版完全一致，停止整图反复生成。
第 6 次修订：因局部修补后的表情不协调，重新参考原始壁画绘制两版横幅、论文分享图和视觉总览。
保留画家的眯眼笑，为睁眼角色绘制协调的瞳孔、高光和不同视线；使用原生无损 PNG，不放大或锐化。
其余五项独立素材保持原样；第 5 次修订及其像素报告仅为历史记录，不描述当前图像。
当前采用第 7 次修订：去掉四张宣传图中机器人额头的块状反光，保留眼睛内的小高光和不同表情。
使用 image_gen 定向编辑并保存原生无损 PNG，未进行放大、锐化或有损转码；不声称其他区域逐像素不变。
