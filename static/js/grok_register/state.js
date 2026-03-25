(function () {
    const app = window.GrokRegisterApp = window.GrokRegisterApp || {};

    app.state = {
        currentTask: null,
        currentTaskUuid: null,
        pollTimer: null,
        socket: null,
        displayedLogs: new Set(),
        historyTasks: [],
        solverStatus: null,
        flaresolverrStatus: null,
        emailServiceCatalog: null,
    };

    app.storageKey = 'grok_register_active_task_uuid';
    app.runningStatuses = new Set(['pending', 'running']);

    app.statusMeta = {
        pending: { text: '等待中', className: 'pending' },
        running: { text: '运行中', className: 'running' },
        completed: { text: '已完成', className: 'completed' },
        failed: { text: '失败', className: 'failed' },
        cancelled: { text: '已取消', className: 'disabled' },
    };

    app.accountStatusMeta = {
        pending: { text: '待开始', className: 'pending' },
        running: { text: '进行中', className: 'running' },
        completed: { text: '成功', className: 'completed' },
        failed: { text: '失败', className: 'failed' },
        cancelled: { text: '取消', className: 'disabled' },
    };

    app.elements = {
        proxy: document.getElementById('grok-proxy'),
        targetCount: document.getElementById('grok-target-count'),
        threadCount: document.getElementById('grok-thread-count'),
        emailDomain: document.getElementById('grok-email-domain'),
        emailServiceType: document.getElementById('grok-email-service-type'),
        emailServiceIdWrap: document.getElementById('grok-email-service-id-wrap'),
        emailServiceId: document.getElementById('grok-email-service-id'),
        emailServiceHint: document.getElementById('grok-email-service-hint'),
        captchaMode: document.getElementById('grok-captcha-mode'),
        bczyApiKey: document.getElementById('grok-bczy-api-key'),
        bczySaved: document.getElementById('grok-bczy-saved'),
        yescaptchaKey: document.getElementById('grok-yescaptcha-key'),
        yescaptchaSaved: document.getElementById('grok-yescaptcha-saved'),
        solverUrlWrap: document.getElementById('grok-solver-url-wrap'),
        solverUrl: document.getElementById('grok-solver-url'),
        solverCommand: document.getElementById('grok-solver-command'),
        flaresolverrUrl: document.getElementById('grok-flaresolverr-url'),
        saveConfigBtn: document.getElementById('grok-save-config-btn'),
        solverStatus: document.getElementById('grok-solver-status'),
        solverStartBtn: document.getElementById('grok-solver-start-btn'),
        solverStopBtn: document.getElementById('grok-solver-stop-btn'),
        flaresolverrStatus: document.getElementById('grok-flaresolverr-status'),
        flaresolverrStartBtn: document.getElementById('grok-flaresolverr-start-btn'),
        flaresolverrStopBtn: document.getElementById('grok-flaresolverr-stop-btn'),
        startBtn: document.getElementById('grok-start-btn'),
        cancelBtn: document.getElementById('grok-cancel-btn'),
        taskUuid: document.getElementById('grok-task-uuid'),
        taskStatus: document.getElementById('grok-task-status'),
        taskSuccess: document.getElementById('grok-task-success'),
        taskFailed: document.getElementById('grok-task-failed'),
        runtimeMessage: document.getElementById('grok-runtime-message'),
        accountsBody: document.getElementById('grok-accounts-body'),
        history: document.getElementById('grok-task-history'),
        refreshHistoryBtn: document.getElementById('grok-refresh-history-btn'),
        clearLogBtn: document.getElementById('grok-clear-log-btn'),
        logSummary: document.getElementById('grok-log-summary'),
        consoleLog: document.getElementById('grok-console-log'),
    };

    app.escapeHtml = function escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text == null ? '' : String(text);
        return div.innerHTML;
    };

    app.getStatusMeta = function getStatusMeta(status) {
        return app.statusMeta[status] || { text: status || '未知', className: 'pending' };
    };

    app.getAccountStatusMeta = function getAccountStatusMeta(status) {
        return app.accountStatusMeta[status] || { text: status || '未知', className: 'pending' };
    };
})();
