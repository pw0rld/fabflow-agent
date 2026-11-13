/* Display-only enhancement: no pricing, uploads or model calls. */
(() => {
  'use strict';
  const normalize = value => String(value || '').normalize('NFKC').toLocaleLowerCase().trim();
  function mount(section) {
    if (section.dataset.partsMounted) return;
    section.dataset.partsMounted = 'true';
    const items = [...section.querySelectorAll('[data-quote-group]')];
    const search = section.querySelector('[data-parts-search]');
    const filters = [...section.querySelectorAll('[data-parts-filter]')];
    const layouts = [...section.querySelectorAll('[data-parts-layout]')];
    const count = section.querySelector('[data-parts-visible-count]');
    const clear = section.querySelector('[data-parts-clear]');
    const empty = section.querySelector('[data-parts-empty]');
    if (!search || !count || !clear || !empty) return;
    const text = new Map(items.map(item => [item, normalize(item.dataset.partSearch)]));
    let filter = 'all';
    const matchesMode = (item, mode) => mode === 'all' || (mode === 'unpriced' ? item.dataset.priced !== 'true' : item.dataset.partMode === mode);
    function update() {
      const words = normalize(search.value).split(/\s+/).filter(Boolean);
      const matched = items.filter(item => words.every(word => text.get(item).includes(word)));
      const visible = new Set(matched.filter(item => matchesMode(item, filter)));
      for (const item of items) item.hidden = !visible.has(item);
      for (const button of filters) {
        const mode = button.dataset.partsFilter;
        button.setAttribute('aria-pressed', String(mode === filter));
        const badge = button.querySelector('[data-parts-filter-count]');
        if (badge) badge.textContent = String(matched.filter(item => matchesMode(item, mode)).length);
      }
      count.textContent = `显示 ${visible.size} / ${items.length} 项物料`;
      section.dataset.visibleGroups = String(visible.size);
      empty.hidden = visible.size !== 0;
      clear.hidden = !words.length && filter === 'all';
    }
    function reset() { search.value = ''; filter = 'all'; update(); search.focus(); }
    search.addEventListener('input', update);
    for (const button of filters) button.addEventListener('click', () => { filter = button.dataset.partsFilter; update(); });
    for (const button of layouts) button.addEventListener('click', () => {
      const view = button.dataset.partsLayout;
      if (!['list', 'cards'].includes(view)) return;
      section.dataset.view = view;
      for (const item of layouts) item.setAttribute('aria-pressed', String(item.dataset.partsLayout === view));
    });
    clear.addEventListener('click', reset);
    section.querySelector('[data-parts-reset]')?.addEventListener('click', reset);
    update();

    // Original new-tab links remain the no-JS and Ctrl/Cmd fallback.
    const dialog = section.querySelector('[data-parts-viewer]');
    const frame = section.querySelector('[data-parts-viewer-frame]');
    const close = section.querySelector('[data-parts-viewer-close]');
    const title = section.querySelector('[data-parts-viewer-title]');
    const open = section.querySelector('[data-parts-viewer-open]');
    const loading = section.querySelector('[data-parts-viewer-loading]');
    if (!dialog || typeof dialog.showModal !== 'function' || !frame || !close || !title || !open) return;
    let previous = null, overflow = '';
    function viewUrl(anchor) {
      try {
        const url = new URL(anchor.getAttribute('href'), location.origin);
        if (url.origin !== location.origin || url.pathname !== '/workbench/quotation/part/' || url.hash
            || [...url.searchParams.keys()].some(key => !['preview', 'scope', 'customer'].includes(key))
            || url.searchParams.getAll('preview').length !== 1
            || (url.searchParams.has('scope') && url.searchParams.get('scope') !== 'poland')
            || (url.searchParams.has('customer') && (url.searchParams.get('customer') !== '1' || url.searchParams.getAll('customer').length !== 1))) return null;
        const preview = url.searchParams.get('preview');
        if (!preview || preview.includes('\\')) return null;
        const source = new URL(preview, location.origin);
        const match = /^\/ortho2cad-test\/customer-cases\/([A-Za-z0-9_-]+)\/(?:customer-parts|parts)\/(L[0-9]{4,6})\.json$/.exec(source.pathname);
        const current = /^\/ortho2cad-test\/customer-cases\/([A-Za-z0-9_-]+)\/[^/]*$/.exec(location.pathname);
        if (source.origin !== location.origin || source.search || source.hash || !match || !current || match[1] !== current[1]
            || anchor.dataset.partPreview !== source.pathname || anchor.dataset.previewLine !== match[2]
            || anchor.dataset.viewUrl !== anchor.getAttribute('href')) return null;
        if (url.searchParams.get('customer') === '1' && !source.pathname.includes('/customer-parts/')) return null;
        return url.href;
      } catch { return null; }
    }
    function dismiss() { if (dialog.open) dialog.close(); }
    close.addEventListener('click', dismiss);
    dialog.addEventListener('click', event => { if (event.target === dialog) dismiss(); });
    dialog.addEventListener('close', () => {
      frame.src = 'about:blank';
      document.body.style.overflow = overflow;
      if (previous?.isConnected) previous.focus();
    });
    frame.addEventListener('load', () => {
      if (!dialog.open) return;
      if (loading) loading.hidden = true;
      try {
        frame.contentWindow.addEventListener('keydown', event => {
          if (event.key === 'Escape') { event.preventDefault(); dismiss(); }
        });
      } catch { /* The original link remains available in a separate tab. */ }
    });
    section.addEventListener('click', event => {
      if (event.defaultPrevented || event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
      const anchor = event.target.closest?.('a.part-preview-link');
      if (!anchor || !section.contains(anchor)) return;
      const url = viewUrl(anchor);
      if (!url) return;
      event.preventDefault();
      previous = anchor;
      title.textContent = (anchor.closest('[data-quote-group]')?.querySelector('h3')?.textContent || '原生零件') + ' · ' + anchor.dataset.previewLine;
      open.href = url;
      if (loading) loading.hidden = false;
      overflow = document.body.style.overflow;
      document.body.style.overflow = 'hidden';
      frame.src = url;
      dialog.showModal();
      close.focus();
    });
  }
  const start = () => document.querySelectorAll('[data-parts-showcase]').forEach(mount);
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start, { once: true });
  else start();
})();
