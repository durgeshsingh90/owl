(() => {
  const dialog = document.getElementById('activity-dialog');
  const items = document.getElementById('activity-items');
  const summary = document.getElementById('activity-summary');
  let offset = 0, sequence = 0;
  const count = value => Number(value || 0).toLocaleString();
  const time = value => value ? new Date(value).toLocaleString() : 'In progress';
  function element(tag, text, parent) {
    const node = document.createElement(tag);
    node.textContent = text;
    parent.append(node);
    return node;
  }
  async function load() {
    const current = ++sequence;
    summary.textContent = 'Loading activity…';
    try {
      const response = await fetch(`/api/activity?limit=25&offset=${offset}`, {cache: 'no-store'});
      if (!response.ok) throw new Error('Unable to load activity history.');
      const data = await response.json();
      if (current !== sequence) return;
      items.replaceChildren();
      summary.textContent = `${count(data.total)} runs · ${data.total ? offset + 1 : 0}–${offset + data.items.length} shown`;
      if (!data.items.length) element('p', 'No activity recorded yet.', items);
      for (const run of data.items) {
        const section = element('section', '', items);
        section.style.cssText = 'border-top:1px solid #8886;padding:16px 0;overflow-x:auto';
        const repos = run.repositories;
        const sum = key => repos.reduce((total, repo) => total + Number(repo[key] || 0), 0);
        element('h3', `${time(run.started_at)} · ${run.status.replaceAll('_', ' ')}`, section);
        element('p', `${count(repos.length)} repositories checked · ${count(repos.filter(r => (r.new || r.updated || r.deleted)).length)} affected · ${count(sum('new') + sum('updated') + sum('deleted'))} PDF changes · New ${count(sum('new'))} · Updated ${count(sum('updated'))} · Deleted ${count(sum('deleted'))} · Failed ${count(sum('failed'))}`, section);
        if (run.completed_at) element('p', `Finished ${time(run.completed_at)}`, section);
        const table = element('table', '', section);
        table.style.cssText = 'width:100%;text-align:left';
        const head = element('tr', '', element('thead', '', table));
        ['Operation', 'Repository', 'Status', 'Total changes', 'New', 'Updated', 'Deleted', 'Unchanged', 'Failed'].forEach(label => element('th', label, head));
        const body = element('tbody', '', table);
        for (const repo of repos) {
          const row = element('tr', '', body);
          [repo.operation || 'Queued / retry', `${repo.project || repo.project_id}/${repo.repo}`,
            repo.status, count(Number(repo.new || 0) + Number(repo.updated || 0) + Number(repo.deleted || 0)),
            ...['new', 'updated', 'deleted', 'unchanged', 'failed'].map(key => count(repo[key]))
          ].forEach(value => element('td', value, row));
        }
      }
      document.getElementById('activity-prev').disabled = offset === 0;
      document.getElementById('activity-next').disabled = offset + 25 >= data.total;
    } catch (error) {
      if (current === sequence) summary.textContent = error.message;
    }
  }
  document.getElementById('activity-button').addEventListener('click', () => {offset = 0; dialog.showModal(); load();});
  document.getElementById('activity-close').addEventListener('click', () => dialog.close());
  document.getElementById('activity-refresh').addEventListener('click', load);
  document.getElementById('activity-prev').addEventListener('click', () => {offset = Math.max(0, offset - 25); load();});
  document.getElementById('activity-next').addEventListener('click', () => {offset += 25; load();});
})();
