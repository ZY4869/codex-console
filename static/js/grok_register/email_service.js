(function () {
    const app = window.GrokRegisterApp = window.GrokRegisterApp || {};
    const { state, elements } = app;

    const LABELS = {
        auto: '自动选择',
        tempmail: 'Tempmail.lol',
        moe_mail: 'MoeMail',
        temp_mail: 'Temp-Mail',
        duck_mail: 'DuckMail',
        freemail: 'Freemail',
    };

    function escapeHtml(text) {
        return app.escapeHtml ? app.escapeHtml(text) : String(text ?? '');
    }

    function flattenAvailableServices(payload) {
        const catalog = {};
        Object.entries(payload || {}).forEach(([type, group]) => {
            if (!group?.available) {
                return;
            }
            catalog[type] = (group.services || []).map((service) => ({
                id: service.id,
                label: service.name || LABELS[type] || type,
                description: service.default_domain || service.description || '',
            }));
        });
        return catalog;
    }

    function renderTypeOptions(selectedType) {
        const types = ['auto', ...Object.keys(state.emailServiceCatalog || {})];
        elements.emailServiceType.innerHTML = types.map((type) => `
            <option value="${escapeHtml(type)}">${escapeHtml(LABELS[type] || type)}</option>
        `).join('');
        elements.emailServiceType.value = types.includes(selectedType) ? selectedType : 'auto';
    }

    function renderServiceOptions(selectedId) {
        const selectedType = elements.emailServiceType.value || 'auto';
        const services = (state.emailServiceCatalog || {})[selectedType] || [];

        if (selectedType === 'auto') {
            elements.emailServiceIdWrap.style.display = 'none';
            elements.emailServiceHint.textContent = '自动按邮箱域名和服务优先级复用程序中的邮箱服务。';
            elements.emailServiceId.innerHTML = '<option value="">自动选择</option>';
            return;
        }

        elements.emailServiceIdWrap.style.display = '';
        if (!services.length) {
            elements.emailServiceHint.textContent = '当前类型没有可用服务，请先到邮箱服务页面启用对应服务。';
            elements.emailServiceId.innerHTML = '<option value="">暂无可用服务</option>';
            elements.emailServiceId.value = '';
            return;
        }

        elements.emailServiceId.innerHTML = services.map((service) => `
            <option value="${escapeHtml(service.id ?? '')}">${escapeHtml(service.label)}</option>
        `).join('');
        const normalizedSelectedId = String(selectedId ?? '');
        const availableIds = services.map((service) => String(service.id ?? ''));
        elements.emailServiceId.value = availableIds.includes(normalizedSelectedId) ? normalizedSelectedId : availableIds[0];

        const active = services.find((service) => String(service.id ?? '') === elements.emailServiceId.value);
        elements.emailServiceHint.textContent = active?.description
            ? `当前服务域名: ${active.description}`
            : '当前类型将复用程序中已配置的邮箱服务。';
    }

    app.emailServices = {
        async syncFromConfig(config) {
            if (!state.emailServiceCatalog) {
                const payload = await app.api.loadAvailableEmailServices();
                state.emailServiceCatalog = flattenAvailableServices(payload);
            }
            renderTypeOptions(config.email_service_type || 'auto');
            renderServiceOptions(config.email_service_id);
        },

        getSelectedServiceId() {
            const selectedType = elements.emailServiceType.value || 'auto';
            if (selectedType === 'auto') {
                return null;
            }
            const text = String(elements.emailServiceId.value || '').trim();
            return text ? parseInt(text, 10) : null;
        },

        bindEvents() {
            elements.emailServiceType.addEventListener('change', () => renderServiceOptions(null));
            elements.emailServiceId.addEventListener('change', () => renderServiceOptions(elements.emailServiceId.value || null));
        },
    };
})();
