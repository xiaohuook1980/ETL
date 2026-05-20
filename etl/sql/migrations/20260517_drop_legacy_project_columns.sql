-- 2026-05-17: 删 projects 表 2 个死字段
--   - format_mode: format 强制化（2026-05-15）后恒为 1，代码层已清；
--   - pivot_attendance: pivot 配置已统一通过 format.handler='pivot_attendance' 走，
--     原项目级开关失效，代码层已清。
-- 数据库列删除后无法回滚（数据丢失），但两列内容此时只剩 0/1 标志且已无逻辑读取。

ALTER TABLE `fish-test`.`projects`
    DROP COLUMN `pivot_attendance`,
    DROP COLUMN `format_mode`;
