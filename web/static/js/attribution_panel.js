/* 项目归属规则配置面板（数据识别 step3 用）
 *
 * 用法：在 classify_*.html 末尾调用
 *   AttributionPanel.init({
 *     container_id: 'step3-body',          // 渲染目标 div id
 *     project_id: PID,
 *     category: 'kaoqin_bill',              // 'kaoqin_bill' | 'wage' | 'payroll'
 *     format_id: FORMAT_ID,                 // null=老模式；string=format 模式
 *     show_enterprise: true                 // 是否显示 enterprise scope（仅 kaoqin_bill = true）
 *   });
 *
 * 数据结构与 etl.views.attribution.get_rules / etl.actions.attribution.save_all_rules 对齐
 * 保存：在 saveAllRules 里调 AttributionPanel.collectAndSave()
 */
(function () {
  const escape = s => String(s == null ? '' : s).replace(/[&<>"']/g, c =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

  const parseKws = input => (input || '')
    .replace(/，/g, ',').replace(/;/g, ',').split(/[\s,]+/)
    .map(s => s.trim()).filter(Boolean);

  const fmt = s => s ? s.replace('T', ' ').slice(0, 16) : '--';

  let _opts = null;
  let _rulesData = null;

  function init(opts) {
    _opts = opts;
    const container = document.getElementById(opts.container_id);
    if (!container) { console.warn('attribution_panel: container not found:', opts.container_id); return; }
    container.innerHTML = renderHtml(opts);
    load();
  }

  function renderHtml(opts) {
    const enterpriseBlock = opts.show_enterprise ? `
      <div class="ap-section mb-3">
        <div class="alert alert-light border py-2 small mb-2">
          <strong>① 企业过滤</strong>（识别本企业行；外企业行直接丢弃）—— 多劳务公司混在同一份时启用
        </div>
        <div class="card mb-2" style="border:1px solid #e9ecef;">
          <div class="card-header py-2 d-flex justify-content-between align-items-center">
            <div><strong>Sheet 规则</strong> <span class="text-muted small">按 sheet 名整页归属/排除</span></div>
            <div class="d-flex align-items-center gap-2">
              <select class="form-select form-select-sm ap-ent-sheet-mode" style="width:120px;">
                <option value="include">包含子串</option>
                <option value="exclude">排除子串</option>
                <option value="eq">精确等于</option>
                <option value="neq">精确不等</option>
              </select>
              <div class="form-check form-switch mb-0">
                <input class="form-check-input ap-ent-sheet-enabled" type="checkbox">
                <label class="form-check-label small">启用</label>
              </div>
            </div>
          </div>
          <div class="card-body py-2">
            <input type="text" class="form-control form-control-sm ap-ent-sheet-keywords" placeholder="如：梦寺达, 长隆 — 多个用逗号分隔">
            <div class="text-muted small mt-1 ap-ent-sheet-stat">--</div>
          </div>
        </div>
        <div class="card mb-2" style="border:1px solid #e9ecef;">
          <div class="card-header py-2"><strong>行级规则</strong> <span class="text-muted small">按"文件特征列"识别文件类型 → 在"提取/过滤列"里看是否含"关键词" → 决定行的归属</span></div>
          <table class="table table-sm mb-0" style="font-size:13px;">
            <thead class="table-light"><tr>
              <th style="width:50px;">启用</th>
              <th style="width:80px;">模式</th>
              <th style="width:200px;">文件特征列<br><span class="text-muted small fw-normal">空格分隔，空=所有文件</span></th>
              <th style="width:150px;">提取/过滤列</th>
              <th>关键词</th>
              <th style="width:130px;">最近匹配</th>
            </tr></thead>
            <tbody class="ap-ent-col-rows"></tbody>
          </table>
        </div>
      </div>
      <hr>` : '';
    const sheetCardLabel = opts.show_enterprise ? '② 项目过滤（识别本项目行）' : '项目过滤（识别本项目行）';
    const sheetBlock = `
      <div class="card mb-2" style="border:1px solid #e9ecef;">
        <div class="card-header py-2 d-flex justify-content-between align-items-center">
          <div><strong>Sheet 规则</strong> <span class="text-muted small">按 sheet 名整页归属/排除</span></div>
          <div class="d-flex align-items-center gap-2">
            <select class="form-select form-select-sm ap-prj-sheet-mode" style="width:120px;">
              <option value="include">包含子串</option>
              <option value="exclude">排除子串</option>
              <option value="eq">精确等于</option>
              <option value="neq">精确不等</option>
            </select>
            <div class="form-check form-switch mb-0">
              <input class="form-check-input ap-prj-sheet-enabled" type="checkbox">
              <label class="form-check-label small">启用</label>
            </div>
          </div>
        </div>
        <div class="card-body py-2">
          <input type="text" class="form-control form-control-sm ap-prj-sheet-keywords" placeholder="如：动物园, 动物世界">
          <div class="text-muted small mt-1 ap-prj-sheet-stat">--</div>
        </div>
      </div>`;
    return `
      ${enterpriseBlock}
      <div class="ap-section">
        <div class="alert alert-light border py-2 small mb-2">
          <strong>${escape(sheetCardLabel)}</strong>
        </div>
        ${sheetBlock}
        <div class="card mb-2" style="border:1px solid #e9ecef;">
          <div class="card-header py-2"><strong>行级规则</strong> <span class="text-muted small">按"文件特征列"识别文件类型 → 在"提取/过滤列"里看是否含"关键词" → 决定行的归属</span></div>
          <table class="table table-sm mb-0" style="font-size:13px;">
            <thead class="table-light"><tr>
              <th style="width:50px;">启用</th>
              <th style="width:80px;">模式</th>
              <th style="width:200px;">文件特征列<br><span class="text-muted small fw-normal">空格分隔，空=所有文件</span></th>
              <th style="width:150px;">提取/过滤列</th>
              <th>关键词</th>
              <th style="width:130px;">最近匹配</th>
            </tr></thead>
            <tbody class="ap-prj-col-rows"></tbody>
          </table>
        </div>
      </div>`;
  }

  function load() {
    const qs = _opts.format_id ? `?format_id=${_opts.format_id}` : '';
    return fetch(`/api/projects/${_opts.project_id}/attribution${qs}`)
      .then(r => r.json())
      .then(d => {
        _rulesData = (d && d.rules) || {};
        render();
      });
  }

  function renderColumnRows($tbody, cellList) {
    const cols = (cellList || []).slice(0, 5);
    while (cols.length < 5) cols.push({});
    $tbody.innerHTML = cols.map((c, i) => {
      const stat = c.last_matched_at ? `${fmt(c.last_matched_at)} · ${c.match_count || 0} 次` : '—';
      return `<tr>
        <td class="text-center"><input type="checkbox" class="form-check-input ap-col-enabled" ${c.enabled ? 'checked' : ''}></td>
        <td><select class="form-select form-select-sm ap-col-mode">
          <option value="include" ${c.mode === 'include' || !c.mode ? 'selected' : ''}>包含</option>
          <option value="exclude" ${c.mode === 'exclude' ? 'selected' : ''}>排除</option>
          <option value="eq" ${c.mode === 'eq' ? 'selected' : ''}>等于</option>
          <option value="neq" ${c.mode === 'neq' ? 'selected' : ''}>不等于</option>
        </select></td>
        <td><input type="text" class="form-control form-control-sm ap-col-fcols" value="${escape(c.file_columns || '')}"></td>
        <td><input type="text" class="form-control form-control-sm ap-col-name" value="${escape(c.column_names || '')}"></td>
        <td><input type="text" class="form-control form-control-sm ap-col-kws" value="${escape((c.keywords || []).join(', '))}"></td>
        <td class="small text-muted">${stat}</td>
      </tr>`;
    }).join('');
  }

  function renderSheet($card, sheetData) {
    const root = $card;
    if (!root) return;
    const data = sheetData || {};
    root.querySelector('.form-control')?.setAttribute('value', (data.keywords || []).join(', '));
    const inp = root.querySelector('.form-control');
    if (inp) inp.value = (data.keywords || []).join(', ');
    const cb = root.querySelector('.form-check-input');
    if (cb) cb.checked = !!data.enabled;
    const stat = root.querySelector('.text-muted.small.mt-1') || root.querySelector('.ap-ent-sheet-stat') || root.querySelector('.ap-prj-sheet-stat');
    if (stat) stat.textContent = data.last_matched_at
      ? `最近匹配 ${fmt(data.last_matched_at)}（累计 ${data.match_count || 0} 次）`
      : '尚无命中记录';
  }

  function render() {
    const cat = _opts.category;
    const catData = _rulesData[cat] || {};
    const container = document.getElementById(_opts.container_id);

    if (_opts.show_enterprise) {
      const ent = catData.enterprise || {};
      const entSheetInput = container.querySelector('.ap-ent-sheet-keywords');
      const entSheetCb = container.querySelector('.ap-ent-sheet-enabled');
      const entSheetMode = container.querySelector('.ap-ent-sheet-mode');
      const entSheetStat = container.querySelector('.ap-ent-sheet-stat');
      if (ent.sheet) {
        if (entSheetInput) entSheetInput.value = (ent.sheet.keywords || []).join(', ');
        if (entSheetCb) entSheetCb.checked = !!ent.sheet.enabled;
        if (entSheetMode) entSheetMode.value = ent.sheet.mode || 'include';
        if (entSheetStat) entSheetStat.textContent = ent.sheet.last_matched_at
          ? `最近匹配 ${fmt(ent.sheet.last_matched_at)}（累计 ${ent.sheet.match_count || 0} 次）` : '尚无命中记录';
      }
      const entColBody = container.querySelector('.ap-ent-col-rows');
      if (entColBody) renderColumnRows(entColBody, ent.column);
    }

    const prj = catData.project || {};
    const prjSheetInput = container.querySelector('.ap-prj-sheet-keywords');
    if (prjSheetInput && prj.sheet) {
      prjSheetInput.value = (prj.sheet.keywords || []).join(', ');
      const cb = container.querySelector('.ap-prj-sheet-enabled');
      if (cb) cb.checked = !!prj.sheet.enabled;
      const mode = container.querySelector('.ap-prj-sheet-mode');
      if (mode) mode.value = prj.sheet.mode || 'include';
      const stat = container.querySelector('.ap-prj-sheet-stat');
      if (stat) stat.textContent = prj.sheet.last_matched_at
        ? `最近匹配 ${fmt(prj.sheet.last_matched_at)}（累计 ${prj.sheet.match_count || 0} 次）` : '尚无命中记录';
    }
    const prjColBody = container.querySelector('.ap-prj-col-rows');
    if (prjColBody) renderColumnRows(prjColBody, prj.column);
  }

  function collectScope(container, prefix) {
    // prefix: 'ent' | 'prj'
    let sheet = null;
    const sheetInput = container.querySelector(`.ap-${prefix}-sheet-keywords`);
    if (sheetInput) {
      sheet = {
        keywords: parseKws(sheetInput.value),
        enabled: container.querySelector(`.ap-${prefix}-sheet-enabled`)?.checked || false,
        mode: container.querySelector(`.ap-${prefix}-sheet-mode`)?.value || 'include',
      };
    }
    const columns = [];
    const tbody = container.querySelector(`.ap-${prefix}-col-rows`);
    if (tbody) {
      tbody.querySelectorAll('tr').forEach(tr => {
        const cn = tr.querySelector('.ap-col-name')?.value.trim() || '';
        const fc = tr.querySelector('.ap-col-fcols')?.value.trim() || '';
        const kws = parseKws(tr.querySelector('.ap-col-kws')?.value);
        const enabled = tr.querySelector('.ap-col-enabled')?.checked || false;
        const mode = tr.querySelector('.ap-col-mode')?.value || 'include';
        if (!fc && !cn && !kws.length) return;
        columns.push({column_names: cn, file_columns: fc, keywords: kws, enabled, mode});
      });
    }
    return {sheet, columns};
  }

  async function collectAndSave() {
    if (!_opts) return {saved: 0};
    const container = document.getElementById(_opts.container_id);
    if (!container) return {saved: 0};
    const payload = {rules: {}, format_id: _opts.format_id || null};
    payload.rules[_opts.category] = {};
    if (_opts.show_enterprise) {
      payload.rules[_opts.category].enterprise = collectScope(container, 'ent');
    }
    payload.rules[_opts.category].project = collectScope(container, 'prj');
    const r = await fetch(`/api/projects/${_opts.project_id}/attribution`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload),
    });
    return await r.json();
  }

  window.AttributionPanel = {init, load, collectAndSave};
})();
