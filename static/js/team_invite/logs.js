(function () {
    const app = window.TeamInviteApp = window.TeamInviteApp || {};
    const { state, elements } = app;

    let connectedTaskUuid = null;

    function resetLogs() {
        state.displayedLogs.clear();
        elements.consoleLog.innerHTML = '';
    }

    function addLogLine(message) {
        if (!message || state.displayedLogs.has(message)) {
            return;
        }
        state.displayedLogs.add(message);
        const line = document.createElement('div');
        line.className = 'log-line info';
        line.textContent = message;
        elements.consoleLog.appendChild(line);
        elements.consoleLog.scrollTop = elements.consoleLog.scrollHeight;
    }

    async function loadTaskLogs(taskUuid, { reset = false } = {}) {
        if (!taskUuid) return;
        try {
            const response = await app.api.loadLogs(taskUuid);
            if (reset) {
                resetLogs();
            }
            (response.logs || []).forEach((line) => addLogLine(line));
        } catch (error) {
            // 保持静默，避免日志面板干扰主流程。
        }
    }

    function disconnect() {
        if (state.inviteSocket) {
            state.inviteSocket.close();
            state.inviteSocket = null;
        }
        connectedTaskUuid = null;
    }

    function connect(taskUuid) {
        if (!taskUuid) return;
        if (state.inviteSocket && connectedTaskUuid === taskUuid) {
            return;
        }
        disconnect();

        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        const socket = new WebSocket(`${protocol}//${window.location.host}/api/ws/team-invite/${taskUuid}`);
        connectedTaskUuid = taskUuid;
        state.inviteSocket = socket;

        socket.onmessage = async (event) => {
            const payload = JSON.parse(event.data);
            if (payload.type === 'log' && payload.message) {
                addLogLine(payload.message);
            }
            if (payload.type === 'status' && payload.snapshot) {
                const snapshot = { ...payload.snapshot };
                if (payload.runtime_message && !snapshot.runtime_message) {
                    snapshot.runtime_message = payload.runtime_message;
                }
                await app.taskView.applyTaskSnapshot(snapshot, { preserveMode: true, hydrateConfig: false });
                renderSummary();
                if (!app.runningInviteStatuses.has(snapshot.status)) {
                    stopPolling();
                }
            }
            if (payload.type === 'ping' && state.inviteSocket === socket) {
                socket.send(JSON.stringify({ type: 'pong' }));
            }
        };

        socket.onclose = () => {
            if (state.inviteSocket === socket) {
                state.inviteSocket = null;
                connectedTaskUuid = null;
            }
        };
    }

    async function pollTask() {
        if (!state.currentTaskUuid) {
            return;
        }
        try {
            const task = await app.api.loadTask(state.currentTaskUuid);
            await app.taskView.applyTaskSnapshot(task, { preserveMode: true, hydrateConfig: false });
            renderSummary();
            if (!app.runningInviteStatuses.has(task.status)) {
                stopPolling();
            }
        } catch (error) {
            stopPolling();
        }
    }

    function startPolling() {
        stopPolling();
        if (!state.currentTaskUuid) {
            return;
        }
        state.invitePollTimer = window.setInterval(pollTask, 3000);
    }

    function stopPolling() {
        if (!state.invitePollTimer) {
            return;
        }
        window.clearInterval(state.invitePollTimer);
        state.invitePollTimer = null;
    }

    function renderSummary() {
        const task = state.currentTask;
        if (!task) {
            elements.logSummary.innerHTML = '<span class="invite-pill">未运行任务</span>';
            return;
        }

        const statusMeta = app.getTaskStatusMeta(task.status);
        const stats = task.stats || {};
        elements.logSummary.innerHTML = `
            <span class="invite-pill"><strong>阶段</strong>${app.escapeHtml(statusMeta.text)}</span>
            <span class="invite-pill"><strong>邀请</strong>${stats.invited_members || 0}/${stats.total_members || 0}</span>
            <span class="invite-pill"><strong>Team-ready</strong>${stats.team_ready_members || 0}</span>
            <span class="invite-pill"><strong>上传</strong>${stats.uploaded_members || 0}</span>
            <span class="invite-pill"><strong>失败</strong>${stats.failed_members || 0}</span>
        `;
    }

    function startTask(taskUuid, { reloadLogs = true } = {}) {
        if (!taskUuid) {
            return;
        }
        if (reloadLogs) {
            loadTaskLogs(taskUuid, { reset: true });
        }
        connect(taskUuid);
        startPolling();
        renderSummary();
    }

    function bindEvents() {
        elements.clearLogBtn.addEventListener('click', resetLogs);
    }

    app.logs = {
        init() {
            bindEvents();
            renderSummary();
        },
        startTask,
        stopPolling,
        loadTaskLogs,
        renderSummary,
        resetLogs,
        disconnect,
    };
})();
