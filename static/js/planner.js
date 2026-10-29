const plannerForm = document.getElementById('plannerForm');
const plannerRoutes = document.getElementById('plannerRoutes');
const addRouteButton = document.getElementById('addRoute');
const routeTemplate = document.getElementById('plannerRouteTemplate');
const staleNotice = document.getElementById('plannerStale');
const maxRoutes = Number(plannerForm.dataset.maxRoutes);

function markPlanStale() {
    if (staleNotice) staleNotice.hidden = false;
}

function updateRouteControls() {
    const routes = plannerRoutes.querySelectorAll('.planner-route');
    routes.forEach((route, index) => {
        route.querySelector('legend').textContent = `Strecke ${index + 1}`;
        const remove = route.querySelector('.remove-route');
        remove.hidden = false;
        remove.disabled = routes.length === 1;
    });
    addRouteButton.disabled = routes.length >= maxRoutes;
}

addRouteButton.hidden = false;
addRouteButton.addEventListener('click', () => {
    if (plannerRoutes.querySelectorAll('.planner-route').length >= maxRoutes) return;
    const newRoute = routeTemplate.content.cloneNode(true);
    const destination = newRoute.querySelector('[name="destination"]');
    plannerRoutes.append(newRoute);
    updateRouteControls();
    markPlanStale();
    destination.focus();
});

plannerRoutes.addEventListener('click', (event) => {
    const remove = event.target.closest('.remove-route');
    if (!remove || plannerRoutes.querySelectorAll('.planner-route').length <= 1) return;
    remove.closest('.planner-route').remove();
    updateRouteControls();
    markPlanStale();
});

plannerForm.addEventListener('input', markPlanStale);
plannerForm.addEventListener('change', markPlanStale);
updateRouteControls();
