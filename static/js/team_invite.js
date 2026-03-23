let currentInviteTaskUuid = null;
let currentInviteTask = null;
let inviteStatusTimer = null;
let inviteSocket = null;
const inviteDisplayedLogs = new Set();
const sub2apiGroupCache = new Map();

const inviteSourceState = {
    accounts: [],
    sourceAccounts: [],
    teamTasks: [],
};

const inviteStatusMeta = {
    pending: { text: '等待中', className: 'pending' },
    verifying: { text: '校验中', className: 'running' },
    inviting: { text: '发送邀请', className: 'running' },
    accepting: { text: '自动接受', className: 'running' },
    uploading: { text: '上传中', className: 'running' },
    completed: { text: '已完成', className: 'completed' },
    failed: { text: '失败', className: 'failed' },
    cancelled: { text: '已取消', className: 'disabled' },
};

const memberStatusMeta = {
    pending: { text: '待开始', className: 'pending' },
    invited: { text: '已邀请', className: 'running' },
    invite_only: { text: '仅发邀请', className: 'pending' },
    accepted: { text: '已接受', className: 'running' },
    uploaded: { text: '已上传', className: 'completed' },
    skipped: { text: '已跳过', className: 'disabled' },
    failed: { text: '失败', className: 'failed' },
    cancelled: { text: '已取消', className: 'disabled' },
};

const memberReasonMeta = {
    already_member: '已在目标 Team 中，系统会刷新 Team 空间后再上传。',
    pending_invite: '存在待接受邀请，必须先接受邀请后才能切换 Team 空间并上传。',
};

const elements = {
    form: document.getElementById('team-invite-form'),
    sourceMode: document.getElementById('source-mode'),
    sourceAccountWrap: document.getElementById('source-account-wrap'),
    sourceTeamTaskWrap: document.getElementById('source-team-task-wrap'),
    sourceAccountId: document.getElementById('source-account-id'),
    sourceTeamTaskUuid: document.getElementById('source-team-task-uuid'),
    currentSourceTeamSummary: document.getElementById('current-source-team-summary'),
    currentSourceTeamMembers: document.getElementById('current-source-team-members'),
    existingAccountIds: document.getElementById('existing-account-ids'),
    existingInviteSelectionHint: document.getElementById('existing-invite-selection-hint'),
    teamSourceTaskUuids: document.getElementById('team-source-task-uuids'),
    teamSourceSelectionHint: document.getElementById('team-source-selection-hint'),
    manualEmails: document.getElementById('manual-emails'),
    proxy: document.getElementById('invite-proxy'),
    selectAllTargetsBtn: document.getElementById('select-all-team-invite-targets-btn'),
    startBtn: document.getElementById('start-team-invite-btn'),
    cancelBtn: document.getElementById('cancel-team-invite-btn'),
    autoUploadSub2api: document.getElementById('invite-auto-upload-sub2api'),
    autoUploadCpa: document.getElementById('invite-auto-upload-cpa'),
    autoUploadTm: document.getElementById('invite-auto-upload-tm'),
    sub2apiWrap: document.getElementById('invite-sub2api-wrap'),
    cpaWrap: document.getElementById('invite-cpa-wrap'),
    tmWrap: document.getElementById('invite-tm-wrap'),
    sub2apiServiceIds: document.getElementById('invite-sub2api-service-ids'),
    cpaServiceIds: document.getElementById('invite-cpa-service-ids'),
    tmServiceIds: document.getElementById('invite-tm-service-ids'),
    sub2apiGroupOverrides: document.getElementById('sub2api-group-overrides'),
    taskId: document.getElementById('invite-task-id'),
    taskStatusBadge: document.getElementById('invite-task-status-badge'),
    sourceAccount: document.getElementById('invite-source-account'),
    teamAccountId: document.getElementById('invite-team-account-id'),
    sourceAccessToken: document.getElementById('invite-source-access-token'),
    sourceSessionToken: document.getElementById('invite-source-session-token'),
    statusNote: document.getElementById('invite-status-note'),
    membersBody: document.getElementById('team-invite-members-body'),
    uploadOutput: document.getElementById('team-invite-upload-output'),
    consoleLog: document.getElementById('team-invite-console-log'),
    clearLogBtn: document.getElementById('clear-team-invite-log-btn'),
};

document.addEventListener('DOMContentLoaded', async () => {
    bindEvents();
    updateSourceMode();
    await Promise.allSettled([
        loadInviteSources(),
        loadUploadServiceOptions('/sub2api-services?enabled=true', elements.sub2apiServiceIds),
        loadUploadServiceOptions('/cpa-services?enabled=true', elements.cpaServiceIds),
        loadUploadServiceOptions('/tm-services?enabled=true', elements.tmServiceIds),
    ]);
    restoreTask();
});

function bindEvents() {
    elements.form.addEventListener('submit', startTeamInviteTask);
    elements.cancelBtn.addEventListener('click', cancelTask);
    elements.clearLogBtn.addEventListener('click', () => {
        inviteDisplayedLogs.clear();
        elements.consoleLog.innerHTML = '';
    });
    elements.sourceMode.addEventListener('change', updateSourceMode);
    elements.sourceAccountId.addEventListener('change', () => syncSourceContext({ autoSelectAccounts: false, resetTeamTasks: true }));
    elements.sourceTeamTaskUuid.addEventListener('change', () => syncSourceContext({ autoSelectAccounts: false, resetTeamTasks: true }));
    elements.selectAllTargetsBtn.addEventListener('click', selectAllInviteTargets);
    elements.teamSourceTaskUuids.addEventListener('change', updateTeamSourceSelectionHint);
    elements.existingAccountIds.addEventListener('change', () => {
        updateExistingInviteSelectionHint(null, getSelectedSourceTeamTask(), false);
    });

    document.querySelectorAll('[data-copy-target]').forEach((button) => {
        button.addEventListener('click', () => {
            const target = document.getElementById(button.dataset.copyTarget);
            copyToClipboard(target ? (target.textContent || '').trim() : '');
        });
    });

    [
        [elements.autoUploadSub2api, elements.sub2apiWrap],
        [elements.autoUploadCpa, elements.cpaWrap],
        [elements.autoUploadTm, elements.tmWrap],
    ].forEach(([checkbox, wrap]) => {
        checkbox.addEventListener('change', () => {
            wrap.classList.toggle('active', checkbox.checked);
        });
    });

    elements.sub2apiServiceIds.addEventListener('change', renderSub2apiGroupOverrides);
}

async function loadInviteSources() {
    try {
        const response = await api.get('/team-invite/sources');
        const teamTasks = Array.isArray(response.team_tasks) ? response.team_tasks : [];
        const sourceAccounts = Array.isArray(response.source_accounts)
            ? response.source_accounts
            : teamTasks
                .filter((task) => task.main_account && task.main_account.id)
                .map((task) => ({
                    id: task.main_account.id,
                    email: task.main_account.email,
                    remark: task.main_account.remark,
                    status: 'active',
                    source: 'team_task',
                    team_task_uuid: task.task_uuid,
                    team_role: 'admin',
                    team_order_index: 0,
                }));

        inviteSourceState.accounts = Array.isArray(response.accounts) ? response.accounts : [];
        inviteSourceState.sourceAccounts = sourceAccounts;
        inviteSourceState.teamTasks = teamTasks;

        renderSourceSelectors();
        syncSourceContext({ autoSelectAccounts: false, resetTeamTasks: true });
    } catch (error) {
        inviteSourceState.accounts = [];
        inviteSourceState.sourceAccounts = [];
        inviteSourceState.teamTasks = [];
        fillSelect(elements.sourceAccountId, [], null, { emptyLabel: '暂无可用 Team 主号' });
        fillSelect(elements.sourceTeamTaskUuid, [], null, { emptyLabel: '暂无 Team 任务' });
        fillSelect(elements.existingAccountIds, [], null, { emptyLabel: '加载账号失败' });
        fillSelect(elements.teamSourceTaskUuids, [], null, { emptyLabel: '加载 Team 任务失败' });
        renderSourceTeamPreview(null);
        updateExistingInviteSelectionHint([], false);
        updateTeamSourceSelectionHint();
        toast.error(error.message || '加载 Team 邀请数据源失败');
    }
}

function renderSourceSelectors() {
    const selectedSourceAccountId = elements.sourceAccountId.value;
    const selectedSourceTaskUuid = elements.sourceTeamTaskUuid.value;

    fillSelect(
        elements.sourceAccountId,
        inviteSourceState.sourceAccounts,
        (account) => ({
            value: String(account.id),
            label: `${account.email}${account.remark ? ` (${account.remark})` : ''}${account.team_task_uuid ? ` | ${account.team_task_uuid.slice(0, 8)}...` : ''}`,
        }),
        {
            emptyLabel: '暂无可用 Team 主号',
            selectedValues: selectedSourceAccountId ? [selectedSourceAccountId] : [],
        },
    );

    fillSelect(
        elements.sourceTeamTaskUuid,
        inviteSourceState.teamTasks,
        (task) => ({
            value: task.task_uuid,
            label: `${task.main_account?.email || '-'} | ${task.workspace_name || 'MyTeam'} | ${task.status}`,
        }),
        {
            emptyLabel: '暂无 Team 任务',
            selectedValues: selectedSourceTaskUuid ? [selectedSourceTaskUuid] : [],
        },
    );
}

function syncSourceContext(options = {}) {
    const normalized = {
        autoSelectAccounts: Boolean(options.autoSelectAccounts),
        resetTeamTasks: Boolean(options.resetTeamTasks),
    };
    renderSourceTeamPreview(getSelectedSourceTeamTask());
    renderInviteTargetSelectors(normalized);
}

function getSelectedSourceAccount() {
    if (elements.sourceMode.value === 'account') {
        return inviteSourceState.sourceAccounts.find((account) => String(account.id) === elements.sourceAccountId.value) || null;
    }

    const task = getSelectedSourceTeamTask();
    if (!task || !task.main_account) {
        return null;
    }
    return {
        id: task.main_account.id,
        email: task.main_account.email,
        remark: task.main_account.remark,
        team_task_uuid: task.task_uuid,
        team_role: 'admin',
        team_order_index: 0,
    };
}

function getSelectedSourceTeamTask() {
    if (elements.sourceMode.value === 'team_task') {
        return inviteSourceState.teamTasks.find((task) => task.task_uuid === elements.sourceTeamTaskUuid.value) || null;
    }

    const sourceAccount = getSelectedSourceAccount();
    if (!sourceAccount) {
        return null;
    }
    if (sourceAccount.team_task_uuid) {
        const matchedByUuid = inviteSourceState.teamTasks.find((task) => task.task_uuid === sourceAccount.team_task_uuid);
        if (matchedByUuid) {
            return matchedByUuid;
        }
    }
    return inviteSourceState.teamTasks.find((task) => String(task.main_account_id || task.main_account?.id || '') === String(sourceAccount.id)) || null;
}

function renderSourceTeamPreview(task) {
    if (!task) {
        const sourceAccount = getSelectedSourceAccount();
        if (sourceAccount) {
            elements.currentSourceTeamSummary.textContent = `已选择主号 ${sourceAccount.email}，但本地还没有找到对应的 Team 任务记录。`;
        } else {
            elements.currentSourceTeamSummary.textContent = '选择主号后会在这里显示当前 Team 摘要和成员。';
        }
        elements.currentSourceTeamMembers.innerHTML = '<div class="empty-state">暂无可预览的 Team 成员。</div>';
        return;
    }

    const proxyText = task.proxy || '将按后端回退策略自动选择';
    elements.currentSourceTeamSummary.innerHTML = `
        <div><strong>${escapeHtml(task.workspace_name || 'MyTeam')}</strong></div>
        <div class="helper-text">任务：${escapeHtml(task.task_uuid)}</div>
        <div class="helper-text">主号：${escapeHtml(task.main_account?.email || '-')}</div>
        <div class="helper-text">Team：${escapeHtml(task.team_account_id || '待运行时发现')}</div>
        <div class="helper-text">域名：${escapeHtml(task.email_domain || '-')}</div>
        <div class="helper-text">代理：${escapeHtml(proxyText)}</div>
        <div class="helper-text">上传前会强制校验成员是否已经切换到这个 Team 空间。</div>
    `;

    const members = Array.isArray(task.members) ? task.members : [];
    if (!members.length) {
        elements.currentSourceTeamMembers.innerHTML = '<div class="empty-state">当前 Team 还没有本地成员记录。</div>';
        return;
    }

    elements.currentSourceTeamMembers.innerHTML = members
        .map((member) => `
            <div class="source-team-member">
                <div>
                    <div>${escapeHtml(member.email)}</div>
                    <div class="source-team-member-meta">${escapeHtml(formatSourceRole(member.team_role, member.team_order_index))}</div>
                </div>
                <span class="status-badge ${Number(member.team_order_index) === 0 ? 'running' : 'pending'}">${Number(member.team_order_index) === 0 ? '主号' : `成员 ${Number(member.team_order_index)}`}</span>
            </div>
        `)
        .join('');
}

function renderInviteTargetSelectors(options = {}) {
    const selectedTask = getSelectedSourceTeamTask();
    const selectedSourceAccount = getSelectedSourceAccount();
    const hasValidSource = Boolean(selectedTask || selectedSourceAccount);
    const preservedAccountIds = options.autoSelectAccounts
        ? null
        : getSelectedIds(elements.existingAccountIds).map(String);
    const preservedTeamTaskUuids = options.selectAllTargets
        ? null
        : options.resetTeamTasks
            ? []
            : getSelectedValues(elements.teamSourceTaskUuids);

    const excludedAccountIds = new Set();
    const excludedEmails = new Set();

    if (selectedSourceAccount) {
        excludedAccountIds.add(String(selectedSourceAccount.id));
        if (selectedSourceAccount.email) {
            excludedEmails.add(String(selectedSourceAccount.email).toLowerCase());
        }
    }

    if (selectedTask && Array.isArray(selectedTask.members)) {
        selectedTask.members.forEach((member) => {
            if (member.id) {
                excludedAccountIds.add(String(member.id));
            }
            if (member.email) {
                excludedEmails.add(String(member.email).toLowerCase());
            }
        });
    }

    const inviteAccounts = hasValidSource
        ? inviteSourceState.accounts.filter((account) => {
        const accountId = String(account.id);
        const email = String(account.email || '').toLowerCase();
        return !excludedAccountIds.has(accountId) && !excludedEmails.has(email);
    })
        : [];

    const teamTaskCandidates = hasValidSource
        ? inviteSourceState.teamTasks.filter((task) => task.task_uuid !== selectedTask?.task_uuid)
        : [];

    const selectedAccountValues = options.selectAllTargets || options.autoSelectAccounts
        ? inviteAccounts.map((account) => String(account.id))
        : preservedAccountIds || [];
    const selectedTeamTaskValues = options.selectAllTargets
        ? teamTaskCandidates.map((task) => task.task_uuid)
        : preservedTeamTaskUuids || [];

    elements.selectAllTargetsBtn.disabled = !hasValidSource;

    fillSelect(
        elements.existingAccountIds,
        inviteAccounts,
        (account) => ({
            value: String(account.id),
            label: `${account.email}${account.remark ? ` (${account.remark})` : ''}${account.team_task_uuid ? ' [Team]' : ''}`,
        }),
        {
            emptyLabel: '暂无可邀请账号',
            selectedValues: selectedAccountValues,
        },
    );

    fillSelect(
        elements.teamSourceTaskUuids,
        teamTaskCandidates,
        (task) => ({
            value: task.task_uuid,
            label: `${task.main_account?.email || '-'} | ${task.workspace_name || 'MyTeam'} | ${task.member_count || 0} 人`,
        }),
        {
            emptyLabel: '暂无 Team 组来源',
            selectedValues: selectedTeamTaskValues,
        },
    );

    updateExistingInviteSelectionHint(inviteAccounts, selectedTask, Boolean(options.autoSelectAccounts || options.selectAllTargets));
    updateTeamSourceSelectionHint();
}

function fillSelect(select, items, mapper, options = {}) {
    if (!select) {
        return;
    }

    const safeItems = Array.isArray(items) ? items : [];
    const selectedValues = new Set((options.selectedValues || []).map((value) => String(value)));
    select.innerHTML = '';

    if (!safeItems.length) {
        const option = document.createElement('option');
        option.value = '';
        option.textContent = options.emptyLabel || '暂无数据';
        option.disabled = true;
        option.selected = true;
        select.appendChild(option);
        return;
    }

    safeItems.forEach((item, index) => {
        const option = document.createElement('option');
        const mapped = mapper(item);
        option.value = String(mapped.value);
        option.textContent = mapped.label;
        if (selectedValues.has(option.value)) {
            option.selected = true;
        }
        if (!select.multiple && !selectedValues.size && index === 0) {
            option.selected = true;
        }
        select.appendChild(option);
    });
}

function clearMultiSelect(select) {
    Array.from(select.options).forEach((option) => {
        if (!option.disabled) {
            option.selected = false;
        }
    });
}

function getDefaultSourceMemberCount(task = null) {
    const resolvedTask = task || getSelectedSourceTeamTask();
    if (!resolvedTask || !Array.isArray(resolvedTask.members)) {
        return 0;
    }
    return resolvedTask.members.filter((member) => Number(member.team_order_index) > 0).length;
}

function updateExistingInviteSelectionHint(inviteAccounts = null, selectedTask = null, autoSelected = false) {
    const visibleAccounts = Array.isArray(inviteAccounts)
        ? inviteAccounts
        : Array.from(elements.existingAccountIds.options).filter((option) => !option.disabled && option.value);
    const selectedCount = getSelectedIds(elements.existingAccountIds).length;
    const defaultMemberCount = getDefaultSourceMemberCount(selectedTask);

    if (!selectedTask) {
        elements.existingInviteSelectionHint.textContent = '先选择主号，系统会默认带入该主号对应 Team 的成员号。';
        return;
    }

    if (!visibleAccounts.length) {
        elements.existingInviteSelectionHint.textContent = defaultMemberCount > 0
            ? `当前主号的 ${defaultMemberCount} 个 Team 成员会默认带入，暂无额外本地账号可补充。`
            : '当前主号还没有可默认带入的 Team 成员，也没有额外本地账号可补充。';
        return;
    }

    if (autoSelected) {
        elements.existingInviteSelectionHint.textContent = `当前 Team 成员会默认带入，另外已勾选 ${selectedCount} 个额外本地账号。`;
        return;
    }

    if (defaultMemberCount > 0) {
        elements.existingInviteSelectionHint.textContent = `默认会邀请当前 Team 的 ${defaultMemberCount} 个成员号；另外已手动补充 ${selectedCount} / ${visibleAccounts.length} 个本地账号。`;
        return;
    }

    elements.existingInviteSelectionHint.textContent = `当前主号还没有默认成员号，已手动补充 ${selectedCount} / ${visibleAccounts.length} 个本地账号。`;
}

function updateTeamSourceSelectionHint() {
    const selectedTaskUuids = new Set(getSelectedValues(elements.teamSourceTaskUuids));
    if (!selectedTaskUuids.size) {
        elements.teamSourceSelectionHint.textContent = '选中 Team 组后，会自动把该组本地成员全部带入邀请对象，重复邮箱会自动去重。';
        return;
    }

    const selectedTasks = inviteSourceState.teamTasks.filter((task) => selectedTaskUuids.has(task.task_uuid));
    const uniqueEmails = new Set();
    let memberCount = 0;
    selectedTasks.forEach((task) => {
        const members = Array.isArray(task.members) ? task.members : [];
        memberCount += members.length;
        members.forEach((member) => {
            if (member.email) {
                uniqueEmails.add(String(member.email).toLowerCase());
            }
        });
    });

    elements.teamSourceSelectionHint.textContent = `已选 ${selectedTasks.length} 个 Team 组，将自动展开 ${memberCount} 个成员，去重后约 ${uniqueEmails.size} 个邮箱。`;
}

function selectAllInviteTargets() {
    if (!getSelectedSourceAccount() && !getSelectedSourceTeamTask()) {
        toast.warning('请先选择主号或源 Team');
        return;
    }

    clearMultiSelect(elements.existingAccountIds);
    clearMultiSelect(elements.teamSourceTaskUuids);
    updateExistingInviteSelectionHint(null, getSelectedSourceTeamTask(), false);
    updateTeamSourceSelectionHint();
    const defaultMemberCount = getDefaultSourceMemberCount(getSelectedSourceTeamTask());
    toast.success(defaultMemberCount > 0 ? `将仅邀请当前 Team 的 ${defaultMemberCount} 个成员号。` : '已重置为仅使用当前主号的默认成员。');
}

async function loadUploadServiceOptions(path, select) {
    try {
        const services = await api.get(path);
        fillSelect(select, services, (service) => ({ value: String(service.id), label: service.name }));
    } catch (error) {
        fillSelect(select, [], null, { emptyLabel: '加载失败' });
    }
}

function updateSourceMode() {
    const useAccount = elements.sourceMode.value === 'account';
    elements.sourceAccountWrap.style.display = useAccount ? 'block' : 'none';
    elements.sourceTeamTaskWrap.style.display = useAccount ? 'none' : 'block';
    syncSourceContext({ autoSelectAccounts: false, resetTeamTasks: true });
}

async function renderSub2apiGroupOverrides() {
    elements.sub2apiGroupOverrides.innerHTML = '';
    const selectedServiceIds = Array.from(elements.sub2apiServiceIds.selectedOptions).map((option) => option.value);
    for (const serviceId of selectedServiceIds) {
        const groups = await loadSub2apiGroups(serviceId);
        const card = document.createElement('div');
        card.className = 'group-card';
        const options = groups.length
            ? groups.map((group) => `
                <label class="checkbox-row">
                    <input type="checkbox" data-service-id="${serviceId}" data-group-id="${group.id}">
                    <span>${escapeHtml(group.name)} (#${group.id})</span>
                </label>
            `).join('')
            : '<div class="empty-state">当前服务没有可选分组。</div>';
        card.innerHTML = `
            <strong>服务 #${serviceId} 分组覆盖</strong>
            <div class="group-options">${options}</div>
        `;
        elements.sub2apiGroupOverrides.appendChild(card);
    }
}

async function loadSub2apiGroups(serviceId) {
    if (sub2apiGroupCache.has(serviceId)) {
        return sub2apiGroupCache.get(serviceId);
    }
    try {
        const groups = await api.post('/sub2api-services/fetch-groups', { service_id: parseInt(serviceId, 10) });
        sub2apiGroupCache.set(serviceId, groups || []);
        return groups || [];
    } catch (error) {
        sub2apiGroupCache.set(serviceId, []);
        return [];
    }
}

async function startTeamInviteTask(event) {
    event.preventDefault();

    if (elements.sourceMode.value === 'account' && !elements.sourceAccountId.value) {
        toast.warning('请选择 Team 主号');
        return;
    }
    if (elements.sourceMode.value === 'team_task' && !elements.sourceTeamTaskUuid.value) {
        toast.warning('请选择源 Team 任务');
        return;
    }

    loading.show(elements.startBtn, '创建中...');
    try {
        const payload = {
            source_mode: elements.sourceMode.value,
            source_account_id: elements.sourceMode.value === 'account' ? parseInt(elements.sourceAccountId.value || '0', 10) || null : null,
            source_team_task_uuid: elements.sourceMode.value === 'team_task' ? elements.sourceTeamTaskUuid.value || null : null,
            existing_account_ids: getSelectedIds(elements.existingAccountIds),
            team_source_task_uuids: getSelectedValues(elements.teamSourceTaskUuids),
            manual_emails: splitManualEmails(elements.manualEmails.value),
            proxy: elements.proxy.value.trim() || null,
            auto_upload_sub2api: elements.autoUploadSub2api.checked,
            sub2api_service_ids: getSelectedIds(elements.sub2apiServiceIds),
            sub2api_group_ids_by_service: getSub2apiGroupOverrides(),
            auto_upload_cpa: elements.autoUploadCpa.checked,
            cpa_service_ids: getSelectedIds(elements.cpaServiceIds),
            auto_upload_tm: elements.autoUploadTm.checked,
            tm_service_ids: getSelectedIds(elements.tmServiceIds),
        };
        const task = await api.post('/team-invite/create', payload);
        attachTask(task);
        toast.success('Team 邀请任务已启动');
    } catch (error) {
        toast.error(error.message || '启动 Team 邀请任务失败');
    } finally {
        loading.hide(elements.startBtn);
    }
}

function getSelectedIds(select) {
    return Array.from(select.selectedOptions)
        .filter((option) => !option.disabled && option.value)
        .map((option) => parseInt(option.value, 10))
        .filter((value) => Number.isFinite(value));
}

function getSelectedValues(select) {
    return Array.from(select.selectedOptions)
        .filter((option) => !option.disabled && option.value)
        .map((option) => option.value);
}

function splitManualEmails(value) {
    return value
        .split(/[\n,;]+/)
        .map((item) => item.trim())
        .filter(Boolean);
}

function getSub2apiGroupOverrides() {
    const map = {};
    elements.sub2apiGroupOverrides.querySelectorAll('input[type="checkbox"]:checked').forEach((input) => {
        const serviceId = input.dataset.serviceId;
        if (!map[serviceId]) {
            map[serviceId] = [];
        }
        map[serviceId].push(parseInt(input.dataset.groupId, 10));
    });
    return map;
}

function attachTask(task) {
    currentInviteTaskUuid = task.task_uuid;
    currentInviteTask = task;
    storage.set('team_invite_active_task_uuid', currentInviteTaskUuid);
    inviteDisplayedLogs.clear();
    elements.consoleLog.innerHTML = '';
    renderTask(task);
    loadLogs(task.task_uuid);
    connectSocket(task.task_uuid);
    startPolling();
}

function restoreTask() {
    const taskUuid = storage.get('team_invite_active_task_uuid');
    if (taskUuid) {
        fetchTask(taskUuid);
    }
}

function startPolling() {
    stopPolling();
    inviteStatusTimer = setInterval(() => {
        if (currentInviteTaskUuid) {
            fetchTask(currentInviteTaskUuid, false);
        }
    }, 3000);
}

function stopPolling() {
    if (inviteStatusTimer) {
        clearInterval(inviteStatusTimer);
        inviteStatusTimer = null;
    }
}

async function fetchTask(taskUuid, reloadLogs = true) {
    try {
        const task = await api.get(`/team-invite/${taskUuid}`);
        currentInviteTaskUuid = task.task_uuid;
        currentInviteTask = task;
        renderTask(task);
        if (reloadLogs) {
            loadLogs(taskUuid);
            connectSocket(taskUuid);
            startPolling();
        }
    } catch (error) {
        toast.error(error.message || '读取 Team 邀请任务失败');
        clearTaskState();
    }
}

async function loadLogs(taskUuid) {
    try {
        const response = await api.get(`/team-invite/${taskUuid}/logs`);
        inviteDisplayedLogs.clear();
        elements.consoleLog.innerHTML = '';
        (response.logs || []).forEach((line) => addLogLine(line));
    } catch (error) {
        // ignore
    }
}

function renderTask(task) {
    const meta = inviteStatusMeta[task.status] || { text: task.status, className: 'pending' };
    elements.taskId.textContent = task.task_uuid || '-';
    elements.taskStatusBadge.textContent = meta.text;
    elements.taskStatusBadge.className = `status-badge ${meta.className}`;
    elements.sourceAccount.textContent = task.source_account?.email || '-';
    elements.teamAccountId.textContent = task.team_account_id || '-';
    elements.sourceAccessToken.textContent = task.source_account?.access_token || '-';
    elements.sourceSessionToken.textContent = task.source_account?.session_token || '-';
    elements.statusNote.textContent = buildInviteStatusNote(task, meta);
    renderMembers(task.members || []);
    elements.uploadOutput.textContent = task.result && Object.keys(task.result).length
        ? JSON.stringify(task.result, null, 2)
        : '暂无上传结果';
    elements.cancelBtn.disabled = !currentInviteTaskUuid || !['pending', 'verifying', 'inviting', 'accepting', 'uploading'].includes(task.status);

    if (['completed', 'failed', 'cancelled'].includes(task.status)) {
        stopPolling();
    }
}

function renderMembers(members) {
    if (!members.length) {
        elements.membersBody.innerHTML = '<tr><td colspan="5" class="empty-state">创建任务后会在这里显示邀请成员。</td></tr>';
        return;
    }

    elements.membersBody.innerHTML = members.map((member, index) => {
        const meta = memberStatusMeta[member.invitation_status] || { text: member.invitation_status, className: 'pending' };
        const note = describeMemberNote(member);
        return `
            <tr>
                <td>${index + 1}</td>
                <td>${escapeHtml(member.email)}</td>
                <td>${escapeHtml(formatSourceType(member.source_type))}</td>
                <td><span class="status-badge ${meta.className}">${escapeHtml(meta.text)}</span></td>
                <td>${escapeHtml(note)}</td>
            </tr>
        `;
    }).join('');
}

function buildInviteStatusNote(task, meta) {
    const parts = [task.runtime_message || `当前阶段：${meta.text}`];
    const members = Array.isArray(task.members) ? task.members : [];
    const pendingInviteCount = members.filter((member) => member?.result?.reason === 'pending_invite').length;
    const alreadyMemberCount = members.filter((member) => member?.result?.reason === 'already_member').length;

    if (task.team_account_id) {
        parts.push(`目标 Team 空间：${task.team_account_id}。上传时会优先使用这个 Team 空间。`);
    }
    if (alreadyMemberCount > 0) {
        parts.push(`已有 ${alreadyMemberCount} 个账号已在 Team 中，系统会在上传前自动刷新 Team token。`);
    }
    if (pendingInviteCount > 0) {
        parts.push(`仍有 ${pendingInviteCount} 个账号处于待接受邀请状态，必须先接受邀请后才能按 Team 上传。`);
    }

    return parts.join(' ');
}

function describeMemberNote(member) {
    if (member.error_message) {
        return member.error_message;
    }
    const reason = member?.result?.reason;
    if (reason && memberReasonMeta[reason]) {
        return memberReasonMeta[reason];
    }
    if (member.invitation_status === 'uploaded') {
        return '已切换到 Team 空间并完成上传。';
    }
    if (member.invitation_status === 'accepted') {
        return '已接受邀请，正在准备按 Team 空间上传。';
    }
    if (member.invitation_status === 'invite_only') {
        return '仅跟踪邀请发送结果，不会自动接受或上传。';
    }
    return '-';
}

function formatSourceType(sourceType) {
    if (sourceType === 'account') return '现有账号';
    if (sourceType === 'team_task') return 'Team 组';
    if (sourceType === 'manual') return '手填邮箱';
    return sourceType || '-';
}

function formatSourceRole(role, orderIndex) {
    if (Number(orderIndex) === 0) {
        return '主号 / admin';
    }
    return role === 'admin' ? '成员 / admin' : '成员 / member';
}

function connectSocket(taskUuid) {
    if (!taskUuid) {
        return;
    }
    if (inviteSocket) {
        inviteSocket.close();
        inviteSocket = null;
    }

    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    inviteSocket = new WebSocket(`${protocol}//${window.location.host}/api/ws/team-invite/${taskUuid}`);
    inviteSocket.onmessage = (event) => {
        const payload = JSON.parse(event.data);
        if (payload.type === 'log' && payload.message) {
            addLogLine(payload.message);
        }
        if (payload.type === 'status' && payload.snapshot) {
            const snapshot = { ...payload.snapshot };
            if (payload.runtime_message && !snapshot.runtime_message) {
                snapshot.runtime_message = payload.runtime_message;
            }
            currentInviteTask = snapshot;
            renderTask(snapshot);
        }
        if (payload.type === 'ping') {
            inviteSocket.send(JSON.stringify({ type: 'pong' }));
        }
    };
}

function addLogLine(message) {
    if (!message || inviteDisplayedLogs.has(message)) {
        return;
    }
    inviteDisplayedLogs.add(message);
    const line = document.createElement('div');
    line.className = 'log-line info';
    line.textContent = message;
    elements.consoleLog.appendChild(line);
    elements.consoleLog.scrollTop = elements.consoleLog.scrollHeight;
}

async function cancelTask() {
    if (!currentInviteTaskUuid) {
        return;
    }
    const confirmed = await window.confirm('确认取消当前 Team 邀请任务吗？', '取消 Team 邀请任务');
    if (!confirmed) {
        return;
    }

    try {
        const result = await api.post(`/team-invite/${currentInviteTaskUuid}/cancel`, {});
        toast.info(result.message || '已提交取消请求');
        fetchTask(currentInviteTaskUuid, false);
    } catch (error) {
        toast.error(error.message || '取消任务失败');
    }
}

function clearTaskState() {
    stopPolling();
    storage.remove('team_invite_active_task_uuid');
    currentInviteTaskUuid = null;
    currentInviteTask = null;
    if (inviteSocket) {
        inviteSocket.close();
        inviteSocket = null;
    }
    elements.cancelBtn.disabled = true;
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text == null ? '' : String(text);
    return div.innerHTML;
}
