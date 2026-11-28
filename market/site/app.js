'use strict';

(() => {
  const KINDS = [
    'all',
    'device_model',
    'predictor',
    'generator',
    'solver',
    'evaluator',
    'algorithm',
  ];

  const MESSAGES = {
    zh_cn: {
      pageTitle: 'pIES Market — 公共模型与算法市场',
      subtitle: '独立静态公共设备模型与算法插件目录（只读离线包分发，零后端依赖）',
      searchLabel: '搜索制品',
      searchPlaceholder: '按 ID、名称、说明或发布者搜索…',
      retry: '重试',
      loading: '正在加载市场索引…',
      loadErrorTitle: '市场索引加载失败',
      loadErrorStatus: '请求 ./index.json 返回 HTTP {status}',
      loadErrorContract: '市场索引格式不符合 ies.market-index@1.0.0 契约',
      emptyCatalog: '当前市场暂无可用制品',
      emptyFiltered: '没有匹配当前筛选条件的制品',
      countText: '显示 {shown} / {total} 个制品',
      generatedAt: '索引生成时间：{time}',
      publisher: '发布者',
      license: '许可证',
      compatibility: '兼容性',
      size: '文件大小',
      version: '版本 / 修订',
      copySha: '复制 SHA-256',
      copiedSha: '已复制',
      details: '查看详情',
      download: '下载离线包',
      consumes: '输入契约 (consumes)',
      produces: '输出契约 (produces)',
      none: '无',
      readmeTitle: '说明文档 (README)',
      readmeLoading: '正在加载 README…',
      readmeNone: '该制品未提供 README 文件。',
      readmeError: 'README 加载失败：{reason}',
      close: '关闭',
      kinds: {
        all: '全部',
        device_model: '设备模型',
        predictor: '预测插件',
        generator: '生成器',
        solver: '求解器',
        evaluator: '评价器',
        algorithm: '一体化算法',
      },
    },
    en_us: {
      pageTitle: 'pIES Market — Public Models & Plugins',
      subtitle: 'Standalone static public catalog for device models and algorithm plugins (read-only offline packages, zero backend)',
      searchLabel: 'Search artifacts',
      searchPlaceholder: 'Search by ID, name, description, or publisher…',
      retry: 'Retry',
      loading: 'Loading market index…',
      loadErrorTitle: 'Failed to load market index',
      loadErrorStatus: 'Request to ./index.json returned HTTP {status}',
      loadErrorContract: 'Market index does not conform to ies.market-index@1.0.0 contract',
      emptyCatalog: 'No artifacts published in this market yet',
      emptyFiltered: 'No artifacts match the current filter',
      countText: 'Showing {shown} of {total} artifacts',
      generatedAt: 'Index generated: {time}',
      publisher: 'Publisher',
      license: 'License',
      compatibility: 'Compatibility',
      size: 'Size',
      version: 'Version / Revision',
      copySha: 'Copy SHA-256',
      copiedSha: 'Copied',
      details: 'Details',
      download: 'Download package',
      consumes: 'Consumes',
      produces: 'Produces',
      none: 'None',
      readmeTitle: 'README',
      readmeLoading: 'Loading README…',
      readmeNone: 'No README provided for this artifact.',
      readmeError: 'Failed to load README: {reason}',
      close: 'Close',
      kinds: {
        all: 'All',
        device_model: 'Device Model',
        predictor: 'Predictor',
        generator: 'Generator',
        solver: 'Solver',
        evaluator: 'Evaluator',
        algorithm: 'Algorithm',
      },
    },
  };

  const state = {
    lang: 'zh_cn',
    kind: 'all',
    query: '',
    loading: true,
    error: null,
    index: null,
    activeArtifact: null,
    readmeAbort: null,
  };

  const dom = {
    subtitle: document.getElementById('brand-subtitle'),
    langZh: document.getElementById('lang-zh'),
    langEn: document.getElementById('lang-en'),
    searchLabel: document.getElementById('search-label'),
    searchInput: document.getElementById('search-input'),
    kindFilters: document.getElementById('kind-filters'),
    catalogCount: document.getElementById('catalog-count'),
    catalogUpdated: document.getElementById('catalog-updated'),
    statusAlert: document.getElementById('status-alert'),
    statusAlertTitle: document.getElementById('status-alert-title'),
    statusAlertDetail: document.getElementById('status-alert-detail'),
    retryBtn: document.getElementById('retry-btn'),
    loadingBox: document.getElementById('loading-box'),
    emptyBox: document.getElementById('empty-box'),
    artifactGrid: document.getElementById('artifact-grid'),
    modal: document.getElementById('artifact-modal'),
    modalKind: document.getElementById('modal-kind'),
    modalTitle: document.getElementById('modal-title'),
    modalId: document.getElementById('modal-id'),
    modalDesc: document.getElementById('modal-desc'),
    modalMeta: document.getElementById('modal-meta'),
    modalContracts: document.getElementById('modal-contracts'),
    modalConsumesTitle: document.getElementById('modal-consumes-title'),
    modalConsumesList: document.getElementById('modal-consumes-list'),
    modalProducesTitle: document.getElementById('modal-produces-title'),
    modalProducesList: document.getElementById('modal-produces-list'),
    modalShaValue: document.getElementById('modal-sha-value'),
    modalCopySha: document.getElementById('modal-copy-sha'),
    modalReadmeTitle: document.getElementById('modal-readme-title'),
    modalReadmeError: document.getElementById('modal-readme-error'),
    modalReadmeBody: document.getElementById('modal-readme-body'),
    modalDownload: document.getElementById('modal-download'),
    modalClose: document.getElementById('modal-close'),
  };

  function msg(key, vars) {
    const dict = MESSAGES[state.lang];
    let text = dict[key] ?? key;
    if (vars) {
      for (const [k, v] of Object.entries(vars)) {
        text = text.replace(`{${k}}`, String(v));
      }
    }
    return text;
  }

  function el(tag, attrs, children) {
    const node = document.createElement(tag);
    if (attrs) {
      for (const [k, v] of Object.entries(attrs)) {
        if (v === null || v === undefined) continue;
        if (k === 'className') node.className = String(v);
        else if (k === 'textContent') node.textContent = String(v);
        else if (k.startsWith('data-')) node.setAttribute(k, String(v));
        else node.setAttribute(k, String(v));
      }
    }
    if (children) {
      for (const child of children) {
        if (child !== null && child !== undefined) node.appendChild(child);
      }
    }
    return node;
  }

  function resolveSafeRelativeUrl(rawPath) {
    if (typeof rawPath !== 'string' || !rawPath) return null;
    if (rawPath.startsWith('/') || rawPath.includes('\\')) return null;
    const segments = rawPath.split('/');
    if (segments.some((s) => s === '' || s === '.' || s === '..')) return null;
    try {
      const resolved = new URL(rawPath, window.location.href);
      if (resolved.origin !== window.location.origin) return null;
      return resolved.href;
    } catch {
      return null;
    }
  }

  function formatBytes(bytes) {
    if (typeof bytes !== 'number' || !Number.isFinite(bytes) || bytes < 0) return '—';
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(2)} MB`;
  }

  function formatCompatibility(compat) {
    if (!compat || typeof compat !== 'object') return '—';
    const parts = [];
    if (typeof compat.pies === 'string' && compat.pies) parts.push(`pIES ${compat.pies}`);
    if (typeof compat.sdk === 'string' && compat.sdk) parts.push(`SDK ${compat.sdk}`);
    return parts.length > 0 ? parts.join(' · ') : '—';
  }

  function localizedName(item) {
    const nameObj = item && item.name && typeof item.name === 'object' ? item.name : null;
    if (!nameObj) return String(item?.id ?? '');
    return (
      nameObj[state.lang] ||
      nameObj.zh_cn ||
      nameObj.en_us ||
      String(item.id ?? '')
    );
  }

  function artifactVersionLabel(item) {
    if (item.kind === 'device_model') {
      return `r${item.revision ?? '?'}`;
    }
    return `v${item.version ?? '?'}`;
  }

  function downloadFilename(item) {
    const safeUrl = resolveSafeRelativeUrl(item.url);
    if (!safeUrl) return `${item.id || 'artifact'}`;
    const parts = item.url.split('/');
    return parts[parts.length - 1] || `${item.id}`;
  }

  async function copySha(sha, btn) {
    if (!sha) return;
    let copied = false;
    try {
      if (navigator.clipboard && typeof navigator.clipboard.writeText === 'function') {
        await navigator.clipboard.writeText(sha);
        copied = true;
      }
    } catch {
      copied = false;
    }
    if (!copied) {
      const ta = document.createElement('textarea');
      ta.value = sha;
      ta.setAttribute('readonly', '');
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      try {
        copied = document.execCommand('copy');
      } catch {
        copied = false;
      }
      document.body.removeChild(ta);
    }
    if (copied && btn) {
      btn.textContent = msg('copiedSha');
      btn.classList.add('is-copied');
      window.setTimeout(() => {
        btn.textContent = msg('copySha');
        btn.classList.remove('is-copied');
      }, 1800);
    }
  }

  function filteredArtifacts() {
    const list = state.index?.artifacts ?? [];
    const q = state.query.trim().toLowerCase();
    return list.filter((item) => {
      if (state.kind !== 'all' && item.kind !== state.kind) return false;
      if (!q) return true;
      const haystacks = [
        item.id,
        item.name?.zh_cn,
        item.name?.en_us,
        item.description,
        item.publisher,
      ]
        .filter((v) => typeof v === 'string')
        .map((v) => v.toLowerCase());
      return haystacks.some((h) => h.includes(q));
    });
  }

  function renderFilterButtons() {
    dom.kindFilters.textContent = '';
    const labels = MESSAGES[state.lang].kinds;
    for (const kind of KINDS) {
      const btn = el('button', {
        type: 'button',
        className: 'market-btn market-filter-btn',
        'data-kind': kind,
        'aria-pressed': state.kind === kind ? 'true' : 'false',
        textContent: labels[kind],
      });
      btn.addEventListener('click', () => {
        state.kind = kind;
        render();
      });
      dom.kindFilters.appendChild(btn);
    }
  }

  function renderKvRow(label, value) {
    return [
      el('dt', { textContent: label }),
      el('dd', { textContent: value }),
    ];
  }

  function renderArtifactCard(item) {
    const kindsDict = MESSAGES[state.lang].kinds;
    const card = el('article', {
      className: 'market-card',
      'data-artifact-id': item.id,
      'data-kind': item.kind,
    });

    const head = el('div', { className: 'market-card__head' }, [
      el('div', null, [
        el('h2', { className: 'market-card__title', textContent: localizedName(item) }),
        el('code', { className: 'market-code', textContent: item.id }),
      ]),
      el('div', { className: 'market-card__badges' }, [
        el('span', { className: 'market-badge', textContent: kindsDict[item.kind] ?? item.kind }),
        el('span', {
          className: 'market-badge market-badge--neutral',
          textContent: artifactVersionLabel(item),
        }),
      ]),
    ]);

    const desc = el('p', { className: 'market-card__desc', textContent: item.description ?? '' });

    const kv = el('dl', { className: 'market-kv' }, [
      ...renderKvRow(msg('publisher'), item.publisher ?? '—'),
      ...renderKvRow(msg('license'), item.license ?? '—'),
      ...renderKvRow(msg('compatibility'), formatCompatibility(item.compatibility)),
      ...renderKvRow(msg('size'), formatBytes(item.size_bytes)),
    ]);

    const copyBtn = el('button', {
      type: 'button',
      className: 'market-btn market-btn--secondary market-btn--sm',
      'data-action': 'copy-sha',
      textContent: msg('copySha'),
    });
    copyBtn.addEventListener('click', () => {
      void copySha(item.sha256, copyBtn);
    });

    const shaRow = el('div', { className: 'market-sha-row' }, [
      el('code', {
        className: 'market-code',
        title: item.sha256 ?? '',
        'data-testid': 'card-sha256',
        textContent: item.sha256 ?? '',
      }),
      copyBtn,
    ]);

    const detailsBtn = el('button', {
      type: 'button',
      className: 'market-btn market-btn--secondary',
      'data-action': 'details',
      textContent: msg('details'),
    });
    detailsBtn.addEventListener('click', () => {
      openDetailModal(item);
    });

    const actions = el('div', { className: 'market-card__actions' }, [detailsBtn]);
    const safeDownloadUrl = resolveSafeRelativeUrl(item.url);
    if (safeDownloadUrl) {
      const dl = el('a', {
        className: 'market-btn market-btn--primary',
        href: safeDownloadUrl,
        download: downloadFilename(item),
        'data-action': 'download',
        textContent: msg('download'),
      });
      actions.appendChild(dl);
    }

    card.appendChild(head);
    card.appendChild(desc);
    card.appendChild(kv);
    card.appendChild(shaRow);
    card.appendChild(actions);
    return card;
  }

  function renderContractTags(container, list) {
    container.textContent = '';
    if (!Array.isArray(list) || list.length === 0) {
      container.appendChild(
        el('span', { className: 'market-badge market-badge--neutral', textContent: msg('none') }),
      );
      return;
    }
    for (const ref of list) {
      container.appendChild(el('code', { className: 'market-badge market-badge--neutral market-code', textContent: String(ref) }));
    }
  }

  function renderModalContent(item) {
    const kindsDict = MESSAGES[state.lang].kinds;
    dom.modalKind.textContent = kindsDict[item.kind] ?? item.kind;
    dom.modalTitle.textContent = localizedName(item);
    dom.modalId.textContent = item.id ?? '';
    dom.modalDesc.textContent = item.description ?? '';
    dom.modalClose.setAttribute('aria-label', msg('close'));

    dom.modalMeta.textContent = '';
    for (const node of [
      ...renderKvRow(msg('version'), artifactVersionLabel(item)),
      ...renderKvRow(msg('publisher'), item.publisher ?? '—'),
      ...renderKvRow(msg('license'), item.license ?? '—'),
      ...renderKvRow(msg('compatibility'), formatCompatibility(item.compatibility)),
      ...renderKvRow(msg('size'), `${formatBytes(item.size_bytes)} (${item.size_bytes ?? '—'} B)`),
    ]) {
      dom.modalMeta.appendChild(node);
    }

    if (item.kind === 'device_model') {
      dom.modalContracts.hidden = true;
    } else {
      dom.modalContracts.hidden = false;
      dom.modalConsumesTitle.textContent = msg('consumes');
      dom.modalProducesTitle.textContent = msg('produces');
      renderContractTags(dom.modalConsumesList, item.consumes);
      renderContractTags(dom.modalProducesList, item.produces);
    }

    dom.modalShaValue.textContent = item.sha256 ?? '';
    dom.modalCopySha.textContent = msg('copySha');
    dom.modalCopySha.classList.remove('is-copied');

    const safeDownloadUrl = resolveSafeRelativeUrl(item.url);
    if (safeDownloadUrl) {
      dom.modalDownload.href = safeDownloadUrl;
      dom.modalDownload.setAttribute('download', downloadFilename(item));
      dom.modalDownload.hidden = false;
      dom.modalDownload.textContent = msg('download');
    } else {
      dom.modalDownload.hidden = true;
    }

    dom.modalReadmeTitle.textContent = msg('readmeTitle');
  }

  function openDetailModal(item) {
    if (state.readmeAbort) {
      state.readmeAbort.abort();
      state.readmeAbort = null;
    }
    state.activeArtifact = item;
    renderModalContent(item);

    dom.modalReadmeError.hidden = true;
    dom.modalReadmeError.textContent = '';

    const safeReadmeUrl = resolveSafeRelativeUrl(item.readme_url);
    if (!safeReadmeUrl) {
      dom.modalReadmeBody.textContent = msg('readmeNone');
    } else {
      dom.modalReadmeBody.textContent = msg('readmeLoading');
      const controller = new AbortController();
      state.readmeAbort = controller;
      fetch(safeReadmeUrl, { cache: 'no-store', signal: controller.signal })
        .then(async (res) => {
          if (!res.ok) {
            throw new Error(`HTTP ${res.status}`);
          }
          return res.text();
        })
        .then((text) => {
          if (state.activeArtifact !== item) return;
          // 严格使用 textContent，禁止 innerHTML 防止外部 README 注入 XSS
          dom.modalReadmeBody.textContent = text;
        })
        .catch((err) => {
          if (controller.signal.aborted || state.activeArtifact !== item) return;
          dom.modalReadmeBody.textContent = '';
          dom.modalReadmeError.hidden = false;
          dom.modalReadmeError.textContent = msg('readmeError', {
            reason: err instanceof Error ? err.message : String(err),
          });
        });
    }

    if (typeof dom.modal.showModal === 'function') {
      if (!dom.modal.open) dom.modal.showModal();
    } else {
      dom.modal.setAttribute('open', '');
    }
  }

  function closeDetailModal() {
    if (state.readmeAbort) {
      state.readmeAbort.abort();
      state.readmeAbort = null;
    }
    state.activeArtifact = null;
    if (typeof dom.modal.close === 'function') {
      if (dom.modal.open) dom.modal.close();
    } else {
      dom.modal.removeAttribute('open');
    }
  }

  function render() {
    document.documentElement.lang = state.lang === 'zh_cn' ? 'zh-CN' : 'en-US';
    document.title = msg('pageTitle');
    dom.subtitle.textContent = msg('subtitle');
    dom.langZh.setAttribute('aria-pressed', state.lang === 'zh_cn' ? 'true' : 'false');
    dom.langEn.setAttribute('aria-pressed', state.lang === 'en_us' ? 'true' : 'false');
    dom.searchLabel.textContent = msg('searchLabel');
    dom.searchInput.placeholder = msg('searchPlaceholder');
    dom.retryBtn.textContent = msg('retry');
    dom.loadingBox.textContent = msg('loading');

    renderFilterButtons();

    if (state.loading) {
      dom.loadingBox.hidden = false;
      dom.statusAlert.hidden = true;
      dom.emptyBox.hidden = true;
      dom.artifactGrid.textContent = '';
      dom.catalogCount.textContent = '';
      dom.catalogUpdated.textContent = '';
      return;
    }

    dom.loadingBox.hidden = true;

    if (state.error) {
      // 加载 ./index.json 失败时只展示显式错误告警，严禁降级为空列表
      dom.statusAlert.hidden = false;
      dom.statusAlertTitle.textContent = msg('loadErrorTitle');
      dom.statusAlertDetail.textContent =
        state.error.type === 'http'
          ? msg('loadErrorStatus', { status: state.error.status })
          : state.error.type === 'contract'
            ? msg('loadErrorContract')
            : state.error.message;
      dom.emptyBox.hidden = true;
      dom.artifactGrid.textContent = '';
      dom.catalogCount.textContent = '';
      dom.catalogUpdated.textContent = '';
      return;
    }

    dom.statusAlert.hidden = true;
    const all = state.index?.artifacts ?? [];
    const items = filteredArtifacts();

    dom.catalogCount.textContent = msg('countText', { shown: items.length, total: all.length });
    dom.catalogUpdated.textContent = state.index?.generated_at
      ? msg('generatedAt', { time: state.index.generated_at })
      : '';

    dom.artifactGrid.textContent = '';
    if (items.length === 0) {
      dom.emptyBox.hidden = false;
      dom.emptyBox.textContent = all.length === 0 ? msg('emptyCatalog') : msg('emptyFiltered');
    } else {
      dom.emptyBox.hidden = true;
      for (const item of items) {
        dom.artifactGrid.appendChild(renderArtifactCard(item));
      }
    }

    if (state.activeArtifact) {
      renderModalContent(state.activeArtifact);
    }
  }

  async function loadIndex() {
    state.loading = true;
    state.error = null;
    render();
    try {
      const res = await fetch('./index.json', { cache: 'no-store' });
      if (!res.ok) {
        state.error = { type: 'http', status: res.status };
        state.index = null;
        return;
      }
      const data = await res.json();
      if (
        !data ||
        typeof data !== 'object' ||
        data.schema !== 'ies.market-index' ||
        data.schema_version !== '1.0.0' ||
        !Array.isArray(data.artifacts)
      ) {
        state.error = { type: 'contract' };
        state.index = null;
        return;
      }
      state.index = data;
    } catch (err) {
      state.error = {
        type: 'network',
        message: err instanceof Error ? err.message : String(err),
      };
      state.index = null;
    } finally {
      state.loading = false;
      render();
    }
  }

  dom.langZh.addEventListener('click', () => {
    state.lang = 'zh_cn';
    render();
  });
  dom.langEn.addEventListener('click', () => {
    state.lang = 'en_us';
    render();
  });
  dom.searchInput.addEventListener('input', (ev) => {
    state.query = ev.target.value;
    render();
  });
  dom.retryBtn.addEventListener('click', () => {
    void loadIndex();
  });
  dom.modalClose.addEventListener('click', closeDetailModal);
  dom.modal.addEventListener('click', (ev) => {
    if (ev.target === dom.modal) closeDetailModal();
  });
  dom.modal.addEventListener('cancel', () => {
    closeDetailModal();
  });
  dom.modalCopySha.addEventListener('click', () => {
    if (state.activeArtifact) {
      void copySha(state.activeArtifact.sha256, dom.modalCopySha);
    }
  });

  void loadIndex();
})();
