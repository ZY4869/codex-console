(function () {
    const app = window.TeamInviteApp = window.TeamInviteApp || {};

    app.state = {
        sources: {
            accounts: [],
            sourceAccounts: [],
            teamTasks: [],
            customSourceServiceTypes: [],
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
        memberDrafts: (window.storage && window.storage.get('team_invite_member_drafts_v1', {})) || {},
    };

    app.taskStorageKey = 'team_invite_active_task_uuid';
    app.memberDraftStorageKey = 'team_invite_member_drafts_v1';
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

    app.emailServiceTypeLabels = {
        moe_mail: 'MoeMail',
        temp_mail: 'Temp-Mail',
        duck_mail: 'DuckMail',
        freemail: 'Freemail',
        outlook: 'Outlook',
        imap_mail: 'IMAP',
        tempmail: 'Tempmail',
    };

    app.elements = {
        sourceMode: document.getElementById('team-source-mode'),
        sourceAccountWrap: document.getElementById('team-source-account-wrap'),
        customSourceWrap: document.getElementById('team-custom-source-wrap'),
        sourceAccountId: document.getElementById('team-source-account-id'),
        customSourceEmail: document.getElementById('team-custom-source-email'),
        customSourceServiceType: document.getElementById('team-custom-source-service-type'),
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
        uploadSourceAccount: document.getElementById('team-upload-source-account'),
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

    app.normalizeEmail = function normalizeEmail(value) {
        return String(value || '').trim().toLowerCase();
    };

    function normalizeMemberDraft(draft) {
        const existingAccountIds = Array.from(new Set(
            (Array.isArray(draft?.existing_account_ids) ? draft.existing_account_ids : [])
                .map((value) => app.parseInteger(value, 0))
                .filter((value) => value > 0)
                .map((value) => String(value))
        ));
        const seenEmails = new Set();
        const manualEmails = [];
        (Array.isArray(draft?.manual_emails) ? draft.manual_emails : []).forEach((value) => {
            const normalizedEmail = app.normalizeEmail(value);
            if (!normalizedEmail || seenEmails.has(normalizedEmail)) {
                return;
            }
            seenEmails.add(normalizedEmail);
            manualEmails.push(normalizedEmail);
        });
        return {
            existing_account_ids: existingAccountIds,
            manual_emails: manualEmails,
        };
    }

    function persistMemberDrafts() {
        if (!window.storage) {
            return false;
        }
        return window.storage.set(app.memberDraftStorageKey, app.state.memberDrafts || {});
    }

    app.formatEmailServiceType = function formatEmailServiceType(value) {
        return app.emailServiceTypeLabels[value] || value || '-';
    };

    app.findSourceTaskByAccountId = function findSourceTaskByAccountId(accountId) {
        return app.state.sources.teamTasks.find((task) => String(task.main_account?.id || task.main_account_id || '') === String(accountId)) || null;
    };

    app.findAccountById = function findAccountById(accountId) {
        return app.state.sources.accounts.find((account) => String(account.id) === String(accountId)) || null;
    };

    app.findAccountByEmail = function findAccountByEmail(email) {
        const normalizedEmail = app.normalizeEmail(email);
        if (!normalizedEmail) {
            return null;
        }
        const seenIds = new Set();
        const merged = [...(app.state.sources.accounts || []), ...(app.state.sources.sourceAccounts || [])];
        for (const account of merged) {
            if (!account || seenIds.has(String(account.id))) {
                continue;
            }
            seenIds.add(String(account.id));
            if (app.normalizeEmail(account.email) === normalizedEmail) {
                return account;
            }
        }
        return null;
    };

    app.findAccountByEmailService = function findAccountByEmailService(email, serviceType) {
        const normalizedEmail = app.normalizeEmail(email);
        const normalizedServiceType = String(serviceType || '').trim().toLowerCase();
        if (!normalizedEmail || !normalizedServiceType) {
            return null;
        }
        return (app.state.sources.accounts || []).find((account) => (
            app.normalizeEmail(account.email) === normalizedEmail
            && String(account.email_service || '').trim().toLowerCase() === normalizedServiceType
        )) || null;
    };

    app.getSelectedSourceMode = function getSelectedSourceMode() {
        return app.elements.sourceMode?.value || 'account';
    };

    app.buildMemberDraftKey = function buildMemberDraftKey(options = {}) {
        const sourceMode = String(options.sourceMode || app.getSelectedSourceMode()).trim();
        if (sourceMode === 'custom_domain_email') {
            const email = app.normalizeEmail(options.customSourceEmail ?? app.elements.customSourceEmail?.value);
            const serviceType = String(
                options.customSourceServiceType ?? app.elements.customSourceServiceType?.value ?? ''
            ).trim().toLowerCase();
            if (!email || !serviceType) {
                return null;
            }
            return `custom:${serviceType}:${email}`;
        }

        const sourceAccountId = app.parseInteger(options.sourceAccountId ?? app.elements.sourceAccountId?.value, 0);
        if (!sourceAccountId) {
            return null;
        }
        return `account:${sourceAccountId}`;
    };

    app.getCurrentMemberDraftKey = function getCurrentMemberDraftKey() {
        return app.buildMemberDraftKey();
    };

    app.getMemberDraft = function getMemberDraft(key) {
        if (!key) {
            return null;
        }
        return normalizeMemberDraft(app.state.memberDrafts?.[key]);
    };

    app.setMemberDraft = function setMemberDraft(key, draft) {
        if (!key) {
            return false;
        }
        const normalized = normalizeMemberDraft(draft);
        if (!normalized.existing_account_ids.length && !normalized.manual_emails.length) {
            delete app.state.memberDrafts[key];
            return persistMemberDrafts();
        }
        app.state.memberDrafts[key] = normalized;
        return persistMemberDrafts();
    };

    app.removeMemberDraft = function removeMemberDraft(key) {
        if (!key) {
            return false;
        }
        delete app.state.memberDrafts[key];
        return persistMemberDrafts();
    };

    app.getCustomSourceSelection = function getCustomSourceSelection() {
        const email = app.normalizeEmail(app.elements.customSourceEmail?.value);
        const serviceType = String(app.elements.customSourceServiceType?.value || '').trim().toLowerCase();
        return {
            email,
            serviceType,
            account: app.findAccountByEmailService(email, serviceType),
            conflictingAccount: app.findAccountByEmail(email),
        };
    };

    app.getSelectedSourceTask = function getSelectedSourceTask() {
        const sourceAccount = app.getSelectedSourceAccount();
        return sourceAccount ? app.findSourceTaskByAccountId(sourceAccount.id) : null;
    };

    app.getSelectedSourceAccount = function getSelectedSourceAccount() {
        if (app.getSelectedSourceMode() === 'custom_domain_email') {
            return app.getCustomSourceSelection().account;
        }
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
