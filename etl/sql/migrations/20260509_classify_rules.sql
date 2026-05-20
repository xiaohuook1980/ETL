-- ===================================================================
-- 2026-05-09：项目级 sheet 分类规则配置化
-- ===================================================================
-- 把原 etl/classify.py 硬编码的 30+ 条 sheet kind 识别规则迁到 DB，每项目独立配。
--
-- project_classify_rules：
--   按表头特征列识别 sheet → target_kind（attendance/bill/wage_sheet/payroll）
--   handler='standard' 走配置的 column_mapping；其他走 etl/parsers/handlers/<name>.py
--   match_columns 支持 * 通配（N 个 * = N 字符），如 "*月平日工时" / "**月平日工时"
--
-- pending_classify_sheets：
--   dispatcher 解析时遇到 unknown sheet 入这里 → UI 上展示供用户一键加规则
-- ===================================================================

CREATE TABLE project_classify_rules (
  id              BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  project_id      BIGINT NOT NULL,
  target_kind     VARCHAR(16) NOT NULL
                  COMMENT 'attendance / bill / wage_sheet / payroll',
  priority        SMALLINT NOT NULL DEFAULT 100
                  COMMENT '同 kind 内尝试顺序（小→大）',
  match_columns   JSON NOT NULL
                  COMMENT 'AND 必含列名数组，支持 * 单字符通配（["*月平日工时","姓名"]）',
  match_columns_any JSON
                  COMMENT 'OR 至少含一项（可空）',
  match_excludes  JSON
                  COMMENT 'NOT 任一命中即不算（可空）',
  scan_rows       TINYINT NOT NULL DEFAULT 4
                  COMMENT '扫表头扫前几行（4 / 10）',
  handler         VARCHAR(64) NOT NULL DEFAULT 'standard'
                  COMMENT 'standard 走 column_mapping；其他走 etl/parsers/handlers/<name>.py',
  column_mapping  JSON
                  COMMENT 'handler=standard 时填 {"姓名":"name_raw","税后实际工资":"amount"}；handler!=standard 时为 NULL',
  enabled         TINYINT NOT NULL DEFAULT 1,
  match_count     INT NOT NULL DEFAULT 0,
  last_matched_at DATETIME,
  note            VARCHAR(255),
  created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  KEY idx_proj_kind_priority (project_id, target_kind, priority, enabled)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
  COMMENT='项目级 sheet 分类规则（按表头特征列识别 kind + handler 路由）';

CREATE TABLE pending_classify_sheets (
  id              BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  project_id      BIGINT NOT NULL,
  raw_file_id     BIGINT NOT NULL,
  sheet_name      VARCHAR(255) NOT NULL,
  headers_preview TEXT NOT NULL
                  COMMENT '前 4 行表头拼接，给用户看着配规则',
  status          VARCHAR(16) NOT NULL DEFAULT 'pending'
                  COMMENT 'pending / resolved / ignored',
  first_seen_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  resolved_at     DATETIME,
  resolved_rule_id BIGINT,
  UNIQUE KEY uk_proj_file_sheet (project_id, raw_file_id, sheet_name),
  KEY idx_status (project_id, status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
  COMMENT='待识别 sheet 待办（unknown 入这里 → UI 一键加规则）';
