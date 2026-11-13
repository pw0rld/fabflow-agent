// Hand-authored DSH browser bundle. React is provided by the installed DSH shell.
window.__ModuleLoader__.load({
  id: '@haoyunlai/dsh-engineering-demo',
  factory: (require) => {
    const React = require('react');
    const h = React.createElement;
    const NS = 'haoyunlai-engineering-demo';
    const words = {
      zh: { title: '好运来报价工作台', open: '打开好运来报价工作台', close: '返回 DSH', standalone: '独立打开', notice: '订单资料 · 预估重量 · BOM 与报价草稿' },
      en: { title: 'Haoyunlai Quotation Workbench', open: 'Open Haoyunlai quotation workbench', close: 'Back to DSH', standalone: 'Open in new tab', notice: 'Order files · Estimated weight · BOM and draft quotation' },
    };
    function apply(ctx) {
      let opened = false;
      const listeners = new Set();
      const subscribe = (fn) => { listeners.add(fn); return () => listeners.delete(fn); };
      const snapshot = () => opened;
      const setOpened = (value) => { opened = value; listeners.forEach((fn) => fn()); };
      ctx.effect(() => ctx.locale.register(NS, words), 'haoyunlai: dictionaries');

      function Entry({ wide, t }) {
        return h('button', {
          type: 'button', onClick: () => setOpened(true), title: t('open'),
          'aria-label': t('open'), 'data-haoyunlai-demo-entry': 'true',
          style: { display: 'flex', alignItems: 'center', gap: 7, maxWidth: '100%', padding: wide ? '7px 9px' : 7,
            border: '1px solid #c9d9eb', borderRadius: 7, background: '#edf5ff', color: '#245aa2',
            cursor: 'pointer', fontSize: 12, whiteSpace: 'nowrap' },
        }, h('svg', { width: 17, height: 17, viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', strokeWidth: 1.7, 'aria-hidden': true },
          h('path', { d: 'M3 6h18v14H3zM3 10h18M8 10v10M8 3v3M16 3v3M11 14h7M11 17h5' })),
        wide ? h('span', null, t('title')) : null);
      }
      function Overlay({ t }) {
        const isOpen = React.useSyncExternalStore(subscribe, snapshot, snapshot);
        const backRef = React.useRef(null);
        React.useEffect(() => {
          if (!isOpen) return;
          const previous = document.activeElement;
          backRef.current?.focus();
          const onKey = (event) => { if (event.key === 'Escape') setOpened(false); };
          window.addEventListener('keydown', onKey);
          return () => { window.removeEventListener('keydown', onKey); previous?.focus?.(); };
        }, [isOpen]);
        if (!isOpen) return null;
        return h('section', {
          role: 'dialog', 'aria-modal': true, 'aria-label': t('title'), 'data-haoyunlai-demo-overlay': 'true',
          style: { position: 'fixed', inset: 0, zIndex: 10000, display: 'flex', flexDirection: 'column',
            background: '#f5f8fc', color: '#243850', pointerEvents: 'auto' },
        }, h('header', { style: { display: 'flex', alignItems: 'center', flexWrap: 'wrap', gap: 12, padding: '10px 16px',
          background: '#fff', borderBottom: '1px solid #dfe7f0', fontFamily: 'system-ui,sans-serif', fontSize: 13 } },
          h('button', { ref: backRef, type: 'button', onClick: () => setOpened(false), style: { padding: '6px 10px',
            border: '1px solid #dfe7f0', borderRadius: 6, background: '#fff', cursor: 'pointer', color: '#245aa2' } }, '← ', t('close')),
          h('strong', null, t('title')), h('span', { style: { color: '#987139', fontSize: 12 } }, t('notice')),
          h('a', { href: '/workbench/quotation/', target: '_blank', rel: 'noopener noreferrer',
            style: { marginLeft: 'auto', color: '#245aa2', textDecoration: 'none' } }, t('standalone'), ' ↗')),
        h('iframe', { title: t('title'), src: '/workbench/quotation/', 'data-haoyunlai-demo-frame': 'true',
          style: { border: 0, width: '100%', flex: 1, minHeight: 0, background: '#f5f8fc' } }));
      }
      ctx.slots.inject('sidebar.footer.action', () => ctx.slots.register({
        name: 'sidebar.footer.action', id: 'haoyunlai-engineering-demo-entry', locale: NS,
      }, Entry));
      ctx.slots.inject('shell.overlay', () => ctx.slots.register({
        name: 'shell.overlay', id: 'haoyunlai-engineering-demo-overlay', locale: NS,
      }, Overlay));
      ctx.effect(() => () => { listeners.clear(); opened = false; }, 'haoyunlai: clear UI state');
    }
    return { inject: ['slots', 'locale'], apply };
  },
});
