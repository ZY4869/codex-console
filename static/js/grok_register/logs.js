(function () {
    const app = window.GrokRegisterApp = window.GrokRegisterApp || {};
    const { state, elements } = app;

    function addLine(message) {
        if (!message || state.displayedLogs.has(message)) {
            return;
        }
        state.displayedLogs.add(message);
        const line = document.createElement('div');
        line.className = 'log-line info';
        line.textContent = message;
        elements.consoleLog.appendChild(line);
        elements.consoleLog.scrollTop = elements.consoleLog.scrollHeight;
        app.logs.renderSummary();
    }

    app.logs = {
        init() {
            elements.clearLogBtn.addEventListener('click', () => {
                state.displayedLogs.clear();
                elements.consoleLog.innerHTML = '';
                app.logs.renderSummary();
            });
        },

        async load(taskUuid) {
            try {
                const response = await app.api.loadLogs(taskUuid);
                state.displayedLogs.clear();
                elements.consoleLog.innerHTML = '';
                (response.logs || []).forEach(addLine);
            } catch (error) {
                // ignore initial log load failures
            }
        },

        connect(taskUuid) {
            if (!taskUuid) {
                return;
            }
            if (state.socket) {
                state.socket.close();
                state.socket = null;
            }

            const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
            state.socket = new WebSocket(`${protocol}//${window.location.host}/api/ws/grok-register/${taskUuid}`);
            state.socket.onmessage = (event) => {
                const payload = JSON.parse(event.data);
                if (payload.type === 'log' && payload.message) {
                    addLine(payload.message);
                }
                if (payload.type === 'status' && payload.snapshot) {
                    app.taskView.applyTaskSnapshot(payload.snapshot, { reloadLogs: false });
                }
                if (payload.type === 'ping') {
                    state.socket.send(JSON.stringify({ type: 'pong' }));
                }
            };
        },

        renderSummary() {
            const count = state.displayedLogs.size;
            elements.logSummary.textContent = count ? `当前已显示 ${count} 条日志` : '暂无日志输出';
        },

        addLine,
    };
})();
