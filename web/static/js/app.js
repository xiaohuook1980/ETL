/* 出款申请页 — 重构版（A2/A3/A1/D1/B 阶段） */
$(function () {
    /* ========== 工具 ========== */
    function escape(s) {
        return String(s == null ? '' : s).replace(/[&<>"']/g, c =>
            ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
    }
    function fmtMoney(v) {
        if (v == null) return '--';
        var n = typeof v === 'number' ? v : parseFloat(v);
        if (isNaN(n)) return v;
        return '¥ ' + Math.round(n).toLocaleString('zh-CN');
    }
    function badge(text, cls) { return '<span class="badge bg-' + cls + '">' + escape(text) + '</span>'; }

    /* ========== 状态 ========== */
    var currentMode = null;       // 'normal' / 'prepay' / null
    var currentProjectId = null;  // 字符串（避免 JS Number 精度丢失）
    var lastDataStatus = null;    // 最近一次 /api/data-status 返回值

    /* ========== Select2 初始化 + 默认日期 ========== */
    /* 从 localStorage 取上次选择（跨 tab/刷新持久化）*/
    var lastParams = null;
    try { lastParams = JSON.parse(localStorage.getItem('paymentFormParams') || 'null'); } catch (e) {}

    /* 申请日期：用户上次的，否则今天 */
    $('#input-date').val((lastParams && lastParams.date) || new Date().toISOString().split('T')[0]);

    var select2Opts = {theme: 'bootstrap-5', language: 'zh-CN', width: '100%'};
    $('#sel-company').select2($.extend({placeholder: '搜索或选择劳务公司...'}, select2Opts));
    $('#sel-project').select2($.extend({placeholder: '请先选择公司'}, select2Opts));
    $('#sel-month').select2($.extend({placeholder: '请选择月份', minimumResultsForSearch: 0}, select2Opts));

    /* 即时保存表单选择到 localStorage（不等"开始审核"才存）*/
    function savePaymentFormParams() {
        try {
            localStorage.setItem('paymentFormParams', JSON.stringify({
                company: $('#sel-company').val() || '',
                project_id: $('#sel-project').val() || '',
                month: $('#sel-month').val() || '',
                date: $('#input-date').val() || '',
            }));
        } catch (e) {}
    }

    /* ========== 公司/项目/月份 加载 ========== */
    $.getJSON('/api/companies', function (data) {
        var $sel = $('#sel-company');
        $sel.empty().append('<option value=""></option>');
        (data.companies || []).forEach(c => $sel.append('<option value="' + escape(c) + '">' + escape(c) + '</option>'));
        if (lastParams && lastParams.company && (data.companies || []).indexOf(lastParams.company) >= 0) {
            $sel.val(lastParams.company).trigger('change');
        } else {
            $sel.trigger('change.select2');
        }
    });

    $.getJSON('/api/months', function (data) {
        var $sel = $('#sel-month');
        $sel.empty();
        var now = new Date();
        var defaultKey = (lastParams && lastParams.month)
                         || (now.getFullYear() + '年' + (now.getMonth() + 1) + '月');
        (data.months || []).forEach(m => {
            var opt = $('<option>').val(m.key).text(m.label);
            if (m.key === defaultKey) opt.prop('selected', true);
            $sel.append(opt);
        });
        $sel.trigger('change.select2');
        checkReady();
        updateBmButtons();
    });

    /* 按项目业务周期重新格式化月份下拉 label
     *   自然月 → "2026年5月"（保持）
     *   上月26-本月25 → "2026年5月（4.26-5.25）"
     *   自定义起始 N → "2026年5月（4.N-5.(N-1)）"
     * key（option.value）保持 "YYYY年M月" 不变，后端识别用 */
    function relabelMonthOptions(cycle) {
        var startDay = 1;  // 自然月默认 1
        if (cycle && /26.*25/.test(cycle)) {
            startDay = 26;
        } else if (cycle) {
            var mt = /(\d+)/.exec(cycle);
            if (mt && +mt[1] >= 2 && +mt[1] <= 31) startDay = +mt[1];
        }
        $('#sel-month option').each(function () {
            var key = $(this).val();
            var m = /(\d{4})年(\d{1,2})月/.exec(key);
            if (!m) return;
            var y = +m[1], mo = +m[2];
            if (startDay === 1) {
                $(this).text(key);
            } else {
                var prevM = mo === 1 ? 12 : mo - 1;
                var endDay = startDay - 1;
                $(this).text(`${y}年${mo}月（${prevM}.${startDay} - ${mo}.${endDay}）`);
            }
        });
        $('#sel-month').trigger('change.select2');
    }

    /* 按钮文字随业务月动态更新 */
    function updateBmButtons() {
        var monthKey = $('#sel-month').val() || '';
        var $fetch = $('#btn-fetch-core');
        var $del = $('#btn-delete-bm');
        if (monthKey) {
            if (!$fetch.prop('disabled')) $fetch.text('拉取 ' + monthKey + ' 数据');
            $del.prop('disabled', false).text('删除 ' + monthKey + ' 数据');
        } else {
            if (!$fetch.prop('disabled')) $fetch.text('拉取数据');
            $del.prop('disabled', true).text('删除数据');
        }
    }

    $('#sel-company').on('change', function () {
        var company = $(this).val();
        var $proj = $('#sel-project');
        $proj.empty().append('<option value=""></option>').prop('disabled', true).trigger('change.select2');
        resetRight();
        currentProjectId = null;
        $('#link-unit-price, #link-classify-config').addClass('d-none');
        if (!company) {
            $proj.select2($.extend({placeholder: '请先选择公司'}, select2Opts));
            checkReady();
            return;
        }
        $.getJSON('/api/projects', {company: company}, function (data) {
            // option value = project_id（唯一标识）；label = title
            window._currentProjects = data._full || [];
            $proj.empty().append('<option value=""></option>');
            (data._full || []).forEach(p => {
                $proj.append('<option value="' + escape(p.project_id) + '">' + escape(p.title) + '</option>');
            });
            $proj.prop('disabled', false);
            $proj.select2($.extend({placeholder: '搜索或选择项目...'}, select2Opts));
            if (lastParams && lastParams.company === company && lastParams.project_id
                && (data._full || []).some(p => String(p.project_id) === String(lastParams.project_id))) {
                $proj.val(String(lastParams.project_id)).trigger('change');
            }
            checkReady();
        });
        savePaymentFormParams();
    });

    $('#sel-project').on('change', function () {
        resetRight();
        var pid = $(this).val();
        var hit = (window._currentProjects || []).find(p => String(p.project_id) === String(pid));
        currentProjectId = hit ? hit.project_id : null;
        // 回填代收阈值 / 出款比例（项目设置里配的值）
        if (hit) {
            if (hit.daishou_threshold != null) $('#input-daishou-threshold').val(hit.daishou_threshold);
            if (hit.profit_ratio != null) $('#input-profit-ratio').val(hit.profit_ratio);
        }
        // 按项目业务周期重新格式化月份下拉 label（自然月不变，非自然月加"X.D-Y.D"标识）
        relabelMonthOptions(hit && hit.business_cycle);
        // 数据识别配置：统一跳 format 管理页（老 classify 入口已停用）
        var $cfgLink = $('#link-classify-config');
        if (currentProjectId) {
            $cfgLink.attr('href', '/projects/' + currentProjectId + '/formats').removeClass('d-none');
        } else {
            $cfgLink.addClass('d-none');
        }
        // 单价配置链接：选了项目就直接跳详情页，否则隐藏
        var $upLink = $('#link-unit-price');
        if (currentProjectId) {
            $upLink.attr('href', '/unit-prices/' + currentProjectId).removeClass('d-none');
        } else {
            $upLink.addClass('d-none');
        }
        checkReady();
        savePaymentFormParams();
        // 查该项目当前有无在跑的任务，自动接续轮询
        if (currentProjectId) checkActivePullTask();
    });

    function checkActivePullTask() {
        $.getJSON('/api/payment/pull-data/active', {project_id: currentProjectId}, function (r) {
            if (r.task_id) {
                var $btn = $('#btn-fetch-core').prop('disabled', true);
                $btn.text(`接续 Step ${r.current_step}/5 · ${r.step_msg}`);
                startPullPolling(r.task_id, $btn);
            }
        });
    }

    $('#sel-month, #input-date').on('change', function () { resetRight(); checkReady(); updateBmButtons(); savePaymentFormParams(); });

    /* ========== 就绪检查 ========== */
    function checkReady() {
        var ok = !!($('#sel-company').val() && $('#sel-project').val()
                  && $('#sel-month').val() && $('#input-date').val());
        $('#btn-load-normal, #btn-load-prepay').prop('disabled', !ok);
    }

    function resetRight() {
        currentMode = null;
        lastDataStatus = null;
        $('#mode-badge').text('').attr('class', 'badge ms-2');
        $('#check-summary').text('');
        $('#check-content').hide();
        $('#check-placeholder').show();
        $('.prepay-only').addClass('d-none');
        $('#prepay-preview').addClass('d-none').empty();
        $('#cards-area').empty();
    }

    /* ========== 普通/预付 核对按钮 ========== */
    $('#btn-load-normal').on('click', function () { loadDataStatus('normal'); });
    $('#btn-load-prepay').on('click', function () { loadDataStatus('prepay'); });

    function loadDataStatus(mode) {
        if (!currentProjectId) { alert('项目未选择'); return; }
        currentMode = mode;
        $('#mode-badge')
            .text(mode === 'prepay' ? '预付模式' : '普通模式')
            .attr('class', 'badge ms-2 ' + (mode === 'prepay' ? 'bg-warning text-dark' : 'bg-primary'));

        var monthKey = $('#sel-month').val();
        var m = /(\d{4})年(\d{1,2})月/.exec(monthKey);
        var business_month = m ? `${m[1]}-${String(m[2]).padStart(2, '0')}` : monthKey;

        var apply_date = $('#input-date').val();

        $('#check-placeholder').hide();
        $('#check-content').show();
        $('#cards-area').html('<div class="col-12 text-center text-muted py-3">加载中…</div>');

        $.getJSON('/api/data-status', {
            project_id: currentProjectId,
            month: business_month,
            date: apply_date,
            mode: mode,
        })
        .done(function (s) {
            lastDataStatus = s;
            $('#check-summary').text(`业务月 ${s.business_month} · 申请日 ${s.apply_date}`);
            renderCards(s);
            $('.prepay-only').toggleClass('d-none', mode !== 'prepay');
            refreshFormulaOptions(mode);
            updatePrepayPreview();
        })
        .fail(function (xhr) {
            $('#cards-area').html('<div class="col-12 alert alert-danger small mb-0">加载失败：' + escape(xhr.responseJSON?.error || xhr.statusText) + '</div>');
        });
    }

    /* ========== 卡片渲染 ========== */
    function cardWrap(colCls, statusCls, headerHtml, bodyHtml) {
        return `<div class="${colCls}">
            <div class="border rounded p-2 h-100" style="border-left:3px solid ${statusCls} !important;">
                <div class="d-flex justify-content-between align-items-center">
                    ${headerHtml}
                </div>
                <div class="mt-1 small">${bodyHtml}</div>
            </div>
        </div>`;
    }

    function renderCards(s) {
        var html = '';
        if (s.mode === 'normal') {
            html += renderKaoqinNormal(s.kaoqin);
            html += renderBill(s.bill);
            html += renderPayrollNormal(s.payroll);
            html += renderWage(s.wage);
        } else {
            html += renderKaoqinPrepay(s.kaoqin);
            html += renderPayrollPrepay(s.payroll);
        }
        $('#cards-area').html(html);
    }

    function checkbox(id, label) {
        return `<div class="form-check mb-0">
            <input class="form-check-input chk-source" type="checkbox" id="${id}" data-source="${id}" checked>
            <label class="form-check-label small" for="${id}">${escape(label)}</label>
        </div>`;
    }

    function renderKaoqinNormal(k) {
        var border = k.has_data ? '#28a745' : '#dc3545';
        var header = `<span class="fw-bold">考勤</span>
            <div class="d-flex gap-2 align-items-center">
              <a href="/projects/${currentProjectId}/settings" target="_blank" class="btn btn-sm btn-outline-secondary py-0 px-2" title="跳到项目设置页（考勤设置 tab）">⚙ 设置</a>
              ${checkbox('chk-attendance', '采用')}
            </div>`;
        var body = !k.has_data
            ? `<span class="text-danger">无考勤数据</span>`
            : `<div><span class="text-muted">最新日期</span> ${escape(k.latest_date || '--')}</div>
               <div><span class="text-muted">月覆盖</span> ${escape(k.month_range_str || '--')}</div>
               <div><span class="text-muted">人数</span> ${(k.workers || 0)} 人 · <span class="text-muted">行数</span> ${(k.rows || 0).toLocaleString()}</div>
               ${k.primary_unit === 'quantity'
                   ? `<div><span class="text-muted">总单量</span> ${(k.total_quantity || 0).toLocaleString()} 单${k.source_note ? '<span class="text-muted small ms-2">· ' + escape(k.source_note) + '</span>' : ''}</div>`
                   : `<div><span class="text-muted">总工时</span> ${(k.total_hours || 0).toLocaleString()} h${k.source_note ? '<span class="text-muted small ms-2">· ' + escape(k.source_note) + '</span>' : ''}</div>`}`;
        return cardWrap('col-6', border, header, body);
    }

    function settingsBtn(cat) {
        return `<a href="/projects/${currentProjectId}/settings?cat=${cat}" target="_blank" class="btn btn-sm btn-outline-secondary py-0 px-2 me-1" title="跳到项目归属规则设置">⚙ 设置</a>`;
    }

    function renderBill(b) {
        var border = b.has_data ? '#28a745' : '#dc3545';
        var header = `<span class="fw-bold">账单</span>
            <div class="d-flex gap-1 align-items-center">
              ${settingsBtn('kaoqin_bill')}
              ${checkbox('chk-bill', '采用')}
            </div>`;
        var body = !b.has_data
            ? `<span class="text-danger">无账单数据</span>`
            : `<div><span class="text-muted">业务周期</span> ${escape(b.business_period_str || '')}</div>
               <div><span class="text-muted">出账日</span> ${escape(b.received_date || '--')}</div>
               <div><span class="text-muted">账单金额</span> ${fmtMoney(b.amount)}</div>
               <div><span class="text-muted">账单人数</span> ${b.person_count} 人</div>`;
        return cardWrap('col-6', border, header, body);
    }

    function renderPayrollNormal(p) {
        var border = p.has_data ? (p.coverage_pct != null && p.coverage_pct < 90 ? '#ffc107' : '#28a745') : '#dc3545';
        var header = `<span class="fw-bold">发薪流水</span>
            <div class="d-flex gap-1 align-items-center">
              ${settingsBtn('payroll')}
              ${checkbox('chk-payroll', '采用')}
            </div>`;
        var body = !p.has_data
            ? `<span class="text-danger">无发薪数据</span>`
            : `<div><span class="text-muted">最新日期</span> ${escape(p.latest_date || '--')}</div>
               <div><span class="text-muted">业务月覆盖</span> ${escape(p.month_range_str || '--')}（已发 ${p.paid_count}/${p.bill_count} 人）</div>
               <div><span class="text-muted">本月发薪</span> ${fmtMoney(p.paid_amount)}${p.coverage_pct != null ? '（' + p.coverage_pct + '%）' : ''}</div>
               ${p.unmatched_count > 0 ? '<div class="text-warning">⚠ 本月有 ' + p.unmatched_count + ' 人未匹配发薪</div>' : ''}`;
        return cardWrap('col-6', border, header, body);
    }

    function renderWage(w) {
        var border = w.has_data ? '#28a745' : '#dc3545';
        var header = `<span class="fw-bold">工资表</span>
            <div class="d-flex gap-1 align-items-center">
              ${settingsBtn('wage')}
              ${checkbox('chk-wage', '采用')}
            </div>`;
        var body = !w.has_data
            ? `<span class="text-danger">无工资表数据</span>`
            : `<div><span class="text-muted">业务月</span> ${escape(w.business_month)}</div>
               <div><span class="text-muted">收到日</span> ${escape(w.received_date || '--')}</div>
               <div><span class="text-muted">应发合计</span> ${fmtMoney(w.payable_total)}</div>
               <div><span class="text-muted">人数</span> ${w.person_count} 人</div>`;
        return cardWrap('col-6', border, header, body);
    }

    function renderKaoqinPrepay(k) {
        var border = k.has_data ? '#28a745' : '#dc3545';
        var header = `<span class="fw-bold">考勤</span>
            <div class="d-flex gap-2 align-items-center">
              <a href="/projects/${currentProjectId}/settings" target="_blank" class="btn btn-sm btn-outline-secondary py-0 px-2" title="跳到项目设置页（考勤设置 tab）">⚙ 设置</a>
              ${checkbox('chk-attendance', '采用')}
            </div>`;
        var body = !k.has_data
            ? `<span class="text-danger">无考勤数据</span>`
            : `<div class="row g-2">
                 <div class="col-3"><span class="text-muted">最新日期</span><br>${escape(k.latest_date || '--')}</div>
                 <div class="col-3"><span class="text-muted">本月覆盖</span><br>${escape(k.month_range_str || '--')}</div>
                 <div class="col-3"><span class="text-muted">近 7 日行数</span><br>${k.rows_7d.toLocaleString()}</div>
                 <div class="col-3"><span class="text-muted">近 7 日工时</span><br>${k.hours_7d.toLocaleString()} h</div>
               </div>`;
        return cardWrap('col-12', border, header, body);
    }

    function renderPayrollPrepay(p) {
        var border = p.has_data ? '#fd7e14' : '#dc3545';
        var header = `<span class="fw-bold">发薪流水 <span class="badge bg-danger ms-1">核心</span></span>
            <div class="d-flex gap-1 align-items-center">
              ${settingsBtn('payroll')}
              ${checkbox('chk-payroll', '采用')}
            </div>`;
        if (!p.has_data) return cardWrap('col-12', border, header, '<span class="text-danger">无发薪数据</span>');

        var detailRows = (p.last7d_detail || []).map(d => {
            var trCls = '';
            var marker = '';
            if (d.marker === 'peak' || d.marker === 'peak+latest') { trCls = 'table-warning'; marker = '<span class="badge bg-warning text-dark">峰值</span>'; }
            else if (d.marker === 'latest') { marker = '<span class="badge bg-secondary">最近</span>'; }
            return `<tr class="${trCls}">
                <td>${escape(d.date)}</td>
                <td class="text-end">${fmtMoney(d.amount)}</td>
                <td class="text-end">${d.count}</td>
                <td>${marker}</td>
            </tr>`;
        }).join('');

        var body = `
            <div><span class="text-muted">最新日期</span> ${escape(p.latest_date || '--')} &nbsp; <span class="text-muted">本月发薪</span> ${fmtMoney(p.month_paid_amount)}（${p.month_rows} 笔）</div>
            <div class="border rounded mt-1" style="background:#fff;">
              <div class="px-2 py-1 small fw-bold">近 7 日发薪明细</div>
              <table class="table table-sm mb-0" style="font-size:12px;">
                <thead class="table-light"><tr><th>日期</th><th class="text-end">金额</th><th class="text-end">人次</th><th>标记</th></tr></thead>
                <tbody>${detailRows}</tbody>
              </table>
            </div>
            <div class="mt-1">
              <span class="text-muted">峰值</span> <strong class="text-warning">${fmtMoney(p.last7d_peak)}</strong>
              &nbsp;<span class="text-muted">平均</span> ${fmtMoney(p.last7d_avg)}
              &nbsp;<span class="text-muted">最近</span> ${fmtMoney(p.last7d_latest)}
            </div>`;
        return cardWrap('col-12', border, header, body);
    }

    /* ========== 计算公式下拉 ========== */
    function refreshFormulaOptions(mode) {
        var opts = mode === 'prepay'
            ? [{v: 'prepay1', t: '计算逻辑预付1（当前默认）'},
               {v: 'prepay2', t: '计算逻辑预付2（待告知）'}]
            : [{v: 'normal1', t: '计算逻辑普通1（当前默认）'}];
        var $sel = $('#sel-calc-formula');
        $sel.empty();
        opts.forEach(o => $sel.append('<option value="' + o.v + '">' + escape(o.t) + '</option>'));
    }

    /* ========== 预付估算预览 ========== */
    $(document).on('change input', '#sel-baseday-mode, #input-prepay-days, #input-baseday-date, #input-profit-ratio', updatePrepayPreview);
    // 基准日选 'custom' 时显示日期输入
    $(document).on('change', '#sel-baseday-mode', function () {
        $('#input-baseday-date').toggleClass('d-none', $(this).val() !== 'custom');
    });

    function updatePrepayPreview() {
        if (currentMode !== 'prepay' || !lastDataStatus) {
            $('#prepay-preview').addClass('d-none');
            return;
        }
        // 基准日金额取考勤×单价（跟 prepay.py 后端正式计算一致）；
        // payroll.last7d_* 是发薪侧近 7 日，仅作信息展示，不当基准
        var k = lastDataStatus.kaoqin || {};
        var mode = $('#sel-baseday-mode').val();
        var days = parseInt($('#input-prepay-days').val()) || 0;
        var profit = parseFloat($('#input-profit-ratio').val()) || 0;
        var base = mode === 'max' ? k.last7d_peak : (mode === 'avg' ? k.last7d_avg : k.last7d_latest);
        var est = (base || 0) * days * profit;
        $('#prepay-preview').removeClass('d-none').html(
            `<strong>预付估算预览</strong>：基准日金额 ${fmtMoney(base)} × 预付天数 ${days} × 出款比例 ${profit} = <strong>${fmtMoney(est)}</strong>（正式计算以"开始审核"为准）`);
    }

    /* ========== 拉取结果可复制弹窗 ========== */
    function showPullResult(text) {
        $('#pull-result-text').text(text);
        new bootstrap.Modal(document.getElementById('pull-result-modal')).show();
    }
    $(document).on('click', '#btn-copy-pull-result', function () {
        var text = $('#pull-result-text').text();
        var $btn = $(this);
        var orig = $btn.text();
        if (navigator.clipboard?.writeText) {
            navigator.clipboard.writeText(text).then(
                () => { $btn.text('✓ 已复制'); setTimeout(() => $btn.text(orig), 1500); },
                () => { $btn.text('✗ 失败'); setTimeout(() => $btn.text(orig), 1500); }
            );
        } else {
            // fallback：选中文本
            var range = document.createRange();
            range.selectNodeContents(document.getElementById('pull-result-text'));
            var sel = window.getSelection();
            sel.removeAllRanges();
            sel.addRange(range);
            $btn.text('请按 Ctrl+C');
            setTimeout(() => $btn.text(orig), 2500);
        }
    });

    /* ========== 拉取/删除业务月数据按钮（异步 + 轮询） ========== */
    var pullPollTimer = null;

    $('#btn-fetch-core').on('click', function () {
        if (!currentProjectId) { alert('请先选择项目'); return; }
        var monthKey = $('#sel-month').val();
        var m = /(\d{4})年(\d{1,2})月/.exec(monthKey);
        var business_month = m ? `${m[1]}-${String(m[2]).padStart(2, '0')}` : null;
        if (!business_month) { alert('请先选择业务月份'); return; }
        if (!confirm(`从老库 + COS 拉取业务月 ${business_month} 的考账发工数据，后台异步跑，确定？`)) return;
        var $btn = $(this).prop('disabled', true).text('启动中…');
        $.ajax({
            url: '/api/payment/pull-data', method: 'POST', contentType: 'application/json',
            data: JSON.stringify({project_id: currentProjectId, business_month: business_month}),
        })
        .done(function (r) {
            startPullPolling(r.task_id, $btn);
        })
        .fail(function (xhr) {
            alert('启动失败：' + (xhr.responseJSON?.error || xhr.statusText));
            $btn.prop('disabled', false);
            updateBmButtons();
        });
    });

    $('#btn-delete-bm').on('click', function () {
        if (!currentProjectId) { alert('请先选择项目'); return; }
        var monthKey = $('#sel-month').val();
        var m = /(\d{4})年(\d{1,2})月/.exec(monthKey);
        var business_month = m ? `${m[1]}-${String(m[2]).padStart(2, '0')}` : null;
        if (!business_month) { alert('请先选择业务月份'); return; }
        if (!confirm(`确定删除 ${monthKey} 的考勤/账单/发薪/工资表 mart 数据？\nraw_files 保留，下次"拉取"会重装。`)) return;
        var $btn = $(this).prop('disabled', true).text('删除中…');
        $.ajax({
            url: '/api/payment/delete-data', method: 'POST', contentType: 'application/json',
            data: JSON.stringify({project_id: currentProjectId, business_month: business_month}),
        })
        .done(function (r) {
            var d = r.deleted || {};
            var total = Object.values(d).reduce(function (a, b) { return a + (b || 0); }, 0);
            alert('已删除 ' + total + ' 行：\n' +
                  Object.entries(d).map(function (kv) { return kv[0] + ': ' + kv[1]; }).join('\n'));
            if (currentMode) loadDataStatus(currentMode);
        })
        .fail(function (xhr) {
            alert('删除失败：' + (xhr.responseJSON?.error || xhr.statusText));
        })
        .always(function () {
            $btn.prop('disabled', false);
            updateBmButtons();
        });
    });

    function startPullPolling(taskId, $btn) {
        if (pullPollTimer) { clearInterval(pullPollTimer); pullPollTimer = null; }
        // 轮询期间禁用删除按钮（避免删-INSERT 互相打架）
        $('#btn-delete-bm').prop('disabled', true).attr('title', '拉取任务进行中，无法删除');
        // 进度面板（独立于按钮）
        if (!$('#pull-progress-panel').length) {
            $('<div id="pull-progress-panel" class="alert alert-info small p-2 mt-2 mb-0"></div>')
                .insertAfter($btn.closest('.d-flex'));
        }
        function buildProgressLine(s) {
            const c = s.live_counts || {};
            const rs = s.raw_status || {};
            const rawSummary = ['parsed', 'pending', 'extracted', 'skipped', 'failed']
                .filter(k => rs[k]).map(k => `${k}=${rs[k]}`).join(' · ');
            return `
                <div><strong>Step ${s.current_step || 0}/5 · ${s.step_msg || '排队中'}</strong></div>
                <div class="text-muted">
                    考勤 ${(c.attendance || 0).toLocaleString()}（汇总 ${c.attendance_summary || 0}） ·
                    账单 ${c.bill_totals || 0}/${(c.bill_persons || 0).toLocaleString()} ·
                    发薪 ${(c.payrolls || 0).toLocaleString()} ·
                    工资表 ${(c.wage_sheets || 0).toLocaleString()}
                </div>
                <div class="text-muted">raw_files: ${rawSummary || '—'}</div>`;
        }
        function poll() {
            $.getJSON('/api/payment/pull-data/status', {task_id: taskId})
                .done(function (s) {
                    var step = s.current_step || 0;
                    var label = s.step_msg || '排队中';
                    if (s.status === 'running' || s.status === 'pending') {
                        $btn.text(`Step ${step}/5 · ${label}`);
                        $('#pull-progress-panel').html(buildProgressLine(s));
                    } else if (s.status === 'ok') {
                        clearInterval(pullPollTimer); pullPollTimer = null;
                        $('#pull-progress-panel').remove();
                        showPullResultFromTask(s);
                        $btn.prop('disabled', false);
                        $('#btn-delete-bm').removeAttr('title');
                        updateBmButtons();
                        if (currentMode) loadDataStatus(currentMode);
                    } else if (s.status === 'failed') {
                        clearInterval(pullPollTimer); pullPollTimer = null;
                        $('#pull-progress-panel').remove();
                        alert('拉取失败：' + (s.error_message || '未知错误'));
                        $btn.prop('disabled', false);
                        $('#btn-delete-bm').removeAttr('title');
                        updateBmButtons();
                    }
                })
                .fail(function () {});
        }
        poll();
        pullPollTimer = setInterval(poll, 2000);
    }

    function showPullResultFromTask(s) {
        var r = s.progress || {};
        var s1 = r.step1_db_mirror || {};
        var counts = r.mart_counts || {};
        var msg =
            `Step 1 fish-prod 镜像：\n` +
            `  mini_a_bill ${s1.mini_a_bill || 0}, mini_user_shift_rel ${s1.mini_user_shift_rel || 0}, mini_shift ${s1.mini_shift || 0}, loan_records ${s1.loan_records || 0}\n\n` +
            `Step 2 COS 同步（尾部日志）：\n${r.step2_cos?.sync_log_tail || '(无)'}\n\n` +
            `Step 3 解压（尾部日志）：\n${r.step3_extract?.extract_log_tail || '(无)'}\n\n` +
            `Step 4 文件解析（尾部日志）：\n${r.step4_parse?.parse_log_tail || '(无)'}\n\n` +
            `Step 5 DB 标准化：\n` +
            `  考勤: ${r.step5_standardize?.attendance || '-'}\n` +
            `  发薪: ${r.step5_standardize?.payrolls || '-'}\n\n` +
            `mart 当前行数：\n` +
            `  考勤 ${counts.attendance || 0}（汇总 ${counts.attendance_summary || 0}）\n` +
            `  账单 总 ${counts.bill_totals || 0} / 人 ${counts.bill_persons || 0}\n` +
            `  发薪 ${counts.payrolls || 0}\n` +
            `  工资表 ${counts.wage_sheets || 0}`;
        if (r.errors && r.errors.length) msg += '\n\n错误：\n' + r.errors.join('\n');
        showPullResult(msg);
    }

    /* ========== 开始审核 ========== */
    $('#btn-analyze').on('click', function () {
        if (!currentMode) { alert('请先点"普通核对"或"预付核对"加载数据'); return; }
        if (!currentProjectId) { alert('项目未选择'); return; }

        var monthKey = $('#sel-month').val();
        var m = /(\d{4})年(\d{1,2})月/.exec(monthKey);
        var business_month = m ? `${m[1]}-${String(m[2]).padStart(2, '0')}` : monthKey;
        var apply_date = $('#input-date').val();
        var customer = $('#input-customer-amount').val();

        var _curProj = (window._currentProjects || []).find(p => String(p.project_id) === String(currentProjectId));
        var payload = {
            project_id: currentProjectId,
            company: $('#sel-company').val(),
            project: _curProj ? _curProj.title : '',
            month: monthKey,
            date: apply_date,
            mode: currentMode,
            calc_formula: $('#sel-calc-formula').val()
                || (currentMode === 'prepay' ? 'prepay1' : 'normal1'),
            customer_amount: customer ? parseFloat(customer) : null,
            engine: '小鱼风控分析',
        };
        if (currentMode === 'prepay') {
            payload.prepay = {
                prepay_days: parseInt($('#input-prepay-days').val()) || 7,
                base_day_mode: $('#sel-baseday-mode').val(),
                base_day_date: $('#input-baseday-date').val() || null,
            };
        }

        $('#btn-analyze').prop('disabled', true).text('计算中…');
        $.ajax({
            url: '/api/payment/analyze', method: 'POST',
            contentType: 'application/json', data: JSON.stringify(payload),
        })
        .done(function (out) {
            if (out.error) {
                alert('计算失败：' + out.error);
                return;
            }
            sessionStorage.setItem('analyzeResult', JSON.stringify(out));
            sessionStorage.setItem('analyzeParams', JSON.stringify({
                company: payload.company, project: payload.project,
                project_id: out.project_id || payload.project_id || currentProjectId,
                month: business_month, date: apply_date, mode: currentMode,
            }));
            window.location.href = '/payment/report/xy';
        })
        .fail(xhr => alert('请求失败：' + (xhr.responseJSON?.error || xhr.statusText)))
        .always(() => $('#btn-analyze').prop('disabled', false).text('开始审核'));
    });
});
