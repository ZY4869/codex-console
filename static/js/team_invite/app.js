(function () {
    const app = window.TeamInviteApp = window.TeamInviteApp || {};
    const { state, elements } = app;

    async function restoreTask() {
        const savedTaskUuid = window.storage.get(app.taskStorageKey);
        if (!savedTaskUuid) {
            app.taskView.render();
            return;
        }

        try {
            await app.taskView.fetchTask(savedTaskUuid, {
                reloadLogs: true,
                hydrateConfig: true,
                preserveMode: false,
            });
            state.currentTaskMode = 'task';
            app.taskView.render();
        } catch (error) {
            window.storage.remove(app.taskStorageKey);
            state.currentTask = null;
            state.currentTaskUuid = null;
            state.currentTaskMode = 'preview';
            app.taskView.render();
        }
    }

    async function handleStartTask() {
        const sourceAccountId = app.parseInteger(elements.sourceAccountId.value, 0);
        if (!sourceAccountId) {
            toast.warning('请先选择一个可用的 Team 主账号');
            return;
        }

        loading.show(elements.startBtn, '启动中...');
        try {
            const task = await app.api.createTask(app.settings.buildCreatePayload());
            state.selectedMemberKey = null;
            state.selectedAccountDetail = null;
            await app.taskView.attachTask(task, {
                mode: 'task',
                hydrateConfig: true,
                reloadLogs: true,
            });
            toast.success('Team 邀请任务已启动');
        } catch (error) {
            toast.error(error.message || '启动 Team 邀请任务失败');
        } finally {
            loading.hide(elements.startBtn);
        }
    }

    async function handleResumeTask() {
        if (!state.currentTaskUuid || !state.currentTask?.resume_available) {
            toast.warning('当前任务没有可继续的成员');
            return;
        }

        loading.show(elements.continueBtn, '继续中...');
        try {
            const task = await app.api.resumeTask(state.currentTaskUuid, app.settings.buildRuntimeConfig());
            state.currentTaskMode = 'task';
            await app.taskView.attachTask(task, {
                mode: 'task',
                hydrateConfig: true,
                reloadLogs: false,
            });
            toast.success('已继续原任务');
        } catch (error) {
            toast.error(error.message || '继续任务失败');
        } finally {
            loading.hide(elements.continueBtn);
        }
    }

    async function handleRestartTask() {
        if (!state.currentTaskUuid || !state.currentTask?.restart_available) {
            toast.warning('当前任务没有需要重新开始的成员');
            return;
        }

        loading.show(elements.restartBtn, '重开中...');
        try {
            const task = await app.api.restartTask(state.currentTaskUuid, app.settings.buildRuntimeConfig());
            state.selectedMemberKey = null;
            state.selectedAccountDetail = null;
            await app.taskView.attachTask(task, {
                mode: 'task',
                hydrateConfig: true,
                reloadLogs: true,
            });
            toast.success('已创建新的重新开始任务');
        } catch (error) {
            toast.error(error.message || '重新开始失败');
        } finally {
            loading.hide(elements.restartBtn);
        }
    }

    async function handleCancelTask() {
        if (!state.currentTaskUuid) {
            return;
        }

        const confirmed = await window.confirm('确认取消当前 Team 邀请任务吗？', '取消 Team 邀请任务');
        if (!confirmed) {
            return;
        }

        loading.show(elements.cancelBtn, '取消中...');
        try {
            const response = await app.api.cancelTask(state.currentTaskUuid);
            toast.info(response.message || '已提交取消请求');
            const task = await app.api.loadTask(state.currentTaskUuid);
            await app.taskView.applyTaskSnapshot(task, { preserveMode: true, hydrateConfig: false });
            app.logs.renderSummary();
        } catch (error) {
            toast.error(error.message || '取消任务失败');
        } finally {
            loading.hide(elements.cancelBtn);
        }
    }

    async function handleStartTask() {
        const sourceMode = app.getSelectedSourceMode();
        const payload = app.settings.buildCreatePayload();
        if (sourceMode === 'custom_domain_email') {
            const selection = app.getCustomSourceSelection();
            if (!selection.email) {
                toast.warning('请先输入自定义主号邮箱');
                return;
            }
            if (!selection.serviceType) {
                toast.warning('请选择自定义主号的邮箱服务类型');
                return;
            }
            if (!selection.account) {
                toast.warning('当前邮箱还没有匹配到可用的本地账号');
                return;
            }
        } else if (!payload.source_account_id) {
            toast.warning('请选择一个可用的 Team 主账号');
            return;
        }

        loading.show(elements.startBtn, '启动中...');
        try {
            const task = await app.api.createTask(payload);
            state.selectedMemberKey = null;
            state.selectedAccountDetail = null;
            await app.taskView.attachTask(task, {
                mode: 'task',
                hydrateConfig: true,
                reloadLogs: true,
            });
            toast.success('Team 邀请任务已启动');
        } catch (error) {
            toast.error(error.message || '启动 Team 邀请任务失败');
        } finally {
            loading.hide(elements.startBtn);
        }
    }

    function bindEvents() {
        elements.startBtn.addEventListener('click', handleStartTask);
        elements.continueBtn.addEventListener('click', handleResumeTask);
        elements.restartBtn.addEventListener('click', handleRestartTask);
        elements.cancelBtn.addEventListener('click', handleCancelTask);
    }

    async function initialize() {
        app.settings.init();
        app.taskView.init();
        app.logs.init();
        bindEvents();

        try {
            await Promise.all([
                app.settings.loadSources(),
                app.settings.loadUploadServices(),
            ]);
        } catch (error) {
            toast.error(error.message || 'Team 邀请页初始化失败');
        }

        await restoreTask();
    }

    document.addEventListener('DOMContentLoaded', initialize);
})();
