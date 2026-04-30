/* Read-only operator UI for skr-crypto.
 *
 * INVARIANT: this file MUST NOT contain a /send POST or any other
 * money-moving call. It only ever does GET on /api/v1/*. Searching
 * this file for `'/send'` should return zero hits.
 *
 * Every API call carries the X-API-Key header from sessionStorage.
 */

function app() {
  return {
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
      if (h && ['dashboard','wallets','audit','risk','tokens','webhooks'].includes(h)) {
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
  };
}
