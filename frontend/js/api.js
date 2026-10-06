// RAT frontend API client.

async function request(path, { method = "GET", body, form } = {}) {
  const opts = { method, headers: {} };
  if (form) {
    opts.body = form; // FormData: let the browser set the content type
  } else if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  let data = null;
  try {
    data = await res.json();
  } catch {
    /* keep null */
  }
  if (!res.ok) {
    const err = new Error((data && data.error) || `${res.status} ${res.statusText}`);
    err.status = res.status;
    throw err;
  }
  return data;
}

export const api = {
  listRepos: () => request("/api/repos"),
  addRepoUrl: (url, name) => request("/api/repos/clone", { method: "POST", body: { url, name } }),
  addRepoZip: (file, name) => {
    const form = new FormData();
    form.append("file", file);
    if (name) form.append("name", name);
    return request("/api/repos/upload", { method: "POST", form });
  },
  deleteRepo: (id) => request(`/api/repos/${id}`, { method: "DELETE" }),
  refreshRepo: (id) => request(`/api/repos/${id}/refresh`, { method: "POST", body: {} }),
  rebuildRepo: (id) => request(`/api/repos/${id}/rebuild`, { method: "POST", body: {} }),
  meta: (id) => request(`/api/repos/${id}/meta`),
  ensureRef: (id, ref) => request(`/api/repos/${id}/refs/ensure`, { method: "POST", body: { ref } }),
  dashboard: (id, filters) => request(`/api/repos/${id}/dashboard`, { method: "POST", body: filters }),
  objectDetail: (id, filters) => request(`/api/repos/${id}/object`, { method: "POST", body: filters }),
  tree: (id, filters) => request(`/api/repos/${id}/tree`, { method: "POST", body: filters }),
  commits: (id, filters) => request(`/api/repos/${id}/commits`, { method: "POST", body: filters }),
  commitDetail: (id, hash) => request(`/api/repos/${id}/commit/${hash}`),
  searchObjects: (id, q) => request(`/api/repos/${id}/objects/search?q=${encodeURIComponent(q)}`),
  authors: (id) => request(`/api/repos/${id}/authors`),
  mergeAuthors: (id, canonical, keys) =>
    request(`/api/repos/${id}/authors/merge`, { method: "POST", body: { canonical, keys } }),
  unmergeAuthors: (id, canonical) =>
    request(`/api/repos/${id}/authors/unmerge`, { method: "POST", body: { canonical } }),
  compare: () => request("/api/compare"),
};
