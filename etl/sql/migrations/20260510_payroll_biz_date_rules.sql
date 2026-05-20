-- ===================================================================
-- 2026-05-10：发薪流水业务日期判定规则表
-- ===================================================================
-- payroll 专有，非 4 类通用：每条发薪记录归到哪个业务周期？
--   两套规则配合：先 extract（指定从字段抽日期），抽不到再 infer（从 datetime 减偏移）
--
-- rule_kind:
--   extract  从某列直接抽出业务日期（如备注'2026.4.5借支' → 2026-04）
--   infer    从付款时间减偏移得业务日（如月结 N=1 月，付款时间 2026-5-1 → 业务日 2026-4）
--
-- file_columns: 文件特征列（空格分隔，按顺序连续相邻；空=对所有 payroll 文件生效）
--   字段值含空格用 #$% 替代
--
-- target_columns: extract 时是"提取列"，infer 时是"推断列"，| 分隔多候选
-- ===================================================================

CREATE TABLE project_payroll_biz_date_rules (
  id              BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  project_id      BIGINT NOT NULL,
  rule_kind       VARCHAR(16) NOT NULL                    COMMENT 'extract / infer',
  priority        SMALLINT NOT NULL DEFAULT 100,
  file_columns    VARCHAR(255)                            COMMENT '文件特征列（空格分隔；空=对所有文件生效）',
  target_columns  VARCHAR(255)                            COMMENT 'extract 提取列 / infer 推断列（| 分隔多候选）',
  offset_n        INT DEFAULT 0                           COMMENT 'infer 偏移数；extract 忽略',
  offset_unit     VARCHAR(8) DEFAULT 'day'                COMMENT 'day / month',
  enabled         TINYINT NOT NULL DEFAULT 1,
  match_count     INT NOT NULL DEFAULT 0,
  last_matched_at DATETIME,
  note            VARCHAR(255),
  created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  KEY idx_proj_kind_priority (project_id, rule_kind, priority, enabled)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
  COMMENT='发薪流水业务日期判定规则（extract 优先 / infer 兜底）';
