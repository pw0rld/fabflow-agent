const SVG_NS = 'http://www.w3.org/2000/svg';
const XMLNS_NS = 'http://www.w3.org/2000/xmlns/';
const XLINK_NS = 'http://www.w3.org/1999/xlink';
const MAX_FILE_BYTES = 32 * 1024 * 1024;
const MAX_SVG_BYTES = 24 * 1024 * 1024;
const MAX_SVG_NODES = 300000;
const TIMEOUT_MS = 90000;
const ALLOWED_ELEMENTS = new Set([
  'svg', 'g', 'defs', 'path', 'line', 'polyline', 'polygon', 'circle', 'ellipse',
  'rect', 'text', 'tspan', 'use', 'title', 'desc',
]);
const ALLOWED_ATTRIBUTES = new Set([
  'id', 'viewBox', 'version', 'width', 'height', 'preserveAspectRatio',
  'fill', 'stroke', 'stroke-width', 'stroke-linecap', 'stroke-linejoin',
  'stroke-miterlimit', 'stroke-dasharray', 'stroke-dashoffset', 'fill-rule',
  'clip-rule', 'opacity', 'fill-opacity', 'stroke-opacity', 'transform', 'd',
  'x', 'y', 'x1', 'y1', 'x2', 'y2', 'cx', 'cy', 'r', 'rx', 'ry', 'points',
  'font-size', 'text-anchor', 'dominant-baseline', 'dx', 'dy', 'rotate',
  'font-family', 'font-weight', 'font-style', 'href',
]);
const NUMERIC_ATTRIBUTES = new Set([
  'viewBox', 'width', 'height', 'stroke-width', 'stroke-miterlimit',
  'stroke-dasharray', 'stroke-dashoffset', 'opacity', 'fill-opacity',
  'stroke-opacity', 'transform', 'd', 'x', 'y', 'x1', 'y1', 'x2', 'y2',
  'cx', 'cy', 'r', 'rx', 'ry', 'points', 'font-size', 'dx', 'dy', 'rotate',
]);

const unsafeSvg = () => new Error('DWG 生成的预览格式不受支持，请下载原件查看。');
const abortError = () => new DOMException('DWG 预览已取消。', 'AbortError');

// The upstream converter interpolates CAD text into XML. Validate its result
// before displaying it, and keep the SVG in an image rather than the live DOM.
export function sanitizeSvg(source) {
  if (typeof source !== 'string' || !source || source.length > MAX_SVG_BYTES ||
      /<!\s*(?:DOCTYPE|ENTITY)\b|<\?xml-stylesheet\b/i.test(source)) throw unsafeSvg();
  const document = new DOMParser().parseFromString(source, 'image/svg+xml');
  const root = document.documentElement;
  if (document.querySelector('parsererror') || root?.localName !== 'svg' || root.namespaceURI !== SVG_NS ||
      document.doctype) throw unsafeSvg();
  const elements = document.getElementsByTagName('*');
  if (elements.length > MAX_SVG_NODES) throw unsafeSvg();
  const ids = new Map();
  for (const node of elements) {
    if (node.namespaceURI !== SVG_NS || !ALLOWED_ELEMENTS.has(node.localName)) throw unsafeSvg();
    for (const attr of node.attributes) {
      const name = attr.localName;
      const value = attr.value;
      if (/^on/i.test(name)) throw unsafeSvg();
      if (attr.namespaceURI === XMLNS_NS) {
        if (!((attr.name === 'xmlns' && value === SVG_NS) ||
              (attr.name === 'xmlns:xlink' && value === XLINK_NS))) throw unsafeSvg();
        continue;
      }
      if ((attr.namespaceURI && !(name === 'href' && attr.namespaceURI === XLINK_NS)) ||
          !ALLOWED_ATTRIBUTES.has(name)) throw unsafeSvg();
      if (name === 'href' && !/^#[^\u0000-\u0020<>"'`]+$/u.test(value)) throw unsafeSvg();
      if ((name === 'fill' || name === 'stroke') &&
          !/^(?:none|currentColor|transparent|#[\da-f]{3,8}|[a-z]{1,24}|rgba?\([\d\s.,%+\-]+\)|hsla?\([\d\s.,%+\-]+\))$/i.test(value)) {
        throw unsafeSvg();
      }
      if (name === 'transform' &&
          !/^(?:(?:matrix|translate|scale|rotate|skewX|skewY)\(\s*[-+\d.eE,\s]+\)\s*)+$/.test(value)) throw unsafeSvg();
      if (name === 'd' && !/^[MmZzLlHhVvCcSsQqTtAa0-9+\-.,\sEe]*$/.test(value)) throw unsafeSvg();
      if (NUMERIC_ATTRIBUTES.has(name)) {
        if (/\b(?:nan|infinity)\b/i.test(value)) throw unsafeSvg();
        const numbers = value.match(/[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?/g) || [];
        if (numbers.some(number => !Number.isFinite(Number(number)) || Math.abs(Number(number)) > 1e15)) throw unsafeSvg();
      }
    }
    if (node.hasAttribute('id')) {
      const id = node.getAttribute('id');
      if (ids.has(id)) throw unsafeSvg();
      ids.set(id, node);
    }
  }
  // Bound the expanded drawing too: a small SVG can contain recursive or
  // exponentially repeated <use> references even when its XML is harmless.
  const stack = [{ node: root, exit: false, depth: 0 }];
  const active = new Set();
  let expandedNodes = 0;
  while (stack.length) {
    const { node, exit, depth } = stack.pop();
    if (exit) { active.delete(node); continue; }
    if (active.has(node) || depth > 128 || ++expandedNodes > MAX_SVG_NODES) throw unsafeSvg();
    active.add(node);
    stack.push({ node, exit: true, depth });
    for (const child of node.children) {
      if (child.localName !== 'defs') stack.push({ node: child, exit: false, depth: depth + 1 });
    }
    if (node.localName === 'use') {
      const href = node.getAttribute('href') || node.getAttributeNS(XLINK_NS, 'href');
      const target = href && ids.get(href.slice(1));
      if (!target) throw unsafeSvg();
      stack.push({ node: target, exit: false, depth: depth + 1 });
    }
  }
  const viewBox = root.getAttribute('viewBox')?.trim().split(/[\s,]+/).map(Number);
  if (!viewBox || viewBox.length !== 4 || viewBox.some(value => !Number.isFinite(value)) ||
      viewBox[2] < 0 || viewBox[3] < 0 || Math.max(viewBox[2], viewBox[3]) <= 0) throw unsafeSvg();
  const padding = Math.max(viewBox[2], viewBox[3]) * 0.03;
  root.setAttribute('viewBox', [viewBox[0] - padding, viewBox[1] - padding,
    viewBox[2] + 2 * padding, viewBox[3] + 2 * padding].join(' '));
  root.setAttribute('width', '100%');
  root.setAttribute('height', '100%');
  root.setAttribute('preserveAspectRatio', 'xMidYMid meet');
  return new XMLSerializer().serializeToString(root);
}

export async function mount({ element, bytes, signal, status = () => {}, warning = () => {} }) {
  if (signal?.aborted) throw abortError();
  if (!(bytes instanceof ArrayBuffer) || !bytes.byteLength) throw new Error('DWG 文件为空或无法读取。');
  if (bytes.byteLength > MAX_FILE_BYTES) throw new Error('DWG 预览支持不超过 32 MB 的文件，请下载原件查看。');
  const signature = String.fromCharCode(...new Uint8Array(bytes, 0, Math.min(6, bytes.byteLength)));
  if (!/^(?:AC\d{4}|AC\d\.\d\d|MC\d\.\d)$/.test(signature)) {
    throw new Error('文件内容不是可识别的 DWG 格式，请核对原件。');
  }
  const surface = document.createElement('div');
  Object.assign(surface.style, {
    position: 'relative', width: '100%', height: '100%', minHeight: '240px',
    overflow: 'hidden', background: '#16222c', touchAction: 'none', cursor: 'grab',
  });
  const image = document.createElement('img');
  image.alt = 'DWG 图纸二维预览';
  image.draggable = false;
  Object.assign(image.style, {
    display: 'block', width: '100%', height: '100%', objectFit: 'contain',
    transformOrigin: 'center center', userSelect: 'none', pointerEvents: 'none',
  });
  surface.append(image);
  element.append(surface);
  let worker;
  let objectUrl;
  let timer;
  let disposed = false;
  let scale = 1;
  let offsetX = 0;
  let offsetY = 0;
  let drag;
  let rejectFailure;
  const applyTransform = () => {
    if (!disposed) image.style.transform = `translate(${offsetX}px, ${offsetY}px) scale(${scale})`;
  };
  const reset = () => { scale = 1; offsetX = 0; offsetY = 0; applyTransform(); };
  const zoom = factor => {
    if (!Number.isFinite(factor) || factor <= 0 || disposed) return;
    scale = Math.max(0.2, Math.min(12, scale * factor));
    applyTransform();
  };
  const onWheel = event => { event.preventDefault(); zoom(event.deltaY < 0 ? 1.15 : 1 / 1.15); };
  const onPointerDown = event => {
    if (event.button !== 0 || disposed || drag) return;
    drag = { id: event.pointerId, x: event.clientX, y: event.clientY, offsetX, offsetY };
    surface.setPointerCapture(event.pointerId);
    surface.style.cursor = 'grabbing';
  };
  const onPointerMove = event => {
    if (drag?.id !== event.pointerId) return;
    offsetX = drag.offsetX + event.clientX - drag.x;
    offsetY = drag.offsetY + event.clientY - drag.y;
    applyTransform();
  };
  const onPointerEnd = event => {
    if (drag?.id !== event.pointerId) return;
    drag = undefined;
    surface.style.cursor = 'grab';
  };
  const stopWorker = () => { worker?.terminate(); worker = undefined; };
  const onAbort = () => { rejectFailure?.(abortError()); dispose(); };
  function dispose() {
    if (disposed) return;
    disposed = true;
    clearTimeout(timer);
    stopWorker();
    signal?.removeEventListener('abort', onAbort);
    surface.removeEventListener('wheel', onWheel);
    surface.removeEventListener('pointerdown', onPointerDown);
    surface.removeEventListener('pointermove', onPointerMove);
    surface.removeEventListener('pointerup', onPointerEnd);
    surface.removeEventListener('pointercancel', onPointerEnd);
    if (objectUrl) URL.revokeObjectURL(objectUrl);
    image.removeAttribute('src');
    surface.remove();
  }
  surface.addEventListener('wheel', onWheel, { passive: false });
  surface.addEventListener('pointerdown', onPointerDown);
  surface.addEventListener('pointermove', onPointerMove);
  surface.addEventListener('pointerup', onPointerEnd);
  surface.addEventListener('pointercancel', onPointerEnd);
  signal?.addEventListener('abort', onAbort, { once: true });
  const failure = new Promise((_, reject) => {
    rejectFailure = reject;
    timer = setTimeout(() => {
      reject(new Error('DWG 预览处理超过 90 秒，请下载原件查看或稍后重试。'));
      dispose();
    }, TIMEOUT_MS);
  });
  try {
    status('正在准备 DWG 预览…');
    const load = (async () => {
      const result = await new Promise((resolve, reject) => {
        worker = new Worker(new URL('./dwg-worker.js', import.meta.url), { type: 'module' });
        worker.onmessage = ({ data }) => {
          if (data?.type === 'status') status(data.message);
          else if (data?.type === 'error') reject(new Error(data.message));
          else if (data?.type === 'result') resolve(data);
        };
        worker.onerror = event => {
          event.preventDefault();
          reject(new Error('DWG 预览组件无法运行，请下载原件查看。'));
        };
        worker.onmessageerror = () => reject(new Error('DWG 预览数据无法读取，请下载原件查看。'));
        worker.postMessage({ type: 'read', bytes }, [bytes]);
      });
      stopWorker();
      if (disposed || signal?.aborted) throw abortError();
      status('正在显示 DWG 图纸…');
      const svg = sanitizeSvg(result.svg);
      objectUrl = URL.createObjectURL(new Blob([svg], { type: 'image/svg+xml' }));
      image.src = objectUrl;
      try { await image.decode(); } catch (_) { throw unsafeSvg(); }
      if (disposed || signal?.aborted) throw abortError();
      for (const message of result.warnings || []) warning(message);
      status('DWG 预览已加载，可缩放或拖动。');
    })();
    await Promise.race([load, failure]);
    clearTimeout(timer);
    return { dispose, reset, zoom };
  } catch (error) {
    dispose();
    throw error;
  }
}
