-- 企业归属规则表（dispatcher 层 drop，外企业行不入 mart）
-- 跟 project_validity_rules 语义不同：validity 是入 mart 标 is_valid=0；本表是 drop 不进库
CREATE TABLE IF NOT EXISTS project_enterprise_rules (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    project_id BIGINT NOT NULL,
    target_kind VARCHAR(16) NOT NULL COMMENT 'attendance/bill (本次只接 attendance)',
    priority INT DEFAULT 100,
    filter_column VARCHAR(64) NOT NULL COMMENT '行级特征列名（如 所属服务商）',
    filter_value VARCHAR(512) NOT NULL COMMENT '关键词，逗号分隔多个；mode=include 时命中保留，exclude 时命中丢弃',
    mode VARCHAR(16) DEFAULT 'include' COMMENT 'include=必须含关键词才入库；exclude=含关键词则丢弃',
    enabled TINYINT DEFAULT 1,
    note VARCHAR(255),
    match_count INT DEFAULT 0 COMMENT '近一次命中计数（人工触发）',
    last_matched_at DATETIME,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_proj_kind (project_id, target_kind, enabled)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
