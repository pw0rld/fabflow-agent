# STEP preview dependencies

Downloaded from the official npm registry, pinned on 2026-10-19. No runtime CDN or external model upload is used. `provenance.json` records file sizes and SHA-256 hashes.

## OCCT Import JS 0.0.23

- Upstream: https://github.com/kovacsv/occt-import-js/tree/0.0.23
- Release and build versions: https://github.com/kovacsv/occt-import-js/releases/tag/0.0.23
- Original npm package: https://registry.npmjs.org/occt-import-js/-/occt-import-js-0.0.23.tgz
- License: GNU LGPL 2.1; see `license.occt-import-js.txt` and `license.occt.txt`.
- The release uses Open CASCADE Technology 7.6.1 and Emscripten 3.1.69. Open CASCADE is licensed under LGPL 2.1 with the Open CASCADE exception 1.0; see `OCCT_LGPL_EXCEPTION.txt`.
- OCCT source: https://github.com/Open-Cascade-SAS/OCCT/tree/V7_6_1
- Complete wrapper/build source: the upstream tag above, including its pinned OCCT submodule and build scripts.
- `occt-import-js.js` and `occt-import-js.wasm` are copied unmodified and loaded separately, so they can be replaced with an API-compatible build.

## Three.js 0.170.0 (r170)

- Upstream source: https://github.com/mrdoob/three.js/tree/r170
- Original npm package: https://registry.npmjs.org/three/-/three-0.170.0.tgz
- License: MIT; see `license.three.txt`.
- `three.module.min.js` is unmodified. `OrbitControls.js` has only its `three` import changed to `./three.module.min.js` and a modification notice prepended; the unmodified source is at https://github.com/mrdoob/three.js/blob/r170/examples/jsm/controls/OrbitControls.js.

## Runtime boundary

`step-worker.js` parses and triangulates in a disposable dedicated WebWorker. The upstream Embind runtime creates JavaScript functions, so only that worker response needs a CSP with `script-src 'self' 'unsafe-eval'`. The workbench and preview document do not need to permit JavaScript eval. No file is converted into quotation parameters by this viewer.

Preview limits: 20 MB input, 45-second worker deadline, 10,000 meshes, 1,000,000 triangles, 2,000,000 vertices. Nonfinite coordinates and invalid mesh indices fail explicitly. These limits control the display path; this is not a CAD validity or completeness certification.
