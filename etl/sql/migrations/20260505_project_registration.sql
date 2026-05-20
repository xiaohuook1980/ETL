-- ===================================================================
-- 迁移 2026-05-05：项目注册功能（3 处改动）
-- ===================================================================
-- 改动 1：projects.daishou_threshold（代收阈值）
-- 改动 2：projects.source_created_at（老库项目创建时间，增量同步用）
-- 改动 3：新建 project_registrations 临时表（DB 合并后废弃）
-- ===================================================================

-- 改动 1
ALTER TABLE projects
  ADD COLUMN daishou_threshold INT NOT NULL DEFAULT 2000
    COMMENT '代收阈值（元），出款计算用'
    AFTER profit_ratio;

-- 改动 2
ALTER TABLE projects
  ADD COLUMN source_created_at DATETIME NULL
    COMMENT '老库 mini_project.created_at；增量同步过滤用'
    AFTER pre_id;

-- 改动 3
CREATE TABLE project_registrations (
  project_id BIGINT NOT NULL PRIMARY KEY,
  status VARCHAR(16) NOT NULL DEFAULT 'unregistered'
    COMMENT 'unregistered / registered / disabled',
  registered_at DATETIME NULL,
  disabled_at DATETIME NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  KEY idx_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
  COMMENT='Web 注册状态（临时表，fish-test/fish-prod 合并后废弃）';
