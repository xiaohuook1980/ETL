-- ===================================================================
-- 2026-05-15: 聚合标签账单规则
-- ===================================================================
-- 场景：账单文件总金额不来自按人累加，而是文件里某个固定位置的"汇总值"
--       （如义乌众新汇总表底 r127 "含税合计 × 应付合计列 = ¥839,153.37"）。
--
-- 定位算法（每条规则）：
--   1) 限定到 sheet（子串匹配 sheet_pattern，空=所有 sheet）
--   2) 找含 col_name 的表头 cell → 确定列号
--   3) 找含 label 的 cell → 确定行号
--   4) 取 (行, 列) 交叉 cell 的数字
--
-- 落点：bill_totals 一行（source_type='aggregate_label'）+ 0 bill_persons
--       不动 bill_totals/bill_persons schema
-- ===================================================================

CREATE TABLE project_aggregate_label_rules (
    id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    project_id BIGINT NOT NULL,
    format_id BIGINT NULL COMMENT 'NULL=对该项目所有 format 生效',
    sheet_pattern VARCHAR(64) NOT NULL DEFAULT '' COMMENT 'sheet 名子串，空=所有 sheet',
    label VARCHAR(64) NOT NULL COMMENT '行定位标签词，子串匹配 cell',
    col_name VARCHAR(64) NOT NULL COMMENT '列定位列名，子串匹配表头 cell',
    priority INT NOT NULL DEFAULT 100,
    enabled TINYINT NOT NULL DEFAULT 1,
    note VARCHAR(255),
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    KEY idx_proj_fmt (project_id, format_id, enabled)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
  COMMENT='聚合标签账单规则：按 sheet+行标签+列名 二维定位金额';
