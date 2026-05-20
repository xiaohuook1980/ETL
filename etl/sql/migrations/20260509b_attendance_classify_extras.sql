-- ===================================================================
-- 2026-05-09 (b)：mart attendance 字段补齐 + 新建 project_validity_rules
-- ===================================================================
-- 配合 sheet 分类规则配置化：
--   1. attendance / attendance_summary 加 4 字段（备注 / 账单内考勤标记 / 有效性 / 过滤原因）
--      注：worker_class 已存在（varchar(16)），跳过
--   2. project_validity_rules：行级过滤规则（合计行剔除 / 工时>0 / 排除某工种 等）
-- ===================================================================

-- ===== mart.attendance =====
ALTER TABLE attendance
  ADD COLUMN extra_data JSON NULL
    COMMENT '未映射列归此（key=文件列名 value=单元格值）'
    AFTER id_card_raw,
  ADD COLUMN from_bill TINYINT NOT NULL DEFAULT 0
    COMMENT '0=独立考勤文件 / 1=从账单文件解析出的考勤'
    AFTER source_type,
  ADD COLUMN is_valid TINYINT NOT NULL DEFAULT 1
    COMMENT '1=有效 / 0=被数据有效性规则过滤（保留供审计但 calc 不参与）'
    AFTER from_bill,
  ADD COLUMN invalid_reason VARCHAR(255) NULL
    COMMENT '过滤原因（哪条 validity 规则命中）'
    AFTER is_valid;

ALTER TABLE attendance ADD KEY idx_proj_month_valid (project_id, business_month, is_valid);

-- ===== mart.attendance_summary =====
ALTER TABLE attendance_summary
  ADD COLUMN extra_data JSON NULL
    COMMENT '未映射列归此'
    AFTER id_card_raw,
  ADD COLUMN from_bill TINYINT NOT NULL DEFAULT 0
    COMMENT '0=独立考勤文件 / 1=从账单文件解析'
    AFTER source_type,
  ADD COLUMN is_valid TINYINT NOT NULL DEFAULT 1
    COMMENT '1=有效 / 0=被数据有效性规则过滤'
    AFTER from_bill,
  ADD COLUMN invalid_reason VARCHAR(255) NULL
    AFTER is_valid;

ALTER TABLE attendance_summary ADD KEY idx_proj_month_valid (project_id, business_month, is_valid);

-- ===== project_validity_rules：数据有效性规则 =====
-- 行级过滤：每条规则独立判断，命中规则的行 → is_valid=0 + invalid_reason=规则 note
-- 字段语义：
--   feature_columns: AND 必含特征列（feature_enabled=0 时忽略，对所有 sheet 生效）
--   filter_column:   过滤列（在哪一列查/比较）
--   mode:            include / exclude / gt / lt / eq
--   filter_value:    include/exclude 时关键词（逗号分隔）；gt/lt/eq 时数值
CREATE TABLE project_validity_rules (
  id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  project_id BIGINT NOT NULL,
  target_kind VARCHAR(16) NOT NULL                      COMMENT 'attendance / bill / wage_sheet / payroll',
  priority SMALLINT NOT NULL DEFAULT 100,
  feature_columns JSON                                  COMMENT '特征列 AND 必含',
  feature_enabled TINYINT NOT NULL DEFAULT 0            COMMENT '0=不参与匹配（对所有 sheet）/ 1=须命中特征列',
  filter_column VARCHAR(64)                             COMMENT '过滤列（值/数值在哪一列）',
  filter_enabled TINYINT NOT NULL DEFAULT 1             COMMENT '0=不参与（仅靠 feature 决定）/ 1=须比较 filter_column',
  mode VARCHAR(8) NOT NULL                              COMMENT 'include / exclude / gt / lt / eq',
  filter_value VARCHAR(255)                             COMMENT 'include/exclude 时关键词（逗号分隔）；gt/lt/eq 时数值',
  is_builtin TINYINT NOT NULL DEFAULT 0                 COMMENT '1=系统内置（如合计行剔除）不可删，可扩展关键词',
  enabled TINYINT NOT NULL DEFAULT 1,
  match_count INT NOT NULL DEFAULT 0,
  last_matched_at DATETIME,
  note VARCHAR(255)                                     COMMENT '说明，如"合计行剔除"',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  KEY idx_proj_kind (project_id, target_kind, enabled, priority)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
  COMMENT='项目级行级数据有效性规则（合计行剔除 / 工时>0 等）';
