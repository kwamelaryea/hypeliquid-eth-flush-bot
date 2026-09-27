let lastPrice = 0;
let lastPnl   = 0;
let pnlDataLoaded    = false;
let lastUpdateTime   = '';

// ── Tab Switching ─────────────────────────────────────────
function switchTab(name) {
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
    document.getElementById('tab-' + name).classList.add('active');
    document.getElementById('panel-' + name).classList.add('active');
    if (name === 'pnl' && !pnlDataLoaded) loadPnlChart();
}

// ── Log entry classification ──────────────────────────────
function classifyLog(msg) {
    const m = msg.toLowerCase();
    if (m.includes('long'))                          return 'log-long';
    if (m.includes('short'))                         return 'log-short';
    if (m.includes('close') || m.includes('exit'))  return 'log-close';
    if (m.includes('error') || m.includes('fail'))  return 'log-error';
    if (m.includes('warn') || m.includes('block') || m.includes('filter')) return 'log-warn';
    if (m.includes('start') || m.includes('init') || m.includes('mode'))   return 'log-info';
    return 'log-hold';
}

// Build all server-derived content with DOM APIs and textContent.
function clearNode(node) {
    node.replaceChildren();
}

function appendEmptyState(node, message) {
    clearNode(node);
    const empty = document.createElement('div');
    empty.className = 'empty-state';
    empty.textContent = message;
    node.appendChild(empty);
}

function appendCell(row, value, className = '') {
    const cell = document.createElement('td');
    cell.textContent = String(value);
    if (className) cell.className = className;
    row.appendChild(cell);
}

function renderTable(node, headings, rows) {
    clearNode(node);
    const table = document.createElement('table');
    table.className = 'data-table';
    const headRow = document.createElement('tr');
    headings.forEach(heading => {
        const th = document.createElement('th');
        th.textContent = heading;
        headRow.appendChild(th);
    });
    const thead = document.createElement('thead');
    thead.appendChild(headRow);
    table.appendChild(thead);
    const tbody = document.createElement('tbody');
    rows.forEach(cells => {
        const row = document.createElement('tr');
        cells.forEach(cell => appendCell(row, cell.value, cell.className || ''));
        tbody.appendChild(row);
    });
    table.appendChild(tbody);
    node.appendChild(table);
}

// ── Synthetic PnL panel ──────────────────────────────────
function loadPnlChart() {
    const msgEl = document.getElementById('pnl-chart-msg');
    fetch('/api/public-pnl-history')
        .then(response => response.json())
        .then(data => {
            document.getElementById('pnl-total').textContent = '$0.00';
            msgEl.textContent = data.demo_data
                ? 'Synthetic demo — account trade history is never public.'
                : 'No public history available.';
            pnlDataLoaded = true;
        })
        .catch(() => { msgEl.textContent = 'Demo history unavailable.'; });
}

// ── Value animation ───────────────────────────────────────
function animateValue(obj, start, end, duration, isCurrency = false) {
    if (start === end || isNaN(end)) return;
    let startTimestamp = null;
    const step = (ts) => {
        if (!startTimestamp) startTimestamp = ts;
        const progress = Math.min((ts - startTimestamp) / duration, 1);
        const val = (progress * (end - start)) + start;
        if (isCurrency) {
            obj.textContent = '$' + val.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
        } else {
            obj.textContent = (val >= 0 ? '+' : '') + val.toFixed(2) + '%';
        }
        if (progress < 1) window.requestAnimationFrame(step);
    };
    window.requestAnimationFrame(step);
}

// ── Main dashboard update ─────────────────────────────────
function updateDashboard() {
    fetch('/api/public-status')
        .then(r => r.json())
        .then(data => {

            // Last update timestamp
            const now = new Date();
            const ts = now.toLocaleTimeString('en-US', { hour12: false }) + ' UTC';
            document.getElementById('last-update-ts').textContent = 'LAST UPDATE: ' + ts;

            // Execution mode pill
            const mode = data.execution_mode || 'LIVE';
            const modeText = document.getElementById('address-text');
            const modePill = document.getElementById('address-pill');
            const modeDot = modePill.querySelector('.dot');
            if (mode === 'DEMO') {
                modeText.textContent = 'DEMO';
                modePill.title = 'Synthetic demo data — no account connection';
                modeDot.style.background = 'var(--cyan)';
                modeDot.style.boxShadow = '0 0 8px var(--cyan)';
            } else if (mode === 'PAUSED_ENTRIES') {
                modeText.textContent = 'PAUSED';
                modePill.title = 'New entries paused; live exits/risk management enabled';
                modeDot.style.background = 'var(--warning)';
                modeDot.style.boxShadow = '0 0 8px var(--warning)';
            } else if (mode === 'DRY_RUN') {
                modeText.textContent = 'PAPER';
                modePill.title = 'Dry run mode — no live order execution';
                modeDot.style.background = 'var(--cyan)';
                modeDot.style.boxShadow = '0 0 8px var(--cyan)';
            } else {
                modeText.textContent = 'LIVE';
                modePill.title = 'Live trading enabled';
                modeDot.style.background = 'var(--success)';
                modeDot.style.boxShadow = '0 0 8px var(--success)';
            }

            // Price
            const priceEl = document.getElementById('price');
            if (data.price) {
                const newPrice = parseFloat(data.price);
                if (lastPrice === 0) {
                    priceEl.textContent = '$' + newPrice.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
                } else {
                    animateValue(priceEl, lastPrice, newPrice, 800, true);
                }
                lastPrice = newPrice;
            }

            // Trend
            const trendText = document.getElementById('trend-text');
            const trendDot  = document.getElementById('trend-dot');
            if (data.ema && data.price) {
                const bullish = parseFloat(data.price) > parseFloat(data.ema);
                trendText.textContent = bullish ? 'Above EMA-200' : 'Below EMA-200';
                trendText.style.color = bullish ? 'var(--success)' : 'var(--danger)';
                trendDot.style.background = bullish ? 'var(--success)' : 'var(--danger)';
                trendDot.style.boxShadow = bullish ? '0 0 8px var(--success)' : '0 0 8px var(--danger)';
                document.getElementById('ema-val').textContent = '$' + parseFloat(data.ema).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
            }

            // RSI
            const rsiEl  = document.getElementById('rsi');
            const rsiBar = document.getElementById('rsi-bar');
            if (data.rsi) {
                const rsi = parseFloat(data.rsi);
                rsiEl.textContent = rsi.toFixed(1);
                rsiBar.style.width = rsi + '%';
                if (rsi <= 35)       { rsiEl.style.color = 'var(--success)'; rsiBar.style.background = 'var(--success)'; }
                else if (rsi >= 65)  { rsiEl.style.color = 'var(--danger)';  rsiBar.style.background = 'var(--danger)';  }
                else                 { rsiEl.style.color = 'var(--text-main)'; rsiBar.style.background = 'var(--cyan)';  }
            }

            // Funding rate
            if (data.funding_rate !== undefined) {
                const fr = parseFloat(data.funding_rate);
                const frEl = document.getElementById('funding');
                frEl.textContent = (fr >= 0 ? '+' : '') + (fr * 100).toFixed(4) + '%';
                frEl.style.color = fr < 0 ? 'var(--success)' : fr > 0.0002 ? 'var(--warning)' : 'var(--text-dim)';
            }

            // PnL
            const pnlEl = document.getElementById('pnl');
            if (data.pnl !== undefined && data.pnl !== null) {
                const newPnl = parseFloat(data.pnl);
                pnlEl.style.color = newPnl > 0 ? 'var(--success)' : newPnl < 0 ? 'var(--danger)' : 'var(--text-dim)';
                if (Math.abs(newPnl - lastPnl) > 0.001) {
                    animateValue(pnlEl, lastPnl, newPnl, 800, false);
                } else {
                    pnlEl.textContent = (newPnl >= 0 ? '+' : '') + newPnl.toFixed(2) + '%';
                }
                lastPnl = newPnl;
            }

            // Position
            const posEl = document.getElementById('position');
            posEl.textContent = (data.position || 'NONE').toUpperCase();
            posEl.style.color = data.position === 'long' ? 'var(--success)' : data.position === 'short' ? 'var(--primary)' : 'var(--text-dim)';

            // Balance
            document.getElementById('balance').textContent = '$' + parseFloat(data.balance || 0).toLocaleString('en-US', { minimumFractionDigits: 2 });

            // Signal badge
            const sigBadge = document.getElementById('signal-badge');
            const sigDesc  = document.getElementById('signal-desc');
            const sig = (data.signal || 'WAIT').toUpperCase();
            sigBadge.textContent = sig;
            sigBadge.className = 'signal-badge ' + (sig === 'LONG' ? 'sig-long' : sig === 'SHORT' ? 'sig-short' : sig === 'CLOSE' ? 'sig-close' : 'sig-wait');
            if (data.demo_data) sigDesc.textContent = 'Synthetic demonstration — no orders or account data';
            else if (data.new_entries_paused && (sig === 'WAIT' || sig === 'HOLD')) sigDesc.textContent = 'New entries paused — exits and risk management remain live';
            else if (sig === 'WAIT' || sig === 'HOLD') sigDesc.textContent = 'Monitoring liquidation clusters...';
            else if (sig === 'LONG')  sigDesc.textContent = data.new_entries_paused ? 'LONG blocked — new entries paused' : 'Liquidation flush detected — entering LONG';
            else if (sig === 'SHORT') sigDesc.textContent = data.new_entries_paused ? 'SHORT blocked — new entries paused' : 'Short squeeze potential — entering SHORT';
            else sigDesc.textContent = 'Closing existing position...';

            // Cascade (main tab)
            if (data.cascade_risk) {
                const risk  = data.cascade_risk.toUpperCase();
                const score = parseFloat(data.cascade_score || 0);
                const cls   = 'cascade-' + risk.toLowerCase();

                ['cascade-badge', 'cascade-badge-mini'].forEach(id => {
                    const el = document.getElementById(id);
                    el.textContent = risk;
                    el.className = 'cascade-badge ' + cls;
                });

                document.getElementById('cascade-score').textContent = score.toFixed(0) + '/100';
                document.getElementById('cascade-score').style.color = risk === 'CRITICAL' ? 'var(--danger)' : risk === 'ELEVATED' ? 'var(--warning)' : 'var(--text-dim)';

                const bar = document.getElementById('cascade-score-bar');
                bar.style.width   = Math.min(score, 100) + '%';
                bar.style.background = risk === 'CRITICAL' ? 'var(--danger)' : risk === 'ELEVATED' ? 'var(--warning)' : 'var(--success)';

                const impact = document.getElementById('cascade-impact');
                if (risk === 'CRITICAL')      impact.textContent = '🔴 Trading: Long blocked — Short only (25% size)';
                else if (risk === 'ELEVATED')  impact.textContent = '🟡 Trading: Long blocked — Short OK (50% size)';
                else                           impact.textContent = '🟢 Trading: Full size active';

                const reasons = Array.isArray(data.cascade_reasons) ? data.cascade_reasons : [];
                const reasonsEl = document.getElementById('cascade-reasons');
                clearNode(reasonsEl);
                (reasons.length > 0 ? reasons : ['No stress signals detected']).forEach(reason => {
                    const reasonEl = document.createElement('span');
                    reasonEl.className = 'cascade-reason';
                    reasonEl.textContent = String(reason);
                    reasonsEl.appendChild(reasonEl);
                });
            }

            // Synthetic position rows are rendered with textContent only.
            const perpsEl = document.getElementById('perps-list');
            if (Array.isArray(data.perps_positions) && data.perps_positions.length > 0) {
                const rows = data.perps_positions.map(pos => {
                    const pnl = Number(pos.unrealized_pnl || 0);
                    return [
                        { value: String(pos.coin || '') + ' ' + String(pos.side || '').toUpperCase() },
                        { value: Number(pos.size || 0).toFixed(3) },
                        { value: '$' + Number(pos.entry_price || 0).toLocaleString() },
                        { value: (pnl >= 0 ? '+$' : '-$') + Math.abs(pnl).toFixed(2), className: pnl >= 0 ? 'pnl-pos' : 'pnl-neg' }
                    ];
                });
                renderTable(perpsEl, ['Coin', 'Size', 'Entry', 'PnL'], rows);
            } else {
                appendEmptyState(perpsEl, 'No synthetic positions');
            }

            const spotEl = document.getElementById('spot-list');
            if (Array.isArray(data.spot_positions) && data.spot_positions.length > 0) {
                const rows = data.spot_positions.map(bal => [
                    { value: String(bal.coin || '') },
                    { value: Number(bal.total || 0).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 4 }) },
                    { value: (Number(bal.total || 0) - Number(bal.hold || 0)).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 4 }) }
                ]);
                renderTable(spotEl, ['Asset', 'Total', 'Available'], rows);
            } else {
                appendEmptyState(spotEl, 'No synthetic balances');
            }

            const logsEl = document.getElementById('logs');
            let logs = Array.isArray(data.logs) ? data.logs : [];
            if (logs.length === 0) {
                logs = [{ time: '--:--:--', msg: 'Synthetic demo dashboard — no account data' }];
            }
            clearNode(logsEl);
            logs.forEach((entry, index) => {
                const row = document.createElement('div');
                row.className = 'log-entry ' + classifyLog(String(entry.msg || ''));
                row.style.opacity = String(Math.max(0.72, 1 - (index * 0.04)));
                const timestamp = document.createElement('span');
                timestamp.className = 'log-ts';
                timestamp.textContent = String(entry.time || '--:--:--');
                const message = document.createElement('span');
                message.className = 'log-msg';
                message.textContent = String(entry.msg || '');
                row.append(timestamp, message);
                logsEl.appendChild(row);
            });

        })
        .catch(err => console.error('Dashboard sync error:', err));
}

document.querySelectorAll('.tab-btn[data-tab]').forEach(button => {
    button.addEventListener('click', () => switchTab(button.dataset.tab));
});
setInterval(updateDashboard, 2000);
updateDashboard();
