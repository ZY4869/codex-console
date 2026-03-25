(function () {
    const app = window.GrokRegisterApp = window.GrokRegisterApp || {};
    const { state, elements } = app;

    function renderAccounts(accounts) {
        if (!accounts.length) {
            elements.accountsBody.innerHTML = '<tr><td colspan="7"><div class="empty-panel">创建任务后，这里会显示账号步骤明细。</div></td></tr>';
            return;
        }

        elements.accountsBody.innerHTML = accounts.map((account) => {
            const meta = app.getAccountStatusMeta(account.status);
            return `
                <tr>
                    <td>${account.order_index + 1}</td>
                    <td>${app.escapeHtml(account.email || '-')}</td>
                    <td><span class="status-badge ${meta.className}">${app.escapeHtml(meta.text)}</span></td>
                    <td>${app.escapeHtml(account.step || '-')}</td>
                    <td>${app.escapeHtml(account.sso_token_preview || '-')}</td>
                    <td>${account.nsfw_enabled ? 'Yes' : 'No'}</td>
                    <td>${app.escapeHtml(account.error_message || '-')}</td>
                </tr>
            `;
        }).join('');
    }

    function renderHistory(tasks) {
        if (!tasks.length) {
            elements.history.innerHTML = '<div class="empty-panel">暂无历史任务。</div>';
            return;
        }

        elements.history.innerHTML = tasks.map((task) => {
            const meta = app.getStatusMeta(task.status);
            return `
                <button class="history-item" type="button" data-task-uuid="${task.task_uuid}">
                    <div class="history-copy">
                        <strong>${app.escapeHtml(task.task_uuid)}</strong>
                        <span>${app.escapeHtml(task.email_domain || '-')} | 成功 ${task.success_count || 0} / 失败 ${task.failed_count || 0}</span>
                    </div>
                    <span class="status-badge ${meta.className}">${app.escapeHtml(meta.text)}</span>
                </button>
            `;
        }).join('');
    }

    function stopPolling() {
        if (state.pollTimer) {
            clearInterval(state.pollTimer);
            state.pollTimer = null;
        }
    }

    function startPolling() {
        stopPolling();
        state.pollTimer = setInterval(() => {
            if (state.currentTaskUuid) {
                app.taskView.fetchTask(state.currentTaskUuid, { reloadLogs: false });
            }
        }, 3000);
    }

    app.taskView = {
        async createTask() {
            loading.show(elements.startBtn, '启动中...');
            try {
                const task = await app.api.createTask(app.runtime.buildCreatePayload());
                await app.taskView.attachTask(task, { reloadLogs: true });
                toast.success('Grok 注册任务已启动');
            } catch (error) {
                toast.error(error.message || '启动 Grok 注册任务失败');
            } finally {
                loading.hide(elements.startBtn);
            }
        },

        async cancelTask() {
            if (!state.currentTaskUuid) {
                return;
            }
            const confirmed = await window.confirm('确认取消当前 Grok 注册任务吗？', '取消 Grok 注册任务');
            if (!confirmed) {
                return;
            }

            loading.show(elements.cancelBtn, '取消中...');
            try {
                const response = await app.api.cancelTask(state.currentTaskUuid);
                toast.info(response.message || '已提交取消请求');
                await app.taskView.fetchTask(state.currentTaskUuid, { reloadLogs: false });
            } catch (error) {
                toast.error(error.message || '取消任务失败');
            } finally {
                loading.hide(elements.cancelBtn);
            }
        },

        async fetchTask(taskUuid, { reloadLogs = false } = {}) {
            const task = await app.api.loadTask(taskUuid);
            await app.taskView.applyTaskSnapshot(task, { reloadLogs });
        },

        async attachTask(task, { reloadLogs = false } = {}) {
            state.currentTask = task;
            state.currentTaskUuid = task.task_uuid;
            window.storage.set(app.storageKey, task.task_uuid);
            await app.taskView.applyTaskSnapshot(task, { reloadLogs });
            app.logs.connect(task.task_uuid);
            startPolling();
        },

        async applyTaskSnapshot(task, { reloadLogs = false } = {}) {
            state.currentTask = task;
            state.currentTaskUuid = task.task_uuid;
            const meta = app.getStatusMeta(task.status);
            elements.taskUuid.textContent = task.task_uuid || '-';
            elements.taskStatus.textContent = meta.text;
            elements.taskStatus.className = `status-badge ${meta.className}`;
            elements.taskSuccess.textContent = String(task.success_count || 0);
            elements.taskFailed.textContent = String(task.failed_count || 0);
            elements.runtimeMessage.textContent = task.runtime_message || task.error_message || '任务已创建，等待下一步。';
            elements.cancelBtn.disabled = !app.runningStatuses.has(task.status);
            renderAccounts(task.accounts || []);
            if (reloadLogs) {
                await app.logs.load(task.task_uuid);
            }
            if (!app.runningStatuses.has(task.status)) {
                stopPolling();
            }
            await app.taskView.loadHistory(false);
        },

        async loadHistory(showToast = false) {
            try {
                const response = await app.api.loadTasks();
                state.historyTasks = Array.isArray(response.tasks) ? response.tasks : [];
                renderHistory(state.historyTasks);
                if (showToast) {
                    toast.success('历史任务已刷新');
                }
            } catch (error) {
                if (showToast) {
                    toast.error(error.message || '刷新历史任务失败');
                }
            }
        },

        async restoreTask() {
            const taskUuid = window.storage.get(app.storageKey);
            if (!taskUuid) {
                await app.taskView.loadHistory(false);
                return;
            }
            try {
                await app.taskView.fetchTask(taskUuid, { reloadLogs: true });
                app.logs.connect(taskUuid);
                startPolling();
            } catch (error) {
                window.storage.remove(app.storageKey);
                state.currentTask = null;
                state.currentTaskUuid = null;
                await app.taskView.loadHistory(false);
            }
        },

        bindEvents() {
            elements.startBtn.addEventListener('click', app.taskView.createTask);
            elements.cancelBtn.addEventListener('click', app.taskView.cancelTask);
            elements.refreshHistoryBtn.addEventListener('click', () => app.taskView.loadHistory(true));
            elements.history.addEventListener('click', (event) => {
                const button = event.target.closest('[data-task-uuid]');
                if (!button) {
                    return;
                }
                app.taskView.fetchTask(button.dataset.taskUuid, { reloadLogs: true });
            });
        },
    };
})();
