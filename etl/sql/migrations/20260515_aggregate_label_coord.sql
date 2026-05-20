-- ===================================================================
-- 2026-05-15: 聚合标签账单规则 - 加坐标定位
-- ===================================================================
-- 现有定位方式：sheet_pattern + label + col_name (二维标签定位)
-- 新增定位方式：sheet_pattern + cell_ref (Excel 标准 cell 引用如 "N3")
--
-- 适用场景：账单文件汇总值在固定位置且无同行标签词
--   (如盛宏乔安「工资报表」N3 = 2,063,190.74 "实发工资"
--    label "实发工资" 在 N2 表头，不跟值同行 → 标签定位走不通)
--
-- 一条规则填法二选一：
--   A. 标签定位: 填 label + col_name (旧)
--   B. 坐标定位: 填 cell_ref="N3" (新)
-- ===================================================================

ALTER TABLE project_aggregate_label_rules
    MODIFY COLUMN label VARCHAR(64) NOT NULL DEFAULT '' COMMENT '行定位标签词 (子串匹配 cell); 走坐标定位时留空',
    MODIFY COLUMN col_name VARCHAR(64) NOT NULL DEFAULT '' COMMENT '列定位列名 (子串匹配表头); 走坐标定位时留空',
    ADD COLUMN cell_ref VARCHAR(16) NULL COMMENT 'Excel 单元格引用如 "N3"; 与 label+col_name 二选一';
