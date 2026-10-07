# MaximizePM brand files

This folder holds the mark, the logo, and the icons of MaximizePM. `export.py` makes all of them from one drawing. Run it again after a change to the drawing or the colours: `python3 design/export.py`. It needs a Chrome for the PNG files and `iconutil` (macOS) for the `.icns`. Run it outside a sandbox.

## The mark

An octopus in a rounded square tile: a long mantle that tilts back to the upper left, two sly eyes, and eight relaxed arms. It is one drawing at three levels of detail:

| Size | File | Detail |
|---|---|---|
| 48 px and larger | `mark.svg` | Thin arm tips, a row of suckers on two arms, a thin line round the tile |
| 17 to 32 px | `mark-32.svg` | Slightly larger eyes, a 1 px line round the tile |
| 16 px | `mark-16.svg` | Thicker arms, a larger mantle, eyes on whole pixels, no line |

## Colours

| Use | Hex |
|---|---|
| Tile, top | `#13254a` |
| Tile, bottom | `#08101f` |
| Line round the tile | `#24365e` |
| Octopus | `#c8323f` |
| Eyes | `#ffc94d` |
| Suckers | `#ef8a8f` |
| Name on a light page | `#141826`, with PM in `#a3202d` |
| Name on a dark page | `#eceef4`, with PM in `#ef6b74` |

## Type

The name is IBM Plex Sans SemiBold, with letter spacing of -0.02 em. The SVG files have the name as outlines, so they need no font file. IBM Plex Sans is under the SIL Open Font License.

## Files

- `logo-light.svg`, `logo-dark.svg`: the mark and the name, for a light page and a dark page.
- `wordmark-light.svg`, `wordmark-dark.svg`: the name only.
- `png/mark-<size>.png`: the mark at 16, 32, 48, 64, 128, 180, 192, 256, 512, and 1024 px.
- `app-icon.svg`, `png/app-icon-<size>.png`: the macOS app icon, with the tile on the macOS icon grid and a soft shadow.
- `MaximizePM.icns`: the macOS app icon file.
- `favicon.ico` (16, 32, and 48 px), and `favicon-16.png`, `favicon-32.png`, `favicon-48.png`: the browser tab icon.
- `apple-touch-icon.png`: 180 px, a square tile with no round corners, because iOS adds its own corners.

## Rules

- Use the file for the size: `mark-16.svg` or the 16 px PNG at 16 px, `mark-32.svg` from 17 to 32 px, and `mark.svg` at 48 px and larger.
- Give the browser the PNG favicons, not an SVG. Then a Retina screen takes the 32 px picture and a 1x screen takes the 16 px picture:

  ```html
  <link rel="icon" type="image/png" sizes="32x32" href="/favicon-32.png">
  <link rel="icon" type="image/png" sizes="16x16" href="/favicon-16.png">
  <link rel="icon" href="/favicon.ico" sizes="48x48">
  <link rel="apple-touch-icon" href="/apple-touch-icon.png">
  ```

- On a light page, keep the line round the tile (it is in the files of 17 px and larger). At 16 px the tile has no line, because a line there is only half a pixel.
- Do not change the colours of the octopus or the eyes. A dark body with red eyes reads as a spider.
- Keep space round the mark of at least one eighth of its width.
