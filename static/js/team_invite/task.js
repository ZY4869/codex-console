(function () {
    const app = window.TeamInviteApp = window.TeamInviteApp || {};
    const { state, elements } = app;

    const platformLabelMap = {
        sub2api: 'Sub2API',
        cpa: 'CPA',
        tm: 'Team Manager',
        noop: '未上传平台',
    };

    function renderStatusBadge(meta, text) {
        return `<span class="status-badge ${meta.className}">${app.escapeHtml(text || meta.text)}</span>`;
    }

    function formatSourceLabel(sourceType) {
        return app.sourceTypeMeta[sourceType] || sourceType || '-';
    }

    function getGuidance(row) {
        if (row?.guidance?.action) {
            return row.guidance;
        }
        if (row?.raw?.guidance?.action) {
            return row.raw.guidance;
        }
        if (row?.kind === 'main_account') {
            return {
                action: 'none',
                label: '主账号上下文',
                tone: 'completed',
                message: '主账号用于提供 Team 上下文，不参与成员重登建议。',
            };
        }
        if (row?.kind === 'manual') {
            return {
                action: 'none',
                label: '仅邀请',
                tone: 'pending',
                message: '手填邮箱只参与邀请，不参与自动补登。',
            };
        }
        return {
            action: 'none',
            label: '等待处理',
            tone: 'pending',
            message: row?.note || '等待任务运行后再给出处理建议。',
        };
    }

    function buildTaskRows(task) {
        const rows = [];
        if (task?.source_account?.id) {
            rows.push({
                key: app.buildMemberKey('main', task.source_account.id, task.source_account.email),
                kind: 'main_account',
                memberId: null,
                accountId: task.source_account.id,
                email: task.source_account.email,
                roleLabel: '主账号',
                sourceLabel: 'Team 主账号',
                invitationStatus: 'accepted',
                teamReady: true,
                note: task.team_account_id ? `目标 Team：${task.team_account_id}` : '运行时自动发现 Team',
                actionFlags: {},
                account: task.source_account,
                raw: task.source_account,
                sourceType: 'main_account',
                guidance: {
                    action: 'none',
                    label: '主账号上下文',
                    tone: 'completed',
                    message: '主账号用于维持 Team 上下文，不参与成员重登建议。',
                },
            });
        }

        (task?.members || []).forEach((member) => {
            rows.push({
                key: app.buildMemberKey('member', member.id, member.email),
                kind: member.source_type === 'manual' ? 'manual' : 'member',
                memberId: member.id,
                accountId: member.account_id,
                email: member.email,
                roleLabel: '成员',
                sourceLabel: formatSourceLabel(member.source_type),
                invitationStatus: member.invitation_status,
                teamReady: Boolean(member.team_ready),
                note: member.error_message || buildMemberNote(member),
                actionFlags: member.action_flags || {},
                account: member.account,
                raw: member,
                sourceType: member.source_type,
                guidance: member.guidance || null,
            });
        });

        return rows;
    }

    function buildMemberNote(member) {
        const reason = String(member?.result?.reason || '').trim();
        if (reason && app.memberReasonMeta[reason]) {
            return app.memberReasonMeta[reason];
        }
        if (member.last_action) {
            return `最近动作：${member.last_action}`;
        }
        if (member.invitation_status === 'uploaded') {
            return '已完成 Team 身份刷新并上传。';
        }
        if (member.invitation_status === 'accepted') {
            return '已接受邀请，正在等待后续处理。';
        }
        if (member.invitation_status === 'invite_only') {
            return '仅发送邀请，不参与成员上传。';
        }
        return '等待开始处理。';
    }

    function buildPreviewNote(preview) {
        const parts = [preview.note];
        if (state.currentTaskUuid && state.currentTask) {
            const meta = app.getTaskStatusMeta(state.currentTask.status);
            parts.push(`最近任务 ${state.currentTask.task_uuid} 当前为 ${meta.text}。`);
            if (state.currentTask.resume_available) {
                parts.push('可以直接点击左侧“继续”续跑未完成成员。');
            }
            if (state.currentTask.restart_available) {
                parts.push('也可以点击“重新开始”生成一份自动跳过已完成成员的新任务。');
            }
        }
        return parts.join(' ');
    }

    function buildTaskNote(task) {
        const parts = [task.runtime_message || `当前状态：${app.getTaskStatusMeta(task.status).text}`];
        if (task.team_account_id) {
            parts.push(`目标 Team：${task.team_account_id}`);
        }
        if (task.error_message) {
            parts.push(`错误：${task.error_message}`);
        }
        if (task.resume_available) {
            parts.push('当前任务支持继续。');
        }
        if (task.restart_available) {
            parts.push('当前任务支持重新开始。');
        }
        if (task.recommended_actions?.relogin_count) {
            parts.push(`有 ${task.recommended_actions.relogin_count} 个成员建议先重新登录。`);
        }
        if (task.recommended_actions?.reinvite_count) {
            parts.push(`有 ${task.recommended_actions.reinvite_count} 个成员建议重新邀请。`);
        }
        return parts.join(' ');
    }

    function getViewState() {
        if (state.currentTaskMode === 'task' && state.currentTask) {
            return {
                mode: 'task',
                task: state.currentTask,
                taskUuid: state.currentTask.task_uuid,
                status: state.currentTask.status,
                statusMeta: app.getTaskStatusMeta(state.currentTask.status),
                rows: buildTaskRows(state.currentTask),
                teamMemberCount: state.currentTask.team_member_count || 0,
                customMemberCount: state.currentTask.custom_member_count || 0,
                note: buildTaskNote(state.currentTask),
                selectedPlatforms: (state.currentTask.selected_platforms || []).filter((item) => item.enabled),
            };
        }

        const preview = app.settings.getPreviewSnapshot();
        return {
            mode: 'preview',
            task: null,
            taskUuid: null,
            status: 'preview',
            statusMeta: { text: '预览', className: 'pending' },
            rows: preview.members || [],
            teamMemberCount: preview.team_member_count || 0,
            customMemberCount: preview.custom_member_count || 0,
            note: buildPreviewNote(preview),
            selectedPlatforms: (preview.selected_platforms || []).filter((item) => item.enabled),
        };
    }

    function getRowByKey(viewState, key) {
        return (viewState.rows || []).find((row) => row.key === key) || null;
    }

    function ensureSelectedMember(viewState) {
        if (!state.selectedMemberKey) {
            return null;
        }
        const row = getRowByKey(viewState, state.selectedMemberKey);
        if (row) {
            return row;
        }
        state.selectedMemberKey = null;
        state.selectedAccountDetail = null;
        return null;
    }

    function getIdentityDisplay(row) {
        if (row.kind === 'main_account') {
            return renderStatusBadge({ className: 'completed', text: '主 Team' });
        }
        if (row.kind === 'manual') {
            return renderStatusBadge({ className: 'pending', text: 'Invite-only' });
        }
        if (row.teamReady) {
            return renderStatusBadge({ className: 'completed', text: 'Team-ready' });
        }
        if (row.raw?.result?.reason === 'pending_invite') {
            return renderStatusBadge({ className: 'warning', text: '待接受' });
        }
        return renderStatusBadge({ className: 'pending', text: '待刷新' });
    }

    function buildAccountMeta(row) {
        const meta = [];
        if (row.roleLabel) {
            meta.push(row.roleLabel);
        }
        if (row.sourceLabel) {
            meta.push(row.sourceLabel);
        }
        if (row.kind === 'main_account') {
            meta.push('主上下文');
        } else if (row.kind === 'manual') {
            meta.push('Invite-only');
        } else {
            meta.push(row.teamReady ? 'Team-ready' : '待处理');
        }
        return meta.join(' · ');
    }

    function buildStatusNote(row) {
        if (row.kind === 'main_account') {
            return '主账号负责提供 Team 上下文，不参与成员上传。';
        }
        if (row.raw?.last_action) {
            return `最近动作：${row.raw.last_action}`;
        }
        if (row.teamReady) {
            return '账号已经具备 Team 身份，可继续上传。';
        }
        return row.note || '等待任务继续推进。';
    }

    function renderGuidancePill(row) {
        const guidance = getGuidance(row);
        return `<span class="recommendation-pill ${app.escapeHtml(guidance.tone || 'pending')}">${app.escapeHtml(guidance.label || '等待处理')}</span>`;
    }

    function renderActions(row, viewState) {
        if (viewState.mode !== 'task' || row.kind === 'main_account') {
            return row.kind === 'main_account' ? '' : '<span class="hint-line">预览</span>';
        }
        if (row.kind === 'manual') {
            return '';
        }

        const buttons = [];
        if (row.actionFlags?.accept_or_refresh && row.memberId) {
            const needsAccept = row.invitationStatus === 'invited' || row.invitationStatus === 'pending'
                || (row.raw?.result?.reason === 'pending_invite');
            const label = needsAccept ? '接受' : '刷新';
            buttons.push(`
                <button class="btn btn-secondary btn-sm" type="button" data-member-action="accept" data-member-id="${row.memberId}">
                    ${label}
                </button>
            `);
        }
        if (row.actionFlags?.upload && row.memberId) {
            buttons.push(`
                <button class="btn btn-ghost btn-sm" type="button" data-member-action="upload" data-member-id="${row.memberId}">
                    上传
                </button>
            `);
        }
        if (!buttons.length) {
            return '';
        }
        return `<div class="mini-actions">${buttons.join('')}</div>`;
    }

    function renderSummary(viewState) {
        elements.taskUuid.textContent = viewState.taskUuid || '-';
        elements.taskStatus.textContent = viewState.statusMeta.text;
        elements.taskTeamCount.textContent = String(viewState.teamMemberCount || 0);
        elements.taskCustomCount.textContent = String(viewState.customMemberCount || 0);
    }

    function renderMembers(viewState) {
        if (!viewState.rows.length) {
            elements.membersBody.innerHTML = `
                <tr>
                    <td colspan="4">
                        <div class="empty-panel">还没有可展示的 Team 团队。先选择主账号，或恢复一个已有邀请任务。</div>
                    </td>
                </tr>
            `;
            return;
        }

        const selectedRow = ensureSelectedMember(viewState);
        elements.membersBody.innerHTML = viewState.rows.map((row) => {
            const statusMeta = row.kind === 'main_account'
                ? { text: '管理员', className: 'completed' }
                : app.getMemberStatusMeta(row.invitationStatus);
            return `
                <tr class="${selectedRow?.key === row.key ? 'active' : ''}" data-row-key="${row.key}">
                    <td>
                        <div class="member-email">
                            <strong>${app.escapeHtml(row.email)}</strong>
                            <span class="member-meta">${app.escapeHtml(row.roleLabel || '')}${row.teamReady ? ' · Team-ready' : ''}</span>
                        </div>
                    </td>
                    <td>${renderStatusBadge(statusMeta)}</td>
                    <td>${renderGuidancePill(row)}</td>
                    <td>${renderActions(row, viewState)}</td>
                </tr>
            `;
        }).join('');
    }

    function buildSub2ApiCopySummary(item) {
        const copies = Array.isArray(item?.copies) ? item.copies : [];
        if (!copies.length) {
            return `<code>${app.escapeHtml(item?.message || item?.error || '-')}</code>`;
        }
        const summary = `副本 ${item.copy_total || copies.length} 份，成功 ${item.success_count || 0}，失败 ${item.failed_count || 0}`;
        const copyLines = copies.slice(0, 4).map((copy) => {
            const groupLabel = copy.group_name || (copy.group_id ? `Group ${copy.group_id}` : '未分组');
            const generatedName = copy.generated_name || '-';
            const resultLabel = copy.success ? '成功' : '失败';
            return app.escapeHtml(`${groupLabel}：${generatedName}（${resultLabel}）`);
        }).join('<br>');
        return `
            <div class="hint-line">${app.escapeHtml(summary)}</div>
            <div class="hint-line">${copyLines}</div>
        `;
    }

    function buildPlatformUploadList(platformUploads) {
        const entries = Object.entries(platformUploads || {});
        if (!entries.length) {
            return '<div class="hint-line">尚未产生成员上传记录。</div>';
        }
        return `
            <div class="detail-grid">
                ${entries.map(([platformKey, item]) => `
                    <div class="detail-item">
                        <span>${app.escapeHtml(platformLabelMap[platformKey] || platformKey)}</span>
                        <strong>${item.success ? '成功' : '失败'}</strong>
                        ${platformKey === 'sub2api'
                            ? buildSub2ApiCopySummary(item)
                            : `<code>${app.escapeHtml(item.message || item.error || '-')}</code>`}
                    </div>
                `).join('')}
            </div>
        `;
    }

    function truncateToken(value) {
        if (!value || value.length <= 32) return value || '-';
        return value.slice(0, 20) + '...' + value.slice(-8);
    }

    function buildTokenRow(label, value) {
        const display = truncateToken(value);
        const isEmpty = !value;
        const copyBtn = value
            ? `<button class="btn btn-ghost btn-sm" type="button" data-copy-token="${app.escapeHtml(value)}">复制</button>`
            : '';
        return `
            <div class="token-row">
                <span class="token-label">${app.escapeHtml(label)}</span>
                <span class="token-value${isEmpty ? ' empty' : ''}">${app.escapeHtml(display)}</span>
                ${copyBtn}
            </div>
        `;
    }

    function renderDetail(row) {
        if (!row) {
            elements.accountDetail.innerHTML = '点击上方成员后，这里会展示账号详情。';
            elements.accountDetail.className = 'empty-panel';
            return;
        }

        if (!row.accountId) {
            elements.accountDetail.className = 'empty-panel';
            elements.accountDetail.innerHTML = `
                <strong>${app.escapeHtml(row.email)}</strong><br>
                这是手填邮箱，只会参与 invite-only 流程，不会展示本地账号详情，也不会开放成员上传。
            `;
            return;
        }

        const detailState = state.selectedAccountDetail && state.selectedAccountDetail.key === row.key
            ? state.selectedAccountDetail
            : null;
        const account = { ...(row.account || {}), ...(detailState?.account || {}) };
        const tokens = detailState?.tokens || {};
        const memberResult = row.raw?.result || {};
        const status = account.status || row.raw?.account?.status || '-';
        const lastAction = row.raw?.last_action || memberResult.last_action || '-';
        const reason = memberResult.reason || '-';
        const loadingHint = detailState?.loading ? '<div class="hint-line">正在刷新账号详情...</div>' : '';
        const errorHint = detailState?.error ? `<div class="warning-banner show">${app.escapeHtml(detailState.error)}</div>` : '';

        const accessToken = tokens.access_token || account.access_token || '';
        const refreshToken = tokens.refresh_token || '';
        const sessionToken = account.session_token || '';

        elements.accountDetail.className = '';
        elements.accountDetail.innerHTML = `
            ${loadingHint}
            ${errorHint}
            <div class="detail-grid">
                <div class="detail-item">
                    <span>邮箱</span>
                    <strong>${app.escapeHtml(account.email || row.email)}</strong>
                </div>
                <div class="detail-item">
                    <span>状态</span>
                    <strong>${app.escapeHtml(status)}</strong>
                </div>
                <div class="detail-item">
                    <span>订阅类型</span>
                    <strong>${app.escapeHtml(account.subscription_type || '-')}</strong>
                </div>
                <div class="detail-item">
                    <span>备注</span>
                    <strong>${app.escapeHtml(account.remark || '-')}</strong>
                </div>
                <div class="detail-item">
                    <span>Account ID</span>
                    <code>${app.escapeHtml(account.account_id || '-')}</code>
                </div>
                <div class="detail-item">
                    <span>Workspace ID</span>
                    <code>${app.escapeHtml(account.workspace_id || '-')}</code>
                </div>
                <div class="detail-item">
                    <span>最近动作</span>
                    <strong>${app.escapeHtml(lastAction)}</strong>
                </div>
                <div class="detail-item">
                    <span>最后刷新</span>
                    <strong>${app.escapeHtml(account.last_refresh ? format.date(account.last_refresh) : '-')}</strong>
                </div>
            </div>
            <div class="token-list">
                ${buildTokenRow('Access Token', accessToken)}
                ${buildTokenRow('Refresh Token', refreshToken)}
                ${buildTokenRow('Session Token', sessionToken)}
            </div>
            <div style="margin-top: 12px;">
                ${buildPlatformUploadList(row.raw?.platform_uploads || {})}
            </div>
        `;
    }

    function renderUploadPreview(viewState) {
        if (!viewState.selectedPlatforms.length) {
            elements.uploadResults.innerHTML = '<div class="empty-panel">尚未选择上传平台。</div>';
            return;
        }

        elements.uploadResults.innerHTML = viewState.selectedPlatforms.map((platform) => {
            const groupCount = platform.key === 'sub2api'
                ? Object.values(platform.group_ids_by_service || {}).reduce((sum, item) => sum + (item || []).length, 0)
                : 0;
            return `
                <div class="upload-card">
                    <h5>${app.escapeHtml(platform.label)}</h5>
                    <p>已选择 ${platform.service_ids?.length || 0} 个服务。</p>
                    <p>${platform.key === 'sub2api' ? `已勾选 ${groupCount} 个分组，真实上传会按分组复制账号，并按 GPT-Identity-序号 自动命名。` : '开始任务后会在这里展示平台上传进度。'}</p>
                </div>
            `;
        }).join('');
    }

    function buildTaskUploadCards(task, selectedPlatforms) {
        const uploadSummary = task?.result?.upload || {};
        const members = task?.members || [];
        const platformKeys = new Set(selectedPlatforms.map((item) => item.key));
        Object.keys(uploadSummary).forEach((key) => {
            if (key !== 'team_context') {
                platformKeys.add(key);
            }
        });

        if (!platformKeys.size) {
            return '<div class="empty-panel">当前任务未配置上传平台。</div>';
        }

        const cards = Array.from(platformKeys).map((platformKey) => {
            const platformLabel = platformLabelMap[platformKey] || platformKey;
            const platformResult = uploadSummary[platformKey] || {};
            const memberUploads = members
                .map((member) => ({ email: member.email, upload: member.platform_uploads?.[platformKey] }))
                .filter((item) => item.upload);
            const platformMessages = memberUploads.slice(0, 4);

            const selected = selectedPlatforms.find((item) => item.key === platformKey);
            const serviceCount = selected?.service_ids?.length || platformResult.services?.length || 0;
            const accountTotal = platformResult.account_total || memberUploads.length;
            const copyTotal = platformKey === 'sub2api'
                ? (platformResult.copy_total || memberUploads.reduce((sum, item) => sum + (item.upload?.copy_total || item.upload?.copies?.length || 0), 0))
                : 0;
            const recentLines = platformMessages.length
                ? `<ul>${platformMessages.map((item) => {
                    if (platformKey !== 'sub2api') {
                        return `<li>${app.escapeHtml(item.email)}：${app.escapeHtml(item.upload.message || item.upload.error || (item.upload.success ? '成功' : '失败'))}</li>`;
                    }
                    const copies = Array.isArray(item.upload?.copies) ? item.upload.copies : [];
                    const copyText = copies.length
                        ? copies.slice(0, 3).map((copy) => {
                            const groupLabel = copy.group_name || (copy.group_id ? `Group ${copy.group_id}` : '未分组');
                            const generatedName = copy.generated_name || '-';
                            const resultLabel = copy.success ? '成功' : '失败';
                            return `${groupLabel}: ${generatedName} (${resultLabel})`;
                        }).join('；')
                        : (item.upload.message || item.upload.error || (item.upload.success ? '成功' : '失败'));
                    return `<li>${app.escapeHtml(item.email)}：${app.escapeHtml(copyText)}</li>`;
                }).join('')}</ul>`
                : '<p>暂未回传成员级结果。</p>';

            return `
                <div class="upload-card">
                    <h5>${app.escapeHtml(platformLabel)}</h5>
                    <p>服务数：${serviceCount}</p>
                    <p>成功 ${platformResult.success_count || 0} / 失败 ${platformResult.failed_count || 0} / 跳过 ${platformResult.skipped_count || 0}</p>
                    ${platformKey === 'sub2api' ? `<p>账号数：${accountTotal} / 副本数：${copyTotal}</p>` : ''}
                    ${recentLines}
                </div>
            `;
        });

        return cards.join('');
    }

    function renderUploadResults(viewState) {
        if (viewState.mode !== 'task' || !viewState.task) {
            renderUploadPreview(viewState);
            return;
        }
        elements.uploadResults.innerHTML = buildTaskUploadCards(viewState.task, viewState.selectedPlatforms);
    }

    function updateActionButtons() {
        const task = state.currentTask;
        elements.continueBtn.disabled = !task?.resume_available;
        elements.restartBtn.disabled = !task?.restart_available;
        elements.cancelBtn.disabled = !(task && app.runningInviteStatuses.has(task.status));
    }

    async function loadAccountDetailForRow(row) {
        if (!row?.accountId) {
            state.selectedAccountDetail = null;
            render();
            return;
        }

        state.selectedAccountDetail = {
            key: row.key,
            loading: true,
            account: row.account || null,
            tokens: null,
            error: null,
        };
        render();

        try {
            const [account, tokens] = await app.api.loadAccountDetail(row.accountId);
            if (state.selectedMemberKey !== row.key) {
                return;
            }
            state.selectedAccountDetail = {
                key: row.key,
                loading: false,
                account,
                tokens,
                error: null,
            };
        } catch (error) {
            if (state.selectedMemberKey !== row.key) {
                return;
            }
            state.selectedAccountDetail = {
                key: row.key,
                loading: false,
                account: row.account || null,
                tokens: null,
                error: error.message || '账号详情读取失败',
            };
        }
        render();
    }

    async function selectRow(rowKey) {
        const viewState = getViewState();
        const row = getRowByKey(viewState, rowKey);
        state.selectedMemberKey = rowKey;
        if (!row) {
            state.selectedAccountDetail = null;
            render();
            return;
        }
        await loadAccountDetailForRow(row);
    }

    async function runMemberAction(button, action, memberId) {
        if (!state.currentTaskUuid) {
            toast.warning('当前没有可操作的 Team 邀请任务');
            return;
        }

        const payload = app.settings.buildRuntimeConfig();
        app.logs.resetLogs();
        loading.show(button, action === 'upload' ? '上传中...' : '处理中...');
        try {
            const response = action === 'upload'
                ? await app.api.uploadMember(state.currentTaskUuid, memberId, payload)
                : await app.api.acceptMember(state.currentTaskUuid, memberId, payload);
            state.currentTaskMode = 'task';
            state.currentTask = response.task;
            state.currentTaskUuid = response.task.task_uuid;
            state.selectedMemberKey = app.buildMemberKey('member', response.member.id, response.member.email);
            window.storage.set(app.taskStorageKey, state.currentTaskUuid);
            await app.settings.hydrateFromTask(response.task);
            render();
            app.logs.startTask(state.currentTaskUuid, { reloadLogs: false });
            await loadAccountDetailForRow(getRowByKey(getViewState(), state.selectedMemberKey));
            toast.success(action === 'upload' ? '成员上传已触发' : '成员操作已完成');
        } catch (error) {
            toast.error(error.message || '成员操作失败');
        } finally {
            loading.hide(button);
        }
    }

    async function runBatchRelogin() {
        if (!state.currentTaskUuid || !state.currentTask) {
            toast.warning('当前没有可操作的 Team 邀请任务');
            return;
        }

        const reloginMemberIds = state.currentTask.recommended_actions?.relogin_member_ids || [];
        if (!reloginMemberIds.length) {
            toast.warning('当前没有建议重登的成员');
            return;
        }

        loading.show(elements.batchReloginBtn, '重登中...');
        try {
            const response = await app.api.reloginMembers(state.currentTaskUuid, {
                ...app.settings.buildRuntimeConfig(),
                member_ids: reloginMemberIds,
            });
            state.currentTaskMode = 'task';
            state.currentTask = response.task;
            state.currentTaskUuid = response.task.task_uuid;
            window.storage.set(app.taskStorageKey, state.currentTaskUuid);
            await app.settings.hydrateFromTask(response.task);
            render();
            app.logs.startTask(state.currentTaskUuid, { reloadLogs: false });

            if (state.selectedMemberKey) {
                const row = getRowByKey(getViewState(), state.selectedMemberKey);
                if (row?.accountId) {
                    await loadAccountDetailForRow(row);
                }
            }

            const successCount = response.result?.success_count || 0;
            const failedCount = response.result?.failed_count || 0;
            toast.success(`批量重登完成：成功 ${successCount}，失败 ${failedCount}`);
        } catch (error) {
            toast.error(error.message || '批量一键重登失败');
        } finally {
            loading.hide(elements.batchReloginBtn);
        }
    }

    async function attachTask(task, options = {}) {
        if (!task) return;
        state.currentTask = task;
        state.currentTaskUuid = task.task_uuid;
        state.currentTaskMode = options.mode || 'task';
        window.storage.set(app.taskStorageKey, state.currentTaskUuid);
        if (options.hydrateConfig !== false) {
            await app.settings.hydrateFromTask(task);
        }
        render();
        app.logs.startTask(task.task_uuid, { reloadLogs: options.reloadLogs !== false });
    }

    async function applyTaskSnapshot(task, options = {}) {
        if (!task) return;
        state.currentTask = task;
        state.currentTaskUuid = task.task_uuid;
        if (!options.preserveMode) {
            state.currentTaskMode = 'task';
        }
        if (options.hydrateConfig) {
            await app.settings.hydrateFromTask(task);
        }
        window.storage.set(app.taskStorageKey, state.currentTaskUuid);
        render();
    }

    async function fetchTask(taskUuid, options = {}) {
        const task = await app.api.loadTask(taskUuid);
        await applyTaskSnapshot(task, {
            preserveMode: options.preserveMode !== false,
            hydrateConfig: Boolean(options.hydrateConfig),
        });
        if (options.reloadLogs) {
            app.logs.startTask(taskUuid, { reloadLogs: true });
        }
        return task;
    }

    function render() {
        const viewState = getViewState();
        renderSummary(viewState);
        renderMembers(viewState);
        renderUploadResults(viewState);
        renderDetail(ensureSelectedMember(viewState));
        updateActionButtons();
        app.logs.renderSummary();
    }

    function bindEvents() {
        elements.membersBody.addEventListener('click', async (event) => {
            const actionButton = event.target.closest('[data-member-action]');
            if (actionButton) {
                event.preventDefault();
                event.stopPropagation();
                const memberId = app.parseInteger(actionButton.dataset.memberId);
                if (!memberId) {
                    return;
                }
                await runMemberAction(actionButton, actionButton.dataset.memberAction, memberId);
                return;
            }

            const rowElement = event.target.closest('tr[data-row-key]');
            if (!rowElement) {
                return;
            }
            await selectRow(rowElement.dataset.rowKey);
        });

        elements.accountDetail.addEventListener('click', (event) => {
            const copyBtn = event.target.closest('[data-copy-token]');
            if (copyBtn) {
                const token = copyBtn.dataset.copyToken;
                if (token && typeof copyToClipboard === 'function') {
                    copyToClipboard(token);
                } else if (token && navigator.clipboard) {
                    navigator.clipboard.writeText(token);
                    toast.success('已复制到剪贴板');
                }
            }
        });
    }

    app.taskView = {
        init() {
            bindEvents();
        },
        render,
        attachTask,
        applyTaskSnapshot,
        fetchTask,
        updateActionButtons,
    };
})();
