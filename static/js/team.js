let currentTaskUuid = null;
let currentTask = null;
let statusTimer = null;
let teamSocket = null;
const displayedLogs = new Set();

const statusMeta = {
    pending: { text: '等待中', className: 'pending' },
    registering: { text: '注册中', className: 'running' },
    waiting_subscription: { text: '等待订阅', className: 'pending' },
    verifying: { text: '校验订阅', className: 'running' },
    inviting: { text: '发送邀请', className: 'running' },
    accepting: { text: '自动接受', className: 'running' },
    uploading: { text: '上传中', className: 'running' },
    completed: { text: '已完成', className: 'completed' },
    failed: { text: '失败', className: 'failed' },
    cancelled: { text: '已取消', className: 'disabled' },
};

const memberStatusMeta = {
    pending: { text: '待开始', className: 'pending' },
    registered: { text: '已注册', className: 'pending' },
    invited: { text: '已邀请', className: 'running' },
    accepted: { text: '已接受', className: 'running' },
    uploaded: { text: '已上传', className: 'completed' },
    failed: { text: '失败', className: 'failed' },
    cancelled: { text: '已取消', className: 'disabled' },
};

const elements = {
    form: document.getElementById('team-form'),
    emailService: document.getElementById('team-email-service'),
    workspaceName: document.getElementById('workspace-name'),
    proxy: document.getElementById('team-proxy'),
    startBtn: document.getElementById('start-team-btn'),
    cancelBtn: document.getElementById('cancel-team-btn'),
    taskId: document.getElementById('task-id'),
    taskStatusBadge: document.getElementById('task-status-badge'),
    taskEmailDomain: document.getElementById('task-email-domain'),
    taskTeamAccountId: document.getElementById('task-team-account-id'),
    statusNote: document.getElementById('team-status-note'),
    memberProgressText: document.getElementById('member-progress-text'),
    memberProgressStage: document.getElementById('member-progress-stage'),
    memberProgressBar: document.getElementById('member-progress-bar'),
    membersBody: document.getElementById('team-members-body'),
    mainAccountEmpty: document.getElementById('main-account-empty'),
    mainAccountPanel: document.getElementById('main-account-panel'),
    mainAccountEmail: document.getElementById('main-account-email'),
    mainAccountPassword: document.getElementById('main-account-password'),
    mainAccountToken: document.getElementById('main-account-token'),
    mainAccountSession: document.getElementById('main-account-session'),
    paymentLinkOutput: document.getElementById('payment-link-output'),
    generatePaymentLinkBtn: document.getElementById('generate-payment-link-btn'),
    manualUploadTeamBtn: document.getElementById('manual-upload-team-btn'),
    continueTeamBtn: document.getElementById('continue-team-btn'),
    gotoTeamInviteBtn: document.getElementById('goto-team-invite-btn'),
    uploadSummaryOutput: document.getElementById('upload-summary-output'),
    consoleLog: document.getElementById('team-console-log'),
    clearLogBtn: document.getElementById('clear-team-log-btn'),
    autoUploadSub2api: document.getElementById('auto-upload-sub2api'),
    autoUploadCpa: document.getElementById('auto-upload-cpa'),
    autoUploadTm: document.getElementById('auto-upload-tm'),
    sub2apiWrap: document.getElementById('sub2api-select-wrap'),
    cpaWrap: document.getElementById('cpa-select-wrap'),
    tmWrap: document.getElementById('tm-select-wrap'),
    sub2apiSelect: document.getElementById('sub2api-service-ids'),
    cpaSelect: document.getElementById('cpa-service-ids'),
    tmSelect: document.getElementById('tm-service-ids'),
};

document.addEventListener('DOMContentLoaded', async () => {
    bindEvents();
    window.TeamNameBuilder?.init();
    await Promise.all([
        loadEmailServices(),
        loadUploadServiceOptions('/sub2api-services?enabled=true', elements.sub2apiSelect),
        loadUploadServiceOptions('/cpa-services?enabled=true', elements.cpaSelect),
        loadUploadServiceOptions('/tm-services?enabled=true', elements.tmSelect),
    ]);
    restoreTask();
});

function bindEvents() {
    elements.form.addEventListener('submit', startTeamCreation);
    elements.cancelBtn.addEventListener('click', cancelTeamTask);
    elements.generatePaymentLinkBtn.addEventListener('click', generatePaymentLink);
    elements.manualUploadTeamBtn.addEventListener('click', manualUploadTeam);
    elements.continueTeamBtn.addEventListener('click', confirmSubscription);
    elements.gotoTeamInviteBtn.addEventListener('click', goToTeamInviteConsole);
    elements.clearLogBtn.addEventListener('click', () => {
        displayedLogs.clear();
        elements.consoleLog.innerHTML = '';
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

    document.querySelectorAll('[data-copy-target]').forEach((button) => {
        button.addEventListener('click', () => {
            const target = document.getElementById(button.dataset.copyTarget);
            copyToClipboard(target ? (target.textContent || '').trim() : '');
        });
    });
}

async function loadEmailServices() {
    const data = await api.get('/team/available-email-services');
    const services = data.services || [];
    elements.emailService.innerHTML = '';

    if (!services.length) {
        const option = document.createElement('option');
        option.value = '';
        option.textContent = '请先配置 moe_mail / freemail / temp_mail';
        option.disabled = true;
        option.selected = true;
        elements.emailService.appendChild(option);
        elements.startBtn.disabled = true;
        return;
    }

    elements.startBtn.disabled = false;
    services.forEach((service) => {
        const option = document.createElement('option');
        option.value = String(service.id);
        option.textContent = service.domain
            ? `${service.name} (@${service.domain})`
            : `${service.name} (${service.service_type})`;
        elements.emailService.appendChild(option);
    });
}

async function loadUploadServiceOptions(path, select) {
    try {
        const services = await api.get(path);
        select.innerHTML = '';
        services.forEach((service) => {
            const option = document.createElement('option');
            option.value = String(service.id);
            option.textContent = service.name;
            select.appendChild(option);
        });
    } catch (error) {
        select.innerHTML = '';
    }
}

async function startTeamCreation(event) {
    event.preventDefault();
    if (!elements.emailService.value) {
        toast.error('请选择可用的邮箱服务');
        return;
    }

    loading.show(elements.startBtn, '创建中...');
    try {
        const payload = {
            email_service_id: parseInt(elements.emailService.value, 10),
            workspace_name: window.TeamNameBuilder?.getName() || elements.workspaceName.value.trim() || 'MyTeam',
            workspace_name_parts: window.TeamNameBuilder?.serializeParts() || [],
            proxy: elements.proxy.value.trim() || null,
            auto_upload_sub2api: elements.autoUploadSub2api.checked,
            sub2api_service_ids: getSelectedServiceIds(elements.sub2apiSelect),
            auto_upload_cpa: elements.autoUploadCpa.checked,
            cpa_service_ids: getSelectedServiceIds(elements.cpaSelect),
            auto_upload_tm: elements.autoUploadTm.checked,
            tm_service_ids: getSelectedServiceIds(elements.tmSelect),
        };
        const task = await api.post('/team/create', payload);
        attachTask(task);
        toast.success('Team 任务已启动');
    } catch (error) {
        toast.error(error.message || '启动 Team 任务失败');
    } finally {
        loading.hide(elements.startBtn);
    }
}

function getSelectedServiceIds(select) {
    return Array.from(select.selectedOptions).map((option) => parseInt(option.value, 10));
}

function buildTeamUploadSelectionPayload() {
    return {
        auto_upload_sub2api: elements.autoUploadSub2api.checked,
        sub2api_service_ids: getSelectedServiceIds(elements.sub2apiSelect),
        auto_upload_cpa: elements.autoUploadCpa.checked,
        cpa_service_ids: getSelectedServiceIds(elements.cpaSelect),
        auto_upload_tm: elements.autoUploadTm.checked,
        tm_service_ids: getSelectedServiceIds(elements.tmSelect),
    };
}

function buildTeamInviteHandoffPayload() {
    return {
        source_mode: 'team_task',
        source_team_task_uuid: currentTaskUuid,
        existing_account_ids: [],
        team_source_task_uuids: [],
        manual_emails: [],
        proxy: currentTask?.proxy || elements.proxy.value.trim() || null,
        ...buildTeamUploadSelectionPayload(),
        sub2api_group_ids_by_service: {},
        retry_limit: 0,
    };
}

function applyUploadConfig(config = {}) {
    const definitions = [
        ['auto_upload_sub2api', elements.autoUploadSub2api, elements.sub2apiWrap, elements.sub2apiSelect],
        ['auto_upload_cpa', elements.autoUploadCpa, elements.cpaWrap, elements.cpaSelect],
        ['auto_upload_tm', elements.autoUploadTm, elements.tmWrap, elements.tmSelect],
    ];
    const selectedMap = {
        auto_upload_sub2api: new Set((config.sub2api_service_ids || []).map((value) => String(value))),
        auto_upload_cpa: new Set((config.cpa_service_ids || []).map((value) => String(value))),
        auto_upload_tm: new Set((config.tm_service_ids || []).map((value) => String(value))),
    };

    definitions.forEach(([flag, checkbox, wrap, select]) => {
        const enabled = Boolean(config[flag]);
        checkbox.checked = enabled;
        wrap.classList.toggle('active', enabled);
        Array.from(select.options).forEach((option) => {
            option.selected = selectedMap[flag].has(option.value);
        });
    });
}

function attachTask(task) {
    currentTaskUuid = task.task_uuid;
    currentTask = task;
    storage.set('team_active_task_uuid', currentTaskUuid);
    displayedLogs.clear();
    elements.consoleLog.innerHTML = '';
    renderTask(task);
    loadLogs(task.task_uuid);
    connectTeamSocket(task.task_uuid);
    startStatusPolling();
}

function restoreTask() {
    const savedTaskUuid = storage.get('team_active_task_uuid');
    if (savedTaskUuid) {
        fetchTask(savedTaskUuid);
    }
}

function startStatusPolling() {
    stopStatusPolling();
    statusTimer = setInterval(() => {
        if (currentTaskUuid) {
            fetchTask(currentTaskUuid, false);
        }
    }, 3000);
}

function stopStatusPolling() {
    if (statusTimer) {
        clearInterval(statusTimer);
        statusTimer = null;
    }
}

async function fetchTask(taskUuid, reloadLogs = true) {
    try {
        const task = await api.get(`/team/${taskUuid}`);
        currentTaskUuid = task.task_uuid;
        currentTask = task;
        renderTask(task);
        if (reloadLogs) {
            loadLogs(taskUuid);
            connectTeamSocket(taskUuid);
            startStatusPolling();
        }
    } catch (error) {
        toast.error(error.message || '读取 Team 任务失败');
        clearTaskState();
    }
}

async function loadLogs(taskUuid) {
    try {
        const data = await api.get(`/team/${taskUuid}/logs`);
        displayedLogs.clear();
        elements.consoleLog.innerHTML = '';
        (data.logs || []).forEach((line) => addLogLine(line));
    } catch (error) {
        // ignore
    }
}

function renderTask(task) {
    const meta = statusMeta[task.status] || { text: task.status, className: 'pending' };
    elements.taskId.textContent = task.task_uuid || '-';
    elements.taskStatusBadge.textContent = meta.text;
    elements.taskStatusBadge.className = `status-badge ${meta.className}`;
    elements.taskEmailDomain.textContent = task.email_domain || '-';
    elements.taskTeamAccountId.textContent = task.team_account_id || '-';
    elements.memberProgressStage.textContent = meta.text;
    elements.statusNote.textContent = buildStatusNote(task, meta.text);
    elements.workspaceName.value = task.workspace_name || 'MyTeam';
    window.TeamNameBuilder?.setFromWorkspaceName(task.workspace_name || 'MyTeam');
    applyUploadConfig(task.upload_config || {});

    renderMembers(task.members || [], task.stats || {});
    renderMainAccount(task);
    renderUploadSummary(task.result || {});
    updateActionButtons(task.status);

    if (['completed', 'failed', 'cancelled'].includes(task.status)) {
        stopStatusPolling();
    }
}

function buildStatusNote(task, statusText) {
    const parts = [];
    if (task.runtime_message) {
        parts.push(task.runtime_message);
    } else {
        parts.push(`当前阶段：${statusText}`);
    }
    if (task.retrying && task.next_retry_in_seconds) {
        parts.push(`将在 ${task.next_retry_in_seconds} 秒后自动重试`);
    }
    if (task.continue_requested && ['pending', 'registering', 'waiting_subscription'].includes(task.status)) {
        parts.push('已记录继续请求，条件满足后会自动进入第二阶段');
    }
    return parts.join(' · ');
}

function renderMembers(members, stats) {
    if (!members.length) {
        elements.membersBody.innerHTML = '<tr><td colspan="5" class="empty-state">任务创建后会在这里显示 5 个成员。</td></tr>';
        elements.memberProgressText.textContent = '0 / 5';
        elements.memberProgressBar.style.width = '0%';
        return;
    }

    const completed = stats.registered_members || 0;
    const percent = Math.round((completed / Math.max(members.length, 1)) * 100);
    elements.memberProgressText.textContent = `${completed} / ${members.length}`;
    elements.memberProgressBar.style.width = `${percent}%`;

    elements.membersBody.innerHTML = members.map((member) => {
        const status = memberStatusMeta[member.invitation_status] || { text: member.invitation_status, className: 'pending' };
        const account = member.account || {};
        return `
            <tr>
                <td>${member.order_index + 1}</td>
                <td>${escapeHtml(member.role === 'admin' ? '主账号' : '成员')}</td>
                <td>${escapeHtml(account.email || '-')}</td>
                <td><span class="status-badge ${status.className}">${escapeHtml(status.text)}</span></td>
                <td>${escapeHtml(account.workspace_id || '-')}</td>
            </tr>
        `;
    }).join('');
}

function renderMainAccount(task) {
    const account = task.main_account;
    const hasAccount = Boolean(account && account.id);
    elements.mainAccountEmpty.style.display = hasAccount ? 'none' : 'block';
    elements.mainAccountPanel.style.display = hasAccount ? 'block' : 'none';
    if (!hasAccount) {
        return;
    }

    elements.mainAccountEmail.textContent = account.email || '-';
    elements.mainAccountPassword.textContent = account.password || '-';
    elements.mainAccountToken.textContent = account.access_token || '-';
    elements.mainAccountSession.textContent = account.session_token || '-';
    if (!elements.paymentLinkOutput.textContent.trim()) {
        elements.paymentLinkOutput.textContent = '-';
    }
}

function renderUploadSummary(result) {
    if (!result || Object.keys(result).length === 0) {
        elements.uploadSummaryOutput.textContent = '暂无上传结果';
        return;
    }
    elements.uploadSummaryOutput.textContent = JSON.stringify(result, null, 2);
}

function updateActionButtons(status) {
    elements.cancelBtn.disabled = !currentTaskUuid || !['registering', 'waiting_subscription', 'verifying', 'inviting', 'accepting', 'uploading'].includes(status);
    elements.generatePaymentLinkBtn.disabled = !(currentTask && currentTask.main_account && currentTask.main_account.id);
    elements.manualUploadTeamBtn.disabled = !currentTaskUuid;
    elements.continueTeamBtn.disabled = !currentTaskUuid;
    elements.gotoTeamInviteBtn.disabled = !currentTaskUuid;
}

function connectTeamSocket(taskUuid) {
    if (!taskUuid) {
        return;
    }
    if (teamSocket) {
        teamSocket.close();
        teamSocket = null;
    }

    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    teamSocket = new WebSocket(`${protocol}//${window.location.host}/api/ws/team/${taskUuid}`);
    teamSocket.onmessage = (event) => {
        const payload = JSON.parse(event.data);
        if (payload.type === 'log' && payload.message) {
            addLogLine(payload.message);
        }
        if (payload.type === 'status' && payload.snapshot) {
            const snapshot = { ...payload.snapshot };
            if (payload.runtime_message && !snapshot.runtime_message) {
                snapshot.runtime_message = payload.runtime_message;
            }
            currentTask = snapshot;
            renderTask(snapshot);
        }
        if (payload.type === 'ping') {
            teamSocket.send(JSON.stringify({ type: 'pong' }));
        }
    };
}

function addLogLine(message) {
    if (!message || displayedLogs.has(message)) {
        return;
    }
    displayedLogs.add(message);
    const line = document.createElement('div');
    line.className = 'log-line info';
    line.textContent = message;
    elements.consoleLog.appendChild(line);
    elements.consoleLog.scrollTop = elements.consoleLog.scrollHeight;
}

async function generatePaymentLink() {
    if (!currentTask || !currentTask.main_account) {
        toast.error('主账号尚未准备好');
        return;
    }

    loading.show(elements.generatePaymentLinkBtn, '生成中...');
    try {
        const response = await api.post('/payment/generate-link', {
            account_id: currentTask.main_account.id,
            plan_type: 'team',
            workspace_name: currentTask.workspace_name,
            proxy: currentTask.proxy,
            auto_open: false,
        });
        elements.paymentLinkOutput.textContent = response.link || '-';
        if (response.link) {
            copyToClipboard(response.link);
        }
        toast.success('支付链接已生成');
    } catch (error) {
        toast.error(error.message || '生成支付链接失败');
    } finally {
        loading.hide(elements.generatePaymentLinkBtn);
    }
}

async function confirmSubscription() {
    if (!currentTaskUuid) {
        return;
    }
    loading.show(elements.continueTeamBtn, '提交中...');
    try {
        const response = await api.post(`/team/${currentTaskUuid}/confirm-subscription`, {});
        toast.success(response.message || '继续请求已提交');
        fetchTask(currentTaskUuid, false);
    } catch (error) {
        toast.error(error.message || '提交继续请求失败');
    } finally {
        loading.hide(elements.continueTeamBtn);
    }
}

async function manualUploadTeam() {
    if (!currentTaskUuid) {
        return;
    }

    const payload = buildTeamUploadSelectionPayload();

    if (!payload.auto_upload_sub2api && !payload.auto_upload_cpa && !payload.auto_upload_tm) {
        toast.error('请先至少选择一个上传平台');
        return;
    }

    loading.show(elements.manualUploadTeamBtn, '上传中...');
    try {
        const response = await api.post(`/team/${currentTaskUuid}/upload`, payload);
        toast.success(response.message || '已开始上传到平台');
        fetchTask(currentTaskUuid);
    } catch (error) {
        toast.error(error.message || '手动上传失败');
    } finally {
        loading.hide(elements.manualUploadTeamBtn);
    }
}

async function goToTeamInviteConsole() {
    if (!currentTaskUuid) {
        return;
    }

    loading.show(elements.gotoTeamInviteBtn, '创建中...');
    try {
        const task = await api.post('/team-invite/create', buildTeamInviteHandoffPayload());
        storage.set('team_invite_active_task_uuid', task.task_uuid);
        window.location.href = '/team/invite';
    } catch (error) {
        toast.error(error.message || '创建 Team 邀请任务失败');
    } finally {
        loading.hide(elements.gotoTeamInviteBtn);
    }
}

async function cancelTeamTask() {
    if (!currentTaskUuid) {
        return;
    }
    const confirmed = await window.confirm('确认取消当前 Team 任务吗？', '取消 Team 任务');
    if (!confirmed) {
        return;
    }

    try {
        const result = await api.post(`/team/${currentTaskUuid}/cancel`, {});
        toast.info(result.message || '已提交取消请求');
        fetchTask(currentTaskUuid, false);
    } catch (error) {
        toast.error(error.message || '取消任务失败');
    }
}

function clearTaskState() {
    stopStatusPolling();
    storage.remove('team_active_task_uuid');
    currentTaskUuid = null;
    currentTask = null;
    if (teamSocket) {
        teamSocket.close();
        teamSocket = null;
    }
    updateActionButtons('');
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text == null ? '' : String(text);
    return div.innerHTML;
}
