-- ===================================================================
-- 2026-05-10 (b)：bill_persons / wage_sheets / payrolls 加 validity + extra_data 字段
-- ===================================================================
-- 配合 standard handler 端到端：rows 出来除了 mart 主字段，还有 extra_data + is_valid
-- attendance/attendance_summary 已在 20260509b 加好；这次补齐其他 3 个 kind
-- ===================================================================

ALTER TABLE bill_persons
  ADD COLUMN is_valid TINYINT NOT NULL DEFAULT 1
    COMMENT '1=有效 / 0=被 validity 规则过滤',
  ADD COLUMN invalid_reason VARCHAR(255) NULL,
  ADD COLUMN extra_data JSON NULL
    COMMENT '未映射列归此（key=文件列名 value=单元格值）';
ALTER TABLE bill_persons ADD KEY idx_proj_month_valid (project_id, business_month, is_valid);

ALTER TABLE wage_sheets
  ADD COLUMN is_valid TINYINT NOT NULL DEFAULT 1,
  ADD COLUMN invalid_reason VARCHAR(255) NULL,
  ADD COLUMN extra_data JSON NULL;
ALTER TABLE wage_sheets ADD KEY idx_proj_month_valid (project_id, business_month, is_valid);

ALTER TABLE payrolls
  ADD COLUMN is_valid TINYINT NOT NULL DEFAULT 1,
  ADD COLUMN invalid_reason VARCHAR(255) NULL,
  ADD COLUMN extra_data JSON NULL;
ALTER TABLE payrolls ADD KEY idx_proj_month_valid (project_id, business_month, is_valid);
