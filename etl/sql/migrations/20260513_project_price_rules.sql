-- 单价配置 v2：项目级 3 维 AND 匹配 + 关键字 OR + 时段过滤
-- 替代 unit_prices 表（老表保留，calc 不再读）
-- 迁移：unit_prices.area/worker_type/shift_name → dim1/2/3 keywords

-- ============================================================
-- 1. 项目级单价匹配维度配置（每个项目 1 行）
-- ============================================================
CREATE TABLE IF NOT EXISTS `project_price_config` (
  `project_id` bigint(20) NOT NULL,
  `dim1_col_name` varchar(64) NOT NULL DEFAULT '' COMMENT '维度 1 列名（如"部门"），空=未启用',
  `dim2_col_name` varchar(64) NOT NULL DEFAULT '' COMMENT '维度 2 列名（如"班次"），空=未启用',
  `dim3_col_name` varchar(64) NOT NULL DEFAULT '' COMMENT '维度 3 列名（如"职务"），空=未启用',
  `note` varchar(255) DEFAULT NULL,
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`project_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='项目级单价匹配维度配置（3 维列名）';

-- ============================================================
-- 2. 单价规则（每行一条，关键字 OR + 多维 AND + 时段）
-- ============================================================
CREATE TABLE IF NOT EXISTS `project_price_rules` (
  `id` bigint(20) NOT NULL AUTO_INCREMENT,
  `project_id` bigint(20) NOT NULL,
  `dim1_keywords` varchar(255) NOT NULL DEFAULT '' COMMENT '维度 1 关键字（逗号分隔多候选 OR），空=通配',
  `dim2_keywords` varchar(255) NOT NULL DEFAULT '' COMMENT '维度 2 关键字',
  `dim3_keywords` varchar(255) NOT NULL DEFAULT '' COMMENT '维度 3 关键字',
  `price` decimal(8,2) NOT NULL,
  `unit` varchar(16) NOT NULL DEFAULT '元/小时' COMMENT '元/小时 / 元/天 / 元/件 / 元/单',
  `effective_start` date DEFAULT NULL,
  `effective_end` date DEFAULT NULL,
  `priority` int(11) NOT NULL DEFAULT 100 COMMENT '同具体度排序（小优先）',
  `note` varchar(255) DEFAULT NULL,
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_proj` (`project_id`, `priority`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='单价规则（3 维 AND + 逗号 OR + 时段）';

-- ============================================================
-- 3. 迁移 unit_prices → project_price_rules
-- 老 unit_prices 的 area/worker_type/shift_name 直接当 dim1/2/3 keywords
-- 项目级 dim*_col_name 配成默认中文（场地/工种/班次）
-- ============================================================

-- 3a. 每个有 unit_prices 数据的项目 → 插入默认 config
INSERT IGNORE INTO project_price_config (project_id, dim1_col_name, dim2_col_name, dim3_col_name, note)
SELECT DISTINCT project_id, '场地', '工种', '班次',
       '从 unit_prices 自动迁移（默认中文维度名，可改）'
FROM unit_prices;

-- 3b. 每条 unit_prices → 一条 rule
INSERT INTO project_price_rules
  (project_id, dim1_keywords, dim2_keywords, dim3_keywords,
   price, unit, effective_start, effective_end, priority, note)
SELECT project_id, area, worker_type, shift_name,
       price, unit, effective_start, effective_end,
       100,  -- 默认 priority
       CONCAT(IFNULL(note, ''), ' [迁自 unit_prices id=', id, ']')
FROM unit_prices;
