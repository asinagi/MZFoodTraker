/* Adds this page's per-process admin token to same-origin state-changing requests only. */
(() => {
  const token = document.querySelector('meta[name="mz-admin-token"]')?.content;
  if (!token) return;
  const original = window.fetch.bind(window);
  window.fetch = (input, init = {}) => {
    const request = input instanceof Request ? input : null;
    const method = (init.method || request?.method || 'GET').toUpperCase();
    const url = new URL(request ? request.url : input, location.href);
    if (method !== 'GET' && method !== 'HEAD' && url.origin === location.origin) {
      const headers = new Headers(init.headers || request?.headers);
      headers.set('X-MZ-Admin-Token', token);
      init = {...init, headers};
    }
    return original(input, init);
  };
})();
