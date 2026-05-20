-- ===================================================================
-- 2026-05-14: 引入 format（数据格式）维度
-- ===================================================================
-- 现状：同 project 同 kind（attendance 等）下，所有规则（classify/enterprise/
-- attribution/validity/pivot）按 (project_id, kind) 共享一份。这导致同项目
-- 计时考勤 vs 计件考勤、横向 vs 纵向考勤 等不同数据结构无法独立配置。
--
-- 引入 format：一个 format = 一种数据结构（如"计时考勤"/"计件考勤"），全套
-- 规则按 format 隔离。一个 classify rule = 一个 format 的入口。
--
-- 兼容策略：
--   - projects.format_mode=0 → 老逻辑（按 project_id+kind 共享）
--   - projects.format_mode=1 → 新逻辑（规则带 format_id；查询 WHERE format_id=?）
--   - 所有规则表加 format_id INT NULL（NULL = 老规则，对所有 format 生效；
--     非 NULL = 仅对该 format 生效）
-- ===================================================================

-- 1. 新表：project_formats
CREATE TABLE project_formats (
    id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    project_id BIGINT NOT NULL,
    target_kind VARCHAR(16) NOT NULL COMMENT 'attendance/bill/wage_sheet/payroll',
    name VARCHAR(128) NOT NULL COMMENT '用户起的名字，如"白班计时考勤"',
    handler VARCHAR(64) NOT NULL DEFAULT 'standard'
        COMMENT '冗余存：与对应 classify rule 的 handler 一致，UI 直接读',
    is_default TINYINT NOT NULL DEFAULT 0 COMMENT '本 kind 下默认（用户切回老模式时迁移的占位）',
    status VARCHAR(16) NOT NULL DEFAULT 'active' COMMENT 'active/archived',
    note VARCHAR(255),
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_proj_kind_name (project_id, target_kind, name),
    KEY idx_proj_kind (project_id, target_kind, status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
  COMMENT='项目级 format（数据结构）：一个 format = 一种数据结构的全套配置容器';

-- 2. projects 加 format_mode 开关
ALTER TABLE projects ADD COLUMN format_mode TINYINT NOT NULL DEFAULT 0
    COMMENT '0=老模式（规则按 project+kind 共享）；1=format 模式（规则按 format_id 隔离）';

-- 3. 5 张规则表加 format_id（NULL = 兼容老规则）
ALTER TABLE project_classify_rules ADD COLUMN format_id BIGINT NULL
    COMMENT 'format 模式下指向 project_formats.id；NULL = 老规则（对所有 format 生效）';
ALTER TABLE project_classify_rules ADD KEY idx_format (project_id, format_id);

ALTER TABLE project_enterprise_rules ADD COLUMN format_id BIGINT NULL;
ALTER TABLE project_enterprise_rules ADD KEY idx_format (project_id, format_id);

ALTER TABLE project_validity_rules ADD COLUMN format_id BIGINT NULL;
ALTER TABLE project_validity_rules ADD KEY idx_format (project_id, format_id);

ALTER TABLE project_attribution_rules ADD COLUMN format_id BIGINT NULL;
ALTER TABLE project_attribution_rules ADD KEY idx_format (project_id, format_id);

ALTER TABLE project_pivot_templates ADD COLUMN format_id BIGINT NULL;
ALTER TABLE project_pivot_templates ADD KEY idx_format (project_id, format_id);

ALTER TABLE project_payroll_biz_date_rules ADD COLUMN format_id BIGINT NULL;
ALTER TABLE project_payroll_biz_date_rules ADD KEY idx_format (project_id, format_id);
