const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

function setup(hasPreview) {
    const handlers = {};
    const routes = [];
    const notice = hasPreview ? { hidden: true } : null;
    const form = {
        dataset: { maxRoutes: '20' },
        addEventListener: (name, callback) => { handlers[`form:${name}`] = callback; },
    };
    const add = {
        hidden: true,
        addEventListener: (name, callback) => { handlers[`add:${name}`] = callback; },
    };
    function makeRoute() {
        const legend = {};
        const destination = { focused: false, focus() { this.focused = true; } };
        const remove = { closest: () => route };
        const route = {
            legend, destination, removeButton: remove,
            querySelector(selector) {
                return { legend, '.remove-route': remove, '[name="destination"]': destination }[selector];
            },
            remove() { routes.splice(routes.indexOf(route), 1); },
        };
        return route;
    }
    routes.push(makeRoute());
    const container = {
        querySelectorAll: () => routes,
        append: (route) => routes.push(route),
        addEventListener: (name, callback) => { handlers[`routes:${name}`] = callback; },
    };
    const elements = {
        plannerForm: form, plannerRoutes: container, addRoute: add,
        plannerRouteTemplate: { content: { cloneNode: makeRoute } },
        plannerStale: notice,
    };
    vm.runInNewContext(
        fs.readFileSync(path.join(__dirname, '..', 'static', 'js', 'planner.js'), 'utf8'),
        { document: { getElementById: (id) => elements[id] } },
    );
    return { handlers, routes, notice, add };
}

for (const hasPreview of [true, false]) {
    const { handlers, routes, notice, add } = setup(hasPreview);
    assert.equal(add.hidden, false);
    assert.equal(routes[0].removeButton.disabled, true);
    assert.equal(routes[0].removeButton.hidden, false);
    handlers['add:click']();
    assert.equal(routes.length, 2);
    assert.equal(routes[1].legend.textContent, 'Strecke 2');
    assert.equal(routes[1].destination.focused, true);
    assert.equal(routes[0].removeButton.disabled, false);
    if (hasPreview) assert.equal(notice.hidden, false);
    handlers['routes:click']({ target: { closest: () => routes[0].removeButton } });
    assert.equal(routes.length, 1);
    assert.equal(routes[0].legend.textContent, 'Strecke 1');
    assert.equal(routes[0].removeButton.disabled, true);
    handlers['routes:click']({ target: { closest: () => routes[0].removeButton } });
    assert.equal(routes.length, 1);
    for (let i = 0; i < 25; i++) handlers['add:click']();
    assert.equal(routes.length, 20);
    assert.equal(add.disabled, true);
    if (hasPreview) {
        notice.hidden = true;
        handlers['form:input']();
        assert.equal(notice.hidden, false);
        notice.hidden = true;
        handlers['form:change']();
        assert.equal(notice.hidden, false);
    }
}
console.log('planner UI: OK');
