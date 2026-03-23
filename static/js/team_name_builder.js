(function () {
    const WORD_BANK_LABELS = {
        prefix: '前缀词',
        core: '核心词',
        suffix: '后缀词',
        collective: '尾词',
    };

    const DEFAULT_BUCKETS = ['prefix', 'core', 'suffix', 'collective'];
    const WORD_BANKS = {
        prefix: ['Nova', 'Atlas', 'Lumen', 'Orbit', 'Signal', 'Prism', 'Cedar', 'Nimbus', 'Summit', 'Cobalt', 'Velvet', 'Solar', 'Echo', 'Turbo', 'Pixel', 'Astra'],
        core: ['Forge', 'Circuit', 'Harbor', 'Studio', 'Beacon', 'Horizon', 'Canvas', 'Bridge', 'Anchor', 'Vector', 'Sprint', 'Pilot', 'Nest', 'Ledger', 'Matrix', 'Engine'],
        suffix: ['Lab', 'Works', 'Cloud', 'Hub', 'Guild', 'Stack', 'Flow', 'Grid', 'Point', 'Base', 'Crew', 'Field', 'Scope', 'Wave', 'Pulse', 'Space'],
        collective: ['Prime', 'One', 'Core', 'Deck', 'Loop', 'Line', 'Node', 'Nest', 'Dock', 'Ring', 'Peak', 'Sync'],
    };

    const elements = {
        workspaceName: document.getElementById('workspace-name'),
        wordCount: document.getElementById('team-name-word-count'),
        parts: document.getElementById('team-name-parts'),
        rerollBtn: document.getElementById('team-name-reroll-btn'),
        resetBtn: document.getElementById('team-name-reset-btn'),
    };

    let state = [];

    function randomWord(bucket) {
        const list = WORD_BANKS[bucket] || WORD_BANKS.core;
        return list[Math.floor(Math.random() * list.length)];
    }

    function normalizeWord(value) {
        return String(value || '')
            .trim()
            .replace(/\s+/g, '-')
            .replace(/[!"#$%&'()*+,./:;<=>?@[\\\]^`{|}~]+/g, '')
            .replace(/^[-_]+|[-_]+$/g, '')
            .slice(0, 15);
    }

    function getBuckets(wordCount) {
        const count = Math.min(Math.max(parseInt(wordCount, 10) || 3, 2), 4);
        return DEFAULT_BUCKETS.slice(0, count);
    }

    function createRandomPart(bucket) {
        return {
            bucket,
            mode: 'random',
            value: randomWord(bucket),
        };
    }

    function ensureState(wordCount) {
        const buckets = getBuckets(wordCount);
        state = buckets.map((bucket, index) => {
            const previous = state[index];
            if (!previous) {
                return createRandomPart(bucket);
            }
            if (previous.mode === 'custom') {
                return {
                    bucket,
                    mode: 'custom',
                    value: normalizeWord(previous.value),
                };
            }
            return {
                bucket,
                mode: 'random',
                value: previous.bucket === bucket && previous.value ? previous.value : randomWord(bucket),
            };
        });
        return state;
    }

    function buildName() {
        const words = state
            .map((item) => normalizeWord(item.value))
            .filter(Boolean);
        return words.join(' ') || 'MyTeam';
    }

    function syncWorkspaceName() {
        if (!elements.workspaceName) {
            return;
        }
        elements.workspaceName.value = buildName();
    }

    function render() {
        if (!elements.parts) {
            return;
        }
        ensureState(elements.wordCount?.value);
        elements.parts.innerHTML = state.map((part, index) => `
            <div class="team-name-part" data-name-index="${index}">
                <div class="team-name-part-top">
                    <span class="team-name-part-title">第 ${index + 1} 词</span>
                    <span class="team-name-part-bucket">${WORD_BANK_LABELS[part.bucket] || part.bucket}</span>
                </div>
                <div class="team-name-part-controls">
                    <select data-name-mode>
                        <option value="random" ${part.mode === 'random' ? 'selected' : ''}>随机词</option>
                        <option value="custom" ${part.mode === 'custom' ? 'selected' : ''}>指定词</option>
                    </select>
                    <input
                        data-name-value
                        type="text"
                        maxlength="15"
                        value="${escapeHtml(part.value)}"
                        ${part.mode === 'random' ? 'readonly' : ''}
                        placeholder="输入这个词"
                    >
                    <button class="btn btn-ghost btn-sm" data-name-reroll type="button" ${part.mode === 'custom' ? 'disabled' : ''}>换词</button>
                </div>
                <div class="team-name-preview">当前词位：<code>${escapeHtml(part.value)}</code></div>
            </div>
        `).join('');
        syncWorkspaceName();
    }

    function escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text == null ? '' : String(text);
        return div.innerHTML;
    }

    function rerollAllRandomParts(resetCustom) {
        ensureState(elements.wordCount?.value);
        state = state.map((part) => {
            if (resetCustom || part.mode === 'random') {
                return createRandomPart(part.bucket);
            }
            return {
                ...part,
                value: normalizeWord(part.value),
            };
        });
        if (resetCustom) {
            state = state.map((part) => ({ ...part, mode: 'random' }));
        }
        render();
    }

    function updatePart(index, updater) {
        if (index < 0 || index >= state.length) {
            return;
        }
        state[index] = updater({ ...state[index] });
        render();
    }

    function setFromWorkspaceName(name) {
        const words = String(name || '')
            .trim()
            .split(/\s+/)
            .filter(Boolean)
            .slice(0, 4);
        const wordCount = Math.min(Math.max(words.length || 3, 2), 4);
        if (elements.wordCount) {
            elements.wordCount.value = String(wordCount);
        }
        state = getBuckets(wordCount).map((bucket, index) => {
            const value = normalizeWord(words[index] || '');
            return value
                ? { bucket, mode: 'custom', value }
                : createRandomPart(bucket);
        });
        render();
    }

    function bindEvents() {
        elements.wordCount?.addEventListener('change', render);
        elements.rerollBtn?.addEventListener('click', () => rerollAllRandomParts(false));
        elements.resetBtn?.addEventListener('click', () => rerollAllRandomParts(true));

        elements.parts?.addEventListener('change', (event) => {
            const partElement = event.target.closest('[data-name-index]');
            if (!partElement) {
                return;
            }
            const index = parseInt(partElement.dataset.nameIndex, 10);
            if (event.target.matches('[data-name-mode]')) {
                const mode = event.target.value === 'custom' ? 'custom' : 'random';
                updatePart(index, (part) => ({
                    ...part,
                    mode,
                    value: mode === 'random' ? randomWord(part.bucket) : normalizeWord(part.value),
                }));
                return;
            }
            if (event.target.matches('[data-name-value]')) {
                updatePart(index, (part) => ({
                    ...part,
                    value: normalizeWord(event.target.value) || (part.mode === 'random' ? randomWord(part.bucket) : ''),
                }));
            }
        });

        elements.parts?.addEventListener('input', (event) => {
            const partElement = event.target.closest('[data-name-index]');
            if (!partElement || !event.target.matches('[data-name-value]')) {
                return;
            }
            const index = parseInt(partElement.dataset.nameIndex, 10);
            const normalized = normalizeWord(event.target.value);
            state[index] = {
                ...state[index],
                value: normalized,
            };
            syncWorkspaceName();
            partElement.querySelector('.team-name-preview code').textContent = normalized || '-';
        });

        elements.parts?.addEventListener('click', (event) => {
            const button = event.target.closest('[data-name-reroll]');
            if (!button) {
                return;
            }
            const partElement = button.closest('[data-name-index]');
            const index = parseInt(partElement.dataset.nameIndex, 10);
            updatePart(index, (part) => ({
                ...part,
                mode: 'random',
                value: randomWord(part.bucket),
            }));
        });
    }

    window.TeamNameBuilder = {
        init() {
            if (!elements.workspaceName || !elements.parts) {
                return;
            }
            bindEvents();
            setFromWorkspaceName(elements.workspaceName.value || 'MyTeam');
        },
        getName() {
            return buildName();
        },
        serializeParts() {
            return state.map((part) => ({
                bucket: part.bucket,
                mode: part.mode,
                value: normalizeWord(part.value),
            }));
        },
        setFromWorkspaceName,
    };
})();
