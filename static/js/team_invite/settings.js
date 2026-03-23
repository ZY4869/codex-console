(function () {
    const app = window.TeamInviteApp = window.TeamInviteApp || {};
    const { state, elements } = app;

    let previewRequestSeq = 0;

    function setButtonBusy(button, busy, busyText) {
        if (!button) return;
        if (busy) {
            if (!button.dataset.originalText) {
                button.dataset.originalText = button.textContent;
            }
            button.textContent = busyText || button.textContent;
            button.disabled = true;
            return;
        }
        button.textContent = button.dataset.originalText || button.textContent;
        delete button.dataset.originalText;
        button.disabled = false;
    }

    function getSourceTaskMembers(task) {
        const mainAccountId = String(task?.main_account?.id || '');
        return (task?.members || []).filter((member) => String(member.id || '') !== mainAccountId);
    }

    function getSelectedManualEmails() {
        return app.splitManualEmails(elements.manualEmails.value);
    }

    function getSelectedCustomAccounts() {
        const selectedIds = new Set(app.getSelectedValues(elements.existingAccountIds));
        return state.sources.accounts.filter((account) => selectedIds.has(String(account.id)));
    }

    function getSelectedSub2ApiGroups() {
        const selectedServiceIds = new Set(app.getSelectedValues(elements.sub2apiServiceIds));
        const groupSelections = {};
        Object.entries(state.sub2apiGroupSelections || {}).forEach(([serviceId, groupIds]) => {
            if (!selectedServiceIds.has(String(serviceId))) {
                return;
            }
            const normalized = Array.from(new Set((groupIds || []).map((value) => app.parseInteger(value)).filter(Boolean)));
            if (normalized.length) {
                groupSelections[String(serviceId)] = normalized;
            }
        });
        return groupSelections;
    }

    function buildSelectedPlatforms() {
        const sub2apiGroups = getSelectedSub2ApiGroups();
        return [
            {
                key: 'sub2api',
                label: 'Sub2API',
                enabled: elements.uploadSub2api.checked,
                service_ids: app.getSelectedIds(elements.sub2apiServiceIds),
                group_ids_by_service: sub2apiGroups,
            },
            {
                key: 'cpa',
                label: 'CPA',
                enabled: elements.uploadCpa.checked,
                service_ids: app.getSelectedIds(elements.cpaServiceIds),
            },
            {
                key: 'tm',
                label: 'Team Manager',
                enabled: elements.uploadTm.checked,
                service_ids: app.getSelectedIds(elements.tmServiceIds),
            },
        ];
    }

    function formatServiceLabel(service) {
        if (!service) return '未命名服务';
        const suffix = service.domain ? ` (@${service.domain})` : '';
        return `${service.name}${suffix}`;
    }

    function renderSourceSummary() {
        const sourceAccount = app.getSelectedSourceAccount();
        const sourceTask = app.getSelectedSourceTask();
        if (!sourceAccount) {
            elements.sourceSummary.innerHTML = `
                <h5>当前 Team</h5>
                <p>选择主账号后会在这里展示对应 Team 摘要。</p>
            `;
            return;
        }

        if (!sourceTask) {
            elements.sourceSummary.innerHTML = `
                <h5>${app.escapeHtml(sourceAccount.email)}</h5>
                <p>当前主账号已选择，但本地暂未找到对应 Team 任务记录。仍然可以继续补充自定义账号与手填邮箱。</p>
            `;
            return;
        }

        const teamMembers = getSourceTaskMembers(sourceTask);
        elements.sourceSummary.innerHTML = `
            <h5>${app.escapeHtml(sourceTask.workspace_name || 'MyTeam')}</h5>
            <p>
                主账号：${app.escapeHtml(sourceTask.main_account?.email || sourceAccount.email)}<br>
                Team：${app.escapeHtml(sourceTask.team_account_id || '运行时自动发现')}<br>
                代理：${app.escapeHtml(sourceTask.proxy || '留空自动回退')}<br>
                当前团队：${sourceTask.member_count || 0} 人，其中默认邀请 ${teamMembers.length} 人
            </p>
        `;
    }

    function renderSourceAccounts(selectedSourceAccountId) {
        elements.sourceAccountCount.textContent = `${state.sources.sourceAccounts.length} 个可用主号`;
        app.fillSelect(
            elements.sourceAccountId,
            state.sources.sourceAccounts,
            (account) => {
                const task = app.findSourceTaskByAccountId(account.id);
                const workspace = task?.workspace_name ? ` | ${task.workspace_name}` : '';
                return {
                    value: String(account.id),
                    label: `${account.email}${account.remark ? ` (${account.remark})` : ''}${workspace}`,
                };
            },
            {
                emptyLabel: '暂无可用 Team 主账号',
                selectedValues: selectedSourceAccountId ? [selectedSourceAccountId] : [],
            },
        );
    }

    function renderExistingAccounts(selectedAccountIds) {
        const selectedSourceAccount = app.getSelectedSourceAccount();
        const sourceTask = app.getSelectedSourceTask();
        const excludedIds = new Set();
        const excludedEmails = new Set();

        if (selectedSourceAccount) {
            excludedIds.add(String(selectedSourceAccount.id));
            excludedEmails.add(String(selectedSourceAccount.email || '').toLowerCase());
        }

        (sourceTask?.members || []).forEach((member) => {
            excludedIds.add(String(member.id));
            excludedEmails.add(String(member.email || '').toLowerCase());
        });

        const availableAccounts = state.sources.accounts.filter((account) => {
            const email = String(account.email || '').toLowerCase();
            return !excludedIds.has(String(account.id)) && !excludedEmails.has(email);
        });

        app.fillSelect(
            elements.existingAccountIds,
            availableAccounts,
            (account) => ({
                value: String(account.id),
                label: `${account.email}${account.remark ? ` (${account.remark})` : ''}`,
            }),
            {
                emptyLabel: selectedSourceAccount ? '暂无可补位账号' : '先选择主账号',
                selectedValues: selectedAccountIds || [],
            },
        );
    }

    function renderUploadServiceSelect(select, services, selectedIds, emptyLabel) {
        app.fillSelect(
            select,
            services,
            (service) => ({ value: String(service.id), label: service.name }),
            {
                emptyLabel,
                selectedValues: selectedIds || [],
            },
        );
    }

    function renderRegisterEmailServices(selectedServiceId) {
        const services = state.teamEmailServices || [];
        app.fillSelect(
            elements.registerEmailService,
            services,
            (service) => ({
                value: String(service.id),
                label: formatServiceLabel(service),
            }),
            {
                emptyLabel: '暂无可用邮箱服务',
                selectedValues: selectedServiceId ? [selectedServiceId] : [],
            },
        );
        elements.registerAccountBtn.disabled = !services.length;
    }

    async function renderSub2ApiGroups() {
        const requestSeq = ++previewRequestSeq;
        const selectedServiceIds = app.getSelectedIds(elements.sub2apiServiceIds);
        const selectedSet = new Set(selectedServiceIds.map((value) => String(value)));
        const serviceLookup = new Map(state.uploadServices.sub2api.map((service) => [String(service.id), service]));

        elements.sub2apiGroups.innerHTML = '';
        if (!elements.uploadSub2api.checked) {
            elements.sub2apiNamePreviews.innerHTML = '';
            return;
        }
        if (!selectedServiceIds.length) {
            elements.sub2apiGroups.innerHTML = '<div class="empty-panel">先选择一个或多个 Sub2API 服务，再配置分组。</div>';
            elements.sub2apiNamePreviews.innerHTML = '<div class="empty-panel">已启用 Sub2API，但尚未选择服务。</div>';
            return;
        }

        await Promise.all(selectedServiceIds.map(async (serviceId) => {
            const cacheKey = String(serviceId);
            if (!state.sub2apiGroups.has(cacheKey)) {
                const groups = await app.api.loadSub2ApiGroups(serviceId).catch(() => []);
                state.sub2apiGroups.set(cacheKey, groups || []);
            }
        }));

        if (requestSeq !== previewRequestSeq) {
            return;
        }

        selectedServiceIds.forEach((serviceId) => {
            const cacheKey = String(serviceId);
            const groups = state.sub2apiGroups.get(cacheKey) || [];
            const service = serviceLookup.get(cacheKey);
            const selectedGroupIds = new Set((state.sub2apiGroupSelections[cacheKey] || []).map((value) => String(value)));

            if (!selectedSet.has(cacheKey)) {
                delete state.sub2apiGroupSelections[cacheKey];
                return;
            }

            const wrapper = document.createElement('div');
            wrapper.className = 'group-card';
            if (!groups.length) {
                wrapper.innerHTML = `
                    <strong>${app.escapeHtml(service?.name || `服务 #${serviceId}`)}</strong>
                    <div class="empty-panel">当前服务没有可用分组，稍后可以直接按服务上传。</div>
                `;
                elements.sub2apiGroups.appendChild(wrapper);
                return;
            }

            wrapper.innerHTML = `
                <strong>${app.escapeHtml(service?.name || `服务 #${serviceId}`)}</strong>
                <div class="group-options">
                    ${groups.map((group) => `
                        <label class="checkbox-row">
                            <input
                                type="checkbox"
                                data-sub2api-group
                                data-service-id="${serviceId}"
                                value="${group.id}"
                                ${selectedGroupIds.has(String(group.id)) ? 'checked' : ''}
                            >
                            <span>${app.escapeHtml(group.name || `分组 ${group.id}`)} (#${group.id})</span>
                        </label>
                    `).join('')}
                </div>
            `;
            elements.sub2apiGroups.appendChild(wrapper);
        });

        await renderSub2ApiNamePreviews();
    }

    function formatSub2ApiPreviewMeta(payload) {
        const matched = Array.isArray(payload?.matched_identities) ? payload.matched_identities : [];
        if (matched.length === 1) {
            return `单身份分组：${matched[0]}`;
        }
        if (matched.length > 1) {
            return `多身份分组：${matched.join(' / ')}，将按账号真实身份命名`;
        }
        return '未识别分组身份，将按账号真实身份命名';
    }

    function formatSub2ApiPreviewNames(payload) {
        const previewNames = Array.isArray(payload?.preview_names) && payload.preview_names.length
            ? payload.preview_names
            : [{
                identity: 'Free',
                next_index: payload?.next_index || 1,
                preview_name: payload?.preview_name || 'GPT-Free-000000001',
            }];
        return previewNames.map((item) => item.preview_name).join(' / ');
    }

    async function renderSub2ApiNamePreviews() {
        const requestSeq = ++previewRequestSeq;
        const selectedPlatforms = buildSelectedPlatforms();
        const sub2api = selectedPlatforms.find((item) => item.key === 'sub2api');
        if (!sub2api?.enabled) {
            elements.sub2apiNamePreviews.innerHTML = '';
            return;
        }

        const services = new Map(state.uploadServices.sub2api.map((service) => [String(service.id), service]));
        const jobs = [];
        Object.entries(sub2api.group_ids_by_service || {}).forEach(([serviceId, groupIds]) => {
            (groupIds || []).forEach((groupId) => {
                jobs.push(
                    app.api.previewSub2ApiName(serviceId, groupId)
                        .then((payload) => ({
                            ok: true,
                            serviceId: String(serviceId),
                            groupId,
                            payload,
                        }))
                        .catch((error) => ({
                            ok: false,
                            serviceId: String(serviceId),
                            groupId,
                            error,
                        })),
                );
            });
        });

        if (!jobs.length) {
            elements.sub2apiNamePreviews.innerHTML = '<div class="empty-panel">已选择 Sub2API 服务，勾选分组后会在这里预览 GPT-Identity-序号 的真实命名规则。</div>';
            return;
        }

        const results = await Promise.all(jobs);
        if (requestSeq !== previewRequestSeq) {
            return;
        }

        elements.sub2apiNamePreviews.innerHTML = results.map((item) => {
            const service = services.get(item.serviceId);
            if (!item.ok) {
                return `
                    <div class="preview-item">
                        <span>${app.escapeHtml(service?.name || `服务 #${item.serviceId}`)} / 分组 #${item.groupId}</span>
                        <code>预览失败</code>
                    </div>
                `;
            }
            return `
                <div class="preview-item">
                    <span>${app.escapeHtml(service?.name || `服务 #${item.serviceId}`)} / ${app.escapeHtml(item.payload.group_name || `分组 #${item.groupId}`)}</span>
                    <small class="hint-line">${app.escapeHtml(formatSub2ApiPreviewMeta(item.payload))}</small>
                    <code>${app.escapeHtml(formatSub2ApiPreviewNames(item.payload))}</code>
                </div>
            `;
        }).join('');
    }

    function updateCustomSummary() {
        const customAccounts = getSelectedCustomAccounts();
        const manualEmails = getSelectedManualEmails();
        elements.customAccountCount.textContent = `${customAccounts.length + manualEmails.length} 个补充账号`;
    }

    function updateCapacityWarning() {
        const sourceTask = app.getSelectedSourceTask();
        const baseCount = sourceTask?.member_count || (app.getSelectedSourceAccount() ? 1 : 0);
        const manualEmails = getSelectedManualEmails();
        const selectedCustomCount = getSelectedCustomAccounts().length;
        const totalCount = baseCount + selectedCustomCount + manualEmails.length;

        if (!baseCount) {
            elements.capacityWarning.classList.remove('show');
            elements.capacityWarning.textContent = '';
            return;
        }

        if (totalCount > 5) {
            elements.capacityWarning.textContent = `当前 Team 预计总人数 ${totalCount} 人，已超过 5 人。本次只提示，不阻断开始。`;
            elements.capacityWarning.classList.add('show');
            return;
        }

        elements.capacityWarning.classList.remove('show');
        elements.capacityWarning.textContent = '';
    }

    function notifyPreviewChanged(forcePreview) {
        updateCustomSummary();
        updateCapacityWarning();
        if (!app.taskView) {
            return;
        }
        if (forcePreview) {
            state.currentTaskMode = 'preview';
        }
        app.taskView.render();
    }

    async function loadSources(options = {}) {
        const selectedSourceAccountId = options.selectedSourceAccountId || elements.sourceAccountId.value;
        const selectedAccountIds = options.selectedAccountIds || app.getSelectedValues(elements.existingAccountIds);

        const response = await app.api.loadSources();
        state.sources.accounts = Array.isArray(response.accounts) ? response.accounts : [];
        state.sources.sourceAccounts = Array.isArray(response.source_accounts) ? response.source_accounts : [];
        state.sources.teamTasks = Array.isArray(response.team_tasks) ? response.team_tasks : [];

        renderSourceAccounts(selectedSourceAccountId);
        renderSourceSummary();
        renderExistingAccounts(selectedAccountIds);
        updateCustomSummary();
        updateCapacityWarning();
    }

    async function loadUploadServices() {
        const [sub2apiServices, cpaServices, tmServices, emailServiceResponse] = await app.api.loadUploadServices();
        state.uploadServices.sub2api = Array.isArray(sub2apiServices) ? sub2apiServices : [];
        state.uploadServices.cpa = Array.isArray(cpaServices) ? cpaServices : [];
        state.uploadServices.tm = Array.isArray(tmServices) ? tmServices : [];
        state.teamEmailServices = Array.isArray(emailServiceResponse?.services) ? emailServiceResponse.services : [];

        renderUploadServiceSelect(elements.sub2apiServiceIds, state.uploadServices.sub2api, app.getSelectedValues(elements.sub2apiServiceIds), '暂无可用服务');
        renderUploadServiceSelect(elements.cpaServiceIds, state.uploadServices.cpa, app.getSelectedValues(elements.cpaServiceIds), '暂无可用服务');
        renderUploadServiceSelect(elements.tmServiceIds, state.uploadServices.tm, app.getSelectedValues(elements.tmServiceIds), '暂无可用服务');
        renderRegisterEmailServices(elements.registerEmailService.value);
        await renderSub2ApiGroups();
    }

    function syncPlatformBodies() {
        elements.uploadSub2apiBody.classList.toggle('active', elements.uploadSub2api.checked);
        elements.uploadCpaBody.classList.toggle('active', elements.uploadCpa.checked);
        elements.uploadTmBody.classList.toggle('active', elements.uploadTm.checked);
    }

    function selectNewlyRegisteredAccount(email) {
        if (!email) return;
        const matched = state.sources.accounts.find((account) => String(account.email || '').toLowerCase() === String(email).toLowerCase());
        if (!matched) return;
        const selectedValues = new Set(app.getSelectedValues(elements.existingAccountIds));
        selectedValues.add(String(matched.id));
        renderExistingAccounts(Array.from(selectedValues));
        notifyPreviewChanged(false);
    }

    async function pollRegistrationTask() {
        if (!state.registrationTaskUuid) {
            return;
        }
        try {
            const task = await app.api.loadRegistrationTask(state.registrationTaskUuid);
            const statusText = `${task.status || 'running'}${task.email ? ` · ${task.email}` : ''}`;
            elements.registerStatus.textContent = `随机注册中：${statusText}`;
            if (!['completed', 'failed', 'cancelled'].includes(task.status)) {
                return;
            }

            clearInterval(state.registrationPollTimer);
            state.registrationPollTimer = null;
            const completedEmail = task.result?.email || task.email || '';
            state.registrationTaskUuid = null;
            setButtonBusy(elements.registerAccountBtn, false);

            if (task.status === 'completed') {
                elements.registerStatus.textContent = completedEmail
                    ? `新账号 ${completedEmail} 已注册完成，正在回填到补位列表。`
                    : '新账号已注册完成，正在回填到补位列表。';
                await loadSources({
                    selectedSourceAccountId: elements.sourceAccountId.value,
                    selectedAccountIds: app.getSelectedValues(elements.existingAccountIds),
                });
                selectNewlyRegisteredAccount(completedEmail);
                toast.success('随机注册成功，已刷新账号源');
                return;
            }

            elements.registerStatus.textContent = task.error_message || '随机注册未完成';
            toast.error(task.error_message || '随机注册失败');
        } catch (error) {
            clearInterval(state.registrationPollTimer);
            state.registrationPollTimer = null;
            state.registrationTaskUuid = null;
            setButtonBusy(elements.registerAccountBtn, false);
            elements.registerStatus.textContent = error.message || '随机注册状态读取失败';
            toast.error(error.message || '随机注册状态读取失败');
        }
    }

    async function handleQuickRegistration() {
        if (state.registrationTaskUuid) {
            toast.warning('已有随机注册任务在运行，请稍候');
            return;
        }
        const serviceId = app.parseInteger(elements.registerEmailService.value);
        const service = state.teamEmailServices.find((item) => item.id === serviceId);
        if (!service) {
            toast.warning('请先选择可用邮箱服务');
            return;
        }

        setButtonBusy(elements.registerAccountBtn, true, '注册中...');
        elements.registerStatus.textContent = '正在启动随机注册任务...';
        try {
            const task = await app.api.startRegistration({
                email_service_type: service.service_type,
                email_service_id: service.id,
                proxy: elements.registerProxy.value.trim() || elements.taskProxy.value.trim() || null,
            });
            state.registrationTaskUuid = task.task_uuid;
            elements.registerStatus.textContent = '随机注册任务已启动，等待账号落地...';
            state.registrationPollTimer = window.setInterval(pollRegistrationTask, 3000);
            await pollRegistrationTask();
        } catch (error) {
            state.registrationTaskUuid = null;
            setButtonBusy(elements.registerAccountBtn, false);
            elements.registerStatus.textContent = error.message || '随机注册启动失败';
            toast.error(error.message || '随机注册启动失败');
        }
    }

    async function hydrateFromTask(task) {
        if (!task) return;

        const sourceAccountId = String(task.source_account?.id || '');
        renderSourceAccounts(sourceAccountId);
        elements.sourceAccountId.value = sourceAccountId;
        renderSourceSummary();

        const selectedCustomIds = (task.members || [])
            .filter((member) => member.source_type === 'account' && member.account_id)
            .map((member) => String(member.account_id));
        renderExistingAccounts(selectedCustomIds);

        elements.manualEmails.value = (task.members || [])
            .filter((member) => member.source_type === 'manual')
            .map((member) => member.email)
            .join('\n');

        elements.taskProxy.value = task.proxy || '';
        elements.retryLimit.value = String(task.retry_limit ?? task.upload_config?.retry_limit ?? 0);

        elements.uploadSub2api.checked = Boolean(task.upload_config?.auto_upload_sub2api);
        elements.uploadCpa.checked = Boolean(task.upload_config?.auto_upload_cpa);
        elements.uploadTm.checked = Boolean(task.upload_config?.auto_upload_tm);
        syncPlatformBodies();

        renderUploadServiceSelect(
            elements.sub2apiServiceIds,
            state.uploadServices.sub2api,
            (task.upload_config?.sub2api_service_ids || []).map(String),
            '暂无可用服务',
        );
        renderUploadServiceSelect(
            elements.cpaServiceIds,
            state.uploadServices.cpa,
            (task.upload_config?.cpa_service_ids || []).map(String),
            '暂无可用服务',
        );
        renderUploadServiceSelect(
            elements.tmServiceIds,
            state.uploadServices.tm,
            (task.upload_config?.tm_service_ids || []).map(String),
            '暂无可用服务',
        );
        state.sub2apiGroupSelections = { ...(task.upload_config?.sub2api_group_ids_by_service || {}) };
        await renderSub2ApiGroups();
        updateCustomSummary();
        updateCapacityWarning();
    }

    function buildCreatePayload() {
        const sourceAccountId = app.parseInteger(elements.sourceAccountId.value, 0);
        return {
            source_mode: 'account',
            source_account_id: sourceAccountId || null,
            existing_account_ids: app.getSelectedIds(elements.existingAccountIds),
            team_source_task_uuids: [],
            manual_emails: getSelectedManualEmails(),
            ...buildRuntimeConfig(),
        };
    }

    function buildRuntimeConfig() {
        const retryLimit = Math.max(0, Math.min(10, app.parseInteger(elements.retryLimit.value, 0)));
        return {
            proxy: elements.taskProxy.value.trim() || null,
            auto_upload_sub2api: elements.uploadSub2api.checked,
            sub2api_service_ids: app.getSelectedIds(elements.sub2apiServiceIds),
            sub2api_group_ids_by_service: getSelectedSub2ApiGroups(),
            auto_upload_cpa: elements.uploadCpa.checked,
            cpa_service_ids: app.getSelectedIds(elements.cpaServiceIds),
            auto_upload_tm: elements.uploadTm.checked,
            tm_service_ids: app.getSelectedIds(elements.tmServiceIds),
            retry_limit: retryLimit,
        };
    }

    function getPreviewSnapshot() {
        const sourceAccount = app.getSelectedSourceAccount();
        const sourceTask = app.getSelectedSourceTask();
        const customAccounts = getSelectedCustomAccounts();
        const manualEmails = getSelectedManualEmails();
        const rows = [];
        const seenEmails = new Set();

        if (sourceAccount) {
            rows.push({
                key: app.buildMemberKey('main', sourceAccount.id, sourceAccount.email),
                kind: 'main_account',
                memberId: null,
                accountId: sourceAccount.id,
                email: sourceAccount.email,
                roleLabel: '主账号',
                sourceLabel: 'Team 主账号',
                invitationStatus: 'accepted',
                teamReady: true,
                note: sourceTask?.team_account_id ? `当前 Team：${sourceTask.team_account_id}` : '运行时自动发现 Team',
                actionFlags: {},
                account: sourceTask?.main_account || sourceAccount,
                raw: sourceTask?.main_account || sourceAccount,
            });
            seenEmails.add(String(sourceAccount.email || '').toLowerCase());
        }

        getSourceTaskMembers(sourceTask).forEach((member) => {
            const email = String(member.email || '').toLowerCase();
            if (!email || seenEmails.has(email)) {
                return;
            }
            seenEmails.add(email);
            rows.push({
                key: app.buildMemberKey('preview-member', member.id, member.email),
                kind: 'member',
                memberId: null,
                accountId: member.id,
                email: member.email,
                roleLabel: '成员',
                sourceLabel: '源 Team 成员',
                invitationStatus: 'pending',
                teamReady: false,
                note: `默认跟随主账号一起邀请${member.team_role ? ` · ${member.team_role}` : ''}`,
                actionFlags: {},
                account: member,
                raw: member,
                sourceType: 'team_task',
            });
        });

        customAccounts.forEach((account) => {
            const email = String(account.email || '').toLowerCase();
            if (!email || seenEmails.has(email)) {
                return;
            }
            seenEmails.add(email);
            rows.push({
                key: app.buildMemberKey('preview-custom', account.id, account.email),
                kind: 'member',
                memberId: null,
                accountId: account.id,
                email: account.email,
                roleLabel: '成员',
                sourceLabel: '自定义账号',
                invitationStatus: 'pending',
                teamReady: false,
                note: '会在开始任务时一并加入 Team 邀请队列',
                actionFlags: {},
                account,
                raw: account,
                sourceType: 'account',
            });
        });

        manualEmails.forEach((email) => {
            const normalized = String(email || '').toLowerCase();
            if (!normalized || seenEmails.has(normalized)) {
                return;
            }
            seenEmails.add(normalized);
            rows.push({
                key: app.buildMemberKey('preview-manual', null, normalized),
                kind: 'manual',
                memberId: null,
                accountId: null,
                email: normalized,
                roleLabel: 'Invite-only',
                sourceLabel: '手填邮箱',
                invitationStatus: 'invite_only',
                teamReady: false,
                note: '仅发送邀请，不参与账号详情和成员上传',
                actionFlags: {},
                account: null,
                raw: { email: normalized },
                sourceType: 'manual',
            });
        });

        const teamMemberCount = getSourceTaskMembers(sourceTask).length;
        const customMemberCount = customAccounts.length + manualEmails.length;
        return {
            task_uuid: null,
            status: 'preview',
            source_account: sourceTask?.main_account || sourceAccount,
            team_account_id: sourceTask?.team_account_id || null,
            source_team_task_uuid: sourceTask?.task_uuid || null,
            team_member_count: teamMemberCount,
            custom_member_count: customMemberCount,
            members: rows,
            selected_platforms: buildSelectedPlatforms(),
            note: sourceAccount
                ? `默认邀请 ${teamMemberCount} 个源 Team 成员，当前额外补位 ${customMemberCount} 个账号。`
                : '先选择一个可用的 Team 主账号。',
        };
    }

    function bindEvents() {
        elements.sourceAccountId.addEventListener('change', () => {
            renderSourceSummary();
            renderExistingAccounts(app.getSelectedValues(elements.existingAccountIds));
            notifyPreviewChanged(true);
        });

        elements.existingAccountIds.addEventListener('change', () => {
            notifyPreviewChanged(false);
        });

        elements.manualEmails.addEventListener('input', window.debounce(() => {
            notifyPreviewChanged(false);
        }, 200));

        elements.uploadSub2api.addEventListener('change', async () => {
            syncPlatformBodies();
            await renderSub2ApiGroups();
            if (app.taskView) app.taskView.render();
        });
        elements.uploadCpa.addEventListener('change', () => {
            syncPlatformBodies();
            if (app.taskView) app.taskView.render();
        });
        elements.uploadTm.addEventListener('change', () => {
            syncPlatformBodies();
            if (app.taskView) app.taskView.render();
        });
        elements.sub2apiServiceIds.addEventListener('change', async () => {
            await renderSub2ApiGroups();
            if (app.taskView) app.taskView.render();
        });
        elements.cpaServiceIds.addEventListener('change', () => app.taskView?.render());
        elements.tmServiceIds.addEventListener('change', () => app.taskView?.render());

        elements.sub2apiGroups.addEventListener('change', async (event) => {
            const checkbox = event.target.closest('[data-sub2api-group]');
            if (!checkbox) {
                return;
            }
            const serviceId = String(checkbox.dataset.serviceId);
            const groupId = app.parseInteger(checkbox.value);
            const selected = new Set((state.sub2apiGroupSelections[serviceId] || []).map((value) => String(value)));
            if (checkbox.checked) {
                selected.add(String(groupId));
            } else {
                selected.delete(String(groupId));
            }
            state.sub2apiGroupSelections[serviceId] = Array.from(selected).map((value) => app.parseInteger(value)).filter(Boolean);
            await renderSub2ApiNamePreviews();
            if (app.taskView) app.taskView.render();
        });

        elements.retryLimit.addEventListener('change', () => {
            elements.retryLimit.value = String(Math.max(0, Math.min(10, app.parseInteger(elements.retryLimit.value, 0))));
        });
        elements.registerAccountBtn.addEventListener('click', handleQuickRegistration);
    }

    app.settings = {
        init() {
            bindEvents();
            syncPlatformBodies();
        },
        loadSources,
        loadUploadServices,
        hydrateFromTask,
        renderSourceSummary,
        renderExistingAccounts,
        buildCreatePayload,
        buildRuntimeConfig,
        getPreviewSnapshot,
        getSelectedCustomAccounts,
        getSelectedManualEmails,
        getSelectedPlatforms: buildSelectedPlatforms,
        syncPreview(forcePreview = false) {
            notifyPreviewChanged(forcePreview);
        },
    };
})();
