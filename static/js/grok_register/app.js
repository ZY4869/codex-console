(function () {
    const app = window.GrokRegisterApp = window.GrokRegisterApp || {};

    async function initialize() {
        app.runtime.bindEvents();
        app.taskView.bindEvents();
        app.logs.init();

        try {
            await Promise.all([
                app.runtime.loadConfig(),
                app.runtime.refreshRuntimeStatuses(),
            ]);
        } catch (error) {
            toast.error(error.message || 'Grok 页面初始化失败');
        }

        await app.taskView.restoreTask();
    }

    document.addEventListener('DOMContentLoaded', initialize);
})();
