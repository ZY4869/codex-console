(function () {
    const app = window.GrokRegisterApp = window.GrokRegisterApp || {};

    app.api = {
        loadConfig() {
            return window.api.get('/grok-register/config');
        },
        loadAvailableEmailServices() {
            return window.api.get('/registration/available-services');
        },
        saveConfig(payload) {
            return window.api.put('/grok-register/config', payload);
        },
        createTask(payload) {
            return window.api.post('/grok-register/create', payload);
        },
        loadTask(taskUuid) {
            return window.api.get(`/grok-register/${taskUuid}`);
        },
        loadTasks() {
            return window.api.get('/grok-register/tasks?limit=20');
        },
        loadLogs(taskUuid) {
            return window.api.get(`/grok-register/${taskUuid}/logs`);
        },
        cancelTask(taskUuid) {
            return window.api.post(`/grok-register/${taskUuid}/cancel`, {});
        },
        startSolver(command) {
            return window.api.post('/grok-register/solver/start', { command });
        },
        stopSolver() {
            return window.api.post('/grok-register/solver/stop', {});
        },
        loadSolverStatus(url) {
            const suffix = url ? `?url=${encodeURIComponent(url)}` : '';
            return window.api.get(`/grok-register/solver/status${suffix}`);
        },
        startFlaresolverr(url) {
            return window.api.post('/grok-register/flaresolverr/start', { url });
        },
        stopFlaresolverr(url) {
            return window.api.post('/grok-register/flaresolverr/stop', { url });
        },
        loadFlaresolverrStatus(url) {
            const suffix = url ? `?url=${encodeURIComponent(url)}` : '';
            return window.api.get(`/grok-register/flaresolverr/status${suffix}`);
        },
    };
})();
