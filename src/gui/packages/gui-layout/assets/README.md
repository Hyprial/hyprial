# Hyprial sidebar mark

`hyprial-hf-48.png` is derived from the frozen product icon at
`/home/huangjiajia/HyprialOS/icon.png` (SHA-256
`ccb87157b7f29e522196d846503efcd41fe2531a63bd3701cac9e86e5f14f6dc`).

The source is 1254x1254 RGBA. The derived asset trims its transparent bounds
and resizes the result to 48x48 for a 24px sidebar mark at 2x density. The
derived PNG has SHA-256
`93c0ab5291788c5dd8dc779924f357e49db5e7baa2f12f6a70fc195b17e05f1a`.

`build.mjs` verifies that hash and the PNG dimensions, then inlines the image as
a data URI in the generated client bundle. The 738 KB source is never shipped
to the browser.
