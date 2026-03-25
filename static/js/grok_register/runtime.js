(function () {
    const app = window.GrokRegisterApp = window.GrokRegisterApp || {};
    const { state, elements } = app;

    function updateSecretHints(config) {
        elements.bczySaved.textContent = config.has_bczy_api_key ? '已保存 BCZY Key，留空则保留。' : '当前未保存 BCZY Key。';
        elements.yescaptchaSaved.textContent = config.has_yescaptcha_key ? '已保存 YesCaptcha Key，留空则保留。' : '当前未保存 YesCaptcha Key。';
    }

    function toggleCaptchaFields() {
        const isLocal = elements.captchaMode.value === 'local';
        elements.solverUrlWrap.style.display = isLocal ? 'block' : 'none';
    }

    function setServiceBadge(element, status) {
        const healthy = Boolean(status?.healthy);
        const running = Boolean(status?.running);
        element.textContent = healthy ? '可用' : (running ? '已启动' : '未启动');
        element.className = `status-badge ${healthy ? 'completed' : (running ? 'running' : 'pending')}`;
    }

    app.runtime = {
        async loadConfig() {
            const config = await app.api.loadConfig();
            elements.proxy.value = config.proxy || '';
            elements.targetCount.value = config.target_count || 1;
            elements.threadCount.value = config.thread_count || 1;
            elements.emailDomain.value = config.email_domain || 'bczy.site';
            elements.captchaMode.value = config.captcha_mode || 'yescaptcha';
            elements.solverUrl.value = config.solver_url || 'http://127.0.0.1:5072';
            elements.solverCommand.value = config.solver_command || '';
            elements.flaresolverrUrl.value = config.flaresolverr_url || 'http://127.0.0.1:8191/v1';
            elements.bczyApiKey.value = '';
            elements.yescaptchaKey.value = '';
            await app.emailServices.syncFromConfig(config);
            updateSecretHints(config);
            toggleCaptchaFields();
        },

        buildConfigPayload() {
            return {
                target_count: parseInt(elements.targetCount.value || '1', 10),
                thread_count: parseInt(elements.threadCount.value || '1', 10),
                proxy: elements.proxy.value.trim(),
                email_domain: elements.emailDomain.value.trim(),
                email_service_type: elements.emailServiceType.value || 'auto',
                email_service_id: app.emailServices.getSelectedServiceId(),
                captcha_mode: elements.captchaMode.value,
                solver_url: elements.solverUrl.value.trim(),
                solver_command: elements.solverCommand.value.trim(),
                flaresolverr_url: elements.flaresolverrUrl.value.trim(),
                bczy_api_key: elements.bczyApiKey.value,
                yescaptcha_key: elements.yescaptchaKey.value,
            };
        },

        buildCreatePayload() {
            const payload = app.runtime.buildConfigPayload();
            if (!payload.proxy) payload.proxy = null;
            if (!payload.solver_url) payload.solver_url = null;
            if (!payload.solver_command) payload.solver_command = null;
            if (!payload.flaresolverr_url) payload.flaresolverr_url = null;
            if (!payload.bczy_api_key) payload.bczy_api_key = null;
            if (!payload.yescaptcha_key) payload.yescaptcha_key = null;
            if (!payload.email_service_type) payload.email_service_type = 'auto';
            return payload;
        },

        async saveConfig() {
            loading.show(elements.saveConfigBtn, '保存中...');
            try {
                const config = await app.api.saveConfig(app.runtime.buildConfigPayload());
                updateSecretHints(config);
                elements.bczyApiKey.value = '';
                elements.yescaptchaKey.value = '';
                toast.success('默认配置已保存');
            } catch (error) {
                toast.error(error.message || '保存默认配置失败');
            } finally {
                loading.hide(elements.saveConfigBtn);
            }
        },

        async refreshRuntimeStatuses() {
            const [solverStatus, flaresolverrStatus] = await Promise.all([
                app.api.loadSolverStatus(elements.solverUrl.value.trim() || null),
                app.api.loadFlaresolverrStatus(elements.flaresolverrUrl.value.trim() || null),
            ]);
            state.solverStatus = solverStatus;
            state.flaresolverrStatus = flaresolverrStatus;
            setServiceBadge(elements.solverStatus, solverStatus);
            setServiceBadge(elements.flaresolverrStatus, flaresolverrStatus);
        },

        async startSolver() {
            loading.show(elements.solverStartBtn, '启动中...');
            try {
                await app.api.startSolver(elements.solverCommand.value.trim() || null);
                await app.runtime.refreshRuntimeStatuses();
                toast.success('Local Solver 已启动');
            } catch (error) {
                toast.error(error.message || '启动 Local Solver 失败');
            } finally {
                loading.hide(elements.solverStartBtn);
            }
        },

        async stopSolver() {
            loading.show(elements.solverStopBtn, '停止中...');
            try {
                await app.api.stopSolver();
                await app.runtime.refreshRuntimeStatuses();
                toast.info('Local Solver 已停止');
            } catch (error) {
                toast.error(error.message || '停止 Local Solver 失败');
            } finally {
                loading.hide(elements.solverStopBtn);
            }
        },

        async startFlaresolverr() {
            loading.show(elements.flaresolverrStartBtn, '启动中...');
            try {
                await app.api.startFlaresolverr(elements.flaresolverrUrl.value.trim() || null);
                await app.runtime.refreshRuntimeStatuses();
                toast.success('FlareSolverr 启动命令已触发');
            } catch (error) {
                toast.error(error.message || '启动 FlareSolverr 失败');
            } finally {
                loading.hide(elements.flaresolverrStartBtn);
            }
        },

        async stopFlaresolverr() {
            loading.show(elements.flaresolverrStopBtn, '停止中...');
            try {
                await app.api.stopFlaresolverr(elements.flaresolverrUrl.value.trim() || null);
                await app.runtime.refreshRuntimeStatuses();
                toast.info('FlareSolverr 已停止');
            } catch (error) {
                toast.error(error.message || '停止 FlareSolverr 失败');
            } finally {
                loading.hide(elements.flaresolverrStopBtn);
            }
        },

        bindEvents() {
            app.emailServices.bindEvents();
            elements.saveConfigBtn.addEventListener('click', app.runtime.saveConfig);
            elements.captchaMode.addEventListener('change', toggleCaptchaFields);
            elements.solverStartBtn.addEventListener('click', app.runtime.startSolver);
            elements.solverStopBtn.addEventListener('click', app.runtime.stopSolver);
            elements.flaresolverrStartBtn.addEventListener('click', app.runtime.startFlaresolverr);
            elements.flaresolverrStopBtn.addEventListener('click', app.runtime.stopFlaresolverr);
        },
    };
})();
