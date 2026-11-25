'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function setup(replies, initialRun = null) {
    const node = (mode) => ({
        dataset: { mode }, textContent: '', hidden: false, disabled: false,
        addEventListener(name, callback) { this[name] = callback; }
    });
    const buttons = [node('scan'), node('all')];
    const status = node();
    const resume = node();
    const storage = new Map(initialRun ? [['adminRunId', initialRun]] : []);
    const calls = [];
    const timers = [];
    const context = vm.createContext({
        document: {
            querySelectorAll: () => buttons,
            getElementById: (id) => id === 'adminStatus' ? status : resume
        },
        sessionStorage: {
            getItem: (key) => storage.get(key) || null,
            setItem: (key, value) => storage.set(key, String(value)),
            removeItem: (key) => storage.delete(key)
        },
        setTimeout: (callback) => timers.push(callback),
        fetch: async (url, options) => {
            calls.push({ url, options });
            const reply = replies.shift();
            if (reply instanceof Error) throw reply;
            return { ok: reply.status < 400, status: reply.status, json: async () => reply.body };
        }
    });
    vm.runInContext(fs.readFileSync(path.join('static', 'js', 'admin.js'), 'utf8'), context);
    return { buttons, status, resume, storage, calls, timers };
}

const response = (body, status = 200) => ({ body, status });

async function main() {
    for (const mode of ['scan', 'all']) {
        const ui = setup([
            response({ run_id: 7 }, 202),
            response({ run: { status: 'running' } }),
            response({ run: { status: 'success', flights_imported: 12 } })
        ]);
        await ui.buttons[mode === 'scan' ? 0 : 1].click();
        assert.equal(ui.calls[0].url, `/api/admin/refresh/${mode}`);
        assert.equal(ui.calls[0].options.method, 'POST');
        assert.equal(ui.calls[0].options.headers['X-Requested-With'], 'XMLHttpRequest');
        assert.ok(ui.buttons.every((button) => button.disabled));
        assert.equal(ui.storage.get('adminRunId'), '7');
        await ui.timers.shift()();
        assert.match(ui.status.textContent, /12 Flugplaneinträge/);
        assert.ok(ui.buttons.every((button) => !button.disabled));
        assert.equal(ui.storage.size, 0);
    }
    for (const state of ['partial', 'failure']) {
        const ui = setup([
            response({ run_id: 8 }, 202),
            response({ run: { status: state, error_summary: 'Broken airline' } })
        ]);
        await ui.buttons[0].click();
        assert.match(ui.status.textContent, /Broken airline/);
        assert.equal(ui.storage.size, 0);
    }
    {
        const ui = setup([
            response({ run_id: 9 }, 202), new Error('offline'),
            response({ run: { status: 'success', flights_imported: 2 } })
        ]);
        await ui.buttons[0].click();
        assert.equal(ui.resume.hidden, false);
        assert.equal(ui.storage.get('adminRunId'), '9');
        assert.ok(ui.buttons.every((button) => button.disabled));
        await ui.resume.click();
        assert.equal(ui.storage.size, 0);
    }
    {
        const ui = setup([response({ run: { status: 'running' } })], '10');
        await new Promise(setImmediate);
        assert.equal(ui.calls[0].url, '/api/admin/refresh/10');
        assert.ok(ui.buttons.every((button) => button.disabled));
    }
    {
        const ui = setup([response({ error: 'Import already running' }, 409)]);
        await ui.buttons[0].click();
        assert.match(ui.status.textContent, /Import already running/);
        assert.ok(ui.buttons.every((button) => !button.disabled));
    }
    console.log('admin UI: OK');
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
