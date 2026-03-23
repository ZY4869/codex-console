(function () {
    const app = window.TeamInviteApp = window.TeamInviteApp || {};

    app.api = {
        loadSources() {
            return window.api.get('/team-invite/sources');
        },
        loadUploadServices() {
            return Promise.all([
                window.api.get('/sub2api-services?enabled=true'),
                window.api.get('/cpa-services?enabled=true'),
                window.api.get('/tm-services?enabled=true'),
                window.api.get('/team/available-email-services'),
            ]);
        },
        loadTask(taskUuid) {
            return window.api.get(`/team-invite/${taskUuid}`);
        },
        loadLogs(taskUuid) {
            return window.api.get(`/team-invite/${taskUuid}/logs`);
        },
        createTask(payload) {
            return window.api.post('/team-invite/create', payload);
        },
        resumeTask(taskUuid, payload) {
            return window.api.post(`/team-invite/${taskUuid}/resume`, payload || {});
        },
        restartTask(taskUuid, payload) {
            return window.api.post(`/team-invite/${taskUuid}/restart`, payload || {});
        },
        cancelTask(taskUuid) {
            return window.api.post(`/team-invite/${taskUuid}/cancel`, {});
        },
        acceptMember(taskUuid, memberId, payload) {
            return window.api.post(`/team-invite/${taskUuid}/members/${memberId}/accept`, payload || {});
        },
        refreshMember(taskUuid, memberId, payload) {
            return window.api.post(`/team-invite/${taskUuid}/members/${memberId}/refresh-team`, payload || {});
        },
        uploadMember(taskUuid, memberId, payload) {
            return window.api.post(`/team-invite/${taskUuid}/members/${memberId}/upload`, payload || {});
        },
        reloginMembers(taskUuid, payload) {
            return window.api.post(`/team-invite/${taskUuid}/members/relogin`, payload || {});
        },
        previewSub2ApiName(serviceId, groupId) {
            return window.api.post('/team-invite/sub2api-name-preview', {
                service_id: serviceId,
                group_id: groupId,
            });
        },
        loadSub2ApiGroups(serviceId) {
            return window.api.post('/sub2api-services/fetch-groups', { service_id: parseInt(serviceId, 10) });
        },
        loadAccountDetail(accountId) {
            return Promise.all([
                window.api.get(`/accounts/${accountId}`),
                window.api.get(`/accounts/${accountId}/tokens`).catch(() => ({})),
            ]);
        },
        startRegistration(payload) {
            return window.api.post('/registration/start', payload);
        },
        loadRegistrationTask(taskUuid) {
            return window.api.get(`/registration/tasks/${taskUuid}`);
        },
    };
})();
