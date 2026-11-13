// Parse untrusted CAD in a disposable Worker, separate from the workbench UI.
const MAX_FILE_BYTES = 32 * 1024 * 1024;
const MAX_ENTITIES = 200000;
const MAX_SVG_BYTES = 24 * 1024 * 1024;
const SVG_ENTITY_TYPES = new Set([
  'ARC', 'CIRCLE', 'DIMENSION', 'ELLIPSE', 'INSERT', 'LINE', 'LWPOLYLINE',
  'MTEXT', 'SPLINE', 'RAY', 'TABLE', 'ACAD_TABLE', 'TEXT', 'XLINE',
]);
let started = false;

self.onmessage = async ({ data }) => {
  if (started || data?.type !== 'read') return;
  started = true;
  let lib;
  let pointer;
  try {
    if (!(data.bytes instanceof ArrayBuffer) || !data.bytes.byteLength) {
      throw new Error('DWG 文件为空或无法读取。');
    }
    if (data.bytes.byteLength > MAX_FILE_BYTES) {
      throw new Error('DWG 预览支持不超过 32 MB 的文件，请下载原件查看。');
    }
    self.postMessage({ type: 'status', message: '正在加载 DWG 预览组件…' });
    const { LibreDwg, Dwg_File_Type } = await import('./vendor/dwg/dist/libredwg-web.js');
    lib = await LibreDwg.create(new URL('./vendor/dwg/wasm', import.meta.url).href);
    self.postMessage({ type: 'status', message: '正在读取 DWG 图形…' });
    pointer = lib.dwg_read_data(data.bytes, Dwg_File_Type.DWG);
    if (!pointer) throw new Error('此 DWG 文件无法解析，请下载原件查看。');
    self.postMessage({ type: 'status', message: '正在整理 DWG 二维图形…' });
    const { database, stats } = lib.convertEx(pointer);
    const entities = new Map();
    for (const entity of database.entities || []) {
      entities.set(entity.handle || entity, entity);
    }
    for (const block of database.tables?.BLOCK_RECORD?.entries || []) {
      for (const entity of block.entities || []) {
        entities.set(entity.handle || entity, entity);
        if (entities.size > MAX_ENTITIES) break;
      }
      if (entities.size > MAX_ENTITIES) break;
    }
    if (entities.size > MAX_ENTITIES) {
      throw new Error('DWG 图形数量过多，无法在浏览器中安全预览，请下载原件查看。');
    }
    const skipped = new Set();
    for (const entity of entities.values()) {
      if (!SVG_ENTITY_TYPES.has(entity.type)) skipped.add(entity.type || '未知类型');
    }
    self.postMessage({ type: 'status', message: '正在生成 DWG 预览…' });
    const svg = lib.dwg_to_svg(database);
    if (typeof svg !== 'string' || svg.length > MAX_SVG_BYTES ||
        new TextEncoder().encode(svg).byteLength > MAX_SVG_BYTES) {
      throw new Error('DWG 预览内容过大，请下载原件查看。');
    }
    const warnings = ['当前显示模型空间的二维预览，标注、字体和复杂图元可能不完整，请以原 CAD 文件为准。'];
    if (stats?.unknownEntityCount > 0 || skipped.size) {
      warnings.push('此图纸有暂不支持的图元，预览可能缺少部分内容，请下载原件核对。');
    }
    self.postMessage({
      type: 'result', svg, warnings,
      entityCount: entities.size,
      unknownEntityCount: stats?.unknownEntityCount || 0,
    });
  } catch (error) {
    const detail = String(error?.message || error);
    const message = /^(?:DWG|此 DWG)/.test(detail) ? detail :
      /out of.*memory|memory access out of bounds/i.test(detail) ?
        '图纸过于复杂，浏览器内存不足，请下载原件查看。' :
        '此文件损坏、格式暂不支持或包含无法显示的图元，请下载原件查看。';
    self.postMessage({ type: 'error', message: `DWG 预览失败：${message.slice(0, 400)}` });
  } finally {
    if (lib && pointer) {
      try { lib.dwg_free(pointer); } catch (_) { /* Worker termination also releases its memory. */ }
    }
  }
};
