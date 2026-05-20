/* 项目注册页：调 /api/registry/* */
$(function () {
  const $tbody = $('#tbody-projects');
  let currentStatus = 'registered';
  let currentCompany = '';
  let currentKeyword = '';
  let editingProject = null; // {project_id, ...} 表示编辑模式

  function formatTime(s) { return s ? s.replace('T', ' ').slice(0, 16) : '--'; }
  function fmtRatio(r) { return r === null || r === undefined ? '--' : Number(r).toFixed(2).replace(/\.?0+$/, ''); }
  function debounce(fn, ms) {
    let t;
    return function (...args) { clearTimeout(t); t = setTimeout(() => fn.apply(this, args), ms); };
  }

  function statusBadge(s) {
    if (s === 'registered') return '<span class="badge bg-success">已注册</span>';
    if (s === 'unregistered') return '<span class="badge bg-warning text-dark">未注册</span>';
    if (s === 'disabled') return '<span class="badge bg-secondary">已注销</span>';
    return s;
  }

  function rowActions(p) {
    const pid = String(p.project_id);
    // 数据识别统一走 format 管理（老 classify 入口已停用）
    const settings = `<a href="/projects/${pid}/formats" class="btn btn-sm btn-outline-secondary py-0 px-2">数据识别</a>`;
    if (p.reg_status === 'registered') {
      return `${settings}
              <button class="btn btn-sm btn-outline-secondary py-0 px-2 act-edit" data-id="${pid}">编辑</button>
              <button class="btn btn-sm btn-outline-danger py-0 px-2 act-disable" data-id="${pid}">注销</button>`;
    }
    if (p.reg_status === 'unregistered') {
      return `${settings}
              <button class="btn btn-sm btn-primary py-0 px-3 act-register" data-id="${pid}">注册</button>`;
    }
    if (p.reg_status === 'disabled') {
      return `${settings}
              <button class="btn btn-sm btn-outline-secondary py-0 px-2 act-enable" data-id="${pid}">恢复</button>`;
    }
    return '';
  }

  function renderProjects(projects) {
    $('#result-count').text(projects.length);
    if (projects.length === 0) {
      $tbody.html('<tr><td colspan="9" class="text-center text-muted py-4">无匹配项目</td></tr>');
      return;
    }
    const rows = projects.map(p => {
      const pid = String(p.project_id);
      const trCls = p.reg_status === 'unregistered' ? 'table-warning'
                  : p.reg_status === 'disabled' ? 'text-muted' : '';
      return `<tr class="${trCls}" data-id="${pid}">
        <td>${escape(p.enterprise_short || '')}</td>
        <td><strong>${escape(p.project_title || '')}</strong></td>
        <td><code>${pid}</code></td>
        <td>${statusBadge(p.reg_status)}</td>
        <td>${p.business_cycle ? escape(p.business_cycle) : '<span class="text-muted">—</span>'}</td>
        <td class="text-end">${p.daishou_threshold == null ? '<span class="text-muted">—</span>' : Number(p.daishou_threshold).toLocaleString()}</td>
        <td class="text-end">${p.reg_status === 'unregistered' ? '<span class="text-muted">—</span>' : fmtRatio(p.profit_ratio)}</td>
        <td>${escape(p.controller_name || '') || '<span class="text-muted">—</span>'}</td>
        <td class="text-end">${rowActions(p)}</td>
      </tr>`;
    });
    $tbody.html(rows.join(''));
  }

  function escape(s) {
    return String(s).replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
  }

  function refreshAll() {
    $.get('/api/registry/sync-status', s => {
      $('#stat-total').text(s.total);
      $('#stat-registered').text(s.registered);
      $('#stat-unregistered').text(s.unregistered);
      $('#last-sync-at').text(formatTime(s.last_sync_at));
      if (s.last_sync_at) {
        $('#input-since-date').val(s.last_sync_at.slice(0, 10));
      }
    });
    $.get('/api/registry/companies', s => {
      const $sel = $('#filter-company');
      const cur = $sel.val();
      $sel.empty().append('<option></option>');
      s.companies.forEach(c => $sel.append(new Option(c, c)));
      $sel.val(cur || '').trigger('change.select2');
    });
    refreshList();
  }

  function refreshList() {
    const params = {};
    if (currentStatus) params.status = currentStatus;
    if (currentCompany) params.company = currentCompany;
    if (currentKeyword) params.keyword = currentKeyword;
    $tbody.html('<tr><td colspan="9" class="text-center text-muted py-4">加载中…</td></tr>');
    $.get('/api/registry/list', params, s => renderProjects(s.projects));
  }

  // ===== 同步 =====
  $('#btn-sync-incremental').on('click', function () {
    const since = $('#input-since-date').val();
    if (!since) { alert('请先选日期'); return; }
    const $btn = $(this).prop('disabled', true).text('拉取中…');
    $.ajax({
      url: '/api/registry/sync-incremental', method: 'POST', contentType: 'application/json',
      data: JSON.stringify({since_date: since})
    })
      .done(r => {
        $('#sync-result').removeClass('d-none alert-danger').addClass('alert-success').text(
          `增量同步完成（${since} 起）：企业 ${r.enterprises_inserted} 新增；项目 ${r.projects_inserted} 新增`
        );
        refreshAll();
      })
      .fail(xhr => $('#sync-result').removeClass('d-none alert-success').addClass('alert-danger').text('拉取失败：' + (xhr.responseJSON?.error || xhr.statusText)))
      .always(() => $btn.prop('disabled', false).text('📅 增量拉取'));
  });

  $('#btn-sync-one').on('click', function () {
    const ent = $('#input-ent-keyword').val().trim();
    const proj = $('#input-proj-keyword').val().trim();
    if (!ent && !proj) { alert('劳务公司 / 项目名 至少填一个'); return; }
    const $btn = $(this).prop('disabled', true).text('拉取中…');
    $.ajax({
      url: '/api/registry/sync-one', method: 'POST', contentType: 'application/json',
      data: JSON.stringify({enterprise_keyword: ent, project_keyword: proj})
    })
      .done(r => {
        const filt = [ent && `劳务公司"${ent}"`, proj && `项目"${proj}"`].filter(Boolean).join(' + ');
        $('#sync-result').removeClass('d-none alert-danger').addClass('alert-success').text(
          `按 ${filt} 拉取完成：企业 ${r.enterprises_inserted} 新增 / ${r.enterprises_updated} 更新；项目 ${r.projects_inserted} 新增 / ${r.projects_updated} 更新`
        );
        refreshAll();
      })
      .fail(xhr => $('#sync-result').removeClass('d-none alert-success').addClass('alert-danger').text('拉取失败：' + (xhr.responseJSON?.error || xhr.statusText)))
      .always(() => $btn.prop('disabled', false).text('🎯 按项目拉取'));
  });

  // ===== 筛选 =====
  // 初始化按钮高亮（防止 HTML 默认 vs JS currentStatus 不一致）
  $('#status-filter button').removeClass('btn-primary').addClass('btn-outline-primary');
  $(`#status-filter button[data-status="${currentStatus}"]`).removeClass('btn-outline-primary').addClass('btn-primary');

  $('#status-filter').on('click', 'button', function () {
    $('#status-filter button').removeClass('btn-primary').addClass('btn-outline-primary');
    $(this).removeClass('btn-outline-primary').addClass('btn-primary');
    currentStatus = $(this).data('status');
    refreshList();
  });
  $('#filter-company').select2({
    theme: 'bootstrap-5',
    placeholder: '全部劳务公司',
    allowClear: true,
    width: '200px',
    language: 'zh-CN',
  });
  $('#filter-company').on('select2:select select2:clear', function () {
    currentCompany = $(this).val() || '';
    refreshList();
  });
  $('#filter-keyword').on('input', debounce(function () { currentKeyword = $(this).val(); refreshList(); }, 250));

  // ===== Modal =====
  function openModal(project, isEdit) {
    editingProject = isEdit ? project : null;
    $('#modal-title').text(isEdit ? '编辑项目' : '注册项目');
    $('#modal-submit').text(isEdit ? '保存修改' : '完成注册');
    const pid = String(project.project_id);
    $('#modal-register').attr('data-pid', pid);
    $('#modal-summary').html(`${escape(project.enterprise_short)} · <strong>${escape(project.project_title)}</strong> · 线上 ID <code>${pid}</code>`);
    $('#modal-controller').val(project.controller_name || '（无）');
    $('#modal-error').addClass('d-none').text('');

    let cycleKey = 'natural', customDay = '';
    if (project.business_cycle === '自然月') cycleKey = 'natural';
    else if (project.business_cycle === '上月26-本月25') cycleKey = '26_25';
    else if (project.business_cycle) {
      cycleKey = 'custom';
      const m = /上月(\d+)-本月/.exec(project.business_cycle);
      if (m) customDay = m[1];
    }
    $('#modal-cycle').val(cycleKey);
    $('#modal-custom-day').val(customDay);
    $('#modal-custom-block').toggleClass('d-none', cycleKey !== 'custom');

    $('#modal-daishou').val(project.daishou_threshold ?? 2000);
    $('#modal-profit').val(project.profit_ratio ?? 0.8);

    $('#modal-register').addClass('show');
  }
  function closeModal() { $('#modal-register').removeClass('show'); editingProject = null; }
  $('#modal-close, #modal-cancel').on('click', closeModal);
  $('#modal-cycle').on('change', function () {
    $('#modal-custom-block').toggleClass('d-none', $(this).val() !== 'custom');
  });

  $('#modal-submit').on('click', function () {
    const project_id = $('#modal-register').attr('data-pid');
    const cycle_key = $('#modal-cycle').val();
    const daishou = $('#modal-daishou').val();
    const profit = $('#modal-profit').val();
    const custom = $('#modal-custom-day').val();
    if (cycle_key === 'custom' && (!custom || custom < 1 || custom > 31)) {
      $('#modal-error').removeClass('d-none').text('自定义起始日必须在 1~31 之间');
      return;
    }
    const payload = {project_id, cycle_key, daishou_threshold: parseInt(daishou), profit_ratio: parseFloat(profit)};
    if (cycle_key === 'custom') payload.custom_start_day = parseInt(custom);

    const url = editingProject ? '/api/registry/update' : '/api/registry/register';
    $.ajax({url, method: 'POST', contentType: 'application/json', data: JSON.stringify(payload)})
      .done(() => { closeModal(); refreshAll(); })
      .fail(xhr => $('#modal-error').removeClass('d-none').text(xhr.responseJSON?.error || xhr.statusText));
  });

  // ===== 行操作 =====
  function findProject(id) {
    const $tr = $(`#tbody-projects tr[data-id="${id}"]`);
    return {
      project_id: id,
      enterprise_short: $tr.find('td:eq(0)').text(),
      project_title: $tr.find('td:eq(1)').text(),
      reg_status: $tr.find('td:eq(3)').text().trim(),
      business_cycle: $tr.find('td:eq(4)').text().trim().replace('—', ''),
      daishou_threshold: parseInt($tr.find('td:eq(5)').text().replace(/[^\d]/g, '')) || 2000,
      profit_ratio: parseFloat($tr.find('td:eq(6)').text()) || 0.8,
      controller_name: $tr.find('td:eq(7)').text().trim().replace('—', ''),
    };
  }

  $tbody.on('click', '.act-register', function () {
    const id = $(this).attr('data-id'); openModal(findProject(id), false);
  });
  $tbody.on('click', '.act-edit', function () {
    const id = $(this).attr('data-id'); openModal(findProject(id), true);
  });
  $tbody.on('click', '.act-disable', function () {
    if (!confirm('确认注销该项目？注销后将不能在出款申请中选用')) return;
    $.ajax({url: '/api/registry/disable', method: 'POST', contentType: 'application/json', data: JSON.stringify({project_id: $(this).attr('data-id')})})
      .done(refreshAll)
      .fail(xhr => alert('失败：' + (xhr.responseJSON?.error || xhr.statusText)));
  });
  $tbody.on('click', '.act-enable', function () {
    $.ajax({url: '/api/registry/enable', method: 'POST', contentType: 'application/json', data: JSON.stringify({project_id: $(this).attr('data-id')})})
      .done(refreshAll)
      .fail(xhr => alert('失败：' + (xhr.responseJSON?.error || xhr.statusText)));
  });

  refreshAll();
});
