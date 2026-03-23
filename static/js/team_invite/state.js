(function () {
    const app = window.TeamInviteApp = window.TeamInviteApp || {};

    app.state = {
        sources: {
            accounts: [],
            sourceAccounts: [],
            teamTasks: [],
        },
        teamEmailServices: [],
        uploadServices: {
            sub2api: [],
            cpa: [],
            tm: [],
        },
        sub2apiGroups: new Map(),
        sub2apiGroupSelections: {},
        currentTask: null,
        currentTaskUuid: null,
        currentTaskMode: 'preview',
        selectedMemberKey: null,
        selectedAccountDetail: null,
        inviteSocket: null,
        invitePollTimer: null,
        displayedLogs: new Set(),
        registrationTaskUuid: null,
        registrationPollTimer: null,
    };

    app.taskStorageKey = 'team_invite_active_task_uuid';
    app.runningInviteStatuses = new Set(['pending', 'verifying', 'inviting', 'accepting', 'uploading']);

    app.statusMeta = {
        pending: { text: '等待中', className: 'pending' },
        verifying: { text: '校验中', className: 'running' },
        inviting: { text: '发送邀请', className: 'running' },
        accepting: { text: '接受邀请', className: 'running' },
        uploading: { text: '上传中', className: 'running' },
        completed: { text: '已完成', className: 'completed' },
        failed: { text: '失败', className: 'failed' },
        cancelled: { text: '已取消', className: 'disabled' },
    };

    app.memberStatusMeta = {
        pending: { text: '待处理', className: 'pending' },
        invited: { text: '已邀请', className: 'running' },
        invite_only: { text: '仅邀请', className: 'pending' },
        accepted: { text: '已接受', className: 'running' },
        uploaded: { text: '已上传', className: 'completed' },
        skipped: { text: '已跳过', className: 'disabled' },
        failed: { text: '失败', className: 'failed' },
        cancelled: { text: '已取消', className: 'disabled' },
    };

    app.memberReasonMeta = {
        already_member: '已在目标 Team 中，可直接刷新 Team 身份。',
        pending_invite: '检测到待处理邀请，需要先接受邀请。',
        manual_refresh: '已按当前配置手动刷新 Team 身份。',
        manual_upload: '已按当前配置触发单成员上传。',
    };

    app.sourceTypeMeta = {
        main_account: '主账号',
        team_task: 'Team 成员',
        account: '自定义账号',
        manual: '手填邮箱',
    };

    app.elements = {
        sourceAccountId: document.getElementById('team-source-account-id'),
        sourceAccountCount: document.getElementById('source-account-count'),
        sourceSummary: document.getElementById('team-source-summary'),
        existingAccountIds: document.getElementById('team-existing-account-ids'),
        customAccountCount: document.getElementById('custom-account-count'),
        capacityWarning: document.getElementById('team-capacity-warning'),
        registerEmailService: document.getElementById('team-register-email-service'),
        registerProxy: document.getElementById('team-register-proxy'),
        registerAccountBtn: document.getElementById('team-register-account-btn'),
        registerStatus: document.getElementById('team-register-status'),
        manualEmails: document.getElementById('team-manual-emails'),
        uploadSub2api: document.getElementById('team-upload-sub2api'),
        uploadSub2apiBody: document.getElementById('team-upload-sub2api-body'),
        sub2apiServiceIds: document.getElementById('team-sub2api-service-ids'),
        sub2apiGroups: document.getElementById('team-sub2api-groups'),
        sub2apiNamePreviews: document.getElementById('team-sub2api-name-previews'),
        uploadCpa: document.getElementById('team-upload-cpa'),
        uploadCpaBody: document.getElementById('team-upload-cpa-body'),
        cpaServiceIds: document.getElementById('team-cpa-service-ids'),
        uploadTm: document.getElementById('team-upload-tm'),
        uploadTmBody: document.getElementById('team-upload-tm-body'),
        tmServiceIds: document.getElementById('team-tm-service-ids'),
        retryLimit: document.getElementById('team-retry-limit'),
        taskProxy: document.getElementById('team-task-proxy'),
        startBtn: document.getElementById('team-start-btn'),
        continueBtn: document.getElementById('team-continue-btn'),
        restartBtn: document.getElementById('team-restart-btn'),
        cancelBtn: document.getElementById('team-cancel-btn'),
        taskUuid: document.getElementById('team-task-uuid'),
        taskStatus: document.getElementById('team-task-status'),
        taskTeamCount: document.getElementById('team-task-team-count'),
        taskCustomCount: document.getElementById('team-task-custom-count'),
        membersBody: document.getElementById('team-members-body'),
        accountDetail: document.getElementById('team-account-detail'),
        uploadResults: document.getElementById('team-upload-results'),
        logSummary: document.getElementById('team-log-summary'),
        consoleLog: document.getElementById('team-console-log'),
        clearLogBtn: document.getElementById('team-clear-log-btn'),
    };

    app.escapeHtml = function escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text == null ? '' : String(text);
        return div.innerHTML;
    };

    app.fillSelect = function fillSelect(select, items, mapper, { emptyLabel = '暂无数据', selectedValues = [] } = {}) {
        if (!select) return;
        const values = new Set((selectedValues || []).map((value) => String(value)));
        select.innerHTML = '';
        if (!Array.isArray(items) || !items.length) {
            const option = document.createElement('option');
            option.value = '';
            option.textContent = emptyLabel;
            option.disabled = true;
            option.selected = true;
            select.appendChild(option);
            return;
        }

        items.forEach((item, index) => {
            const mapped = mapper(item);
            const option = document.createElement('option');
            option.value = String(mapped.value);
            option.textContent = mapped.label;
            if (values.has(option.value) || (!select.multiple && !values.size && index === 0)) {
                option.selected = true;
            }
            select.appendChild(option);
        });
    };

    app.getSelectedValues = function getSelectedValues(select) {
        return Array.from(select?.selectedOptions || [])
            .filter((option) => !option.disabled && option.value)
            .map((option) => option.value);
    };

    app.getSelectedIds = function getSelectedIds(select) {
        return app.getSelectedValues(select)
            .map((value) => parseInt(value, 10))
            .filter((value) => Number.isFinite(value));
    };

    app.parseInteger = function parseInteger(value, fallback = 0) {
        const numeric = parseInt(value, 10);
        return Number.isFinite(numeric) ? numeric : fallback;
    };

    app.findSourceTaskByAccountId = function findSourceTaskByAccountId(accountId) {
        return app.state.sources.teamTasks.find((task) => String(task.main_account?.id || task.main_account_id || '') === String(accountId)) || null;
    };

    app.findAccountById = function findAccountById(accountId) {
        return app.state.sources.accounts.find((account) => String(account.id) === String(accountId)) || null;
    };

    app.getSelectedSourceTask = function getSelectedSourceTask() {
        return app.findSourceTaskByAccountId(app.elements.sourceAccountId.value);
    };

    app.getSelectedSourceAccount = function getSelectedSourceAccount() {
        return app.state.sources.sourceAccounts.find((account) => String(account.id) === String(app.elements.sourceAccountId.value)) || null;
    };

    app.getTaskStatusMeta = function getTaskStatusMeta(status) {
        return app.statusMeta[status] || { text: status || '待处理', className: 'pending' };
    };

    app.getMemberStatusMeta = function getMemberStatusMeta(status) {
        return app.memberStatusMeta[status] || { text: status || '待处理', className: 'pending' };
    };

    app.buildMemberKey = function buildMemberKey(kind, id, email) {
        return `${kind}:${id || email || 'unknown'}`;
    };

    app.splitManualEmails = function splitManualEmails(value) {
        return String(value || '')
            .split(/[\n,;]+/)
            .map((item) => item.trim())
            .filter(Boolean);
    };
})();
