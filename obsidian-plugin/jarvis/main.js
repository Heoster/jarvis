/*
 * JARVIS for Obsidian — talk to your local JARVIS operator from inside a note.
 *
 * Plain CommonJS on purpose: no bundler, no npm install, no build step.  Drop
 * this folder into `<vault>/.obsidian/plugins/jarvis/` (or run
 * `python -m jarvis obsidian plugins --install`) and enable it.
 *
 * Everything goes to the JARVIS API on localhost:
 *   POST /ask   {message}            → {answer, provider, model, tool_calls}
 *   POST /tool  {name, args}         → {ok, content, error}
 *   GET  /health                     → {status: "ok"}
 *   GET  /status                     → full operator status
 */

const { Plugin, PluginSettingTab, Setting, Notice, Modal, ItemView,
        MarkdownRenderer, requestUrl, moment } = require('obsidian');

const VIEW_TYPE_JARVIS = 'jarvis-chat-view';

const DEFAULT_SETTINGS = {
  apiUrl: 'http://127.0.0.1:8077',
  token: '',
  timeoutMs: 120000,
  insertMode: 'callout',        // callout | plain | none
  flashcardHeading: 'Flashcards',
  summaryHeading: 'JARVIS summary',
  showStatusBar: true,
};

/* ────────────────────────────── helpers ───────────────────────────────── */

function joinUrl(base, path) {
  return String(base || '').replace(/\/+$/, '') + path;
}

function nowStamp() {
  return moment ? moment().format('YYYY-MM-DD HH:mm') : new Date().toISOString().slice(0, 16);
}

function stripCodeFence(text) {
  const match = String(text || '').match(/^```[a-zA-Z]*\n([\s\S]*?)\n```$/);
  return (match ? match[1] : text || '').trim();
}

/** Lines of the form "Question :: Answer" → the spaced-repetition format. */
function extractCards(text) {
  return String(text || '')
    .split('\n')
    .map((line) => line.replace(/^\s*(?:[-*+]|\d+\.)\s*/, '').trim())
    .filter((line) => line.includes('::') && line.split('::')[0].trim().length > 3)
    .filter((line) => !line.startsWith('#'))
    .map((line) => `- ${line}`);
}

/* ──────────────────────────── settings tab ─────────────────────────────── */

class JarvisSettingTab extends PluginSettingTab {
  constructor(app, plugin) {
    super(app, plugin);
    this.plugin = plugin;
  }

  display() {
    const { containerEl } = this;
    containerEl.empty();
    containerEl.createEl('h2', { text: 'JARVIS' });

    new Setting(containerEl)
      .setName('API URL')
      .setDesc('Where the JARVIS API server is listening. Start it with: python -m jarvis serve')
      .addText((text) => text
        .setPlaceholder('http://127.0.0.1:8077')
        .setValue(this.plugin.settings.apiUrl)
        .onChange(async (value) => {
          this.plugin.settings.apiUrl = value.trim() || DEFAULT_SETTINGS.apiUrl;
          await this.plugin.saveSettings();
          this.plugin.pingServer(true);
        }));

    new Setting(containerEl)
      .setName('Bearer token')
      .setDesc('Only needed if you set JARVIS_API_TOKEN on the server.')
      .addText((text) => text
        .setPlaceholder('(empty)')
        .setValue(this.plugin.settings.token)
        .onChange(async (value) => {
          this.plugin.settings.token = value.trim();
          await this.plugin.saveSettings();
        }));

    new Setting(containerEl)
      .setName('Timeout (ms)')
      .setDesc('How long to wait for an answer. Local models on a laptop need 1–2 minutes.')
      .addText((text) => text
        .setValue(String(this.plugin.settings.timeoutMs))
        .onChange(async (value) => {
          const parsed = parseInt(value, 10);
          if (!Number.isNaN(parsed) && parsed > 1000) {
            this.plugin.settings.timeoutMs = parsed;
            await this.plugin.saveSettings();
          }
        }));

    new Setting(containerEl)
      .setName('Insert answers as')
      .setDesc('Callout keeps your notes tidy and collapsible.')
      .addDropdown((drop) => drop
        .addOption('callout', 'Callout block')
        .addOption('plain', 'Plain markdown')
        .addOption('none', 'Nothing (answer shown in a notice only)')
        .setValue(this.plugin.settings.insertMode)
        .onChange(async (value) => {
          this.plugin.settings.insertMode = value;
          await this.plugin.saveSettings();
        }));

    new Setting(containerEl)
      .setName('Flashcard heading')
      .setDesc('Heading JARVIS writes generated cards under.')
      .addText((text) => text
        .setValue(this.plugin.settings.flashcardHeading)
        .onChange(async (value) => {
          this.plugin.settings.flashcardHeading = value.trim() || 'Flashcards';
          await this.plugin.saveSettings();
        }));

    new Setting(containerEl)
      .setName('Show status bar')
      .setDesc('A small “JARVIS ●” indicator showing whether the server is reachable.')
      .addToggle((toggle) => toggle
        .setValue(this.plugin.settings.showStatusBar)
        .onChange(async (value) => {
          this.plugin.settings.showStatusBar = value;
          await this.plugin.saveSettings();
          this.plugin.updateStatusBar();
        }));

    containerEl.createEl('p', {
      text: 'JARVIS is free-tier only and runs locally: your notes are sent to '
        + '127.0.0.1, never to a third party unless you configured a cloud provider.',
    });
  }
}

/* ────────────────────────── ask-question modal ─────────────────────────── */

class AskModal extends Modal {
  constructor(app, plugin, title, placeholder, onSubmit) {
    super(app);
    this.plugin = plugin;
    this.titleText = title;
    this.placeholder = placeholder;
    this.onSubmit = onSubmit;
  }

  onOpen() {
    const { contentEl } = this;
    contentEl.createEl('h3', { text: this.titleText });
    const input = contentEl.createEl('textarea', { cls: 'jarvis-modal-input' });
    input.placeholder = this.placeholder;
    input.rows = 3;
    const actions = contentEl.createDiv({ cls: 'jarvis-modal-actions' });
    const ask = actions.createEl('button', { text: 'Ask JARVIS', cls: 'mod-cta' });
    actions.createEl('button', { text: 'Cancel' }).onclick = () => this.close();
    const submit = () => {
      const value = input.value.trim();
      if (!value) return;
      this.close();
      this.onSubmit(value);
    };
    ask.onclick = submit;
    input.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) submit();
    });
    window.setTimeout(() => input.focus(), 50);
  }

  onClose() {
    this.contentEl.empty();
  }
}

/* ─────────────────────────────── chat view ─────────────────────────────── */

class JarvisView extends ItemView {
  constructor(leaf, plugin) {
    super(leaf);
    this.plugin = plugin;
    this.history = [];
  }

  getViewType() { return VIEW_TYPE_JARVIS; }
  getDisplayText() { return 'JARVIS'; }
  getIcon() { return 'bot'; }

  async onOpen() {
    const root = this.containerEl.children[1];
    root.empty();
    root.addClass('jarvis-view');

    const header = root.createDiv({ cls: 'jarvis-view-header' });
    header.createEl('span', { text: 'JARVIS', cls: 'jarvis-view-title' });
    this.statusEl = header.createEl('span', { text: '…', cls: 'jarvis-view-status' });

    this.messagesEl = root.createDiv({ cls: 'jarvis-messages' });
    this.addMessage('assistant',
      'Ask me about this note, your vault, or anything else. '
      + 'Try: “summarize this note”, “make flashcards”, “what is due today”.');

    const composer = root.createDiv({ cls: 'jarvis-composer' });
    this.inputEl = composer.createEl('textarea', { cls: 'jarvis-input' });
    this.inputEl.placeholder = 'Ask JARVIS…  (Ctrl+Enter to send)';
    const buttons = composer.createDiv({ cls: 'jarvis-composer-actions' });
    const send = buttons.createEl('button', { text: 'Send', cls: 'mod-cta' });
    const noteButton = buttons.createEl('button', { text: 'Use current note' });
    send.onclick = () => this.send();
    noteButton.onclick = () => this.send(true);
    this.inputEl.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) this.send();
    });

    this.plugin.pingServer();
  }

  async onClose() {
    this.containerEl.empty();
  }

  setStatus(text, cls) {
    if (!this.statusEl) return;
    this.statusEl.setText(text);
    this.statusEl.className = `jarvis-view-status ${cls || ''}`;
  }

  addMessage(role, text) {
    const bubble = this.messagesEl.createDiv({ cls: `jarvis-msg jarvis-msg-${role}` });
    const body = bubble.createDiv({ cls: 'jarvis-msg-body' });
    if (role === 'assistant') {
      MarkdownRenderer.renderMarkdown(text, body, '', this).catch(() => {
        body.setText(text);
      });
      const actions = bubble.createDiv({ cls: 'jarvis-msg-actions' });
      actions.createEl('button', { text: 'Insert into note' }).onclick =
        () => this.plugin.insertText(text);
      actions.createEl('button', { text: 'Copy' }).onclick = async () => {
        await navigator.clipboard.writeText(text);
        new Notice('Copied JARVIS answer.');
      };
      actions.createEl('button', { text: 'Flashcards' }).onclick =
        () => this.plugin.insertFlashcards(text);
    } else {
      body.setText(text);
    }
    this.messagesEl.scrollTop = this.messagesEl.scrollHeight;
    return bubble;
  }

  async send(withNote) {
    const value = this.inputEl.value.trim();
    if (withNote) {
      const note = await this.plugin.readActiveNote();
      if (!note) { new Notice('Open a note first.'); return; }
      this.inputEl.value = '';
      this.addMessage('user', `${value || 'Tell me about this note'} — (note: ${note.path})`);
      this.setStatus('thinking…');
      const answer = await this.plugin.askAboutNote(value || 'Summarize this note.', note);
      this.setStatus(this.plugin.serverOk ? 'online' : 'offline');
      this.addMessage('assistant', answer);
      return;
    }
    if (!value) return;
    this.inputEl.value = '';
    this.addMessage('user', value);
    this.setStatus('thinking…');
    const answer = await this.plugin.ask(value);
    this.setStatus(this.plugin.serverOk ? 'online' : 'offline');
    this.addMessage('assistant', answer);
  }
}

/* ─────────────────────────────── plugin ────────────────────────────────── */

module.exports = class JarvisPlugin extends Plugin {
  async onload() {
    await this.loadSettings();
    this.serverOk = null;

    this.addSettingTab(new JarvisSettingTab(this.app, this));

    this.registerView(VIEW_TYPE_JARVIS, (leaf) => new JarvisView(leaf, this));

    this.statusBarEl = this.addStatusBarItem();
    this.statusBarEl.addClass('jarvis-status-bar');
    this.updateStatusBar();

    this.addRibbonIcon('bot', 'Open JARVIS panel', () => this.activateView());

    /* ── commands ── */
    this.addCommand({
      id: 'jarvis-ask',
      name: 'Ask JARVIS a question',
      callback: () => new AskModal(this.app, this, 'Ask JARVIS',
        'e.g. explain this chapter in simple words',
        (question) => this.run((answer) => this.insertText(answer), () => this.ask(question))),
    });

    this.addCommand({
      id: 'jarvis-explain-note',
      name: 'Explain the current note',
      checkCallback: (checking) => {
        if (!this.hasActiveNote()) return false;
        if (!checking) {
          this.run((answer) => this.insertText(answer), async () => {
            const note = await this.readActiveNote();
            if (!note) return '⚠ Open a note first.';
            return this.askAboutNote('Explain this note for a student: core ideas, then the '
              + 'tricky parts, then a worked example. Use short paragraphs.', note);
          });
        }
        return true;
      },
    });

    this.addCommand({
      id: 'jarvis-summarize-note',
      name: 'Summarize the current note',
      checkCallback: (checking) => {
        if (!this.hasActiveNote()) return false;
        if (!checking) {
          this.run((answer) => this.insertUnderHeading(this.settings.summaryHeading, answer),
            async () => {
              const note = await this.readActiveNote();
              if (!note) return '⚠ Open a note first.';
              return this.askAboutNote('Summarize this note for revision: 5 bullet points plus '
                + '3 exam questions it should let me answer.', note);
            });
        }
        return true;
      },
    });

    this.addCommand({
      id: 'jarvis-explain-selection',
      name: 'Explain the selected text',
      editorCallback: (editor) => {
        const selected = editor.getSelection();
        if (!selected) { new Notice('Select some text first.'); return; }
        this.run((answer) => editor.replaceSelection(selected + '\n\n' + this.wrap(answer)),
          () => this.ask(`Explain this for a student, step by step:\n\n${selected}`));
      },
    });

    this.addCommand({
      id: 'jarvis-flashcards-note',
      name: 'Make flashcards from the current note',
      checkCallback: (checking) => {
        if (!this.hasActiveNote()) return false;
        if (!checking) {
          this.run((answer) => this.insertUnderHeading(this.settings.flashcardHeading,
            this.cardsBlock(answer)),
            async () => {
              const note = await this.readActiveNote();
              if (!note) return '⚠ Open a note first.';
              return this.askAboutNote('Create flashcards from this note. Reply with ONLY lines '
                + 'of the form "Question :: Answer" — 8 to 15 cards, atomic facts, no '
                + 'numbering, no markdown inside the lines.', note);
            });
        }
        return true;
      },
    });

    this.addCommand({
      id: 'jarvis-revision-queue',
      name: 'Show today’s revision queue',
      callback: () => this.run((answer) => this.insertText(answer),
        () => this.toolCall('obsidian_revision_queue', { limit: 12 })),
    });

    this.addCommand({
      id: 'jarvis-research',
      name: 'Research this topic with JARVIS (web + notes)',
      callback: () => new AskModal(this.app, this, 'Research with JARVIS',
        'e.g. projectile motion: formulas and common exam traps',
        (topic) => this.run((answer) => this.insertUnderHeading('Research', answer),
          () => this.ask(`Research "${topic}". Use your search and read tools, then answer `
            + 'with headings, key formulas/facts, and sources as markdown links.'))),
    });

    this.addCommand({
      id: 'jarvis-remember-selection',
      name: 'Save selection to JARVIS memory',
      editorCallback: (editor) => {
        const selected = editor.getSelection();
        if (!selected) { new Notice('Select some text first.'); return; }
        this.toolCall('remember', { fact: selected.slice(0, 900), importance: 0.8 })
          .then(() => new Notice('Saved to JARVIS long-term memory.'));
      },
    });

    this.addCommand({
      id: 'jarvis-open-panel',
      name: 'Open the JARVIS panel',
      callback: () => this.activateView(),
    });

    this.addCommand({
      id: 'jarvis-test-connection',
      name: 'Test the JARVIS connection',
      callback: async () => {
        const ok = await this.pingServer(true);
        new Notice(ok
          ? 'JARVIS is reachable ✅'
          : `Cannot reach JARVIS at ${this.settings.apiUrl}.\nStart it with: python -m jarvis serve`);
      },
    });

    this.pingServer();
    this.registerInterval(window.setInterval(() => this.pingServer(),
      Math.max(30, 60) * 1000));
  }

  onunload() {
    this.app.workspace.getLeavesOfType(VIEW_TYPE_JARVIS).forEach((leaf) => leaf.detach());
  }

  /* ── settings ── */
  async loadSettings() {
    this.settings = Object.assign({}, DEFAULT_SETTINGS, await this.loadData());
  }

  async saveSettings() {
    await this.saveData(this.settings);
  }

  /* ── http ── */
  headers() {
    const headers = { 'Content-Type': 'application/json' };
    if (this.settings.token) headers.Authorization = `Bearer ${this.settings.token}`;
    return headers;
  }

  async post(path, body) {
    const response = await requestUrl({
      url: joinUrl(this.settings.apiUrl, path),
      method: 'POST',
      headers: this.headers(),
      body: JSON.stringify(body || {}),
      throw: false,
    });
    if (response.status >= 400) {
      throw new Error(`JARVIS HTTP ${response.status}: ${(response.text || '').slice(0, 200)}`);
    }
    return response.json;
  }

  /** Ask a free-form question; returns markdown text (never throws). */
  async ask(message) {
    try {
      const data = await this.post('/ask', { message });
      const meta = data.provider ? `\n\n---\n_${data.provider}/${data.model}` +
        `${data.tool_calls && data.tool_calls.length
            ? ` · ${data.tool_calls.length} tool call(s): `
              + data.tool_calls.map((c) => c.name).join(', ')
            : ''}_` : '';
      this.serverOk = true;
      this.updateStatusBar();
      return `${data.answer || '(no answer)'}${meta}`;
    } catch (error) {
      this.serverOk = false;
      this.updateStatusBar();
      return this.offlineMessage(error);
    }
  }

  /** Ask about a specific note: the note is pasted in as context. */
  async askAboutNote(instruction, note) {
    const body = `${instruction}\n\n---\nNote: ${note.path}\n\n${note.content}`;
    return this.ask(body);
  }

  async toolCall(name, args) {
    try {
      const data = await this.post('/tool', { name, args: args || {} });
      this.serverOk = true;
      this.updateStatusBar();
      if (!data.ok) return `⚠ ${data.error || 'tool failed'}`;
      return data.content || '(empty result)';
    } catch (error) {
      this.serverOk = false;
      this.updateStatusBar();
      return this.offlineMessage(error);
    }
  }

  async pingServer(notify) {
    try {
      const response = await requestUrl({
        url: joinUrl(this.settings.apiUrl, '/health'),
        method: 'GET',
        headers: this.headers(),
        throw: false,
      });
      this.serverOk = response.status < 400;
    } catch (error) {
      this.serverOk = false;
    }
    this.updateStatusBar();
    if (notify && this.serverOk) new Notice('JARVIS is reachable ✅');
    return this.serverOk;
  }

  offlineMessage(error) {
    return `🚫 **JARVIS is not reachable.**\n\n`
      + 'Start the local server in a terminal:\n\n```bash\npython -m jarvis serve '
      + '--port 8077\n```\n\n'
      + `Tried: \`${this.settings.apiUrl}\`\n\n_details: ${String(error && error.message || error)}_`;
  }

  updateStatusBar() {
    if (!this.statusBarEl) return;
    if (!this.settings || !this.settings.showStatusBar) {
      this.statusBarEl.setText('');
      return;
    }
    const symbol = this.serverOk === null ? '…' : (this.serverOk ? '●' : '○');
    const label = this.serverOk === null ? 'JARVIS' : (this.serverOk ? 'JARVIS' : 'JARVIS offline');
    this.statusBarEl.setText(`${symbol} ${label}`);
    this.statusBarEl.toggleClass('jarvis-status-offline', this.serverOk === false);
  }

  /* ── vault helpers ── */
  /** Commands must decide synchronously whether they apply. */
  hasActiveNote() {
    return Boolean(this.app.workspace.getActiveFile());
  }

  /** The note currently in focus, with its text (async: vault reads are async). */
  async readActiveNote() {
    const file = this.app.workspace.getActiveFile();
    if (!file) return null;
    const content = await this.app.vault.cachedRead(file);
    return { file, path: file.path, content };
  }

  async activateView() {
    const { workspace } = this.app;
    let leaf = workspace.getLeavesOfType(VIEW_TYPE_JARVIS)[0];
    if (!leaf) {
      leaf = workspace.getRightLeaf(false);
      await leaf.setViewState({ type: VIEW_TYPE_JARVIS, active: true });
    }
    workspace.revealLeaf(leaf);
  }

  /* ── insertion ── */
  wrap(text) {
    if (this.settings.insertMode === 'none') return text;
    if (this.settings.insertMode === 'plain') return text;
    const stamp = nowStamp();
    const body = String(text).split('\n').map((line) => `> ${line}`).join('\n');
    return `> [!jarvis] JARVIS · ${stamp}\n${body}`;
  }

  insertText(text) {
    const editor = this.app.workspace.activeEditor
      && this.app.workspace.activeEditor.editor;
    if (!editor) { new Notice('Open a note in edit mode to insert.'); return; }
    if (this.settings.insertMode === 'none') {
      new Notice('Answer ready (insert mode is off in settings).');
      return;
    }
    editor.replaceSelection(`\n\n${this.wrap(text)}\n`);
  }

  /** Insert under a heading, creating it at the end when missing. */
  insertUnderHeading(heading, text) {
    const editor = this.app.workspace.activeEditor
      && this.app.workspace.activeEditor.editor;
    if (!editor) { new Notice('Open a note in edit mode to insert.'); return; }
    const lines = editor.getValue().split('\n');
    const wanted = String(heading).toLowerCase();
    let headingIndex = -1;
    let level = 2;
    for (let index = 0; index < lines.length; index += 1) {
      const match = lines[index].match(/^(#{1,6})\s+(.*)$/);
      if (match && match[2].trim().toLowerCase() === wanted) {
        headingIndex = index;
        level = match[1].length;
        break;
      }
    }
    const block = this.wrap(text);
    if (headingIndex === -1) {
      editor.setValue(`${editor.getValue().replace(/\s+$/, '')}\n\n${'#'.repeat(level)} ${heading}\n\n${block}\n`);
      new Notice(`Added a “${heading}” section.`);
      return;
    }
    let insertAt = lines.length;
    for (let index = headingIndex + 1; index < lines.length; index += 1) {
      const match = lines[index].match(/^(#{1,6})\s+/);
      if (match && match[1].length <= level) { insertAt = index; break; }
    }
    lines.splice(insertAt, 0, '', block);
    editor.setValue(lines.join('\n'));
    new Notice(`Added to “${heading}”.`);
  }

  cardsBlock(text) {
    const cards = extractCards(stripCodeFence(text));
    if (!cards.length) {
      return this.wrap('⚠ JARVIS did not return card lines. Raw answer:\n\n' + text);
    }
    return `_Generated by JARVIS · ${nowStamp()}_\n\n${cards.join('\n')}`;
  }

  insertFlashcards(text) {
    this.insertUnderHeading(this.settings.flashcardHeading, this.cardsBlock(text));
  }

  /** Run a task with consistent error + notice handling. */
  async run(insert, task) {
    const notice = new Notice('JARVIS is thinking…', 0);
    try {
      const answer = await task();
      if (insert) insert(answer);
      else new Notice(String(answer).slice(0, 240), 8000);
    } catch (error) {
      new Notice(`JARVIS error: ${error && error.message ? error.message : error}`, 8000);
    } finally {
      notice.hide();
    }
  }
};
