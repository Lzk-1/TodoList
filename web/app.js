/**
 * 待办事项追踪 —— 前端逻辑
 * 实现：树形视图 / 内联编辑 / 筛选搜索 / 统计看板 / 深色模式
 */

(function () {
    'use strict';

    // ---------- 常量映射 ----------
    const STATUS_LABEL = {
        pending: '待开始',
        in_progress: '进行中',
        done: '已完成',
        blocked: '阻塞',
    };
    const PRIORITY_LABEL = {
        urgent: '紧急',
        high: '高',
        medium: '中',
        low: '低',
    };
    const PRIORITY_ORDER = { urgent: 4, high: 3, medium: 2, low: 1 };

    // ---------- 状态 ----------
    let allItems = [];          // 全量数据（未归档）
    let archivedItems = [];     // 归档数据
    let filteredItems = [];    // 当前展示数据
    let expandedIds = new Set(); // 展开的节点
    let editingId = null;      // 当前编辑的 id（null 表示新建）
    let currentView = 'list';   // 当前视图
    let calCursor = new Date(); // 日历游标
    let selectedArchiveIds = new Set(); // 归档视图选中的事项

    // ---------- DOM 引用 ----------
    const $ = (id) => document.getElementById(id);
    const tree = $('tree');
    const statsCard = $('statsCard');
    const filterOwnerSel = $('filterOwner');

    // ==========================================================================
    // API 调用
    // ==========================================================================
    async function api(method, path, body) {
        const opt = { method, headers: {} };
        if (body !== undefined) {
            opt.headers['Content-Type'] = 'application/json';
            opt.body = JSON.stringify(body);
        }
        const resp = await fetch(path, opt);
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        return resp.json();
    }

    // ==========================================================================
    // Toast 提示
    // ==========================================================================
    let toastTimer = null;
    function toast(msg, type) {
        const el = $('toast');
        el.textContent = msg;
        el.className = 'toast' + (type ? ' ' + type : '');
        clearTimeout(toastTimer);
        toastTimer = setTimeout(() => el.classList.add('hidden'), 2200);
    }

    // ==========================================================================
    // 工具函数
    // ==========================================================================
    function escapeHtml(s) {
        if (s == null) return '';
        return String(s)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;')
            .replace(/>/g, '&gt;').replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    function formatDate(iso) {
        if (!iso) return '';
        // 兼容完整 ISO 与日期 only
        const d = iso.length <= 10 ? iso : iso.slice(0, 10);
        return d;
    }

    function isOverdue(item) {
        if (item.status === 'done' || !item.plan_end) return false;
        const today = new Date().toISOString().slice(0, 10);
        return (item.plan_end.length <= 10 ? item.plan_end : item.plan_end.slice(0, 10)) < today;
    }

    function todayStr() {
        return new Date().toISOString().slice(0, 10);
    }

    // 由扁平列表构建子项索引
    function buildChildIndex(items) {
        const map = {};
        for (const it of items) {
            const pid = it.parent_id || '__root__';
            (map[pid] = map[pid] || []).push(it);
        }
        return map;
    }

    // 获取节点所有后代 id（用于搜索/筛选时确定是否保留父项）
    function getDescendants(items, id) {
        const result = [];
        const queue = [id];
        while (queue.length) {
            const cur = queue.shift();
            for (const it of items) {
                if (it.parent_id === cur) {
                    result.push(it.id);
                    queue.push(it.id);
                }
            }
        }
        return result;
    }

    // ==========================================================================
    // 数据加载
    // ==========================================================================
    async function loadItems() {
        try {
            allItems = await api('GET', '/api/items');
            refreshOwnerFilter();
            applyFilters();
            loadStats();
            // 归档视图处于激活状态时同步刷新
            if (currentView === 'archive') renderArchive();
        } catch (e) {
            toast('加载失败：' + e.message, 'error');
        }
    }

    async function loadArchived() {
        try {
            archivedItems = await api('GET', '/api/items/archived');
            renderArchive();
        } catch (e) {
            toast('归档数据加载失败：' + e.message, 'error');
        }
    }

    async function loadStats() {
        try {
            const s = await api('GET', '/api/stats');
            renderStats(s);
        } catch (e) {
            console.error(e);
        }
    }

    function refreshOwnerFilter() {
        const owners = [...new Set(allItems.map(i => i.owner).filter(Boolean))].sort();
        const cur = filterOwnerSel.value;
        filterOwnerSel.innerHTML = '<option value="">全部</option>' +
            owners.map(o => `<option value="${escapeHtml(o)}">${escapeHtml(o)}</option>`).join('');
        if (cur) filterOwnerSel.value = cur;
    }

    // ==========================================================================
    // 筛选与搜索
    // ==========================================================================
    function applyFilters() {
        const kw = $('searchInput').value.trim().toLowerCase();
        const fStatus = $('filterStatus').value;
        const fPriority = $('filterPriority').value;
        const fOwner = filterOwnerSel.value;
        const fOverdue = $('filterOverdue').checked;

        const matched = new Set();
        const keepParent = new Set();

        // 第一轮：找出直接命中的项
        for (const it of allItems) {
            let ok = true;
            if (fStatus && it.status !== fStatus) ok = false;
            if (fPriority && it.priority !== fPriority) ok = false;
            if (fOwner && it.owner !== fOwner) ok = false;
            if (fOverdue && !isOverdue(it)) ok = false;
            if (kw) {
                const text = (it.title + ' ' + it.owner + ' ' + (it.remark || '') +
                    (it.tags || []).join(' ')).toLowerCase();
                if (!text.includes(kw)) ok = false;
            }
            if (ok) {
                matched.add(it.id);
                // 标记所有祖先需要保留
                let pid = it.parent_id;
                while (pid) {
                    keepParent.add(pid);
                    const parent = allItems.find(x => x.id === pid);
                    pid = parent ? parent.parent_id : null;
                }
            }
        }

        // 第二轮：保留命中项 + 其所有祖先；同时保留命中项的后代（树完整性）
        filteredItems = allItems.filter(it =>
            matched.has(it.id) || keepParent.has(it.id)
        );

        // 无筛选条件时直接全量
        if (!kw && !fStatus && !fPriority && !fOwner && !fOverdue) {
            filteredItems = allItems.slice();
        }

        renderTree();
    }

    // ==========================================================================
    // 渲染：统计看板
    // ==========================================================================
    function renderStats(s) {
        const today = todayStr();
        const overdueCount = allItems.filter(i => isOverdue(i)).length;

        statsCard.innerHTML = `
            <div class="stats-title">整体进度</div>
            <div class="stats-rate">${s.completion_rate}<span class="unit">%</span></div>
            <div class="stats-grid">
                <div class="stats-item">
                    <div class="stats-item-num">${s.total}</div>
                    <div class="stats-item-label">总数</div>
                </div>
                <div class="stats-item">
                    <div class="stats-item-num">${s.by_status.in_progress}</div>
                    <div class="stats-item-label">进行中</div>
                </div>
                <div class="stats-item">
                    <div class="stats-item-num">${s.by_status.done}</div>
                    <div class="stats-item-label">已完成</div>
                </div>
                <div class="stats-item">
                    <div class="stats-item-num">${overdueCount}</div>
                    <div class="stats-item-label">逾期</div>
                </div>
            </div>
        `;
    }

    // ==========================================================================
    // 渲染：事项树
    // ==========================================================================
    function renderTree() {
        // 视图分发：根据 currentView 调用对应渲染器
        if (currentView === 'board') return renderBoard();
        if (currentView === 'gantt') return renderGantt();
        if (currentView === 'calendar') return renderCalendar();
        if (currentView === 'archive') return renderArchive();

        // 默认列表视图
        if (filteredItems.length === 0) {
            tree.innerHTML = `
                <div class="empty-state">
                    <div class="empty-icon">📝</div>
                    <p>暂无匹配事项</p>
                </div>`;
            return;
        }

        const childMap = buildChildIndex(filteredItems);
        const roots = filteredItems
            .filter(i => !i.parent_id || !filteredItems.find(x => x.id === i.parent_id))
            .sort((a, b) => (a.sort_order || 0) - (b.sort_order || 0)
                          || PRIORITY_ORDER[b.priority] - PRIORITY_ORDER[a.priority]);

        const html = roots.map(r => renderNode(r, childMap, 0)).join('');
        tree.innerHTML = html;
        bindNodeEvents();
        bindDragEvents();
    }

    function renderNode(item, childMap, depth) {
        const children = childMap[item.id] || [];
        const hasChildren = children.length > 0;
        const expanded = expandedIds.has(item.id);
        const overdue = isOverdue(item);

        const childrenHtml = hasChildren && expanded
            ? `<div class="tree-children">
                ${children
                    .sort((a, b) => (a.sort_order || 0) - (b.sort_order || 0)
                                  || PRIORITY_ORDER[b.priority] - PRIORITY_ORDER[a.priority])
                    .map(c => renderNode(c, childMap, depth + 1)).join('')}
               </div>`
            : '';

        return `
        <div class="tree-node" data-id="${escapeHtml(item.id)}">
            <div class="tree-node-row ${item.status === 'done' ? 'done' : ''}" data-dragrow="${escapeHtml(item.id)}">
                <span class="drag-handle" data-drag="${escapeHtml(item.id)}" title="拖拽排序">⋮⋮</span>
                <span class="toggle-btn ${hasChildren ? '' : 'empty'} ${expanded ? 'expanded' : ''}"
                      data-toggle="${escapeHtml(item.id)}">
                    <svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                        <polyline points="6 4 10 8 6 12"></polyline>
                    </svg>
                </span>
                <span class="priority-bar ${item.priority}"></span>
                <span class="node-title" data-edit="${escapeHtml(item.id)}">${escapeHtml(item.title)}</span>
                ${item.owner ? `<span class="node-owner">👤 ${escapeHtml(item.owner)}</span>` : ''}
                ${item.tags && item.tags.length
                    ? `<span class="node-tags">${item.tags.map(t => `<span class="tag">${escapeHtml(t)}</span>`).join('')}</span>`
                    : ''}
                ${item.plan_end
                    ? `<span class="node-date ${overdue ? 'overdue' : ''}">📅 ${formatDate(item.plan_end)}</span>`
                    : ''}
                <span class="progress-bar"><span class="progress-fill ${item.status === 'done' ? 'done' : ''}"
                       style="width:${item.progress || 0}%"></span></span>
                <span class="status-tag ${item.status}">${STATUS_LABEL[item.status]}</span>
                ${item.depends_on && item.depends_on.length
                    ? `<span class="node-owner" title="依赖 ${item.depends_on.length} 项">🔗${item.depends_on.length}</span>` : ''}
                ${item.comments && item.comments.length
                    ? `<span class="node-owner" title="${item.comments.length} 条评论">💬${item.comments.length}</span>` : ''}
                <span class="node-actions">
                    <button class="node-action-btn" data-addchild="${escapeHtml(item.id)}" title="添加子项">➕</button>
                    <button class="node-action-btn" data-edit="${escapeHtml(item.id)}" title="编辑">✏️</button>
                    <button class="node-action-btn" data-archive="${escapeHtml(item.id)}" title="归档（不限状态）">📦</button>
                    <button class="node-action-btn danger" data-del="${escapeHtml(item.id)}" title="删除">🗑</button>
                </span>
            </div>
            ${childrenHtml}
        </div>`;
    }

    // ==========================================================================
    // 事件绑定：树节点
    // ==========================================================================
    function bindNodeEvents() {
        // 展开/折叠
        tree.querySelectorAll('[data-toggle]').forEach(el => {
            el.addEventListener('click', (e) => {
                e.stopPropagation();
                const id = el.dataset.toggle;
                if (expandedIds.has(id)) expandedIds.delete(id);
                else expandedIds.add(id);
                renderTree();
            });
        });

        // 标题点击 → 编辑
        tree.querySelectorAll('.node-title[data-edit]').forEach(el => {
            el.addEventListener('click', () => openModal(el.dataset.edit));
        });

        // 编辑按钮
        tree.querySelectorAll('[data-edit]:not(.node-title)').forEach(el => {
            if (el.dataset.edit) {
                el.addEventListener('click', () => openModal(el.dataset.edit));
            }
        });

        // 添加子项
        tree.querySelectorAll('[data-addchild]').forEach(el => {
            el.addEventListener('click', () => openModal(null, el.dataset.addchild));
        });

        // 归档
        tree.querySelectorAll('[data-archive]').forEach(el => {
            el.addEventListener('click', () => archiveItem(el.dataset.archive));
        });

        // 删除
        tree.querySelectorAll('[data-del]').forEach(el => {
            el.addEventListener('click', async () => {
                const id = el.dataset.del;
                if (!confirm('确定删除该事项及其所有子项？')) return;
                try {
                    await api('DELETE', '/api/items/' + id);
                    toast('已删除', 'success');
                    await loadItems();
                } catch (e) {
                    toast('删除失败：' + e.message, 'error');
                }
            });
        });
    }

    // ==========================================================================
    // 弹窗：新建/编辑
    // ==========================================================================
    function openModal(id, parentId) {
        editingId = id || null;
        const item = id ? allItems.find(i => i.id === id) : null;

        $('modalTitle').textContent = id ? '编辑事项' : (parentId ? '新建子事项' : '新建事项');
        $('fieldId').value = id || '';
        $('fieldParentId').value = parentId || '';
        $('fieldTitle').value = item ? item.title : '';
        $('fieldOwner').value = item ? (item.owner || '') : '';
        $('fieldStatus').value = item ? item.status : 'pending';
        $('fieldPriority').value = item ? item.priority : 'medium';
        $('fieldProgress').value = item ? (item.progress || 0) : 0;
        $('fieldPlanEnd').value = item ? formatDate(item.plan_end) : '';
        $('fieldActualEnd').value = item ? formatDate(item.actual_end) : '';
        $('fieldTags').value = item && item.tags ? item.tags.join(', ') : '';
        $('fieldRemark').value = item ? (item.remark || '') : '';

        // 依赖选择器
        refreshDependsOptions(id || '', (item && item.depends_on) || []);

        // 评论区（仅编辑时显示）
        const cmtSec = $('commentSection');
        if (id && item) {
            cmtSec.style.display = '';
            renderComments(item);
        } else {
            cmtSec.style.display = 'none';
        }
        $('commentAuthor').value = '';
        $('commentText').value = '';

        $('modalOverlay').classList.remove('hidden');
        setTimeout(() => $('fieldTitle').focus(), 50);
    }

    function closeModal() {
        $('modalOverlay').classList.add('hidden');
        editingId = null;
    }

    async function submitForm(e) {
        e.preventDefault();
        const payload = {
            title: $('fieldTitle').value,
            owner: $('fieldOwner').value,
            status: $('fieldStatus').value,
            priority: $('fieldPriority').value,
            progress: parseInt($('fieldProgress').value, 10) || 0,
            plan_end: $('fieldPlanEnd').value || null,
            actual_end: $('fieldActualEnd').value || null,
            tags: $('fieldTags').value.split(',').map(s => s.trim()).filter(Boolean),
            remark: $('fieldRemark').value,
            depends_on: Array.from($('fieldDepends').selectedOptions).map(o => o.value),
        };
        const parentId = $('fieldParentId').value;
        if (parentId) payload.parent_id = parentId;

        const id = $('fieldId').value;
        try {
            if (id) {
                await api('PUT', '/api/items/' + id, payload);
                toast('已更新', 'success');
            } else {
                await api('POST', '/api/items', payload);
                toast('已创建', 'success');
                // 新建后展开父项
                if (parentId) expandedIds.add(parentId);
            }
            closeModal();
            await loadItems();
        } catch (e) {
            toast('保存失败：' + e.message, 'error');
        }
    }

    // ==========================================================================
    // 深色模式
    // ==========================================================================
    function initTheme() {
        const saved = localStorage.getItem('todo-theme') || 'light';
        applyTheme(saved);
    }
    function applyTheme(theme) {
        document.documentElement.setAttribute('data-theme', theme);
        $('themeToggle').textContent = theme === 'dark' ? '☀️' : '🌙';
    }
    function toggleTheme() {
        const cur = document.documentElement.getAttribute('data-theme') || 'light';
        const next = cur === 'dark' ? 'light' : 'dark';
        localStorage.setItem('todo-theme', next);
        applyTheme(next);
    }

    // ==========================================================================
    // 展开/折叠全部
    // ==========================================================================
    function expandAll() {
        for (const it of filteredItems) {
            const hasChildren = filteredItems.some(x => x.parent_id === it.id);
            if (hasChildren) expandedIds.add(it.id);
        }
        renderTree();
    }
    function collapseAll() {
        expandedIds.clear();
        renderTree();
    }

    // ==========================================================================
    // 导出
    // ==========================================================================
    async function exportData() {
        try {
            const data = await api('GET', '/api/export');
            const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = 'todolist-' + todayStr() + '.json';
            a.click();
            URL.revokeObjectURL(url);
            toast('已导出', 'success');
        } catch (e) {
            toast('导出失败：' + e.message, 'error');
        }
    }

    // ==========================================================================
    // 视图切换
    // ==========================================================================
    function switchView(view) {
        currentView = view;
        document.querySelectorAll('.view-tab').forEach(b => {
            b.classList.toggle('active', b.dataset.view === view);
        });
        document.querySelectorAll('.view-pane').forEach(p => p.classList.remove('active'));
        const pane = $('view' + view.charAt(0).toUpperCase() + view.slice(1));
        if (pane) pane.classList.add('active');
        // 列表工具栏只在列表视图显示
        $('listToolbar').style.display = view === 'list' ? '' : 'none';
        // 归档视图切换时加载数据
        if (view === 'archive') {
            selectedArchiveIds.clear();
            loadArchived();
            return;
        }
        renderTree();
        // 切换视图时隐藏浮动 tooltip
        const tip = document.getElementById('ganttTip');
        if (tip) tip.classList.remove('show');
    }

    // ==========================================================================
    // 看板视图
    // ==========================================================================
    const BOARD_STATUSES = ['pending', 'in_progress', 'blocked', 'done'];

    function renderBoard() {
        const board = $('board');
        if (filteredItems.length === 0) {
            board.innerHTML = '<div class="empty-state"><div class="empty-icon">📐</div><p>暂无匹配事项</p></div>';
            return;
        }
        const groups = {};
        for (const s of BOARD_STATUSES) groups[s] = [];
        for (const it of filteredItems) {
            const key = it.status || 'pending';
            (groups[key] = groups[key] || []).push(it);
        }
        board.innerHTML = BOARD_STATUSES.map(s => {
            const items = groups[s] || [];
            const cards = items.map(it => renderBoardCard(it)).join('');
            return `
                <div class="board-column" data-status="${s}">
                    <div class="board-column-header">
                        <span>${STATUS_LABEL[s]}</span>
                        <span class="board-column-count">${items.length}</span>
                    </div>
                    <div class="board-cards" data-droptarget="${s}">
                        ${cards}
                    </div>
                </div>`;
        }).join('');
        bindBoardEvents();
    }

    function renderBoardCard(it) {
        const overdue = isOverdue(it);
        const owner = it.owner ? `👤 ${escapeHtml(it.owner)}` : '';
        const date = it.plan_end ? `📅 ${formatDate(it.plan_end)}` : '';
        const tags = (it.tags || []).map(t => `<span class="tag">${escapeHtml(t)}</span>`).join('');
        return `
            <div class="board-card" draggable="true" data-card-id="${escapeHtml(it.id)}">
                <span class="priority-bar ${it.priority}"></span>
                <div class="board-card-title">${escapeHtml(it.title)}</div>
                <div class="board-card-meta">
                    ${owner}
                    ${date}
                    ${overdue ? '<span class="node-date overdue">逾期</span>' : ''}
                    ${tags}
                </div>
                <span class="progress-bar"><span class="progress-fill ${it.status === 'done' ? 'done' : ''}"
                      style="width:${it.progress || 0}%"></span></span>
            </div>`;
    }

    function bindBoardEvents() {
        // 卡片拖拽改状态
        let dragId = null;
        document.querySelectorAll('.board-card').forEach(card => {
            card.addEventListener('dragstart', () => {
                dragId = card.dataset.cardId;
                card.classList.add('dragging');
            });
            card.addEventListener('dragend', () => {
                card.classList.remove('dragging');
                dragId = null;
            });
            card.addEventListener('click', () => openModal(card.dataset.cardId));
        });
        document.querySelectorAll('[data-droptarget]').forEach(col => {
            col.addEventListener('dragover', (e) => {
                e.preventDefault();
                col.classList.add('drop-target');
            });
            col.addEventListener('dragleave', () => col.classList.remove('drop-target'));
            col.addEventListener('drop', async (e) => {
                e.preventDefault();
                col.classList.remove('drop-target');
                const newStatus = col.dataset.droptarget;
                if (!dragId) return;
                try {
                    await api('PUT', '/api/items/' + dragId, { status: newStatus });
                    toast('状态已更新', 'success');
                    await loadItems();
                } catch (e) {
                    toast('更新失败：' + e.message, 'error');
                }
            });
        });
    }

    // ==========================================================================
    // 甘特图视图
    // ==========================================================================
    function renderGantt() {
        const gantt = $('gantt');
        // 取有 plan_end 的叶子项（避免父项重复占位）
        const tasks = filteredItems.filter(it => it.plan_end);

        if (tasks.length === 0) {
            gantt.innerHTML = '<div class="gantt-empty">暂无带计划完成时间的事项，请在事项中填入"计划完成时间"</div>';
            return;
        }

        // 计算日期范围：从最早 plan_end 到最晚 plan_end（+3 天余量）
        const days = tasks.map(it => it.plan_end.slice(0, 10));
        let minD = days.reduce((a, b) => a < b ? a : b);
        let maxD = days.reduce((a, b) => a > b ? a : b);
        // 起点前移 2 天，终点后推 3 天，留视觉空间
        const start = new Date(minD); start.setDate(start.getDate() - 2);
        const end = new Date(maxD); end.setDate(end.getDate() + 3);

        // 生成日期列
        const dateCols = [];
        const cur = new Date(start);
        while (cur <= end) {
            dateCols.push(new Date(cur));
            cur.setDate(cur.getDate() + 1);
        }

        // 表头
        const headHtml = '<tr><th class="gantt-task-col">事项</th>' +
            dateCols.map(d => {
                const weekend = (d.getDay() === 0 || d.getDay() === 6) ? 'gantt-weekend' : '';
                return `<th class="${weekend}">${d.getMonth() + 1}/${d.getDate()}</th>`;
            }).join('') + '</tr>';

        // 行
        const rowsHtml = tasks.map(it => {
            const taskDate = new Date(it.plan_end.slice(0, 10));
            const colIdx = Math.round((taskDate - start) / 86400000);
            const barCls = isOverdue(it) ? 'overdue'
                         : it.status === 'done' ? 'done'
                         : it.status === 'blocked' ? 'blocked' : '';
            const cellPre = dateCols.slice(0, colIdx).map(d => {
                const weekend = (d.getDay() === 0 || d.getDay() === 6) ? 'gantt-weekend' : '';
                return `<td class="${weekend}"></td>`;
            }).join('');
            const cellPost = dateCols.slice(colIdx + 1).map(d => {
                const weekend = (d.getDay() === 0 || d.getDay() === 6) ? 'gantt-weekend' : '';
                return `<td class="${weekend}"></td>`;
            }).join('');
            return `<tr>
                <td class="gantt-task-col"><span class="gantt-tooltip" data-title="${escapeHtml(it.title)}">${escapeHtml(it.title)}</span></td>
                ${cellPre}
                <td class="gantt-bar-cell">
                    <div class="gantt-bar ${barCls} gantt-tooltip" data-gantt-id="${escapeHtml(it.id)}" data-title="${escapeHtml(it.title)}">
                        ${escapeHtml(it.title.slice(0, 12))}
                    </div>
                </td>
                ${cellPost}
            </tr>`;
        }).join('');

        gantt.innerHTML = `<table class="gantt-table">${headHtml}${rowsHtml}</table>`;
        gantt.querySelectorAll('[data-gantt-id]').forEach(el => {
            el.addEventListener('click', () => openModal(el.dataset.ganttId));
        });

        // 自定义浮动 tooltip（避免被 overflow: hidden 裁剪）
        if (!gantt._tipBound) {
            gantt._tipBound = true;
            const tip = document.getElementById('ganttTip') || (() => {
                const el = document.createElement('div');
                el.id = 'ganttTip';
                el.className = 'gantt-float-tip';
                document.body.appendChild(el);
                return el;
            })();
            gantt.addEventListener('mouseenter', () => { tip._on = true; }, {passive:true});
            gantt.addEventListener('mouseleave', () => { tip._on = false; tip.classList.remove('show'); }, {passive:true});
            gantt.addEventListener('mousemove', (e) => {
                const el = e.target.closest('.gantt-tooltip');
                if (el && tip._on) {
                    const text = el.dataset.title || el.title;
                    if (tip.textContent !== text) tip.textContent = text;
                    tip.classList.add('show');
                    let x = e.clientX + 14, y = e.clientY - 8;
                    const tw = tip.offsetWidth;
                    if (x + tw > window.innerWidth - 10) x = e.clientX - tw - 14;
                    tip.style.left = x + 'px';
                    tip.style.top = y + 'px';
                } else {
                    tip.classList.remove('show');
                }
            }, {passive:true});
        }
    }

    // ==========================================================================
    // 日历视图
    // ==========================================================================
    function renderCalendar() {
        const cal = $('calendar');
        const year = calCursor.getFullYear();
        const month = calCursor.getMonth();
        $('calTitle').textContent = `${year}年${month + 1}月`;

        const first = new Date(year, month, 1);
        const startDay = first.getDay(); // 0=周日
        const daysInMonth = new Date(year, month + 1, 0).getDate();

        const cells = [];
        // 前置补齐（上月）
        for (let i = 0; i < startDay; i++) {
            const d = new Date(year, month, -startDay + 1 + i);
            cells.push({ date: d, other: true });
        }
        // 当月
        for (let i = 1; i <= daysInMonth; i++) {
            cells.push({ date: new Date(year, month, i), other: false });
        }
        // 后置补齐到 6 行（42 个格子）
        while (cells.length < 42) {
            const last = cells[cells.length - 1].date;
            const d = new Date(last); d.setDate(d.getDate() + 1);
            cells.push({ date: d, other: true });
        }

        // 按日期索引事项
        const evMap = {};
        for (const it of filteredItems) {
            if (!it.plan_end) continue;
            const key = it.plan_end.slice(0, 10);
            (evMap[key] = evMap[key] || []).push(it);
        }

        const today = todayStr();
        const dayNames = ['日', '一', '二', '三', '四', '五', '六'];
        const headHtml = dayNames.map((n, i) => {
            const weekend = (i === 0 || i === 6) ? ' weekend' : '';
            return `<div class="cal-day-name${weekend}">${n}</div>`;
        }).join('');
        const bodyHtml = cells.map(c => {
            const key = c.date.toISOString().slice(0, 10);
            const events = evMap[key] || [];
            const cls = [];
            if (c.other) cls.push('other-month');
            if (key === today) cls.push('today');
            const evHtml = events.slice(0, 3).map(it => {
                const ec = it.status === 'done' ? 'done'
                         : it.status === 'blocked' ? 'blocked'
                         : isOverdue(it) ? 'overdue'
                         : it.status === 'pending' ? 'pending' : '';
                return `<div class="cal-event ${ec}" data-cal-id="${escapeHtml(it.id)}" title="${escapeHtml(it.title)}">${escapeHtml(it.title)}</div>`;
            }).join('');
            const more = events.length > 3 ? `<div class="cal-event-more">+${events.length - 3} 更多</div>` : '';
            return `<div class="cal-day ${cls.join(' ')}" data-cal-date="${key}">
                <div class="cal-day-num">${c.date.getDate()}</div>
                ${evHtml}${more}
            </div>`;
        }).join('');

        cal.innerHTML = headHtml + bodyHtml;
        cal.querySelectorAll('[data-cal-id]').forEach(el => {
            el.addEventListener('click', (e) => {
                e.stopPropagation();
                openModal(el.dataset.calId);
            });
        });
    }

    // ==========================================================================
    // 列表内拖拽排序
    // ==========================================================================
    let dragRowId = null;
    let pendingDragRow = null;   // mousedown 时记录，dragstart 才正式启用
    function bindDragEvents() {
        document.querySelectorAll('[data-drag]').forEach(handle => {
            handle.addEventListener('mousedown', (e) => {
                e.stopPropagation();
                const row = handle.closest('.tree-node-row');
                pendingDragRow = row;
                dragRowId = handle.dataset.drag;
                row.classList.add('dragging');
                row.draggable = true;
                row.addEventListener('dragstart', onDragStart);
                row.addEventListener('dragend', onDragEnd);
                // 安全网：mouseup/mouseleave 若没触发 dragstart 就清掉
                const cleanup = () => {
                    if (pendingDragRow === row) {
                        row.classList.remove('dragging');
                        row.draggable = false;
                        row.removeEventListener('dragstart', onDragStart);
                        row.removeEventListener('dragend', onDragEnd);
                        pendingDragRow = null;
                        dragRowId = null;
                    }
                    row.removeEventListener('mouseup', cleanup);
                    row.removeEventListener('mouseleave', cleanup);
                };
                row.addEventListener('mouseup', cleanup);
                row.addEventListener('mouseleave', cleanup);
            });
        });
        document.querySelectorAll('[data-dragrow]').forEach(row => {
            row.addEventListener('dragover', onDragOver);
            row.addEventListener('drop', onDrop);
            row.addEventListener('dragleave', onDragLeave);
        });
        // 拖到 tree 空白区域 = 移到根级末尾
        tree.addEventListener('dragover', (e) => {
            e.preventDefault();
            e.dataTransfer.dropEffect = 'move';
            tree.classList.add('drop-inside');
        });
        tree.addEventListener('dragleave', (e) => {
            // 只在真正离开 tree 时清，避免子元素间移动时误清
            const related = e.relatedTarget;
            if (!related || !tree.contains(related)) {
                tree.classList.remove('drop-inside');
            }
        });
        tree.addEventListener('drop', async (e) => {
            // drop 在子行已被 onDrop 的 stopPropagation 拦截，到这里一定是空白区
            e.preventDefault();
            tree.classList.remove('drop-inside');
            if (!dragRowId) return;
            const dragItem = allItems.find(i => i.id === dragRowId);
            if (!dragItem) return;
            const newParentId = null;
            if (dragItem.parent_id === newParentId) {
                toast('该项已在根级', 'error');
                return;
            }
            const siblings = allItems
                .filter(i => (i.parent_id || null) === newParentId && i.id !== dragRowId)
                .sort((a, b) => (a.sort_order || 0) - (b.sort_order || 0));
            const orders = [{ id: dragRowId, sort_order: siblings.length, parent_id: newParentId }];
            try {
                await api('PUT', '/api/reorder', { orders });
                await loadItems();
                toast('已移到根级', 'success');
            } catch (err) {
                toast('操作失败：' + err.message, 'error');
            }
        });
    }
    function onDragStart(e) {
        e.dataTransfer.effectAllowed = 'move';
        e.dataTransfer.setData('text/plain', dragRowId || '');
        pendingDragRow = null; // 已进入拖拽，撤销 mouseup 安全网
    }
    function onDragEnd(e) {
        const row = e.currentTarget;
        row.classList.remove('dragging');
        row.draggable = false;
        row.removeEventListener('dragstart', onDragStart);
        row.removeEventListener('dragend', onDragEnd);
        document.querySelectorAll('.drop-before,.drop-after,.drop-inside')
            .forEach(el => el.classList.remove('drop-before', 'drop-after', 'drop-inside'));
        dragRowId = null;
        pendingDragRow = null;
    }
    function onDragOver(e) {
        e.preventDefault();
        e.stopPropagation(); // 阻止冒泡到 tree 容器，避免双重高亮
        e.dataTransfer.dropEffect = 'move';
        const row = e.currentTarget;
        if (row.dataset.dragrow === dragRowId) return;
        const rect = row.getBoundingClientRect();
        const y = e.clientY - rect.top;
        row.classList.remove('drop-before', 'drop-after', 'drop-inside');
        if (y < rect.height * 0.25) row.classList.add('drop-before');
        else if (y > rect.height * 0.75) row.classList.add('drop-after');
        else row.classList.add('drop-inside');
    }
    function onDragLeave(e) {
        e.currentTarget.classList.remove('drop-before', 'drop-after', 'drop-inside');
    }
    async function onDrop(e) {
        e.preventDefault();
        e.stopPropagation(); // 阻止冒泡到 tree 容器
        const row = e.currentTarget;
        const targetId = row.dataset.dragrow;
        if (!dragRowId || !targetId || dragRowId === targetId) return;
        const rect = row.getBoundingClientRect();
        const y = e.clientY - rect.top;
        let pos;
        if (y < rect.height * 0.25) pos = 'before';
        else if (y > rect.height * 0.75) pos = 'after';
        else pos = 'inside';

        const dragItem = allItems.find(i => i.id === dragRowId);
        const targetItem = allItems.find(i => i.id === targetId);
        if (!dragItem || !targetItem) return;

        // 计算新的 parent_id
        const newParentId = pos === 'inside' ? targetItem.id : targetItem.parent_id;

        // 循环依赖检测：禁止把节点移到自己或自己的后代下
        if (newParentId) {
            const desc = new Set(getDescendants(allItems, dragRowId));
            if (desc.has(newParentId) || newParentId === dragRowId) {
                toast('不能移到自己的后代下，会形成循环', 'error');
                return;
            }
        }

        // 计算 sort_order
        const siblings = allItems
            .filter(i => (i.parent_id || null) === (newParentId || null) && i.id !== dragRowId)
            .sort((a, b) => (a.sort_order || 0) - (b.sort_order || 0));

        let orders = [];
        if (pos === 'before') {
            const idx = siblings.findIndex(i => i.id === targetId);
            for (let i = idx; i < siblings.length; i++) {
                orders.push({ id: siblings[i].id, sort_order: i + 1, parent_id: newParentId });
            }
            orders.push({ id: dragRowId, sort_order: idx, parent_id: newParentId });
        } else if (pos === 'after') {
            const idx = siblings.findIndex(i => i.id === targetId);
            for (let i = idx + 1; i < siblings.length; i++) {
                orders.push({ id: siblings[i].id, sort_order: i + 1, parent_id: newParentId });
            }
            orders.push({ id: dragRowId, sort_order: idx + 1, parent_id: newParentId });
        } else {
            orders.push({ id: dragRowId, sort_order: siblings.length, parent_id: newParentId });
        }

        try {
            await api('PUT', '/api/reorder', { orders });
            await loadItems();
            toast('已重新排序', 'success');
        } catch (err) {
            toast('排序失败：' + err.message, 'error');
        }
    }

    // ==========================================================================
    // 依赖选择器填充
    // ==========================================================================
    function refreshDependsOptions(currentId, selected) {
        const sel = $('fieldDepends');
        // 候选项：除了自己和自己的后代（避免循环依赖）
        const forbidden = new Set(getDescendants(allItems, currentId));
        forbidden.add(currentId);
        const opts = allItems
            .filter(i => !forbidden.has(i.id))
            .map(i => ({ id: i.id, title: i.title }));
        sel.innerHTML = opts.map(o =>
            `<option value="${escapeHtml(o.id)}">${escapeHtml(o.title)}</option>`
        ).join('');
        // 选中已依赖项
        (selected || []).forEach(id => {
            const opt = sel.querySelector(`option[value="${id}"]`);
            if (opt) opt.selected = true;
        });
    }

    // ==========================================================================
    // 评论 CRUD
    // ==========================================================================
    function renderComments(item) {
        const list = $('commentList');
        const comments = item.comments || [];
        if (comments.length === 0) {
            list.innerHTML = '<div style="color:var(--text-muted);font-size:12px;padding:6px 0">暂无评论</div>';
            return;
        }
        list.innerHTML = comments.map(c => `
            <div class="comment-item">
                <div class="comment-item-head">
                    <span class="comment-item-author">${escapeHtml(c.author)}</span>
                    <span class="comment-item-time">${c.created_at.slice(0, 16).replace('T', ' ')}</span>
                </div>
                <div class="comment-item-text">${escapeHtml(c.text)}</div>
                <button class="comment-item-del" data-delcomment="${escapeHtml(c.id)}">删除</button>
            </div>
        `).join('');
        list.querySelectorAll('[data-delcomment]').forEach(btn => {
            btn.addEventListener('click', async () => {
                try {
                    await api('DELETE', `/api/items/${item.id}/comments/${btn.dataset.delcomment}`);
                    toast('评论已删除', 'success');
                    // 更新本地数据并重渲染
                    item.comments = item.comments.filter(c => c.id !== btn.dataset.delcomment);
                    renderComments(item);
                } catch (e) {
                    toast('删除失败：' + e.message, 'error');
                }
            });
        });
    }

    async function addComment() {
        const author = $('commentAuthor').value.trim();
        const text = $('commentText').value.trim();
        if (!text) { toast('请输入评论内容', 'error'); return; }
        if (!editingId) return;
        try {
            const c = await api('POST', `/api/items/${editingId}/comments`, { author, text });
            const item = allItems.find(i => i.id === editingId);
            if (item) {
                item.comments = item.comments || [];
                item.comments.push(c);
            }
            $('commentText').value = '';
            renderComments(item);
        } catch (e) {
            toast('评论失败：' + e.message, 'error');
        }
    }

    // ==========================================================================
    // 归档视图
    // ==========================================================================
    function renderArchive() {
        const list = $('archiveList');
        if (!list) return;

        const kw = $('archiveSearch').value.trim().toLowerCase();
        const fStatus = $('archiveFilterStatus').value;

        let items = archivedItems.slice();
        if (fStatus) items = items.filter(i => i.status === fStatus);
        if (kw) {
            items = items.filter(i => {
                const text = (i.title + ' ' + i.owner + ' ' + (i.remark || '') +
                    (i.tags || []).join(' ')).toLowerCase();
                return text.includes(kw);
            });
        }
        // 归档时间倒序
        items.sort((a, b) => (b.archived_at || '').localeCompare(a.archived_at || ''));

        if (items.length === 0) {
            list.innerHTML = `
                <div class="empty-state">
                    <div class="empty-icon">📦</div>
                    <p>暂无归档事项</p>
                </div>`;
            updateArchiveRestoreBtn();
            return;
        }

        list.innerHTML = items.map(it => {
            const checked = selectedArchiveIds.has(it.id) ? 'checked' : '';
            const overdue = isOverdue(it);
            return `
            <div class="archive-row ${checked ? 'selected' : ''}" data-id="${escapeHtml(it.id)}">
                <input type="checkbox" class="archive-check" data-arcid="${escapeHtml(it.id)}" ${checked}>
                <span class="priority-bar ${it.priority}"></span>
                <span class="node-title">${escapeHtml(it.title)}</span>
                ${it.owner ? `<span class="node-owner">👤 ${escapeHtml(it.owner)}</span>` : ''}
                ${it.tags && it.tags.length
                    ? `<span class="node-tags">${it.tags.map(t => `<span class="tag">${escapeHtml(t)}</span>`).join('')}</span>`
                    : ''}
                <span class="status-tag ${it.status}">${STATUS_LABEL[it.status]}</span>
                ${it.plan_end
                    ? `<span class="node-date ${overdue ? 'overdue' : ''}">📅 ${formatDate(it.plan_end)}</span>`
                    : ''}
                <span class="node-date">归档于 ${formatDate(it.archived_at)}</span>
                <span class="node-actions">
                    <button class="node-action-btn" data-restore="${escapeHtml(it.id)}" title="恢复">↩️</button>
                    <button class="node-action-btn danger" data-del="${escapeHtml(it.id)}" title="永久删除">🗑</button>
                </span>
            </div>`;
        }).join('');

        bindArchiveEvents();
        updateArchiveRestoreBtn();
    }

    function updateArchiveRestoreBtn() {
        const btn = $('archiveRestoreBtn');
        if (btn) btn.disabled = selectedArchiveIds.size === 0;
    }

    function bindArchiveEvents() {
        const list = $('archiveList');
        list.querySelectorAll('.archive-check').forEach(cb => {
            cb.addEventListener('change', () => {
                const id = cb.dataset.arcid;
                if (cb.checked) selectedArchiveIds.add(id);
                else selectedArchiveIds.delete(id);
                cb.closest('.archive-row').classList.toggle('selected', cb.checked);
                updateArchiveRestoreBtn();
            });
        });
        list.querySelectorAll('[data-restore]').forEach(btn => {
            btn.addEventListener('click', async () => {
                const id = btn.dataset.restore;
                try {
                    await api('POST', `/api/items/${id}/unarchive`);
                    toast('已恢复', 'success');
                    await loadArchived();
                    await loadItems();
                } catch (e) {
                    toast('恢复失败：' + e.message, 'error');
                }
            });
        });
        list.querySelectorAll('[data-del]').forEach(btn => {
            btn.addEventListener('click', async () => {
                const id = btn.dataset.del;
                if (!confirm('永久删除该归档事项？此操作不可撤销。')) return;
                try {
                    await api('DELETE', '/api/items/' + id);
                    toast('已永久删除', 'success');
                    selectedArchiveIds.delete(id);
                    await loadArchived();
                } catch (e) {
                    toast('删除失败：' + e.message, 'error');
                }
            });
        });
    }

    async function restoreSelected() {
        const ids = Array.from(selectedArchiveIds);
        if (!ids.length) return;
        let okN = 0;
        for (const id of ids) {
            try {
                await api('POST', `/api/items/${id}/unarchive`);
                okN++;
            } catch (e) { /* 静默跳过 */ }
        }
        selectedArchiveIds.clear();
        toast(`已恢复 ${okN} 条`, 'success');
        await loadArchived();
        await loadItems();
    }

    // 主视图行操作：手动归档单条（不限状态）
    async function archiveItem(id) {
        try {
            await api('POST', `/api/items/${id}/archive`);
            toast('已归档', 'success');
            await loadItems();
        } catch (e) {
            toast('归档失败：' + e.message, 'error');
        }
    }

    // ==========================================================================
    // 初始化
    // ==========================================================================
    function init() {
        initTheme();

        $('addItemBtn').addEventListener('click', () => openModal(null));
        $('modalClose').addEventListener('click', closeModal);
        $('modalCancel').addEventListener('click', closeModal);
        $('itemForm').addEventListener('submit', submitForm);
        $('modalOverlay').addEventListener('click', (e) => {
            if (e.target.id === 'modalOverlay') closeModal();
        });

        $('themeToggle').addEventListener('click', toggleTheme);
        $('exportBtn').addEventListener('click', exportData);

        $('searchInput').addEventListener('input', debounce(applyFilters, 200));
        $('filterStatus').addEventListener('change', applyFilters);
        $('filterPriority').addEventListener('change', applyFilters);
        filterOwnerSel.addEventListener('change', applyFilters);
        $('filterOverdue').addEventListener('change', applyFilters);
        $('resetFilter').addEventListener('click', () => {
            $('searchInput').value = '';
            $('filterStatus').value = '';
            $('filterPriority').value = '';
            filterOwnerSel.value = '';
            $('filterOverdue').checked = false;
            applyFilters();
        });

        $('expandAllBtn').addEventListener('click', expandAll);
        $('collapseAllBtn').addEventListener('click', collapseAll);

        // 视图切换 Tab
        document.querySelectorAll('.view-tab').forEach(tab => {
            tab.addEventListener('click', () => switchView(tab.dataset.view));
        });

        // 归档视图事件
        $('archiveSearch').addEventListener('input', debounce(renderArchive, 200));
        $('archiveFilterStatus').addEventListener('change', renderArchive);
        $('archiveRestoreBtn').addEventListener('click', restoreSelected);

        // 日历视图按钮
        $('calPrev').addEventListener('click', () => {
            calCursor.setMonth(calCursor.getMonth() - 1);
            renderCalendar();
        });
        $('calNext').addEventListener('click', () => {
            calCursor.setMonth(calCursor.getMonth() + 1);
            renderCalendar();
        });
        $('calToday').addEventListener('click', () => {
            calCursor = new Date();
            renderCalendar();
        });

        // 评论发送
        $('commentAdd').addEventListener('click', addComment);
        $('commentText').addEventListener('keydown', (e) => {
            if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); addComment(); }
        });

        // ESC 关闭弹窗
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape') closeModal();
        });

        loadItems();
    }

    function debounce(fn, ms) {
        let t;
        return function (...args) {
            clearTimeout(t);
            t = setTimeout(() => fn.apply(this, args), ms);
        };
    }

    document.addEventListener('DOMContentLoaded', init);
})();
