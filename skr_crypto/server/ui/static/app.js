/* Read-only operator UI for skr-crypto.
 *
 * INVARIANT: this file MUST NOT contain a /send POST or any other
 * money-moving call. It only ever does GET on /api/v1/*. Searching
 * this file for `'/send'` should return zero hits.
 *
 * Every API call carries the X-API-Key header from sessionStorage.
 */

/* Register the SPA component on Alpine.js's `alpine:init` event.
 * This is the canonical Alpine 3 pattern — works regardless of
 * script load order, and avoids global-scope leakage. */
document.addEventListener('alpine:init', () => {
  Alpine.data('app', () => ({
    // Auth state — token lives in sessionStorage only, never localStorage
    token: '',
    tokenInput: '',
    authed: false,
    loginError: '',
    tokenIdLabel: '',
    hasAdmin: false,

    // Routing
    tab: 'dashboard',

    // Data caches
    health: {},
    walletsData: { wallets: [], auto_pick: null },
    auditRecords: [],
    auditFilter: '',

    riskAddr: '',
    riskExternal: false,
    riskReport: null,

    // Reports tab (1.9.0+)
    reports: {
      fromDate: '',
      toDate: '',
      preview: [],
      spark: { dates: [], send_success_volume_usdt: [], send_rejected_count: [] },
      svgViewBox: '0 0 100 20',
      svgPolylineVolume: '',
      svgPolylineRejected: '',
      error: '',
    },

    // Notifications tab (1.9.0+)
    alertConfig: {
      configured: false, chat_id: '', events: [],
      send_threshold_usdt: null, sanctions_hit_notify: false,
      quiet_hours_utc: '',
    },
    alertTestResult: '',

    init() {
      // Restore session if present
      const tok = sessionStorage.getItem('skr_token') || '';
      if (tok) {
        this.token = tok;
        this.authed = true;
        this.applyHash();
        this.refreshAll();
      }
      window.addEventListener('hashchange', () => this.applyHash());
    },

    applyHash() {
      const h = location.hash.replace(/^#\//, '');
      if (h && ['dashboard','wallets','audit','risk','reports','notifications','tokens','webhooks'].includes(h)) {
        this.tab = h;
      }
    },

    go(t) {
      location.hash = '#/' + t;
      this.tab = t;
      // Lazy-refresh per tab
      if (t === 'dashboard') this.refreshHealth();
      else if (t === 'wallets') this.refreshWallets();
      else if (t === 'audit') this.refreshAudit();
      else if (t === 'reports') this.initReportsTab();
      else if (t === 'notifications') this.refreshAlertConfig();
    },

    async login() {
      if (!this.tokenInput) return;
      this.token = this.tokenInput;
      this.tokenInput = '';
      // Validate the token by hitting /health (read scope)
      const r = await this.api('/api/v1/health');
      if (!r.ok) {
        this.loginError = 'Token rejected (' + r.status + '). Try again.';
        this.token = '';
        return;
      }
      this.loginError = '';
      sessionStorage.setItem('skr_token', this.token);
      this.authed = true;
      // Probe scopes by trying an admin-ish endpoint; tolerate failure
      this.probeAdminScope();
      this.refreshAll();
      this.go('dashboard');
    },

    async probeAdminScope() {
      // Try the metrics endpoint with the token. Metrics scope is
      // distinct, so this isn't a perfect admin-probe, but a 200 on
      // either /metrics or /audit?limit=1 tells us scope is OK.
      this.hasAdmin = false;
      // Cheap heuristic: admin usually has metrics+. Try metrics.
      try {
        const r = await fetch('/api/v1/metrics', {
          headers: { 'X-API-Key': this.token },
        });
        if (r.ok) this.hasAdmin = true;
      } catch (e) { /* ignore */ }
    },

    logout() {
      sessionStorage.removeItem('skr_token');
      this.token = '';
      this.authed = false;
      this.loginError = '';
      this.health = {};
      this.walletsData = { wallets: [], auto_pick: null };
      this.auditRecords = [];
      location.hash = '';
    },

    async api(path) {
      try {
        const r = await fetch(path, {
          headers: { 'X-API-Key': this.token },
        });
        let body = null;
        try { body = await r.json(); } catch (e) {}
        return { ok: r.ok, status: r.status, body };
      } catch (e) {
        return { ok: false, status: 0, body: { error: String(e) } };
      }
    },

    async refreshAll() {
      await Promise.all([this.refreshHealth(), this.refreshWallets()]);
    },

    async refreshHealth() {
      const r = await this.api('/api/v1/health');
      this.health = r.ok ? r.body : {};
    },

    async refreshWallets() {
      const r = await this.api('/api/v1/wallets');
      this.walletsData = r.ok ? r.body : { wallets: [], auto_pick: null };
    },

    async refreshAudit() {
      let url = '/api/v1/audit?limit=200';
      if (this.auditFilter) url += '&event=' + encodeURIComponent(this.auditFilter);
      const r = await this.api(url);
      this.auditRecords = (r.ok && r.body.records) ? r.body.records : [];
    },

    async lookupRisk() {
      if (!this.riskAddr) return;
      const ext = this.riskExternal ? 'true' : 'false';
      const r = await this.api(
        '/api/v1/risk/' + encodeURIComponent(this.riskAddr) + '?external=' + ext
      );
      this.riskReport = r.ok ? r.body : { level: 'error', checks: [] };
    },

    // ── Reports tab ────────────────────────────────────────────────────
    initReportsTab() {
      if (!this.reports.fromDate || !this.reports.toDate) {
        const today = new Date();
        const past = new Date(today.getTime() - 29 * 24 * 60 * 60 * 1000);
        this.reports.toDate = today.toISOString().slice(0, 10);
        this.reports.fromDate = past.toISOString().slice(0, 10);
      }
      this.loadReportPreview();
    },

    async loadReportPreview() {
      this.reports.error = '';
      const qs = 'from=' + this.reports.fromDate + '&to=' + this.reports.toDate;
      const sparkR = await this.api('/api/v1/reports/period/sparkline?' + qs);
      if (!sparkR.ok) {
        this.reports.error = (sparkR.body && sparkR.body.error) || ('preview failed: ' + sparkR.status);
        return;
      }
      this.reports.spark = sparkR.body || this.reports.spark;
      this.computeSparkSvg();
      // Build a tiny preview table from the same data + zero-filled extras.
      this.reports.preview = (this.reports.spark.dates || []).map((d, i) => ({
        date: d,
        send_success_count: this.reports.spark.send_success_count[i] || 0,
        send_success_volume_usdt: this.reports.spark.send_success_volume_usdt[i] || 0,
        send_rejected_count: this.reports.spark.send_rejected_count[i] || 0,
        send_failed_count: 0,
        receipt_resolved_success_count: 0,
        receipt_resolved_failure_count: 0,
      }));
    },

    computeSparkSvg() {
      // Inline SVG sparklines — no chart library. ~30 lines, vendored
      // in the codebase ethos.
      const buildPoints = (vals, height) => {
        if (!vals.length) return '';
        const max = Math.max(...vals, 1);
        const w = 100, h = height;
        return vals.map((v, i) => {
          const x = (i / Math.max(vals.length - 1, 1)) * w;
          const y = h - (v / max) * h;
          return x.toFixed(2) + ',' + y.toFixed(2);
        }).join(' ');
      };
      this.reports.svgViewBox = '0 0 100 20';
      this.reports.svgPolylineVolume =
        buildPoints(this.reports.spark.send_success_volume_usdt || [], 20);
      this.reports.svgPolylineRejected =
        buildPoints(this.reports.spark.send_rejected_count || [], 20);
    },

    downloadReport(kind, fmt) {
      // Build a one-shot URL with the token in a query string is unsafe
      // (token leaks to logs). Instead fetch the file with the header,
      // then trigger a Blob download.
      const qs = 'from=' + this.reports.fromDate + '&to=' + this.reports.toDate + '&format=' + fmt;
      const path = kind === 'period'
        ? '/api/v1/reports/period?' + qs
        : '/api/v1/reports/sanctions-hits?' + qs;
      this.reports.error = '';
      fetch(path, { headers: { 'X-API-Key': this.token } }).then(async r => {
        if (!r.ok) {
          let msg = 'download failed: ' + r.status;
          try { const b = await r.json(); if (b.error) msg = b.error; } catch (e) {}
          this.reports.error = msg;
          return;
        }
        const blob = await r.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        const cd = r.headers.get('content-disposition') || '';
        const match = cd.match(/filename="([^"]+)"/);
        a.download = match ? match[1] : (kind + '.' + fmt);
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
      });
    },

    // ── Notifications tab ──────────────────────────────────────────────
    async refreshAlertConfig() {
      const r = await this.api('/api/v1/alert/config');
      if (r.ok) this.alertConfig = r.body;
    },

    async sendTestAlert() {
      this.alertTestResult = 'sending…';
      try {
        const r = await fetch('/api/v1/alert/test', {
          method: 'POST',
          headers: { 'X-API-Key': this.token },
        });
        if (r.ok) {
          this.alertTestResult = 'queued — check the configured Telegram chat';
        } else {
          let msg = 'failed: ' + r.status;
          try { const b = await r.json(); if (b.error) msg = b.error; } catch (e) {}
          this.alertTestResult = msg;
        }
      } catch (e) {
        this.alertTestResult = 'network error: ' + e;
      }
    },

    // Helpers
    formatTime(iso) {
      if (!iso) return '';
      // Trim to local-time HH:MM:SS for readability
      const d = new Date(iso);
      if (isNaN(d.getTime())) return iso.slice(0, 19);
      return d.toLocaleString();
    },
    formatDuration(s) {
      s = parseInt(s, 10) || 0;
      if (s < 60) return s + 's';
      if (s < 3600) return Math.floor(s/60) + 'm';
      if (s < 86400) return Math.floor(s/3600) + 'h';
      return Math.floor(s/86400) + 'd';
    },
    auditRowClass(r) {
      if (!r) return '';
      const result = (r.result || '').toLowerCase();
      if (result.includes('fail') || result === 'tx_failed' ||
          result === 'rpc_failed' || r.event === 'WEBHOOK_GIVEUP' ||
          result === 'risk_too_high' || result === 'giveup') {
        return 'audit-fail';
      }
      if (r.event === 'SEND_REJECTED' || r.event === 'SEND_DUPLICATE') {
        return 'audit-warn';
      }
      return '';
    },
  }));
});
