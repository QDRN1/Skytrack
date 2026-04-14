/* Thin fetch wrapper used by every page. */
(function () {
  async function request(method, url, body) {
    const opts = {
      method,
      headers: { 'Accept': 'application/json' },
      credentials: 'same-origin',
    };
    if (body !== undefined) {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(body);
    }
    const resp = await fetch(url, opts);
    const text = await resp.text();
    let data;
    try { data = text ? JSON.parse(text) : null; }
    catch { data = { _raw: text }; }
    if (!resp.ok) {
      const err = new Error((data && data.error) || resp.statusText);
      err.status = resp.status;
      err.data = data;
      throw err;
    }
    return data;
  }

  window.api = {
    get: (url) => request('GET', url),
    post: (url, body) => request('POST', url, body || {}),
  };
})();
