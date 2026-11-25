'use strict';

const buttons = document.querySelectorAll('[data-mode]');
const status = document.getElementById('adminStatus');
const resume = document.getElementById('resumeStatus');
let runId = sessionStorage.getItem('adminRunId');

function setBusy(busy) {
    buttons.forEach((button) => { button.disabled = busy; });
}

async function fetchResult(url, options) {
    const response = await fetch(url, options);
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
    return result;
}

async function poll() {
    setBusy(true);
    resume.hidden = true;
    try {
        const { run } = await fetchResult(`/api/admin/refresh/${runId}`);
        if (run.status === 'running') {
            status.textContent = 'Import läuft im Hintergrund …';
            setTimeout(poll, 3000);
            return;
        }
        status.textContent = run.status === 'success'
            ? `Import abgeschlossen. ${run.flights_imported} Flugplaneinträge importiert.`
            : `Import ${run.status === 'partial' ? 'nur teilweise abgeschlossen' : 'fehlgeschlagen'}: ${run.error_summary || 'Unbekannter Fehler'}`;
        sessionStorage.removeItem('adminRunId');
        runId = null;
        setBusy(false);
    } catch (error) {
        status.textContent = `Status konnte nicht geladen werden: ${error.message}. Der Import läuft möglicherweise weiter.`;
        resume.hidden = false;
    }
}

buttons.forEach((button) => button.addEventListener('click', async () => {
    setBusy(true);
    status.textContent = 'Import wird gestartet …';
    try {
        const result = await fetchResult(`/api/admin/refresh/${button.dataset.mode}`, {
            method: 'POST', headers: { 'X-Requested-With': 'XMLHttpRequest' }
        });
        runId = result.run_id;
        sessionStorage.setItem('adminRunId', runId);
        await poll();
    } catch (error) {
        status.textContent = `Start fehlgeschlagen: ${error.message}`;
        setBusy(false);
    }
}));

resume.addEventListener('click', poll);
if (runId) poll();
