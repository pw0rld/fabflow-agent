# Vendored DXF parser

`dxf-parser-1.1.2.js` is the unmodified browser UMD distribution from
[`dxf-parser` 1.1.2](https://github.com/gdsestimating/dxf-parser), MIT licensed.
`SOURCE.json` records its npm archive URL and SHA-256. The bundle includes
`loglevel`; its MIT license is retained as `LICENSE-loglevel`.

The application loads this file only from its own origin inside a dedicated
worker. It has no CDN, external-font, external-reference, or network dependency.

The local preview driver is visual only. It renders common planar entities and
reports unsupported entity types. Browser fonts and discretized curves may
differ from a CAD application's display. Do not use the preview as a measurement
or as confirmed quotation parameters.
