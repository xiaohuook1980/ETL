-- 2026-05-17: project_pivot_templates 唯一键加入 format_id
--   原 (project_id, target_kind) → 新 (project_id, target_kind, format_id)
--   format 体系下每个 format 独立 pivot 模板；NULL format_id 兜底（兼容老数据）

ALTER TABLE `fish-test`.`project_pivot_templates`
    DROP INDEX `uk_proj_kind`,
    ADD UNIQUE KEY `uk_proj_kind_format` (`project_id`, `target_kind`, `format_id`);
