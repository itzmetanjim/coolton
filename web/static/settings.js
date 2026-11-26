(function (global) {
  function createCooltonSettingsController({ apiJson, el, icon, policyUrl, acceptPolicy, setPolicyConsent }) {
    const settingsModal = document.getElementById('settingsModal');
    const settingsNav = document.getElementById('settingsNav');
    const settingsBody = document.getElementById('settingsBody');
    const settingsCloseBtn = document.getElementById('settingsClose');

    function makeField(labelText, value = '', placeholder = '', type = 'text', tag = 'input') {
      const field = el('div', 'settings-field');
      field.append(el('label', '', labelText));
      const input = document.createElement(tag);
      input.className = 'settings-input';
      if (tag === 'textarea') input.rows = 3;
      else input.type = type || 'text';
      input.value = value || '';
      input.placeholder = placeholder || '';
      field.appendChild(input);
      return { field, input };
    }

    // ---------------------------------------------------------------------
    // Settings popup: mirrors the Slack App Home controls (policy consent,
    // BYOK endpoints, instructions, reminders, fallback cache, MCP servers)
    // as REST-backed sections. Same stores, same validation as the Slack
    // handlers — a change made here is immediately visible there and back.
    // ---------------------------------------------------------------------

    let settingsData = null;
    let settingsSection = 'policy';
    let byokEditId = null;

    const SETTINGS_SECTIONS = [
      { id: 'policy', label: 'Policy consent', icon: 'i-shield' },
      { id: 'endpoints', label: 'BYOK endpoints', icon: 'i-key' },
      { id: 'instructions', label: 'Instructions', icon: 'i-book' },
      { id: 'reminders', label: 'Reminders', icon: 'i-bell' },
      { id: 'cache', label: 'Fallback cache', icon: 'i-zap' },
      { id: 'mcp', label: 'MCP servers', icon: 'i-plug' },
    ];

    async function openSettings() {
      try {
        settingsData = await apiJson('/api/settings');
      } catch (err) {
        return;
      }
      renderSettingsNav();
      renderSettingsSection();
      settingsModal.hidden = false;
      settingsCloseBtn.focus();
    }

    function closeSettings() {
      settingsModal.hidden = true;
    }

    async function refreshSettings() {
      try {
        settingsData = await apiJson('/api/settings');
      } catch (err) {
        return;
      }
      renderSettingsSection();
    }

    function renderSettingsNav() {
      settingsNav.innerHTML = '';
      const head = el('div', 'settings-nav-head');
      const img = document.createElement('img');
      img.src = settingsData.avatar_url;
      img.alt = '';
      head.append(img, el('span', '', settingsData.display_name));
      settingsNav.append(head);

      const list = el('div', 'settings-nav-list');
      for (const s of SETTINGS_SECTIONS) {
        const btn = el('button', 'settings-nav-item');
        btn.type = 'button';
        btn.dataset.section = s.id;
        btn.classList.toggle('active', s.id === settingsSection);
        btn.append(icon(s.icon), el('span', '', s.label));
        btn.addEventListener('click', () => {
          settingsSection = s.id;
          renderSettingsNav();
          renderSettingsSection();
        });
        list.appendChild(btn);
      }
      settingsNav.append(list);

      const signout = document.createElement('a');
      signout.className = 'settings-nav-signout';
      signout.href = '/oauth/logout';
      signout.append(icon('i-user'), el('span', '', 'Sign out'));
      settingsNav.append(signout);
    }

    function renderSettingsSection() {
      settingsBody.innerHTML = '';
      if (!settingsData) return;
      const renderers = {
        policy: renderSectionPolicy,
        endpoints: renderSectionEndpoints,
        instructions: renderSectionInstructions,
        reminders: renderSectionReminders,
        cache: renderSectionCache,
        mcp: renderSectionMcp,
      };
      (renderers[settingsSection] || renderSectionPolicy)();
    }

    function settingsNote(text, kind) {
      return el('div', 'settings-note' + (kind ? ' ' + kind : ''), text);
    }

    function settingsError(text) {
      return settingsNote(text, 'err');
    }

    function renderSectionPolicy() {
      const box = el('div');
      box.append(el('h2', 'settings-section-title', 'Policy consent'));
      box.append(el('p', 'settings-section-sub',
        'What Coolton may do with your data and messages, and the rules for using it. The full policy lives in the Slack canvas linked below.'));

      const row = el('div', 'settings-row');
      const label = el('div', 'settings-row-label');
      label.append(el('div', 'settings-row-title', settingsData.policy.consent ? 'You’re opted in' : 'You haven’t opted in yet'));
      label.append(el('div', 'settings-row-sub', settingsData.policy.consent
        ? 'Coolton can process your requests under the policy.'
        : 'Opt in before asking coolton to do anything — it won’t run without consent.'));
      row.appendChild(label);

      const actions = el('div', 'settings-actions');
      if (settingsData.policy.consent) {
        const out = el('button', 'settings-btn danger', 'Opt out');
        out.addEventListener('click', async () => {
          try {
            await apiJson('/api/settings/policy/opt-out', { method: 'POST' });
            settingsData.policy.consent = false;
            if (setPolicyConsent) setPolicyConsent(false);
            renderSettingsSection();
          } catch (err) {
            settingsBody.appendChild(settingsError('Couldn’t record opt-out.'));
          }
        });
        actions.appendChild(out);
      } else {
        const join = el('button', 'settings-btn primary', 'Opt in and join channel');
        const nojoin = el('button', 'settings-btn', 'Opt in without joining');
        join.addEventListener('click', () => acceptPolicy(true, null, refreshSettings));
        nojoin.addEventListener('click', () => acceptPolicy(false, null, refreshSettings));
        actions.append(join, nojoin);
      }
      row.appendChild(actions);
      box.appendChild(row);

      const link = document.createElement('a');
      link.href = policyUrl();
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      link.className = 'settings-btn';
      link.append(icon('i-book'), el('span', '', 'Read the policy'));
      box.appendChild(link);
      settingsBody.appendChild(box);
    }

    function renderSectionEndpoints() {
      const box = el('div');
      box.append(el('h2', 'settings-section-title', 'BYOK endpoints'));
      box.append(el('p', 'settings-section-sub',
        'Bring-your-own-key: OpenAI-compatible endpoints used instead of the global key, for text and/or image generation.'));

      const eps = settingsData.endpoints || [];
      if (!eps.length) {
        box.append(el('p', 'settings-empty', 'No endpoints configured.'));
      } else {
        for (const ep of eps) {
          const row = el('div', 'settings-row');
          const label = el('div', 'settings-row-label');
          const title = el('div', 'settings-row-title', ep.name);
          const tags = [];
          if (ep.id === settingsData.text_endpoint_id) tags.push('text');
          if (ep.id === settingsData.image_endpoint_id) tags.push('image');
          if (tags.length) title.append(' ', el('span', 'settings-note ok', `[${tags.join(', ')}]`));
          label.append(title, el('div', 'settings-row-sub', `${ep.model || 'default model'} · ${ep.base_url}`));
          row.appendChild(label);

          const actions = el('div', 'settings-actions');
          const edit = el('button', 'settings-btn', 'Edit');
          edit.addEventListener('click', () => { byokEditId = ep.id; renderSettingsSection(); });
          const del = el('button', 'settings-btn danger', 'Delete');
          del.addEventListener('click', async () => {
            del.disabled = true;
            try {
              await apiJson(`/api/settings/byok/${ep.id}`, { method: 'DELETE' });
              byokEditId = null;
              await refreshSettings();
            } catch (err) {
              settingsBody.appendChild(settingsError('Couldn’t delete endpoint.'));
              del.disabled = false;
            }
          });
          actions.append(edit, del);
          row.appendChild(actions);
          box.appendChild(row);
        }
      }

      box.append(el('h3', 'settings-sec-title', 'Default models'));
      box.appendChild(renderDefaultSelect('text', 'Text model', settingsData.text_endpoint_id, eps));
      box.appendChild(renderDefaultSelect('image', 'Image model', settingsData.image_endpoint_id, eps));

      box.append(el('h3', 'settings-sec-title', byokEditId ? 'Edit endpoint' : 'Add endpoint'));
      const target = byokEditId ? eps.find((e) => e.id === byokEditId) : null;
      box.appendChild(renderByokForm(target));
      settingsBody.appendChild(box);
    }

    function renderDefaultSelect(kind, labelText, currentId, eps) {
      const field = el('div', 'settings-field');
      field.append(el('label', '', labelText));
      const select = document.createElement('select');
      select.className = 'settings-input';
      const noneOpt = document.createElement('option');
      noneOpt.value = 'none';
      noneOpt.textContent = kind === 'text' ? 'Use global key (no BYOK)' : 'Disable image generation';
      select.appendChild(noneOpt);
      for (const ep of eps) {
        const opt = document.createElement('option');
        opt.value = ep.id;
        opt.textContent = `${ep.name} — ${ep.model || 'default'}`;
        select.appendChild(opt);
      }
      select.value = currentId || 'none';
      select.addEventListener('change', async () => {
        try {
          await apiJson(`/api/settings/byok/${kind}-default`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ endpoint_id: select.value }),
          });
          settingsData[kind === 'text' ? 'text_endpoint_id' : 'image_endpoint_id'] =
            select.value === 'none' ? null : select.value;
          renderSettingsSection();
        } catch (err) {
          settingsBody.appendChild(settingsError(`Couldn’t set ${kind} default.`));
          select.value = currentId || 'none';
        }
      });
      field.appendChild(select);
      return field;
    }

    function renderByokForm(target) {
      const form = el('div', 'settings-formgrid');
      const nameF = makeField('Name', target && target.name, 'My endpoint');
      const urlF = makeField('Base URL', target && target.base_url, 'https://api.example.com/v1');
      const keyF = makeField('API key', '', 'sk-…', 'password');
      const modelF = makeField('Model', target && target.model, 'gpt-4o-mini');
      form.append(nameF.field, urlF.field, keyF.field, modelF.field);

      const actions = el('div', 'settings-actions');
      const submit = el('button', 'settings-btn primary', target ? 'Save changes' : 'Add endpoint');
      const cancel = el('button', 'settings-btn', 'Cancel');
      cancel.addEventListener('click', () => { byokEditId = null; renderSettingsSection(); });
      submit.addEventListener('click', async () => {
        const name = nameF.input.value.trim();
        const baseUrl = urlF.input.value.trim();
        if (!name || !baseUrl) {
          settingsBody.appendChild(settingsError('Name and base URL are required.'));
          return;
        }
        submit.disabled = true;
        try {
          if (target) {
            const patch = { name, base_url: baseUrl, model: modelF.input.value.trim() };
            if (keyF.input.value) patch.api_key = keyF.input.value;
            await apiJson(`/api/settings/byok/${target.id}`, {
              method: 'PATCH',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify(patch),
            });
          } else {
            await apiJson('/api/settings/byok', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({
                name,
                base_url: baseUrl,
                api_key: keyF.input.value,
                model: modelF.input.value.trim(),
              }),
            });
          }
          byokEditId = null;
          await refreshSettings();
        } catch (err) {
          settingsBody.appendChild(settingsError('Couldn’t save endpoint — check the URL.'));
          submit.disabled = false;
        }
      });
      actions.append(submit, cancel);
      form.appendChild(actions);
      return form;
    }

    function renderSectionInstructions() {
      const box = el('div');
      box.append(el('h2', 'settings-section-title', 'Custom instructions'));
      box.append(el('p', 'settings-section-sub', 'Shapes how coolton talks to you. Same effect as setting them on Slack.'));

      const field = el('div', 'settings-field');
      field.append(el('label', '', 'Instructions'));
      const ta = document.createElement('textarea');
      ta.className = 'settings-input';
      ta.rows = 6;
      ta.value = settingsData.instructions || '';
      ta.placeholder = 'e.g. always reply in lowercase, call me Cap…';
      field.appendChild(ta);
      box.appendChild(field);

      const actions = el('div', 'settings-actions');
      const save = el('button', 'settings-btn primary', 'Save');
      save.addEventListener('click', async () => {
        save.disabled = true;
        try {
          await apiJson('/api/settings/instructions', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ text: ta.value }),
          });
          settingsBody.appendChild(settingsNote('Saved.', 'ok'));
        } catch (err) {
          settingsBody.appendChild(settingsError('Couldn’t save instructions.'));
        } finally {
          save.disabled = false;
        }
      });
      actions.appendChild(save);
      box.appendChild(actions);
      settingsBody.appendChild(box);
    }

    function renderSectionReminders() {
      const box = el('div');
      box.append(el('h2', 'settings-section-title', 'Reminders'));
      box.append(el('p', 'settings-section-sub', 'One-shot reminders coolton will DM you. Set new ones from Slack (“remind me in 10 minutes to …”).'));

      const pending = (settingsData.reminders || []).filter((r) => !r.sent && !r.failed);
      if (!pending.length) {
        box.append(el('p', 'settings-empty', 'No pending reminders.'));
      } else {
        for (const r of pending.slice(0, 20)) {
          const row = el('div', 'settings-row');
          const label = el('div', 'settings-row-label');
          label.append(el('div', 'settings-row-title', r.text));
          const due = new Date(r.due_at * 1000);
          const dueStr = due.toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
          label.append(el('div', 'settings-row-sub', `#${r.id} · due ${dueStr}`));
          row.appendChild(label);
          box.appendChild(row);
        }
      }
      settingsBody.appendChild(box);
    }

    function renderSectionCache() {
      const box = el('div');
      box.append(el('h2', 'settings-section-title', 'AI fallback cache'));
      box.append(el('p', 'settings-section-sub',
        'Every 30 minutes coolton re-tests each AI provider and caches which one to try first, so it doesn’t wait or retry when one is down. Clearing wipes that and re-probes immediately.'));

      const actions = el('div', 'settings-actions');
      const clear = el('button', 'settings-btn danger', 'Clear fallback cache');
      clear.addEventListener('click', async () => {
        clear.disabled = true;
        try {
          await apiJson('/api/settings/fallback-cache/clear', { method: 'POST' });
          settingsBody.appendChild(settingsNote('Cache cleared.', 'ok'));
        } catch (err) {
          settingsBody.appendChild(settingsError('Couldn’t clear the cache.'));
        } finally {
          clear.disabled = false;
        }
      });
      actions.appendChild(clear);

      if (settingsData.is_admin) {
        const test = el('button', 'settings-btn primary', 'Test all providers');
        test.addEventListener('click', async () => {
          test.disabled = true;
          test.textContent = 'Testing…';
          try {
            const res = await apiJson('/api/settings/providers/test', { method: 'POST' });
            const results = el('div', 'settings-note mono');
            const list = res.results || [];
            if (!list.length) {
              results.textContent = 'No providers configured.';
            } else {
              for (const p of list) {
                results.appendChild(document.createTextNode(`${p.ok ? '✓' : '✕'} ${p.provider} — ${p.display} (${p.elapsed}s)\n`));
              }
            }
            settingsBody.appendChild(results);
          } catch (err) {
            settingsBody.appendChild(settingsError('Provider test failed.'));
          } finally {
            test.disabled = false;
            test.textContent = 'Test all providers';
          }
        });
        actions.appendChild(test);
      }
      box.appendChild(actions);
      settingsBody.appendChild(box);
    }

    function renderSectionMcp() {
      const box = el('div');
      box.append(el('h2', 'settings-section-title', 'MCP servers'));
      box.append(el('p', 'settings-section-sub',
        'Model Context Protocol servers give coolton extra tools (Notion, Linear, …). Managed from here, same as Slack App Home.'));

      const slack = el('div', 'settings-row');
      const slackLabel = el('div', 'settings-row-label');
      slackLabel.append(el('div', 'settings-row-title', 'Slack MCP server'));
      slackLabel.append(el('div', 'settings-row-sub', settingsData.mcp_is_connected ? 'Connected' : 'Disconnected'));
      slack.appendChild(slackLabel);
      box.appendChild(slack);

      const servers = settingsData.mcp_servers || [];
      if (!servers.length) {
        box.append(el('p', 'settings-empty', 'No custom MCP servers.'));
      } else {
        for (const s of servers) {
          const row = el('div', 'settings-row');
          const label = el('div', 'settings-row-label');
          label.append(el('div', 'settings-row-title', s.name));
          label.append(el('div', 'settings-row-sub', `${s.url} · ${s.transport === 'sse' ? 'SSE' : 'Streamable HTTP'}`));
          row.appendChild(label);

          const actions = el('div', 'settings-actions');
          const rm = el('button', 'settings-btn danger', 'Remove');
          rm.addEventListener('click', async () => {
            rm.disabled = true;
            try {
              await apiJson(`/api/settings/mcp/${s.id}`, { method: 'DELETE' });
              await refreshSettings();
            } catch (err) {
              settingsBody.appendChild(settingsError('Couldn’t remove server.'));
              rm.disabled = false;
            }
          });
          actions.appendChild(rm);
          row.appendChild(actions);
          box.appendChild(row);
        }
      }

      box.append(el('h3', 'settings-sec-title', 'Add MCP server'));
      const form = el('div', 'settings-formgrid');
      const nameF = makeField('Name', '', 'Notion');
      const urlF = makeField('URL', '', 'https://mcp.example.com/sse');
      const tokF = makeField('Token (optional)', '', 'secret', 'password');
      const headerF = makeField('Headers (optional)', '', 'Authorization: Bearer …', '', 'textarea');
      const transportF = (() => {
        const field = el('div', 'settings-field');
        field.append(el('label', '', 'Transport'));
        const select = document.createElement('select');
        select.className = 'settings-input';
        for (const [v, t] of [['auto', 'Auto'], ['http', 'Streamable HTTP'], ['sse', 'SSE']]) {
          const opt = document.createElement('option');
          opt.value = v;
          opt.textContent = t;
          select.appendChild(opt);
        }
        field.appendChild(select);
        return { field, input: select };
      })();
      form.append(nameF.field, urlF.field, tokF.field, transportF.field, headerF.field);

      const actions = el('div', 'settings-actions');
      const submit = el('button', 'settings-btn primary', 'Connect server');
      submit.addEventListener('click', async () => {
        if (!nameF.input.value.trim() || !urlF.input.value.trim()) {
          settingsBody.appendChild(settingsError('Name and URL are required.'));
          return;
        }
        submit.disabled = true;
        try {
          await apiJson('/api/settings/mcp', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              name: nameF.input.value.trim(),
              url: urlF.input.value.trim(),
              token: tokF.input.value,
              transport: transportF.input.value,
              headers: headerF.input.value,
            }),
          });
          await refreshSettings();
        } catch (err) {
          settingsBody.appendChild(settingsError('Couldn’t connect — check the URL and token.'));
          submit.disabled = false;
        }
      });
      actions.appendChild(submit);
      form.appendChild(actions);
      box.appendChild(form);
      settingsBody.appendChild(box);
    }

    settingsCloseBtn.addEventListener('click', closeSettings);
    settingsModal.addEventListener('click', (e) => {
      if (e.target === settingsModal) closeSettings();
    });
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && !settingsModal.hidden) closeSettings();
    });


    return {
      open: openSettings,
      refreshIfOpen: async () => {
        if (!settingsModal.hidden) await refreshSettings();
      },
    };
  }

  global.createCooltonSettingsController = createCooltonSettingsController;
})(window);
