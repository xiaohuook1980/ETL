-- ===================================================================
-- 迁移 2026-05-06：项目归属规则
-- ===================================================================
-- project_attribution_rules：每项目每类每种规则一行（最多 6 行/项目）
-- - category: 考账(kaoqin_bill) / 工资(wage) / 发薪(payroll)
-- - rule_type: sheet / column
-- - sheet 规则：keywords 命中 sheet 名 → 该 sheet 归本项目
-- - column 规则：column_names 任一命中表头 → 该列每行值含 keywords → 该行归本项目
-- ===================================================================

CREATE TABLE project_attribution_rules (
  id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  project_id BIGINT NOT NULL,
  category VARCHAR(16) NOT NULL
    COMMENT '类别：kaoqin_bill / wage / payroll',
  rule_type VARCHAR(8) NOT NULL
    COMMENT '规则类型：sheet / column',
  column_names VARCHAR(255) NOT NULL DEFAULT ''
    COMMENT 'rule_type=column 时填一个列名；sheet 类型保持为空字符串',
  keywords JSON NOT NULL
    COMMENT '关键词数组 ["动物园","动物世界"]；任一命中即匹配，最长优先',
  enabled TINYINT NOT NULL DEFAULT 0
    COMMENT '0=禁用（数据按 raw_files.source_project_ids 归属，即小鱼系统挂载位置即本项目专有）；1=启用此规则参与匹配',
  match_count INT NOT NULL DEFAULT 0
    COMMENT '历史累计匹配次数（统计/调优用）',
  last_matched_at DATETIME
    COMMENT '最近一次命中时间',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uk_proj_cat_type_col (project_id, category, rule_type, column_names),
  KEY idx_category (category, rule_type)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
  COMMENT='项目归属规则（解析考账发工时按本表把 sheet/行装到对应项目）；column 类型可多行';
