-- ===================================================================
-- 2026-05-19: project_formats 增加 is_xiaoyu_payroll 标志
-- ===================================================================
-- 用途：标识"此 format 为小鱼系统发薪格式 = DB 流直接装入 mart.payrolls"
-- 影响：
--   - UI: classify_payroll 页"文件类型归属"/"列映射" 自动禁用
--   - dispatcher: 命中该 format 的 xlsx 跳过装入（仅 DB 流装入）
--   - std_payrolls.standardize: 按 8 字段精简映射，不塞冗余 extra_data
-- ===================================================================

ALTER TABLE project_formats
    ADD COLUMN is_xiaoyu_payroll TINYINT(1) NOT NULL DEFAULT 0
    COMMENT '1=小鱼系统发薪格式（DB→mart 直映，无需配置特征列/列映射）';
