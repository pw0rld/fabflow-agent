# LibreDWG browser runtime

Unmodified runtime artifacts from `@mlightcad/libredwg-web@0.8.1`.

- Upstream: https://github.com/mlightcad/libredwg-web
- Fixed source: https://github.com/mlightcad/libredwg-web/tree/53137eeb1fe54e400bfd77a7ad8e21bc373cc998
- Corresponding source archive: https://github.com/mlightcad/libredwg-web/archive/53137eeb1fe54e400bfd77a7ad8e21bc373cc998.tar.gz
- License: GPL-3.0. The upstream GPL license text is retained in `COPYING`.
- Package origin, source provenance and runtime SHA-256 values: `SOURCE.json`.

The same-origin module Worker loads this runtime only for DWG preview. Keep the relative `dist/` and `wasm/` directories. No npm installation or remote runtime CDN is needed. The upstream WASM was built with 1 GB initial linear memory; the Worker is terminated after conversion, timeout, cancellation or disposal. SVG conversion covers a subset of 2D model-space entities, so the original CAD file remains authoritative.
