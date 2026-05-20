-- ===================================================================
-- 2026-05-10 (c)：project_validity_rules.mode VARCHAR(8) → VARCHAR(16)
-- ===================================================================
-- 加 not_empty / empty 模式时 8 字符不够（not_empty 9 字符），扩到 16
-- ===================================================================
ALTER TABLE project_validity_rules MODIFY COLUMN mode VARCHAR(16) NOT NULL
  COMMENT 'include / exclude / gt / lt / eq / not_empty / empty';
